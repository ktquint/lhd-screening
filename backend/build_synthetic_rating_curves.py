"""
Build synthetic stage-vs-discharge rating curves (SRC) for every dam's NHDPlus V2
reach (Reach_ID / COMID) in full_lhd_website.csv.

Source: s3://lynker-spatial/tabular/riverml_channel_geometry_with_ahg.parquet
(NOAA/Lynker hydrofabric, open NODD bucket, anonymous access, no AWS billing).
That table gives per-feature_id (= COMID) bankfull top width, bankfull depth,
bankfull area/perimeter, Manning's roughness, and channel slope. We back out an
equivalent trapezoidal channel from those four bankfull quantities and evaluate
Manning's equation, Q = (1/n)*A*R^(2/3)*S^(1/2), over a range of stages.

In-channel only (no floodplain spreading) -- fine for screening as long as the
dangerous-flow range stays roughly in-bank.

Usage:
    python backend/build_synthetic_rating_curves.py
    python backend/build_synthetic_rating_curves.py --csv data/full_lhd_website.csv \
        --out frontend/data/synthetic_rating_curves.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import s3fs

_BACKEND_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_ROOT.parent

_PARQUET_URL = "lynker-spatial/tabular/riverml_channel_geometry_with_ahg.parquet"
_CACHE_PATH = _BACKEND_ROOT / "cache" / "riverml_channel_geometry_with_ahg.parquet"

DEFAULT_CSV = _REPO_ROOT / "data" / "full_lhd_website.csv"
DEFAULT_OUT = _REPO_ROOT / "frontend" / "data" / "synthetic_rating_curves.json"

_N_STAGE_POINTS = 40
_STAGE_MAX_MULTIPLIER = 2.0  # evaluate up to 2x bankfull depth
_MIN_SLOPE = 1e-4
_MIN_BOTTOM_WIDTH_FRAC = 0.05  # floor bottom width at this fraction of top width


def _log(msg: str) -> None:
    print(f"[build_src] {msg}", flush=True)


def _download_geometry_table(force: bool) -> Path:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _CACHE_PATH.exists() and not force:
        _log(f"Using cached {_CACHE_PATH}")
        return _CACHE_PATH
    _log(f"Downloading s3://{_PARQUET_URL} (anonymous, ~188MB, one-time) ...")
    fs = s3fs.S3FileSystem(anon=True)
    fs.get(_PARQUET_URL, str(_CACHE_PATH))
    _log(f"Cached to {_CACHE_PATH}")
    return _CACHE_PATH


def _load_channel_geometry(parquet_path: Path, feature_ids: set[int]) -> pd.DataFrame:
    table = pq.read_table(
        parquet_path,
        columns=[
            "feature_id",
            "owp_y_bf",
            "owp_tw_bf",
            "bf_area",
            "owp_roughness_bathy",
            "owp_roughness_no_bathy",
            "slope",
        ],
        filters=[("feature_id", "in", feature_ids)],
    )
    return table.to_pandas()


def _trapezoid_rating_curve(row: pd.Series) -> dict | None:
    y_bf = float(row["owp_y_bf"])
    tw_bf = float(row["owp_tw_bf"])
    area_bf = float(row["bf_area"])
    slope = float(row["slope"])
    n = row["owp_roughness_bathy"]
    if pd.isna(n) or n <= 0:
        n = row["owp_roughness_no_bathy"]

    if not all(np.isfinite(v) and v > 0 for v in (y_bf, tw_bf, area_bf, n)):
        return None

    slope = max(slope, _MIN_SLOPE) if np.isfinite(slope) else _MIN_SLOPE

    # Back out an equivalent trapezoid from bankfull depth/top-width/area:
    #   area_bf = bw*y_bf + z*y_bf^2  and  tw_bf = bw + 2*z*y_bf
    # => z = (tw_bf - area_bf/y_bf) / y_bf
    side_slope = (tw_bf - area_bf / y_bf) / y_bf
    side_slope = max(side_slope, 0.0)
    bottom_width = tw_bf - 2 * side_slope * y_bf
    bottom_width = max(bottom_width, _MIN_BOTTOM_WIDTH_FRAC * tw_bf)

    stages = np.linspace(0.0, y_bf * _STAGE_MAX_MULTIPLIER, _N_STAGE_POINTS)
    stages[0] = 1e-3  # avoid a literal zero-flow point
    width = bottom_width + 2 * side_slope * stages
    area = stages * bottom_width + side_slope * stages**2
    perimeter = bottom_width + 2 * stages * np.sqrt(1 + side_slope**2)
    hydraulic_radius = area / perimeter
    discharge = (1.0 / float(n)) * area * hydraulic_radius ** (2.0 / 3.0) * slope**0.5

    return {
        "stage_m": [round(float(s), 4) for s in stages],
        "discharge_cms": [round(float(q), 4) for q in discharge],
        "bankfull_stage_m": round(y_bf, 4),
        "bottom_width_m": round(bottom_width, 4),
        "side_slope": round(side_slope, 4),
        "manning_n": round(float(n), 4),
        "slope": round(slope, 6),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--force-download", action="store_true", help="re-download the parquet even if cached")
    args = ap.parse_args()

    if not args.csv.exists():
        _log(f"ERROR: {args.csv} not found")
        return 1

    _log(f"Loading {args.csv} ...")
    df = pd.read_csv(args.csv, low_memory=False)
    reach_ids = pd.to_numeric(df["Reach_ID"], errors="coerce").dropna().astype(int)
    feature_ids = set(reach_ids.unique().tolist())
    _log(f"{len(feature_ids)} unique Reach_ID/COMID values across {len(df)} dams")

    parquet_path = _download_geometry_table(args.force_download)

    _log("Filtering channel geometry table to dam reaches ...")
    geom = _load_channel_geometry(parquet_path, feature_ids)
    _log(f"Matched {len(geom)}/{len(feature_ids)} reaches in hydrofabric")

    curves: dict[str, dict] = {}
    for _, row in geom.iterrows():
        curve = _trapezoid_rating_curve(row)
        if curve is not None:
            curves[str(int(row["feature_id"]))] = curve

    _log(f"Computed synthetic rating curves for {len(curves)} reaches")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(curves, f, separators=(",", ":"))
    _log(f"Wrote {args.out} ({args.out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
