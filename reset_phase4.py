"""
Reset Phase 4 results for a scene so geocode_match.py will reprocess it.
Usage: python reset_phase4.py <scene_name_fragment>
Example: python reset_phase4.py 20260507
"""
import sqlite3
import sys

fragment = sys.argv[1] if len(sys.argv) > 1 else "20260507"
c = sqlite3.connect("ais_memory.db")
n = c.execute(
    "UPDATE detections SET lat=NULL, lon=NULL, dark=NULL, "
    "matched_mmsi=NULL, match_dist_m=NULL "
    "WHERE scene_name LIKE ?", (f"%{fragment}%",)
).rowcount
c.commit()
print(f"Reset {n} detections matching '%{fragment}%'")
