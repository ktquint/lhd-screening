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
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# ── backend on path ──────────────────────────────────────────────────────────
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from screening.reach import (
    DEFAULT_FLAT_THRESHOLD as FLAT_THRESHOLD,
    DEFAULT_SEARCH_M_UP as SEARCH_M,
    build_reach,
    pick_crest_cell,
    walk_width,
)

# ── paths ────────────────────────────────────────────────────────────────────
STAGING  = Path("/Users/kennyquintana/Developer/dam geometry")
RESULTS  = STAGING / "RESULTS"
VAL_CSV  = Path("/Users/kennyquintana/Developer/dam geometry/lhd_with_geom.csv")
OUT_PNG  = Path("/Users/kennyquintana/Developer/lhd-screening/output/width_method_comparison.png")


# ── reach builder (uses shared screening.reach module) ───────────────────────

def _build_reach(dam_id: str, summary: dict):
    snap_row = summary.get("snap_row")
    snap_col = summary.get("snap_col")
    if snap_row is None:
        return None
    strm_dir = STAGING / "STRM" / dam_id
    gpkgs = list(strm_dir.glob("nhd_flowline_*.gpkg"))
    strms = list(strm_dir.glob("nhd_*_clean.tif"))
    curve_path = RESULTS / dam_id / "VDT" / f"{dam_id}_Curve.csv"
    xs_path    = RESULTS / dam_id / "XS"  / f"{dam_id}_XS.txt"
    if not (gpkgs and strms and curve_path.exists() and xs_path.exists()):
        return None
    return build_reach(
        snap_row=int(snap_row), snap_col=int(snap_col),
        curve_path=curve_path, xs_path=xs_path,
        flowline_path=gpkgs[0], strm_path=strms[0],
        search_m=SEARCH_M,
    )


# ── sampling methods ─────────────────────────────────────────────────────────

def method2_flat_zone(merged: pd.DataFrame) -> tuple[int, int, float]:
    row = pick_crest_cell(merged, search_m=SEARCH_M, flat_threshold=FLAT_THRESHOLD)
    return int(row["Row"]), int(row["Col"]), float(row["DEM_Elev"])


def method3_inflection(merged: pd.DataFrame) -> tuple[int, int, float]:
    """Method 3 (inflection) — kept inline since it isn't used in production."""
    window = merged[np.abs(merged["x"]) <= SEARCH_M].copy()
    if len(window) < 3:
        snap = merged.iloc[(merged["x"].abs()).argsort()[:1]]
        row = snap.iloc[0]
        return int(row["Row"]), int(row["Col"]), float(row["DEM_Elev"])

    grad = np.gradient(merged["DEM_Elev"].values, merged["x"].values)
    window = window.assign(grad_abs=np.abs(grad[window.index]))
    inflection_pos = window["grad_abs"].idxmax()
    candidates = window[window["x"] < window.loc[inflection_pos, "x"]]
    if candidates.empty:
        candidates = window
    target = candidates.iloc[-1]
    return int(target["Row"]), int(target["Col"]), float(target["DEM_Elev"])


# ── main ─────────────────────────────────────────────────────────────────────

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
            w2 = walk_width(xs_m2.iloc[0]["XS1_Profile"],
                            xs_m2.iloc[0]["XS2_Profile"],
                            xs_m2.iloc[0]["Ordinate_Dist"], wse2)

        # Method 3
        r3, c3, wse3 = method3_inflection(merged)
        xs_m3 = xs_df[(xs_df["Row"] == r3) & (xs_df["Col"] == c3)]
        w3 = None
        if xs_m3.empty:
            print(f"  Dam {dam_id} M3: no XS profile at selected cell ({r3},{c3})")
        else:
            w3 = walk_width(xs_m3.iloc[0]["XS1_Profile"],
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
        ax_sc  = axes[0, col_idx]
        ax_res = axes[1, col_idx]

        sub = df.dropna(subset=[col]).copy()
        sub["resid"] = sub[col] - sub["known"]
        is_out = outlier_mask(sub["resid"])

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

        sub["norm_resid"] = sub["resid"] / sub["known"]
        is_out_n = outlier_mask(sub["norm_resid"])

        bins = np.linspace(-2, 2, 33)
        ax_res.hist(sub.loc[~is_out_n, "norm_resid"], bins=bins,
                    color=color, alpha=0.7, edgecolor="k", linewidth=0.4,
                    label="Normal")
        ax_res.hist(sub.loc[is_out_n, "norm_resid"], bins=bins,
                    color="crimson", alpha=0.8, edgecolor="k", linewidth=0.4,
                    label="Outlier")
        ax_res.axvline(0, color="k", linewidth=1.0, linestyle="--")
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
