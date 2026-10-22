"""Score every venue x hour against activity/profile limits. Pure code: no model, no network."""
import re
from datetime import datetime, timedelta

from app.data import INDUSTRY_R, ROAD_R, compass

# Base penalty for being directly downwind of a pollution source, before distance falloff and profile multiplier.
SOURCE_PENALTY = {"motorway": 25, "trunk": 18, "primary": 10, "industrial": 25, "works": 25,
                  "power plant": 30, "landfill": 20}
ROAD_KINDS = ("motorway", "trunk", "primary")
DOWNWIND_CONE = 45  # degrees either side of the wind's origin
STAGNANT_BLH_M = 300  # below this mixing height, traffic exhaust lingers near the road whatever the wind does
NEEDS_FLOODLIGHTS = {"football", "tennis", "cricket"}

# India's National AQI (CPCB) breakpoints: (conc lo, conc hi, index lo, index hi). CPCB uses 24-hour means;
# we apply them to hourly forecasts, which is an approximation (and said so in the README).
NAQI_BP = {"pm2_5": [(0, 30, 0, 50), (30, 60, 50, 100), (60, 90, 100, 200), (90, 120, 200, 300), (120, 250, 300, 400),
                     (250, 500, 400, 500)],
           "pm10": [(0, 50, 0, 50), (50, 100, 50, 100), (100, 250, 100, 200), (250, 350, 200, 300),
                    (350, 430, 300, 400), (430, 1000, 400, 500)]}
NAQI_CATS = [(50, "Good"), (100, "Satisfactory"), (200, "Moderate"), (300, "Poor"), (400, "Very Poor"), (500, "Severe")]


def naqi(pm2_5, pm10):
    """(index, category) from PM2.5 and PM10 in µg/m³, or (None, None) without data."""
    subs = []
    for pol, c in (("pm2_5", pm2_5), ("pm10", pm10)):
        if c is None:
            continue
        for lo, hi, ilo, ihi in NAQI_BP[pol]:
            if c <= hi:
                subs.append(ilo + (min(c, hi) - lo) * (ihi - ilo) / (hi - lo))
                break
        else:
            subs.append(500)
    if not subs:
        return None, None
    v = round(max(subs))
    return v, next(cat for top, cat in NAQI_CATS if v <= top)


def traffic(t):
    """Heuristic traffic level (no free live-traffic source): weekday rush hours heavy, nights light, Sundays lighter."""
    weekday, h = t.weekday() < 5, t.hour
    level = 1.4 if weekday and (8 <= h < 11 or 17 <= h < 21) else 0.6 if (h >= 22 or h < 5) else 1.0
    return level * (0.8 if t.weekday() == 6 else 1.0)


def is_rush(t):
    return t.weekday() < 5 and (8 <= t.hour < 11 or 17 <= t.hour < 21)


def open_at(oh, t):
    """True/False from simple OSM opening_hours ("06:00-21:00", "Mo-Su 05:30-20:00", "24/7"); None when unsure."""
    if not oh:
        return None
    if "24/7" in oh:
        return True
    rest = re.sub(r"^\s*Mo-Su\s*", "", oh)
    if re.search(r"[A-Za-z]", rest) or ";" in rest:  # day-specific or exceptions: don't guess
        return None
    ranges = re.findall(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", rest)
    if not ranges:
        return None
    m = t.hour * 60 + t.minute
    for h1, m1, h2, m2 in ranges:
        a, b = int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)
        if (a <= m < b) if a < b else (m >= a or m < b):
            return True
    return False


def _angle_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def _series(js, keys):
    """{time_str: {key: value}} for an Open-Meteo hourly block."""
    h = js["hourly"]
    return {t: {k: h[k][i] for k in keys if k in h} for i, t in enumerate(h["time"])}


def _sun(wx):
    d = wx["daily"]
    return {day: (datetime.fromisoformat(sr), datetime.fromisoformat(ss))
            for day, sr, ss in zip(d["time"], d["sunrise"], d["sunset"])}


def downwind(ctx, wind_from, wind_kmh, mult, t=None):
    """Worst pollution source the venue sits downwind of this hour, as (penalty, description)."""
    worst = (0, None)
    for src in ctx["roads"] + ctx["industry"]:
        if _angle_diff(wind_from, src["bearing"]) > DOWNWIND_CONE:
            continue
        kind = src["kind"]
        road = kind in ROAD_KINDS
        falloff = max(0.0, 1 - src["dist_m"] / (ROAD_R if road else INDUSTRY_R))
        calm = 0.6 if wind_kmh < 4 else 1.0  # near-calm air: plume doesn't travel as cleanly
        level = traffic(t) if (road and t) else 1.0
        pen = SOURCE_PENALTY.get(kind, 15) * falloff * calm * mult * level
        if pen > worst[0]:
            label = src["name"] if src["name"] == kind else f"{src['name']} ({kind})"
            worst = (pen, f"downwind of {label}, {src['dist_m']} m {compass(src['bearing'])} "
                          f"(wind from {compass(wind_from)}" + (", rush hour" if road and t and level > 1 else "") + ")")
    return worst


def score_hour(lim, w, aq, ctx, sun, t, travel_min, venue=None):
    """(score 0-100, blocked: bool, reasons: list[str]) for one venue at one hour."""
    reasons, blocks, s = [], [], 100.0
    tags = (venue or {}).get("tags", {})
    # Open-Meteo returns nulls near the end of the forecast range: never let one crash the answer
    feels, rain, uv = w["apparent_temperature"], w.get("precipitation_probability"), w.get("uv_index")
    wind = w.get("wind_speed_10m") or 0
    gust = w.get("wind_gusts_10m") or wind
    is_day = w.get("is_day", 1)
    aqi, cat = naqi(aq.get("pm2_5"), aq.get("pm10"))

    if (w.get("weather_code") or 0) >= 95:  # WMO 95-99: thunderstorm
        blocks.append("thunderstorm forecast")
    elif (w.get("cape") or 0) > 1500 and (rain or 0) >= 40:
        s -= 10
        reasons.append(f"storm potential (CAPE {w['cape']:.0f} J/kg)")
    if rain is not None:
        if rain > lim["rain_max"]:
            blocks.append(f"rain chance {rain}% (limit {lim['rain_max']}%)")
        s -= rain / lim["rain_max"] * 20
    if feels > lim["feels_max"]:
        blocks.append(f"feels like {feels:.0f}°C (limit {lim['feels_max']}°C)")
    elif feels > lim["feels_max"] - 6:
        heat = (feels - (lim["feels_max"] - 6)) * 3
        if ctx.get("green", 0) >= 3 and is_day:  # shade from mapped parks/woods softens the heat
            heat *= 0.7
            reasons.append(f"warm, feels like {feels:.0f}°C; {ctx['green']} green areas within 400 m give some shade")
        else:
            reasons.append(f"warm, feels like {feels:.0f}°C")
        s -= heat
    if wind > lim["wind_max"] or gust > lim["gust_max"]:
        blocks.append(f"wind {wind:.0f} km/h, gusts {gust:.0f} km/h")
    else:
        s -= wind / lim["wind_max"] * 10
    if aqi is not None:
        if aqi > lim["aqi_max"]:
            blocks.append(f"AQI {aqi} ({cat}; limit {lim['aqi_max']})")
        s -= aqi / lim["aqi_max"] * 20
    if uv is not None and uv > lim["uv_max"]:
        s -= (uv - lim["uv_max"]) * 5
        reasons.append(f"high UV {uv:.0f}")
    if not is_day:
        if lim["daylight"]:
            blocks.append("after dark")
        elif lim["label"] in NEEDS_FLOODLIGHTS and tags.get("lit") != "yes":
            blocks.append("after dark, and no floodlights are mapped here")
        elif lim["label"] != "photography":
            s -= 10 if tags.get("lit") != "yes" else 4
    if lim["hours"] and t.hour not in lim["hours"]:
        s -= 10
    if open_at(tags.get("opening_hours"), t) is False:
        blocks.append(f"closed then (opening hours {tags['opening_hours']})")
    if (venue or {}).get("maybe_private"):
        s -= 15
        reasons.append("may be members-only (name suggests a school, club or campus)")

    if lim["label"] == "photography" and sun:
        sr, ss = sun
        if timedelta(0) <= t - sr <= timedelta(hours=1) or timedelta(0) <= ss - t <= timedelta(hours=1):
            s += 20
            reasons.append("golden hour")
        elif not is_day:
            s -= 30
        else:
            s -= 15  # flat midday light
        if w.get("cloud_cover") is not None and 20 <= w["cloud_cover"] <= 60:
            s += 5
            reasons.append(f"broken cloud {w['cloud_cover']}%")

    if w.get("wind_direction_10m") is not None:
        pen, why = downwind(ctx, w["wind_direction_10m"], wind, lim["downwind_mult"], t)
        if why:
            s -= pen
            reasons.append(why)
    # low mixing height traps exhaust near busy roads whatever the wind direction (calm mornings, evenings)
    blh = w.get("boundary_layer_height")
    near = min(ctx["roads"], key=lambda r: r["dist_m"], default=None)
    if blh is not None and blh < STAGNANT_BLH_M and near and near["dist_m"] <= 500:
        s -= 8 * (1 - near["dist_m"] / 500) * lim["downwind_mult"] * traffic(t)
        reasons.append(f"stagnant air near {near['name']} (mixing height {blh:.0f} m)")
    if travel_min:
        trip = travel_min * (1.6 if is_rush(t) else 1.0)  # free-flow OSRM time, stretched in rush hour
        s -= min(15, trip / 4)
        if is_rush(t) and trip >= 20:
            reasons.append(f"about {trip:.0f} min by road at rush hour")

    # blocked hours keep a (heavily cut, possibly negative) score, so the least-bad option is the one that
    # fails by the least, not simply the first hour (clamping at 0 made them all tie)
    return min(100.0, s - 50 * len(blocks)) if blocks else max(0.0, min(100.0, s)), bool(blocks), blocks + reasons


def _next(a, b):
    return datetime.fromisoformat(b["time"]) - datetime.fromisoformat(a["time"]) == timedelta(hours=1)


def rank(venues, wxs, aqs, ctxs, travels, lim, start, end, hour_range=(0, 24), top=3):
    """Best (venue, time window) options plus hours to avoid at the top venue."""
    results = []
    for v, wx, aqj, ctx, tr in zip(venues, wxs, aqs, ctxs, travels):
        ws = _series(wx, [k for k in wx["hourly"] if k != "time"])
        aqs_ = _series(aqj, [k for k in aqj["hourly"] if k != "time"]) if aqj else {}
        suns = _sun(wx)
        hours = []
        for ts, w in ws.items():
            t = datetime.fromisoformat(ts)
            if not (start <= t < end and hour_range[0] <= t.hour < hour_range[1]) or w["apparent_temperature"] is None:
                continue
            aq = aqs_.get(ts, {})
            sc, blocked, why = score_hour(lim, w, aq, ctx, suns.get(ts[:10]), t, tr["min"] if tr else None, v)
            aqi, cat = naqi(aq.get("pm2_5"), aq.get("pm10"))
            hours.append({"time": ts, "score": round(sc), "blocked": blocked, "reasons": why,
                          "feels_c": w["apparent_temperature"], "rain_pct": w.get("precipitation_probability"),
                          "wind_kmh": w.get("wind_speed_10m"), "gust_kmh": w.get("wind_gusts_10m"),
                          "wind_from": None if w.get("wind_direction_10m") is None else compass(w["wind_direction_10m"]),
                          "uv": w.get("uv_index"), "aqi": aqi, "aqi_cat": cat, "us_aqi": aq.get("us_aqi"),
                          "pm2_5": aq.get("pm2_5"), "pm10": aq.get("pm10"), "mixing_height_m": w.get("boundary_layer_height"),
                          "weather_code": w.get("weather_code"), "is_day": w.get("is_day")})
        if not hours:
            continue
        best_i = max(range(len(hours)), key=lambda i: (not hours[i]["blocked"], hours[i]["score"]))
        best = hours[best_i]
        lo = hi = best_i  # grow a contiguous window of near-best hours
        while lo > 0 and _next(hours[lo - 1], hours[lo]) and not hours[lo - 1]["blocked"] and hours[lo - 1]["score"] >= best["score"] - 8:
            lo -= 1
        while hi < len(hours) - 1 and _next(hours[hi], hours[hi + 1]) and not hours[hi + 1]["blocked"] and hours[hi + 1]["score"] >= best["score"] - 8:
            hi += 1
        results.append({"venue": v, "travel": tr, "surroundings": ctx, "best": best,
                        "window": [hours[lo]["time"], (datetime.fromisoformat(hours[hi]["time"]) + timedelta(hours=1)).isoformat(timespec="minutes")],
                        "hours": hours, "sun": {d: [a.isoformat(), b.isoformat()] for d, (a, b) in suns.items()}})

    results.sort(key=lambda r: (not r["best"]["blocked"], r["best"]["score"]), reverse=True)
    avoid = []
    if results:
        avoid = [h for h in results[0]["hours"] if 5 <= datetime.fromisoformat(h["time"]).hour < 22
                 and "after dark" not in h["reasons"]
                 and (h["blocked"] or any("downwind" in r for r in h["reasons"]))]
    return results[:top], avoid


if __name__ == "__main__":
    # self-check: wind from the road's bearing penalises, opposite wind doesn't
    ctx = {"roads": [{"name": "EEH", "kind": "motorway", "dist_m": 300, "bearing": 70}], "industry": []}
    assert downwind(ctx, 75, 10, 1.0)[0] > 15
    assert downwind(ctx, 250, 10, 1.0)[0] == 0
    assert downwind(ctx, 75, 10, 2.0)[0] > downwind(ctx, 75, 10, 1.0)[0]
    # H6: weekday rush hour weighs the road more than Sunday morning
    rush, sunday = datetime(2026, 10, 26, 9), datetime(2026, 10, 25, 9)
    assert downwind(ctx, 75, 10, 1.0, rush)[0] > downwind(ctx, 75, 10, 1.0, sunday)[0]
    # M11: NAQI from CPCB breakpoints
    assert naqi(55, None) == (92, "Satisfactory") and naqi(95, 300)[1] == "Poor" and naqi(None, None) == (None, None)
    assert naqi(20, 40) == (40, "Good")
    # H8: simple opening hours
    assert open_at("06:00-21:00", datetime(2026, 10, 23, 22)) is False and open_at("Mo-Su 05:30-20:00", datetime(2026, 10, 23, 6))
    assert open_at("Mo-Fr 06:00-09:00", datetime(2026, 10, 23, 7)) is None and open_at("24/7", rush)
    # H2: an hour full of nulls scores instead of crashing
    lim = {"rain_max": 30, "feels_max": 35, "wind_max": 25, "gust_max": 40, "aqi_max": 150, "uv_max": 7, "daylight": False,
           "label": "photography", "hours": [], "downwind_mult": 1.0}
    nulls = {"apparent_temperature": 30, "precipitation_probability": None, "wind_speed_10m": None, "wind_gusts_10m": None,
             "uv_index": None, "is_day": None, "cloud_cover": None, "wind_direction_10m": None}
    score_hour(lim, nulls, {}, ctx, None, datetime(2026, 10, 23, 7), None)
    # H4: among blocked hours, two limits broken ranks below one
    hot = {**nulls, "apparent_temperature": 45, "is_day": 1}
    hot_dark = {**hot, "is_day": 0}
    dlim = {**lim, "label": "cricket", "daylight": True}
    assert score_hour(dlim, hot, {}, ctx, None, rush, None)[0] > score_hour(dlim, hot_dark, {}, ctx, None, rush, None)[0]
    # W3: thunderstorm blocks; low mixing height near a road costs points
    storm = {**nulls, "weather_code": 95, "is_day": 1}
    assert score_hour(lim, storm, {}, ctx, None, rush, None)[1]
    calm = {**nulls, "is_day": 1, "boundary_layer_height": 120}
    assert any("stagnant" in r for r in score_hour(lim, calm, {}, ctx, None, rush, None)[2])
    # H8: football after dark needs floodlights
    flim = {**lim, "label": "football"}
    night = {**nulls, "is_day": 0}
    assert score_hour(flim, night, {}, ctx, None, rush, None, {"tags": {}})[1]
    assert not score_hour(flim, night, {}, ctx, None, rush, None, {"tags": {"lit": "yes"}})[1]
    print("ok")
