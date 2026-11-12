"""Tiny local web UI. Standard library only; binds to 127.0.0.1 and nothing else."""
from __future__ import annotations

import json
import mimetypes
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__
from .aoi import MAX_AREA_KM2, AOIError, geometry_from_geojson
from .periods import parse_period
from .pipeline import InsufficientDataError, RunOptions, run_analysis
from .stac import STACError

HOST = "127.0.0.1"
MAX_BODY = 2_000_000
# Largest area one UI run may cover. Same as the MCP server's hard maximum; the pipeline picks a coarser grid
# (20/30/60 m) for large areas so the window stays under ~4 M pixels. The page reads this value, so both checks agree.
UI_MAX_AREA_KM2 = MAX_AREA_KM2
class Job:
    def __init__(self, name: str):
        self.id = uuid.uuid4().hex[:10]
        self.name = name
        self.status = "running"  # running | done | error
        self.log: list[str] = []
        self.error: str | None = None
        self.summary: dict[str, Any] | None = None
        self.started = time.time()

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "status": self.status, "log": self.log[-60:], "error": self.error,
            "summary": self.summary, "report_url": f"/runs/{self.id}/report.html" if self.status == "done" else None,
            "elapsed_s": round(time.time() - self.started),
        }


class State:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def running(self) -> bool:
        return any(j.status == "running" for j in self.jobs.values())


def _read_pkg(rel: str) -> bytes:
    return resources.files("earthdiff").joinpath(rel).read_bytes()


def page() -> bytes:
    """The UI page with the server's area limit filled in."""
    return _read_pkg("static/ui.html").replace(b"__MAX_AREA_KM2__", str(int(UI_MAX_AREA_KM2)).encode())


# ---- drawing page: a relief basemap for the drawing map
# Relief tiles are shaded from the open AWS Terrain Tiles (Terrarium encoding); the server does it because those tiles
# carry no CORS header, so the page could not read their pixels itself.
RELIEF_SRC = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
RELIEF_MAX_ZOOM = 15
_relief_cache: dict[tuple[int, int, int], bytes] = {}


def relief_tile(z: int, x: int, y: int, fetch=None) -> bytes | None:
    """A grey hillshade PNG for web-map tile z/x/y (256 px), or None when the elevation tile cannot be read."""
    import io
    import math
    import urllib.request

    import numpy as np
    from PIL import Image

    from .terrain import hillshade

    if not (0 <= z <= RELIEF_MAX_ZOOM and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        return None
    key = (z, x, y)
    if key in _relief_cache:
        return _relief_cache[key]
    url = RELIEF_SRC.format(z=z, x=x, y=y)
    try:
        if fetch:
            raw = fetch(url)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": f"earthdiff/{__version__}"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
        rgb = np.asarray(Image.open(io.BytesIO(raw)).convert("RGB"), dtype=np.float64)
    except Exception:
        return None
    dem = rgb[..., 0] * 256 + rgb[..., 1] + rgb[..., 2] / 256 - 32768
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / 2 ** z))))
    px_m = 40075016.686 * math.cos(math.radians(lat)) / (256 * 2 ** z)
    exag = 1 + max(0, 13 - z) * 0.6  # small-scale tiles need taller relief to show it at all
    shade = hillshade(dem * exag, max(px_m, 1.0))
    grey = (60 + shade * 195).astype(np.uint8)
    grey[dem <= 0] = 205  # sea and flat lowland at sea level: plain light grey
    buf = io.BytesIO()
    Image.fromarray(grey, "L").save(buf, "PNG", optimize=True)
    if len(_relief_cache) > 1024:
        _relief_cache.clear()
    _relief_cache[key] = buf.getvalue()
    return _relief_cache[key]


def _execute(state: State, job: Job, payload: dict[str, Any]) -> None:
    try:
        geom = geometry_from_geojson(payload["geometry"])
        after = parse_period(str(payload["after"]))
        before = parse_period(str(payload["before"])) if payload.get("before") else after.shift_years(-1)
        mmu = float(payload.get("mmu_ha") or 0.5)
        if not 0.1 <= mmu <= 50:
            raise ValueError("smallest change (mmu_ha) must be between 0.1 and 50 ha")
        opts = RunOptions(name=job.name, sensitivity=payload.get("sensitivity", "default"), mmu_ha=mmu, max_area_km2=UI_MAX_AREA_KM2)
        res = run_analysis(geom, before, after, state.workdir / job.id, opts, progress=job.log.append)
        job.summary = res.summary
        job.status = "done"
    except (AOIError, InsufficientDataError, STACError, ValueError, KeyError) as exc:
        job.error, job.status = str(exc) or exc.__class__.__name__, "error"
    except Exception as exc:  # keep the server alive whatever happens in a job
        job.error, job.status = f"unexpected error: {exc!r}", "error"


def make_handler(state: State, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = f"earthdiff/{__version__}"

        def log_message(self, fmt, *args):  # quiet
            pass

        # -- helpers
        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts  # blocks DNS-rebinding

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"))

        # -- routes
        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._send(200, page(), "text/html; charset=utf-8")
            if path in ("/vendor/leaflet.js", "/vendor/leaflet.css"):
                ctype = "application/javascript" if path.endswith("js") else "text/css"
                return self._send(200, _read_pkg(path.lstrip("/")), ctype)
            if path.startswith("/tiles/relief/") and path.endswith(".png"):
                try:
                    z, x, y = (int(v) for v in path[len("/tiles/relief/"):-4].split("/"))
                except ValueError:
                    return self._json(400, {"error": "bad tile"})
                png = relief_tile(z, x, y)
                return self._send(200, png, "image/png") if png else self._json(404, {"error": "no elevation tile"})
            if path.startswith("/api/jobs/"):
                job = state.jobs.get(path.rsplit("/", 1)[1])
                return self._json(200, job.public()) if job else self._json(404, {"error": "unknown job"})
            if path.startswith("/runs/"):
                parts = path.split("/")
                if len(parts) == 4 and parts[2] in state.jobs and parts[3] and "/" not in parts[3] and ".." not in parts[3]:
                    f = (state.workdir / parts[2] / parts[3]).resolve()
                    if f.is_file() and state.workdir.resolve() in f.parents:
                        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                        if f.suffix == ".html":
                            ctype = "text/html; charset=utf-8"
                        return self._send(200, f.read_bytes(), ctype)
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            origin = self.headers.get("Origin")
            if origin and origin.replace("http://", "") not in allowed_hosts:
                return self._json(403, {"error": "bad origin"})
            if self.path != "/api/run" or "application/json" not in self.headers.get("Content-Type", ""):
                return self._json(400, {"error": "expected JSON POST /api/run"})
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._json(400, {"error": "bad body size"})
            try:
                payload = json.loads(self.rfile.read(n))
                assert isinstance(payload, dict) and isinstance(payload.get("geometry"), dict) and payload.get("after")
            except Exception:
                return self._json(400, {"error": "payload needs geometry (GeoJSON) and after (YYYY-MM)"})
            with state.lock:
                if state.running():
                    return self._json(409, {"error": "another run is in progress; wait for it to finish"})
                job = Job(str(payload.get("name") or "Drawn area")[:80])
                state.jobs[job.id] = job
            threading.Thread(target=_execute, args=(state, job, payload), daemon=True).start()
            self._json(202, {"id": job.id})

    return Handler


def serve(port: int = 8765, workdir: Path = Path("earthdiff-runs"), open_browser: bool = True) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    state = State(workdir)
    httpd = ThreadingHTTPServer((HOST, port), make_handler(state, port))
    url = f"http://{HOST}:{port}/"
    print(f"earthdiff UI on {url}  (local only; runs stored in {workdir.resolve()})  Ctrl+C to stop")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
