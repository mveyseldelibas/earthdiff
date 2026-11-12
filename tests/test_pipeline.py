"""End-to-end test with a fake reader and fake STAC search (no network)."""
import json
from datetime import datetime, timezone

import numpy as np
import pytest
from shapely.geometry import box

from earthdiff.composite import BANDS
from earthdiff.periods import parse_period
from earthdiff.pipeline import InsufficientDataError, RunOptions, run_analysis
from earthdiff.stac import Scene


def make_scene(tag, year):
    return Scene(
        id=f"S2A_36SUF_{year}0701_{tag}_L2A", datetime=datetime(year, 7, 1 + tag, tzinfo=timezone.utc), cloud_cover=5.0,
        tile="MGRS-36SUF", hrefs={}, radiometry={b: (1e-4, 0.0) for b in BANDS},
    )


def fake_search(backend, geom, period, max_cloud=80.0, limit=30):
    return [make_scene(i, period.start.year) for i in range(3)]


def make_reader(cloud=False):
    def reader(scene, band, grid):
        year = scene.datetime.year
        h, w = grid.shape
        if band == "scl":
            scl = np.full((h, w), 4, "u1")
            if cloud:
                scl[:] = 9
            return scl
        base = dict(blue=500, green=800, red=400, nir=4000, swir16=1500)[band]
        arr = np.full((h, w), base, "u2")
        if year == 2024:  # clear a square in the 'after' year
            r0, r1, c0, c1 = h // 4, h // 2, w // 4, w // 2
            arr[r0:r1, c0:c1] = dict(blue=1200, green=1500, red=2000, nir=2200, swir16=3500)[band]
        return arr

    return reader


def test_end_to_end_outputs(tmp_path):
    geom = box(31.50, 36.80, 31.53, 36.83)
    res = run_analysis(
        geom, parse_period("2023-07"), parse_period("2024-07"), tmp_path, RunOptions(name="synthetic", resolution=20),
        reader=make_reader(), search=fake_search,
    )
    for name in ("report.html", "changes.geojson", "before.png", "after.png", "change_map.png", "change_class.tif", "summary.json", "overview.png",
                 "before.tif", "after.tif"):
        assert (tmp_path / name).stat().st_size > 0, name
    # the true-colour GeoTIFFs sit on the same grid as the class raster, with an alpha band outside the area
    import rasterio
    with rasterio.open(tmp_path / "change_class.tif") as c, rasterio.open(tmp_path / "after.tif") as a:
        assert a.crs == c.crs and a.transform == c.transform and a.count == 4
        assert [ci.name for ci in a.colorinterp] == ["red", "green", "blue", "alpha"]
        assert a.read(4).max() == 255
    fc = json.loads((tmp_path / "changes.geojson").read_text(encoding="utf-8"))
    assert len(fc["features"]) == 1 and fc["features"][0]["properties"]["class"] == "vegetation_loss"
    s = res.summary
    assert s["totals"]["vegetation_loss"]["area_ha"] > 5
    assert s["attribution"] == "Contains modified Copernicus Sentinel data 2023, 2024"
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "Contains modified Copernicus Sentinel data" in html and "L.map" in html and "<script src" not in html
    assert "http://" not in html.split('id="data"')[0].replace("http://www.w3.org", "")  # no external assets in the page shell


def test_refuses_when_everything_is_cloudy(tmp_path):
    geom = box(31.50, 36.80, 31.52, 36.82)
    with pytest.raises(InsufficientDataError):
        run_analysis(geom, parse_period("2023-07"), parse_period("2024-07"), tmp_path, RunOptions(resolution=20),
                     reader=make_reader(cloud=True), search=fake_search)


def test_area_limit(tmp_path):
    from earthdiff.aoi import AOIError

    with pytest.raises(AOIError):
        run_analysis(box(31, 36, 33, 38), parse_period("2023-07"), parse_period("2024-07"), tmp_path, RunOptions(),
                     reader=make_reader(), search=fake_search)


def test_watch_roundtrip(tmp_path):
    from datetime import date

    from earthdiff.watch import run_watch

    (tmp_path / "w.toml").write_text(
        '[defaults]\nout_dir="out"\n[[area]]\nname="a"\nbbox=[31.50,36.80,31.52,36.82]\n', encoding="utf-8"
    )

    def runner(geom, before, after, out, opts, progress=None):
        return run_analysis(geom, before, after, out, RunOptions(name=opts.name, resolution=20), reader=make_reader(), search=fake_search)

    o1 = run_watch(tmp_path / "w.toml", today=date(2024, 8, 5), runner=runner)
    assert o1[0].status == "done", o1[0].message
    o2 = run_watch(tmp_path / "w.toml", today=date(2024, 8, 5), runner=runner)
    assert o2[0].status == "skipped"
    assert (tmp_path / "out" / "a" / "history.csv").exists()


def test_excel_workbook_and_in_between_years(tmp_path):
    import io
    import zipfile
    import xml.etree.ElementTree as ET

    geom = box(31.50, 36.80, 31.53, 36.83)
    run_analysis(geom, parse_period("2021-07"), parse_period("2024-07"), tmp_path, RunOptions(name="synthetic", resolution=20),
                 reader=make_reader(), search=fake_search)
    # Excel: a real workbook with three sheets, valid XML, the change as a numeric row
    z = zipfile.ZipFile(tmp_path / "changes.xlsx")
    for n in z.namelist():
        ET.fromstring(z.read(n))
    assert {"xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml", "xl/worksheets/sheet3.xml"} <= set(z.namelist())
    changes = z.read("xl/worksheets/sheet2.xml").decode("utf-8")
    assert "Vegetation loss" in changes and "Green vegetation was lost here" in changes and "openstreetmap.org" in changes
    # the report embeds the workbook in English and Turkish
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    data = json.loads(html.split('id="data">', 1)[1].split("</script>", 1)[0])
    tr = zipfile.ZipFile(io.BytesIO(__import__("base64").b64decode(data["xlsx"]["tr"])))
    assert "Bitki kaybı" in tr.read("xl/worksheets/sheet2.xml").decode("utf-8") and "Özet" in tr.read("xl/workbook.xml").decode("utf-8")
    # 2021 -> 2024: one small frame for each of 2022 and 2023 between the before and after passes
    periods = [f["period"] for f in data["timeline"]["frames"]]
    assert periods.count("between") == 2 and periods.index("between") > periods.index("before") and periods[-1] == "after"
    years = sorted({f["date"][:4] for f in data["timeline"]["frames"] if f["period"] == "between"})
    assert years == ["2022", "2023"]
