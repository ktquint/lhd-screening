"""
HUC8-batched LHD screening pipeline.

For each HUC8 in frontend/data/full_lhd_website_huc8.csv:
  1. Free-space gate — if free disk on --local-staging-root is below
     --min-free-gb the loop stops cleanly so you can move a completed
     huc8_XXXXXXXX/ directory off to an external drive, then rerun.
  2. Write a per-HUC dams subset CSV to <local>/huc8_<HUC8>/dams.csv.
  3. Subprocess-chain the 6 pipeline steps targeting <local>/huc8_<HUC8>:
        stage_nhd_dem → build_trimmed_dems → build_stream_rasters
        → build_streamflow_csv → run_arc_batch → run_analysis_batch
  4. Tally ok/failed dams by checking RESULTS/{dam_id}/arc_done.json and
     RESULTS/{dam_id}/analysis_summary.json. Failures do NOT abort the loop.
  5. Write <local>/huc8_<HUC8>/.READY_TO_ARCHIVE with counts + size, and
     update <local>/huc8_ledger.json (status=ready_to_archive or partial).

Manual archive workflow
-----------------------
    # 1. Run the orchestrator — it fills HUC8s until free space drops.
    python backend/rolling_pipeline.py \\
        --local-staging-root /Users/me/staging --min-free-gb 600

    # 2. Move a completed huc8_XXXXXXXX/ to your external drive.
    mv /Users/me/staging/huc8_14060003 /Volumes/MyDrive/lhd_archive/

    # 3. Mark it archived so subsequent runs skip it.
    python backend/rolling_pipeline.py \\
        --local-staging-root /Users/me/staging \\
        --mark-archived 14060003

    # 4. Re-run step 1 — the loop continues from the next HUC8.

Other flags
-----------
    --huc8 <HUC8>   process only this HUC8 (string; 8 digits, leading zeros ok)
    --status        print the ledger and exit
    --workers N     worker count forwarded to subprocess steps [default: 8]
"""
from __future__ import annotations

import argparse
import json
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def _dir_size_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


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


def _process_huc8(
    huc8: str,
    dams_subset: pd.DataFrame,
    local_root: Path,
    ledger: Dict[str, dict],
    ledger_path: Path,
    workers: int,
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

    marker = {
        "huc8": huc8,
        "status": status,
        "dam_count": len(dam_ids),
        "ok": ok,
        "failed": failed,
        "size_bytes": size_bytes,
        "size_human": f"{size_bytes / 1e9:.1f} GB",
        "completed_at": _now(),
    }
    (huc_dir / ".READY_TO_ARCHIVE").write_text(json.dumps(marker, indent=2))

    entry.update(marker)
    _save_ledger(ledger_path, ledger)
    print(
        f"\nHUC8 {huc8}: {status}  "
        f"({len(ok)} ok / {len(failed)} failed, {marker['size_human']})"
    )


def _cmd_run(args: argparse.Namespace) -> None:
    local_root: Path = args.local_staging_root
    local_root.mkdir(parents=True, exist_ok=True)
    ledger_path = local_root / "huc8_ledger.json"
    ledger = _load_ledger(ledger_path)

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

    # Skip already-completed HUCs unless the user explicitly targeted one with --huc8
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

    for huc8, group in pending:
        free = _free_gb(local_root)
        if free < args.min_free_gb:
            print(
                f"\nFree space on {local_root}: {free:.1f} GB "
                f"< {args.min_free_gb} GB threshold."
            )
            print("Archive a completed HUC8 to your external drive, then:")
            print(f"  python backend/rolling_pipeline.py "
                  f"--local-staging-root {local_root} --mark-archived <HUC8>")
            print("…then rerun this command to continue.")
            return

        print(f"\n{'=' * 60}")
        print(f"HUC8 {huc8}  ({len(group)} dams, {free:.1f} GB free)")
        print(f"{'=' * 60}")
        try:
            _process_huc8(huc8, group, local_root, ledger, ledger_path, args.workers)
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
        rows.append((
            huc8,
            status,
            entry.get("dam_count", len(entry.get("dam_ids", []))),
            len(entry.get("failed", [])),
            entry.get("size_human", ""),
        ))
    print(f"Ledger: {ledger_path}\n")
    print(f"{'HUC8':<10} {'status':<18} {'dams':>5} {'failed':>7} {'size':>10}")
    print("-" * 55)
    for r in rows:
        print(f"{r[0]:<10} {r[1]:<18} {r[2]:>5} {r[3]:>7} {r[4]:>10}")
    print("\nTotals:")
    for s in sorted(counts):
        print(f"  {s:<20} {counts[s]}")


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
    parser.add_argument("--huc8", type=str, default=None,
                        help="Process only this HUC8 (8-digit string, leading zeros optional)")
    parser.add_argument("--mark-archived", type=str, default=None,
                        help="Mark the given HUC8 as archived in the ledger and exit")
    parser.add_argument("--status", action="store_true",
                        help="Print ledger contents and exit")
    args = parser.parse_args()

    if args.status:
        _cmd_status(args)
    elif args.mark_archived:
        _cmd_mark_archived(args)
    else:
        _cmd_run(args)


if __name__ == "__main__":
    main()
