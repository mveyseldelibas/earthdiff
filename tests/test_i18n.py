"""Turkish report texts: every warning the pipeline can produce has a translation."""
from __future__ import annotations

from types import SimpleNamespace

from earthdiff.change import CLASSES
from earthdiff.i18n import CLASS_LABELS_TR, basis_tr, has_warning_tr, method_html_tr, tr_num, warning_tr
from earthdiff.periods import parse_period
from earthdiff.pipeline import RunOptions, _warnings, period_warnings


def _all_pipeline_warnings() -> list[str]:
    opts = RunOptions()
    one, none = SimpleNamespace(scenes=[1]), SimpleNamespace(scenes=[])
    jul, jan = parse_period("2023-07"), parse_period("2024-01")
    short = parse_period("2024-07-01/2024-07-05")
    totals = {"vegetation_loss": {"area_ha": 900.0}, "new_bare_or_built": {"area_ha": 40.0}}
    bright = SimpleNamespace(bright_fraction=0.56, bright_both_fraction=0.5)
    extra = {
        "bright_surface": {"water_like_share_of_bright_pixels": {"before": 0.84, "after": 0.015}},
        "seasonal": {"enabled": True, "checked_polygons": 10,
                     "likely_seasonal": {"polygons": 3, "area_ha": 1234.5, "share_of_vegetation_loss": 0.27}},
    }
    w = []
    w += period_warnings(jul, jan) + period_warnings(short, short)
    w += _warnings(opts, jul, parse_period("2024-07"), one, none, 0.4, totals, 10.0, bright, extra,
                   "likely_cause is unavailable: Sentinel-2 band B12 (SWIR2) could not be read for these scenes, so vegetation loss is not labelled fire-like / clearing-like.")
    w += _warnings(opts, jul, parse_period("2024-07"), one, one, 1.0, totals, 10.0,
                   extra={"cause": {"totals": {"fire-like": {"area_ha": 900.0}}}, "seasonal": {"enabled": True, "checked_polygons": 0, "likely_seasonal": {"polygons": 0}}})
    return w


def test_every_pipeline_warning_has_a_turkish_version():
    ws = _all_pipeline_warnings()
    assert len(ws) >= 10
    missing = [x for x in ws if not has_warning_tr(x)]
    assert not missing, missing
    assert all(warning_tr(x) != x for x in ws)


def test_unknown_warning_falls_back_to_english():
    assert warning_tr("Something new.") == "Something new."


def test_numbers_and_basis_and_method():
    assert tr_num("1,234.5") == "1.234,5"
    b = basis_tr("dNBR 0.38 (at least moderate-low burn severity); dark after (visible reflectance 0.05), like char or ash; irregular outline (rectangularity 0.21)")
    assert b == "dNBR 0,38 (en az orta-düşük yanık şiddeti); sonrasında koyu yüzey (görünür yansıma 0,05), kömür ya da kül gibi; düzensiz sınır (dikdörtgenlik 0,21)"
    assert set(CLASS_LABELS_TR) == {c.key for c in CLASSES.values()}
    html = method_html_tr(CLASSES.values(), 0.5, 10.0)
    assert "Bitki kaybı" in html and "0,5&nbsp;ha" in html
