#!/usr/bin/env python3
"""
review_chips.py — generate an interactive HTML chip review page.

Shows all dark vessel detection chips for a scene side by side.
Click each chip to mark it as VESSEL or FALSE POSITIVE (rock/land/clutter).
Results are saved to a JSON file and written back to the database.

Usage:
    python review_chips.py --scene <SAFE folder name>
    Then open review_chips.html in a browser and click to review.
    When done, click "Save Results" — writes review_<date>.json
    Then run with --apply to commit the decisions to the database.

    python review_chips.py --scene <SAFE folder name> --apply --results review_20260512.json
"""

import argparse
import base64
import json
import re
import sqlite3
from pathlib import Path

DEFAULT_DB = Path("ais_memory.db")
OUT_FILE   = Path("review_chips.html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene",   required=True)
    ap.add_argument("--db",      default=str(DEFAULT_DB))
    ap.add_argument("--apply",   action="store_true",
                    help="Apply saved review results to the database")
    ap.add_argument("--results", type=str,
                    help="JSON file to apply (used with --apply)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    # ensure false_positive column exists
    cols = [r[1] for r in conn.execute("PRAGMA table_info(detections)").fetchall()]
    if "false_positive" not in cols:
        conn.execute("ALTER TABLE detections ADD COLUMN false_positive INTEGER DEFAULT 0")
        conn.commit()
        print("Added false_positive column to detections table.")

    # --- APPLY MODE ---
    if args.apply:
        if not args.results:
            raise SystemExit("--apply requires --results <json file>")
        data = json.loads(Path(args.results).read_text())
        updated = 0
        for det_id_str, verdict in data.items():
            det_id = int(det_id_str)
            # verdict: "vessel" or "false_positive"
            is_fp = 1 if verdict == "false_positive" else 0
            conn.execute(
                "UPDATE detections SET false_positive = ? WHERE id = ?",
                (is_fp, det_id)
            )
            updated += 1
        conn.commit()
        fp_count = sum(1 for v in data.values() if v == "false_positive")
        print(f"Applied {updated} review decisions ({fp_count} false positives marked).")
        return

    # --- GENERATE HTML ---
    dt_match = re.search(r'_(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})_', args.scene)
    if dt_match:
        y, mo, d, h, mi, s = dt_match.groups()
        scene_time = f"{y}-{mo}-{d} {h}:{mi}:{s} UTC"
        date_str   = f"{y}{mo}{d}"
    else:
        scene_time = "unknown"
        date_str   = None

    chips_dir = Path(f"dark_chips_{date_str}") if date_str else None
    out_file  = Path(f"review_chips_{date_str}.html") if date_str else OUT_FILE
    json_file = f"review_{date_str}.json" if date_str else "review.json"

    def load_chip(det_id, lat, lon):
        if not chips_dir or not chips_dir.exists():
            return None
        fname = chips_dir / f"dark_{det_id:04d}_{lat:.4f}N_{lon:.4f}E.png"
        if not fname.exists():
            return None
        return base64.b64encode(fname.read_bytes()).decode("ascii")

    dets = conn.execute(
        """SELECT id, lat, lon, confidence, dark, matched_mmsi
           FROM detections
           WHERE scene_name = ? AND dark = 1 AND lat IS NOT NULL
           ORDER BY confidence DESC""",
        (args.scene,)
    ).fetchall()

    if not dets:
        raise SystemExit("No dark detections found for this scene.")

    # load existing review if present
    existing = {}
    if Path(json_file).exists():
        existing = json.loads(Path(json_file).read_text())
        print(f"Loaded existing review from {json_file} ({len(existing)} decisions)")

    # missed matches (to show as pre-labelled)
    missed_mmsis = {}
    for row in conn.execute(
        "SELECT id, matched_mmsi FROM detections WHERE scene_name = ? AND dark = 0",
        (args.scene,)
    ).fetchall():
        pass  # not needed here

    missed_ids = set(
        r[0] for r in conn.execute(
            """SELECT d1.id FROM detections d1
               WHERE d1.scene_name = ? AND d1.dark = 1
                 AND EXISTS (
                   SELECT 1 FROM detections d2
                   WHERE d2.scene_name = d1.scene_name AND d2.dark = 0
                     AND d2.matched_mmsi IS NOT NULL
                 )""",
            (args.scene,)
        ).fetchall()
    )
    # simpler: just check which IDs appeared in ais_reappearance missed matches
    # We don't store that — just load all and let user decide

    vessel_names = {}
    for row in conn.execute("SELECT mmsi, name FROM vessels").fetchall():
        if row[1]:
            vessel_names[row[0]] = row[1].strip()

    chips_data = []
    for det_id, lat, lon, conf, dark, mmsi in dets:
        chip_b64 = load_chip(det_id, lat, lon)
        chips_data.append({
            "id":    det_id,
            "lat":   lat,
            "lon":   lon,
            "conf":  round(conf, 3),
            "chip":  chip_b64,
            "mmsi":  mmsi,
            "name":  vessel_names.get(mmsi) if mmsi else None,
        })

    chips_json    = json.dumps(chips_data)
    existing_json = json.dumps(existing)

    html = f"""<!DOCTYPE html>
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
      border-bottom: 2px solid #0f3460;
    }}
    #header h1 {{ font-size: 16px; color: #e2b714; letter-spacing: 1px; }}
    #header .sub {{ font-size: 12px; color: #aaa; margin-top: 3px; }}
    #stats {{ font-size: 13px; color: #aaa; }}
    #stats span {{ color: #fff; font-weight: bold; }}
    #save-btn {{
      background: #e2b714; color: #1a1a2e; border: none;
      padding: 10px 24px; border-radius: 5px; font-size: 14px;
      font-weight: bold; cursor: pointer; letter-spacing: 1px;
    }}
    #save-btn:hover {{ background: #f0c830; }}
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
    .card.vessel      {{ border-color: #2ecc71; }}
    .card.false-pos   {{ border-color: #e74c3c; }}
    .card.unreviewed  {{ border-color: #555; }}
    .chip-img {{
      width: 100%; height: 180px; object-fit: cover;
      display: block; background: #000;
    }}
    .no-chip {{
      width: 100%; height: 180px; background: #111;
      display: flex; align-items: center; justify-content: center;
      color: #555; font-size: 12px;
    }}
    .card-info {{
      padding: 8px 10px; font-size: 11px; line-height: 18px;
    }}
    .card-id   {{ font-size: 13px; font-weight: bold; color: #e2b714; }}
    .card-conf {{ color: #aaa; }}
    .card-pos  {{ color: #888; font-size: 10px; }}
    .badge {{
      position: absolute; top: 6px; right: 6px;
      font-size: 10px; font-weight: bold; padding: 3px 7px;
      border-radius: 3px; letter-spacing: 0.5px;
    }}
    .badge.vessel    {{ background: #2ecc71; color: #000; }}
    .badge.false-pos {{ background: #e74c3c; color: #fff; }}
    #legend {{
      display: flex; gap: 20px; align-items: center;
      font-size: 12px; color: #aaa;
    }}
    .dot {{ width: 12px; height: 12px; border-radius: 2px;
            display: inline-block; margin-right: 5px; }}
  </style>
</head>
<body>
  <div id="header">
    <div>
      <h1>NOCTURNAL — CHIP REVIEW</h1>
      <div class="sub">{scene_time} &nbsp;·&nbsp; Click each chip to toggle: vessel / false positive</div>
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
    <button id="save-btn" onclick="saveResults()">💾 Save Results</button>
  </div>
  <div id="grid"></div>

  <script>
    var CHIPS = {chips_json};
    var results = {existing_json};

    function verdict(id) {{
      return results[id] || null;
    }}

    function toggle(id) {{
      var cur = verdict(id);
      if      (!cur)              results[id] = 'vessel';
      else if (cur === 'vessel')  results[id] = 'false_positive';
      else                        delete results[id];
      renderCard(id);
      updateStats();
    }}

    function renderCard(id) {{
      var card  = document.getElementById('card-' + id);
      var badge = document.getElementById('badge-' + id);
      var v = verdict(id);
      card.className = 'card ' + (v === 'vessel' ? 'vessel' : v === 'false_positive' ? 'false-pos' : 'unreviewed');
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
      var vessels  = Object.values(results).filter(v => v === 'vessel').length;
      var fps      = Object.values(results).filter(v => v === 'false_positive').length;
      document.getElementById('n-total').textContent    = total;
      document.getElementById('n-reviewed').textContent = reviewed;
      document.getElementById('n-vessel').textContent   = vessels;
      document.getElementById('n-fp').textContent       = fps;
    }}

    function saveResults() {{
      var json = JSON.stringify(results, null, 2);
      var blob = new Blob([json], {{type: 'application/json'}});
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = '{json_file}';
      a.click();
      alert('Saved {json_file} — copy it to the Nocturnal folder, then run:\\n\\npython review_chips.py --scene "..." --apply --results {json_file}');
    }}

    // build grid
    var grid = document.getElementById('grid');
    CHIPS.forEach(function(c) {{
      var card = document.createElement('div');
      card.id = 'card-' + c.id;
      card.className = 'card unreviewed';
      card.onclick = function() {{ toggle(c.id); }};

      var badge = document.createElement('div');
      badge.id = 'badge-' + c.id;
      badge.style.display = 'none';
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
        '<div class="card-pos">' + c.lat.toFixed(4) + 'N, ' + c.lon.toFixed(4) + 'E</div>' +
        (c.name ? '<div style="color:#3498db;font-size:10px">' + c.name + '</div>' : '');
      card.appendChild(info);

      grid.appendChild(card);
    }});

    // restore existing reviews
    Object.keys(results).forEach(function(id) {{
      renderCard(parseInt(id));
    }});

    updateStats();
  </script>
</body>
</html>"""

    out_file.write_text(html, encoding="utf-8")
    print(f"Review page saved to {out_file.resolve()}")
    print(f"Open it in your browser — click each chip to mark vessel / false positive.")
    print(f"When done, click 'Save Results' to download {json_file}.")
    print(f"Then run: python review_chips.py --scene \"...\" --apply --results {json_file}")


if __name__ == "__main__":
    main()
