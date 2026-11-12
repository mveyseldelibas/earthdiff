"""The real MCP server over stdio (tools/list, tools/call), talking to it with the official MCP client SDK.

Data sources are fakes (tests/mcp_fake_server.py injects fake STAC, rasters and Nominatim); everything else is the shipped server:
protocol, schemas, strict arguments, images, progress notifications, error redaction.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = str(Path(__file__).resolve().parent / "mcp_fake_server.py")
EXPECTED = {"find_place", "plan_change", "detect_change", "explain_method"}
SMALL = [31.50, 36.80, 31.53, 36.83]


def _text(res) -> str:
    return "".join(getattr(c, "text", "") for c in res.content if c.type == "text")


def _is_error(res) -> bool:
    return bool(getattr(res, "is_error", getattr(res, "isError", False)))


def _params(tmp_path, mode="ok", **env):
    e = {**os.environ, "EARTHDIFF_OUTPUT_DIR": str(tmp_path / "out"), "FAKE_MODE": mode, "PYTHONIOENCODING": "utf-8", **env}
    return StdioServerParameters(command=sys.executable, args=[SERVER], env=e)


def session_run(params, body):
    """Run ``await body(session, init)`` against a freshly spawned server."""

    async def main():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                return await body(s, init)

    return anyio.run(main)


def _hint(ann, camel):
    snake = "".join("_" + c.lower() if c.isupper() else c for c in camel)  # mcp 2.x spells it read_only_hint
    return getattr(ann, camel, getattr(ann, snake, None))


def _schema(tool):
    return getattr(tool, "input_schema", None) or tool.inputSchema


# ------------------------------------------------------------------------------------------------
def test_tools_list_and_metadata(tmp_path):
    async def body(s, init):
        info = getattr(init, "server_info", None) or init.serverInfo
        tools = {t.name: t for t in (await s.list_tools()).tools}
        prompts = {p.name for p in (await s.list_prompts()).prompts}
        return info, init.instructions, tools, prompts

    info, instructions, tools, prompts = session_run(_params(tmp_path), body)
    assert info.name == "earthdiff"
    assert "Answer honestly" in instructions and "every" in instructions.lower()
    assert set(tools) == EXPECTED
    for t in tools.values():
        assert t.description and len(t.description) > 60
    sch = _schema(tools["detect_change"])
    assert set(sch["required"]) == {"after"}
    assert {"bbox", "geojson", "before", "name", "sensitivity", "resolution_m", "mmu_ha", "max_cloud", "backend"} <= set(sch["properties"])
    assert sch["properties"]["sensitivity"]["enum"] == ["strict", "default", "sensitive"]
    ann = tools["detect_change"].annotations
    assert ann is not None and _hint(ann, "readOnlyHint") is False and _hint(ann, "destructiveHint") is False
    assert _hint(tools["find_place"].annotations, "readOnlyHint") is True and _hint(tools["explain_method"].annotations, "openWorldHint") is False
    assert "what_changed" in prompts


def test_find_place_plan_and_explain_over_stdio(tmp_path):
    async def body(s, init):
        amb = await s.call_tool("find_place", {"query": "Test Lake"})
        one = await s.call_tool("find_place", {"query": "Test Lake, Exampleland", "margin_km": 2})
        big = await s.call_tool("find_place", {"query": "Wide Valley"})
        plan = await s.call_tool("plan_change", {"bbox": SMALL, "after": "2024-07"})
        exp = await s.call_tool("explain_method", {"topic": "rules"})
        return amb, one, big, plan, exp

    amb, one, big, plan, exp = session_run(_params(tmp_path), body)
    a = json.loads(_text(amb))
    assert not _is_error(amb) and a["status"] == "ambiguous" and len(a["candidates"]) == 2
    o = json.loads(_text(one))
    assert o["status"] == "ok" and o["margin_km"] == 2
    b = json.loads(_text(big))["candidates"][0]
    assert b["fits_analysis_limit"] is False and b["narrowing_options"]["tiles"]
    p = json.loads(_text(plan))
    assert p["status"] == "ready" and p["candidate_scenes"]["after"] == 3
    e = json.loads(_text(exp))
    assert len(e["rules"]["classes"]) == 5
    assert not (tmp_path / "out" / "20").exists()


def test_detect_change_returns_images_numbers_files_and_progress(tmp_path):
    progress = []

    async def on_progress(p, total, message):
        progress.append((p, total, message))

    async def body(s, init):
        return await s.call_tool(
            "detect_change", {"bbox": SMALL, "after": "2024-07", "name": "Stdio test", "resolution_m": 20}, progress_callback=on_progress
        )

    res = session_run(_params(tmp_path, "slow"), body)
    assert not _is_error(res)
    kinds = [c.type for c in res.content]
    assert kinds == ["image", "image", "image", "text"]
    for c in res.content[:3]:
        assert getattr(c, "mime_type", getattr(c, "mimeType", None)) == "image/jpeg"
        assert base64.b64decode(c.data)[:2] == b"\xff\xd8"
    r = json.loads(res.content[-1].text)
    veg = next(c for c in r["changes"] if c["class"] == "vegetation_loss")
    assert veg["area_ha"] > 5
    assert r["warnings"] == [] and r["data_quality"]["level"] == "good" and r["comparable_fraction"] > 0.99
    assert "Contains modified Copernicus Sentinel data" in r["attribution"]
    out_root = (tmp_path / "out").resolve()
    for key in ("report_html", "changes_geojson"):
        p = Path(r["files"][key])
        assert p.is_file() and out_root in p.parents
    # progress notifications arrived, strictly increasing, with readable text and no paths
    assert len(progress) >= 5
    values = [p for p, _, _ in progress]
    assert values == sorted(values) and len(set(values)) == len(values)
    assert all(total == 100 for _, total, _ in progress)
    assert any("downloading" in (m or "") for _, _, m in progress)
    assert all(str(tmp_path) not in (m or "") for _, _, m in progress)


def test_unknown_parameters_are_rejected_not_ignored(tmp_path):
    async def body(s, init):
        a = await s.call_tool("detect_change", {"bbox": SMALL, "after": "2024-07", "crs": "EPSG:3857", "threshold": 0.1})
        b = await s.call_tool("find_place", {"query": "Test Lake", "radius": 5})
        c = await s.call_tool("explain_method", {"verbose": True})
        return a, b, c

    a, b, c = session_run(_params(tmp_path), body)
    for res, name in ((a, "detect_change"), (b, "find_place"), (c, "explain_method")):
        assert _is_error(res) and f"Unknown parameter(s) for {name}" in _text(res)
    assert "crs, threshold" in _text(a) and "Allowed:" in _text(a)
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").glob("2*"))  # nothing ran


def test_invalid_input_errors_are_clear(tmp_path):
    async def body(s, init):
        return [
            await s.call_tool("detect_change", {"bbox": [31, 36, 33, 38], "after": "2024-07"}),  # too large
            await s.call_tool("detect_change", {"after": "2024-07"}),  # no area
            await s.call_tool("detect_change", {"bbox": SMALL, "after": "07/2024"}),  # bad period
            await s.call_tool("detect_change", {"bbox": SMALL, "after": "2024-07", "sensitivity": "wild"}),  # bad enum
            await s.call_tool("plan_change", {"bbox": SMALL, "after": "2030-01"}),  # future
            await s.call_tool("detect_change", {"bbox": SMALL}),  # missing required
        ]

    results = session_run(_params(tmp_path), body)
    assert all(_is_error(r) for r in results)
    assert "limit is 2,000 km2" in _text(results[0]) and "exactly one of bbox" in _text(results[1])
    assert "future" in _text(results[4])
    for r in results:
        assert str(tmp_path) not in _text(r)


def test_name_is_sanitised_over_stdio(tmp_path):
    async def body(s, init):
        return await s.call_tool("detect_change", {"bbox": SMALL, "after": "2024-07", "resolution_m": 20, "name": "..\\..\\..\\evil/<b>x</b>"})

    res = session_run(_params(tmp_path), body)
    assert not _is_error(res)
    r = json.loads(res.content[-1].text)
    folder = Path(r["files"]["output_dir"])
    assert folder.parent == (tmp_path / "out").resolve() and "evil" in folder.name and ".." not in folder.name
    assert not (tmp_path / "evil").exists()


def test_scenes_from_unlisted_hosts_are_refused(tmp_path):
    async def body(s, init):
        return await s.call_tool("detect_change", {"bbox": SMALL, "after": "2024-07", "resolution_m": 20})

    res = session_run(_params(tmp_path, "evil"), body)
    assert _is_error(res) and "no Sentinel-2 scenes" in _text(res)


@pytest.mark.parametrize("mode,needle", [("crash", "Internal error in detect_change"), ("stacerr", "STAC search failed")])
def test_errors_never_leak_paths(tmp_path, mode, needle):
    async def body(s, init):
        return await s.call_tool("detect_change", {"bbox": SMALL, "after": "2024-07", "resolution_m": 20})

    res = session_run(_params(tmp_path, mode), body)
    text = _text(res)
    assert _is_error(res) and needle in text
    assert "bob" not in text and "secret" not in text and "C:\\" not in text and "/home" not in text and str(tmp_path) not in text


def test_runtime_setting_is_clamped_and_server_still_answers(tmp_path):
    async def body(s, init):
        return await s.call_tool("detect_change", {"bbox": SMALL, "after": "2024-07", "resolution_m": 20})

    # EARTHDIFF_MAX_RUNTIME_S is clamped to >= 60 s by design, so exercise the limit through the service-level test; here only check the
    # server starts with the clamped value and still answers normally.
    res = session_run(_params(tmp_path, "ok", EARTHDIFF_MAX_RUNTIME_S="1"), body)
    assert not _is_error(res)


def test_cli_check_and_version():
    import subprocess

    out = subprocess.run([sys.executable, "-m", "earthdiff.mcp_server", "--check"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and json.loads(out.stdout)["transport"] == "stdio"
    v = subprocess.run([sys.executable, "-m", "earthdiff.mcp_server", "--version"], capture_output=True, text=True, timeout=60)
    assert v.stdout.startswith("earthdiff-mcp 0.2.0")


# ------------------------------------------------------------------------------------------------ likely cause, seasonal check, bright surfaces
def _detect(tmp_path, mode, args):
    async def body(s, init):
        return await s.call_tool("detect_change", {"bbox": SMALL, "resolution_m": 20, **args})

    res = session_run(_params(tmp_path, mode), body)
    assert not _is_error(res), _text(res)
    return json.loads(res.content[-1].text)


def test_likely_cause_over_stdio(tmp_path):
    r = _detect(tmp_path, "fire", {"after": "2024-07"})
    lc = r["vegetation_loss_likely_cause"]
    assert lc["totals"]["fire-like"]["polygons"] == 1 and "not a verdict" in lc["note"] and len(lc["sources"]) == 3
    top = r["largest_areas"][0]
    assert top["likely_cause"] == "fire-like" and "dNBR" in top["likely_cause_basis"] and top["dnbr"] > 0.4
    feats = json.loads(Path(r["files"]["changes_geojson"]).read_text(encoding="utf-8"))["features"]
    assert feats[0]["properties"]["likely_cause"] == "fire-like"
    r2 = _detect(tmp_path / "b", "clear", {"after": "2024-07"})
    assert r2["vegetation_loss_likely_cause"]["totals"]["clearing-like"]["polygons"] == 1


def test_seasonal_check_over_stdio_and_in_plan(tmp_path):
    async def body(s, init):
        plan = await s.call_tool("plan_change", {"bbox": SMALL, "before": "2024-07", "after": "2024-08", "seasonal_check": True})
        off = await s.call_tool("plan_change", {"bbox": SMALL, "after": "2024-07"})
        det = await s.call_tool("detect_change", {"bbox": SMALL, "before": "2024-07", "after": "2024-08", "resolution_m": 20, "seasonal_check": True})
        plain = await s.call_tool("detect_change", {"bbox": SMALL, "before": "2024-07", "after": "2024-08", "resolution_m": 20})
        return plan, off, det, plain

    plan, off, det, plain = session_run(_params(tmp_path, "seasonal"), body)
    p = json.loads(_text(plan))["seasonal_check"]
    assert p["enabled"] and len(p["extra_windows"]) == 4 and "typically adds" in p["extra_time"]
    assert json.loads(_text(off))["seasonal_check"]["enabled"] is False
    r = json.loads(det.content[-1].text)
    assert r["seasonal_check"]["likely_seasonal"]["polygons"] == 1 and r["seasonal_check"]["extra_composites"] == 4
    assert r["largest_areas"][0]["likely_seasonal"] is True
    assert any("likely_seasonal" in w for w in r["warnings"])
    assert json.loads(plain.content[-1].text)["seasonal_check"] is None  # off unless asked for


def test_bright_surface_warning_over_stdio(tmp_path):
    r = _detect(tmp_path, "salt", {"after": "2024-07"})
    assert r["total_changed_ha"] == 0
    assert r["bright_surface"]["reliable"] is False
    assert r["confidence"]["level"] == "low" and r["confidence"]["reliable"] is False
    assert r["data_quality"]["level"] == "poor"
    assert any("NOT reliable" in w for w in r["warnings"])


def test_new_tool_parameters_are_in_the_schema_and_explain_method_has_the_topics(tmp_path):
    async def body(s, init):
        tools = {t.name: t for t in (await s.list_tools()).tools}
        exp = await s.call_tool("explain_method", {"topic": "likely_cause"})
        bad = await s.call_tool("detect_change", {"bbox": SMALL, "after": "2024-07", "seasonal": True})
        return tools, exp, bad

    tools, exp, bad = session_run(_params(tmp_path), body)
    for name in ("plan_change", "detect_change"):
        prop = _schema(tools[name])["properties"]["seasonal_check"]
        assert prop["type"] == "boolean" and prop["default"] is False
    assert "Key & Benson" in _text(exp)
    assert _is_error(bad) and "Unknown parameter(s) for detect_change: seasonal" in _text(bad)
