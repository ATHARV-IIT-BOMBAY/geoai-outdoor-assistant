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

`GET /ask?q=...` works too. `lat`/`lon` are optional and used when the question names no place.
The response contains `answer` (plain text), `status` (`ok` / `clarify` / `no_venues` / `error`),
`recommendations` (top 3 places, each with an hour-by-hour score and reasons), `avoid`, `parsed`, `limits`, `sources` and `fetched_at`.

## How it works

```
question → parse (rules + SLM, JSON-schema constrained) → geocode → OSM venues for the activity
        → per venue: major roads / industry / water / green cover, hourly weather + air quality, travel time
        → score every venue × hour → top 3 + hours to avoid → SLM writes a short summary → number check
```

- **Plain code decides; the model only reads and phrases.** Scoring uses per-activity limits (heat index, rain, wind and gusts, US AQI, UV, daylight, preferred hours), tightened for asthma, senior, child and pregnancy profiles.
- **Wind-aware pollution exposure:** a venue gets a penalty in any hour when the forecast wind blows from a nearby highway, factory or landfill towards it (with a ±45° cone and distance falloff).
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
| Air quality (PM2.5, PM10, US AQI) | Open-Meteo Air Quality API (CAMS) | CC BY 4.0 |
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

- CAMS air-quality forecasts often read high for Indian cities, so many Mumbai and Delhi answers carry a caution.
- OpenStreetMap coverage varies: some venues are unnamed or mis-tagged, and opening hours or access aren't checked.
- Travel times come from OSRM driving routes without live traffic.
- Responses take roughly 15–25 s on a 2-vCPU CPU server (AWS m7i-flex.large, Sydney), mostly spent waiting on the public APIs.

## Disclosure

Claude (Anthropic) was used as a coding aid during development. No hosted large model runs in the assistant.
