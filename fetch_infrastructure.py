#!/usr/bin/env python3
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
"""
fetch_infrastructure.py — Download offshore infrastructure GeoJSON for the AOI.

Queries OpenStreetMap via the Overpass API for:
  - Offshore wind farms (polygons + individual turbines grouped by farm)
  - Oil / gas platforms
  - Submarine cables
  - Underwater pipelines

Saves results to infrastructure.geojson (embedded into the timeline map at
build time, no runtime API calls needed).

Usage:
    python fetch_infrastructure.py           # fetch for the project AOI
    python fetch_infrastructure.py --force   # re-fetch even if file exists
"""

import argparse
import json
import math
import time
from pathlib import Path

try:
    import requests
except ImportError:
    raise SystemExit("pip install requests")

from config import AOI_LAT_MIN, AOI_LAT_MAX, AOI_LON_MIN, AOI_LON_MAX

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OUT_PATH     = Path("infrastructure.geojson")

# Small pad around the AOI so wind farms on the edge are fully included
_PAD = 0.2

BBOX = (
    AOI_LAT_MIN - _PAD,   # south
    AOI_LON_MIN - _PAD,   # west
    AOI_LAT_MAX + _PAD,   # north
    AOI_LON_MAX + _PAD,   # east
)

# ── Overpass query ─────────────────────────────────────────────────────────────
QUERY = (
    "[out:json][timeout:90];"
    "("
    # wind farm areas
    'relation["power"="plant"]["plant:source"="wind"]({s},{w},{n},{e});'
    'way["power"="plant"]["plant:source"="wind"]({s},{w},{n},{e});'
    'relation["landuse"="wind_farm"]({s},{w},{n},{e});'
    'way["landuse"="wind_farm"]({s},{w},{n},{e});'
    # individual turbines
    'node["power"="generator"]["generator:source"="wind"]({s},{w},{n},{e});'
    # oil/gas platforms
    'node["man_made"="offshore_platform"]({s},{w},{n},{e});'
    'node["seamark:type"="platform"]({s},{w},{n},{e});'
    # submarine cables
    'way["power"="cable"]["location"="underwater"]({s},{w},{n},{e});'
    'way["power"="cable"]["submarine"="yes"]({s},{w},{n},{e});'
    'way["communication"="line"]["location"="underwater"]({s},{w},{n},{e});'
    'way["seamark:type"="cable_area"]({s},{w},{n},{e});'
    # underwater pipelines
    'way["man_made"="pipeline"]["location"="underwater"]({s},{w},{n},{e});'
    'way["man_made"="pipeline"]["submarine"="yes"]({s},{w},{n},{e});'
    ");out body;>;out skel qt;"
).format(s=BBOX[0], w=BBOX[1], n=BBOX[2], e=BBOX[3])


# ── helpers ────────────────────────────────────────────────────────────────────

def _centroid(coords: list) -> tuple:
    """Approximate centroid of a list of [lon, lat] pairs."""
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return sum(lons)/len(lons), sum(lats)/len(lats)


def _category(tags: dict) -> str:
    if tags.get("power") in ("plant", "generator") and \
       tags.get("plant:source") == "wind" or tags.get("generator:source") == "wind":
        return "wind_farm"
    if tags.get("landuse") == "wind_farm":
        return "wind_farm"
    if tags.get("man_made") == "offshore_platform" or \
       tags.get("seamark:type") == "platform":
        return "platform"
    if tags.get("power") == "cable" or \
       (tags.get("communication") == "line" and
        tags.get("location") in ("underwater", None)):
        return "cable"
    if tags.get("seamark:type") == "cable_area":
        return "cable"
    if tags.get("man_made") == "pipeline":
        return "pipeline"
    if tags.get("industrial") == "oil":
        return "oil_field"
    return "other"


def osm_to_geojson(elements: list) -> dict:
    """
    Convert Overpass JSON elements to a GeoJSON FeatureCollection.
    Nodes → Points, Ways → LineStrings or Polygons, Relations → MultiPolygons.
    """
    # Build a node lookup for way geometry resolution
    node_map = {e["id"]: e for e in elements if e["type"] == "node"}

    features = []

    for el in elements:
        tags  = el.get("tags", {})
        if not tags:
            continue   # bare geometry nodes used only for way refs

        cat   = _category(tags)
        name  = tags.get("name") or tags.get("seamark:name") or \
                tags.get("operator") or cat.replace("_", " ").title()
        props = {
            "category": cat,
            "name":     name,
            "operator": tags.get("operator", ""),
            "osm_id":   el["id"],
            "osm_type": el["type"],
        }

        if el["type"] == "node":
            geom = {"type": "Point",
                    "coordinates": [el["lon"], el["lat"]]}

        elif el["type"] == "way":
            node_ids = el.get("nodes", [])
            coords   = [[node_map[nid]["lon"], node_map[nid]["lat"]]
                        for nid in node_ids if nid in node_map]
            if not coords:
                continue
            # closed way → Polygon, open way → LineString
            if len(coords) > 2 and coords[0] == coords[-1]:
                geom = {"type": "Polygon", "coordinates": [coords]}
            else:
                geom = {"type": "LineString", "coordinates": coords}

        elif el["type"] == "relation":
            # Build outer ring(s) from member ways
            outer_ways = [m for m in el.get("members", [])
                          if m["type"] == "way" and m.get("role") in ("outer", "")]
            rings = []
            for m in outer_ways:
                way = next((e for e in elements
                            if e["type"] == "way" and e["id"] == m["ref"]), None)
                if not way:
                    continue
                coords = [[node_map[nid]["lon"], node_map[nid]["lat"]]
                          for nid in way.get("nodes", []) if nid in node_map]
                if coords:
                    rings.append(coords)
            if not rings:
                continue
            if len(rings) == 1:
                geom = {"type": "Polygon", "coordinates": rings}
            else:
                geom = {"type": "MultiPolygon",
                        "coordinates": [[r] for r in rings]}
        else:
            continue

        features.append({"type": "Feature", "geometry": geom,
                          "properties": props})

    return {"type": "FeatureCollection", "features": features}


# ── main ───────────────────────────────────────────────────────────────────────

def fetch(force: bool = False) -> dict:
    if OUT_PATH.exists() and not force:
        print(f"Using cached {OUT_PATH}  (use --force to re-fetch)")
        return json.loads(OUT_PATH.read_text())

    print(f"Querying Overpass API for AOI {BBOX} ...")
    for attempt in range(3):
        try:
            resp = requests.post(
                OVERPASS_URL,
                data={"data": QUERY},
                headers={"User-Agent": "NOCTURNAL/1.0 (maritime surveillance research)"},
                timeout=120,
            )
            resp.raise_for_status()
            break
        except Exception as exc:
            print(f"  Attempt {attempt+1} failed: {exc}")
            if attempt < 2:
                time.sleep(10)
    else:
        raise SystemExit("Overpass API unavailable after 3 attempts.")

    elements = resp.json().get("elements", [])
    print(f"  Received {len(elements)} OSM elements")

    gj = osm_to_geojson(elements)

    # Summary by category
    cats = {}
    for f in gj["features"]:
        c = f["properties"]["category"]
        cats[c] = cats.get(c, 0) + 1
    print("\nFeature summary:")
    for cat, count in sorted(cats.items()):
        print(f"  {cat:20s}: {count}")
    print(f"  {'TOTAL':20s}: {len(gj['features'])}")

    OUT_PATH.write_text(json.dumps(gj, separators=(',', ':')))
    print(f"\nSaved → {OUT_PATH}  ({OUT_PATH.stat().st_size >> 10} KB)")
    return gj


def main():
    ap = argparse.ArgumentParser(description="Download offshore infrastructure GeoJSON for the AOI")
    ap.add_argument("--force", action="store_true",
                    help="Re-fetch even if infrastructure.geojson already exists")
    args = ap.parse_args()
    fetch(args.force)


if __name__ == "__main__":
    main()
