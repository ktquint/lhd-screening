"""
One-shot repair for staging dirs created before download_raw_tile knew how
to unpack zipped 1/9 arc-second NED tiles.

Walks DEM/raw_3dep/, extracts each *.zip to its inner raster (.img/.tif),
deletes the zip, then rewrites tile_manifest.json (tile_catalog keys +
dam_tiles lists) and tile_manifest.csv so downstream steps see the
extracted filenames.

Usage
-----
    python scripts/repair_zipped_dem_tiles.py --staging-dir /path/to/staging

Idempotent: rerunning after a successful pass is a no-op.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Dict, List

import pandas as pd

_RASTER_EXTS = (".tif", ".tiff", ".img")


def _sanitize(name: str) -> str:
    """Match sanitize_filename in lhd_processor.download_geospatial_data."""
    return name.replace("/", "_").replace("\\", "_")


def _extract_zip(zip_path: Path) -> Path | None:
    """Extract the first raster member of zip_path into its parent dir; delete the zip."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = [
                m for m in zf.namelist()
                if not m.endswith("/")
                and Path(m).name.lower().endswith(_RASTER_EXTS)
            ]
            if not members:
                print(f"  SKIP (no raster inside): {zip_path.name}")
                return None
            member = members[0]
            out_name = _sanitize(Path(member).name)
            out_path = zip_path.parent / out_name
            with zf.open(member) as src, open(out_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        zip_path.unlink()
        return out_path
    except Exception as e:
        print(f"  FAIL extracting {zip_path.name}: {e}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staging-dir", required=True, type=Path,
                    help="Staging root containing DEM/raw_3dep and tile_manifest.json")
    args = ap.parse_args()

    staging_dir: Path = args.staging_dir
    raw_dem_dir = staging_dir / "DEM" / "raw_3dep"
    manifest_path = staging_dir / "tile_manifest.json"

    if not raw_dem_dir.exists():
        sys.exit(f"Not found: {raw_dem_dir}")
    if not manifest_path.exists():
        sys.exit(f"Not found: {manifest_path}")

    zips = sorted(raw_dem_dir.glob("*.zip"))
    if not zips:
        print(f"No .zip files in {raw_dem_dir} — nothing to repair.")
        return 0

    print(f"Found {len(zips)} zipped tile(s) in {raw_dem_dir}\n")

    rename_map: Dict[str, str] = {}
    failed: List[str] = []
    for i, zp in enumerate(zips, 1):
        out = _extract_zip(zp)
        if out is None:
            failed.append(zp.name)
            continue
        rename_map[zp.name] = out.name
        print(f"  [{i}/{len(zips)}] {zp.name}  →  {out.name}")

    if not rename_map:
        print("\nNothing extracted; manifest unchanged.")
        return 1 if failed else 0

    # --- Rewrite tile_manifest.json ---
    with open(manifest_path) as f:
        payload = json.load(f)

    tile_catalog: Dict[str, dict] = payload.get("tile_catalog", {})
    new_catalog: Dict[str, dict] = {}
    for fn, meta in tile_catalog.items():
        new_catalog[rename_map.get(fn, fn)] = meta
    payload["tile_catalog"] = new_catalog

    dam_tiles: Dict[str, List[str]] = payload.get("dam_tiles", {})
    for dam_id, filenames in dam_tiles.items():
        dam_tiles[dam_id] = [rename_map.get(fn, fn) for fn in filenames]
    payload["dam_tiles"] = dam_tiles

    with open(manifest_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nUpdated {manifest_path}")

    # --- Rewrite tile_manifest.csv (regenerate from updated payload) ---
    csv_path = staging_dir / "tile_manifest.csv"
    comid_map = payload.get("dam_comids", {})
    rows = []
    for dam_id, filenames in dam_tiles.items():
        comid = comid_map.get(dam_id)
        for fn in filenames:
            meta = new_catalog.get(fn, {})
            rows.append({
                "dam_id": int(dam_id),
                "nwm_comid": comid,
                "tile_filename": fn,
                "dataset": meta.get("dataset", ""),
                "resolution_m": meta.get("resolution_m", ""),
                "url": meta.get("url", ""),
            })
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    print(f"Updated {csv_path}")

    print(f"\nRepair complete: {len(rename_map)} extracted, {len(failed)} failed.")
    if failed:
        print("Failed zips (left in place):")
        for n in failed:
            print(f"  {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
