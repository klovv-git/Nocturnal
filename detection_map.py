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
import base64
import json
import re
import sqlite3
from pathlib import Path

DEFAULT_DB = Path("ais_memory.db")
OUT_FILE   = Path("detection_map.html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", required=True)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()

    # parse scene date and time from scene name, e.g. 20260512T061448
    dt_match = re.search(r'_(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})_', args.scene)
    if dt_match:
        y, mo, d, h, mi, s = dt_match.groups()
        scene_time = f"{y}-{mo}-{d} {h}:{mi}:{s} UTC"
        date_str   = f"{y}{mo}{d}"
    else:
        scene_time = "unknown"
        date_str   = None

    # dark chips folder for this scene date
    chips_dir = Path(f"dark_chips_{date_str}") if date_str else None

    def load_chip(det_id, lat, lon):
        """Return a base64-encoded PNG string for this detection, or None."""
        if not chips_dir or not chips_dir.exists():
            return None
        fname = chips_dir / f"dark_{det_id:04d}_{lat:.4f}N_{lon:.4f}E.png"
        if not fname.exists():
            return None
        return base64.b64encode(fname.read_bytes()).decode("ascii")

    conn = sqlite3.connect(args.db)

    # check whether ais_pings has a vessel_name column
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(ais_pings)").fetchall()]
        has_vessel_name = "vessel_name" in cols
    except Exception:
        has_vessel_name = False

    def lookup_vessel_name(mmsi):
        if not mmsi or not has_vessel_name:
            return None
        row = conn.execute(
            """SELECT vessel_name FROM ais_pings
               WHERE mmsi = ? AND vessel_name IS NOT NULL AND vessel_name != ''
               LIMIT 1""",
            (str(mmsi),)
        ).fetchone()
        return row[0].strip() if row else None

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

    # centre map on mean position
    all_lats = [d[1] for d in dets]
    all_lons = [d[2] for d in dets]
    clat = sum(all_lats) / len(all_lats)
    clon = sum(all_lons) / len(all_lons)

    # build marker data as a JSON array — avoids all JS string escaping issues
    markers_data = []
    for d in dark:
        det_id, lat, lon, conf, _, mmsi, dist = d
        chip_b64 = load_chip(det_id, lat, lon)
        markers_data.append({
            "lat":      lat,
            "lon":      lon,
            "color":    "#e74c3c",
            "type":     "dark",
            "id":       det_id,
            "conf":     round(conf, 2),
            "time":     scene_time,
            "chip":     chip_b64,   # None if not found
        })
    for d in matched:
        det_id, lat, lon, conf, _, mmsi, dist = d
        vessel_name = lookup_vessel_name(mmsi)
        markers_data.append({
            "lat":         lat,
            "lon":         lon,
            "color":       "#2ecc71",
            "type":        "matched",
            "id":          det_id,
            "conf":        round(conf, 2),
            "time":        scene_time,
            "mmsi":        mmsi,
            "dist":        round(dist) if dist else None,
            "vessel_name": vessel_name,
        })

    markers_json = json.dumps(markers_data)

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
    .popup-chip {{
      display: block; width: 128px; height: 128px;
      margin-top: 8px; border: 1px solid #ccc;
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

    var markers = {markers_json};

    markers.forEach(function(m) {{
      var html;
      if (m.type === 'dark') {{
        html  = '<b>DARK VESSEL CANDIDATE</b><br>';
        html += 'Detection ID: ' + m.id + '<br>';
        html += 'Position: ' + m.lat.toFixed(5) + 'N, ' + m.lon.toFixed(5) + 'E<br>';
        html += 'Satellite pass: ' + m.time + '<br>';
        html += 'Model confidence: ' + m.conf + '<br>';
        html += 'No AIS signal within 1km / 30min';
        if (m.chip) {{
          html += '<br><img class="popup-chip" src="data:image/png;base64,' + m.chip + '">';
        }}
      }} else {{
        html  = '<b>MATCHED VESSEL</b><br>';
        html += 'Detection ID: ' + m.id + '<br>';
        html += 'Position: ' + m.lat.toFixed(5) + 'N, ' + m.lon.toFixed(5) + 'E<br>';
        html += 'Satellite pass: ' + m.time + '<br>';
        html += 'Model confidence: ' + m.conf + '<br>';
        if (m.vessel_name) {{
          html += 'Vessel name: <b>' + m.vessel_name + '</b><br>';
        }}
        html += 'MMSI: ' + m.mmsi + '<br>';
        html += 'AIS distance: ' + (m.dist !== null ? m.dist + 'm' : '?');
      }}
      L.circleMarker([m.lat, m.lon], {{
        radius: 8, color: m.color, fillColor: m.color,
        fillOpacity: 0.8, weight: 2
      }}).addTo(map).bindPopup(html, {{maxWidth: 300}});
    }});
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
