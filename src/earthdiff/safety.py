"""Input hygiene, output-folder confinement, error redaction and the network allow-list of the MCP server.

Everything a model (or an OSM / STAC response) can put into a file name, a folder name or a message goes through here.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# C0/C1 control characters, zero-width and bidi override characters, line/paragraph separators
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]")
_TURKISH = str.maketrans({"ı": "i", "İ": "I", "ş": "s", "Ş": "S", "ğ": "g", "Ğ": "G", "ç": "c", "Ç": "C", "ö": "o", "Ö": "O", "ü": "u", "Ü": "U"})

# Hosts the server may talk to: STAC APIs, Sentinel-2 COG storage and the Nominatim place search. Nothing else.
STAC_HOSTS = ("earth-search.aws.element84.com", "planetarycomputer.microsoft.com")
DATA_HOSTS = ("sentinel-cogs.s3.us-west-2.amazonaws.com", "*.blob.core.windows.net")
NOMINATIM_HOST = "nominatim.openstreetmap.org"


def clean_text(value: object, max_len: int = 200) -> str:
    """Printable single-line text: control and bidi characters removed, whitespace collapsed, length capped."""
    s = unicodedata.normalize("NFC", str(value))
    s = _CONTROL.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len]


def safe_filename(stem: str, max_len: int = 48) -> str:
    """ASCII slug that is safe as a file or folder name on Windows, macOS and Linux (no separators, no dots at the ends)."""
    s = str(stem).translate(_TURKISH)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_-")[:max_len].strip("_-")
    return s or "area"


def run_folder(root: Path, name: str, now: datetime | None = None) -> Path:
    """Create and return ``<root>/<YYYYmmdd-HHMMSS>_<slug>``; refuses anything that would resolve outside ``root``."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve()
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}_{safe_filename(name)}"
    for n in range(1, 100):
        candidate = root_resolved / (base if n == 1 else f"{base}-{n}")
        if candidate.parent != root_resolved:  # defence in depth: the slug cannot contain separators
            raise ValueError("invalid output folder name")
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise ValueError("could not create a unique output folder")


# ----------------------------------------------------------------------------------------------- redaction
_WIN_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s'\"<>|*?]*")
_UNC_PATH = re.compile(r"\\\\[^\s'\"<>|*?]+")
_POSIX_PATH = re.compile(r"(?<![\w:/.])/(?:[\w.\-~]+/)+[\w.\-~]*")
_MAX_ERROR = 600


def redact(text: object, extra: tuple[str, ...] = ()) -> str:
    """Remove local file-system paths (and the given extra literals such as the home or output folder) from a message."""
    s = str(text)
    s = _UNC_PATH.sub("<path>", s)
    s = _WIN_PATH.sub("<path>", s)
    s = _POSIX_PATH.sub("<path>", s)
    for lit in sorted({e for e in extra if e and len(e) >= 3}, key=len, reverse=True):
        s = s.replace(lit, "<path>").replace(lit.replace("\\", "/"), "<path>")
    s = _CONTROL.sub(" ", s)
    return s[:_MAX_ERROR]


# ----------------------------------------------------------------------------------------------- network allow-list
def host_allowed(url: str, patterns: tuple[str, ...]) -> bool:
    """True when ``url`` is https and its host matches an exact name or a ``*.suffix`` pattern."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if parsed.scheme != "https" or not host:
        return False
    for pat in patterns:
        if pat.startswith("*."):
            if host.endswith(pat[1:]) and len(host) > len(pat) - 1:
                return True
        elif host == pat:
            return True
    return False
