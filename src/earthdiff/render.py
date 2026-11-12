"""PNG / WebP rendering: true colour, change overlays, Web-Mercator warping, montage."""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import array_bounds
from rasterio.warp import calculate_default_transform, reproject

from .aoi import Grid
from .change import CLASSES


def _hex(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def true_color(bands: dict[str, np.ndarray], mask: np.ndarray | None = None, vmax: float = 0.20, gamma: float = 0.75) -> np.ndarray:
    """Sentinel-2 true colour (B04,B03,B02) as RGBA uint8. NaN / outside-AOI pixels are transparent."""
    rgb = np.stack([bands["red"], bands["green"], bands["blue"]], axis=-1)
    finite = np.isfinite(rgb).all(axis=-1)
    x = np.clip(np.nan_to_num(rgb, nan=0.0) / vmax, 0.0, 1.0) ** gamma
    out = np.zeros(rgb.shape[:2] + (4,), dtype=np.uint8)
    out[..., :3] = (x * 255).astype(np.uint8)
    alpha = finite if mask is None else (finite & mask)
    out[..., 3] = np.where(alpha, 255, 0).astype(np.uint8)
    return out


def class_rgba(classmap: np.ndarray, alpha: int = 215) -> np.ndarray:
    out = np.zeros(classmap.shape + (4,), dtype=np.uint8)
    for cid, info in CLASSES.items():
        m = classmap == cid
        if m.any():
            out[m, :3] = _hex(info.color)
            out[m, 3] = alpha
    return out


def to_mercator(rgba: np.ndarray, grid: Grid, categorical: bool = False, max_dim: int | None = None):
    """Warp an RGBA raster on ``grid`` to EPSG:3857. Returns (rgba, [[south, west], [north, east]])."""
    h, w = rgba.shape[:2]
    dst_t, dw, dh = calculate_default_transform(grid.crs, "EPSG:3857", w, h, *grid.bounds)
    dst = np.zeros((4, dh, dw), dtype=np.uint8)
    rs = Resampling.nearest if categorical else Resampling.bilinear
    for i in range(4):
        reproject(
            source=np.ascontiguousarray(rgba[..., i]),
            destination=dst[i],
            src_transform=grid.transform,
            src_crs=grid.crs,
            dst_transform=dst_t,
            dst_crs="EPSG:3857",
            resampling=rs,
        )
    out = np.moveaxis(dst, 0, -1)
    west, south, east, north = array_bounds(dh, dw, dst_t)
    t = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    w_lon, s_lat = t.transform(west, south)
    e_lon, n_lat = t.transform(east, north)
    if max_dim and max(out.shape[:2]) > max_dim:
        im = Image.fromarray(out, "RGBA")
        k = max_dim / max(out.shape[:2])
        im = im.resize((max(1, round(out.shape[1] * k)), max(1, round(out.shape[0] * k))), Image.NEAREST if categorical else Image.LANCZOS)
        out = np.asarray(im)
    return np.ascontiguousarray(out), [[s_lat, w_lon], [n_lat, e_lon]]


def save_png(rgba: np.ndarray, path: str | Path) -> None:
    Image.fromarray(rgba, "RGBA").save(path, optimize=True)


def webp_bytes(rgba: np.ndarray, quality: int = 86) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "WEBP", quality=quality, method=4)
    return buf.getvalue()


def png_bytes(rgba: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def _flatten(rgba: np.ndarray, bg=(235, 235, 235)) -> Image.Image:
    base = Image.new("RGBA", (rgba.shape[1], rgba.shape[0]), bg + (255,))
    base.alpha_composite(Image.fromarray(rgba, "RGBA"))
    return base.convert("RGB")


def change_map_image(after_rgba: np.ndarray, classmap: np.ndarray, totals: dict, max_dim: int = 1400) -> Image.Image:
    """After-image (faded) with change classes on top and a legend with hectares."""
    base = Image.fromarray(after_rgba, "RGBA")
    faded = np.asarray(base).copy()
    faded[..., :3] = (faded[..., :3].astype(np.float32) * 0.55 + 255 * 0.45).astype(np.uint8)
    img = _flatten(faded)
    over = Image.fromarray(class_rgba(classmap, 235), "RGBA")
    img = img.convert("RGBA")
    img.alpha_composite(over)
    img = img.convert("RGB")
    k = min(1.0, max_dim / max(img.size))
    if k < 1.0:
        img = img.resize((round(img.width * k), round(img.height * k)), Image.NEAREST)
    d = ImageDraw.Draw(img)
    f = _font(max(13, img.width // 70))
    rows = [(info, totals.get(info.key, {}).get("area_ha", 0.0)) for info in CLASSES.values()]
    lh = f.size + 8
    box_w = int(f.size * 20)
    box_h = lh * len(rows) + 14
    x0, y0 = 10, img.height - box_h - 10
    d.rectangle([x0, y0, x0 + box_w, y0 + box_h], fill=(255, 255, 255), outline=(60, 60, 60))
    for i, (info, ha) in enumerate(rows):
        y = y0 + 8 + i * lh
        d.rectangle([x0 + 8, y, x0 + 8 + f.size, y + f.size], fill=_hex(info.color), outline=(40, 40, 40))
        d.text((x0 + 16 + f.size, y - 1), f"{info.label}: {ha:,.1f} ha", fill=(20, 20, 20), font=f)
    return img


def montage(before_rgba: np.ndarray, after_rgba: np.ndarray, change_img: Image.Image, titles: tuple[str, str, str], panel: int = 620) -> Image.Image:
    panels = [_flatten(before_rgba), _flatten(after_rgba), change_img.convert("RGB")]
    resized = []
    for p in panels:
        k = panel / p.width
        resized.append(p.resize((panel, max(1, round(p.height * k))), Image.LANCZOS))
    hmax = max(p.height for p in resized)
    cap = 36
    sheet = Image.new("RGB", (panel * 3 + 8 * 4, hmax + cap + 16), (255, 255, 255))
    d = ImageDraw.Draw(sheet)
    f = _font(20)
    for i, (p, t) in enumerate(zip(resized, titles)):
        x = 8 + i * (panel + 8)
        sheet.paste(p, (x, cap + 8))
        d.text((x + 4, 8), t, fill=(20, 20, 20), font=f)
    return sheet
