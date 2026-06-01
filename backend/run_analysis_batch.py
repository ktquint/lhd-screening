"""
Post-ARC analysis batch: for every staged dam that already has ARC outputs,
derive the surrogate weir length, extract the 5 local hydraulic
cross-sections at multiples of L, then solve for dam height and the
dangerous-flow range (Qmin / Qmax) at each downstream cross-section.

This is the second half of what run_arc_batch.py used to do, split off so
ARC can be re-run independently of the geometry analysis.

For each dam in tile_manifest.json this script:
  1. Confirms RESULTS/{dam_id}/arc_done.json exists (from run_arc_batch).
  2. Reconstructs an ArcDam object (no ARC run — just path bookkeeping).
  3. Calls estimate_weir_length() on the staged stream raster + ARC outputs.
  4. Calls ArcDam.extract_local_xs() to write the Local_*.gpkg files Dam needs.
  5. Constructs lhd_processor.analysis_classes.Dam with an empty flow_series
     (so FDC-based prob_min/prob_max are skipped — geometry only).
  6. Calls Dam.run_analysis() to populate per-XS dam_height + Qmin/Qmax.
  7. Writes RESULTS/{dam_id}/analysis_summary.json.

Existing summaries are reused; pass --force to recompute.

Usage
-----
    python backend/run_analysis_batch.py --staging-dir /data/lhd_staging
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
from lhd_processor.analysis_classes import Dam
from screening.height import estimate_dam_height
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
    """Locate every staged file we need. Returns None if anything is missing."""
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
    for p in paths.values():
        if not p.exists():
            return None
    return paths


def _read_baseflow(flow_csv: Path, comid: Optional[int]) -> Optional[float]:
    """Pull q_ep_50 for `comid` out of the per-dam flow CSV. Falls back to
    the first non-NaN row if comid is missing/unknown."""
    try:
        df = pd.read_csv(flow_csv)
    except Exception:
        return None
    if df.empty or "q_ep_50" not in df.columns:
        return None
    if comid is not None and "comid" in df.columns:
        match = df.loc[df["comid"] == int(comid), "q_ep_50"]
        if not match.empty and pd.notna(match.iloc[0]):
            return float(match.iloc[0])
    valid = df["q_ep_50"].dropna()
    if valid.empty:
        return None
    return float(valid.iloc[0])


# ---------------------------------------------------------------------------
# Per-dam worker
# ---------------------------------------------------------------------------

def _process_dam(
    idx: int,
    total: int,
    dam_id: int,
    lat: float,
    lon: float,
    comid: Optional[int],
    staging_dir: Path,
    results_dir: Path,
    force: bool,
) -> Tuple[int, str]:
    summary_path = results_dir / str(dam_id) / "analysis_summary.json"
    if not force and summary_path.exists():
        _log(f"[{idx}/{total}] Dam {dam_id}: cached")
        return dam_id, "cached"

    arc_marker = results_dir / str(dam_id) / "arc_done.json"
    if not arc_marker.exists():
        _log(f"[{idx}/{total}] Dam {dam_id}: SKIP (no ARC output — run run_arc_batch first)")
        return dam_id, "missing-arc"

    paths = _resolve_inputs(dam_id, staging_dir)
    if paths is None:
        _log(f"[{idx}/{total}] Dam {dam_id}: SKIP (missing staged inputs)")
        return dam_id, "missing-input"

    baseflow = _read_baseflow(paths["flow_csv"], comid)
    if baseflow is None or baseflow <= 0:
        _log(f"[{idx}/{total}] Dam {dam_id}: SKIP (no valid q_ep_50 in {paths['flow_csv'].name})")
        return dam_id, "no-baseflow"

    # Reconstruct ArcDam without running ARC — we only need the path bookkeeping
    # plus extract_local_xs(). _prepare_dirs_and_paths() is called by
    # extract_local_xs() automatically when vdt_txt is None.
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
        arc._prepare_dirs_and_paths()
    except Exception as e:
        _log(f"[{idx}/{total}] Dam {dam_id}: FAIL ArcDam init ({e})")
        return dam_id, f"init-error:{e}"

    for attr in ("vdt_txt", "curvefile_csv", "xs_txt"):
        p = getattr(arc, attr, None)
        if p is None or not Path(p).exists():
            _log(f"[{idx}/{total}] Dam {dam_id}: FAIL — ARC output missing ({attr})")
            return dam_id, f"arc-no-{attr}"

    # ----- Weir length (Method 2 — flat-zone crest sampler) -----
    try:
        width_info = estimate_weir_length(
            dam_lat=lat,
            dam_lon=lon,
            strm_path=paths["strm_clean"],
            vdt_path=arc.vdt_txt,
            xs_path=arc.xs_txt,
            curve_path=arc.curvefile_csv,
            flowline_path=paths["flowline"],
        )
    except Exception as e:
        _log(f"[{idx}/{total}] Dam {dam_id}: FAIL weir length ({e})")
        return dam_id, f"weir-error:{e}"
    weir_length = float(width_info["weir_length"])

    # ----- Dam height (Method B — ARC-slope flat-zone tailwater) -----
    height_info = estimate_dam_height(
        baseflow_cms=baseflow,
        weir_length_m=weir_length,
        reach=width_info["reach"],
        crest_wse=float(width_info["crest_wse"]),
    )
    precomputed_P = (
        float(height_info["P_height_m"]) if height_info is not None else None
    )

    # ----- Local cross-sections at multiples of L -----
    try:
        arc.extract_local_xs(
            snap_row=int(width_info["snap_row"]),
            snap_col=int(width_info["snap_col"]),
            weir_length=weir_length,
        )
    except Exception as e:
        _log(f"[{idx}/{total}] Dam {dam_id}: FAIL local XS extract ({e})")
        return dam_id, f"localxs-error:{e}"

    # ----- Dam height + dangerous-flow range -----
    # Empty flow_series so Dam skips FDC / probability calculations
    # (we asked for geometry-only) and skips the NWM zarr fetch.
    try:
        dam = Dam(
            lhd_id=dam_id,
            latitude=lat,
            longitude=lon,
            weir_length=weir_length,
            baseflow=baseflow,
            base_results_dir=results_dir,
            flow_series=pd.Series(dtype=float),
            flowline_source="NHDPlus",
            streamflow_source="National Water Model",
            calc_mode="Simplified",
            nwm_id=comid,
            precomputed_dam_height_m=precomputed_P,
        )
        xs_data_list, _hydro_results = dam.run_analysis()
    except Exception as e:
        _log(f"[{idx}/{total}] Dam {dam_id}: FAIL Dam analysis ({e})")
        return dam_id, f"analysis-error:{e}"

    # Trim per-XS payload to the geometry-only fields we care about.
    xs_results = []
    for row in xs_data_list:
        xs_results.append({
            "xs_index": row.get("xs_index"),
            "P_height_m": row.get("P_height"),
            "Qmin_cms": row.get("Qmin"),
            "Qmax_cms": row.get("Qmax"),
        })

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "dam_id": dam_id,
        "latitude": lat,
        "longitude": lon,
        "comid": int(comid) if comid is not None else None,
        "baseflow_q_ep_50_cms": baseflow,
        "weir_length_m": weir_length,
        "snap_row": int(width_info["snap_row"]),
        "snap_col": int(width_info["snap_col"]),
        "snap_dist_m": float(width_info["snap_dist_m"]),
        "wse_used": float(width_info["wse_used"]),
        "arc_wse": (float(width_info["arc_wse"])
                    if width_info.get("arc_wse") is not None else None),
        "ordinate_dist": float(width_info["ordinate_dist"]),
        "crest_row": int(width_info["crest_row"]),
        "crest_col": int(width_info["crest_col"]),
        "crest_wse": float(width_info["crest_wse"]),
        "crest_base": float(width_info["crest_base"]),
        "crest_x_m": float(width_info["crest_x_m"]),
        "dam_height_m": precomputed_P,
        "height_info": (
            {k: v for k, v in height_info.items()} if height_info is not None else None
        ),
        "xs_results": xs_results,
    }
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    p_str = f"P={precomputed_P:.2f} m" if precomputed_P is not None else "P=–"
    _log(f"[{idx}/{total}] Dam {dam_id}: ok (L={weir_length:.1f} m, {p_str})")
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
                        help="Recompute even if analysis_summary.json exists")
    args = parser.parse_args()

    staging_dir: Path = args.staging_dir
    results_dir = staging_dir / "RESULTS"
    if not results_dir.exists():
        sys.exit(f"No RESULTS/ directory under {staging_dir} — run run_arc_batch.py first.")

    manifest_path = staging_dir / "tile_manifest.json"
    if not manifest_path.exists():
        sys.exit(f"Manifest not found: {manifest_path}")
    with open(manifest_path) as f:
        manifest = json.load(f)
    dam_ids: List[int] = [int(k) for k in manifest.get("dam_tiles", {}).keys()]
    dam_comids: Dict[int, Optional[int]] = {
        int(k): (int(v) if v is not None else None)
        for k, v in manifest.get("dam_comids", {}).items()
    }
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

    pairs: List[Tuple[int, float, float, Optional[int]]] = []
    no_coords = 0
    for dam_id in dam_ids:
        if dam_id not in coord_lookup:
            no_coords += 1
            continue
        lat, lon = coord_lookup[dam_id]
        pairs.append((dam_id, lat, lon, dam_comids.get(dam_id)))
    if no_coords:
        print(f"Skipping {no_coords} dams with no lat/lon in {args.dams_csv}\n")
    total = len(pairs)

    print(f"Running post-ARC analysis for {total} dams (workers={args.workers})\n")

    counts: Dict[str, int] = {}
    failures: Dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(
                _process_dam, i + 1, total, dam_id, lat, lon, comid,
                staging_dir, results_dir, args.force,
            ): dam_id
            for i, (dam_id, lat, lon, comid) in enumerate(pairs)
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

    failures_path = staging_dir / "failures_analysis.json"
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
