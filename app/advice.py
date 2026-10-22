"""What to carry and watch for, computed from the hours the person will actually be out (not fixed lists)."""
import re
from datetime import datetime, timedelta

from app.rules import ACTIVITIES, PROFILES

# fixed activity tips about weather would contradict the data ("wet outfield after rain" at 0% rain); the data covers it
WEATHERY = re.compile(r"rain|wet|heat|wind|gust|lightning|humid|shower|haze|overheat", re.I)


def _t(ts):
    return datetime.fromisoformat(ts).strftime("%I:%M %p").lstrip("0")


def window_hours(rec):
    a, b = rec["window"]
    return [h for h in rec["hours"] if a <= h["time"] < b] or [rec["best"]]


def _dedupe(items):
    out, seen = [], set()
    for i in items:
        key = i.split(" (")[0].lower()
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out


def advice(activity, profiles, rec):
    """(carry, watch) lists, each item carrying the number that triggered it."""
    hs, ctx = window_hours(rec), rec["surroundings"]
    vals = lambda k: [h[k] for h in hs if h.get(k) is not None]
    carry, watch = [ACTIVITIES[activity]["carry"][0]], []

    rain, uv, gust = max(vals("rain_pct"), default=None), max(vals("uv"), default=None), max(vals("gust_kmh"), default=None)
    hot, cold = max(hs, key=lambda h: h["feels_c"]), min(hs, key=lambda h: h["feels_c"])
    smog = max((h for h in hs if h.get("aqi") is not None), key=lambda h: h["aqi"], default=None)
    if rain is not None and rain >= 30:
        carry.append(f"an umbrella or rain jacket (rain chance up to {rain:.0f}%)")
    if uv is not None and uv >= 6:
        carry.append(f"sunscreen and a cap (UV up to {uv:.0f})")
    if smog and smog["aqi"] > 100:
        carry.append(f"an N95 mask (AQI up to {smog['aqi']}, {smog['aqi_cat']})")
    if hot["feels_c"] >= 32:
        carry.append(f"extra water and electrolytes (feels like up to {hot['feels_c']:.0f}°C)")
    if cold["feels_c"] <= 15:
        carry.append(f"a warm layer (feels like {cold['feels_c']:.0f}°C)")

    start, end = (datetime.fromisoformat(x) for x in rec["window"])
    sun = rec.get("sun", {}).get(rec["window"][0][:10])
    water = min(ctx.get("water", []), key=lambda w: w["dist_m"], default=None)
    if sun:
        sunset = datetime.fromisoformat(sun[1])
        if end > sunset:
            carry.append(f"a torch or phone light (sunset {_t(sun[1])})")
        dusk = start < sunset + timedelta(hours=2) and end > sunset - timedelta(hours=1)
        if water and water["dist_m"] <= 500 and dusk and 6 <= start.month <= 11:  # monsoon and post-monsoon
            carry.append(f"mosquito repellent ({water['name']} is {water['dist_m']} m away; mosquitoes peak at dusk)")
    for p in profiles:
        carry += PROFILES[p]["carry"]

    if gust is not None and gust >= 30:
        watch.append(f"gusts up to {gust:.0f} km/h")
    storm = next((h for h in hs if (h.get("weather_code") or 0) >= 95), None)
    if storm:
        watch.append(f"thunderstorm forecast around {_t(storm['time'])}: leave open ground if you hear thunder")
    if hot["feels_c"] >= 35:
        watch.append(f"heat stress around {_t(hot['time'])} (feels like {hot['feels_c']:.0f}°C)")
    for key, say in (("downwind", "polluted air at {t}: {r}"), ("stagnant", "{r} around {t}")):
        h = next((h for h in hs if any(r.startswith(key) for r in h["reasons"])), None)
        if h:
            watch.append(say.format(t=_t(h["time"]), r=next(r for r in h["reasons"] if r.startswith(key))))
    if "child" in profiles and water and water["dist_m"] <= 200:
        watch.append(f"keep children away from the edge of {water['name']} ({water['dist_m']} m away)")
    watch += [w for w in ACTIVITIES[activity]["watch"] if not WEATHERY.search(w)]
    for p in profiles:
        watch += [w for w in PROFILES[p]["watch"] if not WEATHERY.search(w)]
    return _dedupe(carry), _dedupe(watch)


def why(rec):
    """One line on why this hour wins, with the numbers that decide it."""
    h, hs = rec["best"], rec["hours"]
    hot = max(hs, key=lambda x: x["feels_c"])
    parts = [f"feels like {h['feels_c']:.0f}°C" + (f", against {hot['feels_c']:.0f}°C at {_t(hot['time'])}"
                                                   if hot["feels_c"] - h["feels_c"] >= 2 else "")]
    if h.get("rain_pct") is not None:
        parts.append(f"{h['rain_pct']:.0f}% rain chance")
    if h.get("aqi") is not None:
        parts.append(f"AQI {h['aqi']} ({h['aqi_cat']})")
    if not any(r.startswith("downwind") for r in h["reasons"]):
        other = next((r for x in hs for r in x["reasons"] if r.startswith("downwind of ")), None)
        if other:
            parts.append(f"upwind of {other[len('downwind of '):].split(',')[0]} at this hour")
    if "golden hour" in h["reasons"]:
        parts.append("golden hour light")
    return "Why: " + "; ".join(parts) + "."


if __name__ == "__main__":
    hour = lambda ts, **k: {"time": ts, "feels_c": 30.0, "rain_pct": 0, "uv": 1, "gust_kmh": 10, "aqi": 80,
                            "aqi_cat": "Satisfactory", "reasons": [], "weather_code": 0, **k}
    rec = {"window": ["2026-10-23T17:00", "2026-10-23T20:00"],
           "best": hour("2026-10-23T18:00"),
           "hours": [hour("2026-10-23T14:00", feels_c=36.0, uv=9, reasons=["downwind of EEH (motorway), 300 m E (wind from E)"]),
                     hour("2026-10-23T17:00", rain_pct=40), hour("2026-10-23T18:00"), hour("2026-10-23T19:00")],
           "surroundings": {"water": [{"name": "Powai Lake", "dist_m": 150}], "green": 0, "roads": [], "industry": []},
           "sun": {"2026-10-23": ["2026-10-23T06:30", "2026-10-23T18:08"]}}
    carry, watch = advice("cricket", ["child"], rec)
    assert any("rain chance up to 40%" in c for c in carry) and any("sunset 6:08 PM" in c for c in carry)
    assert any("mosquito" in c for c in carry) and not any("sunscreen and a cap" in c for c in carry)  # UV 9 was outside
    assert any("Powai Lake" in w for w in watch) and not any("wet outfield" in w for w in watch)
    w = why(rec)
    assert "against 36°C at 2:00 PM" in w and "upwind of EEH (motorway)" in w, w
    print("ok")
