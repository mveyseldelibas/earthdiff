"""Seasonal-cycle check for vegetation loss ("is this just the harvest?").

For every vegetation-loss polygon we look at the same two time windows in the previous 1 to ``years`` years. If NDVI fell by a similar amount
between those earlier windows too, the loss is a recurring annual cycle (harvest, senescence, grazing, irrigation pattern) and the polygon is
flagged ``likely_seasonal``. Polygons are never removed.

Cost control: only the bounding window of the vegetation-loss pixels is read, only red and NIR, at most ``max_scenes`` scenes per window, and
windows that equal the main before/after windows are reused. Typical extra cost is 2 to 4 small composites.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import numpy as np
from affine import Affine

from .aoi import Grid
from .composite import Reader, build_composite
from .indices import ndvi
from .periods import Period

REPEAT_MIN_DROP = 0.12  # an earlier-year NDVI drop smaller than this does not count as a repeat
REPEAT_FRACTION = 0.5  # ... and it must be at least this fraction of the current drop
MIN_VEG_BEFORE = 0.30  # the earlier "before" window must have been vegetated


def subgrid(mask: np.ndarray, grid: Grid, pad: int = 1) -> tuple[Grid, tuple[int, int, int, int]] | None:
    """Smallest grid window (same pixel lattice) that contains every True pixel of ``mask``."""
    if not mask.any():
        return None
    rows, cols = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
    r0, r1 = max(int(rows[0]) - pad, 0), min(int(rows[-1]) + 1 + pad, grid.height)
    c0, c1 = max(int(cols[0]) - pad, 0), min(int(cols[-1]) + 1 + pad, grid.width)
    t = grid.transform
    sub = Grid(grid.epsg, Affine(grid.res, 0, t.c + c0 * grid.res, 0, -grid.res, t.f - r0 * grid.res), c1 - c0, r1 - r0, grid.res)
    return sub, (r0, r1, c0, c1)


def prior_windows(before: Period, after: Period, years: int) -> list[tuple[Period, Period]]:
    return [(before.shift_years(-k), after.shift_years(-k)) for k in range(1, years + 1)]


def _mean_by_label(labels: np.ndarray, arr: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    ok = np.isfinite(arr) & (labels > 0)
    cnt = np.bincount(labels[ok], minlength=n + 1).astype(np.float64)
    tot = np.bincount(labels[ok], weights=arr[ok].astype(np.float64), minlength=n + 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = tot / cnt
    mean[cnt == 0] = np.nan
    return mean, cnt


def seasonal_check(
    result,
    grid: Grid,
    before: Period,
    after: Period,
    geom,
    backend: str,
    max_cloud: float,
    search: Callable,
    reader: Reader,
    years: int = 2,
    max_scenes: int = 3,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Annotate the vegetation-loss features of ``result`` in place and return a summary dict."""
    say = progress or (lambda m: None)
    t0 = time.monotonic()
    veg_ids = [(f, lid) for f, lid in zip(result.features, result.label_ids) if f["properties"]["class"] == "vegetation_loss"]
    summary: dict[str, Any] = {
        "enabled": True, "years": years, "extra_composites": 0, "windows": [], "checked_polygons": 0, "no_data_polygons": 0,
        "likely_seasonal": {"polygons": 0, "area_ha": 0.0, "share_of_vegetation_loss": 0.0},
    }
    veg_mask = result.classmap == 1
    sg = subgrid(veg_mask, grid)
    if not veg_ids or sg is None:
        summary["elapsed_s"] = round(time.monotonic() - t0, 1)
        return summary
    sub, (r0, r1, c0, c1) = sg
    sl = (slice(r0, r1), slice(c0, c1))
    labels = result.labels[sl].copy()
    veg_labels = {lid for _, lid in veg_ids}
    labels[~np.isin(labels, list(veg_labels))] = 0
    n = int(result.labels.max())
    mask_sub = veg_mask[sl]

    known = {(before.start, before.end): result.indices_before["ndvi"][sl], (after.start, after.end): result.indices_after["ndvi"][sl]}

    def ndvi_of(period: Period) -> np.ndarray | None:
        key = (period.start, period.end)
        if key in known:
            return known[key]
        cands = search(backend, geom, period, max_cloud)
        summary["windows"].append(period.label())
        if not cands:
            known[key] = None
            return None
        comp = build_composite(cands, sub, mask_sub, max_scenes, reader=reader, progress=say, label=f"seasonal {period.label()}", bands=("red", "nir"))
        summary["extra_composites"] += 1
        known[key] = ndvi(comp.bands["nir"], comp.bands["red"]) if comp.scenes else None
        return known[key]

    per_year: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None] = []
    for b_k, a_k in prior_windows(before, after, years):
        say(f"seasonal check: {b_k.label()} -> {a_k.label()}")
        nb_k, na_k = ndvi_of(b_k), ndvi_of(a_k)
        if nb_k is None or na_k is None:
            per_year.append(None)
            continue
        mb, cb = _mean_by_label(labels, nb_k, n)
        ma, ca = _mean_by_label(labels, na_k, n)
        per_year.append((mb, ma, cb, ca, np.bincount(labels.ravel(), minlength=n + 1).astype(np.float64)))

    total_loss = sum(f["properties"]["area_ha"] for f, _ in veg_ids)
    seasonal_ha = 0.0
    for f, lid in veg_ids:
        p = f["properties"]
        cur_drop = abs(p["ndvi_after"] - p["ndvi_before"])
        checked = repeating = 0
        priors: list[float | None] = []
        for item in per_year:
            if item is None:
                priors.append(None)
                continue
            mb, ma, cb, ca, npix = item
            if not (np.isfinite(mb[lid]) and np.isfinite(ma[lid])) or min(cb[lid], ca[lid]) < max(3.0, 0.3 * npix[lid]):
                priors.append(None)
                continue
            d = float(ma[lid] - mb[lid])
            priors.append(round(d, 3))
            checked += 1
            if mb[lid] >= MIN_VEG_BEFORE and d <= -max(REPEAT_MIN_DROP, REPEAT_FRACTION * cur_drop):
                repeating += 1
        p["seasonal_years_checked"] = checked
        p["seasonal_years_repeating"] = repeating
        p["seasonal_prior_dndvi"] = priors
        if checked == 0:
            p["likely_seasonal"] = None
            summary["no_data_polygons"] += 1
            continue
        summary["checked_polygons"] += 1
        p["likely_seasonal"] = bool(repeating >= 1 and repeating / checked >= 0.5)
        if p["likely_seasonal"]:
            summary["likely_seasonal"]["polygons"] += 1
            seasonal_ha += p["area_ha"]
    summary["likely_seasonal"]["area_ha"] = round(seasonal_ha, 2)
    summary["likely_seasonal"]["share_of_vegetation_loss"] = round(seasonal_ha / total_loss, 3) if total_loss > 0 else 0.0
    summary["method"] = (
        f"For each vegetation-loss polygon the same two windows of the previous {years} year(s) are compared (NDVI, red and NIR only, up to {max_scenes} scenes per window). "
        f"A year repeats the loss when NDVI fell by at least max({REPEAT_MIN_DROP}, {REPEAT_FRACTION} x the current drop). likely_seasonal = repeated in at least half of the years that had data. Polygons are never removed."
    )
    summary["elapsed_s"] = round(time.monotonic() - t0, 1)
    return summary
