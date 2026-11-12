from datetime import date, datetime, timezone

import numpy as np
import pytest
from affine import Affine
from shapely.geometry import box

from earthdiff import aoi as aoimod
from earthdiff.change import (
    CLASSES, Thresholds, classify, confidence_level, confidence_score, detect_changes,
    label_components, thresholds_for,
)
from earthdiff.composite import BANDS, Composite, build_composite, strip_median, to_reflectance, valid_from_scl
from earthdiff.indices import bsi, compute_indices, mndwi, ndbi, ndvi
from earthdiff.periods import parse_period, previous_month, season_gap_days
from earthdiff.stac import Scene


# ---------------------------------------------------------------- indices
def test_ndvi_values():
    nir, red = np.array([0.5, 0.3], "f4"), np.array([0.1, 0.3], "f4")
    out = ndvi(nir, red)
    assert out[0] == pytest.approx(0.4 / 0.6, rel=1e-5)
    assert out[1] == pytest.approx(0.0)


def test_indices_nan_when_denominator_zero():
    z = np.zeros(2, "f4")
    assert np.isnan(ndvi(z, z)).all()
    assert np.isnan(mndwi(np.array([np.nan, 0.1], "f4"), np.array([0.1, 0.1], "f4"))[0])


def test_mndwi_ndbi_bsi_formulas():
    g, s, n, r, b = (np.array([v], "f4") for v in (0.10, 0.05, 0.30, 0.08, 0.04))
    assert mndwi(g, s)[0] == pytest.approx((0.10 - 0.05) / 0.15, rel=1e-5)
    assert ndbi(s, n)[0] == pytest.approx((0.05 - 0.30) / 0.35, rel=1e-5)
    assert bsi(b, r, n, s)[0] == pytest.approx(((0.05 + 0.08) - (0.30 + 0.04)) / ((0.05 + 0.08) + (0.30 + 0.04)), rel=1e-5)


def test_compute_indices_keys():
    bands = {k: np.full((2, 2), 0.2, "f4") for k in BANDS}
    assert set(compute_indices(bands)) == {"ndvi", "mndwi", "bsi", "ndbi"}


# ---------------------------------------------------------------- periods
def test_parse_period_month_and_range():
    p = parse_period("2024-02")
    assert (p.start, p.end) == (date(2024, 2, 1), date(2024, 2, 29))
    p = parse_period("2023-06/2023-07")
    assert (p.start, p.end) == (date(2023, 6, 1), date(2023, 7, 31))
    p = parse_period("2023-06-10..2023-06-20")
    assert p.days() == 11
    assert parse_period("2023-06").label() == "2023-06"
    with pytest.raises(ValueError):
        parse_period("2023-13")
    with pytest.raises(ValueError):
        parse_period("2023-07/2023-06")


def test_period_shift_and_season_gap():
    p = parse_period("2024-06")
    assert p.shift_years(-1).start == date(2023, 6, 1)
    assert season_gap_days(p, p.shift_years(-1)) <= 2
    assert season_gap_days(parse_period("2024-01"), parse_period("2024-07")) > 150
    assert previous_month(date(2025, 1, 15)).start == date(2024, 12, 1)


# ---------------------------------------------------------------- masking
def test_scl_mask_and_dilation():
    scl = np.full((9, 9), 4, "u1")
    scl[4, 4] = 9  # cloud
    scl[0, 0] = 0  # nodata
    scl[8, 8] = 11  # snow
    scl[0, 8] = 2  # dark area: kept
    v = valid_from_scl(scl, res=30.0, buffer_m=30.0)  # 1 px dilation
    assert not v[4, 4] and not v[3, 4] and not v[4, 5] and v[3, 3]
    assert not v[0, 0] and not v[8, 8]
    assert v[0, 8] and v[7, 1]
    v0 = valid_from_scl(scl, res=30.0, buffer_m=0.0)
    assert v0[3, 4]


def test_reflectance_offset_and_clip():
    dn = np.array([500, 1000, 3000, 0], "u2")
    out = to_reflectance(dn, 1e-4, -0.1)
    assert out[0] == 0.0  # clipped (below offset)
    assert out[2] == pytest.approx(0.2)
    assert to_reflectance(np.array([2000], "u2"), 1e-4, 0.0)[0] == pytest.approx(0.2)


# ---------------------------------------------------------------- composite
def test_strip_median_ignores_nan():
    stack = np.array([[[1.0, np.nan]], [[3.0, np.nan]], [[100.0, 5.0]]], "f4")
    out = strip_median(stack, max_elems=1)
    assert out[0, 0] == pytest.approx(3.0)
    assert out[0, 1] == pytest.approx(5.0)


def _grid(h=20, w=20, res=20.0):
    return aoimod.Grid(32636, Affine(res, 0, 500000, 0, -res, 4100000), w, h, res)


def _scene(i, cloud=10.0):
    return Scene(
        id=f"S2X_36SUF_2024060{i}_0_L2A", datetime=datetime(2024, 6, i, tzinfo=timezone.utc), cloud_cover=cloud,
        tile="MGRS-36SUF", hrefs={}, radiometry={b: (1e-4, 0.0) for b in BANDS},
    )


def test_build_composite_median_and_cloud_removal():
    grid = _grid()
    mask = np.ones(grid.shape, bool)
    scenes = [_scene(1), _scene(2), _scene(3), _scene(4)]
    values = {1: 1000, 2: 2000, 3: 9000, 4: 2000}  # scene 3 has a bright "cloud" in the whole top half

    def reader(scene, band, g):
        i = int(scene.id.split("_")[2][-1])
        if band == "scl":
            scl = np.full(g.shape, 4, "u1")
            if i == 3:
                scl[:10] = 9
            if i == 4:
                scl[:] = 9  # fully clouded scene -> must be rejected
            return scl
        return np.full(g.shape, values[i], "u2")

    comp = build_composite(scenes, grid, mask, max_scenes=8, reader=reader, workers=2)
    assert len(comp.scenes) == 3  # fully clouded scene dropped
    assert comp.bands["red"][0, 0] == pytest.approx(0.15)  # median(0.1,0.2), cloud 0.9 excluded
    assert comp.bands["red"][19, 0] == pytest.approx(0.2)  # median(0.1,0.2,0.9)
    assert comp.count[0, 0] == 2 and comp.count[19, 0] == 3


def test_build_composite_no_valid_scene():
    grid = _grid(4, 4)
    reader = lambda s, b, g: np.full(g.shape, 9 if b == "scl" else 1000, "u1" if b == "scl" else "u2")
    comp = build_composite([_scene(1)], grid, np.ones(grid.shape, bool), reader=reader, workers=1)
    assert comp.scenes == [] and comp.count.sum() == 0 and np.isnan(comp.bands["red"]).all()


# ---------------------------------------------------------------- classification
def _idx(ndvi_v, mndwi_v, bsi_v, shape=(4, 4)):
    f = lambda v: np.full(shape, v, "f4")
    return {"ndvi": f(ndvi_v), "mndwi": f(mndwi_v), "bsi": f(bsi_v)}


T = Thresholds()


@pytest.mark.parametrize(
    "before,after,expected",
    [
        ((0.7, -0.4, -0.1), (0.2, -0.3, 0.1), 1),   # forest -> cleared
        ((0.2, -0.3, 0.0), (0.7, -0.4, -0.1), 2),   # bare -> vegetation
        ((0.1, -0.4, 0.0), (-0.1, 0.5, -0.3), 3),   # land -> water
        ((-0.1, 0.5, -0.3), (0.1, -0.4, 0.0), 4),   # water -> land
        ((0.15, -0.3, -0.05), (0.05, -0.2, 0.2), 5),  # sparse -> bare/built
        ((0.6, -0.4, -0.1), (0.62, -0.4, -0.1), 0),  # stable
        ((0.6, -0.4, -0.1), (0.5, -0.4, -0.05), 0),  # small noise
    ],
)
def test_classify_rules(before, after, expected):
    out = classify(_idx(*before), _idx(*after), T)
    assert (out == expected).all()


def test_classify_nan_is_no_change():
    b, a = _idx(0.7, -0.4, -0.1), _idx(0.1, -0.3, 0.2)
    a["ndvi"][0, 0] = np.nan
    out = classify(b, a, T)
    assert out[0, 0] == 0 and out[1, 1] == 1


def test_sensitivity_presets():
    assert thresholds_for("strict").veg_drop > thresholds_for("default").veg_drop > thresholds_for("sensitive").veg_drop
    assert thresholds_for("default", veg_drop=0.3).veg_drop == 0.3
    with pytest.raises(ValueError):
        thresholds_for("nope")


# ---------------------------------------------------------------- MMU / polygons / area
def test_mmu_removes_small_blobs_and_noise():
    res = 20.0  # 0.04 ha per pixel; MMU 0.5 ha -> 13 px
    cm = np.zeros((40, 40), "u1")
    cm[2:6, 2:6] = 1       # 16 px -> kept
    cm[20:23, 20:23] = 1   # 9 px -> dropped
    cm[30, 30] = 1         # noise
    cm[10:30, 35] = 2      # 1-px wide line, 20 px -> removed by opening
    labels, lc = label_components(cm, res, 0.5)
    assert labels.max() == 1 and lc[1] == 1
    assert (labels > 0).sum() == 16


def test_eight_connectivity_merges_diagonals():
    cm = np.zeros((20, 20), "u1")
    cm[2:6, 2:6] = 3
    cm[6:10, 6:10] = 3  # touches diagonally
    labels, _ = label_components(cm, 20.0, 0.1)
    assert labels.max() == 1


def test_detect_changes_polygons_area_and_properties():
    grid = _grid(60, 60, 20.0)
    mask = np.ones(grid.shape, bool)

    def comp(red, nir, green=0.08, blue=0.05, swir=0.15, patch=None):
        bands = {k: np.full(grid.shape, v, "f4") for k, v in dict(red=red, nir=nir, green=green, blue=blue, swir16=swir).items()}
        if patch:
            (r0, r1, c0, c1), vals = patch
            for k, v in vals.items():
                bands[k][r0:r1, c0:c1] = v
        return Composite(bands, np.full(grid.shape, 3, "u1"), [], 3)

    before = comp(0.04, 0.40)  # dense vegetation everywhere
    after = comp(0.04, 0.40, patch=((10, 30, 10, 30), dict(red=0.20, nir=0.22, green=0.15, blue=0.12, swir16=0.35)))  # 20x20 px cleared
    res = detect_changes(before, after, grid, mask, T, mmu_ha=0.5)
    assert len(res.features) == 1
    p = res.features[0]["properties"]
    assert p["class"] == "vegetation_loss"
    assert p["area_ha"] == pytest.approx(400 * 0.04, abs=0.01)  # 16 ha
    assert p["ndvi_before"] > 0.8 and p["ndvi_after"] < 0.2
    assert p["confidence_level"] in {"high", "medium"}
    assert res.totals["vegetation_loss"]["area_ha"] == pytest.approx(16.0, abs=0.01)
    assert res.totals["new_water"]["polygons"] == 0
    assert res.features[0]["geometry"]["type"] == "Polygon"
    lon, lat = p["centroid_lon"], p["centroid_lat"]
    assert 32 < lon < 35 and 36 < lat < 38  # UTM 36N around 500000 E, 4100000 N


def test_confidence_monotonic():
    lo = confidence_score(0.0, 1, 0.5, 0.5)
    hi = confidence_score(1.0, 3, 10.0, 0.5)
    assert 0 <= lo < hi <= 1.0 + 1e-9
    assert confidence_level(hi) == "high" and confidence_level(lo) == "low"


# ---------------------------------------------------------------- AOI helpers
def test_aoi_parsing_and_limits():
    g = aoimod.parse_bbox("31.5,36.8,31.6,36.9")
    assert g.bounds == (31.5, 36.8, 31.6, 36.9)
    assert 80 < aoimod.area_km2(g) < 100
    with pytest.raises(aoimod.AOIError):
        aoimod.parse_bbox("1,2,3")
    with pytest.raises(aoimod.AOIError):
        aoimod.bbox_geometry(10, 5, 5, 6)
    fc = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [0, 0]}}]}
    with pytest.raises(aoimod.AOIError):
        aoimod.geometry_from_geojson(fc)
    poly = {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[[31.5, 36.8], [31.6, 36.8], [31.6, 36.9], [31.5, 36.9], [31.5, 36.8]]]}}
    assert aoimod.geometry_from_geojson(poly).area > 0


def test_grid_and_mask():
    g = box(31.50, 36.80, 31.52, 36.82)
    res = aoimod.choose_resolution(g)
    assert res == 10.0
    grid = aoimod.make_grid(g, res)
    assert grid.epsg == 32636
    assert 150 < grid.width < 220 and 200 < grid.height < 260
    m = aoimod.aoi_mask(g, grid)
    assert m.mean() > 0.95
    big = box(30.0, 36.0, 32.0, 38.0)
    assert aoimod.choose_resolution(big) == 60.0


def test_utm_zone_hemisphere():
    assert aoimod.utm_epsg(31.5, 36.8) == 32636
    assert aoimod.utm_epsg(-60.0, -10.0) == 32721
