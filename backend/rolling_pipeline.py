"""
HUC-batched LHD screening pipeline.

Dams are grouped by HUC code at a configurable level (--huc-level, default 6).
HUC codes nest by prefix, so HUC6 = HUC8[:6], HUC4 = HUC8[:4]. Lower levels =
fewer, larger batches. Bundle folders are named huc<level>_<KEY>/ (e.g.
huc6_140600/).

For each HUC group in data/full_lhd_website.csv:
  1. Free-space gate — if free disk on --local-staging-root is below
     --min-free-gb the loop stops cleanly so you can move a completed
     huc<level>_<KEY>/ directory off to an external drive, then rerun.
  2. (Optional) Symlink existing per-dam staging from --existing-data-dir
     paths into the new bundle. Reuses prior DEM/STRM/LAND/FLOW/RESULTS
     dirs without copying. The external sources used are recorded in the
     ledger and in .READY_TO_ARCHIVE so you can locate them later.
  3. Write a per-batch dams subset CSV to <local>/huc<level>_<KEY>/dams.csv.
  4. Subprocess-chain the 6 pipeline steps targeting that bundle:
        stage_nhd_dem → build_trimmed_dems → build_stream_rasters
        → build_streamflow_csv → run_arc_batch → run_analysis_batch
  5. Tally ok/failed dams by checking RESULTS/{dam_id}/arc_done.json and
     RESULTS/{dam_id}/analysis_summary.json. Failures do NOT abort the loop;
     a batch with failures is marked "partial" and retried on the next run.
  6. Write <local>/huc<level>_<KEY>/.READY_TO_ARCHIVE with counts, size, and
     the external paths that need to come along when archiving.

Typical workflow (HUC6 batches)
-------------------------------
    # one-time
    python backend/assign_huc8.py

    # main loop (existing scattered data passed via --existing-data-dir)
    python backend/rolling_pipeline.py \\
        --local-staging-root /Users/me/staging \\
        --existing-data-dir /old/lhd_staging \\
        --huc-level 6 --min-free-gb 600

    # before archiving a batch, collapse symlinks into real files
    # (also prunes DEM/raw_3dep/ by default — tile_manifest.json keeps the
    # dam → tile + URL mapping so the tiles can be re-fetched if needed):
    python backend/rolling_pipeline.py \\
        --local-staging-root /Users/me/staging --consolidate 140600

    # then move and mark archived
    mv /Users/me/staging/huc6_140600 /Volumes/MyDrive/
    python backend/rolling_pipeline.py \\
        --local-staging-root /Users/me/staging --mark-archived 140600

Other commands
--------------
    --huc-level {2,4,6,8}  batch granularity (default 6)
    --huc8 <KEY>           process only this group key (at the active level)
    --status               print the ledger and exit
    --locate <dam_id>      print which HUC8 + batch a dam belongs to
    --archive-to <PATH>    batch-archive every ready_to_archive bundle:
                           consolidate + mv to PATH + mark archived
    --keep-raw-tiles       skip the raw_3dep prune that --consolidate /
                           --archive-to do by default
    --aggregate            roll per-dam analysis_summary.json into
                           Qmin_env/Qmax_env + Qmin_stable/Qmax_stable
                           + Dam_Height_GIS_Ft + Dam_Length_GIS_Ft
                           + Rp100_cms + Tailwater_a + Tailwater_b
                           columns on --dams-csv (drops legacy Qmin/Qmax)
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import xarray as xr

_BACKEND_ROOT = Path(__file__).resolve().parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from build_streamflow_csv import (  # noqa: E402  (sys.path tweak above)
    _DEFAULT_NWM_URL,
    _FEATURE_DIM,
    open_nwm_zarr,
    process_streamflow_with_ds,
)
from stage_nhd_dem import load_vaa_df, run_stage as run_stage_nhd_dem  # noqa: E402

_REPO_ROOT = _BACKEND_ROOT.parent
DEFAULT_DAMS_CSV = _REPO_ROOT / "data" / "full_lhd_website.csv"

# A HUC is skipped on rerun only when it is fully done. "partial" is NOT here
# on purpose: those HUCs get re-attempted so transient upstream failures (e.g. a
# TNM outage) are retried — per-dam caches make re-runs cheap. Accept a partial
# HUC by consolidating + --mark-archived, which flips it to terminal "archived".
_SKIP_STATUSES = {"ready_to_archive", "archived"}

# Per-dam subdirectories worth reusing from existing staging trees
_REUSE_SUBDIRS = ("DEM", "STRM", "LAND", "FLOW", "RESULTS")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def _huc_dirname(key: str) -> str:
    """Bundle folder name for a HUC group key, e.g. '140600' -> 'huc6_140600'."""
    return f"huc{len(key)}_{key}"


def _dir_size_bytes(path: Path) -> int:
    """Recursive size in bytes. Follows symlinks (counts target file size)."""
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass  # broken symlink or transient error
    return total


def _load_ledger(ledger_path: Path) -> Dict[str, dict]:
    if not ledger_path.exists():
        return {}
    with open(ledger_path) as f:
        return json.load(f)


def _save_ledger(ledger_path: Path, ledger: Dict[str, dict]) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = ledger_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2, sort_keys=True)
    tmp.replace(ledger_path)


def _run_step(name: str, cmd: List[str]) -> None:
    print(f"\n  → {name}")
    print(f"    $ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def _tally_results(huc_dir: Path, dam_ids: List[int]) -> Tuple[List[int], List[int]]:
    results_dir = huc_dir / "RESULTS"
    ok: List[int] = []
    failed: List[int] = []
    for did in dam_ids:
        arc_marker = results_dir / str(did) / "arc_done.json"
        ana_marker = results_dir / str(did) / "analysis_summary.json"
        if arc_marker.exists() and ana_marker.exists():
            ok.append(did)
        else:
            failed.append(did)
    return ok, failed


def _link_existing_data(
    huc_dir: Path,
    dam_ids: List[int],
    existing_dirs: List[Path],
) -> Dict[str, Dict[str, str]]:
    """
    Symlink per-dam subdirs from existing_dirs into huc_dir.

    For each dam, for each subdir in _REUSE_SUBDIRS, if the target slot
    in huc_dir is empty and some existing_dir has a non-empty matching
    subdir, create a symlink (first hit wins).

    Returns: { "<dam_id>": { "<sub>": "<absolute source path>" } }
    """
    if not existing_dirs:
        return {}
    links: Dict[str, Dict[str, str]] = {}
    n_total = 0
    for did in dam_ids:
        per_dam: Dict[str, str] = {}
        for sub in _REUSE_SUBDIRS:
            target = huc_dir / sub / str(did)
            if target.exists() or target.is_symlink():
                continue  # already linked / created from a prior orchestrator run
            for src_root in existing_dirs:
                candidate = src_root / sub / str(did)
                try:
                    if candidate.is_dir() and any(candidate.iterdir()):
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.symlink_to(candidate.resolve())
                        per_dam[sub] = str(candidate.resolve())
                        n_total += 1
                        break
                except OSError:
                    continue
        if per_dam:
            links[str(did)] = per_dam
    if n_total:
        n_dams = len(links)
        print(f"  Reused {n_total} subdir(s) across {n_dams} dam(s) via symlinks "
              f"from {len(existing_dirs)} existing source(s).")
    return links


def _consolidate_huc(huc_dir: Path) -> Tuple[int, List[str]]:
    """
    Replace every top-level per-dam symlink under huc_dir/<sub>/<dam_id>/
    with the actual contents via shutil.move.

    Returns: (count_moved, list_of_dangling_links_skipped)
    """
    moved = 0
    dangling: List[str] = []
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


def _prune_raw_dem_tiles(huc_dir: Path) -> Tuple[int, int]:
    """
    Delete huc_dir/DEM/raw_3dep/ (the downloaded 3DEP tiles) and stamp the
    deletion into tile_manifest.json so the dam→tile mapping + URLs remain
    on record. Returns (count_deleted, bytes_freed).

    Idempotent — does nothing if raw_3dep/ is already gone.
    """
    raw_dir = huc_dir / "DEM" / "raw_3dep"
    if not raw_dir.exists():
        return 0, 0

    count = 0
    total_bytes = 0
    for p in raw_dir.rglob("*"):
        if p.is_file() and not p.is_symlink():
            try:
                total_bytes += p.stat().st_size
            except OSError:
                pass
            count += 1
    shutil.rmtree(raw_dir)

    manifest_path = huc_dir / "tile_manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
            manifest["raw_tiles_deleted_at"] = _now()
            manifest["raw_tiles_deleted_count"] = count
            manifest["raw_tiles_deleted_bytes"] = total_bytes
            manifest_path.write_text(json.dumps(manifest, indent=2))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  ! could not stamp tile_manifest.json: {e}")

    return count, total_bytes


def _process_huc8(
    key: str,
    dams_subset: pd.DataFrame,
    local_root: Path,
    ledger: Dict[str, dict],
    ledger_path: Path,
    workers: int,
    existing_dirs: List[Path],
    nwm_dataset: xr.Dataset | None = None,
    nwm_ids: set[int] | None = None,
    vaa_df=None,
    keep_raw_tiles: bool = False,
    retry_failed: bool = False,
    arc_workers: int | None = None,
) -> None:
    label = f"HUC{len(key)} {key}"
    huc_dir = local_root / _huc_dirname(key)
    huc_dir.mkdir(parents=True, exist_ok=True)
    dams_csv = huc_dir / "dams.csv"
    dams_subset.drop(columns=["_GROUP"], errors="ignore").to_csv(dams_csv, index=False)
    dam_ids = [int(x) for x in dams_subset["OBJECTID"].tolist()]

    # --retry-failed: drop this bundle's failure registry so build_trimmed_dems
    # re-attempts every dam and stage_nhd_dem re-downloads every tile they need.
    if retry_failed:
        failures_path = huc_dir / "dam_failures.json"
        if failures_path.exists():
            failures_path.unlink()
            print(f"  → cleared {failures_path.name} (--retry-failed)")

    entry = ledger.setdefault(key, {})
    entry["huc_level"] = len(key)
    entry["dam_ids"] = dam_ids
    entry["status"] = "staging"
    entry["staged_at"] = _now()

    external_links = _link_existing_data(huc_dir, dam_ids, existing_dirs)
    if external_links:
        # Merge with any links recorded on a prior partial run.
        prev = entry.get("external_links", {})
        prev.update(external_links)
        entry["external_links"] = prev
    _save_ledger(ledger_path, ledger)

    workers_s = str(workers)
    py = sys.executable
    backend = str(_BACKEND_ROOT)
    staging_arg = ["--staging-dir", str(huc_dir)]
    dams_arg = ["--dams-csv", str(dams_csv)]
    common_workers = ["--workers", workers_s]

    if vaa_df is not None:
        print(f"\n  → stage_nhd_dem (in-process, shared VAA table)")
        run_stage_nhd_dem(
            staging_dir=huc_dir,
            dams_csv=dams_csv,
            workers=workers,
            download_workers=workers,
            vaa_df=vaa_df,
        )
    else:
        _run_step("stage_nhd_dem", [
            py, f"{backend}/stage_nhd_dem.py",
            *staging_arg, *dams_arg, *common_workers,
            "--download-workers", workers_s,
        ])
    _run_step("build_trimmed_dems", [
        py, f"{backend}/build_trimmed_dems.py",
        *staging_arg, *common_workers,
    ])
    _run_step("build_stream_rasters", [
        py, f"{backend}/build_stream_rasters.py",
        *staging_arg, *common_workers,
    ])

    print(f"\n  → build_streamflow_csv (in-memory shared zarr connection)")
    if nwm_dataset is not None:
        # Reuse the persistent opened dataset (and its feature_id set) from
        # the main runner loop — saves ~2.7M id materialization per HUC.
        process_streamflow_with_ds(
            ds=nwm_dataset, staging_dir=huc_dir, nwm_ids=nwm_ids,
        )
    else:
        # Fallback to subprocess if run stand-alone or context isn't passed
        _run_step("build_streamflow_csv", [
            py, f"{backend}/build_streamflow_csv.py",
            *staging_arg,
        ])

    entry["status"] = "arc_running"
    entry["arc_at"] = _now()
    _save_ledger(ledger_path, ledger)

    arc_workers_arg = ["--workers", str(arc_workers if arc_workers is not None else workers)]
    _run_step("run_arc_batch", [
        py, f"{backend}/run_arc_batch.py",
        *staging_arg, *dams_arg, *arc_workers_arg,
    ])
    _run_step("run_analysis_batch", [
        py, f"{backend}/run_analysis_batch.py",
        *staging_arg, *common_workers,
    ])

    ok, failed = _tally_results(huc_dir, dam_ids)

    if not keep_raw_tiles:
        pruned, freed = _prune_raw_dem_tiles(huc_dir)
        if pruned:
            print(f"\n  Auto-pruned {pruned} raw 3DEP tile(s); "
                  f"freed {freed / 1e9:.1f} GB. "
                  f"Record retained in tile_manifest.json.")

    size_bytes = _dir_size_bytes(huc_dir)
    status = "ready_to_archive" if not failed else "partial"

    # Flatten the external_links dict to a sorted unique list of paths the user
    # needs to move (or consolidate) alongside the HUC bundle.
    external_paths = sorted({
        p for per_dam in entry.get("external_links", {}).values()
        for p in per_dam.values()
    })

    marker = {
        "huc": key,
        "huc_level": len(key),
        "status": status,
        "dam_count": len(dam_ids),
        "ok": ok,
        "failed": failed,
        "size_bytes": size_bytes,
        "size_human": f"{size_bytes / 1e9:.1f} GB",
        "completed_at": _now(),
        "uses_external_data": bool(external_paths),
        "external_paths": external_paths,
    }
    (huc_dir / ".READY_TO_ARCHIVE").write_text(json.dumps(marker, indent=2))

    entry.update({k: v for k, v in marker.items()
                  if k not in ("external_paths",)})  # external_links already in entry
    _save_ledger(ledger_path, ledger)

    print(
        f"\n{label}: {status}  "
        f"({len(ok)} ok / {len(failed)} failed, {marker['size_human']})"
    )
    if external_paths:
        print(f"  Uses {len(external_paths)} external symlink source(s). "
              f"Run --consolidate {key} before moving the bundle.")


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------

def _cmd_run(args: argparse.Namespace) -> None:
    local_root: Path = args.local_staging_root
    local_root.mkdir(parents=True, exist_ok=True)
    ledger_path = local_root / "huc8_ledger.json"
    ledger = _load_ledger(ledger_path)

    existing_dirs = [Path(p).resolve() for p in (args.existing_data_dir or [])]
    for d in existing_dirs:
        if not d.is_dir():
            sys.exit(f"--existing-data-dir does not exist: {d}")

    if not args.dams_csv.exists():
        sys.exit(
            f"Dam CSV not found: {args.dams_csv}\n"
            f"Run: python backend/assign_huc8.py"
        )
    dams = pd.read_csv(args.dams_csv, low_memory=False)
    if "HUC8" not in dams.columns:
        sys.exit(
            f"{args.dams_csv} has no HUC8 column.\n"
            f"Run: python backend/assign_huc8.py"
        )
    dams = dams[
        dams["OBJECTID"].notna()
        & dams["Latitude"].notna()
        & dams["Longitude"].notna()
        & dams["HUC8"].notna()
    ].copy()
    dams["OBJECTID"] = dams["OBJECTID"].astype(int)
    dams["HUC8"] = dams["HUC8"].astype(str).str.split(".").str[0].str.zfill(8)
    dams = dams[dams["HUC8"] != "00000000"]

    # Drop dams the LHDI review has flagged as non-LHD or removed so the
    # pipeline doesn't re-stage work that audit_non_lhd_outputs.py would
    # immediately clean back up. Matches the set used by the audit script
    # and the frontend's render filter.
    EXCLUDED_REVIEW_STATUSES = {
        "Removed",
        "Confirmed not a LHD",
        "Appears to not be LHD",
    }
    if "Review_Status" in dams.columns:
        status = dams["Review_Status"].fillna("").astype(str).str.strip()
        excluded_mask = status.isin(EXCLUDED_REVIEW_STATUSES)
        n_excluded = int(excluded_mask.sum())
        if n_excluded:
            print(f"Excluding {n_excluded} dams with non-LHD Review_Status "
                  f"({', '.join(sorted(EXCLUDED_REVIEW_STATUSES))})")
            dams = dams[~excluded_mask]

    # HUC codes nest by prefix: HUC6 = HUC8[:6], HUC4 = HUC8[:4], etc.
    level = args.huc_level
    dams["_GROUP"] = dams["HUC8"].str[: level]
    groups = sorted(dams.groupby("_GROUP"), key=lambda kv: kv[0])

    if args.huc8:
        target = args.huc8
        groups = [(h, g) for h, g in groups if h == target]
        if not groups:
            sys.exit(
                f"No dams for group '{target}' at HUC level {level} in {args.dams_csv}"
            )

    skip = {"archived"} if args.huc8 else _SKIP_STATUSES
    pending = [
        (h, g) for h, g in groups
        if ledger.get(h, {}).get("status") not in skip
    ]
    n_partial = sum(
        1 for h, _ in pending
        if ledger.get(h, {}).get("status") == "partial"
    )

    if args.reverse:
        pending = list(reversed(pending))

    print(
        f"HUC{level} batches in CSV: {len(groups)} | "
        f"done (ready/archived): {len(groups) - len(pending)} | "
        f"pending: {len(pending)} (incl. {n_partial} partial being retried)"
        + (" | order: reversed" if args.reverse else "")
    )
    if existing_dirs:
        print(f"Existing data dirs: {', '.join(str(d) for d in existing_dirs)}")
    
    # Open the NWM zarr (and materialize its feature_id set) once for the
    # whole session — both are reused across every HUC, saving the S3 open
    # + ~2.7M id round-trip per batch.
    nwm_ds: xr.Dataset | None = None
    nwm_ids: set[int] | None = None
    if pending:
        print("\nOpening shared NWM retrospective zarr for the session …")
        try:
            nwm_ds = open_nwm_zarr(_DEFAULT_NWM_URL)
            nwm_ids = set(int(x) for x in nwm_ds[_FEATURE_DIM].values.tolist())
            print(f"  zarr open, {len(nwm_ids):,} reaches in coordinate set.")
        except Exception as e:
            print(f"  ! shared zarr open failed: {e}. "
                  "Falling back to per-HUC subprocess.")
            nwm_ds = None
            nwm_ids = None

    # Pre-load the NHDPlus VAA table once — pynhd caches the parquet on
    # disk, so this saves a ~5-10s parquet-read + DataFrame-construct on
    # every HUC. If it fails for any reason, fall back to per-HUC subprocess
    # invocation of stage_nhd_dem (which loads VAA inline).
    vaa_df = None
    if pending:
        try:
            vaa_df = load_vaa_df()
        except Exception as e:
            print(f"  ! shared VAA preload failed: {e}. "
                  "Falling back to per-HUC subprocess for stage_nhd_dem.")
            vaa_df = None


    for key, group in pending:
        free = _free_gb(local_root)
        if free < args.min_free_gb:
            print(
                f"\nFree space on {local_root}: {free:.1f} GB "
                f"< {args.min_free_gb} GB threshold."
            )
            print("Archive a completed HUC bundle to your external drive, then:")
            print(f"  python backend/rolling_pipeline.py "
                  f"--local-staging-root {local_root} --consolidate <KEY>")
            print(f"  mv {local_root}/huc{level}_<KEY> /Volumes/<drive>/")
            print(f"  python backend/rolling_pipeline.py "
                  f"--local-staging-root {local_root} --mark-archived <KEY>")
            print(f"…or batch-archive every ready bundle in one shot:")
            print(f"  python backend/rolling_pipeline.py "
                  f"--local-staging-root {local_root} --archive-to /Volumes/<drive>")
            print("…then rerun this command to continue.")
            print("\nAuto-aggregating dangerous-flow columns into master CSV …")
            try:
                _cmd_aggregate(args)
            except Exception as e:
                print(f"  WARN aggregate step failed: {e}")
            return

        print(f"\n{'=' * 60}")
        print(f"HUC{level} {key}  ({len(group)} dams, {free:.1f} GB free)")
        print(f"{'=' * 60}")
        try:
            _process_huc8(
                key, group, local_root, ledger, ledger_path,
                args.workers, existing_dirs,
                nwm_dataset=nwm_ds, nwm_ids=nwm_ids,
                vaa_df=vaa_df,
                keep_raw_tiles=args.keep_raw_tiles,
                retry_failed=args.retry_failed,
                arc_workers=args.arc_workers,
            )
        except (subprocess.CalledProcessError, Exception) as e:
            # Catches both subprocess failures (the original error path) and
            # uncaught Python exceptions from the in-process steps
            # (stage_nhd_dem.run_stage, process_streamflow_with_ds). Either
            # way we flip the ledger to "errored", persist, and halt cleanly
            # so the user can investigate and rerun without leaving the
            # batch in a half-staged state that wouldn't auto-retry.
            kind = (
                "subprocess step"
                if isinstance(e, subprocess.CalledProcessError)
                else f"in-process step ({type(e).__name__})"
            )
            print(f"\nFATAL: HUC{level} {key} {kind} failed: {e}")
            traceback.print_exc()
            entry = ledger.setdefault(key, {})
            entry["status"] = "errored"
            entry["error"] = str(e)
            entry["error_kind"] = kind
            entry["errored_at"] = _now()
            _save_ledger(ledger_path, ledger)
            print("Halting loop. Investigate, then rerun.")
            return

    print("\nAll pending HUCs processed.")

    # Refresh Qmin/Qmax columns on the master CSV from whatever analysis
    # summaries now exist (across staging-root + each --existing-data-dir).
    # Failure here shouldn't undo the pipeline run, so swallow exceptions.
    print("\nAuto-aggregating dangerous-flow columns into master CSV …")
    try:
        _cmd_aggregate(args)
    except Exception as e:
        print(f"  WARN aggregate step failed: {e}")


def _cmd_mark_archived(args: argparse.Namespace) -> None:
    local_root: Path = args.local_staging_root
    ledger_path = local_root / "huc8_ledger.json"
    ledger = _load_ledger(ledger_path)
    key = args.mark_archived
    if key not in ledger:
        sys.exit(f"HUC group '{key}' not found in ledger at {ledger_path}")
    ledger[key]["status"] = "archived"
    ledger[key]["archived_at"] = _now()
    _save_ledger(ledger_path, ledger)
    huc_dir = local_root / _huc_dirname(key)
    if huc_dir.exists():
        print(
            f"WARNING: {huc_dir} still exists locally — confirm the archive copy "
            f"is good, then `rm -rf {huc_dir}`."
        )
    print(f"HUC group {key} marked archived.")


def _cmd_consolidate(args: argparse.Namespace) -> None:
    local_root: Path = args.local_staging_root
    ledger_path = local_root / "huc8_ledger.json"
    ledger = _load_ledger(ledger_path)
    key = args.consolidate
    huc_dir = local_root / _huc_dirname(key)
    if not huc_dir.is_dir():
        sys.exit(f"HUC dir not found: {huc_dir}")

    print(f"Consolidating {huc_dir} (replacing symlinks with real files)…")
    moved, dangling = _consolidate_huc(huc_dir)
    print(f"  Moved {moved} symlinked dir(s) into the bundle.")
    if dangling:
        print(f"  ! {len(dangling)} dangling symlink(s) skipped:")
        for d in dangling:
            print(f"      {d}")

    entry = ledger.setdefault(key, {})
    entry["consolidated"] = True
    entry["consolidated_at"] = _now()
    entry["consolidated_moves"] = moved
    entry.pop("external_links", None)

    if not args.keep_raw_tiles:
        pruned, freed = _prune_raw_dem_tiles(huc_dir)
        if pruned:
            print(f"  Deleted {pruned} raw 3DEP tile(s) — freed {freed / 1e9:.1f} GB. "
                  f"Record retained in tile_manifest.json.")
        else:
            print("  No raw 3DEP tiles to delete (already pruned or never staged).")

    _save_ledger(ledger_path, ledger)

    # Refresh size + marker
    size_bytes = _dir_size_bytes(huc_dir)
    marker_path = huc_dir / ".READY_TO_ARCHIVE"
    if marker_path.exists():
        marker = json.loads(marker_path.read_text())
        marker["size_bytes"] = size_bytes
        marker["size_human"] = f"{size_bytes / 1e9:.1f} GB"
        marker["uses_external_data"] = False
        marker["external_paths"] = []
        marker["consolidated_at"] = entry["consolidated_at"]
        marker_path.write_text(json.dumps(marker, indent=2))

    print(f"HUC group {key} consolidated. New size: {size_bytes / 1e9:.1f} GB. "
          f"Safe to `mv {huc_dir} /Volumes/<drive>/`.")


def _cmd_prune_raw_all(args: argparse.Namespace) -> None:
    """Walk every bundle in the ledger and delete DEM/raw_3dep/ in place.

    Does NOT consolidate symlinks, move bundles, or change ledger status —
    pure local-disk reclamation. tile_manifest.json keeps the dam → tile +
    URL mapping plus a deletion stamp for each pruned bundle.
    """
    local_root: Path = args.local_staging_root
    ledger_path = local_root / "huc8_ledger.json"
    ledger = _load_ledger(ledger_path)
    if not ledger:
        sys.exit(f"No ledger at {ledger_path}")

    total_files = 0
    total_bytes = 0
    pruned_keys: List[str] = []
    skipped_missing: List[str] = []
    nothing_to_do: List[str] = []

    for key in sorted(ledger.keys()):
        huc_dir = local_root / _huc_dirname(key)
        if not huc_dir.is_dir():
            skipped_missing.append(key)
            continue
        count, freed = _prune_raw_dem_tiles(huc_dir)
        if count:
            print(f"[{key}] deleted {count} tile(s); freed {freed / 1e9:.2f} GB")
            total_files += count
            total_bytes += freed
            pruned_keys.append(key)
        else:
            nothing_to_do.append(key)

    print(
        f"\nDone. pruned {len(pruned_keys)} bundle(s); "
        f"{total_files} tile(s); freed {total_bytes / 1e9:.1f} GB total."
    )
    if nothing_to_do:
        print(f"  {len(nothing_to_do)} bundle(s) had no raw tiles to delete "
              f"(already pruned or never staged).")
    if skipped_missing:
        print(f"  {len(skipped_missing)} ledger key(s) had no local dir "
              f"(already archived/moved).")


def _cmd_status(args: argparse.Namespace) -> None:
    local_root: Path = args.local_staging_root
    ledger_path = local_root / "huc8_ledger.json"
    ledger = _load_ledger(ledger_path)
    if not ledger:
        print(f"No ledger at {ledger_path}")
        return
    counts: Dict[str, int] = {}
    rows = []
    for huc8, entry in sorted(ledger.items()):
        status = entry.get("status", "?")
        counts[status] = counts.get(status, 0) + 1
        ext = "yes" if entry.get("external_links") else ""
        rows.append((
            huc8,
            status,
            entry.get("dam_count", len(entry.get("dam_ids", []))),
            len(entry.get("failed", [])),
            entry.get("size_human", ""),
            ext,
        ))
    print(f"Ledger: {ledger_path}\n")
    print(f"{'HUC':<10} {'status':<18} {'dams':>5} {'failed':>7} {'size':>10}  {'ext':<3}")
    print("-" * 60)
    for r in rows:
        print(f"{r[0]:<10} {r[1]:<18} {r[2]:>5} {r[3]:>7} {r[4]:>10}  {r[5]:<3}")
    print("\nTotals:")
    for s in sorted(counts):
        print(f"  {s:<20} {counts[s]}")


def _cmd_archive_to(args: argparse.Namespace) -> None:
    """Batch-archive every ready_to_archive batch in the ledger to --archive-to.

    For each batch with status == "ready_to_archive":
      1. Consolidate symlinks into real files (so the bundle is self-contained).
      2. Move huc<level>_<KEY>/ to <archive-to>/.
      3. Flip ledger status to "archived".

    Errors on one batch are reported but don't stop the loop — subsequent
    batches still get archived.
    """
    local_root: Path = args.local_staging_root
    dest: Path = args.archive_to.resolve()
    if not dest.is_dir():
        sys.exit(f"--archive-to directory does not exist: {dest}")

    ledger_path = local_root / "huc8_ledger.json"
    ledger = _load_ledger(ledger_path)
    if not ledger:
        sys.exit(f"No ledger at {ledger_path}")

    ready = [k for k, v in ledger.items() if v.get("status") == "ready_to_archive"]
    if not ready:
        print("No batches in ready_to_archive state. Nothing to do.")
        return

    print(f"Archiving {len(ready)} batch(es) → {dest}")
    archived: List[str] = []
    failed: List[Tuple[str, str]] = []
    for key in sorted(ready):
        huc_dir = local_root / _huc_dirname(key)
        if not huc_dir.is_dir():
            failed.append((key, f"bundle missing at {huc_dir}"))
            continue

        try:
            print(f"\n[{key}] consolidating symlinks …")
            moved, dangling = _consolidate_huc(huc_dir)
            print(f"  moved {moved} symlinked dir(s)"
                  + (f"; {len(dangling)} dangling skipped" if dangling else ""))

            entry = ledger.setdefault(key, {})
            entry["consolidated"] = True
            entry["consolidated_at"] = _now()
            entry["consolidated_moves"] = moved
            entry.pop("external_links", None)

            if not args.keep_raw_tiles:
                pruned, freed = _prune_raw_dem_tiles(huc_dir)
                if pruned:
                    print(f"  deleted {pruned} raw 3DEP tile(s); "
                          f"freed {freed / 1e9:.1f} GB")

            size_bytes = _dir_size_bytes(huc_dir)
            marker_path = huc_dir / ".READY_TO_ARCHIVE"
            if marker_path.exists():
                marker = json.loads(marker_path.read_text())
                marker["size_bytes"] = size_bytes
                marker["size_human"] = f"{size_bytes / 1e9:.1f} GB"
                marker["uses_external_data"] = False
                marker["external_paths"] = []
                marker["consolidated_at"] = entry["consolidated_at"]
                marker_path.write_text(json.dumps(marker, indent=2))

            print(f"[{key}] moving {size_bytes / 1e9:.1f} GB → {dest}/{huc_dir.name} …")
            shutil.move(str(huc_dir), str(dest / huc_dir.name))

            entry["status"] = "archived"
            entry["archived_at"] = _now()
            entry["archived_to"] = str(dest / huc_dir.name)
            _save_ledger(ledger_path, ledger)
            print(f"[{key}] archived.")
            archived.append(key)
        except Exception as e:
            # Persist whatever ledger updates we did make before the failure
            _save_ledger(ledger_path, ledger)
            print(f"[{key}] FAILED: {e}")
            failed.append((key, str(e)))

    print(f"\nDone. archived={len(archived)} failed={len(failed)}")
    if failed:
        for k, why in failed:
            print(f"  ! {k}: {why}")


def _cmd_aggregate(args: argparse.Namespace) -> None:
    """Roll per-dam analysis_summary.json into screening columns on the master CSV.

    Searches the local staging root + every --existing-data-dir for
    RESULTS/<dam_id>/analysis_summary.json files (rglob handles both the
    huc<level>_<KEY>/RESULTS/ bundle layout and the flat RESULTS/ layout used
    by legacy scattered staging). For each dam computes:

      Qmin_env / Qmax_env       — min / max across the 4 downstream XS.
      Qmin_stable / Qmax_stable — XS whose along-flowline distance
                                  (xs_index * weir_length_m) is closest to
                                  height_info.ds_x_m (slope-stabilization
                                  cell from the dam-height estimator).
      Dam_Length_GIS_Ft         — Method-2 crest length (weir_length_m → ft).
      Dam_Height_GIS_Ft         — Method-B GIS height estimate (dam_height_m → ft).
      Rp100_cms                 — 100-year recurrence-interval flow (cms),
                                  intended as the upper bound when plotting
                                  rating curves on the website.
      Tailwater_a / Tailwater_b — depth rating-curve coefficients at the
                                  stable XS: D[m] = a * (Q[cms]) ** b.

    Drops any pre-existing Qmin / Qmax / Dam_*_GIS_Ft / Rp100_cms /
    Tailwater_a / Tailwater_b columns on the master CSV before writing the
    new columns back in place.
    """
    dams_csv: Path = args.dams_csv
    if not dams_csv.exists():
        sys.exit(f"Dam CSV not found: {dams_csv}")

    search_roots: List[Path] = [args.local_staging_root.resolve()]
    for d in (args.existing_data_dir or []):
        p = Path(d).resolve()
        if not p.is_dir():
            sys.exit(f"--existing-data-dir does not exist: {p}")
        search_roots.append(p)

    print(f"Scanning {len(search_roots)} root(s) for analysis_summary.json …")
    seen: Dict[int, Path] = {}
    for root in search_roots:
        if not root.exists():
            continue
        for summary_path in root.rglob("analysis_summary.json"):
            # Layout invariant: .../RESULTS/<dam_id>/analysis_summary.json
            try:
                dam_id = int(summary_path.parent.name)
            except ValueError:
                continue
            # First hit wins — caller orders staging-root before existing dirs.
            seen.setdefault(dam_id, summary_path)

    print(f"  → {len(seen)} dam summaries found")
    if not seen:
        print("Nothing to aggregate.")
        return

    rows: Dict[int, Dict[str, float]] = {}
    n_no_xs = 0
    n_no_stable = 0
    for dam_id, summary_path in seen.items():
        try:
            with open(summary_path) as f:
                summary = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"  WARN dam {dam_id}: unreadable summary ({e})")
            continue

        xs_list = summary.get("xs_results") or []
        weir_length_m = summary.get("weir_length_m")
        dam_height_m = summary.get("dam_height_m")
        rp100_cms = summary.get("rp100_cms")
        ds_x_m = (summary.get("height_info") or {}).get("ds_x_m")

        # GIS estimates in feet, for direct comparison with the NID Dam_*_Ft columns.
        M_TO_FT = 3.28084
        length_gis_ft = (float(weir_length_m) * M_TO_FT
                         if weir_length_m and weir_length_m > 0 else None)
        height_gis_ft = (float(dam_height_m) * M_TO_FT
                         if dam_height_m and dam_height_m > 0 else None)
        rp100_out = float(rp100_cms) if rp100_cms and rp100_cms > 0 else None

        valid = [
            xs for xs in xs_list
            if xs.get("Qmin_cms") is not None
            and xs.get("Qmax_cms") is not None
            and xs.get("xs_index") is not None
        ]
        if not valid:
            n_no_xs += 1
            # Still record GIS height/length + Rp100 if available — they're
            # independent of XS Qmin/Qmax.
            if length_gis_ft is not None or height_gis_ft is not None or rp100_out is not None:
                rows[dam_id] = {
                    "Qmin_env": None, "Qmax_env": None,
                    "Qmin_stable": None, "Qmax_stable": None,
                    "Dam_Length_GIS_Ft": length_gis_ft,
                    "Dam_Height_GIS_Ft": height_gis_ft,
                    "Rp100_cms": rp100_out,
                    "Tailwater_a": None,
                    "Tailwater_b": None,
                }
            continue

        qmin_env = min(float(xs["Qmin_cms"]) for xs in valid)
        qmax_env = max(float(xs["Qmax_cms"]) for xs in valid)

        qmin_stable = qmax_stable = None
        tailwater_a = tailwater_b = None
        if (weir_length_m and weir_length_m > 0
                and ds_x_m is not None and ds_x_m > 0):
            chosen = min(
                valid,
                key=lambda xs: abs(int(xs["xs_index"]) * float(weir_length_m)
                                   - float(ds_x_m)),
            )
            qmin_stable = float(chosen["Qmin_cms"])
            qmax_stable = float(chosen["Qmax_cms"])
            da = chosen.get("depth_a")
            db = chosen.get("depth_b")
            tailwater_a = float(da) if da is not None else None
            tailwater_b = float(db) if db is not None else None
        else:
            n_no_stable += 1

        rows[dam_id] = {
            "Qmin_env": qmin_env,
            "Qmax_env": qmax_env,
            "Qmin_stable": qmin_stable,
            "Qmax_stable": qmax_stable,
            "Dam_Length_GIS_Ft": length_gis_ft,
            "Dam_Height_GIS_Ft": height_gis_ft,
            "Rp100_cms": rp100_out,
            "Tailwater_a": tailwater_a,
            "Tailwater_b": tailwater_b,
        }

    print(f"  rolled up {len(rows)} dams "
          f"({n_no_xs} skipped — no valid XS, {n_no_stable} missing stable pick)")

    dams = pd.read_csv(dams_csv, low_memory=False)
    for old_col in ("Qmin", "Qmax", "Qmin_env", "Qmax_env",
                    "Qmin_stable", "Qmax_stable",
                    "Dam_Length_GIS_Ft", "Dam_Height_GIS_Ft",
                    "Rp100_cms", "Tailwater_a", "Tailwater_b"):
        if old_col in dams.columns:
            dams = dams.drop(columns=[old_col])

    add = pd.DataFrame.from_dict(rows, orient="index")
    add.index.name = "OBJECTID"
    add = add.reset_index()

    merged = dams.merge(add, on="OBJECTID", how="left")
    merged.to_csv(dams_csv, index=False)
    print(f"Wrote 9 columns (Qmin_env, Qmax_env, Qmin_stable, Qmax_stable, "
          f"Dam_Length_GIS_Ft, Dam_Height_GIS_Ft, Rp100_cms, Tailwater_a, "
          f"Tailwater_b) to {dams_csv}")


def _cmd_locate(args: argparse.Namespace) -> None:
    target = int(args.locate)
    dams_csv: Path = args.dams_csv
    if not dams_csv.exists():
        sys.exit(f"Dam CSV not found: {dams_csv}")
    dams = pd.read_csv(dams_csv, low_memory=False)
    if "HUC8" not in dams.columns:
        sys.exit(f"{dams_csv} has no HUC8 column. Run: python backend/assign_huc8.py")
    row = dams.loc[dams["OBJECTID"] == target]
    if row.empty:
        sys.exit(f"OBJECTID {target} not found in {dams_csv}")
    level = args.huc_level
    raw = row.iloc[0]["HUC8"]
    huc8 = str(raw).split(".")[0]
    if pd.isna(raw) or not huc8.isdigit() or huc8.zfill(8) == "00000000":
        print(f"Dam {target} has no valid HUC8 assignment (value={raw!r}).")
        print(f"  Run `python backend/assign_huc8.py` to populate HUC codes "
              f"in {dams_csv.name}.")
        return
    huc8 = huc8.zfill(8)
    key = huc8[:level]
    print(f"Dam {target} → HUC8 {huc8}  (HUC{level} batch {key}, folder {_huc_dirname(key)})")

    ledger_path = args.local_staging_root / "huc8_ledger.json"
    ledger = _load_ledger(ledger_path)
    entry = ledger.get(key, {})
    if entry:
        print(f"  ledger status: {entry.get('status', '?')}")
        if entry.get("size_human"):
            print(f"  bundle size:   {entry['size_human']}")
        if entry.get("external_links", {}).get(str(target)):
            print(f"  this dam's external sources:")
            for sub, path in entry["external_links"][str(target)].items():
                print(f"    {sub:<8} {path}")
    else:
        print(f"  not yet processed (no entry in {ledger_path})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--local-staging-root", type=Path, required=True,
                        help="Local staging root (per-HUC subdirs created here)")
    parser.add_argument("--dams-csv", type=Path, default=DEFAULT_DAMS_CSV,
                        help=f"Dam CSV with HUC8 column (default: {DEFAULT_DAMS_CSV})")
    parser.add_argument("--huc-level", type=int, choices=[2, 4, 6, 8], default=6,
                        help="HUC digit level to batch by (codes nest by prefix: "
                             "HUC6 = HUC8[:6]). Fewer/larger batches as the number "
                             "drops. [default: 6]")
    parser.add_argument("--min-free-gb", type=float, default=600.0,
                        help="Min free GB on staging root before starting a new batch [default: 600]")
    parser.add_argument("--arc-workers", type=int, default=4, metavar="N",
                        help="Worker count for run_arc_batch only (other steps "
                             "use --workers). run_arc_batch now parallelizes "
                             "via ProcessPoolExecutor instead of threads, since "
                             "the upstream ARC package keeps its config in "
                             "module-level globals that raced across threads "
                             "in one process (Windows access violation "
                             "0xC0000005). Separate processes don't share "
                             "those globals, so this is safe to raise — bound "
                             "mainly by CPU/RAM per dam.")
    parser.add_argument("--workers", type=int, default=8,
                        help="Worker count forwarded to subprocess steps [default: 8]")
    parser.add_argument("--existing-data-dir", type=Path, action="append", default=None,
                        help="Path to an old staging dir whose per-dam subfolders "
                             "(DEM/STRM/LAND/FLOW/RESULTS) should be symlinked into "
                             "new HUC bundles. May be passed multiple times.")
    parser.add_argument("--huc8", type=str, default=None, metavar="KEY",
                        help="Process only this HUC group key, matching the active "
                             "--huc-level (e.g. '140600' at level 6). Exact match.")
    parser.add_argument("--reverse", action="store_true",
                        help="Walk pending HUC batches from the highest key down "
                             "instead of the lowest key up. Lets a second instance "
                             "run concurrently against the other end of the list "
                             "without re-doing the same batches the forward "
                             "instance already claimed — they only risk meeting "
                             "(and re-touching) the same batch once both runs "
                             "reach the middle.")
    parser.add_argument("--mark-archived", type=str, default=None, metavar="KEY",
                        help="Mark the given HUC group key as archived in the ledger and exit")
    parser.add_argument("--consolidate", type=str, default=None, metavar="KEY",
                        help="Replace every symlink in huc<level>_<KEY>/ with the underlying "
                             "files via shutil.move (makes the bundle self-contained "
                             "for moving). Existing sources are emptied. Exits after.")
    parser.add_argument("--locate", type=str, default=None, metavar="DAM_ID",
                        help="Print the HUC8 + batch key (and ledger status, if any) "
                             "for the given dam OBJECTID, then exit")
    parser.add_argument("--archive-to", type=Path, default=None, metavar="PATH",
                        help="Batch-archive: for every ledger entry with status "
                             "ready_to_archive, consolidate symlinks, move "
                             "huc<level>_<KEY>/ to PATH/, and mark archived. "
                             "One command clears the entire queue.")
    parser.add_argument("--keep-raw-tiles", action="store_true",
                        help="Skip the raw_3dep tile prune. By default, raw tiles "
                             "are deleted at the end of each HUC during the main "
                             "loop, and also during --consolidate / --archive-to. "
                             "tile_manifest.json always retains the dam → tile + "
                             "URL mapping either way.")
    parser.add_argument("--prune-raw-all", action="store_true",
                        help="Walk every bundle in the ledger and delete its "
                             "DEM/raw_3dep/ in place. Does NOT consolidate "
                             "symlinks, move bundles, or change ledger status — "
                             "pure local-disk reclamation. tile_manifest.json "
                             "keeps the dam → tile + URL mapping plus a "
                             "deletion stamp.")
    parser.add_argument("--retry-failed", action="store_true",
                        help="By default, build_trimmed_dems writes a per-bundle "
                             "dam_failures.json and skips listed dams on retry; "
                             "stage_nhd_dem also skips downloading tiles only "
                             "referenced by those dams. Pass --retry-failed to "
                             "wipe dam_failures.json on every bundle the main "
                             "loop visits, forcing one more attempt (useful "
                             "after fixing an upstream issue like a TNM outage).")
    parser.add_argument("--aggregate", action="store_true",
                        help="Walk RESULTS/<dam_id>/analysis_summary.json under the "
                             "staging root + each --existing-data-dir and write "
                             "Qmin_env/Qmax_env (envelope across 4 downstream XS) and "
                             "Qmin_stable/Qmax_stable (XS nearest the slope-stabilization "
                             "cell) back into --dams-csv. Drops legacy Qmin/Qmax first.")
    parser.add_argument("--status", action="store_true",
                        help="Print ledger contents and exit")
    args = parser.parse_args()

    # Subcommand dispatch (mutually exclusive but argparse doesn't enforce)
    if args.status:
        _cmd_status(args)
    elif args.locate:
        _cmd_locate(args)
    elif args.consolidate:
        _cmd_consolidate(args)
    elif args.mark_archived:
        _cmd_mark_archived(args)
    elif args.archive_to:
        _cmd_archive_to(args)
    elif args.prune_raw_all:
        _cmd_prune_raw_all(args)
    elif args.aggregate:
        _cmd_aggregate(args)
    else:
        _cmd_run(args)


if __name__ == "__main__":
    main()
