#!/usr/bin/env python3
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
"""
batch_download.py — Download all missing Sentinel-1 GRD scenes for a date range.

Queries the CDSE catalogue for every scene over the AOI, skips those already
present in sentinel_data/, downloads the rest, then prints the run_pipeline
command to process them.

Usage:
    # Download all morning-pass scenes from AIS start to today
    python batch_download.py --after 2026-04-23 --before 2026-06-02

    # Preview without downloading
    python batch_download.py --after 2026-04-23 --before 2026-06-02 --dry-run

    # Include all passes (not just ~06 UTC morning)
    python batch_download.py --after 2026-04-23 --before 2026-06-02 --all-hours
"""

import argparse
import os
import getpass
from pathlib import Path

import requests
from download_scene import get_token, download_product, _attr
from config import SENTINEL_DATA_DIR, CHANNEL_PASS_HOUR_UTC, AOI_WKT

# ── CDSE OData endpoint ────────────────────────────────────────────────────────
CATALOGUE_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"


def _search_window(after: str, before: str, wkt: str, limit: int) -> list:
    """Single CDSE OData query for a narrow date window (no pagination needed)."""
    filter_parts = [
        "Collection/Name eq 'SENTINEL-1'",
        (
            "Attributes/OData.CSC.StringAttribute/any("
            "att:att/Name eq 'productType' and "
            "att/OData.CSC.StringAttribute/Value eq 'GRD')"
        ),
        f"OData.CSC.Intersects(area=geography'SRID=4326;{wkt}')",
        f"ContentDate/Start gt {after}T00:00:00.000Z",
        f"ContentDate/Start lt {before}T00:00:00.000Z",
    ]
    params = {
        "$filter":  " and ".join(filter_parts),
        "$orderby": "ContentDate/Start desc",
        "$expand":  "Attributes",
        "$top":     limit,
    }
    resp = requests.get(CATALOGUE_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def search_all_scenes(after: str, before: str, wkt: str = AOI_WKT,
                      chunk_days: int = 7, limit_per_chunk: int = 200) -> list:
    """
    Retrieve ALL Sentinel-1 GRD scenes in [after, before) by splitting the
    date range into chunk_days-wide windows and querying each separately.

    CDSE OData does not support $skip with complex filters, so we use
    narrow date windows to stay well under the 200-result cap per request.
    """
    from datetime import date, timedelta

    d_start = date.fromisoformat(after)
    d_end   = date.fromisoformat(before)
    chunk   = timedelta(days=chunk_days)

    all_scenes = []
    seen_ids   = set()      # deduplicate across windows
    d = d_start

    while d < d_end:
        d_next = min(d + chunk, d_end)
        page = _search_window(
            d.isoformat(), d_next.isoformat(), wkt, limit_per_chunk
        )
        new = [s for s in page if s["Id"] not in seen_ids]
        for s in new:
            seen_ids.add(s["Id"])
        all_scenes.extend(new)
        print(f"  {d} → {d_next}: {len(page)} found  (total so far: {len(all_scenes)})")
        d = d_next

    return all_scenes


def hour_matches(product: dict, pass_hour: int) -> bool:
    start = product.get("ContentDate", {}).get("Start", "")
    try:
        h = int(start[11:13])
        return abs(h - pass_hour) <= 1
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(
        description="Batch-download all Sentinel-1 GRD scenes over the AOI for a date range"
    )
    ap.add_argument("--after",    default="2026-04-23",
                    help="Start date inclusive (YYYY-MM-DD, default: %(default)s)")
    ap.add_argument("--before",   default=None,
                    help="End date exclusive (YYYY-MM-DD, default: tomorrow)")
    ap.add_argument("--pass-hour", type=int, default=CHANNEL_PASS_HOUR_UTC,
                    help=f"Only download passes within ±1h of this UTC hour "
                         f"(default: {CHANNEL_PASS_HOUR_UTC}  = morning Channel pass)")
    ap.add_argument("--all-hours", action="store_true",
                    help="Download all passes regardless of acquisition hour")
    ap.add_argument("--limit",    type=int, default=200,
                    help="Max search results to request from CDSE (default: 200)")
    ap.add_argument("--username", default=None, help="CDSE username")
    ap.add_argument("--password", default=None, help="CDSE password")
    ap.add_argument("--dry-run",  action="store_true",
                    help="Print what would be downloaded without downloading anything")
    ap.add_argument("--keep-zip", action="store_true",
                    help="Keep zip files after extraction (default: delete to save space)")
    ap.add_argument("--run-pipeline", action="store_true",
                    help="Automatically run run_pipeline.py on newly downloaded scenes when done")
    args = ap.parse_args()

    # ── default before = tomorrow ──────────────────────────────────────────────
    if not args.before:
        from datetime import date, timedelta
        args.before = (date.today() + timedelta(days=1)).isoformat()

    # ── search (paginated, no hard limit) ─────────────────────────────────────
    print(f"Searching CDSE for Sentinel-1 GRD scenes {args.after} → {args.before} ...")
    scenes = search_all_scenes(args.after, args.before)
    print(f"  Found {len(scenes)} scene(s) total in catalogue\n")

    if not scenes:
        print("No scenes found — try a wider date range.")
        return

    # ── filter by pass hour ────────────────────────────────────────────────────
    if not args.all_hours:
        scenes = [s for s in scenes if hour_matches(s, args.pass_hour)]
        print(f"  Filtered to ~{args.pass_hour:02d}:00 UTC (±1h): {len(scenes)} scene(s)")

    # ── find already-downloaded scenes ─────────────────────────────────────────
    SENTINEL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    already = {p.name for p in SENTINEL_DATA_DIR.glob("*.SAFE")}

    to_download = [s for s in scenes if s["Name"] not in already]
    already_have = [s for s in scenes if s["Name"] in already]

    if already_have:
        print(f"\n  Already downloaded ({len(already_have)}):")
        for s in already_have:
            start = s.get("ContentDate", {}).get("Start", "")[:16]
            print(f"    ✓ {s['Name'][:72]}  [{start} UTC]")

    if not to_download:
        print(f"\nAll {len(scenes)} scene(s) already present. Nothing to download.")
        return

    print(f"\n  To download ({len(to_download)}):")
    total_mb = 0
    for s in to_download:
        start   = s.get("ContentDate", {}).get("Start", "")[:16]
        size_mb = (s.get("ContentLength", 0) or 0) >> 20
        total_mb += size_mb
        print(f"    → {s['Name'][:72]}")
        print(f"       {start} UTC  |  {size_mb} MB")

    print(f"\n  Total estimated download: ~{total_mb:,} MB  (~{total_mb/1024:.1f} GB)")

    if args.dry_run:
        print("\n--dry-run: no files downloaded.")
        return

    # ── credentials ────────────────────────────────────────────────────────────
    username = args.username or os.environ.get("CDSE_USER")
    password = args.password or os.environ.get("CDSE_PASS")

    if not (username and password):
        cred_file = Path.home() / ".cdse_credentials"
        if cred_file.exists():
            lines = cred_file.read_text().strip().splitlines()
            if len(lines) >= 2:
                username = username or lines[0].strip()
                password = password or lines[1].strip()

    if not username:
        username = input("\nCDSE username: ").strip()
    if not password:
        password = getpass.getpass("CDSE password: ")

    # ── authenticate ───────────────────────────────────────────────────────────
    print("\nAuthenticating with CDSE ...")
    token = get_token(username, password)
    token_use_count = 0
    print("Authenticated ✓\n")

    # ── download loop ──────────────────────────────────────────────────────────
    downloaded_names = []
    failed_names     = []

    for i, prod in enumerate(to_download):
        name = prod["Name"]
        uuid = prod["Id"]
        size_mb = (prod.get("ContentLength", 0) or 0) >> 20
        start   = prod.get("ContentDate", {}).get("Start", "")[:16]

        print(f"\n{'═'*70}")
        print(f"  [{i+1}/{len(to_download)}] {name[:72]}")
        print(f"  {start} UTC  |  {size_mb} MB")
        print(f"{'═'*70}")

        # Refresh token every 5 downloads (tokens last ~10 min; large files may expire)
        if token_use_count > 0 and token_use_count % 5 == 0:
            print("Refreshing CDSE token ...")
            token = get_token(username, password)

        try:
            safe_dir = download_product(uuid, name, token, SENTINEL_DATA_DIR)
            token_use_count += 1

            # Delete zip to conserve disk space
            if not args.keep_zip:
                out_zip = SENTINEL_DATA_DIR / f"{name}.zip"
                if out_zip.exists():
                    out_zip.unlink()
                    print("  Deleted zip to save space (use --keep-zip to retain)")

            downloaded_names.append(name)

        except Exception as exc:
            print(f"\n  ✗ Download failed: {exc}")
            failed_names.append(name)
            # Refresh token in case it expired
            try:
                token = get_token(username, password)
            except Exception:
                pass
            continue

    # ── summary ────────────────────────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print(f"  Batch complete: {len(downloaded_names)} downloaded, {len(failed_names)} failed")
    if failed_names:
        print(f"\n  Failed scenes:")
        for n in failed_names:
            print(f"    ✗ {n}")
    print(f"{'═'*70}\n")

    if not downloaded_names:
        return

    # ── run pipeline ───────────────────────────────────────────────────────────
    if args.run_pipeline:
        import subprocess
        print("Running pipeline on newly downloaded scenes ...")
        for name in downloaded_names:
            safe_path = SENTINEL_DATA_DIR / name
            if not safe_path.exists():
                print(f"  ✗ Not found (extraction may have failed): {name}")
                continue
            result = subprocess.run(
                [sys.executable, "run_pipeline.py", str(safe_path)]
            )
            if result.returncode != 0:
                print(f"  ✗ Pipeline failed for {name}")
        print("\nPipeline done. Regenerate the timeline map:")
        print("  python ais_timeline_map.py --all")
    else:
        print("Next — process the new scenes through the pipeline:\n")
        scene_paths = " ".join(
            f'"sentinel_data/{n}"'
            for n in downloaded_names
            if (SENTINEL_DATA_DIR / n).exists()
        )
        print(f"  python run_pipeline.py {scene_paths}")
        print(f"\nOr process everything (including existing scenes):")
        print(f"  python run_pipeline.py --all")
        print(f"\nThen regenerate the timeline map:")
        print(f"  python ais_timeline_map.py --all")


if __name__ == "__main__":
    main()
