"""
Shared reach-building + crest/tailwater cell pickers used by the production
screening pipeline and the validation scripts in /scripts.

The "reach" is the single NHDPlus COMID that passes through the dam.  For
that reach we build a DataFrame indexed upstream→downstream with a signed
distance column ``x`` (m, x=0 at the snap cell), plus the per-cell
``BaseElev``, ``DEM_Elev``, ``Slope`` (from ARC Curve.csv) and the wetted
cross-section profiles (from ARC XS.txt).

Two cell pickers implement the methods evaluated in the 2026 ASDSO paper:

* :func:`pick_crest_cell` — Method 2 (flat-zone) crest sampler.  Walks
  upstream from the snap and selects the first cell where the DEM_Elev
  gradient drops below ``flat_threshold``.

* :func:`pick_downstream_cell` — Method B (ARC-slope flat zone) tailwater
  sampler.  Walks downstream until ARC's per-cell ``Slope`` reconverges to
  the reference channel slope (median ``Slope`` over ``[ref_min, ref_max]``
  m downstream).

These were the methods recommended for the production rollout.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional, Tuple

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from pyproj import Transformer
from shapely.ops import linemerge

# Sentinel used by ARC for "no data" in XS profiles.
INVALID_THRESHOLD = -1e5

# Defaults match the 2026 ASDSO paper.
DEFAULT_SEARCH_M_UP = 50.0
DEFAULT_SEARCH_M_DN = 500.0
DEFAULT_FLAT_THRESHOLD = 0.02
DEFAULT_REF_SLOPE_MIN = 100.0
DEFAULT_REF_SLOPE_MAX = 500.0
DEFAULT_SLOPE_TOL = 0.20


# ---------------------------------------------------------------------------
# Profile / width helpers
# ---------------------------------------------------------------------------

def parse_profile(profile) -> list:
    """Coerce an ARC XS profile cell value into a list of elevations."""
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


def walk_width(xs1, xs2, ordinate_dist: float, wse: float) -> float:
    """Wetted width (m) for a cell, summing both half-profiles outward from
    the channel centerline until a dry cell is hit."""
    def count_wet(profile) -> int:
        n = 0
        for elev in profile:
            if wse >= elev > INVALID_THRESHOLD:
                n += 1
            else:
                break
        return n

    return (
        count_wet(parse_profile(xs1)) + count_wet(parse_profile(xs2))
    ) * float(ordinate_dist)


# ---------------------------------------------------------------------------
# Reach construction
# ---------------------------------------------------------------------------

def _upstream_rowcol(
    snap_comid: int, flowline_path: Path, strm_path: Path
) -> Tuple[Optional[Tuple[float, float]], Optional[float]]:
    """Return the (row, col) of the NHDPlus reach's first coordinate
    (canonically the upstream end) in the stream-raster grid, plus the
    raster pixel size in metres.  Returns (None, None) on failure."""
    gdf = gpd.read_file(flowline_path)
    match = gdf[gdf["nhdplusid"].astype(int) == snap_comid]
    if match.empty:
        return None, None
    geom = match.iloc[0].geometry
    if geom.geom_type == "MultiLineString":
        geom = linemerge(geom)
    us_lon, us_lat = geom.coords[0][:2]
    with rasterio.open(strm_path) as src:
        tf = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        x, y = tf.transform(us_lon, us_lat)
        col_f, row_f = (~src.transform) * (x, y)
        px_m = abs(src.transform.a)
    return (row_f, col_f), px_m


def build_reach(
    snap_row: int,
    snap_col: int,
    curve_path: Path,
    xs_path: Path,
    flowline_path: Path,
    strm_path: Path,
    search_m: float = DEFAULT_SEARCH_M_UP,
) -> Optional[pd.DataFrame]:
    """Build the upstream→downstream reach DataFrame for the COMID that
    contains the snapped dam cell.

    Returns a DataFrame with columns ``Row, Col, COMID, BaseElev, DEM_Elev,
    Slope, XS1_Profile, XS2_Profile, Ordinate_Dist, x`` (or ``None`` if the
    snap cell is not in the curve table).  ``x`` is metres downstream of the
    snap cell (negative = upstream).
    """
    curve = pd.read_csv(curve_path)
    curve.columns = [c.strip() for c in curve.columns]

    xs = pd.read_csv(xs_path, sep="\t")
    xs.columns = [c.strip() for c in xs.columns]

    snap_mask = (curve["Row"] == snap_row) & (curve["Col"] == snap_col)
    if not snap_mask.any():
        return None
    snap_comid = int(curve.loc[snap_mask, "COMID"].iloc[0])

    reach_c = curve[curve["COMID"] == snap_comid].copy().reset_index(drop=True)
    reach_x = xs[xs["COMID"] == snap_comid].copy()

    merged = reach_c.merge(
        reach_x[["COMID", "Row", "Col",
                 "XS1_Profile", "XS2_Profile", "Ordinate_Dist"]],
        on=["COMID", "Row", "Col"], how="left",
    ).reset_index(drop=True)

    snap_idx = merged[(merged["Row"] == snap_row)
                      & (merged["Col"] == snap_col)].index
    if snap_idx.empty:
        return None
    snap_pos = int(snap_idx[0])

    # Orient using the NHDPlus flowline (first coord = upstream end).
    us_rc, px_m = _upstream_rowcol(snap_comid, flowline_path, strm_path)
    if us_rc is not None:
        us_row_f, us_col_f = us_rc
        dist_sq = ((merged["Row"].values - us_row_f) ** 2
                   + (merged["Col"].values - us_col_f) ** 2)
        if dist_sq[-1] < dist_sq[0]:
            merged = merged.iloc[::-1].reset_index(drop=True)
            snap_pos = len(merged) - 1 - snap_pos
    else:
        px_m = 1.0
        base = merged["BaseElev"].values
        if float(np.polyfit(np.arange(len(base)), base, 1)[0]) > 0:
            merged = merged.iloc[::-1].reset_index(drop=True)
            snap_pos = len(merged) - 1 - snap_pos

    # Cumulative chainage from raster row/col steps.
    dr = merged["Row"].diff().fillna(0).values
    dc = merged["Col"].diff().fillna(0).values
    step = np.sqrt(dr ** 2 + dc ** 2) * (px_m or 1.0)
    cum = np.cumsum(step)
    merged["x"] = cum - cum[snap_pos]

    # Sanity: within the search window, downstream cells should sit lower
    # than upstream cells.  If not, our orientation was wrong.
    x_arr = merged["x"].values
    elev = merged["DEM_Elev"].values
    snap_elev = elev[snap_pos]
    in_win = np.abs(x_arr) <= search_m
    if in_win.any():
        xw, yw = x_arr[in_win], elev[in_win] - snap_elev
        up_m, dn_m = xw < -1, xw > 1
        if up_m.any() and dn_m.any() and yw[up_m].mean() < yw[dn_m].mean():
            merged["x"] = -merged["x"]

    return merged


# ---------------------------------------------------------------------------
# Cell pickers
# ---------------------------------------------------------------------------

def _gradient(reach: pd.DataFrame) -> np.ndarray:
    """Per-cell DEM_Elev gradient (m/m) centred on each cell."""
    return np.gradient(reach["DEM_Elev"].values, reach["x"].values)


def pick_crest_cell(
    reach: pd.DataFrame,
    search_m: float = DEFAULT_SEARCH_M_UP,
    flat_threshold: float = DEFAULT_FLAT_THRESHOLD,
) -> pd.Series:
    """Method 2 (flat zone) crest sampler.

    Walks upstream from the snap cell within ``search_m`` and returns the
    first cell whose |dDEM/dx| drops below ``flat_threshold``.  Falls back to
    the flattest cell in the window, then to the snap cell.

    Returns the reach DataFrame row for the chosen cell.
    """
    upstream = reach[(reach["x"] < 0) & (reach["x"] >= -search_m)].copy()
    if not upstream.empty:
        grad = _gradient(reach)
        upstream = upstream.assign(grad_abs=np.abs(grad[upstream.index]))
        upstream = upstream.sort_values("x", ascending=False)  # closest to dam first
        for _, row in upstream.iterrows():
            if row["grad_abs"] < flat_threshold:
                return row
        return upstream.loc[upstream["grad_abs"].idxmin()]

    # No upstream cells in window — fall back to snap cell.
    snap = reach.iloc[(reach["x"].abs()).argsort()[:1]]
    return snap.iloc[0]


def pick_downstream_cell(
    reach: pd.DataFrame,
    ref_min: float = DEFAULT_REF_SLOPE_MIN,
    ref_max: float = DEFAULT_REF_SLOPE_MAX,
    slope_tol: float = DEFAULT_SLOPE_TOL,
) -> pd.Series:
    """Method B (ARC-slope flat zone) tailwater sampler.

    Reference slope = median ARC ``Slope`` of cells ``[ref_min, ref_max]``
    metres downstream.  Walk downstream from the snap and return the first
    cell whose ``Slope`` lies within ``slope_tol`` of that reference.  Falls
    back to the cell with the closest slope, then to the first downstream
    cell.

    Returns the reach DataFrame row for the chosen cell.
    """
    def _first_ds_cell() -> pd.Series:
        ds = reach[reach["x"] > 0].sort_values("x")
        if not ds.empty:
            return ds.iloc[0]
        return reach.iloc[(reach["x"].abs()).argsort()[:1]].iloc[0]

    if "Slope" not in reach.columns:
        return _first_ds_cell()

    ref_band = reach[(reach["x"] >= ref_min) & (reach["x"] <= ref_max)]
    if ref_band.empty:
        ref_band = reach[reach["x"] > 20]
    if ref_band.empty:
        return _first_ds_cell()

    ref_slope = float(ref_band["Slope"].median())
    if ref_slope <= 0:
        return _first_ds_cell()

    downstream = reach[reach["x"] > 0].sort_values("x")
    if downstream.empty:
        return _first_ds_cell()

    for _, row in downstream.iterrows():
        if abs(float(row["Slope"]) - ref_slope) / ref_slope < slope_tol:
            return row

    # No convergence — use the cell whose slope is closest to reference.
    downstream = downstream.copy()
    downstream["slope_diff"] = (downstream["Slope"] - ref_slope).abs() / ref_slope
    return downstream.loc[downstream["slope_diff"].idxmin()]
