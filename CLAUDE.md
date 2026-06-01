# NOCTURNAL — Claude Handoff Guide

## What This Project Is

A maritime surveillance system for the **English Channel / Western Approaches**.
Two independent data streams are correlated to detect "dark vessels" — ships visible on
Sentinel-1 SAR radar imagery that have no corresponding AIS transponder signal.

```
AIS stream  (live, every ~60s)  →  ais_memory.db   (~40+ GB, growing)
SAR stream  (Sentinel-1 passes) →  sentinel_data/  (.SAFE folders, ~1.7 GB each)
                                        ↓
                              run_pipeline.py
                                        ↓
                    dark vessel detections + AIS correlation
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

**NEVER use the system Python** (`C:\Users\...\WindowsApps\python.exe`). It lacks
the project's dependencies and will silently write to the wrong paths.

---

## The One Config File That Controls Everything

**`config.py`** — read this first.

- **AOI**: loaded from `aoi.geojson` (a non-rectangular polygon following the Channel shape,
  drawn at geojson.io). If the file is absent, falls back to a hardcoded bounding box.
  The polygon is converted to WKT (`AOI_WKT`) for CDSE satellite queries and
  decomposed into `AOI_LAT/LON_MIN/MAX` for DB range queries.
- **All paths are relative** (`Path("ais_memory.db")` etc.) — scripts must be run
  from the project root, not from a subdirectory.
- `CHANNEL_PASS_HOUR_UTC = 6` — Sentinel-1 morning pass over the Channel is ~06:13 UTC.
- `STORMGLASS_API_KEY` is hardcoded here (don't expose in logs/commits).

---

## Databases

| File | Size | Contents |
|---|---|---|
| `ais_memory.db` | ~41 GB | All AIS vessel positions since 2026-04-23, ~96M rows |
| `weather.db` | small | Hourly weather grid from StormGlass API |

### AIS schema (key tables)
```sql
positions  (mmsi, lat, lon, sog, cog, ts_epoch, ts_iso, ship_type, name, ...)
vessels    (mmsi, name, ship_type, ...)   -- deduped vessel registry
```
**Critical indexes:** `idx_positions_ts(ts_epoch)` and `idx_positions_mmsi_ts(mmsi, ts_epoch)`.
If you write SQL against `positions`, avoid `ORDER BY mmsi, ts_epoch` on large scans —
it forces the composite index and causes ~100s query times. Sort in Python after fetching.

The DB file uses **WAL mode**. Before copying the file, run:
```python
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
```

---

## Always-Running Services

These 7 processes must be running at all times. Start them in separate PowerShell
windows from `D:\Github\Nocturnal`:

```powershell
# In separate windows — all from D:\Github\Nocturnal
.\venv\Scripts\python.exe aishub_tracker.py          # AIS collection (continuous)
.\venv\Scripts\python.exe nocturnal_server.py         # Web UI at localhost:5050
.\venv\Scripts\python.exe weather_fetch.py --daemon   # Weather data collection
.\venv\Scripts\python.exe ais_live_map.py             # rebuilds every 5 min
.\venv\Scripts\python.exe ais_map.py                  # rebuilds every 12h
.\venv\Scripts\python.exe weather_map.py              # rebuilds every 20 min
.\venv\Scripts\python.exe weather_map2.py             # rebuilds every 20 min
```

**Do NOT kill `C:\Users\...\WindowsApps\python.exe` processes.**
The venv was built on the Windows Store Python (`pyvenv.cfg home` points to
`WindowsApps\PythonSoftwareFoundation.Python.3.10_...`). On Windows, this uses
an App Execution Alias: running `venv\Scripts\python.exe` spawns a stub that
launches the real interpreter at the WindowsApps path as a child. Both show up
in the process list — they are **one process**, not a duplicate. Killing the
WindowsApps child kills the actual Python worker.

To check service status, use Task Scheduler:
```powershell
Get-ScheduledTask -TaskPath "\Nocturnal\" | Select-Object TaskName, State
```

---

## Output Files (served by nocturnal_server.py)

| File | Rebuilt by | Interval |
|---|---|---|
| `ais_live_map.html` | `ais_live_map.py` | every 5 min |
| `ais_map.html` | `ais_map.py` | every 12h |
| `weather_map.html` | `weather_map.py` | every 20 min |
| `weather_map2.html` | `weather_map2.py` | every 20 min |
| `ais_timeline_map.html` | `ais_timeline_map.py` | **manually**, after new SAR scenes |

All HTML files contain `<meta http-equiv="refresh">` so the browser auto-reloads.

---

## SAR / Dark Vessel Pipeline

### Data layout
```
sentinel_data/          ← downloaded .SAFE folders (~1.7 GB each)
sar_overlays/           ← sar_overlay_YYYYMMDDTHHMMSS.{png,json} per scene slice
dark_chips_YYYYMMDDTHHMMSS/  ← cropped SAR chip images for dark vessel popups
reviews/                ← JSON review records
runs/detect/...         ← YOLO weight files (best.pt)
```

### Running the pipeline on new scenes
```powershell
# Single scene
.\venv\Scripts\python.exe run_pipeline.py sentinel_data\S1X_IW_GRDH_...SAFE

# All scenes at once
.\venv\Scripts\python.exe run_pipeline.py --all

# By orbit number
.\venv\Scripts\python.exe run_pipeline.py --orbit 007704
```

Pipeline phases (run in order, each can fail independently):
1. `extract_sar_overlay.py` — warp SAR → WGS84 PNG + bounds JSON
2. `yolo_infer_sar.py` — YOLOv8 ship detection on the PNG
3. `geocode_match.py` — geocode detections, correlate with AIS within ±30 min / 1 km
4. `extract_chips.py` — crop SAR chips for dark vessel popup previews (non-fatal)

After new scenes are processed, regenerate the timeline map:
```powershell
.\venv\Scripts\python.exe ais_timeline_map.py --all
```

### Downloading new Sentinel-1 scenes
```powershell
# Dry run first — see what's available
.\venv\Scripts\python.exe batch_download.py --after 2026-04-23 --before 2026-06-02 --dry-run

# Actually download (credentials from ~/.cdse_credentials)
.\venv\Scripts\python.exe batch_download.py --after 2026-04-23 --before 2026-06-02
```

`batch_download.py` queries CDSE in **7-day chunks** (CDSE's OData API returns max 200
results and doesn't support `$skip` with complex spatial filters). Already-downloaded
scenes are skipped. Token is refreshed every 5 scenes. Partial `.zip` files from
interrupted downloads must be **manually deleted** before restarting — the script
treats an existing zip as "done" and tries to extract it, which fails if partial.

CDSE credentials are stored in `~/.cdse_credentials` (plaintext, line 1: username, line 2: password).

---

## AIS Collection Details

**`aishub_tracker.py`** polls the AISHub API every 60s (rate-limited by `~/.aishub_last_poll`),
filters positions to the AOI, and writes to `ais_memory.db`.

Bounding box filter (inside the script, separate from `config.py`'s AOI):
```python
LAT_MIN, LAT_MAX = 47.9, 51.7
LON_MIN, LON_MAX = -5.8, 4.0
```
This was expanded in a recent commit (was `LON_MAX = 2.5`, missing northeast AOI coverage).
If you see a spatial gap in AIS data on the east side, check these values.

AIS data started: **2026-04-23**. Coverage has a known gap in the northeast before
the bbox fix was deployed.

---

## Windows-Specific Gotchas

### Unicode crash
Windows uses cp1252 encoding by default. Any script that prints Unicode characters
(box-drawing, degree symbols, arrows) **must** wrap stdout at the top:
```python
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
```
All main scripts already have this. If a new script crashes with `UnicodeEncodeError: 'charmap'`, add these two lines at the very top.

### PowerShell vs bash
Don't use `&&` in PowerShell — it's not supported in PS 5.1. Use `;` or `if ($?) { ... }`.
Here-strings for git commit messages must use `@'...'@` (single-quoted), not `$(cat <<EOF)`.

### Venv portability
Python venvs have hardcoded absolute paths internally and **cannot be moved**.
If the project is relocated, delete `venv/` and recreate:
```powershell
python -m venv venv
.\venv\Scripts\pip.exe install -r requirements.txt
# Torch/CUDA requires separate index:
.\venv\Scripts\pip.exe install torch==2.6.0+cu124 torchvision==0.21.0+cu124 --index-url https://download.pytorch.org/whl/cu124
```

### SQLite WAL + large file copy
`ais_memory.db` uses WAL mode and may have a multi-GB `.wal` file. Before copying:
```python
import sqlite3
conn = sqlite3.connect("ais_memory.db")
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
```
Use `robocopy /COPY:DAT` (not `/COPYALL` which requires audit rights) to copy the file.

---

## File Map — Quick Reference

| File | Role |
|---|---|
| `config.py` | Single source of truth: AOI, paths, API keys, thresholds |
| `aoi.geojson` | Non-rectangular Channel polygon (drawn at geojson.io) |
| `aishub_tracker.py` | AIS collection daemon |
| `nocturnal_server.py` | Flask web server — serves HTML maps at localhost:5050 |
| `weather_fetch.py` | StormGlass weather collection daemon |
| `download_scene.py` | Interactive single-scene Sentinel-1 downloader |
| `batch_download.py` | Batch downloader — all scenes for a date range |
| `run_pipeline.py` | Orchestrator: runs phases 1–4 on one or more scenes |
| `extract_sar_overlay.py` | Phase 1: SAR → WGS84 PNG |
| `yolo_infer_sar.py` | Phase 2: YOLOv8 detection |
| `geocode_match.py` | Phase 3: geocode + AIS match |
| `extract_chips.py` | Phase 4: SAR chip images for popups |
| `ais_timeline_map.py` | Generates the main multi-scene dark vessel map |
| `ais_live_map.py` | Generates live AIS snapshot map (latest pos per vessel) |
| `ais_map.py` | Generates full AIS track map with time slider |
| `weather_map.py` | Weather conditions map |
| `weather_map2.py` | Weather + AIS combined map |
| `ais_store.py` | SQLite schema + insert helpers for AIS |
| `weather_store.py` | SQLite schema + insert helpers for weather |
| `geocode_match.py` | Correlates YOLO detections with AIS positions |
| `review_chips.py` | Manual chip review tool |
| `requirements.txt` | Pip dependencies (torch needs special index URL, see above) |

---

## Current State (as of 2026-06-01)

- **AIS**: collecting continuously since 2026-04-23. ~96M positions, 20k vessels.
- **Sentinel SAR**: 12 scenes processed (May 6, 12, 18, 24×5, 25×2).
- **Batch download in progress**: `batch_download.py` downloading 163 morning-pass scenes
  (Apr 23 → Jun 1). Check progress: `Get-Content D:\Github\Nocturnal\download.log -Tail 5`.
  When complete, run `run_pipeline.py --all` then `ais_timeline_map.py --all`.
- **C: copy**: `C:\Users\Exposition\Documents\Github\_Nocturnal\Nocturnal` — original location,
  preserved pending user deletion confirmation. **Do not treat as active.** All services
  run from D: only.
- **Known gap**: AIS data northeast of ~2.5°E is sparse before the bbox fix was deployed
  (exact date in git log: commit `8fc463da`).
