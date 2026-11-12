"""Tool logic of the MCP server, independent of the MCP wiring (so it can be unit-tested with fake STAC and raster readers).

The numerical method is not re-implemented here: ``detect_change`` calls :func:`earthdiff.pipeline.run_analysis`, ``plan_change``
calls :func:`earthdiff.pipeline.plan`. This module adds what an assistant needs around them: input validation and limits,
place search with disambiguation, quality assessment, a compact answer and captioned images.
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry

from . import __version__
from .aoi import MAX_AREA_KM2, AOIError, area_km2, bbox_geometry, choose_resolution, geometry_from_geojson, make_grid
from .change import CLASSES, SENSITIVITY, Thresholds, thresholds_for
from .composite import Reader
from .geocode import ATTRIBUTION as PLACE_ATTRIBUTION, GeocodeError, Nominatim, Place
from .periods import Period, parse_period, season_gap_days
from .pipeline import MAX_WINDOW_DAYS as PIPELINE_MAX_WINDOW_DAYS
from .pipeline import SEASON_GAP_MAX as PIPELINE_SEASON_GAP_MAX
from .pipeline import FIRE_EXPLAINS_SHARE, LARGE_CHANGE_SHARE, InsufficientDataError, RunOptions, fire_like_share, period_warnings, plan, run_analysis
from .reader import cog_reader
from .render import _font
from .safety import DATA_HOSTS, clean_text, host_allowed, redact, run_folder
from .stac import BACKENDS, STACError, Scene, search_scenes

DEFAULT_MAX_AREA_KM2 = MAX_AREA_KM2  # same limit as the CLI and the web UI; operators may lower it (EARTHDIFF_MAX_AREA_KM2)
HARD_MAX_AREA_KM2 = MAX_AREA_KM2
MIN_AREA_KM2 = 0.1
MAX_WINDOW_DAYS = PIPELINE_MAX_WINDOW_DAYS  # same limit as the CLI and the web UI
LONG_WINDOW_DAYS = 92
EARLIEST_L2A = date(2017, 1, 1)
MAX_GEOJSON_CHARS = 200_000
MAX_VERTICES = 50_000
RESOLUTIONS = (10, 20, 30, 60)
LEVEL_ORDER = ("high", "medium", "low")

SCREENING_NOTE = "earthdiff is a screening tool: it flags where free Sentinel-2 imagery changed. It is not an official damage, burned-area, flood or deforestation assessment."


class ToolInputError(ValueError):
    """Bad or unsupported tool input. The message is written for the model and the user."""


class RunStopped(RuntimeError):
    """The analysis was cancelled by the client or exceeded the time limit."""


# ------------------------------------------------------------------------------------------------ settings
@dataclass(frozen=True)
class Settings:
    output_dir: Path = field(default_factory=lambda: Path.home() / "earthdiff-mcp-output")
    max_area_km2: float = DEFAULT_MAX_AREA_KM2
    max_runtime_s: int = 480
    contact: str | None = None
    open_report: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env

        def num(name: str, default: float, lo: float, hi: float) -> float:
            try:
                v = float(e.get(name, "") or default)
            except ValueError:
                v = default
            return min(max(v, lo), hi)

        return cls(
            output_dir=Path(e.get("EARTHDIFF_OUTPUT_DIR") or (Path.home() / "earthdiff-mcp-output")),
            max_area_km2=num("EARTHDIFF_MAX_AREA_KM2", DEFAULT_MAX_AREA_KM2, 1.0, HARD_MAX_AREA_KM2),
            max_runtime_s=int(num("EARTHDIFF_MAX_RUNTIME_S", 480, 60, 1800)),
            contact=clean_text(e.get("EARTHDIFF_CONTACT", ""), 80) or None,
            open_report=(e.get("EARTHDIFF_OPEN_REPORT", "") or "").strip().lower() in {"1", "true", "yes"},
        )


# ------------------------------------------------------------------------------------------------ input parsing
def parse_area(bbox: list[float] | None, geojson: Any) -> BaseGeometry:
    """Exactly one of ``bbox`` ([west, south, east, north], degrees) or ``geojson`` (Polygon/MultiPolygon, WGS84)."""
    if (bbox is None) == (geojson is None):
        raise ToolInputError("give exactly one of bbox ([west, south, east, north] in degrees) or geojson (Polygon/MultiPolygon, WGS84)")
    if isinstance(bbox, str):  # models sometimes send "w,s,e,n"
        try:
            bbox = [float(x) for x in bbox.replace(" ", "").split(",")]
        except ValueError:
            raise ToolInputError("bbox must be four numbers: [west, south, east, north]") from None
    if bbox is not None:
        if len(bbox) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox):
            raise ToolInputError("bbox must be four finite numbers: [west, south, east, north] = [lon_min, lat_min, lon_max, lat_max]")
        try:
            return bbox_geometry(*(float(v) for v in bbox))
        except AOIError as exc:
            raise ToolInputError(str(exc)) from None
    obj = geojson
    if isinstance(obj, str):
        if len(obj) > MAX_GEOJSON_CHARS:
            raise ToolInputError(f"geojson is too large (limit {MAX_GEOJSON_CHARS:,} characters); simplify it or use a bbox")
        try:
            obj = json.loads(obj)
        except ValueError:
            raise ToolInputError("geojson is not valid JSON") from None
    if not isinstance(obj, dict):
        raise ToolInputError("geojson must be a GeoJSON object (Polygon, MultiPolygon, Feature or FeatureCollection)")
    if len(json.dumps(obj)) > MAX_GEOJSON_CHARS:
        raise ToolInputError(f"geojson is too large (limit {MAX_GEOJSON_CHARS:,} characters); simplify it or use a bbox")
    try:
        geom = geometry_from_geojson(obj)
    except AOIError as exc:
        raise ToolInputError(str(exc)) from None
    except Exception:  # malformed coordinates etc.
        raise ToolInputError("could not read the GeoJSON geometry") from None
    from shapely import get_num_coordinates

    if int(get_num_coordinates(geom)) > MAX_VERTICES:
        raise ToolInputError(f"the area has more than {MAX_VERTICES:,} vertices; simplify it or use a bbox")
    return geom


def check_area(geom: BaseGeometry, max_area_km2: float) -> float:
    km2 = area_km2(geom)
    if km2 < MIN_AREA_KM2:
        raise ToolInputError(f"the area is only {km2:.3f} km2; at least {MIN_AREA_KM2:g} km2 is needed (Sentinel-2 pixels are 10 to 60 m)")
    if km2 > max_area_km2:
        raise ToolInputError(
            f"the area is {km2:,.0f} km2 but this server's limit is {max_area_km2:,.0f} km2 (operator setting EARTHDIFF_MAX_AREA_KM2). "
            "Narrow it: pick the part the user cares about, or use the narrowing_options returned by find_place; "
            "do not split a bigger area silently, tell the user which part you analysed"
        )
    return km2


def area_info(geom: BaseGeometry, km2: float) -> dict[str, Any]:
    w, s, e, n = geom.bounds
    c = geom.representative_point()
    return {
        "bbox_west_south_east_north": [round(w, 5), round(s, 5), round(e, 5), round(n, 5)],
        "center_lon_lat": [round(c.x, 5), round(c.y, 5)],
        "area_km2": round(km2, 2),
    }


def parse_periods(after: str, before: str | None, today: date) -> tuple[Period, Period, bool, list[str]]:
    """Parse and validate the two windows. Returns ``(before, after, before_was_defaulted, warnings)``."""
    try:
        p_after = parse_period(clean_text(after, 40))
        p_before = parse_period(clean_text(before, 40)) if before else None
    except ValueError as exc:
        raise ToolInputError(clean_text(exc, 200)) from None
    defaulted = p_before is None
    if p_before is None:
        p_before = p_after.shift_years(-1)
    for label, p in (("after", p_after), ("before", p_before)):
        if p.days() > MAX_WINDOW_DAYS:
            raise ToolInputError(f"the {label} window is {p.days()} days long; the limit is {MAX_WINDOW_DAYS} days. Use a shorter window (one to three months of the same season works best)")
    gap0 = season_gap_days(p_before, p_after)
    if gap0 > PIPELINE_SEASON_GAP_MAX:
        raise ToolInputError(f"the windows are {gap0} days apart in the seasonal cycle (limit {PIPELINE_SEASON_GAP_MAX}): compare the same months of different years, e.g. before 2020-08 and after 2024-08; different seasons make almost everything look changed")
    if p_after.start > today:
        raise ToolInputError(f"the after window starts in the future ({p_after.start.isoformat()}); today is {today.isoformat()}")
    if p_before.start >= p_after.start or p_before.end >= p_after.start:
        raise ToolInputError("the before window must end before the after window starts (before is the earlier period)")
    warns: list[str] = []
    if p_after.end > today:
        warns.append(f"The after window extends beyond today ({today.isoformat()}); only scenes acquired so far can be used.")
    if p_before.start < EARLIEST_L2A:
        warns.append("Sentinel-2 L2A cloud-optimized scenes in the free archives are sparse before 2017; the before period may have no usable scenes.")
    longest = max(p_before.days(), p_after.days())
    if longest > LONG_WINDOW_DAYS:
        warns.append(
            f"A window of {longest} days spans several seasons. The composite uses the clearest scenes, which can come from different times of the year in the two periods; "
            "prefer one to three months of the same season."
        )
    lo, hi = sorted((p_before.days(), p_after.days()))
    if hi > 2 * lo and hi - lo > 30:
        warns.append(f"The windows have very different lengths ({p_before.days()} and {p_after.days()} days), which makes the composites less comparable.")
    return p_before, p_after, defaulted, warns


# ------------------------------------------------------------------------------------------------ bbox helpers
def expand_bbox(bbox: tuple[float, float, float, float], margin_km: float) -> tuple[float, float, float, float]:
    if margin_km <= 0:
        return bbox
    w, s, e, n = bbox
    lat_c = (s + n) / 2
    dlat = margin_km / 111.32
    dlon = margin_km / (111.32 * max(math.cos(math.radians(lat_c)), 0.05))
    return (max(-180.0, w - dlon), max(-90.0, s - dlat), min(180.0, e + dlon), min(90.0, n + dlat))


def _bbox_km2(b: tuple[float, float, float, float]) -> float:
    return area_km2(box(*b))


def centered_bbox_within(bbox: tuple[float, float, float, float], limit_km2: float) -> tuple[float, float, float, float]:
    """The largest box around the centre of ``bbox`` whose area is at most ``limit_km2`` (90 % of it, to leave room)."""
    w, s, e, n = bbox
    cx, cy = (w + e) / 2, (s + n) / 2
    k = math.sqrt(0.9 * limit_km2 / max(_bbox_km2(bbox), 1e-9))
    if k >= 1:
        return bbox
    for _ in range(40):
        hw, hh = (e - w) / 2 * k, (n - s) / 2 * k
        cand = (cx - hw, cy - hh, cx + hw, cy + hh)
        if _bbox_km2(cand) <= limit_km2:
            return cand
        k *= 0.97
    return cand


_ROWS = {1: [""], 2: ["north", "south"], 3: ["north", "middle", "south"]}
_COLS = {1: [""], 2: ["west", "east"], 3: ["west", "middle", "east"]}


def tile_bbox(bbox: tuple[float, float, float, float], limit_km2: float) -> list[dict[str, Any]]:
    """Cover ``bbox`` with the smallest grid (at most 3x3) whose tiles are each within ``limit_km2``; [] if impossible."""
    w, s, e, n = bbox
    for nx, ny in ((1, 2), (2, 1), (2, 2), (1, 3), (3, 1), (2, 3), (3, 2), (3, 3)):
        tiles = []
        ok = True
        for r in range(ny):
            for c in range(nx):
                b = (w + (e - w) * c / nx, n - (n - s) * (r + 1) / ny, w + (e - w) * (c + 1) / nx, n - (n - s) * r / ny)
                a = _bbox_km2(b)
                if a > limit_km2:
                    ok = False
                    break
                parts = [p for p in (_ROWS[ny][r], _COLS[nx][c]) if p]
                label = "-".join(parts) if parts else "all"
                if label == "middle-middle":
                    label = "centre"
                tiles.append({"label": label, "bbox_west_south_east_north": [round(x, 5) for x in b], "area_km2": round(a, 1)})
            if not ok:
                break
        if ok:
            return tiles
    return []


# ------------------------------------------------------------------------------------------------ place disambiguation
def _hav_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12742.0 * math.asin(math.sqrt(a))


def distinct_places(places: list[Place]) -> list[Place]:
    """Drop near-duplicates (same name, centres within 10 km) so that 'ambiguous' means genuinely different places."""
    kept: list[Place] = []
    for p in places:
        dup = any(p.name.casefold() == q.name.casefold() and _hav_km(p.lon, p.lat, q.lon, q.lat) < 10 for q in kept)
        if not dup:
            kept.append(p)
    return kept


# ------------------------------------------------------------------------------------------------ results
def confidence_summary(features: list[dict[str, Any]], bright: dict[str, Any] | None = None) -> dict[str, Any]:
    out = _confidence_summary(features)
    if bright and not bright.get("reliable", True):
        frac = float(bright["fraction_of_comparable_area"])
        if out["area_weighted_mean"] is not None:
            out["area_weighted_mean"] = round(out["area_weighted_mean"] * (1.0 - frac), 2)
        out["level"] = "low"
        out["reliable"] = False
        out["note"] = (
            f"LOWERED: {frac * 100:.0f}% of the comparable area is very bright non-vegetated surface (salt, sand, snow or similar) where change detection is not reliable. "
            "Do not read the absence of change there as no change. " + out["note"]
        )
    return out


def _confidence_summary(features: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(f["properties"]["area_ha"] for f in features)
    if not features or total <= 0:
        return {"area_weighted_mean": None, "level": None, "share_of_changed_area": {k: 0.0 for k in LEVEL_ORDER}, "note": "no change polygons"}
    mean = sum(f["properties"]["confidence"] * f["properties"]["area_ha"] for f in features) / total
    share = {k: 0.0 for k in LEVEL_ORDER}
    for f in features:
        share[f["properties"]["confidence_level"]] += f["properties"]["area_ha"] / total
    from .change import confidence_level

    return {
        "area_weighted_mean": round(mean, 2),
        "level": confidence_level(mean),
        "share_of_changed_area": {k: round(v, 3) for k, v in share.items()},
        "note": "Heuristic score from 0 to 1 (magnitude of the index change, number of valid observations, size). It is not a probability of being correct.",
    }


def assess_quality(summary: dict[str, Any], before: Period, after: Period) -> dict[str, Any]:
    """Data-quality grade of the comparison itself (not an accuracy claim)."""
    poor: list[str] = []
    fair: list[str] = []
    vf = float(summary["valid_fraction"])
    if vf < 0.60:
        poor.append(f"only {vf * 100:.0f}% of the area is cloud-free in both periods")
    elif vf < 0.85:
        fair.append(f"{vf * 100:.0f}% of the area is cloud-free in both periods")
    gap = season_gap_days(before, after)
    if gap > 45:
        poor.append(f"the windows are {gap} days apart in the seasonal cycle")
    bs = summary.get("bright_surface") or {}
    if bs and not bs.get("reliable", True):
        poor.append(f"{bs['fraction_of_comparable_area'] * 100:.0f}% of the area is very bright non-vegetated surface (salt, sand, snow): change there is not reliable")
    if float(summary.get("changed_fraction_of_aoi", 0.0)) > LARGE_CHANGE_SHARE:
        # a large changed share is a mismatch symptom, unless dNBR says most of it is one burn (then it is the event itself)
        if fire_like_share(summary.get("totals") or {}, summary.get("likely_cause")) < FIRE_EXPLAINS_SHARE:
            poor.append(f"{summary['changed_fraction_of_aoi'] * 100:.0f}% of the area is flagged, which usually points to a seasonal or atmospheric mismatch")
    for label in ("before", "after"):
        n = int(summary[f"scenes_{label}"])
        if n == 0:
            poor.append(f"no usable scene in the {label} period")
        elif n == 1:
            fair.append(f"a single scene in the {label} period")
        elif n < 3:
            fair.append(f"only {n} scenes in the {label} period")
    level = "poor" if poor else "fair" if fair else "good"
    return {
        "level": level,
        "reasons": poor + fair,
        "note": "Grades the data behind the comparison, not the correctness of the polygons; even 'good' is unverified by field data.",
    }


def top_areas(features: list[dict[str, Any]], n: int = 8) -> list[dict[str, Any]]:
    out = []
    for f in features[:n]:
        p = f["properties"]
        out.append(
            {
                "id": p["id"],
                "class": p["class"],
                "area_ha": p["area_ha"],
                "confidence": p["confidence"],
                "confidence_level": p["confidence_level"],
                "centroid_lon_lat": [p["centroid_lon"], p["centroid_lat"]],
                "ndvi_before_after": [p["ndvi_before"], p["ndvi_after"]],
                "mndwi_before_after": [p["mndwi_before"], p["mndwi_after"]],
                "bsi_before_after": [p["bsi_before"], p["bsi_after"]],
                "valid_obs_before_after": [p["valid_obs_before"], p["valid_obs_after"]],
            }
        )
        for k in ("likely_cause", "likely_cause_basis", "dnbr", "likely_seasonal", "bright_share"):
            if p.get(k) is not None:
                out[-1][k] = p[k]
    return out


def captioned_jpeg(path: Path, caption: str, max_dim: int = 960, quality: int = 82) -> bytes:
    """Flatten an RGBA map image onto light grey, downscale, add a caption strip and return JPEG bytes (small enough for chat)."""
    im = Image.open(path).convert("RGBA")
    bg = Image.new("RGBA", im.size, (232, 232, 232, 255))
    bg.alpha_composite(im)
    im = bg.convert("RGB")
    k = min(1.0, max_dim / max(im.size))
    if k < 1.0:
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
    font = _font(max(14, im.width // 45))
    strip = font.size + 14
    sheet = Image.new("RGB", (im.width, im.height + strip), (255, 255, 255))
    ImageDraw.Draw(sheet).text((8, 6), clean_text(caption, 120), fill=(20, 20, 20), font=font)
    sheet.paste(im, (0, strip))
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


# ------------------------------------------------------------------------------------------------ progress
_BAND_ORDER = ("blue", "green", "red", "nir", "swir16", "swir22")


class ProgressMapper:
    """Maps the pipeline's progress lines to a monotonic 0..100 value and a path-free message."""

    def __init__(self) -> None:
        self.value = 0.0
        self.text = "starting"

    def update(self, msg: str) -> tuple[float, str]:
        m = msg.strip()
        v, text = self.value, None
        if m.startswith("AOI"):
            v, text = 2, "area prepared"
        elif m.startswith("searching"):
            v, text = 4, "searching Sentinel-2 scenes"
        elif m.startswith("found"):
            v, text = 7, m
        elif (mm := re.match(r"(before|after): checking cloud cover", m)):
            base = 8 if mm.group(1) == "before" else 47
            v, text = base, f"{mm.group(1)}: checking cloud cover of candidate scenes"
        elif (mm := re.match(r"(before|after): downloading \+ compositing band '(\w+)'", m)):
            base = 14 if mm.group(1) == "before" else 53
            idx = _BAND_ORDER.index(mm.group(2)) if mm.group(2) in _BAND_ORDER else 0
            v, text = base + idx * 5.4, f"{mm.group(1)}: downloading band {idx + 1} of 6 ({mm.group(2)})"
        elif m.startswith("seasonal"):
            v, text = max(self.value, 84), "seasonal check: comparing with earlier years"
        elif m.startswith("detecting"):
            v, text = 88, "detecting changes"
        elif m.startswith("rendering"):
            v, text = 94, "rendering report and maps"
        elif m.startswith("done"):
            v, text = 99, "finished"
        if text is None:
            text = redact(m, ())[:100]
        self.value = max(self.value, float(v))
        self.text = text
        return self.value, self.text


# ------------------------------------------------------------------------------------------------ runtime estimate
def runtime_hint(pixels: int, scenes: int) -> str:
    """Coarse expectation, from the validation runs on a home connection (see validation/mcp_questions.md)."""
    work = pixels * max(scenes, 1)
    if work < 3_000_000:
        return "usually under 1 minute"
    if work < 20_000_000:
        return "usually 1 to 3 minutes"
    if work < 60_000_000:
        return "usually 2 to 5 minutes"
    return "can take 4 to 8 minutes; consider a smaller area or a coarser resolution_m"


SEASONAL_YEARS = 2


def seasonal_plan(before: Period, after: Period, years: int, enabled: bool) -> dict[str, Any]:
    """What the optional seasonal check would do and cost (measured on the validation runs, see validation/mcp_questions.md)."""
    from .seasonal import prior_windows

    windows: dict[tuple, str] = {}
    for b, a in prior_windows(before, after, years):
        for p in (b, a):
            windows[(p.start, p.end)] = p.label()
    known = {(before.start, before.end), (after.start, after.end)}
    new = [lab for k, lab in windows.items() if k not in known]
    return {
        "enabled": enabled,
        "earlier_years_checked": years,
        "extra_windows": new,
        "extra_composites_at_most": len(new),
        "extra_time": "typically adds about 45 seconds or more (measured +47 s on a 39 km2 test: 4 small composites, +75% on a 61 s run); it is off by default",
        "what_it_does": "flags vegetation-loss polygons whose NDVI also fell in the same windows of earlier years (likely harvest or a seasonal cycle); polygons are never removed",
    }


@dataclass
class DetectOutcome:
    result: dict[str, Any]
    images: list[tuple[str, bytes]]  # (caption, JPEG)
    report_path: Path


# ------------------------------------------------------------------------------------------------ service
class EarthDiffService:
    """All tool logic. Data sources are injectable for tests; the defaults are the real STAC API, the real COGs and Nominatim."""

    def __init__(
        self,
        settings: Settings,
        search=search_scenes,
        reader: Reader = cog_reader,
        geocoder: Nominatim | None = None,
        today: Callable[[], date] = date.today,
    ):
        self.settings = settings
        self._search = search
        self._reader = reader
        self._geocoder = geocoder
        self._today = today
        self._run_lock = threading.Lock()

    # -- network allow-list
    def _scene_ok(self, s: Scene) -> bool:
        return all(host_allowed(h, DATA_HOSTS) for h in s.hrefs.values())

    def guarded_search(self, backend, geom, period, max_cloud=80.0, limit=30, retries=3):
        scenes = self._search(backend, geom, period, max_cloud)
        return [s for s in scenes if self._scene_ok(s)]

    def guarded_reader(self, scene: Scene, band: str, grid) -> Any:
        url = scene.hrefs.get(band)
        if url is not None and not host_allowed(url, DATA_HOSTS):
            raise RuntimeError("refusing to read from a host outside the Sentinel-2 data allow-list")
        return self._reader(scene, band, grid)

    def _geo(self) -> Nominatim:
        if self._geocoder is None:
            cache = Path(self.settings.output_dir) / ".cache" / "nominatim.json"
            self._geocoder = Nominatim(contact=self.settings.contact, cache_path=cache)
        return self._geocoder

    def _redact(self, text: object) -> str:
        return redact(text, (str(Path.home()), str(self.settings.output_dir), os.getcwd()))

    # -- find_place
    def find_place(self, query: str, country_codes: str | None = None, language: str | None = None, limit: int = 5, margin_km: float = 0.0) -> dict[str, Any]:
        q = clean_text(query, 200)
        if len(q) < 2:
            raise ToolInputError("query must be a place name of at least two characters")
        if country_codes is not None:
            country_codes = country_codes.strip().lower().replace(" ", "")
            if not re.fullmatch(r"[a-z]{2}(,[a-z]{2}){0,9}", country_codes):
                raise ToolInputError("country_codes must be comma-separated ISO 3166-1 alpha-2 codes, e.g. 'tr' or 'us,ca'")
        if language is not None and not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?", language.strip()):
            raise ToolInputError("language must be a language tag such as 'en' or 'tr'")
        limit = int(limit)
        if not 1 <= limit <= 5:
            raise ToolInputError("limit must be between 1 and 5")
        if not 0 <= margin_km <= 20:
            raise ToolInputError("margin_km must be between 0 and 20")
        try:
            places, cached = self._geo().search(q, country_codes, language.strip() if language else None, limit)
        except GeocodeError as exc:
            raise ToolInputError(str(exc)) from None
        max_km2 = self.settings.max_area_km2
        distinct = distinct_places(places)
        cands = []
        for i, p in enumerate(distinct, 1):
            b = expand_bbox(p.bbox, margin_km)
            km2 = _bbox_km2(b)
            c: dict[str, Any] = {
                "rank": i,
                "name": p.name,
                "display_name": p.display_name,
                "osm_kind": p.kind,
                "osm": p.osm,
                "center_lon_lat": [round(p.lon, 5), round(p.lat, 5)],
                "bbox_west_south_east_north": [round(x, 5) for x in b],
                "area_km2": round(km2, 2),
                "fits_analysis_limit": km2 <= max_km2,
            }
            if km2 < MIN_AREA_KM2:
                c["note"] = "This is a point-like feature with a tiny bounding box; pass margin_km (for example 2 to 5) or give a larger bbox."
            if km2 > max_km2:
                tiles = tile_bbox(b, max_km2)
                cb = centered_bbox_within(b, max_km2)
                c["narrowing_options"] = {
                    "why": f"The bounding box is {km2:,.0f} km2 and the analysis limit is {max_km2:,.0f} km2. Ask the user which part matters, or offer one of these.",
                    "centered_bbox": {"bbox_west_south_east_north": [round(x, 5) for x in cb], "area_km2": round(_bbox_km2(cb), 1)},
                    "tiles": tiles,
                    "tiles_note": None if tiles else "The area is too large to cover with at most 9 tiles; ask the user for a smaller area of interest.",
                }
            cands.append(c)
        if not cands:
            status, nxt = "not_found", "No match. Ask the user for a more specific name (add the country or region) or for a bounding box."
        elif len(cands) == 1:
            status = "ok"
            nxt = "One candidate. Confirm with the user if the place name could mean something else, then call plan_change or detect_change with its bbox."
            if not cands[0]["fits_analysis_limit"]:
                nxt = "The only candidate is larger than the analysis limit: offer the narrowing_options to the user; do not shrink it silently."
        else:
            status = "ambiguous"
            nxt = (
                "Several different places match. Do not guess: show the candidates and ask which one the user means "
                "(unless the user already made it clear, for example 'the lake'; then say which candidate you used)."
            )
        return {
            "status": status,
            "query": q,
            "candidates": cands,
            "analysis_limit_km2": max_km2,
            "margin_km": margin_km,
            "next_step": nxt,
            "from_cache": cached,
            "attribution": PLACE_ATTRIBUTION,
        }

    # -- plan_change
    def _prepare(self, bbox, geojson, after, before) -> tuple[BaseGeometry, float, Period, Period, bool, list[str]]:
        geom = parse_area(bbox, geojson)
        km2 = check_area(geom, self.settings.max_area_km2)
        p_before, p_after, defaulted, warns = parse_periods(after, before, self._today())
        return geom, km2, p_before, p_after, defaulted, warns

    def plan_change(
        self,
        bbox: list[float] | None = None,
        geojson: Any = None,
        after: str = "",
        before: str | None = None,
        max_cloud: float = 80.0,
        resolution_m: int | None = None,
        backend: str = "earth-search",
        seasonal_check: bool = False,
    ) -> dict[str, Any]:
        self._check_common(max_cloud, resolution_m, backend)
        geom, km2, p_before, p_after, defaulted, warns = self._prepare(bbox, geojson, after, before)
        res = float(resolution_m) if resolution_m else choose_resolution(geom)
        grid = make_grid(geom, res)
        opts = RunOptions(name="plan", backend=backend, resolution=res, max_cloud=max_cloud, max_area_km2=self.settings.max_area_km2)
        try:
            info = plan(geom, p_before, p_after, opts, search=self.guarded_search)
        except (STACError, AOIError) as exc:
            raise ToolInputError(self._redact(exc)) from None
        warnings = list(period_warnings(p_before, p_after)) + warns
        status = "ready"
        for label, p, n, clouds in (("before", p_before, info["before_candidates"], info["before_cloud"]), ("after", p_after, info["after_candidates"], info["after_cloud"])):
            if n == 0:
                status = "no_data"
                warnings.append(f"No Sentinel-2 L2A scene with less than {max_cloud:g}% cloud found for the {label} window ({p.label()}). Use another window or raise max_cloud.")
            elif n == 1:
                warnings.append(f"Only one candidate scene in the {label} window: the composite would be a single acquisition.")
            elif n < 3:
                warnings.append(f"Only {n} candidate scenes in the {label} window: little averaging of residual cloud or haze.")
            if clouds and sorted(clouds)[len(clouds) // 2] > 50:
                warnings.append(f"Most candidate scenes in the {label} window are cloudy (median scene cloud cover {sorted(clouds)[len(clouds) // 2]:.0f}%); the cloud-free share of the area may be low.")
        scenes_used = min(8, info["before_candidates"]) + min(8, info["after_candidates"])
        return {
            "status": status,
            "today": self._today().isoformat(),
            "area": area_info(geom, km2),
            "before_window": {"label": p_before.label(), "start": p_before.start.isoformat(), "end": p_before.end.isoformat(), "defaulted_to_same_period_last_year": defaulted},
            "after_window": {"label": p_after.label(), "start": p_after.start.isoformat(), "end": p_after.end.isoformat()},
            "season_gap_days": season_gap_days(p_before, p_after),
            "resolution_m": info["resolution_m"],
            "grid_pixels": grid.width * grid.height,
            "candidate_scenes": {
                "before": info["before_candidates"],
                "after": info["after_candidates"],
                "before_dates": info["before_dates"][:20],
                "after_dates": info["after_dates"][:20],
            },
            "expected_runtime": runtime_hint(grid.width * grid.height, scenes_used),
            "seasonal_check": seasonal_plan(p_before, p_after, SEASONAL_YEARS, seasonal_check),
            "warnings": warnings,
            "next_step": "Call detect_change with the same arguments." if status == "ready" else "Adjust the windows (or area) and plan again before running detect_change.",
            "attribution": f"Scene search: {BACKENDS[backend].attribution}",
        }

    def _check_common(self, max_cloud: float, resolution_m: int | None, backend: str) -> None:
        if backend not in BACKENDS:
            raise ToolInputError(f"backend must be one of: {', '.join(BACKENDS)}")
        if not 10 <= max_cloud <= 100:
            raise ToolInputError("max_cloud must be between 10 and 100 (percent scene-level cloud cover)")
        if resolution_m is not None and resolution_m not in RESOLUTIONS:
            raise ToolInputError("resolution_m must be one of 10, 20, 30, 60 (metres) or omitted for automatic")

    # -- detect_change
    def detect_change(
        self,
        bbox: list[float] | None = None,
        geojson: Any = None,
        after: str = "",
        before: str | None = None,
        name: str | None = None,
        sensitivity: str = "default",
        resolution_m: int | None = None,
        mmu_ha: float = 0.5,
        max_cloud: float = 80.0,
        backend: str = "earth-search",
        seasonal_check: bool = False,
        progress: Callable[[float, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> DetectOutcome:
        self._check_common(max_cloud, resolution_m, backend)
        if sensitivity not in SENSITIVITY:
            raise ToolInputError(f"sensitivity must be one of: {', '.join(SENSITIVITY)}")
        if not 0.1 <= mmu_ha <= 50:
            raise ToolInputError("mmu_ha must be between 0.1 and 50 hectares")
        geom, km2, p_before, p_after, defaulted, extra_warns = self._prepare(bbox, geojson, after, before)
        display = clean_text(name, 80) if name else "Area of interest"
        if not display:
            display = "Area of interest"
        if not self._run_lock.acquire(blocking=False):
            raise ToolInputError("another analysis is still running in this server; wait for it to finish (one at a time protects memory)")
        t0 = time.monotonic()
        out: Path | None = None
        try:
            out = run_folder(self.settings.output_dir, display)
            mapper = ProgressMapper()

            def say(msg: str) -> None:
                if (should_stop and should_stop()) or time.monotonic() - t0 > self.settings.max_runtime_s:
                    raise RunStopped(
                        f"the analysis was stopped after {time.monotonic() - t0:.0f} s (limit {self.settings.max_runtime_s} s, operator setting EARTHDIFF_MAX_RUNTIME_S). "
                        "Try a smaller area, a coarser resolution_m or shorter windows."
                    )
                if progress:
                    progress(*mapper.update(msg))

            opts = RunOptions(
                name=display, backend=backend, resolution=float(resolution_m) if resolution_m else None, max_scenes=8, max_cloud=max_cloud,
                sensitivity=sensitivity, mmu_ha=mmu_ha, max_area_km2=self.settings.max_area_km2, force=False,
                likely_cause=True, seasonal_check=seasonal_check, seasonal_years=SEASONAL_YEARS,
            )
            try:
                res = run_analysis(geom, p_before, p_after, out, opts, progress=say, reader=self.guarded_reader, search=self.guarded_search)
            except InsufficientDataError as exc:
                msg = re.sub(r"\s*Widen the date windows, use other months, or pass --force to run anyway\.", " Widen the windows or use other months.", str(exc))
                msg = msg.replace("Use a longer window, a different month, or raise --max-cloud.", "Use a longer window, a different month, or raise max_cloud.")
                raise ToolInputError(self._redact(msg)) from None
            except (STACError, AOIError) as exc:
                raise ToolInputError(self._redact(exc)) from None
            geojson_data = json.loads(res.files["geojson"].read_text(encoding="utf-8"))
            result = self._build_result(res, geojson_data["features"], p_before, p_after, defaulted, extra_warns, display, time.monotonic() - t0)
            images = [
                (f"Before  {p_before.label()}", captioned_jpeg(res.files["before"], f"Before  {p_before.label()}  (Sentinel-2 true colour)")),
                (f"After  {p_after.label()}", captioned_jpeg(res.files["after"], f"After  {p_after.label()}  (Sentinel-2 true colour)")),
                ("Detected changes", captioned_jpeg(res.files["change_map"], "Detected changes (hectares per class in the legend)", max_dim=1000)),
            ]
            return DetectOutcome(result, images, res.files["report"])
        except BaseException:
            if out is not None:
                try:
                    out.rmdir()  # only succeeds when empty: nothing is ever deleted
                except OSError:
                    pass
            raise
        finally:
            self._run_lock.release()

    def _build_result(self, res, features, p_before, p_after, defaulted, extra_warns, display, elapsed_s) -> dict[str, Any]:
        s = res.summary
        warnings = list(s["warnings"])
        for w in extra_warns:
            if w not in warnings:
                warnings.append(w)
        totals = s["totals"]
        changes = [
            {"class": key, "label": t["label"], "area_ha": t["area_ha"], "polygons": t["polygons"]}
            for key, t in totals.items()
        ]
        total_ha = round(sum(t["area_ha"] for t in totals.values()), 2)
        files = {
            "output_dir": str(res.out_dir),
            "report_html": str(res.files["report"]),
            "changes_geojson": str(res.files["geojson"]),
            "summary_json": str(res.files["summary"]),
            "before_png": str(res.files["before"]),
            "after_png": str(res.files["after"]),
            "change_map_png": str(res.files["change_map"]),
            "overview_png": str(res.files["overview"]),
            "change_class_tif": str(res.files["class_tif"]),
            "before_tif": str(res.files["before_tif"]),
            "after_tif": str(res.files["after_tif"]),
        }
        if "xlsx" in res.files:
            files["changes_xlsx"] = str(res.files["xlsx"])
        return {
            "name": display,
            "earthdiff_version": __version__,
            "before_window": {"label": p_before.label(), "defaulted_to_same_period_last_year": defaulted},
            "after_window": {"label": p_after.label()},
            "area_km2": s["aoi_km2"],
            "resolution_m": s["resolution_m"],
            "comparable_fraction": s["valid_fraction"],
            "scenes": {
                "before": s["scenes_before"],
                "after": s["scenes_after"],
                "detail": [{"period": x["period"], "date": x["date"], "scene_cloud_cover": round(x["cloud_cover"], 1), "cloud_free_share_of_area": x["valid_fraction"]} for x in s["scenes"]],
            },
            "changes": changes,
            "total_changed_ha": total_ha,
            "changed_fraction_of_area": s["changed_fraction_of_aoi"],
            "confidence": confidence_summary(features, s.get("bright_surface")),
            "vegetation_loss_likely_cause": s.get("likely_cause"),
            "seasonal_check": s.get("seasonal_check"),
            "bright_surface": s.get("bright_surface"),
            "data_quality": assess_quality(s, p_before, p_after),
            "largest_areas": top_areas(features),
            "warnings": warnings,
            "files": files,
            "attribution": s["attribution"],
            "backend": s["backend_attribution"],
            "elapsed_seconds": round(elapsed_s, 1),
            "present_to_user": [
                "The three images (before, after, detected changes) are returned with this result; refer to them.",
                "Give hectares and polygon counts per class, the comparable share of the area, the confidence and the data quality, and EVERY warning above.",
                "Say what the classes mean (a spectral change). likely_cause is only a hint from dNBR, brightness and shape: say 'fire-like' or 'clearing-like', never 'it was a fire'; drought, harvest and construction can look alike.",
                "If seasonal_check is present, report the hectares flagged likely_seasonal (they repeat in earlier years, so probably harvest or a seasonal cycle); those polygons are kept.",
                "If bright_surface.reliable is false, say that change on salt, sand or snow is not reliable and that no change there must not be read as no change; the confidence was lowered.",
                SCREENING_NOTE,
                f"Credit the data: {s['attribution']}.",
                "Tell the user where report.html (interactive swipe map) and changes.geojson were saved.",
            ],
        }

    # -- explain_method
    def explain_method(self, topic: str = "all") -> dict[str, Any]:
        topics = ("all", "rules", "pipeline", "likely_cause", "seasonal", "bright_surfaces", "confidence", "limits", "data")
        if topic not in topics:
            raise ToolInputError(f"topic must be one of: {', '.join(topics)}")
        t = Thresholds()
        rules = [
            {"class": c.key, "label": c.label, "rule": c.rule}
            for c in CLASSES.values()
        ]
        sections: dict[str, Any] = {
            "rules": {
                "summary": "Five plain spectral-index rules on median composites; no machine-learning model. d = after minus before.",
                "indices": {
                    "NDVI": "(NIR - Red) / (NIR + Red), bands B08 and B04",
                    "MNDWI": "(Green - SWIR1) / (Green + SWIR1), bands B03 and B11",
                    "BSI": "((SWIR1 + Red) - (NIR + Blue)) / ((SWIR1 + Red) + (NIR + Blue))",
                },
                "classes": rules,
                "default_thresholds": t.as_dict(),
                "sensitivity_presets": {k: v for k, v in SENSITIVITY.items()},
                "priority_when_rules_overlap": "new water > water loss > vegetation loss > vegetation gain > new bare soil / built-up",
            },
            "pipeline": [
                "Search Sentinel-2 L2A scenes of each window in a public STAC API (Element 84 Earth Search by default).",
                "Read only the pixels of the area from cloud-optimized GeoTIFFs and warp them to a UTM grid (10, 20, 30 or 60 m).",
                "Mask no-data, saturated, cloud, cloud shadow, cirrus and snow pixels with the scene classification layer; clouds and shadows are buffered by 30 m.",
                "Keep the best scenes by cloud-free coverage of YOUR area (up to 8 per window) and take the per-pixel median.",
                "Apply the rules, remove narrow noise (opening for pixels of 20 m or finer), drop blobs below the minimum mapping unit (default 0.5 ha), polygonize.",
                "Area is the pixel count times the pixel area on the UTM grid.",
            ],
            "likely_cause": {
                "applies_to": "vegetation_loss polygons only (not a new class: a field of each polygon)",
                "labels": {
                    "fire-like": "dNBR at least 0.27 (moderate-low burn severity) AND a dark surface after (visible reflectance <= 0.09) AND more evidence for fire (also an irregular outline) than for clearing",
                    "clearing-like": "little or no burn signal (dNBR below 0.27), or a straight-edged, bright outline: felling, harvest, ploughing, construction",
                    "unclear": "mixed or weak evidence; the default whenever the measurements disagree or B12 is missing",
                },
                "measurements": ["dnbr = NBR before - NBR after, NBR = (NIR - SWIR2) / (NIR + SWIR2), bands B08 and B12", "visible reflectance after = mean(blue, green, red)", "rectangularity = area / area of the minimum rotated rectangle"],
                "thresholds": {
                    "dnbr": "Key & Benson (2006) FIREMON severity classes, also used by Copernicus EFFIS: 0.10 / 0.27 / 0.44 / 0.66 (derived for Landsat in specific ecosystems, indicative only)",
                    "own_heuristics": "visible reflectance after <= 0.09 dark, >= 0.20 bright; rectangularity >= 0.80 straight-edged, <= 0.60 irregular. These are this project's, checked on two public cases only.",
                },
                "caveat": "A hint, not a verdict. 'fire-like' does not prove a fire and 'clearing-like' does not prove a clearing; a wrong hint is expected in some cases. Never state a cause as fact.",
            },
            "seasonal": {
                "what": "Optional (seasonal_check=true, off by default). For each vegetation-loss polygon the same two windows of the previous 2 years are compared (red and NIR only, up to 3 scenes per window, only the area around the loss). If NDVI fell by at least max(0.12, half the current drop) in at least half of the earlier years that had data, the polygon is flagged likely_seasonal.",
                "use": "Recurring annual drops are typically harvest, senescence or irrigation cycles. Polygons are never removed; report the flagged hectares separately.",
                "limits": "Needs scenes in earlier years (nothing before 2017); a one-off event that happens to repeat is flagged too; slow multi-year trends are not captured.",
            },
            "bright_surfaces": {
                "rule": "A pixel is very bright when mean(blue, green, red) >= 0.25 and NDVI < 0.15 in the before or after composite (salt pans, sand, snow, bare limestone, concrete).",
                "effect": f"If at least 20% of the comparable area is bright, a warning is added, bright_surface.reliable is false, the overall confidence is capped at 'low' and scaled down, and polygons on bright pixels get up to 50% lower confidence. data_quality becomes 'poor'.",
                "meaning": "The vegetation and water rules are unreliable on such surfaces and true-colour images saturate; absence of detected change there must not be read as no change.",
                "exploratory_signal": "On the bright pixels the share of water-like pixels by NDWI = (green - NIR) / (green + NIR) > 0 is reported before and after (water_like_share_of_bright_pixels). A large change suggests a salt flat or shallow lake got wetter or drier, which MNDWI cannot show because dry salt scores as water in MNDWI. It is not a mapped change.",
            },
            "confidence": {
                "formula": "0.5 x magnitude + 0.3 x valid observations + 0.2 x size",
                "magnitude": "how far the mean index change exceeds the class threshold (saturates at twice the threshold)",
                "valid_observations": "the smaller number of cloud-free observations of the two periods (saturates at 3)",
                "size": "log10(area / minimum mapping unit), saturates at 10 times the unit",
                "levels": "0.70 and above is high, 0.45 and above is medium, otherwise low",
                "caveat": "A heuristic score, not a probability. data_quality in the result grades the comparison itself (cloud-free share, seasons, scene count).",
            },
            "limits": {
                "intended_use": "Preliminary screening of land, water, vegetation and surface changes at area scale.",
                "not": [
                    "an official damage, burned-area, flood or deforestation assessment",
                    "an explanation of the cause (fire, felling, harvest, drought and construction can look alike)",
                    "able to see built-up growth on ground that was already bare",
                    "reliable for features narrower than a few pixels or smaller than the minimum mapping unit",
                ],
                "known_error_sources": [
                    "seasonal change and agriculture (compare the same season of different years)",
                    "cloud, haze, cloud shadow, terrain shadow, snow, bright salt or sand surfaces (flagged by the bright-surface warning)",
                    "turbid, shallow or algae-covered water, wet soil",
                    "co-registration of about 10 m and sun/view-angle differences, which create edge false alarms",
                    "scene availability: some regions and years have few cloud-free scenes",
                ],
                "server_limits": {
                    "max_area_km2": self.settings.max_area_km2,
                    "max_window_days": MAX_WINDOW_DAYS,
                    "min_area_km2": MIN_AREA_KM2,
                    "max_runtime_s": self.settings.max_runtime_s,
                    "one_analysis_at_a_time": True,
                },
                "not_for": "surveillance, targeting, or tracking people or vehicles",
            },
            "data": {
                "imagery": "Copernicus Sentinel-2 L2A, free and open. Served as cloud-optimized GeoTIFFs by Element 84 Earth Search (AWS Open Data) or Microsoft Planetary Computer.",
                "place_search": "OpenStreetMap Nominatim, (c) OpenStreetMap contributors (ODbL), at most one request per second, results cached.",
                "attribution": "Contains modified Copernicus Sentinel data [years of the scenes used]",
                "network": "Only the STAC API, Sentinel-2 COG storage hosts and nominatim.openstreetmap.org are contacted. No account or API key.",
                "files": "Written only to the configured output folder (EARTHDIFF_OUTPUT_DIR).",
            },
        }
        if topic == "all":
            return {"version": __version__, **sections, "screening_note": SCREENING_NOTE}
        return {"version": __version__, topic: sections[topic], "screening_note": SCREENING_NOTE}
