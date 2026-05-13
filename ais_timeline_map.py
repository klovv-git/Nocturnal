#!/usr/bin/env python3
"""
ais_timeline_map.py — interactive timeline map showing AIS vessel tracks
alongside radar detections, with a scrubbable timeline and satellite
snapshot markers.

Layers (each toggleable):
  - AIS vessel tracks with animated trails
  - Radar detections (dark and matched) per satellite pass

Controls:
  - Timeline scrubber to move through time
  - Play / Pause button
  - Satellite pass markers on timeline (click to jump to that moment)

Usage:
    python ais_timeline_map.py --scene <SAFE folder name> [--hours 12] [--thin 5]
"""

import argparse
import base64
import json
import re
import sqlite3
import datetime
import calendar
from pathlib import Path

DEFAULT_DB = Path("ais_memory.db")
OUT_FILE   = Path("ais_timeline_map.html")
BBOX_PAD   = 1.0    # degrees around detections bounding box


def parse_scene_time(scene_name):
    """Extract start and end Unix timestamps from a scene name."""
    matches = re.findall(r'(\d{8}T\d{6})', scene_name)
    if len(matches) < 2:
        return None, None
    def to_ts(s):
        dt = datetime.datetime.strptime(s, "%Y%m%dT%H%M%S")
        return calendar.timegm(dt.timetuple())
    return to_ts(matches[0]), to_ts(matches[1])


def fmt_time(ts):
    dt = datetime.datetime.utcfromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene",  required=True,
                    help="SAFE folder name of the primary satellite scene")
    ap.add_argument("--db",     default=str(DEFAULT_DB))
    ap.add_argument("--hours",  type=float, default=12,
                    help="Total time window to show (hours, centred on satellite pass)")
    ap.add_argument("--thin",   type=int, default=5,
                    help="Keep one AIS ping per vessel per N minutes")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    # --- satellite pass time ---
    t_start, t_end = parse_scene_time(args.scene)
    if not t_start:
        raise SystemExit("Could not parse timestamps from scene name.")
    t_mid = (t_start + t_end) / 2
    half  = (args.hours * 3600) / 2
    t_lo  = t_mid - half
    t_hi  = t_mid + half

    # --- detections for this scene ---
    dets = conn.execute(
        """SELECT id, lat, lon, confidence, dark, matched_mmsi, match_dist_m
           FROM detections
           WHERE scene_name = ? AND lat IS NOT NULL AND dark IS NOT NULL""",
        (args.scene,)
    ).fetchall()

    if not dets:
        raise SystemExit("No geocoded detections found for this scene.")

    dark_dets    = [d for d in dets if d[4] == 1]
    matched_dets = [d for d in dets if d[4] == 0]

    all_lats = [d[1] for d in dets]
    all_lons = [d[2] for d in dets]
    lat_min  = min(all_lats) - BBOX_PAD
    lat_max  = max(all_lats) + BBOX_PAD
    lon_min  = min(all_lons) - BBOX_PAD
    lon_max  = max(all_lons) + BBOX_PAD
    clat     = sum(all_lats) / len(all_lats)
    clon     = sum(all_lons) / len(all_lons)

    # --- dark chips ---
    date_str  = re.findall(r'(\d{8})T', args.scene)
    chips_dir = Path(f"dark_chips_{date_str[0]}") if date_str else None

    def load_chip(det_id, lat, lon):
        if not chips_dir or not chips_dir.exists():
            return None
        fname = chips_dir / f"dark_{det_id:04d}_{lat:.4f}N_{lon:.4f}E.png"
        if not fname.exists():
            return None
        return base64.b64encode(fname.read_bytes()).decode("ascii")

    # --- vessel names ---
    vessel_names = {}
    for row in conn.execute("SELECT mmsi, name FROM vessels").fetchall():
        if row[1]:
            vessel_names[row[0]] = row[1].strip()

    # --- AIS tracks ---
    print(f"Querying AIS positions {fmt_time(t_lo)} → {fmt_time(t_hi)} ...")
    rows = conn.execute(
        """SELECT mmsi, lat, lon, ts_epoch
           FROM positions
           WHERE ts_epoch BETWEEN ? AND ?
             AND lat BETWEEN ? AND ?
             AND lon BETWEEN ? AND ?
           ORDER BY mmsi, ts_epoch""",
        (t_lo, t_hi, lat_min, lat_max, lon_min, lon_max)
    ).fetchall()

    # thin: one ping per vessel per thin-minute bucket
    thin_sec = args.thin * 60
    tracks   = {}   # mmsi -> list of [ts, lat, lon]
    for mmsi, lat, lon, ts in rows:
        if mmsi not in tracks:
            tracks[mmsi] = []
        bucket = int(ts // thin_sec)
        if not tracks[mmsi] or tracks[mmsi][-1][3] != bucket:
            tracks[mmsi].append([ts, lat, lon, bucket])

    # strip the bucket column
    tracks_clean = {
        mmsi: [[p[0], p[1], p[2]] for p in pings]
        for mmsi, pings in tracks.items()
        if len(pings) >= 2   # need at least 2 points for a track
    }

    print(f"  {len(tracks_clean)} vessel tracks")

    # build track data for JS
    tracks_js = []
    for mmsi, pings in tracks_clean.items():
        tracks_js.append({
            "mmsi":  mmsi,
            "name":  vessel_names.get(mmsi, None),
            "pings": pings,   # [[ts, lat, lon], ...]
        })

    # --- radar detections data ---
    detections_js = {
        "scene":   args.scene,
        "t_mid":   t_mid,
        "time":    fmt_time(t_mid),
        "dark":    [{"id": d[0], "lat": d[1], "lon": d[2],
                     "conf": round(d[3], 2),
                     "chip": load_chip(d[0], d[1], d[2])} for d in dark_dets],
        "matched": [{"id": d[0], "lat": d[1], "lon": d[2],
                     "conf": round(d[3], 2), "mmsi": d[5],
                     "name": vessel_names.get(d[5], None)} for d in matched_dets],
    }

    data_json = json.dumps({
        "t_lo":       t_lo,
        "t_hi":       t_hi,
        "t_mid":      t_mid,
        "thin_sec":   thin_sec,
        "tracks":     tracks_js,
        "detections": detections_js,
    })

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>NOCTURNAL — Timeline Map</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: Arial, sans-serif; display: flex; flex-direction: column; height: 100vh; }}
    #map {{ flex: 1; height: 100%; width: 100%; }}

    /* timeline bar */
    #timeline-bar {{
      background: #1a1a2e; color: #eee; padding: 10px 16px;
      display: flex; align-items: center; gap: 12px; flex-shrink: 0;
      user-select: none;
    }}
    #time-label {{ font-size: 13px; white-space: nowrap; min-width: 180px; }}
    #timeline-wrap {{
      position: relative; flex: 1; height: 36px; display: flex; align-items: center;
    }}
    #timeline-slider {{
      width: 100%; cursor: pointer; accent-color: #e74c3c;
    }}
    #satellite-markers {{
      position: absolute; top: 0; left: 0; width: 100%; height: 100%;
      pointer-events: none;
    }}
    .sat-tick {{
      position: absolute; top: 0; height: 100%;
      display: flex; flex-direction: column; align-items: center;
      cursor: pointer; pointer-events: all;
    }}
    .sat-tick-line {{
      width: 2px; background: #e74c3c; flex: 1;
      opacity: 0.8;
    }}
    .sat-tick-label {{
      font-size: 10px; color: #e74c3c; white-space: nowrap;
      margin-top: 2px;
    }}
    #play-btn {{
      background: #e74c3c; color: white; border: none;
      padding: 6px 14px; border-radius: 4px; cursor: pointer;
      font-size: 13px; white-space: nowrap;
    }}
    #play-btn:hover {{ background: #c0392b; }}

    /* layer toggle */
    #layer-toggle {{
      position: absolute; top: 10px; right: 10px; z-index: 1000;
      background: white; padding: 10px 14px; border-radius: 6px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.3); font-size: 13px;
      line-height: 24px;
    }}
    #layer-toggle label {{ display: flex; align-items: center; gap: 6px; cursor: pointer; }}
    .dot {{ display: inline-block; width: 11px; height: 11px;
            border-radius: 50%; flex-shrink: 0; }}

    #title {{
      position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
      z-index: 1000; background: white; padding: 7px 18px;
      border-radius: 6px; box-shadow: 0 2px 8px rgba(0,0,0,0.3);
      font-size: 14px; font-weight: bold; white-space: nowrap;
    }}
  </style>
</head>
<body>
  <div style="position:relative; flex:1; min-height:0;">
    <div id="title">NOCTURNAL — Maritime Timeline</div>
    <div id="map"></div>
    <div id="layer-toggle">
      <label><input type="checkbox" id="tog-ais" checked>
        <span class="dot" style="background:#3498db"></span> AIS tracks</label>
      <label><input type="checkbox" id="tog-radar" checked>
        <span class="dot" style="background:#e74c3c"></span> Radar detections</label>
    </div>
  </div>
  <div id="timeline-bar">
    <button id="play-btn">▶ Play</button>
    <div id="time-label">--</div>
    <div id="timeline-wrap">
      <input type="range" id="timeline-slider" min="0" max="1000" value="500">
      <div id="satellite-markers"></div>
    </div>
  </div>

  <script>
  var DATA = {data_json};

  var map = L.map('map').setView([{clat}, {clon}], 9);
  L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '© OpenStreetMap contributors'
  }}).addTo(map);

  var aisGroup   = L.layerGroup().addTo(map);
  var radarGroup = L.layerGroup().addTo(map);

  var TRAIL_SEC = 30 * 60;   // 30 minute trail
  var tLo = DATA.t_lo, tHi = DATA.t_hi;

  // --- build vessel index ---
  // for each track, index pings by position for fast lookup
  var tracks = DATA.tracks;

  function getPos(pings, t) {{
    // return interpolated [lat, lon] at time t, or null
    if (!pings.length) return null;
    if (t < pings[0][0] || t > pings[pings.length-1][0]) return null;
    for (var i = 0; i < pings.length - 1; i++) {{
      var a = pings[i], b = pings[i+1];
      if (t >= a[0] && t <= b[0]) {{
        var frac = (t - a[0]) / (b[0] - a[0]);
        return [a[1] + frac*(b[1]-a[1]), a[2] + frac*(b[2]-a[2])];
      }}
    }}
    return null;
  }}

  function getTrail(pings, t) {{
    // return array of [lat,lon] for the trail up to t
    var trail = [];
    var t0 = t - TRAIL_SEC;
    for (var i = 0; i < pings.length; i++) {{
      if (pings[i][0] >= t0 && pings[i][0] <= t) {{
        trail.push([pings[i][1], pings[i][2]]);
      }}
    }}
    return trail;
  }}

  // pre-build marker objects
  var vesselMarkers = {{}};
  var vesselTrails  = {{}};

  tracks.forEach(function(t) {{
    var marker = L.circleMarker([0,0], {{
      radius: 5, color: '#3498db', fillColor: '#3498db',
      fillOpacity: 0.85, weight: 1.5
    }});
    var label = '<b>' + (t.name || 'Unknown') + '</b><br>MMSI: ' +
      '<a href="https://www.marinetraffic.com/en/ais/details/ships/mmsi:' +
      t.mmsi + '" target="_blank">' + t.mmsi + ' ↗</a>';
    marker.bindPopup(label, {{maxWidth: 250}});
    vesselMarkers[t.mmsi] = {{ marker: marker, pings: t.pings }};

    var trail = L.polyline([], {{color:'#3498db', weight:1.5, opacity:0.4}});
    vesselTrails[t.mmsi] = trail;
  }});

  // radar detection markers (static)
  var det = DATA.detections;
  det.dark.forEach(function(d) {{
    var m = L.circleMarker([d.lat, d.lon], {{
      radius: 8, color: '#e74c3c', fillColor: '#e74c3c',
      fillOpacity: 0.9, weight: 2
    }});
    var chipHtml = d.chip
      ? '<br><img src="data:image/png;base64,' + d.chip +
        '" style="width:640px;height:640px;margin-top:8px;image-rendering:pixelated;border:1px solid #ccc;">'
      : '';
    m.bindPopup('<b>DARK VESSEL CANDIDATE</b><br>ID: ' + d.id +
      '<br>Confidence: ' + d.conf +
      '<br>Satellite: ' + det.time +
      '<br>No AIS signal within 1km/30min' + chipHtml,
      {{maxWidth: 660}});
    radarGroup.addLayer(m);
  }});
  det.matched.forEach(function(d) {{
    var m = L.circleMarker([d.lat, d.lon], {{
      radius: 8, color: '#2ecc71', fillColor: '#2ecc71',
      fillOpacity: 0.9, weight: 2
    }});
    var name = d.name ? '<b>' + d.name + '</b><br>' : '';
    m.bindPopup('<b>MATCHED VESSEL</b><br>' + name + 'MMSI: ' +
      '<a href="https://www.marinetraffic.com/en/ais/details/ships/mmsi:' +
      d.mmsi + '" target="_blank">' + d.mmsi + ' ↗</a>' +
      '<br>Satellite: ' + det.time +
      '<br>Confidence: ' + d.conf, {{maxWidth:250}});
    radarGroup.addLayer(m);
  }});

  // --- timeline ---
  var slider   = document.getElementById('timeline-slider');
  var timeLabel = document.getElementById('time-label');
  var satDiv   = document.getElementById('satellite-markers');

  // place satellite pass tick on the timeline
  var satFrac = (DATA.t_mid - tLo) / (tHi - tLo);
  var tick = document.createElement('div');
  tick.className = 'sat-tick';
  tick.style.left = (satFrac * 100) + '%';
  tick.innerHTML = '<div class="sat-tick-line"></div>' +
    '<div class="sat-tick-label">📡 ' + det.time + '</div>';
  tick.title = 'Jump to satellite pass';
  tick.addEventListener('click', function() {{
    slider.value = Math.round(satFrac * 1000);
    update();
  }});
  satDiv.appendChild(tick);

  function currentTime() {{
    return tLo + (slider.value / 1000) * (tHi - tLo);
  }}

  function fmtTs(ts) {{
    var d = new Date(ts * 1000);
    return d.getUTCFullYear() + '-' +
      String(d.getUTCMonth()+1).padStart(2,'0') + '-' +
      String(d.getUTCDate()).padStart(2,'0') + ' ' +
      String(d.getUTCHours()).padStart(2,'0') + ':' +
      String(d.getUTCMinutes()).padStart(2,'0') + ' UTC';
  }}

  function update() {{
    var t = currentTime();
    timeLabel.textContent = fmtTs(t);

    aisGroup.clearLayers();

    if (document.getElementById('tog-ais').checked) {{
      tracks.forEach(function(tr) {{
        var pos = getPos(tr.pings, t);
        if (!pos) return;
        var marker = vesselMarkers[tr.mmsi].marker;
        marker.setLatLng(pos);
        aisGroup.addLayer(marker);

        var trail = getTrail(tr.pings, t);
        if (trail.length > 1) {{
          var line = vesselTrails[tr.mmsi];
          line.setLatLngs(trail);
          aisGroup.addLayer(line);
        }}
      }});
    }}
  }}

  slider.addEventListener('input', update);

  // layer toggles
  document.getElementById('tog-ais').addEventListener('change', update);
  document.getElementById('tog-radar').addEventListener('change', function() {{
    if (this.checked) map.addLayer(radarGroup);
    else map.removeLayer(radarGroup);
  }});

  // play / pause
  var playing = false;
  var playInterval = null;
  var STEP = 2;   // slider units per tick
  document.getElementById('play-btn').addEventListener('click', function() {{
    playing = !playing;
    this.textContent = playing ? '⏸ Pause' : '▶ Play';
    if (playing) {{
      playInterval = setInterval(function() {{
        var v = parseInt(slider.value) + STEP;
        if (v > 1000) v = 0;
        slider.value = v;
        update();
      }}, 100);
    }} else {{
      clearInterval(playInterval);
    }}
  }});

  // initialise at satellite pass time
  slider.value = Math.round(satFrac * 1000);
  update();
  </script>
</body>
</html>"""

    OUT_FILE.write_text(html, encoding="utf-8")
    print(f"Map saved to {OUT_FILE.resolve()}")
    print(f"  {len(tracks_clean)} AIS vessel tracks")
    print(f"  {len(dark_dets)} dark detections, {len(matched_dets)} matched detections")
    print(f"  Time window: {fmt_time(t_lo)} → {fmt_time(t_hi)}")
    print("Open ais_timeline_map.html in your browser.")


if __name__ == "__main__":
    main()
