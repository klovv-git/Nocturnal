import sqlite3
from datetime import datetime, timezone

c = sqlite3.connect("ais_memory.db")

t_mid = 1778134016  # 2026-05-07 06:06:56 UTC
t_lo  = t_mid - 1800  # -30 min
t_hi  = t_mid + 1800  # +30 min

print(f"Searching AIS pings between:")
print(f"  {datetime.fromtimestamp(t_lo, timezone.utc)}")
print(f"  {datetime.fromtimestamp(t_hi, timezone.utc)}")
print()

# How many pings in the time window at all?
n = c.execute(
    "SELECT COUNT(*) FROM positions WHERE ts_epoch BETWEEN ? AND ?",
    (t_lo, t_hi)
).fetchone()[0]
print(f"Total AIS pings in ±30 min window: {n}")

# How many in the English Channel in that window?
n2 = c.execute(
    "SELECT COUNT(*) FROM positions "
    "WHERE lat BETWEEN 47 AND 52 AND lon BETWEEN -6 AND 3 "
    "AND ts_epoch BETWEEN ? AND ?",
    (t_lo, t_hi)
).fetchone()[0]
print(f"AIS pings in Channel in that window: {n2}")

# How many rows in the R-tree?
n3 = c.execute("SELECT COUNT(*) FROM positions_rtree").fetchone()[0]
print(f"Rows in positions_rtree: {n3}")

# How many rows in positions?
n4 = c.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
print(f"Rows in positions: {n4}")

# Sample a few pings near one of the detections (48.31, 1.70)
print()
print("Sample AIS pings near (48.31, 1.70) in ±30 min:")
rows = c.execute(
    "SELECT mmsi, lat, lon, ts_epoch FROM positions "
    "WHERE lat BETWEEN 48.2 AND 48.4 AND lon BETWEEN 1.6 AND 1.8 "
    "AND ts_epoch BETWEEN ? AND ? LIMIT 5",
    (t_lo, t_hi)
).fetchall()
for r in rows:
    print(r)
if not rows:
    print("  (none)")
