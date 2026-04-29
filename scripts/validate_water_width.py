"""
Validate using the DEM water-surface width at the stream pixel nearest the
dam's lat/lon as a substitute for the known `weir_length`.

For each dam in the database that has completed ARC outputs, this script:
  1. Opens the rasterized stream file (STRM/{id}/{nhd|tdx}_{id}_clean.tif)
     to snap the dam point to the nearest stream cell
  2. Looks up that (Row, Col) in the raw ARC outputs (Curve.csv + XS.txt)
  3. Walks outward from the channel center along the XS profile, counting
     cells where DEM elevation is at or below the ARC-estimated WSE
  4. Multiplies that cell count by Ordinate_Dist to get a measured width
  5. Compares to the known weir_length and writes a CSV + 1:1 scatter plot
"""
import argparse
import ast
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import rasterio
from pyproj import Transformer

INVALID_THRESHOLD = -1e5


def _parse_profile(profile):
    if isinstance(profile, str):
        try:
            return ast.literal_eval(profile.strip())
        except (ValueError, SyntaxError):
            return []
    if isinstance(profile, np.ndarray):
        return profile.tolist()
    if isinstance(profile, list):
        return profile
    if profile is None or (isinstance(profile, float) and np.isnan(profile)):
        return []
    return [profile]


def water_surface_width(xs1_profile, xs2_profile, ordinate_dist, wse):
    """
    Mirrors lhd_processor.analysis.classes._init_geometry: walk outward from
    center, count cells where INVALID_THRESHOLD < elev <= WSE, stop at the
    first cell that rises above WSE or hits the invalid sentinel.
    """
    p1 = _parse_profile(xs1_profile)
    p2 = _parse_profile(xs2_profile)

    def count_wet(profile):
        n = 0
        for elev in profile:
            if wse >= elev > INVALID_THRESHOLD:
                n += 1
            else:
                break
        return n

    return (count_wet(p1) + count_wet(p2)) * float(ordinate_dist)


def find_strm_raster(strm_dir: Path, site_id: int, prefix: str):
    """Look for {prefix}_*_clean.tif in STRM/{site_id}/."""
    site_dir = strm_dir / str(site_id)
    if not site_dir.exists():
        return None
    matches = sorted(site_dir.glob(f"{prefix}_*_clean.tif"))
    return matches[0] if matches else None


def find_raw_arc_files(results_dir: Path, dam_id: int):
    """Return paths to (VDT.txt, XS.txt, Curve.csv) or None if any missing."""
    base = results_dir / str(dam_id)
    vdt_txt = base / "VDT" / f"{dam_id}_VDT.txt"
    xs_txt = base / "XS" / f"{dam_id}_XS.txt"
    curve_csv = base / "VDT" / f"{dam_id}_Curve.csv"
    if not vdt_txt.exists() or not xs_txt.exists() or not curve_csv.exists():
        return None
    return vdt_txt, xs_txt, curve_csv


def load_curve(curve_csv: Path) -> pd.DataFrame:
    df = pd.read_csv(curve_csv)
    df.columns = [c.strip() for c in df.columns]
    return df


def merge_vdt_curve(vdt_df: pd.DataFrame, curve_df: pd.DataFrame) -> pd.DataFrame:
    """Merge DEM_Elev from Curve into VDT, keyed on (COMID, Row, Col)."""
    keep = ["COMID", "Row", "Col", "DEM_Elev"]
    return vdt_df.merge(curve_df[keep], on=["COMID", "Row", "Col"], how="left")


def project_dam_to_pixel(strm_path: Path, dam_lat: float, dam_lon: float):
    """
    Use the stream raster only to get a CRS + geotransform for the ARC grid;
    project the dam (lat, lon) into continuous (row, col) coordinates.
    Returns (row_f, col_f, px_size_x, px_size_y) or None if outside raster.
    """
    with rasterio.open(strm_path) as src:
        transformer = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        x, y = transformer.transform(dam_lon, dam_lat)

        rows, cols = src.shape
        inv = ~src.transform
        col_f, row_f = inv * (x, y)
        if not (0 <= row_f < rows and 0 <= col_f < cols):
            return None

        return (float(row_f), float(col_f),
                abs(src.transform.a), abs(src.transform.e))


def snap_to_vdt(vdt_df: pd.DataFrame, dam_row_f: float, dam_col_f: float,
                px_size_x: float, px_size_y: float):
    """
    Find the (Row, Col) in vdt_df closest to the dam's continuous pixel
    location. VDT.txt is the source of truth for which cells ARC actually
    processed (the cleaned stream raster sometimes contains cells ARC skips).
    """
    vdt_rows = vdt_df["Row"].astype(float).values
    vdt_cols = vdt_df["Col"].astype(float).values
    dr = (vdt_rows - dam_row_f) * px_size_y
    dc = (vdt_cols - dam_col_f) * px_size_x
    d2 = dr ** 2 + dc ** 2
    i = int(np.argmin(d2))
    return int(vdt_rows[i]), int(vdt_cols[i]), float(np.sqrt(d2[i]))


def load_vdt(vdt_txt: Path) -> pd.DataFrame:
    df = pd.read_csv(vdt_txt)
    df.columns = [c.strip() for c in df.columns]
    return df


def load_xs(xs_txt: Path) -> pd.DataFrame:
    df = pd.read_csv(xs_txt, sep="\t")
    df.columns = [c.strip() for c in df.columns]
    return df


def width_at_cell(vdt_row, xs_df: pd.DataFrame, wse: float):
    """Returns (width_m, ordinate_dist) or None if XS missing."""
    r, c = int(vdt_row["Row"]), int(vdt_row["Col"])
    match = xs_df[(xs_df["Row"] == r) & (xs_df["Col"] == c)]
    if match.empty:
        return None
    xs_row = match.iloc[0]
    od = float(xs_row["Ordinate_Dist"])
    width = water_surface_width(
        xs_row["XS1_Profile"], xs_row["XS2_Profile"], od, wse
    )
    return width, od


def infer_prefix(results_dir: Path, override: str | None) -> str:
    if override:
        return override.lower()
    name = results_dir.name.upper()
    if name.startswith("NHD"):
        return "nhd"
    if name.startswith("TDX"):
        return "tdx"
    raise ValueError(
        f"Cannot infer flowline prefix from '{results_dir.name}'. "
        f"Pass --flowline-prefix nhd|tdx explicitly."
    )


def validate(database_path: Path, results_dir: Path, strm_dir: Path,
             output_dir: Path, prefix: str):
    sites = pd.read_excel(database_path, sheet_name="Sites")

    rows = []
    for _, site in sites.iterrows():
        dam_id = int(site["site_id"])
        true_L = site.get("weir_length")
        if pd.isna(true_L) or float(true_L) <= 0:
            continue
        dam_lat = float(site["latitude"])
        dam_lon = float(site["longitude"])

        strm_path = find_strm_raster(strm_dir, dam_id, prefix)
        if strm_path is None:
            print(f"[skip] Dam {dam_id}: no {prefix}_*_clean.tif in STRM/{dam_id}/")
            continue

        raw = find_raw_arc_files(results_dir, dam_id)
        if raw is None:
            print(f"[skip] Dam {dam_id}: missing VDT.txt, XS.txt, or Curve.csv")
            continue
        vdt_txt, xs_txt, curve_csv = raw

        try:
            proj = project_dam_to_pixel(strm_path, dam_lat, dam_lon)
        except Exception as e:
            print(f"[skip] Dam {dam_id}: projection failed: {e}")
            continue
        if proj is None:
            print(f"[skip] Dam {dam_id}: dam projects outside stream raster")
            continue
        dam_row_f, dam_col_f, px_x, px_y = proj

        try:
            vdt_df = load_vdt(vdt_txt)
            curve_df = load_curve(curve_csv)
            vdt_df = merge_vdt_curve(vdt_df, curve_df)
        except Exception as e:
            print(f"[skip] Dam {dam_id}: VDT/Curve load failed: {e}")
            continue
        if vdt_df.empty:
            print(f"[skip] Dam {dam_id}: VDT.txt has no rows")
            continue
        if vdt_df["DEM_Elev"].isna().all():
            print(f"[skip] Dam {dam_id}: no DEM_Elev after merge")
            continue
        snap_row, snap_col, snap_dist_m = snap_to_vdt(
            vdt_df, dam_row_f, dam_col_f, px_x, px_y
        )

        try:
            xs_df = load_xs(xs_txt)
        except Exception as e:
            print(f"[skip] Dam {dam_id}: XS load failed: {e}")
            continue

        snap_match = vdt_df[(vdt_df["Row"] == snap_row) & (vdt_df["Col"] == snap_col)]
        if snap_match.empty:
            print(f"[skip] Dam {dam_id}: snapped row not in VDT after merge")
            continue
        snap_vdt = snap_match.iloc[0]

        dem_elev = snap_vdt.get("DEM_Elev")
        arc_wse = snap_vdt.get("Elev")
        if pd.isna(dem_elev):
            print(f"[skip] Dam {dam_id}: DEM_Elev missing for snapped cell")
            continue

        # Width using lidar-observed surface (pool elevation if impounded)
        w_dem = width_at_cell(snap_vdt, xs_df, float(dem_elev))
        if w_dem is None:
            print(f"[skip] Dam {dam_id}: no XS for snapped cell")
            continue
        width_dem = w_dem[0]

        # Width using ARC's baseflow WSE (for comparison)
        width_arc = float("nan")
        if pd.notna(arc_wse):
            w_arc = width_at_cell(snap_vdt, xs_df, float(arc_wse))
            if w_arc is not None:
                width_arc = w_arc[0]

        ratio = width_dem / float(true_L)
        rows.append({
            "site_id": dam_id,
            "weir_length_known": float(true_L),
            "width_dem_wse": width_dem,
            "width_arc_wse": width_arc,
            "ratio_measured_over_known": ratio,
            "dam_to_snap_m": snap_dist_m,
            "dem_elev_m": float(dem_elev),
            "arc_elev_m": float(arc_wse) if pd.notna(arc_wse) else float("nan"),
            "ordinate_dist_m": w_dem[1],
            "snap_row": snap_row,
            "snap_col": snap_col,
        })
        print(f"[ok]   Dam {dam_id}: known={float(true_L):.1f}m  "
              f"dem_wse={width_dem:.1f}m  arc_wse={width_arc:.1f}m  "
              f"ratio={ratio:.2f}  snap={snap_dist_m:.0f}m")

    if not rows:
        print("\nNo dams processed.")
        return

    df = pd.DataFrame(rows)
    output_dir.mkdir(parents=True, exist_ok=True)

    out_csv = output_dir / "water_width_validation.csv"
    df.to_csv(out_csv, index=False)

    print(f"\nWrote {out_csv}")
    print(f"  N            = {len(df)}")
    print(f"  median ratio = {df['ratio_measured_over_known'].median():.2f}")
    print(f"  mean ratio   = {df['ratio_measured_over_known'].mean():.2f}")
    print(f"  std ratio    = {df['ratio_measured_over_known'].std():.2f}")
    print(f"  median dam->snap dist = {df['dam_to_snap_m'].median():.1f} m")

    fig, ax = plt.subplots(figsize=(8, 8))
    lim = max(df["weir_length_known"].max(),
              df["width_dem_wse"].max()) * 1.1
    ax.plot([0, lim], [0, lim], "k--", alpha=0.5, label="1:1")
    ax.scatter(df["weir_length_known"], df["width_dem_wse"],
               alpha=0.7, edgecolor="black", label="DEM_Elev as water level")
    ax.scatter(df["weir_length_known"], df["width_arc_wse"],
               alpha=0.3, edgecolor="none", color="orange", marker="s",
               s=15, label="ARC's Elev (baseflow WSE)")
    ax.set_xlabel("Known weir_length (m)")
    ax.set_ylabel("Measured width at snapped pixel (m)")
    ax.set_title(f"Width at dam pixel vs. known weir length  (N={len(df)})")
    ax.set_xlim(0, lim)
    ax.set_ylim(0, lim)
    ax.legend()
    ax.grid(True, alpha=0.3)
    out_png = output_dir / "water_width_validation.png"
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    print(f"  Plot: {out_png}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", required=True, type=Path,
                        help="Path to dam database .xlsx (with a 'Sites' sheet)")
    parser.add_argument("--results", required=True, type=Path,
                        help="Path to ARC Results directory (per-dam subfolders)")
    parser.add_argument("--strm-dir", required=True, type=Path,
                        help="Path to STRM directory (per-dam subfolders with *_clean.tif)")
    parser.add_argument("--output", type=Path, default=Path("output"),
                        help="Where to write the CSV and scatter plot")
    parser.add_argument("--flowline-prefix", choices=["nhd", "tdx"], default=None,
                        help="Override stream raster prefix (default: infer from --results name)")
    args = parser.parse_args()
    prefix = infer_prefix(args.results, args.flowline_prefix)
    validate(args.database, args.results, args.strm_dir, args.output, prefix)


if __name__ == "__main__":
    main()
