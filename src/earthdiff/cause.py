"""Likely cause of vegetation loss: fire-like, clearing-like or unclear.

This is a *hint*, never a verdict. Vegetation loss is first found by the NDVI rule (``change.py``); here each polygon is described by
a few measurements and compared with published thresholds:

* **dNBR** (differenced Normalized Burn Ratio, NBR = (NIR - SWIR2) / (NIR + SWIR2), Sentinel-2 B08 and B12; dNBR = NBR before - NBR after).
  Burn-severity classes of Key & Benson (2006), "Landscape assessment (LA)", FIREMON, USDA Forest Service RMRS-GTR-164-CD, as used by the
  Copernicus EFFIS fire-severity product (https://forest-fire.emergency.copernicus.eu/about-effis/technical-background/fire-severity)
  and described by UN-SPIDER (burn severity recommended practice):
  < 0.10 unburned or regrowth, 0.10-0.27 low, 0.27-0.44 moderate-low, 0.44-0.66 moderate-high, >= 0.66 high severity.
  The thresholds were derived for Landsat in specific ecosystems and vary between regions and land covers, so they are indicative only.
* **Post-event brightness** and **shape** are NOT taken from a standard: burn scars are usually darker (char, ash) than the bare soil that
  fields and clear-cuts expose, and clearings and fields have straight edges (area close to the area of the minimum rotated rectangle).
  These thresholds are ours (``DARK_VIS_AFTER``, ``BRIGHT_VIS_AFTER``, ``RECT_MIN``, ``IRREGULAR_MAX``), set by looking at two public cases only (Manavgat 2021 fire, Istanbul Airport 2017-2019 construction; see validation/)
  and may not transfer to other regions, seasons or fire ages (old, ashy or grey burn scars are brighter than fresh char).

Labels (stored in ``likely_cause`` of the feature, with the measurements and a plain-language basis):
``fire-like``      dNBR at least moderate-low AND a dark post-event surface AND more evidence for fire than for clearing
``clearing-like``  little or no burn signal, or straight-edged and bright, i.e. more evidence for clearing, harvest or construction
``unclear``        mixed or weak evidence (the default whenever the measurements disagree)
"""
from __future__ import annotations

from typing import Any

import numpy as np

from .aoi import Grid

DNBR_LOW = 0.10  # Key & Benson 2006: below this: unburned / regrowth
DNBR_MODERATE_LOW = 0.27  # Key & Benson 2006: lower bound of "moderate-low" severity
DNBR_MODERATE_HIGH = 0.44  # Key & Benson 2006: lower bound of "moderate-high" severity
DARK_VIS_AFTER = 0.09  # ours: mean(blue, green, red) after, at or below = dark surface (char / ash). Manavgat median 0.055, Istanbul Airport 5th percentile 0.092
BRIGHT_VIS_AFTER = 0.20  # ours: at or above = bright bare surface (soil, stubble, concrete)
RECT_MIN = 0.80  # ours: area / minimum rotated rectangle at or above = straight-edged (field, clear-cut, site)
IRREGULAR_MAX = 0.60  # ours: at or below = irregular outline

LABELS = ("fire-like", "clearing-like", "unclear")
SOURCES = [
    "Key & Benson (2006) FIREMON Landscape Assessment: dNBR severity thresholds 0.10 / 0.27 / 0.44 / 0.66",
    "Copernicus EFFIS fire severity (same thresholds applied to Sentinel-2): https://forest-fire.emergency.copernicus.eu/about-effis/technical-background/fire-severity",
    "UN-SPIDER recommended practice: burn severity mapping with NBR / dNBR",
]


def classify_cause(m: dict[str, float]) -> tuple[str, list[str]]:
    """``m`` holds dnbr, vis_after, vis_delta, rectangularity, area_ha. Returns (label, basis sentences)."""
    fire: list[str] = []
    clear: list[str] = []
    d = m["dnbr"]
    if d is None or not np.isfinite(d):
        return "unclear", ["no SWIR2 (B12) measurement, so no burn signal could be computed"]
    if d >= DNBR_MODERATE_HIGH:
        fire.append(f"dNBR {d:.2f} (at least moderate-high burn severity by Key & Benson thresholds)")
    elif d >= DNBR_MODERATE_LOW:
        fire.append(f"dNBR {d:.2f} (at least moderate-low burn severity)")
    elif d < DNBR_LOW:
        clear.append(f"dNBR {d:.2f} (below 0.10: no burn signal)")
    else:
        clear.append(f"dNBR {d:.2f} (low, 0.10 to 0.27: weak burn signal)")
    if m["vis_after"] <= DARK_VIS_AFTER:
        fire.append(f"dark after (visible reflectance {m['vis_after']:.2f}), like char or ash")
    elif m["vis_after"] >= BRIGHT_VIS_AFTER:
        clear.append(f"bright after (visible reflectance {m['vis_after']:.2f}), like exposed soil or stubble")
    r = m["rectangularity"]
    if r >= RECT_MIN:
        clear.append(f"straight-edged outline (rectangularity {r:.2f})")
    elif r <= IRREGULAR_MAX:
        fire.append(f"irregular outline (rectangularity {r:.2f})")

    burn_signal = d >= DNBR_MODERATE_LOW
    dark = m["vis_after"] <= DARK_VIS_AFTER
    if burn_signal and dark and len(fire) > len(clear):  # dNBR alone is not enough (harvest and clearing raise it too): the surface must also be dark
        return "fire-like", fire + clear
    if (not burn_signal) and len(clear) >= len(fire) and len(clear) >= 1:
        return "clearing-like", clear + fire
    if burn_signal and len(clear) > len(fire):
        return "clearing-like", clear + fire
    return "unclear", fire + clear


def _bincount_mean(labels_flat: np.ndarray, arr_flat: np.ndarray, n: int) -> np.ndarray:
    ok = np.isfinite(arr_flat)
    cnt = np.bincount(labels_flat[ok], minlength=n + 1).astype(np.float64)
    tot = np.bincount(labels_flat[ok], weights=arr_flat[ok].astype(np.float64), minlength=n + 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = tot / cnt
    out[cnt == 0] = np.nan
    return out


def label_causes(result, before_bands: dict[str, np.ndarray], after_bands: dict[str, np.ndarray]) -> dict[str, Any] | None:
    """Add ``likely_cause`` and its measurements to the vegetation-loss features of ``result`` (in place).

    Returns a summary dict (hectares per label, method and sources) or ``None`` when B12 is unavailable (then the features get no label)."""
    nb, na = result.indices_before.get("nbr"), result.indices_after.get("nbr")
    if nb is None or na is None:
        return None
    n = int(result.labels.max())
    if n == 0:
        return _summary({})
    sel = result.labels > 0
    lab = result.labels[sel]
    dnbr = _bincount_mean(lab, (nb - na)[sel], n)
    vis_a = _bincount_mean(lab, ((after_bands["blue"] + after_bands["green"] + after_bands["red"]) / 3.0)[sel], n)
    vis_b = _bincount_mean(lab, ((before_bands["blue"] + before_bands["green"] + before_bands["red"]) / 3.0)[sel], n)
    totals: dict[str, dict[str, float]] = {k: {"polygons": 0, "area_ha": 0.0} for k in LABELS}
    for f, lid in zip(result.features, result.label_ids):
        p = f["properties"]
        if p["class"] != "vegetation_loss":
            continue
        shape = result.shape_stats.get(lid, {})
        m = {
            "dnbr": float(dnbr[lid]) if np.isfinite(dnbr[lid]) else None,
            "vis_after": float(vis_a[lid]),
            "vis_delta": float(vis_a[lid] - vis_b[lid]),
            "rectangularity": float(shape.get("rectangularity", 0.0)),
            "area_ha": float(p["area_ha"]),
        }
        label, basis = classify_cause(m)
        p["likely_cause"] = label
        p["likely_cause_basis"] = "; ".join(basis)
        p["dnbr"] = None if m["dnbr"] is None else round(m["dnbr"], 3)
        p["vis_reflectance_after"] = round(m["vis_after"], 3)
        p["rectangularity"] = round(m["rectangularity"], 3)
        totals[label]["polygons"] += 1
        totals[label]["area_ha"] = round(totals[label]["area_ha"] + p["area_ha"], 2)
    return _summary(totals)


def _summary(totals: dict[str, dict[str, float]]) -> dict[str, Any]:
    full = {k: totals.get(k, {"polygons": 0, "area_ha": 0.0}) for k in LABELS}
    return {
        "applies_to": "vegetation_loss polygons",
        "totals": full,
        "note": "A hint from dNBR, brightness and shape, not a verdict on the cause. 'fire-like' does not prove a fire; 'clearing-like' covers felling, harvest, ploughing and construction.",
        "thresholds": {
            "dnbr_unburned_below": DNBR_LOW, "dnbr_moderate_low": DNBR_MODERATE_LOW, "dnbr_moderate_high": DNBR_MODERATE_HIGH,
            "dark_visible_reflectance_after_at_most": DARK_VIS_AFTER, "bright_visible_reflectance_after_at_least": BRIGHT_VIS_AFTER,
            "straight_edged_rectangularity_at_least": RECT_MIN, "irregular_rectangularity_at_most": IRREGULAR_MAX,
        },
        "sources": SOURCES,
        "own_thresholds": "brightness and rectangularity thresholds are this project's heuristics, checked on only two public cases",
    }
