import pytest
from shapely.geometry import box

from earthdiff.periods import parse_period
from earthdiff.stac import search_scenes


@pytest.mark.network
def test_earth_search_returns_scenes_for_tiny_aoi():
    scenes = search_scenes("earth-search", box(31.50, 36.90, 31.51, 36.91), parse_period("2023-07"), limit=5)
    assert scenes, "expected Sentinel-2 L2A scenes"
    s = scenes[0]
    assert set(s.hrefs) >= {"red", "nir", "swir16", "green", "blue", "scl"}
    assert s.hrefs["red"].startswith("https://")
    scale, offset = s.radiometry["red"]
    assert scale == pytest.approx(1e-4) and offset in (0.0, pytest.approx(-0.1))


@pytest.mark.network
def test_mcp_end_to_end_real_data(tmp_path):
    """The shipped MCP server over stdio against the real services: Nominatim, Earth Search STAC and Sentinel-2 COGs.

    Small public example (Lake Mead, Nevada, July 2019 vs July 2022): the shoreline retreated, so water loss must be found.
    Takes a few minutes and about 300 MB of RAM at 30 m."""
    import json
    import os
    import sys

    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {**os.environ, "EARTHDIFF_OUTPUT_DIR": str(tmp_path / "out"), "PYTHONIOENCODING": "utf-8"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "earthdiff.mcp_server"], env=env)
    bbox = [-114.72, 36.10, -114.62, 36.16]  # about 60 km2 in Boulder Basin

    async def main():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                place = await s.call_tool("find_place", {"query": "Lake Mead", "country_codes": "us", "limit": 2})
                plan = await s.call_tool("plan_change", {"bbox": bbox, "before": "2019-07", "after": "2022-07", "resolution_m": 30})
                det = await s.call_tool("detect_change", {"bbox": bbox, "before": "2019-07", "after": "2022-07", "resolution_m": 30, "name": "network test"})
                return place, plan, det

    place, plan, det = anyio.run(main)
    txt = lambda res: "".join(c.text for c in res.content if c.type == "text")  # noqa: E731
    assert json.loads(txt(place))["candidates"], "Nominatim returned nothing"
    assert json.loads(txt(plan))["status"] == "ready"
    assert [c.type for c in det.content] == ["image", "image", "image", "text"]
    result = json.loads(txt(det))
    loss = next(c for c in result["changes"] if c["class"] == "water_loss")
    assert loss["area_ha"] > 20, result["changes"]
    assert result["attribution"].startswith("Contains modified Copernicus Sentinel data")
