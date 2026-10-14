# -*- coding: utf-8 -*-
"""Картинки за споделяне: една и съща карта в размерите на всяка мрежа.

Досега всяка страница споделяше една и съща обща картинка, а споделеното
разчитане — никаква (страницата се пълнеше с JavaScript, а прегледите на
Facebook, Viber, WhatsApp и т.н. не го изпълняват). Тук картата се рисува на
сървъра с Pillow, без браузър и без нови системни пакети: DejaVu поддържа
кирилица и зодиакалните знаци.

Размери (SIZES):
- og      1200×630  — прегледът на линк: Facebook, Messenger, Viber, WhatsApp,
                      Telegram, X, LinkedIn
- square  1080×1080 — пост в Instagram/Facebook, изпращане като снимка
- story   1080×1920 — Instagram/Facebook Stories (горе ~250 px и долу ~340 px
                      остават празни — там са бутоните на приложението)

render_card() е чиста функция (данни → JPEG байтове); кешът е в app.py.
Pillow идва с reportlab (задължителна зависимост за PDF-ите) — без нов пакет.
"""
from __future__ import annotations

import io
import os
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

CARD_VERSION = 1   # вдигни при промяна на дизайна — сменя адресите в кеша

SIZES = {"og": (1200, 630), "square": (1080, 1080), "story": (1080, 1920)}

BG = (8, 7, 26)
GLOW = (124, 58, 237)
GILT = (232, 197, 107)
HEADING = (214, 198, 255)
TEXT = (230, 226, 244)
MUTED = (154, 147, 184)
CHIP_BG = (28, 24, 60)

_FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu",
              "C:/Windows/Fonts"]
_FONT_FILES = {
    "serif": ["DejaVuSerif-Bold.ttf", "georgiab.ttf"],
    "sans": ["DejaVuSans.ttf", "arial.ttf"],
    "sans_bold": ["DejaVuSans-Bold.ttf", "arialbd.ttf"],
}
_FONT_CACHE: dict = {}

_HERE = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(_HERE, "static", "logo-full.png")


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    key = (kind, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    for d in _FONT_DIRS:
        for name in _FONT_FILES[kind]:
            path = os.path.join(d, name)
            if os.path.exists(path):
                _FONT_CACHE[key] = ImageFont.truetype(path, size)
                return _FONT_CACHE[key]
    _FONT_CACHE[key] = ImageFont.load_default(size)
    return _FONT_CACHE[key]


def _background(w: int, h: int) -> Image.Image:
    """Тъмен фон с виолетово сияние отгоре — като /static/og-image.jpg."""
    img = Image.new("RGB", (w, h), BG)
    glow = Image.radial_gradient("L").resize((int(w * 1.4), int(h * 1.1)))
    mask = glow.point(lambda v: max(0, 150 - v) * 0.55)   # центърът свети, краищата не
    layer = Image.new("RGB", glow.size, GLOW)
    img.paste(layer, (int(-w * 0.2), int(-h * 0.55)), mask)
    return img


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int, max_lines: int) -> list:
    """Редове, които се побират в `width`; последният завършва с „…“ при нужда."""
    words, lines, line = (text or "").split(), [], ""
    for word in words:
        trial = (line + " " + word).strip()
        if draw.textlength(trial, font=font) <= width:
            line = trial
            continue
        if line:
            lines.append(line)
        line = word
        if len(lines) == max_lines:
            break
    if line and len(lines) < max_lines:
        lines.append(line)
    consumed = " ".join(lines).split()
    if len(consumed) < len(words) and lines:
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > width:
            last = last[:-1]
        lines[-1] = last.rstrip(" ,.;:—-") + "…"
    return lines


def _fit(draw, text: str, kind: str, start: int, smallest: int, width: int, max_lines: int):
    """Най-големият шрифт, при който текстът се побира в `max_lines` реда."""
    size = start
    while size > smallest:
        font = _font(kind, size)
        lines = _wrap(draw, text, font, width, max_lines + 1)
        if len(lines) <= max_lines and not lines[-1].endswith("…"):
            return font, lines
        size -= 4
    font = _font(kind, smallest)
    return font, _wrap(draw, text, font, width, max_lines)


def _emblem(img: Image.Image, box: tuple, glyph: Optional[str]) -> None:
    """Логото или голям знак в златен кръг."""
    x, y, size = box
    draw = ImageDraw.Draw(img)
    if glyph:
        draw.ellipse((x, y, x + size, y + size), outline=GILT, width=max(3, size // 60))
        draw.ellipse((x + size * 0.06, y + size * 0.06, x + size * 0.94, y + size * 0.94),
                     outline=(*GILT,), width=1)
        font = _font("sans", int(size * 0.5))
        draw.text((x + size / 2, y + size / 2), glyph, font=font, fill=GILT, anchor="mm")
        return
    try:
        logo = Image.open(LOGO_PATH).convert("RGBA").resize((size, size), Image.LANCZOS)
        img.paste(logo, (int(x), int(y)), logo)
    except Exception:
        draw.ellipse((x, y, x + size, y + size), outline=GILT, width=4)


def _chips(draw, chips: list, x: int, y: int, width: int, font, center: bool = False) -> int:
    """Етикети в редове; връща y под тях."""
    pad_x, pad_y, gap = 18, 10, 12
    rows, row, row_w = [], [], 0
    for chip in [c for c in chips if c][:6]:
        w = int(draw.textlength(chip, font=font)) + 2 * pad_x
        if row and row_w + gap + w > width:
            rows.append((row, row_w))
            row, row_w = [], 0
        row.append((chip, w))
        row_w += (gap if row_w else 0) + w
    if row:
        rows.append((row, row_w))
    h = font.size + 2 * pad_y
    for items, total in rows[:2]:
        cx = x + (width - total) / 2 if center else x
        for chip, w in items:
            draw.rounded_rectangle((cx, y, cx + w, y + h), radius=h // 2, fill=CHIP_BG,
                                   outline=(70, 60, 130), width=1)
            draw.text((cx + w / 2, y + h / 2), chip, font=font, fill=TEXT, anchor="mm")
            cx += w + gap
        y += h + gap
    return y


def render_card(data: dict, size: str = "og") -> bytes:
    """data: kicker, title, body, chips (list), glyph (знак или None за логото),
    footer (домейн). Връща JPEG ≤ ~200 KB (WhatsApp изпуска по-големите)."""
    w, h = SIZES.get(size, SIZES["og"])
    img = _background(w, h)
    draw = ImageDraw.Draw(img)
    kicker = (data.get("kicker") or "").upper()
    title, body = data.get("title") or "", data.get("body") or ""
    chips, glyph = data.get("chips") or [], data.get("glyph")
    footer = data.get("footer") or ""

    if size == "og":
        _emblem(img, (70, (h - 380) // 2, 380), glyph)
        x, col = 500, w - 500 - 60
        y = 120
        if kicker:
            kf, kl = _fit(draw, kicker, "sans_bold", 26, 18, col, 1)
            draw.text((x, y), kl[0], font=kf, fill=GILT)
            y += kf.size + 22
        tf, tl = _fit(draw, title, "serif", 72, 40, col, 2)
        for line in tl:
            draw.text((x, y), line, font=tf, fill=HEADING)
            y += int(tf.size * 1.12)
        y += 10
        draw.line((x, y, x + min(col, 380), y), fill=GILT, width=2)
        y += 26
        if body:
            bf, bl = _fit(draw, body, "sans", 32, 24, col, 3 if not chips else 2)
            for line in bl:
                draw.text((x, y), line, font=bf, fill=TEXT)
                y += int(bf.size * 1.3)
            y += 10
        if chips and y < h - 90:
            _chips(draw, chips, x, y, col, _font("sans", 22))
        if footer:
            draw.text((w - 60, h - 40), footer, font=_font("sans_bold", 22), fill=MUTED, anchor="rs")
    else:
        story = size == "story"
        top, bottom = (250, h - 340) if story else (80, h - 110)
        col = w - 2 * 90
        emblem = 360 if story else 230
        # Първо се мери, после се рисува — блокът се центрира във видимата зона.
        kf = kl = None
        if kicker:
            kf, kl = _fit(draw, kicker, "sans_bold", 34 if story else 30, 20, col, 1)
        tf, tl = _fit(draw, title, "serif", 96 if story else 72, 44, col, 3 if story else 2)
        bf = bl = None
        if body:
            bf, bl = _fit(draw, body, "sans", 44 if story else 34, 26, col, 7 if story else 3)
        cf = _font("sans", 32 if story else 26)
        chip_h = (cf.size + 32) * (2 if len(chips) > 3 else 1) if chips else 0
        gap = 60 if story else 36
        block = (emblem + gap + ((kf.size + 26) if kf else 0) + int(tf.size * 1.14) * len(tl) + 54
                 + (int(bf.size * 1.38) * len(bl) + 24 if bl else 0) + chip_h)
        y = top + max(0, (bottom - top - block) // 2)
        _emblem(img, ((w - emblem) / 2, y, emblem), glyph)
        y += emblem + gap
        if kf:
            draw.text((w / 2, y), kl[0], font=kf, fill=GILT, anchor="ma")
            y += kf.size + 26
        for line in tl:
            draw.text((w / 2, y), line, font=tf, fill=HEADING, anchor="ma")
            y += int(tf.size * 1.14)
        y += 18
        draw.line((w / 2 - 160, y, w / 2 + 160, y), fill=GILT, width=2)
        y += 36
        if bl:
            for line in bl:
                draw.text((w / 2, y), line, font=bf, fill=TEXT, anchor="ma")
                y += int(bf.size * 1.38)
            y += 24
        if chips:
            _chips(draw, chips, 90, y, col, cf, center=True)
        if footer:
            draw.text((w / 2, bottom + (70 if story else 50)), footer, font=_font("sans_bold", 32),
                      fill=MUTED, anchor="ma")

    out = io.BytesIO()
    img.save(out, "JPEG", quality=86, optimize=True, progressive=True)
    return out.getvalue()
