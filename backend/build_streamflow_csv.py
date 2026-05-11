"""
Build per-dam NWM-derived streamflow CSVs (q_ep_50 + rp100) for ARC.

For each dam in tile_manifest.json:
  1. Read the staged NHD flowline to collect its NWM feature_ids
     (nhdplusid == NWM feature_id).
  2. Slice the NWM v3 retrospective daily streamflow series for those ids
     from the AWS public Zarr.
  3. Compute q_ep_50 (median daily flow) and rp100 (100-year return-period
     flow via Log-Pearson Type III on the annual maximum series).
  4. Write FLOW/{dam_id}/flow_{dam_id}.csv with columns:
        comid, q_ep_50, rp100

The NWM Zarr is opened once and shared across worker threads.

Usage
-----
    python backend/build_streamflow_csv.py --staging-dir /data/lhd_staging
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Tuple

import fsspec
import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr
from scipy.stats import pearson3

# NWM v3.0 retrospective on the AWS Open Data registry. Override with
# --nwm-url if the path changes. Use the daily-aggregated channel routing
# variable so we don't need to resample 40+ years of hourly data on the fly.
_DEFAULT_NWM_URL = "s3://noaa-nwm-retrospective-3-0-pds/CONUS/zarr/chrtout.zarr"
_FEATURE_DIM = "feature_id"
_FLOW_VAR = "streamflow"
_RP_TARGET_YEARS = 100

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# ---------------------------------------------------------------------------
# NWM access
# ---------------------------------------------------------------------------

def _open_nwm_zarr(url: str) -> xr.Dataset:
    """Open the NWM retrospective Zarr (anonymous S3 read) and return ds."""
    scheme, _, rest = url.partition("://")
    if not scheme:
        # Local path
        return xr.open_zarr(url, consolidated=True)
    fs = fsspec.filesystem(scheme, anon=True)
    mapper = fs.get_mapper(rest)
    return xr.open_zarr(mapper, consolidated=True)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _log_pearson_iii_rp(annual_max: np.ndarray, T: int) -> float:
    """Estimate T-year return flow from an annual-maximum series using
    Log-Pearson Type III (Bulletin 17C). Returns NaN if the series is too
    short or degenerate."""
    annual_max = annual_max[np.isfinite(annual_max)]
    annual_max = annual_max[annual_max > 0]
    if len(annual_max) < 10:
        return float("nan")
    y = np.log10(annual_max)
    y_mean = float(np.mean(y))
    y_std = float(np.std(y, ddof=1))
    if y_std == 0.0:
        return float("nan")
    y_skew = float(pd.Series(y).skew())  # unbiased sample skew
    # pearson3.ppf returns the K_T frequency factor (standardized P-III quantile)
    try:
        K_T = float(pearson3.ppf(1.0 - 1.0 / T, y_skew))
    except Exception:
        return float("nan")
    return float(10.0 ** (y_mean + K_T * y_std))


def _per_id_stats(da: xr.DataArray) -> Tuple[float, float]:
    """Return (q_ep_50, rp100) for a single feature_id's daily flow series."""
    flow = da.values.astype(float)
    flow = flow[np.isfinite(flow)]
    if len(flow) == 0:
        return float("nan"), float("nan")
    q_ep_50 = float(np.median(flow))
    if "time" in da.dims:
        annual_max = da.groupby("time.year").max(dim="time").values.astype(float)
        rp100 = _log_pearson_iii_rp(annual_max, _RP_TARGET_YEARS)
    else:
        rp100 = float("nan")
    return q_ep_50, rp100


# ---------------------------------------------------------------------------
# Per-dam worker
# ---------------------------------------------------------------------------

def _flowline_ids(gpkg_path: Path) -> List[int]:
    gdf = gpd.read_file(gpkg_path)
    if "nhdplusid" not in gdf.columns:
        return []
    ids = pd.to_numeric(gdf["nhdplusid"], errors="coerce").dropna().astype(np.int64)
    return ids.unique().tolist()


def _process_dam(
    idx: int,
    total: int,
    dam_id: int,
    gpkg_path: Path,
    ds: xr.Dataset,
    nwm_ids: set,
    out_dir: Path,
    force: bool,
) -> Tuple[int, str]:
    out_path = out_dir / str(dam_id) / f"flow_{dam_id}.csv"
    if not force and out_path.exists():
        _log(f"[{idx}/{total}] Dam {dam_id}: cached")
        return dam_id, "cached"

    try:
        ids = _flowline_ids(gpkg_path)
    except Exception as e:
        _log(f"[{idx}/{total}] Dam {dam_id}: FAIL read flowline ({e})")
        return dam_id, f"flowline-error:{e}"

    if not ids:
        _log(f"[{idx}/{total}] Dam {dam_id}: SKIP (no nhdplusid in flowline)")
        return dam_id, "no-ids"

    valid_ids = [fid for fid in ids if fid in nwm_ids]
    missing = sorted(set(ids) - set(valid_ids))
    if missing:
        _log(
            f"[{idx}/{total}] Dam {dam_id}: WARN — {len(missing)} of {len(ids)} "
            f"reach ids missing from NWM (e.g. {missing[:3]})"
        )
    if not valid_ids:
        _log(f"[{idx}/{total}] Dam {dam_id}: SKIP (no reach ids matched NWM)")
        return dam_id, "no-match"

    try:
        sub = ds[_FLOW_VAR].sel({_FEATURE_DIM: valid_ids}).load()
    except Exception as e:
        _log(f"[{idx}/{total}] Dam {dam_id}: FAIL S3 load ({e})")
        return dam_id, f"load-error:{e}"

    rows = []
    for fid in valid_ids:
        da = sub.sel({_FEATURE_DIM: fid})
        q_ep_50, rp100 = _per_id_stats(da)
        rows.append({"comid": int(fid), "q_ep_50": q_ep_50, "rp100": rp100})

    df = pd.DataFrame(rows, columns=["comid", "q_ep_50", "rp100"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    _log(f"[{idx}/{total}] Dam {dam_id}: ok ({len(rows)} reaches → {out_path.name})")
    return dam_id, "ok"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _resolve_gpkg(flowline_dir: Path, dam_id: int) -> Path | None:
    site_dir = flowline_dir / str(dam_id)
    gpkg_files = list(site_dir.glob("nhd_flowline_*.gpkg"))
    return gpkg_files[0] if gpkg_files else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--staging-dir", required=True, type=Path)
    parser.add_argument("--nwm-url", default=_DEFAULT_NWM_URL,
                        help=f"Zarr URL (S3 or local) [default: {_DEFAULT_NWM_URL}]")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N dams")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel workers [default: 4]")
    parser.add_argument("--force", action="store_true",
                        help="Rebuild per-dam CSV even if cached on disk")
    args = parser.parse_args()

    staging_dir: Path = args.staging_dir
    flowline_dir = staging_dir / "STRM"
    out_dir = staging_dir / "FLOW"
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = staging_dir / "tile_manifest.json"
    if not manifest_path.exists():
        sys.exit(f"Manifest not found: {manifest_path}\nRun stage_nhd_dem.py first.")
    with open(manifest_path) as f:
        manifest = json.load(f)
    dam_ids: List[int] = [int(k) for k in manifest.get("dam_tiles", {}).keys()]
    if args.limit:
        dam_ids = dam_ids[: args.limit]

    # Filter to dams that actually have a staged flowline
    pairs: List[Tuple[int, Path]] = []
    missing_fl = 0
    for dam_id in dam_ids:
        gpkg = _resolve_gpkg(flowline_dir, dam_id)
        if gpkg is None:
            missing_fl += 1
            continue
        pairs.append((dam_id, gpkg))
    if missing_fl:
        print(f"Skipping {missing_fl} dams with no staged flowline gpkg.\n")
    total = len(pairs)

    print(f"Opening NWM retrospective Zarr at {args.nwm_url} ...")
    ds = _open_nwm_zarr(args.nwm_url)
    print(f"  vars: {list(ds.data_vars)}")
    print(f"  dims: {dict(ds.sizes)}")
    if _FLOW_VAR not in ds.data_vars:
        sys.exit(
            f"\nNWM dataset has no '{_FLOW_VAR}' variable. Available: "
            f"{list(ds.data_vars)}\nUse --nwm-url to point at a different Zarr."
        )
    if _FEATURE_DIM not in ds.dims:
        sys.exit(
            f"\nNWM dataset has no '{_FEATURE_DIM}' dimension. Dims: "
            f"{list(ds.dims)}\nUse --nwm-url to point at a different Zarr."
        )

    print("Loading NWM feature_id coordinate (one-time) ...")
    nwm_ids = set(int(x) for x in ds[_FEATURE_DIM].values.tolist())
    print(f"  {len(nwm_ids):,} reaches available\n")

    print(f"Building streamflow CSVs for {total} dams (workers={args.workers})\n")

    counts: Dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(
                _process_dam, i + 1, total, dam_id, gpkg,
                ds, nwm_ids, out_dir, args.force,
            ): dam_id
            for i, (dam_id, gpkg) in enumerate(pairs)
        }
        for fut in as_completed(futures):
            dam_id = futures[fut]
            try:
                _, status = fut.result()
            except Exception as e:
                status = f"worker-error:{e}"
                _log(f"  ! Dam {dam_id} worker error: {e}")
            counts[status] = counts.get(status, 0) + 1

    print("\nSummary:")
    for status in sorted(counts):
        print(f"  {status:<24} {counts[status]}")


if __name__ == "__main__":
    main()
