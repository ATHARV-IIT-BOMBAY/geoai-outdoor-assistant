"""Live data fetchers. Every call hits a free, open API at query time; responses are cached briefly."""
import asyncio
import math
import time

import httpx

UA = "geoai-outdoor-assistant/0.1 (GeoAI Hackathon 2026, IIT Bombay)"
# retries cover transient connect failures only (a judged run once hit a ConnectTimeout on a public API)
client = httpx.AsyncClient(headers={"User-Agent": UA}, timeout=30, follow_redirects=True,
                           transport=httpx.AsyncHTTPTransport(retries=2))

OVERPASS_MIRRORS = [
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# ponytail: in-process TTL cache, swap for redis/diskcache if we run >1 worker
_cache: dict[str, tuple[float, object]] = {}


async def cached(key: str, ttl: int, fn):
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    val = await fn()
    _cache[key] = (time.time() + ttl, val)
    return val


# ---------- geometry helpers ----------

def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Compass bearing from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(deg):
    return COMPASS[round(deg / 22.5) % 16]


# ---------- geocoding (Nominatim) ----------

BROAD_AREAS = {"state_district", "county", "state", "region", "country"}

_nominatim_lock = asyncio.Lock()
_nominatim_last = 0.0


async def geocode(place: str):
    async def fetch():
        global _nominatim_last
        async with _nominatim_lock:  # Nominatim policy: max 1 request/second
            wait = 1.0 - (time.time() - _nominatim_last)
            if wait > 0:
                await asyncio.sleep(wait)
            r = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": place, "format": "jsonv2", "limit": 5, "countrycodes": "in"},
            )
            _nominatim_last = time.time()
        r.raise_for_status()
        js = r.json()
        if js:
            # "Thane" also names a district whose centroid is 45 km out in farmland: prefer the settlement
            best = next((x for x in js if x.get("addresstype") not in BROAD_AREAS), js[0])
            return {"name": best["display_name"], "lat": float(best["lat"]), "lon": float(best["lon"])}
        # Nominatim is inconsistent on alternate names (Bangalore/Bengaluru); Photon (also OSM data) is better at them
        r = await client.get("https://photon.komoot.io/api/", params={"q": place, "limit": 5})
        feats = [f for f in r.json().get("features", []) if f["properties"].get("countrycode") == "IN"]
        if not feats:
            return None
        pr, (lon, lat) = feats[0]["properties"], feats[0]["geometry"]["coordinates"]
        name = ", ".join(x for x in (pr.get("name"), pr.get("city"), pr.get("state")) if x)
        return {"name": name, "lat": lat, "lon": lon}

    return await cached(f"geo:{place.lower()}", 86400, fetch)


# ---------- OpenStreetMap (Overpass) ----------

async def overpass(query: str):
    # Public Overpass servers are often at capacity, so race the mirrors and take the first good answer.
    async def one(url):
        r = await client.post(url, data={"data": query}, timeout=45)
        r.raise_for_status()
        js = r.json()
        # overloaded mirrors answer 200 with a "remark" and no elements; that's a failure, not "nothing here"
        if "error" in js.get("remark", "").lower() or "timed out" in js.get("remark", "").lower():
            raise ValueError(js["remark"][:80])
        return js["elements"]

    async def fetch():
        tasks = [asyncio.create_task(one(u)) for u in OVERPASS_MIRRORS]
        errors = []
        try:
            for fut in asyncio.as_completed(tasks):
                try:
                    return await fut
                except (httpx.HTTPError, ValueError, KeyError) as e:
                    errors.append(repr(e)[:80])
            raise RuntimeError(f"All Overpass mirrors failed: {errors}")
        finally:
            for t in tasks:
                t.cancel()

    return await cached(f"op:{hash(query)}", 3600, fetch)


def _center(el):
    if "lat" in el:
        return el["lat"], el["lon"]
    if "center" in el:
        return el["center"]["lat"], el["center"]["lon"]
    if "bounds" in el:
        b = el["bounds"]
        return (b["minlat"] + b["maxlat"]) / 2, (b["minlon"] + b["maxlon"]) / 2
    if "geometry" in el and el["geometry"]:
        g = el["geometry"]
        return sum(p["lat"] for p in g) / len(g), sum(p["lon"] for p in g) / len(g)
    return None


async def venues(lat, lon, radius_m, selectors, limit=8):
    """Named OSM features matching any selector (e.g. '[leisure=park]') within radius, nearest first."""
    body = "".join(f"nwr{s}(around:{radius_m},{lat},{lon});" for s in selectors)
    els = await overpass(f"[out:json][timeout:25];({body});out tags center 300;")
    seen, out = set(), []
    for el in els:
        c = _center(el)
        name = el.get("tags", {}).get("name:en") or el.get("tags", {}).get("name")
        if not c or not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({
            "name": name, "lat": c[0], "lon": c[1], "tags": el["tags"],
            "osm": f"{el['type']}/{el['id']}",
            "dist_m": round(haversine_m(lat, lon, *c)),
        })
    out.sort(key=lambda v: v["dist_m"])
    kept = []  # one pick per site: "Tennis Court 1/2/3" in the same complex are not alternatives
    for v in out:
        if all(haversine_m(v["lat"], v["lon"], k["lat"], k["lon"]) > 150 for k in kept):
            kept.append(v)
    return kept[:limit]


ROAD_R, INDUSTRY_R, WATER_R, GREEN_R = 800, 2500, 1000, 400
MIN_INDUSTRY_M2 = 30000  # drops single mislabelled shops; real estates/plants are far larger


def _area_m2(el):
    b = el.get("bounds")
    if not b:  # plain node: only trust it if it's tagged as a plant/works
        return MIN_INDUSTRY_M2 if el.get("tags", {}).get("power") == "plant" else 0
    h = haversine_m(b["minlat"], b["minlon"], b["maxlat"], b["minlon"])
    w = haversine_m(b["minlat"], b["minlon"], b["minlat"], b["maxlon"])
    return h * w * 0.6  # bbox overestimates a polygon; rough fill factor


async def surroundings(points):
    """Pollution sources, water and green cover around each venue, in one Overpass call."""
    roads = inds = water = green = ""
    for p in points:
        a = f"{p['lat']},{p['lon']}"
        roads += f'way[highway~"^(motorway|trunk|primary)$"](around:{ROAD_R},{a});'
        inds += (f"nwr[landuse=industrial](around:{INDUSTRY_R},{a});"
                 f"nwr[man_made=works](around:{INDUSTRY_R},{a});"
                 f"nwr[power=plant](around:{INDUSTRY_R},{a});"
                 f"nwr[landuse=landfill](around:{INDUSTRY_R},{a});")
        water += (f"nwr[natural=water](around:{WATER_R},{a});"
                  f"way[waterway=river](around:{WATER_R},{a});"
                  f"nwr[natural=beach](around:{WATER_R},{a});")
        green += (f"nwr[leisure~\"^(park|garden|nature_reserve)$\"](around:{GREEN_R},{a});"
                  f"nwr[landuse~\"^(forest|grass|meadow)$\"](around:{GREEN_R},{a});"
                  f"nwr[natural~\"^(wood|scrub)$\"](around:{GREEN_R},{a});")
    q = (f"[out:json][timeout:40];({roads})->.r;({inds})->.i;({water})->.w;({green})->.g;"
         ".r out tags geom;.i out tags bb;.w out tags center;.g out tags center;")
    els = await overpass(q)

    result = []
    for p in points:
        ctx = {"roads": [], "industry": [], "water": [], "green": 0}
        for el in els:
            t = el.get("tags", {})
            name = t.get("name:en") or t.get("name")
            if "highway" in t and el.get("geometry"):
                # nearest vertex is a good-enough distance to a road at this scale
                best = min([g for g in el["geometry"] if g] or [{"lat": 90, "lon": 0}], key=lambda g: haversine_m(p["lat"], p["lon"], g["lat"], g["lon"]))
                d = haversine_m(p["lat"], p["lon"], best["lat"], best["lon"])
                if d <= ROAD_R:
                    ctx["roads"].append({"name": name or t.get("ref") or f"{t['highway']} road",
                                         "kind": t["highway"], "dist_m": round(d),
                                         "bearing": round(bearing_deg(p["lat"], p["lon"], best["lat"], best["lon"])),
                                         "lat": best["lat"], "lon": best["lon"]})
                continue
            c = _center(el)
            if not c:
                continue
            d = haversine_m(p["lat"], p["lon"], *c)
            if t.get("landuse") in ("industrial", "landfill") or t.get("man_made") == "works" or t.get("power") == "plant":
                if d <= INDUSTRY_R and _area_m2(el) >= MIN_INDUSTRY_M2:
                    kind = t.get("landuse") or t.get("man_made") or "power plant"
                    ctx["industry"].append({"name": name or kind, "kind": kind, "dist_m": round(d),
                                            "bearing": round(bearing_deg(p["lat"], p["lon"], *c)),
                                            "lat": c[0], "lon": c[1]})
            elif t.get("natural") in ("water", "beach") or t.get("waterway") == "river":
                if d <= WATER_R:
                    ctx["water"].append({"name": name or t.get("natural") or "river", "dist_m": round(d)})
            elif d <= GREEN_R:
                ctx["green"] += 1
        # keep the closest few of each, deduped by name
        for k in ("roads", "industry", "water"):
            uniq = {}
            for x in sorted(ctx[k], key=lambda x: x["dist_m"]):
                uniq.setdefault(x["name"], x)
            ctx[k] = list(uniq.values())[:4]
        result.append(ctx)
    return result


# ---------- weather + air quality (Open-Meteo) ----------

HOURLY = ("temperature_2m,apparent_temperature,relative_humidity_2m,precipitation_probability,precipitation,"
          "wind_speed_10m,wind_direction_10m,wind_gusts_10m,uv_index,cloud_cover,visibility,is_day")
AQ_HOURLY = "pm2_5,pm10,us_aqi,nitrogen_dioxide,ozone"


def _multi(points):
    return ",".join(f"{p['lat']:.4f}" for p in points), ",".join(f"{p['lon']:.4f}" for p in points)


async def weather(points, days=7):
    lats, lons = _multi(points)

    async def fetch():
        r = await client.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lats, "longitude": lons, "hourly": HOURLY, "daily": "sunrise,sunset",
            "timezone": "Asia/Kolkata", "forecast_days": days})
        r.raise_for_status()
        js = r.json()
        return js if isinstance(js, list) else [js]

    return await cached(f"wx:{lats}:{lons}:{days}", 900, fetch)


async def air_quality(points, days=5):
    lats, lons = _multi(points)

    async def fetch():
        r = await client.get("https://air-quality-api.open-meteo.com/v1/air-quality", params={
            "latitude": lats, "longitude": lons, "hourly": AQ_HOURLY,
            "timezone": "Asia/Kolkata", "forecast_days": days})
        r.raise_for_status()
        js = r.json()
        return js if isinstance(js, list) else [js]

    return await cached(f"aq:{lats}:{lons}:{days}", 1800, fetch)


# ---------- travel time (OSRM public demo server) ----------

async def travel(origin, points):
    """Driving duration (min) and distance (km) from origin to each point; None on failure."""
    coords = ";".join(f"{p['lon']:.5f},{p['lat']:.5f}" for p in [origin, *points])

    async def fetch():
        try:
            r = await client.get(f"https://router.project-osrm.org/table/v1/driving/{coords}",
                                 params={"sources": 0, "annotations": "duration,distance"}, timeout=15)
            js = r.json()
            if js.get("code") != "Ok":
                return [None] * len(points)
            return [{"min": round(d / 60), "km": round(k / 1000, 1)} if d is not None else None
                    for d, k in zip(js["durations"][0][1:], js["distances"][0][1:])]
        except (httpx.HTTPError, ValueError):
            return [None] * len(points)

    return await cached(f"osrm:{coords}", 3600, fetch)
