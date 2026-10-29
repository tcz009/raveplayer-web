#!/usr/bin/env python3
"""Build every Rave Player artwork asset from code.

Run with a Python that has Pillow (build-time only; Pillow is deliberately
not a runtime dependency):

    /path/to/python3 scripts/make-art.py

Nothing here downloads anything.  Every mark, texture and gradient is drawn
with Pillow primitives so the whole set can be rebuilt and reviewed.
"""

from __future__ import annotations

import argparse
import math
import os
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent.parent
ART = ROOT / "static" / "art"
SS = 2  # supersample factor for the 512 px tiles

# --------------------------------------------------------------------------- palette
INK = (10, 10, 15)
INK_2 = (16, 16, 23)
INK_3 = (24, 24, 33)
PAPER = (242, 238, 227)
PAPER_DIM = (207, 201, 190)
ACID = (232, 255, 0)
MAGENTA = (255, 45, 143)
CYAN = (34, 230, 255)
ORANGE = (255, 106, 26)

FONT_CANDIDATES = {
    "display": [
        "/System/Library/Fonts/Supplemental/Impact.ttf",
        "/Library/Fonts/Impact.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    ],
    "mono": [
        "/System/Library/Fonts/SFNSMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    ],
}

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(kind: str, size: int):
    key = (kind, size)
    if key in _font_cache:
        return _font_cache[key]
    for path in FONT_CANDIDATES[kind]:
        if os.path.exists(path):
            try:
                face = ImageFont.truetype(path, size)
                _font_cache[key] = face
                return face
            except OSError:
                continue
    face = ImageFont.load_default(size=size) if hasattr(ImageFont, "load_default") else ImageFont.load_default()
    _font_cache[key] = face
    return face


def with_alpha(color, alpha):
    return (color[0], color[1], color[2], max(0, min(255, int(alpha))))


# --------------------------------------------------------------------------- drawing helpers


class Pen:
    """A very small wrapper that accepts 512-space coordinates.

    Every tile is drawn at SS times the final size, so coordinates, widths and
    fonts are scaled here instead of at every call site.
    """

    def __init__(self, draw: ImageDraw.ImageDraw, scale: int = SS):
        self.d = draw
        self.s = scale

    def _box(self, box):
        return [int(round(v * self.s)) for v in box]

    def _width(self, width):
        return max(1, int(round(width * self.s)))

    def ellipse(self, box, fill=None, outline=None, width=0):
        self.d.ellipse(self._box(box), fill=fill, outline=outline, width=self._width(width) if width else 0)

    def rect(self, box, fill=None, outline=None, width=0, radius=0):
        if radius:
            self.d.rounded_rectangle(self._box(box), radius=self._width(radius), fill=fill,
                                     outline=outline, width=self._width(width) if width else 0)
        else:
            self.d.rectangle(self._box(box), fill=fill, outline=outline,
                             width=self._width(width) if width else 0)

    def rrect(self, box, radius, fill=None, outline=None, width=0):
        self.d.rounded_rectangle(self._box(box), radius=self._width(radius), fill=fill,
                                 outline=outline, width=self._width(width) if width else 0)

    def poly(self, points, fill=None, outline=None, width=0):
        pts = [(int(round(x * self.s)), int(round(y * self.s))) for x, y in points]
        self.d.polygon(pts, fill=fill)
        if outline and width:
            self.d.line(pts + [pts[0]], fill=outline, width=self._width(width), joint="curve")

    def line(self, points, fill, width=1, joint="curve"):
        pts = [(int(round(x * self.s)), int(round(y * self.s))) for x, y in points]
        self.d.line(pts, fill=fill, width=self._width(width), joint=joint)

    def arc(self, box, start, end, fill, width=1):
        self.d.arc(self._box(box), start=start, end=end, fill=fill, width=self._width(width))

    def text(self, xy, text, face, fill, anchor="la", spacing=0):
        self.d.text((int(round(xy[0] * self.s)), int(round(xy[1] * self.s))),
                    text, font=face, fill=fill, anchor=anchor, spacing=spacing)


def bezier(points, steps: int = 18):
    """Sample a chain of cubic Béziers: [p0, c1, c2, p1, c1, c2, p2, ...]."""
    out = [points[0]]
    i = 1
    while i + 2 < len(points):
        p0 = out[-1]
        p1, p2, p3 = points[i], points[i + 1], points[i + 2]
        for k in range(1, steps + 1):
            t = k / steps
            mt = 1.0 - t
            out.append((
                mt ** 3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t ** 3 * p3[0],
                mt ** 3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t ** 3 * p3[1],
            ))
        i += 3
    return out


def radial_glow(size, center: tuple[int, int], radius: int, color, peak: int):
    """A soft, round light in RGBA.  ``size`` is an int or a (w, h) tuple."""
    width, height = (size, size) if isinstance(size, int) else size
    diameter = max(8, radius * 2)
    small = Image.radial_gradient("L").resize((diameter, diameter), Image.Resampling.BILINEAR)
    small = ImageOps.invert(small).point(lambda value: int(value * peak / 255))
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    disc = Image.new("RGBA", (diameter, diameter), with_alpha(color, 255))
    layer.paste(disc, (int(center[0] - radius), int(center[1] - radius)), small)
    return layer


def halftone(size, color, spacing: int = 18, peak_alpha: int = 54,
             light=(0.78, 0.12), base: float = 0.0, min_radius: float = 0.7,
             max_radius: float = 3.0):
    """A printed dot screen that fades away from a light source."""
    width, height = (size, size) if isinstance(size, int) else size
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    lx, ly = light[0] * width, light[1] * height
    diagonal = 0.5 + 0.5 * math.sin((0.55 * light[0] + 0.45) * math.pi)
    for y in range(0, height, spacing):
        for x in range(0, width, spacing):
            distance = math.hypot(x - lx, y - ly) / (max(width, height) * 0.78)
            t = max(0.0, min(1.0, 1.0 - distance))
            t = (t ** 1.5) * (0.65 + 0.35 * diagonal)
            alpha = int(peak_alpha * (base + (1.0 - base) * t))
            if alpha <= 2:
                continue
            radius = min_radius + (max_radius - min_radius) * (0.25 + 0.75 * t)
            draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                         fill=with_alpha(color, alpha))
    return layer


def diagonal_hatch(size, color, spacing: int = 26, alpha: int = 10, width: int = 1):
    width_px, height_px = (size, size) if isinstance(size, int) else size
    layer = Image.new("RGBA", (width_px, height_px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for offset in range(-height_px, width_px + height_px, spacing):
        draw.line([(offset, 0), (offset + height_px, height_px)],
                  fill=with_alpha(color, alpha), width=max(1, int(width * SS)))
    return layer


def finish(img: Image.Image) -> Image.Image:
    return img.convert("RGB")


def save_png(img: Image.Image, path: Path, colors: int = 256):
    """Palette PNG: keeps the flat spot colours small without banding the art."""
    rgb = img.convert("RGB")
    if colors:
        rgb = rgb.quantize(colors=colors, method=Image.Quantize.MEDIANCUT,
                           dither=Image.Dither.FLOYDSTEINBERG)
    rgb.save(path, "PNG", optimize=True)


# --------------------------------------------------------------------------- glyphs


def glyph_smiley(p: Pen, cx=256, cy=242, r=148, fill=ACID, shadow=MAGENTA, wink=False):
    p.ellipse((cx - r + 14, cy - r + 16, cx + r + 14, cy + r + 16), fill=shadow)
    p.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill, outline=INK, width=7)
    # eyes
    p.ellipse((cx - 74, cy - 54, cx - 36, cy - 8), fill=INK)
    if wink:
        p.poly([(cx + 36, cy - 54), (cx + 36, cy - 8), (cx + 82, cy - 31)], fill=INK)
    else:
        p.ellipse((cx + 36, cy - 54, cx + 74, cy - 8), fill=INK)
    # smile, with round ends so it survives a 22 px downscale
    box = (cx - 92, cy - 46, cx + 92, cy + 104)
    p.arc(box, 22, 158, INK, width=24)
    for angle in (22, 158):
        a = math.radians(angle)
        ex = cx + 92 * math.cos(a)
        ey = cy + 29 + 75 * math.sin(a)
        p.ellipse((ex - 12, ey - 12, ex + 12, ey + 12), fill=INK)


def glyph_tower(p: Pen, accent=CYAN, signal=MAGENTA):
    def tower(color, dx=0, dy=0, width=16):
        p.line([(160 + dx, 412 + dy), (238 + dx, 150 + dy)], fill=color, width=width)
        p.line([(352 + dx, 412 + dy), (274 + dx, 150 + dy)], fill=color, width=width)
        for y in (180, 228, 276, 324, 372):
            t = (y - 150) / (412 - 150)
            left = 238 + (160 - 238) * t
            right = 274 + (352 - 274) * t
            p.line([(left + dx, y + dy), (right + dx, y + dy)], fill=color, width=8)
        # X bracing in the three visible bays
        for top, bottom in ((180, 228), (228, 276), (276, 324), (324, 372)):
            t1 = (top - 150) / (412 - 150)
            t2 = (bottom - 150) / (412 - 150)
            l1, r1 = 238 + (160 - 238) * t1, 274 + (352 - 274) * t1
            l2, r2 = 238 + (160 - 238) * t2, 274 + (352 - 274) * t2
            p.line([(l1 + dx, top + dy), (r2 + dx, bottom + dy)], fill=color, width=5)
            p.line([(r1 + dx, top + dy), (l2 + dx, bottom + dy)], fill=color, width=5)

    # shadow pass, then the chalk-white steel
    tower(with_alpha(MAGENTA, 230), 13, 15, 18)
    tower(PAPER, 0, 0, 16)
    p.line([(256, 150), (256, 96)], fill=PAPER, width=12)
    p.poly([(256, 78), (272, 108), (240, 108)], fill=PAPER)
    p.ellipse((244, 62, 268, 86), fill=signal, outline=INK, width=4)
    # transmitter arcs
    for radius in (66, 100, 134):
        p.arc((256 - radius, 150 - radius, 256 + radius, 150 + radius), 208, 332,
              with_alpha(accent, 215), width=12)
    # base and frequency ticks
    p.rect((142, 410, 370, 434), fill=ACID, outline=INK, width=6)
    for x in range(166, 356, 28):
        p.line([(x, 416), (x, 428)], fill=INK, width=5)


def glyph_cassette(p: Pen):
    p.rrect((98, 154, 418, 364), 26, fill=MAGENTA)
    p.rrect((84, 140, 404, 350), 26, fill=PAPER, outline=INK, width=7)
    # label
    p.rrect((108, 158, 380, 208), 10, fill=ACID)
    p.text((120, 168), "RAVE", font("display", 34 * SS), INK)
    p.text((368, 170), "C90", font("mono", 22 * SS), INK, anchor="ra")
    # window and reels
    p.rrect((124, 220, 364, 306), 14, fill=INK)
    p.line([(200, 263), (288, 263)], fill=CYAN, width=5)
    for cx in (190, 298):
        p.ellipse((cx - 38, 225, cx + 38, 301), fill=CYAN, outline=INK, width=5)
        p.ellipse((cx - 12, 251, cx + 12, 275), fill=INK)
        for angle in range(0, 360, 60):
            a = math.radians(angle)
            p.line([(cx + 12 * math.cos(a), 263 + 12 * math.sin(a)),
                    (cx + 27 * math.cos(a), 263 + 27 * math.sin(a))], fill=INK, width=3)
    # shell details
    for cx, cy in ((108, 158), (380, 158), (108, 336), (380, 336)):
        p.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=INK)
    p.ellipse((226, 326, 242, 342), fill=INK)
    p.ellipse((270, 326, 286, 342), fill=INK)


def glyph_mask(p: Pen):
    shell = [(148, 126), (364, 126), (342, 292), (292, 378), (256, 432), (220, 378), (170, 292)]
    p.poly([(x + 13, y + 15) for x, y in shell], fill=MAGENTA)
    p.poly(shell, fill=INK_3, outline=PAPER, width=11)
    # forehead marks
    p.poly([(256, 150), (278, 176), (256, 202), (234, 176)], fill=ACID)
    p.poly([(256, 160), (268, 176), (256, 192), (244, 176)], fill=INK)
    # slit eyes
    p.poly([(192, 210), (240, 200), (236, 240), (198, 248)], fill=ACID, outline=INK, width=5)
    p.poly([(320, 210), (272, 200), (276, 240), (314, 248)], fill=ACID, outline=INK, width=5)
    p.line([(190, 194), (244, 184)], fill=MAGENTA, width=7)
    p.line([(322, 194), (268, 184)], fill=MAGENTA, width=7)
    # nose
    p.poly([(256, 248), (230, 306), (282, 306)], fill=None, outline=PAPER, width=7)
    # mouth and teeth
    p.rrect((212, 326, 300, 366), 6, fill=INK, outline=PAPER, width=5)
    for x in range(222, 296, 15):
        p.rect((x, 330, x + 8, 362), fill=PAPER)
    # tribal cheek striping
    for side in (-1, 1):
        for i in range(3):
            x = 206 if side < 0 else 306
            y = 286 + i * 18
            p.line([(x - 14 * side, y), (x + 10 * side, y + 8)], fill=MAGENTA, width=6)


def glyph_bolt(p: Pen):
    shape = [(302, 86), (170, 284), (246, 284), (212, 426), (348, 230), (272, 230), (318, 86)]
    p.poly([(x + 13, y + 15) for x, y in shape], fill=MAGENTA)
    p.poly(shape, fill=ACID, outline=INK, width=9)
    # speed cuts on the left
    for x1, x2, y in ((88, 148, 232), (106, 166, 268), (126, 178, 304)):
        p.rect((x1, y, x2, y + 10), fill=CYAN, outline=INK, width=3)


def glyph_calendar(p: Pen):
    p.rrect((156, 126, 384, 428), 20, fill=MAGENTA)
    p.rrect((142, 112, 370, 414), 20, fill=PAPER, outline=INK, width=7)
    p.rrect((142, 112, 370, 192), 20, fill=ACID, outline=INK, width=7)
    p.rect((146, 174, 366, 193), fill=ACID)
    for x in (198, 314):
        p.rrect((x - 9, 92, x + 9, 134), 8, fill=INK)
        p.rrect((x - 4, 97, x + 4, 129), 4, fill=PAPER)
    # the era strap repeats in the ticket head: 88 -> NOW, not a single year
    p.text((160, 148), "88", font("mono", 23 * SS), INK, anchor="lm")
    p.text((352, 148), "NOW", font("mono", 23 * SS), INK, anchor="rm")
    p.line([(208, 153), (284, 153)], fill=INK, width=5)
    p.poly([(280, 145), (300, 153), (280, 161)], fill=INK)
    # 1993 is one page inside the range, so it stays the giant calendar numeral
    p.text((256, 326), "93", font("display", 150 * SS), INK, anchor="mb")
    # tiny month grid
    for row in range(2):
        for col in range(4):
            x = 174 + col * 46
            y = 348 + row * 26
            fill = MAGENTA if (row, col) == (1, 2) else INK
            p.rect((x, y, x + 28, y + 15), fill=fill)
    p.line([(160, 408), (352, 214)], fill=with_alpha(CYAN, 120), width=4)


def glyph_crt(p: Pen):
    # stand first
    p.poly([(238, 352), (274, 352), (294, 412), (218, 412)], fill=PAPER, outline=INK, width=6)
    p.rrect((188, 406, 324, 430), 8, fill=PAPER, outline=INK, width=6)
    p.rrect((110, 134, 430, 370), 26, fill=MAGENTA)
    p.rrect((96, 120, 416, 356), 26, fill=PAPER, outline=INK, width=7)
    p.rrect((126, 146, 386, 328), 12, fill=ACID, outline=INK, width=7)
    p.rect((130, 150, 382, 178), fill=INK)
    for x, color in ((146, MAGENTA), (162, CYAN), (178, ACID)):
        p.ellipse((x - 5, 159, x + 5, 169), fill=color)
    # a 1996 homepage, reduced to blocks
    p.rect((146, 196, 284, 206), fill=INK)
    p.rect((146, 216, 248, 224), fill=INK)
    p.rect((146, 234, 298, 242), fill=INK)
    # tiny smiley on the screen and a cursor
    glyph_smiley(p, cx=322, cy=262, r=28, fill=ACID, shadow=ACID)
    p.poly([(300, 276), (300, 316), (310, 306), (318, 322), (326, 318), (318, 302), (332, 302)], fill=INK)


def glyph_heart(p: Pen):
    p.ellipse((152, 190, 280, 318), fill=ACID)
    p.ellipse((256, 190, 384, 318), fill=ACID)
    p.poly([(160, 262), (376, 262), (268, 424)], fill=ACID)
    p.ellipse((140, 178, 268, 306), fill=MAGENTA, outline=INK, width=7)
    p.ellipse((244, 178, 372, 306), fill=MAGENTA, outline=INK, width=7)
    p.poly([(148, 250), (364, 250), (256, 412)], fill=MAGENTA, outline=INK, width=7)
    p.line([(196, 216), (236, 200)], fill=with_alpha(PAPER, 210), width=8)
    p.line([(180, 252), (300, 252)], fill=INK, width=5)


def glyph_search(p: Pen):
    # shadow, lens, smiley, ring, handle
    p.ellipse((142, 128, 382, 368), fill=MAGENTA)
    p.ellipse((128, 114, 368, 354), fill=ACID, outline=INK, width=8)
    glyph_smiley(p, cx=248, cy=234, r=82, fill=ACID, shadow=ACID)
    p.ellipse((114, 100, 382, 368), outline=PAPER, width=26)
    p.ellipse((114, 100, 382, 368), outline=INK, width=4)
    # handle
    p.line([(342, 330), (420, 408)], fill=INK, width=56)
    p.line([(342, 330), (420, 408)], fill=PAPER, width=34)
    p.ellipse((406, 394, 438, 426), fill=PAPER, outline=INK, width=6)
    # little dial ticks
    for angle in (-118, -90, -62):
        a = math.radians(angle)
        x1, y1 = 248 + 146 * math.cos(a), 234 + 146 * math.sin(a)
        x2, y2 = 248 + 170 * math.cos(a), 234 + 170 * math.sin(a)
        p.line([(x1, y1), (x2, y2)], fill=CYAN, width=7)


def glyph_garage(p: Pen):
    """UK Garage: a forward-sheared 2 with rewind chevrons.

    The chrome is a two-colour illusion: the numeral is one ink plate and the
    specular arc is a paper knock-out, so the mark keeps its silhouette at
    22 px and gains the flyer gloss only at review size.
    """
    face = font("display", 330 * SS)
    layer = Image.new("RGBA", (512 * SS, 512 * SS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.text((256 * SS, 262 * SS), "2", font=face, fill=PAPER, anchor="mm")
    layer = layer.transform(layer.size, Image.Transform.AFFINE,
                            (1, -0.14, 0.14 * 512 * SS / 2, 0, 1, 0),
                            resample=Image.Resampling.BICUBIC)
    p.d._image.alpha_composite(layer)
    # one specular arc along the bowl: a chrome glint, not a cut through the 2
    p.arc((188, 120, 392, 304), 190, 305, INK, width=8)
    # a double rewind chevron (wheel it up), side by side and pointing back
    for dx in (0, 60):
        p.poly([(100 + dx, 244), (100 + dx, 300), (46 + dx, 272)], fill=PAPER)
    p.text((256, 456), "2 STEP", font("mono", 16 * SS), PAPER, anchor="ma")


def glyph_djez(p: Pen):
    """DJ EZ: selector headphones with a crossfader strip in the headband."""
    # one black plate; the grille panels and the fader slot are knock-outs
    p.arc((104, 88, 408, 392), 180, 360, PAPER, width=40)
    p.rrect((72, 224, 176, 400), 34, fill=PAPER)
    p.rrect((336, 224, 440, 400), 34, fill=PAPER)
    # an inner keyline in the headband, like the rest of the sticker set
    p.arc((124, 108, 388, 372), 180, 360, INK, width=5)
    # cups: field-coloured grille panel with black slats
    for x in (72, 336):
        p.rrect((x + 18, 244, x + 86, 384), 24, fill=INK)
        for y in range(264, 374, 18):
            p.line([(x + 26, y), (x + 78, y)], fill=PAPER, width=7)
    # crossfader strip in the headband: slot and ticks are holes, the cap is ink
    p.rrect((186, 114, 326, 150), 14, fill=INK)
    p.rrect((240, 98, 288, 166), 9, fill=PAPER)
    p.rrect((248, 106, 280, 158), 5, fill=INK)
    for x in range(206, 306, 18):
        p.line([(x, 128), (x, 136)], fill=INK, width=3)
    p.text((256, 456), "SELECTOR", font("mono", 16 * SS), PAPER, anchor="ma")


# --------------------------------------------------------------------------- light sticker tiles


def tile_base_light(accent, code="RP"):
    """A bright screen-print sticker: one flat spot colour, ink text and screen."""
    size = 512 * SS
    img = Image.new("RGBA", (size, size), accent + (255,))
    img = Image.alpha_composite(img, radial_glow(size, (int(size * 0.14), int(size * 0.08)),
                                                 int(size * 0.72), PAPER, 34))
    img = Image.alpha_composite(img, radial_glow(size, (int(size * 0.92), int(size * 0.95)),
                                                 int(size * 0.60), INK, 42))
    img = Image.alpha_composite(img, halftone(size, INK, spacing=16, peak_alpha=48,
                                              light=(0.78, 0.14), base=0.10,
                                              min_radius=0.7, max_radius=2.6))
    img = Image.alpha_composite(img, halftone(size, PAPER, spacing=26, peak_alpha=26,
                                              light=(0.16, 0.16), base=0.08,
                                              min_radius=0.6, max_radius=1.8))
    img = Image.alpha_composite(img, diagonal_hatch(size, INK, spacing=28, alpha=7))

    pen = Pen(ImageDraw.Draw(img))
    pen.rrect((14, 14, 498, 498), 24, outline=with_alpha(INK, 50), width=2)
    for x, y, sx, sy in ((30, 30, 1, 1), (482, 30, -1, 1), (30, 482, 1, -1), (482, 482, -1, -1)):
        pen.line([(x, y), (x + sx * 24, y)], fill=with_alpha(INK, 120), width=3)
        pen.line([(x, y), (x, y + sy * 24)], fill=with_alpha(INK, 120), width=3)
    pen.text((452, 468), code, font("mono", 16 * SS), with_alpha(INK, 140), anchor="rs")
    return img


def glyph_to_ink(layer):
    """Reduce a multicolour glyph to an ink plate with knocked-out details.

    Bright fills become solid ink; dark fills and keylines become holes, so the
    sticker colour shows through eyes, tape windows, dates and grilles.  This is
    the two-colour screen-print logic that keeps the set legible at 22 px.
    """
    alpha = layer.getchannel("A")
    grey = layer.convert("L")
    bright = grey.point(lambda value: 255 if value >= 72 else 0)
    plate = Image.new("RGBA", layer.size, INK + (255,))
    plate.putalpha(ImageChops.multiply(alpha, bright))
    return plate


# --------------------------------------------------------------------------- tiles

TILES = {
    "rave.png": dict(code="RP·01", glyph=glyph_smiley, scale=1.28, field=ACID),
    "radio.png": dict(code="RP·02", glyph=glyph_tower, scale=1.16, field=CYAN),
    "tapepacks.png": dict(code="RP·03", glyph=glyph_cassette, scale=1.26, field=PAPER),
    "jungle.png": dict(code="RP·04", glyph=glyph_mask, scale=1.28, field=MAGENTA),
    "hardcore.png": dict(code="RP·05", glyph=glyph_bolt, scale=1.26, field=ACID),
    "house.png": dict(code="RP·06", glyph=glyph_calendar, scale=1.24, field=CYAN),
    "pirate.png": dict(code="RP·07", glyph=glyph_crt, scale=1.24, field=PAPER),
    "about.png": dict(code="RP·08", glyph=glyph_heart, scale=1.26, field=MAGENTA),
    "search.png": dict(code="RP·09", glyph=glyph_search, scale=1.24, field=ACID),
    "garage.png": dict(code="RP·10", glyph=glyph_garage, scale=1.0, field=PAPER),
    "djez.png": dict(code="RP·11", glyph=glyph_djez, scale=1.0, field=MAGENTA),
}


def build_tile(code, glyph, scale=1.0, field=ACID):
    img = tile_base_light(field, code)
    size = 512 * SS
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glyph(Pen(ImageDraw.Draw(layer)))
    if scale != 1.0:
        grown = layer.resize((int(size * scale), int(size * scale)), Image.Resampling.LANCZOS)
        left = (grown.width - size) // 2
        top = (grown.height - size) // 2
        layer = grown.crop((left, top, left + size, top + size))
    img = Image.alpha_composite(img, glyph_to_ink(layer))
    return finish(img.resize((512, 512), Image.Resampling.LANCZOS))


# --------------------------------------------------------------------------- app icon


def glyph_dove(p: Pen, cx: float, cy: float, w: float, h: float):
    """A white-dove stamp drawn in 512-space: bird in profile, wing raised.

    Built from sampled Bézier outlines so the body, wing and fan tail stay one
    solid ink plate.  The eye is a paper hole; it is the first detail to go at
    22 px, which is the intended degradation.  The shape faces left to read as
    the dove of the 1990s UK love-dove stamp, not as a logo or a real tablet.
    """
    def P(x, y):
        return (cx - w / 2 + x / 100 * w, cy - h / 2 + y / 100 * h)

    body = bezier([
        P(23, 20), P(29, 18), P(33, 21), P(34, 26),      # crown
        P(35, 31), P(37, 36), P(41, 40),                 # neck
        P(46, 44), P(52, 46), P(58, 46),                 # back
        P(65, 46), P(71, 48), P(76, 51),                 # tail root
        P(82, 47), P(89, 43), P(95, 42),                 # upper tail
        P(99, 42), P(100, 46), P(98, 50),
        P(94, 52), P(97, 57), P(93, 61),                 # lower tail
        P(86, 62), P(80, 65), P(73, 63),
        P(66, 64), P(58, 62), P(51, 59),                 # belly
        P(44, 57), P(37, 54), P(31, 49),
        P(26, 44), P(23, 38), P(22, 33),                 # breast
        P(17, 32), P(11, 34), P(5, 35),                  # lower beak
        P(11, 30), P(15, 29), P(17, 28),                 # upper beak
        P(18, 24), P(20, 21), P(23, 20),
    ])
    wing = bezier([
        P(34, 38), P(40, 28), P(48, 14), P(56, 4),       # leading edge
        P(60, 2), P(63, 4), P(63, 9),                    # rounded tip
        P(62, 16), P(62, 23), P(66, 28),                 # trailing edge
        P(62, 30), P(59, 34), P(63, 38),                 # feather notch
        P(59, 41), P(55, 44), P(58, 48),                 # feather notch
        P(52, 48), P(45, 46), P(39, 42), P(34, 38),
    ])
    p.poly(body, fill=INK)
    p.poly(wing, fill=INK)
    r = max(0.8, h * 0.035)
    ex, ey = P(26, 26)
    p.ellipse((ex - r, ey - r, ex + r, ey + r), fill=PAPER)


def glyph_pill(p: Pen, cx: float, cy: float, d: float, offset=(10, 12)):
    """The Love Dove: a white round tablet with the dove stamped in ink.

    The magenta disc is a misregistered second pass, the same print logic as
    the face behind it.  No text, dosage or slogan is added: this is an
    archival emblem, not packaging.
    """
    p.ellipse((cx - d / 2 + offset[0], cy - d / 2 + offset[1],
               cx + d / 2 + offset[0], cy + d / 2 + offset[1]), fill=MAGENTA)
    p.ellipse((cx - d / 2, cy - d / 2, cx + d / 2, cy + d / 2),
              fill=PAPER, outline=INK, width=4)
    glyph_dove(p, cx, cy + d * 0.01, d * 0.76, d * 0.60)


def build_app_icon(size=512):
    """The home-screen face: acid stock, a black wink/play smiley, magenta offset."""
    scale = 4
    big = size * scale
    img = Image.new("RGBA", (big, big), ACID + (255,))
    img = Image.alpha_composite(img, radial_glow(big, (int(big * 0.18), int(big * 0.10)),
                                                 int(big * 0.80), PAPER, 20))
    img = Image.alpha_composite(img, halftone(big, INK, spacing=big // 30, peak_alpha=30,
                                              light=(0.80, 0.14), base=0.05,
                                              min_radius=1.0, max_radius=4.0))
    img = Image.alpha_composite(img, diagonal_hatch(big, INK, spacing=big // 20, alpha=4))

    p = Pen(ImageDraw.Draw(img), scale)
    # a magenta second pass, then the black plate, then acid knock-outs
    p.ellipse((72 + 15, 74 + 17, 440 + 15, 442 + 17), fill=MAGENTA)
    p.ellipse((72, 74, 440, 442), fill=INK)
    # the old left eye becomes the Love Dove pill; the play-triangle wink stays
    glyph_pill(p, 180, 214, 160)
    p.poly([(286, 184), (286, 246), (346, 215)], fill=ACID)
    p.arc((112, 170, 368, 392), 22, 158, ACID, width=30)
    for angle in (22, 158):
        a = math.radians(angle)
        ex = 240 + 128 * math.cos(a)
        ey = 281 + 111 * math.sin(a)
        p.ellipse((ex - 15, ey - 15, ex + 15, ey + 15), fill=ACID)
    return finish(img.resize((size, size), Image.Resampling.LANCZOS))

# --------------------------------------------------------------------------- hero


def _noise(size, amount=8):
    width, height = size
    rng = random.Random(1993)
    noise = Image.frombytes("L", (width, height), rng.randbytes(width * height))
    noise = noise.point(lambda value: max(0, min(255, 128 + int((value - 128) * amount / 24))))
    return noise.convert("RGB")


def halftone_disc(size, center, radius, color, spacing=9, peak_alpha=210,
                  max_radius=6.0, min_radius=1.0):
    """A printed sun: dots that grow and brighten towards the middle."""
    width, height = (size, size) if isinstance(size, int) else size
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    cx, cy = center
    for y in range(int(cy - radius), int(cy + radius), spacing):
        for x in range(int(cx - radius), int(cx + radius), spacing):
            distance = math.hypot(x - cx, y - cy)
            if distance > radius:
                continue
            t = 1.0 - distance / radius
            # a serrated edge: the outermost dot ring is small, the middle is dense
            edge = min(1.0, max(0.0, (radius - distance) / (spacing * 1.6)))
            dot = min_radius + (max_radius - min_radius) * (t ** 0.72)
            alpha = int(peak_alpha * (0.20 + 0.80 * t) * edge)
            if alpha < 4:
                continue
            draw.ellipse((x - dot, y - dot, x + dot, y + dot),
                         fill=with_alpha(color, alpha))
    return layer


def build_hero(w=1920, h=1080):
    img = Image.new("RGBA", (w, h), (5, 5, 9, 255))

    # sky: a cold top, a warm low haze
    sky = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sky_draw = ImageDraw.Draw(sky)
    for y in range(h):
        t = y / h
        r = int(4 + 15 * t)
        g = int(4 + 6 * t)
        b = int(10 + 16 * t)
        sky_draw.line([(0, y), (w, y)], fill=(r, g, b, 255))
    img = Image.alpha_composite(img, sky)

    # low horizon glows, right-heavy so the left stays clean for type
    img = Image.alpha_composite(img, radial_glow((w, h), (int(w * 0.78), int(h * 0.66)),
                                                 int(w * 0.55), MAGENTA, 52))
    img = Image.alpha_composite(img, radial_glow((w, h), (int(w * 0.22), int(h * 0.82)),
                                                 int(w * 0.48), ACID, 22))
    img = Image.alpha_composite(img, radial_glow((w, h), (int(w * 0.55), int(h * 0.56)),
                                                 int(w * 0.40), CYAN, 18))

    # the halftone sun, misregistered like a two-pass screen print
    sun_x, sun_y, sun_r = int(w * 0.655), int(h * 0.36), int(h * 0.32)
    offset_sun = halftone_disc((w, h), (sun_x + 16, sun_y + 18), sun_r, MAGENTA,
                               spacing=9, peak_alpha=135, max_radius=6.0)
    img = Image.alpha_composite(img, offset_sun)
    sun = halftone_disc((w, h), (sun_x, sun_y), sun_r, ACID,
                        spacing=9, peak_alpha=225, max_radius=6.2)
    img = Image.alpha_composite(img, sun)
    # a thin ring outside the dots
    ring = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    rd.ellipse((sun_x - sun_r - 18, sun_y - sun_r - 18, sun_x + sun_r + 18, sun_y + sun_r + 18),
               outline=with_alpha(ACID, 70), width=3)
    img = Image.alpha_composite(img, ring)

    # stars above the haze
    rng = random.Random(1993)
    star_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    star_draw = ImageDraw.Draw(star_layer)
    for _ in range(320):
        x = rng.randrange(0, w)
        y = rng.randrange(0, int(h * 0.55))
        if math.hypot(x - sun_x, y - sun_y) < sun_r * 1.05:
            continue
        a = rng.randrange(16, 80)
        r = rng.choice((1, 1, 1, 2))
        star_draw.ellipse((x, y, x + r, y + r), fill=with_alpha(PAPER, a))
    img = Image.alpha_composite(img, star_layer)

    # searchlight cones, drawn before the mast so the steel cuts through them
    beams = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    beam_draw = ImageDraw.Draw(beams)
    origin = (sun_x, int(h * 0.19))
    beam_draw.polygon([origin, (int(w * 0.30), 0), (int(w * 0.48), 0)],
                      fill=with_alpha(ACID, 26))
    beam_draw.polygon([origin, (int(w * 0.96), int(h * 0.04)), (int(w * 1.03), int(h * 0.38))],
                      fill=with_alpha(MAGENTA, 30))
    beam_draw.polygon([origin, (int(w * 0.82), 0), (int(w * 0.92), 0)],
                      fill=with_alpha(CYAN, 18))
    beams = beams.filter(ImageFilter.GaussianBlur(22))
    img = Image.alpha_composite(img, beams)

    # city skyline
    city = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    city_draw = ImageDraw.Draw(city)
    horizon = int(h * 0.76)
    rng = random.Random(93)
    x = -40
    buildings = []
    while x < w + 80:
        bw = rng.randrange(55, 160)
        bh = rng.randrange(26, 200)
        if 0.56 * w < x < 0.76 * w:      # keep the mast base clear
            bh = rng.randrange(16, 64)
        buildings.append((x, horizon - bh, x + bw, horizon + 40))
        x += bw + rng.randrange(4, 22)
    for box in buildings:
        city_draw.rectangle(box, fill=(6, 6, 11, 255))
    for box in buildings:
        x0, y0, x1, y1 = box
        for wx in range(x0 + 8, x1 - 6, 15):
            for wy in range(y0 + 10, y1 - 12, 19):
                if rng.random() < 0.14:
                    color = rng.choice((ACID, CYAN, MAGENTA, PAPER))
                    city_draw.rectangle((wx, wy, wx + 3, wy + 4),
                                        fill=with_alpha(color, rng.randrange(30, 135)))
    img = Image.alpha_composite(img, city)

    # rooftop deck
    deck = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    deck_draw = ImageDraw.Draw(deck)
    deck_y = int(h * 0.80)
    deck_draw.rectangle((0, deck_y, w, h), fill=(6, 6, 10, 255))
    deck_draw.line([(0, deck_y), (w, deck_y)], fill=with_alpha(PAPER, 45), width=2)
    for x in range(0, w, 96):
        deck_draw.line([(x, deck_y), (x, h)], fill=with_alpha(PAPER, 9), width=2)
    # a low railing adds scale
    for x in range(0, w, 64):
        deck_draw.line([(x, deck_y - 58), (x, deck_y)], fill=with_alpha(PAPER, 20), width=2)
    deck_draw.line([(0, deck_y - 58), (w, deck_y - 58)], fill=with_alpha(PAPER, 26), width=2)
    img = Image.alpha_composite(img, deck)

    # the transmitter, a silhouette with rim light
    tower = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    td = ImageDraw.Draw(tower)
    base_y = deck_y + 10
    top_y = int(h * 0.18)
    cx = sun_x
    base_half = int(w * 0.062)
    top_half = int(w * 0.011)

    def half_at(y):
        t = (base_y - y) / max(1, base_y - top_y)
        return top_half + (base_half - top_half) * t

    # dark lattice, then a hot edge
    for sign in (-1, 1):
        td.line([(cx + sign * base_half, base_y), (cx + sign * top_half, top_y)],
                fill=(5, 5, 8, 255), width=17)
    levels = [top_y + (base_y - top_y) * i / 13 for i in range(14)]
    for i, y in enumerate(levels):
        half = half_at(y)
        td.line([(cx - half, y), (cx + half, y)], fill=(5, 5, 8, 255), width=9)
        if i < len(levels) - 1:
            y2 = levels[i + 1]
            half2 = half_at(y2)
            td.line([(cx - half, y), (cx + half2, y2)], fill=(5, 5, 8, 255), width=5)
            td.line([(cx + half, y), (cx - half2, y2)], fill=(5, 5, 8, 255), width=5)
    for sign in (-1, 1):
        td.line([(cx + sign * base_half, base_y), (cx + sign * top_half, top_y)],
                fill=with_alpha(ACID if sign < 0 else MAGENTA, 190), width=3)
    # mast and beacon
    td.line([(cx, top_y), (cx, int(h * 0.105))], fill=(5, 5, 8, 255), width=10)
    td.line([(cx - 2, top_y), (cx - 2, int(h * 0.12))], fill=with_alpha(ACID, 190), width=2)
    td.ellipse((cx - 15, int(h * 0.098), cx + 15, int(h * 0.098) + 30), fill=MAGENTA)
    td.ellipse((cx - 7, int(h * 0.098) + 7, cx + 7, int(h * 0.098) + 21), fill=PAPER)
    # dish silhouettes
    for sign in (-1, 1):
        dx = cx + sign * int(w * 0.043)
        td.ellipse((dx - 25, base_y - 160, dx + 25, base_y - 110), fill=(5, 5, 8, 255),
                   outline=with_alpha(PAPER, 110), width=2)
        td.line([(dx, base_y - 110), (cx + sign * top_half, top_y + 45)],
                fill=with_alpha(PAPER, 45), width=2)
    img = Image.alpha_composite(img, tower)

    # signal rings from the beacon
    rings = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rings)
    for radius in (int(h * 0.22), int(h * 0.36), int(h * 0.52)):
        rd.arc((cx - radius, int(h * 0.11) - radius, cx + radius, int(h * 0.11) + radius),
               200, 340, fill=with_alpha(CYAN, 48), width=2)
    img = Image.alpha_composite(img, rings)

    # a crowd along the front of the roof: silhouettes, not portraits
    crowd = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    cd = ImageDraw.Draw(crowd)
    rng = random.Random(1994)
    people = []
    for _ in range(150):
        x = rng.randrange(-80, w + 80)
        base = h + rng.randrange(8, 70)
        head_r = rng.randrange(15, 27)
        body_h = rng.randrange(70, 120)
        people.append((base, x, head_r, body_h, rng.random() < 0.14))
    people.sort()
    for base, x, head_r, body_h, arm_up in people:
        head_y = base - body_h
        shoulder_y = head_y + head_r * 1.6
        cd.rounded_rectangle((x - head_r * 2.1, shoulder_y, x + head_r * 2.1, base + 20),
                             radius=head_r, fill=(6, 6, 10, 255))
        cd.ellipse((x - head_r, head_y - head_r, x + head_r, head_y + head_r),
                   fill=(6, 6, 10, 255))
        # a little backlight catches the crowns and shoulders
        cd.arc((x - head_r, head_y - head_r, x + head_r, head_y + head_r), 185, 330,
               fill=with_alpha(ACID, 42), width=2)
        cd.arc((x - head_r * 2.1, shoulder_y, x + head_r * 2.1, shoulder_y + head_r * 2),
               190, 300, fill=with_alpha(MAGENTA, 30), width=2)
        if arm_up:
            hand_x = x + rng.choice((-1, 1)) * rng.randrange(head_r, head_r * 3)
            hand_y = head_y - rng.randrange(30, 80)
            cd.line([(x, shoulder_y + 10), (hand_x, hand_y)], fill=(6, 6, 10, 255), width=9)
            cd.ellipse((hand_x - 7, hand_y - 10, hand_x + 7, hand_y + 4), fill=(6, 6, 10, 255))
    img = Image.alpha_composite(img, crowd)

    # acid/magenta light spill on the deck
    spill = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(spill)
    sd.polygon([(0, h), (0, deck_y + 30), (int(w * 0.34), h)], fill=with_alpha(ACID, 24))
    sd.polygon([(w, h), (int(w * 0.70), deck_y + 30), (int(w * 0.58), h)],
               fill=with_alpha(MAGENTA, 22))
    spill = spill.filter(ImageFilter.GaussianBlur(50))
    img = Image.alpha_composite(img, spill)

    # print texture and grain
    img = Image.alpha_composite(img, halftone((w, h), ACID, spacing=6, peak_alpha=15,
                                              light=(0.72, 0.26), base=0.0,
                                              min_radius=0.4, max_radius=1.5))
    img = Image.alpha_composite(img, diagonal_hatch((w, h), PAPER, spacing=9, alpha=4))
    img = Image.blend(img.convert("RGB"), _noise((w, h), 5), 0.055)

    # left-side falloff keeps the page title readable over the artwork
    grad = Image.new("L", (w, 1))
    for x in range(w):
        t = x / w
        grad.putpixel((x, 0), int(255 * min(1.0, max(0.0, (t - 0.0) / 0.52)) ** 1.5))
    mask_full = grad.resize((w, h))
    dark = Image.new("RGB", (w, h), (7, 7, 11))
    img = Image.composite(img, dark, mask_full).convert("RGBA")
    # flyer furniture: crop marks and a tiny transmission credit
    marks = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    md = ImageDraw.Draw(marks)
    for x, y, sx, sy in ((54, 54, 1, 1), (w - 54, 54, -1, 1),
                         (54, h - 54, 1, -1), (w - 54, h - 54, -1, -1)):
        md.line([(x, y), (x + sx * 34, y)], fill=with_alpha(PAPER, 105), width=3)
        md.line([(x, y), (x, y + sy * 34)], fill=with_alpha(PAPER, 105), width=3)
    img = Image.alpha_composite(img, marks)
    return img.convert("RGB")


def build_hero_mobile(w=960, h=480):
    """A 2:1 phone crop: the mast and the halftone sun, little empty sky."""
    full = build_hero(1920, 1080)
    crop = full.crop((0, 0, 1920, 960))
    return crop.resize((w, h), Image.Resampling.LANCZOS)


# --------------------------------------------------------------------------- contact sheet


def contact_sheet(paths, out, sizes=(22, 44, 52, 60, 128, 256)):
    pad = 18
    row_h = max(sizes) + 40
    width = 300 + len(sizes) * (max(sizes) + pad) + pad
    sheet = Image.new("RGB", (width, len(paths) * row_h + pad * 2), (7, 7, 11))
    draw = ImageDraw.Draw(sheet)
    face = font("mono", 14)
    for row, path in enumerate(paths):
        y = pad + row * row_h
        draw.text((pad, y + 8), Path(path).name, font=face, fill=(220, 220, 210))
        source = Image.open(path).convert("RGB")
        for col, size in enumerate(sizes):
            icon = source.resize((size, size), Image.Resampling.LANCZOS)
            x = 300 + col * (max(sizes) + pad)
            sheet.paste(icon, (x, y + 4))
            draw.text((x, y + size + 8), f"{size}px", font=face, fill=(130, 130, 125))
    sheet.save(out, "PNG")


# --------------------------------------------------------------------------- main


def main():
    parser = argparse.ArgumentParser(description="Rebuild the Rave Player artwork.")
    parser.add_argument("--contact", action="store_true", help="write a review contact sheet")
    parser.add_argument("--only", choices=["icons", "hero"], help="build one group")
    args = parser.parse_args()

    ART.mkdir(parents=True, exist_ok=True)
    if args.only != "hero":
        for name, spec in TILES.items():
            tile = build_tile(**spec)
            path = ART / name
            save_png(tile, path)
            print(f"{path.relative_to(ROOT)}  {path.stat().st_size / 1024:.0f} KB")

        icon = build_app_icon(512)
        save_png(icon, ART / "icon.png")
        print(f"static/art/icon.png  {(ART / 'icon.png').stat().st_size / 1024:.0f} KB")
        save_png(icon.resize((180, 180), Image.Resampling.LANCZOS), ART / "icon-180.png")
        print(f"static/art/icon-180.png  {(ART / 'icon-180.png').stat().st_size / 1024:.0f} KB")

        if args.contact:
            import tempfile
            sheet = Path(tempfile.gettempdir()) / "raveplayer-contact.png"
            contact_sheet([ART / name for name in TILES]
                          + [ART / "icon.png", ART / "icon-180.png"], sheet)
            print(f"contact sheet  {sheet}")

    if args.only != "icons":
        hero = build_hero()
        hero.save(ART / "hero.jpg", "JPEG", quality=80, optimize=True, progressive=True)
        print(f"static/art/hero.jpg  {(ART / 'hero.jpg').stat().st_size / 1024:.0f} KB")
        mobile = build_hero_mobile()
        mobile.save(ART / "hero-960.jpg", "JPEG", quality=78, optimize=True, progressive=True)
        print(f"static/art/hero-960.jpg  {(ART / 'hero-960.jpg').stat().st_size / 1024:.0f} KB")
        # The Kodi shim (core/kodi.py) still asks for fanart.jpg; keep that
        # legacy filename pointing at the same artwork.
        hero.save(ART / "fanart.jpg", "JPEG", quality=80, optimize=True, progressive=True)
        print(f"static/art/fanart.jpg    {(ART / 'fanart.jpg').stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
