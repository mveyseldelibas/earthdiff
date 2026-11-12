"""Cloud masking (SCL) and per-period median composites."""
from __future__ import annotations

import gc
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
from scipy.ndimage import binary_dilation

from .aoi import Grid
from .reader import cog_reader
from .stac import Scene

BANDS = ("blue", "green", "red", "nir", "swir16")
BANDS_WITH_B12 = BANDS + ("swir22",)  # swir22 (B12) feeds NBR / dNBR; it is optional: an unreadable B12 never blocks a run
OPTIONAL_BANDS = ("swir22",)

# Sentinel-2 L2A Scene Classification Layer values
SCL_NODATA, SCL_SATURATED, SCL_DARK, SCL_CLOUD_SHADOW = 0, 1, 2, 3
SCL_CLOUD_MEDIUM, SCL_CLOUD_HIGH, SCL_CIRRUS, SCL_SNOW = 8, 9, 10, 11
# Rejected by default: no data, saturated/defective, cloud shadow, clouds (all), cirrus, snow/ice.
# Kept: dark area pixels (2), vegetation (4), not-vegetated (5), water (6), unclassified (7).
DEFAULT_INVALID_SCL = (0, 1, 3, 8, 9, 10, 11)
_CLOUDY = (3, 8, 9, 10)

Reader = Callable[[Scene, str, Grid], np.ndarray]
Progress = Callable[[str], None]


def valid_from_scl(
    scl: np.ndarray, res: float = 20.0, invalid: tuple[int, ...] = DEFAULT_INVALID_SCL, buffer_m: float = 30.0
) -> np.ndarray:
    """True where the pixel is usable. Cloud/shadow pixels are dilated by ``buffer_m`` metres."""
    bad = np.isin(scl, invalid)
    iters = int(round(buffer_m / res))
    if iters > 0:
        cloudy = np.isin(scl, _CLOUDY)
        if cloudy.any():
            bad |= binary_dilation(cloudy, iterations=iters)
    return ~bad


def to_reflectance(dn: np.ndarray, scale: float, offset: float) -> np.ndarray:
    refl = dn.astype(np.float32) * np.float32(scale) + np.float32(offset)
    np.clip(refl, 0.0, 2.0, out=refl)
    return refl


@dataclass
class Composite:
    bands: dict[str, np.ndarray]  # float32 reflectance; NaN where no valid observation
    count: np.ndarray  # uint8, number of valid observations per pixel
    scenes: list[Scene]
    candidates: int
    scene_valid_fraction: dict[str, float] = field(default_factory=dict)
    # per-scene true-colour bands, subsampled by ``frame_step`` (float16, NaN = cloud / no data), for the report timeline
    frames: dict[str, np.ndarray] = field(default_factory=dict)
    frame_step: int = 1


def strip_median(stack: np.ndarray, max_elems: int = 12_000_000) -> np.ndarray:
    """nanmedian over axis 0, in row strips to bound temporary memory."""
    n, h, w = stack.shape
    out = np.empty((h, w), dtype=np.float32)
    rows = max(1, max_elems // max(1, n * w))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for r0 in range(0, h, rows):
            out[r0 : r0 + rows] = np.nanmedian(stack[:, r0 : r0 + rows], axis=0)
    return out


def build_composite(
    candidates: list[Scene],
    grid: Grid,
    mask: np.ndarray,
    max_scenes: int = 8,
    min_scene_fraction: float = 0.02,
    reader: Reader = cog_reader,
    workers: int = 6,
    progress: Progress | None = None,
    label: str = "",
    bands: tuple[str, ...] = BANDS,
    frame_max_dim: int = 0,
) -> Composite:
    """Select the scenes that really cover the AOI cloud-free, then median-composite them."""
    say = progress or (lambda m: None)
    h, w = grid.shape
    n_aoi = max(1, int(mask.sum()))

    # 1) read SCL of every candidate (cheap, 20 m) and rank by cloud-free AOI coverage
    say(f"{label}: checking cloud cover of {len(candidates)} candidate scenes")
    def read_scl(s: Scene):
        try:
            return reader(s, "scl", grid)
        except Exception as exc:  # unreadable scene: skip it instead of failing the whole run
            say(f"{label}: skipping {s.id} ({str(exc)[:80]})")
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        scls = list(ex.map(read_scl, candidates))
    ranked = []
    for s, scl in zip(candidates, scls):
        if scl is None:
            continue
        valid = valid_from_scl(scl, grid.res) & mask
        ranked.append((float(valid.sum()) / n_aoi, s, valid))
    del scls
    ranked.sort(key=lambda t: (-t[0], t[1].date))
    chosen = [t for t in ranked if t[0] >= min_scene_fraction][:max_scenes]
    chosen.sort(key=lambda t: t[1].date)
    fractions = {t[1].id: round(t[0], 4) for t in ranked}
    if not chosen:
        empty = {b: np.full((h, w), np.nan, dtype=np.float32) for b in bands}
        return Composite(empty, np.zeros((h, w), dtype=np.uint8), [], len(candidates), fractions)

    scenes = [t[1] for t in chosen]
    valid_stack = np.stack([t[2] for t in chosen])  # (n,h,w) bool
    del ranked, chosen
    gc.collect()

    # 2) one band at a time: read -> reflectance -> mask -> strip-wise median
    out: dict[str, np.ndarray] = {}
    # small copies of each scene's red/green/blue (a few MB) let the report play the scenes one by one
    step = max(1, -(-max(h, w) // frame_max_dim)) if frame_max_dim else 0
    frames: dict[str, np.ndarray] = {}
    for band in bands:
        say(f"{label}: downloading + compositing band '{band}' from {len(scenes)} scenes")
        stack = np.full((len(scenes), h, w), np.nan, dtype=np.float32)

        def work(i: int, band=band, stack=stack):
            s = scenes[i]
            try:
                dn = reader(s, band, grid)
            except Exception:
                if band in OPTIONAL_BANDS:  # leave this scene out of the optional band, keep the run going
                    return
                raise
            scale, offset = s.radiometry.get(band, (1e-4, 0.0))
            refl = to_reflectance(dn, scale, offset)
            refl[(dn == 0) | ~valid_stack[i]] = np.nan
            stack[i] = refl

        with ThreadPoolExecutor(max_workers=min(workers, len(scenes))) as ex:
            list(ex.map(work, range(len(scenes))))
        if step and band in ("red", "green", "blue"):
            frames[band] = stack[:, ::step, ::step].astype(np.float16)
        out[band] = strip_median(stack)
        del stack
        gc.collect()

    count = valid_stack.sum(axis=0).astype(np.uint8)
    del valid_stack
    # a pixel is only usable if all bands have a value
    for b in [b for b in bands if b in OPTIONAL_BANDS]:
        if not np.isfinite(out[b]).any():  # B12 unreadable everywhere: drop the band instead of the whole composite
            del out[b]
    required = [b for b in bands if b not in OPTIONAL_BANDS]
    ok = np.ones((h, w), dtype=bool)
    for b in required:
        ok &= np.isfinite(out[b])
    for b in out:
        if b in required:
            out[b][~ok] = np.nan
    count[~ok] = 0
    return Composite(out, count, scenes, len(candidates), fractions, frames, step or 1)
