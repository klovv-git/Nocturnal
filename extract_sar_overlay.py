#!/usr/bin/env python3
"""
extract_sar_overlay.py — warp the Sentinel-1 SAR measurement image to WGS84
and export as a PNG + bounds JSON that ais_overlay_map.py can load as a layer.

The Sentinel-1 GRD TIFF is in radar/slant geometry. This script uses the
GCPs embedded in the TIFF to reproject it to lat/lon, downsamples to a
web-friendly size, applies sigma0 dB normalization, and saves:
  sar_overlay_YYYYMMDD.png   — grayscale image, WGS84
  sar_overlay_YYYYMMDD.json  — bounds {lat_min, lat_max, lon_min, lon_max}

Usage:
    python extract_sar_overlay.py --scene <scene name> --safe <path to .SAFE>

Then run:
    python ais_overlay_map.py --scene <scene name> --sar-overlay sar_overlay_YYYYMMDD.png
"""

import argparse
import json
import re
import numpy as np
from pathlib import Path
from PIL import Image

try:
    import rasterio
    from rasterio.warp import reproject, calculate_default_transform, Resampling
    from rasterio.crs import CRS
    from rasterio.transform import Affine
except ImportError:
    raise SystemExit("pip install rasterio")

from sar_preprocess import locate_measurement

MAX_WIDTH = 2000   # default output width in pixels
DB_MIN    = -25.0  # dB clip low  (sea clutter floor)
DB_MAX    =   5.0  # dB clip high (bright ship return)


def dn_to_u8(dn: np.ndarray) -> np.ndarray:
    """Convert raw DN array to sigma0 dB, clip, and normalise to uint8."""
    # sigma0 ∝ DN² (calibration constant omitted — fine for display)
    sigma0_db = 10.0 * np.log10(np.maximum(dn.astype(np.float32) ** 2, 1e-10))
    clipped   = np.clip(sigma0_db, DB_MIN, DB_MAX)
    normed    = (clipped - DB_MIN) / (DB_MAX - DB_MIN)
    return (normed * 255).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene",  required=True)
    ap.add_argument("--safe",   required=True, type=Path)
    ap.add_argument("--pol",    default="vv",
                    help="Polarisation to use (vv or vh)")
    ap.add_argument("--width",  type=int, default=MAX_WIDTH,
                    help="Output image width in pixels (height scaled to match)")
    args = ap.parse_args()

    date_match = re.search(r'_(\d{8})T', args.scene)
    date_str   = date_match.group(1) if date_match else "unknown"
    out_png    = Path(f"sar_overlay_{date_str}.png")
    out_json   = Path(f"sar_overlay_{date_str}.json")

    tif = locate_measurement(args.safe, args.pol)
    print(f"SAR image : {tif.name}")

    with rasterio.open(tif) as src:
        gcps, gcp_crs = src.gcps
        if not gcps:
            raise SystemExit(
                "No GCPs found in the TIFF.\n"
                "Make sure you are pointing at the measurement .tiff inside the .SAFE folder."
            )

        dst_crs = CRS.from_epsg(4326)

        # calculate the natural output transform at full resolution
        transform_full, w_full, h_full = calculate_default_transform(
            gcp_crs, dst_crs, src.width, src.height, gcps=gcps
        )

        # scale to target width
        scale = min(1.0, args.width / w_full)
        out_w = max(1, int(w_full * scale))
        out_h = max(1, int(h_full * scale))

        # build scaled affine transform
        dst_transform = Affine(
            transform_full.a / scale, transform_full.b, transform_full.c,
            transform_full.d, transform_full.e / scale, transform_full.f,
        )

        print(f"Reprojecting to WGS84 at {out_w} × {out_h} px ...")

        dn = np.zeros((out_h, out_w), dtype=np.float32)
        reproject(
            source       = rasterio.band(src, 1),
            destination  = dn,
            gcps         = gcps,
            src_crs      = gcp_crs,
            dst_crs      = dst_crs,
            dst_transform= dst_transform,
            resampling   = Resampling.bilinear,
        )

    # geographic bounds from the scaled transform
    lon_min = dst_transform.c
    lat_max = dst_transform.f
    lon_max = lon_min + dst_transform.a * out_w
    lat_min = lat_max + dst_transform.e * out_h   # e is negative

    print(f"Bounds    : {lat_min:.4f}°N – {lat_max:.4f}°N, "
          f"{lon_min:.4f}°E – {lon_max:.4f}°E")

    # normalise and save
    print("Normalising ...")
    u8  = dn_to_u8(dn)
    img = Image.fromarray(u8, mode="L")
    img.save(out_png, optimize=True)
    print(f"Saved     : {out_png}  ({out_png.stat().st_size // 1024} KB)")

    bounds = {
        "lat_min": round(lat_min, 6),
        "lat_max": round(lat_max, 6),
        "lon_min": round(lon_min, 6),
        "lon_max": round(lon_max, 6),
    }
    out_json.write_text(json.dumps(bounds, indent=2))
    print(f"Saved     : {out_json}")
    print(f"\nNext step:")
    print(f'  python ais_overlay_map.py --scene "{args.scene}" '
          f'--sar-overlay {out_png}')


if __name__ == "__main__":
    main()
