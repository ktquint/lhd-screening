"""
Regenerate Figure 2 (crest width comparison) restricted to the 72-dam cohort
where the full L+P pipeline succeeded. Reads paper_stats_per_dam.csv.

Output:
  output/width_method_comparison.png   (overwrites existing 98-dam version)
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
CSV  = ROOT / "output" / "paper_stats_per_dam.csv"
PNG  = ROOT / "output" / "width_method_comparison.png"

# Dams later found not to be true low-head dams; dropped from the validation cohort.
REMOVED_IDS = {23, 27, 33, 48}

df = pd.read_csv(CSV)
mask = df["P_method_b"].notna() & df["known_P"].notna() & df["known_L"].notna()
mask &= ~df["dam_id"].isin(REMOVED_IDS)
df = df[mask].copy()
print(f"cohort n = {len(df)}")

configs = [
    ("L_baseline", "Method A (snap cell)",   "steelblue"),
    ("L_method2",  "Method B (flat zone)",   "darkorange"),
    ("L_method3",  "Method C (inflection)",  "seagreen"),
]


def outlier_mask(s: pd.Series) -> pd.Series:
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    return (s < q1 - 1.5 * iqr) | (s > q3 + 1.5 * iqr)


fig, axes = plt.subplots(2, 3, figsize=(15, 13))

for col_idx, (col, title, color) in enumerate(configs):
    ax_sc  = axes[0, col_idx]
    ax_res = axes[1, col_idx]
    sub = df.dropna(subset=[col]).copy()
    sub["resid"] = sub[col] - sub["known_L"]
    is_out = outlier_mask(sub["resid"])

    lim = max(sub["known_L"].max(), sub[col].max()) * 1.05
    ax_sc.scatter(sub.loc[~is_out, "known_L"], sub.loc[~is_out, col],
                  alpha=0.6, color=color, edgecolors="k", linewidths=0.4)
    ax_sc.scatter(sub.loc[is_out, "known_L"], sub.loc[is_out, col],
                  alpha=0.9, color="crimson", edgecolors="k", linewidths=0.6,
                  zorder=5, label="Outlier")
    for _, r in sub[is_out].iterrows():
        ax_sc.annotate(f" {int(r.dam_id)}", (r.known_L, r[col]),
                       fontsize=7, color="crimson")
    ax_sc.plot([0, lim], [0, lim], "k--", linewidth=0.8, label="1:1")
    ratio = sub[col] / sub["known_L"]
    rmse  = np.sqrt(np.mean(sub["resid"] ** 2))
    ax_sc.set_title(f"{title}\nmedian ratio={ratio.median():.2f}  "
                    f"RMSE={rmse:.0f} m  n={len(sub)}")
    ax_sc.set_xlabel("Known width (m)")
    ax_sc.legend(fontsize=8)

    sub["norm_resid"] = sub["resid"] / sub["known_L"]
    ax_res.boxplot(sub["norm_resid"].values, vert=False, widths=0.6,
                   patch_artist=True,
                   boxprops=dict(facecolor=color, alpha=0.7, edgecolor="k"),
                   medianprops=dict(color="k", linewidth=1.5),
                   whiskerprops=dict(color="k"), capprops=dict(color="k"),
                   flierprops=dict(marker="o", markerfacecolor="crimson",
                                   markersize=4, markeredgecolor="k",
                                   markeredgewidth=0.4))
    ax_res.axvline(0, color="k", linewidth=1.0, linestyle="--")
    ax_res.set_xlabel("Normalised residual  (est − known) / known")
    ax_res.set_yticks([])
    ax_res.set_title(f"Residuals — {title}  (median={sub['norm_resid'].median():.2f})")

axes[0, 0].set_ylabel("Estimated width (m)")
fig.tight_layout()
fig.savefig(PNG, dpi=150)
print(f"Saved → {PNG}")
