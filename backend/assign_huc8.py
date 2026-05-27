"""
Assign WBD HUC8 to every dam in full_lhd_website.csv via spatial join.

Downloads WBD_National_GPKG once into cache/wbd/ (~2.5 GB) if not present,
loads the WBDHU8 layer, spatial-joins to dam points, and writes
frontend/data/full_lhd_website_huc8.csv with a new HUC8 column appended.

Dams that fall outside every HUC8 polygon (offshore, non-CONUS) are tagged
HUC8 == "00000000" and skipped by the HUC8 orchestrator.

Idempotent: skips work if the output CSV already has a HUC8 column.

Usage
-----
    python backend/assign_huc8.py
    python backend/assign_huc8.py --force      # recompute even if cached
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path
from urllib.request import urlretrieve

import geopandas as gpd
import pandas as pd

_BACKEND_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_ROOT.parent

DEFAULT_DAMS_CSV = _REPO_ROOT / "frontend" / "data" / "full_lhd_website.csv"
DEFAULT_OUTPUT_CSV = _REPO_ROOT / "frontend" / "data" / "full_lhd_website_huc8.csv"

WBD_CACHE_DIR = _REPO_ROOT / "cache" / "wbd"
WBD_ZIP_URL = (
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/"
    "Hydrography/WBD/National/GPKG/WBD_National_GPKG.zip"
)
WBD_ZIP_PATH = WBD_CACHE_DIR / "WBD_National_GPKG.zip"
WBD_GPKG_PATH = WBD_CACHE_DIR / "WBD_National_GPKG.gpkg"


def _ensure_wbd_gpkg() -> Path:
    """Download + extract WBD national GPKG into cache/wbd/. Returns the .gpkg path."""
    if WBD_GPKG_PATH.exists():
        return WBD_GPKG_PATH

    WBD_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if not WBD_ZIP_PATH.exists():
        print(f"Downloading WBD national GPKG (~2.5 GB) → {WBD_ZIP_PATH}")
        urlretrieve(WBD_ZIP_URL, WBD_ZIP_PATH)
    else:
        print(f"Using cached zip {WBD_ZIP_PATH}")

    print(f"Extracting → {WBD_CACHE_DIR}")
    with zipfile.ZipFile(WBD_ZIP_PATH) as zf:
        zf.extractall(WBD_CACHE_DIR)

    candidates = list(WBD_CACHE_DIR.rglob("WBD_National_GPKG.gpkg"))
    if not candidates:
        sys.exit("Could not find WBD_National_GPKG.gpkg after extraction.")
    found = candidates[0]
    if found != WBD_GPKG_PATH:
        found.replace(WBD_GPKG_PATH)
    return WBD_GPKG_PATH


def _find_huc8_layer(gpkg_path: Path) -> str:
    """Return the HUC8 layer name (handles WBDHU8 vs wbdhu8 vs WBD_HU8 variants)."""
    import fiona
    layers = fiona.listlayers(str(gpkg_path))
    for cand in ("WBDHU8", "wbdhu8", "WBD_HU8"):
        if cand in layers:
            return cand
    sys.exit(
        f"No HUC8 layer found in {gpkg_path}. Layers available: {layers}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dams-csv", type=Path, default=DEFAULT_DAMS_CSV,
                        help=f"Input dam CSV (default: {DEFAULT_DAMS_CSV})")
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV,
                        help=f"Output CSV with HUC8 column (default: {DEFAULT_OUTPUT_CSV})")
    parser.add_argument("--force", action="store_true",
                        help="Recompute even if output CSV already has HUC8.")
    args = parser.parse_args()

    if args.output_csv.exists() and not args.force:
        header = pd.read_csv(args.output_csv, nrows=0)
        if "HUC8" in header.columns:
            print(f"{args.output_csv} already has HUC8 column — pass --force to recompute.")
            return

    gpkg = _ensure_wbd_gpkg()
    layer = _find_huc8_layer(gpkg)
    print(f"Loading HUC8 polygons from layer '{layer}' …")
    huc8 = gpd.read_file(gpkg, layer=layer)

    huc_col = next((c for c in ("huc8", "HUC8") if c in huc8.columns), None)
    if huc_col is None:
        sys.exit(f"No huc8/HUC8 column in layer '{layer}'. Columns: {list(huc8.columns)}")
    huc8 = huc8[[huc_col, "geometry"]].rename(columns={huc_col: "HUC8"})
    print(f"  → {len(huc8)} HUC8 polygons (CRS = {huc8.crs})")

    print(f"Loading dams from {args.dams_csv} …")
    dams = pd.read_csv(args.dams_csv)
    n_raw = len(dams)
    dams = dams[dams["Latitude"].notna() & dams["Longitude"].notna()].copy()
    if len(dams) < n_raw:
        print(f"  Dropped {n_raw - len(dams)} row(s) missing lat/lon.")
    dams_gdf = gpd.GeoDataFrame(
        dams,
        geometry=gpd.points_from_xy(dams["Longitude"], dams["Latitude"]),
        crs="EPSG:4326",
    ).to_crs(huc8.crs)

    print("Spatial joining dams → HUC8 …")
    joined = gpd.sjoin(dams_gdf, huc8, how="left", predicate="within")

    # A dam exactly on a HUC8 boundary can match more than once; keep first.
    joined = joined[~joined.index.duplicated(keep="first")]

    joined["HUC8"] = joined["HUC8"].fillna("00000000").astype(str).str.zfill(8)
    out = pd.DataFrame(joined.drop(columns=["geometry", "index_right"], errors="ignore"))

    n_missing = (out["HUC8"] == "00000000").sum()
    unique_hucs = out.loc[out["HUC8"] != "00000000", "HUC8"].nunique()
    out.to_csv(args.output_csv, index=False)
    print(
        f"Wrote {args.output_csv}\n"
        f"  {len(out)} dams across {unique_hucs} HUC8 basins  "
        f"({n_missing} unassigned)"
    )


if __name__ == "__main__":
    main()
