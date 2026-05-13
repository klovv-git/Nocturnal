#!/usr/bin/env python3
"""
ais_overlay_map.py — generate an interactive HTML map showing a frozen
snapshot of the sea at the exact moment the satellite passed.

Three layers:
  Blue   = every vessel broadcasting AIS within ±30 min of the satellite pass
  Green  = radar detections that matched an AIS ping (confirmed ships)
  Red    = radar detections with no AIS match (dark vessel candidates)

Usage:
    python ais_overlay_map.py --scene <SAFE folder name>
    Then open ais_overlay_map.html in a browser.
"""

import argparse
import base64
import json
import re
import sqlite3
from pathlib import Path

DEFAULT_DB       = Path("ais_memory.db")
OUT_FILE         = Path("ais_overlay_map.html")
AIS_WINDOW       = 1800   # ±30 minutes in seconds
BBOX_PAD         = 0.5    # degrees of padding around scene bounding box
SENTINEL_HUB_ID  = "5ba9f0a2-f8ce-4d7d-be53-f0f7c93510ac"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", required=True)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--window", type=int, default=AIS_WINDOW,
                    help="AIS time window in seconds either side of t_mid")
    ap.add_argument("--sh-id", default=SENTINEL_HUB_ID,
                    help="Sentinel Hub instance ID for Sentinel-2 WMS overlay")
    args = ap.parse_args()

    # parse scene date and time from scene name
    dt_match = re.search(r'_(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})_', args.scene)
    if dt_match:
        y, mo, d, h, mi, s = dt_match.groups()
        scene_time = f"{y}-{mo}-{d} {h}:{mi}:{s} UTC"
        date_str   = f"{y}{mo}{d}"
        wms_date   = f"{y}-{mo}-{d}/{y}-{mo}-{int(d)+1:02d}"
    else:
        scene_time = "unknown"
        date_str   = None
        wms_date   = None

    # dark chips folder
    chips_dir = Path(f"dark_chips_{date_str}") if date_str else None

    def load_chip(det_id, lat, lon):
        if not chips_dir or not chips_dir.exists():
            return None
        fname = chips_dir / f"dark_{det_id:04d}_{lat:.4f}N_{lon:.4f}E.png"
        if not fname.exists():
            return None
        return base64.b64encode(fname.read_bytes()).decode("ascii")

    conn = sqlite3.connect(args.db)

    # get detections for this scene
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

    # calculate t_mid from the two timestamps in the scene name
    # e.g. S1D_..._20260512T061448_20260512T061513_...
    import datetime, calendar
    ts_matches = re.findall(r'(\d{8}T\d{6})', args.scene)
    if len(ts_matches) < 2:
        print("Could not parse start/end time from scene name.")
        return
    def parse_ts(s):
        dt = datetime.datetime.strptime(s, "%Y%m%dT%H%M%S")
        return calendar.timegm(dt.timetuple())
    t_start = parse_ts(ts_matches[0])
    t_end   = parse_ts(ts_matches[1])
    t_mid   = (t_start + t_end) / 2
    t_lo  = t_mid - args.window
    t_hi  = t_mid + args.window

    # bounding box from detections + padding
    all_lats = [d[1] for d in dets]
    all_lons = [d[2] for d in dets]
    lat_min = min(all_lats) - BBOX_PAD
    lat_max = max(all_lats) + BBOX_PAD
    lon_min = min(all_lons) - BBOX_PAD
    lon_max = max(all_lons) + BBOX_PAD
    clat = sum(all_lats) / len(all_lats)
    clon = sum(all_lons) / len(all_lons)

    # query AIS pings within time window and bounding box
    print(f"Querying AIS pings for {scene_time} ±{args.window//60} min ...")
    ais_rows = conn.execute(
        """SELECT mmsi, lat, lon, ts_epoch
           FROM positions
           WHERE ts_epoch BETWEEN ? AND ?
             AND lat BETWEEN ? AND ?
             AND lon BETWEEN ? AND ?""",
        (t_lo, t_hi, lat_min, lat_max, lon_min, lon_max)
    ).fetchall()

    # deduplicate: keep one ping per MMSI (closest to t_mid)
    best = {}
    for mmsi, lat, lon, ts in ais_rows:
        if mmsi not in best or abs(ts - t_mid) < abs(best[mmsi][2] - t_mid):
            best[mmsi] = (lat, lon, ts)

    print(f"  {len(best)} unique AIS vessels in snapshot")

    # build marker data
    markers_data = []

    # build vessel name lookup from vessels table
    vessel_names = {}
    rows = conn.execute("SELECT mmsi, name FROM vessels").fetchall()
    for vmmi, vname in rows:
        if vname:
            vessel_names[vmmi] = vname.strip()

    # AIS snapshot layer (blue)
    for mmsi, (lat, lon, ts) in best.items():
        markers_data.append({
            "lat":   lat,
            "lon":   lon,
            "color": "#3498db",
            "type":  "ais",
            "mmsi":  mmsi,
            "name":  vessel_names.get(mmsi, None),
            "time":  scene_time,
            "link":  f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{mmsi}",
        })

    # dark detections (red)
    for d in dark:
        det_id, lat, lon, conf, _, mmsi, dist = d
        chip_b64 = load_chip(det_id, lat, lon)
        markers_data.append({
            "lat":   lat,
            "lon":   lon,
            "color": "#e74c3c",
            "type":  "dark",
            "id":    det_id,
            "conf":  round(conf, 2),
            "time":  scene_time,
            "chip":  chip_b64,
        })

    # matched detections (green)
    for d in matched:
        det_id, lat, lon, conf, _, mmsi, dist = d
        markers_data.append({
            "lat":   lat,
            "lon":   lon,
            "color": "#2ecc71",
            "type":  "matched",
            "id":    det_id,
            "conf":  round(conf, 2),
            "time":  scene_time,
            "mmsi":  mmsi,
            "name":  vessel_names.get(mmsi, None),
            "dist":  round(dist) if dist else None,
            "link":  f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{mmsi}",
        })

    markers_json = json.dumps(markers_data)

    sh_wms_url = f"https://sh.dataspace.copernicus.eu/ogc/wms/{args.sh_id}"
    s2_time    = wms_date or "unknown"

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>NOCTURNAL — AIS Overlay Map</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; }}
    #map {{ height: 100vh; }}
    #legend {{
      position: absolute; bottom: 30px; left: 10px; z-index: 1000;
      background: white; padding: 12px 16px; border-radius: 6px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.3); font-size: 13px; line-height: 22px;
    }}
    .dot {{ display: inline-block; width: 12px; height: 12px;
            border-radius: 50%; margin-right: 6px; vertical-align: middle; }}
    #title {{
      position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
      z-index: 1000; background: white; padding: 8px 20px;
      border-radius: 6px; box-shadow: 0 2px 8px rgba(0,0,0,0.3);
      font-size: 14px; font-weight: bold; white-space: nowrap;
    }}
    .popup-chip {{
      display: block; width: 128px; height: 128px;
      margin-top: 8px; border: 1px solid #ccc;
    }}
    /* opacity slider */
    #s2-controls {{
      position: absolute; top: 60px; right: 50px; z-index: 1000;
      background: white; padding: 10px 14px; border-radius: 6px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.3); font-size: 12px;
      display: none;
    }}
    #s2-controls label {{ display: block; margin-bottom: 4px; color: #555; }}
    #s2-opacity {{ width: 130px; }}
  </style>
</head>
<body>
  <div id="title">NOCTURNAL — Snapshot {scene_time}</div>
  <div id="map"></div>
  <div id="legend">
    <div><span class="dot" style="background:#3498db"></span>AIS vessel ({len(best)})</div>
    <div><span class="dot" style="background:#2ecc71"></span>Radar matched ({len(matched)})</div>
    <div><span class="dot" style="background:#e74c3c"></span>Dark candidate ({len(dark)})</div>
  </div>
  <div id="s2-controls">
    <label>Sentinel-2 opacity</label>
    <input id="s2-opacity" type="range" min="0" max="1" step="0.05" value="0.8"
           oninput="s2Layer.setOpacity(this.value)">
  </div>
  <script>
    // --- Base layers ---
    var osmLayer = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      attribution: '© OpenStreetMap contributors'
    }});

    var s2Layer = L.tileLayer.wms('{sh_wms_url}', {{
      layers:      'TRUE-COLOR',
      format:      'image/jpeg',
      transparent: false,
      TIME:        '{s2_time}',
      attribution: 'Sentinel-2 L2A © ESA / Copernicus',
      maxZoom:     18,
      tileSize:    512
    }});

    var map = L.map('map', {{
      center: [{clat}, {clon}],
      zoom:   9,
      layers: [osmLayer]
    }});

    // --- Overlay layer groups ---
    var aisGroup     = L.layerGroup().addTo(map);
    var darkGroup    = L.layerGroup().addTo(map);
    var matchedGroup = L.layerGroup().addTo(map);

    // --- Markers ---
    var markers = {markers_json};

    markers.forEach(function(m) {{
      var html, radius, opacity, group;

      if (m.type === 'ais') {{
        radius  = 5;
        opacity = 0.5;
        group   = aisGroup;
        html    = '<b>AIS VESSEL</b><br>';
        if (m.name) html += '<b>' + m.name + '</b><br>';
        html += 'MMSI: <a href="' + m.link + '" target="_blank">' + m.mmsi + ' ↗</a><br>';
        html += 'Position at: ' + m.time + '<br>(broadcasting AIS)';
      }} else if (m.type === 'dark') {{
        radius  = 9;
        opacity = 0.9;
        group   = darkGroup;
        html    = '<b>DARK VESSEL CANDIDATE</b><br>';
        html   += 'Detection ID: ' + m.id + '<br>';
        html   += 'Position: ' + m.lat.toFixed(5) + 'N, ' + m.lon.toFixed(5) + 'E<br>';
        html   += 'Satellite pass: ' + m.time + '<br>';
        html   += 'Confidence: ' + m.conf + '<br>';
        html   += 'No AIS signal within 1km / 30min';
        if (m.chip) html += '<br><img class="popup-chip" src="data:image/png;base64,' + m.chip + '">';
      }} else {{
        radius  = 9;
        opacity = 0.9;
        group   = matchedGroup;
        html    = '<b>MATCHED VESSEL</b><br>';
        html   += 'Detection ID: ' + m.id + '<br>';
        html   += 'Position: ' + m.lat.toFixed(5) + 'N, ' + m.lon.toFixed(5) + 'E<br>';
        html   += 'Satellite pass: ' + m.time + '<br>';
        html   += 'Confidence: ' + m.conf + '<br>';
        if (m.name) html += 'Vessel: <b>' + m.name + '</b><br>';
        html   += 'MMSI: <a href="' + m.link + '" target="_blank">' + m.mmsi + ' ↗</a><br>';
        html   += 'AIS distance: ' + (m.dist !== null ? m.dist + 'm' : '?');
      }}

      L.circleMarker([m.lat, m.lon], {{
        radius: radius, color: m.color, fillColor: m.color,
        fillOpacity: opacity, weight: 2
      }}).addTo(group).bindPopup(html, {{maxWidth: 300}});
    }});

    // --- Layer control ---
    var baseLayers = {{
      'OpenStreetMap':           osmLayer,
      'Sentinel-2 L2A (optical)': s2Layer
    }};
    var overlays = {{
      'AIS vessels':         aisGroup,
      'Dark detections':     darkGroup,
      'Matched detections':  matchedGroup
    }};
    L.control.layers(baseLayers, overlays, {{collapsed: false}}).addTo(map);

    // Show opacity slider only when Sentinel-2 base layer is active
    map.on('baselayerchange', function(e) {{
      var ctrl = document.getElementById('s2-controls');
      ctrl.style.display = (e.name === 'Sentinel-2 L2A (optical)') ? 'block' : 'none';
    }});
  </script>
</body>
</html>"""

    OUT_FILE.write_text(html, encoding="utf-8")
    print(f"Map saved to {OUT_FILE.resolve()}")
    print(f"  {len(best)} AIS vessels in snapshot (blue)")
    print(f"  {len(matched)} matched detections (green)")
    print(f"  {len(dark)} dark vessel candidates (red)")
    print("Open ais_overlay_map.html in your browser.")


if __name__ == "__main__":
    main()
