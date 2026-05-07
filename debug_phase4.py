import sqlite3
import math
from datetime import datetime, timezone

c = sqlite3.connect("ais_memory.db")

t_mid = 1778134016  # 2026-05-07 06:06:56 UTC
t_lo  = t_mid - 1800
t_hi  = t_mid + 1800

# Pull all detections for the May 7th scene
dets = c.execute(
    "SELECT id, lat, lon FROM detections "
    "WHERE scene_name LIKE '%20260507%' AND lat IS NOT NULL LIMIT 20"
).fetchall()

# Pull a sample of AIS pings in the time window
ais = c.execute(
    "SELECT mmsi, lat, lon FROM positions "
    "WHERE ts_epoch BETWEEN ? AND ? LIMIT 5000",
    (t_lo, t_hi)
).fetchall()

print(f"Checking {len(dets)} detections against {len(ais)} AIS pings\n")

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return 2 * R * math.asin(math.sqrt(a))

for det_id, dlat, dlon in dets[:5]:
    best_dist = float('inf')
    best_mmsi = None
    for mmsi, alat, alon in ais:
        try:
            d = haversine_km(dlat, dlon, alat, alon)
            if d < best_dist:
                best_dist = d
                best_mmsi = mmsi
        except Exception:
            pass
    print(f"Detection {det_id} at ({dlat:.4f}, {dlon:.4f}): "
          f"nearest AIS ping = MMSI {best_mmsi} at {best_dist:.2f} km")
