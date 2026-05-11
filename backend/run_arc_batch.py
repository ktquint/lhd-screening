"""
Run ARC for every staged dam: produce VDT / Curve / XS artifacts, derive a
surrogate weir length from the lidar water-surface width at the snapped
stream pixel, and extract the 5 hydraulic cross-sections at multiples of
that weir length.

For each dam in tile_manifest.json this script:
  1. Resolves the staged input set
        DEM/{dam_id}/dem_{dam_id}.tif
        LAND/{dam_id}/constant_land.tif + Manning_n.txt
        STRM/{dam_id}/nhd_flowline_{nhdplusid}.gpkg
        STRM/{dam_id}/nhd_{nhdplusid}_clean.tif
        FLOW/{dam_id}/flow_{dam_id}.csv
     plus dam lat/lon from the dam inventory CSV (OBJECTID-keyed).
  2. Runs ArcDam.run_arc()  →  VDT.txt, Curve.csv, XS.txt under RESULTS/{dam_id}/
  3. Snaps the dam point to the nearest VDT cell and reads the lidar
     water-surface width there  →  weir_length (the "surrogate L").
  4. Re-extracts the 5 local hydraulic cross-sections at multiples of L.
  5. Writes RESULTS/{dam_id}/arc_summary.json with weir_length + snap info.

Existing summaries are reused; pass --force to re-run.

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
from screening.width import estimate_weir_length

_REPO_ROOT = _BACKEND_ROOT.parent
DEFAULT_DAMS_CSV = _REPO_ROOT / "frontend" / "data" / "full_lhd_website.csv"

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
    summary_path = results_dir / str(dam_id) / "arc_summary.json"
    if not force and summary_path.exists():
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

    # Verify ARC actually produced its three core artifacts before we proceed.
    for attr in ("vdt_txt", "curvefile_csv", "xs_txt"):
        p = getattr(arc, attr, None)
        if p is None or not Path(p).exists():
            _log(f"[{idx}/{total}] Dam {dam_id}: FAIL — ARC missing {attr}")
            return dam_id, f"arc-no-{attr}"

    try:
        width_info = estimate_weir_length(
            dam_lat=lat,
            dam_lon=lon,
            strm_path=paths["strm_clean"],
            vdt_path=arc.vdt_txt,
            xs_path=arc.xs_txt,
            curve_path=arc.curvefile_csv,
        )
    except Exception as e:
        _log(f"[{idx}/{total}] Dam {dam_id}: FAIL weir length ({e})")
        return dam_id, f"weir-error:{e}"

    weir_length = float(width_info["weir_length"])

    try:
        arc.extract_local_xs(
            snap_row=int(width_info["snap_row"]),
            snap_col=int(width_info["snap_col"]),
            weir_length=weir_length,
        )
    except Exception as e:
        _log(f"[{idx}/{total}] Dam {dam_id}: FAIL local XS extract ({e})")
        return dam_id, f"localxs-error:{e}"

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "dam_id": dam_id,
        "latitude": lat,
        "longitude": lon,
        "weir_length_m": weir_length,
        "snap_row": int(width_info["snap_row"]),
        "snap_col": int(width_info["snap_col"]),
        "snap_dist_m": float(width_info["snap_dist_m"]),
        "wse_used": float(width_info["wse_used"]),
        "arc_wse": (float(width_info["arc_wse"])
                    if width_info.get("arc_wse") is not None else None),
        "ordinate_dist": float(width_info["ordinate_dist"]),
        "vdt_txt": str(arc.vdt_txt),
        "curvefile_csv": str(arc.curvefile_csv),
        "xs_txt": str(arc.xs_txt),
    }
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    _log(f"[{idx}/{total}] Dam {dam_id}: ok (L={weir_length:.1f} m, snap_dist={summary['snap_dist_m']:.1f} m)")
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
                        help="Re-run ARC even if RESULTS/{dam_id}/arc_summary.json exists")
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

    print("\nSummary:")
    for status in sorted(counts):
        print(f"  {status:<22} {counts[status]}")


if __name__ == "__main__":
    main()
