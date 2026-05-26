"""
Compare two WSP-based crest-width sampling strategies against known widths.

Method 2 — Flat zone:
    Walk upstream from the snap cell.  The first cell where |dy/dx| drops
    below FLAT_THRESHOLD is assumed to be in the flat pool.  Measure width
    there using the XS cross-section profile at that cell.

Method 3 — Inflection (steepest gradient):
    Find the cell with the largest |dy/dx| within ±SEARCH_M of the snap.
    That marks the dam face.  Measure width one cell upstream of it.

Baseline: current snap-cell approach (weir_length_m from analysis_summary.json).

Output: scatter plots + printed stats for all three methods.

Usage:
    python scripts/compare_width_methods.py
"""
from __future__ import annotations
import ast
import json
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from pyproj import Transformer
from shapely.ops import linemerge
import matplotlib.pyplot as plt

# ── paths ────────────────────────────────────────────────────────────────────
STAGING  = Path("/Users/kennyquintana/Developer/dam geometry")
RESULTS  = STAGING / "RESULTS"
VAL_CSV  = Path("/Users/kennyquintana/Developer/dam geometry/lhd_with_geom.csv")
OUT_PNG  = Path("/Users/kennyquintana/Developer/lhd-screening/output/width_method_comparison.png")

# ── tuning ───────────────────────────────────────────────────────────────────
SEARCH_M        = 50.0   # metres on each side of snap to search
FLAT_THRESHOLD  = 0.02   # m/m — gradient below this = flat pool (method 2)

INVALID = -1e5

# ── width helper (mirrors screening/width.py) ────────────────────────────────

def _parse(val):
    if isinstance(val, str):
        val = val.strip()
        if val.startswith("["):
            try:
                return ast.literal_eval(val)
            except (ValueError, SyntaxError):
                return []
    if isinstance(val, list):
        return val
    return []


def _walk_width(xs1, xs2, ord_dist: float, wse: float) -> float:
    def count_wet(prof):
        n = 0
        for e in _parse(prof):
            if wse >= e > INVALID:
                n += 1
            else:
                break
        return n
    return (count_wet(xs1) + count_wet(xs2)) * float(ord_dist)


# ── direction detection (same as plot_wsp_all.py) ────────────────────────────

def _upstream_rowcol(snap_comid: int, strm_dir: Path):
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
    us_lon, us_lat = geom.coords[0][:2]
    with rasterio.open(strms[0]) as src:
        tf = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        x, y = tf.transform(us_lon, us_lat)
        col_f, row_f = (~src.transform) * (x, y)
        px_m = abs(src.transform.a)
    return (row_f, col_f), px_m


def _build_reach(dam_id: str, summary: dict):
    """
    Load, orient, and annotate the reach for this dam.
    Returns a DataFrame with columns: Row, Col, x, DEM_Elev, XS1_Profile,
    XS2_Profile, Ordinate_Dist  — sorted upstream→downstream with x=0 at snap.
    Returns None on failure.
    """
    snap_row = summary.get("snap_row")
    snap_col = summary.get("snap_col")
    if snap_row is None:
        return None

    curve_path = RESULTS / dam_id / "VDT" / f"{dam_id}_Curve.csv"
    xs_path    = RESULTS / dam_id / "XS"  / f"{dam_id}_XS.txt"
    if not curve_path.exists() or not xs_path.exists():
        return None

    curve = pd.read_csv(curve_path)
    xs    = pd.read_csv(xs_path, sep="\t")
    xs.columns = [c.strip() for c in xs.columns]

    snap_mask = (curve["Row"] == snap_row) & (curve["Col"] == snap_col)
    if not snap_mask.any():
        return None
    snap_comid = int(curve.loc[snap_mask, "COMID"].iloc[0])

    reach_c = curve[curve["COMID"] == snap_comid].copy().reset_index(drop=True)
    reach_x = xs[xs["COMID"] == snap_comid].copy()

    # Merge XS profiles onto reach
    merged = reach_c.merge(
        reach_x[["COMID", "Row", "Col", "XS1_Profile", "XS2_Profile", "Ordinate_Dist"]],
        on=["COMID", "Row", "Col"], how="left"
    ).reset_index(drop=True)

    snap_pos = merged[(merged["Row"] == snap_row) & (merged["Col"] == snap_col)].index[0]

    # Orient: NHD flowline first coord = upstream
    strm_dir = STAGING / "STRM" / dam_id
    us_rc, px_m = _upstream_rowcol(snap_comid, strm_dir)
    if us_rc is not None:
        us_row_f, us_col_f = us_rc
        dist_sq = (merged["Row"].values - us_row_f)**2 + (merged["Col"].values - us_col_f)**2
        if dist_sq[-1] < dist_sq[0]:
            merged   = merged.iloc[::-1].reset_index(drop=True)
            snap_pos = len(merged) - 1 - snap_pos
    else:
        px_m = 1.0
        base = merged["BaseElev"].values
        if float(np.polyfit(np.arange(len(base)), base, 1)[0]) > 0:
            merged   = merged.iloc[::-1].reset_index(drop=True)
            snap_pos = len(merged) - 1 - snap_pos

    # Cumulative distance
    dr   = merged["Row"].diff().fillna(0).values
    dc   = merged["Col"].diff().fillna(0).values
    step = np.sqrt(dr**2 + dc**2) * (px_m or 1.0)
    cum  = np.cumsum(step)
    x    = cum - cum[snap_pos]
    merged["x"] = x

    # Sanity check on ±SEARCH_M window
    elev      = merged["DEM_Elev"].values
    snap_elev = elev[snap_pos]
    keep      = np.abs(x) <= SEARCH_M
    x_w = x[keep]; y_w = elev[keep] - snap_elev
    up_m = x_w < -1;  dn_m = x_w > 1
    if up_m.any() and dn_m.any() and y_w[up_m].mean() < y_w[dn_m].mean():
        merged["x"] = -merged["x"]

    return merged


# ── sampling methods ──────────────────────────────────────────────────────────

def _gradient(merged: pd.DataFrame):
    """
    Compute per-cell gradient (m/m) centred on each cell using neighbours.
    Returns array aligned with merged index.
    """
    x = merged["x"].values
    y = merged["DEM_Elev"].values
    grad = np.gradient(y, x)   # uses central differences, handles edges
    return grad


def method2_flat_zone(merged: pd.DataFrame) -> tuple[int, int, float]:
    """
    Walk upstream from snap (x<0) and return (Row, Col, wse) of the first
    cell where the gradient magnitude drops below FLAT_THRESHOLD.
    Falls back to the snap cell (x=0) if no upstream cells exist.
    """
    upstream = merged[(merged["x"] < 0) & (merged["x"] >= -SEARCH_M)].copy()

    if not upstream.empty:
        grad = _gradient(merged)
        upstream = upstream.assign(grad_abs=np.abs(grad[upstream.index]))
        upstream = upstream.sort_values("x", ascending=False)  # closest to dam first

        for _, row in upstream.iterrows():
            if row["grad_abs"] < FLAT_THRESHOLD:
                return int(row["Row"]), int(row["Col"]), float(row["DEM_Elev"])

        # No flat cell found — use the flattest upstream cell within window
        flattest = upstream.loc[upstream["grad_abs"].idxmin()]
        return int(flattest["Row"]), int(flattest["Col"]), float(flattest["DEM_Elev"])

    # No upstream cells at all — fall back to snap cell
    snap = merged[merged["x"] == 0.0]
    if snap.empty:
        snap = merged.iloc[(merged["x"].abs()).argsort()[:1]]
    row = snap.iloc[0]
    return int(row["Row"]), int(row["Col"]), float(row["DEM_Elev"])


def method3_inflection(merged: pd.DataFrame) -> tuple[int, int, float]:
    """
    Find the cell with the steepest |gradient| within ±SEARCH_M.
    Return (Row, Col, wse) of the cell immediately upstream of it.
    Falls back to the snap cell if the window is too small.
    """
    window = merged[np.abs(merged["x"]) <= SEARCH_M].copy()
    if len(window) < 3:
        snap = merged.iloc[(merged["x"].abs()).argsort()[:1]]
        row = snap.iloc[0]
        return int(row["Row"]), int(row["Col"]), float(row["DEM_Elev"])

    grad = _gradient(merged)
    window = window.assign(grad_abs=np.abs(grad[window.index]))
    inflection_pos = window["grad_abs"].idxmax()

    # One cell upstream (lower x) of the inflection
    candidates = window[window["x"] < window.loc[inflection_pos, "x"]]
    if candidates.empty:
        candidates = window  # fallback to inflection cell itself
    target = candidates.iloc[-1]  # closest upstream cell
    return int(target["Row"]), int(target["Col"]), float(target["DEM_Elev"])


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    val = pd.read_csv(VAL_CSV)
    processed = {int(d.name) for d in RESULTS.iterdir()
                 if (d / "analysis_summary.json").exists()}
    val = val[val["OBJECTID"].isin(processed)].copy()

    rows = []
    for _, vrow in val.iterrows():
        dam_id  = str(int(vrow["OBJECTID"]))
        known   = float(vrow["crest_length"])

        summary_path = RESULTS / dam_id / "analysis_summary.json"
        with open(summary_path) as f:
            summary = json.load(f)

        baseline = summary.get("weir_length_m")

        merged = _build_reach(dam_id, summary)
        if merged is None:
            continue

        xs_df = merged.dropna(subset=["XS1_Profile", "XS2_Profile"])

        # Method 2
        r2, c2, wse2 = method2_flat_zone(merged)
        xs_m2 = xs_df[(xs_df["Row"] == r2) & (xs_df["Col"] == c2)]
        w2 = None
        if xs_m2.empty:
            print(f"  Dam {dam_id} M2: no XS profile at selected cell ({r2},{c2})")
        else:
            w2 = _walk_width(xs_m2.iloc[0]["XS1_Profile"],
                             xs_m2.iloc[0]["XS2_Profile"],
                             xs_m2.iloc[0]["Ordinate_Dist"], wse2)

        # Method 3
        r3, c3, wse3 = method3_inflection(merged)
        xs_m3 = xs_df[(xs_df["Row"] == r3) & (xs_df["Col"] == c3)]
        w3 = None
        if xs_m3.empty:
            print(f"  Dam {dam_id} M3: no XS profile at selected cell ({r3},{c3})")
        else:
            w3 = _walk_width(xs_m3.iloc[0]["XS1_Profile"],
                             xs_m3.iloc[0]["XS2_Profile"],
                             xs_m3.iloc[0]["Ordinate_Dist"], wse3)

        rows.append({
            "dam_id":   int(dam_id),
            "known":    known,
            "baseline": baseline,
            "method2":  w2,
            "method3":  w3,
        })

    df = pd.DataFrame(rows).dropna(subset=["known"])

    # ── stats ────────────────────────────────────────────────────────────────
    def stats(est, known, label):
        ratio = est / known
        rmse  = np.sqrt(np.mean((est - known)**2))
        print(f"\n{label}  (n={len(est)}):")
        print(f"  ratio  mean={ratio.mean():.3f}  median={np.median(ratio):.3f}  std={ratio.std():.3f}")
        print(f"  RMSE={rmse:.1f} m   MAE={np.mean(np.abs(est-known)):.1f} m")

    print("=" * 55)
    for method, col in [("Baseline (snap cell)", "baseline"),
                         ("Method 2 (flat zone)", "method2"),
                         ("Method 3 (inflection)", "method3")]:
        sub = df.dropna(subset=[col])
        if sub.empty:
            print(f"\n{method}: no valid estimates")
            continue
        stats(sub[col].values, sub["known"].values, method)
    print("=" * 55)

    # ── plot ─────────────────────────────────────────────────────────────────
    configs = [
        ("baseline", "Baseline (snap cell)", "steelblue"),
        ("method2",  "Method 2 (flat zone)",  "darkorange"),
        ("method3",  "Method 3 (inflection)", "seagreen"),
    ]

    def outlier_mask(resid: pd.Series) -> pd.Series:
        q1, q3 = resid.quantile(0.25), resid.quantile(0.75)
        iqr = q3 - q1
        return (resid < q1 - 1.5 * iqr) | (resid > q3 + 1.5 * iqr)

    fig, axes = plt.subplots(2, 3, figsize=(15, 10))

    for col_idx, (col, title, color) in enumerate(configs):
        ax_sc  = axes[0, col_idx]   # scatter
        ax_res = axes[1, col_idx]   # residuals

        sub = df.dropna(subset=[col]).copy()
        sub["resid"] = sub[col] - sub["known"]
        is_out = outlier_mask(sub["resid"])

        # ── scatter: estimated vs known ──────────────────────────────────
        lim = max(sub["known"].max(), sub[col].max()) * 1.05
        ax_sc.scatter(sub.loc[~is_out, "known"], sub.loc[~is_out, col],
                      alpha=0.6, color=color, edgecolors="k", linewidths=0.4)
        ax_sc.scatter(sub.loc[is_out, "known"], sub.loc[is_out, col],
                      alpha=0.9, color="crimson", edgecolors="k", linewidths=0.6,
                      zorder=5, label="Outlier")
        for _, r in sub[is_out].iterrows():
            ax_sc.annotate(f" {int(r.dam_id)}", (r.known, r[col]),
                           fontsize=7, color="crimson")
        ax_sc.plot([0, lim], [0, lim], "k--", linewidth=0.8, label="1:1")
        ratio = sub[col] / sub["known"]
        rmse  = np.sqrt(np.mean(sub["resid"] ** 2))
        ax_sc.set_title(f"{title}\nmedian ratio={ratio.median():.2f}  "
                        f"RMSE={rmse:.0f} m  n={len(sub)}")
        ax_sc.set_xlabel("Known width (m)")
        ax_sc.legend(fontsize=8)

        # ── normalised residual histogram ────────────────────────────────
        sub["norm_resid"] = sub["resid"] / sub["known"]
        is_out_n = outlier_mask(sub["norm_resid"])

        bins = np.linspace(-2, 2, 33)
        ax_res.hist(sub.loc[~is_out_n, "norm_resid"], bins=bins,
                    color=color, alpha=0.7, edgecolor="k", linewidth=0.4,
                    label="Normal")
        ax_res.hist(sub.loc[is_out_n, "norm_resid"], bins=bins,
                    color="crimson", alpha=0.8, edgecolor="k", linewidth=0.4,
                    label="Outlier")
        ax_res.axvline(0, color="k",    linewidth=1.0, linestyle="--")
        ax_res.axvline(sub["norm_resid"].median(), color=color,
                       linewidth=1.5, linestyle="-",
                       label=f"median={sub['norm_resid'].median():.2f}")
        ax_res.set_xlabel("Normalised residual  (est − known) / known")
        ax_res.set_ylabel("Count")
        ax_res.set_title(f"Residuals — {title}")
        ax_res.legend(fontsize=8)

    axes[0, 0].set_ylabel("Estimated width (m)")
    fig.suptitle("Crest width: sampling method comparison  "
                 "(red = IQR outliers)", fontweight="bold")
    fig.tight_layout()
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PNG, dpi=150)
    print(f"\nSaved → {OUT_PNG}")

    # ── print outlier summary ─────────────────────────────────────────────
    print("\nOutliers by method (IQR rule):")
    for col, title, _ in configs:
        sub = df.dropna(subset=[col]).copy()
        sub["resid"] = sub[col] - sub["known"]
        out = sub[outlier_mask(sub["resid"])][["dam_id", "known", col, "resid"]].copy()
        out["ratio"] = out[col] / out["known"]
        out = out.sort_values("resid", key=abs, ascending=False)
        print(f"\n  {title}:")
        print(out.to_string(index=False, float_format=lambda x: f"{x:.1f}"))


if __name__ == "__main__":
    main()
