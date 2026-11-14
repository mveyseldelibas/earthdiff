"""The earthdiff panel: area, dates and settings; runs earthdiff and adds its results to the map."""
from __future__ import annotations

import html
import json
import re
import tempfile
from datetime import date, datetime
from pathlib import Path

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea, QgsGeometry, QgsProject,
                       QgsRectangle, QgsSettings, QgsWkbTypes)
from qgis.gui import QgsMapLayerComboBox, QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout,
                                 QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                                 QRadioButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from . import layers
from .i18n import num, t
from .runner import Runner, find_command, run_args

MAX_KM2 = 2000  # earthdiff's own limit (CLI, web UI and MCP server)
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


def _polygon_geom_type():
    try:  # QGIS >= 3.30
        from qgis.core import Qgis
        return Qgis.GeometryType.Polygon
    except AttributeError:
        return QgsWkbTypes.PolygonGeometry


def _polygon_filter():
    try:  # QGIS >= 3.34
        from qgis.core import Qgis
        return Qgis.LayerFilter.PolygonLayer
    except AttributeError:
        from qgis.core import QgsMapLayerProxyModel
        return QgsMapLayerProxyModel.PolygonLayer


class _Outline:
    """The area outline: a black line on a white halo, readable on any base map and unlike every result colour."""

    def __init__(self, canvas):
        self.canvas = canvas
        self.halo, self.line = QgsRubberBand(canvas, _polygon_geom_type()), QgsRubberBand(canvas, _polygon_geom_type())
        for band, colour, width in ((self.halo, QColor(255, 255, 255, 230), 6), (self.line, QColor(0, 0, 0, 255), 2)):
            band.setStrokeColor(colour)
            band.setFillColor(QColor(0, 0, 0, 0))  # no fill: the map and the results stay readable inside
            band.setWidth(width)

    def setToGeometry(self, geom):
        for band in (self.halo, self.line):
            band.setToGeometry(geom, None)

    def reset(self):
        for band in (self.halo, self.line):
            band.reset(_polygon_geom_type())

    def numberOfVertices(self) -> int:
        return self.line.numberOfVertices()

    def remove(self):
        self.reset()
        for band in (self.halo, self.line):
            self.canvas.scene().removeItem(band)


class _RectTool(QgsMapTool):
    """Draw a rectangle by dragging; the outline is shown while dragging (QgsMapToolExtent's own is too faint)."""
    drawn = pyqtSignal(QgsRectangle)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.start = None
        self.band = _Outline(canvas)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _rect(self, e) -> QgsRectangle:
        r = QgsRectangle(self.start, e.mapPoint())
        r.normalize()
        return r

    def canvasPressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.start = e.mapPoint()

    def canvasMoveEvent(self, e):
        if self.start is not None:
            self.band.setToGeometry(QgsGeometry.fromRect(self._rect(e)))

    def canvasReleaseEvent(self, e):
        if self.start is None or e.button() != Qt.MouseButton.LeftButton:
            return
        r = self._rect(e)
        self.start = None
        self.band.reset()
        if r.width() > 0 and r.height() > 0:
            self.drawn.emit(r)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.start = None
            self.band.reset()

    def deactivate(self):
        self.start = None
        self.band.reset()
        super().deactivate()

    def remove_band(self):
        self.band.remove()


def _slug(s: str) -> str:
    s = re.sub(r"[^\w\-]+", "-", s.strip(), flags=re.UNICODE).strip("-").lower()
    return s[:40] or "earthdiff"


class EarthdiffDock(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__(t("title"), parent)
        self.setObjectName("EarthdiffDock")
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.settings = QgsSettings()
        self.runner = Runner(self)
        self.runner.line.connect(self._log)
        self.runner.finished.connect(self._finished)
        self.drawn: QgsRectangle | None = None  # in the canvas CRS
        self.drawn_crs = None
        self.aoi_wgs84: QgsGeometry | None = None
        self.job: dict | None = None
        self.tool = None
        self.prev_tool = None
        self.band = _Outline(self.canvas)
        self.result_runs: list[list[str]] = []  # layer ids added by each run of this panel
        self.view_before = None  # (extent, crs) of the map before the first of those runs
        self._build()
        self.canvas.extentsChanged.connect(self._area_changed)
        self.canvas.destinationCrsChanged.connect(self._area_changed)
        self._area_changed()
        self._detect()

    # ------------------------------------------------------------------ layout
    def _build(self):
        body = QWidget()
        v = QVBoxLayout(body)
        intro = QLabel(t("intro"))
        intro.setWordWrap(True)
        v.addWidget(intro)

        # 1. area
        g = QGroupBox(t("area", max=num(MAX_KM2, 0)))
        gl = QVBoxLayout(g)
        self.r_extent, self.r_draw, self.r_sel = QRadioButton(t("a_extent")), QRadioButton(t("a_draw")), QRadioButton(t("a_sel"))
        self.area_mode = QButtonGroup(self)
        for i, r in enumerate((self.r_extent, self.r_draw, self.r_sel)):
            self.area_mode.addButton(r, i)
        self.r_extent.setChecked(True)
        gl.addWidget(self.r_extent)
        row = QHBoxLayout()
        row.addWidget(self.r_draw)
        self.b_draw = QPushButton(t("draw"))
        self.b_draw.clicked.connect(self._start_draw)
        row.addWidget(self.b_draw)
        row.addStretch(1)
        gl.addLayout(row)
        gl.addWidget(self.r_sel)
        self.layer_box = QgsMapLayerComboBox()
        self.layer_box.setFilters(_polygon_filter())
        gl.addWidget(self.layer_box)
        self.l_size = QLabel()
        self.l_size.setWordWrap(True)
        gl.addWidget(self.l_size)
        self.area_mode.buttonClicked.connect(lambda *_: self._area_changed())
        self.layer_box.layerChanged.connect(self._layer_changed)
        v.addWidget(g)
        self._sel_layer = None
        self._layer_changed(self.layer_box.currentLayer())

        # 2. dates (after = a month; before = the same month of an earlier year, so both are the same season)
        g = QGroupBox(t("dates"))
        f = QFormLayout(g)
        last = date.today().replace(day=1)
        last = date(last.year - 1, 12, 1) if last.month == 1 else date(last.year, last.month - 1, 1)
        row = QHBoxLayout()
        self.month = QComboBox()
        self.month.addItems(t("months"))
        self.month.setCurrentIndex(last.month - 1)
        self.year = QSpinBox()
        self.year.setRange(2018, date.today().year)
        self.year.setValue(last.year)
        row.addWidget(self.month, 2)
        row.addWidget(self.year, 1)
        f.addRow(t("after"), row)
        self.before = QComboBox()
        f.addRow(t("before"), self.before)
        note = QLabel(t("same_month"))
        note.setWordWrap(True)
        f.addRow(note)
        self.year.valueChanged.connect(self._fill_years)
        self._fill_years()
        v.addWidget(g)

        # 3. settings
        g = QGroupBox(t("opts"))
        f = QFormLayout(g)
        self.sens = QComboBox()
        for key in ("strict", "default", "sensitive"):
            self.sens.addItem(t("s_" + key), key)
        self.sens.setCurrentIndex(1)
        f.addRow(t("sens"), self.sens)
        self.mmu = QDoubleSpinBox()
        self.mmu.setRange(0.1, 50)
        self.mmu.setDecimals(1)
        self.mmu.setSingleStep(0.5)
        self.mmu.setValue(0.5)
        self.mmu.setSuffix(" ha")
        f.addRow(t("mmu"), self.mmu)
        self.c_cause, self.c_seasonal, self.c_terrain = QCheckBox(t("cause")), QCheckBox(t("seasonal")), QCheckBox(t("terrain"))
        for c, tip in ((self.c_cause, "cause_tip"), (self.c_seasonal, "seasonal_tip"), (self.c_terrain, "terrain_tip")):
            c.setToolTip(t(tip))
        self.c_cause.setChecked(True)
        self.c_terrain.setChecked(True)
        for c in (self.c_cause, self.c_seasonal, self.c_terrain):
            f.addRow(c)
        self.name = QLineEdit()
        self.name.setPlaceholderText(t("name_ph"))
        f.addRow(t("name"), self.name)
        v.addWidget(g)

        # output and command
        g = QGroupBox(t("adv"))
        f = QFormLayout(g)
        row = QHBoxLayout()
        self.out = QLineEdit(str(self.settings.value("earthdiff/outdir", str(Path.home() / "earthdiff-qgis"))))
        b = QPushButton(t("browse"))
        b.clicked.connect(self._pick_out)
        row.addWidget(self.out, 1)
        row.addWidget(b)
        f.addRow(t("out"), row)
        row = QHBoxLayout()
        self.cmd = QLineEdit(str(self.settings.value("earthdiff/command", "")))
        self.cmd.setPlaceholderText(t("cmd_auto"))
        self.cmd.editingFinished.connect(self._detect)
        b1, b2 = QPushButton(t("cmd_pick")), QPushButton(t("cmd_test"))
        b1.clicked.connect(self._pick_cmd)
        b2.clicked.connect(self._test_cmd)
        row.addWidget(self.cmd, 1)
        row.addWidget(b1)
        row.addWidget(b2)
        f.addRow(t("cmd"), row)
        self.l_cmd = QLabel()
        self.l_cmd.setWordWrap(True)
        self.l_cmd.setOpenExternalLinks(True)
        self.l_cmd.setTextFormat(Qt.TextFormat.RichText)
        f.addRow(self.l_cmd)
        v.addWidget(g)

        # actions
        row = QHBoxLayout()
        self.b_plan, self.b_run, self.b_stop = QPushButton(t("plan")), QPushButton(t("run")), QPushButton(t("stop"))
        self.b_run.setDefault(True)
        self.b_plan.clicked.connect(lambda: self._start(plan=True))
        self.b_run.clicked.connect(lambda: self._start(plan=False))
        self.b_stop.clicked.connect(self.runner.stop)
        self.b_stop.setEnabled(False)
        for b in (self.b_plan, self.b_run, self.b_stop):
            row.addWidget(b)
        v.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setVisible(False)
        v.addWidget(self.bar)
        self.status = QLabel()
        self.status.setWordWrap(True)
        v.addWidget(self.status)

        # result
        self.result = QLabel()
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.TextFormat.RichText)
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.result)
        row = QHBoxLayout()
        self.b_open, self.b_folder, self.b_new = QPushButton(t("open")), QPushButton(t("folder")), QPushButton(t("new"))
        self.b_open.clicked.connect(lambda: self._open("report.html"))
        self.b_folder.clicked.connect(lambda: self._open(""))
        for b in (self.b_open, self.b_folder):
            b.setEnabled(False)
            row.addWidget(b)
        row.addStretch(1)
        self.b_new.setToolTip(t("new_tip"))
        self.b_new.clicked.connect(self.new_analysis)
        row.addWidget(self.b_new)
        v.addLayout(row)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setMinimumHeight(140)
        v.addWidget(self.log, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        self.setWidget(scroll)

    # ------------------------------------------------------------------ area
    def _layer_changed(self, layer):
        if self._sel_layer is not None:
            try:
                self._sel_layer.selectionChanged.disconnect(self._area_changed)
            except (TypeError, RuntimeError):
                pass
        self._sel_layer = layer
        if layer is not None:
            layer.selectionChanged.connect(self._area_changed)
        self._area_changed()

    def _start_draw(self):
        self.r_draw.setChecked(True)
        if self.tool is None:
            self.tool = _RectTool(self.canvas)
            self.tool.drawn.connect(self._drawn)
        self.prev_tool = self.canvas.mapTool()
        self.canvas.setMapTool(self.tool)
        self.status.setText(t("draw_hint"))

    def _drawn(self, rect):
        if rect is None or rect.isEmpty():
            return
        self.drawn, self.drawn_crs = QgsRectangle(rect), self.canvas.mapSettings().destinationCrs()
        self._stop_draw()
        self.status.setText("")
        self._area_changed()

    def _stop_draw(self):
        if self.tool is None or self.canvas.mapTool() is not self.tool:
            return
        if self.prev_tool is not None and self.prev_tool is not self.tool:
            self.canvas.setMapTool(self.prev_tool)
        else:
            self.canvas.unsetMapTool(self.tool)

    def _current_area(self) -> tuple[QgsGeometry | None, str]:
        """The area in WGS84 and a short default name, or (None, reason)."""
        mode = self.area_mode.checkedId()
        if mode == 0:
            g = QgsGeometry.fromRect(self.canvas.extent())
            src, nm = self.canvas.mapSettings().destinationCrs(), t("extent")
        elif mode == 1:
            if self.drawn is None:
                return None, t("size_none")
            g, src, nm = QgsGeometry.fromRect(self.drawn), self.drawn_crs, t("drawn")
        else:
            lyr = self.layer_box.currentLayer()
            feats = list(lyr.getSelectedFeatures()) if lyr is not None else []
            if not feats:
                return None, t("no_sel")
            g = QgsGeometry.unaryUnion([f.geometry() for f in feats if f.hasGeometry()])
            src, nm = lyr.crs(), lyr.name()
        if g is None or g.isEmpty():
            return None, t("size_none")
        g = QgsGeometry(g)
        if src != WGS84:
            g.transform(QgsCoordinateTransform(src, WGS84, QgsProject.instance()))
        return g, nm

    def _area_km2(self, g: QgsGeometry) -> float:
        da = QgsDistanceArea()
        da.setSourceCrs(WGS84, QgsProject.instance().transformContext())
        da.setEllipsoid("WGS84")
        return da.measureArea(g) / 1e6

    def _area_changed(self, *_):
        g, why = self._current_area()
        self.aoi_wgs84 = None
        self.band.reset()
        if g is None:
            self.l_size.setText(why)
            return
        km2 = self._area_km2(g)
        txt = num(km2, 1 if km2 < 10 else 0)
        if km2 > MAX_KM2:
            self.l_size.setText("<b style='color:#b42318'>" + html.escape(t("too_big", km2=txt, max=num(MAX_KM2, 0))) + "</b>")
        else:
            self.l_size.setText(t("size", km2=txt))
            self.aoi_wgs84 = g
        if self.area_mode.checkedId() != 0 and self.isVisible():  # the map view itself needs no outline
            show = QgsGeometry(g)
            dst = self.canvas.mapSettings().destinationCrs()
            if dst != WGS84:
                show.transform(QgsCoordinateTransform(WGS84, dst, QgsProject.instance()))
            self.band.setToGeometry(show)

    # ------------------------------------------------------------------ dates and paths
    def _fill_years(self):
        keep = self.before.currentData()
        ay = self.year.value()
        self.before.clear()
        for y in range(ay - 1, 2016, -1):
            n = ay - y
            self.before.addItem(t("years_ago" if n == 1 else "years_ago_n", y=y, n=n), y)
        i = self.before.findData(keep)
        self.before.setCurrentIndex(i if i >= 0 else 0)

    def _pick_out(self):
        d = QFileDialog.getExistingDirectory(self, t("out"), self.out.text())
        if d:
            self.out.setText(d)

    def _pick_cmd(self):
        f, _ = QFileDialog.getOpenFileName(self, t("cmd"), str(Path.home()))
        if f:
            self.cmd.setText(f)
            self._detect()

    def _detect(self):
        self.settings.setValue("earthdiff/command", self.cmd.text().strip())
        c = find_command(self.cmd.text())
        self.command = c
        if c:
            shown = html.escape(t("cmd_found", c=" ".join(c)))
            self.l_cmd.setText(shown.replace("\\", "\\&#8203;").replace("/", "/&#8203;"))  # long paths may wrap
            self.l_cmd.setToolTip(" ".join(c))
        else:
            self.l_cmd.setText(t("cmd_none"))

    def _test_cmd(self):
        if self.command and not self.runner.busy():
            self.job = None
            self.runner.start(self.command, ["--version"])

    def _open(self, name: str):
        if self.job and self.job.get("out"):
            p = Path(self.job["out"]) / name if name else Path(self.job["out"])
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    # ------------------------------------------------------------------ run
    def _start(self, plan: bool):
        if self.runner.busy():
            self.status.setText(t("busy"))
            return
        if not self.command:
            self._detect()
            if not self.command:
                self.status.setText(t("cmd_none"))
                return
        g, why = self._current_area()
        if g is None or self.aoi_wgs84 is None:
            self.status.setText(why if g is None else self.l_size.text())
            return
        if (self.year.value(), self.month.currentIndex() + 1) > (date.today().year, date.today().month):
            self.status.setText(t("future"))
            return
        name = self.name.text().strip() or why
        after = f"{self.year.value()}-{self.month.currentIndex() + 1:02d}"
        before = f"{self.before.currentData()}-{self.month.currentIndex() + 1:02d}"
        self.settings.setValue("earthdiff/outdir", self.out.text())
        out = Path(self.out.text()) / f"{_slug(name)}-{datetime.now():%Y%m%d-%H%M%S}"
        aoi = Path(tempfile.gettempdir()) / f"earthdiff-aoi-{datetime.now():%Y%m%d%H%M%S}.geojson"
        aoi.write_text(json.dumps({"type": "Feature", "properties": {}, "geometry": json.loads(g.asJson(6))}), encoding="utf-8")
        args = run_args(str(aoi), after, before, str(out), name, self.sens.currentData(), self.mmu.value(),
                        self.c_cause.isChecked(), self.c_seasonal.isChecked(), self.c_terrain.isChecked(), plan=plan)
        self.job = {"plan": plan, "out": None if plan else str(out), "name": name}
        if not plan and not self.result_runs:
            self.view_before = (QgsRectangle(self.canvas.extent()), self.canvas.mapSettings().destinationCrs())
        self.log.clear()
        self.result.setText("")
        for b in (self.b_open, self.b_folder):
            b.setEnabled(False)
        self._busy(True)
        self.status.setText(t("running") if not plan else "")
        self.runner.start(self.command, args)

    def _busy(self, on: bool):
        self.bar.setVisible(on)
        self.b_stop.setEnabled(on)
        self.b_run.setEnabled(not on)
        self.b_plan.setEnabled(not on)
        self.b_new.setEnabled(not on)

    def _log(self, s: str):
        self.log.appendPlainText(s)

    def _finished(self, code: int):
        self._busy(False)
        job = self.job or {}
        if code in (-1, -2):
            self.status.setText(t("stopped" if code == -1 else "nostart"))
            return
        if code != 0:
            self.status.setText(t("failed", code=code))
            return
        if job.get("plan") or not job.get("out"):
            self.status.setText("")
            return
        out = Path(job["out"])
        try:
            added = layers.add_outputs(out, job["name"])
        except Exception as exc:  # a broken file must not take QGIS down
            self._log(f"could not add the layers: {exc!r}")
            added = []
        if added:
            self.result_runs.append([lyr.id() for lyr in added])
            ext = added[0].extent()
            tr = QgsCoordinateTransform(added[0].crs(), self.canvas.mapSettings().destinationCrs(), QgsProject.instance())
            self.canvas.setExtent(tr.transformBoundingBox(ext).buffered(ext.width() * 0.05 if added[0].crs().isGeographic() else 200))
            self.canvas.refresh()
        self.result.setText(self._summary_html(layers.read_summary(out), job["name"]))
        self.status.setText(t("done"))
        for b in (self.b_open, self.b_folder):
            b.setEnabled(True)

    def _summary_html(self, s: dict, name: str) -> str:
        """Every class, the comparable share, the likely-cause split and EVERY warning: the numbers need their limits."""
        if not s:
            return ""
        e = html.escape
        rows = [f"<b>{e(t('res_title', name=name))}</b>",
                e(f"{s.get('before_label', '')} → {s.get('after_label', '')} · {num(s.get('aoi_km2', 0))} km² · {num(s.get('resolution_m', 0), 0)} m")]
        tot = s.get("totals", {})
        items = []
        for _, key, col in layers.CLASSES:
            v = tot.get(key)
            if v:
                items.append(f"<span style='color:{col}'>■</span> {e(t('c_' + key))}: <b>{num(v.get('area_ha', 0))} ha</b> "
                             f"({e(t('polys', n=v.get('polygons', 0)))})")
        rows.append("<br>".join(items))
        vf = s.get("valid_fraction")
        if vf is not None:
            rows.append(e(t("comparable", p=round(vf * 100))))
        lc = (s.get("likely_cause") or {}).get("totals") or {}
        if lc and (tot.get("vegetation_loss") or {}).get("area_ha"):
            ha = lambda k: num((lc.get(k) or {}).get("area_ha", 0))
            rows.append(e(t("cause_line", f=ha("fire-like"), c=ha("clearing-like"), u=ha("unclear"))))
        sc = s.get("seasonal_check") or {}
        ls = sc.get("likely_seasonal") if isinstance(sc, dict) else None
        if isinstance(ls, dict) and "area_ha" in ls:
            rows.append(e(t("seasonal_line", a=num(ls["area_ha"]))))
        w = s.get("warnings") or []
        rows.append(f"<b>{e(t('warnings'))}</b><br>" + ("<br>".join("⚠ " + e(str(x)) for x in w) if w else e(t("no_warnings"))))
        rows.append(f"<i>{e(t('screening'))}</i>")
        if s.get("attribution"):
            rows.append(f"<small>{e(s['attribution'])}</small>")
        return "<br><br>".join(rows)

    # ------------------------------------------------------------------ new analysis / closing
    def new_analysis(self, *_):
        """Back to a clean panel; the result layers of this panel go too, if the user agrees."""
        if self.runner.busy():
            self.status.setText(t("busy"))
            return
        self._stop_draw()
        project = QgsProject.instance()
        runs = [[i for i in run if project.mapLayer(i) is not None] for run in self.result_runs]
        runs = [r for r in runs if r]
        if runs:
            box = QMessageBox(QMessageBox.Icon.Question, t("clear_title"), t("clear_q", n=len(runs)), parent=self)
            rm = box.addButton(t("remove"), QMessageBox.ButtonRole.AcceptRole)
            box.addButton(t("keep"), QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(rm)
            box.exec()
            if box.clickedButton() is rm:
                self._remove_results([i for r in runs for i in r])
        self.result_runs, self.view_before = [], None
        self.job, self.drawn, self.drawn_crs = None, None, None
        self.r_extent.setChecked(True)
        self.name.clear()
        for w in (self.result, self.status):
            w.setText("")
        self.log.clear()
        for b in (self.b_open, self.b_folder):
            b.setEnabled(False)
        self._area_changed()

    def _remove_results(self, ids: list[str]):
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        groups = []
        for i in ids:
            node = root.findLayer(i)
            parent = node.parent() if node is not None else None
            if parent is not None and parent is not root and all(parent is not g for g in groups):
                groups.append(parent)
        project.removeMapLayers(ids)
        for g in groups:
            if not g.children() and g.parent() is not None:
                g.parent().removeChildNode(g)
        if self.view_before is not None:
            ext, crs = self.view_before
            dst = self.canvas.mapSettings().destinationCrs()
            if crs != dst:
                ext = QgsCoordinateTransform(crs, dst, project).transformBoundingBox(ext)
            self.canvas.setExtent(ext)
        self.canvas.refresh()

    def closeEvent(self, e):  # the panel's close button or the toolbar button
        self._stop_draw()
        if not self.runner.busy():  # a running analysis keeps going and adds its layers when done
            self.new_analysis()
        super().closeEvent(e)

    def hideEvent(self, e):
        self.band.reset()
        super().hideEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        self._area_changed()

    # ------------------------------------------------------------------ cleanup
    def cleanup(self):
        self.runner.stop()
        self.band.remove()
        if self.tool is not None:
            if self.canvas.mapTool() is self.tool:
                self.canvas.unsetMapTool(self.tool)
            self.tool.remove_band()
        try:
            self.canvas.extentsChanged.disconnect(self._area_changed)
            self.canvas.destinationCrsChanged.disconnect(self._area_changed)
        except (TypeError, RuntimeError):
            pass
