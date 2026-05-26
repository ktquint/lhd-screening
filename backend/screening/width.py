"""
Estimate a dam's weir length from ARC's outputs and the lidar-captured DEM.

Implements the Method 2 (flat-zone) crest sampler from Quintana &
Hotchkiss, "Estimating Low-Head Dam Geometry using Open-Source Remote
Sensing Data" (ASDSO 2026).  The dam point is snapped to the nearest VDT
cell; from there we walk upstream along the NHDPlus reach and pick the
first cell whose DEM elevation gradient drops below 0.02 m/m.  That cell
sits in the ponded pool above the dam, where the lidar water surface is
horizontal and best represents the crest elevation.  The crest length is
then the wetted XS width at that cell.

Validated against the 72-dam cohort with median estimate/reported ratio
of 1.02 and RMSE of 68 m.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from pyproj import Transformer

from screening.reach import (
    DEFAULT_FLAT_THRESHOLD,
    DEFAULT_SEARCH_M_UP,
    build_reach,
    pick_crest_cell,
    walk_width,
)


def estimate_weir_length(
    dam_lat: float,
    dam_lon: float,
    strm_path: Path,
    vdt_path: Path,
    xs_path: Path,
    curve_path: Path,
    flowline_path: Path,
    search_m: float = DEFAULT_SEARCH_M_UP,
    flat_threshold: float = DEFAULT_FLAT_THRESHOLD,
) -> dict:
    """Snap the dam to the nearest VDT cell, build the reach, pick the
    flat-zone crest cell upstream, and return the wetted DEM width there.

    Returns a dict with the derived ``weir_length`` plus the snap cell,
    the selected crest cell (``crest_row``, ``crest_col``, ``crest_wse``,
    ``crest_base``), and the prebuilt ``reach`` DataFrame so a downstream
    height step can reuse it without re-reading Curve.csv / XS.txt.
    """
    with rasterio.open(strm_path) as src:
        transformer = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        x, y = transformer.transform(dam_lon, dam_lat)
        rows, cols = src.shape
        inv = ~src.transform
        col_f, row_f = inv * (x, y)
        if not (0 <= row_f < rows and 0 <= col_f < cols):
            raise ValueError(
                f"Dam ({dam_lat}, {dam_lon}) projects outside stream raster {strm_path}"
            )
        px_x = abs(src.transform.a)
        px_y = abs(src.transform.e)

    vdt = pd.read_csv(vdt_path)
    vdt.columns = [c.strip() for c in vdt.columns]
    if vdt.empty:
        raise RuntimeError(f"VDT.txt is empty: {vdt_path}")

    dr = (vdt["Row"].astype(float).values - row_f) * px_y
    dc = (vdt["Col"].astype(float).values - col_f) * px_x
    d2 = dr ** 2 + dc ** 2
    i = int(np.argmin(d2))
    snap_row = int(vdt.iloc[i]["Row"])
    snap_col = int(vdt.iloc[i]["Col"])
    snap_dist_m = float(np.sqrt(d2[i]))
    arc_wse_snap = (float(vdt.iloc[i]["Elev"])
                    if pd.notna(vdt.iloc[i].get("Elev")) else None)

    reach = build_reach(
        snap_row=snap_row,
        snap_col=snap_col,
        curve_path=curve_path,
        xs_path=xs_path,
        flowline_path=flowline_path,
        strm_path=strm_path,
        search_m=search_m,
    )
    if reach is None or reach.empty:
        raise RuntimeError(
            f"Could not build reach for snap cell ({snap_row}, {snap_col})"
        )

    crest = pick_crest_cell(reach, search_m=search_m, flat_threshold=flat_threshold)
    if pd.isna(crest.get("XS1_Profile")) or pd.isna(crest.get("XS2_Profile")):
        raise RuntimeError(
            f"No XS profile at flat-zone crest cell "
            f"({int(crest['Row'])}, {int(crest['Col'])})"
        )

    wse = float(crest["DEM_Elev"])
    width = walk_width(
        crest["XS1_Profile"],
        crest["XS2_Profile"],
        float(crest["Ordinate_Dist"]),
        wse,
    )
    if width <= 0:
        raise RuntimeError(
            f"Computed weir length is zero at flat-zone cell "
            f"({int(crest['Row'])}, {int(crest['Col'])})"
        )

    return {
        "weir_length": width,
        "snap_row": snap_row,
        "snap_col": snap_col,
        "snap_dist_m": snap_dist_m,
        "wse_used": wse,                # WSE at the crest cell (Method 2)
        "arc_wse": arc_wse_snap,         # ARC's hydraulic WSE at the snap (diagnostic)
        "ordinate_dist": float(crest["Ordinate_Dist"]),
        "crest_row": int(crest["Row"]),
        "crest_col": int(crest["Col"]),
        "crest_wse": wse,
        "crest_base": float(crest["BaseElev"]),
        "crest_x_m": float(crest["x"]),
        "reach": reach,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dam-lat", type=float, required=True)
    parser.add_argument("--dam-lon", type=float, required=True)
    parser.add_argument("--strm", type=Path, required=True)
    parser.add_argument("--vdt", type=Path, required=True)
    parser.add_argument("--xs", type=Path, required=True)
    parser.add_argument("--curve", type=Path, required=True)
    parser.add_argument("--flowline", type=Path, required=True)
    args = parser.parse_args()

    info = estimate_weir_length(
        dam_lat=args.dam_lat,
        dam_lon=args.dam_lon,
        strm_path=args.strm,
        vdt_path=args.vdt,
        xs_path=args.xs,
        curve_path=args.curve,
        flowline_path=args.flowline,
    )
    info.pop("reach", None)  # don't dump the DataFrame
    for k, v in info.items():
        print(f"{k}: {v}")
