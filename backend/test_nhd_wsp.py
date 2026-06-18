"""
Test driver for the NHD-based WSP geometry pipeline.

Runs a full ARC-free geometry estimate for a single dam.  Flowlines and DEMs
are resolved automatically in this priority order:

  1. --staging-dir  (existing stage_nhd_dem + build_trimmed_dems output tree)
  2. backend/cache/ (local tile cache + previously downloaded flowlines)
  3. On-the-fly    (download flowline via NLDI, query TNM + download DEM tiles,
                    warp to a trimmed DEM cached in backend/cache/)

So you can run it with nothing pre-staged and it will fetch what it needs.

Usage
-----
    # Fully automatic — downloads whatever is missing
    python backend/test_nhd_wsp.py \\
        --dam-id 1234 --lat 40.123 --lon -111.456 --comid 9876543

    # Point at an existing staging tree to skip downloads
    python backend/test_nhd_wsp.py \\
        --dam-id 1234 --lat 40.123 --lon -111.456 --comid 9876543 \\
        --staging-dir /data/lhd_staging

    # Override discharge (cms); default uses NWM FDC ep50 (median flow)
    python backend/test_nhd_wsp.py ... --Q 5.0

    # Widen upstream search window for dams with long backwater pools
    python backend/test_nhd_wsp.py ... --search-up 150
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path
from typing import Optional

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pynhd as nhd
import rasterio
from pyproj import Transformer
from rasterio.merge import merge as rio_merge
from shapely.geometry import box

_BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(_BACKEND))

from lhd_processor.download_geospatial_data import (
    download_nhd_flowline,
    download_raw_tile,
    query_dem_tiles,
)
from lhd_processor.hydraulics import solve_weir_geom
from screening.wsp import (
    get_upstream_comid,
    pick_downstream_node,
    pick_upstream_node,
    sample_dem_along_flowlines,
)

_PARQUET_URL  = "lynker-spatial/tabular/riverml_channel_geometry_with_ahg.parquet"
_CACHE_PATH   = _BACKEND / "cache" / "riverml_channel_geometry_with_ahg.parquet"
_DEFAULT_FDC  = _BACKEND.parent / "frontend" / "data" / "nwm_fdc.json"
_DEFAULT_SRC  = _BACKEND.parent / "frontend" / "data" / "synthetic_rating_curves.json"
_RAW_DEM_DIR  = _BACKEND / "cache" / "raw_3dep"
_FLOWLINE_DIR = _BACKEND / "cache" / "flowlines"
_DEM_OUT_DIR  = _BACKEND / "cache" / "dem_trimmed"

# FDC percentile array: [0, 2, 5, 10, 20, 25, 30, 50, 75, 90, 95, 99, 100]
# ep50 = 50th percentile (median flow) is index 7
_EP50_IDX = 7


def _log(msg: str) -> None:
    print(f"[test_nhd_wsp] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Flowline resolution
# ---------------------------------------------------------------------------

def _ensure_flowline(
    dam_id: int,
    lat: float,
    lon: float,
    staging_dir: Optional[Path],
) -> tuple[Path, gpd.GeoDataFrame]:
    """Return (gpkg_path, gdf) — from staging dir, local cache, or fresh download."""

    def _try_dir(d: Path):
        files = list(d.glob("nhd_flowline_*.gpkg")) if d.exists() else []
        if files:
            gdf = gpd.read_file(files[0])
            return files[0], gdf
        return None, None

    # 1. Staging dir
    if staging_dir:
        p, gdf = _try_dir(staging_dir / "STRM" / str(dam_id))
        if p:
            _log(f"Flowline: {p} (staging dir)")
            return p, gdf

    # 2. Local cache
    p, gdf = _try_dir(_FLOWLINE_DIR / str(dam_id))
    if p:
        _log(f"Flowline: {p} (cache)")
        return p, gdf

    # 3. Download
    _log("Flowline not cached — downloading via NLDI ...")
    _FLOWLINE_DIR.mkdir(parents=True, exist_ok=True)
    path_str, gdf = download_nhd_flowline(
        lat, lon,
        flowline_dir=str(_FLOWLINE_DIR),
        site_id=dam_id,
    )
    if path_str is None or gdf is None:
        raise RuntimeError(f"NLDI returned no flowline for dam {dam_id} @ ({lat}, {lon})")
    p = Path(path_str)
    _log(f"Flowline downloaded: {p}")
    return p, gdf


# ---------------------------------------------------------------------------
# DEM resolution
# ---------------------------------------------------------------------------

def _utm_epsg(lon: float, lat: float) -> int:
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    return (32600 if lat >= 0 else 32700) + zone


def _flowline_bbox_wgs84(gdf: gpd.GeoDataFrame, buffer_m: float = 200.0):
    """Return (lon_min, lat_min, lon_max, lat_max) with a metric buffer."""
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    centroid = gdf.geometry.union_all().centroid
    utm = _utm_epsg(centroid.x, centroid.y)
    gdf_utm = gdf.to_crs(epsg=utm)
    minx, miny, maxx, maxy = gdf_utm.total_bounds
    minx -= buffer_m; miny -= buffer_m; maxx += buffer_m; maxy += buffer_m
    tf = Transformer.from_crs(utm, 4326, always_xy=True)
    lon_min, lat_min = tf.transform(minx, miny)
    lon_max, lat_max = tf.transform(maxx, maxy)
    return lon_min, lat_min, lon_max, lat_max


def _tile_covers_bbox(tile_path: Path, bbox_wgs84: tuple) -> bool:
    """True if the raster tile spatially intersects the WGS-84 bbox."""
    lon_min, lat_min, lon_max, lat_max = bbox_wgs84
    target = box(lon_min, lat_min, lon_max, lat_max)
    try:
        with rasterio.open(tile_path) as src:
            tf = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True)
            b = src.bounds
            tile_box = box(
                *tf.transform(b.left, b.bottom),
                *tf.transform(b.right, b.top),
            )
        return tile_box.intersects(target)
    except Exception:
        return False


def _warp_to_trimmed(tile_paths: list[Path], bbox_wgs84: tuple, out_path: Path) -> Path:
    """Mosaic tile_paths and clip to bbox_wgs84 using rasterio.merge + crop."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lon_min, lat_min, lon_max, lat_max = bbox_wgs84

    datasets = [rasterio.open(p) for p in tile_paths]
    try:
        mosaic, mosaic_tf = rio_merge(datasets, bounds=(lon_min, lat_min, lon_max, lat_max))
        profile = datasets[0].profile.copy()
    finally:
        for ds in datasets:
            ds.close()

    profile.update({
        "driver": "GTiff",
        "height": mosaic.shape[1],
        "width":  mosaic.shape[2],
        "transform": mosaic_tf,
        "compress": "lzw",
        "count": 1,
    })
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(mosaic[0], 1)
    return out_path


def _ensure_dem(
    dam_id: int,
    flowline_gdf: gpd.GeoDataFrame,
    staging_dir: Optional[Path],
) -> Path:
    """Return a path to a usable DEM, building/downloading one if needed."""

    # 1. Pre-trimmed DEM in staging dir
    if staging_dir:
        p = staging_dir / "DEM" / str(dam_id) / f"dem_{dam_id}.tif"
        if p.exists():
            _log(f"DEM: {p} (staging dir)")
            return p

    # 2. Previously built trimmed DEM in local cache
    cached = _DEM_OUT_DIR / str(dam_id) / f"dem_{dam_id}.tif"
    if cached.exists():
        _log(f"DEM: {cached} (cache)")
        return cached

    bbox = _flowline_bbox_wgs84(flowline_gdf)

    # 3. Find raw tiles that cover the bbox
    raw_dir = staging_dir / "DEM" / "raw_3dep" if staging_dir else _RAW_DEM_DIR
    covering: list[Path] = []
    if raw_dir.exists():
        for tif in raw_dir.glob("*.tif"):
            if _tile_covers_bbox(tif, bbox):
                covering.append(tif)
        # Also check local cache dir if we looked at a staging dir first
        if staging_dir and _RAW_DEM_DIR.exists():
            for tif in _RAW_DEM_DIR.glob("*.tif"):
                if tif not in covering and _tile_covers_bbox(tif, bbox):
                    covering.append(tif)

    # 4. Download missing tiles from TNM if nothing covers the bbox
    if not covering:
        _log("No cached DEM tiles cover this dam — querying TNM ...")
        tiles = query_dem_tiles(dam_id, flowline_gdf)
        if not tiles:
            raise RuntimeError(f"TNM returned no DEM tiles for dam {dam_id}")
        _RAW_DEM_DIR.mkdir(parents=True, exist_ok=True)
        for tile in tiles:
            _log(f"  Downloading {tile['filename']} ({tile['dataset']}) ...")
            local = download_raw_tile(tile["url"], str(_RAW_DEM_DIR))
            if local:
                covering.append(Path(local))
        if not covering:
            raise RuntimeError(f"DEM tile download failed for dam {dam_id}")

    _log(f"Warping {len(covering)} tile(s) → {cached} ...")
    return _warp_to_trimmed(covering, bbox, cached)


# ---------------------------------------------------------------------------
# Parquet + FDC helpers
# ---------------------------------------------------------------------------

def _ep50_from_fdc(comid: int, fdc_path: Path = _DEFAULT_FDC) -> Optional[float]:
    if not fdc_path.exists():
        return None
    with open(fdc_path) as f:
        raw = json.load(f)
    raw.pop("_meta", None)
    vals = raw.get(str(comid)) or raw.get(f"{comid}.0")
    if not isinstance(vals, list) or len(vals) <= _EP50_IDX:
        return None
    v = float(vals[_EP50_IDX])
    return v if np.isfinite(v) and v > 0 else None


def _load_parquet_rows(comids: list[int]) -> pd.DataFrame:
    if not _CACHE_PATH.exists():
        _log("Lynker parquet not cached — downloading (~188 MB, one-time) ...")
        import s3fs
        fs = s3fs.S3FileSystem(anon=True)
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fs.get(_PARQUET_URL, str(_CACHE_PATH))
    return (
        pq.read_table(
            _CACHE_PATH,
            columns=["feature_id", "owp_tw_bf", "owp_y_bf", "y_coef", "y_exp"],
            filters=[("feature_id", "in", comids)],
        )
        .to_pandas()
        .set_index("feature_id")
    )


def _get_geometry(gdf: gpd.GeoDataFrame, comid: int):
    rows = gdf[gdf["_comid_int"] == comid]
    return rows.iloc[0].geometry if not rows.empty else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dam-id",      type=int,   required=True)
    ap.add_argument("--lat",         type=float, required=True)
    ap.add_argument("--lon",         type=float, required=True)
    ap.add_argument("--comid",       type=int,   required=True)
    ap.add_argument("--staging-dir", type=Path,  default=None,
                    help="Root of an existing stage_nhd_dem / build_trimmed_dems tree")
    ap.add_argument("--Q",           type=float, default=None,
                    help="baseflow discharge (cms); default: NWM FDC ep50 for COMID")
    ap.add_argument("--search-up",   type=float, default=50.0,
                    help="upstream flat-zone search window (m) [50]")
    ap.add_argument("--search-dn",   type=float, default=500.0,
                    help="downstream slope-match search window (m) [500]")
    args = ap.parse_args()

    _log(f"Dam {args.dam_id} | COMID {args.comid} | ({args.lat:.5f}, {args.lon:.5f})")

    # --- Flowline ---
    _, flowline_gdf = _ensure_flowline(args.dam_id, args.lat, args.lon, args.staging_dir)
    flowline_gdf.columns = flowline_gdf.columns.str.lower()
    id_col = next(
        (c for c in ("nhdplusid", "comid", "nhdplus_comid") if c in flowline_gdf.columns), None
    )
    if id_col is None:
        _log("ERROR: no COMID column in flowline gpkg"); return 1
    flowline_gdf["_comid_int"] = pd.to_numeric(
        flowline_gdf[id_col], errors="coerce"
    ).astype("Int64")

    # --- DEM ---
    dem_path = _ensure_dem(args.dam_id, flowline_gdf, args.staging_dir)

    # --- NHDPlus VAA: upstream COMID ---
    _log("Loading NHDPlus VAA table ...")
    vaa_df = nhd.nhdplus_vaa()
    upstream_comid = get_upstream_comid(args.comid, vaa_df)
    _log(f"Upstream COMID: {upstream_comid}")

    dam_geom = _get_geometry(flowline_gdf, args.comid)
    if dam_geom is None:
        _log(f"ERROR: COMID {args.comid} not found in flowline gpkg"); return 1

    up_geom = None
    if upstream_comid is not None:
        up_geom = _get_geometry(flowline_gdf, upstream_comid)
        if up_geom is None:
            _log(f"WARNING: upstream COMID {upstream_comid} not in gpkg — upstream reach omitted")
        else:
            _log(f"Found upstream reach geometry (COMID {upstream_comid})")

    # --- DEM sampling ---
    _log("Sampling DEM along flowlines ...")
    profile = sample_dem_along_flowlines(
        dam_lat=args.lat, dam_lon=args.lon,
        dam_reach_geom=dam_geom, upstream_reach_geom=up_geom,
        dem_path=dem_path,
    )
    valid = profile["elev_m"].notna().sum()
    _log(
        f"Profile: {len(profile)} cells ({valid} valid), "
        f"x=[{profile['x_m'].min():.0f}, {profile['x_m'].max():.0f}] m, "
        f"elev=[{profile['elev_m'].min():.2f}, {profile['elev_m'].max():.2f}] m"
    )

    # --- Energy nodes ---
    us_node = pick_upstream_node(profile, search_m=args.search_up)
    ds_node = pick_downstream_node(profile, search_m=args.search_dn)
    wse_us = float(us_node["elev_m"])
    wse_ds = float(ds_node["elev_m"])
    delta_wse = wse_us - wse_ds

    _log(f"Upstream node:   x={us_node['x_m']:+.1f} m, elev={wse_us:.3f} m")
    _log(f"Downstream node: x={ds_node['x_m']:+.1f} m, elev={wse_ds:.3f} m")
    _log(f"ΔWSE = {delta_wse:.3f} m")

    if delta_wse <= 0:
        _log("ERROR: upstream node not above downstream — check flowline orientation.")
        return 1

    # --- Lynker parquet: crest length + AHG ---
    comids_needed = [args.comid]
    if upstream_comid is not None:
        comids_needed.append(upstream_comid)
    geom_df = _load_parquet_rows(comids_needed)

    if upstream_comid is None or upstream_comid not in geom_df.index:
        _log("ERROR: owp_tw_bf not found for upstream COMID"); return 1
    tw_bf = float(geom_df.loc[upstream_comid, "owp_tw_bf"])
    if not (np.isfinite(tw_bf) and tw_bf > 0):
        _log(f"ERROR: owp_tw_bf = {tw_bf} is invalid"); return 1
    _log(f"Crest length (upstream COMID {upstream_comid} owp_tw_bf): {tw_bf:.2f} m")

    # --- Q: CLI → FDC ep50 → warning ---
    Q = args.Q
    if Q is None:
        Q = _ep50_from_fdc(args.comid)
        if Q is not None:
            _log(f"Q = {Q:.3f} cms  (NWM FDC ep50)")
    if Q is None:
        Q = 1.0
        _log(f"WARNING: ep50 not in nwm_fdc.json for COMID {args.comid} — Q={Q} cms; pass --Q to override")

    # --- Tailwater depth y_T from synthetic rating curve at Q ---
    y_T = None
    src_dict: dict = {}
    if _DEFAULT_SRC.exists():
        with open(_DEFAULT_SRC) as _f:
            src_dict = json.load(_f)
    src = src_dict.get(str(args.comid))
    if src:
        q_arr = src["discharge_cms"]
        s_arr = src["stage_m"]
        y_T = float(np.interp(Q, q_arr, s_arr))
        _log(f"y_T = {y_T:.3f} m  (SRC interpolated at Q={Q:.3f} cms, method={src.get('method')})")
    if y_T is None and args.comid in geom_df.index:
        row = geom_df.loc[args.comid]
        y_coef = float(row["y_coef"])
        y_exp  = float(row["y_exp"])
        if np.isfinite(y_coef) and y_coef > 0 and np.isfinite(y_exp) and y_exp > 0:
            y_T = float(y_coef * Q ** y_exp)
            _log(f"y_T = {y_T:.3f} m  (AHG fallback: y_coef*Q^y_exp)")
    if y_T is None or y_T <= 0:
        y_T = max(delta_wse * 0.1, 0.1)
        _log(f"WARNING: SRC+AHG unavailable — y_T = 10% of ΔWSE = {y_T:.3f} m")

    # --- Energy balance ---
    H, P = solve_weir_geom(Q, tw_bf, y_T, delta_wse)
    if P is None or P <= 0 or P == -9999:
        _log("ERROR: energy balance has no physical solution.")
        _log(f"  Inputs: Q={Q:.3f}, L={tw_bf:.2f}, y_T={y_T:.3f}, ΔWSE={delta_wse:.3f}")
        return 1

    print()
    print("--- RESULT ---")
    print(f"  Dam height P  = {P:.3f} m")
    print(f"  Head H        = {H:.3f} m")
    print(f"  Crest length  = {tw_bf:.2f} m  (upstream COMID {upstream_comid} owp_tw_bf)")
    print(f"  ΔWSE          = {delta_wse:.3f} m")
    print(f"  y_T           = {y_T:.3f} m")
    print(f"  Q             = {Q:.3f} cms  (ep50)")
    print(f"  US node x     = {us_node['x_m']:+.1f} m")
    print(f"  DS node x     = {ds_node['x_m']:+.1f} m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
