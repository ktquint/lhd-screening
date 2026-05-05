"""
Stage NHD flowlines and USGS 3DEP DEM tiles for the full LHD dataset.

Steps
-----
1. For each dam, fetch NHD flowlines (100 m upstream, 1 km downstream)
   and save as individual .gpkg files under STRM/{dam_id}/.
2. Query TNM for 1-m DEM tiles covering each flowline bbox
   (fallback: 1/9 arc-second, then 1/3 arc-second).
3. Record the dam → tile association in a manifest
   (tile_manifest.json + tile_manifest.csv).
4. Download all unique tiles to DEM/raw_3dep/.

Usage
-----
    python backend/stage_nhd_dem.py --staging-dir /data/lhd_staging

Optional flags
    --dams-csv PATH     path to dam CSV  [default: frontend/data/full_lhd_website.csv]
    --limit N           process only the first N dams (for smoke-testing)
    --skip-flowlines    re-use existing .gpkg files, skip NLDI calls
    --skip-download     build manifest but do not download tiles
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

_BACKEND_ROOT = Path(__file__).resolve().parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from lhd_processor.download_geospatial_data import (
    download_nhd_flowline,
    query_dem_tiles,
    download_raw_tile,
)

_REPO_ROOT = _BACKEND_ROOT.parent
DEFAULT_DAMS_CSV = _REPO_ROOT / "frontend" / "data" / "full_lhd_website.csv"

# NHD reach window: 100 m upstream, 1 km downstream
_FLOWLINE_DISTANCE_KM = (0.1, 1.0)


# ---------------------------------------------------------------------------
# Step 1 – flowlines
# ---------------------------------------------------------------------------

def stage_flowlines(
    dams_df: pd.DataFrame,
    flowline_dir: Path,
    skip: bool = False,
) -> Dict[int, dict]:
    """
    Download NHD flowlines for every dam.

    Returns
    -------
    dict mapping dam_id → {path: str|None, gdf: GeoDataFrame|None, comid: int|None}
    """
    results: Dict[int, dict] = {}
    total = len(dams_df)

    for i, row in dams_df.iterrows():
        dam_id = int(row["OBJECTID"])
        lat = float(row["Latitude"])
        lon = float(row["Longitude"])
        entry: dict = {"path": None, "gdf": None, "comid": None}

        site_dir = flowline_dir / str(dam_id)

        if skip and site_dir.exists():
            gpkg_files = list(site_dir.glob("nhd_flowline_*.gpkg"))
            if gpkg_files:
                import geopandas as gpd
                entry["path"] = str(gpkg_files[0])
                try:
                    entry["gdf"] = gpd.read_file(gpkg_files[0])
                except Exception:
                    pass
                m = re.search(r"nhd_flowline_(\d+)", gpkg_files[0].stem)
                if m:
                    entry["comid"] = int(m.group(1))
                print(f"[{i+1}/{total}] Dam {dam_id}: using cached flowline {gpkg_files[0].name}")
                results[dam_id] = entry
                continue

        print(f"[{i+1}/{total}] Dam {dam_id}: fetching NHD flowlines ({lat:.5f}, {lon:.5f}) ...")
        path, gdf = download_nhd_flowline(
            lat, lon,
            flowline_dir=str(flowline_dir),
            distance_km=_FLOWLINE_DISTANCE_KM,
            site_id=dam_id,
        )
        entry["path"] = path
        entry["gdf"] = gdf

        if path:
            m = re.search(r"nhd_flowline_(\d+)", Path(path).stem)
            if m:
                entry["comid"] = int(m.group(1))
            elif gdf is not None and not gdf.empty and "nhdplusid" in gdf.columns:
                entry["comid"] = int(gdf.iloc[0]["nhdplusid"])
            print(f"  → {path}")
        else:
            print(f"  → FAILED (no flowline)")

        results[dam_id] = entry

    return results


# ---------------------------------------------------------------------------
# Step 2 – tile manifest
# ---------------------------------------------------------------------------

def build_tile_manifest(
    dams_df: pd.DataFrame,
    flowline_results: Dict[int, dict],
) -> tuple[Dict[int, List[str]], Dict[str, dict]]:
    """
    Query TNM for DEM tiles that cover each dam's flowline.

    Returns
    -------
    manifest   : dam_id → [tile_filename, ...]
    tile_catalog: filename → {url, dataset, resolution_m}
    """
    manifest: Dict[int, List[str]] = {}
    tile_catalog: Dict[str, dict] = {}
    total = len(dams_df)

    for i, row in dams_df.iterrows():
        dam_id = int(row["OBJECTID"])
        fl = flowline_results.get(dam_id, {})
        gdf = fl.get("gdf")

        if gdf is None or gdf.empty:
            print(f"[{i+1}/{total}] Dam {dam_id}: skipping tile query (no flowline)")
            manifest[dam_id] = []
            continue

        tiles = query_dem_tiles(dam_id, gdf)
        filenames = [t["filename"] for t in tiles]
        manifest[dam_id] = filenames

        for t in tiles:
            if t["filename"] not in tile_catalog:
                tile_catalog[t["filename"]] = {
                    "url": t["url"],
                    "dataset": t["dataset"],
                    "resolution_m": t["resolution_m"],
                }

        if tiles:
            print(f"[{i+1}/{total}] Dam {dam_id}: {len(tiles)} tile(s) @ {tiles[0]['resolution_m']} m")
        else:
            print(f"[{i+1}/{total}] Dam {dam_id}: 0 tiles found")

    return manifest, tile_catalog


# ---------------------------------------------------------------------------
# Step 3 – save manifest
# ---------------------------------------------------------------------------

def save_manifest(
    manifest: Dict[int, List[str]],
    tile_catalog: Dict[str, dict],
    flowline_results: Dict[int, dict],
    staging_dir: Path,
) -> None:
    """Write tile_manifest.json and tile_manifest.csv to staging_dir."""
    # Enrich manifest JSON with COMID lookup for NWM
    comid_map = {dam_id: fl.get("comid") for dam_id, fl in flowline_results.items()}

    payload = {
        "dam_tiles": {str(k): v for k, v in manifest.items()},
        "dam_comids": {str(k): v for k, v in comid_map.items()},
        "tile_catalog": tile_catalog,
    }
    json_path = staging_dir / "tile_manifest.json"
    with open(json_path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Manifest saved: {json_path}")

    # Flat CSV for easy inspection
    rows = []
    for dam_id, filenames in manifest.items():
        comid = comid_map.get(dam_id)
        for fn in filenames:
            meta = tile_catalog.get(fn, {})
            rows.append({
                "dam_id": dam_id,
                "nwm_comid": comid,
                "tile_filename": fn,
                "dataset": meta.get("dataset", ""),
                "resolution_m": meta.get("resolution_m", ""),
                "url": meta.get("url", ""),
            })

    csv_path = staging_dir / "tile_manifest.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    print(f"CSV manifest saved: {csv_path}")


# ---------------------------------------------------------------------------
# Step 4 – download tiles
# ---------------------------------------------------------------------------

def download_all_tiles(tile_catalog: Dict[str, dict], raw_dem_dir: Path) -> None:
    """Download every unique tile in tile_catalog to raw_dem_dir."""
    total = len(tile_catalog)
    print(f"\nDownloading {total} unique DEM tile(s) → {raw_dem_dir}")
    ok = fail = 0

    for filename, meta in tile_catalog.items():
        local = raw_dem_dir / filename
        if local.exists():
            print(f"  [cached]  {filename}")
            ok += 1
            continue

        print(f"  [{ok+fail+1}/{total}] {filename} ...")
        result = download_raw_tile(meta["url"], str(raw_dem_dir))
        if result:
            ok += 1
        else:
            fail += 1

    print(f"\nTile downloads complete: {ok} ok, {fail} failed")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--staging-dir", required=True, type=Path,
        help="Root directory where staged data will be written",
    )
    parser.add_argument(
        "--dams-csv", type=Path, default=DEFAULT_DAMS_CSV,
        help=f"Dam inventory CSV (default: {DEFAULT_DAMS_CSV})",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only the first N dams (smoke-test mode)",
    )
    parser.add_argument(
        "--skip-flowlines", action="store_true",
        help="Skip NLDI calls; re-use any existing .gpkg files in STRM/",
    )
    parser.add_argument(
        "--skip-download", action="store_true",
        help="Build the manifest but do not download DEM tiles",
    )
    args = parser.parse_args()

    staging_dir: Path = args.staging_dir
    flowline_dir = staging_dir / "STRM"
    raw_dem_dir = staging_dir / "DEM" / "raw_3dep"
    flowline_dir.mkdir(parents=True, exist_ok=True)
    raw_dem_dir.mkdir(parents=True, exist_ok=True)

    # Load dam inventory
    dams_df = pd.read_csv(args.dams_csv)
    dams_df = dams_df[
        dams_df["Latitude"].notna() & dams_df["Longitude"].notna()
    ].reset_index(drop=True)
    if args.limit:
        dams_df = dams_df.head(args.limit)

    print(f"Processing {len(dams_df)} dams from {args.dams_csv}\n")

    # Step 1
    print("=" * 60)
    print("Step 1 — NHD Flowlines")
    print("=" * 60)
    flowline_results = stage_flowlines(dams_df, flowline_dir, skip=args.skip_flowlines)
    n_ok = sum(1 for v in flowline_results.values() if v["path"])
    print(f"\nFlowlines: {n_ok}/{len(dams_df)} succeeded\n")

    # Step 2
    print("=" * 60)
    print("Step 2 — DEM Tile Manifest")
    print("=" * 60)
    manifest, tile_catalog = build_tile_manifest(dams_df, flowline_results)
    n_tiles = sum(len(v) for v in manifest.values())
    n_unique = len(tile_catalog)
    print(f"\nTile references: {n_tiles} total, {n_unique} unique\n")

    # Step 3
    print("=" * 60)
    print("Step 3 — Saving Manifest")
    print("=" * 60)
    save_manifest(manifest, tile_catalog, flowline_results, staging_dir)

    # Step 4
    if not args.skip_download:
        print()
        print("=" * 60)
        print("Step 4 — Downloading DEM Tiles")
        print("=" * 60)
        download_all_tiles(tile_catalog, raw_dem_dir)
    else:
        print(f"\nSkipped tile download (--skip-download). {n_unique} unique tiles recorded.")

    print("\nDone.")


if __name__ == "__main__":
    main()
