"""Lake Tuz (Salt Lake, Turkey): can a spring baseline or another index show the drying that the default rules miss?

Builds median composites (the same code as the analysis) of the same box for three windows and compares, on the pixels that are very bright
in late summer 2021 (the dry salt crust), the share that looks like water under several indices.

    python validation/tuz_experiment.py          # writes validation/mcp/tuz_golu/spring_experiment.json

Needs internet. About 3 minutes at 30 m, a few hundred MB of RAM.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from earthdiff.aoi import aoi_mask, make_grid, bbox_geometry
from earthdiff.change import bright_mask
from earthdiff.composite import BANDS_WITH_B12, build_composite
from earthdiff.indices import mndwi, ndvi, normalized_difference
from earthdiff.periods import parse_period
from earthdiff.stac import search_scenes

BBOX = (33.36096, 38.71086, 33.54119, 38.91818)  # the centred box find_place offered for "Tuz Gölü"
WINDOWS = {"spring_2021": "2021-03/2021-04", "late_summer_2021": "2021-08/2021-09", "late_summer_2019": "2019-08/2019-09"}
OUT = Path(__file__).resolve().parent / "mcp" / "tuz_golu" / "spring_experiment.json"


def main() -> None:
    geom = bbox_geometry(*BBOX)
    grid = make_grid(geom, 30)
    mask = aoi_mask(geom, grid)
    comps = {}
    for name, win in WINDOWS.items():
        cands = search_scenes("earth-search", geom, parse_period(win), 80.0)
        comps[name] = build_composite(cands, grid, mask, 8, progress=print, label=name, bands=BANDS_WITH_B12)
        print(name, "scenes", len(comps[name].scenes))
    ref = bright_mask(comps["late_summer_2021"].bands) & mask & (comps["late_summer_2021"].count > 0)
    out: dict = {"box": BBOX, "grid_m": 30, "reference_pixels": int(ref.sum()), "reference": "pixels very bright in late summer 2021 (dry salt crust)", "windows": {}}
    for name, c in comps.items():
        b = c.bands
        valid = ref & (c.count > 0) & np.isfinite(b["red"])
        vis = (b["blue"] + b["green"] + b["red"]) / 3
        nd = ndvi(b["nir"], b["red"])
        m = mndwi(b["green"], b["swir16"])
        ndwi = normalized_difference(b["green"], b["nir"])
        n = max(1, int(valid.sum()))
        out["windows"][name] = {
            "scenes": len(c.scenes),
            "valid_share_of_reference": round(float(valid.sum()) / max(1, int(ref.sum())), 3),
            "median_visible_reflectance": round(float(np.nanmedian(vis[valid])), 3) if valid.any() else None,
            "median_swir1_reflectance": round(float(np.nanmedian(b["swir16"][valid])), 3) if valid.any() else None,
            "share_mndwi_water (MNDWI>0.10 and NDVI<0.30, the default rule)": round(float(((m > 0.10) & (nd < 0.30) & valid).sum()) / n, 3),
            "share_ndwi_positive ((green-nir)/(green+nir)>0)": round(float(((ndwi > 0) & valid).sum()) / n, 3),
            "share_dark_wet (mean visible < 0.15)": round(float(((vis < 0.15) & valid).sum()) / n, 3),
            "share_still_very_bright": round(float((bright_mask(b) & valid).sum()) / n, 3),
        }
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
