"""Checks of the QGIS plugin that need no QGIS: metadata, Python 3.9 syntax (QGIS 3.28), texts, command line, zip."""
import ast
import configparser
import importlib
import sys
import types
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "qgis" / "earthdiff_qgis"


def _stub_qgis(monkeypatch):
    """Just enough of qgis.core / qgis.PyQt for i18n.py and runner.py to import."""
    core = types.ModuleType("qgis.core")

    class Settings:
        store: dict = {}

        def value(self, k, d=None):
            return self.store.get(k, d)

        def setValue(self, k, v):
            self.store[k] = v

    core.QgsSettings = Settings
    core.QgsApplication = type("QgsApplication", (), {"locale": staticmethod(lambda: "en_US")})
    qtcore = types.ModuleType("qgis.PyQt.QtCore")
    qtcore.QObject = object
    qtcore.pyqtSignal = lambda *a: None
    qtcore.QProcess = type("QProcess", (), {})
    qtcore.QProcessEnvironment = type("QProcessEnvironment", (), {})
    pyqt = types.ModuleType("qgis.PyQt")
    qgis = types.ModuleType("qgis")
    for name, mod in (("qgis", qgis), ("qgis.core", core), ("qgis.PyQt", pyqt), ("qgis.PyQt.QtCore", qtcore)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.syspath_prepend(str(PLUGIN.parent))
    for m in ("earthdiff_qgis", "earthdiff_qgis.i18n", "earthdiff_qgis.runner"):
        sys.modules.pop(m, None)
    return Settings


def test_metadata_has_what_the_plugin_repository_needs():
    meta = configparser.ConfigParser()
    meta.read(PLUGIN / "metadata.txt", encoding="utf-8")
    g = meta["general"]
    for key in ("name", "qgisMinimumVersion", "description", "about", "version", "author", "email", "repository", "tracker", "icon", "category"):
        assert g.get(key), key
    assert "supportsQt6" not in g and g["qgisMaximumVersion"].startswith("4.")  # QGIS 4 support comes from qgisMaximumVersion
    assert (PLUGIN / g["icon"]).is_file() and (PLUGIN / "LICENSE").is_file()
    emails = [ln for ln in (PLUGIN / "metadata.txt").read_text(encoding="utf-8").splitlines() if "@" in ln]
    # plugins.qgis.org writes to this address about the plugin, so it must be a reachable one; it is the only address there
    assert emails == ["email=mveyseldelibas@gmail.com"]


def test_plugin_code_runs_on_the_python_of_qgis_3_28():
    for f in PLUGIN.glob("*.py"):
        ast.parse(f.read_text(encoding="utf-8"), filename=str(f), feature_version=(3, 9))


def test_turkish_and_english_texts_match(monkeypatch):
    settings = _stub_qgis(monkeypatch)
    i18n = importlib.import_module("earthdiff_qgis.i18n")
    assert set(i18n.TEXT["tr"]) == set(i18n.TEXT["en"])
    assert len(i18n.TEXT["tr"]["months"]) == len(i18n.TEXT["en"]["months"]) == 12
    settings.store["earthdiff/lang"] = "tr"
    assert i18n.num(1234.5) == "1.234,5" and i18n.t("area", max=i18n.num(2000, 0)).endswith("2.000 km²)")
    settings.store["earthdiff/lang"] = "en"
    assert i18n.num(1234.5) == "1,234.5" and i18n.lang() == "en"


def test_command_line_and_command_lookup(monkeypatch):
    _stub_qgis(monkeypatch)
    runner = importlib.import_module("earthdiff_qgis.runner")
    a = runner.run_args("aoi.geojson", "2024-08", "2020-08", "out", "Manavgat", "default", 0.5,
                        likely_cause=True, seasonal=True, terrain=False)
    assert a[:7] == ["run", "--aoi", "aoi.geojson", "--after", "2024-08", "--before", "2020-08"]
    assert "--seasonal-check" in a and "--no-terrain" in a and "--no-likely-cause" not in a and a[a.index("--name") + 1] == "Manavgat"
    p = runner.run_args("aoi.geojson", "2024-08", "2020-08", "out", "x", "strict", 1, likely_cause=False, seasonal=True, terrain=True, plan=True)
    assert p[0] == "plan" and "--out" not in p and "--seasonal-check" not in p and "--no-likely-cause" in p
    assert runner.find_command(r"C:\Py\python.exe") == [r"C:\Py\python.exe", "-m", "earthdiff"]
    assert runner.find_command("/opt/bin/uvx") == ["/opt/bin/uvx", "--from", "earthdiff", "earthdiff"]
    assert runner.find_command('"C:\\tools\\earthdiff.exe"') == ["C:\\tools\\earthdiff.exe"]
    # earthdiff inside the QGIS installation would run on QGIS's own Python: never picked automatically
    assert runner._not_qgis(str(Path(sys.prefix) / "Scripts" / "earthdiff.exe")) is False


def test_plugin_zip(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        build = importlib.import_module("build_qgis_plugin")
    finally:
        sys.path.pop(0)
    monkeypatch.setattr(build, "ROOT", tmp_path)
    out = build.main()
    names = zipfile.ZipFile(out).namelist()
    for n in ("metadata.txt", "__init__.py", "plugin.py", "dock.py", "runner.py", "layers.py", "i18n.py", "icon.png", "LICENSE"):
        assert f"earthdiff_qgis/{n}" in names, n
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
