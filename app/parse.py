"""Rule-based question parsing. The SLM will do this tomorrow; these rules stay as its fallback."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.rules import ACTIVITIES, PROFILES

IST = ZoneInfo("Asia/Kolkata")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
PARTS = {  # part of day -> (start hour, end hour)
    "early morning": (4, 8), "morning": (5, 12), "afternoon": (12, 16), "evening": (16, 21),
    "tonight": (18, 23), "night": (18, 23), "after work": (17, 21), "sunrise": (5, 8), "sunset": (16, 20),
    "noon": (11, 14),
}
STOP = (r"this|next|on|tomorrow|today|tonight|for|with|within|after|before|during|around|morning|evening|"
        r"afternoon|night|weekend|" + "|".join(WEEKDAYS) + r"|to|and|i|we|my|is|best|good")
PLACE_RE = re.compile(rf"\b(?:near|in|at|around|of|from)\s+((?:(?!\b(?:{STOP})\b)[a-z][\w.'-]*\s?){{1,5}})", re.I)


def _match(text, keywords):
    """Longest keyword found as a word prefix in text, or ''."""
    hits = [k for k in keywords if re.search(rf"\b{re.escape(k)}", text)]
    return max(hits, key=len, default="")


def parse(q: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(IST).replace(tzinfo=None)
    t = q.lower()

    acts = {a: _match(t, d["keywords"]) for a, d in ACTIVITIES.items()}
    activity = max(acts, key=lambda a: len(acts[a])) if any(acts.values()) else None
    profiles = [p for p, d in PROFILES.items() if _match(t, d["keywords"])]

    place = None
    for m in PLACE_RE.finditer(q):
        cand = m.group(1).strip(" .,'")
        if cand and not re.fullmatch(r"\d.*|the|a|an|km|the (?:park|evening|morning)", cand, re.I):
            place = cand
            break

    r = re.search(r"(\d+(?:\.\d+)?)\s*(?:km|kilomet)", t)
    radius_km = float(r.group(1)) if r else None

    # ---- day ----
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day0, ndays = today, 2
    if "day after tomorrow" in t:
        day0, ndays = today + timedelta(days=2), 1
    elif "tomorrow" in t:
        day0, ndays = today + timedelta(days=1), 1
    elif "weekend" in t:
        sat = today + timedelta(days=(5 - today.weekday()) % 7)
        if today.weekday() == 6:  # already Sunday: just today
            sat = today - timedelta(days=1)
        day0, ndays = sat, 2
    elif "next week" in t:
        day0, ndays = today + timedelta(days=7 - today.weekday()), 7
    elif "today" in t or "tonight" in t or "this evening" in t or "this morning" in t:
        day0, ndays = today, 1
    else:
        for i, d in enumerate(WEEKDAYS):
            if re.search(rf"\b{d}", t):
                ahead = (i - today.weekday()) % 7
                day0, ndays = today + timedelta(days=ahead), 1
                break

    part = _match(t, PARTS)
    h0, h1 = PARTS[part] if part else (0, 24)
    days = [day0 + timedelta(days=i) for i in range(ndays)]
    # ponytail: one contiguous window; a part-of-day over several days scores each day's slice via `hours`
    start, end = days[0] + timedelta(hours=h0), days[-1] + timedelta(hours=h1)
    start = max(start, now.replace(minute=0, second=0, microsecond=0))
    end = min(end, today + timedelta(days=7))

    return {"activity": activity, "profiles": profiles, "place": place, "radius_km": radius_km,
            "start": start, "end": end, "part_of_day": part or None, "hour_range": (h0, h1)}


if __name__ == "__main__":
    now = datetime(2026, 10, 22, 17, 0)  # a Thursday
    p = parse("Best time to go running near Powai tomorrow morning? I have asthma", now)
    assert p["activity"] == "running" and p["profiles"] == ["asthma"] and p["place"].lower() == "powai", p
    assert p["start"] == datetime(2026, 10, 23, 5) and p["end"] == datetime(2026, 10, 23, 12), p
    p = parse("Where can my parents go for a nature walk in Thane this weekend?", now)
    assert p["activity"] == "hiking" and p["profiles"] == ["senior"] and p["place"] == "Thane", p
    assert p["start"] == datetime(2026, 10, 24), p
    p = parse("golf within 30 km of Pune on Saturday evening", now)
    assert p["activity"] == "golf" and p["radius_km"] == 30 and p["place"] == "Pune", p
    assert p["start"] == datetime(2026, 10, 24, 16), p
    p = parse("is it a good day for cricket?", now)
    assert p["place"] is None and p["activity"] == "cricket", p
    print("ok")
