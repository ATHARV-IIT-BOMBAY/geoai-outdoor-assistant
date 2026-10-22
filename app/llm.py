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

_llm = None
_lock = threading.Lock()  # llama.cpp contexts aren't thread-safe; one request at a time


def model():
    """Lazy-load once (local file, else from the HF repo); None on failure so the API still works on rules alone."""
    global _llm
    if _llm is None:
        from llama_cpp import Llama
        kw = dict(n_ctx=4096, n_gpu_layers=-1, verbose=False)
        try:
            _llm = (Llama(MODEL_PATH, **kw) if os.path.exists(MODEL_PATH)
                    else Llama.from_pretrained(MODEL_REPO, MODEL_FILE, **kw))
        except Exception as e:  # noqa: BLE001 - offline or bad weights: degrade to rules
            print("model load failed, using rules only:", e)
            _llm = False
        else:
            atexit.register(_llm.close)  # freeing at interpreter teardown trips a Metal assert
    return _llm or None


def _chat(messages, max_tokens, schema=None):
    m = model()
    if m is None:
        return None
    kw = {"response_format": {"type": "json_object", "schema": schema}} if schema else {}
    messages = [*messages[:-1], {**messages[-1], "content": messages[-1]["content"] + " /no_think"}]  # Qwen3
    with _lock:
        r = m.create_chat_completion(messages=messages, temperature=0, max_tokens=max_tokens, **kw)
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
                                          {"role": "user", "content": q}], 120, PARSE_SCHEMA)
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
    "You are an outdoor-activity assistant. Answer the user's QUESTION in 2-3 friendly sentences using only the "
    "FACTS: name the recommended place and its time window and give the main reason. If the facts start with "
    "'Caution', say that first. Copy numbers, times, dates and place names exactly; never add new ones, never describe facilities, and never say a place is open or closed. "
    "Reply in English. Plain text, no headings, no lists."
)
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


async def write_answer(q: str, facts: str):
    """(text, writer): SLM phrasing if it passes the number check, else the template facts."""
    if facts.startswith("Caution"):  # safety warnings stay deterministic; a 3B model once called AQI 156 "safer"
        return facts, "template"
    brief = "\n".join(facts.splitlines()[:3])  # best option, conditions, note: shorter prompt = faster on CPU
    out = await asyncio.to_thread(_chat, [{"role": "system", "content": WRITE_PROMPT},
                                          {"role": "user", "content": f"QUESTION: {q}\n\nFACTS:\n{brief}"}], 100)
    if out and grounded(out, facts):
        out = re.sub(r"(\d)\. (\d)", r"\1.\2", out)  # "3. 8 km" -> "3.8 km"
        return f"{out.strip()}\n\nDetails:\n{facts}", "slm"
    return facts, "template"


if __name__ == "__main__":
    assert grounded("Go at 7:00 AM, feels like 30°C", "7:00 AM ... feels like 30°C")
    assert not grounded("Go at 6:00 AM", "7:00 AM")
    assert grounded("Go at 7 am", "7:00 AM") and not grounded("at 12:00 PM", "12:00 AM, 7:00 PM")
    print("ok")
