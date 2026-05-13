"""
Compare two weir-length estimation methods on staged dams that have a
published weir_length in the dam database.

Method A — ARC perpendicular sampling (current pipeline)
  Wraps `screening.width.estimate_weir_length`. Requires ARC outputs
  (VDT.txt, Curve.csv, XS.txt) under --results-dir/{site_id}/.

Method B — Pool-mask + crest tracing (proposed)
  Wraps `scripts/_pool_weir_method.pool_weir_length`. Reads the DEM
  and flowline directly from paths stored in the database; no ARC dependency.

For every dam in the database:
  - Method B is always attempted (needs DEM + flowline from the database).
  - Method A is attempted if ARC outputs exist under --results-dir.
  - Both are recorded along with ratios to the published weir_length.

Outputs
-------
  --output-dir/weir_method_comparison.csv
  --output-dir/weir_method_comparison.png

Usage
-----
    # TDX flowlines + TDX_GEO_50 ARC results
    python scripts/compare_weir_methods.py \\
        --dams-db /Volumes/KenDrive/lhd_testing/lhd_database_50.xlsx \\
        --results-dir /Volumes/KenDrive/lhd_testing/TDX_GEO_50 \\
        --flowline-source tdx \\
        --output-dir output/weir_comparison_tdx_geo_50

    # NHD flowlines + NHD_NWM_50 ARC results
    python scripts/compare_weir_methods.py \\
        --dams-db /Volumes/KenDrive/lhd_testing/lhd_database_50.xlsx \\
        --results-dir /Volumes/KenDrive/lhd_testing/NHD_NWM_50 \\
        --flowline-source nhd \\
        --output-dir output/weir_comparison_nhd_nwm_50
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND = _REPO_ROOT / "backend"
_SCRIPTS = _REPO_ROOT / "scripts"
for p in (_BACKEND, _SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from screening.width import estimate_weir_length            # method A
from _pool_weir_method import pool_weir_length              # method B

DEFAULT_DAMS_DB = Path("/Volumes/KenDrive/lhd_testing/lhd_database_50.xlsx")


# ---------------------------------------------------------------------------
# Path resolution — reads directly from database row
# ---------------------------------------------------------------------------

def _resolve_paths(
    row: pd.Series,
    results_dir: Path,
    flowline_source: str,
) -> Dict[str, Path | None]:
    dam_id = int(row["site_id"])

    dem = Path(row["dem_path"]) if pd.notna(row["dem_path"]) else None
    if dem is not None and not dem.exists():
        dem = None

    fl_col = f"flowline_path_{flowline_source}"
    raster_col = f"flowline_raster_{flowline_source}"
    gpkg = Path(row[fl_col]) if pd.notna(row[fl_col]) else None
    strm_clean = Path(row[raster_col]) if pd.notna(row[raster_col]) else None
    if strm_clean is not None and not strm_clean.exists():
        strm_clean = None

    dam_results = results_dir / str(dam_id)
    vdt   = dam_results / "VDT" / f"{dam_id}_VDT.txt"
    curve = dam_results / "VDT" / f"{dam_id}_Curve.csv"
    xs    = dam_results / "XS"  / f"{dam_id}_XS.txt"

    return {
        "flowline_gpkg": gpkg,
        "strm_clean":    strm_clean,
        "dem":           dem,
        "vdt":           vdt   if vdt.exists()   else None,
        "curve":         curve if curve.exists() else None,
        "xs":            xs    if xs.exists()    else None,
    }


# ---------------------------------------------------------------------------
# Per-dam methods
# ---------------------------------------------------------------------------

def _run_method_a(dam_lat: float, dam_lon: float, paths: Dict[str, Path | None]) -> Tuple[float | None, str]:
    if paths["strm_clean"] is None or paths["vdt"] is None \
            or paths["curve"] is None or paths["xs"] is None:
        return None, "no-arc-outputs"
    try:
        info = estimate_weir_length(
            dam_lat=dam_lat, dam_lon=dam_lon,
            strm_path=paths["strm_clean"],
            vdt_path=paths["vdt"],
            xs_path=paths["xs"],
            curve_path=paths["curve"],
        )
        return float(info["weir_length"]), "ok"
    except Exception as e:
        return None, f"error:{type(e).__name__}"


def _run_method_b(dam_lat: float, dam_lon: float, paths: Dict[str, Path | None]) -> Tuple[float | None, str, dict]:
    if paths["dem"] is None or paths["flowline_gpkg"] is None:
        return None, "no-dem-or-flowline", {}
    try:
        info = pool_weir_length(
            dam_lat=dam_lat, dam_lon=dam_lon,
            dem_path=paths["dem"],
            flowline_path=paths["flowline_gpkg"],
        )
    except Exception as e:
        return None, f"error:{type(e).__name__}", {}
    if info.get("fallback"):
        return None, info.get("fallback_reason", "fallback"), info
    return float(info["weir_length"]), "ok", info


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dams-db", type=Path, default=DEFAULT_DAMS_DB,
                        help=f"Dam database xlsx (default: {DEFAULT_DAMS_DB})")
    parser.add_argument("--results-dir", required=True, type=Path,
                        help="Folder containing per-dam ARC results (e.g. .../TDX_GEO_50)")
    parser.add_argument("--flowline-source", choices=["nhd", "tdx"], default="nhd",
                        help="Which flowline columns to use from the database (default: nhd)")
    parser.add_argument("--output-dir", type=Path,
                        default=_REPO_ROOT / "output" / "weir_method_comparison")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N dams")
    args = parser.parse_args()

    if not args.dams_db.exists():
        sys.exit(f"Dam database not found: {args.dams_db}")
    dams = pd.read_excel(args.dams_db)
    needed = {"site_id", "latitude", "longitude", "weir_length"}
    missing = needed - set(dams.columns)
    if missing:
        sys.exit(f"Database missing columns: {sorted(missing)}")
    dams = dams.dropna(subset=["site_id", "latitude", "longitude", "weir_length"])
    dams["site_id"] = dams["site_id"].astype(int)
    dams["weir_length"] = pd.to_numeric(dams["weir_length"], errors="coerce")
    dams = dams[dams["weir_length"] > 0].reset_index(drop=True)
    if args.limit:
        dams = dams.head(args.limit)

    print(f"Database      : {args.dams_db}")
    print(f"Results dir   : {args.results_dir}")
    print(f"Flowline src  : {args.flowline_source.upper()}")
    print(f"Dams to compare: {len(dams)}\n")

    rows = []
    counters = {"a_ok": 0, "a_fail": 0, "b_ok": 0, "b_fail": 0, "both_ok": 0}
    for i, dam in dams.iterrows():
        dam_id = int(dam["site_id"])
        lat    = float(dam["latitude"])
        lon    = float(dam["longitude"])
        known  = float(dam["weir_length"])

        paths = _resolve_paths(dam, args.results_dir, args.flowline_source)

        a_val, a_status          = _run_method_a(lat, lon, paths)
        b_val, b_status, b_info  = _run_method_b(lat, lon, paths)

        if a_status == "ok":   counters["a_ok"]   += 1
        else:                  counters["a_fail"]  += 1
        if b_status == "ok":   counters["b_ok"]   += 1
        else:                  counters["b_fail"]  += 1
        if a_status == "ok" and b_status == "ok":
            counters["both_ok"] += 1

        ratio_a = a_val / known if a_val is not None else float("nan")
        ratio_b = b_val / known if b_val is not None else float("nan")

        rows.append({
            "dam_id":               dam_id,
            "latitude":             lat,
            "longitude":            lon,
            "weir_length_known":    known,
            "weir_length_method_a": a_val if a_val is not None else float("nan"),
            "method_a_status":      a_status,
            "ratio_a":              ratio_a,
            "weir_length_method_b": b_val if b_val is not None else float("nan"),
            "method_b_status":      b_status,
            "ratio_b":              ratio_b,
            "wse_method_b":         b_info.get("wse", float("nan")),
            "drop_m_method_b":      b_info.get("drop_m", float("nan")),
            "pool_size_px_method_b": b_info.get("pool_size_px"),
            "n_crest_px_method_b":  b_info.get("n_crest_pixels"),
        })

        a_str = f"{a_val:.1f}" if a_val is not None else f"  -   ({a_status})"
        b_str = f"{b_val:.1f}" if b_val is not None else f"  -   ({b_status})"
        print(f"[{i+1}/{len(dams)}] Dam {dam_id}: known={known:.1f}  A={a_str}  B={b_str}")

    if not rows:
        print("\nNo dams produced any result. Exiting.")
        return

    df = pd.DataFrame(rows)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out_csv = args.output_dir / "weir_method_comparison.csv"
    df.to_csv(out_csv, index=False)
    print(f"\nWrote {out_csv}")

    # Summary stats
    def _stats(label: str, ratios: pd.Series) -> None:
        r = ratios.dropna()
        if r.empty:
            print(f"  {label}: no rows")
            return
        bias   = float(r.mean())
        med    = float(r.median())
        sd     = float(r.std())
        abserr = float((r - 1.0).abs().mean())
        print(f"  {label} (N={len(r)}):  median ratio={med:.2f}  "
              f"mean ratio={bias:.2f} ± {sd:.2f}  mean|ratio−1|={abserr:.2f}")

    print("\nSummary:")
    print(f"  A_ok={counters['a_ok']}  B_ok={counters['b_ok']}  "
          f"both_ok={counters['both_ok']}  "
          f"A_fail={counters['a_fail']}  B_fail={counters['b_fail']}")
    _stats("Method A (ARC perpendicular)", df["ratio_a"])
    _stats("Method B (pool + crest)    ", df["ratio_b"])

    both = df.dropna(subset=["ratio_a", "ratio_b"])
    if not both.empty:
        a_abs = float((both["ratio_a"] - 1.0).abs().mean())
        b_abs = float((both["ratio_b"] - 1.0).abs().mean())
        print(f"\n  Head-to-head where both produced a value (N={len(both)}):")
        print(f"    Method A mean|ratio−1| = {a_abs:.2f}")
        print(f"    Method B mean|ratio−1| = {b_abs:.2f}")
        if b_abs < a_abs:
            print(f"    → Method B is closer to truth by {(a_abs - b_abs):.2f} on average")
        elif a_abs < b_abs:
            print(f"    → Method A is closer to truth by {(b_abs - a_abs):.2f} on average")
        else:
            print("    → Tie")

    # Scatter plot
    lim = float(np.nanmax([
        df["weir_length_known"].max(),
        np.nanmax(df["weir_length_method_a"]) if df["weir_length_method_a"].notna().any() else 0,
        np.nanmax(df["weir_length_method_b"]) if df["weir_length_method_b"].notna().any() else 0,
    ])) * 1.1
    fig, ax = plt.subplots(figsize=(8, 8))
    ax.plot([0, lim], [0, lim], "k--", alpha=0.5, label="1:1")
    if df["weir_length_method_a"].notna().any():
        ax.scatter(df["weir_length_known"], df["weir_length_method_a"],
                   alpha=0.7, edgecolor="black", color="steelblue",
                   label=f"Method A (ARC), N={int(df['weir_length_method_a'].notna().sum())}")
    if df["weir_length_method_b"].notna().any():
        ax.scatter(df["weir_length_known"], df["weir_length_method_b"],
                   alpha=0.7, edgecolor="black", color="orange", marker="s",
                   label=f"Method B (pool+crest), N={int(df['weir_length_method_b'].notna().sum())}")
    ax.set_xlabel("Known weir_length (m)")
    ax.set_ylabel("Estimated weir length (m)")
    ax.set_title(f"Weir-length comparison — {args.results_dir.name} / {args.flowline_source.upper()} (N={len(df)})")
    ax.set_xlim(0, lim)
    ax.set_ylim(0, lim)
    ax.legend()
    ax.grid(True, alpha=0.3)
    out_png = args.output_dir / "weir_method_comparison.png"
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    print(f"  Plot: {out_png}")


if __name__ == "__main__":
    main()
