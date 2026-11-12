"""Unit tests of the MCP tool logic with fake STAC / raster / Nominatim data (no network, no MCP transport)."""
import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
from shapely.geometry import box

import fakes
from earthdiff import geocode
from earthdiff.geocode import GeocodeError, Nominatim
from earthdiff.mcp_service import (
    EarthDiffService, ProgressMapper, RunStopped, Settings, ToolInputError, assess_quality, centered_bbox_within, check_area,
    confidence_summary, expand_bbox, parse_area, parse_periods, tile_bbox,
)
from earthdiff.periods import parse_period
from earthdiff.safety import clean_text, host_allowed, redact, run_folder, safe_filename, STAC_HOSTS, DATA_HOSTS

TODAY = date(2025, 1, 15)
SMALL = [31.50, 36.80, 31.53, 36.83]  # ~9 km2


def make_service(tmp_path, search=fakes.fake_search, reader=None, geocoder=None, **settings):
    s = Settings(output_dir=tmp_path / "out", **settings)
    return EarthDiffService(s, search=search, reader=reader or fakes.make_reader(), geocoder=geocoder or fakes.default_geocoder(), today=lambda: TODAY)


# ------------------------------------------------------------------------------------------------ safety
def test_clean_text_strips_control_and_bidi():
    assert clean_text("Gö\u202el\x00 \n  Test\u200b") == "Gö l Test"
    assert len(clean_text("x" * 500, 50)) == 50


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../etc/passwd", "etc_passwd"),
        ("C:\\Windows\\System32", "C_Windows_System32"),
        ("Örnek Gölü çevresi", "Ornek_Golu_cevresi"),
        ("", "area"),
        ("....", "area"),
        ("a/b\\c:d*e?f", "a_b_c_d_e_f"),
        ("<script>alert(1)</script>", "script_alert_1_script"),
    ],
)
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_safe_filename_length_and_no_separators():
    s = safe_filename("x/" * 200)
    assert len(s) <= 48 and "/" not in s and "\\" not in s


def test_run_folder_is_confined_and_unique(tmp_path):
    root = tmp_path / "root"
    now = datetime(2026, 11, 6, 10, 0, 0)
    a = run_folder(root, "../../evil", now)
    b = run_folder(root, "../../evil", now)
    assert a != b and a.parent == root.resolve() and b.parent == root.resolve()
    assert a.name == "20261106-100000_evil" and b.name.endswith("-2")
    assert not (tmp_path / "evil").exists()


def test_redact_removes_paths_but_keeps_urls():
    msg = r"could not open C:\Users\bob\secret\file.tif and /home/bob/data/x.tif; see https://earth-search.aws.element84.com/v1/search for 2023/07/01 and/or more"
    out = redact(msg, ("bob",))
    assert "C:\\Users" not in out and "/home/bob" not in out and "bob" not in out
    assert "https://earth-search.aws.element84.com/v1/search" in out and "2023/07/01" in out and "and/or" in out
    assert "<path>" in redact(r"\\server\share\x.tif")


def test_redact_extra_literals():
    assert "myhome" not in redact("failed in /myhome/out", ("/myhome",))


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://sentinel-cogs.s3.us-west-2.amazonaws.com/a/b.tif", True),
        ("https://sentinel2l2a01.blob.core.windows.net/x/y.tif", True),
        ("http://sentinel-cogs.s3.us-west-2.amazonaws.com/a.tif", False),
        ("https://evil.example.com/a.tif", False),
        ("https://sentinel-cogs.s3.us-west-2.amazonaws.com.evil.com/a.tif", False),
        ("https://blob.core.windows.net/a.tif", False),
        ("file:///C:/secret.tif", False),
        ("https://evilblob.core.windows.net.attacker.io/a", False),
        ("", False),
    ],
)
def test_host_allowlist(url, ok):
    assert host_allowed(url, DATA_HOSTS) is ok


def test_stac_hosts_are_the_known_backends():
    from earthdiff.stac import BACKENDS

    for be in BACKENDS.values():
        assert host_allowed(be.url, STAC_HOSTS)


# ------------------------------------------------------------------------------------------------ geocode
class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def test_nominatim_user_agent_rate_limit_and_cache(tmp_path):
    reqs = []
    clk = Clock()
    cache = tmp_path / "c" / "n.json"
    g = fakes.nominatim_with(fakes.DEFAULT_RESULTS, reqs, cache_path=cache, min_interval_s=1.1, clock=clk.now, sleep=clk.sleep, now=clk.now)
    places, cached = g.search("Test Lake")
    assert len(places) == 3 and not cached
    ua = reqs[0].headers["user-agent"]
    assert ua.startswith("earthdiff-mcp/") and "github.com/mveyseldelibas/earthdiff" in ua
    assert reqs[0].url.host == "nominatim.openstreetmap.org" and reqs[0].url.params["format"] == "jsonv2"
    assert clk.sleeps == []  # first request goes out immediately
    g.search("Wide Valley")  # a different query right after: must wait >= 1 s
    assert clk.sleeps and clk.sleeps[0] >= 1.0
    n = len(reqs)
    places, cached = g.search("test lake")  # same query (case-insensitive): served from the cache
    assert cached and len(reqs) == n and len(places) == 3
    g2 = fakes.nominatim_with(fakes.DEFAULT_RESULTS, reqs, cache_path=cache, clock=clk.now, now=clk.now)  # new process, same disk cache
    _, cached = g2.search("Test Lake")
    assert cached and len(reqs) == n


def test_nominatim_cache_expires(tmp_path):
    reqs = []
    clk = Clock()
    g = fakes.nominatim_with(fakes.DEFAULT_RESULTS, reqs, ttl_s=100, clock=clk.now, sleep=clk.sleep, now=clk.now)
    g.search("Test Lake")
    clk.t += 500
    _, cached = g.search("Test Lake")
    assert not cached and len(reqs) == 2


def test_nominatim_errors_and_hourly_cap():
    def handler(request):
        return httpx.Response(429, text="slow down")

    g = Nominatim(client=httpx.Client(transport=httpx.MockTransport(handler)), min_interval_s=0)
    with pytest.raises(GeocodeError, match="throttled"):
        g.search("anything")

    g = fakes.nominatim_with(fakes.DEFAULT_RESULTS, max_per_hour=2)
    g.search("Test Lake")
    g.search("Wide Valley")
    with pytest.raises(GeocodeError, match="limit reached"):
        g.search("nowhere")


def test_nominatim_ignores_malformed_results():
    bad = [{"display_name": "x", "boundingbox": ["a", "b", "c", "d"], "lon": "1", "lat": "2"}, {"display_name": "y", "boundingbox": ["10", "5", "1", "2"], "lon": "1", "lat": "2"}, fakes.LAKE]
    g = fakes.nominatim_with({"qq": bad})
    places, _ = g.search("qq")
    assert [p.name for p in places] == ["Test Lake"]


def test_nominatim_no_redirects_and_sanitised_names():
    evil = {**fakes.LAKE, "display_name": "Lake\u202e\x00 Evil, Land", "name": "Lake\u202e Evil"}
    g = fakes.nominatim_with({"qq": [evil]})
    p = g.search("qq")[0][0]
    assert "\u202e" not in p.name and "\x00" not in p.display_name
    assert g._client.follow_redirects is False


# ------------------------------------------------------------------------------------------------ input parsing
def test_parse_area_variants():
    assert parse_area(SMALL, None).bounds == pytest.approx(tuple(SMALL))
    assert parse_area("31.5, 36.8, 31.53, 36.83", None).bounds == pytest.approx(tuple(SMALL))
    gj = {"type": "Polygon", "coordinates": [[[31.5, 36.8], [31.53, 36.8], [31.53, 36.83], [31.5, 36.8]]]}
    assert parse_area(None, json.dumps(gj)).area > 0
    for bad in (([1, 2, 3], None), (None, None), (SMALL, gj), ([10, 10, 5, 20], None), ([1, 2, 3, float("nan")], None), ("a,b,c,d", None)):
        with pytest.raises(ToolInputError):
            parse_area(*bad)
    with pytest.raises(ToolInputError, match="not valid JSON"):
        parse_area(None, "{nope")
    with pytest.raises(ToolInputError, match="too large"):
        parse_area(None, "x" * 300_000)
    with pytest.raises(ToolInputError):
        parse_area(None, {"type": "Point", "coordinates": [1, 2]})


def test_check_area_limits():
    assert check_area(box(*SMALL), 400) == pytest.approx(9.3, abs=1)
    with pytest.raises(ToolInputError, match="limit is 400 km2"):
        check_area(box(31, 36, 33, 38), 400)
    with pytest.raises(ToolInputError, match="at least"):
        check_area(box(31.5, 36.8, 31.5005, 36.8005), 400)


def test_parse_periods_default_and_validation():
    b, a, defaulted, warns = parse_periods("2024-07", None, TODAY)
    assert defaulted and (b.start, a.start) == (date(2023, 7, 1), date(2024, 7, 1)) and warns == []
    with pytest.raises(ToolInputError, match="before the after window starts"):
        parse_periods("2024-07", "2024-07-15/2024-08-15", TODAY)
    with pytest.raises(ToolInputError, match="future"):
        parse_periods("2026-01", None, TODAY)
    with pytest.raises(ToolInputError, match="366"):
        parse_periods("2023-01/2024-06", "2021-01/2021-12", TODAY)
    with pytest.raises(ToolInputError):
        parse_periods("2024-13", None, TODAY)
    _, _, _, warns = parse_periods("2024-12/2025-02", "2023-12/2024-02", TODAY)
    assert any("beyond today" in w for w in warns)
    _, _, _, warns = parse_periods("2024-03/2024-09", "2023-03/2023-09", TODAY)
    assert any("spans several seasons" in w for w in warns)
    _, _, _, warns = parse_periods("2024-07", "2015-07", TODAY)
    assert any("before 2017" in w for w in warns)


# ------------------------------------------------------------------------------------------------ bbox helpers
def test_centered_bbox_and_tiles_respect_the_limit():
    wide = (31.3, 36.65, 31.7, 36.95)
    cb = centered_bbox_within(wide, 400)
    assert geocode_area(cb) <= 400 and geocode_area(cb) > 300
    tiles = tile_bbox(wide, 400)
    assert len(tiles) == 4 and all(t["area_km2"] <= 400 for t in tiles)
    assert {t["label"] for t in tiles} == {"north-west", "north-east", "south-west", "south-east"}
    assert tile_bbox((31, 36, 33, 38), 400) == []  # would need more than 9 tiles
    assert centered_bbox_within(tuple(SMALL), 400) == tuple(SMALL)


def geocode_area(b):
    from earthdiff.aoi import area_km2

    return area_km2(box(*b))


def test_expand_bbox():
    b = (31.5, 36.8, 31.6, 36.9)
    e = expand_bbox(b, 5)
    assert e[0] < b[0] and e[2] > b[2] and e[1] < b[1] and e[3] > b[3]
    assert expand_bbox(b, 0) == b


# ------------------------------------------------------------------------------------------------ find_place
def test_find_place_ambiguous_returns_candidates(tmp_path):
    svc = make_service(tmp_path)
    r = svc.find_place("Test Lake")
    assert r["status"] == "ambiguous"
    assert [c["osm"] for c in r["candidates"]] == ["relation/1", "node/3"]  # the duplicate node of the same lake was merged
    assert "Do not guess" in r["next_step"] and "OpenStreetMap" in r["attribution"]
    assert all(c["fits_analysis_limit"] for c in r["candidates"])


def test_find_place_single_not_found_and_margin(tmp_path):
    svc = make_service(tmp_path)
    r = svc.find_place("Test Lake, Exampleland")
    assert r["status"] == "ok" and len(r["candidates"]) == 1
    base = r["candidates"][0]["area_km2"]
    r2 = svc.find_place("Test Lake, Exampleland", margin_km=3)
    assert r2["candidates"][0]["area_km2"] > base
    assert svc.find_place("nowhere")["status"] == "not_found"


def test_find_place_oversized_offers_narrowing(tmp_path):
    svc = make_service(tmp_path)
    r = svc.find_place("Wide Valley")
    c = r["candidates"][0]
    limit = svc.settings.max_area_km2
    assert not c["fits_analysis_limit"] and c["area_km2"] > limit
    opts = c["narrowing_options"]
    assert opts["centered_bbox"]["area_km2"] <= limit and len(opts["tiles"]) >= 2
    assert all(t["area_km2"] <= limit for t in opts["tiles"])
    assert "silently" in r["next_step"] or "narrowing_options" in r["next_step"]
    huge = svc.find_place("Huge Region")["candidates"][0]["narrowing_options"]
    assert huge["tiles"] == [] and huge["tiles_note"]


def test_find_place_validation(tmp_path):
    svc = make_service(tmp_path)
    for kw in ({"country_codes": "turkey"}, {"language": "x y"}, {"limit": 9}, {"margin_km": 50}):
        with pytest.raises(ToolInputError):
            svc.find_place("Test Lake", **kw)
    with pytest.raises(ToolInputError):
        svc.find_place(" ")


def test_find_place_reports_geocoder_failure_without_internals(tmp_path):
    def handler(request):
        raise httpx.ConnectError("boom C:\\Users\\bob\\x")

    g = Nominatim(client=httpx.Client(transport=httpx.MockTransport(handler)), min_interval_s=0)
    svc = make_service(tmp_path, geocoder=g)
    with pytest.raises(ToolInputError) as e:
        svc.find_place("Test Lake")
    assert "bob" not in str(e.value) and "ConnectError" in str(e.value)


# ------------------------------------------------------------------------------------------------ plan_change
def test_plan_change_ready(tmp_path):
    svc = make_service(tmp_path)
    r = svc.plan_change(bbox=SMALL, after="2024-07")
    assert r["status"] == "ready" and r["before_window"]["defaulted_to_same_period_last_year"]
    assert r["candidate_scenes"]["before"] == 3 and r["candidate_scenes"]["after"] == 3
    assert r["area"]["center_lon_lat"] == pytest.approx([31.515, 36.815], abs=1e-3)
    assert r["warnings"] == [] and r["expected_runtime"]
    assert not (tmp_path / "out").exists()  # planning writes nothing


def test_plan_change_warnings(tmp_path):
    svc = make_service(tmp_path, search=fakes.one_scene_search)
    with pytest.raises(ToolInputError, match="seasonal cycle"):  # January vs July: refused, almost everything would look changed
        svc.plan_change(bbox=SMALL, after="2024-07", before="2023-01")
    r = svc.plan_change(bbox=SMALL, after="2024-07", before="2023-05-20/2023-06-10")  # 46 days apart: allowed, with a warning
    assert r["status"] == "ready"
    text = " ".join(r["warnings"])
    assert "seasonal cycle" in text and "Only one candidate scene" in text
    r = make_service(tmp_path, search=fakes.empty_search).plan_change(bbox=SMALL, after="2024-07")
    assert r["status"] == "no_data" and "No Sentinel-2" in " ".join(r["warnings"])


def test_plan_change_rejects_bad_input(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(ToolInputError, match="limit is 2,000"):
        svc.plan_change(bbox=[31, 36, 33, 38], after="2024-07")
    with pytest.raises(ToolInputError, match="backend"):
        svc.plan_change(bbox=SMALL, after="2024-07", backend="google")
    with pytest.raises(ToolInputError, match="resolution_m"):
        svc.plan_change(bbox=SMALL, after="2024-07", resolution_m=15)
    with pytest.raises(ToolInputError, match="max_cloud"):
        svc.plan_change(bbox=SMALL, after="2024-07", max_cloud=5)


def test_configurable_area_limit(tmp_path):
    svc = make_service(tmp_path, max_area_km2=5)
    with pytest.raises(ToolInputError, match="limit is 5 km2"):
        svc.plan_change(bbox=SMALL, after="2024-07")


# ------------------------------------------------------------------------------------------------ detect_change
def test_detect_change_end_to_end(tmp_path):
    svc = make_service(tmp_path)
    seen = []
    out = svc.detect_change(bbox=SMALL, after="2024-07", name="Unit test area", resolution_m=20, progress=lambda v, t: seen.append((v, t)))
    r = out.result
    veg = next(c for c in r["changes"] if c["class"] == "vegetation_loss")
    assert veg["area_ha"] > 5 and veg["polygons"] == 1
    assert r["comparable_fraction"] == pytest.approx(1.0) and r["scenes"] == {"before": 3, "after": 3, "detail": r["scenes"]["detail"]}
    assert r["confidence"]["level"] in {"low", "medium", "high"} and r["confidence"]["area_weighted_mean"] is not None
    assert r["data_quality"]["level"] == "good" and r["warnings"] == []
    assert r["largest_areas"][0]["class"] == "vegetation_loss" and len(r["largest_areas"][0]["centroid_lon_lat"]) == 2
    assert r["attribution"] == "Contains modified Copernicus Sentinel data 2023, 2024"
    assert r["present_to_user"] and "screening" in " ".join(r["present_to_user"]).lower()
    root = (tmp_path / "out").resolve()
    for key in ("report_html", "changes_geojson", "summary_json", "before_png", "after_png", "change_map_png", "overview_png", "change_class_tif"):
        p = Path(r["files"][key])
        assert p.is_file() and p.stat().st_size > 0 and root in p.parents, key
    assert [c for c, _ in out.images] == ["Before  2023-07", "After  2024-07", "Detected changes"]
    for _, data in out.images:
        assert data[:2] == b"\xff\xd8" and 1_000 < len(data) < 600_000
    values = [v for v, _ in seen]
    assert values == sorted(values) and values[-1] >= 90 and any("downloading" in t for _, t in seen)
    assert all("\\" not in t and "/" not in t.replace("YYYY", "") for _, t in seen)  # no paths in progress text


def test_detect_change_single_scene_and_season_mismatch_are_reported(tmp_path):
    svc = make_service(tmp_path, search=fakes.one_scene_search)
    r = svc.detect_change(bbox=SMALL, after="2024-07", before="2023-05-20/2023-06-10", resolution_m=20, name="warn test").result
    text = " ".join(r["warnings"])
    assert "Only one usable scene" in text and "seasonal cycle" in text
    assert r["data_quality"]["level"] == "poor" and r["data_quality"]["reasons"]


def test_detect_change_implausibly_large_change_warns(tmp_path):
    big = fakes.make_reader()

    def reader(scene, band, grid):
        import numpy as np

        if scene.datetime.year == 2024 and band != "scl":
            return np.full(grid.shape, dict(blue=1200, green=1500, red=2000, nir=2200, swir16=3500)[band], "u2")
        return big(scene, band, grid)

    r = make_service(tmp_path, reader=reader).detect_change(bbox=SMALL, after="2024-07", resolution_m=20).result
    assert any("often means a seasonal, atmospheric" in w for w in r["warnings"]) and r["data_quality"]["level"] == "poor"


def test_detect_change_cloudy_refuses_with_mcp_friendly_message(tmp_path):
    svc = make_service(tmp_path, reader=fakes.make_reader(cloud=True))
    with pytest.raises(ToolInputError) as e:
        svc.detect_change(bbox=SMALL, after="2024-07", resolution_m=20)
    msg = str(e.value)
    assert "cloud-free" in msg and "--force" not in msg and "--max-cloud" not in msg
    assert not list((tmp_path / "out").glob("*/*"))  # nothing half-written is left behind


def test_name_cannot_escape_the_output_folder(tmp_path):
    svc = make_service(tmp_path)
    out = svc.detect_change(bbox=SMALL, after="2024-07", name="../../../evil<script>alert(1)</script>", resolution_m=20)
    root = (tmp_path / "out").resolve()
    folder = Path(out.result["files"]["output_dir"])
    assert folder.parent == root and ".." not in folder.name and "<" not in folder.name
    assert not (tmp_path / "evil").exists()
    html = out.report_path.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html


def test_scenes_from_unlisted_hosts_are_dropped_and_never_read(tmp_path):
    called = []

    def reader(scene, band, grid):
        called.append(scene.id)
        return fakes.make_reader()(scene, band, grid)

    svc = make_service(tmp_path, search=fakes.evil_search, reader=reader)
    with pytest.raises(ToolInputError, match="no Sentinel-2 scenes"):
        svc.detect_change(bbox=SMALL, after="2024-07", resolution_m=20)
    assert called == []
    with pytest.raises(RuntimeError, match="allow-list"):
        svc.guarded_reader(fakes.evil_search("x", None, parse_period("2024-07"))[0], "red", None)


def test_errors_do_not_leak_paths(tmp_path):
    from earthdiff.stac import STACError

    def search(*a, **k):
        raise STACError(f"STAC search failed (https://earth-search.aws.element84.com/v1): cannot open {tmp_path}\\secret\\x.db and C:\\Users\\bob\\.config")

    svc = make_service(tmp_path, search=search)
    with pytest.raises(ToolInputError) as e:
        svc.detect_change(bbox=SMALL, after="2024-07", resolution_m=20)
    msg = str(e.value)
    assert "bob" not in msg and str(tmp_path) not in msg and "earth-search.aws.element84.com" in msg


def test_time_limit_and_cancel(tmp_path):
    svc = make_service(tmp_path, max_runtime_s=0)
    with pytest.raises(RunStopped, match="stopped after"):
        svc.detect_change(bbox=SMALL, after="2024-07", resolution_m=20)
    svc = make_service(tmp_path)
    with pytest.raises(RunStopped):
        svc.detect_change(bbox=SMALL, after="2024-07", resolution_m=20, should_stop=lambda: True)
    assert not list((tmp_path / "out").glob("*"))
    # the lock is released after a failure: a normal run still works
    assert svc.detect_change(bbox=SMALL, after="2024-07", resolution_m=20).result["total_changed_ha"] > 0


def test_only_one_analysis_at_a_time(tmp_path):
    svc = make_service(tmp_path)
    assert svc._run_lock.acquire(blocking=False)
    try:
        with pytest.raises(ToolInputError, match="another analysis"):
            svc.detect_change(bbox=SMALL, after="2024-07", resolution_m=20)
    finally:
        svc._run_lock.release()


def test_detect_change_rejects_bad_parameters(tmp_path):
    svc = make_service(tmp_path)
    for kw in ({"sensitivity": "extreme"}, {"mmu_ha": 0.01}, {"resolution_m": 25}, {"backend": "bing"}, {"max_cloud": 200}):
        with pytest.raises(ToolInputError):
            svc.detect_change(bbox=SMALL, after="2024-07", **kw)


# ------------------------------------------------------------------------------------------------ explain / helpers
def test_explain_method_topics(tmp_path):
    svc = make_service(tmp_path)
    full = svc.explain_method()
    assert set(full) >= {"rules", "pipeline", "confidence", "limits", "data", "screening_note"}
    keys = {c["class"] for c in full["rules"]["classes"]}
    assert keys == {"vegetation_loss", "vegetation_gain", "new_water", "water_loss", "new_bare_or_built"}
    assert full["rules"]["default_thresholds"]["veg_drop"] == 0.25
    assert full["limits"]["server_limits"]["max_area_km2"] == 2000.0
    assert "not an official" in full["screening_note"]
    assert set(svc.explain_method("limits")) == {"version", "limits", "screening_note"}
    with pytest.raises(ToolInputError):
        svc.explain_method("everything")
    assert str(tmp_path) not in json.dumps(full)


def test_settings_from_env_clamps(tmp_path):
    s = Settings.from_env({"EARTHDIFF_OUTPUT_DIR": str(tmp_path), "EARTHDIFF_MAX_AREA_KM2": "99999", "EARTHDIFF_MAX_RUNTIME_S": "1", "EARTHDIFF_OPEN_REPORT": "1", "EARTHDIFF_CONTACT": "me@example.org"})
    assert s.output_dir == tmp_path and s.max_area_km2 == 2000.0 and s.max_runtime_s == 60 and s.open_report and s.contact == "me@example.org"
    d = Settings.from_env({"EARTHDIFF_MAX_AREA_KM2": "abc"})
    assert d.max_area_km2 == 2000.0 and d.output_dir.name == "earthdiff-mcp-output"


def test_progress_mapper_is_monotonic():
    m = ProgressMapper()
    lines = [
        "AOI 9.0 km2 -> grid 100 x 100 px at 20 m (EPSG:32636)", "searching Sentinel-2 L2A scenes (earth-search) ...", "found 3 candidate scenes (before) and 3 (after)",
        "before: checking cloud cover of 3 candidate scenes", "before: downloading + compositing band 'blue' from 3 scenes", "before: downloading + compositing band 'swir16' from 3 scenes",
        "after: checking cloud cover of 3 candidate scenes", "after: downloading + compositing band 'red' from 3 scenes", "detecting changes ...", "rendering outputs ...", r"done -> C:\x\report.html",
    ]
    vals = []
    for ln in lines:
        v, t = m.update(ln)
        vals.append(v)
        assert "C:" not in t
    assert vals == sorted(vals) and vals[-1] == 99
    v, t = m.update("before: skipping S2A_x (HTTP 404 on C:\\secret\\a.tif)")
    assert v == 99 and "secret" not in t


def test_quality_and_confidence_helpers():
    summary = {"valid_fraction": 0.5, "changed_fraction_of_aoi": 0.05, "scenes_before": 4, "scenes_after": 2}
    q = assess_quality(summary, parse_period("2023-07"), parse_period("2024-07"))
    assert q["level"] == "poor" and any("cloud-free" in r for r in q["reasons"]) and any("2 scenes" in r for r in q["reasons"])
    assert confidence_summary([])["level"] is None
    f = {"properties": {"area_ha": 10, "confidence": 0.8, "confidence_level": "high"}}
    g = {"properties": {"area_ha": 10, "confidence": 0.2, "confidence_level": "low"}}
    c = confidence_summary([f, g])
    assert c["area_weighted_mean"] == 0.5 and c["level"] == "medium" and c["share_of_changed_area"]["high"] == 0.5


def test_large_change_inside_a_burn_is_not_graded_poor():
    base = {"valid_fraction": 1.0, "changed_fraction_of_aoi": 0.63, "scenes_before": 5, "scenes_after": 6,
            "totals": {"vegetation_loss": {"area_ha": 6131.0}, "new_bare_or_built": {"area_ha": 24.0}}}
    jul, sep = parse_period("2021-07-01/2021-07-27"), parse_period("2021-08-10/2021-09-10")
    # mostly fire-like (dNBR): the large share is the burn itself, data quality stays good
    burn = {**base, "likely_cause": {"totals": {"fire-like": {"area_ha": 6125.0}}}}
    assert assess_quality(burn, jul, sep)["level"] == "good"
    # same share without a burn signal (or without cause labels): still a mismatch symptom
    assert assess_quality({**base, "likely_cause": {"totals": {"fire-like": {"area_ha": 300.0}}}}, jul, sep)["level"] == "poor"
    assert assess_quality({**base, "likely_cause": None}, jul, sep)["level"] == "poor"


def test_server_json_matches_the_package_version():
    import tomllib

    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    server = json.loads((root / "server.json").read_text(encoding="utf-8"))
    assert server["version"] == version == server["packages"][0]["version"]
    assert server["name"] == "io.github.mveyseldelibas/earthdiff"
    assert f"mcp-name: {server['name']}" in (root / "README.md").read_text(encoding="utf-8")
