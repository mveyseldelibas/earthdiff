"""In-between years for the report timeline.

When the before and after windows are years apart, the timeline would jump straight from one to the other. For every
year in between, this module takes the same season as the after window, finds the clearest Sentinel-2 pass and reads
only a small true-colour frame (the same size as the other timeline frames, from the COG overviews). These frames are
for looking only: they do not change the analysis, which still compares the before and after composites."""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Callable

import numpy as np
from affine import Affine

from .aoi import Grid
from .composite import to_reflectance, valid_from_scl
from .periods import Period
from .render import to_mercator, true_color, webp_bytes
from .report import data_url

MAX_BETWEEN_YEARS = 10  # at most this many in-between frames (spread evenly when the gap is longer)
CANDIDATES_PER_YEAR = 3  # passes checked per year (least cloudy first), the clearest one is used
MIN_CLEAR = 0.5  # a pass must show at least half of the area cloud-free to be used
WIDEN_DAYS = 31  # a year with no clear pass is retried with its window widened by this much on each side
FIRST_DATA = date(2016, 11, 1)  # Sentinel-2 L2A COGs start around then


def between_periods(before: Period, after: Period) -> list[Period]:
    """The after window shifted back by 1, 2, ... years, as long as it lies strictly between the two windows."""
    out: list[Period] = []
    k = 1
    while k < 60:
        p = after.shift_years(-k)
        if p.end < before.end or p.start <= before.end:
            break
        if p.end < after.start:
            out.append(p)
        k += 1
    out.reverse()
    out = [p for p in out if p.end >= FIRST_DATA]  # Sentinel-2 L2A COGs start in 2016-2017: earlier years would only cost searches
    if len(out) > MAX_BETWEEN_YEARS:
        idx = sorted({round(i * (len(out) - 1) / (MAX_BETWEEN_YEARS - 1)) for i in range(MAX_BETWEEN_YEARS)})
        out = [out[i] for i in idx]
    return out


def coarse_grid(grid: Grid, step: int) -> Grid:
    return Grid(grid.epsg, grid.transform * Affine.scale(step), math.ceil(grid.width / step), math.ceil(grid.height / step), grid.res * step)


def between_frames(geom, grid: Grid, mask: np.ndarray, before: Period, after: Period, backend: str, max_cloud: float,
                   step: int, search, reader, say: Callable[[str], None] | None = None, with_nbr: bool = True) -> dict[str, Any]:
    """Frames for the timeline and, with ``with_nbr``, a series point per year (NBR, NDVI, brightness) for ``history.py``."""
    say = say or (lambda m: None)
    periods = between_periods(before, after)
    if not periods:
        return {"frames": [], "series": []}
    g = coarse_grid(grid, step)
    sub = mask[::step, ::step]
    n = max(1, int(sub.sum()))
    say(f"in-between years: one small frame for each of {len(periods)} year(s) ({periods[0].start.year}-{periods[-1].start.year})")
    frames: list[dict[str, Any]] = []
    series: list[dict[str, Any]] = []
    def pick(p: Period, k: int):
        try:
            cands = sorted(search(backend, geom, p, max_cloud), key=lambda s: s.cloud_cover)[:k]
        except Exception:  # a failed search only costs one frame
            return None
        best = None
        for sc in cands:
            try:
                valid = valid_from_scl(reader(sc, "scl", g), g.res) & sub
            except Exception:
                continue
            share = float(valid.sum()) / n
            if share >= MIN_CLEAR and (best is None or share > best[1]):
                best = (sc, share, valid)
        return best

    for p in periods:
        best = pick(p, CANDIDATES_PER_YEAR)
        if best is None:
            # nothing clear enough (cloud, smoke of the very fire we are after...): widen the window by a month on each side, inside the gap
            wide = Period(max(p.start - timedelta(days=WIDEN_DAYS), before.end + timedelta(days=1)), min(p.end + timedelta(days=WIDEN_DAYS), after.start - timedelta(days=1)))
            if wide.start <= wide.end and (wide.start, wide.end) != (p.start, p.end):
                best = pick(wide, CANDIDATES_PER_YEAR + 3)
        if best is None:
            continue
        sc, share, valid = best
        try:
            bands = {}
            for b in ("red", "green", "blue") + (("nir", "swir22") if with_nbr else ()):
                dn = reader(sc, b, g)
                scale, offset = sc.radiometry.get(b, (1e-4, 0.0))
                refl = to_reflectance(dn, scale, offset)
                refl[(dn == 0) | ~valid] = np.nan
                bands[b] = refl
        except Exception:
            if not all(b in bands for b in ("red", "green", "blue")):
                continue
        img, _ = to_mercator(true_color(bands, sub), g, max_dim=None)
        if with_nbr and "nir" in bands and "swir22" in bands:
            from .history import point

            series.append(point("between", sc.date[:10], bands))
        frames.append({"period": "between", "date": sc.date[:10], "scene": sc.id, "clear_fraction": round(share, 3),
                       "img": data_url(webp_bytes(img, quality=80), "image/webp")})
    return {"frames": frames, "series": series}
