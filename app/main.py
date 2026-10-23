"""HTTP API: /ask (GET or POST, many input shapes) -> grounded recommendation + evidence. Serves the portal at /."""
import asyncio
import contextvars
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
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from app import data, llm
from app.advice import advice, why as why_line
from app.parse import DEICTIC, IST, parse
from app.rules import ACTIVITIES, limits_for
from app.score import avoid_hours, rank

SOURCES = {
    "places": "OpenStreetMap via Overpass API (© OpenStreetMap contributors, ODbL)",
    "geocoding": "OpenStreetMap Nominatim; Photon (komoot) fallback",
    "weather": "Open-Meteo Forecast API (CC BY 4.0)",
    "air_quality": "Open-Meteo Air Quality API (CAMS, CC BY 4.0); AQI on India's NAQI (CPCB) scale",
    "travel": "OSRM public routing server (OpenStreetMap data)",
    "land_cover": "ESA WorldCover 10 m 2021 (CC BY 4.0), read from the public COG on AWS",
    "terrain": "Copernicus DEM GLO-30 (© DLR/Airbus, Copernicus licence), read from the public COG on AWS",
}
HINGLISH = re.compile(r"\b(?:mein|ko|ka|ki|ke|hai|hain|kal|aaj|parso|subah|shaam|raat|dopahar|karna|jana|chahiye|"
                      r"kahan|kab|paas|liye|mujhe|hum|abhi|accha|acha|theek)\b", re.I)
GENERIC = re.compile(r"\b(?:outside|outdoors?|go out|step out|weather|nice day|bahar)\b", re.I)
DEFAULT_ACTIVITY_HINT = ", ".join(a["label"] for a in ACTIVITIES.values())
MAX_Q = 500
Q_KEYS = ("q", "question", "query", "prompt", "text", "message")
LOG_PATH = Path(os.environ.get("LOG_PATH", "logs/requests.jsonl"))
WRITER_BUDGET_S = 20  # a rewrite (ranking changed after the early summary) only if the request is still this young
REQUEST_DEADLINE_S = 75  # a hung public API must not hang the request
ANSWER_TTL_S = 600
_answers: dict = {}
_progress: contextvars.ContextVar = contextvars.ContextVar("progress", default=None)


def _say(msg):
    """Report a finished stage to a streaming client, if this request has one (GET /ask/stream)."""
    q = _progress.get()
    if q is not None:
        q.put_nowait(msg)


@asynccontextmanager
async def lifespan(_):
    for kind in ("parse", "write"):  # warm both SLM contexts without blocking startup
        asyncio.create_task(asyncio.to_thread(llm.model, kind))
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
            + (f", AQI {h['aqi']} ({h['aqi_cat']}; PM2.5 {_n(h['pm2_5'], unit=' µg/m³')})" if h["aqi"] is not None else ""))


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


def build_evidence(p, origin, top):
    """Numbered evidence for the top recommendation: each item is a source, what was read, and a link that
    reproduces it (OSM feature pages, the exact Open-Meteo/OSRM query). Returns (items, refs by name)."""
    items, refs = [], {}
    if not top:
        return items, refs
    b, h = top[0], top[0]["best"]
    v, day = b["venue"], h["time"][:10]

    def add(name, source, what, value, url):
        items.append({"id": len(items) + 1, "source": source, "what": what, "value": value, "url": url})
        refs[name] = len(items)

    if p.get("place"):
        add("origin", "OpenStreetMap Nominatim", "where the search was centred", origin["name"],
            f"https://nominatim.openstreetmap.org/search?q={quote(p['place'])}&countrycodes=in&format=jsonv2")
    add("venue", "OpenStreetMap", "the recommended place", v["name"], f"https://www.openstreetmap.org/{v['osm']}")
    pt = f"latitude={v['lat']:.4f}&longitude={v['lon']:.4f}"
    add("weather", "Open-Meteo forecast", f"hourly forecast at the place, {_fmt(h['time'])}", _conditions(h).split(", AQI")[0],
        f"https://api.open-meteo.com/v1/forecast?{pt}&hourly=apparent_temperature,precipitation_probability,"
        f"wind_speed_10m,wind_direction_10m,uv_index,boundary_layer_height&start_date={day}&end_date={day}&timezone=Asia%2FKolkata")
    if h.get("aqi") is not None:
        add("air", "Open-Meteo air quality (CAMS)", f"PM2.5 and PM10 at the place, {_fmt(h['time'])}; India NAQI computed from them",
            f"PM2.5 {_n(h['pm2_5'])} µg/m³, PM10 {_n(h['pm10'])} µg/m³ → AQI {h['aqi']} ({h['aqi_cat']})",
            f"https://air-quality-api.open-meteo.com/v1/air-quality?{pt}&hourly=pm2_5,pm10&start_date={day}&end_date={day}"
            f"&timezone=Asia%2FKolkata")
    if b["travel"]:
        add("travel", "OSRM", "driving time from the searched area", f"{b['travel']['min']} min, {b['travel']['km']} km",
            f"https://router.project-osrm.org/route/v1/driving/{origin['lon']:.5f},{origin['lat']:.5f};{v['lon']:.5f},{v['lat']:.5f}?overview=false")
    ctx = b["surroundings"]
    if ctx.get("tree_pct") is not None:
        add("land", "ESA WorldCover 10 m (2021, Sentinel-1/2)", f"land cover within {data.EO_R_M} m of the place",
            f"{ctx['tree_pct']}% tree cover, {ctx['green_pct']}% green, {ctx['built_pct']}% built-up, {ctx['water_pct']}% water",
            data.worldcover_url(v["lat"], v["lon"]))
    if p["activity"] in ("hiking", "cycling") and ctx.get("elev_m") is not None and b.get("origin_elev_m") is not None:
        add("terrain", "Copernicus DEM GLO-30", "elevation", f"{ctx['elev_m']} m at the place, {b['origin_elev_m']} m where "
            "the search was centred", data.dem_url(v["lat"], v["lon"]))
    named = " ".join(r for x in b["hours"] for r in x["reasons"])  # sources the reasoning mentions, at any hour
    for src in ctx["roads"] + ctx["industry"]:
        label = src["name"] if src["name"] == src["kind"] else f"{src['name']} ({src['kind']})"  # as the reasons say it
        cited = f"downwind of {label}," in named or f"upwind of {label}" in named or f"near {src['name']} (mixing" in named
        if src.get("osm") and cited and f"src:{src['osm']}" not in refs:
            add(f"src:{src['osm']}", "OpenStreetMap", f"{src['kind']} near the place",
                f"{src['name']}, {src['dist_m']} m away", f"https://www.openstreetmap.org/{src['osm']}")
    return items, refs


def _cite(refs, *names):
    marks = "".join(f"[{refs[n]}]" for n in names if n in refs)
    return f" {marks}" if marks else ""


def plan_advice(p, top, avoid):
    """{carry, watch, avoid}: lists of short strings, each backed by a number from the fetched data."""
    if not top:
        return {"carry": [], "watch": [], "avoid": []}
    h = top[0]["best"]
    carry, watch = advice(p["activity"], p["profiles"], top[0])
    why = lambda a: a["reasons"][0] if a["blocked"] else next(r for r in a["reasons"] if "downwind" in r)
    return {"carry": carry, "watch": watch,
            "avoid": [f"{_fmt(a['time'])} ({why(a)})" for a in avoid if a["time"] != h["time"]][:3]}


YES_NO = re.compile(r"^\s*(?:is|are|will|would|should|shall|can|could|do|does)\b|\b(?:okay|ok|alright|good idea|"
                    r"good time|good day|safe|worth it|fine)\b", re.I)


def verdict_line(q, p, top):
    """'Verdict: yes / possible, with care / not ideal' for yes-no questions; None otherwise."""
    if not top or p["compare_days"] or not YES_NO.search(q):
        return None
    b, h = top[0], top[0]["best"]
    when = f"{_fmt(b['window'][0])} – {_hhmm(b['window'][1])} at {b['venue']['name']}"
    if h["blocked"]:
        return f"Verdict: not ideal. Every hour you asked about breaks a limit; the main issue is {h['reasons'][0]}."
    concerns = [r for r in h["reasons"] if r.startswith(("warm", "downwind", "stagnant", "high UV", "storm"))]
    if h["score"] >= 60 and not concerns:
        return f"Verdict: yes. {when} works best."
    return f"Verdict: possible, with care: {when}; watch {concerns[0] if concerns else 'the conditions below'}."


def template_answer(p, lim, origin, top, adv, cmp_line=None, evidence=None, verdict=None, named_line=None):
    """Plain-language answer built only from evidence: verdict, where and when, why, what to carry/watch/avoid, data.
    The SLM summarises it; it's also the fallback."""
    items, refs = evidence or ([], {})
    lines = [p["rolled"]] if p.get("rolled") else []
    if not top:
        return "\n".join(lines + [f"I couldn't find a usable hour for {lim['label']} near {_short(origin['name'])} "
                                  f"in that time window. Try a different day or a wider area."])
    b = top[0]
    h = b["best"]
    lines += [x for x in (verdict, cmp_line) if x]
    if h["blocked"]:
        lines.append(f"Caution: no hour in that window meets the safe limits for {lim['label']}"
                     + (f" ({', '.join(p['profiles'])} profile)" if p["profiles"] else "")
                     + f". Main issue: {h['reasons'][0]}. If you still go, this is the least-bad option; "
                       "consider an indoor alternative or another day.")
    if named_line:
        lines.append(named_line)
    lines += [f"{'Least-bad' if h['blocked'] else 'Best'} option: {b['venue']['name']} ({b['venue']['dist_m'] / 1000:.1f} km away"
              + (f", ~{b['travel']['min']} min by road" if b["travel"] else "") + "), "
              f"{_fmt(b['window'][0])} – {_hhmm(b['window'][1])}." + _cite(refs, "venue", "travel"),
              f"Searching around {_short(origin['name'])}." + _cite(refs, "origin"),
              f"At {_hhmm(h['time'])}: {_conditions(h)}." + _cite(refs, "weather", "air"),
              why_line(b) + _cite(refs, "land", "terrain")]
    notes = [r for r in h["reasons"] if not r.startswith("warm")] if not h["blocked"] else []
    if notes:
        lines.append("Note: " + "; ".join(notes) + "." + _cite(refs, *[k for k in refs if k.startswith("src:")]))
    for alt in top[1:]:
        lines.append(f"Alternative: {alt['venue']['name']}, {_fmt(alt['window'][0])} – {_hhmm(alt['window'][1])}"
                     + (" (also over limits)." if alt["best"]["blocked"] else "."))
    for label, key in (("Avoid", "avoid"), ("Carry", "carry"), ("Watch for", "watch")):
        if adv[key]:
            lines.append(f"{label}: " + "; ".join(adv[key]) + ".")
    lines.append(f"Data: OpenStreetMap (places, roads, industry, water, green), Open-Meteo forecast and CAMS air quality "
                 f"(AQI on India's NAQI scale), OSRM travel times; fetched {datetime.now(IST):%I:%M %p} IST.")
    if items:
        lines.append("Evidence: " + " ".join(f"[{e['id']}] {e['source']}: {e['value']} ({e['url']})" for e in items))
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
    key = (" ".join(q.lower().split()), lat and round(lat, 3), lon and round(lon, 3))
    hit = _answers.get(key)
    if hit and hit[0] > time.time():  # judges often ask the same thing twice; data is still fresh for 10 min
        _say("Answered from data fetched in the last 10 minutes")
        return {**hit[1], "cached": True, "latency_s": round(time.perf_counter() - t0, 2)}
    trace = None
    try:
        res = await asyncio.wait_for(_answer(q, lat, lon), REQUEST_DEADLINE_S)
    except asyncio.TimeoutError:
        res = {"status": "error", "question": q, "error": "deadline",
               "answer": "Live map and weather services are slow right now, so I couldn't finish in time. Please try again."}
        res["summary"] = res["answer"]
    except Exception as e:  # judges hit this live: never return a bare 500
        trace = traceback.format_exc()[-2000:]
        print(trace)
        res = {"status": "error", "question": q, "error": repr(e)[:200],
               "answer": "A live data source didn't respond properly just now. Please try again in a minute."}
        res["summary"] = res["answer"]
    res["latency_s"] = round(time.perf_counter() - t0, 2)
    _log(res, trace)
    if res["status"] in ("ok", "clarify", "no_venues"):
        if len(_answers) > 500:
            _answers.clear()  # ponytail: crude bound; an LRU if this ever matters
        _answers[key] = (time.time() + ANSWER_TTL_S, res)
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


PLACE_CATEGORIES = {"leisure", "tourism", "natural", "historic"}  # a park, beach, fort, viewpoint: a place, not an area


def _with_named_place(origin, venues):
    """When the user named a place ("picnic in Cubbon Park"), make sure that place is a candidate: the matching
    OSM venue if one is within 300 m, else the place itself. Returns (venues, its OSM id or None)."""
    if origin.get("category") not in PLACE_CATEGORIES:
        return venues, None
    near = next((v for v in venues if data.haversine_m(v["lat"], v["lon"], origin["lat"], origin["lon"]) < 300), None)
    if near:
        return venues, near["osm"]
    place = {"name": origin["name"].split(",")[0], "lat": origin["lat"], "lon": origin["lon"], "tags": {},
             "osm": origin.get("osm") or "named-place", "dist_m": 0, "maybe_private": False}
    return [place] + venues[:7], place["osm"]


def _prefer_named(ranked, named):
    """Put the named place first unless it is clearly worse; if it is, say how it compares. (ranked, line or None)"""
    rec = next((r for r in ranked if r["venue"]["osm"] == named), None) if named else None
    if not rec or rec is ranked[0]:
        return ranked, None
    best = ranked[0]
    if not rec["best"]["blocked"] and rec["best"]["score"] >= best["best"]["score"] - 15:
        return [rec] + [r for r in ranked if r is not rec], None
    h = rec["best"]
    why = h["reasons"][0] if h["reasons"] else f"feels like {_n(h['feels_c'], unit='°C')}"
    return [best, rec] + [r for r in ranked if r not in (best, rec)], (
        f"You asked about {rec['venue']['name']}: its best hour is {_fmt(h['time'])} ({why}), "
        f"but {best['venue']['name']} scores higher, so it comes first.")


def _writer_targets(rec):
    """(venue name, times) the summary must mention."""
    return rec["venue"]["name"], [_hhmm(rec["window"][0]), _hhmm(rec["best"]["time"])]


async def _answer(q: str, lat=None, lon=None):
    t0 = time.perf_counter()
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

    _say(f"Understood: {ACTIVITIES[p['activity']]['label']}"
         + (f" near {p['place_candidates'][0]}" if p["place_candidates"] else "")
         + f", {p['start']:%a %d %b %I %p} to {p['end']:%a %I %p}" + (f", {', '.join(p['profiles'])}" if p["profiles"] else ""))
    origin, ask = await _resolve_origin(p, q, lat, lon)
    if not origin:
        return _clarify(q, ask, p)
    _say(f"Searching around {_short(origin['name'])}")

    lim = limits_for(p["activity"], p["profiles"])
    radius_km = p["radius_km"] or lim["radius_km"]
    tm = {"understand+geocode": time.perf_counter() - t0}
    venues = await data.venues(origin["lat"], origin["lon"], int(radius_km * 1000), lim["venues"])
    if not venues:  # widen once before giving up
        radius_km *= 2.5
        venues = await data.venues(origin["lat"], origin["lon"], int(radius_km * 1000), lim["venues"])
    venues, named = _with_named_place(origin, venues)
    if not venues:
        text = (f"OpenStreetMap lists no {lim['label']} venues within {radius_km:.0f} km of "
                f"{_short(origin['name'])}. Try a larger area.")
        return {"status": "no_venues", "question": q, "parsed": _jsonable(p), "origin": origin,
                "answer": text, "summary": text}

    days = min(7, max(1, (p["end"] - datetime.now(IST).replace(tzinfo=None)).days + 2))
    tm["venues"] = time.perf_counter() - t0 - sum(tm.values())
    _say(f"Found {len(venues)} candidate places on OpenStreetMap; fetching their weather, air quality and surroundings")
    ctx_task = asyncio.create_task(data.surroundings(venues))  # the slow one (Overpass): ~10 s
    eo_task = asyncio.create_task(data.earth(venues + [origin]))  # satellite land cover + DEM, alongside it
    wxs, aqs, travels = await asyncio.gather(data.weather(venues, days), data.air_quality(venues, min(days, 5)),
                                             data.travel(origin, venues))
    # Start the SLM summary now, on a ranking without surroundings, while Overpass works. It is kept only if the
    # final ranking picks the same venue and window and the summary passes the checks against the final facts.
    early = None
    if not p["compare_days"]:
        blank = [{"roads": [], "industry": [], "water": [], "green": 0} for _ in venues]
        pre, pre_avoid = rank(venues, wxs, aqs, blank, travels, lim, p["start"], p["end"], p["hour_range"])
        if pre and not pre[0]["best"]["blocked"]:
            pre_facts = template_answer(p, lim, origin, pre, plan_advice(p, pre, pre_avoid))
            early = (pre[0], asyncio.create_task(llm.write_answer(q, pre_facts, *_writer_targets(pre[0]))))
            early[1].add_done_callback(lambda t: t.cancelled() or t.exception())  # unused result: never warn
    tm["weather+air+travel"] = time.perf_counter() - t0 - sum(tm.values())
    _say("Got the hourly forecast, air quality and travel times; checking roads, industry, water and satellite land cover")
    ctxs = await ctx_task
    eo = await eo_task
    ctxs = [{**c, **e} for c, e in zip(ctxs, eo)]  # new dicts: the cached contexts stay as fetched
    tm["surroundings (after weather)"] = time.perf_counter() - t0 - sum(tm.values())
    ranked, avoid = rank(venues, wxs, aqs, ctxs, travels, lim, p["start"], p["end"], p["hour_range"], top=len(venues))
    _say(f"Scored {len(venues)} places across every hour of your window")
    ranked, named_line = _prefer_named(ranked, named)
    top = ranked[:3]
    if top:
        avoid = avoid_hours(top[0])

    comparison = []
    for d in p["compare_days"]:  # "Saturday or Sunday?": the best of each day, then a verdict
        day = datetime.fromisoformat(d)
        best_of_day, _ = rank(venues, wxs, aqs, ctxs, travels, lim, max(day, p["start"]), day + timedelta(days=1),
                              p["hour_range"], top=1)
        if best_of_day:
            b = best_of_day[0]
            comparison.append({"day": d, "venue": b["venue"]["name"], "window": b["window"], "best": b["best"]})
    cmp_line = comparison_line(comparison)

    if top:
        top[0]["origin_elev_m"] = eo[-1]["elev_m"]
    adv = plan_advice(p, top, avoid)
    evidence = build_evidence(p, origin, top)
    facts = template_answer(p, lim, origin, top, adv, cmp_line, evidence, verdict_line(q, p, top), named_line)
    caution = any(l.startswith("Caution") for l in facts.splitlines())
    core = facts.split("\nEvidence:")[0]  # the writer is checked against facts, not URLs full of numbers
    slm = None
    tw = time.perf_counter()
    if top and not caution:
        if early and early[0]["venue"]["osm"] == top[0]["venue"]["osm"]:  # times are re-checked against the final facts
            out = await early[1]  # usually already done: it ran while Overpass was working
            slm = out if out and llm.acceptable(out, core, *_writer_targets(top[0]), q) else None
        elif time.perf_counter() - t0 < WRITER_BUDGET_S:  # the ranking changed: write again if time allows
            slm = await llm.write_answer(q, core, *_writer_targets(top[0]))
    tm["writer wait"] = time.perf_counter() - tw
    tm["early writer"] = ("used" if early and slm and early[0]["venue"]["osm"] == top[0]["venue"]["osm"]
                          else "discarded" if early else "not started")
    prefix = [x for x in (assumed and "No activity was named, so I planned for a walk. Name one for a sharper answer.",
                          p.get("rolled")) if x]
    if slm:
        summary = " ".join(prefix + [slm])
        text = f"{summary}\n\nDetails:\n{facts}"
    else:
        key = next((l for l in facts.splitlines() if l.startswith(("Verdict:", "Caution", "Comparison:", "Best option",
                                                                   "Least-bad option", "I couldn't"))), "")
        summary = " ".join(prefix + [re.sub(r"\s*\[\d+\]", "", key)])  # marks belong to the full answer
        text = "\n".join(prefix[:1] + [facts]) if assumed else facts

    return {
        "status": "ok", "question": q,
        "answer": text, "summary": summary, "caution": caution, "writer": "slm" if slm else "template",
        "facts": facts, "searched": _short(origin["name"]), "comparison": comparison, "advice": adv,
        "evidence": evidence[0],
        "parsed": _jsonable(p),
        "origin": origin, "radius_km": radius_km, "limits": {k: lim[k] for k in (
            "feels_max", "rain_max", "wind_max", "gust_max", "aqi_max", "uv_max", "daylight", "downwind_mult")},
        "recommendations": top, "avoid": avoid[:12], "sources": SOURCES,
        "fetched_at": datetime.now(IST).isoformat(timespec="seconds"),
        "timings": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in tm.items()},
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


@app.get("/ask/stream")
async def ask_stream(request: Request):
    """Same inputs as /ask; streams server-sent events: `progress` (one per finished stage), then `result`."""
    q, lat, lon, _ = await _inputs(request)
    queue: asyncio.Queue = asyncio.Queue()

    async def run():
        _progress.set(queue)  # this task's context only
        res = await answer(q, lat, lon)
        res["view_url"] = f"{str(request.base_url).rstrip('/')}/?q={quote(q)}"
        queue.put_nowait(res)

    async def events():
        task = asyncio.create_task(run())
        try:
            while True:
                item = await queue.get()
                if isinstance(item, dict):
                    yield f"event: result\ndata: {json.dumps(item, default=str, ensure_ascii=False)}\n\n"
                    return
                yield f"event: progress\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/", include_in_schema=False)
async def portal():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/health")
async def health():
    return {"ok": True}
