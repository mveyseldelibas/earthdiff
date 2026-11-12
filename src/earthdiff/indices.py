"""Spectral indices (all inputs are surface reflectance, 0..1)."""
from __future__ import annotations

import numpy as np


def normalized_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """(a - b) / (a + b), NaN where the sum is not positive or an input is NaN."""
    a = a.astype(np.float32, copy=False)
    b = b.astype(np.float32, copy=False)
    s = a + b
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (a - b) / s
    out[~(s > 1e-6)] = np.nan
    return out.astype(np.float32, copy=False)


def ndvi(nir, red):
    """Normalized Difference Vegetation Index: (NIR - Red) / (NIR + Red)."""
    return normalized_difference(nir, red)


def mndwi(green, swir1):
    """Modified NDWI (Xu 2006): (Green - SWIR1) / (Green + SWIR1). > 0 mostly open water."""
    return normalized_difference(green, swir1)


def ndbi(swir1, nir):
    """Normalized Difference Built-up Index: (SWIR1 - NIR) / (SWIR1 + NIR)."""
    return normalized_difference(swir1, nir)


def bsi(blue, red, nir, swir1):
    """Bare Soil Index: ((SWIR1 + Red) - (NIR + Blue)) / ((SWIR1 + Red) + (NIR + Blue))."""
    return normalized_difference(swir1 + red, nir + blue)


def nbr(nir, swir2):
    """Normalized Burn Ratio (Key & Benson 2006): (NIR - SWIR2) / (NIR + SWIR2), Sentinel-2 B08 and B12."""
    return normalized_difference(nir, swir2)


def compute_indices(bands: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    out = {
        "ndvi": ndvi(bands["nir"], bands["red"]),
        "mndwi": mndwi(bands["green"], bands["swir16"]),
        "bsi": bsi(bands["blue"], bands["red"], bands["nir"], bands["swir16"]),
        "ndbi": ndbi(bands["swir16"], bands["nir"]),
    }
    if "swir22" in bands:  # optional band: only present when B12 could be read
        out["nbr"] = nbr(bands["nir"], bands["swir22"])
    return out
