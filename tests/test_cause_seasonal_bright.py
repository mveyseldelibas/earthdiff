"""Likely cause (dNBR), seasonal check and bright-surface warning: unit tests with synthetic rasters (no network)."""
import json
from datetime import date

import numpy as np
import pytest
from shapely.geometry import box

import fakes
from earthdiff import cause as causemod
from earthdiff.cause import classify_cause
from earthdiff.change import bright_mask, polygonize
from earthdiff.composite import BANDS, BANDS_WITH_B12, build_composite
from earthdiff.indices import compute_indices, nbr
from earthdiff.mcp_service import EarthDiffService, Settings, confidence_summary, seasonal_plan
from earthdiff.periods import parse_period
from earthdiff.pipeline import RunOptions, run_analysis
from earthdiff.seasonal import prior_windows, subgrid

GEOM = box(31.50, 36.80, 31.53, 36.83)


def run(tmp_path, reader, before="2023-07", after="2024-07", **opts):
    opts.setdefault("resolution", 20)
    return run_analysis(GEOM, parse_period(before), parse_period(after), tmp_path, RunOptions(name="t", **opts), reader=reader, search=fakes.fake_search)


# ------------------------------------------------------------------------------------------------ indices / composite
def test_nbr_formula_and_optional_in_compute_indices():
    nir, s2 = np.array([0.4], "f4"), np.array([0.1], "f4")
    assert nbr(nir, s2)[0] == pytest.approx(0.3 / 0.5, rel=1e-5)
    bands = {k: np.full((2, 2), 0.2, "f4") for k in BANDS}
    assert "nbr" not in compute_indices(bands)
    bands["swir22"] = np.full((2, 2), 0.1, "f4")
    assert "nbr" in compute_indices(bands)


def test_unreadable_b12_does_not_block_the_composite():
    from earthdiff.aoi import make_grid

    grid = make_grid(GEOM, 40)
    mask = np.ones(grid.shape, bool)
    good = fakes.make_reader()

    def reader(scene, band, g):
        if band == "swir22":
            raise OSError("HTTP 404")
        return good(scene, band, g)

    comp = build_composite(fakes.fake_search("x", GEOM, parse_period("2023-07")), grid, mask, reader=reader, bands=BANDS_WITH_B12)
    assert "swir22" not in comp.bands and set(BANDS) <= set(comp.bands) and comp.count.max() == 3
    comp2 = build_composite(fakes.fake_search("x", GEOM, parse_period("2023-07")), grid, mask, reader=good, bands=BANDS_WITH_B12)
    assert "swir22" in comp2.bands and np.isfinite(comp2.bands["swir22"]).all()


# ------------------------------------------------------------------------------------------------ cause rules
def m(dnbr, vis_after, rect, area=10.0):
    return {"dnbr": dnbr, "vis_after": vis_after, "vis_delta": 0.0, "rectangularity": rect, "area_ha": area}


@pytest.mark.parametrize(
    "metrics,expected",
    [
        (m(0.60, 0.08, 0.45), "fire-like"),  # strong dNBR, dark, irregular
        (m(0.35, 0.07, 0.50), "fire-like"),
        (m(0.60, 0.12, 0.45), "unclear"),  # burn-like dNBR and irregular outline, but not dark: not enough for fire-like
        (m(0.05, 0.25, 0.92), "clearing-like"),  # no burn signal, bright, straight edges
        (m(0.15, 0.16, 0.85), "clearing-like"),
        (m(0.60, 0.25, 0.95), "clearing-like"),  # burn-like dNBR but bright and rectangular: more evidence for clearing
        (m(0.60, 0.16, 0.70), "unclear"),  # strong dNBR, nothing else
        (m(0.30, 0.16, 0.92), "unclear"),  # one for fire, one for clearing
        (m(0.05, 0.16, 0.70), "clearing-like"),  # no burn signal alone is evidence for clearing
        (m(None, 0.16, 0.70), "unclear"),
    ],
)
def test_classify_cause(metrics, expected):
    label, basis = classify_cause(metrics)
    assert label == expected and basis


def test_cause_thresholds_cite_their_sources():
    assert (causemod.DNBR_LOW, causemod.DNBR_MODERATE_LOW, causemod.DNBR_MODERATE_HIGH) == (0.10, 0.27, 0.44)
    doc = causemod.__doc__
    assert "Key & Benson" in doc and "EFFIS" in doc and "UN-SPIDER" in doc and "ours" in doc


# ------------------------------------------------------------------------------------------------ pipeline
def test_pipeline_labels_fire_like_and_clearing_like(tmp_path):
    res = run(tmp_path / "fire", fakes.make_reader(kind="fire"))
    f = json.loads((tmp_path / "fire" / "changes.geojson").read_text(encoding="utf-8"))["features"]
    veg = [x["properties"] for x in f if x["properties"]["class"] == "vegetation_loss"]
    assert len(veg) == 1 and veg[0]["likely_cause"] == "fire-like"
    assert veg[0]["dnbr"] > 0.44 and "dNBR" in veg[0]["likely_cause_basis"] and veg[0]["rectangularity"] > 0.9
    s = res.summary
    assert s["likely_cause"]["totals"]["fire-like"]["area_ha"] > 5 and s["likely_cause"]["totals"]["clearing-like"]["polygons"] == 0
    assert "not a verdict" in s["likely_cause"]["note"] and len(s["likely_cause"]["sources"]) == 3
    html = (tmp_path / "fire" / "report.html").read_text(encoding="utf-8")
    assert "Likely cause" in html and "fire-like" in html

    res = run(tmp_path / "clear", fakes.make_reader(kind="clear"))
    assert res.summary["likely_cause"]["totals"]["clearing-like"]["polygons"] == 1
    res = run(tmp_path / "unclear", fakes.make_reader(kind="default"))
    assert res.summary["likely_cause"]["totals"]["unclear"]["polygons"] == 1


def test_likely_cause_can_be_switched_off_and_reads_no_b12(tmp_path):
    seen = set()
    good = fakes.make_reader()

    def reader(scene, band, grid):
        seen.add(band)
        return good(scene, band, grid)

    res = run(tmp_path, reader, likely_cause=False)
    assert "swir22" not in seen and res.summary["likely_cause"] is None
    f = json.loads((tmp_path / "changes.geojson").read_text(encoding="utf-8"))["features"]
    assert all("likely_cause" not in x["properties"] for x in f)


def test_missing_b12_degrades_with_a_warning(tmp_path):
    good = fakes.make_reader()

    def reader(scene, band, grid):
        if band == "swir22":
            raise KeyError(band)
        return good(scene, band, grid)

    res = run(tmp_path, reader)
    assert res.summary["likely_cause"] is None and any("B12" in w for w in res.summary["warnings"])
    assert res.summary["totals"]["vegetation_loss"]["area_ha"] > 5  # the rest of the analysis is unaffected


def test_cli_summary_prints_new_lines(tmp_path, capsys):
    from earthdiff.cli import print_summary

    s = run(tmp_path, fakes.make_reader(kind="fire"), seasonal_check=True).summary
    print_summary(s)
    out = capsys.readouterr().out
    assert "likely cause" in out and "fire-like" in out and "seasonal check" in out


# ------------------------------------------------------------------------------------------------ seasonal
def test_prior_windows_and_subgrid():
    b, a = parse_period("2024-07"), parse_period("2024-08")
    w = prior_windows(b, a, 2)
    assert [(x.start.year, y.start.year) for x, y in w] == [(2023, 2023), (2022, 2022)]
    from earthdiff.aoi import make_grid

    grid = make_grid(GEOM, 20)
    mask = np.zeros(grid.shape, bool)
    mask[10:20, 30:50] = True
    sub, (r0, r1, c0, c1) = subgrid(mask, grid, pad=1)
    assert (r0, r1, c0, c1) == (9, 21, 29, 51) and sub.res == grid.res
    assert sub.transform.c == pytest.approx(grid.transform.c + 29 * 20) and sub.transform.f == pytest.approx(grid.transform.f - 9 * 20)
    assert subgrid(np.zeros(grid.shape, bool), grid) is None


def test_seasonal_check_flags_recurring_loss_but_keeps_polygons(tmp_path):
    reader = fakes.make_reader(change_year=None, repeat_month=8)  # the patch is cleared every August
    res = run(tmp_path, reader, before="2024-07", after="2024-08", seasonal_check=True)
    s = res.summary
    f = json.loads((tmp_path / "changes.geojson").read_text(encoding="utf-8"))["features"]
    veg = [x["properties"] for x in f if x["properties"]["class"] == "vegetation_loss"]
    assert len(veg) == 1, "the polygon must not be removed"
    assert veg[0]["likely_seasonal"] is True and veg[0]["seasonal_years_repeating"] == 2 and veg[0]["seasonal_years_checked"] == 2
    sea = s["seasonal_check"]
    assert sea["enabled"] and sea["likely_seasonal"]["polygons"] == 1 and sea["likely_seasonal"]["share_of_vegetation_loss"] == 1.0
    assert sea["extra_composites"] == 4 and sea["elapsed_s"] >= 0
    assert any("likely_seasonal" in w for w in s["warnings"])


def test_seasonal_check_does_not_flag_a_one_off_loss(tmp_path):
    res = run(tmp_path, fakes.make_reader(), seasonal_check=True)  # only 2024 changes; defaults compare 2023-07 with 2024-07
    veg = [x["properties"] for x in json.loads((tmp_path / "changes.geojson").read_text(encoding="utf-8"))["features"] if x["properties"]["class"] == "vegetation_loss"]
    assert veg[0]["likely_seasonal"] is False and veg[0]["seasonal_years_repeating"] == 0
    assert res.summary["seasonal_check"]["likely_seasonal"]["polygons"] == 0
    assert not any("likely_seasonal" in w for w in res.summary["warnings"])
    # default before is the 2023 window, which is reused: only the 2022 and 2021 windows are new
    assert res.summary["seasonal_check"]["extra_composites"] == 2


def test_seasonal_check_is_off_by_default_and_costs_nothing(tmp_path):
    calls = []
    good = fakes.make_reader()

    def reader(scene, band, grid):
        calls.append(scene.datetime.year)
        return good(scene, band, grid)

    res = run(tmp_path, reader)
    assert res.summary["seasonal_check"] is None and set(calls) == {2023, 2024}


def test_seasonal_check_without_earlier_data_says_so(tmp_path):
    def search(backend, geom, period, max_cloud=80.0, limit=30):
        return fakes.fake_search(backend, geom, period) if period.start.year >= 2023 else []

    res = run_analysis(GEOM, parse_period("2023-07"), parse_period("2024-07"), tmp_path, RunOptions(name="t", resolution=20, seasonal_check=True),
                       reader=fakes.make_reader(), search=search)
    assert res.summary["seasonal_check"]["no_data_polygons"] == 1
    assert any("no usable scenes in earlier years" in w for w in res.summary["warnings"])


# ------------------------------------------------------------------------------------------------ bright surfaces
def test_bright_mask_rule():
    one = lambda v: np.full((2, 2), v, "f4")  # noqa: E731
    salt = {"blue": one(0.35), "green": one(0.38), "red": one(0.40), "nir": one(0.42)}
    veg = {"blue": one(0.05), "green": one(0.08), "red": one(0.04), "nir": one(0.40)}
    dark_soil = {"blue": one(0.10), "green": one(0.12), "red": one(0.15), "nir": one(0.20)}
    assert bright_mask(salt).all() and not bright_mask(veg).any() and not bright_mask(dark_soil).any()


def test_bright_surface_warning_and_lowered_confidence(tmp_path):
    res = run(tmp_path, fakes.make_reader(salt=True))
    s = res.summary
    assert s["bright_surface"]["reliable"] is False and s["bright_surface"]["fraction_of_comparable_area"] == 1.0
    assert any("NOT reliable" in w and "bright" in w for w in s["warnings"])
    # a normal landscape does not trigger it
    ok = run(tmp_path / "ok", fakes.make_reader()).summary
    assert ok["bright_surface"]["reliable"] is True and not any("bright" in w for w in ok["warnings"])


def test_polygon_confidence_is_lowered_on_bright_pixels():
    labels = np.zeros((20, 20), np.int32)
    labels[5:15, 5:15] = 1
    from earthdiff.aoi import make_grid

    grid = make_grid(GEOM, 20)
    ib = {k: np.full(labels.shape, v, "f4") for k, v in dict(ndvi=0.7, mndwi=-0.4, bsi=-0.1).items()}
    ia = {k: np.full(labels.shape, v, "f4") for k, v in dict(ndvi=0.1, mndwi=-0.4, bsi=0.2).items()}
    obs = np.full(labels.shape, 5, "u1")
    from earthdiff.change import Thresholds

    plain = polygonize(labels, np.array([0, 1], "u1"), grid, ib, ia, obs, obs, Thresholds(), 0.5)
    bright = np.ones(labels.shape, bool)
    low = polygonize(labels, np.array([0, 1], "u1"), grid, ib, ia, obs, obs, Thresholds(), 0.5, bright, bright)
    assert low[0]["properties"]["confidence"] == pytest.approx(plain[0]["properties"]["confidence"] * 0.5, abs=0.01)
    assert low[0]["properties"]["bright_share"] == 1.0 and plain[0]["properties"]["bright_share"] == 0.0


def test_confidence_summary_is_capped_when_surface_is_bright():
    f = {"properties": {"area_ha": 10, "confidence": 0.9, "confidence_level": "high"}}
    ok = confidence_summary([f], {"reliable": True, "fraction_of_comparable_area": 0.0})
    assert ok["level"] == "high" and "reliable" not in ok
    bad = confidence_summary([f], {"reliable": False, "fraction_of_comparable_area": 0.5})
    assert bad["level"] == "low" and bad["reliable"] is False and bad["area_weighted_mean"] == 0.45 and "LOWERED" in bad["note"]
    none = confidence_summary([], {"reliable": False, "fraction_of_comparable_area": 0.7})
    assert none["level"] == "low" and none["area_weighted_mean"] is None


# ------------------------------------------------------------------------------------------------ MCP service layer
def make_service(tmp_path, reader):
    return EarthDiffService(Settings(output_dir=tmp_path / "out"), search=fakes.fake_search, reader=reader, geocoder=fakes.default_geocoder(), today=lambda: date(2025, 1, 15))


SMALL = [31.50, 36.80, 31.53, 36.83]


def test_mcp_result_carries_cause_seasonal_and_bright_fields(tmp_path):
    r = make_service(tmp_path, fakes.make_reader(change_year=None, repeat_month=8, kind="fire")).detect_change(
        bbox=SMALL, before="2024-07", after="2024-08", resolution_m=20, seasonal_check=True).result
    lc = r["vegetation_loss_likely_cause"]
    assert lc["totals"]["fire-like"]["polygons"] == 1 and "not a verdict" in lc["note"]
    assert r["seasonal_check"]["likely_seasonal"]["polygons"] == 1
    top = r["largest_areas"][0]
    assert top["likely_cause"] == "fire-like" and top["likely_seasonal"] is True and top["dnbr"] > 0.4
    assert r["bright_surface"]["reliable"] is True
    text = " ".join(r["present_to_user"])
    assert "never 'it was a fire'" in text and "likely_seasonal" in text and "bright_surface" in text


def test_mcp_bright_surface_lowers_confidence_and_quality(tmp_path):
    r = make_service(tmp_path, fakes.make_reader(salt=True)).detect_change(bbox=SMALL, after="2024-07", resolution_m=20).result
    assert r["total_changed_ha"] == 0  # a bright surface looks unchanged: exactly the false reassurance this must not give
    assert r["bright_surface"]["reliable"] is False
    assert r["confidence"]["level"] == "low" and r["confidence"]["reliable"] is False
    assert r["data_quality"]["level"] == "poor" and any("bright" in x for x in r["data_quality"]["reasons"])
    assert any("NOT reliable" in w for w in r["warnings"])


def test_plan_change_reports_the_seasonal_check_cost_and_windows(tmp_path):
    svc = make_service(tmp_path, fakes.make_reader())
    off = svc.plan_change(bbox=SMALL, after="2024-07")["seasonal_check"]
    assert off["enabled"] is False and off["extra_windows"] == ["2022-07", "2021-07"] and off["extra_composites_at_most"] == 2 and "off by default" in off["extra_time"]
    on = svc.plan_change(bbox=SMALL, after="2024-08", before="2024-07", seasonal_check=True)["seasonal_check"]
    assert on["enabled"] is True and len(on["extra_windows"]) == 4
    p = seasonal_plan(parse_period("2023-07"), parse_period("2024-07"), 2, False)
    assert p["extra_windows"] == ["2022-07", "2021-07"]


def test_explain_method_covers_the_new_topics(tmp_path):
    svc = make_service(tmp_path, fakes.make_reader())
    lc = svc.explain_method("likely_cause")["likely_cause"]
    assert "Key & Benson" in json.dumps(lc) and "0.27" in json.dumps(lc) and "Never state a cause as fact" in lc["caveat"]
    assert "likely_seasonal" in json.dumps(svc.explain_method("seasonal")) and "20%" in json.dumps(svc.explain_method("bright_surfaces"))
    assert {"likely_cause", "seasonal", "bright_surfaces"} <= set(svc.explain_method())


def test_wet_to_dry_salt_gives_an_exploratory_signal_not_a_mapped_change(tmp_path):
    res = run(tmp_path, fakes.make_reader(salt=True, wet_salt_year=2023, change_year=None))  # 2023 wet bright surface, 2024 dry
    bs = res.summary["bright_surface"]
    wl = bs["water_like_share_of_bright_pixels"]
    assert bs["reliable"] is False and wl["before"] == 1.0 and wl["after"] == 0.0
    assert res.summary["totals"]["water_loss"]["area_ha"] == 0  # MNDWI-based rules see nothing: the default rules cannot show this
    assert any("Exploratory signal" in w and "drier" in w and "Not a mapped change" in w for w in res.summary["warnings"])
    dry = run(tmp_path / "dry", fakes.make_reader(salt=True, change_year=None)).summary
    assert dry["bright_surface"]["water_like_share_of_bright_pixels"]["before"] == 0.0
    assert not any("Exploratory signal" in w for w in dry["warnings"])


def test_large_change_warning_names_a_burn_when_dnbr_explains_it():
    from types import SimpleNamespace

    from earthdiff.periods import parse_period
    from earthdiff.pipeline import RunOptions, _warnings

    opts, comp = RunOptions(), SimpleNamespace(scenes=[1, 2, 3])
    jul, sep = parse_period("2021-07-01/2021-07-27"), parse_period("2021-08-10/2021-09-10")
    totals = {"vegetation_loss": {"area_ha": 6131.0}, "new_bare_or_built": {"area_ha": 24.0}}
    burn = _warnings(opts, jul, sep, comp, comp, 1.0, totals, 97.9, extra={"cause": {"totals": {"fire-like": {"area_ha": 6125.0}}}})
    assert any("one large burn" in w for w in burn) and not any("mismatch rather than" in w for w in burn)
    plain = _warnings(opts, jul, sep, comp, comp, 1.0, totals, 97.9, extra={})
    assert any("mismatch rather than real land-cover change" in w for w in plain)
