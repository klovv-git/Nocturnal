#!/usr/bin/env python3
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
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

from config import DB_PATH, WEATHER_DB_PATH, TIMELINE_HOURS, THIN_MINUTES, BBOX_PAD, SAR_OVERLAYS_DIR, SENTINEL_DATA_DIR

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


def extract_abs_orbit(scene_name: str) -> str:
    """
    Extract the 6-digit absolute orbit number from a scene name.
    e.g. 'S1D_IW_GRDH_1SDV_20260512T061448_..._002747_...' → '002747'
    Falls back to scene_name itself so ungrouped scenes still work.
    """
    parts = scene_name.replace(".SAFE", "").split("_")
    if len(parts) >= 7:
        candidate = parts[6]
        if candidate.isdigit() and len(candidate) == 6:
            return candidate
    return scene_name


def find_sar_overlay(sar_dir: Path, scene_name: str):
    """
    Find SAR overlay PNG for a scene. Tries:
      1. sar_overlays/sar_overlay_20260512T061448.png  (new per-slice naming)
      2. sar_overlays/sar_overlay_20260512.png          (old date-only naming)
    Returns Path if found, else None.
    """
    if not sar_dir:
        return None
    # try timestamp-based name
    m = re.search(r'_(\d{8}T\d{6})_', scene_name)
    if m:
        p = sar_dir / f"sar_overlay_{m.group(1)}.png"
        if p.exists():
            return p
    # fall back to date-only name
    date8 = scene_date_str(scene_name)
    p = sar_dir / f"sar_overlay_{date8}.png"
    return p if p.exists() else None


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


# ── weather loading ───────────────────────────────────────────────────────

def load_weather_for_scene(t_lo, t_hi):
    """
    Query weather.db for observations in [t_lo, t_hi].
    Returns a compact list of arrays: [lat, lon, ts_epoch, wind_speed, wind_dir,
    wind_gust, wave_height, wave_dir, wave_period, visibility, air_temp, pressure,
    current_speed, current_dir]
    Returns [] if weather.db is missing or empty.
    """
    db = Path(str(WEATHER_DB_PATH))
    if not db.exists():
        return []
    try:
        wconn = sqlite3.connect(str(db))
        rows  = wconn.execute("""
            SELECT lat, lon, ts_epoch,
                   wind_speed, wind_dir, wind_gust,
                   wave_height, wave_dir, wave_period,
                   visibility, air_temp, pressure,
                   current_speed, current_dir
            FROM weather_obs
            WHERE ts_epoch BETWEEN ? AND ?
            ORDER BY ts_epoch
        """, (t_lo, t_hi)).fetchall()
        wconn.close()
        result = []
        for r in rows:
            result.append([
                round(r[0], 3), round(r[1], 3), int(r[2]),
                round(r[3], 2) if r[3] is not None else None,
                round(r[4], 1) if r[4] is not None else None,
                round(r[5], 2) if r[5] is not None else None,
                round(r[6], 2) if r[6] is not None else None,
                round(r[7], 1) if r[7] is not None else None,
                round(r[8], 1) if r[8] is not None else None,
                round(r[9], 1) if r[9] is not None else None,
                round(r[10], 1) if r[10] is not None else None,
                round(r[11], 1) if r[11] is not None else None,
                round(r[12], 2) if r[12] is not None else None,
                round(r[13], 1) if r[13] is not None else None,
            ])
        return result
    except Exception as e:
        print(f"  Weather DB load failed: {e}")
        return []


# ── per-pass data loading ─────────────────────────────────────────────────────

def load_pass(conn, scene_names: list, hours: float, thin_min: int,
              sar_dir: Path) -> dict:
    """
    Load and combine data for one or more scene slices from the same satellite pass.
    Returns a dict suitable for embedding in the SCENES JS variable, or None if
    no detections are found across any of the slices.
    """
    thin_sec = thin_min * 60

    # ── vessel info (load once) ───────────────────────────────────────────────
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

    # ── collect data across all slices ────────────────────────────────────────
    all_dark     = []
    all_matched  = []
    all_chips    = {}
    sar_overlays = []   # list of {"b64": ..., "bounds": {...}}
    t_lo_list, t_hi_list, t_mid_list = [], [], []
    all_lats, all_lons = [], []

    for scene_name in scene_names:
        t_start, t_end = parse_scene_time(scene_name)
        if not t_start:
            print(f"  Warning: cannot parse timestamps for {scene_name[:50]} — skipping.")
            continue

        t_mid = (t_start + t_end) / 2
        half  = (hours * 3600) / 2
        t_lo  = t_mid - half
        t_hi  = t_mid + half
        t_lo_list.append(t_lo)
        t_hi_list.append(t_hi)
        t_mid_list.append(t_mid)

        dets = conn.execute(
            """SELECT id, lat, lon, confidence, dark, matched_mmsi, match_dist_m
               FROM detections
               WHERE scene_name = ? AND lat IS NOT NULL AND dark IS NOT NULL""",
            (scene_name,)
        ).fetchall()

        dark    = [d for d in dets if d[4] == 1]
        matched = [d for d in dets if d[4] == 0]
        all_dark.extend(dark)
        all_matched.extend(matched)
        all_lats.extend(d[1] for d in dets)
        all_lons.extend(d[2] for d in dets)

        # chips
        date8     = scene_date_str(scene_name)
        chips_dir = Path(f"dark_chips_{date8}")
        for d in dark:
            det_id, lat, lon = d[0], d[1], d[2]
            fname = chips_dir / f"dark_{det_id:04d}_{lat:.4f}N_{lon:.4f}E.png"
            if fname.exists():
                all_chips[det_id] = base64.b64encode(fname.read_bytes()).decode("ascii")

        # SAR overlay for this slice
        sar_path = find_sar_overlay(sar_dir, scene_name)
        if sar_path:
            b64, bounds = load_sar_overlay(sar_path)
            if b64 and bounds:
                sar_overlays.append({"b64": b64, "bounds": bounds})
                print(f"  SAR overlay : {sar_path.name}  ({len(b64) >> 10} KB b64)")

        n_dark = len(dark); n_match = len(matched)
        print(f"  {scene_name[:55]}  →  {n_dark} dark  |  {n_match} matched")

    if not all_lats:
        print(f"  No detections found across {len(scene_names)} slice(s) — skipping pass.")
        return None

    # ── combined geometry ─────────────────────────────────────────────────────
    t_lo_combined  = min(t_lo_list)
    t_hi_combined  = max(t_hi_list)
    t_mid_combined = sum(t_mid_list) / len(t_mid_list)
    lat_min = min(all_lats) - BBOX_PAD
    lat_max = max(all_lats) + BBOX_PAD
    lon_min = min(all_lons) - BBOX_PAD
    lon_max = max(all_lons) + BBOX_PAD
    clat    = sum(all_lats) / len(all_lats)
    clon    = sum(all_lons) / len(all_lons)

    # ── AIS tracks (query combined bounding box + time window) ────────────────
    rows = conn.execute(
        """SELECT mmsi, lat, lon, ts_epoch
           FROM positions
           WHERE ts_epoch BETWEEN ? AND ?
             AND lat BETWEEN ? AND ?
             AND lon BETWEEN ? AND ?
           ORDER BY mmsi, ts_epoch""",
        (t_lo_combined, t_hi_combined, lat_min, lat_max, lon_min, lon_max)
    ).fetchall()

    tracks = {}
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

    date8 = scene_date_str(scene_names[0])
    n_slices = len(scene_names)

    detections_js = {
        "scene": f"{n_slices} slice(s)",
        "t_mid": t_mid_combined,
        "time":  fmt_utc(t_mid_combined),
        "dark":  [{"id": d[0], "lat": d[1], "lon": d[2],
                   "conf": round(d[3], 2)} for d in all_dark],
        "matched": [{"id": d[0], "lat": d[1], "lon": d[2],
                     "conf": round(d[3], 2), "mmsi": d[5],
                     "name": vessel_names.get(d[5])} for d in all_matched],
    }

    print(f"  → Combined: {len(tracks_clean)} AIS tracks  |  "
          f"{len(all_dark)} dark  |  {len(all_matched)} matched  |  "
          f"{len(all_chips)} chips  |  {len(sar_overlays)} SAR overlay(s)")

    return {
        "scenes":        scene_names,
        "date8":         date8,
        "t_lo":          t_lo_combined,
        "t_hi":          t_hi_combined,
        "t_mid":         t_mid_combined,
        "thin_sec":      thin_sec,
        "center":        [clat, clon],
        "tracks":        tracks_js,
        "detections":    detections_js,
        "chips":         all_chips,
        "sar_overlays":  sar_overlays,   # list of {b64, bounds} — one per slice
        "dark_count":    len(all_dark),
        "matched_count": len(all_matched),
        "n_slices":      n_slices,
        "pass_time":     fmt_utc(t_mid_combined),
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

    /* weather block inside vessel popup */
    .wx-block { margin-top: 10px; padding-top: 8px; border-top: 1px solid #e0e0e0; font-size: 12px; }
    .wx-block b { display: block; margin-bottom: 5px; color: #444; font-size: 13px; }
    .wx-row { display: flex; justify-content: space-between; padding: 2px 0; }
    .wx-row span:first-child { color: #888; padding-right: 12px; }
    .wx-row span:last-child  { font-weight: 500; text-align: right; }
    .wx-nodata { color: #aaa; font-style: italic; }
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
  var AOI_GEOJSON = __AOI_GEOJSON__;

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
  var sarLayers  = [];   // one imageOverlay per SAR slice

  // ── AOI boundary ───────────────────────────────────────────────────────────
  if (AOI_GEOJSON) {
    L.geoJSON(AOI_GEOJSON, {
      style: {
        color: '#4a7aff', weight: 2, opacity: 0.5,
        fill: true, fillColor: '#4a7aff', fillOpacity: 0.04,
        dashArray: '6 5'
      }
    }).addTo(map);
  }

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
  //  Weather helpers
  // ════════════════════════════════════════════════════════════════════════════
  // obs row: [lat, lon, ts_epoch, wind_speed, wind_dir, wind_gust,
  //           wave_height, wave_dir, wave_period,
  //           visibility, air_temp, pressure, current_speed, current_dir]
  function nearestWx(lat, lon, t, obs) {
    if (!obs || !obs.length) return null;
    var best = null, bestScore = 1e9;
    for (var i = 0; i < obs.length; i++) {
      var o = obs[i];
      var dt = Math.abs(o[2] - t);
      if (dt > 7200) continue;  // ignore obs more than 2h away
      var dlat = o[0] - lat, dlon = o[1] - lon;
      var distKm = Math.sqrt(dlat*dlat + dlon*dlon) * 111;
      var score = distKm + dt / 3600;
      if (score < bestScore) { bestScore = score; best = o; }
    }
    return best;
  }

  function wxHtml(w) {
    function f(v, unit, dec) {
      return (v != null) ? (+v).toFixed(dec || 0) + unit : '—';
    }
    function dir(d) {
      if (d == null) return '';
      return ' ' + ['N','NE','E','SE','S','SW','W','NW'][Math.round(d / 45) % 8];
    }
    if (!w) {
      return '<div class="wx-block"><b>⛅ Weather</b>' +
             '<span class="wx-nodata">No weather data near this position</span></div>';
    }
    var windStr = f(w[3], ' m/s', 1) + dir(w[4]);
    if (w[5] != null) windStr += ' · gust ' + f(w[5], ' m/s', 1);
    var waveStr = f(w[6], 'm', 1) + dir(w[7]);
    if (w[8] != null) waveStr += ' · ' + f(w[8], 's', 0) + ' period';
    var rows = [
      ['Wind',       windStr],
      ['Waves',      waveStr],
      ['Visibility', f(w[9],  'km', 0)],
      ['Air temp',   f(w[10], '°C', 1)],
      ['Pressure',   f(w[11], ' hPa', 0)],
    ];
    if (w[12] != null) rows.push(['Current', f(w[12], ' m/s', 1) + dir(w[13])]);
    var html = '<div class="wx-block"><b>⛅ Weather at vessel</b>';
    rows.forEach(function(r) {
      html += '<div class="wx-row"><span>' + r[0] + '</span><span>' + r[1] + '</span></div>';
    });
    return html + '</div>';
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
      var baseLabel = '<b>' + (t.name || 'Unknown vessel') + '</b><br>' +
        'MMSI: <a href="https://www.marinetraffic.com/en/ais/details/ships/mmsi:' +
        t.mmsi + '" target="_blank">' + t.mmsi + ' ↗</a>';
      if (t.ship_type) baseLabel += '<br>Type: ' + t.ship_type;
      if (t.callsign)  baseLabel += '<br>Callsign: ' + t.callsign;
      if (t.imo)       baseLabel += '<br>IMO: ' + t.imo;
      marker.bindPopup(baseLabel, {maxWidth: 320});
      marker.on('click', (function(mmsi) {
        return function() { selectVessel(mmsi); };
      })(t.mmsi));
      vesselMarkers[t.mmsi] = { marker: marker, pings: t.pings, baseLabel: baseLabel };

      var trail = L.polyline([], {color:'#3498db', weight:1.5, opacity:0.4});
      vesselTrails[t.mmsi] = trail;
    });
  }

  function selectVessel(mmsi) {
    selectedMmsi = (selectedMmsi === mmsi) ? null : mmsi;
    update();
    if (selectedMmsi && vesselMarkers[mmsi]) {
      var data = vesselMarkers[mmsi];
      var pos  = getPos(data.pings, currentTime());
      var sc   = SCENES[activeDateKey];
      var wx   = pos ? nearestWx(pos[0], pos[1], currentTime(), sc.weather || []) : null;
      data.marker.setPopupContent(data.baseLabel + wxHtml(wx));
      data.marker.openPopup();
    }
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
    sarLayers.forEach(function(l) { l.setOpacity(sarOpacity); });
  });
  document.getElementById('tog-sar').addEventListener('change', function() {
    var show = this.checked;
    sarLayers.forEach(function(l) {
      if (show) map.addLayer(l); else map.removeLayer(l);
    });
  });

  function updateSarOverlay(sc) {
    // remove old SAR layers
    sarLayers.forEach(function(l) { map.removeLayer(l); });
    sarLayers = [];
    var show = document.getElementById('tog-sar').checked;
    (sc.sar_overlays || []).forEach(function(ov) {
      var b = ov.bounds;
      var layer = L.imageOverlay(
        'data:image/png;base64,' + ov.b64,
        [[b.lat_min, b.lon_min], [b.lat_max, b.lon_max]],
        {opacity: sarOpacity}
      );
      sarLayers.push(layer);
      if (show) layer.addTo(map);
    });
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

    var sliceBadge = sc.n_slices > 1
      ? '<span class="cal-badge" style="background:#8e44ad22;color:#8e44ad;border:1px solid #8e44ad55">' + sc.n_slices + ' slices</span>'
      : '';

    card.innerHTML =
      '<div class="cal-date">' + dateLabel + '</div>' +
      '<div class="cal-time">📡 ' + timeLabel2 + '</div>' +
      '<div class="cal-stats">' +
        '<span class="cal-badge badge-dark">' + sc.dark_count + ' dark</span>' +
        '<span class="cal-badge badge-matched">' + sc.matched_count + ' matched</span>' +
        sarBadge + sliceBadge +
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

    # accept --scene / --scenes / --all
    scene_group = ap.add_mutually_exclusive_group(required=True)
    scene_group.add_argument("--scene",  help="Single scene (backward-compatible)")
    scene_group.add_argument("--scenes", nargs="+",
                             help="One or more scene SAFE folder names")
    scene_group.add_argument("--all", action="store_true", dest="all_scenes",
                             help="Auto-include every .SAFE folder in sentinel_data/")

    ap.add_argument("--db",       default=str(DEFAULT_DB))
    ap.add_argument("--hours",    type=float, default=TIMELINE_HOURS,
                    help="Time window to show per scene (hours, centred on pass)")
    ap.add_argument("--thin",     type=int, default=THIN_MINUTES,
                    help="Thin AIS pings to one per vessel per N minutes")

    # SAR overlay: single path (old) or directory (new multi-scene auto-detect)
    sar_group = ap.add_mutually_exclusive_group()
    sar_group.add_argument("--sar-overlay", type=Path,
                           help="Single SAR overlay PNG (single-scene mode)")
    sar_group.add_argument("--sar-dir",     type=Path, default=SAR_OVERLAYS_DIR,
                           help=f"Directory to auto-detect sar_overlay_YYYYMMDD.png files (default: {SAR_OVERLAYS_DIR})")

    args = ap.parse_args()

    # normalise to a list of scene names
    if args.all_scenes:
        # only include scenes that have a SAR overlay — guarantees they were processed
        scene_list = []
        for p in sorted(SENTINEL_DATA_DIR.glob("*.SAFE")):
            m = re.search(r'_(\d{8}T\d{6})_', p.name)
            if m:
                overlay = SAR_OVERLAYS_DIR / f"sar_overlay_{m.group(1)}.png"
                if overlay.exists():
                    scene_list.append(p.name)
        if not scene_list:
            raise SystemExit(f"No processed scenes found in {SENTINEL_DATA_DIR} — run the pipeline first.")
        print(f"--all: {len(scene_list)} processed scene(s) found")
    elif args.scene:
        scene_list = [args.scene]
    else:
        scene_list = args.scenes

    # sort scenes by date (oldest → newest; displayed newest first in calendar)
    def scene_sort_key(name):
        t, _ = parse_scene_time(name)
        return t or 0
    scene_list = sorted(scene_list, key=scene_sort_key)

    conn = sqlite3.connect(args.db)

    # ── group scenes by absolute orbit (same orbit = same satellite pass) ─────
    from collections import defaultdict
    orbit_groups = defaultdict(list)   # orbit_key → [scene_name, ...]

    for scene_name in scene_list:
        orbit = extract_abs_orbit(scene_name)
        orbit_groups[orbit].append(scene_name)

    # sort slices within each pass by acquisition time (oldest first = southernmost)
    for orbit in orbit_groups:
        orbit_groups[orbit].sort(key=lambda n: parse_scene_time(n)[0] or 0)

    # sort passes by time (oldest first, reversed later for newest-first calendar)
    sorted_orbits = sorted(orbit_groups.keys(),
                           key=lambda o: parse_scene_time(orbit_groups[o][0])[0] or 0)

    scenes_data = {}   # orbit_key → pass dict
    scene_order = []   # orbit keys, newest first

    sar_dir_resolved = args.sar_dir if not args.sar_overlay else None

    for orbit in sorted_orbits:
        slices = orbit_groups[orbit]
        date8  = scene_date_str(slices[0])
        n      = len(slices)
        print(f"\nLoading pass orbit={orbit}  ({n} slice(s), {date8}) ...")

        # single-scene backward compat: use explicit --sar-overlay if only one scene total
        if args.sar_overlay and len(scene_list) == 1:
            sar_dir_resolved = args.sar_overlay.parent

        sc = load_pass(conn, slices, args.hours, args.thin, sar_dir_resolved)
        if sc is None:
            continue

        sc["display_date"] = fmt_display_date(date8)
        sc["orbit"]        = orbit
        sc["weather"]      = load_weather_for_scene(sc["t_lo"], sc["t_hi"])
        if sc["weather"]:
            print(f"  Weather obs  : {len(sc['weather'])} rows from weather.db")

        scenes_data[orbit] = sc
        scene_order.append(orbit)

    if not scenes_data:
        raise SystemExit("No passes with valid detections found — nothing to render.")

    # newest first in calendar
    scene_order.reverse()

    # embed into HTML
    scenes_json = json.dumps(scenes_data)
    order_json  = json.dumps(scene_order)

    # load AOI GeoJSON if available
    _aoi_file = Path(__file__).parent / "aoi.geojson"
    aoi_json  = _aoi_file.read_text(encoding="utf-8") if _aoi_file.exists() else "null"

    html = HTML_TEMPLATE \
        .replace("__SCENES_JSON__",      scenes_json) \
        .replace("__SCENE_ORDER_JSON__", order_json) \
        .replace("__AOI_GEOJSON__",      aoi_json)

    OUT_FILE.write_text(html, encoding="utf-8")
    size_kb = OUT_FILE.stat().st_size >> 10
    print(f"\nMap saved to {OUT_FILE.resolve()}  ({size_kb} KB)")
    print(f"  {len(scenes_data)} scene(s) embedded")
    print("Open ais_timeline_map.html in your browser.")


if __name__ == "__main__":
    main()
