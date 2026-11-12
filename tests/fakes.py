"""Fake STAC search, fake raster reader and fake Nominatim shared by the MCP tests (no network)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import numpy as np

from earthdiff.composite import BANDS_WITH_B12 as BANDS
from earthdiff.geocode import Nominatim, user_agent
from earthdiff.stac import Scene

GOOD_HOST = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/36/S/UF/2024/7/x/"


def make_scene(tag: int, year: int, month: int = 7, hrefs: dict | None = None) -> Scene:
    return Scene(
        id=f"S2A_36SUF_{year}{month:02d}{tag + 1:02d}_{tag}_L2A", datetime=datetime(year, month, 1 + tag, tzinfo=timezone.utc),
        cloud_cover=5.0 + tag, tile="MGRS-36SUF", hrefs=hrefs if hrefs is not None else {}, radiometry={b: (1e-4, 0.0) for b in BANDS},
    )


def fake_search(backend, geom, period, max_cloud=80.0, limit=30):
    return [make_scene(i, period.start.year, period.start.month) for i in range(3)]


def one_scene_search(backend, geom, period, max_cloud=80.0, limit=30):
    return [make_scene(0, period.start.year, period.start.month)]


def evil_search(backend, geom, period, max_cloud=80.0, limit=30):
    bad = {b: "https://evil.example.com/x.tif" for b in ("blue", "green", "red", "nir", "swir16", "scl")}
    return [make_scene(0, period.start.year, period.start.month, hrefs=bad)]


def empty_search(backend, geom, period, max_cloud=80.0, limit=30):
    return []


AFTER_VALUES = {
    "default": dict(blue=1200, green=1500, red=2000, nir=2200, swir16=3500, swir22=3000),  # cleared: mixed evidence -> "unclear"
    "fire": dict(blue=500, green=600, red=700, nir=1500, swir16=3000, swir22=2800),  # dark, strong dNBR
    "clear": dict(blue=2000, green=2300, red=2800, nir=2400, swir16=3200, swir22=2000),  # bright bare soil
}
def _patch_bounds() -> tuple[float, float, float, float]:
    """The square cleared by the fake reader: the north-west quarter of the 31.50,36.80,31.53,36.83 box (UTM 36N)."""
    from pyproj import Transformer

    t = Transformer.from_crs("EPSG:4326", "EPSG:32636", always_xy=True)
    (x0, y0), (x1, y1) = t.transform(31.50, 36.80), t.transform(31.53, 36.83)
    return (x0 + 0.25 * (x1 - x0), y0 + 0.5 * (y1 - y0), x0 + 0.5 * (x1 - x0), y0 + 0.75 * (y1 - y0))


PATCH_UTM36N = _patch_bounds()
BASE_VALUES = dict(blue=500, green=800, red=400, nir=4000, swir16=1500, swir22=900)
SALT_VALUES = dict(blue=3500, green=3800, red=4000, nir=4200, swir16=2000, swir22=2000)  # dry salt: SWIR1 below green, so MNDWI scores it as water
WET_SALT_VALUES = dict(blue=2800, green=3000, red=2700, nir=1000, swir16=500, swir22=300)  # bright but water-like: NDWI > 0, SWIR low


def make_reader(cloud: bool = False, change_year: int | None = 2024, kind: str = "default", repeat_month: int | None = None, salt: bool = False, wet_salt_year: int | None = None):
    """Stable landscape; a square of vegetation is cleared in ``change_year`` (vegetation loss), or every year in month ``repeat_month``
    (a recurring harvest-like cycle). ``kind`` picks the look of the cleared square; ``salt`` makes everything very bright."""
    after = AFTER_VALUES[kind]

    def reader(scene, band, grid):
        h, w = grid.shape
        if band == "scl":
            return np.full((h, w), 9 if cloud else 4, "u1")
        if salt:
            vals = WET_SALT_VALUES if scene.datetime.year == wet_salt_year else SALT_VALUES
            return np.full((h, w), vals[band], "u2")
        arr = np.full((h, w), BASE_VALUES[band], "u2")
        hit = (change_year is not None and scene.datetime.year == change_year) or (repeat_month is not None and scene.datetime.month == repeat_month)
        if hit:  # the square is fixed in map coordinates, so windowed (sub-grid) reads see it at the right place
            t = grid.transform
            xs = t.c + t.a * (np.arange(w) + 0.5)
            ys = t.f + t.e * (np.arange(h) + 0.5)
            x0, y0, x1, y1 = PATCH_UTM36N
            sel = ((ys >= y0) & (ys < y1))[:, None] & ((xs >= x0) & (xs < x1))[None, :]
            arr[sel] = after[band]
        return arr

    return reader


# --- Nominatim
LAKE = {
    "name": "Test Lake", "display_name": "Test Lake, Exampleland", "category": "natural", "type": "water", "lon": "31.5", "lat": "36.8",
    "boundingbox": ["36.78", "36.84", "31.46", "31.56"], "importance": 0.6, "osm_type": "relation", "osm_id": 1,
}
LAKE_DUP = {**LAKE, "display_name": "Test Lake, Exampleland", "osm_type": "node", "osm_id": 2, "lon": "31.51", "lat": "36.81", "boundingbox": ["36.809", "36.811", "31.509", "31.511"]}
TOWN = {
    "name": "Test Lake", "display_name": "Test Lake, Otherland", "category": "place", "type": "town", "lon": "-100.0", "lat": "40.0",
    "boundingbox": ["39.95", "40.05", "-100.05", "-99.95"], "importance": 0.4, "osm_type": "node", "osm_id": 3,
}
HUGE = {
    "name": "Huge Region", "display_name": "Huge Region, Exampleland", "category": "boundary", "type": "administrative", "lon": "32.0", "lat": "37.0",
    "boundingbox": ["36.0", "38.0", "31.0", "33.0"], "importance": 0.7, "osm_type": "relation", "osm_id": 4,
}


def nominatim_with(results_by_query: dict[str, list[dict]], requests: list | None = None, **kw) -> Nominatim:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        q = request.url.params.get("q", "")
        return httpx.Response(200, json=results_by_query.get(q.casefold(), []))

    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": user_agent()})
    kw.setdefault("min_interval_s", 0.0)
    return Nominatim(client=client, **kw)


WIDE = {
    "name": "Wide Valley", "display_name": "Wide Valley, Exampleland", "category": "natural", "type": "valley", "lon": "31.5", "lat": "36.8",
    "boundingbox": ["36.5", "37.1", "31.1", "31.9"], "importance": 0.5, "osm_type": "relation", "osm_id": 5,
}

DEFAULT_RESULTS = {
    "wide valley": [WIDE],
    "test lake": [LAKE, LAKE_DUP, TOWN],
    "test lake, exampleland": [LAKE],
    "huge region": [HUGE],
    "nowhere": [],
}


def default_geocoder(requests: list | None = None, **kw) -> Nominatim:
    return nominatim_with(DEFAULT_RESULTS, requests, **kw)


def dumps(o) -> str:
    return json.dumps(o)
