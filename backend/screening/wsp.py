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

    return (
        pd.DataFrame(records)
        .sort_values("x_m")
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# Node pickers (mirror pick_crest_cell / pick_downstream_cell from reach.py)
# ---------------------------------------------------------------------------

def _grad_abs(profile: pd.DataFrame) -> np.ndarray:
    """|dElev/dx| at each profile cell; NaN elevations are linearly interpolated."""
    x = profile["x_m"].values.astype(float)
    y = profile["elev_m"].values.astype(float)
    if len(x) < 2:
        return np.zeros(len(x))
    y_filled = pd.Series(y).interpolate(method="linear", limit_direction="both").values
    return np.abs(np.gradient(y_filled, x))


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


def pick_downstream_node(
    profile: pd.DataFrame,
    search_m: float = _DEFAULT_SEARCH_M_DN,
) -> pd.Series:
    """Slope-match downstream node picker (mirrors pick_downstream_cell in reach.py).

    Reference slope = median |dElev/dx| of cells in [0, search_m] downstream.
    Returns the downstream cell whose gradient is closest (relative) to that
    reference — the point where normal-depth channel conditions have recovered.
    Falls back to the first downstream cell.
    """
    downstream = profile[profile["x_m"] > 0].copy()
    if downstream.empty:
        return profile.iloc[(profile["x_m"].abs()).argsort()[:1]].iloc[0]

    grad = _grad_abs(profile)
    downstream = downstream.assign(grad_abs=grad[downstream.index])

    ref_band = downstream[downstream["x_m"] <= search_m]
    ref_slope = float(ref_band["grad_abs"].median()) if not ref_band.empty else 0.0
    if ref_slope <= 0:
        return downstream.sort_values("x_m").iloc[0]

    downstream["slope_diff"] = (downstream["grad_abs"] - ref_slope).abs() / ref_slope
    return downstream.loc[downstream["slope_diff"].idxmin()]
