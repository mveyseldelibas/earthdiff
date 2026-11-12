"""Windowed reads of Sentinel-2 COGs onto the analysis grid (never whole tiles)."""
from __future__ import annotations

import time

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT

from .aoi import Grid
from .stac import Scene

GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff,.TIF",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "GDAL_HTTP_TIMEOUT": "60",
    "VSI_CACHE": "TRUE",
    "GDAL_NUM_THREADS": "1",
}


def read_to_grid(href: str, grid: Grid, categorical: bool = False, retries: int = 3) -> np.ndarray:
    """Read one band of a COG, warped/resampled onto ``grid``. 0 is the nodata value."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with rasterio.Env(**GDAL_ENV):
                with rasterio.open(href) as src:
                    native = abs(src.transform.a)
                    if categorical:
                        rs = Resampling.nearest
                    elif grid.res > native * 1.01:
                        rs = Resampling.average
                    else:
                        rs = Resampling.bilinear
                    with WarpedVRT(
                        src,
                        crs=grid.crs,
                        transform=grid.transform,
                        width=grid.width,
                        height=grid.height,
                        resampling=rs,
                        src_nodata=0,
                        nodata=0,
                    ) as vrt:
                        return vrt.read(1)
        except Exception as exc:  # pragma: no cover - network dependent
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"could not read {href}: {last}")


def cog_reader(scene: Scene, band: str, grid: Grid) -> np.ndarray:
    """Default reader: ``band`` is a logical name ('red', 'nir', 'scl', ...)."""
    return read_to_grid(scene.hrefs[band], grid, categorical=(band == "scl"))
