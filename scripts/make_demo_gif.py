"""Build the README / social demo GIF and the square image from REAL tool outputs.

Inputs are the files written by  python validation/run_mcp_questions.py  (validation/mcp/<case>/): the exact JPEGs the MCP client
received (before, after, change map) and detect_change.json. Nothing is drawn by hand except the frame around them.

    python scripts/make_demo_gif.py            # docs/images/earthdiff-mcp-demo.gif + earthdiff-mcp-square.png
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
VAL = ROOT / "validation" / "mcp"
OUT = ROOT / "docs" / "images"
W = H = 1080
FPS = 15
NAVY_TOP, NAVY_BOTTOM = (11, 22, 48), (20, 44, 86)
GOLD, BLUE, WHITE, GREY, GREEN, AMBER = (227, 176, 75), (142, 197, 255), (240, 244, 250), (150, 165, 190), (120, 220, 150), (255, 190, 90)

CASES = [
    ("manavgat", "What changed around Manavgat after the July 2021 wildfire?", ['find_place("Manavgat")', "plan_change(bbox, 2021-08/09)", "detect_change(bbox, 2021-08/09)"]),
    ("lake_mead", "How did Lake Mead change between July 2019 and July 2022?", ['find_place("Lake Mead")', "plan_change(bbox, 2022-07)", "detect_change(bbox, 2022-07)"]),
]


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for name in (("segoeuib.ttf" if bold else "segoeui.ttf"), ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


MONO = None
for _n in ("consola.ttf", "DejaVuSansMono.ttf"):
    try:
        MONO = ImageFont.truetype(_n, 30)
        break
    except OSError:
        pass
MONO = MONO or font(30)


def background() -> Image.Image:
    bg = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(bg)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(NAVY_TOP, NAVY_BOTTOM)))
    for i in range(7):
        r = 260 + i * 95
        d.ellipse([W - 160 - r, -120 - r // 2, W - 160 + r, -120 + r], outline=(36, 62, 108), width=2)
    return bg


BG = background()


def ease(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def footer(d: ImageDraw.ImageDraw) -> None:
    d.text((60, H - 64), "github.com/mveyseldelibas/earthdiff", font=font(28), fill=GREY)
    d.text((W - 60, H - 64), "earthdiff", font=font(28, True), fill=GOLD, anchor="ra")


def wrap(d, text, fnt, width):
    lines, line = [], ""
    for w in text.split():
        cand = (line + " " + w).strip()
        if d.textlength(cand, font=fnt) > width:
            lines.append(line)
            line = w
        else:
            line = cand
    return lines + [line]


def bubble(d, y, who, text, color, fnt):
    d.text((60, y), who, font=font(26, True), fill=GREY)
    lines = wrap(d, text, fnt, W - 180)
    h = 30 + 46 * len(lines)
    d.rounded_rectangle([60, y + 38, W - 60, y + 38 + h], radius=22, fill=(30, 52, 92), outline=color, width=2)
    for i, ln in enumerate(lines):
        d.text((90, y + 52 + i * 46), ln, font=fnt, fill=WHITE)
    return y + 38 + h


ANCHOR = {"manavgat": 1.0, "lake_mead": 0.5}  # which part of a tall image to show: 0 = top, 1 = bottom


def triptych(case: str, max_h: int) -> Image.Image:
    """Before / after / change map side by side, each cropped to ``max_h`` px (the returned JPEGs keep their caption strip)."""
    gap, wpanel = 8, (W - 120 - 16) // 3
    panels = []
    for n in ("before", "after", "change_map"):
        im = Image.open(VAL / case / f"{n}.jpg").convert("RGB")
        strip = max(14, im.width // 45) + 14
        cap, body = im.crop((0, 0, im.width, strip)), im.crop((0, strip, im.width, im.height))
        k = wpanel / im.width
        cap = cap.resize((wpanel, max(1, round(cap.height * k))), Image.LANCZOS)
        body = body.resize((wpanel, max(1, round(body.height * k))), Image.LANCZOS)
        room = max_h - cap.height
        if body.height > room:
            top = round((body.height - room) * ANCHOR.get(case, 0.5))
            body = body.crop((0, top, wpanel, top + room))
        panel = Image.new("RGB", (wpanel, cap.height + body.height), (255, 255, 255))
        panel.paste(cap, (0, 0))
        panel.paste(body, (0, cap.height))
        panels.append(panel)
    sheet = Image.new("RGB", (W - 120, max(p.height for p in panels)), (255, 255, 255))
    for k, p in enumerate(panels):
        sheet.paste(p, (k * (wpanel + gap), 0))
    return sheet


def summary_line(case: str) -> str:
    r = json.loads((VAL / case / "detect_change.json").read_text(encoding="utf-8"))
    top = max(r["changes"], key=lambda c: c["area_ha"])
    return f"{top['area_ha']:,.0f} ha {top['label'].lower()} | {r['comparable_fraction'] * 100:.0f}% comparable | confidence {r['confidence']['level']}"


def title_frame(t: float) -> Image.Image:
    im = BG.copy()
    d = ImageDraw.Draw(im)
    d.text((W // 2, 300), "earthdiff", font=font(110, True), fill=tuple(int(c * ease(t * 2)) for c in GOLD), anchor="mm")
    for i, ln in enumerate(["Satellite eyes for your", "AI assistant"]):
        d.text((W // 2, 470 + i * 78), ln, font=font(60), fill=tuple(int(c * ease(t * 2 - 0.4)) for c in WHITE), anchor="mm")
    d.text((W // 2, 700), "Free Sentinel-2 | no account | MCP", font=font(40), fill=tuple(int(c * ease(t * 2 - 0.8)) for c in BLUE), anchor="mm")
    footer(d)
    return im


def scene_frame(case: str, question: str, calls: list[str], t: float) -> Image.Image:
    im = BG.copy()
    d = ImageDraw.Draw(im)
    n = int(len(question) * ease(t / 0.3))
    y = bubble(d, 50, "You", question[:n] + ("|" if t < 0.3 else ""), BLUE, font(36)) + 24
    d.text((60, y), "Assistant -> earthdiff", font=font(26, True), fill=GREY)
    y += 40
    for i, c in enumerate(calls):
        if t > 0.32 + i * 0.07:
            d.rounded_rectangle([60, y, 60 + d.textlength(c, font=MONO) + 40, y + 50], radius=12, fill=(18, 34, 64), outline=GOLD, width=2)
            d.text((80, y + 9), c, font=MONO, fill=GOLD)
            y += 60
    a = ease((t - 0.55) / 0.2)
    if a > 0:
        x, y = 60, y + 12
        tri = triptych(case, H - y - 130)
        im.paste(Image.blend(im.crop((x, y, x + tri.width, y + tri.height)), tri, a), (x, y))
        y += tri.height + 14
        d.text((W // 2, y), summary_line(case), font=font(32, True), fill=tuple(int(c * a) for c in GREEN), anchor="ma")
    footer(d)
    return im


def limits_frame(t: float) -> Image.Image:
    im = BG.copy()
    d = ImageDraw.Draw(im)
    d.text((W // 2, 150), "Honest by design", font=font(70, True), fill=GOLD, anchor="mm")
    rows = [
        "Every warning is passed on: season mismatch, clouds,",
        "a single scene, implausibly large change.",
        "",
        "Plain spectral rules you can read. No black box.",
        "Describes what changed, never why.",
        "",
        "A screening tool, not an official damage assessment.",
    ]
    for i, ln in enumerate(rows):
        a = ease(t * 3 - i * 0.25)
        d.text((W // 2, 300 + i * 70), ln, font=font(40), fill=tuple(int(c * a) for c in (AMBER if "screening" in ln else WHITE)), anchor="mm")
    footer(d)
    return im


def end_frame(t: float) -> Image.Image:
    im = BG.copy()
    d = ImageDraw.Draw(im)
    d.text((W // 2, 340), "earthdiff", font=font(100, True), fill=GOLD, anchor="mm")
    d.text((W // 2, 470), 'uvx --from "earthdiff[mcp]" earthdiff-mcp', font=MONO, fill=WHITE, anchor="mm")
    d.text((W // 2, 560), "Open source | MIT | Claude, Cursor, any MCP client", font=font(36), fill=BLUE, anchor="mm")
    d.text((W // 2, 640), "Contains modified Copernicus Sentinel data", font=font(30), fill=GREY, anchor="mm")
    footer(d)
    return im


def timeline():
    yield (lambda t: title_frame(1.0), 0.4)
    yield (title_frame, 2.5)
    for case, q, calls in CASES:
        yield ((lambda t, c=case, qq=q, cc=calls: scene_frame(c, qq, cc, min(1.0, t * 1.25))), 7.0)
    yield (limits_frame, 5.0)
    yield (end_frame, 3.5)


def frames(size: int = W):
    for fn, secs in timeline():
        n = int(secs * FPS)
        for i in range(n):
            im = fn(i / max(1, n - 1))
            yield im if size == W else im.resize((size, size), Image.LANCZOS)


def result_strip(case: str, height: int = 760) -> Image.Image:
    """The three images exactly as the client receives them, side by side (for the README)."""
    ims = [Image.open(VAL / case / f"{n}.jpg").convert("RGB") for n in ("before", "after", "change_map")]
    ims = [i.resize((round(i.width * height / i.height), height), Image.LANCZOS) for i in ims]
    sheet = Image.new("RGB", (sum(i.width for i in ims) + 8 * (len(ims) - 1), height), (255, 255, 255))
    x = 0
    for i in ims:
        sheet.paste(i, (x, 0))
        x += i.width + 8
    return sheet


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for case in ("manavgat", "lake_mead"):
        p = OUT / f"earthdiff-mcp-{case}.jpg"
        result_strip(case).save(p, quality=88, optimize=True)
        print("strip", p, round(p.stat().st_size / 1e3), "KB")
    gif = [im.quantize(colors=128, method=Image.Quantize.MEDIANCUT) for i, im in enumerate(frames(720)) if i % 3 == 0]
    path = OUT / "earthdiff-mcp-demo.gif"
    gif[0].save(path, save_all=True, append_images=gif[1:], duration=int(1000 * 3 / FPS), loop=0, optimize=True)
    print("gif", path, round(path.stat().st_size / 1e6, 2), "MB", len(gif), "frames")
    square = scene_frame(CASES[0][0], CASES[0][1], CASES[0][2], 1.0)
    sq = OUT / "earthdiff-mcp-square.png"
    square.save(sq, optimize=True)
    print("square", sq, round(sq.stat().st_size / 1e6, 2), "MB")


if __name__ == "__main__":
    main()
