#!/usr/bin/env python3
"""
jurisdiction_lookup.py — Point-in-polygon maritime jurisdiction query.

Loads the pre-fetched boundary GeoJSON files from jurisdiction_data/ and
returns the jurisdiction details for any (lat, lon) in the Channel AOI.

Priority (most specific first):
    territorial_sea  → contiguous_zone  → eez  → high_seas

Usage as a module:
    from jurisdiction_lookup import get_jurisdiction
    info = get_jurisdiction(50.1, -1.3)

Usage as a script (quick test):
    python jurisdiction_lookup.py 50.1 -1.3
"""

from __future__ import annotations
import json
from pathlib import Path
from functools import lru_cache
from typing import Optional

try:
    from shapely.geometry import shape, Point
except ImportError:
    raise SystemExit("pip install shapely")

DATA_DIR = Path(__file__).parent / "jurisdiction_data"

# Longitude split between CROSS Jobourg (west) and CROSS Gris-Nez (east)
_CROSS_BOUNDARY = -0.5

def _dynamic_authority(sovereign: str, zone_type: str, lon: float) -> tuple[str, str, str]:
    """Return (name, detail, contact) based on actual query longitude."""
    s = sovereign.lower()
    if zone_type == "high_seas":
        return ("Flag state jurisdiction",
                "IMO conventions apply. Contact flag state maritime authority.",
                "VHF Ch 16")
    if "france" in s:
        if lon < _CROSS_BOUNDARY:
            return ("CROSS Jobourg (Western Channel)",
                    "French Maritime Prefecture of the Channel and North Sea",
                    "VHF Ch 16  /  +33 2 33 52 72 13")
        else:
            return ("CROSS Gris-Nez (Dover Strait & Eastern Channel)",
                    "French Maritime Prefecture of the Channel and North Sea",
                    "VHF Ch 16  /  +33 3 21 87 21 87")
    if "united kingdom" in s or "britain" in s:
        return ("MRCC Dover (UK Maritime & Coastguard Agency)",
                "His Majesty's Coastguard — Channel Watch",
                "VHF Ch 16  /  +44 1304 210008")
    if "belgium" in s:
        return ("MRCC Oostende", "Belgian Coast Guard", "VHF Ch 16  /  +32 59 70 10 00")
    if "netherlands" in s:
        return ("MRCC Den Helder", "Netherlands Coastguard", "VHF Ch 16  /  +31 223 542300")
    if "ireland" in s:
        return ("MRCC Valentia", "Irish Coast Guard", "VHF Ch 16  /  +353 66 947 6109")
    return (f"{sovereign} maritime authority", f"Contact {sovereign} coast guard", "VHF Ch 16")

# Layer priority: check territorial sea first, then contiguous, then EEZ
_LAYERS = [
    ("territorial_sea",  DATA_DIR / "territorial_sea.geojson"),
    ("contiguous_zone",  DATA_DIR / "contiguous_zone.geojson"),
    ("eez",              DATA_DIR / "eez.geojson"),
]

_HIGH_SEAS = {
    "zone_type":   "high_seas",
    "zone_label":  "High Seas",
    "sovereign":   "—",
    "territory":   "International Waters",
    "zone_legal":  (
        "No national jurisdiction. Flag state has exclusive jurisdiction. "
        "Boarding only permitted under IMO/UNCLOS exceptions "
        "(piracy, slave trade, stateless vessel, hot pursuit from territorial sea)."
    ),
    "colour":      "#3498db",
    "authority":   "Flag state jurisdiction",
    "auth_detail": "IMO conventions apply. Contact flag state maritime authority.",
    "contact":     "VHF Ch 16",
}


@lru_cache(maxsize=None)
def _load_layer(path: Path) -> list:
    """Load and cache a GeoJSON layer as a list of (shapely_geom, props) tuples."""
    if not path.exists():
        return []
    gj   = json.loads(path.read_text(encoding="utf-8"))
    out  = []
    for f in gj.get("features", []):
        try:
            geom = shape(f["geometry"])
            out.append((geom, f.get("properties", {})))
        except Exception:
            pass
    return out


def _load_all():
    return [(zone_type, _load_layer(path)) for zone_type, path in _LAYERS]


def get_jurisdiction(lat: float, lon: float) -> dict:
    """
    Return the maritime jurisdiction at (lat, lon).

    Returns a dict with keys:
        zone_type, zone_label, sovereign, territory, zone_legal,
        colour, authority, auth_detail, contact
    """
    pt = Point(lon, lat)   # shapely uses (x=lon, y=lat)

    for zone_type, features in _load_all():
        for geom, props in features:
            try:
                if geom.contains(pt):
                    sov = props.get("sovereign", "Unknown")
                    # Compute authority from actual query longitude (not polygon centroid)
                    auth_name, auth_detail, contact = _dynamic_authority(sov, zone_type, lon)
                    return {
                        "zone_type":   zone_type,
                        "zone_label":  props.get("zone_label", zone_type),
                        "sovereign":   sov,
                        "territory":   props.get("territory", "Unknown"),
                        "zone_legal":  props.get("zone_legal", ""),
                        "colour":      props.get("colour", "#888"),
                        "authority":   auth_name,
                        "auth_detail": auth_detail,
                        "contact":     contact,
                    }
            except Exception:
                pass

    return dict(_HIGH_SEAS)


def preload():
    """Warm up the geometry cache — call once at server startup."""
    _load_all()


# ── CLI test ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python jurisdiction_lookup.py <lat> <lon>")
        print("Example: python jurisdiction_lookup.py 50.1 -1.3")
        sys.exit(1)
    lat = float(sys.argv[1])
    lon = float(sys.argv[2])
    info = get_jurisdiction(lat, lon)
    print(f"\nJurisdiction at {lat:.4f}N, {lon:.4f}E:")
    print(f"  Zone:      {info['zone_label']}")
    print(f"  Country:   {info['sovereign']}")
    print(f"  Territory: {info['territory']}")
    print(f"  Authority: {info['authority']}")
    print(f"  Contact:   {info['contact']}")
    print(f"  Legal:     {info['zone_legal'][:80]}...")
