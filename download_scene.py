#!/usr/bin/env python3
"""
download_scene.py — search and download Sentinel-1 GRD scenes from CDSE
(Copernicus Data Space Ecosystem)

Usage:
    # List scenes acquired after a date
    python download_scene.py --after 2026-05-12

    # Auto-select first result and download without prompting
    python download_scene.py --after 2026-05-12 --select 0

    # Specify a different area (WKT polygon, lon/lat order)
    python download_scene.py --after 2026-05-12 --wkt "POLYGON((-2 49, 3 49, 3 51, -2 51, -2 49))"

Credentials (any one of):
    --username / --password   flags
    CDSE_USER / CDSE_PASS     environment variables
    ~/.cdse_credentials       plain text file: two lines, username then password

After downloading, run:
    python extract_sar_overlay.py --scene <scene_name> --safe <path_to_.SAFE>

Dependencies:
    pip install requests tqdm
"""

import argparse
import getpass
import json
import os
import sys
import zipfile
from pathlib import Path

try:
    import requests
except ImportError:
    raise SystemExit("pip install requests")

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

from config import AOI_WKT, SENTINEL_DATA_DIR, SAR_OVERLAYS_DIR

# ── CDSE endpoints ─────────────────────────────────────────────────────────────
CATALOGUE_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
TOKEN_URL     = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
DOWNLOAD_BASE = "https://download.dataspace.copernicus.eu/odata/v1/Products({uuid})/$value"

DEFAULT_WKT = AOI_WKT   # defined in config.py


# ──────────────────────────────────────────────────────────────────────────────
def search_scenes(after: str, before: str = None, wkt: str = DEFAULT_WKT,
                  limit: int = 10) -> list:
    """
    Query the CDSE OData catalogue for Sentinel-1 GRD scenes.

    Parameters
    ----------
    after  : ISO date string, e.g. "2026-05-12"
    before : ISO date string (optional upper bound)
    wkt    : WKT polygon in lon/lat (SRID 4326)
    limit  : max number of results to return

    Returns
    -------
    List of product dicts from the OData response.
    """
    filter_parts = [
        "Collection/Name eq 'SENTINEL-1'",
        (
            "Attributes/OData.CSC.StringAttribute/any("
            "att:att/Name eq 'productType' and "
            "att/OData.CSC.StringAttribute/Value eq 'GRD')"
        ),
        f"OData.CSC.Intersects(area=geography'SRID=4326;{wkt}')",
        f"ContentDate/Start gt {after}T00:00:00.000Z",
    ]
    if before:
        filter_parts.append(f"ContentDate/Start lt {before}T00:00:00.000Z")

    params = {
        "$filter":  " and ".join(filter_parts),
        "$orderby": "ContentDate/Start desc",
        "$top":     limit,
        "$expand":  "Attributes",
    }

    resp = requests.get(CATALOGUE_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def get_token(username: str, password: str) -> str:
    """Obtain a short-lived Bearer token from the CDSE Keycloak endpoint."""
    resp = requests.post(TOKEN_URL, data={
        "client_id":  "cdse-public",
        "grant_type": "password",
        "username":   username,
        "password":   password,
    }, timeout=30)
    if resp.status_code != 200:
        raise SystemExit(
            f"Authentication failed ({resp.status_code}):\n{resp.text[:300]}"
        )
    return resp.json()["access_token"]


def download_product(uuid: str, name: str, token: str, out_dir: Path) -> Path:
    """
    Stream-download a CDSE product zip into out_dir, then extract the .SAFE folder.

    Returns the path of the extracted .SAFE directory (or the zip if extraction fails).
    """
    url     = DOWNLOAD_BASE.format(uuid=uuid)
    out_zip = out_dir / f"{name}.zip"

    if out_zip.exists():
        print(f"Zip already exists: {out_zip}  (skipping download)")
    else:
        print(f"Downloading {name} ...")
        headers = {"Authorization": f"Bearer {token}"}

        with requests.get(url, headers=headers, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))

            with open(out_zip, "wb") as f:
                if HAS_TQDM and total:
                    with tqdm(total=total, unit="B", unit_scale=True,
                              desc=name[:45], leave=True) as bar:
                        for chunk in resp.iter_content(chunk_size=1 << 20):
                            f.write(chunk)
                            bar.update(len(chunk))
                else:
                    downloaded = 0
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
                        downloaded += len(chunk)
                        mb = downloaded >> 20
                        if total:
                            pct = 100 * downloaded / total
                            print(f"\r  {pct:5.1f}%  {mb} / {total >> 20} MB",
                                  end="", flush=True)
                        else:
                            print(f"\r  {mb} MB downloaded", end="", flush=True)
                    print()

        size_mb = out_zip.stat().st_size >> 20
        print(f"Saved: {out_zip}  ({size_mb} MB)")

    # ── extract ───────────────────────────────────────────────────────────────
    print("Extracting ...")
    with zipfile.ZipFile(out_zip) as z:
        z.extractall(out_dir)

    # find the .SAFE folder
    safe_dirs = sorted(out_dir.glob("*.SAFE"))
    if safe_dirs:
        safe_dir = safe_dirs[-1]
        print(f"Extracted: {safe_dir}")
        return safe_dir

    print(f"Warning: could not find .SAFE folder inside {out_zip}")
    return out_zip


def _attr(product: dict, name: str) -> str:
    """Extract an attribute value from the product's Attributes list."""
    for a in product.get("Attributes", []):
        if a.get("Name") == name:
            return str(a.get("Value", "?"))
    return "?"


def extract_orbit_from_name(scene_name: str) -> str:
    """
    Extract the 6-digit absolute orbit number from a Sentinel scene name.
    e.g. 'S1D_IW_GRDH_1SDV_20260512T061448_20260512T061513_002747_...' → '002747'
    """
    parts = scene_name.replace(".SAFE", "").split("_")
    if len(parts) >= 7:
        candidate = parts[6]
        if candidate.isdigit() and len(candidate) == 6:
            return candidate
    return None


def fmt_scene(idx: int, product: dict) -> str:
    """Format one search result for display."""
    name    = product.get("Name", "?")
    start   = product.get("ContentDate", {}).get("Start", "?")
    size_b  = product.get("ContentLength", 0) or 0
    size_mb = size_b >> 20
    pol     = _attr(product, "polarisationChannels")
    sat     = _attr(product, "platformSerialIdentifier")
    orbit   = _attr(product, "relativeOrbitNumber")
    return (
        f"  [{idx}] {name[:72]}\n"
        f"       {start[:16]} UTC  |  sat={sat}  orbit={orbit}  pol={pol}  |  {size_mb} MB"
    )


def load_credentials(args) -> tuple:
    """Resolve username/password from args → env vars → credentials file."""
    username = args.username or os.environ.get("CDSE_USER")
    password = args.password or os.environ.get("CDSE_PASS")

    if not (username and password):
        cred_file = Path.home() / ".cdse_credentials"
        if cred_file.exists():
            lines = cred_file.read_text().strip().splitlines()
            if len(lines) >= 2:
                username = username or lines[0].strip()
                password = password or lines[1].strip()

    return username, password


# ──────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="Search and download Sentinel-1 GRD scenes from CDSE"
    )
    ap.add_argument("--after",    default="2026-05-13",
                    help="Show scenes acquired after this date (YYYY-MM-DD)")
    ap.add_argument("--before",   default=None,
                    help="Show scenes acquired before this date (YYYY-MM-DD)")
    ap.add_argument("--wkt",      default=DEFAULT_WKT,
                    help="AOI as WKT polygon in lon/lat (default: English Channel)")
    ap.add_argument("--limit",    type=int, default=10,
                    help="Max number of search results (default: 10)")
    ap.add_argument("--orbit",    type=int, default=None,
                    help="Only show scenes with this relative orbit number")
    ap.add_argument("--pass-hour", type=int, default=None,
                    help="Only show scenes acquired within ±1h of this UTC hour (e.g. 6 for the ~06:13 UTC English Channel pass)")
    ap.add_argument("--select",   type=int, default=None,
                    help="Auto-select result at this index and download immediately")
    ap.add_argument("--username", default=None, help="CDSE username")
    ap.add_argument("--password", default=None, help="CDSE password")
    ap.add_argument("--out-dir",  type=Path, default=SENTINEL_DATA_DIR,
                    help=f"Directory to save the downloaded scene (default: {SENTINEL_DATA_DIR})")
    ap.add_argument("--no-extract", action="store_true",
                    help="Keep the zip file but skip extraction")
    ap.add_argument("--keep-zip", action="store_true",
                    help="Keep the zip file after extraction (default: delete it to save space)")
    ap.add_argument("--all-slices", action="store_true",
                    help="Download ALL slices from the same pass as the selected scene (same orbit number)")
    args = ap.parse_args()

    # ── search ─────────────────────────────────────────────────────────────────
    print(f"Searching CDSE for Sentinel-1 GRD scenes after {args.after} ...")
    scenes = search_scenes(args.after, args.before, args.wkt, args.limit)

    if not scenes:
        print("No scenes found. Try an earlier --after date.")
        return

    # filter by relative orbit number if requested
    if args.orbit is not None:
        scenes = [s for s in scenes
                  if _attr(s, "relativeOrbitNumber") == str(args.orbit)]
        if not scenes:
            print(f"No scenes found with relative orbit {args.orbit}.")
            return
        print(f"Filtered to orbit {args.orbit}: {len(scenes)} scene(s)\n")

    # filter by acquisition UTC hour if requested
    if args.pass_hour is not None:
        def _hour_matches(product):
            start = product.get("ContentDate", {}).get("Start", "")
            try:
                h = int(start[11:13])  # "2026-05-12T06:14:..." → 6
                return abs(h - args.pass_hour) <= 1
            except Exception:
                return False
        scenes = [s for s in scenes if _hour_matches(s)]
        if not scenes:
            print(f"No scenes found near {args.pass_hour:02d}:00 UTC.")
            return
        print(f"Filtered to ~{args.pass_hour:02d}:00 UTC passes: {len(scenes)} scene(s)\n")

    print(f"\nFound {len(scenes)} scene(s):\n")
    for i, s in enumerate(scenes):
        print(fmt_scene(i, s))
    print()

    # ── select ─────────────────────────────────────────────────────────────────
    if args.select is not None:
        idx = args.select
    else:
        try:
            raw = input("Enter number to download (or Ctrl-C to cancel): ").strip()
            idx = int(raw)
        except (ValueError, KeyboardInterrupt):
            print("\nAborted.")
            return

    if not (0 <= idx < len(scenes)):
        raise SystemExit(f"Invalid selection: {idx}  (must be 0–{len(scenes)-1})")

    product = scenes[idx]
    name    = product["Name"]
    uuid    = product["Id"]

    # --all-slices: expand selection to all scenes with the same orbit
    if args.all_slices:
        selected_orbit = extract_orbit_from_name(name)
        if selected_orbit:
            to_download = [s for s in scenes
                           if extract_orbit_from_name(s["Name"]) == selected_orbit]
            print(f"\nOrbit {selected_orbit} — {len(to_download)} slice(s) to download:")
            for s in to_download:
                print(f"  {s['Name'][:72]}")
        else:
            print("Warning: could not extract orbit number — downloading selected scene only.")
            to_download = [product]
    else:
        to_download = [product]
        print(f"\nSelected: {name}\n")

    # ── credentials ────────────────────────────────────────────────────────────
    username, password = load_credentials(args)
    if not username:
        username = input("CDSE username: ").strip()
    if not password:
        password = getpass.getpass("CDSE password: ")

    # ── authenticate ───────────────────────────────────────────────────────────
    print("Authenticating ...")
    token = get_token(username, password)
    print("Authenticated ✓\n")

    # ── download all selected slices ───────────────────────────────────────────
    args.out_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for prod in to_download:
        pname = prod["Name"]
        puuid = prod["Id"]

        if args.no_extract:
            url     = DOWNLOAD_BASE.format(uuid=puuid)
            out_zip = args.out_dir / f"{pname}.zip"
            print(f"Downloading {pname} ...")
            with requests.get(url, headers={"Authorization": f"Bearer {token}"},
                              stream=True, timeout=120) as resp:
                resp.raise_for_status()
                with open(out_zip, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
            print(f"Saved: {out_zip}")
            results.append((pname, out_zip))
        else:
            safe_dir = download_product(puuid, pname, token, args.out_dir)
            if not args.keep_zip:
                out_zip = args.out_dir / f"{pname}.zip"
                if out_zip.exists():
                    out_zip.unlink()
                    print(f"Deleted zip (use --keep-zip to retain it)")
            results.append((pname, safe_dir))

    # ── next steps ─────────────────────────────────────────────────────────────
    print(f"\n✓ Done! Downloaded {len(results)} slice(s):\n")
    sar_cmds   = []
    yolo_cmds  = []
    scene_args = []

    for pname, path in results:
        print(f"  {path}")
        sar_cmds.append(
            f'  python extract_sar_overlay.py --scene "{pname}" --safe "{path}"'
        )
        yolo_cmds.append(
            f'  python yolo_infer_sar.py "{path}" '
            f'--weights runs\\detect\\runs\\nocturnal\\yolov8n-sar-finetune\\weights\\best.pt --conf 0.2\n'
            f'  python geocode_match.py --scene "{pname}" --safe "{path}"'
        )
        scene_args.append(f'"{pname}"')

    print(f"\nNext steps — run for each slice:")
    print(f"\n1. Extract SAR overlays:")
    print("\n".join(sar_cmds))
    print(f"\n2. Run detection pipeline (YOLO + geocode) for each slice:")
    print("\n".join(yolo_cmds))
    print(f"\n3. View multi-scene timeline:")
    print(f'   python ais_timeline_map.py --scenes {" ".join(scene_args)}')


if __name__ == "__main__":
    main()
