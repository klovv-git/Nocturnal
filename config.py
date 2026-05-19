"""
config.py — shared constants for the NOCTURNAL pipeline.

Edit this file to change the area of interest, database path, or other
pipeline-wide settings. All scripts import from here.

Area of interest:
  Drop an aoi.geojson file in this folder to override the default AOI.
  Easiest way to create one: draw your polygon at https://geojson.io,
  then save as aoi.geojson next to this file.
  If no aoi.geojson is found, the hardcoded bounding box below is used.
"""

import json
from pathlib import Path

# ── Area of interest ──────────────────────────────────────────────────────────
# Fallback bounding box — overridden below if aoi.geojson is present.
AOI_LAT_MIN =  47.8
AOI_LAT_MAX =  51.8
AOI_LON_MIN =  -5.8
AOI_LON_MAX =   4.0

def _geojson_to_wkt(path: Path) -> str:
    """Convert the first polygon in a GeoJSON file to a WKT string."""
    gj = json.loads(path.read_text())
    # unwrap FeatureCollection → Feature → Geometry
    if gj.get("type") == "FeatureCollection":
        gj = gj["features"][0]
    if gj.get("type") == "Feature":
        gj = gj["geometry"]
    coords = gj["coordinates"][0]   # outer ring of first polygon
    pairs  = ", ".join(f"{lon} {lat}" for lon, lat in coords)
    return f"POLYGON(({pairs}))"

def _default_wkt() -> str:
    return (
        f"POLYGON(({AOI_LON_MIN} {AOI_LAT_MIN}, "
        f"{AOI_LON_MAX} {AOI_LAT_MIN}, "
        f"{AOI_LON_MAX} {AOI_LAT_MAX}, "
        f"{AOI_LON_MIN} {AOI_LAT_MAX}, "
        f"{AOI_LON_MIN} {AOI_LAT_MIN}))"
    )

# Load aoi.geojson if present — overrides both AOI_WKT and the bounding box constants
_AOI_GEOJSON = Path(__file__).parent / "aoi.geojson"
if _AOI_GEOJSON.exists():
    AOI_WKT = _geojson_to_wkt(_AOI_GEOJSON)
    _gj = json.loads(_AOI_GEOJSON.read_text())
    if _gj.get("type") == "FeatureCollection": _gj = _gj["features"][0]
    if _gj.get("type") == "Feature":           _gj = _gj["geometry"]
    _coords     = _gj["coordinates"][0]
    AOI_LON_MIN = min(c[0] for c in _coords)
    AOI_LON_MAX = max(c[0] for c in _coords)
    AOI_LAT_MIN = min(c[1] for c in _coords)
    AOI_LAT_MAX = max(c[1] for c in _coords)
    print(f"[config] AOI loaded from {_AOI_GEOJSON.name}  "
          f"({AOI_LAT_MIN:.2f}–{AOI_LAT_MAX:.2f}°N, "
          f"{AOI_LON_MIN:.2f}–{AOI_LON_MAX:.2f}°E)")
else:
    AOI_WKT = _default_wkt()

# ── Folder layout ─────────────────────────────────────────────────────────────
SENTINEL_DATA_DIR = Path("sentinel_data")   # downloaded .SAFE folders + zips
SAR_OVERLAYS_DIR  = Path("sar_overlays")    # sar_overlay_YYYYMMDD.png + .json
CHIPS_DIR_PREFIX  = "dark_chips_"           # per-scene chip folders: dark_chips_YYYYMMDD
REVIEWS_DIR       = Path("reviews")         # chip review JSON files

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH         = Path("ais_memory.db")
WEATHER_DB_PATH = Path("weather.db")

# ── API keys ──────────────────────────────────────────────────────────────────
# Set your Storm Glass key here, or leave empty and use the
# STORMGLASS_API_KEY environment variable instead.
STORMGLASS_API_KEY = "fa31f8e8-90cc-11ed-bce5-0242ac130002-fa31f956-90cc-11ed-bce5-0242ac130002"

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
