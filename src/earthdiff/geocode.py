"""Place-name search through OSM Nominatim, following the public usage policy
(https://operations.osmfoundation.org/policies/nominatim/): identifying User-Agent, at most one request per second,
cached results, no bulk use, attribution. Results are (c) OpenStreetMap contributors (ODbL)."""
from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from . import __version__
from .safety import NOMINATIM_HOST, clean_text

NOMINATIM_URL = f"https://{NOMINATIM_HOST}/search"
ATTRIBUTION = "Place search: (c) OpenStreetMap contributors (ODbL), via Nominatim"
REPO_URL = "https://github.com/mveyseldelibas/earthdiff"


class GeocodeError(RuntimeError):
    """The place search failed (network, rate limit, bad response). The message is safe to show to the model."""


@dataclass(frozen=True)
class Place:
    name: str
    display_name: str
    kind: str  # "class/type" as reported by OSM, e.g. "natural/water"
    lon: float
    lat: float
    bbox: tuple[float, float, float, float]  # west, south, east, north
    importance: float
    osm: str  # "relation/123"


def user_agent(contact: str | None = None) -> str:
    ua = f"earthdiff-mcp/{__version__} (+{REPO_URL})"
    if contact:
        ua += f" contact:{clean_text(contact, 80)}"
    return ua


def _place_from(raw: dict[str, Any]) -> Place | None:
    try:
        south, north, west, east = (float(x) for x in raw["boundingbox"])
        lon, lat = float(raw["lon"]), float(raw["lat"])
    except (KeyError, TypeError, ValueError):
        return None
    vals = (south, north, west, east, lon, lat)
    if not all(math.isfinite(v) for v in vals) or not (-90 <= south <= north <= 90 and -180 <= west <= east <= 180):
        return None
    display = clean_text(raw.get("display_name", ""), 240)
    name = clean_text(raw.get("name") or display.split(",")[0], 120)
    return Place(
        name=name,
        display_name=display,
        kind=clean_text(f"{raw.get('category') or raw.get('class', '')}/{raw.get('type', '')}", 60),
        lon=lon,
        lat=lat,
        bbox=(west, south, east, north),
        importance=float(raw.get("importance") or 0.0),
        osm=clean_text(f"{raw.get('osm_type', '')}/{raw.get('osm_id', '')}", 40),
    )


class Nominatim:
    """Rate-limited, caching Nominatim client. One instance per server process."""

    def __init__(
        self,
        contact: str | None = None,
        cache_path: Path | None = None,
        client: httpx.Client | None = None,
        min_interval_s: float = 1.1,
        max_per_hour: int = 60,
        ttl_s: float = 30 * 86400,
        timeout_s: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ):
        self._client = client or httpx.Client(
            headers={"User-Agent": user_agent(contact), "Accept": "application/json"},
            timeout=timeout_s,
            follow_redirects=False,
        )
        self._cache_path = cache_path
        self._min_interval = min_interval_s
        self._max_per_hour = max_per_hour
        self._ttl = ttl_s
        self._clock, self._sleep, self._now = clock, sleep, now
        self._lock = threading.Lock()
        self._last = -1e9
        self._sent: list[float] = []
        self._cache: dict[str, dict[str, Any]] = self._load()

    # -- cache
    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._cache_path:
            return {}
        try:
            data = json.loads(Path(self._cache_path).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        if not self._cache_path:
            return
        try:
            path = Path(self._cache_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if len(self._cache) > 500:  # keep the newest entries only
                keep = sorted(self._cache.items(), key=lambda kv: kv[1].get("t", 0), reverse=True)[:400]
                self._cache = dict(keep)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass  # a cache that cannot be written is not an error

    @staticmethod
    def _key(q: str, countrycodes: str | None, language: str | None, limit: int) -> str:
        return json.dumps([q.casefold(), countrycodes or "", (language or "").casefold(), limit], ensure_ascii=False)

    # -- search
    def search(self, q: str, countrycodes: str | None = None, language: str | None = None, limit: int = 5) -> tuple[list[Place], bool]:
        """Return ``(places, from_cache)``. Raises :class:`GeocodeError`."""
        q = clean_text(q, 200)
        if len(q) < 2:
            raise GeocodeError("the place query is too short")
        key = self._key(q, countrycodes, language, limit)
        with self._lock:
            hit = self._cache.get(key)
            if hit and self._now() - float(hit.get("t", 0)) < self._ttl:
                return [p for p in (_place_from(r) for r in hit["data"]) if p], True

            t = self._clock()
            self._sent = [x for x in self._sent if t - x < 3600]
            if len(self._sent) >= self._max_per_hour:
                raise GeocodeError(
                    f"place search limit reached ({self._max_per_hour} requests per hour per server, to respect the Nominatim usage policy); "
                    "give a bounding box instead or try again later"
                )
            wait = self._min_interval - (t - self._last)
            if wait > 0:
                self._sleep(wait)  # <= 1 request per second, process wide
            params: dict[str, Any] = {"q": q, "format": "jsonv2", "limit": limit, "addressdetails": 0, "dedupe": 1}
            if countrycodes:
                params["countrycodes"] = countrycodes
            if language:
                params["accept-language"] = language
            self._last = self._clock()
            self._sent.append(self._last)
            try:
                resp = self._client.get(NOMINATIM_URL, params=params)
            except httpx.HTTPError as exc:
                raise GeocodeError(f"place search failed ({type(exc).__name__}); try again or give a bounding box") from None
            if resp.status_code in (403, 429, 503):
                raise GeocodeError(
                    f"the Nominatim service refused or throttled the request (HTTP {resp.status_code}); wait a minute or give a bounding box"
                )
            if resp.status_code != 200:
                raise GeocodeError(f"the Nominatim service answered HTTP {resp.status_code}")
            try:
                data = resp.json()
            except ValueError:
                raise GeocodeError("the Nominatim service returned an unreadable answer") from None
            if not isinstance(data, list):
                raise GeocodeError("the Nominatim service returned an unexpected answer")
            slim = [
                {k: r.get(k) for k in ("name", "display_name", "category", "class", "type", "lon", "lat", "boundingbox", "importance", "osm_type", "osm_id")}
                for r in data[:limit]
                if isinstance(r, dict)
            ]
            self._cache[key] = {"t": self._now(), "data": slim}
            self._save()
            return [p for p in (_place_from(r) for r in slim) if p], False

    def close(self) -> None:
        self._client.close()
