"""
Joint error structure — earn the claim.

The Joint Error Structure subsection asserts that crest-length error and
dam-height error are coupled (and should be propagated jointly) because L
feeds the weir equation that recovers P.  This script *demonstrates and
quantifies* that propagation instead of just reporting a correlation.

Mechanism
---------
P is solved from Q, L, and the up/downstream WSE via solve_weir_geom.  For a
fixed Q and fixed WSE sampling, only L varies.  So for each dam we solve P
twice:

    P_hat    = solve_weir_geom(Q, L_hat,   y_t, dWSE)   # estimated crest length
    P_trueL  = solve_weir_geom(Q, L_known, y_t, dWSE)   # true crest length

and decompose the normalized height residual (relative to known P):

    nrP_total      = (P_hat   - known_P) / known_P
    contrib_from_L = (P_hat   - P_trueL) / known_P       # L-driven component
    contrib_other  = (P_trueL - known_P) / known_P       # WSE/tailwater/ref
    (nrP_total == contrib_from_L + contrib_other)

We also compute the per-dam elasticity dln P / dln L numerically.  The weir
algebra predicts elasticity = (2/3)(H/P): for low-head dams H << P, so the
coupling should be weak — which would mechanistically explain a small r.

Outputs:
  output/joint_error_propagation.csv
  output/joint_error_propagation.png

Usage:
    python scripts/joint_error_propagation.py
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

from compare_width_methods import (
    _build_reach, walk_width, method2_flat_zone, RESULTS, VAL_CSV,
)
from compare_height_methods import upstream_wse_method2, method_b_arc_flat
from lhd_processor.hydraulics import solve_weir_geom

OUT_CSV = ROOT / "output" / "joint_error_propagation.csv"
OUT_PNG = ROOT / "output" / "joint_error_propagation.png"

# Same cohort exclusion as the figures.
REMOVED_IDS = {23, 27, 33, 48}


def solve_P(Q, L, y_t, d_wse):
    if not (Q > 0 and L and L > 0 and y_t > 0 and d_wse > 0):
        return None
    try:
        _, P = solve_weir_geom(Q, L, y_t, d_wse)
        return float(P) if P and P > 0 else None
    except Exception:
        return None


def main():
    val = pd.read_csv(VAL_CSV)
    processed = {int(d.name) for d in RESULTS.iterdir()
                 if (d / "analysis_summary.json").exists()}
    val = val[val["OBJECTID"].isin(processed)].copy()

    rows = []
    for _, vrow in val.iterrows():
        dam_id = str(int(vrow["OBJECTID"]))
        if int(dam_id) in REMOVED_IDS:
            continue
        known_L = float(vrow.get("crest_length", np.nan))
        known_P = float(vrow.get("dam_height", np.nan))
        if not (np.isfinite(known_L) and np.isfinite(known_P) and known_L > 0 and known_P > 0):
            continue

        with open(RESULTS / dam_id / "analysis_summary.json") as f:
            summ = json.load(f)
        Q = float(summ.get("baseflow_q_ep_50_cms") or 0)
        if Q <= 0:
            continue

        merged = _build_reach(dam_id, summ)
        if merged is None or "x" not in merged.columns:
            continue

        # crest-length estimate (Method 2 flat zone) — the reported L, kept
        # consistent with the P we solve so the (L,P) pair is self-consistent.
        xs_df = merged.dropna(subset=["XS1_Profile", "XS2_Profile"])
        r2, c2, wse2 = method2_flat_zone(merged)
        sub = xs_df[(xs_df["Row"] == r2) & (xs_df["Col"] == c2)]
        if sub.empty:
            continue
        L_hat = walk_width(sub.iloc[0]["XS1_Profile"], sub.iloc[0]["XS2_Profile"],
                           sub.iloc[0]["Ordinate_Dist"], wse2)
        if not (L_hat and L_hat > 0):
            continue

        # fixed WSE sampling (upstream Method 2, downstream Method B)
        us = upstream_wse_method2(merged)
        if us is None:
            continue
        wse_us, _ = us
        wse_ds, base_ds = method_b_arc_flat(merged)
        d_wse = wse_us - wse_ds
        y_t = wse_ds - base_ds

        P_hat   = solve_P(Q, L_hat,   y_t, d_wse)
        P_trueL = solve_P(Q, known_L, y_t, d_wse)
        if P_hat is None or P_trueL is None:
            continue

        # numeric elasticity dlnP/dlnL at L_hat
        Pp = solve_P(Q, L_hat * 1.01, y_t, d_wse)
        Pm = solve_P(Q, L_hat * 0.99, y_t, d_wse)
        elasticity = (np.log(Pp) - np.log(Pm)) / (np.log(1.01) - np.log(0.99)) \
            if (Pp and Pm) else np.nan

        rows.append({
            "dam_id":  int(dam_id),
            "known_L": known_L, "L_hat": L_hat,
            "known_P": known_P, "P_hat": P_hat, "P_trueL": P_trueL,
            "Q": Q,
            "nrL":            (L_hat - known_L) / known_L,
            "nrP_total":      (P_hat - known_P) / known_P,
            "contrib_from_L": (P_hat - P_trueL) / known_P,
            "contrib_other":  (P_trueL - known_P) / known_P,
            "elasticity_dlnP_dlnL": elasticity,
        })

    df = pd.DataFrame(rows)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False)
    n = len(df)
    print(f"n = {n}")

    nrL = df["nrL"].values
    nrP = df["nrP_total"].values
    cL  = df["contrib_from_L"].values
    cO  = df["contrib_other"].values

    r_total = float(np.corrcoef(nrL, nrP)[0, 1])
    # how much of the height residual *variance* is L-driven
    # regress nrP_total on the L-driven component
    slope, intercept = np.polyfit(nrL, nrP, 1)
    # share of |error| budget
    share_L = float(np.mean(np.abs(cL)) / (np.mean(np.abs(cL)) + np.mean(np.abs(cO))))
    var_share_L = float(np.var(cL) / np.var(nrP))

    print("=" * 64)
    print("JOINT ERROR PROPAGATION  (L  ->  P  via weir equation)")
    print("=" * 64)
    print(f"  corr(nrL, nrP_total)            r = {r_total:+.3f}")
    print(f"  regression nrP = {slope:+.3f}*nrL {intercept:+.3f}")
    print(f"  elasticity dlnP/dlnL   median = {np.nanmedian(df['elasticity_dlnP_dlnL']):.3f}"
          f"   mean = {np.nanmean(df['elasticity_dlnP_dlnL']):.3f}")
    print(f"  predicted (2/3)(H/P) is small because H << P for low-head dams")
    print("-" * 64)
    print(f"  mean |L-driven  P error|  = {np.mean(np.abs(cL))*100:5.1f}%  of known P")
    print(f"  mean |other     P error|  = {np.mean(np.abs(cO))*100:5.1f}%  of known P")
    print(f"  L-driven share of |error| budget   = {share_L*100:4.1f}%")
    print(f"  L-driven share of error variance   = {var_share_L*100:4.1f}%")
    print("=" * 64)

    # ── figure ───────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(1, 2, figsize=(13, 5.5))

    # (a) observed joint residuals + propagation-only prediction
    ax[0].axhline(0, color="k", lw=0.6); ax[0].axvline(0, color="k", lw=0.6)
    ax[0].scatter(nrL, nrP, s=28, color="steelblue", edgecolors="k", linewidths=0.4,
                  label="Total Height Residual")
    ax[0].scatter(nrL, cL, s=18, color="crimson", marker="^",
                  label="L-Driven Component Only")
    xs = np.linspace(nrL.min(), nrL.max(), 50)
    ax[0].plot(xs, slope * xs + intercept, "b--", lw=1,
               label=f"Fit: Slope={slope:+.2f}, r={r_total:+.2f}")
    ax[0].set_xlabel("Normalized Crest-Length Residual  (L̂−L)/L")
    ax[0].set_ylabel("Normalized Height Residual  (P̂−P)/P")
    ax[0].set_title("Joint Residuals: Total vs. L-Driven Component")
    ax[0].legend(fontsize=8)

    # (b) error attribution
    comp_means = [np.mean(np.abs(cL)) * 100, np.mean(np.abs(cO)) * 100]
    ax[1].bar(["L-Driven\n(Length Error)", "Other\n(WSE/Tailwater/Ref)"],
              comp_means, color=["crimson", "darkorange"],
              edgecolor="k", alpha=0.8)
    for i, v in enumerate(comp_means):
        ax[1].text(i, v, f"{v:.1f}%", ha="center", va="bottom")
    ax[1].set_ylabel("Mean |Height Error|  (% of Known P)")
    ax[1].set_title("Height-Error Attribution")

    fig.tight_layout()
    fig.savefig(OUT_PNG, dpi=150)
    print(f"Saved → {OUT_CSV}")
    print(f"Saved → {OUT_PNG}")


if __name__ == "__main__":
    main()
