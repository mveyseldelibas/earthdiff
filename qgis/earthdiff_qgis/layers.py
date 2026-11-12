"""Adds an earthdiff output folder to the QGIS project: change polygons, class raster and before/after images, styled."""
from __future__ import annotations

import json
from pathlib import Path

from qgis.core import (QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem, QgsFillSymbol, QgsPalettedRasterRenderer,
                       QgsProject, QgsRasterLayer, QgsRendererCategory, QgsVectorLayer)
from qgis.PyQt.QtGui import QColor

from .i18n import t

# the colour-blind friendly (Okabe-Ito) colours of earthdiff's classes, same as its report
CLASSES = [
    (1, "vegetation_loss", "#D55E00"),
    (2, "vegetation_gain", "#009E73"),
    (3, "new_water", "#0072B2"),
    (4, "water_loss", "#F0E442"),
    (5, "new_bare_or_built", "#CC79A7"),
]


def read_summary(folder: str | Path) -> dict:
    p = Path(folder) / "summary.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def _changes_renderer() -> QgsCategorizedSymbolRenderer:
    cats = []
    for _, key, col in CLASSES:
        c = QColor(col)
        # "r,g,b,a": an 8-digit hex colour would be read as #AARRGGBB
        sym = QgsFillSymbol.createSimple({"color": f"{c.red()},{c.green()},{c.blue()},110", "outline_color": col, "outline_width": "0.5"})
        cats.append(QgsRendererCategory(key, sym, t("c_" + key)))
    return QgsCategorizedSymbolRenderer("class", cats)


def _class_renderer(layer: QgsRasterLayer) -> QgsPalettedRasterRenderer:
    classes = [QgsPalettedRasterRenderer.Class(v, QColor(col), t("c_" + key)) for v, key, col in CLASSES]
    return QgsPalettedRasterRenderer(layer.dataProvider(), 1, classes)


def add_outputs(folder: str | Path, name: str, project: QgsProject | None = None) -> list:
    """Adds the layers of one run in a group at the top of the layer tree; returns them (changes layer first)."""
    project = project or QgsProject.instance()
    folder = Path(folder)
    s = read_summary(folder)
    before_lbl, after_lbl = s.get("before_label", ""), s.get("after_label", "")
    root = project.layerTreeRoot()
    group = root.insertGroup(0, t("g_group", name=name))
    added = []

    def put(layer, visible=True):
        if not layer.isValid():
            return None
        project.addMapLayer(layer, False)
        node = group.addLayer(layer)
        node.setItemVisibilityChecked(visible)
        added.append(layer)
        return layer

    gj = folder / "changes.geojson"
    if gj.is_file():
        v = QgsVectorLayer(str(gj), t("l_changes"), "ogr")
        if v.isValid():
            if not v.crs().isValid():
                v.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
            v.setRenderer(_changes_renderer())
            put(v)
    ct = folder / "change_class.tif"
    if ct.is_file():
        r = QgsRasterLayer(str(ct), t("l_classes"))
        if r.isValid():
            r.setRenderer(_class_renderer(r))
            put(r, visible=False)
    for fn, key, lbl, vis in (("after.tif", "l_after", after_lbl, True), ("before.tif", "l_before", before_lbl, False)):
        f = folder / fn
        if f.is_file():
            put(QgsRasterLayer(str(f), t(key, lbl=lbl)), visible=vis)
    return added
