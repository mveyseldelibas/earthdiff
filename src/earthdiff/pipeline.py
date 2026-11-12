"""End-to-end orchestration: AOI + two periods -> report folder."""
from __future__ import annotations

import gc
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import rasterio
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from . import __version__
from .aoi import MAX_AREA_KM2, AOIError, aoi_mask, area_km2, choose_resolution, make_grid
from .change import CLASSES, detect_changes, thresholds_for
from .cause import label_causes
from .composite import BANDS, BANDS_WITH_B12, Composite, Reader, build_composite
from .seasonal import seasonal_check
from .periods import Period, season_gap_days
from .reader import cog_reader
from .render import change_map_image, class_rgba, montage, png_bytes, save_png, to_mercator, true_color, webp_bytes
from .report import data_url, render_report
from .stac import BACKENDS, STACError, search_scenes

Progress = Callable[[str], None]


class InsufficientDataError(RuntimeError):
    """Raised when too few cloud-free observations exist to make a meaningful comparison."""


@dataclass
class RunOptions:
    name: str = "Area of interest"
    backend: str = "earth-search"
    resolution: float | None = None  # metres; None = auto (10/20/30/60 m, <= 4 M pixels)
    max_scenes: int = 8
    max_cloud: float = 80.0
    sensitivity: str = "default"
    threshold_overrides: dict[str, float] = field(default_factory=dict)
    mmu_ha: float = 0.5
    max_area_km2: float = MAX_AREA_KM2
    warn_valid_fraction: float = 0.60
    min_valid_fraction: float = 0.20  # below this we refuse unless force=True
    force: bool = False
    allow_season_mismatch: bool = False  # run even when the windows are in different seasons (CLI --any-season)
    likely_cause: bool = True  # label vegetation-loss polygons fire-like / clearing-like / unclear (reads B12 as well)
    terrain: bool = True  # read a small Copernicus DEM window for the report's offline 3D view and relief basemap
    yearly_frames: bool = True  # windows years apart: one small timeline frame per in-between year (same season; for viewing only)
    seasonal_check: bool = False  # compare with the same windows of earlier years to flag recurring (harvest-like) loss
    seasonal_years: int = 2
    warn_bright_fraction: float = 0.20  # warn when this share of the comparable area is very bright (salt, sand, snow)


@dataclass
class RunResult:
    out_dir: Path
    summary: dict[str, Any]
    files: dict[str, Path]


MAX_WINDOW_DAYS = 366  # one time window may span at most a year: a longer window mixes seasons in one median (same limit in the CLI, the web UI and the MCP server)


SEASON_GAP_MAX = 60  # days apart in the yearly cycle above which a run is refused: crops, snow, sun angle and leaves differ too much between seasons


def check_windows(before: Period, after: Period, allow_season_mismatch: bool = False) -> None:
    for label, per in (("before", before), ("after", after)):
        if per.days() > MAX_WINDOW_DAYS:
            raise ValueError(f"the {label} window is {per.days()} days long; the limit is {MAX_WINDOW_DAYS} days. "
                             "Use a shorter window (one to three months of the same season works best); the two windows themselves may be years apart")
    gap = season_gap_days(before, after)
    if gap > SEASON_GAP_MAX and not allow_season_mismatch:
        raise ValueError(f"the before and after windows are {gap} days apart in the seasonal cycle (limit {SEASON_GAP_MAX}): winter and summer differ in snow, "
                         "leaves, crops and shadows, so almost everything would look changed. Compare the same months of different years "
                         "(e.g. August 2020 and August 2024); the CLI can override this with --any-season")


def plan(geom: BaseGeometry, before: Period, after: Period, opts: RunOptions, search=search_scenes) -> dict[str, Any]:
    """Cheap pre-flight (STAC search only, nothing downloaded)."""
    check_windows(before, after, opts.allow_season_mismatch)
    km2 = area_km2(geom)
    if km2 > opts.max_area_km2:
        raise AOIError(f"AOI is {km2:,.0f} km2; the limit is {opts.max_area_km2:,.0f} km2 (raise it with --max-area-km2 if you have the RAM)")
    res = opts.resolution or choose_resolution(geom)
    sb = search(opts.backend, geom, before, opts.max_cloud)
    sa = search(opts.backend, geom, after, opts.max_cloud)
    return {
        "aoi_km2": round(km2, 2),
        "resolution_m": res,
        "before_candidates": len(sb),
        "after_candidates": len(sa),
        "before_dates": sorted({s.date for s in sb}),
        "after_dates": sorted({s.date for s in sa}),
        "before_cloud": [round(s.cloud_cover, 1) for s in sb],
        "after_cloud": [round(s.cloud_cover, 1) for s in sa],
    }


def run_analysis(
    geom: BaseGeometry,
    before: Period,
    after: Period,
    out_dir: str | Path,
    opts: RunOptions | None = None,
    progress: Progress | None = None,
    reader: Reader = cog_reader,
    search=search_scenes,
    dem_reader=None,
) -> RunResult:
    opts = opts or RunOptions()
    # terrain is read with the real network reader only when the imagery is too (tests that inject a fake reader get none)
    if dem_reader is None and reader is cog_reader and opts.terrain:
        from .terrain import read_dem_window

        dem_reader = read_dem_window
    say = progress or (lambda m: None)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if opts.backend not in BACKENDS:
        raise STACError(f"unknown backend {opts.backend!r}")

    check_windows(before, after, opts.allow_season_mismatch)
    km2 = area_km2(geom)
    if km2 > opts.max_area_km2:
        raise AOIError(f"AOI is {km2:,.0f} km2; the limit is {opts.max_area_km2:,.0f} km2 (raise it with --max-area-km2 if you have the RAM)")
    res = opts.resolution or choose_resolution(geom)
    grid = make_grid(geom, res)
    mask = aoi_mask(geom, grid)
    say(f"AOI {km2:,.1f} km2 -> grid {grid.width} x {grid.height} px at {res:g} m ({grid.epsg})")

    say(f"searching Sentinel-2 L2A scenes ({opts.backend}) ...")
    cand_b = search(opts.backend, geom, before, opts.max_cloud)
    cand_a = search(opts.backend, geom, after, opts.max_cloud)
    if not cand_b or not cand_a:
        missing = "before" if not cand_b else "after"
        raise InsufficientDataError(
            f"no Sentinel-2 scenes with < {opts.max_cloud:g}% cloud found for the {missing} period. "
            "Use a longer window, a different month, or raise --max-cloud."
        )
    say(f"found {len(cand_b)} candidate scenes (before) and {len(cand_a)} (after)")

    bands = BANDS_WITH_B12 if opts.likely_cause else BANDS
    comp_b = build_composite(cand_b, grid, mask, opts.max_scenes, reader=reader, progress=say, label="before", bands=bands, frame_max_dim=FRAME_MAX_DIM)
    comp_a = build_composite(cand_a, grid, mask, opts.max_scenes, reader=reader, progress=say, label="after", bands=bands, frame_max_dim=FRAME_MAX_DIM)
    between = {"frames": [], "series": []}
    if (opts.yearly_frames or opts.likely_cause) and comp_a.frames:
        from .yearly import between_frames

        between = between_frames(geom, grid, mask, before, after, opts.backend, opts.max_cloud, comp_a.frame_step, search, reader, say,
                                 with_nbr=opts.likely_cause)
    n_aoi = max(1, int(mask.sum()))
    valid_fraction = float((mask & (comp_b.count > 0) & (comp_a.count > 0)).sum()) / n_aoi
    if valid_fraction < opts.min_valid_fraction and not opts.force:
        raise InsufficientDataError(
            f"only {valid_fraction * 100:.0f}% of the AOI is cloud-free in both periods (minimum {opts.min_valid_fraction * 100:.0f}%). "
            "Widen the date windows, use other months, or pass --force to run anyway."
        )

    say("detecting changes ...")
    thr = thresholds_for(opts.sensitivity, **opts.threshold_overrides)
    result = detect_changes(comp_b, comp_a, grid, mask, thr, opts.mmu_ha)

    extra: dict[str, Any] = {"cause": None, "seasonal": None}
    cause_note = None
    if opts.likely_cause:
        if "swir22" in comp_b.bands and "swir22" in comp_a.bands:
            extra["cause"] = label_causes(result, comp_b.bands, comp_a.bands)
            if extra["cause"] is not None and between["series"]:
                # windows years apart: judge each loss on its largest one-year step, so an old, partly regrown burn is still recognised
                from .history import composite_point, refine_with_history

                k = comp_a.frame_step
                ser = [composite_point("before", before.label(), comp_b, k)] + between["series"] + [composite_point("after", after.label(), comp_a, k)]
                refined = refine_with_history(result, ser, k)
                if refined:
                    extra["cause"] = refined
        else:
            cause_note = "likely_cause is unavailable: Sentinel-2 band B12 (SWIR2) could not be read for these scenes, so vegetation loss is not labelled fire-like / clearing-like."
    if opts.seasonal_check:
        say("seasonal check: comparing with earlier years ...")
        extra["seasonal"] = seasonal_check(
            result, grid, before, after, geom, opts.backend, opts.max_cloud, search, reader, years=opts.seasonal_years, progress=say
        )
    extra["bright_surface"] = {
        "fraction_of_comparable_area": round(result.bright_fraction, 4),
        "fraction_in_both_periods": round(result.bright_both_fraction, 4),
        "warn_threshold": opts.warn_bright_fraction,
        "reliable": result.bright_fraction < opts.warn_bright_fraction,
        "rule": "mean(blue, green, red) >= 0.25 and NDVI < 0.15 in the before or after composite (salt, sand, snow, bare limestone, concrete)",
    }
    if result.bright_ndwi:
        extra["bright_surface"]["water_like_share_of_bright_pixels"] = {
            "index": "NDWI = (green - NIR) / (green + NIR) > 0 (exploratory; the default MNDWI rule scores dry salt as water too)",
            "before": result.bright_ndwi["before"], "after": result.bright_ndwi["after"], "pixels": result.bright_ndwi["pixels"],
        }

    warnings = _warnings(opts, before, after, comp_b, comp_a, valid_fraction, result.totals, km2, result, extra, cause_note)

    say("rendering outputs ...")
    files = _write_outputs(out, geom, grid, mask, comp_b, comp_a, result, thr, opts, before, after, valid_fraction, warnings, km2, extra,
                           dem_reader=dem_reader if opts.terrain else None, say=say, between=between["frames"] if opts.yearly_frames else [])
    summary = json.loads(files["summary"].read_text(encoding="utf-8"))
    del comp_b, comp_a, result
    gc.collect()
    say(f"done -> {files['report']}")
    return RunResult(out, summary, files)


# A changed share above LARGE_CHANGE_SHARE of the AOI usually means a seasonal/atmospheric mismatch, unless most of the change
# is fire-like vegetation loss: dNBR is an independent burn signal, so a large share then fits one big burn (e.g. inside a fire scar).
LARGE_CHANGE_SHARE = 0.30
MIN_FRAME_CLEAR = 0.3  # timeline frames must show at least this share of the area (cloudier passes are left out)
FRAME_MAX_DIM = 900  # longest side (pixels) of the per-scene frames played in the report timeline
FIRE_EXPLAINS_SHARE = 0.80


def fire_like_share(totals: dict[str, Any], cause: dict[str, Any] | None) -> float:
    """Share of the total changed area that ``likely_cause`` labels fire-like (0 when the cause labels are missing)."""
    changed = sum(float(t.get("area_ha", 0.0)) for t in (totals or {}).values())
    fire = float((((cause or {}).get("totals") or {}).get("fire-like") or {}).get("area_ha", 0.0))
    return fire / changed if changed > 0 else 0.0


def _warnings(opts, before, after, cb: Composite, ca: Composite, valid_fraction, totals, km2, result=None, extra=None, cause_note=None) -> list[str]:
    w: list[str] = period_warnings(before, after)
    for label, c in (("before", cb), ("after", ca)):
        if len(c.scenes) == 1:
            w.append(f"Only one usable scene in the {label} period: the composite is a single acquisition, so residual cloud/haze or one-off conditions are not averaged out.")
        elif not c.scenes:
            w.append(f"No usable scene in the {label} period.")
    if valid_fraction < opts.warn_valid_fraction:
        w.append(f"Only {valid_fraction * 100:.0f}% of the AOI is cloud-free in both periods; the remainder is excluded and not reported as 'no change'.")
    changed_ha = sum(t["area_ha"] for t in totals.values())
    if km2 > 0 and changed_ha / (km2 * 100.0) > LARGE_CHANGE_SHARE:
        fire = fire_like_share(totals, (extra or {}).get("cause"))
        if fire >= FIRE_EXPLAINS_SHARE:
            w.append(
                f"Changes cover {changed_ha / (km2 * 100.0) * 100:.0f}% of the AOI, and {fire * 100:.0f}% of the changed area is fire-like vegetation loss "
                "(dNBR, an independent burn signal): this is consistent with one large burn rather than a seasonal or atmospheric mismatch. "
                "Inspect the before/after images to confirm."
            )
        else:
            w.append(
                f"Changes cover {changed_ha / (km2 * 100.0) * 100:.0f}% of the AOI: such a large share often means a seasonal, atmospheric or "
                "image-registration mismatch rather than real land-cover change. Inspect the before/after images."
            )
    if result is not None and result.bright_fraction >= opts.warn_bright_fraction:
        w.append(
            f"{result.bright_fraction * 100:.0f}% of the comparable area is very bright, non-vegetated surface (salt, sand, snow or similar; {result.bright_both_fraction * 100:.0f}% in both periods). "
            "Change detection is NOT reliable on such surfaces: do not read the absence of change there as 'no change'. Confidence of polygons on bright pixels is lowered."
        )
    wl = ((extra or {}).get("bright_surface") or {}).get("water_like_share_of_bright_pixels")
    if result is not None and wl and result.bright_fraction >= opts.warn_bright_fraction and abs(wl["after"] - wl["before"]) >= 0.25:
        w.append(
            f"Exploratory signal on the bright surface: the share of water-like pixels (NDWI > 0) changed from {wl['before'] * 100:.0f}% to {wl['after'] * 100:.0f}%, "
            "which suggests a bright surface (a salt flat or shallow lake) became " + ("drier" if wl["after"] < wl["before"] else "wetter")
            + ". The default MNDWI rule cannot see this because dry salt also scores as water in MNDWI. Not a mapped change: verify with other data."
        )
    if cause_note:
        w.append(cause_note)
    sea = (extra or {}).get("seasonal")
    if sea and sea.get("checked_polygons", 0) == 0 and sea.get("enabled"):
        w.append("The seasonal check found no usable scenes in earlier years (windows before 2017 have no data), so no polygon was checked.")
    elif sea:
        ls = sea["likely_seasonal"]
        if ls["polygons"] and ls["share_of_vegetation_loss"] >= 0.10:
            w.append(
                f"{ls['area_ha']:,.1f} ha ({ls['share_of_vegetation_loss'] * 100:.0f}% of the vegetation loss, {ls['polygons']} polygons) repeats at the same time of year in earlier years: "
                "likely_seasonal (harvest or a seasonal cycle rather than a one-off loss). The polygons are kept and flagged."
            )
    return w



def timeline_frames(grid, mask: np.ndarray, periods) -> dict[str, Any]:
    """One small true-colour image per scene, in date order, for the report timeline.

    Only what the pass really saw is shown: cloud, cloud shadow, snow and missing pixels are left transparent (they are not filled
    from other dates, which in winter made dark, misleading frames); ``clear_fraction`` says how much of the area is visible."""
    from affine import Affine

    from .aoi import Grid

    frames: list[dict[str, Any]] = []
    bounds = None
    for label, comp in periods:
        if not comp.frames or not comp.scenes:
            continue
        k = comp.frame_step
        sub_mask = mask[::k, ::k]
        hh, ww = sub_mask.shape
        g = Grid(grid.epsg, grid.transform * Affine.scale(k), ww, hh, grid.res * k)
        for i, sc in enumerate(comp.scenes):
            bands = {}
            own = np.ones((hh, ww), dtype=bool)
            for b in ("red", "green", "blue"):
                arr = comp.frames[b][i].astype(np.float32)
                own &= np.isfinite(arr)
                bands[b] = arr  # NaN (not seen clear) stays transparent
            rgba = true_color(bands, sub_mask)
            img, bounds = to_mercator(rgba, g, max_dim=FRAME_MAX_DIM)
            n = max(1, int(sub_mask.sum()))
            frames.append({
                "period": label, "date": sc.date[:10], "scene": sc.id,
                "clear_fraction": round(float((own & sub_mask).sum()) / n, 3),
                "img": data_url(webp_bytes(img, quality=80), "image/webp"),
            })
    # a pass that saw almost nothing (cloud) would show an empty frame: keep the ones showing at least MIN_FRAME_CLEAR of the area,
    # and at least the clearest one of each period
    keep = [f for f in frames if f["clear_fraction"] >= MIN_FRAME_CLEAR]
    for label in {f["period"] for f in frames}:
        if not any(f["period"] == label for f in keep):
            keep.append(max((f for f in frames if f["period"] == label), key=lambda f: f["clear_fraction"]))
    keep.sort(key=lambda f: f["date"])
    return {"frames": keep, "bounds": bounds}


def period_warnings(before: Period, after: Period) -> list[str]:
    """Warnings that depend on the two time windows only (shared by ``run_analysis`` and the MCP ``plan_change``)."""
    w: list[str] = []
    gap = season_gap_days(before, after)
    if gap > 45:
        w.append(
            f"The two windows are {gap} days apart in the seasonal cycle. Vegetation and water change naturally with the season: "
            "compare the same time of year in different years to avoid false alarms."
        )
    if min(before.days(), after.days()) < 14:
        w.append("A window shorter than two weeks usually contains very few cloud-free scenes; consider a longer window.")
    return w


def _years(scenes_b, scenes_a) -> str:
    years = sorted({s.datetime.year for s in list(scenes_b) + list(scenes_a)})
    if not years:
        return ""
    return ", ".join(str(y) for y in years)


def _write_outputs(out, geom, grid, mask, cb, ca, result, thr, opts, before, after, valid_fraction, warnings, km2, extra=None, dem_reader=None, say=None, between=None):
    extra = extra or {}
    files: dict[str, Path] = {}
    fc = {
        "type": "FeatureCollection",
        "name": "earthdiff_changes",
        "features": result.features,
        "earthdiff": {
            "version": __version__,
            "before": before.label(),
            "after": after.label(),
            "note": "Heuristic spectral-index change polygons. Areas in hectares (UTM pixel counts). Screening product - verify before acting.",
        },
    }
    files["geojson"] = out / "changes.geojson"
    files["geojson"].write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")

    files["class_tif"] = out / "change_class.tif"
    with rasterio.open(
        files["class_tif"], "w", driver="GTiff", height=grid.height, width=grid.width, count=1, dtype="uint8",
        crs=grid.crs, transform=grid.transform, nodata=0, compress="deflate",
    ) as dst:
        dst.write(result.classmap, 1)
        dst.update_tags(classes=json.dumps({str(c.id): c.key for c in CLASSES.values()}))

    rgb_b = true_color(cb.bands, mask)
    rgb_a = true_color(ca.bands, mask)
    cls_rgba = class_rgba(result.classmap)

    big = 2400
    img_b, bounds = to_mercator(rgb_b, grid, max_dim=big)
    img_a, _ = to_mercator(rgb_a, grid, max_dim=big)
    img_c, _ = to_mercator(cls_rgba, grid, categorical=True, max_dim=big)
    files["before"], files["after"] = out / "before.png", out / "after.png"
    save_png(img_b, files["before"])
    save_png(img_a, files["after"])

    totals = result.totals
    cm = change_map_image(rgb_a, result.classmap, totals)
    files["change_map"] = out / "change_map.png"
    cm.save(files["change_map"], optimize=True)
    files["overview"] = out / "overview.png"
    montage(rgb_b, rgb_a, cm, (f"Before  {before.label()}", f"After  {after.label()}", "Detected changes")).save(files["overview"], optimize=True)

    web = 1600
    wb, _ = to_mercator(rgb_b, grid, max_dim=web)
    wa, _ = to_mercator(rgb_a, grid, max_dim=web)
    wc, _ = to_mercator(cls_rgba, grid, categorical=True, max_dim=web)
    images = {
        "before": data_url(webp_bytes(wb), "image/webp"),
        "after": data_url(webp_bytes(wa), "image/webp"),
        "changes": data_url(png_bytes(wc), "image/png"),
    }
    timeline = timeline_frames(grid, mask, (("before", cb), ("after", ca)))
    if between:
        timeline["frames"] = sorted(timeline["frames"] + list(between), key=lambda f: f["date"])
    terrain = None
    if dem_reader is not None:
        from .terrain import build_terrain

        (say or (lambda m: None))("reading terrain (Copernicus DEM GLO-30) for the 3D view ...")
        try:
            terrain = build_terrain(bounds, dem_reader)
        except Exception:  # pragma: no cover - the 3D view is optional; never fail a run for it
            terrain = None

    scenes_meta = []
    for period, comp, label in ((before, cb, "before"), (after, ca, "after")):
        for s in comp.scenes:
            scenes_meta.append(
                {"period": f"{label} ({period.label()})", "id": s.id, "date": s.date, "cloud_cover": s.cloud_cover,
                 "valid_fraction": comp.scene_valid_fraction.get(s.id, 0.0)}
            )
    be = BACKENDS[opts.backend]
    years = _years(cb.scenes, ca.scenes)
    attribution = f"Contains modified Copernicus Sentinel data {years}".strip()
    meta = {
        "name": opts.name,
        "version": __version__,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "before_label": before.label(),
        "after_label": after.label(),
        "aoi_km2": round(km2, 2),
        "resolution_m": grid.res,
        "valid_fraction": round(valid_fraction, 4),
        "scenes_before": len(cb.scenes),
        "scenes_after": len(ca.scenes),
        "mmu_ha": opts.mmu_ha,
        "sensitivity": opts.sensitivity,
        "backend": opts.backend,
        "backend_attribution": be.attribution,
    }
    from .xlsx import report_workbook

    files["xlsx"] = out / "changes.xlsx"
    files["xlsx"].write_bytes(report_workbook("en", meta, totals, warnings, fc, scenes_meta, extra, attribution, (terrain or {}).get("attribution")))
    html_text = render_report(
        title=f"earthdiff - {opts.name}",
        meta=meta,
        totals=totals,
        warnings=warnings,
        geojson=fc,
        aoi_geojson={"type": "Feature", "properties": {}, "geometry": mapping(geom)},
        bounds=bounds,
        images=images,
        scenes=scenes_meta,
        thresholds=thr.as_dict(),
        attribution=attribution,
        extras=extra,
        timeline=timeline,
        terrain=terrain,
    )
    files["report"] = out / "report.html"
    files["report"].write_text(html_text, encoding="utf-8")

    summary = {
        **meta,
        "before_period": [before.start.isoformat(), before.end.isoformat()],
        "after_period": [after.start.isoformat(), after.end.isoformat()],
        "totals": totals,
        "changed_fraction_of_aoi": round(sum(t["area_ha"] for t in totals.values()) / max(km2 * 100.0, 1e-9), 4),
        "warnings": warnings,
        "likely_cause": extra.get("cause"),
        "seasonal_check": extra.get("seasonal"),
        "bright_surface": extra.get("bright_surface"),
        "scenes": scenes_meta,
        "thresholds": thr.as_dict(),
        "attribution": attribution,
        "files": {k: v.name for k, v in files.items()},
    }
    files["summary"] = out / "summary.json"
    files["summary"].write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return files
