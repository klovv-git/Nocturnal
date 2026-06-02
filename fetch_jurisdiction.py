#!/usr/bin/env python3
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
"""
fetch_jurisdiction.py — Download and prepare maritime jurisdiction boundary GeoJSON.

Downloads from Marine Regions VLIZ WFS:
  eez_12nm   → territorial_sea.geojson    (0–12 nm)
  eez_24nm   → contiguous_zone.geojson    (0–24 nm)
  eez        → eez.geojson                (0–200 nm, clipped to AOI)

Geometries are simplified to ~0.005° tolerance for web display.
Also writes metadata.json mapping sovereign → zone → authority.

Usage:
    python fetch_jurisdiction.py           # fetch for project AOI
    python fetch_jurisdiction.py --force   # re-fetch even if files exist
"""

import argparse
import json
from pathlib import Path

try:
    import requests
except ImportError:
    raise SystemExit("pip install requests")

try:
    from shapely.geometry import shape, mapping
    from shapely.ops import unary_union
except ImportError:
    raise SystemExit("pip install shapely")

from config import AOI_LAT_MIN, AOI_LAT_MAX, AOI_LON_MIN, AOI_LON_MAX

WFS_URL  = "https://geo.vliz.be/geoserver/MarineRegions/wfs"
OUT_DIR  = Path("jurisdiction_data")
PAD      = 0.1   # slight bbox pad so coastal edges aren't clipped

BBOX = f"{AOI_LON_MIN - PAD},{AOI_LAT_MIN - PAD},{AOI_LON_MAX + PAD},{AOI_LAT_MAX + PAD}"

# ── Authority metadata ─────────────────────────────────────────────────────────
# keyed by (sovereign1.lower(), zone_type)
# zone_type in: territorial_sea | contiguous_zone | eez | high_seas

ZONE_LABELS = {
    "territorial_sea":  "Territorial Sea (0–12 nm)",
    "contiguous_zone":  "Contiguous Zone (12–24 nm)",
    "eez":              "Exclusive Economic Zone (24–200 nm)",
    "high_seas":        "High Seas",
}

ZONE_LEGAL = {
    "territorial_sea": (
        "Full sovereignty. Foreign vessels have right of innocent passage. "
        "Coastal state may board and inspect any vessel."
    ),
    "contiguous_zone": (
        "Coastal state may enforce customs, fiscal, immigration and sanitary laws. "
        "No general right of hot pursuit begins here. Limited boarding authority."
    ),
    "eez": (
        "Coastal state has sovereign rights over resources and installations. "
        "Flag state jurisdiction applies to navigation. "
        "Boarding requires flag state consent except under UNCLOS exceptions."
    ),
    "high_seas": (
        "No national jurisdiction. Flag state has exclusive jurisdiction. "
        "Boarding only permitted under IMO/UNCLOS exceptions "
        "(piracy, slave trade, stateless vessel, hot pursuit)."
    ),
}

# Longitude boundary between CROSS Jobourg (west) and CROSS Gris-Nez (east)
CROSS_BOUNDARY_LON = -0.5   # approx. Cap de la Hague / mid-Channel split

def _authority(sovereign: str, zone: str, lon: float) -> dict:
    """Return authority dict for a given sovereign, zone type, and longitude."""
    s = sovereign.lower()

    if zone == "high_seas":
        return {
            "name":    "Flag state jurisdiction",
            "detail":  "IMO conventions apply. Contact flag state maritime authority.",
            "contact": "VHF Ch 16",
        }

    if "france" in s:
        if lon < CROSS_BOUNDARY_LON:
            return {
                "name":    "CROSS Jobourg (Western Channel)",
                "detail":  "French Maritime Prefecture of the Channel and North Sea",
                "contact": "VHF Ch 16  /  +33 2 33 52 72 13",
            }
        else:
            return {
                "name":    "CROSS Gris-Nez (Dover Strait & Eastern Channel)",
                "detail":  "French Maritime Prefecture of the Channel and North Sea",
                "contact": "VHF Ch 16  /  +33 3 21 87 21 87",
            }

    if "united kingdom" in s or "britain" in s:
        return {
            "name":    "MRCC Dover (UK Maritime & Coastguard Agency)",
            "detail":  "His Majesty's Coastguard — Channel Watch",
            "contact": "VHF Ch 16  /  +44 1304 210008",
        }

    if "belgium" in s:
        return {
            "name":    "MRCC Oostende",
            "detail":  "Belgian Coast Guard",
            "contact": "VHF Ch 16  /  +32 59 70 10 00",
        }

    if "netherlands" in s:
        return {
            "name":    "MRCC Den Helder",
            "detail":  "Netherlands Coastguard",
            "contact": "VHF Ch 16  /  +31 223 542300",
        }

    if "ireland" in s:
        return {
            "name":    "MRCC Valentia",
            "detail":  "Irish Coast Guard",
            "contact": "VHF Ch 16  /  +353 66 947 6109",
        }

    # fallback
    return {
        "name":    f"{sovereign} maritime authority",
        "detail":  f"Contact {sovereign} coast guard",
        "contact": "VHF Ch 16",
    }


ZONE_COLOURS = {
    "territorial_sea": "#e74c3c",   # red
    "contiguous_zone": "#e67e22",   # orange
    "eez":             "#f1c40f",   # yellow
}


# ── WFS fetch ──────────────────────────────────────────────────────────────────

def fetch_layer(layer_name: str, max_features: int = 100) -> dict:
    params = {
        "service":       "WFS",
        "version":       "1.0.0",
        "request":       "GetFeature",
        "typeName":      f"MarineRegions:{layer_name}",
        "outputFormat":  "application/json",
        "BBOX":          BBOX,
        "maxFeatures":   max_features,
    }
    resp = requests.get(WFS_URL, params=params, timeout=60,
                        headers={"User-Agent": "NOCTURNAL/1.0"})
    resp.raise_for_status()
    return resp.json()


def simplify_feature(feature: dict, tolerance: float = 0.005) -> dict:
    """Simplify geometry using shapely and return updated feature."""
    geom = shape(feature["geometry"])
    simplified = geom.simplify(tolerance, preserve_topology=True)
    feature = dict(feature)
    feature["geometry"] = mapping(simplified)
    return feature


def process_layer(layer_name: str, zone_type: str, out_path: Path) -> list:
    """Fetch, simplify, annotate and save one layer. Returns feature list."""
    print(f"  Fetching {layer_name} ...", end=" ", flush=True)
    gj = fetch_layer(layer_name)
    feats = gj.get("features", [])
    print(f"{len(feats)} features")

    annotated = []
    for f in feats:
        props    = f.get("properties", {})
        sov      = props.get("sovereign1") or props.get("SOVEREIGN1") or "Unknown"
        ter      = props.get("territory1") or props.get("TERRITORY1") or sov
        geoname  = props.get("geoname")    or props.get("GEONAME")    or ter

        # approximate centroid longitude for authority split
        try:
            geom = shape(f["geometry"])
            cx = float(geom.centroid.x)
        except Exception:
            cx = 0.0

        auth = _authority(sov, zone_type, cx)

        f = simplify_feature(f)
        f["properties"] = {
            "zone_type":   zone_type,
            "sovereign":   sov,
            "territory":   ter,
            "geoname":     geoname,
            "zone_label":  ZONE_LABELS[zone_type],
            "zone_legal":  ZONE_LEGAL[zone_type],
            "colour":      ZONE_COLOURS.get(zone_type, "#aaa"),
            "authority":   auth["name"],
            "auth_detail": auth["detail"],
            "contact":     auth["contact"],
        }
        annotated.append(f)

    out_path.write_text(json.dumps(
        {"type": "FeatureCollection", "features": annotated},
        separators=(",", ":"),
    ))
    kb = out_path.stat().st_size >> 10
    print(f"    Saved {out_path.name}  ({kb} KB)")
    return annotated


# ── main ───────────────────────────────────────────────────────────────────────

def fetch(force: bool = False):
    OUT_DIR.mkdir(exist_ok=True)

    files = {
        "territorial_sea": OUT_DIR / "territorial_sea.geojson",
        "contiguous_zone": OUT_DIR / "contiguous_zone.geojson",
        "eez":             OUT_DIR / "eez.geojson",
    }

    if all(p.exists() for p in files.values()) and not force:
        print("All jurisdiction files already present. Use --force to re-fetch.")
        return

    print(f"Fetching jurisdiction boundaries for bbox {BBOX} ...\n")

    process_layer("eez_12nm", "territorial_sea", files["territorial_sea"])
    process_layer("eez_24nm", "contiguous_zone", files["contiguous_zone"])
    process_layer("eez",      "eez",             files["eez"])

    # Write a summary metadata file for quick reference
    meta = {
        "bbox":    BBOX,
        "zones":   ZONE_LABELS,
        "legal":   ZONE_LEGAL,
        "colours": ZONE_COLOURS,
    }
    (OUT_DIR / "metadata.json").write_text(json.dumps(meta, indent=2))
    print("\nDone. Files in jurisdiction_data/")


def main():
    ap = argparse.ArgumentParser(
        description="Download maritime jurisdiction boundaries for the project AOI"
    )
    ap.add_argument("--force", action="store_true",
                    help="Re-fetch even if files already exist")
    args = ap.parse_args()
    fetch(args.force)


if __name__ == "__main__":
    main()
