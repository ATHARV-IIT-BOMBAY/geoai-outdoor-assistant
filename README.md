# GeoAI Outdoor Assistant

GeoAI Hackathon 2026 (CSRE, IIT Bombay), PS-01 *"Where & when to head outdoors"*.

Ask a free-form question such as *"kal subah Bandra mein jogging, mujhe asthma hai"* or *"cricket in Andheri this weekend?"*.
The assistant picks the best nearby place and time window, and says what to carry, what to watch for and which hours to avoid.
Every fact comes from data fetched live for that question.

- **Live portal:** https://32-237-40-94.sslip.io (map, answer card and an hour-by-hour grid; API docs at `/docs`)
- **Model repo:** https://huggingface.co/AtharvIITB/geoai-outdoor-slm

## API

```bash
curl -X POST https://32-237-40-94.sslip.io/ask \
  -H 'content-type: application/json' \
  -d '{"q": "Best time to go running near Powai tomorrow morning? I have asthma"}'
```

**API endpoint:** `https://32-237-40-94.sslip.io/ask` (GET or POST; CORS enabled).

- The question can be sent as `q`, `question`, `query`, `prompt` or `text`: in a JSON body, a form, a plain-text body, or as a GET parameter (up to 500 characters).
- `lat`/`lon` are optional and used when the question names no place (for example "near me").
- `format=text` returns just the answer as plain text.

The JSON response contains:

| Field | What it holds |
|---|---|
| `status` | `ok`, `clarify`, `no_venues` or `error` |
| `summary` | One-paragraph answer |
| `answer` | Full text answer |
| `caution` | Whether no hour meets the safety limits |
| `searched` | The area that was searched around |
| `comparison` | Per-day bests, for "Saturday or Sunday?" questions |
| `recommendations` | Top 3 places, each with hour-by-hour scores and reasons |
| `avoid` | Hours to avoid, with reasons |
| `parsed` | How the question was understood |
| `limits` | The safety limits applied |
| `sources` | Data sources used |
| `fetched_at` | When the data was fetched |
| `view_url` | Opens this answer in the portal |

## How it works

```
question → parse (rules + SLM, JSON-schema constrained) → geocode → OSM venues for the activity
        → per venue: major roads / industry / water / green cover, hourly weather + air quality, travel time
        → score every venue × hour → top 3 + hours to avoid → SLM writes a short summary → number check
```

- **Plain code decides; the model only reads and phrases.** Scoring uses per-activity limits, tightened for asthma, senior, child and pregnancy profiles. The limits cover heat index, rain, wind and gusts, air quality on **India's NAQI scale** (CPCB breakpoints), UV, daylight and preferred hours.
- **Wind-aware pollution exposure:** a venue gets a penalty in any hour when the forecast wind blows from a nearby highway, factory or landfill towards it (with a ±45° cone and distance falloff).
  - **Traffic:** road exposure is weighted by the hour, with weekday rush hours heavier and nights lighter. Travel time is stretched at rush hour.
  - **Mixing height:** when the atmospheric mixing height is low (under 300 m, as on calm mornings), the venue gets a stagnant-air penalty near busy roads, whatever the wind direction.
- **More geospatial context:**
  - Thunderstorm forecasts (WMO weather codes) block an hour; high storm energy (CAPE) costs points.
  - Mapped green areas soften the heat penalty.
  - Water bodies trigger dusk mosquito advice and, for children, water-edge warnings.
- **Venue checks (OpenStreetMap):**
  - Places with private or members-only access, and non-venues such as cinemas or toilets, are dropped.
  - School, club and campus grounds are flagged.
  - Evening football, tennis and cricket need mapped floodlights.
  - Simple opening hours are respected.
- **Advice from the data:** each "carry" and "watch for" item comes from the hours you'll actually be out, with its number ("UV up to 9", "rain chance up to 40%", "sunset 6:08 PM"). Every answer shows the place it searched around, why the hour wins, and the data used, with its fetch time.
- **Grounding guards:** the model's summary is discarded if it contains any number or time not present in the computed facts. Safety cautions are never paraphrased. Places and profiles must come from the user's own words.
- **Graceful input handling:** a missing activity or place gets a clarifying question. Dates like "tomorrow", "this weekend", "Saturday evening", "after work", "kal shaam" and ages ("5 year old") are understood. If no hour is safe, the assistant names the least-bad option with a clear caution.
- **Activities (11):** running, cycling, golf, cricket, football, tennis, hiking, walking, picnic, outdoor event, photography.

## Model

[`Qwen/Qwen3-1.7B`](https://huggingface.co/Qwen/Qwen3-1.7B) (2.0B parameters, Apache-2.0) as an unmodified Q4_K_M GGUF, run with llama.cpp on CPU.
There's no fine-tuning; the prompts, schema and settings are in the model repo's `geoai_config.json`.

## Data sources (all free and open, fetched at query time)

| Data | Source | Licence |
|---|---|---|
| Venues, roads, industry, water, green cover | OpenStreetMap via Overpass API | © OpenStreetMap contributors, ODbL |
| Geocoding | OpenStreetMap Nominatim, Photon (komoot) fallback | ODbL |
| Hourly weather, sunrise/sunset | Open-Meteo Forecast API | CC BY 4.0 |
| Air quality (PM2.5, PM10 → India NAQI) | Open-Meteo Air Quality API (CAMS) | CC BY 4.0 |
| Mixing height, thunderstorm code, CAPE | Open-Meteo Forecast API | CC BY 4.0 |
| Map tiles in the portal | OpenFreeMap (OpenMapTiles, © OpenStreetMap contributors); MapLibre GL JS | ODbL / BSD |
| Travel time | OSRM public demo server | OSM data, ODbL |

## Run locally

```bash
uv sync
hf download AtharvIITB/geoai-outdoor-slm Qwen3-1.7B-Q4_K_M.gguf --local-dir models
uv run uvicorn app.main:app --port 8000
```

Without the model file, the API still works using rule-based parsing and template answers.
Self-checks: `uv run python -m app.parse`, `-m app.score`, `-m app.llm`.

## Limitations

- Air quality is a CAMS model forecast, not a station measurement. NAQI officially uses 24-hour averages; we apply its breakpoints to hourly values, which is an approximation.
- OpenStreetMap coverage varies: some venues are unnamed or mis-tagged. Access, floodlights and opening hours are only as good as their tags.
- There is no free live-traffic source. Rush-hour effects on travel time and road exposure are a disclosed heuristic (weekdays 8–11 AM and 5–9 PM), applied to OSRM's free-flow driving times.
- Green cover counts mapped parks and woods within 400 m; it isn't measured shade.
- Responses take roughly 15–25 s on a 2-vCPU CPU server (AWS m7i-flex.large, Sydney), mostly spent waiting on the public APIs.

## Disclosure

Claude (Anthropic) was used as a coding aid during development. No hosted large model runs in the assistant.
