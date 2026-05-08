"""
Screening pipeline: take a lat/lon, run the trimmed lhd_processor flow, and
return hydraulic analysis results — all without needing weir_length, an
Excel database, or a precomputed reanalysis CSV.

End-to-end flow:
    1. Download TDX-Hydro flowlines around the dam point.
    2. Download a DEM that covers the flowline bbox.
    3. Generate a constant-value land cover raster + Manning_n.txt (n=0.035).
    4. Generate the cleaned stream raster ARC needs.
    5. Build a per-request streamflow table (q_ep_50 + rp100 from GEOGLOWS).
    6. Run ARC.
    7. Estimate the dam's weir length from the lidar water-surface width at
       the snapped stream pixel (see screening.width).
    8. Re-extract the 5 hydraulic cross-sections at multiples of that L.
    9. Run the hydraulic analysis (lhd_processor.analysis_classes.Dam).

Configuration — set as env vars or override in the call:
    LHD_VPU_MAP   path to TDX-Hydro VPU boundary file (.gpkg or .geojson)
"""
from __future__ import annotations

import os
import time
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import geopandas as gpd
import pandas as pd

# Backend modules
import sys
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from lhd_processor.download_geospatial_data import download_tdx_flowline
from lhd_processor.prep_classes import (
    create_arc_strm_raster,
    clean_strm_raster,
    make_constant_land_raster,
)
from lhd_processor.lhd_arc import ArcDam
from lhd_processor.analysis_classes import Dam

from screening.streamflow_table import build_geoglows_flow_csv
from screening.width import estimate_weir_length


# ----------------------------------------------------------------------------
# External static data (not lat/lon-derived). Override via env vars or kwargs.
# ----------------------------------------------------------------------------
# VPU map is shipped with the repo; resolve relative to this file so it works
# regardless of cwd or install location.
_PIPELINE_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _PIPELINE_DIR.parent

DEFAULT_VPU_MAP = os.environ.get(
    "LHD_VPU_MAP",
    str(_BACKEND_DIR / "data" / "vpu-boundaries.gpkg"),
)

_MANNING_N = 0.035  # uniform roughness used for all dams


# ----------------------------------------------------------------------------


def _new_site_id() -> int:
    """Cheap unique-per-request ID. ARC uses it as a directory name only."""
    return int(time.time() * 1000)


def run_screening(
    latitude: float,
    longitude: float,
    work_dir: Optional[Path] = None,
    vpu_map: Optional[Path] = None,
    keep_artifacts: bool = False,
    flowline_distance_km=(1, 2),
    dem_resolution: int = 1,
) -> Dict[str, Any]:
    """
    Run the screening flow for a single (lat, lon).

    Parameters
    ----------
    work_dir
        Where to write intermediate files. If None, a TemporaryDirectory is
        used and cleaned up unless keep_artifacts=True.
    vpu_map
        Override the module-level default VPU map path if needed.
    keep_artifacts
        If True, the work_dir is preserved (still printed in the result).
    flowline_distance_km
        (upstream_km, downstream_km) reach buffer used by NLDI/TDX nav.
    dem_resolution
        1 (1m) or 10 (1/3 arc-sec). USGS 3DEP coverage is denser at 10m.

    Returns
    -------
    dict with keys:
        site_id, latitude, longitude, weir_length, baseflow_q_ep_50,
        max_flow_rp100, snap_row, snap_col, snap_dist_m,
        xs_data (list of per-XS analysis results), hydro_results (per-incident
        is None for screening; kept for parity), work_dir
    """
    vpu_map = Path(vpu_map or DEFAULT_VPU_MAP)
    if not vpu_map.exists():
        raise FileNotFoundError(f"VPU map not found: {vpu_map}")

    cleanup_ctx = None
    if work_dir is None:
        cleanup_ctx = tempfile.TemporaryDirectory(prefix="lhd_screen_")
        work_dir = Path(cleanup_ctx.name)
    else:
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

    site_id = _new_site_id()
    flowline_dir = work_dir / "STRM"
    dem_dir = work_dir / "DEM"
    land_dir = work_dir / "LAND"
    results_dir = work_dir / "Results"
    flow_csv_path = work_dir / f"flow_{site_id}.csv"
    for d in (flowline_dir, dem_dir, land_dir, results_dir):
        d.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Flowlines (TDX-Hydro / GEOGLOWS) ------------------------------
        flowline_path, flowline_gdf = download_tdx_flowline(
            latitude, longitude,
            flowline_dir=str(flowline_dir),
            vpu_map_path=str(vpu_map),
            distance_km=flowline_distance_km,
            site_id=site_id,
        )
        if flowline_path is None or flowline_gdf is None or flowline_gdf.empty:
            raise RuntimeError("No TDX flowlines found near this point.")
        linknos = flowline_gdf["LINKNO"].astype(int).tolist()
        # The "primary" reach (used for analysis flow_series later) is the
        # one closest to the dam point — already returned as the start_id by
        # navigate_tdx_network and saved as the gpkg filename suffix.
        primary_linkno = int(Path(flowline_path).stem.split("_")[-1])

        # 2. DEM -----------------------------------------------------------
        # TODO: rewire to consume staged trimmed DEM produced by
        # backend/build_trimmed_dems.py (DEM/{dam_id}/dem_{dam_id}.tif).
        # The screening pipeline no longer downloads DEM tiles itself —
        # tiles are staged in bulk by stage_nhd_dem.py and merged into
        # per-dam trimmed DEMs by build_trimmed_dems.py.
        raise NotImplementedError(
            "screening.pipeline must be rewired to read the staged trimmed DEM "
            "and constant land cover instead of calling download_dem."
        )

        # 3. Constant land cover raster + Manning_n.txt (n=0.035 everywhere) -
        land_raster, manning_n_txt = make_constant_land_raster(
            dem_path, land_dir, n=_MANNING_N
        )

        # 4. Cleaned stream raster (rasterize flowline → filter for ARC) ----
        strm_raw = flowline_dir / str(site_id) / f"tdx_{primary_linkno}.tif"
        strm_clean = flowline_dir / str(site_id) / f"tdx_{primary_linkno}_clean.tif"
        create_arc_strm_raster(str(flowline_path), str(strm_raw),
                               str(dem_path), value_field="LINKNO")
        clean_strm_raster(str(strm_raw), str(strm_clean))

        # 5. Per-request streamflow table -----------------------------------
        flow_df = build_geoglows_flow_csv(linknos, flow_csv_path)
        baseflow_q_ep_50 = float(
            flow_df.loc[flow_df["comid"] == primary_linkno, "q_ep_50"].iloc[0]
        )
        max_flow_rp100 = float(
            flow_df.loc[flow_df["comid"] == primary_linkno, "rp100"].iloc[0]
        )

        # 6. Run ARC --------------------------------------------------------
        arc_dam = ArcDam(
            dam_id=site_id,
            latitude=latitude, longitude=longitude,
            dem_path=dem_path,
            land_raster=land_raster,
            strm_tif_clean=strm_clean,
            flowline_path=flowline_path,
            flowline_source="TDX-Hydro",
            streamflow_source="GEOGLOWS",
            streamflow_csv=flow_csv_path,
            results_dir=results_dir,
            manning_n_txt=str(manning_n_txt),
            baseflow_col="q_ep_50",
            qmax_col="rp100",
        )
        arc_dam.run_arc()

        # 7. Estimate weir length from ARC outputs --------------------------
        width_info = estimate_weir_length(
            dam_lat=latitude, dam_lon=longitude,
            strm_path=strm_clean,
            vdt_path=arc_dam.vdt_txt,
            xs_path=arc_dam.xs_txt,
            curve_path=arc_dam.curvefile_csv,
        )
        weir_length = width_info["weir_length"]

        # 8. Extract local cross-sections at multiples of L -----------------
        arc_dam.extract_local_xs(
            snap_row=width_info["snap_row"],
            snap_col=width_info["snap_col"],
            weir_length=weir_length,
        )

        # 9. Hydraulic analysis ---------------------------------------------
        # We already have the GEOGLOWS retrospective via build_geoglows_flow_csv
        # implicitly, but Dam needs the full series for FDC + dangerous-flow
        # range. Refetching is simplest; could be cached if we plumb it through.
        dam = Dam(
            lhd_id=site_id,
            latitude=latitude, longitude=longitude,
            weir_length=weir_length,
            baseflow=baseflow_q_ep_50,
            base_results_dir=results_dir,
            flow_series=None,  # let Dam.get_flow_data() fetch via geoglows
            flowline_source="TDX-Hydro",
            streamflow_source="GEOGLOWS",
            calc_mode="Simplified",
            geoglows_id=primary_linkno,
        )
        xs_data, hydro_results = dam.run_analysis()

        return {
            "site_id": site_id,
            "latitude": latitude,
            "longitude": longitude,
            "weir_length": weir_length,
            "baseflow_q_ep_50": baseflow_q_ep_50,
            "max_flow_rp100": max_flow_rp100,
            "primary_linkno": primary_linkno,
            "snap_row": width_info["snap_row"],
            "snap_col": width_info["snap_col"],
            "snap_dist_m": width_info["snap_dist_m"],
            "xs_data": xs_data,
            "hydro_results": hydro_results,
            "work_dir": str(work_dir),
        }
    finally:
        if cleanup_ctx is not None and not keep_artifacts:
            cleanup_ctx.cleanup()


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--work-dir", type=Path, default=None,
                        help="Persistent work dir (default: temp dir, cleaned up)")
    parser.add_argument("--keep-artifacts", action="store_true")
    args = parser.parse_args()

    result = run_screening(
        latitude=args.lat, longitude=args.lon,
        work_dir=args.work_dir,
        keep_artifacts=args.keep_artifacts,
    )
    # xs_data and hydro_results are list-of-dicts; jsonify what serializes
    print(json.dumps({k: v for k, v in result.items()
                      if k not in {"xs_data", "hydro_results"}},
                     indent=2, default=str))
    print(f"\nXS rows: {len(result['xs_data'])}")
    print(f"Hydro rows: {len(result['hydro_results'])}")
    print(f"Work dir: {result['work_dir']}")
