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

Steps 1 + 2 run in parallel across dams; step 4 downloads in parallel.
Cached flowline .gpkg files and cached DEM tiles are reused automatically
(use --force-flowlines / --force-tiles to override).

Usage
-----
    python backend/stage_nhd_dem.py --staging-dir /data/lhd_staging

Optional flags
    --dams-csv PATH       path to dam CSV  [default: frontend/data/full_lhd_website.csv]
    --limit N             process only the first N dams (for smoke-testing)
    --force-flowlines     refetch NHD flowlines even if a cached .gpkg exists
    --force-tiles         re-download DEM tiles even if already on disk
    --skip-download       build manifest but do not download tiles
    --workers N           parallel workers for flowline + tile-query stage [default: 8]
    --download-workers N  parallel workers for DEM tile downloads [default: 8]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cached_flowline(site_dir: Path) -> Optional[dict]:
    """Return {path, gdf, comid} if a cached nhd_flowline_*.gpkg exists, else None."""
    if not site_dir.exists():
        return None
    gpkg_files = list(site_dir.glob("nhd_flowline_*.gpkg"))
    if not gpkg_files:
        return None

    import geopandas as gpd
    path = gpkg_files[0]
    try:
        gdf = gpd.read_file(path)
    except Exception as e:
        _log(f"  ! failed reading cached {path.name}: {e} — will refetch")
        return None

    comid = None
    m = re.search(r"nhd_flowline_(\d+)", path.stem)
    if m:
        comid = int(m.group(1))
    elif gdf is not None and not gdf.empty and "nhdplusid" in gdf.columns:
        comid = int(gdf.iloc[0]["nhdplusid"])

    return {"path": str(path), "gdf": gdf, "comid": comid}


# ---------------------------------------------------------------------------
# Per-dam worker: flowline + tile query
# ---------------------------------------------------------------------------

def _process_dam(
    idx: int,
    total: int,
    dam_id: int,
    lat: float,
    lon: float,
    flowline_dir: Path,
    force_flowlines: bool,
) -> Tuple[int, dict, List[dict], bool]:
    """
    Resolve a single dam's flowline (cached or freshly downloaded) and query
    its DEM tiles. Returns (dam_id, flowline_entry, tiles, used_cache).
    """
    site_dir = flowline_dir / str(dam_id)

    flowline_entry: Optional[dict] = None
    used_cache = False
    if not force_flowlines:
        flowline_entry = _load_cached_flowline(site_dir)
        if flowline_entry is not None:
            used_cache = True
            _log(
                f"[{idx}/{total}] Dam {dam_id}: cached flowline "
                f"{Path(flowline_entry['path']).name}"
            )

    if flowline_entry is None:
        _log(f"[{idx}/{total}] Dam {dam_id}: fetching NHD flowlines ({lat:.5f}, {lon:.5f}) ...")
        path, gdf = download_nhd_flowline(
            lat, lon,
            flowline_dir=str(flowline_dir),
            distance_km=_FLOWLINE_DISTANCE_KM,
            site_id=dam_id,
        )
        comid: Optional[int] = None
        if path:
            m = re.search(r"nhd_flowline_(\d+)", Path(path).stem)
            if m:
                comid = int(m.group(1))
            elif gdf is not None and not gdf.empty and "nhdplusid" in gdf.columns:
                comid = int(gdf.iloc[0]["nhdplusid"])
            _log(f"[{idx}/{total}] Dam {dam_id}: → {path}")
        else:
            _log(f"[{idx}/{total}] Dam {dam_id}: → FAILED (no flowline)")
        flowline_entry = {"path": path, "gdf": gdf, "comid": comid}

    gdf = flowline_entry.get("gdf")
    if gdf is None or gdf.empty:
        return dam_id, flowline_entry, [], used_cache

    tiles = query_dem_tiles(dam_id, gdf)
    if tiles:
        _log(
            f"[{idx}/{total}] Dam {dam_id}: {len(tiles)} tile(s) @ "
            f"{tiles[0]['resolution_m']} m"
        )
    else:
        _log(f"[{idx}/{total}] Dam {dam_id}: 0 tiles found")

    return dam_id, flowline_entry, tiles, used_cache


def stage_dams_parallel(
    dams_df: pd.DataFrame,
    flowline_dir: Path,
    force_flowlines: bool,
    workers: int,
) -> Tuple[Dict[int, dict], Dict[int, List[str]], Dict[str, dict]]:
    """
    Run flowline fetch + DEM-tile query for each dam in parallel.

    Returns
    -------
    flowline_results : dam_id → {path, gdf, comid}
    manifest         : dam_id → [tile_filename, ...] (in dams_df order)
    tile_catalog     : filename → {url, dataset, resolution_m}
    """
    flowline_results: Dict[int, dict] = {}
    per_dam_tiles: Dict[int, List[str]] = {}
    tile_catalog: Dict[str, dict] = {}
    total = len(dams_df)

    jobs = []
    for i, row in dams_df.iterrows():
        jobs.append((
            i + 1,
            int(row["OBJECTID"]),
            float(row["Latitude"]),
            float(row["Longitude"]),
        ))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _process_dam,
                idx, total, dam_id, lat, lon,
                flowline_dir, force_flowlines,
            ): dam_id
            for idx, dam_id, lat, lon in jobs
        }
        for fut in as_completed(futures):
            dam_id = futures[fut]
            try:
                dam_id, entry, tiles, _used_cache = fut.result()
            except Exception as e:
                _log(f"  ! Dam {dam_id} worker error: {e}")
                flowline_results[dam_id] = {"path": None, "gdf": None, "comid": None}
                per_dam_tiles[dam_id] = []
                continue

            flowline_results[dam_id] = entry
            per_dam_tiles[dam_id] = [t["filename"] for t in tiles]
            for t in tiles:
                tile_catalog.setdefault(t["filename"], {
                    "url": t["url"],
                    "dataset": t["dataset"],
                    "resolution_m": t["resolution_m"],
                })

    # Preserve dams_df ordering in manifest
    manifest: Dict[int, List[str]] = {
        int(row["OBJECTID"]): per_dam_tiles.get(int(row["OBJECTID"]), [])
        for _, row in dams_df.iterrows()
    }
    return flowline_results, manifest, tile_catalog


# ---------------------------------------------------------------------------
# Manifest output
# ---------------------------------------------------------------------------

def save_manifest(
    manifest: Dict[int, List[str]],
    tile_catalog: Dict[str, dict],
    flowline_results: Dict[int, dict],
    staging_dir: Path,
) -> None:
    """Write tile_manifest.json and tile_manifest.csv to staging_dir."""
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
# Parallel tile downloads
# ---------------------------------------------------------------------------

def download_all_tiles(
    tile_catalog: Dict[str, dict],
    raw_dem_dir: Path,
    workers: int,
    force: bool,
) -> None:
    """Download every unique tile in tile_catalog, skipping those already on disk."""
    todo: List[Tuple[str, dict]] = []
    cached = 0
    for filename, meta in tile_catalog.items():
        if not force and (raw_dem_dir / filename).exists():
            cached += 1
        else:
            todo.append((filename, meta))

    print(
        f"\nDEM tile downloads → {raw_dem_dir}: "
        f"{len(todo)} to fetch, {cached} already cached "
        f"(of {len(tile_catalog)} total)"
    )

    if not todo:
        print("Nothing to download.")
        return

    ok = fail = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(download_raw_tile, meta["url"], str(raw_dem_dir)): filename
            for filename, meta in todo
        }
        done = 0
        for fut in as_completed(futures):
            filename = futures[fut]
            done += 1
            try:
                result = fut.result()
            except Exception as e:
                result = None
                _log(f"  [{done}/{len(todo)}] FAIL  {filename}: {e}")
            if result:
                ok += 1
                _log(f"  [{done}/{len(todo)}] ok    {filename}")
            else:
                # download_raw_tile already prints its own error; just count it.
                fail += 1

    print(
        f"\nTile downloads complete: {ok + cached} ok ({cached} cached), "
        f"{fail} failed"
    )


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
        "--force-flowlines", action="store_true",
        help="Refetch NHD flowlines even if a cached .gpkg already exists.",
    )
    parser.add_argument(
        "--force-tiles", action="store_true",
        help="Re-download DEM tiles even if already present on disk.",
    )
    parser.add_argument(
        "--skip-download", action="store_true",
        help="Build the manifest but do not download DEM tiles",
    )
    parser.add_argument(
        "--workers", type=int, default=8,
        help="Parallel workers for flowline + tile-query stage [default: 8]",
    )
    parser.add_argument(
        "--download-workers", type=int, default=8,
        help="Parallel workers for DEM tile downloads [default: 8]",
    )
    args = parser.parse_args()

    staging_dir: Path = args.staging_dir
    flowline_dir = staging_dir / "STRM"
    raw_dem_dir = staging_dir / "DEM" / "raw_3dep"
    flowline_dir.mkdir(parents=True, exist_ok=True)
    raw_dem_dir.mkdir(parents=True, exist_ok=True)

    dams_df = pd.read_csv(args.dams_csv)
    dams_df = dams_df[
        dams_df["Latitude"].notna() & dams_df["Longitude"].notna()
    ].reset_index(drop=True)
    if args.limit:
        dams_df = dams_df.head(args.limit)

    print(
        f"Processing {len(dams_df)} dams from {args.dams_csv} "
        f"(workers={args.workers}, download_workers={args.download_workers})\n"
    )

    print("=" * 60)
    print("Steps 1 + 2 — NHD Flowlines + DEM Tile Manifest (parallel)")
    print("=" * 60)
    flowline_results, manifest, tile_catalog = stage_dams_parallel(
        dams_df, flowline_dir,
        force_flowlines=args.force_flowlines,
        workers=args.workers,
    )
    n_ok = sum(1 for v in flowline_results.values() if v.get("path"))
    n_tiles = sum(len(v) for v in manifest.values())
    n_unique = len(tile_catalog)
    print(
        f"\nFlowlines: {n_ok}/{len(dams_df)} succeeded — "
        f"tile references: {n_tiles} total, {n_unique} unique\n"
    )

    print("=" * 60)
    print("Step 3 — Saving Manifest")
    print("=" * 60)
    save_manifest(manifest, tile_catalog, flowline_results, staging_dir)

    if not args.skip_download:
        print()
        print("=" * 60)
        print("Step 4 — Downloading DEM Tiles")
        print("=" * 60)
        download_all_tiles(
            tile_catalog, raw_dem_dir,
            workers=args.download_workers,
            force=args.force_tiles,
        )
    else:
        print(f"\nSkipped tile download (--skip-download). {n_unique} unique tiles recorded.")

    print("\nDone.")


if __name__ == "__main__":
    main()
