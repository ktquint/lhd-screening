"""
Run ARC for a single staged dam with full Python traceback on failure.

Useful for digging into "FAIL run_arc (...)" entries from run_arc_batch.py —
the batch wrapper swallows the traceback, this script lets it propagate.

Usage
-----
    python scripts/debug_arc_one_dam.py --staging-dir <path> --dam-id <id>
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from lhd_processor.lhd_arc import ArcDam

DEFAULT_DAMS_CSV = _REPO_ROOT / "data" / "full_lhd_website.csv"


def _resolve(staging_dir: Path, dam_id: int):
    strm_site = staging_dir / "STRM" / str(dam_id)
    gpkgs = list(strm_site.glob("nhd_flowline_*.gpkg"))
    if not gpkgs:
        sys.exit(f"No flowline gpkg under {strm_site}")
    gpkg = gpkgs[0]
    m = re.search(r"nhd_flowline_(\d+)", gpkg.stem)
    if not m:
        sys.exit(f"Could not parse nhdplusid from {gpkg.name}")
    nhdplusid = int(m.group(1))
    paths = {
        "flowline": gpkg,
        "strm_clean": strm_site / f"nhd_{nhdplusid}_clean.tif",
        "dem": staging_dir / "DEM" / str(dam_id) / f"dem_{dam_id}.tif",
        "land": staging_dir / "LAND" / str(dam_id) / "constant_land.tif",
        "manning": staging_dir / "LAND" / str(dam_id) / "Manning_n.txt",
        "flow_csv": staging_dir / "FLOW" / str(dam_id) / f"flow_{dam_id}.csv",
    }
    for k, p in paths.items():
        if not p.exists():
            sys.exit(f"Missing input '{k}': {p}")
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--staging-dir", required=True, type=Path)
    parser.add_argument("--dam-id", required=True, type=int)
    parser.add_argument("--dams-csv", type=Path, default=DEFAULT_DAMS_CSV)
    args = parser.parse_args()

    dams = pd.read_csv(args.dams_csv)
    dams = dams.dropna(subset=["OBJECTID", "Latitude", "Longitude"])
    dams["OBJECTID"] = dams["OBJECTID"].astype(int)
    row = dams[dams["OBJECTID"] == args.dam_id]
    if row.empty:
        sys.exit(f"Dam {args.dam_id} not found in {args.dams_csv}")
    lat = float(row.iloc[0]["Latitude"])
    lon = float(row.iloc[0]["Longitude"])

    paths = _resolve(args.staging_dir, args.dam_id)
    results_dir = args.staging_dir / "RESULTS"

    print(f"Dam {args.dam_id}: lat={lat:.5f}, lon={lon:.5f}")
    for k, p in paths.items():
        print(f"  {k}: {p}")

    arc = ArcDam(
        dam_id=args.dam_id,
        latitude=lat, longitude=lon,
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
    arc.run_arc()  # Let any exception propagate with full traceback


if __name__ == "__main__":
    main()
