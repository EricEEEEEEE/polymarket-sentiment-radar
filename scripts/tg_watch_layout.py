#!/usr/bin/env python3
"""Thin declarative layout layer over Pillow.

Vendored from EricEEEEEEE/TG-watch-skill at
02852122319960e82ed58101ee5eecd69b64fc76.

Zero dependencies beyond Pillow (no browser, no Node, no system libs). Cards are
described as a tree of nodes with padding / gap / rows / columns; positions are
computed instead of hand-written pixel coordinates. Fonts are content-aware:
pure-Latin text uses Helvetica/Arial, anything with CJK falls back to
Hiragino/STHeiti/Noto so Chinese renders correctly.

Public surface:
    Palette, light_palette(), dark_palette(), severity_color()
    Text, Gap, Divider, Column, Row, Bar
    stat_box(), bar_row()
    render_card()
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional, Sequence, Union

from PIL import Image, ImageDraw, ImageFont

RGB = tuple

# --------------------------------------------------------------------------- #
# Fonts (content-aware: Latin vs CJK, each with a regular / bold face)
# --------------------------------------------------------------------------- #
_LATIN_REGULAR = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_LATIN_BOLD = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_CJK_REGULAR = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]
_CJK_BOLD = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJKsc-Bold.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
]
_HANGUL_REGULAR = [
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
] + _CJK_REGULAR
_HANGUL_BOLD = [
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
] + _CJK_BOLD


def _has_hangul(text: str) -> bool:
    return any(
        0x1100 <= ord(ch) <= 0x11FF
        or 0x3130 <= ord(ch) <= 0x318F
        or 0xAC00 <= ord(ch) <= 0xD7AF
        for ch in text
    )


def _has_cjk(text: str) -> bool:
    for ch in text:
        o = ord(ch)
        if (
            0x1100 <= o <= 0x11FF
            or 0x3130 <= o <= 0x318F
            or 0xAC00 <= o <= 0xD7AF
            or 0x3000 <= o <= 0x30FF
            or 0x3400 <= o <= 0x4DBF
            or 0x4E00 <= o <= 0x9FFF
            or 0x20000 <= o <= 0x2EBEF
            or 0x30000 <= o <= 0x323AF
            or 0xFF00 <= o <= 0xFFEF
        ):
            return True
    return False


@lru_cache(maxsize=256)
def _load(paths: tuple, size: int) -> ImageFont.FreeTypeFont:
    for path in paths:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def pick_font(text: str, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    if _has_hangul(text):
        return _load(tuple(_HANGUL_BOLD if bold else _HANGUL_REGULAR), size)
    if _has_cjk(text):
        return _load(tuple(_CJK_BOLD if bold else _CJK_REGULAR), size)
    return _load(tuple(_LATIN_BOLD if bold else _LATIN_REGULAR), size)


_MEASURE = ImageDraw.Draw(Image.new("RGB", (8, 8)))


def _text_w(text: str, font: ImageFont.FreeTypeFont) -> float:
    return _MEASURE.textlength(text, font=font)


def _line_h(font: ImageFont.FreeTypeFont) -> int:
    ascent, descent = font.getmetrics()
    return ascent + descent


def wrap_text(text: str, size: int, bold: bool, max_w: int, max_lines: int) -> list:
    """CJK-aware greedy wrap. Splits on hard newlines and adds a trailing ellipsis when truncated."""
    font = pick_font(text, size, bold)
    # The "\n" check MUST short-circuit before _text_w(): Pillow's textlength()
    # raises ValueError on any string containing a newline, so measuring first
    # would crash before the guard could skip it.
    if "\n" not in text and _text_w(text, font) <= max_w:
        return [text]
    cjk = _has_cjk(text)
    joiner = "" if cjk else " "
    lines: list = []
    truncated = False
    for segment in text.split("\n"):
        if len(lines) >= max_lines:
            truncated = True
            break
        if segment == "":
            lines.append("")
            continue
        units = list(segment) if cjk else segment.split(" ")
        cur = ""
        for unit in units:
            # A URL, hash, identifier, or other unbroken Latin token may be
            # wider than the whole text box. Split it deterministically so no
            # glyphs can escape the card while preserving the exact text.
            if not cjk and _text_w(unit, font) > max_w:
                chunks = []
                chunk = ""
                for character in unit:
                    candidate_chunk = chunk + character
                    if chunk and _text_w(candidate_chunk, font) > max_w:
                        chunks.append(chunk)
                        chunk = character
                    else:
                        chunk = candidate_chunk
                if chunk:
                    chunks.append(chunk)
            else:
                chunks = [unit]
            for chunk in chunks:
                cand = chunk if not cur else f"{cur}{joiner}{chunk}"
                if _text_w(cand, font) <= max_w:
                    cur = cand
                    continue
                if cur:
                    lines.append(cur)
                    if len(lines) >= max_lines:
                        cur = ""
                        truncated = True
                        break
                cur = chunk
            if truncated:
                break
        if cur:
            if len(lines) < max_lines:
                lines.append(cur)
            else:
                truncated = True
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    if truncated and lines:
        tail = lines[-1].rstrip("。., ")
        while tail and _text_w(tail + "…", font) > max_w:
            tail = tail[:-1]
        lines[-1] = tail + "…"
    return lines or [""]


# --------------------------------------------------------------------------- #
# Palette / theme
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Palette:
    bg: RGB
    card: RGB
    panel: RGB
    line: RGB
    text: RGB
    muted: RGB
    accent: RGB
    good: RGB
    warn: RGB
    bad: RGB
    on_accent: RGB


def light_palette(accent: RGB = (37, 99, 235)) -> Palette:
    return Palette(
        bg=(233, 236, 241), card=(255, 255, 255), panel=(244, 246, 249),
        line=(224, 228, 235), text=(17, 24, 33), muted=(94, 109, 130),
        accent=accent, good=(15, 118, 55), warn=(185, 90, 0), bad=(220, 38, 38),
        on_accent=(255, 255, 255),
    )


def dark_palette(accent: RGB = (96, 165, 250)) -> Palette:
    return Palette(
        bg=(8, 12, 18), card=(20, 27, 38), panel=(28, 37, 52), line=(46, 58, 78),
        text=(236, 241, 247), muted=(140, 154, 175), accent=accent,
        good=(51, 200, 120), warn=(240, 175, 60), bad=(239, 90, 80),
        on_accent=(9, 13, 20),
    )


def severity_color(pal: Palette, severity: str) -> RGB:
    key = str(severity).upper()
    if key in ("P0", "RED", "CRITICAL"):
        return pal.bad
    if key in ("P1", "YELLOW", "WATCH", "WARN"):
        return pal.warn
    if key in ("B1", "B2", "B3", "GREEN", "OK"):
        return pal.good
    return pal.accent


# --------------------------------------------------------------------------- #
# Layout nodes: measure(draw, w) -> height ; render(draw, x, y, w)
# --------------------------------------------------------------------------- #
def _pad4(pad: Union[int, tuple]) -> tuple:
    if isinstance(pad, int):
        return (pad, pad, pad, pad)
    if len(pad) == 2:
        return (pad[0], pad[1], pad[0], pad[1])
    return tuple(pad)


class Node:
    def measure(self, draw: ImageDraw.ImageDraw, w: int) -> int:
        raise NotImplementedError

    def render(self, draw: ImageDraw.ImageDraw, x: int, y: int, w: int) -> None:
        raise NotImplementedError


@dataclass
class Gap(Node):
    h: int

    def measure(self, draw, w):
        return self.h

    def render(self, draw, x, y, w):
        return None


@dataclass
class Divider(Node):
    color: RGB
    pad: int = 0

    def measure(self, draw, w):
        return self.pad * 2 + 2

    def render(self, draw, x, y, w):
        yy = y + self.pad + 1
        draw.line((x, yy, x + w, yy), fill=self.color, width=1)


@dataclass
class Text(Node):
    text: str
    size: int
    color: RGB
    bold: bool = False
    max_lines: int = 1
    leading: float = 1.3
    align: str = "left"  # left | center | right
    tracking: int = 0     # extra px between chars (for small caps labels)

    def _lines(self, w):
        return wrap_text(str(self.text), self.size, self.bold, w, self.max_lines)

    def measure(self, draw, w):
        # Sum per-line font metrics so mixed Latin/CJK lines match render() exactly
        # (each line may resolve to a different font with a different line height).
        total = 0
        for line in self._lines(w):
            font = pick_font(line, self.size, self.bold)
            total += int(_line_h(font) * self.leading)
        return total

    def render(self, draw, x, y, w):
        lines = self._lines(w)
        for i, line in enumerate(lines):
            font = pick_font(line, self.size, self.bold)
            step = int(_line_h(font) * self.leading)
            ly = y + i * step
            if self.tracking and self.align == "left":
                cx = x
                for ch in line:
                    draw.text((cx, ly), ch, font=font, fill=self.color)
                    cx += _text_w(ch, font) + self.tracking
                continue
            lw = _text_w(line, font)
            if self.align == "center":
                lx = x + (w - lw) / 2
            elif self.align == "right":
                lx = x + w - lw
            else:
                lx = x
            draw.text((lx, ly), line, font=font, fill=self.color)


@dataclass
class Bar(Node):
    ratio: float
    track: RGB
    fill: RGB
    height: int = 16

    def measure(self, draw, w):
        return self.height

    def render(self, draw, x, y, w):
        r = self.height / 2
        draw.rounded_rectangle((x, y, x + w, y + self.height), radius=r, fill=self.track)
        filled = max(0.0, min(1.0, self.ratio))
        if filled > 0:
            fw = max(self.height, int(w * filled))
            draw.rounded_rectangle((x, y, x + fw, y + self.height), radius=r, fill=self.fill)


@dataclass
class Column(Node):
    children: Sequence[Node]
    gap: int = 0
    pad: Union[int, tuple] = 0
    bg: Optional[RGB] = None
    radius: int = 0
    border: Optional[RGB] = None
    border_w: int = 1

    def _inner_w(self, w):
        t, r, b, l = _pad4(self.pad)
        return w - l - r

    def measure(self, draw, w):
        t, r, b, l = _pad4(self.pad)
        inner = w - l - r
        kids = [c for c in self.children if c is not None]
        h = sum(c.measure(draw, inner) for c in kids)
        if kids:
            h += self.gap * (len(kids) - 1)
        return h + t + b

    def render(self, draw, x, y, w):
        h = self.measure(draw, w)
        if self.bg is not None or self.border is not None:
            box = (x, y, x + w, y + h)
            draw.rounded_rectangle(
                box, radius=self.radius, fill=self.bg,
                outline=self.border, width=self.border_w if self.border else 1,
            )
        t, r, b, l = _pad4(self.pad)
        inner = w - l - r
        cy = y + t
        for c in self.children:
            if c is None:
                continue
            ch = c.measure(draw, inner)
            c.render(draw, x + l, cy, inner)
            cy += ch + self.gap


@dataclass
class Row(Node):
    children: Sequence[Node]
    gap: int = 0
    widths: Optional[Sequence[Optional[int]]] = None  # px for fixed, None for flex
    valign: str = "top"  # top | center

    def _resolved(self, w):
        kids = list(self.children)
        n = len(kids)
        if self.widths is None:
            spec = [None] * n
        else:
            spec = list(self.widths)
        fixed = sum(v for v in spec if v)
        flex_n = sum(1 for v in spec if not v)
        avail = w - self.gap * (n - 1) - fixed
        flex_w = avail / flex_n if flex_n else 0
        return [int(v) if v else int(flex_w) for v in spec]

    def measure(self, draw, w):
        widths = self._resolved(w)
        return max((c.measure(draw, cw) for c, cw in zip(self.children, widths)), default=0)

    def render(self, draw, x, y, w):
        widths = self._resolved(w)
        row_h = self.measure(draw, w)
        cx = x
        for c, cw in zip(self.children, widths):
            cy = y
            if self.valign == "center":
                cy = y + (row_h - c.measure(draw, cw)) // 2
            c.render(draw, cx, cy, cw)
            cx += cw + self.gap


# --------------------------------------------------------------------------- #
# Semantic builders
# --------------------------------------------------------------------------- #
def stat_box(pal: Palette, label: str, value: str, *, value_color: Optional[RGB] = None,
             value_size: int = 64, sub: Optional[str] = None, big: bool = True) -> Column:
    kids = [
        Text(label, 30, pal.muted, bold=True, tracking=1),
        Gap(14 if big else 10),
        Text(value, value_size, value_color or pal.text, bold=True, max_lines=1),
    ]
    if sub:
        kids += [Gap(8), Text(sub, 30, pal.muted, max_lines=2)]
    return Column(kids, pad=(20, 22, 22, 22) if big else (16, 16, 16, 16),
                  bg=pal.panel, radius=16, border=pal.line)


def bar_row(pal: Palette, label: str, ratio: float, value: str,
            fill: Optional[RGB] = None) -> Row:
    return Row(
        [
            Text(label, 32, pal.muted, bold=True),
            Bar(ratio, pal.line, fill or pal.good, height=24),
            Text(value, 32, pal.text, bold=True, align="right"),
        ],
        gap=22,
        widths=[120, None, 170],
        valign="center",
    )


# --------------------------------------------------------------------------- #
# Card frame
# --------------------------------------------------------------------------- #
def render_card(out_path, *, children: Sequence[Node], palette: Palette,
                width: int = 1200, margin: int = 26, pad: Union[int, tuple] = 44,
                radius: int = 34, accent_rail: bool = True, rail_color: Optional[RGB] = None,
                min_height: Optional[int] = None) -> Path:
    """Measure the content column, size the card to fit, and rasterize to PNG."""
    root = Column(children, pad=pad)
    inner_w = width - 2 * margin
    content_h = root.measure(_MEASURE, inner_w)
    height = content_h + 2 * margin
    if min_height:
        height = max(height, min_height)

    img = Image.new("RGB", (width, height), palette.bg)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((margin, margin, width - margin, height - margin),
                           radius=radius, fill=palette.card, outline=palette.line, width=1)
    if accent_rail:
        rc = rail_color or palette.accent
        draw.rounded_rectangle((margin, margin, margin + 12, height - margin),
                               radius=radius, fill=rc)
        draw.rectangle((margin + 8, margin + 6, margin + 12, height - margin - 6), fill=rc)
    root.render(draw, margin, margin, inner_w)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out
