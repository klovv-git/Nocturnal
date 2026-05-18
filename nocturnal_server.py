#!/usr/bin/env python3
"""
nocturnal_server.py — local web server for the NOCTURNAL pipeline.

Serves the map at http://localhost:5050 and injects a "📡 New Pass"
button into the sidebar so you can search, download, and process new
Sentinel-1 scenes directly from your browser — no terminal needed.

Usage:
    python nocturnal_server.py
    # Opens http://localhost:5050 automatically

Requirements:
    pip install flask
"""

import json
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from queue import Empty, Queue

# Force UTF-8 for all child processes on Windows
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

try:
    from flask import Flask, Response, jsonify, request, stream_with_context
except ImportError:
    raise SystemExit("pip install flask")

from config import AOI_WKT, SENTINEL_DATA_DIR
from download_scene import _attr, download_product, get_token, search_scenes

app = Flask(__name__)

# ── Global pipeline state ──────────────────────────────────────────────────────
_running = False
_queue   = Queue()

def _log(msg: str):
    print(msg, flush=True)
    _queue.put(str(msg))


# ── Download panel — injected into ais_timeline_map.html before </body> ────────
PANEL_HTML = r"""
<!-- ═══ NOCTURNAL server panel — injected by nocturnal_server.py ═══ -->
<style>
  #np-btn {
    margin: 10px 12px 4px; padding: 8px 12px;
    background: #0e2a50; color: #7ab3ff;
    border: 1px solid #1e4a8a; border-radius: 5px;
    cursor: pointer; font-size: 12px; font-weight: bold;
    width: calc(100% - 24px); text-align: left;
    transition: background 0.15s;
  }
  #np-btn:hover { background: #1a3a6a; }

  #np-overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,0.7); z-index: 9000;
    align-items: center; justify-content: center;
  }
  #np-overlay.open { display: flex; }

  #np-panel {
    background: #12122a; color: #dde;
    border: 1px solid #2a2a5a; border-radius: 8px;
    width: 540px; max-width: 96vw; max-height: 88vh;
    display: flex; flex-direction: column;
    box-shadow: 0 8px 40px rgba(0,0,0,0.7);
    overflow: hidden;
  }
  #np-panel-header {
    padding: 13px 18px; background: #0e0e22;
    border-bottom: 1px solid #2a2a5a;
    display: flex; justify-content: space-between; align-items: center;
    font-weight: bold; font-size: 14px; color: #7ab3ff; flex-shrink: 0;
  }
  #np-close {
    background: none; border: none; color: #889;
    font-size: 20px; cursor: pointer; padding: 0 4px; line-height: 1;
  }
  #np-close:hover { color: #fff; }
  #np-body { padding: 16px; overflow-y: auto; flex: 1; }

  .np-row { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
  .np-label { font-size: 12px; color: #8899cc; white-space: nowrap; }
  .np-input {
    flex: 1; background: #1a1a3a; border: 1px solid #2a2a5a;
    color: #dde; padding: 6px 10px; border-radius: 4px; font-size: 13px;
  }
  .np-input:focus { outline: none; border-color: #4a7aff; }

  .np-btn {
    padding: 7px 16px; background: #0e2a50; color: #7ab3ff;
    border: 1px solid #1e4a8a; border-radius: 4px;
    cursor: pointer; font-size: 13px; font-weight: bold; white-space: nowrap;
  }
  .np-btn:hover { background: #1a3a6a; }
  .np-btn:disabled { opacity: 0.4; cursor: default; }
  .np-btn-go {
    width: 100%; padding: 9px; margin-top: 10px;
    background: #0e3a1a; color: #5dcc7a;
    border: 1px solid #1a6a2a; border-radius: 5px;
    cursor: pointer; font-size: 13px; font-weight: bold;
  }
  .np-btn-go:hover { background: #1a5a2a; }
  .np-btn-go:disabled { opacity: 0.4; cursor: default; }

  #np-creds {
    margin-bottom: 12px; padding: 10px 12px;
    background: #1a1a3a; border-radius: 5px; border: 1px solid #2a2a5a;
  }
  #np-creds-note { font-size: 11px; margin-bottom: 4px; }
  #np-creds-fields { display: none; }

  .np-scene-card {
    display: flex; align-items: flex-start; gap: 10px;
    padding: 9px 10px; margin-bottom: 6px;
    background: #1a1a3a; border: 1px solid #2a2a5a;
    border-radius: 5px; cursor: pointer;
    transition: background 0.1s;
  }
  .np-scene-card:hover { background: #1e1e4a; }
  .np-scene-card.selected { border-color: #4a7aff; background: #0e1030; }
  .np-scene-card input[type=checkbox] { margin-top: 3px; flex-shrink: 0; accent-color: #4a7aff; }
  .np-scene-name { font-size: 11px; color: #aac; word-break: break-all; }
  .np-scene-meta { font-size: 11px; color: #8899cc; margin-top: 2px; }

  #np-log {
    background: #080818; border: 1px solid #1a1a3a; border-radius: 4px;
    padding: 10px; font-family: monospace; font-size: 11px; color: #9ba;
    height: 240px; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
  }
  #np-reload-btn {
    width: 100%; padding: 10px; margin-top: 10px;
    background: #0e3a1a; color: #5dcc7a;
    border: 1px solid #1a6a2a; border-radius: 5px;
    cursor: pointer; font-size: 14px; font-weight: bold; display: none;
  }
  #np-reload-btn:hover { background: #1a5a2a; }
  .np-spinner { display: inline-block; animation: np-spin 0.8s linear infinite; }
  @keyframes np-spin { to { transform: rotate(360deg); } }
  #np-scene-count { font-size: 11px; color: #8899cc; margin-bottom: 8px; }
</style>

<div id="np-overlay">
  <div id="np-panel">
    <div id="np-panel-header">
      <span>📡 Download New Satellite Pass</span>
      <button id="np-close">✕</button>
    </div>
    <div id="np-body">

      <div id="np-creds">
        <div id="np-creds-note"></div>
        <div id="np-creds-fields">
          <div class="np-row" style="margin-top:8px">
            <span class="np-label">Username</span>
            <input class="np-input" id="np-user" type="text" placeholder="your@email.com">
          </div>
          <div class="np-row">
            <span class="np-label">Password</span>
            <input class="np-input" id="np-pass" type="password" placeholder="CDSE password">
          </div>
        </div>
      </div>

      <div id="np-step-search">
        <div class="np-row">
          <span class="np-label">Pass date</span>
          <input class="np-input" id="np-date" type="date">
          <button class="np-btn" id="np-search-btn" onclick="npSearch()">🔍 Search</button>
        </div>
        <div id="np-scene-count"></div>
        <div id="np-results"></div>
        <button class="np-btn-go" id="np-dl-btn" style="display:none"
                onclick="npDownload()">⬇ Download &amp; Process Selected</button>
      </div>

      <div id="np-step-progress" style="display:none">
        <div id="np-log"></div>
        <button id="np-reload-btn" onclick="window.location.reload()">
          ✓ Map updated — click to reload
        </button>
      </div>

    </div>
  </div>
</div>

<script>
(function () {
  // ── Inject "📡 New Pass" button into calendar sidebar ──────────────────
  var calHeader = document.getElementById('cal-header');
  if (calHeader) {
    var btn = document.createElement('button');
    btn.id = 'np-btn';
    btn.textContent = '📡 New Pass';
    btn.onclick = npOpen;
    calHeader.parentNode.insertBefore(btn, calHeader.nextSibling);
  }

  document.getElementById('np-close').onclick = npClose;
  document.getElementById('np-overlay').onclick = function (e) {
    if (e.target === this) npClose();
  };

  // ── Open modal ──────────────────────────────────────────────────────────
  function npOpen() {
    // default date = today
    document.getElementById('np-date').value = new Date().toISOString().slice(0, 10);

    // check credentials
    fetch('/api/creds').then(r => r.json()).then(function (data) {
      var note = document.getElementById('np-creds-note');
      var fields = document.getElementById('np-creds-fields');
      if (data.ok) {
        note.textContent = '✓ Credentials loaded from ~/.cdse_credentials';
        note.style.color = '#5dcc7a';
        fields.style.display = 'none';
      } else {
        note.textContent = 'No credentials file found — enter them below:';
        note.style.color = '#f39c12';
        fields.style.display = 'block';
      }
    });

    document.getElementById('np-overlay').classList.add('open');
  }

  // ── Close / reset modal ─────────────────────────────────────────────────
  function npClose() {
    document.getElementById('np-overlay').classList.remove('open');
    document.getElementById('np-step-search').style.display = 'block';
    document.getElementById('np-step-progress').style.display = 'none';
    document.getElementById('np-reload-btn').style.display = 'none';
    document.getElementById('np-results').innerHTML = '';
    document.getElementById('np-scene-count').textContent = '';
    document.getElementById('np-dl-btn').style.display = 'none';
  }

  // ── Search CDSE ─────────────────────────────────────────────────────────
  window.npSearch = function () {
    var after = document.getElementById('np-date').value;
    if (!after) { alert('Pick a date first.'); return; }
    var btn = document.getElementById('np-search-btn');
    btn.disabled = true;
    btn.innerHTML = '<span class="np-spinner">⟳</span> Searching…';
    document.getElementById('np-results').innerHTML = '';
    document.getElementById('np-scene-count').textContent = '';
    document.getElementById('np-dl-btn').style.display = 'none';

    // search the selected day only (after = date, before = date + 1)
    var d = new Date(after + 'T12:00:00Z');
    d.setDate(d.getDate() + 1);
    var before = d.toISOString().slice(0, 10);

    fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ after: after, before: before })
    })
      .then(r => r.json())
      .then(function (data) {
        btn.disabled = false;
        btn.textContent = '🔍 Search';
        if (data.error) {
          document.getElementById('np-results').innerHTML =
            '<div style="color:#e74c3c;font-size:12px;padding:6px">' + data.error + '</div>';
          return;
        }
        if (!data.scenes.length) {
          document.getElementById('np-scene-count').textContent = 'No scenes found. Try an earlier date.';
          return;
        }
        document.getElementById('np-scene-count').textContent =
          data.scenes.length + ' scene(s) found — tick the ones to download:';

        var html = '';
        data.scenes.forEach(function (s) {
          html +=
            '<div class="np-scene-card" onclick="npToggle(this)">' +
            '<input type="checkbox" data-name="' + s.name + '" data-id="' + s.id + '">' +
            '<div>' +
            '<div class="np-scene-name">' + s.name.substring(0, 72) + '</div>' +
            '<div class="np-scene-meta">' +
            s.start + ' UTC &nbsp;|&nbsp; ' + s.size_mb + ' MB' +
            (s.orbit !== '?' ? ' &nbsp;|&nbsp; orbit=' + s.orbit : '') +
            ' &nbsp;|&nbsp; ' + s.sat +
            '</div></div></div>';
        });
        document.getElementById('np-results').innerHTML = html;
        document.getElementById('np-dl-btn').style.display = 'block';
      })
      .catch(function (e) {
        btn.disabled = false;
        btn.textContent = '🔍 Search';
        document.getElementById('np-results').innerHTML =
          '<div style="color:#e74c3c;font-size:12px;padding:6px">Search failed: ' + e + '</div>';
      });
  };

  // ── Toggle scene card checkbox ──────────────────────────────────────────
  window.npToggle = function (card) {
    var cb = card.querySelector('input[type=checkbox]');
    cb.checked = !cb.checked;
    card.classList.toggle('selected', cb.checked);
  };

  // ── Download & Process ──────────────────────────────────────────────────
  window.npDownload = function () {
    var checked = document.querySelectorAll('#np-results input[type=checkbox]:checked');
    if (!checked.length) { alert('Select at least one scene.'); return; }

    var scenes = [];
    checked.forEach(function (cb) {
      scenes.push({ name: cb.dataset.name, id: cb.dataset.id });
    });

    var body = {
      scenes:   scenes,
      username: document.getElementById('np-user').value,
      password: document.getElementById('np-pass').value
    };

    // switch to progress view
    document.getElementById('np-step-search').style.display = 'none';
    document.getElementById('np-step-progress').style.display = 'block';
    var log = document.getElementById('np-log');
    log.textContent = 'Starting pipeline…\n';

    fetch('/api/pipeline/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    })
      .then(r => r.json())
      .then(function (data) {
        if (data.error) { log.textContent += 'ERROR: ' + data.error + '\n'; return; }

        // stream progress via SSE
        var es = new EventSource('/api/pipeline/stream');
        es.onmessage = function (e) {
          var d = JSON.parse(e.data);
          if (d.msg === 'ping') return;
          log.textContent += d.msg + '\n';
          log.scrollTop = log.scrollHeight;
          if (d.msg.startsWith('DONE')) {
            es.close();
            document.getElementById('np-reload-btn').style.display = 'block';
          }
          if (d.msg.startsWith('ERROR')) {
            es.close();
          }
        };
        es.onerror = function () {
          es.close();
          log.textContent += '\n[stream closed]\n';
        };
      })
      .catch(function (e) {
        log.textContent += 'ERROR: ' + e + '\n';
      });
  };

})();
</script>
"""


# ── Flask routes ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    map_file = Path("ais_timeline_map.html")
    if map_file.exists():
        html = map_file.read_text(encoding="utf-8")
    else:
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<style>body{background:#12122a;color:#dde;font-family:sans-serif;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
            "#msg{text-align:center}</style></head><body>"
            "<div id='msg'><h2>NOCTURNAL</h2>"
            "<p style='color:#8899cc;margin-top:8px'>"
            "No map yet — click 📡 New Pass to download your first scene.</p></div>"
            "<div id='cal-header'></div>"  # stub so the button injection works
            "</body></html>"
        )
    html = html.replace("</body>", PANEL_HTML + "\n</body>")
    return Response(html, mimetype="text/html")


@app.route("/api/creds")
def api_creds():
    cred_file = Path.home() / ".cdse_credentials"
    if cred_file.exists():
        lines = cred_file.read_text().strip().splitlines()
        if len(lines) >= 2 and lines[0].strip() and lines[1].strip():
            return jsonify({"ok": True})
    return jsonify({"ok": False})


@app.route("/api/search", methods=["POST"])
def api_search():
    data   = request.json or {}
    after  = data.get("after",  "2026-01-01")
    before = data.get("before", None)
    try:
        scenes = search_scenes(after, before=before, wkt=AOI_WKT, limit=15)
        results = [
            {
                "name":    s.get("Name", "?"),
                "id":      s.get("Id", "?"),
                "start":   s.get("ContentDate", {}).get("Start", "?")[:16],
                "size_mb": (s.get("ContentLength", 0) or 0) >> 20,
                "orbit":   _attr(s, "relativeOrbitNumber"),
                "sat":     _attr(s, "platformSerialIdentifier"),
            }
            for s in scenes
        ]
        return jsonify({"scenes": results})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/pipeline/start", methods=["POST"])
def api_pipeline_start():
    global _running
    if _running:
        return jsonify({"error": "Pipeline already running — check the log"}), 409
    data   = request.json or {}
    scenes = data.get("scenes", [])
    if not scenes:
        return jsonify({"error": "No scenes selected"}), 400
    _running = True
    t = threading.Thread(
        target=_pipeline_thread,
        args=(scenes, data.get("username", ""), data.get("password", "")),
        daemon=True,
    )
    t.start()
    return jsonify({"status": "started"})


@app.route("/api/pipeline/stream")
def api_pipeline_stream():
    def generate():
        while True:
            try:
                msg = _queue.get(timeout=25)
                yield f"data: {json.dumps({'msg': msg})}\n\n"
                if msg.startswith("DONE") or msg.startswith("ERROR"):
                    break
            except Empty:
                yield f"data: {json.dumps({'msg': 'ping'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/status")
def api_status():
    return jsonify({"running": _running})


# ── Pipeline background thread ─────────────────────────────────────────────────
def _load_creds(username: str, password: str):
    if username and password:
        return username, password
    cred_file = Path.home() / ".cdse_credentials"
    if cred_file.exists():
        lines = cred_file.read_text().strip().splitlines()
        if len(lines) >= 2:
            return lines[0].strip(), lines[1].strip()
    return None, None


def _stream_proc(proc) -> int:
    for line in proc.stdout:
        _log(line.rstrip())
    proc.wait()
    return proc.returncode


def _pipeline_thread(scenes: list, username: str, password: str):
    global _running
    try:
        # 1. Credentials
        user, pw = _load_creds(username, password)
        if not user or not pw:
            _log("ERROR: No CDSE credentials found.")
            _log("ERROR: Create ~/.cdse_credentials with username on line 1, password on line 2.")
            return

        # 2. Authenticate
        _log("Authenticating with CDSE ...")
        token = get_token(user, pw)
        _log("Authenticated ✓")

        # 3. Download each scene
        SENTINEL_DATA_DIR.mkdir(parents=True, exist_ok=True)
        safe_paths = []

        for scene in scenes:
            name = scene["name"]
            uuid = scene["id"]
            _log(f"\nDownloading: {name[:65]} ...")
            safe_dir = download_product(uuid, name, token, SENTINEL_DATA_DIR)
            # clean up zip
            zip_path = SENTINEL_DATA_DIR / f"{name}.zip"
            if zip_path.exists():
                zip_path.unlink()
                _log("  (zip deleted to save space)")
            safe_paths.append(safe_dir)
            _log(f"✓ Saved: {safe_dir.name}")

        # 4. Run pipeline on each scene
        for safe_path in safe_paths:
            _log(f"\n{'═' * 55}")
            _log(f"  Processing: {safe_path.name[:52]}")
            _log(f"{'═' * 55}")
            proc = subprocess.Popen(
                [sys.executable, "run_pipeline.py", str(safe_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            rc = _stream_proc(proc)
            if rc != 0:
                _log(f"✗ Pipeline failed (exit {rc}) — check output above.")
            else:
                _log("✓ Pipeline complete.")

        # 5. Regenerate the map
        _log(f"\n{'─' * 55}")
        _log("Regenerating map with all scenes ...")
        proc = subprocess.Popen(
            [sys.executable, "ais_timeline_map.py", "--all"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        rc = _stream_proc(proc)
        if rc == 0:
            _log("✓ Map saved.")
            _log("DONE — click the button below to reload the page.")
        else:
            _log(f"ERROR: map generation failed (exit {rc}).")

    except Exception as exc:
        _log(f"ERROR: {exc}")
    finally:
        _running = False


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("╔══════════════════════════════════════════╗")
    print("║  NOCTURNAL — local server                ║")
    print("║  http://localhost:5050                   ║")
    print("║  Ctrl+C to stop                          ║")
    print("╚══════════════════════════════════════════╝")
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:5050")).start()
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
