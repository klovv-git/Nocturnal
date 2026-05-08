import sqlite3

c = sqlite3.connect("ais_memory.db")

print("=== Dark detections for evening scene, with size info ===\n")
rows = c.execute(
    """SELECT id, width_px, height_px, confidence, lat, lon
       FROM detections
       WHERE dark = 1 AND scene_name LIKE '%3F5D%'
       ORDER BY confidence DESC"""
).fetchall()

print(f"{'id':>6}  {'w_px':>6}  {'h_px':>6}  {'w_m':>6}  {'h_m':>6}  {'conf':>5}  {'lat':>8}  {'lon':>8}  size_ok")
print("-" * 90)
ship_size = []
for det_id, w, h, conf, lat, lon in rows:
    w_m = w * 10   # approx metres at 10m/pixel
    h_m = h * 10
    ok = w < 50 and h < 50
    if ok:
        ship_size.append((det_id, w, h, conf, lat, lon))
    flag = "YES" if ok else "no"
    print(f"{det_id:>6}  {w:>6.1f}  {h:>6.1f}  {w_m:>6.0f}  {h_m:>6.0f}  "
          f"{conf:>5.2f}  {lat:>8.4f}  {lon:>8.4f}  {flag}")

print()
print(f"Detections that could be real ships (< 50px = < 500m): {len(ship_size)}")
for r in ship_size:
    print(f"  id={r[0]}  {r[3]:.2f} conf  ({r[4]:.4f}N, {r[5]:.4f}E)")
