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

from config import AOI_WKT

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


def fmt_scene(idx: int, product: dict) -> str:
    """Format one search result for display."""
    name    = product.get("Name", "?")
    start   = product.get("ContentDate", {}).get("Start", "?")
    size_b  = product.get("ContentLength", 0) or 0
    size_mb = size_b >> 20
    pol     = _attr(product, "polarisationChannels")
    sat     = _attr(product, "platformSerialIdentifier")  # e.g. S1A, S1D
    return (
        f"  [{idx}] {name[:72]}\n"
        f"       {start[:16]} UTC  |  sat={sat}  pol={pol}  |  {size_mb} MB"
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
    ap.add_argument("--select",   type=int, default=None,
                    help="Auto-select result at this index and download immediately")
    ap.add_argument("--username", default=None, help="CDSE username")
    ap.add_argument("--password", default=None, help="CDSE password")
    ap.add_argument("--out-dir",  type=Path, default=Path("."),
                    help="Directory to save the downloaded scene (default: .)")
    ap.add_argument("--no-extract", action="store_true",
                    help="Keep the zip file but skip extraction")
    args = ap.parse_args()

    # ── search ─────────────────────────────────────────────────────────────────
    print(f"Searching CDSE for Sentinel-1 GRD scenes after {args.after} ...")
    scenes = search_scenes(args.after, args.before, args.wkt, args.limit)

    if not scenes:
        print("No scenes found. Try an earlier --after date.")
        return

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

    # ── download ───────────────────────────────────────────────────────────────
    args.out_dir.mkdir(parents=True, exist_ok=True)

    if args.no_extract:
        url     = DOWNLOAD_BASE.format(uuid=uuid)
        out_zip = args.out_dir / f"{name}.zip"
        print(f"Downloading {name} ...")
        with requests.get(url, headers={"Authorization": f"Bearer {token}"},
                          stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(out_zip, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        print(f"Saved: {out_zip}")
        result = out_zip
    else:
        result = download_product(uuid, name, token, args.out_dir)

    # ── next steps ─────────────────────────────────────────────────────────────
    print(f"\n✓ Done! Scene is at: {result}")
    print(f"\nNext steps:")
    print(f'  1. Extract SAR overlay:')
    print(f'     python extract_sar_overlay.py --scene "{name}" --safe "{result}"')
    print(f'  2. Run YOLO detection + geocoding as normal, then:')
    print(f'  3. View multi-scene timeline:')
    print(f'     python ais_timeline_map.py \\')
    print(f'       --scenes "<old_scene>" "{name}" \\')
    print(f'       --sar-dir .')


if __name__ == "__main__":
    main()
