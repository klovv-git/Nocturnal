import sqlite3

c = sqlite3.connect("ais_memory.db")
rows = c.execute(
    "SELECT pixel_x, pixel_y, lat, lon FROM detections "
    "WHERE scene_name LIKE '%20260507%' LIMIT 10"
).fetchall()

print(f"Found {len(rows)} rows:")
for r in rows:
    print(r)
