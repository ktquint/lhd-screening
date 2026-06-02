"""
Build per-dam NWM-derived streamflow CSVs (q_ep_50 + rp100) for ARC.
"""
from __future__ import annotations

import argparse
import calendar
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

import fsspec
import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr
from scipy.stats import pearson3

_DEFAULT_NWM_URL = "s3://noaa-nwm-retrospective-3-0-pds/CONUS/zarr/chrtout.zarr"
_FEATURE_DIM = "feature_id"
_FLOW_VAR = "streamflow"
_RP_TARGET_YEARS = 100


def open_nwm_zarr(url: str) -> xr.Dataset:
    """Helper to open the Zarr connection cleanly."""
    scheme, _, rest = url.partition("://")
    if not scheme:
        return xr.open_zarr(url, consolidated=True)
    fs = fsspec.filesystem(scheme, anon=True)
    mapper = fs.get_mapper(rest)
    return xr.open_zarr(mapper, consolidated=True)


def _log_pearson_iii_rp(annual_max: np.ndarray, T: int) -> float:
    annual_max = annual_max[np.isfinite(annual_max)]
    annual_max = annual_max[annual_max > 0]
    if len(annual_max) < 10:
        return float("nan")
    y = np.log10(annual_max)
    y_mean = float(np.mean(y))
    y_std = float(np.std(y, ddof=1))
    if y_std == 0.0:
        return float("nan")
    y_skew = float(pd.Series(y).skew())
    try:
        K_T = float(pearson3.ppf(1.0 - 1.0 / T, y_skew))
    except Exception:
        return float("nan")
    return float(10.0 ** (y_mean + K_T * y_std))


def _resolve_gpkg(flowline_dir: Path, dam_id: int) -> Path | None:
    site_dir = flowline_dir / str(dam_id)
    gpkgs = list(site_dir.glob("nhd_flowline_*.gpkg"))
    return gpkgs[0] if gpkgs else None


def _flowline_ids(gpkg_path: Path) -> List[int]:
    gdf = gpd.read_file(gpkg_path)
    if "nhdplusid" not in gdf.columns:
        return []
    ids = pd.to_numeric(gdf["nhdplusid"], errors="coerce").dropna().astype(np.int64)
    return ids.unique().tolist()


def process_streamflow_with_ds(
    ds: xr.Dataset,
    staging_dir: Path,
    force: bool = False,
    limit: int | None = None,
    start_year_opt: int | None = None,
    end_year_opt: int | None = None,
    nwm_ids: set[int] | None = None,
) -> None:
    """
    Core calculation logic that accepts an already open xarray Dataset,
    saving substantial overhead when run inside loops.

    ``nwm_ids`` is the set of every feature_id present in ``ds``. Building
    it materializes ~2.7M ids into a Python set (slow), so when called
    inside a loop the caller can build it once and reuse it across calls.
    Defaults to building from ``ds`` if not supplied.
    """
    flowline_dir = staging_dir / "STRM"
    out_dir = staging_dir / "FLOW"
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = staging_dir / "tile_manifest.json"
    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}\nRun stage_nhd_dem.py first.")
        return
    with open(manifest_path) as f:
        manifest = json.load(f)
    dam_ids: List[int] = [int(k) for k in manifest.get("dam_tiles", {}).keys()]
    if limit:
        dam_ids = dam_ids[:limit]

    dam_features: Dict[int, List[int]] = {}
    missing_fl = 0
    cached = 0
    for dam_id in dam_ids:
        out_path = out_dir / str(dam_id) / f"flow_{dam_id}.csv"
        if not force and out_path.exists():
            cached += 1
            continue
        gpkg = _resolve_gpkg(flowline_dir, dam_id)
        if gpkg is None:
            missing_fl += 1
            continue
        try:
            ids = _flowline_ids(gpkg)
        except Exception as e:
            print(f"  ! Dam {dam_id}: failed to read flowline ({e})")
            continue
        if ids:
            dam_features[dam_id] = ids

    print(f"Dams: {len(dam_ids)} in manifest")
    if cached:
        print(f"  {cached} already have a CSV (use force=True to rebuild)")
    if missing_fl:
        print(f"  {missing_fl} skipped (no staged flowline gpkg)")
    print(f"  {len(dam_features)} dams to process\n")
    if not dam_features:
        print("Nothing to do for this batch.")
        return

    if nwm_ids is None:
        print("Filtering feature_ids against the active NWM coordinate context ...")
        t0 = time.time()
        nwm_ids = set(int(x) for x in ds[_FEATURE_DIM].values.tolist())
        print(f"  {len(nwm_ids):,} reaches available ({time.time()-t0:.1f}s)\n")
    else:
        print(f"Using preloaded NWM feature_id set ({len(nwm_ids):,} reaches).\n")

    for dam_id in list(dam_features.keys()):
        valid = [fid for fid in dam_features[dam_id] if fid in nwm_ids]
        if not valid:
            print(f"  ! Dam {dam_id}: no reach ids matched NWM — skipping")
            dam_features.pop(dam_id)
        else:
            dam_features[dam_id] = valid

    needed_ids = sorted({fid for fids in dam_features.values() for fid in fids})
    print(f"Unique reaches across remaining dams: {len(needed_ids):,}")

    time_min = pd.Timestamp(ds.time.min().values)
    time_max = pd.Timestamp(ds.time.max().values)
    ds_start_year = time_min.year if (time_min.month == 1 and time_min.day == 1) else time_min.year + 1
    ds_end_year = time_max.year if (time_max.month == 12 and time_max.day >= 30) else time_max.year - 1
    start_year = max(ds_start_year, start_year_opt or ds_start_year)
    end_year = min(ds_end_year, end_year_opt or ds_end_year)
    years = list(range(start_year, end_year + 1))
    print(f"NWM time range: {time_min.date()} → {time_max.date()}; "
          f"using complete years {start_year}–{end_year} ({len(years)} years)\n")

    n_features = len(needed_ids)
    feature_idx = {fid: i for i, fid in enumerate(needed_ids)}
    total_days = sum(366 if calendar.isleap(y) else 365 for y in years)
    daily_full = np.full((total_days, n_features), np.nan, dtype=np.float32)
    annual_max_full = np.full((len(years), n_features), np.nan, dtype=np.float32)

    day_offset = 0
    overall_t0 = time.time()
    for yi, y in enumerate(years):
        t_y = time.time()
        print(f"[{yi+1}/{len(years)}] year {y}: loading ...", end=" ", flush=True)
        sub = ds[_FLOW_VAR].sel(
            time=slice(f"{y}-01-01", f"{y}-12-31T23:59:59"),
            **{_FEATURE_DIM: needed_ids},
        )
        sub_loaded = sub.load()
        t_loaded = time.time() - t_y
        
        if sub_loaded.dims[0] != "time":
            sub_loaded = sub_loaded.transpose("time", _FEATURE_DIM)

        annual_max_full[yi] = sub_loaded.max(dim="time").values
        daily_y = sub_loaded.resample(time="1D").mean().values  
        n_days = daily_y.shape[0]
        daily_full[day_offset:day_offset + n_days] = daily_y
        day_offset += n_days

        t_total = time.time() - t_y
        print(f"loaded in {t_loaded:.1f}s, processed in {t_total:.1f}s total")

    if day_offset != total_days:
        daily_full = daily_full[:day_offset]

    print(f"\nAll years loaded in {time.time()-overall_t0:.1f}s.")
    print("Computing q_ep_50 (median) and rp100 (Log-Pearson III) per reach ...")

    q_ep_50 = np.nanmedian(daily_full, axis=0)  
    rp100 = np.empty(n_features, dtype=np.float64)
    for i in range(n_features):
        rp100[i] = _log_pearson_iii_rp(annual_max_full[:, i], _RP_TARGET_YEARS)

    print(f"Writing {len(dam_features)} per-dam CSVs ...")
    written = 0
    for dam_id, fids in dam_features.items():
        rows = []
        for fid in fids:
            i = feature_idx[fid]
            rows.append({
                "comid": int(fid),
                "q_ep_50": float(q_ep_50[i]),
                "rp100": float(rp100[i]),
            })
        df = pd.DataFrame(rows, columns=["comid", "q_ep_50", "rp100"])
        out_path = out_dir / str(dam_id) / f"flow_{dam_id}.csv"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_path, index=False)
        written += 1
    print(f"\nDone: {written} CSVs written, {cached} already cached.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--staging-dir", required=True, type=Path)
    parser.add_argument("--nwm-url", default=_DEFAULT_NWM_URL,
                        help=f"Zarr URL (S3 or local) [default: {_DEFAULT_NWM_URL}]")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--start-year", type=int, default=None)
    parser.add_argument("--end-year", type=int, default=None)
    args = parser.parse_args()

    print(f"Opening NWM retrospective Zarr at {args.nwm_url} ...")
    ds = open_nwm_zarr(args.nwm_url)
    
    if _FLOW_VAR not in ds.data_vars or _FEATURE_DIM not in ds.dims:
        sys.exit(f"\nExpected variable '{_FLOW_VAR}' and dim '{_FEATURE_DIM}'.")

    process_streamflow_with_ds(
        ds=ds,
        staging_dir=args.staging_dir,
        force=args.force,
        limit=args.limit,
        start_year_opt=args.start_year,
        end_year_opt=args.end_year
    )

if __name__ == "__main__":
    main()
    