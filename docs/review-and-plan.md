# GeoAI Outdoor Assistant: review and plan to win

Written 22 Oct 2026, about 20:30 IST. The deadline is 24 Oct, 11:59 PM IST, which leaves about 51 hours.
Nothing in the code was changed while writing this. Every finding below was checked by reading the code or by running it against the live server; the evidence is in the appendix.

This file is a work order. Each item has an ID, the problem, the evidence, the fix, the files to change, and a test that shows it's done.
Work through it in tier order (section 5). Don't commit this file to the public repo; it holds internal infrastructure details.

---

## 1. Summary

**Where we stand.** The base is sound: data is fetched live, code does the reasoning, the SLM is guarded, there's a portal, and the API is deployed. But testing found **wrong answers on the official example questions**, and the system **can confidently plan in the wrong city**. Both are the kind of failure a judge notices in their first five questions. Fix those before adding any feature.

**The ten things that matter most, in order:**
1. Fix place extraction and geocoding (C1–C3). "running near me" currently plans running in **Noida**, "Sector 18 Noida" resolves to **Kolkata**, and "Hinjewadi" resolves to a **flyover**.
2. Support comparisons and yes/no verdicts (C4, M6). The official example "Is **Saturday or Sunday** better for a hike near Lonavala?" only looks at Saturday.
3. Move a time window forward once it has passed (C5). "Sunset photos this evening", asked at 8 PM, recommends a night shoot at a place called **"Trash Collector"**.
4. Make the server hold up under parallel judging (C6). Four simultaneous questions took 13, 23, 32 and 42 s.
5. Restructure the answer: verdict first, the place it searched, why, then carry/watch/avoid, then the data line (H13). Make carry/watch data-driven instead of fixed lists (H5).
6. Use **every** factor the problem statement names. Water bodies and green cover are fetched but never used; traffic doesn't vary with the hour; rush-hour travel is missing even though it's the statement's own example (H6, H7).
7. Cite a source for every claim: an `evidence[]` list with OSM IDs, source URLs and fetch times (W2). The statement asks to "support every claim with the live data it actually used".
8. Build an evaluation harness with metrics, so we can say "we measured this" (W1). It doubles as the regression suite for every change after this.
9. Fix `from_pretrained` compliance on Hugging Face (H9), ideally with a small LoRA fine-tune so the model slide has substance (W6).
10. Lean into the remote-sensing angle for a CSRE jury: ESA WorldCover green cover, Copernicus DEM terrain, mixing height, and CPCB ground stations (W3–W5).

**A correction to something I said earlier:** I told you IIT Bombay Wi-Fi couldn't reach the server. **That was wrong.** I had tested before the server finished starting. Today, at about 20:00, both `https://32-237-40-94.sslip.io/health` and `http://32.237.40.94/health` returned 200 from your Mac on campus. Judges on campus can use it.

---

## 2. How we'll likely be judged, and how we score today

The official statement (PS-01) asks the assistant to do the following. The status column is our honest standing today.

| What PS-01 asks for | Status today |
|---|---|
| Identify the activity, the area or radius, the time range, and personal details like age or health | ⚠️ Mostly works. Fails on "in the next two days", "near me", "Saturday or Sunday" and Devanagari "kal" (C1, C2, C4, H1). |
| Consider several nearby places across the coming hours | ✅ 8 venues × every hour |
| Use wind direction | ✅ downwind cone |
| Use roads **and traffic** | ⚠️ Roads yes; traffic doesn't vary with the hour (H6) |
| Use nearby industry | ✅ |
| Use **water bodies** | ❌ Fetched, never used (H7) |
| Use **green cover** | ❌ Fetched as a count, never used (H7) |
| Give a clear recommendation | ⚠️ No verdict sentence; doesn't say where it searched (H13) |
| Say what to carry, watch for, and when to avoid | ⚠️ Carry and watch are fixed lists, so they sometimes contradict the data (H5) |
| Support **every claim** with the live data used | ⚠️ Sources are listed, but claims aren't linked to them (W2) |
| Don't invent weather or places | ⚠️ Mostly. The writer still says things like "safe", "pleasant weather", "good spot" (C9) |
| Present richly (maps, images) | ✅ The portal. The API could link to it (H10). |
| Handle messy input "gracefully rather than guessing" | ⚠️ Mostly. But "near me" gets geocoded, and "is it nice outside" assumes a walk, which is a guess (C2, H12) |
| An open model ≤3B, public on HF, loads with `from_pretrained(...)` | ⚠️ GGUF only. A plain transformers `from_pretrained(repo)` fails (H9) |
| The SLM "reasons about place and time" | ⚠️ Code reasons. The SLM parsed only 3 of 12 official-style questions and its summary is rejected about 60% of the time (M9, C9) |
| Live API available through judging | ✅ Up, but no monitoring and no concurrency protection (C6, H11) |
| README covers run steps, base model, data sources and endpoint | ⚠️ Missing OpenFreeMap tiles, Photon and MapLibre; no explicit "API endpoint" line or response schema (M10) |
| Slides (4–6): problem/approach, architecture, worked example, model and fine-tuning, limitations | ❌ Not started |
| Ready to demo live and defend | ❌ No demo script, Q&A prep or backup video yet |

What the judges will probably reward (inferred from the statement; the actual criteria aren't published): correct, grounded answers to *new* questions; visible geospatial reasoning; graceful handling of messy input; a credible model story (fine-tuning plus measurements); clarity of the response and demo; honest limitations. The jury is CSRE, a remote-sensing and geoinformatics department, so Earth-observation data and spatial analysis will land especially well. Their own website illustrates Mumbai as layers: coastline and water, green cover, roads and rail, and "road density · hexes".

---

## 3. Verified issues

Severity:
- **Critical:** a wrong answer, or a likely failure during judging.
- **High:** a visible gap against the statement.
- **Medium:** quality or robustness.
- **Low:** polish.

### Critical

**C1. Place extraction swallows the words that follow the place.** *`app/parse.py`*
- Evidence: "near Powai **in the next two days**" (an official example) → `"Powai in the"`. "Mumbai in the evening" → `"Mumbai in the"`. "Lonavala in the next 3 days" → `"Lonavala in the"`. "around Aarey Colony from 6 to 8 am" → `"Aarey Colony from"`. "the park near Powai" → `"the park near Powai"`, which fails to geocode. "Sector 18 Noida" → `"Sector"`. Geocoding those strings gives: a housing society in Powai (right by luck), the **US Consulate** in BKC, a **resort 8 km from Lonavala**, and **Salt Lake Sector in Kolkata**.
- Root cause: `STOP` doesn't include prepositions or articles (`in, the, a, an, from, by, near, at, of`), and the word pattern `[a-z][\w.'-]*` rejects number tokens like `18`.
- Fix:
  - Add the prepositions and articles to `STOP`.
  - Allow number tokens after the first word ("Sector 18", "Phase 2", "Block C").
  - Strip leading articles.
  - Build an ordered list of place candidates and geocode them in turn until one passes validation (C3): the rule-based place, then the SLM's place, then trimmed variants.
  - The rules-only shortcut in `app/main.py` (`understand`) must not skip validation.
- Done when the cases above parse to `Powai`, `Mumbai`, `Lonavala`, `Aarey Colony`, `Powai` and `Sector 18 Noida`, and all six geocode to the right city.

**C2. "near me", "here", "nearby" and "my area" are treated as place names.** *`app/parse.py`, `app/main.py`, portal*
- Evidence: "running near me tomorrow" planned for **"Me, Jaypee Greens, Noida"** and answered with a Noida park, under an AQI caution.
- Fix:
  - Treat these words as "no place given".
  - Use `lat`/`lon` if they were sent, otherwise ask.
  - In the portal, if the question has no place, prompt for location before sending.
- Done when "running near me" returns `clarify` without `lat`/`lon`, and plans around those coordinates when they're sent.

**C3. Geocoding silently picks the wrong feature, and the answer never says where it searched.** *`app/data.py` `geocode`, `app/main.py` `template_answer`*
- Evidence: "Hinjewadi" → **"Wakad Flyover, Parkhe Vasti"**. This is a regression from yesterday's Thane fix: `BROAD_AREAS` now takes the *first non-district* result, which can be a road. "Sector" → Kolkata. "me" → Noida.
- Fix:
  - Rank the Nominatim top 5 by type. Prefer `city`, `town`, `village`, `suburb`, `neighbourhood`, `quarter`, `hamlet`, `city_district` and `borough`.
  - Accept a POI only when the user named one (C1 / M5).
  - Never accept a `road`.
  - Require the user's place words to appear in the result's `name` or `display_name`.
  - If confidence is low, ask: "Did you mean Hinjewadi, Pune?"
  - Always add a line like **"Searching around: Hinjewadi, Pune"** to the answer and the portal. That one line turns a silent wrong answer into one the user can spot.
- Done when Hinjewadi, Thane, Powai, Koregaon Park, Whitefield, Lonavala and Sector 18 Noida each land within about 2 km of the real locality, and every `ok` answer names the place searched.

**C4. "Saturday or Sunday" only evaluates Saturday, with no comparison.** This is an official example. *`app/parse.py`, `app/main.py`*
- Evidence: the window came back as Sat 00:00 → Sun 00:00. The answer named Lohgad on Saturday with an empty reason ("high score and a pleasant weather forecast").
- Fix:
  - Collect *every* weekday mentioned, and make the window cover all of them.
  - Detect comparison intent (`or`, `vs`, `better`, `which day`).
  - Compute the best result per day and per place, and lead with a verdict: "Sunday is better: 21°C at 7 AM, 0% rain, versus 40% rain on Saturday morning."
  - Handle comparisons between named places the same way ("Powai or Aarey for a run?").
- Done when "Is Saturday or Sunday better for a hike near Lonavala?" names one day, gives the main numbers for both, and the API returns `comparison: [{day, best_time, score, key factors}]`.

**C5. A time window that has already passed isn't moved forward.** *`app/parse.py`*
- Evidence: "sunset photos this evening around Bandra", asked at about 8 PM, gave a window of 19:00–21:00, after sunset. The result was a caution and "Least-bad option: **Trash Collector**".
- Fix:
  - If less than one usable hour remains in the requested window, or if sunset or sunrise has already passed for a golden-hour request, move to the same part of the next day.
  - Say so: "Today's sunset (6:08 PM) has passed; here's tomorrow."
  - Make the portal's example questions time-proof by using "tomorrow" wording.
- Done when that question, asked after sunset, plans tomorrow's 5–7 PM and says why.

**C6. The model handles one request at a time, so parallel requests queue.** *`app/llm.py`, `app/main.py`*
- Evidence: 4 simultaneous questions finished at **12.9, 22.7, 31.5 and 41.9 s**. With 10 parallel judge calls, the last would wait about 100 s. In 3 of those 4, the model's summary was then **thrown away** by the guards, about 10 s of CPU per request for nothing.
- Fix:
  1. Acquire the model lock with a timeout of about 2 s. If it's busy, use the template answer, which costs 0 s.
  2. Cache answers to identical questions: normalise the question (lowercase, collapse whitespace) and keep the answer for 10 minutes.
  3. Keep each system prompt's KV prefix warm, either with `llama_cpp.LlamaRAMCache` or with separate contexts for parse and write. The two calls alternate prompts, so today the prefix is recomputed every time. My estimate is 3–5 s saved per call; measure it.
  4. Set a deadline of about 35 s per request. If it runs out, return what we have with `status: "partial"` instead of timing out.
  5. Log the time each request spends waiting for the model.
- Done when 6 parallel requests all finish within 35 s, and none returns `error`.

**C7. Health-profile keywords match inside other words.** *`app/rules.py`, `app/parse.py` `_match`*
- Evidence: "picnic in **Son**ipat" → child. "walk after my **kid**ney surgery" → child. "school ground" → child. Golf's `tee` matches "**tee**nager".
- Root cause: `_match` uses `\bkeyword` with no closing word boundary.
- Fix:
  - Use whole-word matching with explicit plural forms (`kid|kids|kiddo`, `son|sons`).
  - Keep prefix matching only where it's safe: `asthm`, `wheez`, `breath`, `pregnan`.
- Done when the four examples above produce no false profile.

**C8. In dense cities, the nearest venues can be dropped before sorting.** *`app/data.py` `venues`*
- Evidence: Overpass `out tags center 300;` keeps the first 300 results *in Overpass's internal order*, and only then do we sort by distance. Delhi walking within 5 km has **507** matches; Bandra picnic within 10 km has **558**.
- Fix:
  - Query in rings: radius ÷ 3, then the full radius only if there are too few venues.
  - Or raise the cap to about 2000 and keep `out center` (the data is small).
- Done when the Delhi and Bandra cases return the true nearest named parks (spot-check against openstreetmap.org).

**C9. The model's summary is rejected about 60% of the time, and the ones that pass still make unverifiable claims.** *`app/llm.py`, portal*
- Evidence:
  - Official run: 4 of 7 non-caution answers were rejected (`writer: template`). Concurrency test: 3 of 4 rejected.
  - Claims that passed the guards: "**Caution:** MS Building Park is **safe** for your parents… the **air quality is acceptable**" (the facts had no caution); "**pleasant** weather"; "a **good spot** for outdoor tennis"; "**Crickte near andheri tmrw:**" (echoing the typo).
  - Portal bug: when `writer` is `template` and there's no caution, the "summary" paragraph shows the **whole facts block**.
- Fix:
  - Constrain the writer to one sentence in a fixed shape: `<verdict>. <place>, <window>: <main reason with 1–2 numbers>.`
  - Then validate it in code:
    - the place must equal the top venue;
    - the window must equal the computed window;
    - every number and time must appear in the facts;
    - no word from a banned list: `safe, unsafe, acceptable, healthy, pleasant, perfect, ideal, great, good spot, beautiful, popular, open, closed, famous, scenic, crowded`;
    - no "Caution" unless the facts contain one.
  - Pass the *normalised* question, not the raw typo'd text.
  - In the portal, build the summary from structured fields and never from the `answer` text.
  - Track the pass rate in the evaluation (target ≥85%).
- Done when the evaluation's writer pass rate is ≥85%, there are zero banned words, and the portal never shows the facts blob as a summary.

### High

**H1. Devanagari time words aren't understood, and the SLM gets "kal" wrong.** *`app/parse.py`*
- Evidence: "कल सुबह पवई में दौड़ना है" (tomorrow morning) was planned for **Sunday** 5–12, not Friday.
- Fix: add कल, आज, परसों, सुबह, शाम, रात, दोपहर, अभी and इस हफ्ते to the rules. The rules' reading of the time should win over the SLM's.
- Done when that question plans tomorrow 05:00–12:00.

**H2. Missing `None` guards crash requests.**
- Evidence (from reading the code):
  - `template_answer` formats `{h['uv']:.0f}`, which crashes if UV is `None`;
  - `score_hour` compares `gust` and `w["cloud_cover"]` without checking for `None`;
  - `downwind` uses `wind_direction_10m` without checking for `None`.
  - Open-Meteo does return nulls near the end of the forecast range. A crash shows the user the generic "data source didn't respond" error.
- Fix: guard every hourly field; skip hours that are missing core fields.

**H3. Air quality only covers 5 days, so days 6–7 escape the AQI penalty.** *`app/score.py`*
- Evidence: `air_quality(..., min(days, 5))`. Hours without AQ data get no AQI penalty, which biases "next week" questions toward days 6–7.
- Fix: flag those hours as "air quality unknown", apply a neutral median penalty, and mention it.

**H4. Choosing the "least-bad" option is arbitrary.** *`app/score.py`*
- Evidence: blocked hours get `s - 50·len(blocks)`, clamped at 0, so many blocked hours tie at 0 and the first one wins.
- Fix: rank blocked hours by how far they exceed the limits, e.g. the sum of `(value − limit) / limit`, with no clamp.

**H5. Carry and watch advice are fixed lists that can contradict the data.**
- Evidence: cricket always says "wet outfield after rain", even at 0% rain; the event says "canopy as rain backup" at 6% rain; duplicates appear ("water" + "extra water").
- Fix: generate items from the data. Examples:
  - umbrella or rain jacket if rain ≥30% in the window;
  - sunscreen and a cap if UV ≥6;
  - an N95 mask if AQI is poor;
  - electrolytes if it feels ≥35°C;
  - a torch if the window ends after sunset;
  - mosquito repellent at dusk near water (H7);
  - a warm layer if it feels ≤15°C (Leh, Ooty);
  - "watch lightning" only if `weather_code` is 95–99.

  Keep one or two activity basics, dedupe, and attach the triggering number to each item ("UV 9 at 12 PM").

**H6. Traffic and rush hour are missing; the statement's own example is about rush hour.** *`app/score.py`, `app/data.py`*
- Evidence: the travel penalty is the same at every hour, and road exposure doesn't vary with the hour or the day of the week.
- Fix (a heuristic, and say so):
  - multiply travel time by about 1.6–2.0 in metros during 8–11 AM and 5–9 PM on weekdays;
  - weight road exposure by hour (rush hours ×1.5, night ×0.6) and by day (Sunday ×0.8);
  - add the reason text: "a 40-minute drive at 8:30 AM rush hour".
- Done when "golf on Monday 8 AM" penalises a far course more than at 6 AM, and the answer says why.

**H7. Water bodies and green cover are fetched but never used.** The statement names both explicitly. *`app/data.py` `surroundings`, `app/score.py`*
- Evidence: `ctx["water"]` and `ctx["green"]` are never read by `score_hour`.
- Fix:
  - **Green:** compute the green-cover % within about 300 m, ideally from **ESA WorldCover 10 m** (W4), else from the area of OSM green polygons. Use it as heat relief: a smaller heat penalty, plus a reason like "72% tree cover, more shade".
  - **Water:**
    - cooling or breeze near large water;
    - "mosquito risk at dusk" near standing water in Oct–Nov (post-monsoon dengue season, which matters in India);
    - "keep kids away from the water edge" only if water is within 100 m;
    - for beaches, the tide and waves from the Open-Meteo Marine API (W11).

**H8. Venue quality: private, odd or unlit places get recommended.** *`app/data.py` `venues`, `app/score.py`*
- Evidence: "Utpal Shanghvi **School** Ground", "Mayo International **School** Tennis Courts", "Maharashtra **Police** Tennis Court", "Willow Sports Cricket **academy**", "**Trash Collector**", "Tennis Court 1", "Pune **Gate**" (for Sinhagad). Football at 8 PM was recommended with no floodlight check.
- Fix:
  - drop `access=private|no|customers|members`;
  - down-rank names containing school, college, academy, club, society, police, cantonment or "court 1" unless tagged `access=yes`;
  - for after-dark sports, require or boost `lit=yes`;
  - read simple `opening_hours` values (`06:00-21:00`) to block closed hours, which would also let us say "open" truthfully;
  - prefer larger parks (polygon area);
  - drop names that look like objects ("Trash", "Toilet", "Gate", "Parking").

**H9. `from_pretrained` compliance on Hugging Face.** *HF repo `AtharvIITB/geoai-outdoor-slm`*
- Evidence: the repo holds only the GGUF, `LICENSE`, `README.md` and `geoai_config.json`. `transformers` can load Qwen3 GGUF only with an explicit `gguf_file=` argument (Qwen3 is in its GGUF mapping); a plain `AutoModelForCausalLM.from_pretrained("AtharvIITB/geoai-outdoor-slm")` fails because there's no `config.json`.
- Fix:
  - Add HF-format weights (config, tokenizer, safetensors): an unmodified Qwen3-1.7B (Apache-2.0 allows this), or better, our fine-tuned merged weights (W6).
  - Keep the GGUF for runtime.
  - Show both loading snippets in the model card.
  - Verify both in a clean virtual environment.
- Done when `AutoModelForCausalLM.from_pretrained("AtharvIITB/geoai-outdoor-slm")` and `Llama.from_pretrained(...)` both work from a fresh venv, with the transcript saved for the README.

**H10. The API input is brittle.** *`app/main.py`*
- Evidence: only the `q` field is accepted. A judge sending `{"question": ...}` gets a 422 error. There's no length cap, so a 1 MB question would overflow the model context. There's no CORS header, so browser-based clients on other sites are blocked.
- Fix:
  - accept `q`, `question`, `query`, `prompt` or `text`, in a JSON body, form data, plain text or a GET parameter;
  - cap the question at 500 characters;
  - validate `lat` and `lon` ranges;
  - allow CORS for GET and POST;
  - add `?format=text` for a plain-text answer;
  - add `view_url`: a portal link with `?q=` that runs the question automatically;
  - return `ok`, `clarify`, `no_venues` or `partial` consistently.

**H11. No observability: we couldn't see a judge's failure.**
- Evidence: there are only uvicorn access logs (no questions, no errors), and `/health` returns `ok` even if the model failed to load.
- Fix:
  - write a JSONL log line per request: time, question, parsed fields, origin, status, latency, writer, error;
  - `/health?deep=1` to report the model status and a ping to each data source;
  - `/version` with the git commit;
  - an external uptime monitor (human task 7).

**H12. "Is it nice outside in Pune" assumes a walk, but the statement says "rather than guessing".**
- Fix: give the general conditions for the next hours in Pune (heat, rain, AQI), then ask which activity. Don't invent a walking plan.

**H13. The answer's structure.**
- Evidence: the answer is a summary, then "Details:", then a raw facts dump that repeats it and includes jargon like "score 79". There's no verdict line and no "Searching around".
- Fix: use this shape for every `ok` answer:
  1. verdict: Yes / Not ideal / "Sunday is better";
  2. where and when, with distance and travel time;
  3. why, in two key factors with numbers and source tags;
  4. carry / watch for / avoid (from data);
  5. the data line: "Data: OSM, Open-Meteo (fetched 20:14 IST)".

  Remove the scores from the text; keep them in JSON.

### Medium

| ID | Issue | Fix |
|---|---|---|
| M1 | Overpass etiquette: every query races all 5 mirrors at once (5× load on public servers). Under judging load this risks rate limits (`overpass-api.de` allows 2 slots per IP); two mirrors already return 403/406 for some requests. | Hedge instead: try the primary, start a second mirror only after about 3 s. Drop dead mirrors. Keep the 200-with-error-remark check. |
| M2 | `_cache` grows forever. | Bounded LRU (~2000 entries). Serve stale venue lists up to 24 h if Overpass is down, with a note in the answer. |
| M3 | The Nominatim user agent has no contact info (their policy asks for it). | Add the repo URL to `UA`. |
| M4 | "next 3 days", "this week", "from 6 to 8 am", "at 4" and "in 2 hours" aren't parsed. | Add these patterns. "this week" means today through Sunday. |
| M5 | Named places get treated as areas: "hike **Sinhagad fort**" → recommends "Pune Gate"; "picnic in **Cubbon Park**" → "Cubbon Park Circle"; "**Lodhi Garden**" → "Rose Garden". | When the place found is a POI (park, fort, beach, viewpoint), score *that place* first and give alternatives second. |
| M6 | Yes/no questions ("Is it okay to cycle at 8 AM?", "Is tomorrow good for tennis?") get no verdict. | Detect the intent and lead with Yes / Not ideal / No, plus the reason. |
| M7 | A state or district as the origin ("Goa") puts the centroid in the countryside: golf 63 km away, 111 min. | For `state` or `state_district` results, ask the user to narrow it down, or use the largest city. |
| M8 | Portal details: dark map style is nearly black; no favicon; "this evening" examples break at night; no `?q=` deep link; profile toggles are sent as English text. | Positron map style in both themes; add a favicon; time-proof examples; read `?q=` and auto-run; send `profiles` as a structured field. |
| M9 | The SLM parsed only **3 of 12** official-style questions (the rest took the rules-only shortcut). That weakens the "SLM assistant" story and lets rule bugs through (C1). | Make the SLM parse every question, with the cached prefix (C6) or a fine-tuned 0.6B/1.7B model (W6), and use the rules as a cross-check. Where they disagree, use the geocode validation (C3) to decide. Report both in `parsed`. |
| M10 | Repo hygiene: `Dockerfile` and `requirements.txt` are left over from the abandoned HF Space path; `.claude/launch.json` is committed; there's **no LICENSE**; `pyproject` still says "Add your description here". README gaps: no OpenFreeMap tiles, MapLibre or Google Fonts in the disclosure, no explicit "API endpoint" line, no response schema, no evaluation results. | Delete or document the leftovers, add Apache-2.0 `LICENSE`, update the README. |
| M11 | Only US AQI is shown. Indian judges expect the CPCB **National AQI** (NAQI) categories; US AQI makes Mumbai look "unhealthy" almost every day. | Compute NAQI sub-indices from PM2.5 and PM10 and show the category (Good / Satisfactory / Moderate / Poor / Very Poor / Severe), with US AQI as secondary. Set activity limits on NAQI. |
| M12 | No thunderstorm or lightning check (golf and hiking say "watch for lightning", generically). | Use Open-Meteo `weather_code` 95–99 (thunderstorm) as a hard block for golf, hiking and cricket, and `cape` > 1500 J/kg as a warning. Both variables are available (checked). |
| M13 | A single server, no standby, no AMI; the IAM key is long-lived; SSH is only allowed from the IITB address range. | Snapshot an AMI (if the free plan allows); write a `deploy.sh` that keeps the previous build for rollback; keep the Mac + Cloudflare tunnel script as an emergency fallback. |

### Low
- The writer always answers in English, even to Hindi questions (see W10).
- `dist_m` is straight-line distance; there's no walking or cycling routing (OpenRouteService has both, with a free key; human task 3).
- The portal shows the fetch time in the viewer's time zone (fine for India).

---

## 4. How we win: ideas beyond the fixes

Anyone can build "Overpass + Open-Meteo + an LLM". These are the things that would make ours stand out, ordered by (impact for *this* jury) ÷ effort.

**W1. An evaluation harness with published metrics.** Effort: medium. Impact: very high. *`tests/`*
- **Labelled set of about 80 questions:**
  - the 10 official examples;
  - 40 messy ones (typos, Hinglish, Devanagari, missing pieces, out of scope, comparisons, past or far-future dates);
  - 20 from real people (human task 6);
  - 10 adversarial (injection attempts, 1 KB of junk, emoji).
- **Expected fields for each:** activity, place (an area must geocode within X km), time window, profiles, intent, and the expected status.
- **Metrics:**
  - accuracy for each parsed field;
  - end-to-end success rate;
  - no-wrong-city rate;
  - writer pass rate;
  - latency at the median and 95th percentile;
  - error rate;
  - concurrency results.
- Run it on every change, and put the table on the README and on one slide. Very few teams will have numbers.

**W2. Every claim cited.** Effort: medium. Impact: very high. This is the statement's literal requirement.
- An `evidence[]` list in the response, where each item has an id, a source, a URL with its query parameters, the fetch time, and the exact value used.
  - Examples: an Open-Meteo hourly value at the venue's coordinates; OSM `way/123456` "Eastern Express Highway" (click it to open openstreetmap.org); a CPCB station reading.
- The answer text cites them as [1], [2]. The portal has an "Evidence" panel where every number is clickable.
- Store OSM IDs for roads and industry too; today only venues keep them.

**W3. Every factor the statement names, used visibly.** Effort: medium.
- **Traffic:** rush-hour exposure plus rush-hour travel time (H6).
- **Water and green:** H7.
- **Mixing height:** Open-Meteo `boundary_layer_height` (available; 100 m tonight in Mumbai). A low mixing height means pollution is trapped near the ground, so near-road exposure lasts until the boundary layer grows, usually around 9–10 AM. It's meteorologically sound, cheap, and a great reason to show: "Air near the road stays stagnant until about 9 AM (mixing height 120 m)."
- **Thunderstorms:** M12.
- **Rail:** OSM `railway=rail` within 200 m as a noise note for photography or picnics. Optional.

**W4. Remote sensing at query time.** Effort: medium. Impact: high *for a CSRE jury*.
- **ESA WorldCover 10 m (2021):** a public Cloud-Optimised GeoTIFF on AWS, no key needed (checked: HTTP 200 and range reads work).
  - For each venue, read a small window (~300 m) and compute the share of tree cover, grass, built-up area and water.
  - That gives "shade %" and "built-up heat" factors.
  - Needs `rasterio` (or `tifffile` + `numpy`) plus caching.
- **Copernicus DEM 30 m:** also public (checked).
  - Elevation, slope and climb for hikes ("Lohgad top 1,033 m, about 3°C cooler than Lonavala town").
  - Flags low-lying spots after heavy rain.
- Present as "Sentinel-derived land cover" and "satellite DEM": it's the jury's own language.

**W5. Ground-truth air quality from CPCB.** Effort: medium. Impact: high. **Needs human task 3.**
- WAQI (free token) or OpenAQ v3 (free key) give the nearest CPCB station's measured value now. We checked: OpenAQ without a key returns 401, and the WAQI demo token only serves Shanghai.
- Show "Measured now at Powai station (2.1 km): NAQI 142, Moderate", and use the CAMS forecast for the *trend* over coming hours.
- Calibrate the CAMS bias on the fly: forecast now minus measured now, applied to the next hours. Label it clearly.
- This answers the obvious judge question, "isn't CAMS inaccurate for India?", with data.

**W6. Fine-tune the SLM for parsing.** Effort: high. Impact: high for the "model and fine-tuning" slide. **Decision point by 23 Oct, 14:00.**
- **Data:** about 3,000 synthetic queries from templates.
  - Cross activities × Indian localities × time phrases × profiles × languages (English, Hinglish, Devanagari).
  - Locality names come from an OSM extract of `place=suburb|neighbourhood|town` in the 10 largest metros. That's training data, not hard-coded answers; disclose it.
  - Generate labels with the fixed rule parser.
- **Train:** LoRA on Qwen3-1.7B, or on **Qwen3-0.6B** for about 3× faster CPU inference, with MLX on the M4 (my estimate: 20–40 min).
- **Measure** field accuracy on W1's held-out set, before and after.
- **Publish** the adapter and merged safetensors, which also fixes H9, with a model card that includes training details and evaluation numbers.

**W7. Comparison and verdict answers.** Effort: small to medium. Impact: high, since it's an official example. C4 and M6, plus: "Which day this week is best for cricket in Thane?" should rank days.

**W8. An "exposure saved" metric for each answer.** Effort: small.
- Per answer: "Going at 6 PM instead of 8 AM cuts your near-road PM2.5 exposure by about 35% and the heat by 4°C."
- Compare the recommended hour with the user's stated time, or with the window's median hour.
- Aggregated over the evaluation set, it becomes a strong impact claim for the slides.

**W9. Demo moments in the portal.** Effort: medium. Impact: medium-high for the finals.
- **Wind plume:** for the selected hour, draw a cone from each upwind road or factory to the venue, red when the venue is downwind and grey when not. Dragging across the hours shows the wind shift; this is the statement's own "downwind at 8 AM, pleasant by evening" story.
- **Hex road-density layer** around the venue (H3 hexes), matching the "road density · hexes" panel on the hackathon site.
- **Live progress:** stream the real steps with server-sent events ("Found 8 parks → fetched hourly weather for 8 points → checking wind against Western Express Highway…") instead of a timer. It's honest, and it hides the latency.
- An evidence panel (W2), and a "Why not #2?" explanation for each alternative.

**W10. Hindi and Hinglish answers.** Effort: medium.
- Deterministic Hindi templates in Devanagari, plus Hinglish, chosen by the question's script or language.
- Optionally an SLM polish, with the same number check.
- It ties to "Vikasit Bharat" and shows the assistant works for people who aren't English-first.

**W11. Activities where the best conditions aren't simply "mild".** Effort: small to medium each. They show depth.
- **Kite flying:** *needs* 10–25 km/h of wind and an open ground; must be away from power lines (OSM `power=line`; the statement lists power lines as a feature).
- **Stargazing:** clear sky, moon phase (computable offline), distance from city lights (WorldCover built-up share within 10 km).
- **Beach walk:** tide and wave height from the Open-Meteo Marine API (checked; `sea_level_height_msl` includes tides).
- **Birdwatching:** wetlands and mangroves (OSM `natural=wetland`), dawn hours.

**W12. A "built to survive judging" story.** Effort: small.
- C6, M1, M2, H11, the deadlines and the fallbacks, presented on one slide with the measured numbers.
- Judges reward engineering maturity.

**Optional: the SLM as a planner.** The statement says the assistant "reasons about place and time using a small language model".
- A light way to show that without giving the model the facts to invent: the SLM picks which factors matter for *this* question from a fixed list (heat, rain, air, wind, traffic, water, shade, terrain, tide, daylight).
  - Example: "my toddler and I" → shade and water safety.
  - Example: "photos" → light and haze.
- Code then weights them and computes everything. Only do this after W1 shows the parser is solid.

---

## 5. Execution plan

Times are IST and assume you do the human tasks in parallel. Effort is my rough estimate.

**Tier 0, tonight (22 Oct), about 3–4 h of correctness work:**
- C1, C2, C3, C4, C5, C7, C8, H1, H2.
- C9's portal summary fix.
- H10's aliases, CORS and length cap.
- H11's request log.

Then run the official, messy and edge suites (`tests/messy.py` plus the new labelled set), deploy with a rollback copy, and commit.

**Tier 1, 23 Oct morning (PS alignment):**
- W1 (the labelled set and metrics; build it first, then use it for everything after);
- H13 (answer structure);
- H5 (data-driven advice);
- H6 (traffic);
- H7 and W3 (water, green, mixing height, thunderstorms);
- M11 (NAQI);
- H8 (venue quality);
- C6 (queueing: timeout, cache, KV prefix, deadline);
- C9 (writer constraints).

**Tier 2, 23 Oct afternoon:**
- H9 (HF safetensors and loading checks).
- **Decision at 14:00:** do W6 (fine-tune) only if Tier 1 is done.
- W2 (evidence and citations).
- W4 (WorldCover green %, DEM for hikes).
- W5 (CPCB ground truth), if the token has arrived.

**Tier 3, 23 Oct evening:**
- W9 (portal plume, evidence panel, server-sent events);
- W7 (comparisons, if not yet done);
- M5 (named-venue mode);
- M6 (verdicts);
- README, model card and LICENSE;
- M10 cleanup;
- W10 (Hindi), if time allows.

**Tier 4, 24 Oct:**
- Slides (section 8);
- the demo script and judge Q&A sheet (section 9);
- a full regression run;
- **code freeze at 16:00**;
- you record the backup video and submit the form by about 20:00;
- monitoring.

**After submission (25–29 Oct):** keep the server up and read the logs daily. Fix only bugs, and only with the regression suite passing. If we're selected, prepare the finals demo.

---

## 6. Things only you can do

1. **The submission form.** Watch your email and the website for the Google Form link and submit it; it needs your account. Tell me which fields it asks for. If it specifies an API format, that changes H10.
2. **Email the organisers** (geoaihackathon.iitb@gmail.com); I'll draft it. Ask:
   - Is there a required request or response format for the API?
   - Is there a maximum response time, and will questions arrive in parallel?
   - Does a GGUF-only repository satisfy "loads with `from_pretrained`"?
   - When does the judging window end?
3. **Free API keys.** Create them and save them in `/Users/atharv/GeoAI/.env`, which is gitignored. **Don't paste them into chat.**
   - **WAQI** token (email signup): https://aqicn.org/data-platform/token/, saved as `WAQI_TOKEN=...`
   - **OpenAQ** key: https://explore.openaq.org (register, then API key), saved as `OPENAQ_API_KEY=...`
   - *Optional:* **OpenRouteService** key for walking and cycling routes: https://openrouteservice.org/dev/#/signup, saved as `ORS_API_KEY=...`
   - *Optional:* a **data.gov.in** key for the CPCB real-time dataset.
4. **AWS billing check** (our deploy user can't read billing). In the console under Billing, tell me:
   - (a) the credit balance and its expiry;
   - (b) the **Free plan end date**;
   - (c) any **spend limit** set by the "Idea Incubator" organisation. There's an `AWSServiceRoleForBudgetsSpendLimitMember` role, so there may be one, and it could stop the server.

   Then create a budget alert to your email. The server costs about $3 a day by my estimate (m7i-flex.large in Sydney plus the public IP and disk), so about $25 through 30 Oct. Earlier I said $1–2 a day; that was low.
5. **Make the GitHub repo public**, or tell me to do it. Decide whether `docs/` (this plan) stays private; I'd keep it out.
6. **Questions from real people.** Ask 5–10 friends or family to type questions the way they naturally would (English, Hinglish, Hindi, Marathi) and put them in `tests/real_users.txt`. Also open the portal on your phone over mobile data and tell me what feels off. We can't simulate real people.
7. **Uptime alert.** Sign up for UptimeRobot (free) and point it at `https://32-237-40-94.sslip.io/health` every 5 minutes, alerting your phone.
8. **A backup demo video** (2–3 minutes, your screen and your voice) on 24 Oct after the code freeze. I'll write the script.
9. **Rehearse the defence** with the Q&A sheet. You'll present if we make the top 5 (28–29 Oct).
10. **Decisions I need from you:**
    - (a) fine-tune, yes or no (W6);
    - (b) for a missing activity: ask (my recommendation) or assume a walk (H12);
    - (c) model summary: constrain it (my recommendation) or turn it off;
    - (d) recruit a teammate for slides, testing and video? Teams can be up to 4. Optional;
    - (e) a nicer URL via DuckDNS? Free, needs your signup. Optional.
11. **After judging:** stop or terminate the EC2 instance (Sydney), delete the IAM access key for `geoai-deployer`, and delete the S3 bucket `geoai-deploy-301338931328`.

---

## 7. Risks and contingencies

| Risk | Likelihood | What we do |
|---|---|---|
| Parallel judge requests queue and time out | Medium-high | C6, then load-test 10 in parallel before submitting. |
| Overpass, Nominatim or Open-Meteo is slow or down during judging | Medium | M1 hedging, M2 stale-but-labelled cache, a 35 s deadline with partial answers. |
| A credit or spend limit stops the server | Unknown | Human task 4. Keep a ready-to-run fallback: the Mac plus a Cloudflare quick tunnel, with a script and steps written down. |
| A late change breaks things | Medium | The W1 regression suite, a deploy script with rollback, and the code freeze at 16:00 on 24 Oct. |
| Judges ask "where's the AI?" | High | M9 (SLM parses every question), W6 (fine-tune with numbers), and a clear "what the SLM does / what code does" slide. |
| CAMS air-quality bias questioned | High | W5 ground truth, NAQI (M11), and the limitations slide. |
| Wrong-city answers | Seen today | C1–C3, plus "Searching around…" in every answer. |
| The live demo fails at the venue | Low-medium | The backup video (human task 8) and recorded runs of the demo questions. |

---

## 8. Slide deck outline (5 slides)

1. **Problem and approach:** "Weather tells you what; we tell you where and when, for you." Principle: *code decides from live data; the SLM understands and explains; nothing comes from model memory.*
2. **Architecture:** the layers, as on the hackathon site:
   - question → SLM parse, plus rule cross-check → geocode, validated;
   - OSM venues plus context layers: roads and traffic, industry, water, green (WorldCover), terrain (DEM);
   - Open-Meteo weather, air, marine and mixing height, plus CPCB ground truth;
   - per-hour scorer → evidence → SLM summary → verifier.
3. **Worked example:** the wind-shift story. Portal screenshot of the map with the plume and the hour grid: "downwind of the expressway at 8 AM, clear by 6 PM", every number cited.
4. **Model:** Qwen3-1.7B (and the fine-tune, if done). Its two jobs, the guardrails, and the parse accuracy before and after, plus the writer pass rate.
5. **Evaluation and limitations:**
   - the W1 metrics table (accuracy, success rate, latency at median and 95th percentile, no-wrong-city rate);
   - honest limits: CAMS bias, OSM gaps, heuristic traffic, CPU latency, no live crowding or opening-hours data.
6. *(optional)* **Impact:** exposure saved (W8), Hindi support, and running on one free-tier CPU server.

---

## 9. Judge Q&A prep

I'll write the full sheet on 24 Oct. The likely questions:

- Where exactly is the AI? Why not let the SLM reason over the numbers?
- How do you guarantee the model doesn't invent places or weather? (Show the verifier and the pass rate.)
- How is "downwind" computed, and what about calm winds or low mixing height?
- CAMS overestimates PM in India. How do you handle it? (CPCB stations, NAQI.)
- What happens when an API is down or slow?
- Why US AQI or NAQI, and what are the thresholds based on? (NWS heat index, CPCB/EPA bands.)
- How did you evaluate it? What's your failure rate?
- Why Qwen3-1.7B? Did you fine-tune it, and what changed?
- How does it handle "near me", Hinglish, past or future dates, and out-of-scope questions?
- What would you do with another month?

---

## 10. Definition of done for each change

- `uv run python -m app.parse`, `-m app.score` and `-m app.llm` all pass, with new asserts for every fixed case.
- The W1 labelled suite passes with no regression against the previous run. Results are saved to `tests/results/<commit>.jsonl`.
- Deployed with rollback available. `/health?deep=1` is green. One live question gets an external 200.
- A commit message that says what changed and why.

---

## Appendix: evidence from today's checks

**Official examples on the live server** (22 Oct, about 20:00 IST; current code):

| Question | Result |
|---|---|
| Running tomorrow evening near Koregaon Park | ✅ Osho Teerth Park, 5–6 PM (23.8 s; summary rejected) |
| Kids' cricket this weekend within 15 km of Whitefield | ✅ Sat 7–10 AM (28.2 s); venue is a cricket academy (H8) |
| Mild asthma, morning walk near Powai in the next two days | ⚠️ Place parsed as "Powai in the"; worked by luck (C1) |
| Cycle to work at 8 AM | ✅ Asks for a location |
| Birthday party Sunday afternoon in Pune | ✅ 12–4 PM, but generic carry list (H5) |
| Golf Saturday within 40 km of Andheri | ✅ Kharghar Valley Golf Course |
| Sunset photos this evening around Bandra (asked after sunset) | ❌ Night caution, "Trash Collector" (C5, H8) |
| Saturday or Sunday better for a hike near Lonavala | ❌ Saturday only, empty reason (C4) |
| When is it safe for my parents to walk in Bengaluru tomorrow | ⚠️ Summary claims "safe" and "air quality acceptable", plus a stray "Caution" (C9) |
| Tennis after work today near Hinjewadi | ⚠️ Origin "Wakad Flyover" (C3) |
| कल सुबह पवई में दौड़ना है | ⚠️ Planned for Sunday instead of tomorrow (H1) |
| running near me tomorrow | ❌ Noida "Me, Jaypee Greens" (C2) |

**Concurrency:** 4 parallel requests finished at 12.9, 22.7, 31.5 and 41.9 s. The writer was used in 1 of 4; the other 3 summaries were discarded.

**Overpass match counts** (we keep 300 before sorting by distance): Powai running (6 km) 207; Delhi walking (5 km) **507**; Bandra picnic (10 km) **558**; Powai golf (40 km) 7.

**Geocoding of broken place strings:**

| Place string | Resolved to |
|---|---|
| "me" | Me, Jaypee Greens, Noida |
| "Sector" | Salt Lake Sector, Kolkata |
| "Lonavala in the" | The Arboreal, Tung Road (about 8 km off) |
| "Mumbai in the" | US Consulate, BKC |
| "the park near Powai" | No result |

**Data sources checked today:**

| Source | What we found |
|---|---|
| Open-Meteo `boundary_layer_height`, `weather_code`, `cape` | Available (`lightning_potential` is null in India) |
| Open-Meteo Marine `wave_height`, `sea_level_height_msl` | Available, with tides |
| ESA WorldCover 10 m and Copernicus DEM 30 m COGs | Public, HTTP 200, range reads supported |
| OpenAQ v3 | Needs a key (401) |
| WAQI demo token | Shanghai only |

**Server:** 0 restarts, 5.5 GB RAM free, disk 29% used, a valid Let's Encrypt certificate. 55 `POST /ask` in the last 3 hours, all returning 200.
