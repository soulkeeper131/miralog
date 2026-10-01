# -*- coding: utf-8 -*-
"""
PDF reports for readings.

Takes the same markdown-ish text the web UI renders and lays it out as a
typeset document: cover page, section headings, bullet lists and a footer.
"""

import io
import os
import re
import datetime
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer,
    Table, TableStyle, KeepTogether,
)

_SOFIA = ZoneInfo("Europe/Sofia")

# Brand colours, matching the app's light theme.
ACCENT = colors.HexColor("#8659a3")
ACCENT_LIGHT = colors.HexColor("#cdb4db")
HEADING = colors.HexColor("#6d4a89")
BODY = colors.HexColor("#2d2438")
MUTED = colors.HexColor("#7a6d8a")
RULE = colors.HexColor("#e5d4ec")
SURFACE = colors.HexColor("#faf7fb")

_FONTS_READY = False


def _register_fonts() -> tuple:
    """Find a Cyrillic-capable font family, preferring DejaVu then the system's.

    Returns (regular, bold, italic) font names already registered with reportlab.
    """
    global _FONTS_READY
    if _FONTS_READY:
        return ("BodyFont", "BodyFont-Bold", "BodyFont-Italic")

    candidates = [
        # (regular, bold, italic) — first set that exists wins.
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"),
        ("C:/Windows/Fonts/arial.ttf",
         "C:/Windows/Fonts/arialbd.ttf",
         "C:/Windows/Fonts/ariali.ttf"),
        ("C:/Windows/Fonts/segoeui.ttf",
         "C:/Windows/Fonts/segoeuib.ttf",
         "C:/Windows/Fonts/segoeuii.ttf"),
    ]
    for regular, bold, italic in candidates:
        if os.path.exists(regular) and os.path.exists(bold):
            pdfmetrics.registerFont(TTFont("BodyFont", regular))
            pdfmetrics.registerFont(TTFont("BodyFont-Bold", bold))
            pdfmetrics.registerFont(
                TTFont("BodyFont-Italic", italic if os.path.exists(italic) else regular))
            pdfmetrics.registerFontFamily(
                "BodyFont", normal="BodyFont", bold="BodyFont-Bold",
                italic="BodyFont-Italic", boldItalic="BodyFont-Bold")
            _FONTS_READY = True
            return ("BodyFont", "BodyFont-Bold", "BodyFont-Italic")

    # Helvetica has no Cyrillic glyphs, but a broken PDF beats a crash.
    return ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique")


def _styles():
    regular, bold, italic = _register_fonts()
    ss = getSampleStyleSheet()

    return {
        "title": ParagraphStyle(
            "MTitle", parent=ss["Title"], fontName=bold, fontSize=30, leading=34,
            textColor=HEADING, alignment=TA_CENTER, spaceAfter=6),
        "subtitle": ParagraphStyle(
            "MSubtitle", parent=ss["Normal"], fontName=regular, fontSize=12, leading=17,
            textColor=MUTED, alignment=TA_CENTER, spaceAfter=4),
        "cover_meta": ParagraphStyle(
            "MCoverMeta", parent=ss["Normal"], fontName=regular, fontSize=10.5, leading=16,
            textColor=BODY, alignment=TA_CENTER),
        "h1": ParagraphStyle(
            "MH1", parent=ss["Heading1"], fontName=bold, fontSize=15, leading=20,
            textColor=HEADING, spaceBefore=16, spaceAfter=8),
        "h2": ParagraphStyle(
            "MH2", parent=ss["Heading2"], fontName=bold, fontSize=12.5, leading=17,
            textColor=ACCENT, spaceBefore=12, spaceAfter=6),
        "body": ParagraphStyle(
            "MBody", parent=ss["Normal"], fontName=regular, fontSize=10.5, leading=16.5,
            textColor=BODY, alignment=TA_JUSTIFY, spaceAfter=7),
        "bullet": ParagraphStyle(
            "MBullet", parent=ss["Normal"], fontName=regular, fontSize=10.5, leading=16,
            textColor=BODY, leftIndent=14, bulletIndent=3, spaceAfter=4),
        "card_label": ParagraphStyle(
            "MCardLabel", parent=ss["Normal"], fontName=regular, fontSize=7.5, leading=10,
            textColor=MUTED, alignment=TA_CENTER),
        "card_value": ParagraphStyle(
            "MCardValue", parent=ss["Normal"], fontName=bold, fontSize=11, leading=14,
            textColor=HEADING, alignment=TA_CENTER),
    }


def _inline(text: str) -> str:
    """Convert **bold** / *italic* to reportlab markup, escaping the rest."""
    text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    text = re.sub(r"\*\*([^*]+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(^|[^*])\*([^*\n]+?)\*(?!\*)", r"\1<i>\2</i>", text)
    return text


def _parse(text: str, st: dict) -> list:
    """Turn the reading's markdown into flowables."""
    flow = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue

        if re.fullmatch(r"(-{3,}|_{3,}|\*{3,})", line):
            flow.append(Spacer(1, 6))
            continue

        md = re.match(r"^(#{1,6})\s+(.*)$", line)
        if md:
            level = len(md.group(1))
            style = st["h1"] if level <= 2 else st["h2"]
            flow.append(Paragraph(_inline(md.group(2).rstrip(":：")), style))
            continue

        numbered = re.match(r"^(\d+)[.)]\s*\*\*(.+?)\*\*[:：]?\s*(.*)$", line)
        if numbered:
            flow.append(Paragraph(
                f"{numbered.group(1)}. {_inline(numbered.group(2))}", st["h1"]))
            if numbered.group(3):
                flow.append(Paragraph(_inline(numbered.group(3)), st["body"]))
            continue

        if re.fullmatch(r"\*\*[^*]+\*\*[:：]?", line):
            flow.append(Paragraph(_inline(line.strip("*").rstrip(":：")), st["h1"]))
            continue

        bullet = re.match(r"^[-•]\s+(.*)$", line) or re.match(r"^\*(?!\*)\s+(.*)$", line)
        if bullet:
            flow.append(Paragraph(_inline(bullet.group(1)), st["bullet"], bulletText="•"))
            continue

        num_item = re.match(r"^(\d+)[.)]\s+(.+)$", line)
        if num_item:
            flow.append(Paragraph(
                _inline(num_item.group(2)), st["bullet"], bulletText=f"{num_item.group(1)}."))
            continue

        flow.append(Paragraph(_inline(line), st["body"]))
    return flow


def _fact_cards(facts: list, st: dict):
    """A row of small labelled cards, like the ones in the app."""
    if not facts:
        return None
    cells, tints = [], [
        colors.HexColor("#f3ecf7"), colors.HexColor("#eaf3fd"),
        colors.HexColor("#fdeef4"), colors.HexColor("#eef5fd"),
    ]
    for label, value in facts:
        cells.append([
            Paragraph(label.upper(), st["card_label"]),
            Paragraph(str(value), st["card_value"]),
        ])

    inner = [Table([[c[0]], [c[1]]], rowHeights=[11, 16]) for c in cells]
    for i, t in enumerate(inner):
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), tints[i % len(tints)]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))

    width = (A4[0] - 40 * mm) / max(len(inner), 1)
    row = Table([inner], colWidths=[width] * len(inner))
    row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]))
    return row


def build_reading_pdf(*, title: str, person_name: str, subtitle: str = "",
                      facts: list = None, body: str = "",
                      logo_path: str = None, brand: str = "АстроКарта") -> bytes:
    """Render one reading as a PDF and return the bytes."""
    st = _styles()
    regular, bold, _ = _register_fonts()
    buf = io.BytesIO()

    generated = datetime.datetime.now().strftime("%d.%m.%Y")

    def decorate(canvas, doc):
        canvas.saveState()
        # A slim accent bar along the top of every page.
        canvas.setFillColor(ACCENT_LIGHT)
        canvas.rect(0, A4[1] - 6 * mm, A4[0], 6 * mm, stroke=0, fill=1)
        canvas.setFont(regular, 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(20 * mm, 12 * mm, f"{brand} · {person_name}")
        canvas.drawRightString(A4[0] - 20 * mm, 12 * mm, f"стр. {doc.page}")
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(20 * mm, 16 * mm, A4[0] - 20 * mm, 16 * mm)
        canvas.restoreState()

    doc = BaseDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=22 * mm,
        title=f"{title} — {person_name}", author=brand,
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(id="all", frames=[frame], onPage=decorate)])

    story = []

    if logo_path and os.path.exists(logo_path):
        from reportlab.platypus import Image as RLImage
        try:
            img = RLImage(logo_path, width=32 * mm, height=32 * mm, kind="proportional")
            img.hAlign = "CENTER"
            story.extend([Spacer(1, 6 * mm), img])
        except Exception:
            pass

    story.extend([
        Spacer(1, 4 * mm),
        Paragraph(title, st["title"]),
        Paragraph(person_name, st["subtitle"]),
    ])
    if subtitle:
        story.append(Paragraph(subtitle, st["cover_meta"]))
    story.append(Spacer(1, 6 * mm))

    cards = _fact_cards(facts or [], st)
    if cards:
        story.extend([cards, Spacer(1, 5 * mm)])

    story.append(Spacer(1, 2 * mm))
    story.extend(_parse(body, st))

    story.extend([
        Spacer(1, 8 * mm),
        Paragraph(
            f"Изготвено на {generated} · Позициите са изчислени със Swiss Ephemeris. "
            "Разчитането е за размисъл и себепознание.",
            ParagraphStyle("MFoot", fontName=regular, fontSize=8, leading=12,
                           textColor=MUTED, alignment=TA_CENTER)),
    ])

    doc.build(story)
    return buf.getvalue()


# --- Фискални документи: касов документ (Н-18) и фактура (ЗДДС) ---


def _items_table(items, regular, bold, sale_document=False):
    """Таблица с артикули: име, ед. цена (без ДДС), ДДС, общо (с ДДС).

    За документа за регистриране на продажба (Н-18, чл. 52о, ал. 1, т. 5):
    наименование, код на данъчна група, количество, единична цена и стойност.
    """
    cell = ParagraphStyle("Cell", fontName=regular, fontSize=9, leading=11, textColor=BODY)
    if sale_document:
        header = ["Наименование", "Дан. група", "К-во", "Ед. цена", "Стойност"]
        rows = [header]
        for it in items:
            quant = int(it.get("quant", 1) or 1)
            total = float(it.get("total", 0))
            rows.append([
                Paragraph(_inline(str(it.get("name", ""))), cell),
                str(it.get("tax_group", "Б")),
                str(quant),
                f"{total / quant:.2f} EUR",
                f"{total:.2f} EUR",
            ])
        widths = [64 * mm, 22 * mm, 14 * mm, 35 * mm, 35 * mm]
    else:
        header = ["Артикул", "Ед. цена (без ДДС)", "ДДС", "Общо (с ДДС)"]
        rows = [header]
        for it in items:
            rows.append([
                Paragraph(_inline(str(it.get("name", ""))), cell),
                f"{float(it.get('net', 0)):.2f} EUR",
                f"{float(it.get('vat', 0)):.2f} EUR",
                f"{float(it.get('total', 0)):.2f} EUR",
            ])
        widths = [70 * mm, 36 * mm, 30 * mm, 32 * mm]
    t = Table(rows, colWidths=widths, repeatRows=1)
    centered = [("ALIGN", (1, 0), (2, -1), "CENTER")] if sale_document else []
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), bold),
        ("FONTNAME", (0, 1), (-1, -1), regular),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("LEADING", (0, 0), (-1, -1), 12),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, 0), (-1, 0), HEADING),
        ("TEXTCOLOR", (0, 1), (-1, -1), BODY),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SURFACE]),
    ] + centered))
    return t


def _totals_table(net_total, vat_total, total, vat_rate, regular, bold):
    rows = [
        ["Данъчна основа (без ДДС):", f"{float(net_total):.2f} EUR"],
        [f"ДДС ({int(vat_rate)}%):", f"{float(vat_total):.2f} EUR"],
        ["ОБЩО ЗА ПЛАЩАНЕ:", f"{float(total):.2f} EUR"],
    ]
    t = Table(rows, colWidths=[112 * mm, 56 * mm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), regular),
        ("FONTNAME", (1, 0), (1, -1), bold),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -2), BODY),
        ("TEXTCOLOR", (1, 0), (1, -2), BODY),
        ("TEXTCOLOR", (0, -1), (1, -1), HEADING),
        ("BACKGROUND", (0, -1), (-1, -1), SURFACE),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, HEADING),
    ]))
    return t


def _document_layout(*, title, subtitle, meta, items, net_total, vat_total,
                     total, vat_rate, footer_lines, brand, logo_path,
                     sale_document=False, qr_data=None) -> bytes:
    """Общ layout за фискални документи: header, мета, таблица, тотали, footer."""
    regular, bold, _ = _register_fonts()
    st = _styles()
    buf = io.BytesIO()

    def decorate(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(ACCENT_LIGHT)
        canvas.rect(0, A4[1] - 6 * mm, A4[0], 6 * mm, stroke=0, fill=1)
        canvas.setFont(bold, 10)
        canvas.setFillColor(HEADING)
        canvas.drawString(20 * mm, A4[1] - 15 * mm, brand)
        canvas.setFont(regular, 8)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(A4[0] - 20 * mm, A4[1] - 15 * mm, title)
        canvas.setFont(regular, 8)
        canvas.setFillColor(MUTED)
        # Местно време — същото като часа в самия документ.
        canvas.drawString(20 * mm, 12 * mm,
                          f"{brand} · {datetime.datetime.now(_SOFIA).strftime('%d.%m.%Y %H:%M')}")
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(20 * mm, 16 * mm, A4[0] - 20 * mm, 16 * mm)
        canvas.restoreState()

    doc = BaseDocTemplate(buf, pagesize=A4,
                          leftMargin=20 * mm, rightMargin=20 * mm,
                          topMargin=24 * mm, bottomMargin=22 * mm,
                          title=title, author=brand)
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(id="all", frames=[frame], onPage=decorate)])

    story = []

    if logo_path and os.path.exists(logo_path):
        from reportlab.platypus import Image as RLImage
        try:
            img = RLImage(logo_path, width=18 * mm, height=18 * mm, kind="proportional")
            img.hAlign = "CENTER"
            story.extend([Spacer(1, 2 * mm), img])
        except Exception:
            pass

    story.append(Paragraph(title, ParagraphStyle(
        "DocTitle", parent=st["title"], fontSize=20, leading=24, spaceAfter=2)))
    if subtitle:
        story.append(Paragraph(subtitle, st["subtitle"]))

    for label, value in meta:
        story.append(Paragraph(
            f"<b>{_inline(label)}</b> {_inline(str(value))}",
            ParagraphStyle("Meta", parent=st["body"], fontSize=9.5, leading=14,
                           alignment=0, spaceAfter=1)))
    story.append(Spacer(1, 6 * mm))

    story.append(_items_table(items, regular, bold, sale_document=sale_document))
    story.append(Spacer(1, 4 * mm))
    story.append(_totals_table(net_total, vat_total, total, vat_rate, regular, bold))

    if qr_data:
        # QR код по Приложение № 18а — не по-малък от 18 × 18 мм.
        from reportlab.graphics.barcode import qr as rl_qr
        from reportlab.graphics.shapes import Drawing
        widget = rl_qr.QrCodeWidget(qr_data)
        x1, y1, x2, y2 = widget.getBounds()
        size = 32 * mm
        drawing = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
        drawing.add(widget)
        drawing.hAlign = "LEFT"
        story.append(Spacer(1, 6 * mm))
        story.append(drawing)

    if footer_lines:
        story.append(Spacer(1, 6 * mm))
        for line in footer_lines:
            story.append(Paragraph(_inline(line), ParagraphStyle(
                "Foot", parent=st["body"], fontSize=8.5, leading=12,
                textColor=MUTED, alignment=0, spaceAfter=1)))

    doc.build(story)
    return buf.getvalue()


def build_receipt_pdf(*, brand, company_name, company_id, address, contact,
                      domain, e_shop_n, doc_number, issued_str, order_no, trans_ref,
                      items, net_total, vat_total, total, vat_rate, payment_method,
                      qr_data, logo_path=None) -> bytes:
    """Документ за регистриране на продажба по Наредба № Н-18, чл. 52о, ал. 1.

    Реквизитите по реда на чл. 52о, ал. 1: т. 1 — наименование, номер и дата;
    т. 2 — данните на търговеца; т. 3 — номер на поръчката; т. 4 —
    референтен номер на трансакцията; т. 5 — артикулите с данъчна група,
    количество, единична цена, стойност, обща сума и начин на плащане;
    т. 6 — QR код по Приложение № 18а.
    """
    meta = [
        ("Търговец:", company_name or "—"),
        ("ЕИК:", company_id or "—"),
        ("Седалище и адрес на управление:", address or "—"),
        ("Контакт:", contact or "—"),
        ("Електронен магазин:", f"{domain} (№ в НАП: {e_shop_n or '—'})"),
        ("Номер на поръчката:", order_no),
        ("Референтен номер на трансакцията:", trans_ref or "—"),
        ("Начин на плащане:", payment_method),
    ]
    footer_lines = [
        "Документът е издаден по реда на чл. 52о от Наредба № Н-18 при "
        "неприсъствено плащане с банкова карта.",
    ]
    return _document_layout(
        title="ДОКУМЕНТ ЗА РЕГИСТРИРАНЕ НА ПРОДАЖБА",
        subtitle=f"№ {doc_number} от {issued_str}",
        meta=meta, items=items, net_total=net_total, vat_total=vat_total,
        total=total, vat_rate=vat_rate, footer_lines=footer_lines,
        brand=brand, logo_path=logo_path, sale_document=True, qr_data=qr_data)


def build_invoice_pdf(*, brand, company_name, company_id, vat_number, address,
                      invoice_number, issued_at, items, net_total, vat_total,
                      vat_rate, total, logo_path=None) -> bytes:
    """Фактура по ЗДДС (чл. 114)."""
    meta = [
        ("Доставчик:", company_name or "—"),
        ("ЕИК:", company_id or "—"),
        ("ДДС номер:", vat_number or "—"),
        ("Адрес:", address or "—"),
        ("Получател:", "Физическо лице"),
    ]
    footer_lines = [
        f"Дата на данъчното събитие: {issued_at}",
    ]
    return _document_layout(
        title=f"ФАКТУРА № {invoice_number}",
        subtitle=f"Дата на издаване: {issued_at}",
        meta=meta, items=items, net_total=net_total, vat_total=vat_total,
        total=total, vat_rate=vat_rate, footer_lines=footer_lines,
        brand=brand, logo_path=logo_path)
