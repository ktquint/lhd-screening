"""
HUC8-batched LHD screening pipeline.

For each HUC8 in frontend/data/full_lhd_website_huc8.csv:
  1. Free-space gate — if free disk on --local-staging-root is below
     --min-free-gb the loop stops cleanly so you can move a completed
     huc8_XXXXXXXX/ directory off to an external drive, then rerun.
  2. (Optional) Symlink existing per-dam staging from --existing-data-dir
     paths into the new HUC dir. Reuses prior DEM/STRM/LAND/FLOW/RESULTS
     dirs without copying. The external sources used are recorded in the
     ledger and in .READY_TO_ARCHIVE so you can locate them later.
  3. Write a per-HUC dams subset CSV to <local>/huc8_<HUC8>/dams.csv.
  4. Subprocess-chain the 6 pipeline steps targeting <local>/huc8_<HUC8>:
        stage_nhd_dem → build_trimmed_dems → build_stream_rasters
        → build_streamflow_csv → run_arc_batch → run_analysis_batch
  5. Tally ok/failed dams by checking RESULTS/{dam_id}/arc_done.json and
     RESULTS/{dam_id}/analysis_summary.json. Failures do NOT abort the loop.
  6. Write <local>/huc8_<HUC8>/.READY_TO_ARCHIVE with counts, size, and the
     external paths that need to come along when archiving.

Typical workflow
----------------
    # one-time
    python backend/assign_huc8.py

    # main loop (existing scattered data passed via --existing-data-dir)
    python backend/rolling_pipeline.py \\
        --local-staging-root /Users/me/staging \\
        --existing-data-dir /old/lhd_staging \\
        --min-free-gb 600

    # before archiving a HUC, collapse symlinks into real files:
    python backend/rolling_pipeline.py \\
        --local-staging-root /Users/me/staging --consolidate 14060003

    # then move and mark archived
    mv /Users/me/staging/huc8_14060003 /Volumes/MyDrive/
    python backend/rolling_pipeline.py \\
        --local-staging-root /Users/me/staging --mark-archived 14060003

Other commands
--------------
    --huc8 <HUC8>       process only this HUC8 (8 digits, leading zeros optional)
    --status            print the ledger and exit
    --locate <dam_id>   print which HUC8 a dam belongs to + its ledger status
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd

_BACKEND_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_ROOT.parent
DEFAULT_DAMS_CSV = _REPO_ROOT / "frontend" / "data" / "full_lhd_website_huc8.csv"

# Statuses that mean "this HUC8 is done from the orchestrator's POV"
_TERMINAL_STATUSES = {"ready_to_archive", "partial", "archived"}

# Per-dam subdirectories worth reusing from existing staging trees
_REUSE_SUBDIRS = ("DEM", "STRM", "LAND", "FLOW", "RESULTS")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


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


def _process_huc8(
    huc8: str,
    dams_subset: pd.DataFrame,
    local_root: Path,
    ledger: Dict[str, dict],
    ledger_path: Path,
    workers: int,
    existing_dirs: List[Path],
) -> None:
    huc_dir = local_root / f"huc8_{huc8}"
    huc_dir.mkdir(parents=True, exist_ok=True)
    dams_csv = huc_dir / "dams.csv"
    dams_subset.to_csv(dams_csv, index=False)
    dam_ids = [int(x) for x in dams_subset["OBJECTID"].tolist()]

    entry = ledger.setdefault(huc8, {})
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
    _run_step("build_streamflow_csv", [
        py, f"{backend}/build_streamflow_csv.py",
        *staging_arg, *common_workers,
    ])

    entry["status"] = "arc_running"
    entry["arc_at"] = _now()
    _save_ledger(ledger_path, ledger)

    _run_step("run_arc_batch", [
        py, f"{backend}/run_arc_batch.py",
        *staging_arg, *dams_arg, *common_workers,
    ])
    _run_step("run_analysis_batch", [
        py, f"{backend}/run_analysis_batch.py",
        *staging_arg, *common_workers,
    ])

    ok, failed = _tally_results(huc_dir, dam_ids)
    size_bytes = _dir_size_bytes(huc_dir)
    status = "ready_to_archive" if not failed else "partial"

    # Flatten the external_links dict to a sorted unique list of paths the user
    # needs to move (or consolidate) alongside the HUC bundle.
    external_paths = sorted({
        p for per_dam in entry.get("external_links", {}).values()
        for p in per_dam.values()
    })

    marker = {
        "huc8": huc8,
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
        f"\nHUC8 {huc8}: {status}  "
        f"({len(ok)} ok / {len(failed)} failed, {marker['size_human']})"
    )
    if external_paths:
        print(f"  Uses {len(external_paths)} external symlink source(s). "
              f"Run --consolidate {huc8} before moving the bundle.")


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
    dams = pd.read_csv(args.dams_csv)
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
    dams["HUC8"] = dams["HUC8"].astype(str).str.zfill(8)
    dams = dams[dams["HUC8"] != "00000000"]

    huc8_groups = sorted(dams.groupby("HUC8"), key=lambda kv: kv[0])

    if args.huc8:
        target = args.huc8.zfill(8)
        huc8_groups = [(h, g) for h, g in huc8_groups if h == target]
        if not huc8_groups:
            sys.exit(f"HUC8 {target} has no dams in {args.dams_csv}")

    skip = {"archived"} if args.huc8 else _TERMINAL_STATUSES
    pending = [
        (h, g) for h, g in huc8_groups
        if ledger.get(h, {}).get("status") not in skip
    ]

    print(
        f"HUC8s in CSV: {len(huc8_groups)} | "
        f"skipped (already terminal): {len(huc8_groups) - len(pending)} | "
        f"pending: {len(pending)}"
    )
    if existing_dirs:
        print(f"Existing data dirs: {', '.join(str(d) for d in existing_dirs)}")

    for huc8, group in pending:
        free = _free_gb(local_root)
        if free < args.min_free_gb:
            print(
                f"\nFree space on {local_root}: {free:.1f} GB "
                f"< {args.min_free_gb} GB threshold."
            )
            print("Archive a completed HUC8 to your external drive, then:")
            print(f"  python backend/rolling_pipeline.py "
                  f"--local-staging-root {local_root} --consolidate <HUC8>")
            print(f"  mv {local_root}/huc8_<HUC8> /Volumes/<drive>/")
            print(f"  python backend/rolling_pipeline.py "
                  f"--local-staging-root {local_root} --mark-archived <HUC8>")
            print("…then rerun this command to continue.")
            return

        print(f"\n{'=' * 60}")
        print(f"HUC8 {huc8}  ({len(group)} dams, {free:.1f} GB free)")
        print(f"{'=' * 60}")
        try:
            _process_huc8(
                huc8, group, local_root, ledger, ledger_path,
                args.workers, existing_dirs,
            )
        except subprocess.CalledProcessError as e:
            print(f"\nFATAL: HUC8 {huc8} pipeline step failed: {e}")
            entry = ledger.setdefault(huc8, {})
            entry["status"] = "errored"
            entry["error"] = str(e)
            entry["errored_at"] = _now()
            _save_ledger(ledger_path, ledger)
            print("Halting loop. Investigate, then rerun.")
            return

    print("\nAll pending HUCs processed.")


def _cmd_mark_archived(args: argparse.Namespace) -> None:
    local_root: Path = args.local_staging_root
    ledger_path = local_root / "huc8_ledger.json"
    ledger = _load_ledger(ledger_path)
    huc8 = args.mark_archived.zfill(8)
    if huc8 not in ledger:
        sys.exit(f"HUC8 {huc8} not found in ledger at {ledger_path}")
    ledger[huc8]["status"] = "archived"
    ledger[huc8]["archived_at"] = _now()
    _save_ledger(ledger_path, ledger)
    huc_dir = local_root / f"huc8_{huc8}"
    if huc_dir.exists():
        print(
            f"WARNING: {huc_dir} still exists locally — confirm the archive copy "
            f"is good, then `rm -rf {huc_dir}`."
        )
    print(f"HUC8 {huc8} marked archived.")


def _cmd_consolidate(args: argparse.Namespace) -> None:
    local_root: Path = args.local_staging_root
    ledger_path = local_root / "huc8_ledger.json"
    ledger = _load_ledger(ledger_path)
    huc8 = args.consolidate.zfill(8)
    huc_dir = local_root / f"huc8_{huc8}"
    if not huc_dir.is_dir():
        sys.exit(f"HUC dir not found: {huc_dir}")

    print(f"Consolidating {huc_dir} (replacing symlinks with real files)…")
    moved, dangling = _consolidate_huc(huc_dir)
    print(f"  Moved {moved} symlinked dir(s) into the bundle.")
    if dangling:
        print(f"  ! {len(dangling)} dangling symlink(s) skipped:")
        for d in dangling:
            print(f"      {d}")

    entry = ledger.setdefault(huc8, {})
    entry["consolidated"] = True
    entry["consolidated_at"] = _now()
    entry["consolidated_moves"] = moved
    entry.pop("external_links", None)
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

    print(f"HUC8 {huc8} consolidated. New size: {size_bytes / 1e9:.1f} GB. "
          f"Safe to `mv {huc_dir} /Volumes/<drive>/`.")


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
    print(f"{'HUC8':<10} {'status':<18} {'dams':>5} {'failed':>7} {'size':>10}  {'ext':<3}")
    print("-" * 60)
    for r in rows:
        print(f"{r[0]:<10} {r[1]:<18} {r[2]:>5} {r[3]:>7} {r[4]:>10}  {r[5]:<3}")
    print("\nTotals:")
    for s in sorted(counts):
        print(f"  {s:<20} {counts[s]}")


def _cmd_locate(args: argparse.Namespace) -> None:
    target = int(args.locate)
    dams_csv: Path = args.dams_csv
    if not dams_csv.exists():
        sys.exit(f"Dam CSV not found: {dams_csv}")
    dams = pd.read_csv(dams_csv)
    if "HUC8" not in dams.columns:
        sys.exit(f"{dams_csv} has no HUC8 column. Run: python backend/assign_huc8.py")
    row = dams.loc[dams["OBJECTID"] == target]
    if row.empty:
        sys.exit(f"OBJECTID {target} not found in {dams_csv}")
    huc8 = str(row.iloc[0]["HUC8"]).zfill(8)
    print(f"Dam {target} → HUC8 {huc8}")

    ledger_path = args.local_staging_root / "huc8_ledger.json"
    ledger = _load_ledger(ledger_path)
    entry = ledger.get(huc8, {})
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
    parser.add_argument("--min-free-gb", type=float, default=600.0,
                        help="Min free GB on staging root before starting a new HUC8 [default: 600]")
    parser.add_argument("--workers", type=int, default=8,
                        help="Worker count forwarded to subprocess steps [default: 8]")
    parser.add_argument("--existing-data-dir", type=Path, action="append", default=None,
                        help="Path to an old staging dir whose per-dam subfolders "
                             "(DEM/STRM/LAND/FLOW/RESULTS) should be symlinked into "
                             "new HUC bundles. May be passed multiple times.")
    parser.add_argument("--huc8", type=str, default=None,
                        help="Process only this HUC8 (8-digit string, leading zeros optional)")
    parser.add_argument("--mark-archived", type=str, default=None,
                        help="Mark the given HUC8 as archived in the ledger and exit")
    parser.add_argument("--consolidate", type=str, default=None,
                        help="Replace every symlink in huc8_<HUC8>/ with the underlying "
                             "files via shutil.move (makes the bundle self-contained "
                             "for moving). Existing sources are emptied. Exits after.")
    parser.add_argument("--locate", type=str, default=None,
                        help="Print the HUC8 (and ledger status, if any) for the "
                             "given dam OBJECTID, then exit")
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
    else:
        _cmd_run(args)


if __name__ == "__main__":
    main()
