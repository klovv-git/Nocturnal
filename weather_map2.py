#!/usr/bin/env python3
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
"""
weather_map2.py — Weather forecast map, weather_sectors.html aesthetic.

Dark CARTO tiles + AOI outline only (no hex grid, no dots).  Click
anywhere inside the AOI to pull the nearest Storm Glass grid point and
browse the full 48-hour forecast in a floating panel.

By default the script rebuilds every 20 minutes and the browser
auto-refreshes to match, so you always see the latest forecast data.

Usage:
    python weather_map2.py                # rebuild every 20 min (default)
    python weather_map2.py --watch 10     # rebuild every 10 min
    python weather_map2.py --once         # single build, no auto-refresh
"""

import argparse
import json
import sqlite3
import time as _time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests as _req
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

import config as _cfg

DEFAULT_DB  = Path(str(_cfg.WEATHER_DB_PATH))
AOI_FILE    = Path(__file__).parent / "aoi.geojson"
DEFAULT_OUT = Path("weather_map2.html")


def _r(v, d=1):
    return round(float(v), d) if v is not None else None


def fetch_grid_depths(pts: list) -> dict:
    """
    Batch-fetch sea-floor depths for a list of (lat, lon) tuples.
    Returns {(lat, lon): depth_m} using GEBCO 2020 via OpenTopoData.
    depth_m is positive = metres below sea level; None on failure.
    """
    if not _HAS_REQUESTS or not pts:
        return {}
    locations = "|".join(f"{lat},{lon}" for lat, lon in pts)
    url = f"https://api.opentopodata.org/v1/gebco2020?locations={locations}"
    try:
        r = _req.get(url, timeout=15, headers={"User-Agent": "NOCTURNAL/1.0"})
        r.raise_for_status()
        results = r.json().get("results", [])
        depths = {}
        for item in results:
            loc  = item.get("location", {})
            elev = item.get("elevation")
            la   = round(float(loc.get("lat", 0)), 4)
            lo   = round(float(loc.get("lng", 0)), 4)
            depths[(la, lo)] = round(-float(elev), 1) if elev is not None else None
        return depths
    except Exception as exc:
        print(f"  [bathymetry] fetch failed: {exc}")
        return {}


def load_data(db_path: Path) -> dict:
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}\nRun the daemon first.")
    conn = sqlite3.connect(str(db_path))
    pts  = conn.execute(
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
                   current_speed, current_dir, precip, humidity, sea_level
            FROM weather_obs WHERE lat=? AND lon=? ORDER BY ts_epoch
        """, (lat, lon)).fetchall()
        bio_rows = conn.execute("""
            SELECT ts_epoch, chlorophyll, phytoplankton, salinity,
                   water_temp, ice_cover, oxygen, nitrate, phosphate, silicate
            FROM bio_obs WHERE lat=? AND lon=? ORDER BY ts_epoch
        """, (lat, lon)).fetchall()
        tide_rows = conn.execute(
            "SELECT ts_epoch, height FROM tide_obs WHERE lat=? AND lon=? ORDER BY ts_epoch",
            (lat, lon)).fetchall()
        extr_rows = conn.execute(
            "SELECT ts_epoch, height, type FROM tide_extremes WHERE lat=? AND lon=? ORDER BY ts_epoch",
            (lat, lon)).fetchall()

        wx = []
        for r in wx_rows:
            ts = int(r[0])
            if ts < t_min: t_min = ts
            if ts > t_max: t_max = ts
            wx.append([ts,
                _r(r[1],1), _r(r[2],0), _r(r[3],1),
                _r(r[4],2), _r(r[5],0), _r(r[6],1),
                _r(r[7],2), _r(r[8],0), _r(r[9],1),
                _r(r[10],0), _r(r[11],0), _r(r[12],1), _r(r[13],0),
                _r(r[14],2), _r(r[15],0), _r(r[16],2), _r(r[17],0),
            ])
        bio = [[int(r[0]),
                _r(r[1],3), _r(r[2],1), _r(r[3],1),
                _r(r[4],1), _r(r[5],3), _r(r[6],1),
                _r(r[7],2), _r(r[8],3), _r(r[9],2)]
               for r in bio_rows]
        tide = [[int(r[0]), _r(r[1],2)] for r in tide_rows]
        extr = [[int(r[0]), _r(r[1],2), r[2]] for r in extr_rows]

        grid.append({"n": f"h{i:02d}", "lat": round(lat,4), "lon": round(lon,4),
                     "wx": wx, "bio": bio, "tide": tide, "extr": extr})

    conn.close()

    # Fetch bathymetry depths for all grid points in one batch request
    print(f"  Fetching bathymetry for {len(grid)} grid points ...", end=" ", flush=True)
    depths = fetch_grid_depths([(p["lat"], p["lon"]) for p in grid])
    for p in grid:
        p["depth"] = depths.get((p["lat"], p["lon"]))
    n_ok = sum(1 for p in grid if p["depth"] is not None)
    print(f"{n_ok}/{len(grid)} depths fetched")

    now_ts = int(datetime.now(timezone.utc).replace(
        minute=0, second=0, microsecond=0).timestamp())
    print(f"  {len(grid)} grid points  |  "
          f"{datetime.utcfromtimestamp(t_min).strftime('%Y-%m-%d %H:%M')} → "
          f"{datetime.utcfromtimestamp(t_max).strftime('%Y-%m-%d %H:%M')} UTC")
    return {"pts": grid, "t_min": int(t_min), "t_max": int(t_max), "now": now_ts}


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
__AUTO_REFRESH__
<title>NOCTURNAL — Weather</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'SF Mono','Consolas','Menlo',monospace;
       background:#0a0e14; color:#c4cad4;
       display:flex; flex-direction:column; height:100vh; overflow:hidden; }

#top { display:flex; flex:1; min-height:0; }
#map-container { flex:1; position:relative; min-width:0; }
#map { width:100%; height:100%; cursor:crosshair; }

/* ── left sidebar ── */
#info-panel {
  width:272px; min-width:272px; flex-shrink:0;
  background:#0a0e14; border-right:1px solid #1e2a3a;
  display:flex; flex-direction:column; overflow:hidden;
}
.panel-head {
  padding:12px 14px 10px; flex-shrink:0;
  border-bottom:1px solid #1e2a3a;
}
.panel-head h2 {
  font-size:11px; font-weight:600; color:#7eb8da;
  letter-spacing:.5px; margin-bottom:8px;
}
#point-name {
  font-size:12px; font-weight:700; color:#c4cad4; margin-bottom:2px;
}
#point-sub  { font-size:9px; color:#6b7785; line-height:1.7; }
#click-hint { font-size:10px; color:#3a4a5a; line-height:1.7; }

/* ── tab bar ── */
#tab-bar {
  display:flex; gap:4px; padding:8px 14px 8px; flex-shrink:0;
  border-bottom:1px solid #1e2a3a;
}
.tab-btn {
  flex:1; padding:4px 0;
  background:#0d1118; border:1px solid #1e2a3a; border-radius:4px;
  color:#6b7785; font-family:inherit; font-size:9px;
  cursor:pointer; letter-spacing:.3px; text-transform:uppercase;
  transition:all .15s;
}
.tab-btn:hover { background:#1a2530; color:#c4cad4; border-color:#3a5a7a; }
.tab-btn.active { background:#1a2d3d; color:#7eb8da; border-color:#2a4a6a; }

/* ── entry list ── */
#entry-list {
  flex:1; overflow-y:auto; min-height:0; padding:4px 0;
}
#entry-list::-webkit-scrollbar { width:3px; }
#entry-list::-webkit-scrollbar-track { background:transparent; }
#entry-list::-webkit-scrollbar-thumb { background:#1e2a3a; border-radius:2px; }

.date-hdr {
  padding:4px 14px 3px; font-size:9px; font-weight:700;
  color:#3a4a5a; letter-spacing:.4px; text-transform:uppercase;
  border-top:1px solid #141c26; margin-top:4px;
}
.date-hdr:first-child { border-top:none; margin-top:0; }

.entry-row {
  padding:6px 14px; cursor:pointer; transition:background .1s;
  border-left:2px solid transparent;
}
.entry-row:hover { background:rgba(126,184,218,.06); }
.entry-row.current {
  background:rgba(126,184,218,.10); border-left-color:#7eb8da;
}
.e-time {
  font-size:10px; font-weight:700; color:#5a6f85; margin-bottom:4px;
}
.entry-row.current .e-time { color:#7eb8da; }

.e-grid { display:grid; grid-template-columns:1fr 1fr; gap:3px 8px; }
.ef { display:flex; flex-direction:column; }
.ef-lbl { font-size:8px; color:#3a4a5a; text-transform:uppercase;
          letter-spacing:.3px; margin-bottom:1px; }
.ef-val { font-size:10px; color:#b0bdc9; }

/* extreme events */
.ext-evt {
  padding:5px 14px; display:flex; align-items:center; gap:8px;
  border-left:2px solid transparent;
}
.ext-badge {
  font-size:8px; font-weight:700; padding:1px 6px; border-radius:3px;
  letter-spacing:.3px; text-transform:uppercase;
}
.ext-high { background:rgba(220,60,40,.15); color:#e05040;
            border:1px solid rgba(220,60,40,.3); }
.ext-low  { background:rgba(60,140,220,.15); color:#5090d0;
            border:1px solid rgba(60,140,220,.3); }
.ext-time { font-size:9px; color:#6b7785; }
.ext-ht   { font-size:9px; color:#c4cad4; margin-left:auto; }

/* sparkline */
.spark-wrap { padding:10px 14px 6px; border-bottom:1px solid #141c26; flex-shrink:0; }
.spark-wrap svg { display:block; width:100%; }
.spark-lbl { display:flex; justify-content:space-between;
             font-size:8px; color:#3a4a5a; margin-top:3px; }

/* ── bottom timeline bar ── */
#timeline-bar {
  flex-shrink:0; background:#0a0e14; border-top:1px solid #1e2a3a;
  padding:8px 16px 10px; display:flex; align-items:center; gap:14px;
  user-select:none;
}
#time-label { font-size:10px; color:#7eb8da; letter-spacing:.3px;
              white-space:nowrap; min-width:192px; }
.slider-wrap { position:relative; flex:1; }
#timeline-slider { width:100%; cursor:pointer; accent-color:#7eb8da; }
#now-mark {
  position:absolute; top:-10px;
  font-size:8px; color:#7eb8da; transform:translateX(-50%);
  pointer-events:none;
}

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
.px-lbl { color:#6b7785; }
.px-val { color:#c4cad4; font-weight:600; }
.px-hd  { color:#7eb8da; font-size:11px; font-weight:700;
          display:block; margin-bottom:6px; }
.px-sep { border:none; border-top:1px solid #1e2a3a; margin:5px 0; }
</style>
</head>
<body>
<div id="top">

  <!-- ── Left sidebar ── -->
  <div id="info-panel">
    <div class="panel-head">
      <h2>⛅ NOCTURNAL WEATHER</h2>
      <div id="point-name">—</div>
      <div id="point-sub">
        <span id="click-hint">Click anywhere on the map<br>to load forecast data.</span>
      </div>
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
    <div id="map"></div>
  </div>

</div>

<!-- ── Timeline bar ── -->
<div id="timeline-bar">
  <div id="time-label">——</div>
  <div class="slider-wrap">
    <div id="now-mark" style="display:none">▼</div>
    <input type="range" id="timeline-slider" min="0" max="1000" value="0">
  </div>
</div>

<script>
// ════════════════════════════════════════════════════════════════════════
//  Data
// ════════════════════════════════════════════════════════════════════════
const DATA        = __DATA_JSON__;
const AOI_GEOJSON = __AOI_GEOJSON__;

// wx indices
const I_TS=0,I_WS=1,I_WD=2,I_WG=3,
      I_WH=4,I_WDR=5,I_WP=6,
      I_SH=7,I_SDR=8,I_SP=9,
      I_VIS=10,I_CC=11,I_AT=12,I_PR=13,
      I_CS=14,I_CDR=15,I_PREC=16,I_HUM=17;
const B_TS=0,B_CHL=1,B_PHY=2,B_SAL=3,
      B_WT=4,B_ICE=5,B_O2=6,B_NO3=7,B_PO4=8,B_SIO=9;

// ════════════════════════════════════════════════════════════════════════
//  Map
// ════════════════════════════════════════════════════════════════════════
const cLat = DATA.pts.reduce((s,p)=>s+p.lat,0)/DATA.pts.length;
const cLon = DATA.pts.reduce((s,p)=>s+p.lon,0)/DATA.pts.length;

const map = L.map('map',{zoomControl:true}).setView([cLat,cLon],8);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{
  attribution:'&copy; <a href="https://www.openstreetmap.org/">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
  maxZoom:18
}).addTo(map);

// AOI outline only — no hex polygons, no centre dots
if (AOI_GEOJSON) {
  L.geoJSON(AOI_GEOJSON,{
    style:{color:'#7eb8da',weight:2,opacity:0.55,
           fill:true,fillColor:'#7eb8da',fillOpacity:0.03,dashArray:'7 5'}
  }).addTo(map);
}

// ════════════════════════════════════════════════════════════════════════
//  State
// ════════════════════════════════════════════════════════════════════════
let selectedPoint = null;
let clickMarker   = null;
let activeTab     = 'wx';

const slider    = document.getElementById('timeline-slider');
const timeLabel = document.getElementById('time-label');

// ════════════════════════════════════════════════════════════════════════
//  Helpers
// ════════════════════════════════════════════════════════════════════════
const C16 = ['N','NNE','NE','ENE','E','ESE','SE','SSE',
             'S','SSW','SW','WSW','W','WNW','NW','NNW'];
const cdir = d => d==null ? '' : C16[Math.round(d/22.5)%16];

function fmtTs(ts, mode) {
  const d = new Date(ts*1000);
  const Y = d.getUTCFullYear(),
        M = String(d.getUTCMonth()+1).padStart(2,'0'),
        D = String(d.getUTCDate()).padStart(2,'0'),
        h = String(d.getUTCHours()).padStart(2,'0'),
        m = String(d.getUTCMinutes()).padStart(2,'0');
  if (mode==='time') return `${h}:${m} UTC`;
  if (mode==='date') return `${Y}-${M}-${D}`;
  return `${Y}-${M}-${D}  ${h}:${m} UTC`;
}
function fmtDay(ts) {
  const d = new Date(ts*1000);
  const days=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const mos=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return `${days[d.getUTCDay()]}  ${d.getUTCDate()} ${mos[d.getUTCMonth()]}  ${d.getUTCFullYear()}`;
}

function currentTs() {
  return DATA.t_min + (slider.value/1000)*(DATA.t_max-DATA.t_min);
}
function tsToSlider(ts) {
  return Math.round((ts-DATA.t_min)/(DATA.t_max-DATA.t_min)*1000);
}

function v(x, unit, dec) {
  if (x==null) return '—';
  return (+x).toFixed(dec!=null?dec:0)+(unit||'');
}
function ef(lbl, val) {
  return `<div class="ef"><div class="ef-lbl">${lbl}</div><div class="ef-val">${val}</div></div>`;
}

function nearestRow(rows, ts) {
  if (!rows||!rows.length) return null;
  let best=rows[0], bd=Math.abs(rows[0][0]-ts);
  for (let i=1;i<rows.length;i++){
    const d=Math.abs(rows[i][0]-ts);
    if(d<bd){bd=d;best=rows[i];}
    if(rows[i][0]>ts+7200)break;
  }
  return best;
}
function nearestPoint(lat,lon) {
  let best=null,bd2=Infinity;
  DATA.pts.forEach(p=>{
    const dlat=p.lat-lat, dlon=(p.lon-lon)*Math.cos(lat*Math.PI/180);
    const d2=dlat*dlat+dlon*dlon;
    if(d2<bd2){bd2=d2;best=p;}
  });
  return best;
}
function distKm(lat1,lon1,lat2,lon2) {
  const R=6371, dLat=(lat2-lat1)*Math.PI/180, dLon=(lon2-lon1)*Math.PI/180;
  const a=Math.sin(dLat/2)**2+Math.cos(lat1*Math.PI/180)*Math.cos(lat2*Math.PI/180)*Math.sin(dLon/2)**2;
  return R*2*Math.asin(Math.sqrt(a));
}

// ════════════════════════════════════════════════════════════════════════
//  Build tab content
// ════════════════════════════════════════════════════════════════════════
function buildWx(point) {
  const tideMap={};
  point.tide.forEach(r=>{tideMap[r[0]]=r[1];});

  // merge wx rows + extreme events, sort by ts
  const items=[];
  point.wx.forEach(r=>items.push({ts:r[0],type:'wx',r}));
  point.extr.forEach(r=>items.push({ts:r[0],type:'ext',r}));
  items.sort((a,b)=>a.ts-b.ts);

  let html='', lastDay='';
  items.forEach(item=>{
    const day=fmtDay(item.ts);
    if(day!==lastDay){
      html+=`<div class="date-hdr">${day}</div>`;
      lastDay=day;
    }
    if(item.type==='ext'){
      const r=item.r;
      const cls=r[2]==='high'?'ext-high':'ext-low';
      const lbl=r[2]==='high'?'HIGH TIDE':'LOW TIDE';
      html+=`<div class="ext-evt">
        <span class="ext-badge ${cls}">${lbl}</span>
        <span class="ext-time">${fmtTs(r[0],'time')}</span>
        <span class="ext-ht">${v(r[1],'m',2)}</span>
      </div>`;
      return;
    }
    const r=item.r, ts=r[I_TS];
    const tH=tideMap[ts];

    let wind=v(r[I_WS],' m/s',1);
    if(r[I_WD]!=null) wind+=`  ${cdir(r[I_WD])}`;
    if(r[I_WG]!=null) wind+=`  g${v(r[I_WG],'',1)}`;

    let wave=v(r[I_WH],'m',2);
    if(r[I_WDR]!=null) wave+=`  ${cdir(r[I_WDR])}`;
    if(r[I_WP] !=null) wave+=`  ${v(r[I_WP],'s',0)}`;

    let swell=v(r[I_SH],'m',2);
    if(r[I_SDR]!=null) swell+=`  ${cdir(r[I_SDR])}`;
    if(r[I_SP] !=null) swell+=`  ${v(r[I_SP],'s',0)}`;

    const tide = tH!=null ? v(tH,'m',2) : '—';

    html+=`<div class="entry-row" data-ts="${ts}">
      <div class="e-time">${fmtTs(ts,'time')}</div>
      <div class="e-grid">
        ${ef('Wind',  wind)}
        ${ef('Waves', wave)}
        ${ef('Swell', swell)}
        ${ef('Tide',  tide)}
        ${ef('Air',   v(r[I_AT],'°C',1))}
        ${ef('Press', v(r[I_PR],' hPa',0))}
        ${ef('Vis',   v(r[I_VIS],' km',0))}
        ${ef('Cloud', v(r[I_CC],'%',0))}
        ${r[I_HUM]  !=null ? ef('Humidity',v(r[I_HUM],'%',0)) : ''}
        ${r[I_PREC] !=null ? ef('Precip',  v(r[I_PREC],' mm/h',2)) : ''}
        ${r[I_CS]   !=null ? ef('Current', v(r[I_CS],' m/s',2)+'  '+cdir(r[I_CDR])) : ''}
      </div>
    </div>`;
  });
  return html||'<div class="entry-row" style="color:#3a4a5a">No weather data</div>';
}

function buildBio(point) {
  if(!point.bio.length) return '<div class="entry-row" style="color:#3a4a5a">No bio data</div>';
  let html='', lastDay='';
  point.bio.forEach(r=>{
    const day=fmtDay(r[B_TS]);
    if(day!==lastDay){ html+=`<div class="date-hdr">${day}</div>`; lastDay=day; }
    html+=`<div class="entry-row" data-ts="${r[B_TS]}">
      <div class="e-time">${fmtTs(r[B_TS],'time')}</div>
      <div class="e-grid">
        ${ef('Water temp', v(r[B_WT],'°C',1))}
        ${ef('Salinity',   v(r[B_SAL],' PSU',1))}
        ${ef('Chlorophyll',v(r[B_CHL],' mg/m³',3))}
        ${ef('Oxygen',     v(r[B_O2],' mL/L',1))}
        ${ef('Phytoplankton',v(r[B_PHY],'',1))}
        ${ef('Nitrate',    v(r[B_NO3],' μM',2))}
        ${ef('Phosphate',  v(r[B_PO4],' μM',3))}
        ${ef('Silicate',   v(r[B_SIO],' μM',2))}
        ${r[B_ICE]!=null ? ef('Ice cover',v(r[B_ICE],'',3)) : ''}
      </div>
    </div>`;
  });
  return html;
}

function buildTide(point) {
  let html='';

  // sparkline
  if(point.tide.length>1){
    const hs=point.tide.map(r=>r[1]).filter(h=>h!=null);
    const minH=Math.min(...hs), maxH=Math.max(...hs), rng=maxH-minH||1;
    const W=244,H=56,P=3,n=point.tide.length;
    const pts=point.tide.map((r,i)=>{
      const x=P+(i/(n-1))*(W-2*P);
      const y=H-P-((( r[1]||minH)-minH)/rng)*(H-2*P);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    const nowFrac=Math.max(0,Math.min(1,(currentTs()-point.tide[0][0])/(point.tide[n-1][0]-point.tide[0][0])));
    const nowX=(P+nowFrac*(W-2*P)).toFixed(1);
    html+=`<div class="spark-wrap">
      <svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
        <polyline points="${pts}" stroke="#7eb8da" stroke-width="1.5" fill="none" opacity="0.8"/>
        <line x1="${nowX}" y1="0" x2="${nowX}" y2="${H}"
              stroke="#e05040" stroke-width="1.5" stroke-dasharray="3 2" opacity="0.7"/>
      </svg>
      <div class="spark-lbl"><span>${v(minH,'m',2)}</span><span>Tidal height</span><span>${v(maxH,'m',2)}</span></div>
    </div>`;
  }

  if(point.extr.length){
    html+='<div class="date-hdr">High / Low Events</div>';
    point.extr.forEach(r=>{
      const cls=r[2]==='high'?'ext-high':'ext-low';
      const lbl=r[2]==='high'?'HIGH':'LOW';
      html+=`<div class="ext-evt">
        <span class="ext-badge ${cls}">${lbl}</span>
        <span class="ext-time">${fmtTs(r[0])}</span>
        <span class="ext-ht">${v(r[1],'m',2)}</span>
      </div>`;
    });
  }

  if(point.tide.length){
    html+='<div class="date-hdr">Hourly Sea Level</div>';
    let lastDay='';
    point.tide.forEach(r=>{
      const day=fmtDay(r[0]);
      if(day!==lastDay){ html+=`<div class="date-hdr" style="color:#2a3a4a">${day}</div>`; lastDay=day; }
      html+=`<div class="entry-row" data-ts="${r[0]}">
        <div class="e-time">${fmtTs(r[0],'time')}</div>
        <div class="e-grid">${ef('Height',v(r[1],'m',2))}</div>
      </div>`;
    });
  }
  return html||'<div class="entry-row" style="color:#3a4a5a">No tide data</div>';
}

// ════════════════════════════════════════════════════════════════════════
//  Sidebar render + highlight
// ════════════════════════════════════════════════════════════════════════
function renderTab(point, tab) {
  const list=document.getElementById('entry-list');
  list.innerHTML = tab==='bio' ? buildBio(point) :
                   tab==='tide'? buildTide(point) :
                                 buildWx(point);
  list.querySelectorAll('.entry-row[data-ts]').forEach(row=>{
    row.addEventListener('click',()=>{
      slider.value=tsToSlider(parseInt(row.dataset.ts));
      onSlider();
    });
  });
}

function highlightRow() {
  const ts=currentTs();
  const rows=document.querySelectorAll('#entry-list .entry-row[data-ts]');
  if(!rows.length) return;
  let best=null,bd=Infinity;
  rows.forEach(r=>{
    const d=Math.abs(parseInt(r.dataset.ts)-ts);
    if(d<bd){bd=d;best=r;}
  });
  document.querySelectorAll('#entry-list .entry-row.current').forEach(r=>r.classList.remove('current'));
  if(best){ best.classList.add('current'); best.scrollIntoView({behavior:'smooth',block:'nearest'}); }
}

function showPoint(point, clickLat, clickLon) {
  selectedPoint=point;
  const km=distKm(clickLat,clickLon,point.lat,point.lon).toFixed(1);
  document.getElementById('point-name').textContent=
    `${point.n}  ·  ${point.lat.toFixed(2)}°N  ${point.lon>=0?'+':''}${point.lon.toFixed(2)}°E`;
  // Replace point-sub content (this also removes #click-hint from the DOM, so don't access it after)
  document.getElementById('point-sub').innerHTML=
    `<span style="color:#4a5a6a">Clicked  ${clickLat.toFixed(3)}°N  ${clickLon>=0?'+':''}${clickLon.toFixed(3)}°E</span><br>`+
    `<span style="color:#4a5a6a">Nearest data  ${km} km</span>`;
  document.getElementById('tab-bar').style.display='flex';

  renderTab(point, activeTab);
  highlightRow();
}

// ════════════════════════════════════════════════════════════════════════
//  Popup content
// ════════════════════════════════════════════════════════════════════════
function buildPopup(point, ts) {
  const wx  =nearestRow(point.wx,   ts);
  const tide=nearestRow(point.tide, ts);
  let html=`<span class="px-hd">${point.n}  ·  ${point.lat.toFixed(2)}°N  ${point.lon>=0?'+':''}${point.lon.toFixed(2)}°E</span>`;
  html+=`<div style="color:#6b7785;margin-bottom:5px;font-size:9px">${fmtTs(ts)}</div>`;
  html+='<hr class="px-sep">';
  if(wx){
    const wind=v(wx[I_WS],' m/s',1)+(wx[I_WD]!=null?'  '+cdir(wx[I_WD]):'')+(wx[I_WG]!=null?'  g'+v(wx[I_WG],'',1):'');
    const wave=v(wx[I_WH],'m',2)+(wx[I_WDR]!=null?'  '+cdir(wx[I_WDR]):'')+(wx[I_WP]!=null?'  '+v(wx[I_WP],'s',0):'');
    const rows=[
      ['Wind',  wind],
      ['Waves', wave],
      ['Air',   v(wx[I_AT],'°C',1)],
      ['Press', v(wx[I_PR],' hPa',0)],
      ['Tide',  tide?v(tide[1],'m',2):'—'],
    ];
    if(wx[I_VIS]!=null) rows.push(['Vis',v(wx[I_VIS],' km',0)]);
    rows.forEach(([l,val])=>{
      html+=`<div style="display:flex;justify-content:space-between;gap:12px">
        <span class="px-lbl">${l}</span><span class="px-val">${val}</span></div>`;
    });
  } else {
    html+='<span style="color:#3a4a5a">No data at this time</span>';
  }
  // depth row — embedded at build time from GEBCO 2020
  const depthVal = (point.depth != null && point.depth > 0)
    ? point.depth.toFixed(0)+' m'
    : (point.depth != null && point.depth <= 0)
      ? '▲ '+Math.abs(point.depth).toFixed(0)+' m (land)'
      : '—';
  html+='<div style="display:flex;justify-content:space-between;gap:12px;margin-top:4px;border-top:1px solid #1e2a38;padding-top:4px">'
    +'<span class="px-lbl">Depth</span>'
    +'<span class="px-val">'+depthVal+'</span></div>';
  return html;
}

// ════════════════════════════════════════════════════════════════════════
//  Map click
// ════════════════════════════════════════════════════════════════════════
map.on('click', e=>{
  const pt=nearestPoint(e.latlng.lat, e.latlng.lng);
  if(!pt) return;

  // marker + popup stay at the exact clicked position
  if(clickMarker){
    clickMarker.setLatLng(e.latlng);
  } else {
    clickMarker=L.circleMarker(e.latlng,{
      radius:7, color:'#7eb8da', fillColor:'#7eb8da',
      fillOpacity:0.25, weight:2, interactive:true
    }).addTo(map);
    clickMarker.bindPopup('',{maxWidth:240});
  }

  showPoint(pt, e.latlng.lat, e.latlng.lng);
  clickMarker.setPopupContent(buildPopup(pt, currentTs()));
  clickMarker.openPopup();

});

// ════════════════════════════════════════════════════════════════════════
//  Slider
// ════════════════════════════════════════════════════════════════════════
function onSlider(){
  const ts=currentTs();
  timeLabel.textContent=fmtTs(ts);
  if(selectedPoint){
    highlightRow();
    if(clickMarker) clickMarker.setPopupContent(buildPopup(selectedPoint,ts));
    if(activeTab==='tide') renderTab(selectedPoint,'tide'); // redraw sparkline cursor
  }
}
slider.addEventListener('input', onSlider);

// ════════════════════════════════════════════════════════════════════════
//  Tabs
// ════════════════════════════════════════════════════════════════════════
document.querySelectorAll('.tab-btn').forEach(btn=>{
  btn.addEventListener('click',function(){
    document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
    this.classList.add('active');
    activeTab=this.dataset.tab;
    if(selectedPoint){ renderTab(selectedPoint,activeTab); highlightRow(); }
  });
});

// ════════════════════════════════════════════════════════════════════════
//  Init
// ════════════════════════════════════════════════════════════════════════
(()=>{
  const frac=Math.max(0,Math.min(1,(DATA.now-DATA.t_min)/(DATA.t_max-DATA.t_min)));
  slider.value=Math.round(frac*1000);
  timeLabel.textContent=fmtTs(currentTs());
  // NOW tick
  const nm=document.getElementById('now-mark');
  if(frac>0&&frac<1){ nm.style.display='block'; nm.style.left=(frac*100)+'%'; }
})();
</script>
</body>
</html>"""


DEFAULT_WATCH_MIN = 20


def build(db_path: Path, out_path: Path, refresh_secs: int = 0):
    """Build one HTML snapshot. refresh_secs > 0 embeds a meta auto-refresh tag."""
    data     = load_data(db_path)
    aoi_json = AOI_FILE.read_text(encoding="utf-8") if AOI_FILE.exists() else "null"

    auto_refresh = (f'<meta http-equiv="refresh" content="{refresh_secs}">'
                    if refresh_secs > 0 else "")

    html = HTML_TEMPLATE \
        .replace("__DATA_JSON__",    json.dumps(data)) \
        .replace("__AOI_GEOJSON__",  aoi_json) \
        .replace("__AUTO_REFRESH__", auto_refresh)

    out_path.write_text(html, encoding="utf-8")
    size_kb = out_path.stat().st_size >> 10
    print(f"\nSaved → {out_path.resolve()}  ({size_kb} KB)")


def main():
    ap = argparse.ArgumentParser(
        description="Generate NOCTURNAL weather map (weather_sectors aesthetic)"
    )
    ap.add_argument("--db",    type=Path, default=DEFAULT_DB)
    ap.add_argument("--out",   type=Path, default=DEFAULT_OUT)
    ap.add_argument("--watch", type=int,  default=DEFAULT_WATCH_MIN, metavar="MIN",
                    help=f"Rebuild every MIN minutes (default: {DEFAULT_WATCH_MIN})")
    ap.add_argument("--once",  action="store_true",
                    help="Single build only — no watch loop, no browser auto-refresh")
    args = ap.parse_args()

    print("NOCTURNAL Weather Map 2")
    print("─" * 40)
    print(f"  DB  : {args.db}")

    if args.once:
        build(args.db, args.out, refresh_secs=0)
        print("Open weather_map2.html in your browser.")
    else:
        refresh_secs = args.watch * 60
        print(f"  Watch: rebuilding every {args.watch} min, browser auto-refreshes every {args.watch} min")
        print(f"  Open {args.out} in your browser — it will stay up to date automatically.")
        print("  Press Ctrl+C to stop.\n")
        while True:
            try:
                build(args.db, args.out, refresh_secs=refresh_secs)
                print(f"  Next rebuild in {args.watch} min…\n")
                _time.sleep(refresh_secs)
            except KeyboardInterrupt:
                print("\n[stopped]")
                break


if __name__ == "__main__":
    main()
