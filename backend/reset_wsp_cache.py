"""
Relabel or delete wsp_result.json cache entries by status.

Typical use-case: TNM was down, dams got cached as no_dem (transient
failure). Relabel them api_fail so they're distinguishable from genuine
missing-DEM cases, then delete to allow the pipeline to retry them.

Usage
-----
    # Preview what would be touched
    python backend/reset_wsp_cache.py --staging-root /data/lhd_wsp --dry-run

    # Relabel no_dem → api_fail
    python backend/reset_wsp_cache.py --staging-root /data/lhd_wsp --relabel api_fail

    # Delete cache files so pipeline retries them
    python backend/reset_wsp_cache.py --staging-root /data/lhd_wsp --delete

    # Target a different status (e.g. clean up exception caches)
    python backend/reset_wsp_cache.py --staging-root /data/lhd_wsp --from-status exception --delete
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Tuple

_HUC_DIR_RE = re.compile(r"^huc\d+_")


def _find_cached(
    staging_root: Path,
    from_status: str,
) -> List[Tuple[Path, dict]]:
    """Return (path, data) for every wsp_result.json matching from_status."""
    hits = []
    for huc_dir in sorted(staging_root.iterdir()):
        if not huc_dir.is_dir() or not _HUC_DIR_RE.match(huc_dir.name):
            continue
        results_dir = huc_dir / "WSP_RESULTS"
        if not results_dir.is_dir():
            continue
        for p in results_dir.rglob("wsp_result.json"):
            try:
                with open(p) as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("status", "").startswith(from_status):
                hits.append((p, data))
    return hits


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--staging-root", type=Path, required=True)
    parser.add_argument("--from-status", default="no_dem",
                        help="Cache status to target [default: no_dem]")

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--relabel", metavar="NEW_STATUS",
                        help="Overwrite status field with NEW_STATUS")
    action.add_argument("--delete", action="store_true",
                        help="Delete the cache files (pipeline will retry)")
    action.add_argument("--dry-run", action="store_true",
                        help="Print what would be changed without touching files")

    args = parser.parse_args()

    if not args.staging_root.is_dir():
        sys.exit(f"Staging root not found: {args.staging_root}")

    hits = _find_cached(args.staging_root, args.from_status)

    if not hits:
        print(f"No cached results with status '{args.from_status}' found.")
        return

    print(f"Found {len(hits):,} cache file(s) with status '{args.from_status}'")

    if args.dry_run:
        for p, data in hits[:20]:
            print(f"  {p}")
        if len(hits) > 20:
            print(f"  ... and {len(hits) - 20} more")
        return

    ok = errors = 0
    for p, data in hits:
        try:
            if args.delete:
                p.unlink()
            else:
                data["status"] = args.relabel
                data["relabeled_from"] = args.from_status
                with open(p, "w") as f:
                    json.dump(data, f, indent=2)
            ok += 1
        except OSError as e:
            print(f"  ! {p}: {e}")
            errors += 1

    if args.delete:
        print(f"Deleted {ok:,} cache file(s) — pipeline will retry these dams.")
    else:
        print(f"Relabeled {ok:,} cache file(s): '{args.from_status}' → '{args.relabel}'")
        print("Run again with --delete to allow the pipeline to retry them.")

    if errors:
        print(f"{errors} error(s) — check output above.")


if __name__ == "__main__":
    main()
