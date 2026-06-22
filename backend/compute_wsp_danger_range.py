"""
Precompute Qmin_env / Qmax_env for dams that have WSP-derived geometry.

Uses Dam_Height_WSP_Ft + Dam_Length_WSP_Ft as P and L (converted to m),
then calls rating_curve_intercepts_simp with the staged Tailwater_a/b and
Rp100_cms bracket to find the discharge range where tailwater sits between
the conjugate and flip depths.

Writes results back to Qmin_env / Qmax_env (cms) in both:
  data/full_lhd_website.csv
  frontend/data/full_lhd_website.csv

Usage
-----
    python backend/compute_wsp_danger_range.py
    python backend/compute_wsp_danger_range.py --dry-run   # print stats only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_BACKEND_ROOT = Path(__file__).resolve().parent
_REPO_ROOT    = _BACKEND_ROOT.parent

if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from lhd_processor.hydraulics import rating_curve_intercepts_simp

FT_TO_M             = 1.0 / 3.28084
SOLVER_FLOOR_CMS    = 0.01
DEFAULT_CSV         = _REPO_ROOT / "data"          / "full_lhd_website.csv"
FRONTEND_CSV        = _REPO_ROOT / "frontend/data" / "full_lhd_website.csv"


def _compute_one(row) -> tuple[float | None, float | None]:
    P_m = float(row["Dam_Height_WSP_Ft"]) * FT_TO_M
    L_m = float(row["Dam_Length_WSP_Ft"]) * FT_TO_M
    a   = float(row["Tailwater_a"])
    b   = float(row["Tailwater_b"])
    q_max = float(row["Rp100_cms"])

    if P_m <= 0 or L_m <= 0 or q_max <= SOLVER_FLOOR_CMS:
        return None, None

    q_min_cms, q_max_cms = rating_curve_intercepts_simp(
        L_m, P_m, a, b, SOLVER_FLOOR_CMS, q_max
    )

    if q_min_cms == -9999 or q_max_cms == -9999:
        return None, None
    if not (np.isfinite(q_min_cms) and np.isfinite(q_max_cms)):
        return None, None
    if q_min_cms >= q_max_cms:
        return None, None

    return float(q_min_cms), float(q_max_cms)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV,
                        help=f"Master CSV to read/write [default: {DEFAULT_CSV}]")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute and print stats without writing any file")
    args = parser.parse_args()

    df = pd.read_csv(args.csv, low_memory=False)
    total = len(df)

    mask = (
        df["Dam_Height_WSP_Ft"].notna() &
        df["Dam_Length_WSP_Ft"].notna() &
        df["Tailwater_a"].notna() &
        df["Tailwater_b"].notna() &
        df["Rp100_cms"].notna()
    )
    candidates = df[mask]
    print(f"Total dams: {total:,}")
    print(f"Candidates (WSP + tailwater): {len(candidates):,}")

    ok = failed = 0
    q_mins: dict[int, float] = {}
    q_maxs: dict[int, float] = {}

    for idx, row in candidates.iterrows():
        q_min, q_max = _compute_one(row)
        if q_min is not None:
            q_mins[idx] = q_min
            q_maxs[idx] = q_max
            ok += 1
        else:
            failed += 1

    print(f"Solved: {ok:,}  |  Failed/degenerate: {failed:,}")

    if args.dry_run:
        if q_mins:
            vals = list(q_mins.values())
            print(f"Qmin_env range: {min(vals):.2f} – {max(vals):.2f} cms")
            vals = list(q_maxs.values())
            print(f"Qmax_env range: {min(vals):.2f} – {max(vals):.2f} cms")
        print("Dry run — no files written.")
        return

    df.loc[list(q_mins.keys()), "Qmin_env"] = list(q_mins.values())
    df.loc[list(q_maxs.keys()), "Qmax_env"] = list(q_maxs.values())

    df.to_csv(args.csv, index=False)
    print(f"Wrote {args.csv}")

    if FRONTEND_CSV.exists():
        df.to_csv(FRONTEND_CSV, index=False)
        print(f"Wrote {FRONTEND_CSV}")
    else:
        print(f"(Frontend CSV not found at {FRONTEND_CSV} — skipped)")


if __name__ == "__main__":
    main()
