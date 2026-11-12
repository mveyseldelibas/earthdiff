# Real questions through the MCP server

Run with `python validation/run_mcp_questions.py` (cases in [`mcp_cases.json`](mcp_cases.json)). The script starts the shipped
`earthdiff-mcp` server over stdio and acts as the assistant: `find_place` (live OSM Nominatim) when a place name is given, then `plan_change`, then
`detect_change` (live Earth Search STAC, Sentinel-2 COGs on AWS). Nothing is faked. The raw tool answers and the three images the
client receives are in [`mcp/<case>/`](mcp/): `find_place.json`, `plan_change.json`, `detect_change.json`, `before.jpg`, `after.jpg`,
`change_map.jpg`, `progress.log`, `timings.json`. Full report folders (`report.html`, `changes.geojson`, ...) were written to
`validation/mcp_runs/` (git-ignored). The Lake Tuz index experiment is [`tuz_experiment.py`](tuz_experiment.py) with its output in `mcp/tuz_golu/spring_experiment.json`.

Run on 2026-11-06, home connection, Windows laptop, 20 m analysis grid. These are **qualitative checks and rough comparisons, not an accuracy assessment**: no field
reference polygons were used. Timings vary with the connection (Lake Mead includes a retried HTTP request).

| Case | Question | plan | detect_change | Peak result |
|---|---|---|---|---|
| Manavgat 2021 | what changed after the July 2021 wildfire? | 6.6 s | 119 s | 3,119 ha vegetation loss, 291 polygons; likely cause: 3,075 ha fire-like |
| Lake Mead | 2019-07 vs 2022-07 | 5.2 s | 173 s | 616 ha water loss, 64 polygons |
| Istanbul Airport | 2017 vs 2019 (construction) | 9.3 s | 131 s | 558 ha vegetation loss; likely cause: 8.6 ha fire-like, 52 clearing-like, 497 unclear |
| Konya plain (cropland) | May vs July 2023, seasonal check off | 5.0 s | 61 s | 668 ha vegetation loss, 2,253 ha gain |
| Konya plain, seasonal check on | same, `seasonal_check: true` | 4.7 s | 107 s (+46 s) | 181 ha (27 %) likely seasonal |
| Lake Tuz | late summer 2019 vs 2021 | 14 s | 116 s | warning: 56 % very bright surface, not reliable |
| Lake Tuz, spring baseline | spring vs late summer 2021 | 16 s | 116 s | same warning, plus an exploratory wet-to-dry signal |

About 30 to 50 progress notifications were sent per run (one per pipeline step plus a heartbeat every 8 s). Returned images: 30 to 205 KB each (JPEG).
Reading B12 for the likely-cause hint made the Manavgat run about 19 s longer than before (99 s to 119 s, same box; within normal variation of the connection, so treat it as roughly +20 %).

## How the ambiguity flow worked (real `find_place` answers)

The server did not guess. For "Manavgat" it returned two different places (the district, 5,755 km2, and the river, 1,420 km2), both over the 400 km2
limit, each with a centred box and tiles as narrowing options. "Lake Mead" returned the reservoir (3,136 km2 as a bounding box) and a pond in
Connecticut. "Tuz Gölü" returned four lakes with that name. The scripted user answers (in `mcp_cases.json`, field `area_choice_note`) were:
the river valley, the reservoir, the big lake; in each case the **centred box** offered by `find_place`. That box is a convenience of the
script, not a statement about where the phenomenon is strongest. Istanbul Airport and the Konya plain use a box stated by the scripted user (no place search).

## 1. Manavgat wildfire, July 2021: detected, partial coverage

Before 2021-07-01..27, after 2021-08-10..09-10; box [31.512, 36.864, 31.654, 37.121] (about 360 km2); 8 scenes in each period; 100 % comparable; data quality good.

- **3,119 ha vegetation loss** in 291 polygons (44 ha new bare/built-up, 6 ha gain, 6 ha water loss). Area-weighted confidence 0.80 (high).
  The largest polygon (892 ha) goes from NDVI 0.67 to 0.25. The change map shows large scars around the Manavgat reservoir that coincide with the dark burn scar in the after image.
- **Comparison with known figures.** Published numbers describe the whole fire complex, which is much larger than this box: about 30,600 ha for the
  fire by 30 July from Landsat-8/Sentinel-2 mapping by Gebze Technical University ([GTU](https://www.gtu.edu.tr/en/icerik/8/12549/display.aspx)), about 56,700 ha for
  Manavgat in a later study ([bianet](https://bianet.org/english/environment/248673-study-83-000-hectares-of-forests-burned-in-turkey-s-south-and-southwest-in-two-weeks-of-fires)).
  Our v0.1 CLI run over a 2,560 km2 window found 31,362 ha (main README). **A 360 km2 box cannot be compared with a fire-wide total; this is a plausibility check, not a validation
  of area.** The part that can be checked is the shape: the flagged polygons sit on the dark scar.
- Known bias: only vegetation that was green (NDVI >= 0.40) before is counted, so burnt sparse shrub and fields are under-counted.

## 2. Lake Mead, July 2019 vs July 2022: detected

Box [-114.595, 36.141, -114.344, 36.285] (the centred part of the reservoir); 6 scenes in each period; 100 % comparable; data quality good.

- **616 ha water loss** in 64 polygons and nothing else flagged. The polygons form the retreating shoreline. Reference: about 1,041 ft in July 2022 ([NASA Earth Observatory](https://science.nasa.gov/earth/earth-observatory/lake-mead-keeps-dropping-150111));
  full pool is 247 sq mi, about 640 km2. The direction matches; the hectares were not checked against an independent number (the CLI run on a different western 300 km2 box gave 421 ha).
- Side effect of the new bright-surface handling: 6 % of the box is bright (probably pale exposed shoreline; not inspected), so the confidence of polygons there is lowered. The overall confidence went from "high 0.97" to "medium 0.70". That is how the rule is meant to work, but it shows that it is blunt: a valid detection loses confidence because of a few bright pixels.

## 3. Likely cause (fire-like / clearing-like / unclear)

Method and sources: [`src/earthdiff/cause.py`](../src/earthdiff/cause.py). dNBR thresholds are Key & Benson (2006) as applied by Copernicus EFFIS (0.10 / 0.27 / 0.44 / 0.66); the post-event brightness and outline thresholds are this project's own. They were set by looking at the two cases below, so the numbers below are **not an independent test**.

| Case | vegetation loss | fire-like | clearing-like | unclear |
|---|---|---|---|---|
| Manavgat 2021 (wildfire) | 3,119 ha | 3,074.8 ha (264 polygons) | 17.0 ha | 26.7 ha |
| Istanbul Airport 2017 to 2019 (airport construction) | 558 ha | **8.6 ha (6 polygons)** | 52.2 ha | 497.4 ha |

- Manavgat: 98.6 % of the loss is `fire-like`. The polygons are very dark after the event (median visible reflectance 0.055) with dNBR about 0.55.
- Istanbul Airport (known construction and forest clearing, no fire): the hint does **not** call it a fire, with one exception to record: 6 polygons (8.6 ha, 1.5 %) came out `fire-like`. Most of the loss is `unclear` because
  dNBR is high there too (clearing raises it) while the surface is brighter than char (median visible reflectance 0.16, Manavgat 0.055).
  The hint therefore separates the two cases by post-event brightness, and "unclear" is the honest answer for most of the airport loss. A first draft of the rule (dNBR plus irregular outline) called 334 of 558 ha fire-like at the airport; that is why `fire-like` now requires a dark surface.
- Limits: old, ashy or grey burn scars are brighter than fresh char and will often be `unclear`; dark shadowed or wet ground after clearing can look dark; two cases cannot support transferable thresholds.

## 4. Seasonal check (harvest filter)

Konya plain, irrigated cropland near Cumra, box [32.70, 37.58, 32.78, 37.63] (39 km2), before 2023-05 (winter wheat ripe), after 2023-07 (harvested, summer crops). The windows are 61 days apart on purpose, so the answer carries the season warning, and 75 % of the box is flagged (also warned).

- Without the check: 668 ha vegetation loss in 103 polygons, 61 s. With `seasonal_check: true`: same polygons, **181 ha (27 %, 51 polygons) flagged `likely_seasonal`**, 107 s. **Extra time: +46 s (+76 %)** for 4 extra composites (2022-05, 2022-07, 2021-05, 2021-07, red and NIR only, at most 3 scenes each, only around the loss). CLI run: +48 s. So it stays **off by default**, and `plan_change` lists the windows and the cost.
- Of the 103 polygons: 52 (487 ha) never repeated in the two earlier years, 42 (142 ha) repeated in one, 9 (39 ha) in both. Most fields showed greening between May and July in the earlier years, not a drop: plausible with crop rotation or different timing, but **not checked against field data**. A field harvested every second year can be missed; a polygon flagged in only one of two years counts as seasonal (at least half of the years).
- No ground truth was used; this shows the mechanism works and what it costs, not its accuracy.

## 5. Lake Tuz: not mapped, but no longer a false reassurance

Box [33.361, 38.711, 33.541, 38.918]; 8 scenes in each period.

- **Late summer 2019 vs 2021.** Result before this change: "high confidence 0.99, quality fair, no warning" with no change on the lake bed. Now: **56 % of the comparable area is very bright, non-vegetated surface (50 % in both periods)**, the answer says change there is not reliable, confidence is **low (0.42)**, data quality **poor**. The mapped classes themselves are unchanged (639 ha vegetation loss and 617 ha gain on irrigated fields in the north-east corner, cause not investigated; 115 ha water loss on margins; nothing on the lake bed). Reference: the lake was reported completely dry in summer 2021 ([Copernicus Sentinel-2 imagery, 23 Oct 2021](https://www.copernicus.eu/en/node/11284)). earthdiff still does not map this, and now says why it cannot.
- Why the default rules fail (index experiment, [`tuz_experiment.py`](tuz_experiment.py), 30 m, on the 161,000 pixels that are very bright in late summer 2021):

  | window | median visible | median SWIR1 | MNDWI-water share | NDWI > 0 share |
  |---|---|---|---|---|
  | spring 2021 (Mar to Apr) | 0.255 | 0.002 | 87 % | 84 % |
  | late summer 2021 | 0.443 | 0.117 | 86 % | 1.5 % |
  | late summer 2019 | 0.507 | 0.089 | 87 % | 2.8 % |

  Dry salt scores as water under MNDWI ((green - SWIR1) / (green + SWIR1) is positive because SWIR1 reflectance of salt is below green), in every window. So MNDWI-based rules see "water" before and after and report no loss. **NDWI = (green - NIR) / (green + NIR), and SWIR1 reflectance itself, do separate the wet spring surface from the dry crust** (84 % vs 1.5 % water-like).
- **Spring baseline (2021-03/04 vs 2021-08/09) in the full pipeline:** did **not** help the default rules: 175 ha water loss, 990 ha vegetation loss, no usable lake-bed change, 53 % bright (28 % in both), quality poor, confidence low. The 153-day season gap is also warned.
- What was added from the experiment: on the bright pixels the answer now reports the NDWI water-like share before and after (`bright_surface.water_like_share_of_bright_pixels`) and, when the bright share is above the threshold and the share changes by at least 25 points, an **exploratory** warning. In the spring case it says the share went from 84 % to 2 %, "suggests a bright surface became drier", "not a mapped change". In the late summer 2019 vs 2021 case no such warning appears (3 % to 2 %). It is an indicator, not a mapped class: it has no area, no polygons and was seen in one lake.
- Not checked: whether the late-summer 2019 baseline was already largely dry (the 2.8 % NDWI share suggests it was); the empty strip at the bottom of the box (about 24 % of rows, not comparable in any scene) was not investigated.

## What this shows about the server

- The full flow (place search, plan, analysis, three images, files) works against the live services in about 1 to 3 minutes for 320 to 360 km2 at 20 m, with progress reported throughout.
- Ambiguity and oversize are surfaced to the caller instead of being guessed.
- The new hints are labelled as hints. `likely_cause` separated a real wildfire from a construction site on the two public cases it was tuned on, with one recorded miss; `seasonal_check` works but nearly doubles the run time on a small area; the bright-surface warning turns an overconfident Lake Tuz answer into a low-confidence one without fixing the mapping.
- The numbers inherit the limits of the method (README, "Limitations").
