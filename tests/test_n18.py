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
    charge = {"payment_intent": "pi_refund_me", "amount_refunded": 300, "created": 1790000000}
    app.record_stripe_refund(charge, "evt_1")
    app.record_stripe_refund(charge, "evt_1")          # повторно изпратено събитие
    assert app.refunded_cents(pay["id"]) == 300
    app.record_stripe_refund({**charge, "amount_refunded": 900}, "evt_2")
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
    app.record_stripe_refund({"payment_intent": "pi_ref", "amount_refunded": 299,
                              "created": int(datetime.datetime(2026, 9, 20, tzinfo=SOFIA).timestamp())},
                             "evt_ref")
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
