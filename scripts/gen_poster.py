#!/usr/bin/env python3
"""A4 плакат за касата на магазини: какво е АстроКарта, услугите и QR към /start.

Чете се от метър-два и от по-възрастни хора: едро, удебелено, висок контраст.
Цени няма нарочно. QR-ът води към безплатната карта с UTM, за да се вижда в
Админ → Отчети → „Кампании и източници“.

    python scripts/gen_poster.py                  # docs/print/plakat-a4.pdf + .png
    python scripts/gen_poster.py --store mladost  # utm_content=mladost, отделни файлове
    python scripts/gen_poster.py --bleed          # +3 мм от всяка страна за печатница

Иска reportlab, qrcode, uharfbuzz, fonttools (последните три не са в requirements)
и pdftoppm за PNG прегледа. Шрифтовете (OFL) се теглят веднъж от Google Fonts в
scripts/.fonts/. Целият текст се чертае като контури с кернинга на шрифта, затова
PDF-ът не зависи от шрифтовете на печатницата.
"""
import argparse
import math
import random
import re
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

import qrcode
import uharfbuzz as hb
from fontTools.pens.basePen import BasePen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from reportlab.lib.colors import HexColor
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = Path(__file__).resolve().parent / ".fonts"
OUT_DIR = ROOT / "docs" / "print"
LOGO = ROOT / "static" / "logo-full.png"

PAGE_W, PAGE_H = 210.0, 297.0  # мм, A4
BLEED = 3.0
M = 13.0  # поле отляво и отдясно

# Цветовете на сайта (static/landing.css)
NIGHT = "#08071a"
PANEL = "#16133d"
GILT = "#e8c56b"
GOLD_RAMP = ["#fbecb4", "#ecc96f", "#c99636"]  # отгоре надолу, като метала на логото
WHITE = "#ffffff"
TEXT = "#ece8f8"
SOFT = "#c3bcdb"
ICY = "#bde0fe"
THISTLE = "#cdb4db"
PAPER = "#fbf8f1"

URL = "https://astrokarta.bg/start"
UTM = {"utm_source": "plakat", "utm_medium": "print", "utm_campaign": "kasa"}

FONTS = {
    "brand": ("Cormorant Garamond", 700, False),
    "black": ("Inter", 800, False),
    "bold": ("Inter", 700, False),
    "semi": ("Inter", 600, False),
    "medium": ("Inter", 500, False),
    "sym": ("Noto Sans Symbols", 400, False),
}
# Safari 5 получава цели .ttf (с кирилицата), а не woff2 на парчета.
FONT_UA = ("Mozilla/5.0 (Macintosh; U; Intel Mac OS X 10_6_8) AppleWebKit/533.21.1 "
           "(KHTML, like Gecko) Version/5.0.5 Safari/533.21.1")

# Какво има вътре: (иконка, име, един ред обяснение). Имената са като в FEATURE_CATALOGUE.
SERVICES = [
    ("sun", "Натална карта", "Кой си ти според звездите"),
    ("moon", "Дневен хороскоп", "Всяка сутрин — лично за теб"),
    ("♀", "Любов и съвместимост", "Подхождате ли си двамата"),
    ("♃", "Хороскоп за период", "Какво те очаква напред"),
    ("7", "Нумерология", "Числата от рождената ти дата"),
    ("phase", "Лунен календар", "Кой ден за какво е подходящ"),
]


def font_file(family, weight, italic):
    path = FONT_DIR / f"{family.replace(' ', '')}-{weight}{'i' if italic else ''}.ttf"
    if not path.exists():
        FONT_DIR.mkdir(exist_ok=True)
        q = f"{family.replace(' ', '+')}:wght@{weight}"
        if italic:
            q = f"{family.replace(' ', '+')}:ital,wght@1,{weight}"
        req = urllib.request.Request(f"https://fonts.googleapis.com/css2?family={q}",
                                     headers={"User-Agent": FONT_UA})
        css = urllib.request.urlopen(req).read().decode()
        url = re.search(r"url\((https://[^)]+)\)", css).group(1)
        path.write_bytes(urllib.request.urlopen(url).read())
    return path


class _PathPen(BasePen):
    """Контурите на глифа → reportlab път (квадратичните криви BasePen ги прави кубични)."""

    def __init__(self, glyphset, path):
        super().__init__(glyphset)
        self.path = path

    def _moveTo(self, pt):
        self.path.moveTo(*pt)

    def _lineTo(self, pt):
        self.path.lineTo(*pt)

    def _curveToOne(self, a, b, c):
        self.path.curveTo(*a, *b, *c)

    def _closePath(self):
        self.path.close()


class Face:
    def __init__(self, path):
        self.hb = hb.Font(hb.Face(hb.Blob(path.read_bytes())))
        self.tt = TTFont(path)
        self.glyphs = self.tt.getGlyphSet()
        self.order = self.tt.getGlyphOrder()
        self.upem = self.tt["head"].unitsPerEm
        self.cap = self.tt["OS/2"].sCapHeight / self.upem
        self.name = path.name

    def shape(self, text):
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.hb, buf, {"kern": True, "liga": True})
        out = []
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
            if info.codepoint == 0:
                raise SystemExit(f"{self.name} няма знак за {text[info.cluster]!r}")
            out.append((self.order[info.codepoint], pos.x_advance, pos.x_offset, pos.y_offset))
        return out

    def width(self, text, size, track=0.0):
        """Ширина в мм; track е разредката между знаците в pt."""
        g = self.shape(text)
        return (sum(a for _, a, _, _ in g) * size / self.upem + track * (len(g) - 1)) / mm


class Poster:
    def __init__(self, path, bleed):
        self.off = BLEED if bleed else 0.0
        self.w, self.h = PAGE_W + 2 * self.off, PAGE_H + 2 * self.off
        self.c = rl_canvas.Canvas(str(path), pagesize=(self.w * mm, self.h * mm))
        self.c.setTitle("АстроКарта — плакат A4")
        self.c.setAuthor("АстроКарта")
        if bleed:
            t = self.off * mm
            self.c.setTrimBox((t, t, (self.w - self.off) * mm, (self.h - self.off) * mm))
            self.c.setBleedBox((0, 0, self.w * mm, self.h * mm))
        self.f = {k: Face(font_file(*v)) for k, v in FONTS.items()}

    # Координатите са в мм от горния ляв ъгъл на A4 (без bleed-а) → pt на PDF-а.
    def pt(self, x, y):
        return (x + self.off) * mm, (self.h - self.off - y) * mm

    def fill(self, color, alpha=1.0):
        self.c.setFillColor(HexColor(color))
        self.c.setFillAlpha(alpha)

    def stroke(self, color, width_pt, alpha=1.0):
        self.c.setStrokeColor(HexColor(color))
        self.c.setLineWidth(width_pt)
        self.c.setStrokeAlpha(alpha)

    def fit(self, s, font, max_w, max_size, track=0.0):
        """Най-големият размер до max_size, при който редът се събира в max_w мм
        (разредката track е в pt и не се мени с размера)."""
        bare = self.f[font].width(s, max_size)
        spacing = self.f[font].width(s, max_size, track) - bare
        return min(max_size, max_size * (max_w - spacing) / bare)

    def text_path(self, s, font, size, x, y, track=0.0, anchor="left"):
        """Контурите на реда на базова линия y (мм) и ширината му в мм."""
        face = self.f[font]
        w = face.width(s, size, track)
        px, py = self.pt(x - {"left": 0, "center": w / 2, "right": w}[anchor], y)
        k = size / face.upem
        path = self.c.beginPath()
        cur = px
        for name, adv, xo, yo in face.shape(s):
            pen = TransformPen(_PathPen(face.glyphs, path), (k, 0, 0, k, cur + xo * k, py + yo * k))
            face.glyphs[name].draw(pen)
            cur += adv * k + track
        return path, w

    def text(self, s, font, size, x, y, color, alpha=1.0, track=0.0, anchor="left"):
        path, w = self.text_path(s, font, size, x, y, track, anchor)
        self.fill(color, alpha)
        self.c.drawPath(path, stroke=0, fill=1, fillMode=1)
        return w

    def gold(self, path, top, bottom):
        """Запълва пътя със златен преход отгоре (top, мм) надолу (bottom, мм)."""
        c = self.c
        c.saveState()
        c.clipPath(path, stroke=0, fill=0, fillMode=1)
        x, y0 = self.pt(0, top)
        _, y1 = self.pt(0, bottom)
        c.linearGradient(x, y0, x, y1, [HexColor(g) for g in GOLD_RAMP], [0, 0.5, 1],
                         extend=True)
        c.restoreState()

    def gold_text(self, s, font, size, x, y, track=0.0, anchor="left"):
        path, w = self.text_path(s, font, size, x, y, track, anchor)
        cap = self.f[font].cap * size / mm
        self.gold(path, y - cap * 1.05, y + cap * 0.25)
        return w

    def glyph_at(self, ch, font, size, x, y, color, alpha=1.0, bold=0.0):
        """Един символ, центриран по рамката на контура си в (x, y); bold (pt)
        удебелява тънките символи с контур в същия цвят."""
        face = self.f[font]
        name = face.shape(ch)[0][0]
        bp = BoundsPen(face.glyphs)
        face.glyphs[name].draw(bp)
        x0, y0, x1, y1 = bp.bounds
        k = size / face.upem
        px, py = self.pt(x, y)
        path = self.c.beginPath()
        pen = TransformPen(_PathPen(face.glyphs, path),
                           (k, 0, 0, k, px - (x0 + x1) / 2 * k, py - (y0 + y1) / 2 * k))
        face.glyphs[name].draw(pen)
        self.fill(color, alpha)
        if bold:
            self.stroke(color, bold, alpha)
            self.c.setLineJoin(1)
        self.c.drawPath(path, stroke=1 if bold else 0, fill=1, fillMode=1)

    def glow(self, x, y, r, color, strength=0.05, steps=24):
        """Меко сияние: кръгове с намаляващ радиус, всеки с малко плътност."""
        px, py = self.pt(x, y)
        for i in range(steps):
            self.fill(color, strength)
            self.c.circle(px, py, r * mm * (1 - i / steps) ** 1.3, stroke=0, fill=1)


def draw_stars(P, avoid, seed=1017):
    """Звезден прах по фона; под картите и панела почти не се вижда."""
    rng = random.Random(seed)
    c = P.c
    colors = [WHITE] * 6 + [ICY] * 2 + [THISTLE]
    for _ in range(700):
        x = rng.uniform(-P.off, PAGE_W + P.off)
        y = rng.uniform(-P.off, PAGE_H + P.off)
        m = rng.random() ** 3.2
        alpha = 0.2 + 0.6 * m
        if any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in avoid):
            alpha *= 0.15
        r = 0.22 + 0.5 * m  # под ~0.15 мм точката не се отпечатва
        px, py = P.pt(x, y)
        P.fill(rng.choice(colors), alpha)
        c.circle(px, py, r, stroke=0, fill=1)


def draw_icon(P, kind, x, y, d):
    """Златна значка с ∅ d мм и център (x, y)."""
    c = P.c
    px, py = P.pt(x, y)
    P.fill(GILT, 0.13)
    c.circle(px, py, d / 2 * mm, stroke=0, fill=1)
    P.stroke(GILT, 1.0, 0.95)
    c.circle(px, py, d / 2 * mm, stroke=1, fill=0)
    s = d / 15.0  # чертано за значка 15 мм
    if kind == "sun":  # кръг с точка
        P.stroke(GILT, 1.5, 1)
        c.circle(px, py, 3.3 * s * mm, stroke=1, fill=0)
        P.fill(GILT)
        c.circle(px, py, 0.9 * s * mm, stroke=0, fill=1)
    elif kind == "moon":  # сърп ☽: външна дъга отляво, вътрешна — от изместен кръг
        R, d = 4.2 * s * mm, 2.5 * s * mm
        b = math.degrees(math.acos(d / 2 / R))  # ъгълът на пресечните точки
        p = c.beginPath()
        p.moveTo(px + d / 2, py + math.sqrt(R * R - d * d / 4))
        p.arcTo(px - R, py - R, px + R, py + R, startAng=b, extent=360 - 2 * b)
        p.arcTo(px + d - R, py - R, px + d + R, py + R, startAng=180 + b, extent=-2 * b)
        p.close()
        P.fill(GILT)
        c.drawPath(p, stroke=0, fill=1)
    elif kind == "phase":  # половин Луна
        r = 3.9 * s * mm
        P.stroke(GILT, 1.3, 1)
        c.circle(px, py, r, stroke=1, fill=0)
        p = c.beginPath()
        p.moveTo(px, py + r)
        p.arcTo(px - r, py - r, px + r, py + r, startAng=90, extent=180)
        p.close()
        P.fill(GILT)
        c.drawPath(p, stroke=0, fill=1)
    elif kind == "7":
        P.glyph_at("7", "brand", 21 * s, x, y, GILT)
    else:
        P.glyph_at(kind, "sym", 25 * s, x, y, GILT, bold=0.9)


def draw_qr(P, url, x, y, size):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    m = qr.get_matrix()  # с тихата зона
    n = len(m)
    cell = size / n
    px, py = P.pt(x, y)
    P.fill(PAPER)
    P.c.roundRect(px, py - size * mm, size * mm, size * mm, 2.4 * mm, stroke=0, fill=1)
    path = P.c.beginPath()
    for row in range(n):
        col = 0
        while col < n:
            if m[row][col]:
                start = col
                while col < n and m[row][col]:
                    col += 1
                rx, ry = P.pt(x + start * cell, y + (row + 1) * cell)
                path.rect(rx, ry, (col - start) * cell * mm, cell * mm)
            else:
                col += 1
    P.fill(NIGHT)
    P.c.drawPath(path, stroke=0, fill=1, fillMode=1)
    return qr.version


def arrow(P, x0, x1, y, color, w=1.8, head=3.2):
    c = P.c
    P.stroke(color, w, 1)
    c.setLineCap(1)
    c.setLineJoin(1)
    c.line(*P.pt(x0, y), *P.pt(x1, y))
    p = c.beginPath()
    p.moveTo(*P.pt(x1 - head, y - head * 0.75))
    p.lineTo(*P.pt(x1, y))
    p.lineTo(*P.pt(x1 - head, y + head * 0.75))
    c.drawPath(p, stroke=1, fill=0)


def build(out_pdf, url, bleed, store):
    P = Poster(out_pdf, bleed)
    c = P.c
    W = PAGE_W - 2 * M

    # Фонът: нощ с виолетово сияние откъм логото
    gx, gy = P.pt(PAGE_W / 2, 40)
    c.radialGradient(gx, gy, 240 * mm,
                     [HexColor("#2a1f6b"), HexColor("#17134a"), HexColor("#0d0b2a"),
                      HexColor(NIGHT)], [0.0, 0.28, 0.6, 1.0], extend=True)

    GRID_TOP, CARD_H, GAP = 152.0, 23.5, 3.0
    PANEL_TOP, PANEL_BOT = 234.5, 287.5
    draw_stars(P, [(M - 2, GRID_TOP - 2, PAGE_W - M + 2, GRID_TOP + 3 * CARD_H + 2 * GAP + 2),
                   (M, PANEL_TOP, PAGE_W - M, PANEL_BOT)])

    # ── Марката: логото голямо, името изписано в злато ─────────────────────
    E = 44.0
    word_size, tag_size, tag_track = 64, 10.5, 1.7
    tagline = "САЙТ № 1 ЗА АСТРОЛОГИЯ И НУМЕРОЛОГИЯ"
    word_w = P.f["brand"].width("АстроКарта", word_size)
    tag_size = P.fit(tagline, "semi", word_w, tag_size, tag_track)  # не по-широк от името
    tag_w = P.f["semi"].width(tagline, tag_size, tag_track)
    gap = 7.0
    x0 = (PAGE_W - (E + gap + max(word_w, tag_w))) / 2
    ey = 11.0
    P.glow(x0 + E / 2, ey + E / 2, E * 0.78, "#7c3aed", 0.035)
    lx, ly = P.pt(x0, ey + E)
    c.setFillAlpha(1)
    c.drawImage(str(LOGO), lx, ly, E * mm, E * mm, mask="auto")
    tx = x0 + E + gap
    word_base = ey + E / 2 + 1.5
    P.gold_text("АстроКарта", "brand", word_size, tx, word_base)
    P.text(tagline, "semi", tag_size, tx + 0.6, word_base + 11.5, GILT, 0.95, track=tag_track)

    # ── Куката ──────────────────────────────────────────────────────────────
    kicker = "Хороскопът във вестника е еднакъв за милиони хора."
    P.text(kicker, "semi", P.fit(kicker, "semi", W, 17.5), PAGE_W / 2, 71.0, TEXT,
           anchor="center")
    lines = ["Хороскопът тук", "е изчислен", "само за теб."]  # последният е в злато
    hs = min(P.fit(t, "black", W - 4, 60) for t in lines)
    lead = hs / mm * 0.98
    base = 71.0 + 7.0 + P.f["black"].cap * hs / mm
    for i, t in enumerate(lines):
        if i < len(lines) - 1:
            P.text(t, "black", hs, PAGE_W / 2, base + i * lead, WHITE, anchor="center")
        else:
            P.gold_text(t, "black", hs, PAGE_W / 2, base + i * lead, anchor="center")
    sub = "По твоята дата, час и място на раждане — на разбираем български."
    P.text(sub, "medium", P.fit(sub, "medium", W, 14.5), PAGE_W / 2, base + 2 * lead + 9.5, SOFT,
           anchor="center")

    # ── Услугите ────────────────────────────────────────────────────────────
    cw = (W - 6) / 2
    D = 15.0
    for i, (icon, title, desc) in enumerate(SERVICES):
        cx = M + (i % 2) * (cw + 6)
        cy = GRID_TOP + (i // 2) * (CARD_H + GAP)
        rx, ry = P.pt(cx, cy + CARD_H)
        P.fill(WHITE, 0.055)
        P.stroke(GILT, 0.7, 0.45)
        c.roundRect(rx, ry, cw * mm, CARD_H * mm, 3 * mm, stroke=1, fill=1)
        draw_icon(P, icon, cx + 4.5 + D / 2, cy + CARD_H / 2, D)
        tx = cx + 4.5 + D + 4.5
        avail = cx + cw - 3.5 - tx
        P.text(title, "bold", P.fit(title, "bold", avail, 16.5), tx, cy + 10.6, WHITE)
        P.text(desc, "medium", P.fit(desc, "medium", avail, 12), tx, cy + 18.2, SOFT)

    # ── Призивът: QR и адресът ──────────────────────────────────────────────
    rx, ry = P.pt(M, PANEL_BOT)
    P.fill(PANEL)
    P.stroke(GILT, 1.0, 0.9)
    c.roundRect(rx, ry, W * mm, (PANEL_BOT - PANEL_TOP) * mm, 4 * mm, stroke=1, fill=1)
    QS = PANEL_BOT - PANEL_TOP - 7.0
    qx, qy = PAGE_W - M - 3.5 - QS, PANEL_TOP + 3.5
    version = draw_qr(P, url, qx, qy, QS)

    lx = M + 8.0
    hsz = 30
    hw = P.text("Сканирай кода", "black", hsz, lx, PANEL_TOP + 15.5, WHITE)
    P.text("с камерата на телефона", "medium", 16, lx, PANEL_TOP + 23.5, TEXT)
    cap = P.f["black"].cap * hsz / mm
    arrow(P, lx + hw + 5, qx - 5, PANEL_TOP + 15.5 - cap / 2, GILT)
    P.text("или отвори в браузъра", "medium", 12.5, lx, PANEL_TOP + 35.5, SOFT)
    site = "astrokarta.bg"
    P.gold_text(site, "black", P.fit(site, "black", qx - 6 - lx, 34), lx, PANEL_TOP + 47.0)

    if store:  # кой плакат е това, когато са няколко
        P.text(store.upper(), "medium", 5, M, PAGE_H - 4.5, SOFT, 0.5, track=1.0)
    c.showPage()
    c.save()
    return version


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--store", help="код на магазина → utm_content и име на файла")
    ap.add_argument("--bleed", action="store_true", help="+3 мм за печатница")
    ap.add_argument("--dpi", type=int, default=200, help="резолюция на PNG прегледа")
    args = ap.parse_args()

    utm = dict(UTM)
    if args.store:
        utm["utm_content"] = args.store
    url = f"{URL}?{urlencode(utm)}"
    stem = "plakat-a4" + (f"-{args.store}" if args.store else "") + ("-bleed" if args.bleed else "")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf = OUT_DIR / f"{stem}.pdf"
    version = build(pdf, url, args.bleed, args.store)
    if not args.bleed:  # прегледът е за A4-а, не за листа на печатницата
        subprocess.run(["pdftoppm", "-r", str(args.dpi), "-png", "-singlefile", str(pdf),
                        str(OUT_DIR / stem)], check=True)
    print(f"{pdf.relative_to(ROOT)}  (QR v{version}: {url})")


if __name__ == "__main__":
    main()
