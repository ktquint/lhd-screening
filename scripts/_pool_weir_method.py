"""
Pool-mask + crest-tracing weir-length estimator.

Lives in scripts/ for now so we can validate it before promoting it into the
main pipeline. Imported by compare_weir_methods.py.

Physical idea
-------------
A lidar DEM captures a dam as three regions:
  1. Pool (upstream): a large patch at the pool elevation — lidar reflects
     off the water surface so DEM ≈ WSE.
  2. Downstream channel: pixels well below WSE.
  3. Crest: a thin line of pixels at WSE separating the two.

The weir crest is the downstream-side boundary of the pool. Its planform
shape (straight / skewed / curved) is preserved by the DEM, so following
that boundary measures the true weir length regardless of orientation.

Algorithm
---------
1. Open DEM, project dam point and flowline into DEM CRS.
2. Pick the flowline geometry closest to the dam point.
3. Densify that flowline and sample DEM along it.
4. Find the dam-induced "step" in the elevation profile within ±search_window
   of the dam point; WSE = mean of the few samples immediately upstream.
5. Threshold DEM to a pool mask (|DEM − WSE| ≤ pool_band).
6. Connected-component label; pick the component nearest the step.
7. Detect crest pixels: pool pixels with at least one 8-neighbor below
   WSE − downstream_drop.
8. Keep only crest pixels on the downstream side of the step (positive dot
   with the flow-direction tangent) and within max_crest_radius.
9. Order them along the axis perpendicular to flow and sum the segment
   lengths to get the weir length. Drop any contiguous run smaller than
   the run closest to the step (filters out unrelated boundary fragments).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import rasterio
import geopandas as gpd
from pyproj import Transformer
from shapely.geometry import Point
from shapely.ops import linemerge, unary_union
from scipy.ndimage import grey_dilation, label as _ndimage_label


def _fail(reason: str, **extra) -> dict:
    out = {
        "weir_length": None,
        "wse": None,
        "drop_m": None,
        "step_row": None,
        "step_col": None,
        "pool_size_px": None,
        "n_crest_pixels": None,
        "fallback": True,
        "fallback_reason": reason,
    }
    out.update(extra)
    return out


def pool_weir_length(
    dam_lat: float,
    dam_lon: float,
    dem_path: Path | str,
    flowline_path: Path | str,
    *,
    pool_band_m: float = 0.5,
    downstream_drop_m: float = 1.0,
    search_window_m: float = 150.0,
    profile_step_m: float = 1.0,
    min_pool_pixels: int = 30,
    min_step_m: float = 0.5,
    max_crest_radius_m: float = 200.0,
    wse_avg_samples: int = 5,
) -> dict:
    """
    Estimate weir length by detecting the dam-induced pool→downstream
    boundary in the DEM.

    Returns a dict with `weir_length` (or None on failure), `fallback`
    boolean, `fallback_reason`, and a handful of diagnostics:
        wse, drop_m, step_row, step_col, pool_size_px, n_crest_pixels
    """
    dem_path = Path(dem_path)
    flowline_path = Path(flowline_path)

    # 1. Open DEM ---------------------------------------------------------
    with rasterio.open(dem_path) as src:
        dem = src.read(1).astype(np.float64)
        dem_transform = src.transform
        dem_crs = src.crs
        nodata = src.nodata
    if nodata is not None:
        dem[dem == nodata] = np.nan
    rows, cols = dem.shape
    px_x = abs(dem_transform.a)
    px_y = abs(dem_transform.e)
    inv = ~dem_transform

    # 2. Project dam point to DEM CRS ------------------------------------
    to_dem = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True)
    dam_x, dam_y = to_dem.transform(dam_lon, dam_lat)
    dam_col_f, dam_row_f = inv * (dam_x, dam_y)
    if not (0 <= dam_row_f < rows and 0 <= dam_col_f < cols):
        return _fail("dam projects outside DEM")

    # 3. Load flowline, merge all features, pick closest component -------
    fl = gpd.read_file(flowline_path)
    if fl.crs is None:
        fl = fl.set_crs(epsg=4326)
    fl = fl.to_crs(dem_crs)
    geoms = [g for g in fl.geometry if g is not None and not g.is_empty
             and g.geom_type in ("LineString", "MultiLineString")]
    if not geoms:
        return _fail("no flowline geometry")

    dam_pt = Point(dam_x, dam_y)
    # Merge all features into one continuous line where possible.
    # This avoids treating a short connector stub as the whole flowline.
    merged = linemerge(unary_union(geoms))
    if merged.geom_type == "MultiLineString":
        # Still disconnected — pick the component that passes closest to the dam
        line = min(merged.geoms, key=lambda g: g.distance(dam_pt))
    else:
        line = merged

    line_len = line.length
    if line_len < 2 * profile_step_m:
        return _fail("flowline too short")
    dam_dist_along = float(line.project(dam_pt))

    # 4. Densify line + sample DEM ---------------------------------------
    distances = np.arange(0.0, line_len, profile_step_m)
    sample_x = np.empty(len(distances))
    sample_y = np.empty(len(distances))
    for i, d in enumerate(distances):
        p = line.interpolate(d)
        sample_x[i] = p.x
        sample_y[i] = p.y
    # Pixel coords for each sample
    sample_col_f = (sample_x - dem_transform.c) / dem_transform.a
    sample_row_f = (sample_y - dem_transform.f) / dem_transform.e
    sample_row_i = np.clip(np.round(sample_row_f).astype(int), 0, rows - 1)
    sample_col_i = np.clip(np.round(sample_col_f).astype(int), 0, cols - 1)
    elevs = dem[sample_row_i, sample_col_i]
    in_bounds = (sample_row_f >= 0) & (sample_row_f < rows) & \
                (sample_col_f >= 0) & (sample_col_f < cols)
    elevs[~in_bounds] = np.nan

    # 5. Locate dam-induced step in profile ------------------------------
    # Use a rolling window sum (~10 m) so a drop spread across several
    # pixels still registers as a single step, rather than relying on
    # one 1-m sample exceeding the threshold.
    dz = np.diff(elevs)  # dz[i] = elevs[i+1] - elevs[i]; negative = drop downstream
    dz_dist = (distances[:-1] + distances[1:]) * 0.5
    win_samples = max(1, int(10.0 / profile_step_m))
    dz_filled = np.where(np.isfinite(dz), dz, 0.0)
    if len(dz_filled) >= win_samples:
        rolled_drop = np.convolve(dz_filled, np.ones(win_samples), mode="valid")
        # distance at the centre of each rolling window
        centre_idx = np.minimum(
            np.arange(len(rolled_drop)) + win_samples // 2, len(dz_dist) - 1
        )
        roll_dist = dz_dist[centre_idx]
    else:
        rolled_drop = np.array([dz_filled.sum()])
        roll_dist = np.array([dz_dist[len(dz_dist) // 2]])

    roll_window_mask = np.abs(roll_dist - dam_dist_along) <= search_window_m
    if not np.any(roll_window_mask):
        return _fail("no finite dz in flowline window")
    masked_rolled = np.where(roll_window_mask, rolled_drop, np.inf)
    best_roll_idx = int(np.argmin(masked_rolled))
    drop = float(rolled_drop[best_roll_idx])
    if drop > -min_step_m:
        return _fail(f"flowline step too small ({drop:.2f} m)")

    # Step position = centre sample of the best rolling window
    best_idx = min(best_roll_idx + win_samples // 2, len(sample_x) - 1)

    # WSE = mean of N samples immediately upstream of the window start
    up0 = max(0, best_roll_idx - wse_avg_samples + 1)
    wse_window = elevs[up0:best_roll_idx + 1]
    wse_window = wse_window[np.isfinite(wse_window)]
    if wse_window.size == 0:
        return _fail("no finite upstream samples")
    wse = float(np.mean(wse_window))

    # Step location in pixel coords + flow direction
    step_x = sample_x[best_idx]
    step_y = sample_y[best_idx]
    step_row_f = sample_row_f[best_idx]
    step_col_f = sample_col_f[best_idx]

    # Flow direction at step (downstream-pointing tangent)
    if best_idx + 1 < len(sample_x):
        fdx = sample_x[best_idx + 1] - step_x
        fdy = sample_y[best_idx + 1] - step_y
    else:
        fdx = step_x - sample_x[best_idx - 1]
        fdy = step_y - sample_y[best_idx - 1]
    fmag = float(np.hypot(fdx, fdy))
    if fmag == 0:
        return _fail("degenerate flow vector at step")
    flow_x = fdx / fmag
    flow_y = fdy / fmag

    # 6. Pool mask + connected components --------------------------------
    pool_mask = np.isfinite(dem) & (dem >= wse - pool_band_m) & (dem <= wse + pool_band_m)
    if not np.any(pool_mask):
        return _fail("empty pool mask")
    labels, _ = _ndimage_label(pool_mask, structure=np.ones((3, 3)))

    step_row_i = int(round(step_row_f))
    step_col_i = int(round(step_col_f))
    comp_label = 0
    if 0 <= step_row_i < rows and 0 <= step_col_i < cols:
        comp_label = int(labels[step_row_i, step_col_i])
    if comp_label == 0:
        # nearest labeled pixel
        rr, cc = np.where(labels > 0)
        d2 = ((rr - step_row_f) * px_y) ** 2 + ((cc - step_col_f) * px_x) ** 2
        comp_label = int(labels[rr[int(np.argmin(d2))], cc[int(np.argmin(d2))]])
    pool = labels == comp_label
    pool_size = int(np.sum(pool))
    if pool_size < min_pool_pixels:
        return _fail(f"pool too small ({pool_size} px < {min_pool_pixels})")

    # 7. Crest pixels: pool pixels with any neighbor below WSE − drop ----
    below_thresh = (dem < wse - downstream_drop_m) & np.isfinite(dem)
    # Expand the "below threshold" mask by one pixel in each direction; the
    # intersection with `pool` gives every pool pixel that has at least one
    # such 8-neighbor.
    expanded = grey_dilation(below_thresh.astype(np.uint8), size=3).astype(bool)
    crest_mask = pool & expanded
    crest_rc = np.argwhere(crest_mask)
    if crest_rc.size == 0:
        return _fail("no crest pixels detected")

    # 8. Filter to downstream side + within max_crest_radius -------------
    crest_world_x = dem_transform.c + (crest_rc[:, 1] + 0.5) * dem_transform.a
    crest_world_y = dem_transform.f + (crest_rc[:, 0] + 0.5) * dem_transform.e
    vx = crest_world_x - step_x
    vy = crest_world_y - step_y
    dist_from_step = np.hypot(vx, vy)
    dot = vx * flow_x + vy * flow_y
    # Allow ~1 pixel of slack so the step pixel itself counts
    keep = (dot >= -max(px_x, px_y)) & (dist_from_step <= max_crest_radius_m)
    if not np.any(keep):
        return _fail("no crest pixels on downstream side")
    crest_world_x = crest_world_x[keep]
    crest_world_y = crest_world_y[keep]

    # 9. Order along perpendicular axis + measure ------------------------
    perp_x = -flow_y
    perp_y = flow_x
    proj = (crest_world_x - step_x) * perp_x + (crest_world_y - step_y) * perp_y
    order = np.argsort(proj)
    crest_world_x = crest_world_x[order]
    crest_world_y = crest_world_y[order]

    if len(crest_world_x) < 2:
        return _fail("only one crest pixel after filtering")

    seg_lengths = np.hypot(np.diff(crest_world_x), np.diff(crest_world_y))
    # Break into contiguous runs (gap > ~3 px = likely jumped onto an
    # unrelated boundary); keep the run nearest the step.
    max_gap = 3.0 * max(px_x, px_y)
    gap_idxs = np.where(seg_lengths > max_gap)[0]
    if gap_idxs.size > 0:
        runs = np.split(np.arange(len(crest_world_x)), gap_idxs + 1)
        best_run = None
        best_proximity = np.inf
        for run in runs:
            if len(run) < 2:
                continue
            rx = crest_world_x[run]
            ry = crest_world_y[run]
            prox = float(np.min(np.hypot(rx - step_x, ry - step_y)))
            if prox < best_proximity:
                best_proximity = prox
                best_run = run
        if best_run is None:
            return _fail("no contiguous crest run", n_crest_pixels=int(len(crest_world_x)))
        crest_world_x = crest_world_x[best_run]
        crest_world_y = crest_world_y[best_run]
        seg_lengths = np.hypot(np.diff(crest_world_x), np.diff(crest_world_y))

    weir_length = float(np.sum(seg_lengths))

    return {
        "weir_length": weir_length,
        "wse": wse,
        "drop_m": -drop,
        "step_row": int(round(step_row_f)),
        "step_col": int(round(step_col_f)),
        "pool_size_px": pool_size,
        "n_crest_pixels": int(len(crest_world_x)),
        "fallback": False,
        "fallback_reason": "",
    }
