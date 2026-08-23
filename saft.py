"""
Генератор на Стандартизиран одиторски файл (SAF-T) за електронни магазини.

Наредба № Н-18 от 13.12.2006 г., чл. 3, ал. 17.
XSD схема: docs/n18/dec_audit.xsd (официална, от НАП)
Пример:    docs/n18/vik_simple.xml

Изходният файл е в windows-1251 (според XSD-то и примера на НАП).

Този модул е чист — не знае нищо за базата данни. app.py подготвя
списъка с поръчки и вика `build_saft_xml(...)`.
"""

import xml.etree.ElementTree as ET
from datetime import datetime

# Кодове на плащане (paym) по XSD.
PAYM_FREE = "1"          # Освободено по чл. 3 плащане без ППП
PAYM_VIRTUAL_POS = "2"   # Виртуален ПОС - терминал
PAYM_COD_PPP = "3"       # Наложен платеж с ППП
PAYM_PSP = "4"           # Доставчик на платежни услуги (Stripe и др.)
PAYM_OTHER = "5"         # Друг вид плащане, неизискващо фискален бон
PAYM_FISCAL = "6"        # плащане, отразено с фискален бон

# Кодове за връщане (r_paym) по XSD.
RPAYM_CARD = "1"   # на платежна карта
RPAYM_BANK = "2"   # по банков път
RPAYM_CASH = "3"   # в брой
RPAYM_OTHER = "4"  # друго

# ДДС ставка за цифровите услуги (20 % в България). Ако администраторът не е
# регистриран по ЗДДС, сложи 0 — тогава art_vat_rate = 0 и ДДС не се начислява.
VAT_RATE = 20


def _f(value) -> str:
    """Форматира число с точно 2 десетични знака."""
    return f"{float(value or 0):.2f}"


def split_vat(gross: float, vat_rate: int = VAT_RATE):
    """От бруто сума (с ДДС) връща (нето, ддс)."""
    gross = float(gross or 0)
    if vat_rate <= 0:
        return gross, 0.0
    vat = round(gross * vat_rate / (100 + vat_rate), 2)
    return round(gross - vat, 2), vat


def build_saft_xml(*, eik, e_shop_n, domain_name, e_shop_type,
                   month, year, orders, refunds=None, creation_date=None) -> bytes:
    """Изгражда целия <audit> документ и го връща като windows-1251 bytes.

    orders: list[dict] с ключове:
        ord_n (str), ord_d (date), doc_n (int), doc_date (date),
        items (list[dict]: name, quant, price, vat_rate, vat, total),
        total1, disc, vat, total2 (float), paym (str),
        pos_n, trans_n, proc_id (str, може празни)
    refunds: list[dict] с ключове ord_n, amount, date, paym (по избор).
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
        ET.SubElement(oe, "doc_n").text = str(o["doc_n"])
        ET.SubElement(oe, "doc_date").text = str(o["doc_date"])

        art = ET.SubElement(oe, "art")
        for it in o.get("items") or []:
            an = ET.SubElement(art, "artenum")
            ET.SubElement(an, "art_name").text = str(it["name"])
            ET.SubElement(an, "art_quant").text = _f(it.get("quant", 1))
            ET.SubElement(an, "art_price").text = _f(it.get("price", 0))
            ET.SubElement(an, "art_vat_rate").text = str(int(it.get("vat_rate", VAT_RATE)))
            ET.SubElement(an, "art_vat").text = _f(it.get("vat", 0))
            ET.SubElement(an, "art_sum").text = _f(it.get("total", 0))

        ET.SubElement(oe, "ord_total1").text = _f(o.get("total1", 0))
        ET.SubElement(oe, "ord_disc").text = _f(o.get("disc", 0))
        ET.SubElement(oe, "ord_vat").text = _f(o.get("vat", 0))
        ET.SubElement(oe, "ord_total2").text = _f(o.get("total2", 0))
        ET.SubElement(oe, "paym").text = str(o.get("paym", PAYM_PSP))
        ET.SubElement(oe, "pos_n").text = str(o.get("pos_n") or "")
        ET.SubElement(oe, "trans_n").text = str(o.get("trans_n") or "")
        ET.SubElement(oe, "proc_id").text = str(o.get("proc_id") or "")

    refunds = refunds or []
    if refunds:
        ET.SubElement(audit, "r_ord").text = str(len(refunds))
        ro = ET.SubElement(audit, "rorder")
        for r in refunds:
            re_ = ET.SubElement(ro, "rorderenum")
            ET.SubElement(re_, "r_ord_n").text = str(r["ord_n"])
            ET.SubElement(re_, "r_amount").text = _f(r.get("amount", 0))
            ET.SubElement(re_, "r_date").text = str(r["date"])
            ET.SubElement(re_, "r_paym").text = str(r.get("paym", RPAYM_CARD))
        ET.SubElement(audit, "r_total").text = _f(sum(float(r.get("amount", 0)) for r in refunds))

    # windows-1251, както изисква XSD-то; кирилицата в имената на модулите се
    # кодира коректно в cp1251.
    return ET.tostring(audit, encoding="windows-1251", xml_declaration=True)
