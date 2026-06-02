#!/usr/bin/env python3
"""
extract_chips.py — crop a small SAR image patch around each dark vessel
detection and save it as a PNG so you can actually see what the radar
saw at that location.

Usage:
    python extract_chips.py --scene <SAFE folder name> --safe <path to .SAFE>
"""

import argparse
import os
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

try:
    import rasterio
    from rasterio.windows import Window
except ImportError:
    raise SystemExit("pip install rasterio")

from sar_preprocess import locate_measurement, locate_calibration, \
    _parse_calibration_lut, _interp_lut, _db_to_u8
from sar_preprocess import DB_MIN, DB_MAX

CHIP_SIZE  = 128   # pixels around the detection centre
OUT_DIR    = Path("dark_chips")
DEFAULT_DB = Path("ais_memory.db")


def extract_chip(safe: Path, pol: str,
                 pixel_x: float, pixel_y: float,
                 chip: int = CHIP_SIZE) -> np.ndarray:
    """Return a uint8 numpy array (chip x chip) centred on the detection."""
    tif  = locate_measurement(safe, pol)
    half = chip // 2
    col  = max(0, int(pixel_x) - half)
    row  = max(0, int(pixel_y) - half)

    with rasterio.open(tif) as src:
        col = min(col, src.width  - chip)
        row = min(row, src.height - chip)
        win = Window(col, row, chip, chip)
        dn  = src.read(1, window=win).astype(np.float32)

    lines, pixels, sigma = _parse_calibration_lut(
        locate_calibration(safe, pol))
    lut = _interp_lut(dn.shape, lines, pixels, sigma,
                      row_off=row, col_off=col)
    sigma0_lin = (dn ** 2) / np.maximum(lut ** 2, 1e-6)
    sigma0_db  = 10.0 * np.log10(np.maximum(sigma0_lin, 1e-10))
    db_clip    = np.clip(sigma0_db, DB_MIN, DB_MAX)
    return _db_to_u8(db_clip)


def annotate(arr: np.ndarray, label: str) -> Image.Image:
    """Draw a crosshair and label on the chip."""
    img  = Image.fromarray(arr, mode="L").convert("RGB")
    draw = ImageDraw.Draw(img)
    cx, cy = arr.shape[1] // 2, arr.shape[0] // 2
    r = 12
    draw.ellipse([cx-r, cy-r, cx+r, cy+r], outline="red", width=2)
    draw.line([cx-r-4, cy, cx+r+4, cy], fill="red", width=1)
    draw.line([cx, cy-r-4, cx, cy+r+4], fill="red", width=1)
    draw.text((4, 4), label, fill="red")
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", required=True)
    ap.add_argument("--safe",  required=True, type=Path)
    ap.add_argument("--db",    default=str(DEFAULT_DB))
    ap.add_argument("--pol",   default="vv")
    ap.add_argument("--chip",  type=int, default=CHIP_SIZE)
    ap.add_argument("--limit", type=int, default=500,
                    help="max dark detections to extract (default: 500)")
    args = ap.parse_args()

    # derive date from scene name, e.g. S1D_IW_..._20260512T061448_... -> 20260512
    import re
    date_match = re.search(r'_(\d{8})T', args.scene)
    date_str = date_match.group(1) if date_match else "unknown"
    out_dir = Path(f"dark_chips_{date_str}")
    out_dir.mkdir(exist_ok=True)
    conn = sqlite3.connect(args.db)

    dets = conn.execute(
        """SELECT id, pixel_x, pixel_y, lat, lon, confidence
           FROM detections
           WHERE scene_name = ? AND dark = 1
             AND lat IS NOT NULL
             AND (score IS NULL OR score >= 0.2)
           ORDER BY confidence DESC
           LIMIT ?""",
        (args.scene, args.limit)
    ).fetchall()

    if not dets:
        print("No dark detections found for this scene.")
        return

    print(f"Extracting {len(dets)} dark vessel chips -> {out_dir}/")
    for det_id, px, py, lat, lon, conf in dets:
        try:
            px = float(px); py = float(py)
        except (TypeError, ValueError):
            import struct
            def _b2f(v):
                b = bytes(v)
                return struct.unpack('<f', b)[0] if len(b)==4 \
                  else struct.unpack('<d', b)[0]
            px, py = _b2f(px), _b2f(py)

        fname = out_dir / f"dark_{det_id:04d}_{lat:.4f}N_{lon:.4f}E.png"
        if fname.exists():
            continue   # already generated, skip
        arr = extract_chip(args.safe, args.pol, px, py, args.chip)
        img = annotate(arr, f"DARK id={det_id} conf={conf:.2f}")
        img.save(fname)
        print(f"  {fname.name}  ({lat:.4f}N, {lon:.4f}E)  conf={conf:.2f}")

    print(f"\nDone. Open the '{out_dir}' folder to see the radar chips.")


if __name__ == "__main__":
    main()
