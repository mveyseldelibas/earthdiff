"""Test launcher: the real MCP server over stdio, with fake STAC / raster / Nominatim data sources injected.

Run by tests/test_mcp_stdio.py as a subprocess:  python tests/mcp_fake_server.py
Environment: EARTHDIFF_OUTPUT_DIR (output folder), FAKE_MODE = ok | slow | evil | empty | crash | stacerr | fire | clear | salt | seasonal.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakes  # noqa: E402
from earthdiff.mcp_server import create_server  # noqa: E402
from earthdiff.mcp_service import EarthDiffService, Settings  # noqa: E402


def stac_error(*a, **k):
    from earthdiff.stac import STACError

    raise STACError(r"STAC search failed: cannot write C:\Users\bob\cache and /home/bob/cache")


def main() -> None:
    mode = os.environ.get("FAKE_MODE", "ok")
    settings = Settings.from_env()
    search = {"evil": fakes.evil_search, "empty": fakes.empty_search, "stacerr": stac_error}.get(mode, fakes.fake_search)
    reader = {
        "fire": lambda: fakes.make_reader(kind="fire"),
        "clear": lambda: fakes.make_reader(kind="clear"),
        "salt": lambda: fakes.make_reader(salt=True),
        "seasonal": lambda: fakes.make_reader(change_year=None, repeat_month=8),
    }.get(mode, fakes.make_reader)()
    if mode == "crash":
        good = reader

        def reader(scene, band, grid):  # noqa: F811
            if band == "scl":
                return good(scene, band, grid)
            raise OSError(r"cannot open C:\Users\bob\secret\scene.tif")
    if mode == "slow":
        inner = reader

        def reader(scene, band, grid):  # noqa: F811
            time.sleep(0.4)
            return inner(scene, band, grid)

    svc = EarthDiffService(settings, search=search, reader=reader, geocoder=fakes.default_geocoder())
    create_server(settings, svc).run("stdio")


if __name__ == "__main__":
    main()
