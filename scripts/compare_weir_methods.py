"""
Compare two weir-length estimation methods on staged dams that have a
published Weir_Length in the dam inventory.

Method A — ARC perpendicular sampling (current pipeline)
  Wraps `screening.width.estimate_weir_length`. Requires ARC outputs
  (VDT.txt, Curve.csv, XS.txt) under --results-dir/{dam_id}/.

Method B — Pool-mask + crest tracing (proposed)
  Wraps `scripts/_pool_weir_method.pool_weir_length`. Reads the trimmed
  DEM and flowline directly; no ARC dependency.

For every dam with a finite Weir_Length in the inventory:
  - Method B is always attempted (only needs DEM + flowline).
  - Method A is attempted if ARC outputs exist.
  - Both are recorded along with ratios to the published value.

Outputs
-------
  --output-dir/weir_method_comparison.csv
  --output-dir/weir_method_comparison.png

Usage
-----
    # TDX flowlines + TDX_GEO_50 ARC results
    python scripts/compare_weir_methods.py \\
        --staging-dir /Volumes/KenDrive/lhd_testing \\
        --results-dir /Volumes/KenDrive/lhd_testing/TDX_GEO_50 \\
        --flowline-source tdx \\
        --output-dir output/weir_comparison_tdx_geo_50

    # NHD flowlines + NHD_NWM_50 ARC results
    python scripts/compare_weir_methods.py \\
        --staging-dir /Volumes/KenDrive/lhd_testing \\
        --results-dir /Volumes/KenDrive/lhd_testing/NHD_NWM_50 \\
        --flowline-source nhd \\
        --output-dir output/weir_comparison_nhd_nwm_50
"""
from __future__ import annotations

import argparse
import re
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


DEFAULT_DAMS_CSV = _REPO_ROOT / "frontend" / "data" / "full_lhd_website.csv"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_paths(
    staging_dir: Path,
    results_dir: Path,
    dam_id: int,
    flowline_source: str,
) -> Dict[str, Path | None]:
    strm_site = staging_dir / "STRM" / str(dam_id)

    prefix = flowline_source.lower()  # "nhd" or "tdx"
    gpkgs = list(strm_site.glob(f"{prefix}_flowline_*.gpkg"))
    gpkg = gpkgs[0] if gpkgs else None
    source_id = None
    if gpkg is not None:
        m = re.search(rf"{prefix}_flowline_(\d+)", gpkg.stem)
        if m:
            source_id = int(m.group(1))

    strm_clean = (strm_site / f"{prefix}_{source_id}_clean.tif"
                  if source_id is not None else None)

    dem_dir = staging_dir / "DEM" / str(dam_id)
    dem_candidates = [
        dem_dir / f"dem_{dam_id}.tif",
        dem_dir / f"{dam_id}_MERGED_DEM.tif",
    ]
    dem = next((p for p in dem_candidates if p.exists()), None)

    dam_results = results_dir / str(dam_id)
    vdt = dam_results / "VDT" / f"{dam_id}_VDT.txt"
    curve = dam_results / "VDT" / f"{dam_id}_Curve.csv"
    xs = dam_results / "XS" / f"{dam_id}_XS.txt"

    return {
        "flowline_gpkg": gpkg,
        "strm_clean": strm_clean if strm_clean and strm_clean.exists() else None,
        "dem": dem,
        "vdt": vdt if vdt.exists() else None,
        "curve": curve if curve.exists() else None,
        "xs": xs if xs.exists() else None,
    }


# ---------------------------------------------------------------------------
# Per-dam comparison
# ---------------------------------------------------------------------------

def _run_method_a(dam_lat: float, dam_lon: float, paths: Dict[str, Path | None]) -> Tuple[float | None, str]:
    """Returns (weir_length_m, status). status = 'ok' or short reason."""
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
    """Returns (weir_length_m, status, diagnostics)."""
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
    parser.add_argument("--staging-dir", required=True, type=Path,
                        help="Root staging dir containing STRM/ and DEM/ subdirs")
    parser.add_argument("--results-dir", type=Path, default=None,
                        help="Folder containing per-dam ARC results (e.g. .../TDX_GEO_50). "
                             "Defaults to <staging-dir>/RESULTS")
    parser.add_argument("--flowline-source", choices=["nhd", "tdx"], default="nhd",
                        help="Which flowline set to use from STRM/ (default: nhd)")
    parser.add_argument("--dams-csv", type=Path, default=DEFAULT_DAMS_CSV,
                        help=f"Dam inventory CSV (default: {DEFAULT_DAMS_CSV})")
    parser.add_argument("--output-dir", type=Path,
                        default=_REPO_ROOT / "output" / "weir_method_comparison")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N eligible dams")
    args = parser.parse_args()

    results_dir = args.results_dir if args.results_dir is not None \
        else args.staging_dir / "RESULTS"

    if not args.dams_csv.exists():
        sys.exit(f"Dam inventory CSV not found: {args.dams_csv}")
    dams = pd.read_csv(args.dams_csv)
    needed = {"OBJECTID", "Latitude", "Longitude", "Weir_Length"}
    missing = needed - set(dams.columns)
    if missing:
        sys.exit(f"Dam CSV missing columns: {sorted(missing)}")
    dams = dams.dropna(subset=["OBJECTID", "Latitude", "Longitude", "Weir_Length"])
    dams["OBJECTID"] = dams["OBJECTID"].astype(int)
    dams["Weir_Length"] = pd.to_numeric(dams["Weir_Length"], errors="coerce")
    dams = dams[dams["Weir_Length"] > 0].reset_index(drop=True)
    if args.limit:
        dams = dams.head(args.limit)

    print(f"Results dir   : {results_dir}")
    print(f"Flowline src  : {args.flowline_source.upper()}")
    print(f"Comparing weir methods on {len(dams)} dams with known Weir_Length\n")

    rows = []
    counters = {"a_ok": 0, "a_fail": 0, "b_ok": 0, "b_fail": 0,
                "both_ok": 0, "no_inputs": 0}
    for i, dam in dams.iterrows():
        dam_id = int(dam["OBJECTID"])
        lat = float(dam["Latitude"])
        lon = float(dam["Longitude"])
        known = float(dam["Weir_Length"])

        paths = _resolve_paths(args.staging_dir, results_dir, dam_id, args.flowline_source)
        if paths["dem"] is None and paths["vdt"] is None:
            counters["no_inputs"] += 1
            continue

        a_val, a_status = _run_method_a(lat, lon, paths)
        b_val, b_status, b_info = _run_method_b(lat, lon, paths)

        if a_status == "ok":
            counters["a_ok"] += 1
        else:
            counters["a_fail"] += 1
        if b_status == "ok":
            counters["b_ok"] += 1
        else:
            counters["b_fail"] += 1
        if a_status == "ok" and b_status == "ok":
            counters["both_ok"] += 1

        ratio_a = a_val / known if a_val is not None else float("nan")
        ratio_b = b_val / known if b_val is not None else float("nan")

        rows.append({
            "dam_id": dam_id,
            "latitude": lat,
            "longitude": lon,
            "weir_length_known": known,
            "weir_length_method_a": a_val if a_val is not None else float("nan"),
            "method_a_status": a_status,
            "ratio_a": ratio_a,
            "weir_length_method_b": b_val if b_val is not None else float("nan"),
            "method_b_status": b_status,
            "ratio_b": ratio_b,
            "wse_method_b": b_info.get("wse", float("nan")),
            "drop_m_method_b": b_info.get("drop_m", float("nan")),
            "pool_size_px_method_b": b_info.get("pool_size_px"),
            "n_crest_px_method_b": b_info.get("n_crest_pixels"),
        })

        a_str = f"{a_val:.1f}" if a_val is not None else f"  -   ({a_status})"
        b_str = f"{b_val:.1f}" if b_val is not None else f"  -   ({b_status})"
        print(f"[{i+1}/{len(dams)}] Dam {dam_id}: known={known:.1f}  "
              f"A={a_str}  B={b_str}")

    if not rows:
        print("\nNo dams processed (no staged inputs?). Exiting.")
        return

    df = pd.DataFrame(rows)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    out_csv = args.output_dir / "weir_method_comparison.csv"
    df.to_csv(out_csv, index=False)
    print(f"\nWrote {out_csv}")

    # ----------------------------------------------------------------------
    # Summary stats
    # ----------------------------------------------------------------------
    def _stats(label: str, ratios: pd.Series):
        r = ratios.dropna()
        if r.empty:
            print(f"  {label}: no rows")
            return
        # Bias = mean ratio; spread = std of ratio; |error| = mean abs deviation from 1
        bias = float(r.mean())
        med = float(r.median())
        sd = float(r.std())
        abserr = float((r - 1.0).abs().mean())
        print(f"  {label} (N={len(r)}):  median ratio={med:.2f}  "
              f"mean ratio={bias:.2f} ± {sd:.2f}  mean|ratio−1|={abserr:.2f}")

    print("\nSummary:")
    print(f"  inputs: A_ok={counters['a_ok']}  B_ok={counters['b_ok']}  "
          f"both_ok={counters['both_ok']}  no_inputs={counters['no_inputs']}")
    _stats("Method A (ARC perpendicular)", df["ratio_a"])
    _stats("Method B (pool + crest)    ", df["ratio_b"])

    # Head-to-head on dams where both succeeded
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
            print(f"    → Tie")

    # ----------------------------------------------------------------------
    # Scatter plot
    # ----------------------------------------------------------------------
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
    ax.set_xlabel("Known Weir_Length (m)")
    ax.set_ylabel("Estimated weir length (m)")
    ax.set_title(f"Weir-length method comparison — {results_dir.name} (N={len(df)} dams)")
    ax.set_xlim(0, lim)
    ax.set_ylim(0, lim)
    ax.legend()
    ax.grid(True, alpha=0.3)
    out_png = args.output_dir / "weir_method_comparison.png"
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    print(f"  Plot: {out_png}")


if __name__ == "__main__":
    main()
