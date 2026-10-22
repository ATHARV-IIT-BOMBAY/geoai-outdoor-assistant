"""Score every venue x hour against activity/profile limits. Pure code: no model, no network."""
from datetime import datetime, timedelta

from app.data import INDUSTRY_R, ROAD_R, compass

# Base penalty for being directly downwind of a pollution source, before distance falloff and profile multiplier.
SOURCE_PENALTY = {"motorway": 25, "trunk": 18, "primary": 10, "industrial": 25, "works": 25,
                  "power plant": 30, "landfill": 20}
DOWNWIND_CONE = 45  # degrees either side of the wind's origin


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


def downwind(ctx, wind_from, wind_kmh, mult):
    """Worst pollution source the venue sits downwind of this hour, as (penalty, description)."""
    worst = (0, None)
    for src in ctx["roads"] + ctx["industry"]:
        if _angle_diff(wind_from, src["bearing"]) > DOWNWIND_CONE:
            continue
        kind = src["kind"]
        falloff = max(0.0, 1 - src["dist_m"] / (ROAD_R if kind in ('motorway', 'trunk', 'primary') else INDUSTRY_R))
        calm = 0.6 if wind_kmh < 4 else 1.0  # near-calm air: plume doesn't travel as cleanly
        pen = SOURCE_PENALTY.get(kind, 15) * falloff * calm * mult
        if pen > worst[0]:
            label = src["name"] if src["name"] == kind else f"{src['name']} ({kind})"
            worst = (pen, f"downwind of {label}, {src['dist_m']} m {compass(src['bearing'])} "
                          f"(wind from {compass(wind_from)})")
    return worst


def score_hour(lim, w, aq, ctx, sun, t, travel_min):
    """(score 0-100, blocked: bool, reasons: list[str]) for one venue at one hour."""
    reasons, blocks, s = [], [], 100.0
    feels, rain, wind, gust = w["apparent_temperature"], w["precipitation_probability"], w["wind_speed_10m"], w["wind_gusts_10m"]
    uv, is_day = w["uv_index"], w["is_day"]
    aqi = aq.get("us_aqi")

    if rain is not None:
        if rain > lim["rain_max"]:
            blocks.append(f"rain chance {rain}% (limit {lim['rain_max']}%)")
        s -= rain / lim["rain_max"] * 20
    if feels > lim["feels_max"]:
        blocks.append(f"feels like {feels:.0f}°C (limit {lim['feels_max']}°C)")
    elif feels > lim["feels_max"] - 6:
        s -= (feels - (lim["feels_max"] - 6)) * 3
        reasons.append(f"warm, feels like {feels:.0f}°C")
    if wind > lim["wind_max"] or gust > lim["gust_max"]:
        blocks.append(f"wind {wind:.0f} km/h, gusts {gust:.0f} km/h")
    else:
        s -= wind / lim["wind_max"] * 10
    if aqi is not None:
        if aqi > lim["aqi_max"]:
            blocks.append(f"US AQI {aqi} (limit {lim['aqi_max']})")
        s -= aqi / lim["aqi_max"] * 20
    if uv is not None and uv > lim["uv_max"]:
        s -= (uv - lim["uv_max"]) * 5
        reasons.append(f"high UV {uv:.0f}")
    if not is_day:
        if lim["daylight"]:
            blocks.append("after dark")
        elif lim["label"] != "photography":
            s -= 10
    if lim["hours"] and t.hour not in lim["hours"]:
        s -= 10

    if lim["label"] == "photography" and sun:
        sr, ss = sun
        if timedelta(0) <= t - sr <= timedelta(hours=1) or timedelta(0) <= ss - t <= timedelta(hours=1):
            s += 20
            reasons.append("golden hour")
        elif not is_day:
            s -= 30
        if 20 <= w["cloud_cover"] <= 60:
            s += 5
            reasons.append(f"broken cloud {w['cloud_cover']}%")

    pen, why = downwind(ctx, w["wind_direction_10m"], wind, lim["downwind_mult"])
    if why:
        s -= pen
        reasons.append(why)
    if travel_min:
        s -= min(15, travel_min / 4)

    # blocked hours keep a (heavily cut) score so we can still name the least-bad option
    return max(0.0, min(100.0, s - 50 * len(blocks))), bool(blocks), blocks + reasons


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
            sc, blocked, why = score_hour(lim, w, aq, ctx, suns.get(ts[:10]), t,
                                          tr["min"] if tr else None)
            hours.append({"time": ts, "score": round(sc), "blocked": blocked, "reasons": why,
                          "feels_c": w["apparent_temperature"], "rain_pct": w["precipitation_probability"],
                          "wind_kmh": w["wind_speed_10m"], "wind_from": compass(w["wind_direction_10m"]),
                          "uv": w["uv_index"], "aqi": aq.get("us_aqi"), "pm2_5": aq.get("pm2_5")})
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
                        "hours": hours})

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
    print("ok")
