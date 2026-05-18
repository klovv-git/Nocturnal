"""
config.py — shared constants for the NOCTURNAL pipeline.

Edit this file to change the area of interest, database path, or other
pipeline-wide settings. All scripts import from here.
"""

from pathlib import Path

# ── Area of interest ──────────────────────────────────────────────────────────
# English Channel + southern North Sea (Thames Estuary → Dutch/Belgian coast)
#
#   53°N  ┌─────────────────────────────────────┐
#         │  southern North Sea                  │
#         │  (Thames Est. / NL / BE coasts)      │
#   51°N  │          ┌──────────────────────────┤
#         │          │ Strait of Dover           │
#   49°N  └──────────┴──────────────────────────┘
#        -2.5°E                               6.5°E

AOI_LAT_MIN =  49.0
AOI_LAT_MAX =  53.0
AOI_LON_MIN =  -2.5
AOI_LON_MAX =   6.5

# WKT polygon for CDSE scene search (lon/lat order, SRID 4326)
AOI_WKT = (
    f"POLYGON(({AOI_LON_MIN} {AOI_LAT_MIN}, "
    f"{AOI_LON_MAX} {AOI_LAT_MIN}, "
    f"{AOI_LON_MAX} {AOI_LAT_MAX}, "
    f"{AOI_LON_MIN} {AOI_LAT_MAX}, "
    f"{AOI_LON_MIN} {AOI_LAT_MIN}))"
)

# ── Folder layout ─────────────────────────────────────────────────────────────
SENTINEL_DATA_DIR = Path("sentinel_data")   # downloaded .SAFE folders + zips
SAR_OVERLAYS_DIR  = Path("sar_overlays")    # sar_overlay_YYYYMMDD.png + .json
CHIPS_DIR_PREFIX  = "dark_chips_"           # per-scene chip folders: dark_chips_YYYYMMDD
REVIEWS_DIR       = Path("reviews")         # chip review JSON files

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = Path("ais_memory.db")

# ── Known good pass times (UTC hour) ─────────────────────────────────────────
# Sentinel-1 passes over the English Channel / southern North Sea at consistent
# times. Use --pass-hour to filter downloads to these windows.
#
#   ~06:13 UTC  — morning pass, confirmed over water (S1D orbit 002747, May 12)
#   (evening passes at ~17:20-18:05 UTC cover a different, land-heavy track)
#
# Usage: python download_scene.py --after 2026-05-18 --pass-hour 6
CHANNEL_PASS_HOUR_UTC = 6

# ── Pipeline defaults ─────────────────────────────────────────────────────────
TIMELINE_HOURS  = 12    # AIS window around each satellite pass (hours)
THIN_MINUTES    = 5     # keep one AIS ping per vessel per N minutes
BBOX_PAD        = 1.0   # degrees of padding around detections bounding box

# AIS matching thresholds
MATCH_RADIUS_KM = 1.0
MATCH_WINDOW_MIN = 30
