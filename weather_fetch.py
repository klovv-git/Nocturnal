#!/usr/bin/env python3
"""
weather_fetch.py — Storm Glass multi-endpoint collector for NOCTURNAL.

Fetches weather, bio, tide sea-level, and tide extremes from stormglass.io
for an 18-point grid across the English Channel (3 latitude rows × 6 longitude
columns), persisting everything into weather.db via weather_store.py.

Endpoints polled
----------------
  weather   — /v2/weather/point       (wind, waves, visibility, pressure …)
  bio       — /v2/bio/point           (chlorophyll, salinity, water temp …)
  tide      — /v2/tide/sea-level/point (hourly tidal height above datum)
  extremes  — /v2/tide/extremes/point  (high / low water events)

Grid (18 points)
----------------
  Rows:    49.0 °N (S) · 50.0 °N (C) · 51.0 °N (N)
  Columns: −5.5 · −3.5 · −1.5 · 0.0 · 1.25 · 2.25 °E

API key
-------
  export STORMGLASS_API_KEY=your_key_here

Usage
-----
  # One-shot: fetch now + 48 h for all points and all endpoints
  python3 weather_fetch.py

  # Historical backfill (for model training)
  python3 weather_fetch.py --history 2025-04-01 2025-05-01

  # Live daemon: refresh every 30 minutes
  python3 weather_fetch.py --daemon

  # Database stats only
  python3 weather_fetch.py --stats
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import math as _math
from weather_store import WeatherStore
import config as _cfg

# ── Endpoints ──────────────────────────────────────────────────────────────────

_BASE = "https://api.stormglass.io/v2"

ENDPOINTS: Dict[str, str] = {
    "weather":  f"{_BASE}/weather/point",
    "bio":      f"{_BASE}/bio/point",
    "tide":     f"{_BASE}/tide/sea-level/point",
    "extremes": f"{_BASE}/tide/extremes/point",
}

# Parameters to request per endpoint.
WEATHER_PARAMS = ",".join([
    "waveHeight", "waveDirection", "wavePeriod",
    "swellHeight", "swellDirection", "swellPeriod",
    "windSpeed", "windDirection", "gust",
    "visibility", "cloudCover", "airTemperature",
    "pressure", "currentSpeed", "currentDirection",
    "precipitation", "humidity", "seaLevel",
])

BIO_PARAMS = ",".join([
    "chlorophyll", "phytoplankton", "salinity",
    "oxygen", "nitrate", "phosphate", "silicate",
])

# Tide and extremes endpoints don't take a params list.

# ── Grid — pointy-top hexagonal, filtered to the AOI polygon ──────────────────
#
# Generates hex centres inside the AOI polygon (from aoi.geojson) using a
# pointy-top offset-row layout.  Every hex has the same area and all centres
# are confirmed inside the AOI — no wasted API calls over land or outside the
# study region.  HEX_RADIUS_KM controls granularity.

HEX_RADIUS_KM = 55.0   # centre-to-vertex distance; tune to change sector count


def _point_in_poly(lat: float, lon: float,
                   coords: List[Tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon.  coords is a list of [lon, lat] pairs."""
    inside = False
    n = len(coords)
    j = n - 1
    for i in range(n):
        xi, yi = coords[i][0], coords[i][1]
        xj, yj = coords[j][0], coords[j][1]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _build_hex_grid(aoi_coords: List[Tuple[float, float]],
                    radius_km: float = HEX_RADIUS_KM) -> List[Tuple[float, float, str]]:
    """
    Pointy-top hexagonal grid whose centres all fall inside aoi_coords.

    Layout (offset-row):
      row spacing  = radius_km * 1.5 / 111  degrees lat
      col spacing  = radius_km * √3 / (111 * cos(centre_lat))  degrees lon
      odd rows offset east by col_spacing / 2
    """
    lons = [c[0] for c in aoi_coords]
    lats = [c[1] for c in aoi_coords]
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)
    ctr_lat = (lat_min + lat_max) / 2

    row_step = radius_km * 1.5 / 111.0
    col_step = radius_km * _math.sqrt(3) / (111.0 * _math.cos(_math.radians(ctr_lat)))

    grid: List[Tuple[float, float, str]] = []
    row_idx = 0
    lat = lat_min - row_step
    while lat <= lat_max + row_step:
        offset = (col_step / 2) if (row_idx % 2) else 0.0
        lon = lon_min - col_step + offset
        col_idx = 0
        while lon <= lon_max + col_step:
            rlat = round(lat, 4)
            rlon = round(lon, 4)
            if _point_in_poly(rlat, rlon, aoi_coords):
                n = len(grid)
                grid.append((rlat, rlon, f"h{n:02d}"))
            lon += col_step
            col_idx += 1
        lat += row_step
        row_idx += 1

    return grid


GRID: List[Tuple[float, float, str]] = _build_hex_grid(
    _cfg.AOI_COORDS if _cfg.AOI_COORDS else [
        [_cfg.AOI_LON_MIN, _cfg.AOI_LAT_MIN],
        [_cfg.AOI_LON_MAX, _cfg.AOI_LAT_MIN],
        [_cfg.AOI_LON_MAX, _cfg.AOI_LAT_MAX],
        [_cfg.AOI_LON_MIN, _cfg.AOI_LAT_MAX],
        [_cfg.AOI_LON_MIN, _cfg.AOI_LAT_MIN],
    ],
    radius_km=HEX_RADIUS_KM,
)

# ── Timing ─────────────────────────────────────────────────────────────────────

POLL_INTERVAL   = 1800   # 30 minutes between daemon cycles
HOURS_AHEAD     = 48     # forecast window per fetch
MAX_CHUNK_DAYS  = 10     # Storm Glass cap on a single request window
INTER_REQ_SLEEP = 0.5    # seconds between individual API calls (be polite)

# ── Default DB path ────────────────────────────────────────────────────────────

DEFAULT_DB       = str(_cfg.WEATHER_DB_PATH)
_DEFAULT_API_KEY = os.environ.get("STORMGLASS_API_KEY", "") or _cfg.STORMGLASS_API_KEY

# ── Exceptions ─────────────────────────────────────────────────────────────────

class QuotaExhausted(RuntimeError):
    pass


# ── Low-level API calls ────────────────────────────────────────────────────────

def _call(api_key: str,
          endpoint: str,
          lat: float,
          lon: float,
          start: datetime,
          end: datetime,
          params: Optional[str] = None) -> Dict[str, Any]:
    """
    Make one Storm Glass API request and return the parsed JSON.

    Raises QuotaExhausted on HTTP 402 or when the API reports the daily
    quota is reached.  Raises RuntimeError for all other HTTP errors.
    """
    qs: Dict[str, Any] = {
        "lat":   lat,
        "lng":   lon,
        "start": start.isoformat(),
        "end":   end.isoformat(),
    }
    if params:
        qs["params"] = params

    url = f"{endpoint}?{urllib.parse.urlencode(qs)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": api_key,
            "User-Agent":    "NOCTURNALWeatherFetch/1.1",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        if e.code == 402:
            raise QuotaExhausted(f"HTTP 402 — daily quota exhausted")
        raise RuntimeError(f"HTTP {e.code} from {endpoint}: "
                           f"{body[:200].decode(errors='replace')}") from e

    data = json.loads(body.decode("utf-8"))

    meta  = data.get("meta", {})
    quota = meta.get("dailyQuota",   0)
    used  = meta.get("requestCount", 0)
    if quota and used >= quota:
        raise QuotaExhausted(
            f"Daily quota reached: {used}/{quota} requests used")

    return data


def fetch_weather(api_key, lat, lon, start, end):
    return _call(api_key, ENDPOINTS["weather"], lat, lon, start, end,
                 params=WEATHER_PARAMS)


def fetch_bio(api_key, lat, lon, start, end):
    return _call(api_key, ENDPOINTS["bio"], lat, lon, start, end,
                 params=BIO_PARAMS)


def fetch_tide(api_key, lat, lon, start, end):
    return _call(api_key, ENDPOINTS["tide"], lat, lon, start, end)


def fetch_extremes(api_key, lat, lon, start, end):
    return _call(api_key, ENDPOINTS["extremes"], lat, lon, start, end)


# ── Fetch helpers ──────────────────────────────────────────────────────────────

def _floor_hour(dt: datetime) -> int:
    """UNIX epoch of dt floored to the hour boundary."""
    return int(dt.replace(minute=0, second=0, microsecond=0).timestamp())


def _floor_day(dt: datetime) -> int:
    """UNIX epoch of dt floored to midnight UTC."""
    return int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _quota_line(meta: Dict) -> str:
    return f"quota {meta.get('requestCount','?')}/{meta.get('dailyQuota','?')}"


# ── Per-endpoint fetch + store ─────────────────────────────────────────────────

def _fetch_and_store_weather(api_key: str,
                              store: WeatherStore,
                              lat: float, lon: float, name: str,
                              start: datetime, end: datetime,
                              force: bool) -> bool:
    slot = _floor_hour(start)
    if not force and store.already_fetched(lat, lon, "weather", slot):
        return False

    data  = fetch_weather(api_key, lat, lon, start, end)
    hours = data.get("hours", [])
    n     = store.record_weather(lat, lon, hours)
    store.mark_fetched(lat, lon, "weather", slot)
    print(f"    weather  {n:4d} new rows  ({_quota_line(data.get('meta', {}))})")
    return True


def _fetch_and_store_bio(api_key: str,
                          store: WeatherStore,
                          lat: float, lon: float, name: str,
                          start: datetime, end: datetime,
                          force: bool) -> bool:
    slot = _floor_hour(start)
    if not force and store.already_fetched(lat, lon, "bio", slot):
        return False

    data  = fetch_bio(api_key, lat, lon, start, end)
    hours = data.get("hours", [])
    n     = store.record_bio(lat, lon, hours)
    store.mark_fetched(lat, lon, "bio", slot)
    print(f"    bio      {n:4d} new rows  ({_quota_line(data.get('meta', {}))})")
    return True


def _fetch_and_store_tide(api_key: str,
                           store: WeatherStore,
                           lat: float, lon: float, name: str,
                           start: datetime, end: datetime,
                           force: bool) -> bool:
    slot = _floor_hour(start)
    if not force and store.already_fetched(lat, lon, "tide", slot):
        return False

    data = fetch_tide(api_key, lat, lon, start, end)
    # sea-level endpoint returns {"data": [...]} not {"hours": [...]}
    rows = data.get("data", [])
    n    = store.record_tide(lat, lon, rows)
    store.mark_fetched(lat, lon, "tide", slot)
    print(f"    tide     {n:4d} new rows  ({_quota_line(data.get('meta', {}))})")
    return True


def _fetch_and_store_extremes(api_key: str,
                               store: WeatherStore,
                               lat: float, lon: float, name: str,
                               start: datetime, end: datetime,
                               force: bool) -> bool:
    # Extremes only need one fetch per day — dedup on the day boundary.
    slot = _floor_day(start)
    if not force and store.already_fetched(lat, lon, "extremes", slot):
        return False

    data = fetch_extremes(api_key, lat, lon, start, end)
    rows = data.get("data", [])
    n    = store.record_tide_extremes(lat, lon, rows)
    store.mark_fetched(lat, lon, "extremes", slot)
    print(f"    extremes {n:4d} new events ({_quota_line(data.get('meta', {}))})")
    return True


# ── Grid fetch ─────────────────────────────────────────────────────────────────

_ENDPOINT_FUNCS = {
    "weather":  _fetch_and_store_weather,
    "bio":      _fetch_and_store_bio,
    "tide":     _fetch_and_store_tide,
    "extremes": _fetch_and_store_extremes,
}


def fetch_grid(api_key: str,
               store: WeatherStore,
               start: datetime,
               end: datetime,
               endpoints: Optional[List[str]] = None,
               force: bool = False) -> None:
    """
    Fetch all active endpoints for every GRID point over [start, end].

    Already-cached (point, endpoint, hour) slots are skipped unless
    force=True.  The run aborts cleanly on QuotaExhausted.
    """
    active = endpoints or list(_ENDPOINT_FUNCS.keys())

    print(f"[{_ts()}] Grid fetch: "
          f"{start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')} UTC  "
          f"| {len(GRID)} points × {len(active)} endpoints")

    for lat, lon, name in GRID:
        any_called = False
        print(f"  {name} ({lat:.2f}, {lon:.2f})")

        for ep in active:
            fn = _ENDPOINT_FUNCS[ep]
            try:
                called = fn(api_key, store, lat, lon, name, start, end, force)
                if called:
                    any_called = True
                    time.sleep(INTER_REQ_SLEEP)
            except QuotaExhausted as e:
                print(f"\n  [QUOTA] {e} — aborting run.")
                print(f"[{_ts()}] DB stats: {store.stats()}")
                return
            except Exception as e:
                print(f"    [{ep}] ERROR — {type(e).__name__}: {e}")

        if not any_called:
            print("    all slots already cached")

    print(f"[{_ts()}] Done.  DB stats: {store.stats()}")


# ── Run modes ──────────────────────────────────────────────────────────────────

def run_now(api_key: str, store: WeatherStore,
            hours_ahead: int = HOURS_AHEAD,
            endpoints: Optional[List[str]] = None,
            force: bool = False) -> None:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = now + timedelta(hours=hours_ahead)
    fetch_grid(api_key, store, now, end, endpoints=endpoints, force=force)


def run_history(api_key: str, store: WeatherStore,
                start_str: str, end_str: str,
                endpoints: Optional[List[str]] = None,
                force: bool = False) -> None:
    """Backfill a historical range, splitting into ≤10-day chunks."""

    def _parse(s: str) -> datetime:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        raise SystemExit(f"Cannot parse date: {s!r}")

    start = _parse(start_str).replace(minute=0, second=0, microsecond=0)
    end   = _parse(end_str).replace(minute=0, second=0, microsecond=0)

    if end <= start:
        raise SystemExit("--history END must be after START")

    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=MAX_CHUNK_DAYS), end)
        print(f"\n── Chunk: {chunk_start.strftime('%Y-%m-%d')} → "
              f"{chunk_end.strftime('%Y-%m-%d')} ──")
        fetch_grid(api_key, store, chunk_start, chunk_end,
                   endpoints=endpoints, force=force)
        chunk_start = chunk_end

    print("\n[history] Complete.")


def run_daemon(api_key: str, store: WeatherStore,
               interval: int = POLL_INTERVAL,
               hours_ahead: int = HOURS_AHEAD,
               endpoints: Optional[List[str]] = None) -> None:
    """Refresh every `interval` seconds (default: 30 min)."""
    print(f"[daemon] Starting — refresh every {interval}s  (Ctrl-C to stop)")
    while True:
        run_now(api_key, store, hours_ahead=hours_ahead, endpoints=endpoints)
        next_at = datetime.now(timezone.utc) + timedelta(seconds=interval)
        print(f"[daemon] Next cycle at "
              f"{next_at.strftime('%Y-%m-%d %H:%M')} UTC\n")
        time.sleep(interval)


# ── CLI ────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fetch Storm Glass weather/bio/tide data into NOCTURNAL weather.db",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--api-key",
                   default=_DEFAULT_API_KEY,
                   help="Storm Glass API key (config.py, env var, or this flag)")
    p.add_argument("--db",
                   default=os.environ.get("NOCTURNAL_WEATHER_DB", DEFAULT_DB),
                   help=f"Path to weather.db (default: {DEFAULT_DB})")

    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--now", action="store_true",
                      help="Fetch now + HOURS_AHEAD for all grid points (default)")
    mode.add_argument("--history", nargs=2, metavar=("START", "END"),
                      help="Backfill a date range (ISO-8601 or YYYY-MM-DD)")
    mode.add_argument("--daemon", action="store_true",
                      help="Run continuously, refreshing every --interval seconds")
    mode.add_argument("--stats", action="store_true",
                      help="Print database stats and exit")

    p.add_argument("--interval", type=int, default=POLL_INTERVAL,
                   help=f"Daemon refresh interval in seconds (default: {POLL_INTERVAL})")
    p.add_argument("--hours-ahead", type=int, default=HOURS_AHEAD,
                   help=f"Forecast window in hours (default: {HOURS_AHEAD})")
    p.add_argument("--endpoints", nargs="+",
                   choices=list(_ENDPOINT_FUNCS.keys()),
                   help="Limit to specific endpoints (default: all four)")
    p.add_argument("--force", action="store_true",
                   help="Re-fetch even if the slot is already cached")

    return p.parse_args()


def main() -> None:
    args = _parse_args()

    if not args.stats and not args.api_key:
        print("ERROR: Storm Glass API key required.\n"
              "  export STORMGLASS_API_KEY=your_key_here\n"
              "  or pass --api-key KEY",
              file=sys.stderr)
        sys.exit(1)

    store = WeatherStore(args.db)

    try:
        if args.stats:
            import pprint
            pprint.pprint(store.stats())
            return

        print("─" * 60)
        print("  NOCTURNAL Weather Fetch — Storm Glass")
        print("─" * 60)
        print(f"  DB       : {args.db}")
        print(f"  Grid     : {len(GRID)} points  "
              f"({len(_LAT_ROWS)} rows × {len(_LON_COLS)} columns)")
        print(f"  Interval : {args.interval}s  ({args.interval//60} min)")
        eps = args.endpoints or list(_ENDPOINT_FUNCS.keys())
        print(f"  Endpoints: {', '.join(eps)}")
        print("─" * 60)
        for lat, lon, name in GRID:
            print(f"  {name:22s}  {lat:.2f}°N  {lon:+.2f}°E")
        print("─" * 60)

        if args.history:
            run_history(args.api_key, store,
                        args.history[0], args.history[1],
                        endpoints=args.endpoints, force=args.force)
        elif args.daemon:
            run_daemon(args.api_key, store,
                       interval=args.interval,
                       hours_ahead=args.hours_ahead,
                       endpoints=args.endpoints)
        else:
            run_now(args.api_key, store,
                    hours_ahead=args.hours_ahead,
                    endpoints=args.endpoints,
                    force=args.force)

    except KeyboardInterrupt:
        print("\n[stopped]")
    finally:
        store.close()


if __name__ == "__main__":
    main()
