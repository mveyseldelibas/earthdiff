"""Terrain for the offline 3D view: tile naming and the embedded grid (no network)."""
from __future__ import annotations

import base64

import numpy as np

from earthdiff.terrain import MESH_MAX_DIM, build_terrain, cop_dem_url, tiles_for


def test_tile_names_and_cover():
    assert cop_dem_url(36, 31).endswith("Copernicus_DSM_COG_10_N36_00_E031_00_DEM/Copernicus_DSM_COG_10_N36_00_E031_00_DEM.tif")
    assert "S01_00_W071_00" in cop_dem_url(-1, -71)
    assert tiles_for(36.7, 31.2, 37.1, 31.7) == [(36, 31), (37, 31)]
    assert tiles_for(36.2, 31.2, 36.8, 31.7) == [(36, 31)]


def test_build_terrain_with_a_fake_reader():
    calls = []

    def reader(url, transform, width, height):
        calls.append(url)
        if "N37" in url:
            return None  # a missing (sea) tile
        yy, xx = np.mgrid[0:height, 0:width]
        return (100 + xx + 2 * yy).astype(np.float32)

    t = build_terrain([[36.80, 31.38], [36.92, 31.52]], reader)
    assert t is not None and calls
    assert t["w"] <= MESH_MAX_DIM and t["h"] <= MESH_MAX_DIM
    z = np.frombuffer(base64.b64decode(t["z"]), dtype="<u2")
    assert z.size == t["w"] * t["h"] and z.max() > 60000  # quantized to the full-resolution min..max
    assert t["min"] < t["max"] and t["relief"].startswith("data:image/webp;base64,")
    (s, w), (n, e) = t["bounds"]
    assert s < 36.80 and n > 36.92 and w < 31.38 and e > 31.52  # padded around the image
    assert "Copernicus DEM" in t["attribution"]


def test_no_tile_means_no_terrain():
    assert build_terrain([[36.80, 31.38], [36.92, 31.52]], lambda *a: None) is None


def test_hillshade_is_lit_from_the_north_west():
    from earthdiff.terrain import hillshade

    x = np.arange(50.0)
    rises_east = np.tile(x * 10, (50, 1))  # faces west
    rises_south = np.tile(x[:, None] * 10, (1, 50))  # row 0 is north: faces north
    assert hillshade(rises_east, 10).mean() > hillshade(-rises_east, 10).mean()
    assert hillshade(rises_south, 10).mean() > hillshade(-rises_south, 10).mean()
    assert abs(hillshade(np.zeros((5, 5)), 10).mean() - np.sin(np.radians(45))) < 1e-9  # flat ground
