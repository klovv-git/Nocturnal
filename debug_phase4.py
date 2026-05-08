import sqlite3
from global_land_mask import globe

c = sqlite3.connect("ais_memory.db")

# Get all detections for May 7th, ordered by id descending (newest first)
rows = c.execute(
    "SELECT id, pixel_x, pixel_y, lat, lon, dark "
    "FROM detections WHERE scene_name LIKE '%20260507%' "
    "AND lat IS NOT NULL ORDER BY id DESC LIMIT 60"
).fetchall()

print(f"{'id':>6}  {'pixel_x':>8}  {'pixel_y':>8}  {'lat':>8}  {'lon':>8}  {'ocean?':>7}  dark")
print("-" * 70)
ocean_count = 0
for det_id, px, py, lat, lon, dark in rows:
    try:
        ocean = globe.is_ocean(lat, lon)
    except Exception:
        ocean = False
    if ocean:
        ocean_count += 1
    print(f"{det_id:>6}  {px:>8.1f}  {py:>8.1f}  {lat:>8.4f}  {lon:>8.4f}  "
          f"{'YES' if ocean else 'no':>7}  {dark}")

print()
print(f"Ocean detections: {ocean_count} / {len(rows)}")
print()
# What's the lat range?
if rows:
    lats = [r[3] for r in rows if r[3] is not None]
    print(f"Lat range: {min(lats):.4f} to {max(lats):.4f}")
    print(f"Channel sea starts around 49.5N")
