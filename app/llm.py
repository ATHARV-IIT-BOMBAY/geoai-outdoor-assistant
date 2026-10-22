"""The SLM's two narrow jobs: read the question into fields, and phrase the computed facts. Never the facts themselves."""
import asyncio
import atexit
import json
import os
import re
import threading

from app.parse import PARTS, WEEKDAYS
from app.rules import ACTIVITIES, PROFILES

# Qwen3-1.7B (Apache-2.0, 2.0B params) as Q4_K_M GGUF, mirrored with our model card in MODEL_REPO
MODEL_REPO = os.environ.get("MODEL_REPO", "AtharvIITB/geoai-outdoor-slm")
MODEL_FILE = "Qwen3-1.7B-Q4_K_M.gguf"
MODEL_PATH = os.environ.get("MODEL_PATH", f"models/{MODEL_FILE}")
DAYS = ["unspecified", "today", "tomorrow", "day after tomorrow", "this weekend", "next week", *WEEKDAYS]

# Two contexts over the same weights (memory-mapped, so loaded once): parse and write prompts each stay
# cached in their own KV cache instead of evicting each other on every request.
_llms: dict = {}
_locks = {"parse": threading.Lock(), "write": threading.Lock()}  # a llama.cpp context isn't thread-safe


def model(kind="parse"):
    """Lazy-load once per kind (local file, else the HF repo); None on failure so the API still works on rules alone."""
    if kind not in _llms:
        from llama_cpp import Llama
        # generation threads default to cpus//2 = 1 on the 2-vCPU server; 2 measured ~16% faster
        kw = dict(n_ctx=2048, n_gpu_layers=-1, n_threads=os.cpu_count() or 2, verbose=False)
        try:
            _llms[kind] = (Llama(MODEL_PATH, **kw) if os.path.exists(MODEL_PATH)
                           else Llama.from_pretrained(MODEL_REPO, MODEL_FILE, **kw))
        except Exception as e:  # noqa: BLE001 - offline or bad weights: degrade to rules
            print("model load failed, using rules only:", e)
            _llms[kind] = False
        else:
            atexit.register(_llms[kind].close)  # freeing at interpreter teardown trips a Metal assert
    return _llms[kind] or None


def _chat(messages, max_tokens, schema=None, kind="parse", wait=20.0):
    """One completion, or None if the model is missing or busy for longer than `wait` seconds."""
    m = model(kind)
    if m is None or not _locks[kind].acquire(timeout=wait):
        return None
    try:
        kw = {"response_format": {"type": "json_object", "schema": schema}} if schema else {}
        messages = [*messages[:-1], {**messages[-1], "content": messages[-1]["content"] + " /no_think"}]  # Qwen3
        r = m.create_chat_completion(messages=messages, temperature=0, max_tokens=max_tokens, **kw)
    finally:
        _locks[kind].release()
    return re.sub(r"<think>.*?</think>", "", r["choices"][0]["message"]["content"], flags=re.S).strip()


PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "activity": {"enum": [*ACTIVITIES, "none"]},
        "place": {"type": ["string", "null"]},
        "profiles": {"type": "array", "items": {
            "type": "object", "properties": {"profile": {"enum": list(PROFILES)}, "quote": {"type": "string"}},
            "required": ["profile", "quote"]}},
        "radius_km": {"type": ["number", "null"]},
        "day": {"enum": DAYS},
        "part_of_day": {"enum": ["any", *PARTS]},
    },
    "required": ["activity", "place", "profiles", "radius_km", "day", "part_of_day"],
}
PARSE_PROMPT = (
    "Extract fields from a user's question about an outdoor activity in India. The question may be in English, "
    "Hindi or Hinglish. activity: closest of " + ", ".join(ACTIVITIES) + " ('event' = party/wedding/gathering; "
    "'walking' = walk/stroll), or 'none' if no outdoor activity is asked about. place: the area/city exactly as "
    "written, or null if none is given; never guess one. profiles: asthma (breathing problems), senior "
    "(elderly/parents/grandparents), child (kids), pregnant; only if the question says so, with 'quote' = the exact words that say it, else []. day and part_of_day: as stated ('kal' usually means "
    "tomorrow, 'subah' morning, 'shaam' evening), else 'unspecified'/'any'. Reply with JSON only."
)


async def parse_fields(q: str):
    out = await asyncio.to_thread(_chat, [{"role": "system", "content": PARSE_PROMPT},
                                          {"role": "user", "content": q}], 120, PARSE_SCHEMA, "parse", 20.0)
    if not out:
        return None
    try:
        f = json.loads(out)
    except ValueError:
        return None
    # grounding guard: a place the user never wrote is a hallucination, drop it
    if f.get("place") and not set(WORD.findall(f["place"].lower())) <= set(WORD.findall(q.lower())):
        f["place"] = None
    f["profiles"] = [x["profile"] for x in f["profiles"] if x["quote"].strip() and x["quote"].lower() in q.lower()]
    return f


WRITE_PROMPT = (
    "You are an outdoor-activity assistant. Using only the FACTS, write ONE short plain sentence that answers the QUESTION: "
    "say where and when to go and the main reason, with its numbers. If there is a 'Comparison' line, start with which "
    "day is better. Copy place names, numbers and times exactly as written; add nothing else. Never repeat the "
    "question, never judge safety or describe the place.\n"
    "Example. FACTS: Best option: Joggers' Park (1.2 km away), Fri 23 Oct, 6:00 AM – 8:00 AM. "
    "Why: feels like 27°C, against 34°C at 2:00 PM; 0% rain chance; AQI 85 (Satisfactory).\n"
    "Answer: Go to Joggers' Park on Fri 23 Oct between 6:00 AM and 8:00 AM, when it feels like 27°C instead of 34°C "
    "at 2:00 PM, with 0% rain chance and AQI 85."
)
# claims we never check (opening hours, reputation, scenery): a summary making them falls back to the template
UNVERIFIED = re.compile(r"\b(?:open|opens|closed|popular|famous|beautiful|scenic|stunning|views?|facilit\w*|crowded|"
                        r"safe|safer|unsafe|acceptable|healthy|pleasant|perfect|ideal|great)\b", re.I)
WORD = re.compile(r"\w+")
NUM = re.compile(r"\d+(?:\.\d+)?")
TIME = re.compile(r"\d{1,2}(?::\d{2})?\s?[AP]M", re.I)


def grounded(text: str, facts: str) -> bool:
    """Every number the model wrote must appear in the facts."""
    allowed, times = set(NUM.findall(facts)), {_t(x) for x in TIME.findall(facts)}
    return all(n in allowed for n in NUM.findall(text)) and all(_t(x) in times for x in TIME.findall(text))


def _t(x):  # "7 am" / "7:00 AM" -> "7:00AM"
    x = x.upper().replace(" ", "")
    return x if ":" in x else x[:-2] + ":00" + x[-2:]


def acceptable(out, facts, venue, times, q):
    """The summary may only restate the facts: right venue, a computed time, no new numbers, no judgement words."""
    low = out.lower()
    return (grounded(out, facts) and not UNVERIFIED.search(out) and not low.lstrip().startswith("caution")
            and venue.lower() in low and any(_t(t) in {_t(x) for x in TIME.findall(out)} for t in times)
            and not (len(q) > 12 and low.startswith(q.lower()[:20])))


async def write_answer(q: str, facts: str, venue: str = "", times=()):
    """A one-sentence SLM summary of the computed facts, or None (caution answers, a busy model, or failed checks)."""
    lines = facts.splitlines()
    if any(l.startswith("Caution") for l in lines):  # safety warnings stay deterministic; a 3B model once called AQI 156 "safer"
        return None
    brief = "\n".join(l for l in lines if l.startswith(("Comparison:", "Best option", "Why:")))
    # a busy writer is skipped (the template answer is complete); never queue a request behind it
    out = await asyncio.to_thread(_chat, [{"role": "system", "content": WRITE_PROMPT},
                                          {"role": "user", "content": f"QUESTION: {q}\n\nFACTS:\n{brief}"}],
                                  60, None, "write", 1.0)
    if out:
        out = re.sub(r"(\d)\. (\d)", r"\1.\2", out).strip()  # "3. 8 km" -> "3.8 km"
        if acceptable(out, facts, venue, times, q):
            return out
    return None


if __name__ == "__main__":
    assert grounded("Go at 7:00 AM, feels like 30°C", "7:00 AM ... feels like 30°C")
    assert not grounded("Go at 6:00 AM", "7:00 AM")
    assert UNVERIFIED.search("It's open from 7 AM") and not UNVERIFIED.search("Go at 7 AM, it's cool and dry")
    assert UNVERIFIED.search("MS Building Park is safe for your parents") and UNVERIFIED.search("pleasant weather")
    facts = "Best option: Lohgad (8.6 km away), Sun 25 Oct, 7:00 AM – 11:00 AM.\nWhy: feels like 20°C; 0% rain chance."
    assert acceptable("Go to Lohgad on Sun 25 Oct from 7:00 AM, feels like 20°C.", facts, "Lohgad", ["7:00 AM"], "hike?")
    assert not acceptable("Hike Lohgad day after tomorrow. Go at 7:00 AM.", facts, "Lohgad", ["7:00 AM"],
                          "hike Lohgad day after tomorrow")  # echoes the question, as seen in the 22 Oct eval
    assert not acceptable("Go to Rajmachi at 7:00 AM.", facts, "Lohgad", ["7:00 AM"], "hike?")
    assert not acceptable("Lohgad has pleasant weather at 7:00 AM.", facts, "Lohgad", ["7:00 AM"], "hike?")
    assert grounded("Go at 7 am", "7:00 AM") and not grounded("at 12:00 PM", "12:00 AM, 7:00 PM")
    print("ok")
