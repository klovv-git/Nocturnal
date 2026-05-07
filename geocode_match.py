#!/usr/bin/env python3
"""
geocode_match.py — NOCTURNAL Phase 4: pixel → GPS conversion and
AIS cross-referencing.

This is where detected ship pixels become geolocated vessels, and
where we decide which of those vessels are "dark" (not broadcasting
AIS at the moment the satellite passed overhead).

Flow, per scene:
  1. Load all detections for the scene (from Phase 3) that still have
     NULL lat/lon.
  2. Open the scene's .tiff, pull its GCPs, build a polynomial
     transform, and convert each detection's (pixel_y, pixel_x) into
     WGS84 (lon, lat).
  3. Look up the scene's acquisition time from sentinel_products.
  4. For each detection, query the `positions` R-tree for any AIS
     ping within `radius_km` of the detection AND within ±`dt_min`
     of the acquisition time. Pick the closest match.
  5. Write back: lat, lon, matched_mmsi, match_dist_m, dark.

A detection is `dark=1` if NO AIS ping passes the spatial+temporal
filter. This is the raw Dark Vessel candidate list — Phase 5 scores
and prioritises it.

Install:
    pip install rasterio  (already a Phase 3 dep)

CLI:
    python geocode_match.py --scene S1A_IW_GRDH_..._SAFE \\
        --safe /data/scenes/S1A_IW_GRDH_..._SAFE \\
        --radius-km 1.0 --dt-min 30
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import struct

try:
    import numpy as np
    import rasterio
    from scipy.interpolate import LinearNDInterpolator
except ImportError as e:
    raise SystemExit(
        "geocode_match.py needs: pip install rasterio scipy numpy\n"
        f"Missing: {getattr(e, 'name', e)}") from e


def _gcp_val(v) -> float:
    """
    Safely convert a GCP attribute to a Python float.

    On some rasterio/GDAL builds (particularly Windows) the raw C float
    is exposed as np.bytes_ instead of a Python float.  We detect and
    unpack it with struct so the rest of the code can treat it normally.
    """
    if isinstance(v, (bytes, np.bytes_)):
        b = bytes(v)
        if len(b) == 8:
            return struct.unpack("<d", b)[0]   # double
        if len(b) == 4:
            return struct.unpack("<f", b)[0]   # float
    return float(v)

# Phase 3 already depends on sar_preprocess; reuse its locator.
from sar_preprocess import locate_measurement   # noqa: E402


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "ais_memory.db"


# ────────────────────── geodesy ──────────────────────

_EARTH_R_M = 6_371_008.8   # IUGG mean Earth radius, metres


def haversine_m(lat1: float, lon1: float,
                lat2: float, lon2: float) -> float:
    """
    Great-circle distance between two WGS84 points, in metres.
    Good enough at the spatial scales we care about (<10 km).
    """
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2)
    return 2 * _EARTH_R_M * math.asin(math.sqrt(a))


def _bbox_deg(lat: float, lon: float, radius_m: float
              ) -> Tuple[float, float, float, float]:
    """Cheap lat/lon box that encloses a radius-R disc around (lat,lon)."""
    dlat = radius_m / 111_320.0
    # cos clamp so we don't blow up near the poles (irrelevant for the
    # Channel, but cheap insurance).
    coslat = max(math.cos(math.radians(lat)), 1e-6)
    dlon = radius_m / (111_320.0 * coslat)
    return (lat - dlat, lat + dlat, lon - dlon, lon + dlon)


# ────────────────────── pixel → geo ──────────────────────

def _parse_geolocation_grid(safe: Path, pol: str
                            ) -> Tuple[np.ndarray, np.ndarray,
                                       np.ndarray, np.ndarray]:
    """
    Parse the Sentinel-1 annotation XML geolocationGridPoint elements.

    Every Sentinel-1 SAFE contains an annotation XML with a regular grid
    of (line, pixel, latitude, longitude) points — typically one point
    every ~500 pixels. This is the authoritative geocoding source and
    requires only the built-in xml module.

    Returns (rows, cols, lats, lons) as float64 arrays.
    """
    import xml.etree.ElementTree as ET
    ann_glob = f"annotation/s1*-iw-grd-{pol.lower()}-*.xml"
    matches = list(safe.glob(ann_glob))
    if not matches:
        # Try without the iw- prefix (EW or SM modes)
        matches = list(safe.glob(f"annotation/*-{pol.lower()}-*.xml"))
    if not matches:
        raise FileNotFoundError(
            f"No annotation XML for pol={pol} under {safe}")
    tree = ET.parse(matches[0])
    rows, cols, lats, lons = [], [], [], []
    for pt in tree.getroot().findall(".//geolocationGridPoint"):
        rows.append(float(pt.find("line").text))
        cols.append(float(pt.find("pixel").text))
        lats.append(float(pt.find("latitude").text))
        lons.append(float(pt.find("longitude").text))
    return (np.array(rows, dtype=np.float64),
            np.array(cols, dtype=np.float64),
            np.array(lats, dtype=np.float64),
            np.array(lons, dtype=np.float64))


def _open_gcp_transform(safe: Path, pol: str):
    """
    Build scipy interpolators (pixel row/col → lon/lat) by parsing the
    Sentinel-1 annotation XML geolocation grid.

    This avoids rasterio's GCP wrapper entirely — on some Windows builds
    rasterio exposes GCP attributes as raw np.bytes_ instead of floats,
    and osgeo (GDAL Python bindings) is not available when rasterio ships
    its own bundled GDAL.  The annotation XML is always present and uses
    only the built-in xml module.
    """
    tif = locate_measurement(safe, pol)
    src = rasterio.open(tif)   # still needed so process_scene can close it

    rows, cols, lats, lons = _parse_geolocation_grid(safe, pol)
    pts = np.column_stack([rows, cols])
    lon_interp = LinearNDInterpolator(pts, lons)
    lat_interp = LinearNDInterpolator(pts, lats)

    return src, (lon_interp, lat_interp), None


def pixel_to_lonlat(transform, row: float, col: float
                    ) -> Tuple[float, float]:
    """
    Map a full-scene pixel (row, col) to (lon, lat) using the
    scipy interpolators built from the scene's GCPs.
    """
    lon_interp, lat_interp = transform
    query = np.array([[row, col]])
    lon = float(lon_interp(query)[0])
    lat = float(lat_interp(query)[0])
    return lon, lat


# ────────────────────── AIS match ──────────────────────

@dataclass
class PingMatch:
    mmsi: int
    dist_m: float
    dt_s: float
    ts_epoch: float
    lat: float
    lon: float


def _nearest_ping(conn: sqlite3.Connection,
                  lat: float, lon: float,
                  t_mid: float, dt_s: float,
                  radius_m: float) -> Optional[PingMatch]:
    """
    Use the R-tree bbox filter first (cheap, index-backed), then
    recompute the precise haversine distance on the shortlist and
    return the closest point that still satisfies both limits.
    """
    lat_lo, lat_hi, lon_lo, lon_hi = _bbox_deg(lat, lon, radius_m)
    q = """
        SELECT p.mmsi, p.ts_epoch, p.lat, p.lon
        FROM positions_rtree r
        JOIN positions p ON p.id = r.id
        WHERE r.min_lat >= ? AND r.max_lat <= ?
          AND r.min_lon >= ? AND r.max_lon <= ?
          AND p.ts_epoch BETWEEN ? AND ?
    """
    t_lo, t_hi = t_mid - dt_s, t_mid + dt_s
    rows = conn.execute(q, (lat_lo, lat_hi, lon_lo, lon_hi,
                            t_lo, t_hi)).fetchall()
    if not rows:
        return None

    best: Optional[PingMatch] = None
    for mmsi, tse, plat, plon in rows:
        d = haversine_m(lat, lon, plat, plon)
        if d > radius_m:
            continue   # bbox let in the corners of the square
        if best is None or d < best.dist_m:
            best = PingMatch(mmsi=int(mmsi), dist_m=float(d),
                             dt_s=float(tse - t_mid),
                             ts_epoch=float(tse),
                             lat=float(plat), lon=float(plon))
    return best


# ────────────────────── scene orchestration ──────────────────────

def _scene_start_epoch(conn: sqlite3.Connection,
                       scene_name: str) -> Optional[float]:
    row = conn.execute(
        "SELECT start_epoch FROM sentinel_products WHERE name = ?",
        (scene_name,)).fetchone()
    return float(row[0]) if row else None


def process_scene(scene_name: str,
                  safe: Path,
                  db_path: os.PathLike = DEFAULT_DB_PATH,
                  pol: Optional[str] = None,
                  radius_km: float = 1.0,
                  dt_min: float = 30.0,
                  dry_run: bool = False) -> dict:
    """
    Geocode and match every detection belonging to `scene_name`.
    Returns a small summary dict; also writes back to the database
    unless dry_run=True.
    """
    safe = Path(safe)
    radius_m = radius_km * 1000.0
    dt_s     = dt_min * 60.0

    conn = sqlite3.connect(os.fspath(db_path), timeout=30)
    try:
        # Pull unresolved detections for this scene (lat IS NULL means
        # Phase 4 hasn't touched it yet; idempotent re-runs).
        dets = conn.execute(
            """SELECT id, pixel_y, pixel_x, polarisation
               FROM detections
               WHERE scene_name = ? AND lat IS NULL""",
            (scene_name,)).fetchall()
        if not dets:
            return {"scene": scene_name, "detections": 0,
                    "reason": "nothing to do (no NULL-lat rows)"}

        # Acquisition time from the Phase 2 catalogue row.
        t_mid = _scene_start_epoch(conn, scene_name)
        if t_mid is None:
            # If the product wasn't registered via sentinel_fetch, fall
            # back to the SAFE folder's embedded start time. Keeping
            # this permissive makes the tool usable on ad-hoc scenes.
            t_mid = _start_epoch_from_safe(safe)

        pol_eff = pol or (dets[0][3] or "vv")
        src, transform, crs = _open_gcp_transform(safe, pol_eff)

        updates: List[tuple] = []
        n_dark = n_match = 0
        try:
            for det_id, py, px, _ in dets:
                lon, lat = pixel_to_lonlat(transform, py, px)
                m = _nearest_ping(conn, lat, lon, t_mid, dt_s, radius_m)
                if m is None:
                    n_dark += 1
                    updates.append((lat, lon, None, None, 1, det_id))
                else:
                    n_match += 1
                    updates.append((lat, lon, m.mmsi, m.dist_m, 0, det_id))
        finally:
            src.close()

        if dry_run:
            return {"scene": scene_name, "detections": len(dets),
                    "matched": n_match, "dark": n_dark,
                    "t_mid": t_mid, "dry_run": True}

        conn.executemany(
            """UPDATE detections
                  SET lat = ?, lon = ?,
                      matched_mmsi = ?, match_dist_m = ?, dark = ?
                WHERE id = ?""",
            updates)
        conn.commit()

        return {"scene": scene_name, "detections": len(dets),
                "matched": n_match, "dark": n_dark, "t_mid": t_mid}
    finally:
        conn.close()


def _start_epoch_from_safe(safe: Path) -> float:
    """
    Fallback when the scene wasn't registered in sentinel_products.
    Parses the SAFE folder name: the 4th underscore-delimited field
    is the acquisition start time in YYYYMMDDTHHMMSS form.
    Example: S1A_IW_GRDH_1SDV_20240115T061528_20240115T061553_...
    """
    from datetime import datetime, timezone
    parts = safe.name.split("_")
    # Find the first *T*-stamped part
    for p in parts:
        if len(p) >= 15 and p[8:9] == "T":
            dt = datetime.strptime(p[:15], "%Y%m%dT%H%M%S")
            return dt.replace(tzinfo=timezone.utc).timestamp()
    raise RuntimeError(f"cannot infer acquisition time from {safe.name}")


# ────────────────────── CLI ──────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scene", required=True,
                    help="scene_name (SAFE folder name) as stored in detections")
    ap.add_argument("--safe", type=Path, required=True,
                    help="path to the unzipped .SAFE directory")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--pol", default=None, choices=["vv", "vh", None])
    ap.add_argument("--radius-km", type=float, default=1.0,
                    help="spatial match radius (default 1.0 km)")
    ap.add_argument("--dt-min", type=float, default=30.0,
                    help="temporal match half-window in minutes (default ±30)")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and report, but don't write back to DB")
    args = ap.parse_args(argv)

    summary = process_scene(
        scene_name=args.scene, safe=args.safe, db_path=args.db,
        pol=args.pol, radius_km=args.radius_km, dt_min=args.dt_min,
        dry_run=args.dry_run)
    print("[phase4]", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
