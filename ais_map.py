#!/usr/bin/env python3
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
"""
ais_map.py — Interactive AIS vessel movement map for NOCTURNAL.

Reads all AIS tracks from ais_memory.db and generates a self-contained
HTML map with an animated timeline showing vessel movement.

Build time scales with DB size (~38 GB = 2–5 min).  Use --days N to
load only the last N days for a faster build.

Usage:
    python ais_map.py                 # full DB range
    python ais_map.py --days 7        # last 7 days
    python ais_map.py --thin 15       # 1 ping per vessel per 15 min
    python ais_map.py --out my.html
"""

import argparse
import json
import sqlite3
import time as _time
from datetime import datetime, timezone
from pathlib import Path

import config as _cfg

DEFAULT_DB      = Path(str(_cfg.DB_PATH))
AOI_FILE        = Path(__file__).parent / "aoi.geojson"
DEFAULT_OUT     = Path("ais_map.html")
DEFAULT_THIN    = 10   # minutes


# ── Data loading ───────────────────────────────────────────────────────────────

def load_tracks(db_path: Path, thin_min: int = DEFAULT_THIN,
                t_lo: int = None, t_hi: int = None) -> dict:
    """
    Stream all AIS positions from the DB and return thinned per-vessel tracks.

    Uses the ts_epoch index when a time range is given, otherwise falls back
    to a full sequential scan (fastest for large ranges on this schema).
    Spatial filtering is done in Python (no lat/lon index exists).
    """
    if not db_path.exists():
        raise SystemExit(f"AIS DB not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA cache_size = -65536")   # 64 MB page cache

    # ── vessel metadata ────────────────────────────────────────────────────
    vessel_info: dict = {}
    try:
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
    except Exception:
        pass

    # ── AOI spatial bounds ─────────────────────────────────────────────────
    lat_min = _cfg.AOI_LAT_MIN - 0.5
    lat_max = _cfg.AOI_LAT_MAX + 0.5
    lon_min = _cfg.AOI_LON_MIN - 0.5
    lon_max = _cfg.AOI_LON_MAX + 0.5

    # ── build query ────────────────────────────────────────────────────────
    # Use ts_epoch index when a time window is specified (fast + selective).
    # For the full range we omit WHERE so SQLite does a sequential heap scan
    # (avoids random I/O from following every index leaf → faster on large DBs).
    if t_lo is not None and t_hi is not None:
        query = ("SELECT mmsi, lat, lon, ts_epoch, sog, cog "
                 "FROM positions WHERE ts_epoch BETWEEN ? AND ?")
        params = (t_lo, t_hi)
        days = (t_hi - t_lo) / 86400
        print(f"  Loading   : last {days:.0f} days  "
              f"({datetime.utcfromtimestamp(t_lo).strftime('%Y-%m-%d')} → "
              f"{datetime.utcfromtimestamp(t_hi).strftime('%Y-%m-%d')} UTC)")
    else:
        query  = "SELECT mmsi, lat, lon, ts_epoch, sog, cog FROM positions"
        params = ()
        print("  Loading   : full DB range  (add --days N for a faster build)")

    # ── stream and accumulate ──────────────────────────────────────────────
    thin_sec  = thin_min * 60
    raw: dict = {}          # mmsi → list of [ts, lat, lon, sog, cog]
    total_rows = in_aoi = 0
    t0 = _time.time()

    cur = conn.execute(query, params)
    print("  Streaming … ", end="", flush=True)

    while True:
        chunk = cur.fetchmany(50000)
        if not chunk:
            break
        total_rows += len(chunk)
        for mmsi, lat, lon, ts, sog, cog in chunk:
            if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
                continue
            in_aoi += 1
            if mmsi not in raw:
                raw[mmsi] = []
            raw[mmsi].append([int(ts),
                               round(float(lat), 4),
                               round(float(lon), 4),
                               round(float(sog), 1) if sog is not None else None,
                               round(float(cog), 0) if cog is not None else None])

        # progress dot every 1M rows
        if (total_rows // 1_000_000) > ((total_rows - len(chunk)) // 1_000_000):
            print(".", end="", flush=True)

    elapsed = _time.time() - t0
    print(f"  {total_rows/1e6:.1f}M rows in {elapsed:.0f}s  |  {in_aoi:,} in AOI")
    conn.close()

    if not raw:
        raise SystemExit("No AIS positions found in AOI — check DB or AOI bounds.")

    # ── sort + thin per vessel ─────────────────────────────────────────────
    t_min = float("inf");  t_max = float("-inf")
    tracks = []
    for mmsi, pings in raw.items():
        pings.sort(key=lambda p: p[0])

        thinned, last_bucket = [], None
        for p in pings:
            b = p[0] // thin_sec
            if b != last_bucket:
                thinned.append(p)
                last_bucket = b

        if len(thinned) < 2:
            continue

        ts_first = thinned[0][0];  ts_last = thinned[-1][0]
        if ts_first < t_min: t_min = ts_first
        if ts_last  > t_max: t_max = ts_last

        info = vessel_info.get(mmsi, {})
        tracks.append({
            "mmsi":      mmsi,
            "name":      info.get("name"),
            "callsign":  info.get("callsign"),
            "imo":       info.get("imo"),
            "ship_type": info.get("ship_type"),
            "pings":     thinned,
        })

    # named vessels first, then by MMSI
    tracks.sort(key=lambda t: (t["name"] is None, t["name"] or "", t["mmsi"]))

    total_pings = sum(len(t["pings"]) for t in tracks)
    print(f"  Tracks    : {len(tracks)} vessels  |  {total_pings:,} pings "
          f"(thin={thin_min} min)")
    print(f"  Range     : {datetime.utcfromtimestamp(t_min).strftime('%Y-%m-%d %H:%M')} "
          f"→ {datetime.utcfromtimestamp(t_max).strftime('%Y-%m-%d %H:%M')} UTC")

    return {"tracks": tracks,
            "t_min":  int(t_min),
            "t_max":  int(t_max),
            "now":    int(datetime.now(timezone.utc).timestamp())}


# ── HTML template ──────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NOCTURNAL — AIS</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'SF Mono','Consolas','Menlo',monospace;
       background:#0a0e14; color:#c4cad4;
       display:flex; flex-direction:column; height:100vh; overflow:hidden; }

#top  { display:flex; flex:1; min-height:0; }
#map-container { flex:1; min-width:0; }
#map  { width:100%; height:100%; }

/* ── left sidebar ── */
#sidebar {
  width:256px; min-width:256px; flex-shrink:0;
  background:#0a0e14; border-right:1px solid #1e2a3a;
  display:flex; flex-direction:column; overflow:hidden;
}
.sb-head {
  padding:12px 14px 10px; flex-shrink:0;
  border-bottom:1px solid #1e2a3a;
}
.sb-head h2 { font-size:11px; font-weight:600; color:#7eb8da; letter-spacing:.5px; margin-bottom:6px; }
#sb-title  { font-size:12px; font-weight:700; color:#c4cad4; min-height:18px; }
#sb-sub    { font-size:9px;  color:#6b7785; line-height:1.7; min-height:32px; }

/* stats strip */
#sb-stats {
  padding:8px 14px; flex-shrink:0;
  border-bottom:1px solid #1e2a3a;
  display:flex; flex-wrap:wrap; gap:6px 12px;
}
.stat { display:flex; flex-direction:column; }
.stat-lbl { font-size:8px; color:#3a4a5a; text-transform:uppercase; letter-spacing:.3px; }
.stat-val { font-size:11px; font-weight:700; color:#7eb8da; }

/* vessel detail */
#vessel-detail {
  flex:1; overflow-y:auto; padding:0;
}
#vessel-detail::-webkit-scrollbar { width:3px; }
#vessel-detail::-webkit-scrollbar-thumb { background:#1e2a3a; border-radius:2px; }

.vd-row {
  padding:5px 14px; display:flex; justify-content:space-between;
  border-bottom:1px solid #0e1620; font-size:10px;
}
.vd-lbl { color:#4a5a6a; }
.vd-val { color:#c4cad4; font-weight:600; text-align:right; max-width:160px;
          word-break:break-word; }
.vd-hint { padding:20px 14px; font-size:10px; color:#3a4a5a; line-height:1.7; }

.vd-section {
  padding:6px 14px 3px; font-size:8px; font-weight:700;
  color:#3a4a5a; letter-spacing:.4px; text-transform:uppercase;
  border-top:1px solid #141c26; margin-top:4px;
}

/* ── timeline bar ── */
#timeline-bar {
  flex-shrink:0; background:#0a0e14; border-top:1px solid #1e2a3a;
  padding:8px 14px 10px; display:flex; align-items:center; gap:10px;
  user-select:none;
}
#play-btn {
  background:#1a2d3d; border:1px solid #2a4a6a; border-radius:4px;
  color:#7eb8da; font-family:inherit; font-size:10px; padding:5px 10px;
  cursor:pointer; white-space:nowrap; transition:all .15s; flex-shrink:0;
}
#play-btn:hover   { background:#1e3548; }
#play-btn.playing { background:#1e3548; color:#e0a040; border-color:#6a4a1a; }
#time-label {
  font-size:10px; color:#7eb8da; letter-spacing:.3px;
  white-space:nowrap; min-width:200px; flex-shrink:0;
}
.slider-wrap { position:relative; flex:1; }
#slider { width:100%; cursor:pointer; accent-color:#7eb8da; }
#now-mark { position:absolute; top:-10px; font-size:8px; color:#7eb8da;
            transform:translateX(-50%); pointer-events:none; }
#vis-count { font-size:9px; color:#4a5a6a; white-space:nowrap; flex-shrink:0; }
.btn-sm {
  padding:5px 10px; background:#0d1118; border:1px solid #1e2a3a; border-radius:4px;
  color:#6b7785; font-family:inherit; font-size:9px; cursor:pointer;
  letter-spacing:.3px; text-transform:uppercase; transition:all .15s;
  white-space:nowrap; flex-shrink:0;
}
.btn-sm:hover  { background:#1a2530; color:#c4cad4; border-color:#3a5a7a; }
.btn-sm.active { background:#1a2d3d; color:#7eb8da; border-color:#2a4a6a; }

/* ── Leaflet popup overrides ── */
.leaflet-popup-content-wrapper {
  background:rgba(10,14,20,.96) !important; border:1px solid #1e2a3a !important;
  border-radius:6px !important; box-shadow:0 4px 20px rgba(0,0,0,.7) !important;
}
.leaflet-popup-tip { background:rgba(10,14,20,.96) !important; }
.leaflet-popup-content {
  margin:10px 14px !important; font-size:10px !important;
  font-family:'SF Mono','Consolas','Menlo',monospace !important;
  color:#c4cad4 !important; line-height:1.8 !important;
}
.leaflet-popup-close-button { color:#6b7785 !important; }
.pp-name { color:#7eb8da; font-size:11px; font-weight:700; display:block; margin-bottom:3px; }
.pp-sub  { color:#4a5a6a; font-size:9px; display:block; margin-bottom:5px; }
.pp-sep  { border:none; border-top:1px solid #1e2a3a; margin:5px 0; }
.pp-row  { display:flex; justify-content:space-between; gap:12px; }
.pp-lbl  { color:#6b7785; }
.pp-val  { color:#c4cad4; font-weight:600; }
</style>
</head>
<body>
<div id="top">

  <!-- ── Sidebar ── -->
  <div id="sidebar">
    <div class="sb-head">
      <h2>⚓ NOCTURNAL  ·  AIS</h2>
      <div id="sb-title">—</div>
      <div id="sb-sub">&nbsp;</div>
    </div>
    <div id="sb-stats">
      <div class="stat"><div class="stat-lbl">Vessels total</div><div class="stat-val" id="st-total">—</div></div>
      <div class="stat"><div class="stat-lbl">Visible now</div><div class="stat-val"  id="st-vis">—</div></div>
      <div class="stat"><div class="stat-lbl">Period</div><div class="stat-val" id="st-period">—</div></div>
    </div>
    <div id="vessel-detail">
      <div class="vd-hint">Click a vessel on the map<br>to inspect its track.</div>
    </div>
  </div>

  <!-- ── Map ── -->
  <div id="map-container"><div id="map"></div></div>
</div>

<!-- ── Timeline bar ── -->
<div id="timeline-bar">
  <button id="play-btn">▶ Play</button>
  <div id="time-label">——</div>
  <div class="slider-wrap">
    <div id="now-mark" style="display:none">▼</div>
    <input type="range" id="slider" min="0" max="1000" value="500">
  </div>
  <div id="vis-count">—</div>
  <button class="btn-sm active" id="btn-trails" onclick="toggleTrails()">Trails</button>
</div>

<script>
// ════════════════════════════════════════════════════════════════════════
//  Data
// ════════════════════════════════════════════════════════════════════════
const DATA        = __DATA_JSON__;
const AOI_GEOJSON = __AOI_GEOJSON__;

const T_MIN  = DATA.t_min;
const T_MAX  = DATA.t_max;
const NOW_TS = DATA.now;

// ════════════════════════════════════════════════════════════════════════
//  Map
// ════════════════════════════════════════════════════════════════════════
const map = L.map('map', {zoomControl:true});

// compute centre from all track pings
let sumLat=0, sumLon=0, nPt=0;
DATA.tracks.forEach(t => {
  if(t.pings.length) { sumLat+=t.pings[0][1]; sumLon+=t.pings[0][2]; nPt++; }
});
map.setView(nPt ? [sumLat/nPt, sumLon/nPt] : [50.0, -1.0], 8);

L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{
  attribution:'&copy; <a href="https://www.openstreetmap.org/">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
  maxZoom:18
}).addTo(map);

if (AOI_GEOJSON) {
  L.geoJSON(AOI_GEOJSON,{
    style:{color:'#7eb8da',weight:2,opacity:0.45,fill:true,
           fillColor:'#7eb8da',fillOpacity:0.03,dashArray:'7 5'}
  }).addTo(map);
}

// ════════════════════════════════════════════════════════════════════════
//  Helpers
// ════════════════════════════════════════════════════════════════════════
const C16 = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW'];
const cdir = d => d==null?'—': C16[Math.round(d/22.5)%16];
const kts  = s => s==null?'—': (s*1.944).toFixed(1)+' kn';

function fmtTs(ts) {
  const d = new Date(ts*1000);
  return d.getUTCFullYear()+'-'+
    String(d.getUTCMonth()+1).padStart(2,'0')+'-'+
    String(d.getUTCDate()).padStart(2,'0')+'  '+
    String(d.getUTCHours()).padStart(2,'0')+':'+
    String(d.getUTCMinutes()).padStart(2,'0')+' UTC';
}
function fmtDate(ts) {
  const d=new Date(ts*1000);
  const M=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return `${d.getUTCDate()} ${M[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

function currentTs() { return T_MIN + (slider.value/1000)*(T_MAX-T_MIN); }

// linear-interpolate position at time t
function getPos(pings, t) {
  if(!pings.length) return null;
  if(t < pings[0][0] || t > pings[pings.length-1][0]) return null;
  for(let i=0; i<pings.length-1; i++){
    const a=pings[i], b=pings[i+1];
    if(t>=a[0]&&t<=b[0]){
      const f=(t-a[0])/(b[0]-a[0]);
      return {lat:a[1]+f*(b[1]-a[1]), lon:a[2]+f*(b[2]-a[2]),
              sog:a[3], cog:a[4]};
    }
  }
  return null;
}

const TRAIL_SEC = 45*60;
function getTrail(pings, t) {
  const t0=t-TRAIL_SEC, trail=[];
  for(const p of pings) if(p[0]>=t0&&p[0]<=t) trail.push([p[1],p[2]]);
  return trail;
}

// ════════════════════════════════════════════════════════════════════════
//  Vessel store
// ════════════════════════════════════════════════════════════════════════
const COLORS = ['#7eb8da','#5aaae0','#4ec9b0','#dcdcaa','#ce9178','#c586c0'];
function vesselColor(i) { return COLORS[i % COLORS.length]; }

const vesselStore = [];   // [{tr, marker, trail, color}, ...]
let selectedIdx = null;
let trailsOn = true;

DATA.tracks.forEach((tr, i) => {
  const col = vesselColor(i);
  const marker = L.circleMarker([0,0],{
    radius:5, color:col, fillColor:col,
    fillOpacity:0.85, weight:1.5, interactive:true
  });
  const trail = L.polyline([],{
    color:col, weight:1.5, opacity:0.35, interactive:false
  });

  marker.bindPopup('', {maxWidth:260});
  marker.on('click', () => {
    selectedIdx = (selectedIdx===i) ? null : i;
    update();
    if(selectedIdx===i) showVesselDetail(tr, i);
  });

  vesselStore.push({tr, marker, trail, color:col});
});

// ════════════════════════════════════════════════════════════════════════
//  Layer groups
// ════════════════════════════════════════════════════════════════════════
const dotGroup   = L.layerGroup().addTo(map);
const trailGroup = L.layerGroup().addTo(map);

// ════════════════════════════════════════════════════════════════════════
//  Update all vessel positions
// ════════════════════════════════════════════════════════════════════════
function update() {
  const t = currentTs();
  timeLabel.textContent = fmtTs(t);
  dotGroup.clearLayers();
  trailGroup.clearLayers();

  let visible = 0;
  vesselStore.forEach(({tr, marker, trail, color}, i) => {
    const pos = getPos(tr.pings, t);
    if(!pos) return;
    visible++;

    const isSel = i === selectedIdx;
    const col   = isSel ? '#e0a040' : color;
    marker.setLatLng([pos.lat, pos.lon]);
    marker.setStyle({
      color:col, fillColor:col,
      radius: isSel?9:5, weight: isSel?2.5:1.5,
      fillOpacity: isSel?0.95:0.85
    });
    dotGroup.addLayer(marker);

    if(trailsOn) {
      const pts = getTrail(tr.pings, t);
      if(pts.length > 1) {
        trail.setLatLngs(pts);
        trail.setStyle({color:col, weight:isSel?2.5:1.5, opacity:isSel?0.7:0.35});
        trailGroup.addLayer(trail);
      }
    }

    // live-update selected popup
    if(isSel && marker.isPopupOpen()) {
      marker.setPopupContent(buildPopup(tr, pos, t));
    }
  });

  document.getElementById('vis-count').textContent = `${visible} visible`;
  document.getElementById('st-vis').textContent    = visible;
}

// ════════════════════════════════════════════════════════════════════════
//  Sidebar vessel detail
// ════════════════════════════════════════════════════════════════════════
function row(lbl, val) {
  return `<div class="vd-row"><span class="vd-lbl">${lbl}</span><span class="vd-val">${val||'—'}</span></div>`;
}
function section(lbl) { return `<div class="vd-section">${lbl}</div>`; }

function showVesselDetail(tr, i) {
  const name = tr.name || 'Unknown vessel';
  document.getElementById('sb-title').textContent = name;
  const sub = `MMSI: ${tr.mmsi}` + (tr.ship_type ? `  ·  ${tr.ship_type}` : '');
  document.getElementById('sb-sub').textContent = sub;

  const t = currentTs();
  const pos = getPos(tr.pings, t);
  const first = tr.pings[0], last = tr.pings[tr.pings.length-1];

  let html = section('Identity');
  html += row('Name',      tr.name || '—');
  html += row('MMSI',
    `<a href="https://www.marinetraffic.com/en/ais/details/ships/mmsi:${tr.mmsi}" `+
    `target="_blank" style="color:#7eb8da">${tr.mmsi} ↗</a>`);
  html += row('Type',      tr.ship_type);
  html += row('Callsign',  tr.callsign);
  html += row('IMO',       tr.imo ? `IMO ${tr.imo}` : null);

  html += section('Current');
  if(pos) {
    html += row('Position', `${pos.lat.toFixed(4)}°N  ${pos.lon>=0?'+':''}${pos.lon.toFixed(4)}°E`);
    html += row('Speed',    kts(pos.sog));
    html += row('Course',   pos.cog!=null ? `${pos.cog.toFixed(0)}°  ${cdir(pos.cog)}` : '—');
  } else {
    html += `<div class="vd-hint" style="padding:8px 14px">Not visible at current time</div>`;
  }

  html += section('Track');
  html += row('First seen', fmtDate(first[0]));
  html += row('Last seen',  fmtDate(last[0]));
  html += row('Pings',      tr.pings.length.toLocaleString());

  document.getElementById('vessel-detail').innerHTML = html;

  // open popup on the map
  if(pos) {
    const vs = vesselStore[i];
    vs.marker.setPopupContent(buildPopup(tr, pos, t));
    vs.marker.openPopup();
  }
}

function clearDetail() {
  document.getElementById('sb-title').textContent = '—';
  document.getElementById('sb-sub').innerHTML = '&nbsp;';
  document.getElementById('vessel-detail').innerHTML =
    '<div class="vd-hint">Click a vessel on the map<br>to inspect its track.</div>';
}

// dismiss selection on map click
map.on('click', () => { selectedIdx=null; clearDetail(); update(); });

// ════════════════════════════════════════════════════════════════════════
//  Vessel popup (map callout)
// ════════════════════════════════════════════════════════════════════════
function buildPopup(tr, pos, t) {
  const name = tr.name || 'Unknown vessel';
  let html = `<span class="pp-name">${name}</span>`;
  let sub = `MMSI: ${tr.mmsi}`;
  if(tr.ship_type) sub += `  ·  ${tr.ship_type}`;
  html += `<span class="pp-sub">${sub}</span><hr class="pp-sep">`;
  html += `<div class="pp-row"><span class="pp-lbl">Speed</span><span class="pp-val">${kts(pos.sog)}</span></div>`;
  html += `<div class="pp-row"><span class="pp-lbl">Course</span><span class="pp-val">${pos.cog!=null?pos.cog.toFixed(0)+'°  '+cdir(pos.cog):'—'}</span></div>`;
  html += `<div class="pp-row"><span class="pp-lbl">Lat / Lon</span><span class="pp-val">${pos.lat.toFixed(4)}°N  ${pos.lon>=0?'+':''}${pos.lon.toFixed(4)}°E</span></div>`;
  html += `<div class="pp-row" style="margin-top:3px"><span class="pp-lbl">At</span><span class="pp-val" style="font-size:9px">${fmtTs(t)}</span></div>`;
  return html;
}

// ════════════════════════════════════════════════════════════════════════
//  Trails toggle
// ════════════════════════════════════════════════════════════════════════
function toggleTrails() {
  trailsOn = !trailsOn;
  document.getElementById('btn-trails').classList.toggle('active', trailsOn);
  if(!trailsOn) trailGroup.clearLayers();
  else update();
}

// ════════════════════════════════════════════════════════════════════════
//  Slider + play/pause
// ════════════════════════════════════════════════════════════════════════
const slider    = document.getElementById('slider');
const timeLabel = document.getElementById('time-label');
let playing = false, playTimer = null;

slider.addEventListener('input', () => {
  update();
  if(selectedIdx !== null) showVesselDetail(vesselStore[selectedIdx].tr, selectedIdx);
});

document.getElementById('play-btn').addEventListener('click', function(){
  playing = !playing;
  this.textContent = playing ? '⏸ Pause' : '▶ Play';
  this.classList.toggle('playing', playing);
  if(playing){
    playTimer = setInterval(()=>{
      let v = parseInt(slider.value) + 2;
      if(v > 1000) v = 0;
      slider.value = v;
      update();
      if(selectedIdx !== null) showVesselDetail(vesselStore[selectedIdx].tr, selectedIdx);
    }, 100);
  } else {
    clearInterval(playTimer);
  }
});

// ════════════════════════════════════════════════════════════════════════
//  Init
// ════════════════════════════════════════════════════════════════════════
(()=>{
  // fill static stats
  document.getElementById('st-total').textContent = DATA.tracks.length.toLocaleString();
  document.getElementById('st-period').textContent =
    `${fmtDate(T_MIN)} – ${fmtDate(T_MAX)}`;

  // position slider at NOW
  const frac = Math.max(0, Math.min(1, (NOW_TS-T_MIN)/(T_MAX-T_MIN)));
  slider.value = Math.round(frac * 1000);
  const nm = document.getElementById('now-mark');
  if(frac > 0 && frac < 1){ nm.style.display='block'; nm.style.left=(frac*100)+'%'; }

  update();
})();
</script>
</body>
</html>"""


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Generate AIS vessel movement map for NOCTURNAL"
    )
    ap.add_argument("--db",   type=Path, default=DEFAULT_DB)
    ap.add_argument("--out",  type=Path, default=DEFAULT_OUT)
    ap.add_argument("--thin", type=int,  default=DEFAULT_THIN,
                    help=f"Thin to 1 ping per vessel per N minutes (default: {DEFAULT_THIN})")
    ap.add_argument("--days", type=int,  default=None,
                    help="Load only the last N days (faster; omit for full DB range)")
    args = ap.parse_args()

    print("NOCTURNAL — AIS Map")
    print("─" * 40)
    print(f"  DB   : {args.db}  ({args.db.stat().st_size >> 30} GB)")

    t_lo = t_hi = None
    if args.days:
        t_hi = int(datetime.now(timezone.utc).timestamp())
        t_lo = t_hi - args.days * 86400

    data = load_tracks(args.db, thin_min=args.thin, t_lo=t_lo, t_hi=t_hi)

    aoi_json = AOI_FILE.read_text(encoding="utf-8") if AOI_FILE.exists() else "null"

    html = HTML \
        .replace("__DATA_JSON__",   json.dumps(data, separators=(',', ':'))) \
        .replace("__AOI_GEOJSON__", aoi_json)

    args.out.write_text(html, encoding="utf-8")
    size_kb = args.out.stat().st_size >> 10
    print(f"\nSaved → {args.out.resolve()}  ({size_kb} KB)")
    print("Open ais_map.html in your browser.")


if __name__ == "__main__":
    main()
