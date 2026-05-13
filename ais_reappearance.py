#!/usr/bin/env python3
"""
ais_reappearance.py — for each dark vessel detection, search the AIS database
for vessels that appear in the vicinity AFTER the satellite pass.

For each candidate, the vessel's position at t_mid is INTERPOLATED from its
surrounding pings (last ping before + first ping after). This separates two
very different situations:

  MISSED MATCH  — vessel was broadcasting and physically at the detection
                  location at t_mid, but Phase 4 didn't catch it. The dark
                  detection is actually a known ship.

  REAPPEARANCE  — vessel was NOT near the detection at t_mid (it was
                  elsewhere or silent), but showed up nearby afterwards.
                  This is a genuine candidate for a vessel that turned
                  its AIS back on.

Usage:
    python ais_reappearance.py --scene <SAFE folder name>
"""

import argparse
import datetime
import math
import re
import sqlite3
import calendar
from pathlib import Path

DEFAULT_DB       = Path("ais_memory.db")
MAX_SPEED_KT     = 25    # knots — expanding radius cap
SEARCH_HOURS     = 1     # hours after t_mid to search
INTERP_HOURS     = 3     # hours either side of t_mid to find surrounding pings
MISSED_MATCH_KM  = 2.0   # km — interpolated position this close = missed match
REAPPEAR_KM      = 30.0  # km — reappearance search radius after t_mid


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a  = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bbox(lat, lon, km):
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
    return datetime.datetime.utcfromtimestamp(ts).strftime("%H:%M UTC")


def fmt_ts_full(ts):
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M UTC")


def interpolate_position(pings_before, pings_after, t_mid):
    """
    Given sorted lists of (ts, lat, lon) before and after t_mid,
    return the interpolated (lat, lon) at t_mid, or None if not possible.
    """
    if not pings_before or not pings_after:
        return None
    a = pings_before[-1]   # last ping before t_mid
    b = pings_after[0]     # first ping after t_mid
    if b[0] == a[0]:
        return a[1], a[2]
    frac = (t_mid - a[0]) / (b[0] - a[0])
    lat  = a[1] + frac * (b[1] - a[1])
    lon  = a[2] + frac * (b[2] - a[2])
    return lat, lon


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene",        required=True)
    ap.add_argument("--db",           default=str(DEFAULT_DB))
    ap.add_argument("--hours",        type=float, default=SEARCH_HOURS,
                    help="Hours after t_mid to search")
    ap.add_argument("--missed-km",    type=float, default=MISSED_MATCH_KM,
                    help="Interpolated distance threshold for missed match (km)")
    ap.add_argument("--reappear-km",  type=float, default=REAPPEAR_KM,
                    help="Search radius for reappearances (km)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    t_start, t_end = parse_scene_time(args.scene)
    if not t_start:
        raise SystemExit("Could not parse scene timestamps.")
    t_mid    = (t_start + t_end) / 2
    t_after  = t_mid + args.hours * 3600
    t_interp = INTERP_HOURS * 3600   # window to find surrounding pings

    print(f"\nNOCTURNAL — AIS Reappearance Analysis (with trajectory interpolation)")
    print(f"Scene  : {args.scene[:60]}")
    print(f"t_mid  : {fmt_ts_full(t_mid)}")
    print(f"Window : {fmt_ts_full(t_mid)} → {fmt_ts_full(t_after)}")
    print(f"Missed match threshold : {args.missed_km} km")
    print(f"Reappearance radius    : {args.reappear_km} km\n")

    dark = conn.execute(
        """SELECT id, lat, lon, confidence
           FROM detections
           WHERE scene_name = ? AND dark = 1 AND lat IS NOT NULL""",
        (args.scene,)
    ).fetchall()

    if not dark:
        raise SystemExit("No dark detections found for this scene.")

    print(f"{len(dark)} dark detection(s) to analyse\n")

    vessel_names = {}
    for row in conn.execute("SELECT mmsi, name FROM vessels").fetchall():
        if row[1]:
            vessel_names[row[0]] = row[1].strip()

    matched_mmsis = set(
        r[0] for r in conn.execute(
            "SELECT matched_mmsi FROM detections WHERE scene_name = ? AND dark = 0",
            (args.scene,)
        ).fetchall() if r[0]
    )

    missed_matches  = []
    reappearances   = []

    for det_id, det_lat, det_lon, conf in dark:

        # find all vessels within reappear_km that appear AFTER t_mid
        la_min, la_max, lo_min, lo_max = bbox(det_lat, det_lon, args.reappear_km)

        after_rows = conn.execute(
            """SELECT mmsi, lat, lon, ts_epoch
               FROM positions
               WHERE ts_epoch BETWEEN ? AND ?
                 AND lat BETWEEN ? AND ?
                 AND lon BETWEEN ? AND ?
               ORDER BY mmsi, ts_epoch""",
            (t_mid, t_after, la_min, la_max, lo_min, lo_max)
        ).fetchall()

        # keep first ping after t_mid per MMSI, within speed-expanding radius
        first_after = {}
        for mmsi, lat, lon, ts in after_rows:
            if mmsi in matched_mmsis:
                continue
            dt_sec = ts - t_mid
            max_km = (dt_sec * MAX_SPEED_KT * 1852) / 3600 / 1000
            dist   = haversine_km(det_lat, det_lon, lat, lon)
            if dist > max_km:
                continue
            if mmsi not in first_after or ts < first_after[mmsi][0]:
                first_after[mmsi] = (ts, lat, lon, dist)

        if not first_after:
            continue

        # for each candidate, interpolate position at t_mid
        for mmsi, (ts_after, lat_after, lon_after, dist_after) in first_after.items():
            name = vessel_names.get(mmsi, "Unknown")

            # get pings surrounding t_mid for this vessel
            surrounding = conn.execute(
                """SELECT ts_epoch, lat, lon FROM positions
                   WHERE mmsi = ?
                     AND ts_epoch BETWEEN ? AND ?
                   ORDER BY ts_epoch""",
                (mmsi, t_mid - t_interp, t_mid + t_interp)
            ).fetchall()

            pings_before = [(r[0], r[1], r[2]) for r in surrounding if r[0] < t_mid]
            pings_after  = [(r[0], r[1], r[2]) for r in surrounding if r[0] >= t_mid]

            interp = interpolate_position(pings_before, pings_after, t_mid)

            if interp:
                interp_lat, interp_lon = interp
                interp_dist = haversine_km(det_lat, det_lon, interp_lat, interp_lon)
                ping_before = pings_before[-1] if pings_before else None
                ping_after_ = pings_after[0]   if pings_after  else None
                gap_min = ((ping_after_[0] - ping_before[0]) / 60
                           if ping_before and ping_after_ else None)

                if interp_dist <= args.missed_km:
                    # vessel was AT the detection at t_mid — missed AIS match
                    missed_matches.append({
                        "det_id":      det_id,
                        "det_lat":     det_lat,
                        "det_lon":     det_lon,
                        "conf":        conf,
                        "mmsi":        mmsi,
                        "name":        name,
                        "interp_dist": interp_dist,
                        "interp_lat":  interp_lat,
                        "interp_lon":  interp_lon,
                        "gap_min":     gap_min,
                        "ping_before": ping_before,
                        "ping_after":  ping_after_,
                    })
                else:
                    # vessel was elsewhere at t_mid — genuine reappearance candidate
                    reappearances.append({
                        "det_id":      det_id,
                        "det_lat":     det_lat,
                        "det_lon":     det_lon,
                        "conf":        conf,
                        "mmsi":        mmsi,
                        "name":        name,
                        "ts":          ts_after,
                        "dist_after":  dist_after,
                        "dt_min":      (ts_after - t_mid) / 60,
                        "interp_dist": interp_dist,
                        "interp_lat":  interp_lat,
                        "interp_lon":  interp_lon,
                        "gap_min":     gap_min,
                        "ping_before": ping_before,
                        "ping_after":  ping_after_,
                    })
            else:
                # no surrounding pings — vessel appeared with no prior track
                reappearances.append({
                    "det_id":      det_id,
                    "det_lat":     det_lat,
                    "det_lon":     det_lon,
                    "conf":        conf,
                    "mmsi":        mmsi,
                    "name":        name,
                    "ts":          ts_after,
                    "dist_after":  dist_after,
                    "dt_min":      (ts_after - t_mid) / 60,
                    "interp_dist": None,
                    "interp_lat":  None,
                    "interp_lon":  None,
                    "gap_min":     None,
                    "ping_before": None,
                    "ping_after":  None,
                })

    # --- MISSED MATCHES ---
    print("=" * 80)
    print("MISSED AIS MATCHES")
    print("Vessels that WERE at the detection location at t_mid (broadcasting,")
    print("but not caught by Phase 4). These dark detections are actually known ships.")
    print("=" * 80)
    if missed_matches:
        print(f"\n{'Det':>4}  {'Conf':>4}  {'MMSI':>12}  {'Vessel name':<22}  "
              f"{'InterpDist':>10}  {'AIS gap':>7}  Last ping → Next ping")
        print("-" * 95)
        for r in sorted(missed_matches, key=lambda x: (x["det_id"], x["interp_dist"])):
            pb = fmt_ts(r["ping_before"][0]) if r["ping_before"] else "?"
            pa = fmt_ts(r["ping_after"][0])  if r["ping_after"]  else "?"
            gap = f"{r['gap_min']:.0f}m" if r["gap_min"] else "?"
            print(f"{r['det_id']:>4}  {r['conf']:>4.2f}  {r['mmsi']:>12}  "
                  f"{r['name']:<22.22}  {r['interp_dist']:>9.2f}km  "
                  f"{gap:>7}  {pb} → {pa}")
    else:
        print("  None found.\n")

    # --- REAPPEARANCES ---
    print()
    print("=" * 80)
    print("GENUINE REAPPEARANCE CANDIDATES")
    print("Vessels that were NOT at the detection location at t_mid but appeared")
    print("nearby afterwards. May have turned AIS back on after the pass.")
    print("=" * 80)
    if reappearances:
        print(f"\n{'Det':>4}  {'Conf':>4}  {'MMSI':>12}  {'Vessel name':<22}  "
              f"{'After':>6}  {'Dist':>7}  {'InterpDist':>10}  {'AIS gap':>7}")
        print("-" * 100)
        for r in sorted(reappearances, key=lambda x: (x["det_id"], x["dt_min"])):
            id_str = f"{r['interp_dist']:.1f}km" if r["interp_dist"] else "no prior"
            gap    = f"{r['gap_min']:.0f}m"       if r["gap_min"]     else "no prior"
            print(f"{r['det_id']:>4}  {r['conf']:>4.2f}  {r['mmsi']:>12}  "
                  f"{r['name']:<22.22}  "
                  f"{r['dt_min']:>5.0f}m  {r['dist_after']:>6.1f}km  "
                  f"{id_str:>10}  {gap:>7}")
    else:
        print("  None found.\n")

    print(f"\nSummary: {len(missed_matches)} missed match(es), "
          f"{len(reappearances)} reappearance candidate(s)")


if __name__ == "__main__":
    main()
