"""earthdiff - explainable change reports from free Sentinel-2 imagery."""

import os as _os
from importlib.util import find_spec as _find_spec

__version__ = "0.2.1"


def _prefer_bundled_proj_data() -> None:
    """Machines with PostGIS/QGIS/OSGeo4W often export PROJ_LIB/GDAL_DATA pointing at an *older*
    PROJ database, which makes rasterio fail with 'EPSG code is unknown'. The rasterio wheel ships
    consistent data, so point this process (only) at it. Set EARTHDIFF_KEEP_PROJ_ENV=1 to opt out."""
    if _os.environ.get("EARTHDIFF_KEEP_PROJ_ENV"):
        return
    spec = _find_spec("rasterio")
    if not spec or not spec.submodule_search_locations:
        return
    root = list(spec.submodule_search_locations)[0]
    proj = _os.path.join(root, "proj_data")
    gdal = _os.path.join(root, "gdal_data")
    if _os.path.isdir(proj):
        _os.environ["PROJ_DATA"] = proj
        _os.environ["PROJ_LIB"] = proj
    if _os.path.isdir(gdal):
        _os.environ["GDAL_DATA"] = gdal


_prefer_bundled_proj_data()
