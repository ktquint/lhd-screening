"""
Compare downstream tailwater sampling strategies for dam height estimation.

Upstream WSE is fixed to Method 2 (flat zone, custom DEM_Elev gradient) for
all three cases — only the downstream sampling location varies.

Baseline  — first available cell downstream of snap (x > 0)
Method B  — flat zone downstream: walk downstream until ARC's Slope column
             converges to the reference channel slope (median slope of cells
             100–500 m downstream, unaffected by the dam).  This is where
             ARC would consider the flow to be at normal depth.
Method C  — steepest downstream gradient: find the steepest DEM_Elev drop
             downstream (dam face), sample one cell further downstream.

Once the tailwater location is chosen, dam height P is solved via:
    solve_weir_geom(Q, L, y_t, delta_wse)
where y_t = WSE_ds - BaseElev_ds  and  delta_wse = WSE_us - WSE_ds.

Usage:
    python scripts/compare_height_methods.py
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# ── backend on path ───────────────────────────────────────────────────────────
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from lhd_processor.hydraulics import solve_weir_geom
from screening.reach import (
    DEFAULT_FLAT_THRESHOLD as FLAT_GRAD_THR,
    DEFAULT_REF_SLOPE_MAX as REF_SLOPE_MAX,
    DEFAULT_REF_SLOPE_MIN as REF_SLOPE_MIN,
    DEFAULT_SEARCH_M_DN as SEARCH_M_DN,
    DEFAULT_SEARCH_M_UP as SEARCH_M_UP,
    build_reach,
    pick_crest_cell,
    pick_downstream_cell,
)

# ── paths ─────────────────────────────────────────────────────────────────────
STAGING  = Path("/Users/kennyquintana/Developer/dam geometry")
RESULTS  = STAGING / "RESULTS"
VAL_CSV  = STAGING / "lhd_with_geom.csv"
OUT_PNG  = Path("/Users/kennyquintana/Developer/lhd-screening/output/height_method_comparison.png")


# ── reach wrapper ────────────────────────────────────────────────────────────

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
        search_m=SEARCH_M_UP,
    )


# ── upstream WSE (Method 2 — delegates to production picker) ─────────────────

def upstream_wse_method2(merged: pd.DataFrame) -> tuple[float, float] | None:
    row = pick_crest_cell(merged, search_m=SEARCH_M_UP, flat_threshold=FLAT_GRAD_THR)
    return float(row["DEM_Elev"]), float(row["BaseElev"])


# ── downstream sampling methods ──────────────────────────────────────────────

def _snap_cell(merged: pd.DataFrame) -> tuple[float, float]:
    ds = merged[merged["x"] > 0].sort_values("x")
    if ds.empty:
        row = merged.iloc[(merged["x"].abs()).argsort()[:1]].iloc[0]
    else:
        row = ds.iloc[0]
    return float(row["DEM_Elev"]), float(row["BaseElev"])


def method_b_arc_flat(merged: pd.DataFrame) -> tuple[float, float]:
    row = pick_downstream_cell(merged, ref_min=REF_SLOPE_MIN, ref_max=REF_SLOPE_MAX)
    return float(row["DEM_Elev"]), float(row["BaseElev"])


def method_c_steepest_ds(merged: pd.DataFrame) -> tuple[float, float]:
    """Method C — kept inline since it isn't used in production."""
    window = merged[(merged["x"] > 0) & (merged["x"] <= SEARCH_M_DN)].copy()
    if len(window) < 3:
        return _snap_cell(merged)

    grad = np.gradient(merged["DEM_Elev"].values, merged["x"].values)
    window = window.assign(grad_abs=np.abs(grad[window.index]))
    inflection_idx = window["grad_abs"].idxmax()
    inflection_x   = window.loc[inflection_idx, "x"]

    candidates = window[window["x"] > inflection_x]
    target = candidates.iloc[0] if not candidates.empty else window.iloc[-1]
    return float(target["DEM_Elev"]), float(target["BaseElev"])


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    val = pd.read_csv(VAL_CSV)
    processed = {int(d.name) for d in RESULTS.iterdir()
                 if (d / "analysis_summary.json").exists()}
    val = val[val["OBJECTID"].isin(processed)].copy()

    rows = []
    for _, vrow in val.iterrows():
        dam_id = str(int(vrow["OBJECTID"]))
        known  = float(vrow["dam_height"])

        with open(RESULTS / dam_id / "analysis_summary.json") as f:
            summary = json.load(f)

        Q = float(summary.get("baseflow_q_ep_50_cms", 0))
        L = float(summary.get("weir_length_m", 0))
        if Q <= 0 or L <= 0:
            continue

        merged = _build_reach(dam_id, summary)
        if merged is None:
            continue

        us = upstream_wse_method2(merged)
        if us is None:
            continue
        wse_us, _ = us

        def solve_p(wse_ds, base_ds):
            delta_wse = wse_us - wse_ds
            y_t       = wse_ds - base_ds
            if delta_wse <= 0 or y_t <= 0:
                return None
            _, P = solve_weir_geom(Q, L, y_t, delta_wse)
            return P if P > 0 else None

        wse_b, base_b = _snap_cell(merged)
        wse_B, base_B = method_b_arc_flat(merged)
        wse_C, base_C = method_c_steepest_ds(merged)

        rows.append({
            "dam_id":   int(dam_id),
            "known":    known,
            "baseline": solve_p(wse_b, base_b),
            "method_b": solve_p(wse_B, base_B),
            "method_c": solve_p(wse_C, base_C),
        })

    df = pd.DataFrame(rows).dropna(subset=["known"])

    # ── stats ──────────────────────────────────────────────────────────────────
    def stats(col, label):
        sub = df.dropna(subset=[col])
        if sub.empty:
            print(f"\n{label}: no valid estimates"); return
        est, kn = sub[col].values, sub["known"].values
        ratio = est / kn
        rmse  = np.sqrt(np.mean((est - kn)**2))
        print(f"\n{label}  (n={len(sub)}):")
        print(f"  ratio  mean={ratio.mean():.3f}  median={np.median(ratio):.3f}  std={ratio.std():.3f}")
        print(f"  RMSE={rmse:.2f} m   MAE={np.mean(np.abs(est-kn)):.2f} m")

    print("=" * 55)
    for col, label in [("baseline", "Baseline (first DS cell)"),
                        ("method_b", "Method B (ARC slope flat zone)"),
                        ("method_c", "Method C (steepest DS gradient)")]:
        stats(col, label)
    print("=" * 55)

    # ── plot ───────────────────────────────────────────────────────────────────
    def outlier_mask(s: pd.Series) -> pd.Series:
        q1, q3 = s.quantile(0.25), s.quantile(0.75)
        iqr = q3 - q1
        return (s < q1 - 1.5 * iqr) | (s > q3 + 1.5 * iqr)

    configs = [
        ("baseline", "Baseline\n(first DS cell)",       "steelblue"),
        ("method_b", "Method B\n(ARC slope flat zone)",  "darkorange"),
        ("method_c", "Method C\n(steepest DS gradient)", "seagreen"),
    ]

    fig, axes = plt.subplots(2, 3, figsize=(15, 10))

    for col_idx, (col, title, color) in enumerate(configs):
        ax_sc  = axes[0, col_idx]
        ax_res = axes[1, col_idx]

        sub = df.dropna(subset=[col]).copy()
        sub["norm_resid"] = (sub[col] - sub["known"]) / sub["known"]
        is_out = outlier_mask(sub["norm_resid"])

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
        rmse  = np.sqrt(np.mean((sub[col] - sub["known"])**2))
        ax_sc.set_title(f"{title}\nmedian ratio={ratio.median():.2f}  "
                        f"RMSE={rmse:.2f} m  n={len(sub)}")
        ax_sc.set_xlabel("Known height (m)")
        ax_sc.legend(fontsize=8)

        bins = np.linspace(-2, 2, 33)
        ax_res.hist(sub.loc[~is_out, "norm_resid"], bins=bins,
                    color=color, alpha=0.7, edgecolor="k", linewidth=0.4,
                    label="Normal")
        ax_res.hist(sub.loc[is_out, "norm_resid"], bins=bins,
                    color="crimson", alpha=0.8, edgecolor="k", linewidth=0.4,
                    label="Outlier")
        ax_res.axvline(0, color="k", linewidth=1.0, linestyle="--")
        ax_res.axvline(sub["norm_resid"].median(), color=color,
                       linewidth=1.5, label=f"median={sub['norm_resid'].median():.2f}")
        ax_res.set_xlabel("Normalised residual  (est − known) / known")
        ax_res.set_ylabel("Count")
        ax_res.set_title(f"Residuals — {title}")
        ax_res.legend(fontsize=8)

    axes[0, 0].set_ylabel("Estimated height (m)")
    fig.suptitle("Dam height: downstream sampling method comparison  "
                 "(red = IQR outliers)", fontweight="bold")
    fig.tight_layout()
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PNG, dpi=150)
    print(f"\nSaved → {OUT_PNG}")


if __name__ == "__main__":
    main()
