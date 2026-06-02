"""
Run ARC for every staged dam: produce the raw VDT / Curve / XS artifacts.

For each dam in tile_manifest.json this script:
  1. Resolves the staged input set
        DEM/{dam_id}/dem_{dam_id}.tif
        LAND/{dam_id}/constant_land.tif + Manning_n.txt
        STRM/{dam_id}/nhd_flowline_{nhdplusid}.gpkg
        STRM/{dam_id}/nhd_{nhdplusid}_clean.tif
        FLOW/{dam_id}/flow_{dam_id}.csv
     plus dam lat/lon from the dam inventory CSV (OBJECTID-keyed).
  2. Runs ArcDam.run_arc() → VDT.txt, Curve.csv, XS.txt under RESULTS/{dam_id}/
  3. Writes RESULTS/{dam_id}/arc_done.json as a fast cache marker so reruns
     can skip without reconstructing ArcDam.

Weir-length estimation, local cross-section extraction, dam-height, and
dangerous-flow analysis are split out into run_analysis_batch.py so ARC
can be re-run independently of the downstream geometry analysis.

Existing arc_done.json markers are reused; pass --force to re-run.

Usage
-----
    python backend/run_arc_batch.py --staging-dir /data/lhd_staging
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

_BACKEND_ROOT = Path(__file__).resolve().parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from lhd_processor.lhd_arc import ArcDam

_REPO_ROOT = _BACKEND_ROOT.parent
DEFAULT_DAMS_CSV = _REPO_ROOT / "data" / "full_lhd_website.csv"

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ---------------------------------------------------------------------------
# Input resolution
# ---------------------------------------------------------------------------

def _resolve_inputs(
    dam_id: int,
    staging_dir: Path,
) -> Optional[Dict[str, Path]]:
    """Locate every staged file ArcDam needs. Returns None if anything is missing."""
    strm_site = staging_dir / "STRM" / str(dam_id)
    gpkgs = list(strm_site.glob("nhd_flowline_*.gpkg"))
    if not gpkgs:
        return None
    gpkg = gpkgs[0]
    m = re.search(r"nhd_flowline_(\d+)", gpkg.stem)
    if not m:
        return None
    nhdplusid = int(m.group(1))

    paths = {
        "flowline": gpkg,
        "strm_clean": strm_site / f"nhd_{nhdplusid}_clean.tif",
        "dem": staging_dir / "DEM" / str(dam_id) / f"dem_{dam_id}.tif",
        "land": staging_dir / "LAND" / str(dam_id) / "constant_land.tif",
        "manning": staging_dir / "LAND" / str(dam_id) / "Manning_n.txt",
        "flow_csv": staging_dir / "FLOW" / str(dam_id) / f"flow_{dam_id}.csv",
    }
    for key, p in paths.items():
        if not p.exists():
            return None
    return paths


# ---------------------------------------------------------------------------
# Per-dam worker
# ---------------------------------------------------------------------------

def _process_dam(
    idx: int,
    total: int,
    dam_id: int,
    lat: float,
    lon: float,
    staging_dir: Path,
    results_dir: Path,
    force: bool,
) -> Tuple[int, str]:
    marker_path = results_dir / str(dam_id) / "arc_done.json"
    if not force and marker_path.exists():
        _log(f"[{idx}/{total}] Dam {dam_id}: cached")
        return dam_id, "cached"

    paths = _resolve_inputs(dam_id, staging_dir)
    if paths is None:
        _log(f"[{idx}/{total}] Dam {dam_id}: SKIP (missing staged inputs)")
        return dam_id, "missing-input"

    try:
        arc = ArcDam(
            dam_id=dam_id,
            latitude=lat,
            longitude=lon,
            dem_path=paths["dem"],
            land_raster=paths["land"],
            strm_tif_clean=paths["strm_clean"],
            flowline_path=paths["flowline"],
            flowline_source="NHDPlus",
            streamflow_source="National Water Model",
            streamflow_csv=paths["flow_csv"],
            results_dir=results_dir,
            manning_n_txt=paths["manning"],
            baseflow_col="q_ep_50",
            qmax_col="rp100",
        )
    except Exception as e:
        _log(f"[{idx}/{total}] Dam {dam_id}: FAIL ArcDam init ({e})")
        return dam_id, f"init-error:{e}"

    try:
        arc.run_arc()
    except Exception as e:
        _log(f"[{idx}/{total}] Dam {dam_id}: FAIL run_arc ({e})")
        return dam_id, f"arc-error:{e}"

    # Verify ARC actually produced its three core artifacts.
    for attr in ("vdt_txt", "curvefile_csv", "xs_txt"):
        p = getattr(arc, attr, None)
        if p is None or not Path(p).exists():
            _log(f"[{idx}/{total}] Dam {dam_id}: FAIL — ARC missing {attr}")
            return dam_id, f"arc-no-{attr}"

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker = {
        "dam_id": dam_id,
        "latitude": lat,
        "longitude": lon,
        "vdt_txt": str(arc.vdt_txt),
        "curvefile_csv": str(arc.curvefile_csv),
        "xs_txt": str(arc.xs_txt),
    }
    with open(marker_path, "w") as f:
        json.dump(marker, f, indent=2)

    _log(f"[{idx}/{total}] Dam {dam_id}: ok")
    return dam_id, "ok"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--staging-dir", required=True, type=Path,
                        help="Root of the staging tree")
    parser.add_argument("--dams-csv", type=Path, default=DEFAULT_DAMS_CSV,
                        help=f"Dam inventory CSV (default: {DEFAULT_DAMS_CSV})")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N dams in the manifest")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel workers [default: 4]")
    parser.add_argument("--force", action="store_true",
                        help="Re-run ARC even if RESULTS/{dam_id}/arc_done.json exists")
    args = parser.parse_args()

    staging_dir: Path = args.staging_dir
    results_dir = staging_dir / "RESULTS"
    results_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = staging_dir / "tile_manifest.json"
    if not manifest_path.exists():
        sys.exit(f"Manifest not found: {manifest_path}\nRun stage_nhd_dem.py first.")
    with open(manifest_path) as f:
        manifest = json.load(f)
    dam_ids: List[int] = [int(k) for k in manifest.get("dam_tiles", {}).keys()]
    if args.limit:
        dam_ids = dam_ids[: args.limit]

    if not args.dams_csv.exists():
        sys.exit(f"Dam inventory CSV not found: {args.dams_csv}")
    dams_df = pd.read_csv(args.dams_csv)
    dams_df = dams_df.dropna(subset=["OBJECTID", "Latitude", "Longitude"])
    dams_df["OBJECTID"] = dams_df["OBJECTID"].astype(int)
    coord_lookup: Dict[int, Tuple[float, float]] = {
        int(r["OBJECTID"]): (float(r["Latitude"]), float(r["Longitude"]))
        for _, r in dams_df.iterrows()
    }

    pairs: List[Tuple[int, float, float]] = []
    no_coords = 0
    for dam_id in dam_ids:
        if dam_id not in coord_lookup:
            no_coords += 1
            continue
        lat, lon = coord_lookup[dam_id]
        pairs.append((dam_id, lat, lon))
    if no_coords:
        print(f"Skipping {no_coords} dams with no lat/lon in {args.dams_csv}\n")
    total = len(pairs)

    print(f"Running ARC for {total} dams (workers={args.workers})\n")

    counts: Dict[str, int] = {}
    failures: Dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(
                _process_dam, i + 1, total, dam_id, lat, lon,
                staging_dir, results_dir, args.force,
            ): dam_id
            for i, (dam_id, lat, lon) in enumerate(pairs)
        }
        for fut in as_completed(futures):
            dam_id = futures[fut]
            try:
                _, status = fut.result()
            except Exception as e:
                status = f"worker-error:{e}"
                _log(f"  ! Dam {dam_id} worker error: {e}")
                traceback.print_exc()
            counts[status] = counts.get(status, 0) + 1
            if status != "ok":
                failures[int(dam_id)] = status

    print("\nSummary:")
    for status in sorted(counts):
        print(f"  {status:<22} {counts[status]}")

    failures_path = staging_dir / "failures_arc.json"
    if failures:
        with open(failures_path, "w") as f:
            json.dump(
                {str(k): v for k, v in sorted(failures.items())},
                f, indent=2,
            )
        print(f"\nWrote per-dam failure reasons → {failures_path}")
    elif failures_path.exists():
        # Last run had failures, this one fixed them — clean up the stale file.
        failures_path.unlink()


if __name__ == "__main__":
    main()
