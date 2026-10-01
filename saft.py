"""
Стандартизиран одиторски файл (Приложение № 38) за електронни магазини.

Наредба № Н-18, чл. 3, ал. 17 и чл. 52т, ал. 2–3: файлът за всеки
календарен месец се подава до 15-о число на следващия месец през
е-услугата на НАП с КЕП. От 01.01.2026 сумите са в евро (ДВ бр. 42/2025);
структурата на XSD е същата.

XSD схема: n18/dec_audit.xsd (официалната от НАП, байт по байт; копие и в
docs/n18/). Пример: docs/n18/vik_simple.xml.

Изходният файл е в windows-1251, както XSD-то и примера на НАП.

Модулът е чист — не знае нищо за базата данни. app.py подготвя поръчките
(в евроцентове) и вика `build_saft_xml(...)`, после `validate_saft(...)`.
Всички суми се смятат в цели центове: файлът минава и аритметичната
проверка на НАП (сборът на редовете = общата сума, до стотинка).
"""

import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

# Кодове на плащане (paym) по XSD.
PAYM_FREE = "1"          # Освободено по чл. 3 плащане без ППП
PAYM_VIRTUAL_POS = "2"   # Виртуален ПОС - терминал
PAYM_COD_PPP = "3"       # Наложен платеж с ППП
PAYM_PSP = "4"           # Доставчик на платежни услуги (Stripe и др.)
PAYM_OTHER = "5"         # Друг вид плащане, неизискващо фискален бон
PAYM_FISCAL = "6"        # плащане, отразено с фискален бон

# Кодове за връщане (r_paym) по XSD: 1 - по платежна сметка; 2 - по карта;
# 3 - в брой; 4 - друг. (До тази версия картата и сметката бяха разменени.)
RPAYM_ACCOUNT = "1"
RPAYM_CARD = "2"
RPAYM_CASH = "3"
RPAYM_OTHER = "4"
REFUND_METHOD_CODES = {"account": RPAYM_ACCOUNT, "card": RPAYM_CARD,
                       "cash": RPAYM_CASH, "other": RPAYM_OTHER}

# ДДС ставка за цифровите услуги (20 % в България).
VAT_RATE = 20

# Данъчни групи по чл. 27 от Н-18 (за документа към клиента).
TAX_GROUPS = {0: "А", 20: "Б", 9: "Г"}

XSD_PATH = Path(__file__).resolve().parent / "n18" / "dec_audit.xsd"


def tax_group(vat_rate: int) -> str:
    return TAX_GROUPS.get(int(vat_rate), "Б")


def money(cents) -> str:
    """Центове → „12.34“ (точно, без float)."""
    cents = int(cents)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def _f(value) -> str:
    """Форматира число с точно 2 десетични знака (за стари извиквания)."""
    return f"{float(value or 0):.2f}"


def vat_cents_of(gross_cents: int, vat_rate: int = VAT_RATE) -> int:
    """ДДС, съдържащ се в бруто сума, закръглен до стотинка (половинката нагоре)."""
    if vat_rate <= 0:
        return 0
    return (int(gross_cents) * vat_rate * 2 + (100 + vat_rate)) // (2 * (100 + vat_rate))


def split_vat(gross: float, vat_rate: int = VAT_RATE):
    """От бруто сума (с ДДС) връща (нето, ддс) в евро."""
    cents = int(round(float(gross or 0) * 100))
    vat = vat_cents_of(cents, vat_rate)
    return (cents - vat) / 100.0, vat / 100.0


def build_saft_xml(*, eik, e_shop_n, domain_name, e_shop_type,
                   month, year, orders, refunds=None, creation_date=None) -> bytes:
    """Изгражда целия <audit> документ и го връща като windows-1251 bytes.

    orders: list[dict] с ключове:
        ord_n (str), ord_d (ГГГГ-ММ-ДД), doc_n (int), doc_date (ГГГГ-ММ-ДД),
        items (list[dict]: name, quant (int), sum_cents (с ДДС), vat_rate, vat_cents),
        disc_cents (int), paym (str), pos_n, trans_n, proc_id (str, може празни)
    refunds: list[dict] с ключове ord_n, amount_cents, date, paym (код по XSD).

    Единичната цена е без ДДС (art_price), art_vat — ДДС за реда, art_sum —
    редът с ДДС; тоталите на поръчката са сборове на редовете.
    """
    audit = ET.Element("audit")

    ET.SubElement(audit, "eik").text = str(eik)
    ET.SubElement(audit, "e_shop_n").text = str(e_shop_n)
    ET.SubElement(audit, "domain_name").text = str(domain_name)
    ET.SubElement(audit, "e_shop_type").text = str(e_shop_type)
    ET.SubElement(audit, "creation_date").text = creation_date or datetime.now().strftime("%Y-%m-%d")
    ET.SubElement(audit, "mon").text = str(month).zfill(2)
    ET.SubElement(audit, "god").text = str(year)

    order = ET.SubElement(audit, "order")
    for o in orders:
        oe = ET.SubElement(order, "orderenum")
        ET.SubElement(oe, "ord_n").text = str(o["ord_n"])
        ET.SubElement(oe, "ord_d").text = str(o["ord_d"])
        ET.SubElement(oe, "doc_n").text = str(int(o["doc_n"]))
        ET.SubElement(oe, "doc_date").text = str(o["doc_date"])

        art = ET.SubElement(oe, "art")
        net_total = vat_total = gross_total = 0
        for it in o.get("items") or []:
            quant = int(it.get("quant", 1)) or 1
            gross = int(it["sum_cents"])
            vat = int(it["vat_cents"])
            net = gross - vat
            net_total += net
            vat_total += vat
            gross_total += gross
            an = ET.SubElement(art, "artenum")
            ET.SubElement(an, "art_name").text = str(it["name"])[:200]
            ET.SubElement(an, "art_quant").text = f"{quant}.00"
            # Единична цена без ДДС. Нетото на реда трябва да се дели точно на
            # количеството — иначе редът не би се връзвал до стотинка.
            if net % quant:
                raise ValueError(f"нетото {net} не се дели на количеството {quant}")
            ET.SubElement(an, "art_price").text = money(net // quant)
            ET.SubElement(an, "art_vat_rate").text = str(int(it.get("vat_rate", VAT_RATE)))
            ET.SubElement(an, "art_vat").text = money(vat)
            ET.SubElement(an, "art_sum").text = money(gross)

        ET.SubElement(oe, "ord_total1").text = money(net_total)
        ET.SubElement(oe, "ord_disc").text = money(int(o.get("disc_cents") or 0))
        ET.SubElement(oe, "ord_vat").text = money(vat_total)
        ET.SubElement(oe, "ord_total2").text = money(gross_total - int(o.get("disc_cents") or 0))
        ET.SubElement(oe, "paym").text = str(o.get("paym", PAYM_PSP))
        ET.SubElement(oe, "pos_n").text = str(o.get("pos_n") or "")
        ET.SubElement(oe, "trans_n").text = str(o.get("trans_n") or "")
        ET.SubElement(oe, "proc_id").text = str(o.get("proc_id") or "")

    refunds = refunds or []
    # Винаги присъстват (както в примера на НАП) — нула, когато няма връщания.
    ET.SubElement(audit, "r_ord").text = str(len({str(r["ord_n"]) for r in refunds}))
    ro = ET.SubElement(audit, "rorder")
    for r in refunds:
        re_ = ET.SubElement(ro, "rorderenum")
        ET.SubElement(re_, "r_ord_n").text = str(r["ord_n"])
        ET.SubElement(re_, "r_amount").text = money(r["amount_cents"])
        ET.SubElement(re_, "r_date").text = str(r["date"])
        ET.SubElement(re_, "r_paym").text = str(r.get("paym") or RPAYM_CARD)
    ET.SubElement(audit, "r_total").text = money(sum(int(r["amount_cents"]) for r in refunds))

    ET.indent(audit, space="\t")
    body = ET.tostring(audit, encoding="windows-1251", xml_declaration=False)
    return b'<?xml version="1.0" encoding="windows-1251"?>\n' + body + b"\n"


def _cents(text) -> int:
    from decimal import Decimal
    return int((Decimal(str(text or "0").strip() or "0") * 100).to_integral_value())


_SCHEMAS: dict = {}


def _schema(xsd_path: Path):
    """Схемата се чете веднъж — разборът ѝ е по-бавен от самата проверка."""
    key = str(xsd_path)
    if key not in _SCHEMAS:
        import xmlschema  # по желание, само за проверката
        _SCHEMAS[key] = xmlschema.XMLSchema(key)
    return _SCHEMAS[key]


def validate_saft(xml_bytes: bytes, xsd_path: Path = XSD_PATH) -> list:
    """Списък с проблеми (празен = файлът е готов за подаване).

    1. Схемата на НАП (XSD) — ако библиотеката xmlschema е налична.
    2. Аритметиката: всеки ред и всяка поръчка да се връзват до стотинка —
       НАП проверява логическа и аритметична коректност при качване.
    3. Незапълнени данни („[[…]]“) и месец без поръчки — XSD-то изисква
       поне една поръчка, затова празен файл не минава.
    """
    problems = []
    try:
        schema = _schema(xsd_path)
        for err in schema.iter_errors(xml_bytes.decode("windows-1251")):
            problems.append(f"XSD: {err.reason} ({err.path})")
    except ImportError:
        problems.append("XSD проверката не е налична (липсва пакетът xmlschema).")
    except Exception as e:      # повредена схема и т.н. — не пречи на останалото
        problems.append(f"XSD проверката не можа да се изпълни: {e}")

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        return problems + [f"Файлът не е валиден XML: {e}"]

    for tag in ("eik", "e_shop_n", "domain_name"):
        value = (root.findtext(tag) or "").strip()
        if not value or "[[" in value:
            problems.append(f"Полето {tag} не е попълнено (Админ → Настройки → Юридически данни).")

    orders = root.findall("./order/orderenum")
    if not orders:
        problems.append("Няма поръчки за месеца — схемата на НАП не допуска файл без поръчки.")
    for oe in orders:
        ord_n = oe.findtext("ord_n")
        net = vat = gross = 0
        for an in oe.findall("./art/artenum"):
            price = _cents(an.findtext("art_price"))
            quant = _cents(an.findtext("art_quant")) // 100 or 1
            a_vat = _cents(an.findtext("art_vat"))
            a_sum = _cents(an.findtext("art_sum"))
            if price * quant + a_vat != a_sum:
                problems.append(f"Поръчка {ord_n}: ред „{an.findtext('art_name')}“ не се връзва "
                                f"({price * quant / 100:.2f} + {a_vat / 100:.2f} ≠ {a_sum / 100:.2f}).")
            net += price * quant
            vat += a_vat
            gross += a_sum
        total1 = _cents(oe.findtext("ord_total1"))
        o_vat = _cents(oe.findtext("ord_vat"))
        disc = _cents(oe.findtext("ord_disc"))
        total2 = _cents(oe.findtext("ord_total2"))
        if total1 != net or o_vat != vat or total2 != gross - disc or total1 + o_vat - disc != total2:
            problems.append(f"Поръчка {ord_n}: тоталите не съвпадат със сбора на редовете.")
        doc_n = (oe.findtext("doc_n") or "").strip()
        if not doc_n.isdigit():
            problems.append(f"Поръчка {ord_n}: номерът на документа не е число.")

    refunds = root.findall("./rorder/rorderenum")
    r_total = _cents(root.findtext("r_total"))
    if sum(_cents(r.findtext("r_amount")) for r in refunds) != r_total:
        problems.append("Сборът на върнатите суми не съвпада с r_total.")
    return problems
