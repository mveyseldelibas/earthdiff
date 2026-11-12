"""Command line interface: run | plan | watch | ui | mcp."""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .aoi import AOIError, area_km2, load_aoi, parse_bbox
from .aoi import MAX_AREA_KM2
from .change import SENSITIVITY
from .periods import parse_period
from .pipeline import InsufficientDataError, RunOptions, plan, run_analysis
from .stac import BACKENDS, STACError


def _say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def _add_common(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("area of interest")
    g.add_argument("--aoi", help="GeoJSON file (Polygon/MultiPolygon, Feature or FeatureCollection; WGS84)")
    g.add_argument("--bbox", help="west,south,east,north in degrees (alternative to --aoi)")
    g = p.add_argument_group("time windows")
    g.add_argument("--after", required=True, help="YYYY-MM | YYYY-MM-DD | START/END   (the 'now' window)")
    g.add_argument("--before", help="baseline window; default = the --after window one year earlier (same season)")
    g = p.add_argument_group("processing")
    g.add_argument("--backend", choices=list(BACKENDS), default="earth-search", help="free public STAC API (default: earth-search)")
    g.add_argument("--resolution", type=float, help="analysis pixel size in metres (default: auto 10/20/30/60)")
    g.add_argument("--max-scenes", type=int, default=8, help="max scenes per composite (default 8)")
    g.add_argument("--max-cloud", type=float, default=80.0, help="discard scenes with more scene-level cloud %% (default 80)")
    g.add_argument("--sensitivity", choices=list(SENSITIVITY), default="default")
    g.add_argument("--mmu-ha", type=float, default=0.5, help="minimum mapping unit in hectares (default 0.5)")
    g.add_argument("--max-area-km2", type=float, default=MAX_AREA_KM2, help=f"AOI size limit (default {MAX_AREA_KM2:,.0f} km2, the same limit as the web UI and the MCP server)")
    g.add_argument("--no-likely-cause", action="store_true", help="do not read B12 / label vegetation loss as fire-like, clearing-like or unclear (dNBR)")
    g.add_argument("--any-season", action="store_true", help="run even when the two windows are in different seasons (refused by default: almost everything would look changed)")
    g.add_argument("--no-yearly-frames", action="store_true", help="windows years apart: do not add one small timeline frame per in-between year")
    g.add_argument("--no-terrain", action="store_true", help="do not read the Copernicus DEM for the report's offline 3D view and relief basemap")
    g.add_argument("--seasonal-check", action="store_true", help="compare vegetation loss with the same windows of earlier years and flag recurring (harvest-like) loss; adds a few small downloads")
    g.add_argument("--seasonal-years", type=int, default=2, choices=[1, 2, 3], help="how many earlier years the seasonal check looks at (default 2)")


def _geometry(args):
    if bool(args.aoi) == bool(args.bbox):
        raise AOIError("give exactly one of --aoi or --bbox")
    return load_aoi(args.aoi) if args.aoi else parse_bbox(args.bbox)


def _options(args, name: str) -> RunOptions:
    return RunOptions(
        name=name,
        backend=args.backend,
        resolution=args.resolution,
        max_scenes=args.max_scenes,
        max_cloud=args.max_cloud,
        sensitivity=args.sensitivity,
        mmu_ha=args.mmu_ha,
        max_area_km2=args.max_area_km2,
        force=getattr(args, "force", False),
        likely_cause=not args.no_likely_cause,
        terrain=not args.no_terrain,
        allow_season_mismatch=args.any_season,
        yearly_frames=not args.no_yearly_frames,
        seasonal_check=args.seasonal_check,
        seasonal_years=args.seasonal_years,
    )


def _periods(args):
    after = parse_period(args.after)
    before = parse_period(args.before) if args.before else after.shift_years(-1)
    return before, after


def cmd_run(args) -> int:
    geom = _geometry(args)
    before, after = _periods(args)
    name = args.name or (Path(args.aoi).stem if args.aoi else "bbox area")
    print(f"earthdiff {__version__}: {name} | before {before.label()} -> after {after.label()}")
    res = run_analysis(geom, before, after, args.out, _options(args, name), progress=_say)
    print_summary(res.summary)
    print(f"\nOpen: {res.files['report']}")
    return 0


def cmd_plan(args) -> int:
    geom = _geometry(args)
    before, after = _periods(args)
    info = plan(geom, before, after, _options(args, "plan"))
    print(f"AOI {info['aoi_km2']} km2, analysis resolution {info['resolution_m']:g} m")
    print(f"before {before.label()}: {info['before_candidates']} candidate scenes  {', '.join(info['before_dates'][:12])}")
    print(f"after  {after.label()}: {info['after_candidates']} candidate scenes  {', '.join(info['after_dates'][:12])}")
    return 0


def print_summary(s: dict) -> None:
    print("\nResult")
    print(f"  comparable (cloud-free in both periods): {s['valid_fraction'] * 100:.0f}% of the AOI")
    for t in s["totals"].values():
        print(f"  {t['label']:<26} {t['area_ha']:>10,.1f} ha   ({t['polygons']} polygons)")
    lc = s.get("likely_cause")
    if lc:
        t = lc["totals"]
        print("  vegetation loss, likely cause (a hint from dNBR, brightness and shape, not a verdict): "
              + ", ".join(f"{k} {t[k]['area_ha']:,.1f} ha" for k in ("fire-like", "clearing-like", "unclear")))
    sc = s.get("seasonal_check")
    if sc and sc.get("enabled"):
        ls = sc["likely_seasonal"]
        print(f"  seasonal check ({sc['years']} earlier year(s), {sc['extra_composites']} extra composites, {sc.get('elapsed_s', 0)} s): likely seasonal {ls['area_ha']:,.1f} ha in {ls['polygons']} polygons")
    for w in s["warnings"]:
        print(f"  ! {w}")
    print(f"  {s['attribution']}")


def cmd_watch(args) -> int:
    from .watch import EXAMPLE, run_watch

    if args.init:
        p = Path(args.config)
        if p.exists():
            print(f"{p} already exists; not overwriting", file=sys.stderr)
            return 2
        p.write_text(EXAMPLE, encoding="utf-8")
        print(f"wrote example config to {p}")
        return 0
    today = date.fromisoformat(args.today) if args.today else None
    outcomes = run_watch(args.config, today=today, force=args.force, progress=_say)
    failed = 0
    for o in outcomes:
        extra = ""
        if o.delta:
            extra = "  (vs previous run: " + ", ".join(f"{k} {v:+.1f} ha" for k, v in o.delta.items() if v) + ")"
        print(f"[{o.status}] {o.name}: {o.message}{extra}")
        failed += o.status == "failed"
    return 3 if failed else 0


def cmd_ui(args) -> int:
    from .ui import serve

    serve(port=args.port, workdir=Path(args.workdir), open_browser=not args.no_browser)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="earthdiff", description="What changed here? Explainable change reports from free Sentinel-2 imagery.")
    p.add_argument("--version", action="version", version=f"earthdiff {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="analyse one area and write a report folder")
    _add_common(r)
    r.add_argument("--out", default="report", help="output folder (default ./report)")
    r.add_argument("--name", help="title shown in the report")
    r.add_argument("--force", action="store_true", help="run even when the cloud-free fraction is very low")
    r.set_defaults(fn=cmd_run)

    pl = sub.add_parser("plan", help="dry run: only search scenes, download nothing")
    _add_common(pl)
    pl.set_defaults(fn=cmd_plan)

    w = sub.add_parser("watch", help="re-run the areas of a TOML config for the last full month (schedule it yourself)")
    w.add_argument("config", help="path of the TOML file")
    w.add_argument("--init", action="store_true", help="write an example config to the given path and exit")
    w.add_argument("--today", help="pretend today is YYYY-MM-DD (testing/backfill)")
    w.add_argument("--force", action="store_true", help="redo periods that already have results")
    w.set_defaults(fn=cmd_watch)

    sub.add_parser("mcp", help="MCP server (stdio) for AI assistants; same as `earthdiff-mcp` (needs: pip install \"earthdiff[mcp]\")")

    u = sub.add_parser("ui", help="local web UI (127.0.0.1 only): draw an area, pick dates, run")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--workdir", default="earthdiff-runs", help="where UI runs are stored")
    u.add_argument("--no-browser", action="store_true")
    u.set_defaults(fn=cmd_ui)
    return p


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    if raw and raw[0] == "mcp":  # `earthdiff mcp [options]` is the same server as `earthdiff-mcp`
        from .mcp_server import main as mcp_main

        mcp_main(raw[1:])
        return 0
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (AOIError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (InsufficientDataError, STACError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
