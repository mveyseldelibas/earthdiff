"""MCP server (stdio): ``earthdiff-mcp``. Tools: find_place, plan_change, detect_change, explain_method."""
from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Annotated, Any, Literal

try:
    import anyio
    from pydantic import Field

    try:  # mcp >= 2.0
        from mcp.server.mcpserver import Context, Image, MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import Context, Image  # type: ignore[assignment]
        from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[assignment]
        from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[assignment]
    from mcp.types import ToolAnnotations
except ImportError as _exc:  # pragma: no cover
    raise SystemExit('earthdiff-mcp needs the "mcp" extra:  pip install "earthdiff[mcp]"  (or: uvx --from "earthdiff[mcp]" earthdiff-mcp)') from _exc

from . import __version__
from .mcp_prompts import GUIDE, INSTRUCTIONS, what_changed
from .mcp_service import HARD_MAX_AREA_KM2, EarthDiffService, RunStopped, Settings, ToolInputError

log = logging.getLogger("earthdiff.mcp")

Bbox = Annotated[
    list[float] | str | None,
    Field(description="Rectangle [west, south, east, north] = [lon_min, lat_min, lon_max, lat_max] in WGS84 degrees (longitude FIRST). Alternative to geojson."),
]
GeoJson = Annotated[
    dict[str, Any] | str | None,
    Field(description="Polygon/MultiPolygon (or Feature/FeatureCollection) in WGS84 for a non-rectangular area. Alternative to bbox."),
]
After = Annotated[str, Field(description="The recent window: 'YYYY-MM' (whole month), 'YYYY-MM-DD', 'YYYY-MM/YYYY-MM' or 'YYYY-MM-DD/YYYY-MM-DD'. At most 366 days; 1 to 3 months works best.")]
Before = Annotated[str | None, Field(description="The baseline window, same formats, ending before 'after' starts. Empty = the 'after' window one year earlier (same season, recommended).")]
MaxCloud = Annotated[float, Field(description="Ignore scenes whose scene-level cloud cover is above this percent (10 to 100).", ge=10, le=100)]
Resolution = Annotated[int | None, Field(description="Analysis pixel size in metres: 10, 20, 30 or 60. Empty = automatic (finest that keeps the grid under 4 million pixels). Coarser is faster.")]
Backend = Literal["earth-search", "planetary-computer"]
Sensitivity = Literal["strict", "default", "sensitive"]
Topic = Literal["all", "rules", "pipeline", "likely_cause", "seasonal", "bright_surfaces", "confidence", "limits", "data"]
SeasonalCheck = Annotated[
    bool,
    Field(description="Also check whether the vegetation loss repeats in the same windows of the 2 earlier years (flags likely_seasonal, e.g. harvest; polygons are kept). Adds roughly 45 s or more (measured +47 s on a 39 km2 test); default false. plan_change reports the extra windows."),
]

_READ = dict(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _open_in_viewer(path: str | None) -> bool:
    """Open report.html in the default browser (``--open-report``). Never raises."""
    if not path:
        return False
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:  # noqa: BLE001
        log.warning("could not open the report in a viewer")
        return False


class _StrictServer(MCPServer):  # type: ignore[misc, valid-type]
    """Rejects unknown tool arguments instead of silently dropping them (a model that invents ``crs=`` or ``threshold=`` must
    get an error, not an analysis that ignored its wish)."""

    async def call_tool(self, name: str, arguments: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        tool = self._tool_manager.get_tool(name)
        if tool is not None and arguments:
            allowed = set((tool.parameters or {}).get("properties", {}))
            unknown = sorted(set(arguments) - allowed)
            if unknown:
                raise ToolError(f"Unknown parameter(s) for {name}: {', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}.")
        return await super().call_tool(name, arguments, *args, **kwargs)


class _Progress:
    """Thread-safe, strictly increasing progress notifications from the worker thread and a heartbeat."""

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.last = 0.0
        self.text = "starting"
        self.started = time.monotonic()
        self._lock = threading.Lock()

    def next_value(self, value: float) -> float:
        with self._lock:
            self.last = max(value, self.last + 0.01)
            return self.last

    async def send(self, value: float, text: str, elapsed: bool = False) -> None:
        self.text = text
        v = self.next_value(value)
        msg = f"{text} ({time.monotonic() - self.started:.0f} s)" if elapsed else text
        try:
            await self.ctx.report_progress(v, 100, msg)
        except Exception:  # noqa: BLE001  (client without progress support, closed stream ...)
            pass

    def from_thread(self, value: float, text: str) -> None:
        try:
            anyio.from_thread.run(self.send, value, text)
        except Exception:  # noqa: BLE001
            pass


def create_server(settings: Settings, service: EarthDiffService | None = None) -> MCPServer:
    svc = service or EarthDiffService(settings)
    try:  # mcp 2.x reports the server version in `initialize`; mcp 1.x FastMCP has no `version` argument
        mcp = _StrictServer("earthdiff", instructions=INSTRUCTIONS, version=__version__)
    except TypeError:  # pragma: no cover
        mcp = _StrictServer("earthdiff", instructions=INSTRUCTIONS)
    try:
        mcp._earthdiff_service = svc  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        pass

    async def call(name: str, fn, *args: Any, **kwargs: Any) -> Any:
        """Run a blocking service call in a worker thread; every failure becomes a path-free ToolError."""
        t0 = time.perf_counter()
        try:
            result = await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs), abandon_on_cancel=True)
        except (ToolInputError, RunStopped) as e:
            raise ToolError(svc._redact(e)) from None
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s failed", name)
            raise ToolError(f"Internal error in {name}: {type(e).__name__}. Check the server log (stderr).") from None
        log.info("tool %s ok in %.0f ms", name, (time.perf_counter() - t0) * 1000)
        return result

    # ------------------------------------------------------------------ tools
    @mcp.tool(
        description="Find a place by name (OpenStreetMap Nominatim) and get a bounding box to analyse. If several different places match, "
        "status is 'ambiguous' and you must ASK the user which one; never guess. If the box is larger than the analysis limit, "
        "narrowing_options (a centred box and tiles) are returned for the user to choose from. Rate limited to 1 request per second and cached. "
        "Use margin_km to include the surroundings of a small feature (a lake, a town).",
        annotations=ToolAnnotations(title="Find a place", **_READ),
        structured_output=False,
    )
    async def find_place(
        query: Annotated[str, Field(description="Place name, ideally with country or region, e.g. 'Lake Mead, Nevada' or 'Manavgat, Antalya'.", min_length=2, max_length=200)],
        country_codes: Annotated[str | None, Field(description="Restrict to countries: comma-separated ISO 3166-1 alpha-2 codes, e.g. 'tr' or 'us,ca'.")] = None,
        language: Annotated[str | None, Field(description="Language of the returned names, e.g. 'en' or 'tr'. Empty = local names.")] = None,
        limit: Annotated[int, Field(description="Maximum number of candidates (1 to 5).", ge=1, le=5)] = 5,
        margin_km: Annotated[float, Field(description="Expand each bounding box by this many km on every side (0 to 20), for example to include the shores around a lake.", ge=0, le=20)] = 0.0,
    ) -> str:
        res = await call("find_place", svc.find_place, query, country_codes, language, limit, margin_km)
        return _dump(res)

    @mcp.tool(
        description="Dry run for detect_change: validates the area and windows and searches Sentinel-2 scenes WITHOUT downloading imagery. "
        "Returns scene counts and dates per window, grid size, expected runtime and warnings (season mismatch, no or few scenes, clouds, "
        "very long or short windows). Call it for large areas, unusual windows, or when unsure. Takes a few seconds.",
        annotations=ToolAnnotations(title="Plan a change analysis", **_READ),
        structured_output=False,
    )
    async def plan_change(
        after: After,
        bbox: Bbox = None,
        geojson: GeoJson = None,
        before: Before = None,
        max_cloud: MaxCloud = 80.0,
        resolution_m: Resolution = None,
        backend: Annotated[Backend, Field(description="Free public STAC API. 'earth-search' (default) or 'planetary-computer' (needs the optional planetary-computer package).")] = "earth-search",
        seasonal_check: SeasonalCheck = False,
    ) -> str:
        res = await call("plan_change", svc.plan_change, bbox, geojson, after, before, max_cloud, resolution_m, backend, seasonal_check)
        return _dump(res)

    @mcp.tool(
        description="Answer 'what changed here?': compares median composites of free Sentinel-2 imagery of two windows and returns hectares and polygon "
        "counts per class (vegetation loss/gain, new water, water loss, new bare soil / built-up), a likely_cause HINT for vegetation loss (fire-like, clearing-like, unclear; from dNBR, brightness and shape), "
        "the comparable share of the area, confidence (lowered on very bright surfaces such as salt, sand, snow), data quality, warnings, the largest changed areas, and THREE IMAGES (before, after, detected changes). Also writes report.html (interactive swipe map), "
        "changes.geojson and more to the local output folder and returns their paths. Takes about 1 to 4 minutes and reports progress; one analysis at a time. "
        "Report ALL warnings to the user. It describes spectral change; likely_cause is only a hint, never a verdict; it is a screening tool, not an official assessment.",
        annotations=ToolAnnotations(title="Detect change", readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True),
        structured_output=False,
    )
    async def detect_change(
        ctx: Context,
        after: After,
        bbox: Bbox = None,
        geojson: GeoJson = None,
        before: Before = None,
        name: Annotated[str | None, Field(description="Title of the analysis (used for the report and the output folder name; sanitised).", max_length=80)] = None,
        sensitivity: Annotated[Sensitivity, Field(description="'strict' fewer, stronger changes; 'default'; 'sensitive' more, weaker changes (more false alarms).")] = "default",
        resolution_m: Resolution = None,
        mmu_ha: Annotated[float, Field(description="Minimum mapping unit in hectares: smaller changes are dropped (0.1 to 50).", ge=0.1, le=50)] = 0.5,
        max_cloud: MaxCloud = 80.0,
        backend: Annotated[Backend, Field(description="Free public STAC API. 'earth-search' (default) or 'planetary-computer'.")] = "earth-search",
        seasonal_check: SeasonalCheck = False,
    ) -> list:
        prog = _Progress(ctx)
        stop = threading.Event()
        state: dict[str, Any] = {}

        def work() -> Any:
            return svc.detect_change(
                bbox=bbox, geojson=geojson, after=after, before=before, name=name, sensitivity=sensitivity,
                resolution_m=resolution_m, mmu_ha=mmu_ha, max_cloud=max_cloud, backend=backend, seasonal_check=seasonal_check,
                progress=prog.from_thread, should_stop=stop.is_set,
            )

        async def heartbeat() -> None:
            while True:
                await anyio.sleep(8)
                await prog.send(prog.last, prog.text, elapsed=True)

        deadline = settings.max_runtime_s + 90
        err: Exception | None = None
        finished = False
        try:
            with anyio.move_on_after(deadline) as scope:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(heartbeat)
                    try:
                        state["outcome"] = await call("detect_change", work)
                    except Exception as e:  # noqa: BLE001  (ToolError): re-raised below, outside the task group
                        err = e
                    tg.cancel_scope.cancel()
            finished = True
        finally:
            if not finished:
                stop.set()  # client cancelled: the worker stops at its next progress line
        if scope.cancelled_caught:
            stop.set()
            raise ToolError(
                f"The analysis did not finish within {settings.max_runtime_s} s and was stopped. Try a smaller area, a coarser resolution_m or shorter windows."
            )
        if err is not None:
            raise err
        outcome = state["outcome"]
        await prog.send(100, "done")
        result = outcome.result
        if settings.open_report:
            result["report_opened"] = _open_in_viewer(str(outcome.report_path))
        content: list[Any] = [Image(data=data, format="jpeg") for _, data in outcome.images]
        content.append(_dump(result))
        return content

    @mcp.tool(
        description="Explain how earthdiff works and how far to trust it: the exact rules per change class with thresholds, the processing pipeline, how confidence is "
        "computed, the known limits and error sources, data sources and attribution, and this server's limits. Use it when the user asks 'how does it know?' or 'can I rely on this?'.",
        annotations=ToolAnnotations(title="Explain the method", readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        structured_output=False,
    )
    async def explain_method(
        topic: Annotated[Topic, Field(description="'all' (default), 'rules', 'pipeline', 'confidence', 'limits' or 'data'.")] = "all",
    ) -> str:
        res = await call("explain_method", svc.explain_method, topic)
        return _dump(res)

    # ------------------------------------------------------------------ prompts & resources
    @mcp.prompt(name="what_changed", description="Answer 'what changed around <place> in <period>?' with the earthdiff tools: asks when ambiguous, reports every warning.")
    def what_changed_prompt(place_or_area: str, period: str = "") -> str:
        return what_changed(place_or_area, period)

    @mcp.resource("earthdiff://guide", name="guide", description="How to use earthdiff effectively and honestly.", mime_type="text/markdown")
    def guide_resource() -> str:
        return GUIDE

    return mcp


# ---------------------------------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="earthdiff-mcp",
        description="MCP server (stdio) that lets AI assistants answer 'what changed here?' with free Sentinel-2 imagery. No account, no API key. "
        "Settings can also be given as environment variables: EARTHDIFF_OUTPUT_DIR, EARTHDIFF_MAX_AREA_KM2, EARTHDIFF_MAX_RUNTIME_S, "
        "EARTHDIFF_CONTACT (an e-mail or URL added to the Nominatim User-Agent), EARTHDIFF_OPEN_REPORT.",
    )
    p.add_argument("--version", action="version", version=f"earthdiff-mcp {__version__}")
    p.add_argument("--output-dir", help="Where reports and images are written (env EARTHDIFF_OUTPUT_DIR, default ~/earthdiff-mcp-output).")
    p.add_argument("--max-area-km2", type=float, help="Largest area one analysis may cover (env EARTHDIFF_MAX_AREA_KM2; default and maximum 2000, the same limit as the CLI and the web UI).")
    p.add_argument("--max-runtime-s", type=int, help="Time limit of one analysis in seconds (env EARTHDIFF_MAX_RUNTIME_S, default 480).")
    p.add_argument("--open-report", action="store_true", help="Open report.html in the default browser after each analysis, for clients that do not show tool images (env EARTHDIFF_OPEN_REPORT=1).")
    p.add_argument("--check", action="store_true", help="Print the effective settings and dependency status as JSON, then exit.")
    return p


def settings_from_args(args: argparse.Namespace) -> Settings:
    import dataclasses
    from pathlib import Path

    s = Settings.from_env()
    upd: dict[str, Any] = {}
    if args.output_dir:
        upd["output_dir"] = Path(args.output_dir)
    if args.max_area_km2:
        upd["max_area_km2"] = min(max(args.max_area_km2, 1.0), HARD_MAX_AREA_KM2)
    if args.max_runtime_s:
        upd["max_runtime_s"] = min(max(args.max_runtime_s, 60), 1800)
    if args.open_report:
        upd["open_report"] = True
    return dataclasses.replace(s, **upd)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    # stdout belongs to the MCP protocol on stdio: log to stderr only
    logging.basicConfig(level=os.environ.get("EARTHDIFF_LOG_LEVEL", "WARNING").upper(), stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = settings_from_args(args)
    if args.check:
        import rasterio

        print(json.dumps({
            "earthdiff_mcp": __version__, "output_dir": str(settings.output_dir), "max_area_km2": settings.max_area_km2,
            "max_runtime_s": settings.max_runtime_s, "rasterio": rasterio.__version__, "transport": "stdio",
        }, indent=2))
        return
    server = create_server(settings)
    try:
        server.run("stdio")
    except KeyboardInterrupt:  # pragma: no cover
        pass


if __name__ == "__main__":  # pragma: no cover
    main()
