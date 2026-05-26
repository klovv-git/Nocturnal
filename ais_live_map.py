#!/usr/bin/env python3
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
"""
ais_live_map.py — Latest-position AIS snapshot map for NOCTURNAL.

Shows every vessel at its most recent known position with a short trail.
Queries only the last N hours via the ts_epoch index — builds in seconds,
not minutes.

Usage:
    python ais_live_map.py                  # last 6 hours (default)
    python ais_live_map.py --hours 12       # last 12 hours
    python ais_live_map.py --out live.html
    python ais_live_map.py --watch 5        # rebuild every 5 min
"""

import argparse
import json
import sqlite3
import time as _time
import subprocess
import sys as _sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import config as _cfg

DEFAULT_DB    = Path(str(_cfg.DB_PATH))
AOI_FILE      = Path(__file__).parent / "aoi.geojson"
DEFAULT_OUT   = Path("ais_live_map.html")
DEFAULT_HOURS = 6


# ── Ship type helpers ──────────────────────────────────────────────────────────

def ship_color(ship_type: str | None) -> str:
    t = int(ship_type) if ship_type and ship_type.isdigit() else 0
    if 80 <= t <= 89: return "#e05555"   # tanker
    if 70 <= t <= 79: return "#3a9a50"   # cargo
    if 60 <= t <= 69: return "#3a7ae0"   # passenger
    if t == 30:       return "#d4a030"   # fishing
    if t in (21,22,31,32,50,51,52,53,54,55,56,57,58,59): return "#9a50c0"  # tug/special
    if 40 <= t <= 49: return "#d46020"   # HSC
    if t in (36, 37): return "#30a0a0"   # sail/leisure
    return "#607080"                     # other/unknown

def ship_label(ship_type: str | None) -> str:
    t = int(ship_type) if ship_type and ship_type.isdigit() else 0
    if 80 <= t <= 89: return "Tanker"
    if 70 <= t <= 79: return "Cargo"
    if 60 <= t <= 69: return "Passenger"
    if t == 30:       return "Fishing"
    if 50 <= t <= 59: return "Special"
    if t in (31, 32): return "Tug"
    if 40 <= t <= 49: return "High-speed"
    if t in (36, 37): return "Sail/Leisure"
    return f"Type {t}" if t else "Unknown"


# ── Data loading ───────────────────────────────────────────────────────────────

def load_snapshot(db_path: Path, hours: int = DEFAULT_HOURS) -> dict:
    """
    Load the last `hours` of AIS positions using the ts_epoch index.
    Returns one entry per vessel: latest position + full trail for the window.
    """
    if not db_path.exists():
        raise SystemExit(f"AIS DB not found: {db_path}")

    now_ts  = int(datetime.now(timezone.utc).timestamp())
    t_lo    = now_ts - hours * 3600

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA cache_size = -32768")   # 32 MB cache

    # ── vessel metadata ────────────────────────────────────────────────────
    vessel_info: dict = {}
    try:
        for mmsi, name, callsign, imo, ship_type in conn.execute(
            "SELECT mmsi, name, callsign, imo, ship_type FROM vessels"
        ).fetchall():
            vessel_info[mmsi] = {
                "name":      name.strip()     if name      else None,
                "callsign":  callsign.strip() if callsign  else None,
                "imo":       imo              if imo        else None,
                "ship_type": str(ship_type)   if ship_type  else None,
            }
    except Exception:
        pass

    # ── AOI bounds ─────────────────────────────────────────────────────────
    lat_min = _cfg.AOI_LAT_MIN - 0.5
    lat_max = _cfg.AOI_LAT_MAX + 0.5
    lon_min = _cfg.AOI_LON_MIN - 0.5
    lon_max = _cfg.AOI_LON_MAX + 0.5

    # ── fetch recent pings via ts_epoch index ──────────────────────────────
    print(f"  Loading   : last {hours}h  "
          f"({datetime.utcfromtimestamp(t_lo).strftime('%Y-%m-%d %H:%M')} → now UTC)")
    print("  Streaming … ", end="", flush=True)

    raw: dict = defaultdict(list)   # mmsi → [[ts, lat, lon, sog, cog], ...]
    total = in_aoi = 0
    t0 = _time.time()

    # No ORDER BY — let SQLite return rows in ts_epoch index order (fastest).
    # Python sorts each vessel's pings after accumulation (trivial for this row count).
    cur = conn.execute(
        "SELECT mmsi, lat, lon, ts_epoch, sog, cog "
        "FROM positions WHERE ts_epoch >= ?",
        (t_lo,)
    )
    while True:
        chunk = cur.fetchmany(50_000)
        if not chunk:
            break
        total += len(chunk)
        for mmsi, lat, lon, ts, sog, cog in chunk:
            if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
                continue
            in_aoi += 1
            raw[mmsi].append([
                int(ts),
                round(float(lat), 4),
                round(float(lon), 4),
                round(float(sog), 1) if sog is not None else None,
                round(float(cog), 0) if cog is not None else None,
            ])

    elapsed = _time.time() - t0
    print(f"  {total:,} rows in {elapsed:.1f}s  |  {in_aoi:,} in AOI")
    conn.close()

    if not raw:
        raise SystemExit("No AIS positions found — DB may not have data in this window.")

    # ── build per-vessel snapshot ──────────────────────────────────────────
    vessels = []
    for mmsi, pings in raw.items():
        pings.sort(key=lambda p: p[0])
        latest = pings[-1]                      # most recent ping
        info   = vessel_info.get(mmsi, {})
        stype  = info.get("ship_type")
        vessels.append({
            "mmsi":      mmsi,
            "name":      info.get("name"),
            "callsign":  info.get("callsign"),
            "imo":       info.get("imo"),
            "ship_type": stype,
            "color":     ship_color(stype),
            "type_label":ship_label(stype),
            "lat":       latest[1],
            "lon":       latest[2],
            "sog":       latest[3],
            "cog":       latest[4],
            "last_ts":   latest[0],
            "trail":     [[p[1], p[2]] for p in pings],   # [lat,lon] pairs
        })

    # named vessels first
    vessels.sort(key=lambda v: (v["name"] is None, v["name"] or "", v["mmsi"]))

    # find absolute latest ping to show data freshness
    latest_ts = max(v["last_ts"] for v in vessels)

    print(f"  Vessels   : {len(vessels):,}")
    print(f"  Data up to: {datetime.utcfromtimestamp(latest_ts).strftime('%Y-%m-%d %H:%M')} UTC")

    return {
        "vessels":   vessels,
        "t_lo":      t_lo,
        "now":       now_ts,
        "latest_ts": latest_ts,
        "hours":     hours,
    }


# ── HTML template ──────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NOCTURNAL — Live AIS</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'SF Mono','Consolas','Menlo',monospace;
       background:#0a0e14; color:#c4cad4;
       display:flex; height:100vh; overflow:hidden; }

#sidebar {
  width:256px; min-width:256px; flex-shrink:0;
  background:#0a0e14; border-right:1px solid #1e2a3a;
  display:flex; flex-direction:column; overflow:hidden;
}
.sb-head {
  padding:12px 14px 10px; flex-shrink:0;
  border-bottom:1px solid #1e2a3a;
}
.sb-head h2 { font-size:11px; font-weight:600; color:#7eb8da; letter-spacing:.5px; margin-bottom:8px; }

/* freshness badge */
#freshness {
  font-size:9px; padding:4px 8px; border-radius:3px;
  background:#0d1c2a; border:1px solid #1e3a52; color:#7eb8da;
  display:inline-block; line-height:1.5;
}
#freshness.stale { background:#1c160a; border-color:#3a2a10; color:#a08030; }

/* stats strip */
#sb-stats {
  padding:8px 14px; flex-shrink:0;
  border-bottom:1px solid #1e2a3a;
  display:flex; flex-wrap:wrap; gap:6px 14px;
}
.stat { display:flex; flex-direction:column; }
.stat-lbl { font-size:8px; color:#3a4a5a; text-transform:uppercase; letter-spacing:.3px; }
.stat-val { font-size:11px; font-weight:700; color:#7eb8da; }

/* search */
#search-wrap {
  padding:7px 10px; flex-shrink:0;
  border-bottom:1px solid #1e2a3a;
}
#search {
  width:100%; background:#0d1118; border:1px solid #1e2a3a;
  border-radius:4px; color:#c4cad4; font-family:inherit;
  font-size:10px; padding:5px 8px; outline:none;
}
#search:focus { border-color:#3a6a9a; }
#search::placeholder { color:#3a4a5a; }

/* vessel list */
#vessel-list {
  flex:1; overflow-y:auto; display:none;
}
#vessel-list::-webkit-scrollbar { width:3px; }
#vessel-list::-webkit-scrollbar-thumb { background:#1e2a3a; border-radius:2px; }
.vl-item {
  padding:5px 14px; font-size:10px; cursor:pointer;
  border-bottom:1px solid #0e1620; display:flex;
  align-items:center; gap:7px; transition:background .1s;
}
.vl-item:hover  { background:#0f1a24; }
.vl-item.active { background:#0f1e2e; }
.vl-dot { width:7px; height:7px; border-radius:50%; flex-shrink:0; }
.vl-name { flex:1; color:#c4cad4; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.vl-age  { font-size:9px; color:#3a4a5a; white-space:nowrap; }
.vl-age.fresh { color:#3a7a3a; }
.vl-age.recent { color:#7a7a3a; }

/* vessel detail */
#vessel-detail {
  flex:1; overflow-y:auto; display:flex; flex-direction:column;
}
#vessel-detail::-webkit-scrollbar { width:3px; }
#vessel-detail::-webkit-scrollbar-thumb { background:#1e2a3a; border-radius:2px; }
.vd-hint { padding:20px 14px; font-size:10px; color:#3a4a5a; line-height:1.7; }
.vd-back {
  padding:7px 14px; font-size:9px; color:#3a6a9a; cursor:pointer;
  border-bottom:1px solid #1e2a3a; display:none; letter-spacing:.3px;
}
.vd-back:hover { color:#7eb8da; }
.vd-name { padding:10px 14px 4px; font-size:13px; font-weight:700; color:#c4cad4; }
.vd-sub  { padding:0 14px 8px; font-size:9px; color:#6b7785; line-height:1.6; }
.vd-row {
  padding:4px 14px; display:flex; justify-content:space-between;
  border-bottom:1px solid #0e1620; font-size:10px;
}
.vd-lbl { color:#4a5a6a; }
.vd-val { color:#c4cad4; font-weight:600; text-align:right; max-width:150px; word-break:break-word; }
.vd-section {
  padding:6px 14px 3px; font-size:8px; font-weight:700;
  color:#3a4a5a; letter-spacing:.4px; text-transform:uppercase;
  border-top:1px solid #141c26; margin-top:4px;
}

/* legend */
#legend {
  flex-shrink:0; padding:8px 14px 10px;
  border-top:1px solid #1e2a3a;
  display:grid; grid-template-columns:1fr 1fr; gap:3px 6px;
}
.lg-item { display:flex; align-items:center; gap:5px; font-size:9px; color:#4a5a6a; }
.lg-dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; }

/* trails toggle */
#trail-btn {
  flex-shrink:0; margin:6px 10px;
  padding:5px 10px; background:#0d1118; border:1px solid #1e2a3a;
  border-radius:4px; color:#6b7785; font-family:inherit; font-size:9px;
  cursor:pointer; letter-spacing:.3px; text-transform:uppercase; transition:all .15s;
}
#trail-btn:hover  { background:#1a2530; color:#c4cad4; border-color:#3a5a7a; }
#trail-btn.active { background:#1a2d3d; color:#7eb8da; border-color:#2a4a6a; }

/* map */
#map { flex:1; }

/* Leaflet popup overrides */
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
.pp-name { color:#7eb8da; font-size:12px; font-weight:700; display:block; margin-bottom:2px; }
.pp-sub  { color:#4a5a6a; font-size:9px;  display:block; margin-bottom:5px; }
.pp-sep  { border:none; border-top:1px solid #1e2a3a; margin:5px 0; }
.pp-row  { display:flex; justify-content:space-between; gap:12px; }
.pp-lbl  { color:#6b7785; }
.pp-val  { color:#c4cad4; font-weight:600; }
.pp-age  { font-size:9px; color:#6b7785; margin-top:4px; }
</style>
</head>
<body>

<!-- ── Sidebar ── -->
<div id="sidebar">
  <div class="sb-head">
    <h2>⚓ NOCTURNAL  ·  Live AIS</h2>
    <div id="freshness">—</div>
  </div>
  <div id="sb-stats">
    <div class="stat"><div class="stat-lbl">Vessels</div><div class="stat-val" id="st-vessels">—</div></div>
    <div class="stat"><div class="stat-lbl">Window</div><div class="stat-val"  id="st-window">—</div></div>
  </div>
  <div id="search-wrap">
    <input id="search" type="text" placeholder="Search vessel name / MMSI…">
  </div>

  <!-- vessel list (search results) -->
  <div id="vessel-list"></div>

  <!-- vessel detail (click on map) -->
  <div id="vessel-detail">
    <div class="vd-back" id="vd-back" onclick="showList()">← back to list</div>
    <div class="vd-hint" id="vd-hint">Click a vessel on the map<br>to inspect its details.</div>
    <div id="vd-content"></div>
  </div>

  <button id="trail-btn" class="active" onclick="toggleTrails()">Trails on</button>
  <div id="legend">
    <div class="lg-item"><div class="lg-dot" style="background:#e05555"></div>Tanker</div>
    <div class="lg-item"><div class="lg-dot" style="background:#3a9a50"></div>Cargo</div>
    <div class="lg-item"><div class="lg-dot" style="background:#3a7ae0"></div>Passenger</div>
    <div class="lg-item"><div class="lg-dot" style="background:#d4a030"></div>Fishing</div>
    <div class="lg-item"><div class="lg-dot" style="background:#9a50c0"></div>Tug/Special</div>
    <div class="lg-item"><div class="lg-dot" style="background:#d46020"></div>High-speed</div>
    <div class="lg-item"><div class="lg-dot" style="background:#30a0a0"></div>Sail/Leisure</div>
    <div class="lg-item"><div class="lg-dot" style="background:#607080"></div>Other</div>
  </div>
</div>

<!-- ── Map ── -->
<div id="map"></div>

<script>
// ════════════════════════════════════════════════════════════════════════
//  Data injected by Python
// ════════════════════════════════════════════════════════════════════════
const DATA        = __DATA_JSON__;
const AOI_GEOJSON = __AOI_GEOJSON__;

const NOW_TS    = DATA.now;
const LATEST_TS = DATA.latest_ts;
const HOURS     = DATA.hours;

// ════════════════════════════════════════════════════════════════════════
//  Map
// ════════════════════════════════════════════════════════════════════════
const map = L.map('map', {zoomControl:true});

const vessels = DATA.vessels;
let sumLat=0, sumLon=0;
vessels.forEach(v => { sumLat+=v.lat; sumLon+=v.lon; });
map.setView(vessels.length ? [sumLat/vessels.length, sumLon/vessels.length] : [50.0,-1.0], 8);

L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{
  attribution:'&copy; <a href="https://www.openstreetmap.org/">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
  maxZoom:18
}).addTo(map);

if(AOI_GEOJSON){
  L.geoJSON(AOI_GEOJSON,{
    style:{color:'#7eb8da',weight:2,opacity:0.4,fill:true,
           fillColor:'#7eb8da',fillOpacity:0.03,dashArray:'7 5'}
  }).addTo(map);
}

// ════════════════════════════════════════════════════════════════════════
//  Helpers
// ════════════════════════════════════════════════════════════════════════
const C16 = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW'];
const cdir = d => d==null?'—': C16[Math.round(d/22.5)%16];
const kts  = s => s==null?'—': s.toFixed(1)+' kn';

function fmtTs(ts){
  const d=new Date(ts*1000);
  return d.getUTCFullYear()+'-'+
    String(d.getUTCMonth()+1).padStart(2,'0')+'-'+
    String(d.getUTCDate()).padStart(2,'0')+'  '+
    String(d.getUTCHours()).padStart(2,'0')+':'+
    String(d.getUTCMinutes()).padStart(2,'0')+' UTC';
}

function ageStr(ts){
  const s = NOW_TS - ts;
  if(s < 60)   return `${s}s ago`;
  if(s < 3600) return `${Math.round(s/60)}m ago`;
  return `${(s/3600).toFixed(1)}h ago`;
}

function ageClass(ts){
  const s = NOW_TS - ts;
  if(s < 1800)  return 'fresh';   // < 30 min
  if(s < 7200)  return 'recent';  // < 2 h
  return '';
}

// opacity fades with staleness: 1.0 for <30 min, down to 0.25 for oldest
const OLD_SEC = HOURS * 3600;
function staleFrac(ts){ return Math.max(0, Math.min(1, (NOW_TS-ts)/OLD_SEC)); }
function markerOpacity(ts){ return 0.25 + 0.75*(1-staleFrac(ts)); }

// ════════════════════════════════════════════════════════════════════════
//  Markers + trails
// ════════════════════════════════════════════════════════════════════════
let trailsOn = true;
let selectedMMSI = null;

const dotGroup   = L.layerGroup().addTo(map);
const trailGroup = L.layerGroup().addTo(map);

// Build marker + trail for each vessel
const store = {};  // mmsi → {v, marker, trail}

vessels.forEach(v => {
  const op = markerOpacity(v.last_ts);
  const selCol = '#e0a040';

  const marker = L.circleMarker([v.lat, v.lon], {
    radius:5, color:v.color, fillColor:v.color,
    fillOpacity: op * 0.85, weight:1.5, opacity:op,
  });

  const trail = L.polyline(v.trail, {
    color:v.color, weight:1.5, opacity: op * 0.4, interactive:false
  });

  marker.bindPopup('', {maxWidth:260});
  marker.on('click', e => {
    L.DomEvent.stopPropagation(e);
    selectVessel(v.mmsi);
  });

  dotGroup.addLayer(marker);
  if(trailsOn && v.trail.length > 1) trailGroup.addLayer(trail);

  store[v.mmsi] = {v, marker, trail};
});

map.on('click', () => {
  if(selectedMMSI){ deselectVessel(); clearDetail(); }
});

// ════════════════════════════════════════════════════════════════════════
//  Select / deselect
// ════════════════════════════════════════════════════════════════════════
function selectVessel(mmsi){
  if(selectedMMSI) deselectVessel(false);
  selectedMMSI = mmsi;
  const {v, marker, trail} = store[mmsi];
  const op = markerOpacity(v.last_ts);
  marker.setStyle({
    radius:8, color:'#e0a040', fillColor:'#e0a040',
    fillOpacity:0.95, weight:2.5, opacity:1
  });
  if(trailsOn && trail){
    trail.setStyle({color:'#e0a040', weight:2.5, opacity:0.65});
  }
  marker.setPopupContent(buildPopup(v));
  marker.openPopup();
  showDetail(v);
}

function deselectVessel(updateDetail=true){
  if(!selectedMMSI) return;
  const {v, marker, trail} = store[selectedMMSI];
  const op = markerOpacity(v.last_ts);
  marker.setStyle({
    radius:5, color:v.color, fillColor:v.color,
    fillOpacity:op*0.85, weight:1.5, opacity:op
  });
  if(trail) trail.setStyle({color:v.color, weight:1.5, opacity:op*0.4});
  selectedMMSI = null;
  if(updateDetail) clearDetail();
}

// ════════════════════════════════════════════════════════════════════════
//  Trails toggle
// ════════════════════════════════════════════════════════════════════════
function toggleTrails(){
  trailsOn = !trailsOn;
  const btn = document.getElementById('trail-btn');
  btn.textContent = trailsOn ? 'Trails on' : 'Trails off';
  btn.classList.toggle('active', trailsOn);
  trailGroup.clearLayers();
  if(trailsOn){
    vessels.forEach(v => {
      const {trail} = store[v.mmsi];
      if(v.trail.length > 1) trailGroup.addLayer(trail);
    });
  }
}

// ════════════════════════════════════════════════════════════════════════
//  Sidebar — detail panel
// ════════════════════════════════════════════════════════════════════════
function row(lbl, val){
  return `<div class="vd-row"><span class="vd-lbl">${lbl}</span><span class="vd-val">${val||'—'}</span></div>`;
}
function section(lbl){ return `<div class="vd-section">${lbl}</div>`; }

function showDetail(v){
  document.getElementById('vd-hint').style.display   = 'none';
  document.getElementById('vessel-list').style.display = 'none';
  document.getElementById('vd-back').style.display   = 'block';
  document.getElementById('vessel-detail').style.display = 'flex';

  const name = v.name || 'Unknown vessel';
  let html = section('Identity');
  html += row('Name', v.name || '—');
  html += row('MMSI',
    `<a href="https://www.marinetraffic.com/en/ais/details/ships/mmsi:${v.mmsi}" `+
    `target="_blank" style="color:#7eb8da">${v.mmsi} ↗</a>`);
  html += row('Type',     `${v.type_label}${v.ship_type?' ('+v.ship_type+')':''}`);
  html += row('Callsign', v.callsign);
  html += row('IMO',      v.imo ? `IMO ${v.imo}` : null);

  html += section('Position');
  html += row('Latitude',  `${v.lat.toFixed(4)}°N`);
  html += row('Longitude', `${v.lon >= 0 ? '+' : ''}${v.lon.toFixed(4)}°E`);
  html += row('Speed',     kts(v.sog));
  html += row('Course',    v.cog != null ? `${v.cog.toFixed(0)}°  ${cdir(v.cog)}` : '—');

  html += section('Track');
  html += row('Last seen', fmtTs(v.last_ts));
  html += row('Age',       ageStr(v.last_ts));
  html += row('Pings (window)', v.trail.length.toLocaleString());

  document.getElementById('vd-content').innerHTML = html;

  // update sb-head with name
  document.querySelector('.sb-head h2').textContent = '⚓ ' + name;
}

function clearDetail(){
  document.getElementById('vd-hint').style.display   = 'block';
  document.getElementById('vd-back').style.display   = 'none';
  document.getElementById('vd-content').innerHTML    = '';
  document.querySelector('.sb-head h2').textContent  = '⚓ NOCTURNAL  ·  Live AIS';
}

function showList(){
  deselectVessel(false);
  clearDetail();
  if(document.getElementById('search').value.trim()){
    document.getElementById('vessel-list').style.display = 'block';
    document.getElementById('vessel-detail').style.display = 'none';
  }
}

// ════════════════════════════════════════════════════════════════════════
//  Popup
// ════════════════════════════════════════════════════════════════════════
function buildPopup(v){
  const name = v.name || 'Unknown vessel';
  let html = `<span class="pp-name">${name}</span>`;
  html += `<span class="pp-sub">MMSI: ${v.mmsi}  ·  ${v.type_label}</span>`;
  html += `<hr class="pp-sep">`;
  html += `<div class="pp-row"><span class="pp-lbl">Speed</span><span class="pp-val">${kts(v.sog)}</span></div>`;
  html += `<div class="pp-row"><span class="pp-lbl">Course</span><span class="pp-val">${v.cog!=null?v.cog.toFixed(0)+'°  '+cdir(v.cog):'—'}</span></div>`;
  html += `<div class="pp-row"><span class="pp-lbl">Lat / Lon</span><span class="pp-val">${v.lat.toFixed(4)}°N  ${v.lon>=0?'+':''}${v.lon.toFixed(4)}°E</span></div>`;
  html += `<div class="pp-age">Last seen ${ageStr(v.last_ts)}</div>`;
  return html;
}

// ════════════════════════════════════════════════════════════════════════
//  Search
// ════════════════════════════════════════════════════════════════════════
const searchEl = document.getElementById('search');
const listEl   = document.getElementById('vessel-list');

searchEl.addEventListener('input', () => {
  const q = searchEl.value.trim().toLowerCase();
  if(!q){ listEl.style.display='none'; listEl.innerHTML=''; return; }

  const hits = vessels.filter(v =>
    (v.name  && v.name.toLowerCase().includes(q)) ||
    String(v.mmsi).includes(q)
  ).slice(0, 40);

  listEl.innerHTML = hits.map(v => {
    const name = v.name || `MMSI ${v.mmsi}`;
    const ac = ageClass(v.last_ts);
    return `<div class="vl-item" data-mmsi="${v.mmsi}" onclick="searchSelect(${v.mmsi})">
      <div class="vl-dot" style="background:${v.color}"></div>
      <span class="vl-name">${name}</span>
      <span class="vl-age ${ac}">${ageStr(v.last_ts)}</span>
    </div>`;
  }).join('');

  document.getElementById('vessel-detail').style.display = 'none';
  listEl.style.display = hits.length ? 'block' : 'none';
});

function searchSelect(mmsi){
  searchEl.value = '';
  listEl.style.display = 'none';
  document.getElementById('vessel-detail').style.display = 'flex';
  const {v, marker} = store[mmsi];
  map.panTo([v.lat, v.lon]);
  selectVessel(mmsi);
}

// ════════════════════════════════════════════════════════════════════════
//  Init — static stats + freshness badge
// ════════════════════════════════════════════════════════════════════════
(()=>{
  document.getElementById('st-vessels').textContent = vessels.length.toLocaleString();
  document.getElementById('st-window').textContent  = `${HOURS}h`;

  const staleMin = Math.round((NOW_TS - LATEST_TS) / 60);
  const fresh    = document.getElementById('freshness');
  const timeStr  = new Date(LATEST_TS*1000).toUTCString().replace(' GMT','') + ' UTC';
  fresh.textContent = `Data as of ${timeStr}  (${staleMin}m ago)`;
  if(staleMin > 30) fresh.classList.add('stale');
})();
</script>
</body>
</html>"""


# ── Main ───────────────────────────────────────────────────────────────────────

def build(db_path: Path, hours: int, out_path: Path):
    data     = load_snapshot(db_path, hours)
    aoi_json = AOI_FILE.read_text(encoding="utf-8") if AOI_FILE.exists() else "null"

    html = HTML \
        .replace("__DATA_JSON__",   json.dumps(data, separators=(',', ':'))) \
        .replace("__AOI_GEOJSON__", aoi_json)

    out_path.write_text(html, encoding="utf-8")
    size_kb = out_path.stat().st_size >> 10
    print(f"\nSaved → {out_path.resolve()}  ({size_kb} KB)")


def main():
    ap = argparse.ArgumentParser(
        description="Generate live-position AIS map for NOCTURNAL"
    )
    ap.add_argument("--db",    type=Path, default=DEFAULT_DB)
    ap.add_argument("--out",   type=Path, default=DEFAULT_OUT)
    ap.add_argument("--hours", type=int,  default=DEFAULT_HOURS,
                    help=f"Lookback window in hours (default: {DEFAULT_HOURS})")
    ap.add_argument("--watch", type=int,  default=None, metavar="MIN",
                    help="Rebuild every MIN minutes (keeps running; open HTML in browser)")
    args = ap.parse_args()

    print("NOCTURNAL — Live AIS Map")
    print("─" * 40)
    print(f"  DB   : {args.db}  ({args.db.stat().st_size >> 30} GB)")

    if args.watch:
        print(f"  Watch: rebuilding every {args.watch} min — open {args.out} in browser")
        print("  Press Ctrl+C to stop.\n")
        while True:
            try:
                build(args.db, args.hours, args.out)
                print(f"  Next rebuild in {args.watch} min…\n")
                _time.sleep(args.watch * 60)
            except KeyboardInterrupt:
                print("\n[stopped]")
                break
    else:
        build(args.db, args.hours, args.out)
        print("Open ais_live_map.html in your browser.")


if __name__ == "__main__":
    main()
