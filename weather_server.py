#!/usr/bin/env python3
"""
weather_server.py — Local Storm Glass API mirror for NOCTURNAL.

Reads from weather.db and serves JSON responses that are format-identical
to the real stormglass.io API.  Any code that calls Storm Glass can be
pointed at this server instead by changing the base URL.

Routes
------
  GET /v2/weather/point          →  {"hours": [...], "meta": {...}}
  GET /v2/bio/point              →  {"hours": [...], "meta": {...}}
  GET /v2/tide/sea-level/point   →  {"data":  [...], "meta": {...}}
  GET /v2/tide/extremes/point    →  {"data":  [...], "meta": {...}}

No fallback to the real Storm Glass API — a 404 is returned if the
requested lat/lng and time window have no data in the local database.

Query parameters (same as Storm Glass)
---------------------------------------
  lat     required  decimal degrees
  lng     required  decimal degrees
  start   optional  ISO-8601 or YYYY-MM-DD  (default: now − 24 h)
  end     optional  ISO-8601 or YYYY-MM-DD  (default: now + 24 h)
  params  optional  comma-separated parameter list (weather and bio only)

Usage
-----
  python3 weather_server.py                      # default port 5050
  python3 weather_server.py --port 5050 --db weather.db

  # Call exactly as you would call Storm Glass:
  curl "http://localhost:5050/v2/weather/point?lat=50&lng=0&params=waveHeight,windSpeed&start=2025-01-15T12:00:00Z&end=2025-01-15T18:00:00Z"
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple

from weather_store import WeatherStore, _haversine_km

# ── Configuration ──────────────────────────────────────────────────────────────

DEFAULT_PORT = 5050
DEFAULT_DB   = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "weather.db"
)

# Radius used to search for nearby grid points.  Our grid spacing is ~110 km
# at worst, so 100 km ensures every requested point hits at least one sector.
SEARCH_RADIUS_KM = 100.0

# ── Storm Glass field maps ─────────────────────────────────────────────────────
# Maps the Storm Glass parameter name (used in ?params=) to the column name
# in our local SQLite table.

WEATHER_FIELDS: Dict[str, str] = {
    "waveHeight":       "wave_height",
    "waveDirection":    "wave_dir",
    "wavePeriod":       "wave_period",
    "swellHeight":      "swell_height",
    "swellDirection":   "swell_dir",
    "swellPeriod":      "swell_period",
    "windSpeed":        "wind_speed",
    "windDirection":    "wind_dir",
    "windGust":         "wind_gust",
    "visibility":       "visibility",
    "cloudCover":       "cloud_cover",
    "airTemperature":   "air_temp",
    "pressure":         "pressure",
    "currentSpeed":     "current_speed",
    "currentDirection": "current_dir",
    "precipitation":    "precip",
    "humidity":         "humidity",
    "seaLevel":         "sea_level",
}

BIO_FIELDS: Dict[str, str] = {
    "chlorophyll":      "chlorophyll",
    "phytoplankton":    "phytoplankton",
    "salinity":         "salinity",
    "waterTemperature": "water_temp",
    "iceCover":         "ice_cover",
    "oxygen":           "oxygen",
    "nitrate":          "nitrate",
    "phosphate":        "phosphate",
    "silicate":         "silicate",
}

# ── Shared state (set at startup) ──────────────────────────────────────────────

_store: Optional[WeatherStore] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_ts(s: str) -> Optional[datetime]:
    """Parse ISO-8601 or YYYY-MM-DD into an aware UTC datetime."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M%z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _sg_ts(dt: datetime) -> str:
    """Format a datetime the way Storm Glass does in meta fields."""
    # Storm Glass uses "YYYY-MM-DD HH:MM:SS+00:00" (space, not T).
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")


def _nearest_by_hour(rows: List[sqlite3.Row],
                     req_lat: float,
                     req_lon: float) -> List[sqlite3.Row]:
    """
    For each unique timestamp, keep only the row from the nearest grid point.
    Returns rows sorted by ts_epoch ascending.
    """
    by_ts: Dict[float, Tuple[sqlite3.Row, float]] = {}
    for row in rows:
        km = _haversine_km(req_lat, req_lon, row["lat"], row["lon"])
        ts = row["ts_epoch"]
        if ts not in by_ts or km < by_ts[ts][1]:
            by_ts[ts] = (row, km)

    return [row for row, _ in sorted(by_ts.values(), key=lambda x: x[0]["ts_epoch"])]


def _build_hours(rows: List[sqlite3.Row],
                 field_map: Dict[str, str],
                 requested_params: Optional[List[str]]) -> List[Dict]:
    """
    Convert DB rows to the Storm Glass `hours` array format.

    Each hour entry looks like:
        {"time": "2025-01-15T12:00:00+00:00", "waveHeight": {"sg": 1.23}, ...}

    Only the parameters in requested_params are included; if None, all
    available fields are returned.
    """
    active = requested_params if requested_params else list(field_map.keys())
    hours  = []

    for row in rows:
        entry: Dict[str, Any] = {"time": row["ts_utc"]}
        for sg_name in active:
            col = field_map.get(sg_name)
            if col:
                val = row[col]
                if val is not None:
                    entry[sg_name] = {"sg": val}
        hours.append(entry)

    return hours


def _build_meta(req_lat: float,
                req_lon: float,
                start: datetime,
                end: datetime,
                params: Optional[List[str]]) -> Dict:
    """Return a meta block shaped like Storm Glass."""
    return {
        "cost":         1,
        "dailyQuota":   9999,
        "end":          _sg_ts(end),
        "lat":          req_lat,
        "lng":          req_lon,
        "params":       params or [],
        "requestCount": 0,
        "start":        _sg_ts(start),
        "station": {
            "distance": 0.0,
            "lat":      req_lat,
            "lng":      req_lon,
            "name":     "NOCTURNAL local cache",
            "source":   "sg",
        },
    }


def _error_body(code: int, message: str) -> bytes:
    return json.dumps({"errors": {"key": [message]}, "message": message}).encode()


# ── Request handler ────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path.rstrip("/")
        qs     = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)

        def q(key, default=None):
            return qs.get(key, [default])[0]

        # ── Parse common parameters ────────────────────────────────────────────
        try:
            lat = float(q("lat"))
            lng = float(q("lng"))
        except (TypeError, ValueError):
            self._send(400, _error_body(400, "lat and lng are required"))
            return

        now   = datetime.now(timezone.utc)
        start = _parse_ts(q("start")) or (now - timedelta(hours=24))
        end   = _parse_ts(q("end"))   or (now + timedelta(hours=24))
        start_ep = start.timestamp()
        end_ep   = end.timestamp()

        raw_params = q("params")
        params = [p.strip() for p in raw_params.split(",") if p.strip()] \
                 if raw_params else None

        # ── Route ──────────────────────────────────────────────────────────────
        if path == "/v2/weather/point":
            self._handle_weather(lat, lng, start_ep, end_ep, start, end, params)

        elif path == "/v2/bio/point":
            self._handle_bio(lat, lng, start_ep, end_ep, start, end, params)

        elif path == "/v2/tide/sea-level/point":
            self._handle_tide(lat, lng, start_ep, end_ep, start, end)

        elif path == "/v2/tide/extremes/point":
            self._handle_extremes(lat, lng, start_ep, end_ep, start, end)

        else:
            self._send(404, _error_body(404, f"Unknown endpoint: {path}"))

    # ── Endpoint handlers ──────────────────────────────────────────────────────

    def _handle_weather(self, lat, lng, start_ep, end_ep, start, end, params):
        rows = _store.weather_within(lat, lng, start_ep, end_ep, SEARCH_RADIUS_KM)
        if not rows:
            self._send(404, _error_body(404, "No weather data for this location and time range"))
            return

        rows  = _nearest_by_hour(rows, lat, lng)
        hours = _build_hours(rows, WEATHER_FIELDS, params)
        body  = {"hours": hours, "meta": _build_meta(lat, lng, start, end, params)}
        self._json(body)

    def _handle_bio(self, lat, lng, start_ep, end_ep, start, end, params):
        rows = _store.bio_within(lat, lng, start_ep, end_ep, SEARCH_RADIUS_KM)
        if not rows:
            self._send(404, _error_body(404, "No bio data for this location and time range"))
            return

        rows  = _nearest_by_hour(rows, lat, lng)
        hours = _build_hours(rows, BIO_FIELDS, params)
        body  = {"hours": hours, "meta": _build_meta(lat, lng, start, end, params)}
        self._json(body)

    def _handle_tide(self, lat, lng, start_ep, end_ep, start, end):
        rows = _store.tide_within(lat, lng, start_ep, end_ep, SEARCH_RADIUS_KM)
        if not rows:
            self._send(404, _error_body(404, "No tide data for this location and time range"))
            return

        rows = _nearest_by_hour(rows, lat, lng)
        data = [
            {"time": row["ts_utc"], "sg": row["height"]}
            for row in rows
            if row["height"] is not None
        ]
        body = {"data": data, "meta": _build_meta(lat, lng, start, end, None)}
        self._json(body)

    def _handle_extremes(self, lat, lng, start_ep, end_ep, start, end):
        rows = _store.extremes_within(lat, lng, start_ep, end_ep, SEARCH_RADIUS_KM)
        if not rows:
            self._send(404, _error_body(404, "No tide extremes for this location and time range"))
            return

        data = [
            {"time": row["ts_utc"], "height": row["height"], "type": row["type"]}
            for row in rows
        ]
        body = {"data": data, "meta": _build_meta(lat, lng, start, end, None)}
        self._json(body)

    # ── Response helpers ───────────────────────────────────────────────────────

    def _json(self, obj: Any):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8")

    def _send(self, code: int, body: bytes,
              content_type: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        ts   = datetime.now().strftime("%H:%M:%S")
        code = args[1] if len(args) > 1 else "?"
        print(f"  [{ts}]  {args[0]}  →  {code}")


class _Server(HTTPServer):
    allow_reuse_address = True


# ── Entry point ────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Local Storm Glass API mirror — serves data from weather.db",
    )
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"Port to listen on (default: {DEFAULT_PORT})")
    p.add_argument("--db",
                   default=os.environ.get("NOCTURNAL_WEATHER_DB", DEFAULT_DB),
                   help=f"Path to weather.db (default: {DEFAULT_DB})")
    p.add_argument("--host", default="localhost",
                   help="Bind address (default: localhost)")
    return p.parse_args()


def main() -> None:
    args   = _parse_args()
    global _store
    _store = WeatherStore(args.db)

    stats = _store.stats()

    print("─" * 60)
    print("  NOCTURNAL — Local Storm Glass API")
    print("─" * 60)
    print(f"  Database : {args.db}")
    print(f"  weather  : {stats['weather_obs']:,} observations")
    print(f"  bio      : {stats['bio_obs']:,} observations")
    print(f"  tide     : {stats['tide_obs']:,} observations")
    print(f"  extremes : {stats['tide_extremes']:,} events")
    print(f"  Base URL : http://{args.host}:{args.port}/v2")
    print("─" * 60)
    print("  Routes:")
    print(f"    /v2/weather/point")
    print(f"    /v2/bio/point")
    print(f"    /v2/tide/sea-level/point")
    print(f"    /v2/tide/extremes/point")
    print("─" * 60)
    print()

    server = _Server((args.host, args.port), _Handler)
    print(f"Listening on http://{args.host}:{args.port}  (Ctrl-C to stop)\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[stopped]")
    finally:
        _store.close()


if __name__ == "__main__":
    main()
