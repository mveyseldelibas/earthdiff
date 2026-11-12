"""Local web UI: the page and the server use the same area limit."""
from __future__ import annotations

from shapely.geometry import box, mapping

from earthdiff import ui


def test_page_carries_the_server_limit():
    html = ui.page().decode("utf-8")
    assert "__MAX_AREA_KM2__" not in html
    assert f"const MAX_KM2={int(ui.UI_MAX_AREA_KM2)};" in html
    assert f"({int(ui.UI_MAX_AREA_KM2)}).toLocaleString(" in html  # the label is built in the page, in the chosen language
    assert "Dikdörtgen" in html and "Rectangle" in html


def test_server_rejects_an_area_over_the_limit(tmp_path):
    state = ui.State(tmp_path)
    job = ui.Job("too big")
    # about 1.0 x 0.8 degrees near Karaman: roughly 7,000 km2, over the 2,000 km2 limit
    payload = {"geometry": mapping(box(32.8, 37.0, 33.8, 37.8)), "after": "2025-11"}
    ui._execute(state, job, payload)
    assert job.status == "error"
    assert "limit is 2,000 km2" in job.error


def test_a_window_longer_than_a_year_is_refused_like_in_the_mcp_server(tmp_path):
    state = ui.State(tmp_path)
    job = ui.Job("long window")
    payload = {"geometry": mapping(box(31.50, 36.80, 31.53, 36.83)), "after": "2023-01-01/2024-06-30"}
    ui._execute(state, job, payload)
    assert job.status == "error" and "limit is 366 days" in job.error


def test_different_seasons_are_refused_like_everywhere_else(tmp_path):
    state = ui.State(tmp_path)
    job = ui.Job("winter vs autumn")
    payload = {"geometry": mapping(box(31.50, 36.80, 31.53, 36.83)), "after": "2026-10", "before": "2025-02"}
    ui._execute(state, job, payload)
    assert job.status == "error" and "seasonal cycle" in job.error



def test_page_shows_the_result_with_the_chosen_basemap():
    html = ui.page().decode("utf-8")
    line = next(ln for ln in html.splitlines() if "$('#result').src=" in ln)
    assert "//" not in line.split("$('#result').src=")[0] and "$('#result').style.display='block'" in line
    # the basemap picked on the drawing page is shown there and passed to the report; no 3D on the drawing page
    assert "$('#result').src=j.report_url+'#base='+encodeURIComponent($('#base').value);" in html
    assert 'id="views"' not in html and 'prev3d' not in html
    for v in ('none', 'relief', 'osm', 'topo'):
        assert f'<option value="{v}"' in html


def test_relief_tile_is_shaded_from_terrarium_elevation():
    import io

    import numpy as np
    from PIL import Image

    # a 256 x 256 Terrarium tile with a ridge running north-south; elevation = R*256 + G + B/256 - 32768
    x = np.arange(256)
    elev = 32768 + 500 + 400 * np.exp(-((x - 128) / 30.0) ** 2)
    rgb = np.zeros((256, 256, 3), np.uint8)
    rgb[..., 0] = (elev // 256)[None, :]
    rgb[..., 1] = (elev % 256)[None, :]
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, "PNG")
    png = ui.relief_tile(13, 4100, 3000, fetch=lambda url: buf.getvalue())
    shade = np.asarray(Image.open(io.BytesIO(png)))
    assert shade.shape == (256, 256)
    assert shade[:, 100].mean() > shade[:, 156].mean()  # west slope faces the north-west light, east slope is darker
    assert ui.relief_tile(3, 99, 0) is None  # outside the tile grid
