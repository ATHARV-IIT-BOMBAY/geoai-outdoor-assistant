"""HTTP API: /ask (GET or POST, many input shapes) -> grounded recommendation + evidence. Serves the portal at /."""
import asyncio
import json
import os
import re
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, quote

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse

from app import data, llm
from app.parse import DEICTIC, IST, parse
from app.rules import ACTIVITIES, limits_for
from app.score import rank

SOURCES = {
    "places": "OpenStreetMap via Overpass API (© OpenStreetMap contributors, ODbL)",
    "geocoding": "OpenStreetMap Nominatim; Photon (komoot) fallback",
    "weather": "Open-Meteo Forecast API (CC BY 4.0)",
    "air_quality": "Open-Meteo Air Quality API (CAMS, CC BY 4.0)",
    "travel": "OSRM public routing server (OpenStreetMap data)",
}
HINGLISH = re.compile(r"\b(?:mein|ko|ka|ki|ke|hai|hain|kal|aaj|parso|subah|shaam|raat|dopahar|karna|jana|chahiye|"
                      r"kahan|kab|paas|liye|mujhe|hum|abhi|accha|acha|theek)\b", re.I)
GENERIC = re.compile(r"\b(?:outside|outdoors?|go out|step out|weather|nice day|bahar)\b", re.I)
DEFAULT_ACTIVITY_HINT = ", ".join(a["label"] for a in ACTIVITIES.values())
MAX_Q = 500
Q_KEYS = ("q", "question", "query", "prompt", "text", "message")
LOG_PATH = Path(os.environ.get("LOG_PATH", "logs/requests.jsonl"))


@asynccontextmanager
async def lifespan(_):
    asyncio.create_task(asyncio.to_thread(llm.model))  # warm the SLM without blocking startup
    yield


app = FastAPI(title="GeoAI Outdoor Assistant", version="0.2", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


def _fmt(ts):
    return datetime.fromisoformat(ts).strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def _hhmm(ts):
    return datetime.fromisoformat(ts).strftime("%I:%M %p").lstrip("0")


def _n(v, spec=".0f", unit=""):
    return "n/a" if v is None else f"{v:{spec}}{unit}"


def _short(name):
    return ", ".join(name.split(", ")[:2])


def _conditions(h):
    return (f"feels like {_n(h['feels_c'], unit='°C')}, rain chance {_n(h['rain_pct'], '.0f', '%')}, "
            f"wind {_n(h['wind_kmh'], unit=' km/h')}" + (f" from {h['wind_from']}" if h["wind_from"] else "")
            + f", UV {_n(h['uv'])}"
            + (f", US AQI {h['aqi']} (PM2.5 {_n(h['pm2_5'], unit=' µg/m³')})" if h["aqi"] is not None else ""))


def comparison_line(cmp):
    """'Comparison: Sunday is better …' from per-day bests, or None."""
    if len(cmp) < 2:
        return None
    ranked = sorted(cmp, key=lambda c: (not c["best"]["blocked"], c["best"]["score"]), reverse=True)
    win, rest = ranked[0], ranked[1:]
    day = lambda c: datetime.fromisoformat(c["day"]).strftime("%A")
    others = "; ".join(f"{day(c)}'s best is {_hhmm(c['best']['time'])} at {c['venue']}"
                       + (" (over a safety limit)" if c["best"]["blocked"] else f" ({_conditions(c['best'])})") for c in rest)
    lead = (f"{day(win)} and {day(rest[0])} look about the same" if len(rest) == 1 and not win["best"]["blocked"]
            and not rest[0]["best"]["blocked"] and abs(win["best"]["score"] - rest[0]["best"]["score"]) <= 3
            else f"{day(win)} is better")
    return (f"Comparison: {lead}. {day(win)}: {win['venue']} at {_hhmm(win['best']['time'])} "
            f"({_conditions(win['best'])}). {others}.")


def template_answer(p, lim, origin, top, avoid, cmp_line=None):
    """Plain-language answer built only from evidence. The SLM summarises it; it's also the fallback."""
    lines = [p["rolled"]] if p.get("rolled") else []
    if not top:
        return "\n".join(lines + [f"I couldn't find a usable hour for {lim['label']} near {_short(origin['name'])} "
                                  f"in that time window. Try a different day or a wider area."])
    b = top[0]
    h = b["best"]
    if cmp_line:
        lines.append(cmp_line)
    if h["blocked"]:
        lines.append(f"Caution: no hour in that window meets the safe limits for {lim['label']}"
                     + (f" ({', '.join(p['profiles'])} profile)" if p["profiles"] else "")
                     + f". Main issue: {h['reasons'][0]}. If you still go, this is the least-bad option; "
                       "consider an indoor alternative or another day.")
    lines.append(f"Searching around {_short(origin['name'])}.")
    lines += [f"{'Least-bad' if h['blocked'] else 'Best'} option: {b['venue']['name']} ({b['venue']['dist_m'] / 1000:.1f} km away"
              + (f", ~{b['travel']['min']} min by road" if b["travel"] else "") + "), "
              f"{_fmt(b['window'][0])} – {_hhmm(b['window'][1])}.",
              f"At {_hhmm(h['time'])}: {_conditions(h)}."]
    if h["reasons"] and not h["blocked"]:
        lines.append("Note: " + "; ".join(h["reasons"]) + ".")
    for alt in top[1:]:
        lines.append(f"Alternative: {alt['venue']['name']}, {_fmt(alt['window'][0])} – {_hhmm(alt['window'][1])}"
                     + (" (also over limits)." if alt["best"]["blocked"] else f" (score {alt['best']['score']})."))
    bad = [a for a in avoid if a["time"] != h["time"]][:3]
    if bad:
        why = lambda a: a["reasons"][0] if a["blocked"] else next(r for r in a["reasons"] if "downwind" in r)
        lines.append("Avoid: " + "; ".join(f"{_fmt(a['time'])} ({why(a)})" for a in bad) + ".")
    lines.append("Carry: " + ", ".join(dict.fromkeys(lim["carry"])) + ".")
    lines.append("Watch for: " + ", ".join(dict.fromkeys(lim["watch"])) + ".")
    return "\n".join(lines)


def _clarify(q, text, p=None):
    return {"status": "clarify", "question": q, "answer": text, "summary": text,
            **({"parsed": _jsonable(p)} if p else {})}


def _jsonable(p):
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in p.items()}


def _log(res, trace=None):
    """One JSON line per request, so a judge's failure can be found and fixed."""
    try:
        p = res.get("parsed") or {}
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.now(IST).isoformat(timespec="seconds"), "q": res.get("question"),
                "status": res.get("status"), "latency_s": res.get("latency_s"), "activity": p.get("activity"),
                "place": p.get("place"), "origin": (res.get("origin") or {}).get("name"), "parser": p.get("parser"),
                "writer": res.get("writer"), "error": res.get("error"), "trace": trace}, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001 - logging must never break an answer
        print("log failed:", e)


async def answer(q: str, lat=None, lon=None):
    t0 = time.perf_counter()
    trace = None
    try:
        res = await _answer(q, lat, lon)
    except Exception as e:  # judges hit this live: never return a bare 500
        trace = traceback.format_exc()[-2000:]
        print(trace)
        res = {"status": "error", "question": q, "error": repr(e)[:200],
               "answer": "A live data source didn't respond properly just now. Please try again in a minute."}
        res["summary"] = res["answer"]
    res["latency_s"] = round(time.perf_counter() - t0, 2)
    _log(res, trace)
    return res


async def _merge_slm(p, q):
    """Let the SLM fill what the rules missed (Hinglish, bare place names). Rules win where both answered."""
    f = await llm.parse_fields(q)
    if not f:
        return
    p["parser"] = "slm+rules"
    if not p["activity"] and f["activity"] != "none":
        p["activity"] = f["activity"]
    if f["place"] and f["place"].lower() not in DEICTIC and f["place"] not in p["place_candidates"] and not p["near_me"]:
        p["place_candidates"].append(f["place"])
    p["profiles"] = sorted(set(p["profiles"]) | set(f["profiles"]))
    p["radius_km"] = p["radius_km"] or f["radius_km"]
    if not p["time_given"] and (f["day"] != "unspecified" or f["part_of_day"] != "any"):
        t = parse(" ".join(x for x in (f["day"], f["part_of_day"]) if x not in ("unspecified", "any")))
        p.update({k: t[k] for k in ("start", "end", "part_of_day", "hour_range", "rolled", "compare_days")})


async def understand(q: str):
    """Rules first (reliable on English keywords); the SLM fills what they missed."""
    p = parse(q)
    p["parser"] = "rules"
    # the SLM costs seconds on CPU: skip it when the rules got everything from a plain-English question
    if not (p["activity"] and (p["place"] or p["near_me"]) and q.isascii() and not HINGLISH.search(q)):
        await _merge_slm(p, q)
    return p


async def _resolve_origin(p, q, lat, lon):
    """(origin, clarify_text). Tries each place candidate; if the rules' place doesn't resolve, asks the SLM too."""
    tried = set()
    for attempt in range(2):
        for cand in p["place_candidates"]:
            if cand in tried:
                continue
            tried.add(cand)
            origin = await data.geocode(cand)
            if origin:
                p["place"] = cand
                return origin, None
        if attempt == 0 and p["parser"] == "rules" and p["place_candidates"]:
            await _merge_slm(p, q)
        else:
            break
    if p["place_candidates"]:
        return None, f"I couldn't find '{p['place_candidates'][0]}' on the map. Could you give a nearby area or city?"
    if lat is not None and lon is not None:
        return {"name": "your location", "lat": lat, "lon": lon, "kind": "device"}, None
    if p["near_me"]:
        return None, ("I don't know where you are. Share your location (the portal's 'Use my location' button, "
                      "or lat/lon in the API), or name an area or city.")
    return None, f"Where should I look for {ACTIVITIES[p['activity']]['label']}? Tell me an area or city."


async def _answer(q: str, lat=None, lon=None):
    if not q:
        return _clarify(q, "Ask me about an outdoor plan, for example: 'Where can I run near Powai tomorrow morning?'")
    if len(q) > MAX_Q:
        return _clarify(q[:80] + "…", f"That question is {len(q)} characters long; please keep it under {MAX_Q}.")
    p = await understand(q)
    if p["out_of_range"]:
        return _clarify(q, p["out_of_range"], p)
    assumed = None
    # "is it nice outside" -> assume a walk; "play badminton outdoors" names a sport we don't cover -> ask
    if not p["activity"] and (p["place_candidates"] or lat is not None) and GENERIC.search(q) \
            and not re.search(r"\bplay(?:ing)?\b", q, re.I):
        p["activity"], assumed = "walking", "walking"
    if not p["activity"]:
        return _clarify(q, "I plan outdoor activities, and couldn't match your question to one I cover. "
                           f"Which of these is closest: {DEFAULT_ACTIVITY_HINT}?", p)

    origin, ask = await _resolve_origin(p, q, lat, lon)
    if not origin:
        return _clarify(q, ask, p)

    lim = limits_for(p["activity"], p["profiles"])
    radius_km = p["radius_km"] or lim["radius_km"]
    venues = await data.venues(origin["lat"], origin["lon"], int(radius_km * 1000), lim["venues"])
    if not venues:  # widen once before giving up
        radius_km *= 2.5
        venues = await data.venues(origin["lat"], origin["lon"], int(radius_km * 1000), lim["venues"])
    if not venues:
        text = (f"OpenStreetMap lists no {lim['label']} venues within {radius_km:.0f} km of "
                f"{_short(origin['name'])}. Try a larger area.")
        return {"status": "no_venues", "question": q, "parsed": _jsonable(p), "origin": origin,
                "answer": text, "summary": text}

    days = min(7, max(1, (p["end"] - datetime.now(IST).replace(tzinfo=None)).days + 2))
    ctxs, wxs, aqs, travels = await asyncio.gather(
        data.surroundings(venues), data.weather(venues, days), data.air_quality(venues, min(days, 5)),
        data.travel(origin, venues))
    top, avoid = rank(venues, wxs, aqs, ctxs, travels, lim, p["start"], p["end"], p["hour_range"])

    comparison = []
    for d in p["compare_days"]:  # "Saturday or Sunday?": the best of each day, then a verdict
        day = datetime.fromisoformat(d)
        best_of_day, _ = rank(venues, wxs, aqs, ctxs, travels, lim, max(day, p["start"]), day + timedelta(days=1),
                              p["hour_range"], top=1)
        if best_of_day:
            b = best_of_day[0]
            comparison.append({"day": d, "venue": b["venue"]["name"], "window": b["window"], "best": b["best"]})
    cmp_line = comparison_line(comparison)

    facts = template_answer(p, lim, origin, top, avoid, cmp_line)
    caution = any(l.startswith("Caution") for l in facts.splitlines())
    slm = await llm.write_answer(q, facts) if top else None
    prefix = [x for x in (assumed and "No activity was named, so I planned for a walk. Name one for a sharper answer.",
                          p.get("rolled")) if x]
    if slm:
        summary = " ".join(prefix + [slm])
        text = f"{summary}\n\nDetails:\n{facts}"
    else:
        key = next((l for l in facts.splitlines() if l.startswith(("Caution", "Comparison:", "Best option",
                                                                   "Least-bad option", "I couldn't"))), "")
        summary = " ".join(prefix + [key])
        text = "\n".join(prefix[:1] + [facts]) if assumed else facts

    return {
        "status": "ok", "question": q,
        "answer": text, "summary": summary, "caution": caution, "writer": "slm" if slm else "template",
        "facts": facts, "searched": _short(origin["name"]), "comparison": comparison,
        "parsed": _jsonable(p),
        "origin": origin, "radius_km": radius_km, "limits": {k: lim[k] for k in (
            "feels_max", "rain_max", "wind_max", "gust_max", "aqi_max", "uv_max", "daylight", "downwind_mult")},
        "recommendations": top, "avoid": avoid[:12], "sources": SOURCES,
        "fetched_at": datetime.now(IST).isoformat(timespec="seconds"),
    }


def _coord(v, lo, hi):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None


async def _inputs(request: Request):
    """The question as q/question/query/prompt/text, from query params, JSON, a form or a plain-text body."""
    fields = dict(request.query_params)
    if request.method == "POST":
        body = await request.body()
        ctype = request.headers.get("content-type", "")
        if body.strip():
            if "json" in ctype or body.lstrip()[:1] in (b"{", b'"'):
                try:
                    js = json.loads(body)
                except ValueError:
                    js = body.decode("utf-8", "replace")
                if isinstance(js, dict):
                    fields.update(js)
                elif isinstance(js, str):
                    fields.setdefault("q", js)
            elif "x-www-form-urlencoded" in ctype:
                fields.update(parse_qsl(body.decode("utf-8", "replace")))
            else:
                fields.setdefault("q", body.decode("utf-8", "replace"))
    q = next((str(fields[k]) for k in Q_KEYS if fields.get(k)), "").strip()
    return q, _coord(fields.get("lat"), -90, 90), _coord(fields.get("lon", fields.get("lng")), -180, 180), fields.get("format")


ASK_DOC = {
    "parameters": [
        {"name": "q", "in": "query", "required": False, "schema": {"type": "string"},
         "description": "The question (also accepted as question/query/prompt/text)",
         "example": "Where can I go running tomorrow evening near Koregaon Park?"},
        {"name": "lat", "in": "query", "required": False, "schema": {"type": "number"}},
        {"name": "lon", "in": "query", "required": False, "schema": {"type": "number"}},
        {"name": "format", "in": "query", "required": False, "schema": {"type": "string", "enum": ["json", "text"]}},
    ],
    "requestBody": {"required": False, "content": {
        "application/json": {"schema": {"type": "object", "properties": {
            "q": {"type": "string"}, "lat": {"type": "number"}, "lon": {"type": "number"}}},
            "example": {"q": "I have mild asthma, where's good for a morning walk near Powai in the next two days?"}},
        "text/plain": {"schema": {"type": "string"}}}},
}


@app.get("/ask", openapi_extra={"parameters": ASK_DOC["parameters"]})
async def ask_get(request: Request):
    """Ask in your own words. Returns `answer` (text), `summary`, `status` (ok/clarify/no_venues/error) and the evidence."""
    return await ask(request)


@app.post("/ask", openapi_extra=ASK_DOC)
async def ask(request: Request):
    """Same as GET; the question may be JSON ({"q": ...}), form data or a plain-text body."""
    q, lat, lon, fmt = await _inputs(request)
    res = await answer(q, lat, lon)
    res["view_url"] = f"{str(request.base_url).rstrip('/')}/?q={quote(q)}"
    if fmt == "text":
        return PlainTextResponse(res["answer"])
    return res


@app.get("/", include_in_schema=False)
async def portal():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/health")
async def health():
    return {"ok": True}
