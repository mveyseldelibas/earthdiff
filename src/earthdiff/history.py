"""When did a vegetation loss happen, and what was it? Using the in-between years.

Comparing only the two ends hides events that happened in between and have partly recovered since: three years after a
wildfire the burn scar is green again in places, so the before/after dNBR is weak and the loss looks "unclear". When the
windows are years apart earthdiff already reads one pass per in-between year (``yearly.py``); here each vegetation-loss
polygon gets a short series of NBR, NDVI and visible brightness (before, each in-between year, after), the largest
one-step NBR drop is taken as the event, and the cause hint is judged on that step (same rules as ``cause.classify_cause``).

The result is stored on each polygon: ``event_after`` (date of the first pass that shows the loss), ``event_before``
(the pass just before), ``event_dnbr`` (NBR drop of that step) and, when the step says so, a new ``likely_cause``.
It is a hint like the rest: one pass per year, at the coarse frame resolution."""
from __future__ import annotations

from typing import Any

import numpy as np

from .cause import LABELS, _bincount_mean, _summary, classify_cause


def composite_point(kind: str, date: str, comp, step: int) -> dict[str, Any] | None:
    """A series point from a period composite, subsampled to the frame grid."""
    b = comp.bands
    if "swir22" not in b or "nir" not in b:
        return None
    return point(kind, date, {k: b[k][::step, ::step] for k in ("red", "green", "blue", "nir", "swir22")})


def point(kind: str, date: str, bands: dict[str, np.ndarray]) -> dict[str, Any]:
    nir, sw2, red = bands["nir"], bands["swir22"], bands["red"]
    with np.errstate(invalid="ignore", divide="ignore"):
        nbr = (nir - sw2) / (nir + sw2)
        ndvi = (nir - red) / (nir + red)
    vis = (bands["red"] + bands["green"] + bands["blue"]) / 3.0
    return {"kind": kind, "date": date, "nbr": nbr.astype(np.float32), "ndvi": ndvi.astype(np.float32), "vis": vis.astype(np.float32)}


def refine_with_history(result, series: list[dict[str, Any]], step: int) -> dict[str, Any] | None:
    """Re-judge vegetation-loss polygons on their largest one-step NBR drop. Changes features in place; returns new cause totals."""
    series = [s for s in series if s is not None]
    if len(series) < 3 or not any(s["kind"] == "between" for s in series):
        return None
    labels = result.labels[::step, ::step]
    h, w = labels.shape
    n = int(result.labels.max())
    if n == 0:
        return None
    flat = labels.ravel()
    sel = flat > 0
    lab = flat[sel]
    means = []
    for s in series:
        means.append({k: _bincount_mean(lab, s[k][:h, :w].ravel()[sel], n) for k in ("nbr", "ndvi", "vis")})
    changed = 0
    totals: dict[str, dict[str, float]] = {k: {"polygons": 0, "area_ha": 0.0} for k in LABELS}
    for f, lid in zip(result.features, result.label_ids):
        p = f["properties"]
        if p["class"] != "vegetation_loss":
            continue
        nbr = [float(m["nbr"][lid]) for m in means]
        ok = [i for i, v in enumerate(nbr) if np.isfinite(v)]
        if len(ok) >= 3:
            steps = [(nbr[a] - nbr[b], a, b) for a, b in zip(ok, ok[1:])]
            drop, a, b = max(steps)
            if drop > 0.05:
                p["event_before"], p["event_after"] = series[a]["date"], series[b]["date"]
                p["event_dnbr"] = round(drop, 3)
                end = nbr[ok[0]] - nbr[ok[-1]]
                # only re-judge when the step is clearly larger than the end-to-end difference (the scar has faded since)
                if drop >= max(end + 0.05, 1.25 * end):
                    shape = result.shape_stats.get(lid, {})
                    m = {"dnbr": drop, "vis_after": float(means[b]["vis"][lid]), "vis_delta": float(means[b]["vis"][lid] - means[a]["vis"][lid]),
                         "rectangularity": float(shape.get("rectangularity", p.get("rectangularity", 0.0))), "area_ha": float(p["area_ha"])}
                    label, basis = classify_cause(m)
                    if label != p.get("likely_cause"):
                        changed += 1
                    p["likely_cause"] = label
                    p["likely_cause_basis"] = f"largest one-step drop between {series[a]['date']} and {series[b]['date']}; " + "; ".join(basis)
                    p["likely_cause_from"] = "in-between years"
        lc = p.get("likely_cause")
        if lc in totals:
            totals[lc]["polygons"] += 1
            totals[lc]["area_ha"] = round(totals[lc]["area_ha"] + p["area_ha"], 2)
    out = _summary(totals)
    out["from_in_between_years"] = {"series_points": [s["date"] for s in series], "relabelled_polygons": changed}
    return out
