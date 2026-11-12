"""Terrain for the offline 3D view and the relief basemap of the report.

Elevation comes from the Copernicus DEM GLO-30 (30 m, free, public COGs on AWS Open Data). Only the area around the
analysis is read, decimated from the COG overviews, onto a small Web Mercator grid; the report embeds the result, so
opening the report needs no internet. Sea areas have no DEM tile: they get 0 m."""
from __future__ import annotations

import base64
import math
from typing import Any, Callable

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.vrt import WarpedVRT

from .reader import GDAL_ENV
from .render import webp_bytes
from .report import data_url

COP_DEM_HOST = "copernicus-dem-30m.s3.amazonaws.com"
DEM_ATTRIBUTION = ("Copernicus DEM GLO-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018, "
                   "provided under COPERNICUS by the European Union and ESA")
PAD = 0.35  # the terrain reaches this fraction beyond the image on every side, for context around the area
RELIEF_MAX_DIM = 640  # relief basemap (2D) pixels on the longest side
MESH_MAX_DIM = 200  # 3D mesh vertices on the longest side (200 x 200 = 40,000 vertices, fits 16-bit indices)

DemReader = Callable[[str, Any, int, int], "np.ndarray | None"]


def cop_dem_url(lat: int, lon: int) -> str:
    """URL of the 1x1 degree GLO-30 tile whose south-west corner is (lat, lon)."""
    name = f"Copernicus_DSM_COG_10_{'N' if lat >= 0 else 'S'}{abs(lat):02d}_00_{'E' if lon >= 0 else 'W'}{abs(lon):03d}_00_DEM"
    return f"https://{COP_DEM_HOST}/{name}/{name}.tif"


def tiles_for(south: float, west: float, north: float, east: float) -> list[tuple[int, int]]:
    return [(la, lo) for la in range(math.floor(south), math.floor(north - 1e-9) + 1) for lo in range(math.floor(west), math.floor(east - 1e-9) + 1)]


def read_dem_window(url: str, transform, width: int, height: int) -> np.ndarray | None:
    """Default reader: one DEM tile warped onto the Web Mercator target grid (NaN outside the tile); None if the tile does not exist."""
    try:
        with rasterio.Env(**GDAL_ENV):
            with rasterio.open(url) as src:
                with WarpedVRT(src, crs="EPSG:3857", transform=transform, width=width, height=height,
                               resampling=Resampling.bilinear, src_nodata=src.nodata, nodata=np.nan, dtype="float32") as vrt:
                    return vrt.read(1)
    except Exception:  # sea tiles do not exist (404); a network error only costs the 3D view
        return None


def _shape(xmin: float, ymin: float, xmax: float, ymax: float, max_dim: int) -> tuple[int, int]:
    k = max_dim / max(xmax - xmin, ymax - ymin)
    return max(8, round((xmax - xmin) * k)), max(8, round((ymax - ymin) * k))


def hillshade(dem: np.ndarray, px_m: float, azimuth: float = 315.0, altitude: float = 45.0) -> np.ndarray:
    """Classic hillshade (0..1) of ``dem`` with square pixels of ``px_m`` metres."""
    # rows run north to south, so the northward slope is -d/drow; the light comes from ``azimuth`` (clockwise from north)
    gy, gx = np.gradient(dem, px_m)
    dzdx, dzdn = gx, -gy
    az, alt = math.radians(azimuth), math.radians(altitude)
    lx, ly, lz = math.sin(az) * math.cos(alt), math.cos(az) * math.cos(alt), math.sin(alt)
    return np.clip((-dzdx * lx - dzdn * ly + lz) / np.sqrt(dzdx ** 2 + dzdn ** 2 + 1), 0, 1)


def build_terrain(image_bounds: list[list[float]], reader: DemReader = read_dem_window) -> dict[str, Any] | None:
    """Terrain around ``image_bounds`` ([[south, west], [north, east]]): a relief PNG for the 2D basemap and a quantized
    elevation grid for the 3D mesh. None when no DEM tile could be read (the report then has no 3D view)."""
    (s, w), (n, e) = image_bounds
    to_m = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    to_ll = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    x0, y0 = to_m.transform(w, s)
    x1, y1 = to_m.transform(e, n)
    dx, dy = (x1 - x0) * PAD, (y1 - y0) * PAD
    x0, y0, x1, y1 = x0 - dx, y0 - dy, x1 + dx, y1 + dy
    width, height = _shape(x0, y0, x1, y1, RELIEF_MAX_DIM)
    transform = from_bounds(x0, y0, x1, y1, width, height)
    tw, ts = to_ll.transform(x0, y0)
    te, tn = to_ll.transform(x1, y1)

    dem = np.full((height, width), np.nan, dtype=np.float32)
    got = 0
    for la, lo in tiles_for(ts, tw, tn, te):
        arr = reader(cop_dem_url(la, lo), transform, width, height)
        if arr is None:
            continue
        got += 1
        ok = np.isfinite(arr)
        dem[ok] = arr[ok]
    if got == 0:
        return None
    dem = np.nan_to_num(dem, nan=0.0)  # no tile = sea

    lat0 = math.radians((ts + tn) / 2)
    px_true = (x1 - x0) / width * math.cos(lat0)  # mercator metres shrink by cos(lat) on the ground
    shade = hillshade(dem, px_true)
    lo_e, hi_e = float(dem.min()), float(dem.max())
    tint = np.clip((dem - lo_e) / max(hi_e - lo_e, 1.0), 0, 1)
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    base = 0.35 + 0.65 * shade
    rgba[..., 0] = np.clip((180 + 40 * tint) * base, 0, 255)
    rgba[..., 1] = np.clip((190 + 25 * tint) * base, 0, 255)
    rgba[..., 2] = np.clip((170 + 10 * tint) * base, 0, 255)
    rgba[..., 3] = 255

    step = max(1, math.ceil(max(width, height) / MESH_MAX_DIM))
    mesh = dem[::step, ::step]
    q = np.round((mesh - lo_e) / max(hi_e - lo_e, 1e-6) * 65535).astype("<u2") if hi_e > lo_e else np.zeros(mesh.shape, "<u2")
    return {
        "bounds": [[ts, tw], [tn, te]],
        "relief": data_url(webp_bytes(rgba, quality=82), "image/webp"),
        "w": int(mesh.shape[1]), "h": int(mesh.shape[0]),
        "min": round(lo_e, 1), "max": round(hi_e, 1),
        "z": base64.b64encode(q.tobytes()).decode("ascii"),
        "attribution": DEM_ATTRIBUTION,
    }
