#!/usr/bin/env python3
"""
weather_store.py — Weather/bio/tide persistence layer for NOCTURNAL.

Stores Storm Glass observations in a local SQLite database (weather.db).
Three data streams are tracked, each with an R-tree spatial index and the
same query pattern as ais_store.AISStore.pings_within():

    weather_within(lat, lon, start_ts, end_ts, radius_km) → List[Row]
    bio_within    (lat, lon, start_ts, end_ts, radius_km) → List[Row]
    tide_within   (lat, lon, start_ts, end_ts, radius_km) → List[Row]
    extremes_within(lat, lon, start_ts, end_ts, radius_km) → List[Row]

Each also has a nearest_*() convenience wrapper that returns the single
closest observation, matching the needs of the dark-vessel model.

Usage (standalone smoke test):
    python3 weather_store.py [path/to/weather.db]
"""

from __future__ import annotations

import math
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


_SCHEMA = """
-- ── Weather (wind, waves, visibility, pressure …) ────────────────────────────
CREATE TABLE IF NOT EXISTS weather_obs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lat           REAL    NOT NULL,
    lon           REAL    NOT NULL,
    ts_utc        TEXT    NOT NULL,
    ts_epoch      REAL    NOT NULL,
    wave_height   REAL,    -- significant wave height, m
    wave_dir      REAL,    -- mean wave direction, deg true
    wave_period   REAL,    -- mean wave period, s
    swell_height  REAL,    -- primary swell height, m
    swell_dir     REAL,    -- primary swell direction, deg
    swell_period  REAL,    -- primary swell period, s
    wind_speed    REAL,    -- 10 m wind speed, m/s
    wind_dir      REAL,    -- 10 m wind direction, deg
    wind_gust     REAL,    -- 1-h max gust, m/s
    visibility    REAL,    -- horizontal visibility, km
    cloud_cover   REAL,    -- total cloud, 0–100 %
    air_temp      REAL,    -- 2 m air temperature, °C
    pressure      REAL,    -- mean sea-level pressure, hPa
    current_speed REAL,    -- surface current speed, m/s
    current_dir   REAL,    -- surface current direction, deg
    precip        REAL,    -- precipitation rate, mm/h
    humidity      REAL,    -- relative humidity, %
    sea_level     REAL,    -- sea level above datum (weather source), m
    src           TEXT    NOT NULL DEFAULT 'stormglass',
    UNIQUE(lat, lon, ts_epoch)
);

CREATE INDEX IF NOT EXISTS idx_weather_ts ON weather_obs(ts_epoch);

CREATE VIRTUAL TABLE IF NOT EXISTS weather_rtree
USING rtree(id, min_lat, max_lat, min_lon, max_lon);


-- ── Bio (chlorophyll, salinity, water temperature …) ─────────────────────────
CREATE TABLE IF NOT EXISTS bio_obs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lat             REAL    NOT NULL,
    lon             REAL    NOT NULL,
    ts_utc          TEXT    NOT NULL,
    ts_epoch        REAL    NOT NULL,
    chlorophyll     REAL,   -- chlorophyll-a concentration, mg/m³
    phytoplankton   REAL,   -- phytoplankton count, cells/L
    salinity        REAL,   -- sea surface salinity, PSU
    water_temp      REAL,   -- sea surface temperature, °C
    ice_cover       REAL,   -- sea ice fraction, 0–1
    oxygen          REAL,   -- dissolved oxygen, mL/L
    nitrate         REAL,   -- nitrate concentration, μmol/L
    phosphate       REAL,   -- phosphate concentration, μmol/L
    silicate        REAL,   -- silicate concentration, μmol/L
    src             TEXT    NOT NULL DEFAULT 'stormglass',
    UNIQUE(lat, lon, ts_epoch)
);

CREATE INDEX IF NOT EXISTS idx_bio_ts ON bio_obs(ts_epoch);

CREATE VIRTUAL TABLE IF NOT EXISTS bio_rtree
USING rtree(id, min_lat, max_lat, min_lon, max_lon);


-- ── Tide sea level (hourly height above chart datum) ─────────────────────────
CREATE TABLE IF NOT EXISTS tide_obs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lat           REAL    NOT NULL,
    lon           REAL    NOT NULL,
    ts_utc        TEXT    NOT NULL,
    ts_epoch      REAL    NOT NULL,
    height        REAL,    -- tidal height above chart datum, m
    src           TEXT    NOT NULL DEFAULT 'stormglass',
    UNIQUE(lat, lon, ts_epoch)
);

CREATE INDEX IF NOT EXISTS idx_tide_ts ON tide_obs(ts_epoch);

CREATE VIRTUAL TABLE IF NOT EXISTS tide_rtree
USING rtree(id, min_lat, max_lat, min_lon, max_lon);


-- ── Tide extremes (high / low water events) ───────────────────────────────────
-- Small dataset (~4 events/day/point); no R-tree needed, lat/lon BETWEEN is fast.
CREATE TABLE IF NOT EXISTS tide_extremes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lat           REAL    NOT NULL,
    lon           REAL    NOT NULL,
    ts_utc        TEXT    NOT NULL,
    ts_epoch      REAL    NOT NULL,
    height        REAL,    -- tidal height at this extreme, m
    type          TEXT    NOT NULL,  -- 'high' or 'low'
    src           TEXT    NOT NULL DEFAULT 'stormglass',
    UNIQUE(lat, lon, ts_epoch, type)
);

CREATE INDEX IF NOT EXISTS idx_tideext_ts  ON tide_extremes(ts_epoch);
CREATE INDEX IF NOT EXISTS idx_tideext_pos ON tide_extremes(lat, lon);


-- ── Fetch log — rate-limiting and deduplication per (point, endpoint, hour) ───
CREATE TABLE IF NOT EXISTS fetch_log (
    lat_grid   REAL    NOT NULL,
    lon_grid   REAL    NOT NULL,
    endpoint   TEXT    NOT NULL,   -- 'weather' | 'bio' | 'tide' | 'extremes'
    ts_hour    INTEGER NOT NULL,   -- UNIX epoch floored to the hour
    fetched_at TEXT    NOT NULL,   -- ISO-8601 UTC of the API call
    PRIMARY KEY (lat_grid, lon_grid, endpoint, ts_hour)
);
"""


class WeatherStore:
    """
    Thread-safe SQLite writer/reader for Storm Glass weather, bio, and tide data.

    Query interface mirrors ais_store.AISStore so model code can call all
    data sources with the same pattern:

        ais.pings_within(lat, lon, t0, t1, radius_km)
        wx.weather_within(lat, lon, t0, t1, radius_km)
        wx.bio_within    (lat, lon, t0, t1, radius_km)
        wx.tide_within   (lat, lon, t0, t1, radius_km)
        wx.extremes_within(lat, lon, t0, t1, radius_km)
    """

    def __init__(self, db_path: str):
        self.db_path = os.fspath(db_path)
        self._lock   = threading.Lock()
        self._conn   = sqlite3.connect(self.db_path,
                                       check_same_thread=False,
                                       timeout=30)
        self._conn.executescript(
            "PRAGMA journal_mode=WAL;"
            "PRAGMA synchronous=NORMAL;"
        )
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ─────────────────── Storm Glass value extractor ───────────────────────────

    @staticmethod
    def _pick(values: Any) -> Optional[float]:
        """
        Extract a numeric value from a Storm Glass multi-source parameter dict.

        Storm Glass returns parameters as {"sg": 1.2, "noaa": 1.4, "icon": 1.1}.
        Preference order: sg (composite) → noaa → icon → dwd → meteo → any.
        """
        if values is None:
            return None
        if isinstance(values, (int, float)):
            return float(values)
        if not isinstance(values, dict):
            return None
        for src in ("sg", "noaa", "icon", "dwd", "meteo", "meto", "fcoo", "fmi",
                    "smhi", "yr", "nws"):
            v = values.get(src)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        for v in values.values():
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    @staticmethod
    def _parse_ts(ts_raw: str) -> Optional[float]:
        """Parse an ISO-8601 timestamp string to a UNIX epoch float."""
        if not ts_raw:
            return None
        ts_utc = ts_raw.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(ts_utc).timestamp()
        except ValueError:
            return None

    # ───────────────────────── weather ingest ──────────────────────────────────

    def record_weather(self,
                       lat: float,
                       lon: float,
                       hours: List[Dict[str, Any]]) -> int:
        """
        Persist hourly observations from a Storm Glass weather/point response.

        `hours` is the API's `hours` list; each element is a dict keyed by
        parameter name whose value is a multi-source dict, e.g.
            {"time": "...", "waveHeight": {"sg": 1.23, "noaa": 1.45}, ...}

        Uses INSERT OR IGNORE — safe to call repeatedly; duplicates are skipped.
        Returns the count of new rows written.
        """
        pick    = self._pick
        written = 0

        with self._lock:
            for h in hours:
                epoch = self._parse_ts(h.get("time", ""))
                if epoch is None:
                    continue
                ts_utc = h["time"].replace("Z", "+00:00")

                cur = self._conn.execute(
                    """INSERT OR IGNORE INTO weather_obs
                       (lat, lon, ts_utc, ts_epoch,
                        wave_height, wave_dir, wave_period,
                        swell_height, swell_dir, swell_period,
                        wind_speed, wind_dir, wind_gust,
                        visibility, cloud_cover, air_temp, pressure,
                        current_speed, current_dir, precip,
                        humidity, sea_level, src)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (lat, lon, ts_utc, epoch,
                     pick(h.get("waveHeight")),
                     pick(h.get("waveDirection")),
                     pick(h.get("wavePeriod")),
                     pick(h.get("swellHeight")),
                     pick(h.get("swellDirection")),
                     pick(h.get("swellPeriod")),
                     pick(h.get("windSpeed")),
                     pick(h.get("windDirection")),
                     pick(h.get("windGust")),
                     pick(h.get("visibility")),
                     pick(h.get("cloudCover")),
                     pick(h.get("airTemperature")),
                     pick(h.get("pressure")),
                     pick(h.get("currentSpeed")),
                     pick(h.get("currentDirection")),
                     pick(h.get("precipitation")),
                     pick(h.get("humidity")),
                     pick(h.get("seaLevel")),
                     "stormglass"))

                if cur.rowcount:
                    self._conn.execute(
                        "INSERT INTO weather_rtree VALUES (?,?,?,?,?)",
                        (cur.lastrowid, lat, lat, lon, lon))
                    written += 1

            self._conn.commit()

        return written

    # ───────────────────────── bio ingest ─────────────────────────────────────

    def record_bio(self,
                   lat: float,
                   lon: float,
                   hours: List[Dict[str, Any]]) -> int:
        """
        Persist hourly observations from a Storm Glass bio/point response.

        Returns the count of new rows written.
        """
        pick    = self._pick
        written = 0

        with self._lock:
            for h in hours:
                epoch = self._parse_ts(h.get("time", ""))
                if epoch is None:
                    continue
                ts_utc = h["time"].replace("Z", "+00:00")

                cur = self._conn.execute(
                    """INSERT OR IGNORE INTO bio_obs
                       (lat, lon, ts_utc, ts_epoch,
                        chlorophyll, phytoplankton, salinity, water_temp,
                        ice_cover, oxygen, nitrate, phosphate, silicate, src)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (lat, lon, ts_utc, epoch,
                     pick(h.get("chlorophyll")),
                     pick(h.get("phytoplankton")),
                     pick(h.get("salinity")),
                     pick(h.get("waterTemperature")),
                     pick(h.get("iceCover")),
                     pick(h.get("oxygen")),
                     pick(h.get("nitrate")),
                     pick(h.get("phosphate")),
                     pick(h.get("silicate")),
                     "stormglass"))

                if cur.rowcount:
                    self._conn.execute(
                        "INSERT INTO bio_rtree VALUES (?,?,?,?,?)",
                        (cur.lastrowid, lat, lat, lon, lon))
                    written += 1

            self._conn.commit()

        return written

    # ───────────────────────── tide sea-level ingest ───────────────────────────

    def record_tide(self,
                    lat: float,
                    lon: float,
                    data: List[Dict[str, Any]]) -> int:
        """
        Persist hourly tide sea-level observations from Storm Glass
        tide/sea-level/point response.

        The API returns a `data` list (not `hours`); each element is:
            {"time": "...", "sg": 1.23}

        Returns the count of new rows written.
        """
        pick    = self._pick
        written = 0

        with self._lock:
            for item in data:
                epoch = self._parse_ts(item.get("time", ""))
                if epoch is None:
                    continue
                ts_utc = item["time"].replace("Z", "+00:00")

                # Tide sea-level response stores the value directly in the
                # source keys at the top level (not nested per parameter).
                height = pick(item)

                cur = self._conn.execute(
                    """INSERT OR IGNORE INTO tide_obs
                       (lat, lon, ts_utc, ts_epoch, height, src)
                       VALUES (?,?,?,?,?,?)""",
                    (lat, lon, ts_utc, epoch, height, "stormglass"))

                if cur.rowcount:
                    self._conn.execute(
                        "INSERT INTO tide_rtree VALUES (?,?,?,?,?)",
                        (cur.lastrowid, lat, lat, lon, lon))
                    written += 1

            self._conn.commit()

        return written

    # ───────────────────────── tide extremes ingest ────────────────────────────

    def record_tide_extremes(self,
                             lat: float,
                             lon: float,
                             data: List[Dict[str, Any]]) -> int:
        """
        Persist high/low tide events from Storm Glass tide/extremes/point.

        The API returns a `data` list; each element is:
            {"time": "...", "height": 1.23, "type": "high"}

        Returns the count of new rows written.
        """
        written = 0

        with self._lock:
            for item in data:
                epoch = self._parse_ts(item.get("time", ""))
                if epoch is None:
                    continue
                ts_utc  = item["time"].replace("Z", "+00:00")
                extreme_type = (item.get("type") or "").lower()
                height  = item.get("height")

                cur = self._conn.execute(
                    """INSERT OR IGNORE INTO tide_extremes
                       (lat, lon, ts_utc, ts_epoch, height, type, src)
                       VALUES (?,?,?,?,?,?,?)""",
                    (lat, lon, ts_utc, epoch,
                     float(height) if height is not None else None,
                     extreme_type, "stormglass"))

                if cur.rowcount:
                    written += 1

            self._conn.commit()

        return written

    # ──────────────────────── core spatial queries ─────────────────────────────
    #
    # All four follow the same signature as ais_store.AISStore.pings_within():
    #   (lat, lon, start_ts, end_ts, radius_km) → List[sqlite3.Row]
    #
    # R-tree pre-filters to a bounding box; Haversine refines to the true circle.

    def _within(self,
                obs_table: str,
                rtree_table: str,
                lat: float,
                lon: float,
                start_ts: float,
                end_ts: float,
                radius_km: float) -> List[sqlite3.Row]:
        """Shared implementation for weather_within / bio_within / tide_within."""
        d_lat = radius_km / 111.0
        d_lon = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))

        self._conn.row_factory = sqlite3.Row
        with self._lock:
            rows = self._conn.execute(f"""
                SELECT t.* FROM {rtree_table} r
                JOIN {obs_table} t ON t.id = r.id
                WHERE r.min_lat >= ? AND r.max_lat <= ?
                  AND r.min_lon >= ? AND r.max_lon <= ?
                  AND t.ts_epoch BETWEEN ? AND ?
            """, (lat - d_lat, lat + d_lat,
                  lon - d_lon, lon + d_lon,
                  start_ts, end_ts)).fetchall()

        return [r for r in rows
                if _haversine_km(lat, lon, r["lat"], r["lon"]) <= radius_km]

    def weather_within(self,
                       lat: float, lon: float,
                       start_ts: float, end_ts: float,
                       radius_km: float = 50.0) -> List[sqlite3.Row]:
        """All weather observations within radius_km and [start_ts, end_ts]."""
        return self._within("weather_obs", "weather_rtree",
                            lat, lon, start_ts, end_ts, radius_km)

    def bio_within(self,
                   lat: float, lon: float,
                   start_ts: float, end_ts: float,
                   radius_km: float = 50.0) -> List[sqlite3.Row]:
        """All bio observations within radius_km and [start_ts, end_ts]."""
        return self._within("bio_obs", "bio_rtree",
                            lat, lon, start_ts, end_ts, radius_km)

    def tide_within(self,
                    lat: float, lon: float,
                    start_ts: float, end_ts: float,
                    radius_km: float = 50.0) -> List[sqlite3.Row]:
        """All tide sea-level observations within radius_km and [start_ts, end_ts]."""
        return self._within("tide_obs", "tide_rtree",
                            lat, lon, start_ts, end_ts, radius_km)

    def extremes_within(self,
                        lat: float, lon: float,
                        start_ts: float, end_ts: float,
                        radius_km: float = 50.0) -> List[sqlite3.Row]:
        """
        High/low tide extreme events within radius_km and [start_ts, end_ts].
        No R-tree (small dataset); uses simple BETWEEN filter + Haversine.
        """
        d_lat = radius_km / 111.0
        d_lon = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))

        self._conn.row_factory = sqlite3.Row
        with self._lock:
            rows = self._conn.execute("""
                SELECT * FROM tide_extremes
                WHERE lat BETWEEN ? AND ?
                  AND lon BETWEEN ? AND ?
                  AND ts_epoch BETWEEN ? AND ?
                ORDER BY ts_epoch
            """, (lat - d_lat, lat + d_lat,
                  lon - d_lon, lon + d_lon,
                  start_ts, end_ts)).fetchall()

        return [r for r in rows
                if _haversine_km(lat, lon, r["lat"], r["lon"]) <= radius_km]

    # ──────────────────── nearest-observation helpers ──────────────────────────
    #
    # Convenience wrappers for the dark-vessel model: given a detection's
    # (lat, lon, ts_epoch), return the single closest observation of each type.
    # Scored as: spatial_km + temporal_hours * 5  (time weighted more).

    def _nearest(self,
                 rows: List[sqlite3.Row],
                 lat: float, lon: float,
                 ts_epoch: float,
                 radius_km: float) -> Optional[sqlite3.Row]:
        best, best_score = None, float("inf")
        for row in rows:
            km    = _haversine_km(lat, lon, row["lat"], row["lon"])
            dt_h  = abs(ts_epoch - row["ts_epoch"]) / 3600.0
            score = km + dt_h * 5
            if km <= radius_km and score < best_score:
                best, best_score = row, score
        return best

    def nearest_weather(self,
                        lat: float, lon: float,
                        ts_epoch: float,
                        radius_km: float = 75.0,
                        dt_hours: float  = 1.5) -> Optional[sqlite3.Row]:
        """Closest weather observation to (lat, lon) at ts_epoch."""
        rows = self.weather_within(lat, lon,
                                   ts_epoch - dt_hours * 3600,
                                   ts_epoch + dt_hours * 3600,
                                   radius_km)
        return self._nearest(rows, lat, lon, ts_epoch, radius_km)

    def nearest_bio(self,
                    lat: float, lon: float,
                    ts_epoch: float,
                    radius_km: float = 75.0,
                    dt_hours: float  = 1.5) -> Optional[sqlite3.Row]:
        """Closest bio observation to (lat, lon) at ts_epoch."""
        rows = self.bio_within(lat, lon,
                               ts_epoch - dt_hours * 3600,
                               ts_epoch + dt_hours * 3600,
                               radius_km)
        return self._nearest(rows, lat, lon, ts_epoch, radius_km)

    def nearest_tide(self,
                     lat: float, lon: float,
                     ts_epoch: float,
                     radius_km: float = 75.0,
                     dt_hours: float  = 1.5) -> Optional[sqlite3.Row]:
        """Closest tide sea-level observation to (lat, lon) at ts_epoch."""
        rows = self.tide_within(lat, lon,
                                ts_epoch - dt_hours * 3600,
                                ts_epoch + dt_hours * 3600,
                                radius_km)
        return self._nearest(rows, lat, lon, ts_epoch, radius_km)

    def next_extreme(self,
                     lat: float, lon: float,
                     ts_epoch: float,
                     radius_km: float = 75.0,
                     window_hours: float = 12.0) -> Optional[sqlite3.Row]:
        """Return the next tide extreme event after ts_epoch."""
        rows = self.extremes_within(lat, lon,
                                    ts_epoch,
                                    ts_epoch + window_hours * 3600,
                                    radius_km)
        return rows[0] if rows else None

    # ──────────────────────── fetch log / dedup ────────────────────────────────

    def mark_fetched(self,
                     lat: float, lon: float,
                     endpoint: str,
                     ts_hour: int) -> None:
        """Record that (lat, lon, endpoint, hour-slot) has been fetched."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO fetch_log
                   (lat_grid, lon_grid, endpoint, ts_hour, fetched_at)
                   VALUES (?,?,?,?,?)""",
                (lat, lon, endpoint, ts_hour, now))
            self._conn.commit()

    def already_fetched(self,
                        lat: float, lon: float,
                        endpoint: str,
                        ts_hour: int) -> bool:
        """True if this (lat, lon, endpoint, hour) slot is in the fetch log."""
        with self._lock:
            row = self._conn.execute(
                """SELECT 1 FROM fetch_log
                   WHERE lat_grid=? AND lon_grid=? AND endpoint=? AND ts_hour=?""",
                (lat, lon, endpoint, ts_hour)).fetchone()
        return row is not None

    def requests_today(self, endpoint: Optional[str] = None) -> int:
        """Count API requests made in the current UTC calendar day."""
        day_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat(timespec="seconds")
        with self._lock:
            if endpoint:
                row = self._conn.execute(
                    """SELECT COUNT(*) FROM fetch_log
                       WHERE endpoint=? AND fetched_at >= ?""",
                    (endpoint, day_start)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM fetch_log WHERE fetched_at >= ?",
                    (day_start,)).fetchone()
        return row[0] if row else 0

    # ──────────────────────────── stats ───────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            def _count(tbl):
                return self._conn.execute(
                    f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]

            return {
                "weather_obs":     _count("weather_obs"),
                "bio_obs":         _count("bio_obs"),
                "tide_obs":        _count("tide_obs"),
                "tide_extremes":   _count("tide_extremes"),
                "fetch_log_slots": _count("fetch_log"),
                "requests_today":  self.requests_today(),
            }


# ──────────────────────────── module helpers ───────────────────────────────────

def _haversine_km(lat1: float, lon1: float,
                  lat2: float, lon2: float) -> float:
    R  = 6371.0088
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a  = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ──────────────────────────── smoke test ──────────────────────────────────────

if __name__ == "__main__":
    import sys, pathlib

    db = sys.argv[1] if len(sys.argv) > 1 else "weather.db"
    with WeatherStore(db) as store:
        print(f"[ok] opened {pathlib.Path(db).resolve()}")
        print(f"[stats] {store.stats()}")
