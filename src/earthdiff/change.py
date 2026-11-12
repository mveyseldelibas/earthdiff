"""Rule-based change classification, minimum mapping unit, polygonization, confidence."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
from pyproj import Transformer
from rasterio import features
from scipy.ndimage import binary_opening, label
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform

from .aoi import WGS84, Grid
from .composite import Composite
from .indices import compute_indices, ndvi, normalized_difference


@dataclass(frozen=True)
class ClassInfo:
    id: int
    key: str
    label: str
    color: str  # colour-blind friendly (Okabe-Ito)
    rule: str


CLASSES: dict[int, ClassInfo] = {
    1: ClassInfo(1, "vegetation_loss", "Vegetation loss", "#D55E00",
                 "NDVI before >= veg_min_before AND dNDVI <= -veg_drop AND not water after"),
    2: ClassInfo(2, "vegetation_gain", "Vegetation gain", "#009E73",
                 "NDVI after >= veg_min_after AND dNDVI >= +veg_rise AND not water before"),
    3: ClassInfo(3, "new_water", "New water", "#0072B2",
                 "water after (MNDWI > water_mndwi, NDVI < 0.30) AND MNDWI before < land_mndwi AND dMNDWI >= water_delta"),
    4: ClassInfo(4, "water_loss", "Water loss", "#F0E442",
                 "water before AND MNDWI after < land_mndwi AND dMNDWI <= -water_delta"),
    5: ClassInfo(5, "new_bare_or_built", "New bare soil / built-up", "#CC79A7",
                 "dBSI >= bare_rise AND NDVI after < bare_ndvi_max AND no water before/after"),
}
KEY_TO_ID = {c.key: c.id for c in CLASSES.values()}


@dataclass(frozen=True)
class Thresholds:
    veg_min_before: float = 0.40
    veg_drop: float = 0.25
    veg_min_after: float = 0.40
    veg_rise: float = 0.25
    water_mndwi: float = 0.10
    land_mndwi: float = -0.05
    water_ndvi_max: float = 0.30
    water_delta: float = 0.25
    bare_rise: float = 0.15
    bare_ndvi_max: float = 0.25

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


SENSITIVITY = {
    "strict": dict(veg_drop=0.35, veg_rise=0.35, water_delta=0.35, bare_rise=0.22),
    "default": {},
    "sensitive": dict(veg_drop=0.18, veg_rise=0.18, water_delta=0.18, bare_rise=0.10),
}


def thresholds_for(sensitivity: str = "default", **overrides: float) -> Thresholds:
    if sensitivity not in SENSITIVITY:
        raise ValueError(f"sensitivity must be one of {', '.join(SENSITIVITY)}")
    params = {**SENSITIVITY[sensitivity], **overrides}
    return Thresholds(**params)


def classify(before: dict[str, np.ndarray], after: dict[str, np.ndarray], thr: Thresholds) -> np.ndarray:
    """Per-pixel class id (uint8; 0 = no change). ``before``/``after`` hold ndvi, mndwi, bsi."""
    nb, na = before["ndvi"], after["ndvi"]
    mb, ma = before["mndwi"], after["mndwi"]
    bb, ba = before["bsi"], after["bsi"]
    valid = np.isfinite(nb) & np.isfinite(na) & np.isfinite(mb) & np.isfinite(ma) & np.isfinite(bb) & np.isfinite(ba)

    with np.errstate(invalid="ignore"):
        dn, dm, db = na - nb, ma - mb, ba - bb
        water_a = (ma > thr.water_mndwi) & (na < thr.water_ndvi_max)
        water_b = (mb > thr.water_mndwi) & (nb < thr.water_ndvi_max)

        new_water = water_a & (mb < thr.land_mndwi) & (dm >= thr.water_delta)
        water_loss = water_b & (ma < thr.land_mndwi) & (dm <= -thr.water_delta)
        veg_loss = (nb >= thr.veg_min_before) & (dn <= -thr.veg_drop) & ~water_a
        veg_gain = (na >= thr.veg_min_after) & (dn >= thr.veg_rise) & ~water_b
        bare = (db >= thr.bare_rise) & (na < thr.bare_ndvi_max) & ~water_a & ~water_b

    out = np.zeros(nb.shape, dtype=np.uint8)
    # lowest priority first; later assignments overwrite earlier ones
    out[bare & valid] = 5
    out[veg_gain & valid] = 2
    out[veg_loss & valid] = 1
    out[water_loss & valid] = 4
    out[new_water & valid] = 3
    return out


def label_components(
    classmap: np.ndarray, res: float, mmu_ha: float, open_noise: bool = True
) -> tuple[np.ndarray, np.ndarray]:
    """Clean each class (opening for <=20 m pixels), drop blobs below the MMU, label the rest.

    Returns ``(labels, label_class)`` where ``label_class[label] -> class id`` (index 0 unused).
    """
    labels = np.zeros(classmap.shape, dtype=np.int32)
    label_class = [0]
    min_pix = max(1, int(math.ceil(mmu_ha * 10_000.0 / (res * res))))
    struct8 = np.ones((3, 3), dtype=bool)
    square3 = np.ones((3, 3), dtype=bool)
    next_id = 1
    for cid in CLASSES:
        m = classmap == cid
        if not m.any():
            continue
        if open_noise and res <= 20.0:
            m = binary_opening(m, structure=square3)
            if not m.any():
                continue
        lab, n = label(m, structure=struct8)
        sizes = np.bincount(lab.ravel(), minlength=n + 1)
        keep = sizes >= min_pix
        keep[0] = False
        kept = np.flatnonzero(keep)
        if kept.size == 0:
            continue
        remap = np.zeros(n + 1, dtype=np.int32)
        remap[kept] = np.arange(next_id, next_id + kept.size, dtype=np.int32)
        sel = keep[lab]
        labels[sel] = remap[lab[sel]]
        label_class.extend([cid] * int(kept.size))
        next_id += int(kept.size)
    return labels, np.array(label_class, dtype=np.uint8)


def _primary(cid: int, mean_b: dict[str, np.ndarray], mean_a: dict[str, np.ndarray], thr: Thresholds):
    """(delta, threshold) that defines each class; used for the magnitude part of the confidence."""
    dn = mean_a["ndvi"] - mean_b["ndvi"]
    dm = mean_a["mndwi"] - mean_b["mndwi"]
    db = mean_a["bsi"] - mean_b["bsi"]
    return {
        1: (-dn, thr.veg_drop),
        2: (dn, thr.veg_rise),
        3: (dm, thr.water_delta),
        4: (-dm, thr.water_delta),
        5: (db, thr.bare_rise),
    }[cid]


def confidence_score(magnitude_ratio: float, obs: float, area_ha: float, mmu_ha: float) -> float:
    """Heuristic 0..1 (NOT a statistical probability).

    0.5 * magnitude + 0.3 * valid observations + 0.2 * size, where
      magnitude = clip((delta - threshold) / threshold, 0, 1)   (delta of 2x threshold -> 1)
      obs       = clip(min(valid obs before, after) / 3, 0, 1)
      size      = clip(log10(area / MMU), 0, 1)                 (10x the MMU -> 1)
    """
    mag = min(max(magnitude_ratio, 0.0), 1.0)
    o = min(max(obs / 3.0, 0.0), 1.0)
    size = min(max(math.log10(max(area_ha, 1e-9) / mmu_ha), 0.0), 1.0)
    return 0.5 * mag + 0.3 * o + 0.2 * size


def confidence_level(score: float) -> str:
    return "high" if score >= 0.70 else "medium" if score >= 0.45 else "low"


@dataclass
class ChangeResult:
    classmap: np.ndarray  # cleaned, uint8
    labels: np.ndarray
    features: list[dict[str, Any]]
    indices_before: dict[str, np.ndarray]
    indices_after: dict[str, np.ndarray]
    valid: np.ndarray  # bool, comparable pixels inside the AOI
    totals: dict[str, dict[str, float]] = field(default_factory=dict)
    label_ids: list[int] = field(default_factory=list)  # raster label of each feature (same order as ``features``)
    shape_stats: dict[int, dict[str, float]] = field(default_factory=dict)
    bright_fraction: float = 0.0  # share of comparable pixels that are very bright in the before OR after composite
    bright_both_fraction: float = 0.0  # ... in BOTH composites
    bright_ndwi: dict[str, float] | None = None  # NDWI > 0 share of the bright pixels before / after (exploratory wet/dry signal), None if no bright pixels


def bright_mask(bands: dict[str, np.ndarray], vis_min: float = 0.25, ndvi_max: float = 0.15) -> np.ndarray:
    """Very bright, non-vegetated surface (salt pan, sand, snow, bare limestone, concrete): mean visible reflectance >= ``vis_min``
    and NDVI < ``ndvi_max``. Such surfaces saturate true-colour images and the water/vegetation rules are unreliable on them."""
    vis = (bands["blue"] + bands["green"] + bands["red"]) / 3.0
    nd = ndvi(bands["nir"], bands["red"])
    with np.errstate(invalid="ignore"):
        return np.isfinite(vis) & (vis >= vis_min) & (nd < ndvi_max)


def polygonize(*args, **kwargs) -> list[dict[str, Any]]:
    """Polygons as GeoJSON features (see :func:`polygonize_detailed` for the label ids and shape statistics)."""
    return polygonize_detailed(*args, **kwargs)[0]


def polygonize_detailed(
    labels: np.ndarray,
    label_class: np.ndarray,
    grid: Grid,
    ib: dict[str, np.ndarray],
    ia: dict[str, np.ndarray],
    obs_b: np.ndarray,
    obs_a: np.ndarray,
    thr: Thresholds,
    mmu_ha: float,
    bright_b: np.ndarray | None = None,
    bright_a: np.ndarray | None = None,
) -> tuple[list[dict[str, Any]], list[int], dict[int, dict[str, float]]]:
    """Like :func:`polygonize` but also returns ``label_ids`` (the raster label of each feature, same order) and per-label
    shape statistics (``rectangularity`` = area / minimum rotated rectangle area, ``solidity`` = area / convex hull area)."""
    n = int(labels.max())
    if n == 0:
        return [], [], {}
    sel = labels > 0
    lab = labels[sel]
    npix = np.bincount(lab, minlength=n + 1).astype(np.float64)
    denom = np.maximum(npix, 1.0)

    def mean_of(arr: np.ndarray) -> np.ndarray:
        return np.bincount(lab, weights=np.nan_to_num(arr[sel].astype(np.float64)), minlength=n + 1) / denom

    mb = {k: mean_of(ib[k]) for k in ("ndvi", "mndwi", "bsi")}
    ma = {k: mean_of(ia[k]) for k in ("ndvi", "mndwi", "bsi")}
    ob, oa = mean_of(obs_b), mean_of(obs_a)

    to_wgs = Transformer.from_crs(grid.crs, WGS84, always_xy=True).transform
    shapes_iter = features.shapes(labels, mask=sel, transform=grid.transform, connectivity=8)
    parts: dict[int, list[dict]] = {}
    for geom, value in shapes_iter:
        parts.setdefault(int(value), []).append(geom)

    bsh_b = mean_of(bright_b.astype(np.float32)) if bright_b is not None else None
    bsh_a = mean_of(bright_a.astype(np.float32)) if bright_a is not None else None
    out: list[tuple[dict[str, Any], int]] = []
    shapes: dict[int, dict[str, float]] = {}
    for lid in sorted(parts):
        cid = int(label_class[lid])
        info = CLASSES[cid]
        area_ha = float(npix[lid] * grid.pixel_area_ha)
        delta_arr, t = _primary(cid, {k: v[lid] for k, v in mb.items()}, {k: v[lid] for k, v in ma.items()}, thr)
        mag_ratio = (float(delta_arr) - t) / t
        obs = float(min(ob[lid], oa[lid]))
        score = confidence_score(mag_ratio, obs, area_ha, mmu_ha)
        bright_share = 0.0
        if bsh_b is not None and bsh_a is not None:
            bright_share = float(max(bsh_b[lid], bsh_a[lid]))
            score *= 1.0 - 0.5 * bright_share  # confidence is lowered where the surface is very bright (salt, sand, snow)
        utm_geom = shape(parts[lid][0])
        mrr, hull = utm_geom.minimum_rotated_rectangle.area, utm_geom.convex_hull.area
        shapes[lid] = {
            "rectangularity": round(float(utm_geom.area / mrr), 3) if mrr > 0 else 0.0,
            "solidity": round(float(utm_geom.area / hull), 3) if hull > 0 else 0.0,
        }
        geom = shp_transform(to_wgs, utm_geom)
        cen = geom.representative_point()
        out.append(
            ({
                "type": "Feature",
                "geometry": mapping(_round_geom(geom)),
                "properties": {
                    "id": len(out) + 1,
                    "class": info.key,
                    "class_label": info.label,
                    "area_ha": round(area_ha, 2),
                    "confidence": round(score, 2),
                    "confidence_level": confidence_level(score),
                    "ndvi_before": round(float(mb["ndvi"][lid]), 3),
                    "ndvi_after": round(float(ma["ndvi"][lid]), 3),
                    "mndwi_before": round(float(mb["mndwi"][lid]), 3),
                    "mndwi_after": round(float(ma["mndwi"][lid]), 3),
                    "bsi_before": round(float(mb["bsi"][lid]), 3),
                    "bsi_after": round(float(ma["bsi"][lid]), 3),
                    "valid_obs_before": round(float(ob[lid]), 1),
                    "valid_obs_after": round(float(oa[lid]), 1),
                    "centroid_lon": round(cen.x, 6),
                    "centroid_lat": round(cen.y, 6),
                    "bright_share": round(bright_share, 3),
                },
            }, lid)
        )
    out.sort(key=lambda t: -t[0]["properties"]["area_ha"])
    for i, (f, _) in enumerate(out, 1):
        f["properties"]["id"] = i
    return [f for f, _ in out], [lid for _, lid in out], shapes


def _round_geom(geom, nd: int = 6):
    from shapely import set_precision

    return set_precision(geom, 10 ** -nd)


def detect_changes(
    before: Composite,
    after: Composite,
    grid: Grid,
    mask: np.ndarray,
    thr: Thresholds,
    mmu_ha: float = 0.5,
) -> ChangeResult:
    ib = compute_indices(before.bands)
    ia = compute_indices(after.bands)
    valid = mask & (before.count > 0) & (after.count > 0)
    for d in (ib, ia):
        for arr in d.values():
            arr[~valid] = np.nan
    raw = classify(ib, ia, thr)
    labels, label_class = label_components(raw, grid.res, mmu_ha)
    del raw
    bright_b, bright_a = bright_mask(before.bands) & valid, bright_mask(after.bands) & valid
    n_valid = max(1, int(valid.sum()))
    bright_fraction = float((bright_b | bright_a).sum()) / n_valid
    bright_both = float((bright_b & bright_a).sum()) / n_valid
    bright_ndwi = None
    ref = bright_b | bright_a
    if int(ref.sum()) >= 100:
        bright_ndwi = {"pixels": int(ref.sum())}
        for tag, comp in (("before", before), ("after", after)):
            nd = normalized_difference(comp.bands["green"], comp.bands["nir"])
            bright_ndwi[tag] = round(float(((nd > 0) & ref).sum()) / int(ref.sum()), 3)
    feats, label_ids, shapes = polygonize_detailed(labels, label_class, grid, ib, ia, before.count, after.count, thr, mmu_ha, bright_b, bright_a)
    classmap = label_class[labels] if labels.size else np.zeros_like(labels, dtype=np.uint8)
    totals: dict[str, dict[str, float]] = {}
    for info in CLASSES.values():
        subset = [f for f in feats if f["properties"]["class"] == info.key]
        totals[info.key] = {
            "label": info.label,
            "polygons": len(subset),
            "area_ha": round(sum(f["properties"]["area_ha"] for f in subset), 2),
        }
    return ChangeResult(classmap.astype(np.uint8), labels, feats, ib, ia, valid, totals, label_ids, shapes, bright_fraction, bright_both, bright_ndwi)
