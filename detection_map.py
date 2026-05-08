#!/usr/bin/env python3
"""
detection_map.py — generate an interactive HTML map showing matched and
dark vessel detections for a scene.

Green markers = vessels detected by radar AND broadcasting AIS.
Red markers   = vessels detected by radar with NO AIS signal (dark).

Usage:
    python detection_map.py --scene <SAFE folder name>
    Then open detection_map.html in a browser.
"""

import argparse
import sqlite3
from pathlib import Path

DEFAULT_DB = Path("ais_memory.db")
OUT_FILE   = Path("detection_map.html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", required=True)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    dets = conn.execute(
        """SELECT id, lat, lon, confidence, dark, matched_mmsi, match_dist_m
           FROM detections
           WHERE scene_name = ? AND lat IS NOT NULL AND dark IS NOT NULL""",
        (args.scene,)
    ).fetchall()

    if not dets:
        print("No geocoded detections found for this scene.")
        return

    dark    = [d for d in dets if d[4] == 1]
    matched = [d for d in dets if d[4] == 0]

    # Centre map on mean position
    all_lats = [d[1] for d in dets]
    all_lons = [d[2] for d in dets]
    clat = sum(all_lats) / len(all_lats)
    clon = sum(all_lons) / len(all_lons)

    def marker_js(d, color):
        det_id, lat, lon, conf, dark_flag, mmsi, dist = d
        if dark_flag:
            popup = (f"<b>DARK VESSEL CANDIDATE</b><br>"
                     f"Detection ID: {det_id}<br>"
                     f"Position: {lat:.5f}N, {lon:.5f}E<br>"
                     f"Model confidence: {conf:.2f}<br>"
                     f"No AIS signal within 1km / 30min")
        else:
            dist_str = f"{dist:.0f}m" if dist else "?"
            popup = (f"<b>MATCHED VESSEL</b><br>"
                     f"Detection ID: {det_id}<br>"
                     f"Position: {lat:.5f}N, {lon:.5f}E<br>"
                     f"Model confidence: {conf:.2f}<br>"
                     f"MMSI: {mmsi}<br>"
                     f"AIS distance: {dist_str}")
        return (f'L.circleMarker([{lat}, {lon}], '
                f'{{radius: 8, color: "{color}", fillColor: "{color}", '
                f'fillOpacity: 0.8, weight: 2}}).addTo(map)'
                f'.bindPopup("{popup}");')

    markers = []
    for d in matched:
        markers.append(marker_js(d, "#2ecc71"))   # green
    for d in dark:
        markers.append(marker_js(d, "#e74c3c"))   # red

    markers_js = "\n        ".join(markers)

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>NOCTURNAL — Detection Map</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; }}
    #map {{ height: 100vh; }}
    #legend {{
      position: absolute; bottom: 30px; right: 10px; z-index: 1000;
      background: white; padding: 12px 16px; border-radius: 6px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.3); font-size: 13px;
    }}
    .dot {{ display: inline-block; width: 12px; height: 12px;
            border-radius: 50%; margin-right: 6px; }}
    #title {{
      position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
      z-index: 1000; background: white; padding: 8px 20px;
      border-radius: 6px; box-shadow: 0 2px 8px rgba(0,0,0,0.3);
      font-size: 15px; font-weight: bold;
    }}
  </style>
</head>
<body>
  <div id="title">NOCTURNAL — {args.scene[:30]}...</div>
  <div id="map"></div>
  <div id="legend">
    <div><span class="dot" style="background:#2ecc71"></span>
         Matched vessel ({len(matched)})</div>
    <div><span class="dot" style="background:#e74c3c"></span>
         Dark vessel candidate ({len(dark)})</div>
  </div>
  <script>
    var map = L.map('map').setView([{clat}, {clon}], 10);
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      attribution: '© OpenStreetMap contributors'
    }}).addTo(map);
    {markers_js}
  </script>
</body>
</html>"""

    OUT_FILE.write_text(html, encoding="utf-8")
    print(f"Map saved to {OUT_FILE.resolve()}")
    print(f"  {len(matched)} matched vessels (green)")
    print(f"  {len(dark)} dark vessel candidates (red)")
    print("Open detection_map.html in your browser.")


if __name__ == "__main__":
    main()
