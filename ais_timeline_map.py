#!/usr/bin/env python3
"""
ais_timeline_map.py — interactive multi-scene timeline map.

Generates a single self-contained HTML file with:
  • Left-panel scene calendar — click any date to switch the map
  • AIS vessel tracks with animated trails and richer popups
  • Radar detections (dark / matched) per scene
  • SAR image overlay per scene (auto-detected from --sar-dir)
  • Scrubbable timeline + play/pause, satellite-pass tick marker

Usage (single scene — backward-compatible):
    python ais_timeline_map.py --scene <SAFE name> [--sar-overlay sar_overlay_YYYYMMDD.png]

Usage (multi-scene):
    python ais_timeline_map.py --scenes <SAFE1> <SAFE2> ... [--sar-dir .]
"""

import argparse
import base64
import json
import re
import sqlite3
import datetime
import calendar
from pathlib import Path

from config import DB_PATH, TIMELINE_HOURS, THIN_MINUTES, BBOX_PAD

DEFAULT_DB = DB_PATH
OUT_FILE   = Path("ais_timeline_map.html")


# ── helpers ───────────────────────────────────────────────────────────────────

def parse_scene_time(scene_name):
    """Return (start_ts, end_ts) Unix timestamps from a Sentinel scene name."""
    matches = re.findall(r'(\d{8}T\d{6})', scene_name)
    if len(matches) < 2:
        return None, None
    def to_ts(s):
        dt = datetime.datetime.strptime(s, "%Y%m%dT%H%M%S")
        return calendar.timegm(dt.timetuple())
    return to_ts(matches[0]), to_ts(matches[1])


def scene_date_str(scene_name):
    """Return the 8-digit date string from a scene name (e.g. '20260512')."""
    m = re.search(r'_(\d{8})T', scene_name)
    return m.group(1) if m else "unknown"


def fmt_utc(ts):
    dt = datetime.datetime.utcfromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def fmt_display_date(date8):
    """'20260512' → '12 May 2026'"""
    try:
        dt = datetime.datetime.strptime(date8, "%Y%m%d")
        return dt.strftime("%-d %b %Y")
    except Exception:
        return date8


def load_sar_overlay(path: Path):
    """
    Load a SAR overlay PNG + companion JSON bounds file.
    Returns (b64_string, bounds_dict) or (None, None).
    """
    if not path or not path.exists():
        return None, None
    bounds_file = path.with_suffix(".json")
    if not bounds_file.exists():
        print(f"  Warning: {bounds_file.name} not found — SAR overlay skipped.")
        return None, None
    b64     = base64.b64encode(path.read_bytes()).decode("ascii")
    bounds  = json.loads(bounds_file.read_text())
    return b64, bounds


# ── per-scene data loading ────────────────────────────────────────────────────

def load_scene(conn, scene_name: str, hours: float, thin_min: int,
               sar_path: Path) -> dict:
    """
    Load everything needed for one scene from the database.
    Returns a dict suitable for embedding in the SCENES JS variable.
    """
    t_start, t_end = parse_scene_time(scene_name)
    if not t_start:
        raise SystemExit(f"Cannot parse timestamps from scene name: {scene_name}")

    t_mid  = (t_start + t_end) / 2
    half   = (hours * 3600) / 2
    t_lo   = t_mid - half
    t_hi   = t_mid + half

    # ── detections ────────────────────────────────────────────────────────────
    dets = conn.execute(
        """SELECT id, lat, lon, confidence, dark, matched_mmsi, match_dist_m
           FROM detections
           WHERE scene_name = ? AND lat IS NOT NULL AND dark IS NOT NULL""",
        (scene_name,)
    ).fetchall()

    if not dets:
        print(f"  Warning: no geocoded detections for {scene_name[:40]} — skipping.")
        return None

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

    # ── vessel info ───────────────────────────────────────────────────────────
    vessel_info = {}
    for row in conn.execute(
        "SELECT mmsi, name, callsign, imo, ship_type FROM vessels"
    ).fetchall():
        mmsi, name, callsign, imo, ship_type = row
        vessel_info[mmsi] = {
            "name":      name.strip()      if name      else None,
            "callsign":  callsign.strip()  if callsign  else None,
            "imo":       imo               if imo        else None,
            "ship_type": str(ship_type)    if ship_type  else None,
        }
    vessel_names = {m: v["name"] for m, v in vessel_info.items() if v["name"]}

    # ── AIS tracks ────────────────────────────────────────────────────────────
    rows = conn.execute(
        """SELECT mmsi, lat, lon, ts_epoch
           FROM positions
           WHERE ts_epoch BETWEEN ? AND ?
             AND lat BETWEEN ? AND ?
             AND lon BETWEEN ? AND ?
           ORDER BY mmsi, ts_epoch""",
        (t_lo, t_hi, lat_min, lat_max, lon_min, lon_max)
    ).fetchall()

    thin_sec = thin_min * 60
    tracks   = {}
    for mmsi, lat, lon, ts in rows:
        if mmsi not in tracks:
            tracks[mmsi] = []
        bucket = int(ts // thin_sec)
        if not tracks[mmsi] or tracks[mmsi][-1][3] != bucket:
            tracks[mmsi].append([ts, lat, lon, bucket])

    tracks_clean = {
        mmsi: [[p[0], p[1], p[2]] for p in pings]
        for mmsi, pings in tracks.items()
        if len(pings) >= 2
    }

    tracks_js = []
    for mmsi, pings in tracks_clean.items():
        info = vessel_info.get(mmsi, {})
        tracks_js.append({
            "mmsi":      mmsi,
            "name":      info.get("name"),
            "callsign":  info.get("callsign"),
            "imo":       info.get("imo"),
            "ship_type": info.get("ship_type"),
            "pings":     pings,
        })

    # ── radar detections ──────────────────────────────────────────────────────
    detections_js = {
        "scene": scene_name,
        "t_mid": t_mid,
        "time":  fmt_utc(t_mid),
        "dark":  [{"id": d[0], "lat": d[1], "lon": d[2],
                   "conf": round(d[3], 2)} for d in dark_dets],
        "matched": [{"id": d[0], "lat": d[1], "lon": d[2],
                     "conf": round(d[3], 2), "mmsi": d[5],
                     "name": vessel_names.get(d[5])} for d in matched_dets],
    }

    # ── dark chips ────────────────────────────────────────────────────────────
    date8    = scene_date_str(scene_name)
    chips_dir = Path(f"dark_chips_{date8}")
    chips = {}
    for d in dark_dets:
        det_id, lat, lon = d[0], d[1], d[2]
        fname = chips_dir / f"dark_{det_id:04d}_{lat:.4f}N_{lon:.4f}E.png"
        if fname.exists():
            chips[det_id] = base64.b64encode(fname.read_bytes()).decode("ascii")

    # ── SAR overlay ───────────────────────────────────────────────────────────
    sar_b64, sar_bounds = load_sar_overlay(sar_path)
    if sar_b64:
        print(f"  SAR overlay : {sar_path.name}  ({len(sar_b64) >> 10} KB b64)")

    print(f"  {len(tracks_clean)} AIS tracks  |  "
          f"{len(dark_dets)} dark  |  {len(matched_dets)} matched  |  "
          f"{len(chips)} chips")

    return {
        "scene":      scene_name,
        "date8":      date8,
        "t_lo":       t_lo,
        "t_hi":       t_hi,
        "t_mid":      t_mid,
        "thin_sec":   thin_sec,
        "center":     [clat, clon],
        "tracks":     tracks_js,
        "detections": detections_js,
        "chips":      chips,
        "sar_b64":    sar_b64,
        "sar_bounds": sar_bounds,
        "dark_count":    len(dark_dets),
        "matched_count": len(matched_dets),
        "pass_time":  fmt_utc(t_mid),
    }


# ── HTML generation ───────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>NOCTURNAL — Timeline Map</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: Arial, sans-serif; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

    /* ── top section: calendar + map ── */
    #top { display: flex; flex: 1; min-height: 0; }

    /* calendar sidebar */
    #calendar-panel {
      width: 230px; min-width: 230px;
      background: #12122a; color: #dde;
      display: flex; flex-direction: column;
      overflow-y: auto; flex-shrink: 0;
      border-right: 2px solid #1e1e40;
    }
    #cal-header {
      padding: 14px 16px 10px;
      font-size: 11px; font-weight: bold; letter-spacing: 1.5px;
      color: #8899cc; text-transform: uppercase;
      border-bottom: 1px solid #1e1e40;
      flex-shrink: 0;
    }
    #cal-header span { font-size: 18px; margin-right: 6px; }
    .cal-card {
      padding: 12px 16px;
      cursor: pointer;
      border-bottom: 1px solid #1e1e40;
      transition: background 0.15s;
    }
    .cal-card:hover { background: #1a1a3a; }
    .cal-card.active { background: #002060; border-left: 3px solid #e2b714; }
    .cal-card .cal-date {
      font-size: 15px; font-weight: bold; color: #eef;
      margin-bottom: 3px;
    }
    .cal-card.active .cal-date { color: #e2b714; }
    .cal-card .cal-time { font-size: 11px; color: #8899cc; margin-bottom: 6px; }
    .cal-card .cal-stats { display: flex; gap: 8px; }
    .cal-badge {
      font-size: 10px; padding: 2px 7px; border-radius: 10px;
      font-weight: bold;
    }
    .badge-dark    { background: #e74c3c22; color: #e74c3c; border: 1px solid #e74c3c55; }
    .badge-matched { background: #2ecc7122; color: #2ecc71; border: 1px solid #2ecc7155; }
    .badge-sar     { background: #f39c1222; color: #f39c12; border: 1px solid #f39c1255; }

    /* map area */
    #map-container { flex: 1; position: relative; min-width: 0; }
    #map { width: 100%; height: 100%; }

    /* overlays on map */
    #title {
      position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
      z-index: 1000; background: white; padding: 7px 18px;
      border-radius: 6px; box-shadow: 0 2px 8px rgba(0,0,0,0.3);
      font-size: 14px; font-weight: bold; white-space: nowrap;
    }
    #layer-toggle {
      position: absolute; top: 10px; right: 10px; z-index: 1000;
      background: white; padding: 10px 14px; border-radius: 6px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.3); font-size: 13px;
      line-height: 28px;
    }
    #layer-toggle label { display: flex; align-items: center; gap: 6px; cursor: pointer; }
    .dot { display: inline-block; width: 11px; height: 11px; border-radius: 50%; flex-shrink: 0; }

    /* ── timeline bar ── */
    #timeline-bar {
      background: #1a1a2e; color: #eee; padding: 10px 16px;
      display: flex; align-items: center; gap: 12px; flex-shrink: 0;
      user-select: none;
    }
    #time-label { font-size: 13px; white-space: nowrap; min-width: 180px; }
    #timeline-wrap { position: relative; flex: 1; height: 36px; display: flex; align-items: center; }
    #timeline-slider { width: 100%; cursor: pointer; accent-color: #e74c3c; }
    #satellite-markers {
      position: absolute; top: 0; left: 0; width: 100%; height: 100%;
      pointer-events: none;
    }
    .sat-tick {
      position: absolute; top: 0; height: 100%;
      display: flex; flex-direction: column; align-items: center;
      cursor: pointer; pointer-events: all;
    }
    .sat-tick-line { width: 2px; background: #e74c3c; flex: 1; opacity: 0.8; }
    .sat-tick-label { font-size: 10px; color: #e74c3c; white-space: nowrap; margin-top: 2px; }
    #play-btn {
      background: #e74c3c; color: white; border: none;
      padding: 6px 14px; border-radius: 4px; cursor: pointer;
      font-size: 13px; white-space: nowrap;
    }
    #play-btn:hover { background: #c0392b; }

    /* SAR opacity slider */
    .opacity-row {
      display: flex; align-items: center; gap: 6px;
      font-size: 11px; color: #666; margin-top: 2px;
    }
  </style>
</head>
<body>
  <div id="top">

    <!-- ── Calendar panel ── -->
    <div id="calendar-panel">
      <div id="cal-header"><span>📅</span>SCENES</div>
      <!-- cards injected by JS -->
    </div>

    <!-- ── Map ── -->
    <div id="map-container">
      <div id="title">NOCTURNAL — Maritime Timeline</div>
      <div id="map"></div>
      <div id="layer-toggle">
        <label><input type="checkbox" id="tog-ais" checked>
          <span class="dot" style="background:#3498db"></span> AIS tracks</label>
        <label><input type="checkbox" id="tog-radar" checked>
          <span class="dot" style="background:#e74c3c"></span> Radar detections</label>
        <label><input type="checkbox" id="tog-sar" checked>
          <span class="dot" style="background:#888;border-radius:2px"></span> SAR image</label>
        <div class="opacity-row">
          Opacity <input type="range" id="sar-opacity" min="0" max="1" step="0.05" value="0.75"
            style="width:80px">
        </div>
      </div>
    </div>
  </div>

  <!-- ── Timeline bar ── -->
  <div id="timeline-bar">
    <button id="play-btn">▶ Play</button>
    <div id="time-label">--</div>
    <div id="timeline-wrap">
      <input type="range" id="timeline-slider" min="0" max="1000" value="500">
      <div id="satellite-markers"></div>
    </div>
  </div>

  <script>
  // ════════════════════════════════════════════════════════════════════════════
  //  DATA — injected by Python
  // ════════════════════════════════════════════════════════════════════════════
  var SCENES      = __SCENES_JSON__;
  var SCENE_ORDER = __SCENE_ORDER_JSON__;

  // ════════════════════════════════════════════════════════════════════════════
  //  Map init
  // ════════════════════════════════════════════════════════════════════════════
  var firstScene = SCENES[SCENE_ORDER[0]];
  var map = L.map('map').setView(firstScene.center, 9);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap contributors'
  }).addTo(map);

  var aisGroup   = L.layerGroup().addTo(map);
  var radarGroup = L.layerGroup().addTo(map);
  var sarLayer   = null;

  var TRAIL_SEC = 30 * 60;

  // ── current scene state ────────────────────────────────────────────────────
  var activeDateKey   = null;
  var vesselMarkers   = {};
  var vesselTrails    = {};
  var selectedMmsi    = null;
  var tLo = 0, tHi = 1, tMid = 0;

  // ════════════════════════════════════════════════════════════════════════════
  //  Timeline controls
  // ════════════════════════════════════════════════════════════════════════════
  var slider    = document.getElementById('timeline-slider');
  var timeLabel = document.getElementById('time-label');
  var satDiv    = document.getElementById('satellite-markers');

  function currentTime() {
    return tLo + (slider.value / 1000) * (tHi - tLo);
  }

  function fmtTs(ts) {
    var d = new Date(ts * 1000);
    return d.getUTCFullYear() + '-' +
      String(d.getUTCMonth()+1).padStart(2,'0') + '-' +
      String(d.getUTCDate()).padStart(2,'0') + ' ' +
      String(d.getUTCHours()).padStart(2,'0') + ':' +
      String(d.getUTCMinutes()).padStart(2,'0') + ' UTC';
  }

  // ── helper: interpolated position ─────────────────────────────────────────
  function getPos(pings, t) {
    if (!pings.length) return null;
    if (t < pings[0][0] || t > pings[pings.length-1][0]) return null;
    for (var i = 0; i < pings.length - 1; i++) {
      var a = pings[i], b = pings[i+1];
      if (t >= a[0] && t <= b[0]) {
        var frac = (t - a[0]) / (b[0] - a[0]);
        return [a[1] + frac*(b[1]-a[1]), a[2] + frac*(b[2]-a[2])];
      }
    }
    return null;
  }

  function getTrail(pings, t) {
    var trail = [], t0 = t - TRAIL_SEC;
    for (var i = 0; i < pings.length; i++) {
      if (pings[i][0] >= t0 && pings[i][0] <= t)
        trail.push([pings[i][1], pings[i][2]]);
    }
    return trail;
  }

  // ════════════════════════════════════════════════════════════════════════════
  //  Build vessel markers for a scene
  // ════════════════════════════════════════════════════════════════════════════
  function buildVesselMarkers(tracks) {
    vesselMarkers = {};
    vesselTrails  = {};
    selectedMmsi  = null;

    tracks.forEach(function(t) {
      var marker = L.circleMarker([0,0], {
        radius: 5, color: '#3498db', fillColor: '#3498db',
        fillOpacity: 0.85, weight: 1.5
      });
      var label = '<b>' + (t.name || 'Unknown vessel') + '</b><br>' +
        'MMSI: <a href="https://www.marinetraffic.com/en/ais/details/ships/mmsi:' +
        t.mmsi + '" target="_blank">' + t.mmsi + ' ↗</a>';
      if (t.ship_type) label += '<br>Type: ' + t.ship_type;
      if (t.callsign)  label += '<br>Callsign: ' + t.callsign;
      if (t.imo)       label += '<br>IMO: ' + t.imo;
      marker.bindPopup(label, {maxWidth: 280});
      marker.on('click', (function(mmsi) {
        return function() { selectVessel(mmsi); };
      })(t.mmsi));
      vesselMarkers[t.mmsi] = { marker: marker, pings: t.pings };

      var trail = L.polyline([], {color:'#3498db', weight:1.5, opacity:0.4});
      vesselTrails[t.mmsi] = trail;
    });
  }

  function selectVessel(mmsi) {
    selectedMmsi = (selectedMmsi === mmsi) ? null : mmsi;
    update();
    if (selectedMmsi && vesselMarkers[mmsi])
      vesselMarkers[mmsi].marker.openPopup();
  }

  // ════════════════════════════════════════════════════════════════════════════
  //  Build radar markers for a scene
  // ════════════════════════════════════════════════════════════════════════════
  function buildRadarMarkers(det, chips) {
    radarGroup.clearLayers();

    det.dark.forEach(function(d) {
      var m = L.circleMarker([d.lat, d.lon], {
        radius: 8, color: '#e74c3c', fillColor: '#e74c3c',
        fillOpacity: 0.9, weight: 2
      });
      var chipB64 = chips[d.id] || null;
      var chipHtml = '';
      if (chipB64) {
        chipHtml = '<br><div style="margin-top:8px">' +
          '<button onclick="nocResize(' + d.id + ',-64)" style="padding:2px 8px;cursor:pointer">&#8722;</button> ' +
          '<button onclick="nocResize(' + d.id + ',64)" style="padding:2px 8px;cursor:pointer">+</button>' +
          '<br><img id="nchip' + d.id + '" src="data:image/png;base64,' + chipB64 + '" ' +
          'style="width:256px;height:256px;margin-top:6px;image-rendering:pixelated;' +
          'border:1px solid #ccc;display:block"></div>';
      }
      m.bindPopup('<b>DARK VESSEL CANDIDATE</b><br>ID: ' + d.id +
        '<br>Confidence: ' + d.conf +
        '<br>Satellite: ' + det.time +
        '<br>No AIS signal within 1km / 30min' + chipHtml, {maxWidth: 700});
      radarGroup.addLayer(m);
    });

    det.matched.forEach(function(d) {
      var m = L.circleMarker([d.lat, d.lon], {
        radius: 8, color: '#2ecc71', fillColor: '#2ecc71',
        fillOpacity: 0.9, weight: 2
      });
      var name = d.name ? '<b>' + d.name + '</b><br>' : '';
      m.bindPopup('<b>MATCHED VESSEL</b><br>' + name +
        'MMSI: <a href="https://www.marinetraffic.com/en/ais/details/ships/mmsi:' +
        d.mmsi + '" target="_blank">' + d.mmsi + ' ↗</a>' +
        '<br>Satellite: ' + det.time +
        '<br>Confidence: ' + d.conf, {maxWidth: 250});
      radarGroup.addLayer(m);
    });
  }

  // ════════════════════════════════════════════════════════════════════════════
  //  SAR overlay
  // ════════════════════════════════════════════════════════════════════════════
  var sarOpacity = 0.75;
  document.getElementById('sar-opacity').addEventListener('input', function() {
    sarOpacity = parseFloat(this.value);
    if (sarLayer) sarLayer.setOpacity(sarOpacity);
  });
  document.getElementById('tog-sar').addEventListener('change', function() {
    if (!sarLayer) return;
    if (this.checked) map.addLayer(sarLayer);
    else map.removeLayer(sarLayer);
  });

  function updateSarOverlay(sc) {
    if (sarLayer) { map.removeLayer(sarLayer); sarLayer = null; }
    if (sc.sar_b64 && sc.sar_bounds) {
      var b = sc.sar_bounds;
      sarLayer = L.imageOverlay(
        'data:image/png;base64,' + sc.sar_b64,
        [[b.lat_min, b.lon_min], [b.lat_max, b.lon_max]],
        {opacity: sarOpacity}
      );
      if (document.getElementById('tog-sar').checked)
        sarLayer.addTo(map);
    }
  }

  // ════════════════════════════════════════════════════════════════════════════
  //  Satellite pass tick
  // ════════════════════════════════════════════════════════════════════════════
  function updateSatTick(sc) {
    satDiv.innerHTML = '';
    var satFrac = (sc.t_mid - tLo) / (tHi - tLo);
    var tick = document.createElement('div');
    tick.className = 'sat-tick';
    tick.style.left = (satFrac * 100) + '%';
    tick.innerHTML = '<div class="sat-tick-line"></div>' +
      '<div class="sat-tick-label">📡 ' + sc.pass_time + '</div>';
    tick.title = 'Jump to satellite pass';
    tick.addEventListener('click', function() {
      slider.value = Math.round(satFrac * 1000);
      update();
    });
    satDiv.appendChild(tick);
    return satFrac;
  }

  // ════════════════════════════════════════════════════════════════════════════
  //  Main update (redraw markers at current time)
  // ════════════════════════════════════════════════════════════════════════════
  function update() {
    var t = currentTime();
    timeLabel.textContent = fmtTs(t);
    aisGroup.clearLayers();

    if (!document.getElementById('tog-ais').checked) return;

    var sc = SCENES[activeDateKey];
    var followPos = null;

    sc.tracks.forEach(function(tr) {
      var data = vesselMarkers[tr.mmsi];
      if (!data) return;
      var pos = getPos(data.pings, t);
      if (!pos) return;

      var isSelected = (tr.mmsi === selectedMmsi);
      var color  = isSelected ? '#9b59b6' : '#3498db';
      var radius = isSelected ? 9 : 5;
      var marker = data.marker;
      marker.setLatLng(pos);
      marker.setStyle({color: color, fillColor: color,
                       radius: radius, weight: isSelected ? 3 : 1.5});
      aisGroup.addLayer(marker);
      if (isSelected) followPos = pos;

      var trail = getTrail(data.pings, t);
      if (trail.length > 1) {
        var line = vesselTrails[tr.mmsi];
        line.setLatLngs(trail);
        line.setStyle({color: color, weight: isSelected ? 2.5 : 1.5,
                       opacity: isSelected ? 0.8 : 0.4});
        aisGroup.addLayer(line);
      }
    });

    if (followPos) map.panTo(followPos, {animate: true, duration: 0.3});
  }

  // ════════════════════════════════════════════════════════════════════════════
  //  Load a scene (called on calendar card click)
  // ════════════════════════════════════════════════════════════════════════════
  function loadScene(dateKey) {
    if (dateKey === activeDateKey) return;
    activeDateKey = dateKey;
    var sc = SCENES[dateKey];

    // update calendar highlight
    document.querySelectorAll('.cal-card').forEach(function(c) {
      c.classList.toggle('active', c.dataset.key === dateKey);
    });

    // update timeline bounds
    tLo  = sc.t_lo;
    tHi  = sc.t_hi;
    tMid = sc.t_mid;

    // rebuild markers
    buildVesselMarkers(sc.tracks);
    buildRadarMarkers(sc.detections, sc.chips);
    updateSarOverlay(sc);
    var satFrac = updateSatTick(sc);

    // show/hide radar per toggle
    if (document.getElementById('tog-radar').checked) map.addLayer(radarGroup);
    else map.removeLayer(radarGroup);

    // jump to satellite pass time
    slider.value = Math.round(satFrac * 1000);
    update();

    // pan to scene centre
    map.setView(sc.center, 9);
  }

  // ════════════════════════════════════════════════════════════════════════════
  //  Build calendar cards
  // ════════════════════════════════════════════════════════════════════════════
  var calPanel = document.getElementById('calendar-panel');

  SCENE_ORDER.forEach(function(key) {
    var sc = SCENES[key];
    var card = document.createElement('div');
    card.className = 'cal-card';
    card.dataset.key = key;

    var dateLabel = sc.display_date || key;
    var timeLabel2 = sc.pass_time.split(' ')[1] + ' ' + sc.pass_time.split(' ')[2]; // "HH:MM UTC"

    var sarBadge = sc.sar_b64
      ? '<span class="cal-badge badge-sar">SAR</span>'
      : '';

    card.innerHTML =
      '<div class="cal-date">' + dateLabel + '</div>' +
      '<div class="cal-time">📡 ' + timeLabel2 + '</div>' +
      '<div class="cal-stats">' +
        '<span class="cal-badge badge-dark">' + sc.dark_count + ' dark</span>' +
        '<span class="cal-badge badge-matched">' + sc.matched_count + ' matched</span>' +
        sarBadge +
      '</div>';

    card.addEventListener('click', function() { loadScene(key); });
    calPanel.appendChild(card);
  });

  // ════════════════════════════════════════════════════════════════════════════
  //  Toggle handlers
  // ════════════════════════════════════════════════════════════════════════════
  slider.addEventListener('input', update);

  document.getElementById('tog-ais').addEventListener('change', update);
  document.getElementById('tog-radar').addEventListener('change', function() {
    if (this.checked) map.addLayer(radarGroup);
    else map.removeLayer(radarGroup);
  });

  // ════════════════════════════════════════════════════════════════════════════
  //  Play / Pause
  // ════════════════════════════════════════════════════════════════════════════
  var playing = false, playInterval = null, STEP = 2;
  document.getElementById('play-btn').addEventListener('click', function() {
    playing = !playing;
    this.textContent = playing ? '⏸ Pause' : '▶ Play';
    if (playing) {
      playInterval = setInterval(function() {
        var v = parseInt(slider.value) + STEP;
        if (v > 1000) v = 0;
        slider.value = v;
        update();
      }, 100);
    } else {
      clearInterval(playInterval);
    }
  });

  // ════════════════════════════════════════════════════════════════════════════
  //  Chip zoom helper
  // ════════════════════════════════════════════════════════════════════════════
  function nocResize(id, delta) {
    var img = document.getElementById('nchip' + id);
    if (!img) return;
    var w = Math.max(64, parseInt(img.style.width) + delta);
    img.style.width  = w + 'px';
    img.style.height = w + 'px';
  }

  // ════════════════════════════════════════════════════════════════════════════
  //  Boot: load most recent scene first
  // ════════════════════════════════════════════════════════════════════════════
  loadScene(SCENE_ORDER[0]);

  </script>
</body>
</html>"""


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Generate an interactive multi-scene timeline map for NOCTURNAL"
    )

    # accept either --scene (singular, backward-compat) or --scenes (plural)
    scene_group = ap.add_mutually_exclusive_group(required=True)
    scene_group.add_argument("--scene",  help="Single scene (backward-compatible)")
    scene_group.add_argument("--scenes", nargs="+",
                             help="One or more scene SAFE folder names")

    ap.add_argument("--db",       default=str(DEFAULT_DB))
    ap.add_argument("--hours",    type=float, default=TIMELINE_HOURS,
                    help="Time window to show per scene (hours, centred on pass)")
    ap.add_argument("--thin",     type=int, default=THIN_MINUTES,
                    help="Thin AIS pings to one per vessel per N minutes")

    # SAR overlay: single path (old) or directory (new multi-scene auto-detect)
    sar_group = ap.add_mutually_exclusive_group()
    sar_group.add_argument("--sar-overlay", type=Path,
                           help="Single SAR overlay PNG (single-scene mode)")
    sar_group.add_argument("--sar-dir",     type=Path, default=Path("."),
                           help="Directory to auto-detect sar_overlay_YYYYMMDD.png files")

    args = ap.parse_args()

    # normalise to a list of scene names
    if args.scene:
        scene_list = [args.scene]
    else:
        scene_list = args.scenes

    # sort scenes by date (oldest → newest; displayed newest first in calendar)
    def scene_sort_key(name):
        t, _ = parse_scene_time(name)
        return t or 0
    scene_list = sorted(scene_list, key=scene_sort_key)

    conn = sqlite3.connect(args.db)

    scenes_data  = {}   # date8 → scene dict
    scene_order  = []   # date8 keys, newest first

    for scene_name in scene_list:
        date8 = scene_date_str(scene_name)
        print(f"\nLoading scene {date8} — {scene_name[:60]} ...")

        # resolve SAR overlay path
        if args.sar_overlay and len(scene_list) == 1:
            sar_path = args.sar_overlay
        elif args.sar_dir:
            sar_path = args.sar_dir / f"sar_overlay_{date8}.png"
        else:
            sar_path = None

        sc = load_scene(conn, scene_name, args.hours, args.thin, sar_path)
        if sc is None:
            continue

        sc["display_date"] = fmt_display_date(date8)

        scenes_data[date8] = sc
        scene_order.append(date8)

    if not scenes_data:
        raise SystemExit("No scenes with valid detections found — nothing to render.")

    # newest first in calendar
    scene_order.reverse()

    # embed into HTML
    scenes_json = json.dumps(scenes_data)
    order_json  = json.dumps(scene_order)

    html = HTML_TEMPLATE \
        .replace("__SCENES_JSON__",      scenes_json) \
        .replace("__SCENE_ORDER_JSON__", order_json)

    OUT_FILE.write_text(html, encoding="utf-8")
    size_kb = OUT_FILE.stat().st_size >> 10
    print(f"\nMap saved to {OUT_FILE.resolve()}  ({size_kb} KB)")
    print(f"  {len(scenes_data)} scene(s) embedded")
    print("Open ais_timeline_map.html in your browser.")


if __name__ == "__main__":
    main()
