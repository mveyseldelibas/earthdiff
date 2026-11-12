"""Builds the QGIS plugin zip (qgis/earthdiff_qgis -> dist/earthdiff_qgis-<version>.zip) for 'Install from ZIP' and plugins.qgis.org.

    python scripts/build_qgis_plugin.py
"""
import configparser
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "qgis" / "earthdiff_qgis"


def main() -> Path:
    meta = configparser.ConfigParser()
    meta.read(SRC / "metadata.txt", encoding="utf-8")
    version = meta["general"]["version"]
    out = ROOT / "dist" / f"earthdiff_qgis-{version}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(SRC.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
                z.write(f, f"earthdiff_qgis/{f.relative_to(SRC).as_posix()}")
    print(out, out.stat().st_size // 1024, "KB")
    return out


if __name__ == "__main__":
    main()
