"""
Paper stats summary — runs width + height per-dam in one loop, computes
the joint (width norm-resid ↔ height norm-resid) Pearson r needed for the
'Joint Error Structure' subsection, and prints a fill-in-the-blanks block
matching the bracketed placeholders in the 2026 paper template.

Outputs:
  output/paper_stats_per_dam.csv     — per-dam table of L and P, all methods
  output/paper_placeholders.txt      — fill-in-the-blanks block

Usage:
    python scripts/paper_stats_summary.py
"""
from __future__ import annotations
import json, sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

from compare_width_methods import (
    _build_reach, walk_width, method2_flat_zone, method3_inflection,
    RESULTS, VAL_CSV,
)
from compare_height_methods import (
    upstream_wse_method2, _snap_cell, method_b_arc_flat, method_c_steepest_ds,
)
from lhd_processor.hydraulics import solve_weir_geom

OUT_DIR  = ROOT / "output"
OUT_CSV  = OUT_DIR / "paper_stats_per_dam.csv"
OUT_TXT  = OUT_DIR / "paper_placeholders.txt"


def tukey_mask(x: np.ndarray) -> np.ndarray:
    q1, q3 = np.percentile(x, [25, 75])
    iqr = q3 - q1
    return (x < q1 - 1.5 * iqr) | (x > q3 + 1.5 * iqr)


def summarise(est: np.ndarray, ref: np.ndarray, label: str) -> dict:
    ratio = est / ref
    resid = est - ref
    norm  = resid / ref
    out   = tukey_mask(norm)
    return dict(
        label=label, n=len(est),
        mean_ratio=float(ratio.mean()),
        median_ratio=float(np.median(ratio)),
        rmse=float(np.sqrt(np.mean(resid**2))),
        mae=float(np.mean(np.abs(resid))),
        n_outliers=int(out.sum()),
        p68_pct=float(np.percentile(np.abs(norm) * 100, 68)),
    )


def fmt(s: dict) -> str:
    return (f"  {s['label']:38s} n={s['n']:3d}  "
            f"mean={s['mean_ratio']:5.2f}  median={s['median_ratio']:5.2f}  "
            f"RMSE={s['rmse']:6.2f}  MAE={s['mae']:6.2f}  "
            f"out={s['n_outliers']:2d}  68%≤{s['p68_pct']:4.0f}%")


def main():
    val = pd.read_csv(VAL_CSV)
    processed = {int(d.name) for d in RESULTS.iterdir()
                 if (d / "analysis_summary.json").exists()}
    val = val[val["OBJECTID"].isin(processed)].copy()

    rows = []
    for _, vrow in val.iterrows():
        dam_id  = str(int(vrow["OBJECTID"]))
        known_L = float(vrow.get("crest_length", np.nan))
        known_P = float(vrow.get("dam_height",  np.nan))

        with open(RESULTS / dam_id / "analysis_summary.json") as f:
            summ = json.load(f)
        baseline_L = summ.get("weir_length_m")

        merged = _build_reach(dam_id, summ)
        if merged is None:
            continue
        xs_df = merged.dropna(subset=["XS1_Profile", "XS2_Profile"])

        def width_at(r, c, wse):
            sub = xs_df[(xs_df["Row"] == r) & (xs_df["Col"] == c)]
            if sub.empty:
                return None
            return walk_width(sub.iloc[0]["XS1_Profile"], sub.iloc[0]["XS2_Profile"],
                              sub.iloc[0]["Ordinate_Dist"], wse)

        r2, c2, wse2 = method2_flat_zone(merged)
        r3, c3, wse3 = method3_inflection(merged)
        L_b  = baseline_L
        L_m2 = width_at(r2, c2, wse2)
        L_m3 = width_at(r3, c3, wse3)

        # ── height: needs Q, L (baseline_L), upstream/downstream WSE ──
        Q = float(summ.get("baseflow_q_ep_50_cms") or 0)
        L_for_P = float(baseline_L or 0)
        P_base = P_B = P_C = None
        if Q > 0 and L_for_P > 0 and "x" in merged.columns:
            us = upstream_wse_method2(merged)
            if us is not None:
                wse_us, _ = us
                def solve_p(wse_ds, base_ds):
                    d_wse = wse_us - wse_ds
                    y_t   = wse_ds - base_ds
                    if d_wse <= 0 or y_t <= 0:
                        return None
                    try:
                        _, P = solve_weir_geom(Q, L_for_P, y_t, d_wse)
                        return float(P) if P and P > 0 else None
                    except Exception:
                        return None
                P_base = solve_p(*_snap_cell(merged))
                P_B    = solve_p(*method_b_arc_flat(merged))
                P_C    = solve_p(*method_c_steepest_ds(merged))

        rows.append({
            "dam_id":     int(dam_id),
            "known_L":    known_L,
            "known_P":    known_P,
            "L_baseline": L_b,
            "L_method2":  L_m2,
            "L_method3":  L_m3,
            "P_baseline": P_base,
            "P_method_b": P_B,
            "P_method_c": P_C,
        })

    df = pd.DataFrame(rows)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False)
    print(f"Saved per-dam table → {OUT_CSV}")

    # ── width / height summaries ────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("WIDTH (crest length L)")
    print("=" * 78)
    width_summ = {}
    for col, label in [("L_baseline", "Baseline (snap cell)"),
                       ("L_method2",  "Method 2 (flat zone)"),
                       ("L_method3",  "Method 3 (inflection)")]:
        sub = df.dropna(subset=[col, "known_L"])
        if sub.empty: continue
        s = summarise(sub[col].values, sub["known_L"].values, label)
        width_summ[col] = s
        print(fmt(s))

    print("\n" + "=" * 78)
    print("HEIGHT (dam height P)")
    print("=" * 78)
    height_summ = {}
    for col, label in [("P_baseline", "Baseline (first DS cell)"),
                       ("P_method_b", "Method B (ARC slope flat zone)"),
                       ("P_method_c", "Method C (steepest DS gradient)")]:
        sub = df.dropna(subset=[col, "known_P"])
        if sub.empty: continue
        s = summarise(sub[col].values, sub["known_P"].values, label)
        height_summ[col] = s
        print(fmt(s))

    # ── joint Pearson r for preferred pair ──────────────────────────────────
    # pick the method whose median ratio is closest to 1.0
    best_L = min(width_summ.items(),  key=lambda kv: abs(kv[1]["median_ratio"] - 1))
    best_P = min(height_summ.items(), key=lambda kv: abs(kv[1]["median_ratio"] - 1))
    pref_Lcol, pref_Pcol = best_L[0], best_P[0]
    pair = df.dropna(subset=[pref_Lcol, "known_L", pref_Pcol, "known_P"])
    if not pair.empty:
        nrL = (pair[pref_Lcol] - pair["known_L"]) / pair["known_L"]
        nrP = (pair[pref_Pcol] - pair["known_P"]) / pair["known_P"]
        r_pearson = float(np.corrcoef(nrL, nrP)[0, 1])
    else:
        r_pearson = float("nan")

    print("\n" + "=" * 78)
    print("JOINT ERROR STRUCTURE")
    print("=" * 78)
    print(f"  preferred width  method: {best_L[1]['label']} ({pref_Lcol})")
    print(f"  preferred height method: {best_P[1]['label']} ({pref_Pcol})")
    print(f"  Pearson r(norm-resid L, norm-resid P) = {r_pearson:+.3f}   n={len(pair)}")

    # ── fill-in-the-blanks block ────────────────────────────────────────────
    def label_strength(r):
        a = abs(r)
        if a < 0.2:  return "weak"
        if a < 0.5:  return "moderate"
        return "strong"

    lines = []
    lines.append("=" * 78)
    lines.append("PAPER PLACEHOLDERS — fill these into the docx template")
    lines.append("=" * 78)
    lines.append("")
    lines.append("Results / Crest Length:")
    lines.append(f"  [BEST METHOD]            : {best_L[1]['label']}")
    lines.append(f"  [X.XX] median ratio      : {best_L[1]['median_ratio']:.2f}")
    lines.append(f"  [XX] RMSE (m)            : {best_L[1]['rmse']:.0f}")
    lines.append(f"  [XX] IQR outliers        : {best_L[1]['n_outliers']}")
    lines.append(f"  [XX]% (~68% of residuals): ±{best_L[1]['p68_pct']:.0f}%")
    bl_med = width_summ['L_baseline']['median_ratio']
    lines.append(f"  Baseline biased [low/high]: "
                 f"{'low' if bl_med < 1 else 'high'}  (baseline median={bl_med:.2f})")
    lines.append(f"  [INFLECTION/FLAT-ZONE]    : "
                 f"{'flat-zone' if 'method2' in pref_Lcol else 'inflection'}")
    lines.append("")
    lines.append("Results / Dam Height:")
    lines.append(f"  [BEST METHOD]              : {best_P[1]['label']}")
    lines.append(f"  [X.XX] median ratio        : {best_P[1]['median_ratio']:.2f}")
    lines.append(f"  [X.XX] RMSE (m)            : {best_P[1]['rmse']:.2f}")
    lines.append(f"  [X.XX] MAE (m)             : {best_P[1]['mae']:.2f}")
    lines.append(f"  [N] dams                   : {best_P[1]['n']}")
    bb = height_summ.get('P_baseline')
    if bb:
        lines.append(f"  Baseline first-DS biased [low/high] : "
                     f"{'low' if bb['median_ratio'] < 1 else 'high'} "
                     f"(median ratio={bb['median_ratio']:.2f})")
        lines.append(f"     [too-small / too-large] y_t        : "
                     f"{'too-small (y_t is shallow at the toe)' if bb['median_ratio'] < 1 else 'too-large'}")
    mb = height_summ.get('P_method_b'); mc = height_summ.get('P_method_c')
    if mb and mc:
        diff = abs(mb['median_ratio'] - mc['median_ratio'])
        lines.append(f"  [agree closely / diverge meaningfully] : "
                     f"{'agree closely' if diff < 0.1 else 'diverge meaningfully'} "
                     f"(B={mb['median_ratio']:.2f}, C={mc['median_ratio']:.2f})")
    lines.append("")
    lines.append("Results / Joint Error Structure:")
    lines.append(f"  [X.XX] Pearson r               : {r_pearson:+.2f}")
    lines.append(f"  [weak/moderate/strong]         : {label_strength(r_pearson)}")
    lines.append(f"  L over → P [under/over]predicted: "
                 f"{'underpredicted' if r_pearson < 0 else 'overpredicted'}  "
                 "(r<0 → algebra of Eq. 3 says overestimating L gives smaller H, larger P → so check sign in context)")
    lines.append(f"  larger/smaller inferred P (text choice): "
                 f"{'larger' if r_pearson < 0 else 'smaller'}")
    lines.append("")
    lines.append("Abstract vs body validation count:")
    n_used_L = width_summ['L_method2']['n']
    n_used_P = best_P[1]['n']
    lines.append(f"  width  n  (crest length validated dams): {n_used_L}")
    lines.append(f"  height n  (dam height validated dams)  : {n_used_P}")
    lines.append(f"  abstract says '98', body says '126' — RECONCILE to actual numbers above.")
    lines.append("")

    block = "\n".join(lines)
    print("\n" + block)

    with open(OUT_TXT, "w") as f:
        f.write(block + "\n")
    print(f"\nSaved → {OUT_TXT}")


if __name__ == "__main__":
    main()
