"""
Flowline → stream raster helpers used by ARC.

Kept in their own module (numpy + geopandas + rasterio + gdal only) so
staging scripts can import them without pulling pandas / xarray / geoglows
through prep_classes.
"""
from __future__ import annotations

import gc

import numpy as np
import geopandas as gpd
import rasterio
from rasterio.features import rasterize

try:
    import gdal
    import gdal_array
except ImportError:
    from osgeo import gdal, gdal_array


def read_raster_w_gdal(input_raster: str):
    """Reads raster data into an array and returns spatial metadata."""
    dataset = gdal.Open(input_raster, gdal.GA_ReadOnly)
    geotransform = dataset.GetGeoTransform()
    band = dataset.GetRasterBand(1)
    raster_array = band.ReadAsArray()
    ncols, nrows = dataset.RasterXSize, dataset.RasterYSize
    cellsize = geotransform[1]
    yll = geotransform[3] - nrows * abs(geotransform[5])
    yur = geotransform[3]
    xll = geotransform[0]
    xur = xll + ncols * geotransform[1]
    lat = abs((yll + yur) / 2.0)
    raster_proj = dataset.GetProjectionRef()
    del dataset, band
    return raster_array, ncols, nrows, cellsize, yll, yur, xll, xur, lat, geotransform, raster_proj


def write_output_raster(s_output_filename, raster_data, dem_geotransform, dem_projection):
    """Writes a numpy array to a GeoTIFF file."""
    gdal_dtype = gdal_array.NumericTypeCodeToGDALTypeCode(raster_data.dtype) or gdal.GDT_Float32
    n_rows, n_cols = raster_data.shape
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(s_output_filename, xsize=n_cols, ysize=n_rows, bands=1, eType=gdal_dtype)
    ds.SetGeoTransform(dem_geotransform)
    ds.SetProjection(dem_projection)
    ds.GetRasterBand(1).WriteArray(raster_data)
    ds.FlushCache()
    del ds


def clean_strm_raster(strm_tif: str, clean_strm_tif: str) -> None:
    """Robust filter to remove single cells and redundant thickness for ARC compatibility."""
    (SN, ncols, nrows, cellsize, yll, yur, xll, xur, lat, gt, proj) = read_raster_w_gdal(strm_tif)
    B = np.zeros((nrows + 2, ncols + 2))
    B[1:nrows + 1, 1:ncols + 1] = np.where(SN > 0, SN, 0)

    del SN
    gc.collect()

    (RR, CC) = np.where(B > 0)
    num_nonzero = len(RR)

    for filterpass in range(2):
        for x in range(num_nonzero):
            r, c = RR[x], CC[x]
            if B[r, c] > 0:
                if B[r, c + 1] == 0 and B[r, c - 1] == 0:
                    if (B[r + 1, c - 1:c + 2].sum() == 0 and B[r - 1, c] > 0) or \
                            (B[r - 1, c - 1:c + 2].sum() == 0 and B[r + 1, c] > 0):
                        B[r, c] = 0
                elif B[r + 1, c] == B[r, c] and (B[r + 1, c + 1] == B[r, c] or B[r + 1, c - 1] == B[r, c]):
                    if sum(B[r + 1, c - 1:c + 2]) == B[r, c] * 2:
                        B[r + 1, c] = 0
    write_output_raster(clean_strm_tif, B[1:nrows + 1, 1:ncols + 1], gt, proj)


def create_arc_strm_raster(StrmSHP, output_raster_path, DEM_File, value_field):
    """Rasterize a flowline file to match the extent and resolution of a DEM."""
    gdf = gpd.read_file(StrmSHP)
    with rasterio.open(DEM_File) as ref:
        meta, ref_transform, out_shape, crs = ref.meta.copy(), ref.transform, (ref.height, ref.width), ref.crs
    if gdf.crs != crs:
        gdf = gdf.to_crs(crs)
    shapes = [(geom, val) for geom, val in zip(gdf.geometry, gdf[value_field])]
    raster = rasterize(shapes=shapes, out_shape=out_shape, fill=0, transform=ref_transform, dtype="int32")
    meta.update({"driver": "GTiff", "dtype": "int32", "count": 1, "nodata": 0})
    with rasterio.open(output_raster_path, "w", **meta) as dst:
        dst.write(raster, 1)
