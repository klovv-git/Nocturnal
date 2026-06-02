# NOCTURNAL — Claude Handoff Guide

## What This Project Is

A maritime surveillance system for the **English Channel / Western Approaches**.
Two independent data streams are correlated to detect "dark vessels" — ships visible on
Sentinel-1 SAR radar imagery that have no corresponding AIS transponder signal.

```
AIS stream  (live, every ~60s)  →  ais_memory.db   (~41+ GB, growing)
SAR stream  (Sentinel-1 passes) →  sentinel_data/  (.SAFE folders, ~1.7 GB each)
                                        ↓
                              run_pipeline.py
                                        ↓
                    dark vessel detections + AIS correlation + quality scoring
                                        ↓
                         ais_timeline_map.html  (browser view)
```

---

## Environment

| Item | Value |
|---|---|
| **Working directory** | `D:\Github\Nocturnal` |
| **Python venv** | `D:\Github\Nocturnal\venv\Scripts\python.exe` |
| **Web UI** | `http://localhost:5050` |
| **OS** | Windows 10/11 |
| **Git remote** | `https://github.com/klovv-git/Nocturnal.git` |

**Always run scripts with the venv Python from the project root:**
```powershell
cd D:\Github\Nocturnal
.\venv\Scripts\python.exe script.py
```

---

## Windows Store Python — Important Venv Behaviour

The venv was created from the **Windows Store Python** (`pyvenv.cfg home` →
`C:\Users\...\WindowsApps\PythonSoftwareFoundation.Python.3.10_...`).

On Windows, this Python uses an **App Execution Alias**: running
`venv\Scripts\python.exe` launches a stub that spawns the real interpreter at the
`WindowsApps\...` path as a child process. **Both appear in Task Manager — they are
ONE process.** Killing the `WindowsApps` child kills the actual Python worker.

**Never kill `C:\Users\...\WindowsApps\python.exe` processes.**

If you want to fix this permanently, rebuild the venv from a python.org installer:
```powershell
Remove-Item -Recurse -Force venv
C:\Python310\python.exe -m venv venv
.\venv\Scripts\pip install -r requirements.txt
.\venv\Scripts\pip install torch==2.6.0+cu124 torchvision==0.21.0+cu124 --index-url https://download.pytorch.org/whl/cu124
powershell -File register_tasks.ps1   # re-register scheduled tasks
```

---

## The One Config File That Controls Everything

**`config.py`** — read this first.

- **AOI**: loaded from `aoi.geojson` (a non-rectangular polygon following the Channel
  shape, drawn at geojson.io). Converted to WKT (`AOI_WKT`) for CDSE satellite queries
  and to `AOI_LAT/LON_MIN/MAX` for DB range queries and detection scoring.
- **All paths are relative** — scripts must be run from the project root.
- `CHANNEL_PASS_HOUR_UTC = 6` — Sentinel-1 morning pass over the Channel ~06:13 UTC.
- `STORMGLASS_API_KEY` is hardcoded here (don't expose in logs/commits).

---

## Databases

| File | Size | Contents |
|---|---|---|
| `ais_memory.db` | ~41+ GB | AIS positions since 2026-04-23, ~96M+ rows, growing |
| `weather.db` | small | Hourly weather grid from StormGlass API |

### AIS schema (key tables)
```sql
positions  (mmsi, lat, lon, sog, cog, ts_epoch, ts_iso, ship_type, name, ...)
vessels    (mmsi, name, ship_type, ...)
detections (id, scene_name, pixel_x, pixel_y, confidence, lat, lon,
            dark, matched_mmsi, match_dist_m, score, width_px, height_px, ...)
```

**SQL performance gotcha:** Avoid `ORDER BY mmsi, ts_epoch` on large `positions` scans —
forces the composite index and causes ~100s queries. Sort in Python after fetching.

**WAL mode:** Before copying `ais_memory.db`, run:
```python
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
```

### `detections` table — dark flag and score
| `dark` value | Meaning |
|---|---|
| `NULL` | Not yet geocoded, or rejected as land / outside AOI |
| `0` | Geocoded + AIS vessel matched within 1 km / 30 min |
| `1` | Geocoded + **no AIS match** — dark vessel candidate |

`score` (0–1): composite quality score. Only `dark=1 AND score >= 0.2` are shown
in the timeline map. See **Detection Quality Scoring** below.

---

## Always-Running Services (Task Scheduler)

Services are managed by **Windows Task Scheduler** — they start automatically at
login and restart on failure. **No manual terminal windows needed.**

### First-time setup (run once):
```powershell
powershell -ExecutionPolicy Bypass -File D:\Github\Nocturnal\register_tasks.ps1
```

### Check status:
```powershell
Get-ScheduledTask -TaskPath "\Nocturnal\" | Select-Object TaskName, State
```

### If a task has crashed and not restarted:
```powershell
powershell -ExecutionPolicy Bypass -File D:\Github\Nocturnal\start_services.ps1
```

### The 7 services registered:
| Task name | Script | Purpose |
|---|---|---|
| AIS-Tracker | `aishub_tracker.py` | AIS collection, continuous |
| Web-Server | `nocturnal_server.py` | Web UI at localhost:5050 |
| Weather-Fetch | `weather_fetch.py --daemon` | Weather data collection |
| AIS-Live-Map | `ais_live_map.py` | Rebuilds every 5 min |
| AIS-Map | `ais_map.py` | Rebuilds every 12h |
| Weather-Map | `weather_map.py` | Rebuilds every 20 min |
| Weather-Map2 | `weather_map2.py` | Rebuilds every 20 min |

---

## Output Files (served by nocturnal_server.py at localhost:5050)

| File | Rebuilt by | Interval |
|---|---|---|
| `ais_live_map.html` | `ais_live_map.py` | every 5 min |
| `ais_map.html` | `ais_map.py` | every 12h |
| `weather_map.html` | `weather_map.py` | every 20 min |
| `weather_map2.html` | `weather_map2.py` | every 20 min |
| `ais_timeline_map.html` | `ais_timeline_map.py` | **manually**, after new SAR scenes |

All HTML files contain `<meta http-equiv="refresh">` for browser auto-reload.

---

## SAR / Dark Vessel Pipeline

### Data layout
```
sentinel_data/              ← downloaded .SAFE folders (~1.7 GB each)
sar_overlays/               ← sar_overlay_YYYYMMDDTHHMMSS.{png,json} per slice
dark_chips_YYYYMMDD/        ← cropped SAR chip images for dark vessel popups
infrastructure.geojson      ← cached offshore infrastructure (wind farms, cables…)
runs/detect/…               ← YOLO weight files (best.pt)
```

### Full workflow for new scenes
```powershell
# 1. Download new scenes (auto-skips already downloaded)
.\venv\Scripts\python.exe batch_download.py --after 2026-04-23 --before 2026-06-02

# 2. Run pipeline on all unprocessed scenes
.\venv\Scripts\python.exe run_pipeline.py --all

# 3. Rescore all detections (if scoring parameters changed)
.\venv\Scripts\python.exe rescore_detections.py

# 4. Regenerate timeline map
.\venv\Scripts\python.exe ais_timeline_map.py --all
```

### Pipeline phases (via run_pipeline.py)
1. `extract_sar_overlay.py` — warp SAR → WGS84 PNG + bounds JSON
2. `yolo_infer_sar.py` — YOLOv8 sliding-window ship detection, writes to `detections` table
3. `geocode_match.py` — pixel → GPS, AIS cross-reference, **computes quality score**
4. `extract_chips.py` — crop SAR chips for popup previews (non-fatal if it fails)

**Do not re-run pipeline on already-processed scenes** — `yolo_infer_sar.py` uses plain
`INSERT` (no `OR IGNORE`), so duplicate rows accumulate. Check `sar_overlays/` to see
what's already done before running `--all`.

---

## Detection Quality Scoring

YOLO confidence alone is a **poor quality signal** — 0.8 conf detections are often land
infrastructure; 0.2 conf detections can be real vessels. A composite score is computed
in `geocode_match.py → detection_score()` and stored in `detections.score`.

### Score formula (0–1, multiplicative — any zero factor kills the score)
```python
# 1. AOI bounds: outside the AOI polygon bbox → score = 0.0
#    (catches North Sea ships when scene footprint extends beyond Channel)
if not (AOI_LAT_MIN <= lat <= AOI_LAT_MAX and AOI_LON_MIN <= lon <= AOI_LON_MAX):
    return 0.0

# 2. Size factor: ships at 10m/px should be <35px across
#    Confirmed AIS vessels avg 29×24px; land blobs are 120–445px
max_dim = max(width_px, height_px)
size_factor = max(0.0, 1.0 - max(0.0, max_dim - 35) / 50)  # 0 at 85px

# 3. Coast factor: 0 if within 1.5km of any land (catches harbour/breakwater bleed)
coast_factor = 0.0 if _near_coast(lat, lon, buffer_km=1.5) else 1.0

score = confidence * size_factor * coast_factor
```

### Map display threshold
Only `dark=1 AND score >= 0.2` shown. Currently ~195 of 663 dark candidates pass.

### To rescore after changing parameters:
```powershell
.\venv\Scripts\python.exe rescore_detections.py          # all detections
.\venv\Scripts\python.exe rescore_detections.py --dry-run  # preview only
```

---

## Downloading Sentinel-1 Scenes

```powershell
# Preview what's available
.\venv\Scripts\python.exe batch_download.py --after 2026-04-23 --before 2026-06-02 --dry-run

# Download (credentials from ~/.cdse_credentials, line 1: user, line 2: password)
.\venv\Scripts\python.exe batch_download.py --after 2026-04-23 --before 2026-06-02
```

**How it works:**
- Queries CDSE in **7-day chunks** (CDSE OData API caps at 200 results, no `$skip` support)
- Skips `.SAFE` folders already on disk
- Filters to morning passes: `CHANNEL_PASS_HOUR_UTC = 6` (±1h)
- Refreshes CDSE auth token every 5 scenes
- Monitor: `Get-Content D:\Github\Nocturnal\download.log -Tail 5`

**If download stops mid-scene:** Delete the partial `.zip` in `sentinel_data/` before
restarting — the script treats any existing zip as complete and tries to extract it.
```powershell
Get-ChildItem D:\Github\Nocturnal\sentinel_data -Filter "*.zip" | Remove-Item -Force
.\venv\Scripts\python.exe batch_download.py --after 2026-04-23 --before 2026-06-02
```

---

## Infrastructure Layer

Offshore infrastructure (wind farms, cables, pipelines, platforms) is fetched from
OpenStreetMap and embedded in the timeline map as a toggleable layer.

```powershell
# Re-fetch from Overpass API (do periodically as new farms are built)
.\venv\Scripts\python.exe fetch_infrastructure.py --force
.\venv\Scripts\python.exe ais_timeline_map.py --all   # rebuild map
```

**Coverage:** 442 features — wind farm polygons (blue-green), submarine cables (purple
dashed), pipelines (orange dashed), platforms (orange dots). Individual turbine nodes
(~7,500) are suppressed at display time for performance.

**Why this matters:** Many dark vessel candidates sit inside wind farms — maintenance
vessels, crew transfer boats, cable lay ships. The infrastructure layer immediately
contextualises them.

**Overpass API note:** Requires `User-Agent` header or returns 406. Already set in
`fetch_infrastructure.py`.

---

## AIS Collection Details

**`aishub_tracker.py`** polls AISHub every 60s (rate-limited by `~/.aishub_last_poll`).

Bounding box filter (inside the script, separate from `config.py`):
```python
LAT_MIN, LAT_MAX = 47.9, 51.7
LON_MIN, LON_MAX = -5.8, 4.0
```
Was `LON_MAX = 2.5` before fix — northeast AOI (Strait of Dover / Belgian coast) had
a coverage gap until that commit. Historical data before the fix has this gap.

AIS started: **2026-04-23**.

---

## SAR Scene Footprint vs AOI

Sentinel-1 IW scenes are ~250×170 km. The scene footprint often extends **beyond the
Channel AOI** into the North Sea (52–54°N). Since there is no AIS collection in the
North Sea, ships there appear as dark candidates.

The **AOI bounds check** in `detection_score()` handles this: any detection geocoded
outside `AOI_LAT/LON_MIN/MAX` receives score=0.0 and is suppressed from the map.

---

## Windows-Specific Gotchas

### Unicode crash
Windows uses cp1252 by default. Scripts printing Unicode must wrap stdout at the top:
```python
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
```
All main scripts already have this. If a new script crashes with
`UnicodeEncodeError: 'charmap'`, add these two lines at the very top.

### PowerShell syntax
- No `&&` operator in PS 5.1 — use `;` or `if ($?) { ... }`
- Git commit here-strings: use `@'...'@` (single-quoted), not `$(cat <<EOF)`
- No `head` command — use `Select-Object -First N`

### SQLite WAL + large file copy
`ais_memory.db` uses WAL mode. Before copying:
```python
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
```
Use `robocopy /COPY:DAT` (not `/COPYALL` which requires audit rights).

---

## File Map — Quick Reference

| File | Role |
|---|---|
| `config.py` | Single source of truth: AOI, paths, API keys, thresholds |
| `aoi.geojson` | Non-rectangular Channel polygon |
| `infrastructure.geojson` | Cached offshore infrastructure from OpenStreetMap |
| `aishub_tracker.py` | AIS collection daemon |
| `nocturnal_server.py` | Flask web server at localhost:5050 |
| `weather_fetch.py` | StormGlass weather collection daemon |
| `download_scene.py` | Interactive single-scene Sentinel-1 downloader |
| `batch_download.py` | Batch downloader — date range, 7-day chunked CDSE queries |
| `fetch_infrastructure.py` | Download offshore infrastructure from Overpass API |
| `register_tasks.ps1` | **Run once** — registers all 7 services as Windows Scheduled Tasks |
| `start_services.ps1` | Start any stopped scheduled tasks |
| `run_pipeline.py` | Orchestrator: phases 1–4 on one or more .SAFE scenes |
| `extract_sar_overlay.py` | Phase 1: SAR → WGS84 PNG |
| `yolo_infer_sar.py` | Phase 2: YOLOv8 detection (plain INSERT — don't re-run on processed scenes) |
| `geocode_match.py` | Phase 3: geocode + AIS match + quality scoring |
| `extract_chips.py` | Phase 4: SAR chip images (limit=500, score≥0.2 filter, skip-if-exists) |
| `rescore_detections.py` | Re-run scoring on already-geocoded detections (no YOLO re-run) |
| `ais_timeline_map.py` | Main multi-scene dark vessel map with infrastructure layer |
| `ais_live_map.py` | Live AIS snapshot (latest position per vessel, rebuilds every 5 min) |
| `ais_map.py` | Full AIS track map with time slider (rebuilds every 12h) |
| `weather_map.py` | Weather conditions map |
| `weather_map2.py` | Weather + AIS combined map |
| `ais_store.py` | SQLite schema + insert helpers for AIS |
| `weather_store.py` | SQLite schema + insert helpers for weather |
| `review_chips.py` | Manual chip review tool |
| `requirements.txt` | Pip deps (torch needs `--index-url https://download.pytorch.org/whl/cu124`) |

---

## Current State (as of 2026-06-02)

### AIS
- Collecting continuously since **2026-04-23**
- ~96M+ positions, 20k+ vessels, growing ~1M rows/day
- Known northeast gap before LON_MAX was fixed to 4.0 (was 2.5)

### Sentinel SAR — processed passes
| Pass | Date | Satellite | Dark (shown) | Chips |
|---|---|---|---|---|
| Apr 28 S1A | 06:23 UTC | S1A orbit 064276 | 33 | 33 |
| Apr 28 S1D | 06:31 UTC | S1D orbit 002543 | 17 | 17 |
| Apr 29 S1C | 06:14 UTC | S1C orbit 007427 | 1 | 1 |
| May 6 S1C | 06:06 UTC | S1C orbit 007529 | 0 (all matched) | — |
| May 12 S1D | 06:14 UTC | S1D orbit 002747 | 49 | 49 |
| May 18 S1C | 06:05 UTC | S1C orbit 007704 | 44 | 44 |
| May 24 S1A | 06:07 UTC | S1A orbit 064655 | 29 | 29 |
| May 24 S1D | 06:14 UTC | S1D orbit 002922 | 13 | 13 |
| May 25 S1A | 17:57 UTC | S1A orbit 064677 | 4 | 4 |

### Batch download (in progress)
`batch_download.py` is downloading all 172 morning-pass scenes Apr 23 → Jun 1.
~70+ of 172 scenes already on disk. Monitor:
```powershell
Get-Content D:\Github\Nocturnal\download.log -Tail 5
Get-ChildItem D:\Github\Nocturnal\sentinel_data -Filter "*.SAFE" | Measure-Object
```
If it stops: delete partial zip, restart with the same command.

**When download completes**, process new scenes:
```powershell
.\venv\Scripts\python.exe run_pipeline.py --all
.\venv\Scripts\python.exe rescore_detections.py
# Re-run extract_chips for any scenes with missing chips:
.\venv\Scripts\python.exe extract_chips.py --scene <name> --safe sentinel_data\<name>
.\venv\Scripts\python.exe ais_timeline_map.py --all
```

### C: copy
`C:\Users\Exposition\Documents\Github\_Nocturnal\Nocturnal` — original location,
preserved pending deletion confirmation. **Not active.** All services run from D: only.
Safe to delete when ready.

### Ongoing automation (TODO)
A scheduled daily task to check for new Sentinel scenes, download, and process them
has been discussed but not yet implemented. Currently manual.
