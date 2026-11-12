"""Periodic re-runs driven by a TOML config. earthdiff never schedules itself: run `earthdiff watch`
from cron / Task Scheduler / a CI job once a month."""
from __future__ import annotations

import csv
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from .aoi import AOIError, bbox_geometry, load_aoi
from .change import CLASSES
from .periods import Period, previous_month
from .aoi import MAX_AREA_KM2
from .pipeline import InsufficientDataError, RunOptions, run_analysis

EXAMPLE = """\
# earthdiff watch config. Run monthly:  earthdiff watch watch.toml
[defaults]
out_dir = "watch-output"      # relative to this file
backend = "earth-search"
sensitivity = "default"       # strict | default | sensitive
mmu_ha = 0.5

[[area]]
name = "my-forest"
aoi = "aoi.geojson"           # or: bbox = [west, south, east, north]
compare = "previous_year"     # after = last full month; before = same month one year earlier
                              # (use "previous_month" to compare with the month before)
"""

HISTORY_FIELDS = ["period", "run_date", "valid_fraction", "scenes_before", "scenes_after"] + [f"{c.key}_ha" for c in CLASSES.values()]


@dataclass
class AreaOutcome:
    name: str
    status: str  # done | skipped | failed
    message: str
    out_dir: Path | None = None
    delta: dict[str, float] | None = None


def load_config(path: str | Path) -> dict[str, Any]:
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    if not cfg.get("area"):
        raise ValueError("config needs at least one [[area]] table")
    return cfg


def _periods(compare: str, today: date) -> tuple[Period, Period]:
    after = previous_month(today)
    if compare == "previous_year":
        return after.shift_years(-1), after
    if compare == "previous_month":
        return previous_month(after.start), after
    raise ValueError(f"compare must be 'previous_year' or 'previous_month', got {compare!r}")


def _read_history(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _append_history(path: Path, row: dict[str, Any]) -> None:
    exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in HISTORY_FIELDS})


def run_watch(
    config_path: str | Path,
    today: date | None = None,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
    runner=run_analysis,
) -> list[AreaOutcome]:
    cfg_path = Path(config_path).resolve()
    base = cfg_path.parent
    cfg = load_config(cfg_path)
    d = cfg.get("defaults", {})
    today = today or date.today()
    out_root = (base / d.get("out_dir", "watch-output")).resolve()
    say = progress or (lambda m: None)
    outcomes: list[AreaOutcome] = []

    for area in cfg["area"]:
        name = area.get("name") or "area"
        try:
            if "aoi" in area:
                geom = load_aoi((base / area["aoi"]).resolve())
            elif "bbox" in area:
                geom = bbox_geometry(*[float(x) for x in area["bbox"]])
            else:
                raise AOIError("area needs 'aoi' (GeoJSON path) or 'bbox'")
            before, after = _periods(area.get("compare", d.get("compare", "previous_year")), today)
            tag = f"{after.start.year}-{after.start.month:02d}"
            area_dir = out_root / name
            run_dir = area_dir / tag
            if (run_dir / "summary.json").exists() and not force:
                outcomes.append(AreaOutcome(name, "skipped", f"{tag} already done", run_dir))
                continue
            opts = RunOptions(
                name=name,
                backend=area.get("backend", d.get("backend", "earth-search")),
                sensitivity=area.get("sensitivity", d.get("sensitivity", "default")),
                mmu_ha=float(area.get("mmu_ha", d.get("mmu_ha", 0.5))),
                max_area_km2=float(area.get("max_area_km2", d.get("max_area_km2", MAX_AREA_KM2))),
            )
            say(f"[{name}] {before.label()} -> {after.label()}")
            res = runner(geom, before, after, run_dir, opts, progress=say)
            s = res.summary
            hist_path = area_dir / "history.csv"
            prev = _read_history(hist_path)
            row = {
                "period": tag,
                "run_date": today.isoformat(),
                "valid_fraction": s["valid_fraction"],
                "scenes_before": s["scenes_before"],
                "scenes_after": s["scenes_after"],
                **{f"{k}_ha": v["area_ha"] for k, v in s["totals"].items()},
            }
            _append_history(hist_path, row)
            delta = None
            if prev:
                last = prev[-1]
                delta = {k: round(float(row[f"{k}_ha"]) - float(last.get(f"{k}_ha") or 0.0), 2) for k in s["totals"]}
            outcomes.append(AreaOutcome(name, "done", f"{tag} done", run_dir, delta))
        except (AOIError, InsufficientDataError, ValueError, RuntimeError) as exc:
            outcomes.append(AreaOutcome(name, "failed", str(exc)))
    return outcomes
