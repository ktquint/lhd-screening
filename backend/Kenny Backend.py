#@@ -3,7 +3,7 @@

#Steps
#-----
#1. For each dam, fetch NHD flowlines (100 m upstream, 1 km downstream)
#1. For each dam, fetch NHD flowlines (1 km upstream, 1.5 km downstream)
#   and save as individual .gpkg files under STRM/{dam_id}/.
#2. Query TNM for 1-m DEM tiles covering each flowline bbox
#   (fallback: 1/9 arc-second, then 1/3 arc-second).
#@@ -55,8 +55,8 @@

#defines parser for command line arguments
parser = argparse.ArgumentParser()

parser.add_argument(
    "--dams_csv",
    type=str,
    default="frontend/data/full_lhd_website.csv"
)

parser.add_argument(
    "--limit",
    type=int,
    default=None
)

args = parser.parse_args()

#constants
_BACKEND_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _BACKEND_ROOT.parent
args = parser.parse_args()
raw_dem_dir = 
## must be defined manually. fetch_nhd_flowlines = 

_REPO_ROOT = _BACKEND_ROOT.parent
DEFAULT_DAMS_CSV = _REPO_ROOT / "frontend" / "data" / "full_lhd_website.csv"

# NHD reach window: 100 m upstream, 1 km downstream
_FLOWLINE_DISTANCE_KM = (0.1, 1.0)
# NHD reach window: 1 km upstream, 1.5 km downstream
_FLOWLINE_DISTANCE_KM = (1.0, 1.5)

import threading
import pandas as pd
import requests
import geopandas as gpd
import argparse
from shapely.geometry import Point, box
from shapely.geometry import Point
from pathlib import Path


_print_lock = threading.Lock()

#@@ -376,9 +376,15 @@ def main() -> None:
raw_dem_dir.mkdir(parents=True, exist_ok=True)

dams_df = pd.read_csv(args.dams_csv)
n_raw = len(dams_df)
dams_df = dams_df[
        dams_df["Latitude"].notna() & dams_df["Longitude"].notna()
        dams_df["OBJECTID"].notna()
        & dams_df["Latitude"].notna()
        & dams_df["Longitude"].notna()
    ].reset_index(drop=True)
dams_df["OBJECTID"] = dams_df["OBJECTID"].astype(int)
if len(dams_df) < n_raw:
        print(f"Dropped {n_raw - len(dams_df)} row(s) with missing OBJECTID/lat/lon")
if args.limit:
        dams_df = dams_df.head(args.limit)

#----------------------------------------
# For each dam, fetch NHD flowlines (100 m upstream, 1 km downstream)
#----------------------------------------
for idx, dam_row in dams_df.iterrows():

        lat = dam_row["Latitude"]
        lon = dam_row["Longitude"]

    # Create dam point
        dam_point = Point(lon, lat)

    # -----------------------------------------
    # FETCH FLOWLINES
    # -----------------------------------------
        flowlines = fetch_nhd_flowlines(lat, lon)

    # -----------------------------------------
    # PROJECT CRS FOR DISTANCE CALC
    # -----------------------------------------
        flowlines_proj = flowlines.to_crs(3857)

        dam_gdf = gpd.GeoDataFrame(
            geometry=[dam_point],
            crs="EPSG:4326"
        ).to_crs(3857)

        dam_point_proj = dam_gdf.geometry.iloc[0]

    # -----------------------------------------
    # FIND NEAREST FLOWLINE
    # -----------------------------------------
        flowlines_proj["distance"] = flowlines_proj.distance(dam_point_proj)

        nearest = flowlines_proj.sort_values("distance").iloc[0]

    # -----------------------------------------
    # EXTRACT COMID and SAVE to GeoPackage
    # -----------------------------------------
        comid = nearest["COMID"]

    # Save COMID
        dams_df.loc[idx, "COMID"] = comid

        print(f"Dam {idx} -> COMID {comid}")

        output_path = f"STRM/{dam_row['OBJECTID']}/flowlines.gpkg"

        flowlines.to_file(
        output_path,
        driver="GPKG"
        )