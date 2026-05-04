"""
Estimate a dam's weir length from ARC's outputs and the lidar-captured DEM.

The dam crest length is approximated as the lateral extent of cells whose
elevation is at or below the lidar-captured water surface (DEM_Elev) at the
stream pixel nearest the dam's lat/lon. This works because lidar reflects
off impounded water at the pool elevation, and the pool is bounded laterally
by the dam abutments.

Validated against ~225 dams across two flowline/streamflow source pairs:
    median ratio (estimated / known) = 0.91 - 0.93
    mean   ratio                     = 0.98 - 1.01
    std    ratio                     = 0.75 - 0.87
"""
from pathlib import Path
import ast
import numpy as np
import pandas as pd
import rasterio
from pyproj import Transformer

INVALID_THRESHOLD = -1e5


def _parse_profile(profile):
    if isinstance(profile, str):
        try:
            return ast.literal_eval(profile.strip())
        except (ValueError, SyntaxError):
            return []
    if isinstance(profile, np.ndarray):
        return profile.tolist()
    if isinstance(profile, list):
        return profile
    if profile is None or (isinstance(profile, float) and np.isnan(profile)):
        return []
    return [profile]


def _walk_width(xs1, xs2, ord_dist: float, wse: float) -> float:
    """Count cells outward from center where INVALID < elev <= WSE."""
    def count_wet(profile):
        n = 0
        for elev in profile:
            if wse >= elev > INVALID_THRESHOLD:
                n += 1
            else:
                break
        return n
    return (count_wet(_parse_profile(xs1)) + count_wet(_parse_profile(xs2))) * float(ord_dist)


def estimate_weir_length(
    dam_lat: float,
    dam_lon: float,
    strm_path: Path,
    vdt_path: Path,
    xs_path: Path,
    curve_path: Path,
) -> dict:
    """
    Snap the dam point to the nearest VDT cell and compute the lidar-water-
    surface width there. Uses DEM_Elev (from Curve.csv) as the water level,
    not ARC's hydraulic Elev — see module docstring.

    Returns a dict with the derived weir_length plus diagnostic fields.
    Raises if any required artifact is missing or no width can be computed.
    """
    with rasterio.open(strm_path) as src:
        transformer = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        x, y = transformer.transform(dam_lon, dam_lat)
        rows, cols = src.shape
        inv = ~src.transform
        col_f, row_f = inv * (x, y)
        if not (0 <= row_f < rows and 0 <= col_f < cols):
            raise ValueError(f"Dam ({dam_lat}, {dam_lon}) projects outside stream raster {strm_path}")
        px_x = abs(src.transform.a)
        px_y = abs(src.transform.e)

    vdt = pd.read_csv(vdt_path)
    vdt.columns = [c.strip() for c in vdt.columns]
    if vdt.empty:
        raise RuntimeError(f"VDT.txt is empty: {vdt_path}")

    curve = pd.read_csv(curve_path)
    curve.columns = [c.strip() for c in curve.columns]
    vdt = vdt.merge(curve[["COMID", "Row", "Col", "DEM_Elev"]],
                    on=["COMID", "Row", "Col"], how="left")
    if vdt["DEM_Elev"].isna().all():
        raise RuntimeError("DEM_Elev missing for every VDT row after Curve.csv merge")

    dr = (vdt["Row"].astype(float).values - row_f) * px_y
    dc = (vdt["Col"].astype(float).values - col_f) * px_x
    d2 = dr ** 2 + dc ** 2
    i = int(np.argmin(d2))
    snap = vdt.iloc[i]
    snap_dist_m = float(np.sqrt(d2[i]))

    if pd.isna(snap.get("DEM_Elev")):
        raise RuntimeError(f"DEM_Elev missing at snapped cell ({int(snap['Row'])}, {int(snap['Col'])})")

    xs = pd.read_csv(xs_path, sep="\t")
    xs.columns = [c.strip() for c in xs.columns]
    xs_match = xs[(xs["Row"] == int(snap["Row"])) & (xs["Col"] == int(snap["Col"]))]
    if xs_match.empty:
        raise RuntimeError(f"No XS row for snapped cell ({int(snap['Row'])}, {int(snap['Col'])})")
    xs_row = xs_match.iloc[0]

    wse = float(snap["DEM_Elev"])
    width = _walk_width(
        xs_row["XS1_Profile"],
        xs_row["XS2_Profile"],
        float(xs_row["Ordinate_Dist"]),
        wse,
    )
    if width <= 0:
        raise RuntimeError(f"Computed weir length is zero at ({int(snap['Row'])}, {int(snap['Col'])})")

    return {
        "weir_length": width,
        "snap_row": int(snap["Row"]),
        "snap_col": int(snap["Col"]),
        "snap_dist_m": snap_dist_m,
        "wse_used": wse,
        "arc_wse": float(snap["Elev"]) if pd.notna(snap.get("Elev")) else None,
        "ordinate_dist": float(xs_row["Ordinate_Dist"]),
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lon", type=float, required=True)
    parser.add_argument("--strm", required=True, type=Path,
                        help="Path to *_clean.tif stream raster")
    parser.add_argument("--vdt", required=True, type=Path, help="Path to *_VDT.txt")
    parser.add_argument("--xs", required=True, type=Path, help="Path to *_XS.txt")
    parser.add_argument("--curve", required=True, type=Path, help="Path to *_Curve.csv")
    args = parser.parse_args()
    result = estimate_weir_length(args.lat, args.lon, args.strm, args.vdt, args.xs, args.curve)
    for k, v in result.items():
        print(f"  {k}: {v}")
