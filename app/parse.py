"""Rule-based question parsing. Runs first; the SLM fills in what these rules miss."""
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
NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "couple of": 2, "few": 3}
SHORTHAND = {r"\b(?:tmrw|tmr|tmrow|tomo|tomorow|tommorow|tommorrow|2moro)\b": "tomorrow", r"\btdy\b": "today",
             r"\bwknd\b": "weekend", r"\bevng\b": "evening", r"\bmrng\b": "morning"}
# Devanagari time words -> the English the rules already understand (an SLM once read "कल" as Sunday)
DEVANAGARI = {"परसों": "day after tomorrow", "कल": "tomorrow", "आज": "today", "सुबह": "morning",
              "दोपहर": "afternoon", "शाम": "evening", "रात": "night", "इस हफ्ते": "this week"}

# words that end (or can't start) a place name; "in the next two days" must not become part of "Powai"
STOP = (r"this|next|on|at|in|near|from|by|of|around|to|till|until|via|between|within|for|with|after|before|during|"
        r"and|or|but|then|vs|the|a|an|tomorrow|today|tonight|morning|evening|afternoon|night|noon|weekend|week|"
        r"day|days|" + "|".join(WEEKDAYS) + r"|i|we|me|my|us|is|it|any|some|best|good|better|safe|okay|ok|"
        r"pe|par|mein|ke|ki|ko|paas")
PLACE_RE = re.compile(
    rf"\b(?:near|in|at|around|of|from|outside)\s+(?:the\s+)?"
    rf"((?!(?:{STOP})\b)[a-z][\w.'-]*(?:\s+(?!(?:{STOP})\b)[\w][\w.'-]*){{0,4}})", re.I)
# "near me", "around here": no place was given; use the device location or ask
DEICTIC = {"me", "here", "nearby", "home", "my place", "my area", "my location", "this area", "us"}
# a bare venue word is the kind of place, not where it is ("at the park near Powai")
GENERIC_PLACE = {"park", "beach", "ground", "garden", "lake", "area", "city", "place", "gym", "office", "work",
                 "school", "college", "home", "house", "club", "court", "field", "sea", "hills", "hill", "mountains"}


def _kw_re(k):
    """Whole word with an optional plural, or a prefix when the keyword ends in '*'."""
    if k.endswith("*"):
        return rf"\b{re.escape(k[:-1])}\w*"
    return rf"\b{re.escape(k)}(?:s|es)?\b"


def _match(text, keywords):
    """Longest keyword found in text, or ''."""
    hits = [k for k in keywords if re.search(_kw_re(k), text)]
    return max(hits, key=len, default="")


ALL_KEYWORDS = {k: a for a, d in ACTIVITIES.items() for k in d["keywords"] if " " not in k and len(k) >= 4}


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


def _places(q):
    """Place candidates in reading order, without deictic or generic words."""
    out = []
    for m in PLACE_RE.finditer(q):
        cand = m.group(1).strip(" .,'?!")
        low = cand.lower()
        if low in DEICTIC or low in GENERIC_PLACE or re.fullmatch(r"\d.*|km|kms", low):
            continue
        out.append(cand)
    return out


def parse(q: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(IST).replace(tzinfo=None)
    for pat, rep in SHORTHAND.items():
        q = re.sub(pat, rep, q, flags=re.I)
    for dev, en in DEVANAGARI.items():
        q = q.replace(dev, f" {en} ")
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

    places = _places(q)
    place = places[0] if places else None
    near_me = bool(re.search(r"\b(?:near|around|close to)\s+(?:me|here|us)\b|\bnearby\b|\bmy (?:area|location)\b", t))

    r = re.search(r"(\d+(?:\.\d+)?)\s*(?:km|kilomet)", t)
    radius_km = float(r.group(1)) if r else None

    # ---- day ----
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day0, ndays, compare_days = today, 2, []
    weekday_dates = sorted({today + timedelta(days=(i - today.weekday()) % 7)
                            for i, d in enumerate(WEEKDAYS) if re.search(rf"\b{d}", t)})
    n = re.search(r"\bnext\s+(\d|" + "|".join(NUMBER_WORDS) + r")\s+days?\b", t)
    if "day after tomorrow" in t or re.search(r"\bparso", t):
        day0, ndays = today + timedelta(days=2), 1
    elif "tomorrow" in t or re.search(r"\bkal\b", t):
        day0, ndays = today + timedelta(days=1), 1
    elif len(weekday_dates) >= 2:  # "Saturday or Sunday": cover both and compare them
        day0, ndays = weekday_dates[0], (weekday_dates[-1] - weekday_dates[0]).days + 1
        compare_days = weekday_dates
    elif "weekend" in t:
        sat = today + timedelta(days=(5 - today.weekday()) % 7)
        if today.weekday() == 6:  # already Sunday: just today
            sat = today - timedelta(days=1)
        day0, ndays = sat, 2
    elif n:
        day0, ndays = today, min(FORECAST_DAYS, int(n.group(1)) if n.group(1).isdigit() else NUMBER_WORDS[n.group(1)])
    elif "this week" in t:
        day0, ndays = today, 7 - today.weekday()
    elif "next week" in t:
        day0, ndays = today + timedelta(days=7 - today.weekday()), 7
    elif "today" in t or re.search(r"\baaj\b", t) or "tonight" in t or "this evening" in t or "this morning" in t:
        day0, ndays = today, 1
    elif weekday_dates:
        day0, ndays = weekday_dates[0], 1
    date = _explicit_date(t, today)
    if date:
        day0, ndays = date, 1
    if re.search(r"\bwhich day\b|\bwhat day\b", t) and ndays > 1:
        compare_days = [day0 + timedelta(days=i) for i in range(ndays)]

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

    # a window that has (nearly) passed today moves to the next day, and the answer says so
    rolled = None
    if ndays == 1 and day0 == today:
        light_gone = (part == "sunset" and now.hour >= 19) or (part == "sunrise" and now.hour >= 8)
        if light_gone or today + timedelta(hours=h1) - now < timedelta(hours=1):
            day0 = today + timedelta(days=1)
            what = {"sunset": "Today's sunset has passed", "sunrise": "Today's sunrise has passed"}.get(
                part, f"Today's {part or 'window'} is almost over")
            rolled = f"{what}, so this plans for tomorrow."

    days = [day0 + timedelta(days=i) for i in range(ndays)]
    # ponytail: one contiguous window; a part-of-day over several days scores each day's slice via `hours`
    start, end = days[0] + timedelta(hours=h0), days[-1] + timedelta(hours=h1)
    start = max(start, now.replace(minute=0, second=0, microsecond=0))
    end = min(end, today + timedelta(days=FORECAST_DAYS))

    time_given = bool(part) or (day0, ndays) != (today, 2)
    return {"activity": activity, "profiles": profiles, "place": place, "place_candidates": places,
            "near_me": near_me, "radius_km": radius_km, "time_given": time_given, "out_of_range": out_of_range,
            "compare_days": [d.date().isoformat() for d in compare_days], "rolled": rolled,
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
    # C1: places stop at prepositions/articles; numbers allowed inside a name
    for q, want in [("I have mild asthma, where's good for a morning walk near Powai in the next two days?", "Powai"),
                    ("cycling in Mumbai in the evening", "Mumbai"), ("trek near Lonavala in the next 3 days", "Lonavala"),
                    ("cycling around Aarey Colony from 6 to 8 am tomorrow", "Aarey Colony"),
                    ("picnic at the park near Powai tomorrow", "Powai"), ("football in Sector 18 Noida tomorrow", "Sector 18 Noida"),
                    ("Outdoor tennis after work today near Hinjewadi, any good spots?", "Hinjewadi"),
                    ("Best time this weekend for a kids' cricket match within 15 km of Whitefield?", "Whitefield")]:
        assert parse(q, now)["place"] == want, (q, parse(q, now)["place"])
    # C2: "near me" is not a place
    p = parse("running near me tomorrow", now)
    assert p["place"] is None and p["near_me"], p
    # C4: two weekdays -> both covered and compared
    p = parse("Is Saturday or Sunday better for a hike near Lonavala?", now)
    assert p["compare_days"] == ["2026-10-24", "2026-10-25"] and p["end"] == datetime(2026, 10, 26), p
    # C5: an evening that has passed rolls to tomorrow
    late = datetime(2026, 10, 22, 20, 30)
    p = parse("Where can I take sunset photos this evening around Bandra?", late)
    assert p["start"] == datetime(2026, 10, 23, 16) and p["rolled"], p
    # C7: no profile from substrings
    for q in ["picnic in Sonipat on Sunday", "walk after my kidney surgery near Juhu", "cricket at the school ground in Pune"]:
        assert parse(q, now)["profiles"] == [], (q, parse(q, now)["profiles"])
    assert parse("I get breathless sometimes, cycling in Pune", now)["profiles"] == ["asthma"]
    # H1: Devanagari time words
    p = parse("कल सुबह पवई में दौड़ना है", now)
    assert p["start"] == datetime(2026, 10, 23, 5) and p["end"] == datetime(2026, 10, 23, 12), p
    # M4: next N days, this week
    assert parse("trek near Lonavala in the next 3 days", now)["end"] == datetime(2026, 10, 25)
    assert parse("cricket in Thane this week", now)["end"] == datetime(2026, 10, 26)
    print("ok")
