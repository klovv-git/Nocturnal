#!/usr/bin/env python3
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
"""
weather_map.py — Clickable weather forecast map for NOCTURNAL.

Click anywhere inside the AOI to see the full hourly forecast for the
nearest Storm Glass grid point.  The time slider scrubs the 48-hour
window stored in weather.db.

Usage:
    python weather_map.py [--db weather.db] [--out weather_map.html]
"""

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config as _cfg

DEFAULT_DB  = Path(str(_cfg.WEATHER_DB_PATH))
AOI_FILE    = Path(__file__).parent / "aoi.geojson"
DEFAULT_OUT = Path("weather_map.html")


# ── data loading ───────────────────────────────────────────────────────────────

def _r(v, d=1):
    return round(float(v), d) if v is not None else None


def load_data(db_path: Path) -> dict:
    """Read all weather/bio/tide data from weather.db, grouped by grid point."""
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}\nRun the daemon first.")

    conn = sqlite3.connect(str(db_path))

    pts = conn.execute(
        "SELECT DISTINCT lat, lon FROM weather_obs ORDER BY lat, lon"
    ).fetchall()

    if not pts:
        conn.close()
        raise SystemExit("No weather data in database — run the daemon first.")

    t_min, t_max = float("inf"), float("-inf")
    grid = []

    for i, (lat, lon) in enumerate(pts):
        wx_rows = conn.execute("""
            SELECT ts_epoch,
                   wind_speed, wind_dir, wind_gust,
                   wave_height, wave_dir, wave_period,
                   swell_height, swell_dir, swell_period,
                   visibility, cloud_cover, air_temp, pressure,
                   current_speed, current_dir,
                   precip, humidity, sea_level
            FROM weather_obs WHERE lat=? AND lon=? ORDER BY ts_epoch
        """, (lat, lon)).fetchall()

        bio_rows = conn.execute("""
            SELECT ts_epoch, chlorophyll, phytoplankton, salinity,
                   water_temp, ice_cover, oxygen, nitrate, phosphate, silicate
            FROM bio_obs WHERE lat=? AND lon=? ORDER BY ts_epoch
        """, (lat, lon)).fetchall()

        tide_rows = conn.execute("""
            SELECT ts_epoch, height FROM tide_obs
            WHERE lat=? AND lon=? ORDER BY ts_epoch
        """, (lat, lon)).fetchall()

        extr_rows = conn.execute("""
            SELECT ts_epoch, height, type FROM tide_extremes
            WHERE lat=? AND lon=? ORDER BY ts_epoch
        """, (lat, lon)).fetchall()

        wx = []
        for r in wx_rows:
            ts = int(r[0])
            if ts < t_min: t_min = ts
            if ts > t_max: t_max = ts
            wx.append([ts,
                _r(r[1],1), _r(r[2],0), _r(r[3],1),     # wind spd/dir/gust
                _r(r[4],2), _r(r[5],0), _r(r[6],1),     # wave h/dir/period
                _r(r[7],2), _r(r[8],0), _r(r[9],1),     # swell h/dir/period
                _r(r[10],0), _r(r[11],0), _r(r[12],1),  # vis / cloud / temp
                _r(r[13],0),                              # pressure
                _r(r[14],2), _r(r[15],0),                # curr spd/dir
                _r(r[16],2), _r(r[17],0),                # precip / humidity
            ])

        bio = [[int(r[0]),
                _r(r[1],3), _r(r[2],1), _r(r[3],1),    # chloro / phyto / sal
                _r(r[4],1), _r(r[5],3), _r(r[6],1),    # wtemp / ice / O2
                _r(r[7],2), _r(r[8],3), _r(r[9],2)]    # nitrate / phos / silicate
               for r in bio_rows]

        tide = [[int(r[0]), _r(r[1],2)] for r in tide_rows]
        extr = [[int(r[0]), _r(r[1],2), r[2]] for r in extr_rows]

        grid.append({
            "n":   f"h{i:02d}",
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "wx":  wx,
            "bio": bio,
            "tide": tide,
            "extr": extr,
        })

    conn.close()

    now_ts = int(datetime.now(timezone.utc).replace(
        minute=0, second=0, microsecond=0).timestamp())

    print(f"  {len(grid)} grid points loaded")
    print(f"  Time range: {datetime.utcfromtimestamp(t_min).strftime('%Y-%m-%d %H:%M')} "
          f"→ {datetime.utcfromtimestamp(t_max).strftime('%Y-%m-%d %H:%M')} UTC")

    return {"pts": grid, "t_min": int(t_min), "t_max": int(t_max), "now": now_ts}


# ── HTML template ──────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>NOCTURNAL — Weather Map</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: Arial, sans-serif; display: flex; flex-direction: column;
           height: 100vh; overflow: hidden; background: #0d0d1f; }

    /* ── layout ── */
    #top { display: flex; flex: 1; min-height: 0; }

    /* ── left panel ── */
    #info-panel {
      width: 290px; min-width: 290px;
      background: #12122a; color: #dde;
      display: flex; flex-direction: column;
      overflow: hidden; flex-shrink: 0;
      border-right: 2px solid #1e1e40;
    }
    #panel-header {
      padding: 14px 16px 10px;
      font-size: 11px; font-weight: bold; letter-spacing: 1.5px;
      color: #8899cc; text-transform: uppercase;
      border-bottom: 1px solid #1e1e40; flex-shrink: 0;
      display: flex; align-items: center; gap: 8px;
    }
    #panel-header span { font-size: 17px; }
    #point-info {
      padding: 10px 16px; border-bottom: 1px solid #1e1e40; flex-shrink: 0;
    }
    #point-name  { font-size: 15px; font-weight: bold; color: #eef; margin-bottom: 2px; }
    #point-coords{ font-size: 11px; color: #8899cc; }
    #click-hint  { font-size: 12px; color: #556688; font-style: italic; line-height: 1.5; }

    /* ── tabs ── */
    #tab-bar {
      display: flex; flex-shrink: 0; background: #0f0f24;
      border-bottom: 1px solid #1e1e40;
    }
    .tab-btn {
      flex: 1; padding: 8px 0; background: none; border: none;
      color: #8899cc; font-size: 10px; cursor: pointer;
      border-bottom: 2px solid transparent; letter-spacing: 0.8px;
      text-transform: uppercase; transition: color 0.15s;
    }
    .tab-btn.active { color: #e2b714; border-bottom-color: #e2b714; background: #12122a; }
    .tab-btn:hover:not(.active) { color: #aabbd0; }

    /* ── entry list ── */
    #entry-list {
      flex: 1; overflow-y: auto; font-size: 11px;
    }
    #entry-list::-webkit-scrollbar { width: 4px; }
    #entry-list::-webkit-scrollbar-track { background: #12122a; }
    #entry-list::-webkit-scrollbar-thumb { background: #2a2a54; border-radius: 2px; }

    .entry-row {
      padding: 9px 16px 9px;
      border-bottom: 1px solid #191932;
      cursor: pointer; transition: background 0.1s;
    }
    .entry-row:hover { background: #1a1a3a; }
    .entry-row.current {
      background: #001848; border-left: 3px solid #e2b714; padding-left: 13px;
    }
    .entry-time { font-size: 12px; font-weight: bold; color: #7a8faa; margin-bottom: 5px; }
    .entry-row.current .entry-time { color: #e2b714; }

    .ef-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 10px; }
    .ef { display: flex; flex-direction: column; }
    .ef-label { font-size: 9px; color: #445566; text-transform: uppercase;
                letter-spacing: 0.5px; margin-bottom: 1px; }
    .ef-val   { font-size: 11px; color: #b8c8dd; }
    .ef-val.hi{ color: #e74c3c; font-weight: bold; }
    .ef-val.lo{ color: #3498db; font-weight: bold; }

    .extreme-event {
      padding: 7px 16px; border-bottom: 1px solid #191932;
      display: flex; align-items: center; gap: 10px;
    }
    .ext-badge {
      font-size: 10px; font-weight: bold; padding: 2px 7px; border-radius: 10px;
    }
    .ext-high { background: #e74c3c22; color: #e74c3c; border: 1px solid #e74c3c55; }
    .ext-low  { background: #3498db22; color: #3498db; border: 1px solid #3498db55; }
    .ext-time { font-size: 11px; color: #8899cc; }
    .ext-ht   { font-size: 11px; color: #ccd; margin-left: auto; }

    .section-hdr {
      padding: 7px 16px; font-size: 9px; font-weight: bold;
      letter-spacing: 1px; text-transform: uppercase;
      color: #445566; background: #0f0f24; border-bottom: 1px solid #1e1e40;
    }

    /* sparkline container */
    .spark-wrap { padding: 10px 16px 6px; border-bottom: 1px solid #1e1e40; }
    .spark-wrap svg { display: block; width: 100%; }
    .spark-labels { display: flex; justify-content: space-between;
                    font-size: 9px; color: #445566; margin-top: 3px; }

    /* ── map ── */
    #map-container { flex: 1; position: relative; min-width: 0; }
    #map { width: 100%; height: 100%; cursor: crosshair; }

    #title {
      position: absolute; top: 10px; left: 50%; transform: translateX(-50%);
      z-index: 1000; background: white; padding: 7px 18px;
      border-radius: 6px; box-shadow: 0 2px 8px rgba(0,0,0,0.3);
      font-size: 14px; font-weight: bold; white-space: nowrap;
    }

    #map-hint {
      position: absolute; bottom: 14px; left: 50%; transform: translateX(-50%);
      z-index: 1000; background: rgba(18,18,42,0.88); color: #8899cc;
      padding: 6px 14px; border-radius: 20px; font-size: 12px;
      pointer-events: none; white-space: nowrap;
      border: 1px solid #2a2a54;
    }

    /* ── timeline bar ── */
    #timeline-bar {
      background: #1a1a2e; color: #eee; padding: 10px 16px;
      display: flex; align-items: center; gap: 12px; flex-shrink: 0;
      user-select: none; border-top: 1px solid #1e1e40;
    }
    #time-label { font-size: 13px; white-space: nowrap; min-width: 192px; }
    #timeline-wrap { position: relative; flex: 1; height: 36px;
                     display: flex; align-items: center; }
    #timeline-slider { width: 100%; cursor: pointer; accent-color: #e74c3c; }
    #now-marker {
      position: absolute; top: 0; height: 100%; pointer-events: none;
      display: flex; flex-direction: column; align-items: center;
    }
    .now-line  { width: 2px; background: #e2b714; flex: 1; opacity: 0.7; }
    .now-label { font-size: 9px; color: #e2b714; white-space: nowrap; }
  </style>
</head>
<body>
<div id="top">

  <!-- ── Left panel ── -->
  <div id="info-panel">
    <div id="panel-header"><span>⛅</span>WEATHER EXPLORER</div>

    <div id="point-info">
      <div id="point-name"></div>
      <div id="point-coords"></div>
      <div id="click-hint">Click anywhere on the map<br>to load weather data.</div>
    </div>

    <div id="tab-bar" style="display:none">
      <button class="tab-btn active" data-tab="wx">Weather</button>
      <button class="tab-btn"        data-tab="bio">Biology</button>
      <button class="tab-btn"        data-tab="tide">Tides</button>
    </div>

    <div id="entry-list"></div>
  </div>

  <!-- ── Map ── -->
  <div id="map-container">
    <div id="title">NOCTURNAL — Weather</div>
    <div id="map"></div>
    <div id="map-hint">Click anywhere inside the AOI to explore weather data</div>
  </div>

</div>

<!-- ── Timeline bar ── -->
<div id="timeline-bar">
  <div id="time-label">--</div>
  <div id="timeline-wrap">
    <input type="range" id="timeline-slider" min="0" max="1000" value="0">
    <div id="now-marker" style="display:none">
      <div class="now-line"></div>
      <div class="now-label">NOW</div>
    </div>
  </div>
</div>

<script>
// ════════════════════════════════════════════════════════════════════════
//  Injected data
// ════════════════════════════════════════════════════════════════════════
var DATA        = __DATA_JSON__;
var AOI_GEOJSON = __AOI_GEOJSON__;

// wx row indices
var I_TS=0, I_WS=1, I_WD=2, I_WG=3,
    I_WH=4, I_WDR=5, I_WP=6,
    I_SH=7, I_SDR=8, I_SP=9,
    I_VIS=10, I_CC=11, I_AT=12, I_PR=13,
    I_CS=14, I_CDR=15, I_PREC=16, I_HUM=17;

// bio row indices
var B_TS=0, B_CHL=1, B_PHY=2, B_SAL=3,
    B_WT=4,  B_ICE=5, B_O2=6,
    B_NO3=7, B_PO4=8, B_SIO=9;

// ════════════════════════════════════════════════════════════════════════
//  Map init
// ════════════════════════════════════════════════════════════════════════
var centerLat = DATA.pts.reduce(function(s,p){ return s+p.lat; }, 0) / DATA.pts.length;
var centerLon = DATA.pts.reduce(function(s,p){ return s+p.lon; }, 0) / DATA.pts.length;

var map = L.map('map', {zoomControl: true}).setView([centerLat, centerLon], 8);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors', maxZoom: 18
}).addTo(map);

// AOI boundary
if (AOI_GEOJSON) {
  L.geoJSON(AOI_GEOJSON, {
    style: {
      color: '#4a7aff', weight: 2, opacity: 0.5,
      fill: true, fillColor: '#4a7aff', fillOpacity: 0.04,
      dashArray: '6 5'
    }
  }).addTo(map);
}

// ════════════════════════════════════════════════════════════════════════
//  Grid point dots
// ════════════════════════════════════════════════════════════════════════
var gridDots = {};   // name → L.circleMarker
DATA.pts.forEach(function(p) {
  var dot = L.circleMarker([p.lat, p.lon], {
    radius: 4, color: '#4a7aff', fillColor: '#4a7aff',
    fillOpacity: 0.55, weight: 1, interactive: false
  }).addTo(map);
  gridDots[p.n] = dot;
});

// ════════════════════════════════════════════════════════════════════════
//  State
// ════════════════════════════════════════════════════════════════════════
var selectedPoint = null;
var clickMarker   = null;
var activeTab     = 'wx';

var slider    = document.getElementById('timeline-slider');
var timeLabel = document.getElementById('time-label');

// ════════════════════════════════════════════════════════════════════════
//  Helpers
// ════════════════════════════════════════════════════════════════════════
var COMPASS16 = ['N','NNE','NE','ENE','E','ESE','SE','SSE',
                 'S','SSW','SW','WSW','W','WNW','NW','NNW'];

function compassDir(deg) {
  if (deg == null) return '';
  return COMPASS16[Math.round(deg / 22.5) % 16];
}

function fmtTs(ts, opts) {
  var d = new Date(ts * 1000);
  var date = d.getUTCFullYear() + '-' +
    String(d.getUTCMonth()+1).padStart(2,'0') + '-' +
    String(d.getUTCDate()).padStart(2,'0');
  var time = String(d.getUTCHours()).padStart(2,'0') + ':' +
    String(d.getUTCMinutes()).padStart(2,'0');
  if (opts && opts.dateOnly) return date;
  if (opts && opts.timeOnly) return time + ' UTC';
  return date + '  ' + time + ' UTC';
}

function fmtDate(ts) {
  var d = new Date(ts * 1000);
  var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return days[d.getUTCDay()] + ' ' + d.getUTCDate() + ' ' + months[d.getUTCMonth()];
}

function currentTs() {
  return DATA.t_min + (slider.value / 1000) * (DATA.t_max - DATA.t_min);
}

function tsToSlider(ts) {
  return Math.round((ts - DATA.t_min) / (DATA.t_max - DATA.t_min) * 1000);
}

function v(val, unit, dec) {
  if (val == null) return '—';
  return (+val).toFixed(dec != null ? dec : 0) + (unit || '');
}

function ef(label, value) {
  return '<div class="ef"><div class="ef-label">' + label + '</div>' +
         '<div class="ef-val">' + value + '</div></div>';
}

// nearest row in a sorted array (by ts index 0) to target ts
function nearestRow(rows, ts) {
  if (!rows || !rows.length) return null;
  var best = rows[0], bestDiff = Math.abs(rows[0][0] - ts);
  for (var i = 1; i < rows.length; i++) {
    var d = Math.abs(rows[i][0] - ts);
    if (d < bestDiff) { bestDiff = d; best = rows[i]; }
    if (rows[i][0] > ts + 7200) break; // past 2h ahead, stop
  }
  return best;
}

function nearestPoint(lat, lon) {
  var best = null, bestD2 = Infinity;
  DATA.pts.forEach(function(p) {
    var dlat = p.lat - lat;
    var dlon = (p.lon - lon) * Math.cos(lat * Math.PI / 180);
    var d2 = dlat*dlat + dlon*dlon;
    if (d2 < bestD2) { bestD2 = d2; best = p; }
  });
  return best;
}

// ════════════════════════════════════════════════════════════════════════
//  Tab: WEATHER
// ════════════════════════════════════════════════════════════════════════
function buildWxTab(point) {
  if (!point.wx.length) return '<div class="entry-row"><i style="color:#556688">No weather data</i></div>';

  // Build tide lookup by ts for sea level
  var tideMap = {};
  point.tide.forEach(function(r) { tideMap[r[0]] = r[1]; });

  // Merge wx rows and extreme events, sorted by ts
  var items = [];
  point.wx.forEach(function(r)  { items.push({ts: r[0], type:'wx',    r: r}); });
  point.extr.forEach(function(r){ items.push({ts: r[0], type:'extr',  r: r}); });
  items.sort(function(a,b){ return a.ts - b.ts; });

  var html = '';
  var lastDate = '';

  items.forEach(function(item) {
    var dateStr = fmtDate(item.ts);

    if (dateStr !== lastDate) {
      html += '<div class="section-hdr">' + dateStr + '</div>';
      lastDate = dateStr;
    }

    if (item.type === 'extr') {
      var cls = item.r[2] === 'high' ? 'ext-high' : 'ext-low';
      var lbl = item.r[2] === 'high' ? 'HIGH TIDE' : 'LOW TIDE';
      html += '<div class="extreme-event">' +
        '<span class="ext-badge ' + cls + '">' + lbl + '</span>' +
        '<span class="ext-time">' + fmtTs(item.ts, {timeOnly:true}) + '</span>' +
        '<span class="ext-ht">' + v(item.r[1], 'm', 2) + '</span>' +
      '</div>';
      return;
    }

    var r = item.r;
    var ts = r[I_TS];
    var tideH = tideMap[ts];

    // Wind
    var windVal = v(r[I_WS], ' m/s', 1);
    if (r[I_WD] != null) windVal += '  ' + compassDir(r[I_WD]);
    if (r[I_WG] != null) windVal += '  g' + v(r[I_WG], '', 1);

    // Waves
    var waveVal = v(r[I_WH], 'm', 2);
    if (r[I_WDR] != null) waveVal += '  ' + compassDir(r[I_WDR]);
    if (r[I_WP]  != null) waveVal += '  ' + v(r[I_WP], 's', 0);

    // Swell
    var swellVal = v(r[I_SH], 'm', 2);
    if (r[I_SDR] != null) swellVal += '  ' + compassDir(r[I_SDR]);
    if (r[I_SP]  != null) swellVal += '  ' + v(r[I_SP], 's', 0);

    // Tide / sea level
    var tideVal = tideH != null ? v(tideH, 'm', 2) : (r[17] != null ? v(r[17], 'm', 2) : '—');

    // Other
    var tempVal   = v(r[I_AT], '°C', 1);
    var pressVal  = v(r[I_PR], ' hPa', 0);
    var visVal    = v(r[I_VIS], ' km', 0);
    var cloudVal  = v(r[I_CC], '%', 0);
    var humVal    = v(r[I_HUM], '%', 0);
    var precipVal = r[I_PREC] != null ? v(r[I_PREC], ' mm/h', 2) : null;
    var currVal   = r[I_CS]  != null
      ? v(r[I_CS], ' m/s', 2) + (r[I_CDR] != null ? '  ' + compassDir(r[I_CDR]) : '')
      : null;

    html += '<div class="entry-row" data-ts="' + ts + '">' +
      '<div class="entry-time">' + fmtTs(ts, {timeOnly:true}) + '</div>' +
      '<div class="ef-grid">' +
        ef('Wind',  windVal) +
        ef('Waves', waveVal) +
        ef('Swell', swellVal) +
        ef('Tide',  tideVal) +
        ef('Air temp', tempVal) +
        ef('Pressure', pressVal) +
        ef('Visibility', visVal) +
        ef('Cloud', cloudVal) +
        (humVal    ? ef('Humidity',    humVal)    : '') +
        (precipVal ? ef('Precip',      precipVal) : '') +
        (currVal   ? ef('Current',     currVal)   : '') +
      '</div>' +
    '</div>';
  });

  return html;
}

// ════════════════════════════════════════════════════════════════════════
//  Tab: BIOLOGY
// ════════════════════════════════════════════════════════════════════════
function buildBioTab(point) {
  if (!point.bio.length) return '<div class="entry-row"><i style="color:#556688">No bio data</i></div>';

  var html = '';
  var lastDate = '';

  point.bio.forEach(function(r) {
    var ts = r[B_TS];
    var dateStr = fmtDate(ts);
    if (dateStr !== lastDate) {
      html += '<div class="section-hdr">' + dateStr + '</div>';
      lastDate = dateStr;
    }

    html += '<div class="entry-row" data-ts="' + ts + '">' +
      '<div class="entry-time">' + fmtTs(ts, {timeOnly:true}) + '</div>' +
      '<div class="ef-grid">' +
        ef('Water temp',   v(r[B_WT],  '°C',   1)) +
        ef('Salinity',     v(r[B_SAL], ' PSU', 1)) +
        ef('Chlorophyll',  v(r[B_CHL], ' mg/m³', 3)) +
        ef('Oxygen',       v(r[B_O2],  ' mL/L',  1)) +
        ef('Phytoplankton',v(r[B_PHY], '',     1)) +
        ef('Nitrate',      v(r[B_NO3], ' μM',  2)) +
        ef('Phosphate',    v(r[B_PO4], ' μM',  3)) +
        ef('Silicate',     v(r[B_SIO], ' μM',  2)) +
        (r[B_ICE] != null ? ef('Ice cover', v(r[B_ICE],'', 3)) : '') +
      '</div>' +
    '</div>';
  });

  return html;
}

// ════════════════════════════════════════════════════════════════════════
//  Tab: TIDES — sparkline + extremes + hourly sea level
// ════════════════════════════════════════════════════════════════════════
function buildTideTab(point) {
  var html = '';

  // ── sparkline ──────────────────────────────────────────────────────
  if (point.tide.length > 1) {
    var heights = point.tide.map(function(r){ return r[1]; })
                            .filter(function(h){ return h != null; });
    var minH = Math.min.apply(null, heights);
    var maxH = Math.max.apply(null, heights);
    var rng  = maxH - minH || 1;

    var W = 258, H = 60, PAD = 4;
    var n = point.tide.length;

    var pts = point.tide.map(function(r, i) {
      var x = PAD + (i / (n - 1)) * (W - 2*PAD);
      var y = H - PAD - (((r[1] || minH) - minH) / rng) * (H - 2*PAD);
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');

    // Current-time cursor line
    var nowTs  = currentTs();
    var tMin   = point.tide[0][0], tMax = point.tide[n-1][0];
    var nowFrac = Math.max(0, Math.min(1, (nowTs - tMin) / (tMax - tMin)));
    var nowX   = (PAD + nowFrac * (W - 2*PAD)).toFixed(1);

    html += '<div class="spark-wrap">' +
      '<svg viewBox="0 0 ' + W + ' ' + H + '" xmlns="http://www.w3.org/2000/svg">' +
        '<polyline points="' + pts + '" stroke="#3498db" stroke-width="1.5" fill="none"/>' +
        '<line x1="' + nowX + '" y1="0" x2="' + nowX + '" y2="' + H + '" ' +
              'stroke="#e74c3c" stroke-width="1.5" stroke-dasharray="3 2" opacity="0.8"/>' +
      '</svg>' +
      '<div class="spark-labels">' +
        '<span>' + v(minH, 'm', 2) + '</span>' +
        '<span>Tidal height</span>' +
        '<span>' + v(maxH, 'm', 2) + '</span>' +
      '</div>' +
    '</div>';
  }

  // ── high / low events ──────────────────────────────────────────────
  if (point.extr.length) {
    html += '<div class="section-hdr">High / Low Events</div>';
    point.extr.forEach(function(r) {
      var cls = r[2] === 'high' ? 'ext-high' : 'ext-low';
      var lbl = r[2] === 'high' ? 'HIGH' : 'LOW';
      html += '<div class="extreme-event">' +
        '<span class="ext-badge ' + cls + '">' + lbl + '</span>' +
        '<span class="ext-time">' + fmtTs(r[0]) + '</span>' +
        '<span class="ext-ht">' + v(r[1], 'm', 2) + '</span>' +
      '</div>';
    });
  }

  // ── hourly sea level ───────────────────────────────────────────────
  if (point.tide.length) {
    html += '<div class="section-hdr">Hourly Sea Level</div>';
    var lastDate = '';
    point.tide.forEach(function(r) {
      var ts = r[0];
      var dateStr = fmtDate(ts);
      if (dateStr !== lastDate) {
        html += '<div class="section-hdr" style="color:#334;">' + dateStr + '</div>';
        lastDate = dateStr;
      }
      html += '<div class="entry-row" data-ts="' + ts + '">' +
        '<div class="entry-time">' + fmtTs(ts, {timeOnly:true}) + '</div>' +
        '<div class="ef-grid">' + ef('Height', v(r[1], 'm', 2)) + '</div>' +
      '</div>';
    });
  }

  if (!html) return '<div class="entry-row"><i style="color:#556688">No tide data</i></div>';
  return html;
}

// ════════════════════════════════════════════════════════════════════════
//  Render sidebar for a point + tab
// ════════════════════════════════════════════════════════════════════════
function renderTab(point, tab) {
  var list = document.getElementById('entry-list');
  if (tab === 'bio')  list.innerHTML = buildBioTab(point);
  else if (tab === 'tide') list.innerHTML = buildTideTab(point);
  else                list.innerHTML = buildWxTab(point);

  // wire up row clicks → jump slider
  list.querySelectorAll('.entry-row[data-ts]').forEach(function(row) {
    row.addEventListener('click', function() {
      slider.value = tsToSlider(parseInt(this.dataset.ts));
      onSliderChange();
    });
  });
}

function showPoint(point) {
  selectedPoint = point;

  // Update header
  document.getElementById('point-name').textContent =
    point.n + '  —  ' + point.lat.toFixed(2) + '°N  ' + point.lon.toFixed(2) + '°E';
  document.getElementById('point-coords').textContent =
    point.wx.length + ' hours of forecast data';
  document.getElementById('click-hint').style.display = 'none';
  document.getElementById('tab-bar').style.display    = 'flex';
  document.getElementById('map-hint').style.display   = 'none';

  // Highlight grid dot
  Object.keys(gridDots).forEach(function(n) {
    gridDots[n].setStyle({
      color: n === point.n ? '#e2b714' : '#4a7aff',
      fillColor: n === point.n ? '#e2b714' : '#4a7aff',
      radius:    n === point.n ? 7 : 4,
      fillOpacity: n === point.n ? 0.9 : 0.55
    });
  });

  renderTab(point, activeTab);
  scrollToCurrentRow();
}

// ════════════════════════════════════════════════════════════════════════
//  Scroll + highlight current time row
// ════════════════════════════════════════════════════════════════════════
function scrollToCurrentRow() {
  var ts   = currentTs();
  var rows = document.querySelectorAll('#entry-list .entry-row[data-ts]');
  if (!rows.length) return;

  var bestRow = null, bestDiff = Infinity;
  rows.forEach(function(row) {
    var d = Math.abs(parseInt(row.dataset.ts) - ts);
    if (d < bestDiff) { bestDiff = d; bestRow = row; }
  });

  document.querySelectorAll('#entry-list .entry-row.current').forEach(function(r) {
    r.classList.remove('current');
  });

  if (bestRow) {
    bestRow.classList.add('current');
    bestRow.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  }
}

// ════════════════════════════════════════════════════════════════════════
//  Map popup content (shown on the grid dot)
// ════════════════════════════════════════════════════════════════════════
function buildPopup(point, ts) {
  var wx   = nearestRow(point.wx,   ts);
  var tide = nearestRow(point.tide, ts);

  var html = '<div style="min-width:190px;font-size:12px;line-height:1.7">';
  html += '<b style="font-size:13px">' + point.n + '</b>  ' +
    '<span style="color:#888">' + point.lat.toFixed(2) + '°N  ' +
    point.lon.toFixed(2) + '°E</span><br>';
  html += '<span style="font-size:11px;color:#888">' + fmtTs(ts) + '</span><hr style="margin:5px 0">';

  if (wx) {
    var windStr = v(wx[I_WS], ' m/s', 1) +
      (wx[I_WD] != null ? '  ' + compassDir(wx[I_WD]) : '') +
      (wx[I_WG] != null ? '  gust ' + v(wx[I_WG], '', 1) : '');
    var waveStr = v(wx[I_WH], 'm', 2) +
      (wx[I_WDR] != null ? '  ' + compassDir(wx[I_WDR]) : '') +
      (wx[I_WP]  != null ? '  ' + v(wx[I_WP], 's', 0) : '');
    var tideStr = tide ? v(tide[1], 'm', 2) : '—';

    var rows = [
      ['💨 Wind',   windStr],
      ['🌊 Waves',  waveStr],
      ['🌡 Air',    v(wx[I_AT], '°C', 1)],
      ['📊 Press',  v(wx[I_PR], ' hPa', 0)],
      ['≈  Tide',   tideStr],
    ];
    if (wx[I_VIS] != null)
      rows.push(['👁 Vis', v(wx[I_VIS], ' km', 0)]);

    rows.forEach(function(r) {
      html += '<div style="display:flex;justify-content:space-between;gap:14px">' +
        '<span style="color:#888">' + r[0] + '</span>' +
        '<span style="font-weight:500">' + r[1] + '</span>' +
      '</div>';
    });
  } else {
    html += '<i style="color:#aaa">No data at this time</i>';
  }

  return html + '</div>';
}

// ════════════════════════════════════════════════════════════════════════
//  Map click
// ════════════════════════════════════════════════════════════════════════
map.on('click', function(e) {
  var pt = nearestPoint(e.latlng.lat, e.latlng.lng);
  if (!pt) return;

  // Move / create click marker at the *grid point* (data origin)
  if (clickMarker) {
    clickMarker.setLatLng([pt.lat, pt.lon]);
  } else {
    clickMarker = L.circleMarker([pt.lat, pt.lon], {
      radius: 10, color: '#e2b714', fillColor: '#e2b714',
      fillOpacity: 0.15, weight: 2, interactive: true
    }).addTo(map);
    clickMarker.bindPopup('', {maxWidth: 260, className: 'wx-popup'});
  }

  showPoint(pt);

  clickMarker.setPopupContent(buildPopup(pt, currentTs()));
  clickMarker.openPopup();
});

// ════════════════════════════════════════════════════════════════════════
//  Slider
// ════════════════════════════════════════════════════════════════════════
function onSliderChange() {
  var ts = currentTs();
  timeLabel.textContent = fmtTs(ts);

  if (selectedPoint) {
    scrollToCurrentRow();
    if (clickMarker && clickMarker.isPopupOpen()) {
      clickMarker.setPopupContent(buildPopup(selectedPoint, ts));
    }
    // Redraw sparkline cursor in tide tab
    if (activeTab === 'tide') renderTab(selectedPoint, 'tide');
  }
}

slider.addEventListener('input', onSliderChange);

// ════════════════════════════════════════════════════════════════════════
//  Tab switching
// ════════════════════════════════════════════════════════════════════════
document.querySelectorAll('.tab-btn').forEach(function(btn) {
  btn.addEventListener('click', function() {
    document.querySelectorAll('.tab-btn').forEach(function(b) {
      b.classList.remove('active');
    });
    this.classList.add('active');
    activeTab = this.dataset.tab;
    if (selectedPoint) {
      renderTab(selectedPoint, activeTab);
      scrollToCurrentRow();
    }
  });
});

// ════════════════════════════════════════════════════════════════════════
//  Init: position slider at "now"
// ════════════════════════════════════════════════════════════════════════
(function() {
  var nowFrac = (DATA.now - DATA.t_min) / (DATA.t_max - DATA.t_min);
  nowFrac = Math.max(0, Math.min(1, nowFrac));
  slider.value = Math.round(nowFrac * 1000);
  timeLabel.textContent = fmtTs(currentTs());

  // NOW marker on slider
  var nowEl = document.getElementById('now-marker');
  if (nowFrac > 0 && nowFrac < 1) {
    nowEl.style.display = 'flex';
    nowEl.style.left = (nowFrac * 100) + '%';
  }
})();

</script>
</body>
</html>"""


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Generate a self-contained weather forecast map for NOCTURNAL"
    )
    ap.add_argument("--db",  type=Path, default=DEFAULT_DB,
                    help=f"Path to weather.db (default: {DEFAULT_DB})")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"Output HTML file (default: {DEFAULT_OUT})")
    args = ap.parse_args()

    print("NOCTURNAL Weather Map")
    print("─" * 40)
    print(f"  DB  : {args.db}")

    data = load_data(args.db)

    aoi_json = AOI_FILE.read_text(encoding="utf-8") if AOI_FILE.exists() else "null"

    html = HTML_TEMPLATE \
        .replace("__DATA_JSON__",   json.dumps(data)) \
        .replace("__AOI_GEOJSON__", aoi_json)

    args.out.write_text(html, encoding="utf-8")
    size_kb = args.out.stat().st_size >> 10
    print(f"\nSaved → {args.out.resolve()}  ({size_kb} KB)")
    print("Open weather_map.html in your browser.")


if __name__ == "__main__":
    main()
