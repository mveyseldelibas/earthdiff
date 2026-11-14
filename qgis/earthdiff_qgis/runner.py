"""Finds the earthdiff command and runs it in the background (QProcess).

earthdiff needs rasterio, pystac and friends; QGIS's own Python does not have them, and installing them there can break QGIS.
So the plugin runs the earthdiff program of a normal Python installation (pip install earthdiff) or through uv (uvx), with
QGIS's own GDAL / PROJ / Python variables removed from its environment.
"""
from __future__ import annotations

import glob
import os
import shutil
import sys
from pathlib import Path

from qgis.PyQt.QtCore import QObject, QProcess, QProcessEnvironment, pyqtSignal

WIN = sys.platform.startswith("win")
EXE = ".exe" if WIN else ""


def _candidates() -> list[list[str]]:
    """Places where `pip install earthdiff` / `uv tool install earthdiff` / pipx usually put the program."""
    home = Path.home()
    found: list[list[str]] = []
    names = []
    if WIN:
        for base in (os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")):
            if base:
                names += glob.glob(os.path.join(base, "Python", "Python3*", "Scripts", "earthdiff.exe"))
                names += glob.glob(os.path.join(base, "Programs", "Python", "Python3*", "Scripts", "earthdiff.exe"))
        names.append(str(home / ".local" / "bin" / "earthdiff.exe"))
    else:
        names += [str(home / ".local" / "bin" / "earthdiff"), "/opt/homebrew/bin/earthdiff", "/usr/local/bin/earthdiff"]
    for n in names:
        if n and os.path.isfile(n):
            found.append([n])
    return found


def _not_qgis(path: str) -> bool:
    """True when ``path`` is not inside the QGIS installation (its own earthdiff would run on QGIS's Python)."""
    p = os.path.normcase(os.path.abspath(path))
    roots = [sys.prefix, os.environ.get("OSGEO4W_ROOT", ""), os.environ.get("QGIS_PREFIX_PATH", "")]
    return not any(r and p.startswith(os.path.normcase(os.path.abspath(r))) for r in roots)


def find_command(setting: str = "") -> list[str] | None:
    """The command line prefix that runs earthdiff, e.g. ['C:/.../earthdiff.exe'] or ['uvx', '--from', 'earthdiff', 'earthdiff']."""
    setting = (setting or "").strip().strip('"')
    if setting:
        base = setting.replace("\\", "/").rsplit("/", 1)[-1].lower()  # either separator, whatever the system
        if base.startswith("python"):
            return [setting, "-m", "earthdiff"]
        if base.startswith("uvx"):
            return [setting, "--from", "earthdiff", "earthdiff"]
        return [setting]
    w = shutil.which("earthdiff")
    if w and _not_qgis(w):
        return [w]
    c = _candidates()
    if c:
        return c[0]
    homes = (Path.home() / ".local" / "bin" / ("uvx" + EXE), Path.home() / ".cargo" / "bin" / ("uvx" + EXE))
    uvx = shutil.which("uvx") or next((str(p) for p in homes if p.is_file()), None)
    if uvx:
        return [uvx, "--from", "earthdiff", "earthdiff"]
    return None


def clean_environment() -> QProcessEnvironment:
    """The system environment without QGIS's Python / GDAL / PROJ / Qt settings, which would confuse another Python."""
    env = QProcessEnvironment.systemEnvironment()
    drop = ("PYTHON", "GDAL", "PROJ", "OSGEO4W", "O4W_", "QT_", "QGIS", "GEOTIFF", "GISBASE", "GRASS", "VSI_", "SSL_CERT", "CURL_CA")
    for k in list(env.keys()):
        if k.upper().startswith(drop):
            env.remove(k)
    sep = ";" if WIN else ":"
    roots = [r for r in (sys.prefix, os.environ.get("OSGEO4W_ROOT", ""), os.environ.get("QGIS_PREFIX_PATH", "")) if r]
    parts = [p for p in env.value("PATH", "").split(sep)
             if p and not any(os.path.normcase(os.path.abspath(p)).startswith(os.path.normcase(os.path.abspath(r))) for r in roots)]
    env.insert("PATH", sep.join(parts))
    env.insert("PYTHONIOENCODING", "utf-8")
    env.insert("PYTHONUTF8", "1")
    return env


def run_args(aoi_path: str, after: str, before: str, out_dir: str, name: str, sensitivity: str, mmu_ha: float,
             likely_cause: bool, seasonal: bool, terrain: bool, plan: bool = False) -> list[str]:
    a = ["plan" if plan else "run", "--aoi", aoi_path, "--after", after, "--before", before,
         "--sensitivity", sensitivity, "--mmu-ha", f"{mmu_ha:g}"]
    if not likely_cause:
        a.append("--no-likely-cause")
    if plan:
        return a
    a += ["--out", out_dir, "--name", name]
    if seasonal:
        a.append("--seasonal-check")
    if not terrain:
        a.append("--no-terrain")
    return a


class Runner(QObject):
    """One earthdiff process at a time; emits its output line by line."""
    line = pyqtSignal(str)
    finished = pyqtSignal(int)  # exit code; -1 when stopped, -2 when it could not start

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: QProcess | None = None
        self._buf = b""
        self._stopped = False

    def busy(self) -> bool:
        return self.proc is not None

    def start(self, command: list[str], args: list[str]) -> None:
        self._buf, self._stopped = b"", False
        p = QProcess(self)
        p.setProcessEnvironment(clean_environment())
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        p.readyReadStandardOutput.connect(self._read)
        p.finished.connect(self._done)
        p.errorOccurred.connect(self._error)
        self.proc = p
        self.line.emit("$ " + " ".join(_quote(x) for x in command + args))
        p.start(command[0], command[1:] + args)

    def stop(self) -> None:
        if self.proc is not None:
            self._stopped = True
            self.proc.kill()

    def _read(self) -> None:
        if self.proc is None:
            return
        self._buf += bytes(self.proc.readAllStandardOutput())
        *lines, self._buf = self._buf.split(b"\n")
        for ln in lines:
            self.line.emit(ln.decode("utf-8", "replace").rstrip("\r"))

    def _done(self, code, _status=None) -> None:
        self._read()
        if self._buf:
            self.line.emit(self._buf.decode("utf-8", "replace"))
        self._buf = b""
        self.proc = None
        self.finished.emit(-1 if self._stopped else int(code))

    def _error(self, err) -> None:
        if err == QProcess.ProcessError.FailedToStart and self.proc is not None:
            self.line.emit("could not start: " + self.proc.program())
            self.proc = None
            self.finished.emit(-2)


def _quote(s: str) -> str:
    return f'"{s}"' if (" " in s or not s) else s
