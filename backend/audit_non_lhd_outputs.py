"""Find pipeline output for dams that the LHDI review has flagged as non-LHD.

Background
----------
A subset of dams in ``full_lhd_website.csv`` have ``Review_Status`` values that
mean they should not have been screened:

    - "Removed"
    - "Confirmed not a LHD"
    - "Appears to not be LHD"

This script scans a local pipeline staging root for any per-dam output dirs
that belong to those OBJECTIDs, so you can decide whether to delete them.

Expected layout (from rolling_pipeline.py)
------------------------------------------
    <staging_root>/
        huc8_ledger.json
        huc{N}_<key>/          # e.g. huc6_140600
            dams.csv
            DEM/<OBJECTID>/
            STRM/<OBJECTID>/
            LAND/<OBJECTID>/
            FLOW/<OBJECTID>/
            RESULTS/<OBJECTID>/
                arc_done.json
                analysis_summary.json
            .READY_TO_ARCHIVE

Usage
-----
    python backend/audit_non_lhd_outputs.py <staging_root>
    python backend/audit_non_lhd_outputs.py <staging_root> --csv frontend/data/full_lhd_website.csv
    python backend/audit_non_lhd_outputs.py <staging_root> --report report.csv
    python backend/audit_non_lhd_outputs.py <staging_root> --delete         # remove per-dam dirs (with confirm)
    python backend/audit_non_lhd_outputs.py <staging_root> --delete --yes   # skip confirmation

Multiple staging roots may be given as positional args; they are scanned in
order and merged into one report.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from collections import Counter
from pathlib import Path

EXCLUDED_REVIEW_STATUSES = {
    "Removed",
    "Confirmed not a LHD",
    "Appears to not be LHD",
}
SUBDIRS = ("DEM", "STRM", "LAND", "FLOW", "RESULTS")
DEFAULT_CSV = Path(__file__).resolve().parent.parent / "frontend" / "data" / "full_lhd_website.csv"


def normalize_objectid(raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def load_non_lhd_ids(csv_path: Path) -> dict[int, str]:
    """Return {OBJECTID: Review_Status} for every excluded dam."""
    excluded: dict[int, str] = {}
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        if "Review_Status" not in (reader.fieldnames or []):
            sys.exit(f"{csv_path} has no Review_Status column. Re-run fetch_lhd_review_metadata.py.")
        for row in reader:
            status = (row.get("Review_Status") or "").strip()
            if status not in EXCLUDED_REVIEW_STATUSES:
                continue
            oid = normalize_objectid(row.get("OBJECTID", ""))
            if oid is not None:
                excluded[oid] = status
    return excluded


def find_bundles(staging_root: Path) -> list[Path]:
    """All huc{N}_<key> directories one level deep under staging_root."""
    if not staging_root.is_dir():
        return []
    bundles = []
    for child in staging_root.iterdir():
        if child.is_dir() and child.name.startswith("huc") and "_" in child.name:
            bundles.append(child)
    return sorted(bundles)


def audit_bundle(bundle: Path, excluded: dict[int, str]) -> list[dict]:
    """
    Scan one HUC bundle for output dirs belonging to excluded OBJECTIDs.

    Returns one record per (bundle, dam_id) pair, with which subdirs exist and
    whether the arc/analysis markers are present.
    """
    findings: dict[int, dict] = {}
    for sub in SUBDIRS:
        sub_dir = bundle / sub
        if not sub_dir.is_dir():
            continue
        for entry in sub_dir.iterdir():
            try:
                oid = int(entry.name)
            except ValueError:
                continue
            if oid not in excluded:
                continue
            rec = findings.setdefault(oid, {
                "OBJECTID": oid,
                "Review_Status": excluded[oid],
                "bundle": bundle.name,
                "bundle_path": str(bundle),
                "subdirs_present": [],
                "is_symlink": {},
                "arc_done": False,
                "analysis_done": False,
                "paths_to_delete": [],
            })
            rec["subdirs_present"].append(sub)
            rec["is_symlink"][sub] = entry.is_symlink()
            rec["paths_to_delete"].append(str(entry))
            if sub == "RESULTS":
                rec["arc_done"] = (entry / "arc_done.json").exists()
                rec["analysis_done"] = (entry / "analysis_summary.json").exists()
    return list(findings.values())


def print_report(findings: list[dict], excluded: dict[int, str]) -> None:
    if not findings:
        print("\nNo pipeline output found for any non-LHD dam. Nothing to clean up.")
        return

    by_status = Counter(r["Review_Status"] for r in findings)
    by_bundle = Counter(r["bundle"] for r in findings)
    fully_processed = sum(1 for r in findings if r["arc_done"] and r["analysis_done"])
    only_staged = sum(1 for r in findings if not r["arc_done"])
    symlinked_only = sum(
        1 for r in findings
        if r["subdirs_present"] and all(r["is_symlink"].get(s, False) for s in r["subdirs_present"])
    )

    print(f"\nFound output for {len(findings)} non-LHD dam(s) "
          f"out of {len(excluded)} total excluded.")
    print(f"  - {fully_processed} fully processed (arc + analysis markers present)")
    print(f"  - {only_staged} staged but never completed ARC")
    print(f"  - {symlinked_only} are symlink-only (data lives in an --existing-data-dir source)")
    print()

    print("Breakdown by Review_Status:")
    for k, v in by_status.most_common():
        print(f"  {v:>5}  {k}")
    print()

    print(f"Breakdown by HUC bundle (top 15 of {len(by_bundle)}):")
    for k, v in by_bundle.most_common(15):
        print(f"  {v:>5}  {k}")
    print()

    print("Per-dam detail (first 30):")
    print(f"  {'OBJECTID':>9}  {'Review_Status':<22}  {'bundle':<22}  "
          f"{'subdirs':<28}  arc  ana")
    for r in sorted(findings, key=lambda x: (x["bundle"], x["OBJECTID"]))[:30]:
        subs = ",".join(r["subdirs_present"])
        arc = "Y" if r["arc_done"] else "."
        ana = "Y" if r["analysis_done"] else "."
        print(f"  {r['OBJECTID']:>9}  {r['Review_Status']:<22}  "
              f"{r['bundle']:<22}  {subs:<28}  {arc:^3}  {ana:^3}")
    if len(findings) > 30:
        print(f"  ... and {len(findings) - 30} more (use --report to dump full CSV)")


def write_report_csv(findings: list[dict], path: Path) -> None:
    cols = ["OBJECTID", "Review_Status", "bundle", "bundle_path",
            "subdirs_present", "arc_done", "analysis_done", "paths_to_delete"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in findings:
            w.writerow({
                "OBJECTID": r["OBJECTID"],
                "Review_Status": r["Review_Status"],
                "bundle": r["bundle"],
                "bundle_path": r["bundle_path"],
                "subdirs_present": ";".join(r["subdirs_present"]),
                "arc_done": r["arc_done"],
                "analysis_done": r["analysis_done"],
                "paths_to_delete": ";".join(r["paths_to_delete"]),
            })
    print(f"\nFull report written to {path}")


def delete_findings(findings: list[dict], skip_confirm: bool) -> None:
    paths: list[Path] = []
    for r in findings:
        for p in r["paths_to_delete"]:
            paths.append(Path(p))

    if not paths:
        return

    total = len(paths)
    print(f"\nAbout to delete {total} per-dam output dir(s) across "
          f"{len({r['bundle'] for r in findings})} HUC bundle(s).")
    if not skip_confirm:
        ans = input("Type 'DELETE' to confirm: ").strip()
        if ans != "DELETE":
            print("Aborted, nothing removed.")
            return

    removed = 0
    failed: list[tuple[str, str]] = []
    for p in paths:
        try:
            if p.is_symlink():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)
            else:
                continue  # already gone
            removed += 1
        except OSError as e:
            failed.append((str(p), str(e)))
    print(f"Removed {removed}/{total}.")
    if failed:
        print(f"{len(failed)} failures. First 10:")
        for p, e in failed[:10]:
            print(f"  {p}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("staging_roots", nargs="+", type=Path,
                    help="One or more local staging roots containing huc*_* bundles.")
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV,
                    help=f"Master dam CSV (default: {DEFAULT_CSV})")
    ap.add_argument("--report", type=Path, default=None,
                    help="Optional path to write the full per-dam findings as CSV.")
    ap.add_argument("--delete", action="store_true",
                    help="After auditing, remove the per-dam output dirs (asks for confirmation).")
    ap.add_argument("--yes", action="store_true",
                    help="Skip the 'DELETE' confirmation prompt (only with --delete).")
    args = ap.parse_args()

    if not args.csv.exists():
        sys.exit(f"Master CSV not found: {args.csv}")

    excluded = load_non_lhd_ids(args.csv)
    print(f"Loaded {len(excluded)} non-LHD OBJECTIDs from {args.csv.name}")
    print(f"  excluded statuses: {sorted(EXCLUDED_REVIEW_STATUSES)}")

    all_findings: list[dict] = []
    for root in args.staging_roots:
        root = root.resolve()
        bundles = find_bundles(root)
        if not bundles:
            print(f"\n[{root}] no huc*_* bundles found, skipping.")
            continue
        print(f"\n[{root}] scanning {len(bundles)} HUC bundle(s)...")
        for bundle in bundles:
            all_findings.extend(audit_bundle(bundle, excluded))

    print_report(all_findings, excluded)

    if args.report:
        write_report_csv(all_findings, args.report)

    if args.delete:
        delete_findings(all_findings, skip_confirm=args.yes)

    return 0


if __name__ == "__main__":
    sys.exit(main())
