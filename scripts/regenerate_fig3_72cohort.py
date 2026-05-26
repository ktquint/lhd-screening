"""
Regenerate Figure 3 (dam height comparison) restricted to the 72-dam cohort.
Reads paper_stats_per_dam.csv.

Output:
  output/height_method_comparison.png
"""
from __future__ import annotations
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
CSV  = ROOT / "output" / "paper_stats_per_dam.csv"
PNG  = ROOT / "output" / "height_method_comparison.png"

df = pd.read_csv(CSV)
mask = df["P_method_b"].notna() & df["known_P"].notna() & df["known_L"].notna()
df = df[mask].copy()
print(f"cohort n = {len(df)}")

configs = [
    ("P_baseline", "Baseline\n(first DS cell)",       "steelblue"),
    ("P_method_b", "Method B\n(ARC slope flat zone)",  "darkorange"),
    ("P_method_c", "Method C\n(steepest DS gradient)", "seagreen"),
]


def outlier_mask(s: pd.Series) -> pd.Series:
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    iqr = q3 - q1
    return (s < q1 - 1.5 * iqr) | (s > q3 + 1.5 * iqr)


fig, axes = plt.subplots(2, 3, figsize=(15, 10))

for col_idx, (col, title, color) in enumerate(configs):
    ax_sc  = axes[0, col_idx]
    ax_res = axes[1, col_idx]
    sub = df.dropna(subset=[col]).copy()
    sub["norm_resid"] = (sub[col] - sub["known_P"]) / sub["known_P"]
    is_out = outlier_mask(sub["norm_resid"])

    lim = max(sub["known_P"].max(), sub[col].max()) * 1.05
    ax_sc.scatter(sub.loc[~is_out, "known_P"], sub.loc[~is_out, col],
                  alpha=0.6, color=color, edgecolors="k", linewidths=0.4)
    ax_sc.scatter(sub.loc[is_out, "known_P"], sub.loc[is_out, col],
                  alpha=0.9, color="crimson", edgecolors="k", linewidths=0.6,
                  zorder=5, label="Outlier")
    for _, r in sub[is_out].iterrows():
        ax_sc.annotate(f" {int(r.dam_id)}", (r.known_P, r[col]),
                       fontsize=7, color="crimson")
    ax_sc.plot([0, lim], [0, lim], "k--", linewidth=0.8, label="1:1")
    ratio = sub[col] / sub["known_P"]
    rmse  = np.sqrt(np.mean((sub[col] - sub["known_P"])**2))
    ax_sc.set_title(f"{title}\nmedian ratio={ratio.median():.2f}  "
                    f"RMSE={rmse:.2f} m  n={len(sub)}")
    ax_sc.set_xlabel("Known height (m)")
    ax_sc.legend(fontsize=8)

    bins = np.linspace(-2, 2, 33)
    ax_res.hist(sub.loc[~is_out, "norm_resid"], bins=bins,
                color=color, alpha=0.7, edgecolor="k", linewidth=0.4, label="Normal")
    ax_res.hist(sub.loc[is_out, "norm_resid"], bins=bins,
                color="crimson", alpha=0.8, edgecolor="k", linewidth=0.4, label="Outlier")
    ax_res.axvline(0, color="k", linewidth=1.0, linestyle="--")
    ax_res.axvline(sub["norm_resid"].median(), color=color,
                   linewidth=1.5, label=f"median={sub['norm_resid'].median():.2f}")
    ax_res.set_xlabel("Normalised residual  (est − known) / known")
    ax_res.set_ylabel("Count")
    ax_res.set_title(f"Residuals — {title}")
    ax_res.legend(fontsize=8)

axes[0, 0].set_ylabel("Estimated height (m)")
fig.suptitle("Dam height: downstream sampling method comparison (n=72 cohort)  "
             "(red = IQR outliers)", fontweight="bold")
fig.tight_layout()
fig.savefig(PNG, dpi=150)
print(f"Saved → {PNG}")
