"""
Constant-value land cover raster helper.

Kept in its own module (rasterio + numpy only) so staging scripts can import
it without pulling in pandas / xarray / geoglows via prep_classes.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio


def make_constant_land_raster(dem_path: str, land_dir, n: float = 0.035, lc_code: int = 1):
    """
    Write a constant-value land cover raster on the DEM grid plus a single-entry
    Manning_n.txt mapping lc_code → n. Returns (raster_path, manning_n_path).
    """
    land_dir = Path(land_dir)
    land_dir.mkdir(parents=True, exist_ok=True)
    land_raster_path = land_dir / "constant_land.tif"
    manning_n_path = land_dir / "Manning_n.txt"

    with rasterio.open(dem_path) as src:
        profile = src.profile.copy()
    profile.update(dtype="uint8", count=1, nodata=0, compress="lzw")
    data = np.full((profile["height"], profile["width"]), lc_code, dtype=np.uint8)

    with rasterio.open(str(land_raster_path), "w", **profile) as dst:
        dst.write(data, 1)

    # ARC overlays the stream raster onto the LC raster using LC_Water_Value
    # (default 80) for stream pixels, so the Manning table must include that
    # code even though our constant LC raster never contains it on disk.
    with open(manning_n_path, "w") as f:
        f.write("LC_Code\tDescription\tMannings_n\n")
        f.write(f"{lc_code}\tconstant\t{n}\n")
        f.write(f"80\twater\t{n}\n")

    return str(land_raster_path), str(manning_n_path)
