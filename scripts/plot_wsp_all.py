"""
Plot water-surface profiles for all processed dams, centered on the dam cell.

x = 0  →  dam location (snap_row / snap_col from analysis_summary.json)
x < 0  →  upstream
x > 0  →  downstream

Direction is determined from the NHD Plus flowline geometry: NHD Plus
guarantees that LineString coordinates run upstream → downstream, so coords[0]
is the upstream end of the reach.  We project that point into the raster's
pixel space, then compare its distance to the FIRST vs LAST row in the Curve
CSV to decide whether the CSV is stored forward or reversed.

The window is asymmetric by design: the reach naturally extends further on
one side of the dam than the other, and clipping both sides to the same value
would hide that.

y is normalised by subtracting DEM_Elev at the snap cell so every profile
sits at 0 at the dam location.

Usage:
    python scripts/plot_wsp_all.py
"""
from __future__ import annotations
import json
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from pyproj import Transformer
from shapely.ops import linemerge
import matplotlib.pyplot as plt

# ── paths ──────────────────────────────────────────────────────────────────
STAGING  = Path("/Users/kennyquintana/Developer/dam geometry")
RESULTS  = STAGING / "RESULTS"
OUT_PNG  = Path("/Users/kennyquintana/Developer/lhd-screening/output/wsp_all.png")

# ── tuning ──────────────────────────────────────────────────────────────────
MAX_DIST = 50.0      # metres clipped on each side

# ───────────────────────────────────────────────────────────────────────────

def _get_pixel_size(strm_path: Path) -> float:
    """Return the pixel size in metres (square pixels assumed)."""
    with rasterio.open(strm_path) as src:
        return abs(src.transform.a)   # UTM rasters: ~1 m/pixel


def _upstream_rowcol(
    snap_comid: int, strm_dir: Path
) -> tuple[tuple[float, float], float] | tuple[None, None]:
    """
    Return ((row_f, col_f), pixel_size_m) for the upstream end of the snap
    COMID's NHD flowline in the STRM raster's pixel coordinate space.
    """
    gpkgs = list(strm_dir.glob("nhd_flowline_*.gpkg"))
    strms = list(strm_dir.glob("nhd_*_clean.tif"))
    if not gpkgs or not strms:
        return None, None

    gdf = gpd.read_file(gpkgs[0])
    match = gdf[gdf["nhdplusid"].astype(int) == snap_comid]
    if match.empty:
        return None, None

    geom = match.iloc[0].geometry
    if geom.geom_type == "MultiLineString":
        geom = linemerge(geom)

    # NHD Plus LineString: coords[0] = upstream end
    us_lon, us_lat = geom.coords[0][:2]

    with rasterio.open(strms[0]) as src:
        tf = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        x, y = tf.transform(us_lon, us_lat)
        col_f, row_f = (~src.transform) * (x, y)
        px_m = abs(src.transform.a)

    return (row_f, col_f), px_m


def load_profile(dam_dir: Path) -> dict | None:
    dam_id = dam_dir.name

    summary_path = dam_dir / "analysis_summary.json"
    curve_path   = dam_dir / "VDT" / f"{dam_id}_Curve.csv"
    if not summary_path.exists() or not curve_path.exists():
        return None

    with open(summary_path) as f:
        summary = json.load(f)

    snap_row = summary.get("snap_row")
    snap_col = summary.get("snap_col")
    if snap_row is None or snap_col is None:
        return None

    df = pd.read_csv(curve_path)
    if df.empty or "DEM_Elev" not in df.columns:
        return None

    # ── isolate the COMID that contains the snap cell ──────────────────────
    snap_mask = (df["Row"] == snap_row) & (df["Col"] == snap_col)
    if not snap_mask.any():
        return None
    snap_comid = int(df.loc[snap_mask, "COMID"].iloc[0])
    reach = df[df["COMID"] == snap_comid].copy().reset_index(drop=True)
    snap_pos = reach[(reach["Row"] == snap_row) & (reach["Col"] == snap_col)].index[0]

    # ── determine direction from NHD Plus flowline geometry ────────────────
    strm_dir = STAGING / "STRM" / dam_id
    us_rc, px_m = _upstream_rowcol(snap_comid, strm_dir)

    if us_rc is not None:
        us_row_f, us_col_f = us_rc
        dist_sq = (
            (reach["Row"].values - us_row_f) ** 2
          + (reach["Col"].values - us_col_f) ** 2
        )
        # Is the upstream flowline endpoint closer to the FIRST or LAST row?
        # Closer to last → CSV is stored downstream→upstream → flip.
        if dist_sq[-1] < dist_sq[0]:
            reach = reach.iloc[::-1].reset_index(drop=True)
            snap_pos = len(reach) - 1 - snap_pos
    else:
        px_m = 1.0   # fallback; BaseElev slope used for direction below

    # ── cumulative arc-length in metres (UTM pixels ≈ 1 m each) ────────────
    dr   = reach["Row"].diff().fillna(0).values
    dc   = reach["Col"].diff().fillna(0).values
    step = np.sqrt(dr ** 2 + dc ** 2) * (px_m if px_m else 1.0)
    cum  = np.cumsum(step)
    x    = cum - cum[snap_pos]   # negative = upstream, positive = downstream

    # ── BaseElev fallback if flowline was unavailable ───────────────────────
    if us_rc is None:
        base = reach["BaseElev"].values
        if float(np.polyfit(np.arange(len(base)), base, 1)[0]) > 0:
            x = -x

    # ── trim to window (asymmetric: each side clipped independently) ────────
    elev      = reach["DEM_Elev"].values
    snap_elev = elev[snap_pos]
    keep      = np.abs(x) <= MAX_DIST
    x_out     = x[keep]
    y_out     = elev[keep] - snap_elev

    if len(x_out) < 1:
        return None

    # ── sanity check on trimmed window ──────────────────────────────────────
    # Within ±MAX_DIST, upstream (x<0) mean should exceed downstream (x>0) mean.
    # If not, everything was determined backwards — flip.
    up_mask = x_out < -1
    dn_mask = x_out >  1
    if up_mask.any() and dn_mask.any():
        if y_out[up_mask].mean() < y_out[dn_mask].mean():
            x_out = -x_out

    return {"dam_id": dam_id, "x": x_out, "y": y_out}


def compute_stats(profiles: list[dict], x_grid: np.ndarray) -> dict:
    """Interpolate all profiles onto x_grid and compute ensemble statistics."""
    matrix = []
    for p in profiles:
        # Only interpolate over the range each profile actually covers
        x_min, x_max = p["x"].min(), p["x"].max()
        col = np.full(len(x_grid), np.nan)
        in_range = (x_grid >= x_min) & (x_grid <= x_max)
        col[in_range] = np.interp(x_grid[in_range], p["x"], p["y"])
        matrix.append(col)

    mat = np.array(matrix)   # shape: (n_profiles, n_x)

    # Coverage: how many profiles contribute at each x
    coverage = np.sum(~np.isnan(mat), axis=0)

    # Mean and std (ignoring NaN)
    mean = np.nanmean(mat, axis=0)
    std  = np.nanstd(mat,  axis=0)

    # RMSE of each profile from the ensemble mean (over shared x)
    rmse_per = []
    for row in mat:
        valid = ~np.isnan(row) & ~np.isnan(mean)
        if valid.sum() > 1:
            rmse_per.append(np.sqrt(np.mean((row[valid] - mean[valid]) ** 2)))
    rmse_arr = np.array(rmse_per)

    # Pairwise Pearson correlations (only over x positions where both are valid)
    n = len(profiles)
    corr_vals = []
    for i in range(n):
        for j in range(i + 1, n):
            valid = ~np.isnan(mat[i]) & ~np.isnan(mat[j])
            if valid.sum() > 3:
                a, b = mat[i][valid], mat[j][valid]
                if a.std() > 0 and b.std() > 0:
                    r = np.corrcoef(a, b)[0, 1]
                    if np.isfinite(r):
                        corr_vals.append(r)
    corr_arr = np.array(corr_vals)

    return {
        "mat": mat,
        "coverage": coverage,
        "mean": mean,
        "std": std,
        "rmse_per_profile": rmse_arr,
        "pairwise_r": corr_arr,
    }


def main() -> None:
    profiles = []
    for dam_dir in sorted(RESULTS.iterdir()):
        if not dam_dir.is_dir():
            continue
        p = load_profile(dam_dir)
        if p is not None:
            profiles.append(p)

    n = len(profiles)
    print(f"Loaded {n} profiles")

    # ── common x grid at 1 m resolution ────────────────────────────────────
    x_grid = np.arange(-MAX_DIST, MAX_DIST + 1, 1.0)
    stats  = compute_stats(profiles, x_grid)

    # ── print summary stats ─────────────────────────────────────────────────
    r = stats["pairwise_r"]
    rmse = stats["rmse_per_profile"]
    print(f"\nPairwise Pearson r  (n={len(r)} pairs):")
    print(f"  mean={r.mean():.3f}  median={np.median(r):.3f}  "
          f"std={r.std():.3f}  min={r.min():.3f}  max={r.max():.3f}")
    print(f"\nRMSE from ensemble mean  (n={len(rmse)} profiles):")
    print(f"  mean={rmse.mean():.3f} m  median={np.median(rmse):.3f} m  "
          f"std={rmse.std():.3f} m  max={rmse.max():.3f} m")

    # Std at key offsets (how spread are profiles right at the dam, ±5 m, ±10 m)
    print(f"\nEnsemble std at key offsets (m):")
    for offset in [-20, -10, -5, 0, 5, 10, 20]:
        idx = np.argmin(np.abs(x_grid - offset))
        print(f"  x={offset:+4d} m : std={stats['std'][idx]:.3f} m  "
              f"(n={stats['coverage'][idx]})")

    # ── plot: spaghetti + mean ± 1σ envelope ───────────────────────────────
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), sharex=True)

    # Top: raw profiles
    for p in profiles:
        ax1.plot(p["x"], p["y"], color="steelblue", alpha=0.2, linewidth=0.7)
    ax1.axvline(0, color="crimson", linewidth=1.5, linestyle="--", label="Dam location")
    ax1.axhline(0, color="gray",    linewidth=0.6, linestyle=":")
    ax1.set_ylabel("ΔElevation from dam cell (m)")
    ax1.set_title(f"Water Surface Profiles — {n} reaches")
    ax1.legend(fontsize=9)

    # Bottom: mean ± 1σ (only where ≥10 profiles overlap)
    enough = stats["coverage"] >= 10
    xp = x_grid[enough]
    mu = stats["mean"][enough]
    sg = stats["std"][enough]

    ax2.fill_between(xp, mu - sg, mu + sg,
                     alpha=0.3, color="steelblue", label="±1σ")
    ax2.plot(xp, mu, color="steelblue", linewidth=1.8, label="Mean")
    ax2.axvline(0, color="crimson", linewidth=1.5, linestyle="--", label="Dam location")
    ax2.axhline(0, color="gray",    linewidth=0.6, linestyle=":")
    ax2.set_xlabel("Distance from dam (m)  ←upstream  |  downstream→")
    ax2.set_ylabel("ΔElevation from dam cell (m)")
    ax2.set_title(f"Ensemble mean ± 1σ  (mean pairwise r = {r.mean():.3f},  "
                  f"mean RMSE = {rmse.mean():.3f} m)")
    ax2.legend(fontsize=9)

    fig.tight_layout()
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PNG, dpi=150)
    print(f"\nSaved → {OUT_PNG}")


if __name__ == "__main__":
    main()
