# -*- coding: utf-8 -*-
"""Известия до собственика по имейл: плащания, проблеми, сутрешно обобщение.

Писмата се подменят — нищо не тръгва навън. В тестовете изпращането е
синхронно (conftest), за да се вижда веднага; отделен тест проверява, че
в production не спира заявката.
"""
import datetime
import secrets
import sqlite3
import threading
import time

import pytest


@pytest.fixture
def mails(app, monkeypatch):
    sent = []
    monkeypatch.setattr(app, "send_email",
                        lambda to, subject, body, **k: sent.append((to, subject, body)))
    monkeypatch.setattr(app, "smtp_setting",
                        lambda key: "smtp.example.com" if key == "smtp_host" else "")
    app.set_setting("notify_email", "vladi@example.com")
    for kind in ("payments", "problems", "daily", "new_users"):
        app.set_setting(f"notify_{kind}", "1")
    app._PROBLEM_LAST.clear()
    app._AI_FAILURES.clear()
    yield sent
    app.set_setting("notify_email", "")


def _session(user_id, keys, amount):
    return {"id": "cs_live_" + secrets.token_hex(6), "amount_total": amount,
            "currency": "eur", "payment_status": "paid",
            "metadata": {"kind": "features", "user_id": str(user_id),
                         "feature_keys": ",".join(keys)}}


# --- плащания ------------------------------------------------------------------

def test_payment_is_announced(app, user, mails):
    app.fulfill_checkout_session(_session(user["id"], ["profile", "akashic"], 1398))
    notes = [m for m in mails if "плащане" in m[1].lower()]
    assert notes, "няма писмо за плащането"
    to, subject, body = notes[0]
    assert to == "vladi@example.com"
    assert "13.98" in subject + body
    assert user["email"] in body
    assert "Пълен астрологически профил" in body


def test_repeated_delivery_announces_once(app, user, mails):
    sess = _session(user["id"], ["profile"], 499)
    for _ in range(3):
        app.fulfill_checkout_session(sess)
    assert len([m for m in mails if "плащане" in m[1].lower()]) == 1


def test_payment_says_how_it_was_confirmed(app, user, mails):
    token = app.AI_ORIGIN.set(("/api/stripe/webhook", None))
    try:
        app.fulfill_checkout_session(_session(user["id"], ["profile"], 499))
    finally:
        app.AI_ORIGIN.reset(token)
    assert "webhook" in mails[-1][2].lower()


def test_payment_notice_can_be_switched_off(app, user, mails):
    app.set_setting("notify_payments", "0")
    app.fulfill_checkout_session(_session(user["id"], ["profile"], 499))
    assert not [m for m in mails if "плащане" in m[1].lower()]


# --- проблеми ------------------------------------------------------------------

def test_crash_is_reported_with_its_code(app, mails):
    app.report_problem("crash", "Срив на сайта", "GET /chart/5", code="7F3A2C")
    assert mails and "7F3A2C" in mails[0][2]


def test_repeated_problem_waits_half_an_hour(app, mails, monkeypatch):
    """Един бъг може да гърми на всяка заявка — едно писмо, не сто."""
    clock = [1000.0]
    monkeypatch.setattr(app, "_now_monotonic", lambda: clock[0])
    for _ in range(5):
        app.report_problem("crash", "Срив на сайта", "GET /x", code="AAAAAA")
    assert len(mails) == 1
    clock[0] += app.PROBLEM_COOLDOWN + 1
    app.report_problem("crash", "Срив на сайта", "GET /x", code="BBBBBB")
    assert len(mails) == 2
    assert "4 подобни" in mails[1][2], "не казва колко са пропуснати"


def test_different_problems_are_not_merged(app, mails):
    app.report_problem("crash", "Срив", "…")
    app.report_problem("webhook", "Stripe webhook", "…")
    assert len(mails) == 2


def test_a_real_crash_triggers_the_report(app, mails):
    from tests.test_logging import _Client
    import app as app_module
    if not any(getattr(r, "path", "") == "/__test/notify-boom" for r in app_module.app.routes):
        @app_module.app.get("/__test/notify-boom")
        def _boom():
            raise RuntimeError("гърмим нарочно")
    r = _Client(app_module.app).get("/__test/notify-boom")
    assert r.status_code == 500
    assert any(r.headers["x-request-id"] in m[2] for m in mails)


def test_refused_webhook_is_reported(app, mails, monkeypatch):
    import asyncio
    monkeypatch.setattr(app.billing, "construct_webhook_event",
                        lambda p, s: (_ for _ in ()).throw(ValueError("лош подпис")))

    class _R:
        headers = {"stripe-signature": "x"}
        async def body(self):
            return b"{}"
    with pytest.raises(Exception):
        asyncio.run(app.api_stripe_webhook(_R()))
    assert any("webhook" in m[1].lower() for m in mails)


def test_ai_outage_is_reported_after_three_failures(app, mails, monkeypatch):
    def fail(*a, **k):
        raise app.AIError("deepseek не отговаря")
    monkeypatch.setattr(app, "_call_ai_unlogged", fail)
    for i in range(2):
        with pytest.raises(app.AIError):
            app.call_ai("k", "deepseek", "п", model="deepseek-v4-flash")
    assert not mails, "две грешки може да са случайност"
    with pytest.raises(app.AIError):
        app.call_ai("k", "deepseek", "п", model="deepseek-v4-flash")
    assert mails and "AI" in mails[0][1]


def test_problem_notice_can_be_switched_off(app, mails):
    app.set_setting("notify_problems", "0")
    app.report_problem("crash", "Срив", "…")
    assert mails == []


# --- сутрешно обобщение --------------------------------------------------------

def _sofia(y, m, d, h):
    return datetime.datetime(y, m, d, h, 0, tzinfo=app_tz())


def app_tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo("Europe/Sofia")


def test_summary_waits_for_the_morning(app, mails):
    app.set_setting("daily_summary_sent", "")
    assert app.maybe_send_daily_summary(now=_sofia(2026, 10, 1, 6)) is False
    assert mails == []


def test_summary_is_sent_once_a_day(app, db, mails):
    app.set_setting("daily_summary_sent", "")
    assert app.maybe_send_daily_summary(now=_sofia(2026, 10, 1, 8)) is True
    assert app.maybe_send_daily_summary(now=_sofia(2026, 10, 1, 9)) is False
    assert len(mails) == 1
    assert app.maybe_send_daily_summary(now=_sofia(2026, 10, 2, 8)) is True


def test_summary_can_be_switched_off(app, mails):
    app.set_setting("daily_summary_sent", "")
    app.set_setting("notify_daily", "0")
    assert app.maybe_send_daily_summary(now=_sofia(2026, 10, 1, 8)) is False
    assert mails == []


def test_summary_counts_yesterday(app, db, mails):
    """Регистрации, реални Stripe плащания, AI разход и хороскопите — за вчера."""
    y_utc = "2026-09-30 10:00:00"      # вчера по София, ако днес е 1 октомври
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("INSERT INTO users (email, password_hash, role, created_at) VALUES (?, 'x', 'user', ?)",
                  ("vchera@example.com", y_utc))
        uid = c.execute("SELECT id FROM users WHERE email = 'vchera@example.com'").fetchone()[0]
        c.execute("INSERT INTO payments (user_id, amount_cents, currency, method, note, paid_at)"
                  " VALUES (?, 2500, 'EUR', 'stripe', 'features:bundle', ?)", (uid, y_utc))
        c.execute("INSERT INTO payments (user_id, amount_cents, currency, method, note, paid_at)"
                  " VALUES (?, 999, 'EUR', 'еднократно', 'ръчно', ?)", (uid, y_utc))
        c.execute("INSERT INTO ai_usage (at, provider, model, source, feature, input_tokens,"
                  " output_tokens, cost_usd, ok) VALUES ('2026-09-30T10:00:00', 'deepseek',"
                  " 'deepseek-v4-pro', 'client', 'profile', 1000, 2000, 0.0123, 1)")
        c.commit()
    text = app.build_daily_summary(datetime.date(2026, 9, 30))
    assert "vchera@example.com" in text
    assert "25.00" in text, "реалното плащане липсва"
    assert "9.99" not in text, "ръчното отключване е броено като приход"
    assert "0.0123" in text or "0.01" in text


def test_summary_warns_when_webhook_is_missing(app, db, mails):
    y_utc = "2026-09-30 11:00:00"
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("INSERT INTO users (email, password_hash, role, created_at) VALUES (?, 'x', 'user', ?)",
                  ("wh@example.com", y_utc))
        uid = c.execute("SELECT id FROM users WHERE email = 'wh@example.com'").fetchone()[0]
        c.execute("INSERT INTO payments (user_id, amount_cents, currency, method, note, paid_at)"
                  " VALUES (?, 699, 'EUR', 'stripe', 'features:numerology', ?)", (uid, y_utc))
        c.commit()
    text = app.build_daily_summary(datetime.date(2026, 9, 30))
    assert "webhook" in text.lower() and "⚠" in text


# --- не бави заявката ------------------------------------------------------------

def test_notices_do_not_block_the_request(app, mails, monkeypatch):
    """SMTP може да чака до 30 s — регистрацията и плащането не бива да чакат с него."""
    release = threading.Event()
    monkeypatch.setattr(app, "send_email", lambda *a, **k: release.wait(5))
    monkeypatch.setattr(app, "NOTIFY_ASYNC", True)
    started = time.monotonic()
    app.report_problem("crash", "Срив", "…")
    assert time.monotonic() - started < 1
    release.set()


def test_admin_can_preview_the_summary_without_sending(app, mails):
    out = app.api_admin_daily_summary(admin={"id": 1})
    assert out["text"].startswith("Обобщение за")
    assert out["to"] == "vladi@example.com"
    assert mails == [], "прегледът не бива да праща писмо"


# --- настройки -------------------------------------------------------------------

def test_settings_expose_every_switch(app, mails):
    notify = app.api_admin_settings(admin={"id": 1})["notify"]
    for key in ("new_users", "payments", "problems", "daily"):
        assert key in notify


def test_settings_save_every_switch(app, mails):
    app.api_admin_save_settings({"notify": {"payments": False, "problems": True, "daily": False}},
                                admin={"id": 1, "email": "admin@example.com"})
    assert app.notify_enabled("payments") is False
    assert app.notify_enabled("problems") is True
    assert app.notify_enabled("daily") is False
