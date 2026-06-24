"""
Rasterize each dam's staged NHD flowline onto its trimmed DEM and write
the cleaned stream raster ARC needs.

Inputs (per dam):
  STRM/{dam_id}/nhd_flowline_{nhdplusid}.gpkg   (from stage_nhd_dem.py)
  DEM/{dam_id}/dem_{dam_id}.tif                  (from build_trimmed_dems.py)

Output (per dam):
  STRM/{dam_id}/nhd_{nhdplusid}_clean.tif

Existing outputs are reused; pass --force to rebuild.

Usage
-----
    python backend/build_stream_rasters.py --staging-dir /data/lhd_staging
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_BACKEND_ROOT = Path(__file__).resolve().parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from lhd_processor.stream_raster import create_arc_strm_raster, clean_strm_raster

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _resolve_inputs(
    dam_id: int, flowline_dir: Path, dem_dir: Path
) -> Optional[Tuple[Path, int, Path, Path]]:
    site_strm_dir = flowline_dir / str(dam_id)
    gpkg_files = list(site_strm_dir.glob("nhd_flowline_*.gpkg"))
    if not gpkg_files:
        return None
    gpkg = gpkg_files[0]

    m = re.search(r"nhd_flowline_(\d+)", gpkg.stem)
    if not m:
        return None
    nhdplusid = int(m.group(1))

    dem = dem_dir / str(dam_id) / f"dem_{dam_id}.tif"
    if not dem.exists():
        return None

    return gpkg, nhdplusid, dem, site_strm_dir


def _build_one(
    idx: int,
    total: int,
    dam_id: int,
    flowline_dir: Path,
    dem_dir: Path,
    force: bool,
) -> Tuple[int, str]:
    inputs = _resolve_inputs(dam_id, flowline_dir, dem_dir)
    if inputs is None:
        _log(f"[{idx}/{total}] Dam {dam_id}: SKIP (missing flowline gpkg or trimmed DEM)")
        return dam_id, "missing-input"

    gpkg, nhdplusid, dem, site_strm_dir = inputs
    raw_tif = site_strm_dir / f"nhd_{nhdplusid}.tif"
    clean_tif = site_strm_dir / f"nhd_{nhdplusid}_clean.tif"

    if not force and clean_tif.exists():
        _log(f"[{idx}/{total}] Dam {dam_id}: cached")
        return dam_id, "cached"

    try:
        create_arc_strm_raster(str(gpkg), str(raw_tif), str(dem), value_field="nhdplusid")
    except Exception as e:
        _log(f"[{idx}/{total}] Dam {dam_id}: FAIL rasterize ({e})")
        return dam_id, f"rasterize-error:{e}"

    try:
        clean_strm_raster(str(raw_tif), str(clean_tif))
    except Exception as e:
        _log(f"[{idx}/{total}] Dam {dam_id}: FAIL clean ({e})")
        return dam_id, f"clean-error:{e}"

    if raw_tif.exists():
        try:
            raw_tif.unlink()
        except Exception:
            pass

    _log(f"[{idx}/{total}] Dam {dam_id}: ok → {clean_tif.name}")
    return dam_id, "ok"


def _load_dam_ids(staging_dir: Path) -> List[int]:
    """Read dam IDs from tile_manifest.json (the authoritative dam list)."""
    manifest_path = staging_dir / "tile_manifest.json"
    if not manifest_path.exists():
        sys.exit(f"Manifest not found: {manifest_path}\nRun stage_nhd_dem.py first.")
    with open(manifest_path) as f:
        manifest = json.load(f)
    dam_tiles: Dict[int, List[str]] = {
        int(k): v for k, v in manifest.get("dam_tiles", {}).items()
    }
    return list(dam_tiles.keys())


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--staging-dir", required=True, type=Path,
                        help="Root of the staging tree (same one used by stage_nhd_dem.py)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N dams from the manifest")
    parser.add_argument("--workers", type=int, default=8,
                        help="Parallel workers [default: 8]")
    parser.add_argument("--force", action="store_true",
                        help="Rebuild stream raster even if cached on disk")
    args = parser.parse_args()

    staging_dir: Path = args.staging_dir
    flowline_dir = staging_dir / "STRM"
    dem_dir = staging_dir / "DEM"

    dam_ids = _load_dam_ids(staging_dir)
    if args.limit:
        dam_ids = dam_ids[: args.limit]
    total = len(dam_ids)

    print(f"Building cleaned stream rasters for {total} dams (workers={args.workers})\n")

    counts: Dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(_build_one, i + 1, total, dam_id, flowline_dir, dem_dir, args.force): dam_id
            for i, dam_id in enumerate(dam_ids)
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
        print(f"  {status:<22} {counts[status]}")


if __name__ == "__main__":
    main()
