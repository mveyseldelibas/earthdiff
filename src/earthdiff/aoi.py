"""Area-of-interest handling: GeoJSON / bbox parsing, UTM grid, area."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from affine import Affine
from pyproj import Transformer
from rasterio import features
from shapely.geometry import box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform, unary_union
from shapely.validation import make_valid

# One area limit for every entry point (CLI, watch, web UI, MCP server). Large areas are read on a coarser grid
# (choose_resolution: 10/20/30/60 m, <= 4 M pixels), so memory stays bounded; run time grows with the area.
MAX_AREA_KM2 = 2000.0

WGS84 = "EPSG:4326"


class AOIError(ValueError):
    pass


def bbox_geometry(west: float, south: float, east: float, north: float) -> BaseGeometry:
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise AOIError("bbox must be west,south,east,north in WGS84 degrees with west<east and south<north")
    return box(west, south, east, north)


def parse_bbox(text: str) -> BaseGeometry:
    try:
        w, s, e, n = (float(x) for x in text.replace(" ", "").split(","))
    except ValueError as exc:
        raise AOIError("bbox must be 'west,south,east,north'") from exc
    return bbox_geometry(w, s, e, n)


def geometry_from_geojson(obj: dict[str, Any]) -> BaseGeometry:
    """Accept FeatureCollection / Feature / bare geometry; return the union of its polygons."""
    kind = obj.get("type")
    if kind == "FeatureCollection":
        geoms = [shape(f["geometry"]) for f in obj.get("features", []) if f.get("geometry")]
    elif kind == "Feature":
        geoms = [shape(obj["geometry"])]
    elif kind in {"Polygon", "MultiPolygon"}:
        geoms = [shape(obj)]
    else:
        raise AOIError(f"unsupported GeoJSON type: {kind!r}")
    polys = []
    for g in geoms:
        if g.geom_type == "Polygon":
            polys.append(g)
        elif g.geom_type == "MultiPolygon":
            polys.extend(g.geoms)
    if not polys:
        raise AOIError("the GeoJSON contains no Polygon/MultiPolygon geometry")
    geom = _polygons_only(unary_union([make_valid(p) for p in polys]))
    if geom.is_empty:
        raise AOIError("AOI geometry is empty")
    minx, miny, maxx, maxy = geom.bounds
    if not (-180 <= minx and maxx <= 180 and -90 <= miny and maxy <= 90):
        raise AOIError("AOI coordinates must be WGS84 longitude/latitude (EPSG:4326)")
    return geom


def _polygons_only(geom: BaseGeometry) -> BaseGeometry:
    if geom.geom_type in {"Polygon", "MultiPolygon"}:
        return geom
    parts = [g for g in getattr(geom, "geoms", []) if g.geom_type in {"Polygon", "MultiPolygon"}]
    return unary_union(parts) if parts else geom.buffer(0)


def load_aoi(path: str | Path) -> BaseGeometry:
    with open(path, "r", encoding="utf-8") as fh:
        return geometry_from_geojson(json.load(fh))


def utm_epsg(lon: float, lat: float) -> int:
    if lat >= 84 or lat <= -80:
        raise AOIError("polar areas (beyond 84N / 80S) are not supported (UTM)")
    zone = int((lon + 180) // 6) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


def to_crs(geom: BaseGeometry, src: str, dst: str) -> BaseGeometry:
    t = Transformer.from_crs(src, dst, always_xy=True)
    return shp_transform(t.transform, geom)


def area_km2(geom_wgs84: BaseGeometry) -> float:
    c = geom_wgs84.representative_point()
    utm = to_crs(geom_wgs84, WGS84, f"EPSG:{utm_epsg(c.x, c.y)}")
    return utm.area / 1e6


@dataclass(frozen=True)
class Grid:
    """Analysis grid: a UTM raster window at a fixed resolution."""

    epsg: int
    transform: Affine
    width: int
    height: int
    res: float

    @property
    def crs(self) -> str:
        return f"EPSG:{self.epsg}"

    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        t = self.transform
        return (t.c, t.f + t.e * self.height, t.c + t.a * self.width, t.f)

    @property
    def pixel_area_ha(self) -> float:
        return self.res * self.res / 10_000.0


def _utm_bounds(geom_wgs84: BaseGeometry) -> tuple[int, tuple[float, float, float, float]]:
    c = geom_wgs84.representative_point()
    epsg = utm_epsg(c.x, c.y)
    return epsg, to_crs(geom_wgs84, WGS84, f"EPSG:{epsg}").bounds


def choose_resolution(geom_wgs84: BaseGeometry, max_pixels: int = 4_000_000) -> float:
    """Smallest of 10/20/30/60 m so that the window stays within ``max_pixels``."""
    _, (minx, miny, maxx, maxy) = _utm_bounds(geom_wgs84)
    w, h = maxx - minx, maxy - miny
    for res in (10.0, 20.0, 30.0, 60.0):
        if (w / res) * (h / res) <= max_pixels:
            return res
    return 60.0


def make_grid(geom_wgs84: BaseGeometry, res: float) -> Grid:
    epsg, (minx, miny, maxx, maxy) = _utm_bounds(geom_wgs84)
    x0 = math.floor(minx / res) * res
    y1 = math.ceil(maxy / res) * res
    width = max(1, int(math.ceil((maxx - x0) / res)))
    height = max(1, int(math.ceil((y1 - miny) / res)))
    return Grid(epsg, Affine(res, 0, x0, 0, -res, y1), width, height, res)


def aoi_mask(geom_wgs84: BaseGeometry, grid: Grid) -> np.ndarray:
    """Boolean raster, True where the pixel centre lies inside the AOI polygon."""
    utm = to_crs(geom_wgs84, WGS84, grid.crs)
    return features.rasterize(
        [(mapping(utm), 1)], out_shape=grid.shape, transform=grid.transform, fill=0, dtype="uint8"
    ).astype(bool)
