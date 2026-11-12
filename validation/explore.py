"""Developer helper: build the two composites once, cache them as .npz, print index statistics.
usage: python validation/explore.py W,S,E,N BEFORE AFTER RES NAME"""
import sys, numpy as np
from pathlib import Path
from earthdiff.aoi import parse_bbox, make_grid, aoi_mask
from earthdiff.periods import parse_period
from earthdiff.stac import search_scenes
from earthdiff.composite import build_composite, BANDS
bbox, b, a, res, name = sys.argv[1:6]
geom = parse_bbox(bbox); grid = make_grid(geom, float(res)); mask = aoi_mask(geom, grid)
out = {}
for tag, p in (("before", b), ("after", a)):
    sc = search_scenes("earth-search", geom, parse_period(p))
    c = build_composite(sc, grid, mask, progress=print, label=tag)
    for k in BANDS: out[f"{tag}_{k}"] = c.bands[k]
    out[f"{tag}_count"] = c.count
np.savez_compressed(Path("validation/cache")/f"{name}.npz", **out)
print("saved")
