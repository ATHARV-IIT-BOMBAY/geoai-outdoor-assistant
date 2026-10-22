"""Activity sensitivities and personal-profile adjustments. All thresholds live here so they're easy to tune."""

# Limits (calibration knobs, tuned for Indian climate): feels_max (°C apparent temp, NWS heat-index bands), rain_max (% precip probability), wind_max/gust_max (km/h),
# aqi_max (India NAQI from CPCB: 101-200 Moderate, 201-300 Poor), uv_max. daylight=True means the activity needs it. hours = preferred local hours.
# keywords match whole words (plural "s"/"es" allowed); a trailing * makes one a prefix ("breath*" -> breathless).
# venues = OpenStreetMap tag filters for Overpass. radius_km = default search radius.
ACTIVITIES = {
    "running": dict(
        label="running", keywords=["run", "running", "runner", "jog", "jogging", "sprint", "marathon"],
        venues=["[leisure=park]", "[leisure=track]", "[leisure=garden]", "[leisure=nature_reserve]"],
        feels_max=37, rain_max=40, wind_max=30, gust_max=45, aqi_max=200, uv_max=7, daylight=False,
        hours=[5, 6, 7, 8, 17, 18, 19], radius_km=6,
        carry=["water bottle", "light breathable clothing"],
        watch=["heat and humidity", "traffic at road crossings"]),
    "cycling": dict(
        label="cycling", keywords=["cycle", "cycling", "bike", "biking", "bicycle", "ride", "riding"],
        venues=["[leisure=park]", "[highway=cycleway]", "[leisure=nature_reserve]", "[natural=beach]"],
        feels_max=38, rain_max=30, wind_max=25, gust_max=40, aqi_max=200, uv_max=7, daylight=True,
        hours=[6, 7, 8, 17, 18], radius_km=10,
        carry=["helmet", "water", "front and rear lights"],
        watch=["crosswinds and gusts", "wet roads", "heavy traffic on arterial roads"]),
    "golf": dict(
        label="golf", keywords=["golf", "tee", "putting", "driving range"],
        venues=["[leisure=golf_course]"],
        feels_max=38, rain_max=30, wind_max=22, gust_max=35, aqi_max=250, uv_max=8, daylight=True,
        hours=[6, 7, 8, 9, 16, 17], radius_km=40,
        carry=["sunscreen", "cap", "plenty of water"],
        watch=["lightning", "gusty wind affecting ball flight"]),
    "cricket": dict(
        label="cricket", keywords=["cricket", "nets", "batting", "bowling"],
        venues=["[leisure=pitch][sport=cricket]", "[leisure=stadium][sport=cricket]",
                "[leisure=sports_centre][sport=cricket]", "[leisure=pitch][sport=multi]", "[leisure=playground]"],
        feels_max=38, rain_max=30, wind_max=35, gust_max=50, aqi_max=200, uv_max=8, daylight=True,
        hours=[7, 8, 9, 16, 17], radius_km=8,
        carry=["water and electrolytes", "cap", "sunscreen"],
        watch=["heat exhaustion during long spells", "wet outfield after rain"]),
    "football": dict(
        label="football", keywords=["football", "soccer", "five-a-side", "5-a-side", "futsal"],
        venues=["[leisure=pitch][sport=soccer]", "[leisure=stadium][sport=soccer]",
                "[leisure=sports_centre][sport=soccer]", "[leisure=pitch][sport=multi]"],
        feels_max=37, rain_max=50, wind_max=35, gust_max=50, aqi_max=200, uv_max=8, daylight=False,
        hours=[6, 7, 17, 18, 19, 20], radius_km=8,
        carry=["water and electrolytes", "spare shirt"],
        watch=["slippery turf after rain", "heat stress"]),
    "tennis": dict(
        label="tennis", keywords=["tennis"],
        venues=["[leisure=pitch][sport=tennis]", "[leisure=sports_centre][sport=tennis]"],
        feels_max=37, rain_max=25, wind_max=20, gust_max=30, aqi_max=200, uv_max=7, daylight=False,
        hours=[6, 7, 8, 17, 18, 19], radius_km=8,
        carry=["water", "towel", "sunscreen"],
        watch=["wind affecting play", "wet, slippery courts"]),
    "hiking": dict(
        label="hiking", keywords=["hike", "hiking", "trek", "trekking", "trail", "nature walk"],
        venues=["[natural=peak]", "[leisure=nature_reserve]", "[tourism=viewpoint]", "[boundary=national_park]",
                "[natural=waterfall]"],
        feels_max=36, rain_max=40, wind_max=30, gust_max=45, aqi_max=200, uv_max=7, daylight=True,
        hours=[6, 7, 8, 9, 10], radius_km=40,
        carry=["2+ litres of water", "trail shoes with grip", "rain jacket", "first-aid kit", "charged phone"],
        watch=["slippery rock after rain", "returning before sunset", "flash streams in monsoon"]),
    "walking": dict(
        label="walking", keywords=["walk", "walking", "stroll", "morning walk", "evening walk"],
        venues=["[leisure=park]", "[leisure=garden]", "[leisure=nature_reserve]", "[natural=beach]"],
        feels_max=37, rain_max=40, wind_max=35, gust_max=50, aqi_max=200, uv_max=7, daylight=False,
        hours=[5, 6, 7, 8, 17, 18, 19], radius_km=5,
        carry=["water", "comfortable shoes"],
        watch=["heat around midday", "uneven paths"]),
    "picnic": dict(
        label="picnic", keywords=["picnic", "park outing", "outing", "day out"],
        venues=["[leisure=park]", "[leisure=garden]", "[tourism=picnic_site]", "[natural=beach]"],
        feels_max=37, rain_max=25, wind_max=25, gust_max=40, aqi_max=250, uv_max=7, daylight=True,
        hours=[8, 9, 10, 16, 17], radius_km=10,
        carry=["mat", "shade/umbrella", "drinking water", "insect repellent"],
        watch=["afternoon showers", "food spoiling in heat"]),
    "event": dict(
        label="outdoor event", keywords=["party", "parties", "birthday", "wedding", "fair", "event", "gathering",
                                         "function", "celebration", "reception", "mela"],
        venues=["[leisure=park]", "[leisure=garden]", "[amenity=events_venue]", "[landuse=recreation_ground]"],
        feels_max=36, rain_max=20, wind_max=25, gust_max=35, aqi_max=250, uv_max=7, daylight=False,
        hours=[16, 17, 18, 19, 20], radius_km=10,
        carry=["canopy or tent as rain backup", "fans/coolers", "drinking water for guests", "mosquito repellent"],
        watch=["rain probability on the day", "gusts that can topple decor", "parking near main roads"]),
    "photography": dict(
        label="photography", keywords=["photo", "photography", "shoot", "shooting", "sunset", "sunrise",
                                       "golden hour", "pictures"],
        venues=["[tourism=viewpoint]", "[natural=beach]", "[man_made=pier]", "[historic=fort]",
                "[natural=peak]"],
        feels_max=40, rain_max=30, wind_max=30, gust_max=45, aqi_max=300, uv_max=11, daylight=False,
        hours=[], radius_km=10,
        carry=["lens cloth", "spare battery", "tripod for low light"],
        watch=["haze reducing visibility", "crowds at popular viewpoints"]),
}

# Profile adjustments: deltas on activity limits and multipliers on penalties.
PROFILES = {
    "asthma": dict(keywords=["asthma", "asthmatic", "breath*", "inhaler", "lung", "copd", "wheez*"],
                   aqi_factor=0.5, downwind_mult=2.0, feels_delta=-1,
                   carry=["reliever inhaler"], watch=["pollution peaks near main roads", "dust on windy days"]),
    "senior": dict(keywords=["parents", "senior", "elderly", "old age", "old parents", "grandparent", "grandma", "grandpa",
                             "grandfather", "grandmother", "aged", "retired"],
                   aqi_factor=0.75, downwind_mult=1.5, feels_delta=-3, uv_delta=-2,
                   carry=["regular medication", "walking stick if used"], watch=["heat exhaustion", "uneven ground"]),
    "child": dict(keywords=["kid", "kiddo", "child", "children", "toddler", "son", "daughter", "baby", "babies"],
                  aqi_factor=0.6, downwind_mult=1.5, feels_delta=-2, uv_delta=-2,
                  carry=["extra water", "sunscreen for kids"], watch=["supervision near water and roads"]),
    "pregnant": dict(keywords=["pregnant", "pregnancy", "expecting"],
                     aqi_factor=0.6, downwind_mult=1.8, feels_delta=-4, uv_delta=-2,
                     carry=["water", "snacks"], watch=["overheating", "long travel"]),
}


def limits_for(activity: str, profiles: list[str]) -> dict:
    """Activity limits tightened by every matching personal profile."""
    lim = dict(ACTIVITIES[activity])
    lim["downwind_mult"] = 1.0
    for p in profiles:
        pr = PROFILES[p]
        lim["aqi_max"] = round(lim["aqi_max"] * pr.get("aqi_factor", 1))
        lim["feels_max"] += pr.get("feels_delta", 0)
        lim["uv_max"] += pr.get("uv_delta", 0)
        lim["downwind_mult"] = max(lim["downwind_mult"], pr.get("downwind_mult", 1))
        lim["carry"] = lim["carry"] + pr["carry"]
        lim["watch"] = lim["watch"] + pr["watch"]
    return lim
