"""Rule-based question parsing. Runs first; the SLM only fills in what these rules miss."""
import difflib
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
    # common Hinglish
    "subah": (5, 12), "dopahar": (12, 16), "shaam": (16, 21), "raat": (18, 23),
}
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
FORECAST_DAYS = 7
STOP = (r"this|next|on|at|tomorrow|today|tonight|for|with|within|after|before|during|around|morning|evening|"
        r"afternoon|night|weekend|" + "|".join(WEEKDAYS) + r"|to|and|i|we|my|is|best|good|pe|mein|ke|ki|ko|paas")
SHORTHAND = {r"\b(?:tmrw|tmr|tmrow|tomo|tomorow|tommorow|tommorrow|2moro)\b": "tomorrow", r"\btdy\b": "today",
             r"\bwknd\b": "weekend", r"\bevng\b": "evening", r"\bmrng\b": "morning"}
ALL_KEYWORDS = {k: a for a, d in ACTIVITIES.items() for k in d["keywords"] if " " not in k and len(k) >= 4}
PLACE_RE = re.compile(rf"\b(?:near|in|at|around|of|from)\s+((?:(?!\b(?:{STOP})\b)[a-z][\w.'-]*\s?){{1,5}})", re.I)


def _match(text, keywords):
    """Longest keyword found as a word prefix in text, or ''."""
    hits = [k for k in keywords if re.search(rf"\b{re.escape(k)}", text)]
    return max(hits, key=len, default="")


def parse(q: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(IST).replace(tzinfo=None)
    for pat, rep in SHORTHAND.items():
        q = re.sub(pat, rep, q, flags=re.I)
    t = q.lower()

    acts = {a: _match(t, d["keywords"]) for a, d in ACTIVITIES.items()}
    activity = max(acts, key=lambda a: len(acts[a])) if any(acts.values()) else None
    if not activity:  # typo tolerance: "crickte", "tenis", "cyclng"
        for w in re.findall(r"[a-z]{4,}", t):
            close = difflib.get_close_matches(w, ALL_KEYWORDS, n=1, cutoff=0.8)
            if close:
                activity = ALL_KEYWORDS[close[0]]
                break
    profiles = [p for p, d in PROFILES.items() if _match(t, d["keywords"])]
    for age in map(int, re.findall(r"(\d+)[ -]?(?:year|yr)s?[ -]?old", t)):
        prof = "child" if age < 15 else "senior" if age >= 60 else None
        if prof and prof not in profiles:
            profiles.append(prof)

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
    if "day after tomorrow" in t or re.search(r"\bparso", t):
        day0, ndays = today + timedelta(days=2), 1
    elif "tomorrow" in t or re.search(r"\bkal\b", t):
        day0, ndays = today + timedelta(days=1), 1
    elif "weekend" in t:
        sat = today + timedelta(days=(5 - today.weekday()) % 7)
        if today.weekday() == 6:  # already Sunday: just today
            sat = today - timedelta(days=1)
        day0, ndays = sat, 2
    elif "next week" in t:
        day0, ndays = today + timedelta(days=7 - today.weekday()), 7
    elif "today" in t or re.search(r"\baaj\b", t) or "tonight" in t or "this evening" in t or "this morning" in t:
        day0, ndays = today, 1
    else:
        for i, d in enumerate(WEEKDAYS):
            if re.search(rf"\b{d}", t):
                ahead = (i - today.weekday()) % 7
                day0, ndays = today + timedelta(days=ahead), 1
                break
    date = _explicit_date(t, today)
    if date:
        day0, ndays = date, 1

    out_of_range = None
    if re.search(r"\b(?:yesterday|last (?:week|month|night)|kal raat)\b", t) or (date and date < today):
        out_of_range = "I can only plan ahead: forecasts start from now."
    elif re.search(r"\bnext (?:month|year)\b", t) or (date and date >= today + timedelta(days=FORECAST_DAYS)):
        last = today + timedelta(days=FORECAST_DAYS - 1)
        out_of_range = f"Weather forecasts only reach {FORECAST_DAYS} days ahead (until {last:%a %d %b}). Ask about a day before then."

    part = _match(t, PARTS)
    h0, h1 = PARTS[part] if part else (0, 24)
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t)
    if m:  # explicit clock time: a window around it
        hr = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
        h0, h1, part = max(0, hr - 1), min(24, hr + 3), f"around {m.group(0)}"
    days = [day0 + timedelta(days=i) for i in range(ndays)]
    # ponytail: one contiguous window; a part-of-day over several days scores each day's slice via `hours`
    start, end = days[0] + timedelta(hours=h0), days[-1] + timedelta(hours=h1)
    start = max(start, now.replace(minute=0, second=0, microsecond=0))
    end = min(end, today + timedelta(days=7))

    time_given = bool(part) or (day0, ndays) != (today, 2)
    return {"activity": activity, "profiles": profiles, "place": place, "radius_km": radius_km, "time_given": time_given,
            "out_of_range": out_of_range,
            "start": start, "end": end, "part_of_day": part or None, "hour_range": (h0, h1)}


def _explicit_date(t, today):
    """'25 oct', '25th october', 'oct 25', '25/10' -> datetime (this year, or next if far past), else None."""
    mon = "|".join(MONTHS)
    m = (re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({mon})[a-z]*\b", t)
         or re.search(rf"\b({mon})[a-z]*\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", t))
    if m:
        a, b = m.groups()
        day, month = (int(a), MONTHS.index(b) + 1) if a.isdigit() else (int(b), MONTHS.index(a) + 1)
    else:
        m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", t)  # Indian order: day/month
        if not m:
            return None
        day, month = int(m.group(1)), int(m.group(2))
    try:
        d = today.replace(month=month, day=day)
    except ValueError:
        return None
    return d.replace(year=d.year + 1) if (today - d).days > 60 else d


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
    assert parse("picnic with my 5 year old in Pune", now)["profiles"] == ["child"]
    assert parse("walk with my 72-year-old mother in Pune", now)["profiles"] == ["senior"]
    p = parse("kal shaam Juhu mein cycling", now)
    assert p["start"] == datetime(2026, 10, 23, 16) and p["end"] == datetime(2026, 10, 23, 21), p
    p = parse("crickte near andheri tmrw", now)
    assert p["activity"] == "cricket" and p["place"] == "andheri" and p["start"] == datetime(2026, 10, 23), p
    p = parse("tennis in Delhi at 3pm tomorrow", now)
    assert p["place"] == "Delhi" and p["hour_range"] == (14, 18), p
    p = parse("outdoor party in Gurgaon on 25 Oct evening", now)
    assert p["start"] == datetime(2026, 10, 25, 16) and p["place"] == "Gurgaon", p
    assert parse("cycling in Chennai next month", now)["out_of_range"]
    assert parse("was yesterday good for a walk in Kolkata", now)["out_of_range"]
    assert parse("golf on 2 Nov in Pune", now)["out_of_range"] and not parse("golf on 27 Oct in Pune", now)["out_of_range"]
    print("ok")
