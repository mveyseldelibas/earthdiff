"""History: an old, partly regrown burn is judged on its largest one-year step (no network)."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from earthdiff.history import refine_with_history


def _pt(kind, date, nbr, vis, h=6, w=6):
    return {"kind": kind, "date": date, "nbr": np.full((h, w), nbr, np.float32), "ndvi": np.full((h, w), 0.5, np.float32), "vis": np.full((h, w), vis, np.float32)}


def _result(cause="unclear"):
    labels = np.zeros((6, 6), np.int32)
    labels[1:5, 1:5] = 1
    feat = {"properties": {"id": 1, "class": "vegetation_loss", "area_ha": 120.0, "likely_cause": cause, "rectangularity": 0.4}}
    return SimpleNamespace(labels=labels, features=[feat], label_ids=[1], shape_stats={1: {"rectangularity": 0.4}})


def test_an_old_burn_is_recognised_on_its_step():
    r = _result("unclear")
    # before 0.60 -> burn year 0.05 (dark) -> regrowth 0.30 -> after 0.42: end-to-end dNBR only 0.18, the step 0.55
    series = [_pt("before", "2020-08", 0.60, 0.07), _pt("between", "2021-08-14", 0.05, 0.05), _pt("between", "2022-08-29", 0.30, 0.07), _pt("after", "2024-08", 0.42, 0.07)]
    out = refine_with_history(r, series, 1)
    p = r.features[0]["properties"]
    assert p["likely_cause"] == "fire-like" and p["event_before"] == "2020-08" and p["event_after"] == "2021-08-14"
    assert p["event_dnbr"] == 0.55 and "largest one-step drop between 2020-08 and 2021-08-14" in p["likely_cause_basis"]
    assert out["totals"]["fire-like"]["area_ha"] == 120.0 and out["from_in_between_years"]["relabelled_polygons"] == 1


def test_a_steady_loss_keeps_its_end_to_end_judgement():
    r = _result("clearing-like")
    # a gradual decline: no single step stands out, so the original hint is kept
    series = [_pt("before", "2020-08", 0.60, 0.2), _pt("between", "2021-08", 0.50, 0.2), _pt("between", "2022-08", 0.40, 0.2), _pt("after", "2023-08", 0.30, 0.2)]
    refine_with_history(r, series, 1)
    p = r.features[0]["properties"]
    assert p["likely_cause"] == "clearing-like" and "likely_cause_from" not in p and p["event_after"] == "2021-08"


def test_no_in_between_years_means_no_change():
    r = _result()
    assert refine_with_history(r, [_pt("before", "2023-08", 0.6, 0.1), _pt("after", "2024-08", 0.1, 0.05)], 1) is None
