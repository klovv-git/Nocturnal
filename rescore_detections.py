#!/usr/bin/env python3
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
"""
rescore_detections.py — Recompute the quality score for all already-geocoded
detections without re-running YOLO or geocoding.

Run this once after upgrading geocode_match.py with the new scoring logic,
or whenever the scoring parameters change.

Usage:
    python rescore_detections.py              # score all detections
    python rescore_detections.py --scene S1A_IW_GRDH_...SAFE
    python rescore_detections.py --dry-run    # show stats, don't write
"""

import argparse
import os
import sqlite3
from pathlib import Path

from geocode_match import detection_score
from config import DB_PATH


def rescore(db_path: Path, scene_filter: str = None, dry_run: bool = False):
    conn = sqlite3.connect(os.fspath(db_path), timeout=60)

    where = "lat IS NOT NULL"
    params = []
    if scene_filter:
        where += " AND scene_name = ?"
        params.append(scene_filter)

    rows = conn.execute(
        f"""SELECT id, confidence, width_px, height_px, lat, lon, dark, score
            FROM detections WHERE {where}""",
        params
    ).fetchall()

    print(f"Rescoring {len(rows)} geocoded detections ...")

    updates = []
    score_dist = {"0.0": 0, "0.0-0.1": 0, "0.1-0.2": 0,
                  "0.2-0.3": 0, "0.3-0.5": 0, "0.5+": 0}

    for det_id, conf, w_px, h_px, lat, lon, dark, old_score in rows:
        new_score = detection_score(
            float(conf or 0), float(w_px or 0), float(h_px or 0),
            float(lat), float(lon)
        )
        updates.append((new_score, det_id))

        if new_score == 0.0:       score_dist["0.0"] += 1
        elif new_score < 0.1:      score_dist["0.0-0.1"] += 1
        elif new_score < 0.2:      score_dist["0.1-0.2"] += 1
        elif new_score < 0.3:      score_dist["0.2-0.3"] += 1
        elif new_score < 0.5:      score_dist["0.3-0.5"] += 1
        else:                      score_dist["0.5+"] += 1

    # Stats before writing
    dark_rows   = [r for r in rows if r[6] == 1]
    dark_updates = [(detection_score(float(r[1] or 0), float(r[2] or 0),
                                     float(r[3] or 0), float(r[4]), float(r[5])), r[0])
                    for r in dark_rows]
    n_dark_pass = sum(1 for s, _ in dark_updates if s >= 0.2)
    n_dark_fail = sum(1 for s, _ in dark_updates if s < 0.2)

    print(f"\nScore distribution (all geocoded):")
    for band, count in score_dist.items():
        bar = "█" * (count * 40 // max(len(rows), 1))
        print(f"  {band:>8}  {count:5d}  {bar}")

    print(f"\nDark candidates (dark=1): {len(dark_rows)}")
    print(f"  Score ≥ 0.2 (shown in map):  {n_dark_pass}")
    print(f"  Score < 0.2 (filtered out):  {n_dark_fail}")

    if dry_run:
        print("\n--dry-run: no changes written.")
        conn.close()
        return

    conn.executemany("UPDATE detections SET score = ? WHERE id = ?", updates)
    conn.commit()
    conn.close()
    print(f"\nDone — wrote scores for {len(updates)} detections.")


def main():
    ap = argparse.ArgumentParser(description="Recompute quality scores for all geocoded detections")
    ap.add_argument("--scene",   default=None, help="Limit to one scene name")
    ap.add_argument("--db",      type=Path, default=DB_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    rescore(args.db, args.scene, args.dry_run)


if __name__ == "__main__":
    main()
