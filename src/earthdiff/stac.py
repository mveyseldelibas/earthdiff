"""Sentinel-2 L2A discovery through public STAC APIs (no account, no API key)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from shapely.geometry.base import BaseGeometry

from .periods import Period


@dataclass(frozen=True)
class Backend:
    name: str
    url: str
    collection: str
    # logical band name -> STAC asset key
    assets: dict[str, str]
    attribution: str
    signed: bool = False


BACKENDS: dict[str, Backend] = {
    "earth-search": Backend(
        name="earth-search",
        url="https://earth-search.aws.element84.com/v1",
        collection="sentinel-2-l2a",
        assets={"blue": "blue", "green": "green", "red": "red", "nir": "nir", "swir16": "swir16", "swir22": "swir22", "scl": "scl"},
        attribution="Element 84 Earth Search (Sentinel-2 L2A COGs on AWS Open Data)",
    ),
    "planetary-computer": Backend(
        name="planetary-computer",
        url="https://planetarycomputer.microsoft.com/api/stac/v1",
        collection="sentinel-2-l2a",
        assets={"blue": "B02", "green": "B03", "red": "B04", "nir": "B08", "swir16": "B11", "swir22": "B12", "scl": "SCL"},
        attribution="Microsoft Planetary Computer (Sentinel-2 L2A)",
        signed=True,
    ),
}


@dataclass
class Scene:
    id: str
    datetime: datetime
    cloud_cover: float
    tile: str
    hrefs: dict[str, str]
    # logical band -> (scale, offset): reflectance = DN * scale + offset
    radiometry: dict[str, tuple[float, float]] = field(default_factory=dict)

    @property
    def date(self) -> str:
        return self.datetime.date().isoformat()


class STACError(RuntimeError):
    pass


def _radiometry(item: Any, asset_key: str, backend: str) -> tuple[float, float]:
    """(scale, offset) so that reflectance = DN * scale + offset.

    ESA processing baseline >= 04.00 adds +1000 to the DN (offset -0.1 must be applied).
    Planetary Computer serves the ESA files as they are. Element84 Earth Search has already
    removed the offset for the reprocessed items (``earthsearch:boa_offset_applied`` = true) even
    though the asset's ``raster:bands`` still lists -0.1 - verified empirically (red DN 1st
    percentile is far below 1000 on baseline 05.xx scenes) - so we must NOT subtract again.
    """
    props = item.properties
    baseline = str(props.get("s2:processing_baseline", "0") or "0")
    try:
        new_baseline = float(baseline) >= 4.0
    except ValueError:
        new_baseline = False
    if backend == "earth-search":
        applied = bool(props.get("earthsearch:boa_offset_applied", False))
        return 1e-4, (-0.1 if (new_baseline and not applied) else 0.0)
    rb = item.assets[asset_key].extra_fields.get("raster:bands") or []
    if rb and "scale" in rb[0]:
        return float(rb[0]["scale"]), float(rb[0].get("offset", 0.0))
    return 1e-4, (-0.1 if new_baseline else 0.0)


def _dedupe(scenes: list[Scene]) -> list[Scene]:
    """Keep one scene per (tile, acquisition date): the highest reprocessing sequence."""
    best: dict[tuple[str, str], Scene] = {}
    for s in scenes:
        key = (s.tile, s.date)
        if key not in best or s.id > best[key].id:
            best[key] = s
    return list(best.values())


def search_scenes(
    backend: str,
    geom_wgs84: BaseGeometry,
    period: Period,
    max_cloud: float = 80.0,
    limit: int = 30,
    retries: int = 3,
) -> list[Scene]:
    """Return up to ``limit`` least-cloudy scenes (by scene-level cloud cover) in ``period``."""
    from pystac_client import Client

    if backend not in BACKENDS:
        raise STACError(f"unknown backend {backend!r}; choose from {', '.join(BACKENDS)}")
    be = BACKENDS[backend]
    modifier = None
    if be.signed:
        try:
            import planetary_computer
        except ImportError as exc:  # pragma: no cover
            raise STACError("backend 'planetary-computer' needs: pip install planetary-computer") from exc
        modifier = planetary_computer.sign_inplace

    last: Exception | None = None
    items: list[Any] = []
    for attempt in range(retries):
        try:
            client = Client.open(be.url, modifier=modifier)
            search = client.search(
                collections=[be.collection],
                bbox=list(geom_wgs84.bounds),
                datetime=period.stac(),
                query={"eo:cloud_cover": {"lt": max_cloud}},
                max_items=400,
            )
            items = list(search.items())
            last = None
            break
        except Exception as exc:  # network / API hiccup
            last = exc
            time.sleep(2 * (attempt + 1))
    if last is not None:
        raise STACError(f"STAC search failed ({be.url}): {last}") from last

    scenes: list[Scene] = []
    for it in items:
        try:
            hrefs = {k: it.assets[v].href for k, v in be.assets.items()}
        except KeyError:
            continue
        # some old Earth Search items point to requester-pays JPEG2000 on s3://; only public COGs are supported
        if not all(h.startswith("http") and h.lower().split("?")[0].endswith((".tif", ".tiff")) for h in hrefs.values()):
            continue
        radio = {k: _radiometry(it, v, backend) for k, v in be.assets.items() if k != "scl"}
        tile = str(it.properties.get("grid:code") or it.properties.get("s2:mgrs_tile") or it.id)
        scenes.append(
            Scene(
                id=it.id,
                datetime=it.datetime,
                cloud_cover=float(it.properties.get("eo:cloud_cover", 100.0)),
                tile=tile,
                hrefs=hrefs,
                radiometry=radio,
            )
        )
    scenes = _dedupe(scenes)
    scenes.sort(key=lambda s: (s.cloud_cover, s.date))
    return scenes[:limit]
