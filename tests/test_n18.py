# -*- coding: utf-8 -*-
"""Плащания и Наредба Н-18: редовете на продажбата, документът за клиента,
месечният одиторски файл и автоматичното му генериране.

Всеки тест описва положение, което струва пари или неприятности с НАП:
грешна сума, изчезнала продажба, двоен документ, отключен отнет модул.
"""
import datetime
import secrets
import sqlite3
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

SOFIA = ZoneInfo("Europe/Sofia")


def _session(user_id, keys, amount, *, amounts=None, bundle=False, subtotal=None,
             status="paid", kind="features", session_id=None, intent=None, email="buyer@example.com"):
    meta = {"kind": kind, "user_id": str(user_id)}
    if kind == "features":
        meta["feature_keys"] = ",".join(keys)
        if amounts:
            meta["feature_amounts"] = ",".join(f"{k}:{a}" for k, a in zip(keys, amounts))
        if bundle:
            meta["bundle"] = "1"
    else:
        meta["feature_key"] = keys[0]
    return {
        "id": session_id or ("cs_test_" + secrets.token_hex(8)),
        "amount_total": amount,
        "amount_subtotal": subtotal if subtotal is not None else amount,
        "currency": "eur",
        "payment_status": status,
        "payment_intent": intent or ("pi_" + secrets.token_hex(8)),
        "customer": "cus_" + secrets.token_hex(6),
        "customer_details": {"email": email},
        "metadata": meta,
    }


def _payment(app, session_id):
    return app._payment_by_session(session_id)


@pytest.fixture
def legal_ok(app):
    """Попълнени юридически данни, както трябва да са в продукция."""
    values = {"company_id": "123456789", "e_shop_n": "RF0000123",
              "company_name": "Тест ЕООД", "address": "София, ул. Тест 1",
              "privacy_email": "privacy@example.com"}
    for key, value in values.items():
        app.set_setting(f"legal_{key}", value)
    yield values
    for key in values:
        app.set_setting(f"legal_{key}", "")


@pytest.fixture(autouse=True)
def _no_mail(app, monkeypatch):
    """Никакъв истински имейл; броим какво би тръгнало."""
    sent = []
    monkeypatch.setattr(app, "send_email", lambda *a, **k: sent.append((a, k)))
    return sent


# --- сметки до стотинка ---------------------------------------------------------

def test_allocation_keeps_the_exact_total(app):
    assert sum(app.allocate_cents(2500, [1] * 6)) == 2500
    assert app.allocate_cents(400, [499, 299]) == [250, 150]
    assert sum(app.allocate_cents(1, [1, 1, 1])) == 1
    assert app.allocate_cents(0, [5, 5]) == [0, 0]


def test_vat_is_rounded_half_up_to_the_cent(app):
    assert app.vat_cents_of(2500, 20) == 417     # 416.67
    assert app.vat_cents_of(499, 20) == 83       # 83.17
    assert app.vat_cents_of(299, 20) == 50       # 49.83
    assert app.vat_cents_of(100, 0) == 0


def test_order_vat_is_split_over_lines_not_rounded_per_line(app):
    """25 € в 6 реда: закръгляне по ред дава 4.18, а ДДС-ът на поръчката е 4.17."""
    parts = app.allocate_cents(2500, [1] * 6)
    vats = app.split_vat_over_lines(parts, 20)
    assert sum(vats) == 417
    assert all(abs(v - app.vat_cents_of(p, 20)) <= 1 for p, v in zip(parts, vats))


# --- редовете на продажбата ------------------------------------------------------

def test_single_purchase_records_one_line_with_the_paid_amount(app, user):
    s = _session(user["id"], ["numerology"], 400, kind="feature")
    app.fulfill_checkout_session(s)
    items = app.payment_items(_payment(app, s["id"])["id"])
    assert [(i["feature_key"], i["amount_cents"]) for i in items] == [("numerology", 400)]
    assert items[0]["vat_cents"] == 67


def test_several_modules_keep_their_own_prices(app, user):
    """Досега сумата се делеше поравно: 4.99 + 2.99 излизаше 3.99 + 3.99."""
    s = _session(user["id"], ["profile", "moon"], 798, amounts=[499, 299])
    app.fulfill_checkout_session(s)
    items = app.payment_items(_payment(app, s["id"])["id"])
    assert [(i["feature_key"], i["amount_cents"]) for i in items] == [("profile", 499), ("moon", 299)]


def test_bundle_is_one_line_at_the_bundle_price(app, user):
    keys = ["profile", "period", "love", "akashic", "numerology", "moon"]
    s = _session(user["id"], keys, 2500, amounts=[417, 417, 417, 417, 416, 416], bundle=True)
    app.fulfill_checkout_session(s)
    pay = _payment(app, s["id"])
    items = app.payment_items(pay["id"])
    assert len(items) == 1 and items[0]["amount_cents"] == 2500
    assert items[0]["name"].startswith("Пакет")
    unlocked = set(app.unlocked_features(app.get_user_by_id(user["id"])))
    assert set(keys) <= unlocked
    with sqlite3.connect(app.DB_PATH) as c:
        granted = c.execute("SELECT SUM(price_cents) FROM feature_purchases WHERE payment_id = ?",
                            (pay["id"],)).fetchone()[0]
    assert granted == 2500, "модулите на пакета се водят по пълна цена, а е платено 25 €"


def test_promo_code_discount_is_spread_exactly(app, user):
    s = _session(user["id"], ["profile", "moon"], 400, amounts=[499, 299], subtotal=798)
    app.fulfill_checkout_session(s)
    pay = _payment(app, s["id"])
    assert sum(i["amount_cents"] for i in app.payment_items(pay["id"])) == 400
    assert pay["discount_cents"] == 398


def test_fully_discounted_order_is_a_sale(app, user):
    """100 % промо код: Stripe казва no_payment_required — пак е продажба."""
    s = _session(user["id"], ["moon"], 0, kind="feature", status="no_payment_required")
    assert app.session_is_paid(s)
    app.fulfill_checkout_session(s)
    assert "moon" in app.unlocked_features(app.get_user_by_id(user["id"]))


# --- устойчивост на повторения и прекъсвания ---------------------------------------

def test_interrupted_fulfilment_is_completed_on_retry(app, user, monkeypatch):
    """Записът минава, отключването гърми (напр. „database is locked“): досега
    всеки следващ опит виждаше записа и спираше — платено, но заключено."""
    s = _session(user["id"], ["akashic"], 900, kind="feature")
    real = app.grant_feature_purchase

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(app, "grant_feature_purchase", boom)
    with pytest.raises(sqlite3.OperationalError):
        app.fulfill_checkout_session(s)
    assert "akashic" not in app.unlocked_features(app.get_user_by_id(user["id"]))

    monkeypatch.setattr(app, "grant_feature_purchase", real)
    app.fulfill_checkout_session(s)
    assert "akashic" in app.unlocked_features(app.get_user_by_id(user["id"]))
    with sqlite3.connect(app.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM payments WHERE user_id = ?",
                         (user["id"],)).fetchone()[0] == 1


def test_documents_go_out_exactly_once(app, user, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "send_sale_documents_for_payment", lambda pid, email: calls.append(pid))
    s = _session(user["id"], ["moon"], 299, kind="feature")
    for _ in range(3):
        app.fulfill_checkout_session(s)
    assert len(calls) == 1


def test_revoked_module_is_not_regranted_by_an_old_success_link(app, db, user):
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    s = _session(user["id"], ["moon"], 299, kind="feature")
    app.fulfill_checkout_session(s)
    app.api_admin_revoke_feature(user["id"], "moon", admin=admin)
    app.fulfill_checkout_session(s)          # клиентът отваря пак линка за успех
    assert "moon" not in app.unlocked_features(app.get_user_by_id(user["id"]))


def test_voided_payment_is_not_fulfilled_again(app, db, user):
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    s = _session(user["id"], ["moon"], 299, kind="feature")
    app.fulfill_checkout_session(s)
    pay = _payment(app, s["id"])
    app.api_admin_void_payment(pay["id"], app.PaymentVoid(reason="тест"), admin=admin)
    app.api_admin_revoke_feature(user["id"], "moon", admin=admin)
    app.fulfill_checkout_session(s)
    assert "moon" not in app.unlocked_features(app.get_user_by_id(user["id"]))
    with sqlite3.connect(app.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM payments WHERE stripe_session_id = ?",
                         (s["id"],)).fetchone()[0] == 1


def test_delete_button_now_voids_instead_of_erasing(app, db, user):
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    s = _session(user["id"], ["moon"], 299, kind="feature")
    app.fulfill_checkout_session(s)
    pay = _payment(app, s["id"])
    app.api_admin_delete_payment(pay["id"], admin=admin)
    after = _payment(app, s["id"])
    assert after is not None and after["voided_at"]


def test_account_deletion_keeps_the_fiscal_records(app, user):
    s = _session(user["id"], ["moon"], 299, kind="feature")
    app.fulfill_checkout_session(s)
    pay = _payment(app, s["id"])
    app.api_delete_account(user=(user["id"], user["email"]))
    assert _payment(app, s["id"])["id"] == pay["id"]
    assert app.payment_items(pay["id"])
    assert app.sale_document(pay["id"])
    assert app.get_user_by_id(user["id"]) is None


# --- документът за клиента (чл. 52о) ------------------------------------------------

def test_document_numbers_are_ten_digits_and_step_by_one(app, user, legal_ok):
    numbers = []
    for key in ("moon", "numerology", "akashic"):
        s = _session(user["id"], [key], 299, kind="feature")
        app.fulfill_checkout_session(s)
        numbers.append(app.sale_document(_payment(app, s["id"])["id"])["number"])
    assert numbers == [numbers[0], numbers[0] + 1, numbers[0] + 2]
    pdf, filename, doc_number = app.build_sale_receipt(_payment(app, s["id"])["id"])
    assert pdf.startswith(b"%PDF") and len(doc_number) == 10 and doc_number.isdigit()
    assert doc_number in filename


def test_test_payments_do_not_consume_document_numbers(app, user, legal_ok):
    s1 = _session(user["id"], ["moon"], 299, kind="feature")
    app.fulfill_checkout_session(s1)
    first = app.sale_document(_payment(app, s1["id"])["id"])["number"]
    app.record_payment(user["id"], plan_key=None, amount_cents=500, currency="EUR",
                       method="тест", note="mock")
    s2 = _session(user["id"], ["numerology"], 400, kind="feature")
    app.fulfill_checkout_session(s2)
    assert app.sale_document(_payment(app, s2["id"])["id"])["number"] == first + 1


def test_qr_code_follows_appendix_18a(app):
    issued = datetime.datetime(2026, 9, 30, 14, 5, 33, tzinfo=SOFIA)
    data = app.sale_qr_data("RF0000123", "42", "pi_abc", issued, 2500)
    assert data == "RF0000123**42*pi_abc*2026-09-30*14:05:33*25.00"


def test_old_payment_never_gets_a_second_document_number(app, user):
    """Плащане отпреди редовете да се пазят е получило документ тогава."""
    pid = app.record_payment(user["id"], plan_key=None, amount_cents=499, currency="EUR",
                             method="stripe", note="feature:profile cs_old", session_id="cs_old_x")
    with pytest.raises(ValueError):
        app.build_sale_receipt(pid)
    assert app.sale_document(pid) is None


def test_old_success_link_does_not_create_lines_for_an_old_payment(app, db, user):
    """Плащане отпреди тази версия (документите му са пратени тогава): повторно
    отваряне на линка не бива да му създаде редове и нов номер на документ."""
    pid = app.record_payment(user["id"], plan_key=None, amount_cents=299, currency="EUR",
                             method="stripe", note="feature:moon cs_old_moon", session_id="cs_old_moon")
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE payments SET granted_at = paid_at, documents_at = paid_at WHERE id = ?", (pid,))
        c.commit()
    app.fulfill_checkout_session(_session(user["id"], ["moon"], 299, kind="feature",
                                          session_id="cs_old_moon"))
    assert app.payment_items(pid) == []
    assert app.sale_document(pid) is None
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    with pytest.raises(HTTPException):
        app.api_admin_resend_documents(pid, admin=admin)


def test_customer_can_download_own_documents_only(app, db, user, legal_ok):
    s = _session(user["id"], ["moon"], 299, kind="feature")
    app.fulfill_checkout_session(s)
    pay = _payment(app, s["id"])
    docs = app.api_account_documents(user=(user["id"], user["email"]))["documents"]
    assert [d["payment_id"] for d in docs] == [pay["id"]]
    res = app.api_account_document_pdf(pay["id"], user=(user["id"], user["email"]))
    assert res.body.startswith(b"%PDF")
    other = db.create_user(f"o-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    with pytest.raises(HTTPException):
        app.api_account_document_pdf(pay["id"], user=(other["id"], other["email"]))


# --- връщане на пари ------------------------------------------------------------------

def test_stripe_refund_is_recorded_once_and_partially(app, user):
    s = _session(user["id"], ["akashic"], 900, kind="feature", intent="pi_refund_me")
    app.fulfill_checkout_session(s)
    pay = _payment(app, s["id"])
    charge = {"payment_intent": "pi_refund_me", "amount_refunded": 300}
    app.record_stripe_refund(charge, "evt_1", 1790000000)
    app.record_stripe_refund(charge, "evt_1", 1790000000)   # повторно изпратено събитие
    assert app.refunded_cents(pay["id"]) == 300
    app.record_stripe_refund({**charge, "amount_refunded": 900}, "evt_2", 1790000100)
    assert app.refunded_cents(pay["id"]) == 900


def test_admin_cannot_refund_more_than_was_paid(app, db, user):
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    s = _session(user["id"], ["moon"], 299, kind="feature")
    app.fulfill_checkout_session(s)
    pid = _payment(app, s["id"])["id"]
    with pytest.raises(HTTPException):
        app.api_admin_refund_payment(pid, app.PaymentRefund(amount_cents=300), admin=admin)
    app.api_admin_refund_payment(pid, app.PaymentRefund(method="account"), admin=admin)
    assert app.refunded_cents(pid) == 299


# --- одиторският файл ------------------------------------------------------------------

def _set_paid_at(app, payment_id, utc):
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE payments SET paid_at = ? WHERE id = ?", (utc, payment_id))
        c.execute("UPDATE sale_documents SET issued_at = ? WHERE payment_id = ?", (utc, payment_id))
        c.commit()


def _orders(xml_bytes):
    root = ET.fromstring(xml_bytes)
    return {o.findtext("ord_n"): o for o in root.findall("./order/orderenum")}, root


def test_month_uses_sofia_time_and_keeps_the_31st(app, user, legal_ok):
    """Досега краят беше „ГГГГ-ММ-31“ в UTC: продажбите на 31-во изчезваха,
    а тези между 00 и 03 ч. (София) отиваха в предишния месец."""
    ids = {}
    for label, utc in [("aug31", "2026-08-31 10:00:00"),          # 31.08 13:00 София
                       ("sep1_night", "2026-08-31 22:30:00"),     # 01.09 01:30 София
                       ("aug1_night", "2026-07-31 21:30:00")]:    # 01.08 00:30 София
        s = _session(user["id"], ["moon"], 299, kind="feature")
        app.fulfill_checkout_session(s)
        ids[label] = _payment(app, s["id"])["id"]
        _set_paid_at(app, ids[label], utc)
    august = app.build_month_saft(2026, 8)
    orders, _ = _orders(august["xml"])
    assert str(ids["aug31"]) in orders
    assert str(ids["aug1_night"]) in orders
    assert str(ids["sep1_night"]) not in orders
    assert orders[str(ids["aug1_night"])].findtext("ord_d") == "2026-08-01"
    september = app.build_month_saft(2026, 9)
    assert str(ids["sep1_night"]) in _orders(september["xml"])[0]


def test_month_file_is_valid_for_nap(app, user, legal_ok):
    """XSD на НАП + аритметика: пакет, няколко модула, промо код, връщане."""
    keys = ["profile", "period", "love", "akashic", "numerology", "moon"]
    sessions = [
        _session(user["id"], keys, 2500, amounts=[417, 417, 417, 417, 416, 416], bundle=True),
        _session(user["id"], ["profile", "moon"], 798, amounts=[499, 299]),
        _session(user["id"], ["profile", "moon"], 400, amounts=[499, 299], subtotal=798),
        _session(user["id"], ["moon"], 299, kind="feature", intent="pi_ref"),
    ]
    for s in sessions:
        app.fulfill_checkout_session(s)
        _set_paid_at(app, _payment(app, s["id"])["id"], "2026-09-15 09:00:00")
    app.record_stripe_refund({"payment_intent": "pi_ref", "amount_refunded": 299},
                             "evt_ref", int(datetime.datetime(2026, 9, 20, tzinfo=SOFIA).timestamp()))
    report = app.build_month_saft(2026, 9)
    assert report["valid"], report["problems"]
    orders, root = _orders(report["xml"])
    assert len(orders) == 4
    bundle = orders[str(_payment(app, sessions[0]["id"])["id"])]
    assert bundle.findtext("ord_total2") == "25.00"
    assert bundle.findtext("ord_vat") == "4.17"
    assert bundle.findtext("ord_total1") == "20.83"
    assert bundle.findtext("paym") == "4"
    assert bundle.findtext("proc_id").startswith("IE3206488LH")
    assert bundle.findtext("trans_n").startswith("pi_")
    assert len(bundle.findtext("doc_n")) <= 10
    assert root.findtext("r_ord") == "1"
    assert root.findtext("./rorder/rorderenum/r_paym") == "2", "връщане по карта е код 2"
    assert root.findtext("r_total") == "2.99"
    assert report["xml"].startswith(b'<?xml version="1.0" encoding="windows-1251"?>')


def test_voided_payments_are_left_out(app, db, user, legal_ok):
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    keep = _session(user["id"], ["moon"], 299, kind="feature")
    drop = _session(user["id"], ["numerology"], 400, kind="feature")
    for s in (keep, drop):
        app.fulfill_checkout_session(s)
        _set_paid_at(app, _payment(app, s["id"])["id"], "2026-09-10 09:00:00")
    app.api_admin_void_payment(_payment(app, drop["id"])["id"], admin=admin)
    orders, _ = _orders(app.build_month_saft(2026, 9)["xml"])
    assert list(orders) == [str(_payment(app, keep["id"])["id"])]


def test_old_payments_are_split_exactly(app, user, legal_ok):
    """Плащане отпреди редовете: поравно, както в документа тогава — до стотинка."""
    pid = app.record_payment(user["id"], plan_key=None, amount_cents=2500, currency="EUR",
                             method="stripe", note="features:profile,period,love,akashic,numerology,moon cs_x",
                             session_id="cs_legacy_bundle")
    _set_paid_at(app, pid, "2026-09-05 09:00:00")
    report = app.build_month_saft(2026, 9)
    assert report["valid"], report["problems"]
    order = _orders(report["xml"])[0][str(pid)]
    assert order.findtext("ord_total2") == "25.00"
    assert order.findtext("doc_n") == str(pid), "старият документ носеше номера на плащането"


def test_placeholders_and_empty_months_are_reported(app, user):
    s = _session(user["id"], ["moon"], 299, kind="feature")
    app.fulfill_checkout_session(s)
    _set_paid_at(app, _payment(app, s["id"])["id"], "2026-09-10 09:00:00")
    problems = app.build_month_saft(2026, 9)["problems"]
    assert any("eik" in p for p in problems)
    assert any("e_shop_n" in p for p in problems)
    empty = app.build_month_saft(2026, 6)["problems"]
    assert any("Няма поръчки" in p for p in empty)


def test_invalid_file_is_not_downloadable(app, db, user):
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    with pytest.raises(HTTPException) as err:
        app.api_admin_saft(2026, 6, admin=admin)
    assert err.value.status_code == 422


# --- автоматизацията ------------------------------------------------------------------

def test_monthly_file_is_generated_mailed_and_reminded(app, user, legal_ok, _no_mail, monkeypatch):
    monkeypatch.setattr(app, "notify_address", lambda: "owner@example.com")
    monkeypatch.setattr(app, "smtp_setting", lambda key: "smtp.example.com" if key == "smtp_host" else "")
    s = _session(user["id"], ["moon"], 299, kind="feature")
    app.fulfill_checkout_session(s)
    _set_paid_at(app, _payment(app, s["id"])["id"], "2026-09-10 09:00:00")
    app.set_setting("saft:2026-09", "")
    _no_mail.clear()

    app.run_saft_automation(datetime.datetime(2026, 10, 1, 5, 0, tzinfo=SOFIA))
    assert not _no_mail, "преди 6 ч. нищо не тръгва"
    app.run_saft_automation(datetime.datetime(2026, 10, 1, 7, 0, tzinfo=SOFIA))
    assert len(_no_mail) == 1
    args, kwargs = _no_mail[0]
    assert kwargs["attachment"][0] == "saft-2026-09.xml"
    assert (app.SAFT_DIR / "saft-2026-09.xml").exists()
    meta = app.saft_meta("2026-09")
    assert meta["valid"] and meta["orders"] == 1 and meta["deadline"] == "2026-10-15"

    app.run_saft_automation(datetime.datetime(2026, 10, 1, 8, 0, tzinfo=SOFIA))
    assert len(_no_mail) == 1, "файлът се генерира и праща само веднъж"

    app.run_saft_automation(datetime.datetime(2026, 10, 12, 9, 0, tzinfo=SOFIA))
    assert len(_no_mail) == 2 and "напомняне" in _no_mail[1][0][1]
    app.run_saft_automation(datetime.datetime(2026, 10, 13, 9, 0, tzinfo=SOFIA))
    assert len(_no_mail) == 2, "напомнянето е едно"
    app.set_setting("saft:2026-09", "")


def test_submitted_month_gets_no_reminder(app, db, user, legal_ok, _no_mail, monkeypatch):
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    monkeypatch.setattr(app, "notify_address", lambda: "owner@example.com")
    monkeypatch.setattr(app, "smtp_setting", lambda key: "smtp.example.com" if key == "smtp_host" else "")
    s = _session(user["id"], ["moon"], 299, kind="feature")
    app.fulfill_checkout_session(s)
    _set_paid_at(app, _payment(app, s["id"])["id"], "2026-09-10 09:00:00")
    app.set_setting("saft:2026-09", "")
    app.run_saft_automation(datetime.datetime(2026, 10, 1, 7, 0, tzinfo=SOFIA))
    app.api_admin_saft_submitted("2026-09", app.SaftSubmitted(submitted=True), admin=admin)
    _no_mail.clear()
    app.run_saft_automation(datetime.datetime(2026, 10, 13, 9, 0, tzinfo=SOFIA))
    assert not _no_mail
    app.set_setting("saft:2026-09", "")


def test_changes_after_generation_are_flagged(app, db, user, legal_ok):
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    s = _session(user["id"], ["moon"], 299, kind="feature")
    app.fulfill_checkout_session(s)
    pid = _payment(app, s["id"])["id"]
    _set_paid_at(app, pid, "2026-09-10 09:00:00")
    app.set_setting("saft:2026-09", "")
    app.api_admin_saft_generate("2026-09", admin=admin)
    months = {m["key"]: m for m in app.api_admin_saft_months(admin=admin)["months"]}
    assert months["2026-09"]["stale"] is False
    app.api_admin_void_payment(pid, admin=admin)
    months = {m["key"]: m for m in app.api_admin_saft_months(admin=admin)["months"]}
    assert months["2026-09"]["stale"] is True
    app.set_setting("saft:2026-09", "")


# --- миграции, споделяне, регенериране -----------------------------------------------------

def test_restart_does_not_regrant_a_revoked_module(app, db, user):
    """Досега всеки деплой връщаше „love“ на всеки с „synastry“."""
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    app.grant_feature_purchase(user["id"], "synastry", 0, "EUR", None)
    app.grant_feature_purchase(user["id"], "love", 0, "EUR", None)
    app.api_admin_revoke_feature(user["id"], "love", admin=admin)
    app.init_db()
    assert "love" not in app.purchased_features(user["id"])


def test_restart_keeps_a_plan_created_by_the_admin(app, db, user):
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    app.api_admin_upsert_plan("trial", app.AdminPlanUpsert(
        key="trial", name="Проба", features=["akashic"], max_persons=3), admin=admin)
    app.api_admin_update_user(user["id"], app.AdminUserUpdate(plan_key="trial"), admin=admin)
    app.init_db()
    assert app.get_plan("trial") is not None
    assert "akashic" not in app.purchased_features(user["id"]), \
        "планът стана вечна безплатна покупка след рестарт"
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE users SET plan_key = 'demo' WHERE id = ?", (user["id"],))
        c.execute("DELETE FROM plans WHERE key = 'trial'")
        c.commit()


def test_admin_created_account_gets_the_free_modules(app, db):
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    res = app.api_admin_create_user(app.AdminUserCreate(
        email=f"made-{secrets.token_hex(3)}@example.com", password="parola-123"), admin=admin)
    unlocked = app.unlocked_features(app.get_user_by_id(res["id"]))
    assert "chart" in unlocked and "horoscope" in unlocked


def _person(app, user_id):
    with sqlite3.connect(app.DB_PATH) as c:
        cur = c.execute("INSERT INTO persons (user_id, name, year, month, day, hour, minute, lat, lon)"
                        " VALUES (?, 'Тест', 1990, 6, 15, 12, 0, 42.7, 23.3)", (user_id,))
        c.commit()
        return cur.lastrowid


def test_share_needs_access_to_the_module(app, db, user):
    pid = _person(app, user["id"])
    app.set_ai_cache(pid, "akashic", "Текст на разчитането")
    from starlette.requests import Request
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                   "query_string": b"", "scheme": "https", "server": ("astrokarta.bg", 443)})
    with pytest.raises(HTTPException) as err:
        app.api_create_share(pid, app.ShareCreate(cache_key="akashic"), req,
                             user=(user["id"], user["email"]))
    assert err.value.status_code == 402


def test_synastry_export_needs_the_love_module(app, user):
    with pytest.raises(HTTPException) as err:
        app.require_reading_access(user["id"], "synastry:1:2")
    assert err.value.status_code == 402


def test_paid_reading_refresh_is_ignored_for_customers(app, user):
    """Бутонът „разчети наново“ е махнат, но адресът беше отворен — всеки
    купувач можеше да харчи скъпия модел в цикъл."""
    app.grant_feature_purchase(user["id"], "profile", 500, "EUR", None)
    pid = _person(app, user["id"])
    app.set_ai_cache(pid, "profile", "Запазено разчитане")
    res = app.api_profile_interpretation(pid, refresh=True, user=(user["id"], user["email"]))
    assert res.get("cached") is True and res["interpretation"] == "Запазено разчитане"


# --- преглед преди деплой: плащания и връщания -------------------------------------

def _post_webhook(app, body: bytes) -> dict:
    """POST към /api/stripe/webhook през ASGI — както го вика uvicorn."""
    import asyncio
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "https", "path": "/api/stripe/webhook",
             "raw_path": b"/api/stripe/webhook", "root_path": "", "query_string": b"",
             "headers": [(b"host", b"astrokarta.bg"), (b"stripe-signature", b"t=1,v1=x"),
                         (b"content-type", b"application/json")],
             "client": ("8.8.8.8", 5000), "server": ("astrokarta.bg", 443)}
    sent = {"status": 0, "body": b""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
        elif message["type"] == "http.response.body":
            sent["body"] += message.get("body", b"")

    asyncio.run(app.app(scope, receive, send))
    return sent


def test_stripe_refund_webhook_with_a_real_event_object(app, user, monkeypatch):
    """stripe.Event не е dict: event.get("id") хвърляше AttributeError и всяко
    връщане от Stripe завършваше с 500 — нито едно не стигаше до файла."""
    import json
    import stripe
    s = _session(user["id"], ["akashic"], 900, kind="feature", intent="pi_hook_refund")
    app.fulfill_checkout_session(s)
    pay = _payment(app, s["id"])
    sold = int(datetime.datetime(2026, 9, 28, 12, 0, tzinfo=SOFIA).timestamp())
    refunded = int(datetime.datetime(2026, 10, 3, 12, 0, tzinfo=SOFIA).timestamp())
    payload = {"id": "evt_hook_refund", "object": "event", "type": "charge.refunded",
               "created": refunded,
               "data": {"object": {"id": "ch_hook", "object": "charge", "created": sold,
                                   "payment_intent": "pi_hook_refund", "amount_refunded": 900}}}
    monkeypatch.setattr(app.billing, "construct_webhook_event",
                        lambda body, sig: stripe.Event.construct_from(json.loads(body), "sk_test"))
    res = _post_webhook(app, json.dumps(payload).encode())
    assert res["status"] == 200, res["body"]
    assert app.refunded_cents(pay["id"]) == 900
    with sqlite3.connect(app.DB_PATH) as c:
        when = c.execute("SELECT refunded_at FROM payment_refunds WHERE payment_id = ?",
                         (pay["id"],)).fetchone()[0]
    assert app.utc_to_sofia(when).strftime("%Y-%m-%d") == "2026-10-03", \
        "датата е на връщането, не на продажбата"
    assert _post_webhook(app, json.dumps(payload).encode())["status"] == 200
    assert app.refunded_cents(pay["id"]) == 900, "повторно изпратено събитие не се брои два пъти"


def test_refund_goes_to_the_month_of_the_refund(app, user, legal_ok):
    """Без разгънат списък с връщания датата идваше от charge.created — часа на
    продажбата — и връщането отиваше в месеца на продажбата."""
    s = _session(user["id"], ["moon"], 299, kind="feature", intent="pi_late_refund")
    app.fulfill_checkout_session(s)
    _set_paid_at(app, _payment(app, s["id"])["id"], "2026-08-20 09:00:00")
    sale = int(datetime.datetime(2026, 8, 20, 12, 0, tzinfo=SOFIA).timestamp())
    event = int(datetime.datetime(2026, 9, 2, 12, 0, tzinfo=SOFIA).timestamp())
    app.record_stripe_refund({"payment_intent": "pi_late_refund", "amount_refunded": 299,
                              "created": sale}, "evt_late", event)
    assert app.build_month_saft(2026, 8)["refunds"] == 0
    september = app.build_month_saft(2026, 9)
    assert september["refunds"] == 1 and september["refund_cents"] == 299
    # разгънатият списък с връщания (ако Stripe го прати) е по-точен от събитието
    s2 = _session(user["id"], ["numerology"], 400, kind="feature", intent="pi_listed")
    app.fulfill_checkout_session(s2)
    listed = int(datetime.datetime(2026, 9, 5, 12, 0, tzinfo=SOFIA).timestamp())
    app.record_stripe_refund({"payment_intent": "pi_listed", "amount_refunded": 400,
                              "refunds": {"data": [{"created": listed}]}}, "evt_listed", event)
    with sqlite3.connect(app.DB_PATH) as c:
        when = c.execute("SELECT r.refunded_at FROM payment_refunds r JOIN payments p"
                         " ON p.id = r.payment_id WHERE p.payment_intent = 'pi_listed'").fetchone()[0]
    assert app.utc_to_sofia(when).strftime("%Y-%m-%d") == "2026-09-05"


def _slow_vat_split(app, monkeypatch):
    """Разширява прозореца между „няма ли редове?“ и записа — така
    състезанието се появява всеки път, а не веднъж на сто пускания."""
    import time
    real = app.split_vat_over_lines

    def slow(*a, **k):
        time.sleep(0.2)
        return real(*a, **k)
    monkeypatch.setattr(app, "split_vat_over_lines", slow)


def test_parallel_fulfilment_records_one_sale(app, user, monkeypatch):
    """Webhook-ът и връщането на клиента едновременно: досега и двамата
    можеха да видят „няма редове“ и да запишат по комплект."""
    import threading
    monkeypatch.setattr(app, "send_sale_documents_for_payment", lambda pid, email: None)
    _slow_vat_split(app, monkeypatch)
    s = _session(user["id"], ["profile", "moon"], 798, amounts=[499, 299])
    barrier = threading.Barrier(6)
    errors = []

    def run():
        try:
            barrier.wait()
            app.fulfill_checkout_session(dict(s))
        except Exception as e:          # pragma: no cover - показва се в assert
            errors.append(e)
    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    pay = _payment(app, s["id"])
    assert [i["amount_cents"] for i in app.payment_items(pay["id"])] == [499, 299]
    with sqlite3.connect(app.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM sale_documents WHERE payment_id = ?",
                         (pay["id"],)).fetchone()[0] == 1


def test_parallel_line_writes_store_one_set(app, user, monkeypatch):
    import threading
    _slow_vat_split(app, monkeypatch)
    pid = app.record_payment(user["id"], plan_key=None, amount_cents=798, currency="EUR",
                             method="тест", note="race")
    lines = [{"key": "profile", "name": "Профил", "list_cents": 499, "amount_cents": 499},
             {"key": "moon", "name": "Луна", "list_cents": 299, "amount_cents": 299}]
    barrier = threading.Barrier(8)

    def run():
        barrier.wait()
        app.store_payment_items(pid, lines)
    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(app.payment_items(pid)) == 2


def test_voided_payment_cannot_be_refunded_and_its_refunds_stay_out(app, db, user, legal_ok):
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    keep = _session(user["id"], ["moon"], 299, kind="feature")
    void = _session(user["id"], ["numerology"], 400, kind="feature", intent="pi_voided")
    for s in (keep, void):
        app.fulfill_checkout_session(s)
        _set_paid_at(app, _payment(app, s["id"])["id"], "2026-09-10 09:00:00")
    vid = _payment(app, void["id"])["id"]
    app.api_admin_void_payment(vid, admin=admin)
    with pytest.raises(HTTPException) as err:
        app.api_admin_refund_payment(vid, app.PaymentRefund(method="account"), admin=admin)
    assert err.value.status_code == 400
    # Stripe все пак връща парите: записва се, но не влиза в одиторския файл,
    # защото и продажбата не е там.
    app.record_stripe_refund({"payment_intent": "pi_voided", "amount_refunded": 400}, "evt_void",
                             int(datetime.datetime(2026, 9, 12, tzinfo=SOFIA).timestamp()))
    assert app.refunded_cents(vid) == 400
    report = app.build_month_saft(2026, 9)
    assert report["valid"], report["problems"]
    assert report["orders"] == 1 and report["refunds"] == 0


def test_numbering_never_repeats_a_number_from_the_old_container(app, user, legal_ok):
    """Докато върви деплой, старият контейнер още приема плащания и праща
    документ с номера на плащането. Новата номерация не бива да го повтори."""
    with sqlite3.connect(app.DB_PATH) as c:
        before = c.execute("SELECT value FROM settings WHERE key = 'sale_doc_seed'").fetchone()
        last = c.execute("SELECT seq FROM sqlite_sequence WHERE name = 'payments'").fetchone()
        # както при миграцията: номерацията тръгва след най-голямото плащане
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sale_doc_seed', ?)",
                  (str(last[0] if last else 0),))
        c.commit()
    try:
        s1 = _session(user["id"], ["moon"], 299, kind="feature")
        app.fulfill_checkout_session(s1)
        old = app.record_payment(user["id"], plan_key=None, amount_cents=400, currency="EUR",
                                 method="stripe", note="feature:numerology cs_old_container",
                                 session_id="cs_old_container")
        s2 = _session(user["id"], ["akashic"], 900, kind="feature")
        app.fulfill_checkout_session(s2)
        n1 = app.sale_document(_payment(app, s1["id"])["id"])["number"]
        n2 = app.sale_document(_payment(app, s2["id"])["id"])["number"]
        assert n1 < old < n2 and n2 == old + 1, (n1, old, n2)
        for pid in (_payment(app, s1["id"])["id"], old, _payment(app, s2["id"])["id"]):
            _set_paid_at(app, pid, "2026-09-10 09:00:00")
        report = app.build_month_saft(2026, 9)
        assert report["valid"], report["problems"]
        doc_ns = [o.findtext("doc_n") for o in _orders(report["xml"])[0].values()]
        assert sorted(doc_ns) == sorted({str(n1), str(old), str(n2)})
    finally:
        with sqlite3.connect(app.DB_PATH) as c:
            c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sale_doc_seed', ?)",
                      (before[0] if before else "0",))
            c.commit()


def test_payment_from_the_old_container_gets_no_second_document(app, user, monkeypatch):
    """Старият контейнер е записал плащането и е пратил своя документ; клиентът
    се връща на новия. Достъпът се довършва, втори документ няма."""
    sent = []
    monkeypatch.setattr(app, "send_sale_documents_for_payment", lambda pid, email: sent.append(pid))
    s = _session(user["id"], ["moon"], 299, kind="feature", session_id="cs_from_old_container")
    pid = app.record_payment(user["id"], plan_key=None, amount_cents=299, currency="EUR",
                             method="stripe", note="feature:moon cs_from_old_container",
                             session_id="cs_from_old_container")
    app.fulfill_checkout_session(s)
    assert "moon" in app.unlocked_features(app.get_user_by_id(user["id"]))
    assert app.sale_document(pid) is None and app.payment_items(pid) == []
    assert not sent
    assert _payment(app, s["id"])["payment_intent"] == s["payment_intent"], \
        "без payment_intent връщане от Stripe не се свързва с плащането"


def test_manual_payment_cannot_use_the_stripe_method(app, db, user):
    """„stripe“ значи онлайн продажба с документ — ръчен запис с този метод
    влизаше в одиторския файл без документ."""
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    with pytest.raises(HTTPException) as err:
        app.api_admin_record_payment(app.AdminPaymentCreate(
            user_id=user["id"], amount_cents=500, method=" Stripe "), admin=admin)
    assert err.value.status_code == 400
    app.api_admin_record_payment(app.AdminPaymentCreate(
        user_id=user["id"], amount_cents=500, method="банка"), admin=admin)


# --- преглед преди деплой: одиторският файл ---------------------------------------

def _mail_on(app, monkeypatch):
    monkeypatch.setattr(app, "notify_address", lambda: "owner@example.com")
    monkeypatch.setattr(app, "smtp_setting", lambda key: "smtp.example.com" if key == "smtp_host" else "")


def test_current_month_generated_early_does_not_block_the_automation(
        app, db, user, legal_ok, _no_mail, monkeypatch):
    """Админът генерира текущия месец (непълен); на 1-во число автоматиката
    го пропускаше, защото „вече е генериран“ — и подаденият файл беше непълен."""
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    _mail_on(app, monkeypatch)
    app.set_setting("saft:2026-09", "")
    early = _session(user["id"], ["moon"], 299, kind="feature")
    app.fulfill_checkout_session(early)
    _set_paid_at(app, _payment(app, early["id"])["id"], "2026-09-10 09:00:00")
    app.api_admin_saft_generate("2026-09", admin=admin)
    assert app.saft_meta("2026-09")["orders"] == 1
    late = _session(user["id"], ["numerology"], 400, kind="feature")
    app.fulfill_checkout_session(late)
    _set_paid_at(app, _payment(app, late["id"])["id"], "2026-09-29 09:00:00")
    _no_mail.clear()
    app.run_saft_automation(datetime.datetime(2026, 10, 1, 7, 0, tzinfo=SOFIA))
    meta = app.saft_meta("2026-09")
    assert meta["orders"] == 2 and meta["valid"] and meta["auto_at"]
    assert len(_no_mail) == 1 and _no_mail[0][1]["attachment"][0] == "saft-2026-09.xml"
    app.set_setting("saft:2026-09", "")


def test_month_without_sales_gets_no_invalid_file_and_no_reminder(
        app, db, user, legal_ok, _no_mail, monkeypatch):
    """Досега месец без продажби (след първата продажба на магазина) даваше
    „файлът НЕ е готов“ всеки месец и напомняне за файл, който не съществува."""
    _mail_on(app, monkeypatch)
    for key in ("2026-08", "2026-09"):
        app.set_setting(f"saft:{key}", "")
    s = _session(user["id"], ["moon"], 299, kind="feature")
    app.fulfill_checkout_session(s)
    _set_paid_at(app, _payment(app, s["id"])["id"], "2026-08-10 09:00:00")
    target = app.SAFT_DIR / "saft-2026-09.xml"
    if target.exists():
        target.unlink()
    _no_mail.clear()
    app.run_saft_automation(datetime.datetime(2026, 10, 1, 7, 0, tzinfo=SOFIA))
    assert not target.exists()
    meta = app.saft_meta("2026-09")
    assert meta["no_sales"] is True and not meta.get("generated_at")
    assert len(_no_mail) == 1 and "няма продажби" in _no_mail[0][0][1]
    assert not _no_mail[0][1].get("attachment")
    for day in (1, 12, 13, 14):
        app.run_saft_automation(datetime.datetime(2026, 10, day, 9, 0, tzinfo=SOFIA))
    assert len(_no_mail) == 1, "без напомняния за месец без файл"
    months = {m["key"]: m for m in app.api_admin_saft_months(
        admin=db.get_user_by_email(app.ADMIN_EMAIL))["months"]}
    assert months["2026-09"]["sales"] == 0 and months["2026-08"]["sales"] == 1
    for key in ("2026-08", "2026-09"):
        app.set_setting(f"saft:{key}", "")


def test_month_list_keeps_the_newest_months(app, db, user):
    """Списъкът режеше след 36 месеца от първата продажба — новите месеци
    изчезваха, точно тези, които предстои да се подават."""
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    s = _session(user["id"], ["moon"], 299, kind="feature")
    app.fulfill_checkout_session(s)
    _set_paid_at(app, _payment(app, s["id"])["id"], "2021-01-10 09:00:00")
    months = app.api_admin_saft_months(admin=admin)["months"]
    now = datetime.datetime.now(SOFIA)
    assert len(months) == 36
    assert months[0]["key"] == f"{now.year:04d}-{now.month:02d}" and months[0]["in_progress"]
    oldest = now.year * 12 + now.month - 1 - 35
    assert months[-1]["key"] == f"{oldest // 12:04d}-{oldest % 12 + 1:02d}"


def test_nap_tab_does_not_rebuild_every_file(app, db, user, legal_ok, monkeypatch):
    """Табът строеше XML и го проверяваше срещу схемата за всеки генериран
    месец при всяко отваряне — за отпечатъка стигат данните."""
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    s = _session(user["id"], ["moon"], 299, kind="feature")
    app.fulfill_checkout_session(s)
    pid = _payment(app, s["id"])["id"]
    _set_paid_at(app, pid, "2026-09-10 09:00:00")
    app.set_setting("saft:2026-09", "")
    app.api_admin_saft_generate("2026-09", admin=admin)

    def no_xml(*a, **k):
        raise AssertionError("списъкът с месеци не бива да строи XML")
    monkeypatch.setattr(app.saft, "build_saft_xml", no_xml)
    monkeypatch.setattr(app.saft, "validate_saft", no_xml)
    months = {m["key"]: m for m in app.api_admin_saft_months(admin=admin)["months"]}
    assert months["2026-09"]["stale"] is False
    app.api_admin_void_payment(pid, admin=admin)
    months = {m["key"]: m for m in app.api_admin_saft_months(admin=admin)["months"]}
    assert months["2026-09"]["stale"] is True
    app.set_setting("saft:2026-09", "")


def test_one_vat_rounding_rule(app):
    assert app.vat_cents_of is app.saft.vat_cents_of


# --- преглед преди деплой: споделяне, фактура, лични данни -------------------------

def _share(app, user, person_id, cache_key):
    token = "shr" + secrets.token_hex(8)
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("INSERT INTO share_links (token, person_id, user_id, cache_key) VALUES (?, ?, ?, ?)",
                  (token, person_id, user["id"], cache_key))
        c.commit()
    return token


def test_share_link_stops_when_the_module_is_taken_back(app, db, user):
    """Линкът продължаваше да показва платеното разчитане и след отнемане на
    модула (напр. след върнати пари) — PDF-ът и имейлът вече бяха спрени."""
    import asyncio
    from starlette.requests import Request
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    app.grant_feature_purchase(user["id"], "akashic", 900, "EUR", None)
    pid = _person(app, user["id"])
    app.set_ai_cache(pid, "akashic", "1. **Мисия** текст")
    token = _share(app, user, pid, "akashic")
    req = Request({"type": "http", "method": "GET", "path": f"/share/{token}", "headers": [],
                   "query_string": b"", "scheme": "https", "server": ("astrokarta.bg", 443)})
    assert app.api_get_share(token)["content"]
    assert asyncio.run(app.share_page(req, token)).status_code == 200

    app.api_admin_revoke_feature(user["id"], "akashic", admin=admin)
    with pytest.raises(HTTPException) as err:
        app.api_get_share(token)
    assert err.value.status_code == 404
    assert asyncio.run(app.share_page(req, token)).status_code == 404

    app.grant_feature_purchase(user["id"], "akashic", 900, "EUR", None)
    assert app.api_get_share(token)["content"], "върнатият модул пуска линка отново"


def test_resent_invoice_keeps_its_number_and_date(app, user, legal_ok, monkeypatch):
    """При „изпрати пак“ фактурата получаваше днешната дата със стария номер —
    два различни документа с един номер."""
    monkeypatch.setattr(app, "smtp_setting", lambda key: "smtp.example.com" if key == "smtp_host" else "")
    seen = []
    real = app.build_invoice_pdf

    def capture(**kw):
        seen.append((kw["invoice_number"], kw["issued_at"]))
        return real(**kw)
    monkeypatch.setattr(app, "build_invoice_pdf", capture)
    s = _session(user["id"], ["moon"], 299, kind="feature")
    monkeypatch.setattr(app, "_in_background", lambda fn, *a, **k: fn(*a, **k))
    app.fulfill_checkout_session(s)
    pid = _payment(app, s["id"])["id"]
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE invoices SET issued_at = '2026-09-03 07:15:00' WHERE payment_id = ?", (pid,))
        c.commit()
    app.send_sale_documents_for_payment(pid, "buyer@example.com")
    assert len(seen) == 2 and seen[0][0] == seen[1][0]
    assert seen[1][1] == "03.09.2026 10:15:00", "датата на издаване, не днешната"
    with sqlite3.connect(app.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM invoices WHERE payment_id = ?", (pid,)).fetchone()[0] == 1


def test_unreadable_refund_amount_is_not_a_full_refund(app, db, user):
    """В админа „5 лв“ ставаше NaN → null → и сървърът връщаше целия остатък."""
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    s = _session(user["id"], ["akashic"], 900, kind="feature")
    app.fulfill_checkout_session(s)
    pid = _payment(app, s["id"])["id"]
    with pytest.raises(HTTPException) as err:
        app.api_admin_refund_payment(pid, app.PaymentRefund.model_validate(
            {"amount_cents": None, "method": "account"}), admin=admin)
    assert err.value.status_code == 400 and app.refunded_cents(pid) == 0
    app.api_admin_refund_payment(pid, app.PaymentRefund.model_validate(
        {"amount_cents": 300, "method": "account"}), admin=admin)
    app.api_admin_refund_payment(pid, app.PaymentRefund.model_validate({"method": "account"}), admin=admin)
    assert app.refunded_cents(pid) == 900, "без поле — целият остатък"


def test_deleting_a_person_removes_the_synastry_kept_by_the_partner(app, user):
    """Синастрията се пази и под партньора — след изтриване на единия текстът
    за него оставаше в базата, в споделен линк и в аудио файл."""
    a, b = _person(app, user["id"]), _person(app, user["id"])
    key = f"synastry:{min(a, b)}:{max(a, b)}"
    app.set_ai_cache(a, key, "Съвместимост на двамата")
    app.set_ai_cache(a, "profile", "Профилът на партньора остава")
    token = _share(app, user, a, key)
    audio = app.DB_PATH.parent / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    pair_mp3 = audio / f"{a}_synastry-{min(a, b)}-{max(a, b)}_abc123.mp3"
    own_mp3 = audio / f"{a}_profile_abc123.mp3"
    for f in (pair_mp3, own_mp3):
        f.write_bytes(b"ID3")
    app.api_delete_person(b, user=(user["id"], user["email"]))
    assert app.get_ai_cache(a, key) is None
    assert app.get_ai_cache(a, "profile") is not None
    with pytest.raises(HTTPException):
        app.api_get_share(token)
    assert not pair_mp3.exists() and own_mp3.exists()
    own_mp3.unlink()


def test_share_page_is_404_once_the_reading_is_gone(app, user):
    import asyncio
    from starlette.requests import Request
    pid = _person(app, user["id"])
    key = "horoscope:2026-10-01"
    app.set_ai_cache(pid, key, "Днешният хороскоп")
    token = _share(app, user, pid, key)
    req = Request({"type": "http", "method": "GET", "path": f"/share/{token}", "headers": [],
                   "query_string": b"", "scheme": "https", "server": ("astrokarta.bg", 443)})
    assert asyncio.run(app.share_page(req, token)).status_code == 200
    app.clear_ai_cache(pid)                 # напр. сменени рождени данни
    assert asyncio.run(app.share_page(req, token)).status_code == 404


def test_month_without_sales_is_reminded_once_a_real_file_exists(
        app, db, user, legal_ok, _no_mail, monkeypatch):
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    _mail_on(app, monkeypatch)
    app.set_setting("saft:2026-09", "")
    app.run_saft_automation(datetime.datetime(2026, 10, 1, 7, 0, tzinfo=SOFIA))
    assert app.saft_meta("2026-09")["no_sales"] is True
    s = _session(user["id"], ["moon"], 299, kind="feature")
    app.fulfill_checkout_session(s)
    _set_paid_at(app, _payment(app, s["id"])["id"], "2026-09-15 09:00:00")
    app.api_admin_saft_generate("2026-09", admin=admin)
    assert "no_sales" not in app.saft_meta("2026-09")
    _no_mail.clear()
    app.run_saft_automation(datetime.datetime(2026, 10, 13, 9, 0, tzinfo=SOFIA))
    assert len(_no_mail) == 1 and "напомняне" in _no_mail[0][0][1]
    app.set_setting("saft:2026-09", "")
