"""
HUC-batched NHD-WSP geometry pipeline (ARC-free replacement branch).

Same HUC batching, ledger, symlink-reuse, and file layout as rolling_pipeline.py.
Replaces the ARC steps with in-process DEM-profile sampling + energy balance.

For each HUC group in data/full_lhd_website.csv:
  1. Free-space gate — same as rolling_pipeline.py.
  2. Symlink existing DEM/STRM data from --existing-data-dir (reuses ARC-pipeline
     staging trees without copying).
  3. stage_nhd_dem  — fetch NHDPlus flowlines + query TNM tiles.
  4. build_trimmed_dems — warp raw tiles to per-dam trimmed DEMs.
  5. _run_wsp_batch — in-process ThreadPoolExecutor; per-dam WSP sampling +
     energy balance; writes WSP_RESULTS/{dam_id}/wsp_result.json.
  6. Write huc<level>_<KEY>/.READY_TO_ARCHIVE with counts + sizes.

Ledger lives at <local-staging-root>/wsp_ledger.json (separate from the ARC
pipeline's huc8_ledger.json so both can coexist in the same root).

Typical workflow
----------------
    python backend/run_wsp_pipeline.py \\
        --local-staging-root /data/lhd_wsp \\
        --existing-data-dir /data/lhd_staging \\
        --huc-level 6

    python backend/run_wsp_pipeline.py \\
        --local-staging-root /data/lhd_wsp --status

    python backend/run_wsp_pipeline.py \\
        --local-staging-root /data/lhd_wsp --aggregate

Other flags
-----------
    --huc-level {2,4,6,8}   batch granularity (default 6)
    --huc KEY               process only this group key
    --workers N             ThreadPoolExecutor workers (default 8)
    --min-free-gb N         free-space gate in GB (default 200)
    --existing-data-dir P   reuse DEM/STRM from this staging tree (repeatable)
    --reverse               walk HUC keys from highest down (lets two instances run in parallel)
    --status                print ledger and exit
    --aggregate             roll WSP_RESULTS into Dam_Height_WSP_Ft / Dam_Length_WSP_Ft on master CSV
    --locate DAM_ID         print HUC batch for a dam and exit
    --consolidate KEY       replace symlinks with real files for a bundle
    --mark-archived KEY     flip ledger status to archived
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

_BACKEND_ROOT = Path(__file__).resolve().parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from screening.wsp import (
    get_upstream_comid,
    pick_downstream_node,
    pick_upstream_node,
    sample_dem_along_flowlines,
)
from lhd_processor.hydraulics import solve_weir_geom
from stage_nhd_dem import load_vaa_df, run_stage as run_stage_nhd_dem

import geopandas as gpd
import pynhd as nhd

_REPO_ROOT     = _BACKEND_ROOT.parent
DEFAULT_CSV    = _REPO_ROOT / "data" / "full_lhd_website.csv"
DEFAULT_FDC    = _REPO_ROOT / "frontend" / "data" / "nwm_fdc.json"
_PARQUET_URL   = "lynker-spatial/tabular/riverml_channel_geometry_with_ahg.parquet"
_PARQUET_CACHE = _BACKEND_ROOT / "cache" / "riverml_channel_geometry_with_ahg.parquet"

_SKIP_STATUSES = {"ready_to_archive", "archived"}
_REUSE_SUBDIRS = ("DEM", "STRM")

EXCLUDED_REVIEW_STATUSES = {
    "Removed",
    "Confirmed not a LHD",
    "Appears to not be LHD",
}

_EP50_IDX = 7   # index of p50 in [0,2,5,10,20,25,30,50,75,90,95,99,100]
M_TO_FT   = 3.28084


# ---------------------------------------------------------------------------
# Utilities (mirror rolling_pipeline.py)
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def _huc_dirname(key: str) -> str:
    return f"huc{len(key)}_{key}"


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _load_ledger(path: Path) -> Dict[str, dict]:
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _save_ledger(path: Path, ledger: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2, sort_keys=True)
    tmp.replace(path)


def _run_step(name: str, cmd: List[str]) -> None:
    print(f"\n  → {name}")
    print(f"    $ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def _build_existing_index(
    existing_dirs: List[Path],
) -> Dict[tuple, Path]:
    """Scan existing staging trees and return {(sub, dam_id): path}.

    Handles both layouts:
      flat:        existing_dir/DEM/{dam_id}/
      HUC-bundled: existing_dir/huc6_140600/DEM/{dam_id}/   ← ARC pipeline output
    """
    index: Dict[tuple, Path] = {}
    for src_root in existing_dirs:
        roots_to_scan = [src_root] + list(src_root.glob("huc*_*"))
        for root in roots_to_scan:
            for sub in _REUSE_SUBDIRS:
                sub_dir = root / sub
                if not sub_dir.is_dir():
                    continue
                for entry in sub_dir.iterdir():
                    try:
                        dam_id = int(entry.name)
                    except ValueError:
                        continue
                    if entry.is_dir():
                        index.setdefault((sub, dam_id), entry)
    return index


def _link_existing_data(
    huc_dir: Path,
    dam_ids: List[int],
    existing_index: Dict[tuple, Path],
) -> Dict[str, Dict[str, str]]:
    links: Dict[str, Dict[str, str]] = {}
    n_total = 0
    for did in dam_ids:
        per_dam: Dict[str, str] = {}
        for sub in _REUSE_SUBDIRS:
            target = huc_dir / sub / str(did)
            if target.exists() or target.is_symlink():
                continue
            src = existing_index.get((sub, did))
            if src is None:
                continue
            try:
                if any(src.iterdir()):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(src.resolve())
                    per_dam[sub] = str(src.resolve())
                    n_total += 1
            except OSError:
                continue
        if per_dam:
            links[str(did)] = per_dam
    if n_total:
        print(f"  Reused {n_total} subdir(s) across {len(links)} dam(s) via symlinks.")
    return links


def _consolidate_huc(huc_dir: Path) -> Tuple[int, List[str]]:
    moved, dangling = 0, []
    for sub in _REUSE_SUBDIRS:
        sub_dir = huc_dir / sub
        if not sub_dir.is_dir():
            continue
        for entry in sub_dir.iterdir():
            if not entry.is_symlink():
                continue
            try:
                src = entry.resolve(strict=True)
            except FileNotFoundError:
                dangling.append(str(entry))
                continue
            entry.unlink()
            shutil.move(str(src), str(entry))
            moved += 1
    return moved, dangling


# ---------------------------------------------------------------------------
# Shared asset loading
# ---------------------------------------------------------------------------

def _load_fdc(fdc_path: Path) -> Dict[str, list]:
    if not fdc_path.exists():
        return {}
    with open(fdc_path) as f:
        raw = json.load(f)
    raw.pop("_meta", None)
    return raw


def _load_parquet(comids: set[int]) -> pd.DataFrame:
    if not _PARQUET_CACHE.exists():
        print("[wsp_pipeline] Downloading Lynker parquet (~188 MB, one-time) ...")
        import s3fs
        fs = s3fs.S3FileSystem(anon=True)
        _PARQUET_CACHE.parent.mkdir(parents=True, exist_ok=True)
        fs.get(_PARQUET_URL, str(_PARQUET_CACHE))
    return (
        pq.read_table(
            _PARQUET_CACHE,
            columns=["feature_id", "owp_tw_bf", "owp_y_bf", "y_coef", "y_exp"],
            filters=[("feature_id", "in", list(comids))],
        )
        .to_pandas()
        .set_index("feature_id")
    )


# ---------------------------------------------------------------------------
# Per-dam WSP estimation
# ---------------------------------------------------------------------------

def _ep50(comid: int, fdc: Dict[str, list]) -> Optional[float]:
    vals = fdc.get(str(comid)) or fdc.get(f"{comid}.0")
    if not isinstance(vals, list) or len(vals) <= _EP50_IDX:
        return None
    v = float(vals[_EP50_IDX])
    return v if np.isfinite(v) and v > 0 else None


def _get_geom(gdf: gpd.GeoDataFrame, comid: int):
    rows = gdf[gdf["_cid"] == comid]
    return rows.iloc[0].geometry if not rows.empty else None


def _process_dam(
    dam_id: int,
    lat: float,
    lon: float,
    comid: int,
    huc_dir: Path,
    vaa_df: pd.DataFrame,
    geom_df: pd.DataFrame,
    fdc: Dict[str, list],
    search_up: float = 50.0,
    search_dn: float = 500.0,
) -> dict:
    """Run WSP geometry estimate for one dam. Returns a result dict."""
    result_dir = huc_dir / "WSP_RESULTS" / str(dam_id)
    result_path = result_dir / "wsp_result.json"
    if result_path.exists():
        with open(result_path) as f:
            return json.load(f)

    # Flowline
    strm_dir = huc_dir / "STRM" / str(dam_id)
    gpkg_files = list(strm_dir.glob("nhd_flowline_*.gpkg")) if strm_dir.exists() else []
    if not gpkg_files:
        return {"dam_id": dam_id, "status": "no_flowline"}

    gdf = gpd.read_file(gpkg_files[0])
    gdf.columns = gdf.columns.str.lower()
    id_col = next((c for c in ("nhdplusid", "comid", "nhdplus_comid") if c in gdf.columns), None)
    if id_col is None:
        return {"dam_id": dam_id, "status": "no_comid_col"}
    gdf["_cid"] = pd.to_numeric(gdf[id_col], errors="coerce").astype("Int64")

    dam_geom = _get_geom(gdf, comid)
    if dam_geom is None:
        return {"dam_id": dam_id, "status": "comid_not_in_gpkg"}

    upstream_comid = get_upstream_comid(comid, vaa_df)
    up_geom = _get_geom(gdf, upstream_comid) if upstream_comid else None

    # DEM
    dem_path = huc_dir / "DEM" / str(dam_id) / f"dem_{dam_id}.tif"
    if not dem_path.exists():
        return {"dam_id": dam_id, "status": "no_dem"}

    # WSP profile
    try:
        profile = sample_dem_along_flowlines(lat, lon, dam_geom, up_geom, dem_path)
    except Exception as e:
        return {"dam_id": dam_id, "status": f"profile_error:{e}"}

    if profile["elev_m"].notna().sum() < 3:
        return {"dam_id": dam_id, "status": "insufficient_dem_coverage"}

    us_node = pick_upstream_node(profile, search_m=search_up)
    ds_node = pick_downstream_node(profile, search_m=search_dn)
    wse_us   = float(us_node["elev_m"])
    wse_ds   = float(ds_node["elev_m"])
    delta_wse = wse_us - wse_ds

    if delta_wse <= 0 or not np.isfinite(delta_wse):
        return {"dam_id": dam_id, "status": "negative_delta_wse"}

    # Crest length: upstream reach owp_tw_bf, fall back to dam reach owp_tw_bf
    tw_bf = None
    tw_source = None
    for cid, label in ((upstream_comid, "upstream"), (comid, "dam_reach")):
        if cid is not None and cid in geom_df.index:
            v = float(geom_df.loc[cid, "owp_tw_bf"])
            if np.isfinite(v) and v > 0:
                tw_bf = v
                tw_source = label
                break
    if tw_bf is None:
        return {"dam_id": dam_id, "status": "no_tw_bf"}

    # Q: ep50 → fallback
    Q = _ep50(comid, fdc)
    if Q is None:
        Q = 1.0

    # y_T: wse_ds - AHG depth(Q) at dam reach
    y_T = None
    if comid in geom_df.index:
        row = geom_df.loc[comid]
        y_coef = float(row["y_coef"])
        y_exp  = float(row["y_exp"])
        if np.isfinite(y_coef) and y_coef > 0 and np.isfinite(y_exp) and y_exp > 0:
            y_T = max(wse_ds - float(y_coef * Q ** y_exp), 0.01)
    if y_T is None:
        y_T = max(delta_wse * 0.1, 0.1)

    # Energy balance
    H, P = solve_weir_geom(Q, tw_bf, y_T, delta_wse)
    if P is None or P <= 0 or P == -9999:
        return {"dam_id": dam_id, "status": "no_solution",
                "Q_cms": Q, "tw_bf_m": tw_bf, "delta_wse_m": delta_wse, "y_T_m": y_T}

    out = {
        "dam_id":          dam_id,
        "comid":           comid,
        "upstream_comid":  upstream_comid,
        "status":          "ok",
        "P_height_m":      round(float(P), 4),
        "H_head_m":        round(float(H), 4),
        "crest_length_m":  round(float(tw_bf), 4),
        "delta_wse_m":     round(float(delta_wse), 4),
        "y_T_m":           round(float(y_T), 4),
        "Q_cms":           round(float(Q), 4),
        "us_x_m":          round(float(us_node["x_m"]), 2),
        "ds_x_m":          round(float(ds_node["x_m"]), 2),
        "tw_source":       tw_source,
        "method":          "nhd_wsp",
        "completed_at":    _now(),
    }
    result_dir.mkdir(parents=True, exist_ok=True)
    with open(result_path, "w") as f:
        json.dump(out, f, indent=2)
    return out


def _run_wsp_batch(
    huc_dir: Path,
    dams_subset: pd.DataFrame,
    vaa_df: pd.DataFrame,
    geom_df: pd.DataFrame,
    fdc: Dict[str, list],
    workers: int,
    search_up: float,
    search_dn: float,
) -> Tuple[List[int], List[int]]:
    """Run WSP for every dam in dams_subset. Returns (ok_ids, failed_ids)."""
    jobs = [
        (int(row["OBJECTID"]), float(row["Latitude"]), float(row["Longitude"]),
         int(float(row["Reach_ID"])))
        for _, row in dams_subset.iterrows()
        if pd.notna(row.get("Reach_ID"))
    ]
    no_comid = [int(row["OBJECTID"]) for _, row in dams_subset.iterrows()
                if pd.isna(row.get("Reach_ID"))]

    ok: List[int] = []
    failed: List[int] = list(no_comid)
    n = len(jobs)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _process_dam, dam_id, lat, lon, comid,
                huc_dir, vaa_df, geom_df, fdc, search_up, search_dn
            ): dam_id
            for dam_id, lat, lon, comid in jobs
        }
        for i, fut in enumerate(as_completed(futures), 1):
            dam_id = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                print(f"  [{i}/{n}] Dam {dam_id}: EXCEPTION — {e}")
                failed.append(dam_id)
                continue
            status = res.get("status", "?")
            if status == "ok":
                ok.append(dam_id)
                if i % 50 == 0 or i == n:
                    print(f"  [{i}/{n}] Dam {dam_id}: ok  "
                          f"P={res['P_height_m']:.2f} m  L={res['crest_length_m']:.1f} m")
            else:
                failed.append(dam_id)
                if status != "wsp_result.json already existed":
                    print(f"  [{i}/{n}] Dam {dam_id}: {status}")

    return ok, failed


def _tally_wsp(huc_dir: Path, dam_ids: List[int]) -> Tuple[List[int], List[int]]:
    results_dir = huc_dir / "WSP_RESULTS"
    ok, failed = [], []
    for did in dam_ids:
        p = results_dir / str(did) / "wsp_result.json"
        if p.exists():
            with open(p) as f:
                data = json.load(f)
            if data.get("status") == "ok":
                ok.append(did)
            else:
                failed.append(did)
        else:
            failed.append(did)
    return ok, failed


# ---------------------------------------------------------------------------
# HUC batch processor
# ---------------------------------------------------------------------------

def _process_huc(
    key: str,
    dams_subset: pd.DataFrame,
    local_root: Path,
    ledger: Dict[str, dict],
    ledger_path: Path,
    workers: int,
    existing_index: Dict[tuple, Path],
    vaa_df,
    geom_df: pd.DataFrame,
    fdc: Dict[str, list],
    search_up: float,
    search_dn: float,
) -> None:
    label = f"HUC{len(key)} {key}"
    huc_dir = local_root / _huc_dirname(key)
    huc_dir.mkdir(parents=True, exist_ok=True)
    dams_csv = huc_dir / "dams.csv"
    dams_subset.drop(columns=["_GROUP"], errors="ignore").to_csv(dams_csv, index=False)
    dam_ids = [int(x) for x in dams_subset["OBJECTID"].tolist()]

    entry = ledger.setdefault(key, {})
    entry.update({"huc_level": len(key), "dam_ids": dam_ids,
                  "status": "staging", "staged_at": _now()})

    external_links = _link_existing_data(huc_dir, dam_ids, existing_index)
    if external_links:
        prev = entry.get("external_links", {})
        prev.update(external_links)
        entry["external_links"] = prev
    _save_ledger(ledger_path, ledger)

    py      = sys.executable
    backend = str(_BACKEND_ROOT)
    stage_args = ["--staging-dir", str(huc_dir), "--dams-csv", str(dams_csv),
                  "--workers", str(workers), "--download-workers", str(workers)]

    # stage_nhd_dem — in-process if vaa_df already loaded, else subprocess
    if vaa_df is not None:
        print(f"\n  → stage_nhd_dem (in-process, shared VAA table)")
        run_stage_nhd_dem(
            staging_dir=huc_dir, dams_csv=dams_csv,
            workers=workers, download_workers=workers, vaa_df=vaa_df,
        )
    else:
        _run_step("stage_nhd_dem",
                  [py, f"{backend}/stage_nhd_dem.py"] + stage_args)

    _run_step("build_trimmed_dems",
              [py, f"{backend}/build_trimmed_dems.py",
               "--staging-dir", str(huc_dir), "--workers", str(workers)])

    entry["status"] = "wsp_running"
    entry["wsp_at"] = _now()
    _save_ledger(ledger_path, ledger)

    print(f"\n  → run_wsp_batch ({len(dam_ids)} dams, {workers} workers)")
    ok, failed = _run_wsp_batch(
        huc_dir, dams_subset, vaa_df, geom_df, fdc,
        workers, search_up, search_dn,
    )

    size_bytes = _dir_size_bytes(huc_dir)
    status = "ready_to_archive" if not failed else "partial"
    marker = {
        "huc": key, "huc_level": len(key), "status": status,
        "dam_count": len(dam_ids), "ok": ok, "failed": failed,
        "size_bytes": size_bytes, "size_human": f"{size_bytes / 1e9:.1f} GB",
        "completed_at": _now(),
        "uses_external_data": bool(entry.get("external_links")),
        "external_paths": sorted({
            p for per in entry.get("external_links", {}).values()
            for p in per.values()
        }),
    }
    (huc_dir / ".READY_TO_ARCHIVE").write_text(json.dumps(marker, indent=2))
    entry.update({k: v for k, v in marker.items() if k != "external_paths"})
    _save_ledger(ledger_path, ledger)

    print(f"\n{label}: {status}  ({len(ok)} ok / {len(failed)} failed, {marker['size_human']})")


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------

def _cmd_run(args: argparse.Namespace) -> None:
    local_root: Path = args.local_staging_root
    local_root.mkdir(parents=True, exist_ok=True)
    ledger_path = local_root / "wsp_ledger.json"
    ledger = _load_ledger(ledger_path)

    existing_dirs = [Path(p).resolve() for p in (args.existing_data_dir or [])]
    for d in existing_dirs:
        if not d.is_dir():
            sys.exit(f"--existing-data-dir does not exist: {d}")

    if not args.dams_csv.exists():
        sys.exit(f"Dam CSV not found: {args.dams_csv}\nRun: python backend/assign_huc8.py")
    dams = pd.read_csv(args.dams_csv, low_memory=False)
    if "HUC8" not in dams.columns:
        sys.exit(f"{args.dams_csv} has no HUC8 column.\nRun: python backend/assign_huc8.py")

    dams = dams[
        dams["OBJECTID"].notna() & dams["Latitude"].notna() &
        dams["Longitude"].notna() & dams["HUC8"].notna()
    ].copy()
    dams["OBJECTID"] = dams["OBJECTID"].astype(int)
    dams["HUC8"] = dams["HUC8"].astype(str).str.split(".").str[0].str.zfill(8)
    dams = dams[dams["HUC8"] != "00000000"]

    if "Review_Status" in dams.columns:
        status_col = dams["Review_Status"].fillna("").astype(str).str.strip()
        excl = status_col.isin(EXCLUDED_REVIEW_STATUSES)
        n_excl = int(excl.sum())
        if n_excl:
            print(f"Excluding {n_excl} non-LHD dams")
            dams = dams[~excl]

    level = args.huc_level
    dams["_GROUP"] = dams["HUC8"].str[:level]
    groups = sorted(dams.groupby("_GROUP"), key=lambda kv: kv[0])

    if args.huc:
        groups = [(h, g) for h, g in groups if h == args.huc]
        if not groups:
            sys.exit(f"No dams for group '{args.huc}' at HUC level {level}")

    skip = {"archived"} if args.huc else _SKIP_STATUSES
    pending = [(h, g) for h, g in groups
               if ledger.get(h, {}).get("status") not in skip]

    if args.reverse:
        pending = list(reversed(pending))

    n_partial = sum(1 for h, _ in pending if ledger.get(h, {}).get("status") == "partial")
    print(f"HUC{level} batches: {len(groups)} total | "
          f"{len(groups) - len(pending)} done | "
          f"{len(pending)} pending ({n_partial} partial being retried)"
          + (" | reversed" if args.reverse else ""))

    if not pending:
        print("Nothing to do.")
        return

    # Build existing-data index once — handles both flat and HUC-bundled layouts
    existing_index: Dict[tuple, Path] = {}
    if existing_dirs:
        print(f"\nIndexing existing data from: {', '.join(str(d) for d in existing_dirs)} ...")
        existing_index = _build_existing_index(existing_dirs)
        n_dem  = sum(1 for sub, _ in existing_index if sub == "DEM")
        n_strm = sum(1 for sub, _ in existing_index if sub == "STRM")
        print(f"  Found {n_dem} DEM dirs, {n_strm} STRM dirs across existing trees.")

    # --- Load shared assets once ---
    print("\nLoading shared assets ...")

    vaa_df = None
    try:
        vaa_df = load_vaa_df()
    except Exception as e:
        print(f"  ! VAA preload failed: {e} — will reload per HUC (slower)")

    fdc = _load_fdc(DEFAULT_FDC)
    print(f"  FDC loaded: {len(fdc):,} reaches")

    # Collect all COMIDs (dam + upstream) for parquet preload
    all_comids: set[int] = set()
    for _, g in pending:
        for comid_raw in g["Reach_ID"].dropna():
            try:
                cid = int(float(comid_raw))
                all_comids.add(cid)
                if vaa_df is not None:
                    up = get_upstream_comid(cid, vaa_df)
                    if up:
                        all_comids.add(up)
            except (ValueError, TypeError):
                pass
    print(f"  Loading Lynker parquet for {len(all_comids):,} COMIDs ...")
    geom_df = _load_parquet(all_comids)
    print(f"  Parquet loaded: {len(geom_df):,} rows")

    # --- Main HUC loop ---
    for key, group in pending:
        free = _free_gb(local_root)
        if free < args.min_free_gb:
            print(f"\nFree space {free:.1f} GB < {args.min_free_gb} GB threshold — stopping.")
            print("Archive a ready bundle, then rerun.")
            return

        print(f"\n{'=' * 60}")
        print(f"HUC{level} {key}  ({len(group)} dams, {free:.1f} GB free)")
        print(f"{'=' * 60}")
        try:
            _process_huc(
                key, group, local_root, ledger, ledger_path,
                args.workers, existing_index, vaa_df, geom_df, fdc,
                args.search_up, args.search_dn,
            )
        except Exception as e:
            print(f"\nFATAL: HUC{level} {key} failed: {e}")
            traceback.print_exc()
            entry = ledger.setdefault(key, {})
            entry.update({"status": "errored", "error": str(e), "errored_at": _now()})
            _save_ledger(ledger_path, ledger)
            print("Halting. Investigate and rerun.")
            return

    print("\nAll pending HUCs processed.")
    try:
        _cmd_aggregate(args)
    except Exception as e:
        print(f"  WARN aggregate step failed: {e}")


def _cmd_status(args: argparse.Namespace) -> None:
    ledger_path = args.local_staging_root / "wsp_ledger.json"
    ledger = _load_ledger(ledger_path)
    if not ledger:
        print(f"No ledger at {ledger_path}")
        return
    counts: Dict[str, int] = {}
    rows = []
    for key, entry in sorted(ledger.items()):
        s = entry.get("status", "?")
        counts[s] = counts.get(s, 0) + 1
        rows.append((key, s,
                     entry.get("dam_count", len(entry.get("dam_ids", []))),
                     len(entry.get("failed", [])),
                     entry.get("size_human", "")))
    print(f"Ledger: {ledger_path}\n")
    print(f"{'HUC':<10} {'status':<18} {'dams':>5} {'failed':>7} {'size':>10}")
    print("-" * 55)
    for r in rows:
        print(f"{r[0]:<10} {r[1]:<18} {r[2]:>5} {r[3]:>7} {r[4]:>10}")
    print("\nTotals:")
    for s in sorted(counts):
        print(f"  {s:<22} {counts[s]}")


def _cmd_aggregate(args: argparse.Namespace) -> None:
    """Roll WSP_RESULTS into Dam_Height_WSP_Ft / Dam_Length_WSP_Ft on the master CSV."""
    dams_csv: Path = args.dams_csv
    if not dams_csv.exists():
        sys.exit(f"Dam CSV not found: {dams_csv}")

    search_roots: List[Path] = [args.local_staging_root.resolve()]
    for d in (args.existing_data_dir or []):
        p = Path(d).resolve()
        if p.is_dir():
            search_roots.append(p)

    print(f"Scanning {len(search_roots)} root(s) for wsp_result.json ...")
    seen: Dict[int, Path] = {}
    for root in search_roots:
        for p in root.rglob("wsp_result.json"):
            try:
                dam_id = int(p.parent.name)
            except ValueError:
                continue
            seen.setdefault(dam_id, p)
    print(f"  → {len(seen)} dam results found")
    if not seen:
        print("Nothing to aggregate.")
        return

    rows: Dict[int, Dict] = {}
    for dam_id, p in seen.items():
        try:
            with open(p) as f:
                r = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if r.get("status") != "ok":
            continue
        rows[dam_id] = {
            "Dam_Height_WSP_Ft": round(float(r["P_height_m"]) * M_TO_FT, 2),
            "Dam_Length_WSP_Ft": round(float(r["crest_length_m"]) * M_TO_FT, 2),
            "WSP_Q_cms":         r["Q_cms"],
            "WSP_delta_wse_m":   r["delta_wse_m"],
            "WSP_upstream_comid": r.get("upstream_comid"),
        }
    print(f"  {len(rows)} dams with ok results")

    dams = pd.read_csv(dams_csv, low_memory=False)
    for col in ("Dam_Height_WSP_Ft", "Dam_Length_WSP_Ft",
                "WSP_Q_cms", "WSP_delta_wse_m", "WSP_upstream_comid"):
        if col in dams.columns:
            dams = dams.drop(columns=[col])

    add = pd.DataFrame.from_dict(rows, orient="index")
    add.index.name = "OBJECTID"
    add = add.reset_index()
    merged = dams.merge(add, on="OBJECTID", how="left")
    merged.to_csv(dams_csv, index=False)
    print(f"Wrote Dam_Height_WSP_Ft / Dam_Length_WSP_Ft (+ 3 diagnostic cols) to {dams_csv}")


def _cmd_locate(args: argparse.Namespace) -> None:
    target = int(args.locate)
    dams_csv: Path = args.dams_csv
    if not dams_csv.exists():
        sys.exit(f"Dam CSV not found: {dams_csv}")
    dams = pd.read_csv(dams_csv, low_memory=False)
    if "HUC8" not in dams.columns:
        sys.exit("No HUC8 column — run assign_huc8.py first")
    row = dams[dams["OBJECTID"] == target]
    if row.empty:
        sys.exit(f"OBJECTID {target} not found")
    huc8 = str(row.iloc[0]["HUC8"]).split(".")[0].zfill(8)
    key = huc8[:args.huc_level]
    print(f"Dam {target} → HUC8 {huc8}  (HUC{args.huc_level} batch {key}, "
          f"folder {_huc_dirname(key)})")
    ledger = _load_ledger(args.local_staging_root / "wsp_ledger.json")
    entry = ledger.get(key, {})
    if entry:
        print(f"  ledger status: {entry.get('status', '?')}")


def _cmd_consolidate(args: argparse.Namespace) -> None:
    local_root: Path = args.local_staging_root
    ledger_path = local_root / "wsp_ledger.json"
    ledger = _load_ledger(ledger_path)
    key = args.consolidate
    huc_dir = local_root / _huc_dirname(key)
    if not huc_dir.is_dir():
        sys.exit(f"HUC dir not found: {huc_dir}")
    print(f"Consolidating {huc_dir} ...")
    moved, dangling = _consolidate_huc(huc_dir)
    print(f"  Moved {moved} symlinked dir(s).")
    if dangling:
        print(f"  {len(dangling)} dangling symlinks skipped.")
    entry = ledger.setdefault(key, {})
    entry.update({"consolidated": True, "consolidated_at": _now(),
                  "consolidated_moves": moved})
    entry.pop("external_links", None)
    _save_ledger(ledger_path, ledger)
    print(f"Done. Safe to move {huc_dir}.")


def _cmd_mark_archived(args: argparse.Namespace) -> None:
    ledger_path = args.local_staging_root / "wsp_ledger.json"
    ledger = _load_ledger(ledger_path)
    key = args.mark_archived
    if key not in ledger:
        sys.exit(f"'{key}' not in ledger")
    ledger[key].update({"status": "archived", "archived_at": _now()})
    _save_ledger(ledger_path, ledger)
    print(f"HUC group {key} marked archived.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--local-staging-root", type=Path, required=True)
    parser.add_argument("--dams-csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--huc-level", type=int, choices=[2, 4, 6, 8], default=6)
    parser.add_argument("--min-free-gb", type=float, default=200.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--existing-data-dir", type=Path, action="append", default=None)
    parser.add_argument("--huc", type=str, default=None, metavar="KEY")
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--search-up", type=float, default=50.0,
                        help="Upstream flat-zone search window (m) [50]")
    parser.add_argument("--search-dn", type=float, default=500.0,
                        help="Downstream slope-match search window (m) [500]")
    parser.add_argument("--status",       action="store_true")
    parser.add_argument("--aggregate",    action="store_true")
    parser.add_argument("--locate",       type=str, default=None, metavar="DAM_ID")
    parser.add_argument("--consolidate",  type=str, default=None, metavar="KEY")
    parser.add_argument("--mark-archived",type=str, default=None, metavar="KEY")
    args = parser.parse_args()

    if args.status:
        _cmd_status(args)
    elif args.locate:
        _cmd_locate(args)
    elif args.consolidate:
        _cmd_consolidate(args)
    elif args.mark_archived:
        _cmd_mark_archived(args)
    elif args.aggregate:
        _cmd_aggregate(args)
    else:
        _cmd_run(args)


if __name__ == "__main__":
    main()
