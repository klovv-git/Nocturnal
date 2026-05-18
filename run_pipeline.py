#!/usr/bin/env python3
"""
run_pipeline.py — run the full NOCTURNAL pipeline on one or more .SAFE scenes.

Phases run automatically for each scene:
  1. extract_sar_overlay  — warp SAR image to WGS84 PNG
  2. yolo_infer_sar       — detect ships with YOLOv8
  3. geocode_match        — geocode detections + AIS matching

Usage:
    # Single scene
    python run_pipeline.py sentinel_data/S1C_IW_GRDH_...SAFE

    # Entire pass (all slices in sentinel_data with the same orbit)
    python run_pipeline.py --orbit 007704

    # All unprocessed scenes in sentinel_data
    python run_pipeline.py --all
"""

import argparse
import subprocess
import sys
import re
from pathlib import Path

from config import SENTINEL_DATA_DIR, SAR_OVERLAYS_DIR

# ── locate YOLO weights ────────────────────────────────────────────────────────
def find_weights() -> Path:
    candidates = list(Path(".").rglob("best.pt"))
    # prefer finetune weights over base
    for c in candidates:
        if "finetune" in str(c) or "nocturnal" in str(c):
            return c
    return candidates[0] if candidates else None


def extract_orbit(safe_name: str) -> str:
    parts = safe_name.replace(".SAFE", "").split("_")
    if len(parts) >= 7 and parts[6].isdigit() and len(parts[6]) == 6:
        return parts[6]
    return None


def run(cmd: list, label: str) -> bool:
    """Run a subprocess command, stream output, return True on success."""
    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"{'─'*60}")
    print(f"  $ {' '.join(str(c) for c in cmd)}\n")
    result = subprocess.run(cmd, executable=sys.executable)
    if result.returncode != 0:
        print(f"\n✗ FAILED (exit code {result.returncode})")
        return False
    return True


def process_scene(safe_path: Path, weights: Path) -> bool:
    """Run all pipeline phases for one .SAFE folder."""
    scene_name = safe_path.name
    print(f"\n{'═'*60}")
    print(f"  SCENE: {scene_name}")
    print(f"{'═'*60}")

    # Phase 1 — SAR overlay
    ok = run(
        [sys.executable, "extract_sar_overlay.py",
         "--scene", scene_name,
         "--safe",  str(safe_path)],
        "Phase 1 — extract SAR overlay"
    )
    if not ok:
        print("  Skipping remaining phases for this scene.")
        return False

    # Phase 2 — YOLO detection
    ok = run(
        [sys.executable, "yolo_infer_sar.py",
         str(safe_path),
         "--weights", str(weights),
         "--conf", "0.2"],
        "Phase 2 — YOLO ship detection"
    )
    if not ok:
        print("  Skipping geocode for this scene.")
        return False

    # Phase 3 — geocode + AIS match
    ok = run(
        [sys.executable, "geocode_match.py",
         "--scene", scene_name,
         "--safe",  str(safe_path)],
        "Phase 3 — geocode + AIS matching"
    )

    return ok


def main():
    ap = argparse.ArgumentParser(
        description="Run the full NOCTURNAL pipeline on one or more scenes"
    )
    ap.add_argument("scenes", nargs="*", type=Path,
                    help="Path(s) to .SAFE folder(s) to process")
    ap.add_argument("--orbit", default=None,
                    help="Process all scenes in sentinel_data with this orbit number")
    ap.add_argument("--all", action="store_true",
                    help="Process all .SAFE folders in sentinel_data")
    ap.add_argument("--weights", type=Path, default=None,
                    help="Path to YOLO weights file (auto-detected if not specified)")
    args = ap.parse_args()

    # ── resolve weights ────────────────────────────────────────────────────────
    weights = args.weights or find_weights()
    if not weights or not weights.exists():
        raise SystemExit(
            "Could not find YOLO weights (best.pt). "
            "Pass --weights path/to/best.pt explicitly."
        )
    print(f"Weights: {weights}")

    # ── resolve scene list ─────────────────────────────────────────────────────
    if args.all:
        scenes = sorted(SENTINEL_DATA_DIR.glob("*.SAFE"))
    elif args.orbit:
        scenes = sorted(
            p for p in SENTINEL_DATA_DIR.glob("*.SAFE")
            if extract_orbit(p.name) == args.orbit
        )
        if not scenes:
            raise SystemExit(f"No .SAFE folders with orbit {args.orbit} in {SENTINEL_DATA_DIR}")
    elif args.scenes:
        scenes = list(args.scenes)
    else:
        ap.print_help()
        return

    if not scenes:
        raise SystemExit(f"No .SAFE folders found.")

    print(f"\nFound {len(scenes)} scene(s) to process:")
    for s in scenes:
        print(f"  {Path(s).name}")

    # ── process each scene ─────────────────────────────────────────────────────
    ok_count  = 0
    fail_count = 0

    for safe_path in scenes:
        safe_path = Path(safe_path)
        if not safe_path.exists():
            # try prepending sentinel_data/
            alt = SENTINEL_DATA_DIR / safe_path.name
            if alt.exists():
                safe_path = alt
            else:
                print(f"\n✗ Not found: {safe_path} — skipping.")
                fail_count += 1
                continue

        success = process_scene(safe_path, weights)
        if success:
            ok_count  += 1
        else:
            fail_count += 1

    # ── summary ────────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print(f"  Done: {ok_count} succeeded, {fail_count} failed")
    print(f"{'═'*60}")

    if ok_count > 0:
        scene_args = " ".join(f'"{Path(s).name}"' for s in scenes
                              if (Path(s).exists() or
                                  (SENTINEL_DATA_DIR / Path(s).name).exists()))
        print(f"\nNext — generate the timeline map:")
        print(f"  python ais_timeline_map.py --scenes {scene_args}")


if __name__ == "__main__":
    main()
