#!/usr/bin/env python3
"""
ais_reappearance.py — for each dark vessel detection, search the AIS database
for vessels that appear in the vicinity AFTER the satellite pass but had no
signal near that location before it.

A vessel that was dark at t_mid but reappears on AIS shortly after may have
turned its transponder back on — making it a strong candidate for the dark
detection.

Usage:
    python ais_reappearance.py --scene <SAFE folder name>

Output:
    Console table of candidate reappearances, sorted by detection ID and
    time of first reappearance.
"""

import argparse
import datetime
import math
import re
import sqlite3
import calendar
from pathlib import Path

DEFAULT_DB   = Path("ais_memory.db")
MAX_SPEED_KT = 25       # knots — maximum plausible vessel speed for radius calc
QUIET_KM     = 50       # km — if a vessel had a ping within this radius BEFORE
                         #       t_mid it is NOT a reappearance (it was already there)
QUIET_HOURS  = 2        # hours before t_mid to check for prior presence


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in kilometres."""
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a  = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bbox(lat, lon, km):
    """Return a (lat_min, lat_max, lon_min, lon_max) bounding box."""
    dlat = km / 111.0
    dlon = km / (111.0 * math.cos(math.radians(lat)))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


def parse_scene_time(scene_name):
    matches = re.findall(r'(\d{8}T\d{6})', scene_name)
    if len(matches) < 2:
        return None, None
    def to_ts(s):
        dt = datetime.datetime.strptime(s, "%Y%m%dT%H%M%S")
        return calendar.timegm(dt.timetuple())
    return to_ts(matches[0]), to_ts(matches[1])


def fmt_ts(ts):
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M UTC")


def fmt_bearing(lat1, lon1, lat2, lon2):
    """Compass bearing from point 1 to point 2."""
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(dlon))
    b = math.degrees(math.atan2(y, x))
    return (b + 360) % 360


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene",   required=True)
    ap.add_argument("--db",      default=str(DEFAULT_DB))
    ap.add_argument("--hours",   type=float, default=6,
                    help="Hours after t_mid to search for reappearances")
    ap.add_argument("--speed",   type=float, default=MAX_SPEED_KT,
                    help="Max vessel speed in knots for expanding radius")
    ap.add_argument("--quiet-km", type=float, default=QUIET_KM,
                    help="Radius (km) within which a prior AIS ping disqualifies a vessel")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    t_start, t_end = parse_scene_time(args.scene)
    if not t_start:
        raise SystemExit("Could not parse scene timestamps.")
    t_mid   = (t_start + t_end) / 2
    t_after = t_mid + args.hours * 3600
    t_quiet = t_mid - QUIET_HOURS * 3600

    print(f"\nNOCTURNAL — AIS Reappearance Analysis")
    print(f"Scene   : {args.scene[:60]}")
    print(f"t_mid   : {fmt_ts(t_mid)}")
    print(f"Window  : {fmt_ts(t_mid)} → {fmt_ts(t_after)}")
    print(f"Max speed: {args.speed} kt   Quiet radius: {args.quiet_km} km")

    # load dark detections for this scene
    dark = conn.execute(
        """SELECT id, lat, lon, confidence
           FROM detections
           WHERE scene_name = ? AND dark = 1 AND lat IS NOT NULL""",
        (args.scene,)
    ).fetchall()

    if not dark:
        raise SystemExit("No dark detections found for this scene.")

    print(f"\n{len(dark)} dark detection(s) to analyse\n")

    # vessel name lookup
    vessel_names = {}
    for row in conn.execute("SELECT mmsi, name FROM vessels").fetchall():
        if row[1]:
            vessel_names[row[0]] = row[1].strip()

    # get MMSIs that were matched (already confirmed AIS vessels — exclude them)
    matched_mmsis = set(
        r[0] for r in conn.execute(
            "SELECT matched_mmsi FROM detections WHERE scene_name = ? AND dark = 0",
            (args.scene,)
        ).fetchall() if r[0]
    )

    all_results = []

    for det_id, det_lat, det_lon, conf in dark:

        # --- step 1: find candidate MMSIs that appear AFTER t_mid ---
        # use an expanding bounding box: max distance = elapsed_time * max_speed
        max_km_after = (args.hours * 3600 * args.speed * 1852) / 1000  # km
        la_min, la_max, lo_min, lo_max = bbox(det_lat, det_lon, max_km_after)

        after_rows = conn.execute(
            """SELECT mmsi, lat, lon, ts_epoch
               FROM positions
               WHERE ts_epoch BETWEEN ? AND ?
                 AND lat BETWEEN ? AND ?
                 AND lon BETWEEN ? AND ?
               ORDER BY mmsi, ts_epoch""",
            (t_mid, t_after, la_min, la_max, lo_min, lo_max)
        ).fetchall()

        # group by MMSI, keep first ping after t_mid
        first_after = {}
        for mmsi, lat, lon, ts in after_rows:
            if mmsi in matched_mmsis:
                continue
            dt_sec = ts - t_mid
            max_km = (dt_sec * args.speed * 1852) / 3600 / 1000
            dist   = haversine_km(det_lat, det_lon, lat, lon)
            if dist > max_km:
                continue   # too far given time elapsed and max speed
            if mmsi not in first_after or ts < first_after[mmsi][0]:
                first_after[mmsi] = (ts, lat, lon, dist)

        if not first_after:
            continue

        # --- step 2: disqualify MMSIs that had a ping near the detection BEFORE t_mid ---
        lb_min, lb_max, lo2_min, lo2_max = bbox(det_lat, det_lon, args.quiet_km)
        before_rows = conn.execute(
            """SELECT DISTINCT mmsi
               FROM positions
               WHERE ts_epoch BETWEEN ? AND ?
                 AND lat BETWEEN ? AND ?
                 AND lon BETWEEN ? AND ?""",
            (t_quiet, t_mid, lb_min, lb_max, lo2_min, lo2_max)
        ).fetchall()
        present_before = set(r[0] for r in before_rows)

        candidates = {
            mmsi: data
            for mmsi, data in first_after.items()
            if mmsi not in present_before
        }

        if not candidates:
            continue

        # sort by time of reappearance
        for mmsi, (ts, lat, lon, dist) in sorted(candidates.items(), key=lambda x: x[1][0]):
            dt_min  = (ts - t_mid) / 60
            bearing = fmt_bearing(det_lat, det_lon, lat, lon)
            name    = vessel_names.get(mmsi, "Unknown")
            all_results.append({
                "det_id":  det_id,
                "det_lat": det_lat,
                "det_lon": det_lon,
                "conf":    conf,
                "mmsi":    mmsi,
                "name":    name,
                "ts":      ts,
                "lat":     lat,
                "lon":     lon,
                "dist_km": dist,
                "dt_min":  dt_min,
                "bearing": bearing,
            })

    # --- print results ---
    if not all_results:
        print("No reappearances found.")
        return

    print(f"{'Det':>4}  {'Conf':>4}  {'MMSI':>12}  {'Vessel name':<22}  "
          f"{'After':>6}  {'Dist':>7}  {'Brg':>4}  {'Reappeared at'}")
    print("-" * 100)

    for r in sorted(all_results, key=lambda x: (x["det_id"], x["dt_min"])):
        print(f"{r['det_id']:>4}  {r['conf']:>4.2f}  {r['mmsi']:>12}  "
              f"{r['name']:<22.22}  "
              f"{r['dt_min']:>5.0f}m  {r['dist_km']:>6.1f}km  "
              f"{r['bearing']:>4.0f}°  "
              f"{fmt_ts(r['ts'])}  ({r['lat']:.4f}N, {r['lon']:.4f}E)")

    print(f"\nTotal candidates: {len(all_results)} across {len(set(r['det_id'] for r in all_results))} detection(s)")
    print("\nNote: these are vessels with NO prior AIS signal near the detection")
    print("location that appeared within plausible sailing distance after the pass.")
    print("Shorter reappearance time and smaller distance = stronger candidate.")


if __name__ == "__main__":
    main()
