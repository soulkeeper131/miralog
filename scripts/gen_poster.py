#!/usr/bin/env python3
"""A4 плакат за касата на магазини: „Това не е твоето небе.“ + QR към /start.

Колелото е истинско: небето над София на 17.10.2026 в 12:00 (Swiss Ephemeris,
Moshier — без файлове от ephe/). QR-ът води към безплатната карта с UTM, за да
се вижда в Админ → Отчети → „Кампании и източници“. Цени няма нарочно.

    python scripts/gen_poster.py                  # docs/print/plakat-a4.pdf + .png
    python scripts/gen_poster.py --store mladost  # utm_content=mladost, отделни файлове
    python scripts/gen_poster.py --bleed          # +3 мм от всяка страна за печатница

Иска pyswisseph, reportlab, qrcode, uharfbuzz, fonttools (последните три не са в
requirements) и pdftoppm за PNG прегледа. Шрифтовете (OFL) се теглят веднъж от
Google Fonts в scripts/.fonts/. Целият текст се чертае като контури с кернинга
на шрифта, затова PDF-ът не зависи от шрифтовете на печатницата.
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
import swisseph as swe
import uharfbuzz as hb
from fontTools.pens.basePen import BasePen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from reportlab.lib.colors import HexColor
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = Path(__file__).resolve().parent / ".fonts"
OUT_DIR = ROOT / "docs" / "print"

PAGE_W, PAGE_H = 210.0, 297.0  # мм, A4
BLEED = 3.0

# Цветовете на сайта (static/landing.css)
NIGHT = "#08071a"
GILT = "#e8c56b"
IVORY = "#f1ecdf"
TEXT = "#e6e2f4"
SOFT = "#b4adcc"
THISTLE = "#cdb4db"
ICY = "#a2d2ff"
PINK = "#ffafcc"
PAPER = "#f4efe4"

# Моментът на колелото
MOMENT = (2026, 10, 17, 9.0)  # UT = 12:00 в София (EEST)
LAT, LON = 42.6977, 23.3219

URL = "https://astrokarta.bg/start"
UTM = {"utm_source": "plakat", "utm_medium": "print", "utm_campaign": "kasa"}

FONTS = {
    "serif": ("Cormorant Garamond", 400, False),
    "serif-med": ("Cormorant Garamond", 500, False),
    "serif-semi": ("Cormorant Garamond", 600, False),
    "serif-it": ("Cormorant Garamond", 400, True),
    "serif-med-it": ("Cormorant Garamond", 500, True),
    "sans": ("Manrope", 300, False),
    "mono": ("JetBrains Mono", 300, False),
    "sym": ("Noto Sans Symbols", 400, False),
    "sym-light": ("Noto Sans Symbols", 300, False),
}
# Safari 5 получава цели .ttf (с кирилицата), а не woff2 на парчета.
FONT_UA = ("Mozilla/5.0 (Macintosh; U; Intel Mac OS X 10_6_8) AppleWebKit/533.21.1 "
           "(KHTML, like Gecko) Version/5.0.5 Safari/533.21.1")


def font_file(family, weight, italic):
    path = FONT_DIR / f"{family.replace(' ', '')}-{weight}{'i' if italic else ''}.ttf"
    if not path.exists():
        FONT_DIR.mkdir(exist_ok=True)
        q = f"{family.replace(' ', '+')}:ital,wght@{int(italic)},{weight}"
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
        """Ширина в pt; track е разредката между знаците в pt."""
        g = self.shape(text)
        return sum(a for _, a, _, _ in g) * size / self.upem + track * (len(g) - 1)


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

    # Координатите в мм от горния ляв ъгъл на A4 (без bleed-а) → pt на PDF-а.
    def pt(self, x, y):
        return (x + self.off) * mm, (self.h - self.off - y) * mm

    def fill(self, color, alpha=1.0):
        self.c.setFillColor(HexColor(color))
        self.c.setFillAlpha(alpha)

    def stroke(self, color, width_pt, alpha=1.0):
        self.c.setStrokeColor(HexColor(color))
        self.c.setLineWidth(width_pt)
        self.c.setStrokeAlpha(alpha)

    def _glyphs(self, path, face, glyphs, size, place):
        """place(i, advance_pt) → (xx, xy, yx, yy, dx, dy) за i-тия глиф."""
        s = size / face.upem
        for i, (name, adv, xo, yo) in enumerate(glyphs):
            xx, xy, yx, yy, dx, dy = place(i, adv * s)
            tr = (xx * s, xy * s, yx * s, yy * s,
                  dx + (xx * xo + yx * yo) * s, dy + (xy * xo + yy * yo) * s)
            face.glyphs[name].draw(TransformPen(_PathPen(face.glyphs, path), tr))

    def text(self, s, font, size, x, y, color, alpha=1.0, track=0.0, anchor="left"):
        """Ред текст на базова линия y (мм). Връща ширината в мм."""
        face = self.f[font]
        w = face.width(s, size, track)
        x0 = {"left": 0, "center": -w / 2, "right": -w}[anchor]
        px, py = self.pt(x, y)
        cur = [px + x0]
        path = self.c.beginPath()

        def place(i, adv):
            t = (1, 0, 0, 1, cur[0], py)
            cur[0] += adv + track
            return t
        self._glyphs(path, face, face.shape(s), size, place)
        self.fill(color, alpha)
        self.c.drawPath(path, stroke=0, fill=1, fillMode=1)
        return w / mm

    def text_rot(self, s, font, size, x, y, deg, color, alpha=1.0, track=0.0, anchor="left"):
        """Ред, завъртян на deg (математически) около (x, y) на базовата линия,
        центриран по главната височина."""
        face = self.f[font]
        w = face.width(s, size, track)
        a = math.radians(deg)
        tx, ty, nx, ny = math.cos(a), math.sin(a), -math.sin(a), math.cos(a)
        shift = {"left": 0, "center": -w / 2, "right": -w}[anchor]
        h = -face.cap * size / 2
        px, py = self.pt(x, y)
        cur = [shift]
        path = self.c.beginPath()

        def place(i, adv):
            t = (tx, ty, nx, ny, px + tx * cur[0] + nx * h, py + ty * cur[0] + ny * h)
            cur[0] += adv + track
            return t
        self._glyphs(path, face, face.shape(s), size, place)
        self.fill(color, alpha)
        self.c.drawPath(path, stroke=0, fill=1, fillMode=1)

    def arc_text(self, s, font, size, cx, cy, r, center_deg, color, alpha=1.0,
                 track=0.0, upright_bottom=False):
        """Текст по окръжност, центриран на ъгъл center_deg (математически, 0° = дясно).
        Отгоре чете по часовника с базова линия на r; отдолу — обратно, изправен,
        с главните букви навътре до r (лентата е една и съща)."""
        face = self.f[font]
        glyphs = face.shape(s)
        w = face.width(s, size, track)
        ox, oy = self.pt(cx, cy)
        rp = r * mm
        sign = 1 if upright_bottom else -1
        if upright_bottom:
            rp += face.cap * size
        phi = [math.radians(center_deg) - sign * (w / 2) / rp]
        path = self.c.beginPath()

        def place(i, adv):
            mid = phi[0] + sign * (adv / 2) / rp
            tx, ty = -sign * math.sin(mid), sign * math.cos(mid)   # посока на реда
            nx, ny = (-math.cos(mid), -math.sin(mid)) if upright_bottom else (math.cos(mid), math.sin(mid))
            bx = ox + rp * math.cos(mid) - tx * adv / 2
            by = oy + rp * math.sin(mid) - ty * adv / 2
            phi[0] += sign * (adv + track) / rp
            return (tx, ty, nx, ny, bx, by)
        self._glyphs(path, face, glyphs, size, place)
        self.fill(color, alpha)
        self.c.drawPath(path, stroke=0, fill=1, fillMode=1)

    def glyph_at(self, ch, font, size, x, y, color, alpha=1.0):
        """Един символ, центриран оптически по ширината и по главната височина."""
        face = self.f[font]
        name = face.shape(ch)[0][0]
        bounds = face.glyphs[name]
        from fontTools.pens.boundsPen import BoundsPen
        bp = BoundsPen(face.glyphs)
        bounds.draw(bp)
        x0, y0, x1, y1 = bp.bounds
        s = size / face.upem
        px, py = self.pt(x, y)
        path = self.c.beginPath()
        self._glyphs(path, face, [(name, 0, 0, 0)], size,
                     lambda i, a: (1, 0, 0, 1, px - (x0 + x1) / 2 * s, py - (y0 + y1) / 2 * s))
        self.fill(color, alpha)
        self.c.drawPath(path, stroke=0, fill=1, fillMode=1)

    def star4(self, x, y, r, color, alpha=1.0, rot=0.0):
        """Четирилъчна звезда с вдлъбнати страни (като искрата на сайта)."""
        px, py = self.pt(x, y)
        rr, k = r * mm, 0.22
        p = self.c.beginPath()
        for i in range(4):
            a = math.radians(rot + 90 * i)
            b = math.radians(rot + 90 * i + 90)
            tip = (px + rr * math.cos(a), py + rr * math.sin(a))
            nxt = (px + rr * math.cos(b), py + rr * math.sin(b))
            if i == 0:
                p.moveTo(*tip)
            p.curveTo(px + rr * k * math.cos(a), py + rr * k * math.sin(a),
                      px + rr * k * math.cos(b), py + rr * k * math.sin(b), *nxt)
        p.close()
        self.fill(color, alpha)
        self.c.drawPath(p, stroke=0, fill=1)


# ── небето ────────────────────────────────────────────────────────────────

PLANETS = [  # (swe id, символ; None = чертаем го)
    (swe.SUN, None), (swe.MOON, "☽"), (swe.MERCURY, "☿"), (swe.VENUS, "♀"),
    (swe.MARS, "♂"), (swe.JUPITER, "♃"), (swe.SATURN, "♄"), (swe.URANUS, "♅"),
    (swe.NEPTUNE, "♆"), (swe.PLUTO, "♇"),
]
ASPECTS = [(60, 4, ICY), (90, 6, PINK), (120, 6, ICY), (180, 7, PINK)]
SIGNS = "♈♉♊♋♌♍♎♏♐♑♒♓"
ROMAN = "I II III IV V VI VII VIII IX X XI XII".split()


def sky():
    jd = swe.julday(*MOMENT)
    bodies = []
    for pid, sym in PLANETS:
        lon, _, _, speed, *_ = swe.calc_ut(jd, pid, swe.FLG_MOSEPH | swe.FLG_SPEED)[0]
        bodies.append({"lon": lon % 360, "sym": sym, "retro": speed < 0})
    cusps, ascmc = swe.houses_ex(jd, LAT, LON, b"P", swe.FLG_MOSEPH)
    return bodies, list(cusps)[:12], ascmc[0], ascmc[1]


def spread(lons, min_gap, rounds=400):
    """Разтваря близките символи по окръжността, без да сменя реда им."""
    order = sorted(range(len(lons)), key=lambda i: lons[i])
    pos = {i: lons[i] for i in order}
    for _ in range(rounds):
        moved = False
        for a, b in zip(order, order[1:] + order[:1]):
            gap = (pos[b] - pos[a]) % 360
            if gap < min_gap:
                d = (min_gap - gap) / 2 + 0.01
                pos[a] -= d
                pos[b] += d
                moved = True
        if not moved:
            break
    return [pos[i] % 360 for i in range(len(lons))]


def draw_wheel(P, cx, cy):
    c = P.c
    bodies, cusps, asc, mc = sky()
    ang = lambda lon: math.radians(180 + lon - asc)  # асцендентът вляво

    def xy(r, lon):
        a = ang(lon)
        return P.pt(cx + r * math.cos(a), cy - r * math.sin(a))

    def ring(r, color, w, alpha=1.0, dash=None):
        P.stroke(color, w, alpha)
        c.setDash(*(dash or ([], 0)))
        x, y = P.pt(cx, cy)
        c.circle(x, y, r * mm, stroke=1, fill=0)
        c.setDash([], 0)

    def radial(lon, r1, r2, color, w, alpha=1.0):
        P.stroke(color, w, alpha)
        c.line(*xy(r1, lon), *xy(r2, lon))

    R_OUT, R_TICK, R_SIGN, R_HOUSE, R_ASP = 63.0, 59.6, 49.5, 44.5, 26.5
    BAND_IN, BAND_OUT = 71.5, 77.0

    # Надписите по ръба: модулите на приложението, като по ръба на астролабия.
    ring(BAND_IN, GILT, 0.35, 0.55)
    ring(BAND_OUT, GILT, 0.35, 0.55)
    for k in range(72):
        lon_deg = k * 5
        a = math.radians(lon_deg)
        r2 = BAND_OUT + (1.9 if k % 6 == 0 else 1.0)
        P.stroke(GILT, 0.3, 0.45)
        c.line(*P.pt(cx + BAND_OUT * math.cos(a), cy - BAND_OUT * math.sin(a)),
               *P.pt(cx + r2 * math.cos(a), cy - r2 * math.sin(a)))
    band_mid = (BAND_IN + BAND_OUT) / 2
    names_top = [(150, "НАТАЛНА КАРТА"), (90, "ДНЕВЕН ХОРОСКОП"), (30, "СЪВМЕСТИМОСТ")]
    names_bot = [(210, "НУМЕРОЛОГИЯ"), (270, "ЛУНЕН КАЛЕНДАР"), (330, "АКАШОВИ ЗАПИСИ")]
    size, track = 5.6, 1.55
    cap_mm = P.f["mono"].cap * size / mm
    for deg, s in names_top:
        P.arc_text(s, "mono", size, cx, cy, band_mid - cap_mm / 2, deg, GILT, 0.85, track)
    for deg, s in names_bot:
        P.arc_text(s, "mono", size, cx, cy, band_mid - cap_mm / 2, deg, GILT, 0.85, track,
                   upright_bottom=True)
    for deg in range(0, 360, 60):
        a = math.radians(deg)
        P.star4(cx + band_mid * math.cos(a), cy - band_mid * math.sin(a), 0.95, GILT, 0.9,
                rot=deg)

    # Градусите: всеки, всеки пети, всеки десети.
    ring(R_OUT, GILT, 0.7, 0.95)
    ring(R_TICK, GILT, 0.35, 0.7)
    for d in range(360):
        if d % 30 == 0:
            continue
        ln = 2.2 if d % 10 == 0 else 1.5 if d % 5 == 0 else 0.85
        radial(d, R_OUT, R_OUT - ln, GILT, 0.28 if d % 5 else 0.4, 0.75)

    # Зодиакът
    ring(R_SIGN, GILT, 0.55, 0.9)
    for i in range(12):
        radial(i * 30, R_SIGN, R_OUT, GILT, 0.55, 0.9)
        gx, gy = xy((R_SIGN + R_TICK) / 2, i * 30 + 15)
        P.glyph_at(SIGNS[i], "sym-light", 10.5, gx / mm - P.off,
                   P.h - P.off - gy / mm, GILT, 0.95)

    # Домовете (Плацидус)
    ring(R_HOUSE, GILT, 0.35, 0.6)
    ring(R_ASP, GILT, 0.45, 0.75)
    # Линиите на домовете са само в своя пръстен; поясът на планетите остава
    # свободен, а към кръга на аспектите има само къса резка.
    for i, cu in enumerate(cusps):
        if i % 3:
            radial(cu, R_HOUSE, R_SIGN, GILT, 0.3, 0.45)
            radial(cu, R_ASP, R_ASP + 1.4, GILT, 0.3, 0.45)
    for i, cu in enumerate(cusps):
        nxt = cusps[(i + 1) % 12]
        mid = cu + ((nxt - cu) % 360) / 2
        hx, hy = xy((R_HOUSE + R_SIGN) / 2, mid)
        P.text(ROMAN[i], "serif-med", 6.2, hx / mm - P.off,
               P.h - P.off - hy / mm + 0.75, THISTLE, 0.7, anchor="center")

    # Осите AC–DC и MC–IC
    for lon, lab in ((asc, "AC"), (asc + 180, "DC"), (mc, "MC"), (mc + 180, "IC")):
        radial(lon, R_HOUSE, R_OUT + 1.6, IVORY, 0.6, 0.9)
        radial(lon, R_ASP, R_ASP + 2.4, IVORY, 0.6, 0.9)
        lx, ly = xy(R_OUT + 4.4, lon)
        P.text(lab, "mono", 5.4, lx / mm - P.off, P.h - P.off - ly / mm + 0.7,
               IVORY, 0.85, track=0.6, anchor="center")

    # Аспектите
    lons = [b["lon"] for b in bodies]
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            d = abs(lons[i] - lons[j]) % 360
            d = min(d, 360 - d)
            for angle, orb, color in ASPECTS:
                if abs(d - angle) <= orb:
                    P.stroke(color, 0.5, 0.75)
                    c.line(*xy(R_ASP, lons[i]), *xy(R_ASP, lons[j]))

    # Планетите
    R_GLYPH, R_DEG = 39.3, 35.3
    shown = spread(lons, 7.5)
    for b, at in zip(bodies, shown):
        radial(b["lon"], R_HOUSE, R_HOUSE - 1.7, IVORY, 0.45, 0.9)
        x, y = xy(R_ASP, b["lon"])
        P.fill(IVORY, 1.0)
        c.circle(x, y, 0.42 * mm, stroke=0, fill=1)
        if abs(((at - b["lon"] + 180) % 360) - 180) > 0.6:
            P.stroke(IVORY, 0.25, 0.4)
            c.line(*xy(R_HOUSE - 1.9, b["lon"]), *xy(R_GLYPH + 2.6, at))
        gx, gy = xy(R_GLYPH, at)
        gx, gy = gx / mm - P.off, P.h - P.off - gy / mm
        if b["sym"] is None:  # Слънцето: кръг с точка
            px, py = P.pt(gx, gy)
            P.stroke(IVORY, 0.6, 1.0)
            c.circle(px, py, 1.5 * mm, stroke=1, fill=0)
            P.fill(IVORY)
            c.circle(px, py, 0.36 * mm, stroke=0, fill=1)
        else:
            P.glyph_at(b["sym"], "sym", 10.5, gx, gy, IVORY)
        # Градусът върви по радиуса към центъра: при близки планети не се застъпва.
        arcmin = int(round(b["lon"] * 60))
        label = f"{arcmin // 60 % 30}°{arcmin % 60:02d}′" + (" R" if b["retro"] else "")
        dx, dy = xy(R_DEG, at)
        a = math.degrees(ang(at))
        right = math.cos(ang(at)) >= 0
        P.text_rot(label, "mono", 4.3, dx / mm - P.off, P.h - P.off - dy / mm,
                   a if right else a + 180, SOFT, 0.9, track=0.15,
                   anchor="right" if right else "left")

    # Кръстчето в центъра
    P.stroke(GILT, 0.3, 0.6)
    x, y = P.pt(cx, cy)
    c.line(x - 1.4 * mm, y, x + 1.4 * mm, y)
    c.line(x, y - 1.4 * mm, x, y + 1.4 * mm)
    return BAND_OUT + 1.9


def draw_stars(P, cx, cy, keep_out_r, soft_zones, seed=1017):
    """Ситен звезден прах с по-гъста ивица по диагонала (Млечния път)."""
    rng = random.Random(seed)
    c = P.c
    colors = [IVORY] * 6 + [ICY] * 2 + [THISTLE]
    # ивицата: от долу-ляво нагоре-дясно, през колелото
    ax, ay, bx, by = -10.0, 250.0, 220.0, 20.0
    L = math.hypot(bx - ax, by - ay)
    placed = 0
    while placed < 1250:
        x = rng.uniform(-P.off, PAGE_W + P.off)
        y = rng.uniform(-P.off, PAGE_H + P.off)
        d = abs((bx - ax) * (ay - y) - (ax - x) * (by - ay)) / L
        if rng.random() > 0.22 + 0.78 * math.exp(-(d / 38.0) ** 2):
            continue
        if math.hypot(x - cx, y - cy) < keep_out_r:
            continue
        placed += 1
        m = rng.random() ** 3.2
        alpha = 0.18 + 0.62 * m
        for (x0, y0, x1, y1, k) in soft_zones:
            if x0 <= x <= x1 and y0 <= y <= y1:
                alpha *= k
        r = 0.2 + 0.45 * m  # под ~0.15 мм точката не се отпечатва
        px, py = P.pt(x, y)
        col = rng.choice(colors)
        if m > 0.55 and alpha > 0.5:  # най-ярките имат ореол
            for k_r, k_a in ((4.5, 0.035), (2.6, 0.07), (1.6, 0.12)):
                P.fill(col, k_a)
                c.circle(px, py, r * k_r, stroke=0, fill=1)
        P.fill(col, alpha)
        c.circle(px, py, r, stroke=0, fill=1)


def draw_qr(P, url, x, y, size):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    m = qr.get_matrix()  # с тихата зона
    n = len(m)
    cell = size / n
    px, py = P.pt(x, y)
    P.fill(PAPER)
    P.c.roundRect(px, py - size * mm, size * mm, size * mm, 1.6 * mm, stroke=0, fill=1)
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
    # маркерите за отчитане в ъглите, като на чертеж
    g, L = 2.2, 3.2
    P.stroke(GILT, 0.45, 0.85)
    for (cx_, cy_, sx, sy) in ((x, y, -1, -1), (x + size, y, 1, -1),
                               (x, y + size, -1, 1), (x + size, y + size, 1, 1)):
        ax, ay = cx_ + sx * g, cy_ + sy * g
        P.c.line(*P.pt(ax, ay), *P.pt(ax - sx * L, ay))
        P.c.line(*P.pt(ax, ay), *P.pt(ax, ay - sy * L))
    return qr.version


def build(out_pdf, url, bleed, store):
    P = Poster(out_pdf, bleed)
    c = P.c
    CX, CY = 105.0, 90.5
    M = 24.0  # полето отляво и отдясно

    # Фонът: нощ с виолетово сияние, което идва от колелото
    gx, gy = P.pt(CX, CY)
    c.radialGradient(gx, gy, 175 * mm,
                     [HexColor("#1e1752"), HexColor("#141039"), HexColor("#0c0a25"),
                      HexColor(NIGHT)], [0.0, 0.3, 0.62, 1.0], extend=True)

    HEAD_Y1, HEAD_Y2 = 188.5, 208.0
    SUB_Y = 220.0
    RULE_Y = 236.0
    QR_SIZE, QR_TOP = 37.0, 243.5
    soft = [(18, 170, 192, 232, 0.25),           # заглавието
            (M - 4, QR_TOP - 4, PAGE_W - M + 4, QR_TOP + QR_SIZE + 4, 0.2)]
    draw_stars(P, CX, CY, 82.0, soft)
    draw_wheel(P, CX, CY)

    # Етикетите на фигурата
    lab = dict(font="mono", size=5.6, color=THISTLE, alpha=0.75, track=1.1)
    P.text("ФИГ. 1", x=M, y=12.6, **lab)
    P.text("НЕБЕТО НАД СОФИЯ", x=M, y=16.4, **lab)
    P.text("17.10.2026 · 12:00", x=PAGE_W - M, y=12.6, anchor="right", **lab)
    P.text("42°41′ С · 23°19′ И", x=PAGE_W - M, y=16.4, anchor="right", **lab)

    # Куката
    hs = 54
    P.text("Това не е", "serif", hs, PAGE_W / 2, HEAD_Y1, TEXT, anchor="center")
    w1 = P.f["serif-med-it"].width("твоето", hs) / mm
    sp = P.f["serif"].width(" ", hs) / mm
    w2 = P.f["serif"].width("небе.", hs) / mm
    x0 = PAGE_W / 2 - (w1 + sp + w2) / 2
    P.text("твоето", "serif-med-it", hs, x0, HEAD_Y2, GILT)
    P.text("небе.", "serif", hs, x0 + w1 + sp, HEAD_Y2, TEXT)
    sub = dict(font="serif-it", size=15.5, color=SOFT, anchor="center")
    P.text("Твоето се е подредило само веднъж —", x=PAGE_W / 2, y=SUB_Y, **sub)
    P.text("в минутата, в която се роди.", x=PAGE_W / 2, y=SUB_Y + 6.6, **sub)

    # Разделител с искра
    P.stroke(GILT, 0.35, 0.5)
    c.line(*P.pt(M, RULE_Y), *P.pt(PAGE_W / 2 - 4, RULE_Y))
    c.line(*P.pt(PAGE_W / 2 + 4, RULE_Y), *P.pt(PAGE_W - M, RULE_Y))
    P.star4(PAGE_W / 2, RULE_Y, 1.5, GILT, 0.9)

    # Призивът и QR кодът
    qx = PAGE_W - M - QR_SIZE
    version = draw_qr(P, url, qx, QR_TOP, QR_SIZE)
    cta_size = 27
    cta_base = QR_TOP + P.f["serif-med"].cap * cta_size / mm
    wcta = P.text("Виж своето.", "serif-med", cta_size, M, cta_base, IVORY)
    # тънка стрелка към кода
    ay = cta_base - P.f["serif-med"].cap * cta_size / mm / 2
    ax0, ax1 = M + wcta + 5, qx - 8.0
    P.stroke(GILT, 0.45, 0.85)
    c.line(*P.pt(ax0, ay), *P.pt(ax1, ay))
    c.line(*P.pt(ax1, ay), *P.pt(ax1 - 1.6, ay - 1.1))
    c.line(*P.pt(ax1, ay), *P.pt(ax1 - 1.6, ay + 1.1))
    body = dict(font="sans", size=8.6, color=SOFT)
    P.text("Насочи камерата на телефона към кода.", x=M, y=cta_base + 8.2, **body)
    P.text("Трябват само дата, час и място на раждане.", x=M, y=cta_base + 12.8, **body)

    # Марката
    logo = 11.5
    lx, ly = P.pt(M, QR_TOP + QR_SIZE)
    c.setFillAlpha(1)
    c.drawImage(str(ROOT / "static" / "logo-full.png"), lx, ly, logo * mm, logo * mm,
                mask="auto")
    base = QR_TOP + QR_SIZE - 4.6
    P.text("АстроКарта", "serif-semi", 19, M + logo + 3.2, base, TEXT)
    P.text("АСТРОЛОГИЯ НА РАЗБИРАЕМ ЕЗИК", "mono", 5.4, M + logo + 3.4,
           QR_TOP + QR_SIZE - 0.2, GILT, 0.9, track=1.25)
    # Адресът под кода — за който не сканира.
    P.text("astrokarta.bg", "mono", 6.6, qx + QR_SIZE / 2, QR_TOP + QR_SIZE + 5.4,
           SOFT, 0.95, track=0.9, anchor="center")
    if store:  # кой плакат е това, когато са няколко
        P.text(store.upper(), "mono", 4.4, M, PAGE_H - 6.0, SOFT, 0.45, track=1.0)
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
