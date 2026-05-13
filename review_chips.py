#!/usr/bin/env python3
"""
review_chips.py — interactive chip review tool for dark vessel detections.

Starts a local web server and opens a browser page showing all dark detection
chips. Click each chip to mark it as VESSEL or FALSE POSITIVE. Click Save and
the result is written directly to reviews/review_YYYYMMDD.json.

Usage:
    python review_chips.py --scene <SAFE folder name>
    # browser opens automatically — review, click Save, Ctrl+C when done

    python review_chips.py --scene <SAFE folder name> --apply
    # reads reviews/review_YYYYMMDD.json and writes decisions to the database
"""

import argparse
import base64
import json
import re
import sqlite3
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DEFAULT_DB  = Path("ais_memory.db")
REVIEWS_DIR = Path("reviews")
PORT        = 5777


def load_chip(chips_dir, det_id, lat, lon):
    if not chips_dir or not chips_dir.exists():
        return None
    fname = chips_dir / f"dark_{det_id:04d}_{lat:.4f}N_{lon:.4f}E.png"
    if not fname.exists():
        return None
    return base64.b64encode(fname.read_bytes()).decode("ascii")


def build_html(chips_data, existing, scene_time, json_filename):
    chips_json    = json.dumps(chips_data)
    existing_json = json.dumps(existing)
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>NOCTURNAL — Chip Review {scene_time}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #1a1a2e; color: #eee; font-family: Arial, sans-serif; }}
    #header {{
      background: #16213e; padding: 14px 24px;
      display: flex; align-items: center; justify-content: space-between;
      border-bottom: 2px solid #0f3460; gap: 20px;
    }}
    #header h1 {{ font-size: 16px; color: #e2b714; letter-spacing: 1px; }}
    #header .sub {{ font-size: 12px; color: #aaa; margin-top: 3px; }}
    #stats {{ font-size: 13px; color: #aaa; white-space: nowrap; }}
    #stats span {{ color: #fff; font-weight: bold; }}
    #save-btn {{
      background: #e2b714; color: #1a1a2e; border: none;
      padding: 10px 24px; border-radius: 5px; font-size: 14px;
      font-weight: bold; cursor: pointer; letter-spacing: 1px; white-space: nowrap;
    }}
    #save-btn:hover {{ background: #f0c830; }}
    #save-btn:disabled {{ background: #555; color: #999; cursor: default; }}
    #save-status {{ font-size: 12px; color: #2ecc71; display: none; }}
    #grid {{
      display: flex; flex-wrap: wrap; gap: 16px;
      padding: 20px; justify-content: flex-start;
    }}
    .card {{
      width: 180px; background: #16213e;
      border-radius: 8px; overflow: hidden;
      border: 3px solid #333; cursor: pointer;
      transition: border-color 0.15s, transform 0.1s;
      position: relative;
    }}
    .card:hover {{ transform: scale(1.03); }}
    .card.vessel    {{ border-color: #2ecc71; }}
    .card.false-pos {{ border-color: #e74c3c; }}
    .chip-img {{
      width: 100%; height: 180px; object-fit: cover; display: block; background: #000;
    }}
    .no-chip {{
      width: 100%; height: 180px; background: #111;
      display: flex; align-items: center; justify-content: center;
      color: #555; font-size: 12px;
    }}
    .card-info {{ padding: 8px 10px; font-size: 11px; line-height: 18px; }}
    .card-id   {{ font-size: 13px; font-weight: bold; color: #e2b714; }}
    .card-conf {{ color: #aaa; }}
    .card-pos  {{ color: #888; font-size: 10px; }}
    .badge {{
      position: absolute; top: 6px; right: 6px;
      font-size: 10px; font-weight: bold; padding: 3px 7px;
      border-radius: 3px; letter-spacing: 0.5px; display: none;
    }}
    .badge.vessel    {{ background: #2ecc71; color: #000; }}
    .badge.false-pos {{ background: #e74c3c; color: #fff; }}
    #legend {{ display: flex; gap: 20px; align-items: center; font-size: 12px; color: #aaa; }}
    .dot {{ width: 12px; height: 12px; border-radius: 2px; display: inline-block; margin-right: 5px; }}
  </style>
</head>
<body>
  <div id="header">
    <div>
      <h1>NOCTURNAL — CHIP REVIEW</h1>
      <div class="sub">{scene_time} &nbsp;·&nbsp; Click chip to toggle: vessel → false positive → unreviewed</div>
    </div>
    <div id="legend">
      <div><span class="dot" style="background:#2ecc71"></span>Vessel</div>
      <div><span class="dot" style="background:#e74c3c"></span>False positive</div>
      <div><span class="dot" style="background:#555"></span>Unreviewed</div>
    </div>
    <div id="stats">
      Reviewed: <span id="n-reviewed">0</span> / <span id="n-total">0</span>
      &nbsp;&nbsp; Vessels: <span id="n-vessel">0</span>
      &nbsp;&nbsp; False pos: <span id="n-fp">0</span>
    </div>
    <div>
      <button id="save-btn" onclick="saveResults()">💾 Save</button>
      <div id="save-status">✓ Saved to reviews/{json_filename}</div>
    </div>
  </div>
  <div id="grid"></div>

  <script>
    var CHIPS   = {chips_json};
    var results = {existing_json};

    function verdict(id) {{ return results[id] || null; }}

    function toggle(id) {{
      var cur = verdict(id);
      if      (!cur)              results[id] = 'vessel';
      else if (cur === 'vessel')  results[id] = 'false_positive';
      else                        delete results[id];
      renderCard(id);
      updateStats();
    }}

    function renderCard(id) {{
      var card  = document.getElementById('card-'  + id);
      var badge = document.getElementById('badge-' + id);
      var v = verdict(id);
      card.className = 'card ' + (v === 'vessel' ? 'vessel' : v === 'false_positive' ? 'false-pos' : '');
      if (v === 'vessel') {{
        badge.textContent = '✓ VESSEL';
        badge.className = 'badge vessel';
        badge.style.display = 'block';
      }} else if (v === 'false_positive') {{
        badge.textContent = '✗ FALSE POS';
        badge.className = 'badge false-pos';
        badge.style.display = 'block';
      }} else {{
        badge.style.display = 'none';
      }}
    }}

    function updateStats() {{
      var total    = CHIPS.length;
      var reviewed = Object.keys(results).length;
      var vessels  = Object.values(results).filter(function(v) {{ return v === 'vessel'; }}).length;
      var fps      = Object.values(results).filter(function(v) {{ return v === 'false_positive'; }}).length;
      document.getElementById('n-total').textContent    = total;
      document.getElementById('n-reviewed').textContent = reviewed;
      document.getElementById('n-vessel').textContent   = vessels;
      document.getElementById('n-fp').textContent       = fps;
    }}

    function saveResults() {{
      var btn = document.getElementById('save-btn');
      btn.disabled = true;
      btn.textContent = 'Saving...';
      fetch('/save', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(results)
      }}).then(function(r) {{ return r.json(); }})
        .then(function(data) {{
          btn.textContent = '💾 Save';
          btn.disabled = false;
          var status = document.getElementById('save-status');
          status.style.display = 'block';
          setTimeout(function() {{ status.style.display = 'none'; }}, 3000);
        }})
        .catch(function(err) {{
          btn.textContent = '💾 Save';
          btn.disabled = false;
          alert('Save failed: ' + err);
        }});
    }}

    // build grid
    var grid = document.getElementById('grid');
    CHIPS.forEach(function(c) {{
      var card = document.createElement('div');
      card.id = 'card-' + c.id;
      card.className = 'card';
      card.onclick = function() {{ toggle(c.id); }};

      var badge = document.createElement('div');
      badge.id = 'badge-' + c.id;
      badge.className = 'badge';
      card.appendChild(badge);

      if (c.chip) {{
        var img = document.createElement('img');
        img.className = 'chip-img';
        img.src = 'data:image/png;base64,' + c.chip;
        card.appendChild(img);
      }} else {{
        var nd = document.createElement('div');
        nd.className = 'no-chip';
        nd.textContent = 'No chip';
        card.appendChild(nd);
      }}

      var info = document.createElement('div');
      info.className = 'card-info';
      info.innerHTML =
        '<div class="card-id">Det #' + c.id + '</div>' +
        '<div class="card-conf">Conf: ' + c.conf + '</div>' +
        '<div class="card-pos">' + c.lat.toFixed(4) + 'N ' + c.lon.toFixed(4) + 'E</div>' +
        (c.name ? '<div style="color:#3498db;font-size:10px">' + c.name + '</div>' : '');
      card.appendChild(info);
      grid.appendChild(card);
    }});

    // restore existing
    Object.keys(results).forEach(function(id) {{ renderCard(parseInt(id)); }});
    updateStats();
  </script>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene",   required=True)
    ap.add_argument("--db",      default=str(DEFAULT_DB))
    ap.add_argument("--apply",   action="store_true",
                    help="Apply saved review results to the database")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    # ensure false_positive column exists
    cols = [r[1] for r in conn.execute("PRAGMA table_info(detections)").fetchall()]
    if "false_positive" not in cols:
        conn.execute("ALTER TABLE detections ADD COLUMN false_positive INTEGER DEFAULT 0")
        conn.commit()
        print("Added false_positive column to detections table.")

    # derive date string from scene name
    dt_match = re.search(r'_(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})_', args.scene)
    if dt_match:
        y, mo, d, h, mi, s = dt_match.groups()
        scene_time   = f"{y}-{mo}-{d} {h}:{mi}:{s} UTC"
        date_str     = f"{y}{mo}{d}"
    else:
        scene_time   = "unknown"
        date_str     = "unknown"

    REVIEWS_DIR.mkdir(exist_ok=True)
    json_filename = f"review_{date_str}.json"
    json_path     = REVIEWS_DIR / json_filename

    # --- APPLY MODE ---
    if args.apply:
        if not json_path.exists():
            raise SystemExit(f"No review file found at {json_path}. Run without --apply first.")
        data = json.loads(json_path.read_text())
        updated = 0
        for det_id_str, verdict in data.items():
            is_fp = 1 if verdict == "false_positive" else 0
            conn.execute(
                "UPDATE detections SET false_positive = ? WHERE id = ?",
                (is_fp, int(det_id_str))
            )
            updated += 1
        conn.commit()
        fp_count = sum(1 for v in data.values() if v == "false_positive")
        print(f"Applied {updated} decisions: {fp_count} false positive(s) marked.")
        return

    # --- REVIEW MODE ---
    chips_dir = Path(f"dark_chips_{date_str}")

    vessel_names = {r[0]: r[1].strip() for r in conn.execute(
        "SELECT mmsi, name FROM vessels").fetchall() if r[1]}

    dets = conn.execute(
        """SELECT id, lat, lon, confidence, matched_mmsi
           FROM detections
           WHERE scene_name = ? AND dark = 1 AND lat IS NOT NULL
           ORDER BY confidence DESC""",
        (args.scene,)
    ).fetchall()

    if not dets:
        raise SystemExit("No dark detections found for this scene.")

    chips_data = []
    for det_id, lat, lon, conf, mmsi in dets:
        chips_data.append({
            "id":   det_id,
            "lat":  lat,
            "lon":  lon,
            "conf": round(conf, 3),
            "chip": load_chip(chips_dir, det_id, lat, lon),
            "name": vessel_names.get(mmsi) if mmsi else None,
        })

    # load existing review if present
    existing = {}
    if json_path.exists():
        existing = json.loads(json_path.read_text())
        print(f"Loaded existing review ({len(existing)} decisions) from {json_path}")

    html_content = build_html(chips_data, existing, scene_time, json_filename)

    # HTTP server
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # suppress request logs

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html_content.encode("utf-8"))

        def do_POST(self):
            if self.path == "/save":
                length  = int(self.headers.get("Content-Length", 0))
                payload = self.rfile.read(length)
                data    = json.loads(payload)
                json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                fp  = sum(1 for v in data.values() if v == "false_positive")
                ok  = sum(1 for v in data.values() if v == "vessel")
                print(f"  Saved {json_path}  ({ok} vessel, {fp} false positive)")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url    = f"http://127.0.0.1:{PORT}"
    print(f"\nNOCTURNAL — Chip Review")
    print(f"Scene  : {args.scene[:60]}")
    print(f"Chips  : {len(chips_data)} dark detections")
    print(f"Saving : {json_path}")
    print(f"\nOpening {url} ...")
    print(f"Press Ctrl+C when done.\n")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\nDone. Results saved to {json_path}")
        print(f"To apply to database, run:")
        print(f'  python review_chips.py --scene "{args.scene}" --apply')


if __name__ == "__main__":
    main()
