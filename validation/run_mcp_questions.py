"""Ask the real earthdiff MCP server (stdio, real STAC / Sentinel-2 / Nominatim) the questions in validation/mcp_cases.json,
the way an assistant would: find_place -> plan_change -> detect_change. Saves the raw tool outputs and the returned images.

    python validation/run_mcp_questions.py [case ...]

Outputs: validation/mcp/<case>/ (JSON of every tool answer, before/after/change images as returned to the client, timings);
the full report folders go to validation/mcp_runs/ (git-ignored). Needs internet and  pip install -e ".[mcp]".
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent
CASES = json.loads((ROOT / "mcp_cases.json").read_text(encoding="utf-8"))


def text_of(res) -> str:
    return "".join(c.text for c in res.content if c.type == "text")


def repo_relative(obj):
    """Rewrite absolute paths inside the repository as repo-relative POSIX paths, so saved answers do not carry a local folder."""
    if isinstance(obj, dict):
        return {k: repo_relative(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [repo_relative(v) for v in obj]
    if isinstance(obj, str):
        try:
            return Path(obj).resolve().relative_to(ROOT.parent).as_posix() if Path(obj).is_absolute() else obj
        except ValueError:
            return obj
    return obj


async def ask(case_id: str, case: dict) -> dict:
    out = ROOT / "mcp" / case_id
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "EARTHDIFF_OUTPUT_DIR": str(ROOT / "mcp_runs"), "PYTHONIOENCODING": "utf-8"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "earthdiff.mcp_server"], env=env)
    timings: dict[str, float] = {}
    progress: list[tuple[float, float, str]] = []
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()

            choice = case["area_choice"]
            if "bbox" in choice:  # the scripted user states the box (a small public area), no place search needed
                bbox = choice["bbox"]
            else:
                t = time.time()
                res = await s.call_tool("find_place", {"query": case["place_query"], **case.get("place_args", {})})
                timings["find_place_s"] = round(time.time() - t, 1)
                places = json.loads(text_of(res))
                (out / "find_place.json").write_text(json.dumps(places, indent=1, ensure_ascii=False), encoding="utf-8")
                cands = [c for c in places["candidates"] if choice["candidate_name"].casefold() in c["name"].casefold() and c["rank"] == choice.get("rank", c["rank"])]
                cand = cands[0]
                if choice["option"] == "centered_bbox":
                    bbox = cand["narrowing_options"]["centered_bbox"]["bbox_west_south_east_north"]
                else:
                    bbox = cand["bbox_west_south_east_north"]

            args = {"bbox": bbox, "after": case["after"], "before": case["before"], "resolution_m": case["resolution_m"]}
            if case.get("seasonal_check"):
                args["seasonal_check"] = True
            t = time.time()
            res = await s.call_tool("plan_change", args)
            timings["plan_change_s"] = round(time.time() - t, 1)
            (out / "plan_change.json").write_text(json.dumps(json.loads(text_of(res)), indent=1, ensure_ascii=False), encoding="utf-8")

            async def on_progress(p, total, message):
                progress.append((round(time.time() - t0, 1), p, message))

            t0 = time.time()
            res = await s.call_tool("detect_change", {**args, "name": case["name"]}, progress_callback=on_progress)
            timings["detect_change_s"] = round(time.time() - t0, 1)
            if res.is_error if hasattr(res, "is_error") else res.isError:
                (out / "detect_change.error.txt").write_text(text_of(res), encoding="utf-8")
                return {"case": case_id, "error": text_of(res), "timings": timings}
            names = ["before", "after", "change_map"]
            sizes = {}
            for name, c in zip(names, [c for c in res.content if c.type == "image"]):
                data = base64.b64decode(c.data)
                (out / f"{name}.jpg").write_bytes(data)
                sizes[name] = len(data)
            result = repo_relative(json.loads(text_of(res)))
            (out / "detect_change.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
            (out / "progress.log").write_text("\n".join(f"{t:6.1f}s {p:6.2f}  {m}" for t, p, m in progress), encoding="utf-8")
            (out / "timings.json").write_text(json.dumps({**timings, "image_bytes": sizes, "progress_events": len(progress)}, indent=1), encoding="utf-8")
            return {"case": case_id, "bbox": bbox, "timings": timings, "result": result, "image_bytes": sizes, "progress_events": len(progress)}


def main() -> None:
    wanted = sys.argv[1:] or list(CASES)
    for cid in wanted:
        print(f"=== {cid}: {CASES[cid]['question']}", flush=True)
        r = anyio.run(ask, cid, CASES[cid])
        if "error" in r:
            print("  ERROR:", r["error"])
            continue
        res = r["result"]
        print(f"  bbox {r['bbox']}  timings {r['timings']}  progress events {r['progress_events']}  images {r['image_bytes']}")
        print(f"  area {res['area_km2']} km2 @ {res['resolution_m']} m, comparable {res['comparable_fraction'] * 100:.0f}%, scenes {res['scenes']['before']}/{res['scenes']['after']}, quality {res['data_quality']['level']}")
        for c in res["changes"]:
            print(f"    {c['label']:<26}{c['area_ha']:>10,.1f} ha  ({c['polygons']} polygons)")
        print("  confidence:", res["confidence"]["level"], res["confidence"]["area_weighted_mean"])
        lc = res.get("vegetation_loss_likely_cause")
        if lc:
            print("  likely cause (ha):", {k: v["area_ha"] for k, v in lc["totals"].items()})
        sc = res.get("seasonal_check")
        if sc:
            print("  seasonal:", sc["likely_seasonal"], "extra composites", sc["extra_composites"], "elapsed", sc["elapsed_s"], "s")
        bs = res.get("bright_surface")
        if bs:
            print("  bright surface:", bs["fraction_of_comparable_area"], "reliable", bs["reliable"])
        for w in res["warnings"]:
            print("  !", w)


if __name__ == "__main__":
    main()
