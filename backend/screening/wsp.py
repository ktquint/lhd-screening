"""
Water Surface Profile (WSP) extraction from 3DEP DEM + NHDPlus flowlines.

Provides the geometric inputs for an energy-balance dam height estimate
without requiring ARC outputs.  The DEM is sampled at every pixel crossed
by the dam reach and its immediate upstream reach; the resulting profile
feeds the same flat-zone / slope-match node pickers used in the ARC pipeline.

Typical call sequence
---------------------
    vaa_df = nhd.nhdplus_vaa()
    upstream_comid = get_upstream_comid(dam_comid, vaa_df)

    profile = sample_dem_along_flowlines(
        dam_lat, dam_lon,
        dam_reach_geom, upstream_reach_geom,
        dem_path,
    )

    us_node = pick_upstream_node(profile)
    ds_node = pick_downstream_node(profile)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import rasterio
from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge

from screening.reach import DEFAULT_FLAT_THRESHOLD, DEFAULT_SEARCH_M_UP

_DEFAULT_SEARCH_M_DN = 500.0


# ---------------------------------------------------------------------------
# NHDPlus VAA upstream-reach lookup
# ---------------------------------------------------------------------------

def get_upstream_comid(comid: int, vaa_df: pd.DataFrame) -> Optional[int]:
    """Return the immediate upstream main-stem COMID via the hydroseq chain.

    Uses uphydroseq (the hydrosequence of the upstream main-stem reach).
    Returns None for headwaters (uphydroseq == 0) or missing entries.
    """
    row = vaa_df[vaa_df["comid"] == comid]
    if row.empty:
        return None
    up_hydroseq = int(row.iloc[0]["uphydroseq"])
    if up_hydroseq == 0:
        return None
    up_row = vaa_df[vaa_df["hydroseq"] == up_hydroseq]
    if up_row.empty:
        return None
    return int(up_row.iloc[0]["comid"])


# ---------------------------------------------------------------------------
# DEM sampling helpers
# ---------------------------------------------------------------------------

def _to_linestring(geom) -> Optional[LineString]:
    """Coerce any flowline geometry to a single LineString."""
    if isinstance(geom, MultiLineString):
        merged = linemerge(geom)
        if isinstance(merged, LineString):
            return merged
        # Still disconnected — take the longest sub-geometry
        return max(merged.geoms, key=lambda g: g.length)
    if isinstance(geom, LineString):
        return geom
    return None


def _cells_along_line(
    line: LineString,
    inv_transform,
    pixel_m: float,
) -> list[tuple[int, int, float]]:
    """Walk a projected LineString and return (row, col, chainage_m) for each
    unique DEM pixel the line crosses, in line order (first coord → last coord).

    Densifies at half-pixel spacing so no pixel is skipped on diagonal crossings.
    """
    cells: list[tuple[int, int, float]] = []
    seen: set[tuple[int, int]] = set()
    chainage = 0.0
    coords = list(line.coords)

    for i in range(len(coords) - 1):
        x0, y0 = coords[i][:2]
        x1, y1 = coords[i + 1][:2]
        seg_len = np.hypot(x1 - x0, y1 - y0)
        n_steps = max(int(np.ceil(seg_len / (pixel_m * 0.5))), 1)
        for j in range(n_steps):
            t = j / n_steps
            x = x0 + t * (x1 - x0)
            y = y0 + t * (y1 - y0)
            col_f, row_f = inv_transform * (x, y)
            rc = (int(row_f), int(col_f))
            if rc not in seen:
                seen.add(rc)
                cells.append((rc[0], rc[1], chainage + t * seg_len))
        chainage += seg_len

    # Final vertex
    x1, y1 = coords[-1][:2]
    col_f, row_f = inv_transform * (x1, y1)
    rc = (int(row_f), int(col_f))
    if rc not in seen:
        cells.append((rc[0], rc[1], chainage))

    return cells


def _sample_cell(band: np.ndarray, row: int, col: int,
                 nrows: int, ncols: int, nodata) -> float:
    if not (0 <= row < nrows and 0 <= col < ncols):
        return float("nan")
    val = float(band[row, col])
    if nodata is not None and val == nodata:
        return float("nan")
    return val


# ---------------------------------------------------------------------------
# Main profile builder
# ---------------------------------------------------------------------------

def sample_dem_along_flowlines(
    dam_lat: float,
    dam_lon: float,
    dam_reach_geom,
    upstream_reach_geom,
    dem_path: Path,
) -> pd.DataFrame:
    """Sample the DEM at every pixel crossed by the dam reach and its upstream reach.

    NHDPlus convention: first coordinate of each flowline is the upstream end,
    last coordinate is the downstream end.  The upstream reach's downstream end
    connects to the dam reach's upstream end.

    Parameters
    ----------
    dam_lat, dam_lon
        WGS-84 dam location — sets x=0 in the output profile.
    dam_reach_geom
        Shapely geometry of the dam COMID flowline (EPSG:4326).
    upstream_reach_geom
        Shapely geometry of the upstream COMID flowline (EPSG:4326), or None.
    dem_path
        Path to the trimmed/staged DEM raster (any projected CRS).

    Returns
    -------
    DataFrame sorted by x_m with columns:
        x_m    — signed distance from dam (m); negative = upstream
        elev_m — DEM elevation (m); NaN for nodata / out-of-extent cells
        row    — DEM row index
        col    — DEM column index
    """
    with rasterio.open(dem_path) as src:
        dem_crs = src.crs
        inv_tf  = ~src.transform
        pixel_m = abs(src.transform.a)
        nrows, ncols = src.shape
        nodata  = src.nodata
        band    = src.read(1)

    tf = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True)

    def _geom_to_cells(geom) -> list[tuple[int, int, float]]:
        ls = _to_linestring(geom)
        if ls is None:
            return []
        proj_coords = [tf.transform(x, y) for x, y in ls.coords]
        return _cells_along_line(LineString(proj_coords), inv_tf, pixel_m)

    dam_cells = _geom_to_cells(dam_reach_geom)
    if not dam_cells:
        raise RuntimeError("Dam reach produced no DEM cells — check flowline geometry.")

    up_cells = _geom_to_cells(upstream_reach_geom) if upstream_reach_geom is not None else []

    # x=0 at the dam cell closest to the dam location
    dam_x_proj, dam_y_proj = tf.transform(dam_lon, dam_lat)
    dam_col_f, dam_row_f = inv_tf * (dam_x_proj, dam_y_proj)
    dam_idx = int(np.argmin(
        [(r - dam_row_f) ** 2 + (c - dam_col_f) ** 2 for r, c, _ in dam_cells]
    ))
    dam_chain = dam_cells[dam_idx][2]

    records = []

    # Dam reach: x = chainage_along_reach − dam_chainage
    for row, col, chain in dam_cells:
        records.append({
            "x_m":   chain - dam_chain,
            "elev_m": _sample_cell(band, row, col, nrows, ncols, nodata),
            "row": row,
            "col": col,
        })

    # Upstream reach: its downstream end (last cell) sits at the same location
    # as the dam reach's upstream end (chainage = 0, x = -dam_chain).
    # For an upstream cell at chainage c_up (from 0=its-upstream to up_length=its-downstream):
    #   x = (c_up - up_length) - dam_chain
    if up_cells:
        up_length = up_cells[-1][2]
        for row, col, chain in up_cells:
            records.append({
                "x_m":   (chain - up_length) - dam_chain,
                "elev_m": _sample_cell(band, row, col, nrows, ncols, nodata),
                "row": row,
                "col": col,
            })

    df = pd.DataFrame(records).sort_values("x_m").reset_index(drop=True)

    # Orientation sanity check: upstream (x < 0) should be higher on average
    # than downstream (x > 0).  NHDPlus flowline coords are not consistently
    # ordered upstream→downstream, so flip x signs when the profile is inverted.
    # Use median (robust to nodata outliers) and require only 1 cell per side.
    valid = df[df["elev_m"].notna()]
    up_elev = valid.loc[valid["x_m"] < -10, "elev_m"]
    dn_elev = valid.loc[valid["x_m"] >  10, "elev_m"]
    if len(up_elev) >= 1 and len(dn_elev) >= 1:
        if up_elev.median() < dn_elev.median():
            df["x_m"] = -df["x_m"]
            df = df.sort_values("x_m").reset_index(drop=True)

    return df


# ---------------------------------------------------------------------------
# Node pickers (mirror pick_crest_cell / pick_downstream_cell from reach.py)
# ---------------------------------------------------------------------------

def _grad_abs(profile: pd.DataFrame) -> np.ndarray:
    """|dElev/dx| at each profile cell; NaN elevations are linearly interpolated.

    Handles duplicate x values (e.g. at reach junctions) the same way
    reach.py's _gradient does: collapse to unique x, compute, broadcast back.
    """
    x = profile["x_m"].values.astype(float)
    y = profile["elev_m"].values.astype(float)
    if len(x) < 2:
        return np.zeros(len(x))
    y_filled = pd.Series(y).interpolate(method="linear", limit_direction="both").values

    unique_x, inverse = np.unique(x, return_inverse=True)
    if len(unique_x) < 2:
        return np.zeros(len(x))
    if len(unique_x) == len(x):
        return np.abs(np.gradient(y_filled, x))
    _, first_idx = np.unique(x, return_index=True)
    grad_unique = np.gradient(y_filled[first_idx], unique_x)
    return np.abs(grad_unique[inverse])


def pick_upstream_node(
    profile: pd.DataFrame,
    search_m: float = DEFAULT_SEARCH_M_UP,
    flat_threshold: float = DEFAULT_FLAT_THRESHOLD,
) -> pd.Series:
    """Flat-zone upstream node picker (mirrors pick_crest_cell in reach.py).

    Walks upstream (x < 0) within search_m of the dam and returns the first
    cell where |dElev/dx| drops below flat_threshold (m/m), indicating the
    ponded pool surface.  Falls back to the flattest cell in the window, then
    the snap cell.
    """
    upstream = profile[(profile["x_m"] < 0) & (profile["x_m"] >= -search_m)].copy()
    if upstream.empty:
        return profile.iloc[(profile["x_m"].abs()).argsort()[:1]].iloc[0]

    grad = _grad_abs(profile)
    upstream = upstream.assign(grad_abs=grad[upstream.index])
    upstream = upstream.sort_values("x_m", ascending=False)  # closest-to-dam first
    for _, row in upstream.iterrows():
        if row["grad_abs"] < flat_threshold:
            return row
    return upstream.loc[upstream["grad_abs"].idxmin()]


_DEFAULT_TARGET_M_DN = 75.0   # fallback fixed distance when no reach slope available
_MIN_DN_M = 10.0              # never pick a node closer than this to the dam


def _centered_slopes(profile: pd.DataFrame) -> np.ndarray:
    """Centered finite-difference slope (m/m) at each cell, using adjacent cells.

    slope[i] = (elev[i-1] - elev[i+1]) / (x[i+1] - x[i-1])

    Positive = elevation decreasing downstream (normal channel convention).
    NaN elevations are linearly interpolated before differencing.
    Edge cells use one-sided differences.
    """
    x = profile["x_m"].values.astype(float)
    y = profile["elev_m"].values.astype(float)
    y = pd.Series(y).interpolate(method="linear", limit_direction="both").values
    slopes = np.empty(len(x))
    for i in range(len(x)):
        x_prev = x[i - 1] if i > 0 else x[i]
        x_next = x[i + 1] if i < len(x) - 1 else x[i]
        y_prev = y[i - 1] if i > 0 else y[i]
        y_next = y[i + 1] if i < len(x) - 1 else y[i]
        dx = x_next - x_prev
        slopes[i] = (y_prev - y_next) / dx if dx != 0 else 0.0
    return slopes


def pick_downstream_node(
    profile: pd.DataFrame,
    reach_slope: Optional[float] = None,
    search_m: float = _DEFAULT_SEARCH_M_DN,
    min_dn_m: float = _MIN_DN_M,
    fallback_m: float = _DEFAULT_TARGET_M_DN,
) -> pd.Series:
    """Downstream node picker: find where local DEM slope recovers to reach slope.

    Computes a centered finite-difference slope at each downstream cell and
    returns the cell whose local slope is closest to ``reach_slope`` (NHDPlus
    VAA ``slope`` for the dam COMID).  This identifies where the water surface
    has recovered from the hydraulic jump back to normal channel gradient.

    A minimum distance of ``min_dn_m`` is enforced to avoid the turbulent
    plunge pool immediately below the crest.

    Falls back to the cell nearest ``fallback_m`` downstream when:
      - ``reach_slope`` is None, ≤ 0, or a sentinel (< -9990)
      - no valid downstream cells exist within ``search_m``
    """
    downstream = profile[
        (profile["x_m"] >= min_dn_m) & (profile["x_m"] <= search_m)
    ].dropna(subset=["elev_m"]).copy()

    if downstream.empty:
        downstream = profile[profile["x_m"] >= min_dn_m].dropna(subset=["elev_m"]).copy()
    if downstream.empty:
        return profile.iloc[(profile["x_m"].abs()).argsort()[:1]].iloc[0]

    if reach_slope is None or reach_slope <= 0 or reach_slope < -9990:
        return downstream.iloc[
            (downstream["x_m"] - fallback_m).abs().argsort()[:1]
        ].iloc[0]

    all_slopes = _centered_slopes(profile)
    downstream = downstream.assign(local_slope=all_slopes[downstream.index])
    downstream = downstream.assign(
        slope_err=(downstream["local_slope"] - reach_slope).abs()
    )
    return downstream.loc[downstream["slope_err"].idxmin()]
