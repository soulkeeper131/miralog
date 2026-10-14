# -*- coding: utf-8 -*-
"""Отчетите за собственика: фуния, приходи, изоставени плащания, източници."""
import datetime
import secrets
import sqlite3

import pytest
from fastapi import HTTPException


@pytest.fixture
def clean(app, db):
    with sqlite3.connect(app.DB_PATH) as c:
        for t in ("page_views", "events", "checkout_sessions", "ai_usage"):
            c.execute(f"DELETE FROM {t}")
        c.commit()
    yield app


def _now():
    return datetime.datetime.utcnow().isoformat(timespec="seconds")


def _sale(app, user_id, keys, amounts, *, ref=None):
    meta = {"kind": "features", "user_id": str(user_id), "feature_keys": ",".join(keys),
            "feature_amounts": ",".join(f"{k}:{a}" for k, a in zip(keys, amounts))}
    if ref:
        meta["checkout_ref"] = ref
    s = {"id": "cs_test_" + secrets.token_hex(6), "amount_total": sum(amounts),
         "amount_subtotal": sum(amounts), "currency": "eur", "payment_status": "paid",
         "payment_intent": "pi_" + secrets.token_hex(6), "customer_details": {"email": "x@example.com"},
         "metadata": meta}
    app.fulfill_checkout_session(s)
    return s


def test_funnel_counts_each_step(clean, user):
    app = clean
    with sqlite3.connect(app.DB_PATH) as c:
        for v in ("a", "a", "b", "c"):
            c.execute("INSERT INTO page_views (path, viewed_at, visitor) VALUES ('/', ?, ?)", (_now(), v))
        c.commit()
    app.record_event("guest_chart")
    app.record_event("guest_chart")
    app.record_event("locked_view", user_id=user["id"], feature="moon")
    app.record_event("locked_view", user_id=user["id"], feature="moon")   # същият човек
    paid = app.checkout_tracking(None)
    app.record_checkout(paid, user_id=user["id"], keys=["moon"], amount_cents=299, currency="EUR", source="module")
    lost = app.checkout_tracking(None)
    app.record_checkout(lost, user_id=user["id"], keys=["love"], amount_cents=500, currency="EUR", source="module")
    _sale(app, user["id"], ["moon"], [299], ref=paid["checkout_ref"])

    r = app.build_reports(30)
    f = r["funnel"]
    assert f["visitors"] == 3 and f["page_views"] == 4
    assert f["guest_charts"] == 2
    assert f["locked_viewers"] == 1
    assert f["checkouts"] == 2 and f["orders"] == 1 and f["buyers"] == 1
    assert f["revenue_cents"] == 299
    assert r["conversion"]["checkout_to_paid"] == 50.0
    mods = {m["key"]: m for m in r["revenue"]["by_module"]}
    assert mods["moon"]["amount_cents"] == 299
    locked = {x["key"]: x for x in r["locked"]}
    assert locked["moon"]["views"] == 2 and locked["moon"]["people"] == 1 and locked["moon"]["bought"] == 1


def test_abandoned_checkout_is_listed_after_an_hour(clean, user):
    app = clean
    t = app.checkout_tracking(None)
    app.record_checkout(t, user_id=user["id"], keys=["akashic"], amount_cents=900, currency="EUR", source="start")
    later = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
    r = app.build_reports(30, now=later)
    assert r["abandoned"]["count"] == 1
    row = r["abandoned"]["recent"][0]
    assert row["email"] == user["email"] and "Акашови" in row["names"] and row["amount_cents"] == 900
    # Платеното не е изоставено.
    _sale(app, user["id"], ["akashic"], [900], ref=t["checkout_ref"])
    assert app.build_reports(30, now=later)["abandoned"]["count"] == 0


def test_test_and_admin_grants_are_not_revenue(clean, user):
    app = clean
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("INSERT INTO payments (user_id, amount_cents, currency, method, paid_at)"
                  " VALUES (?, 2500, 'EUR', 'тест', CURRENT_TIMESTAMP)", (user["id"],))
        c.execute("INSERT INTO payments (user_id, amount_cents, currency, method, paid_at)"
                  " VALUES (?, 500, 'EUR', 'еднократно', CURRENT_TIMESTAMP)", (user["id"],))
        c.commit()
    assert app.build_reports(30)["funnel"]["revenue_cents"] == 0


def test_sources_and_devices(clean):
    app = clean
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("INSERT INTO page_views (path, viewed_at, visitor, referrer, device) VALUES ('/', ?, 'a', 'google.com', 'mobile')", (_now(),))
        c.execute("INSERT INTO page_views (path, viewed_at, visitor, utm_source, utm_medium, device)"
                  " VALUES ('/horoskop', ?, 'b', 'facebook', 'paid', 'desktop')", (_now(),))
        c.execute("INSERT INTO page_views (path, viewed_at, visitor, device) VALUES ('/chart/12', ?, 'c', 'mobile')", (_now(),))
        c.execute("INSERT INTO page_views (path, viewed_at, visitor, device) VALUES ('/chart/99', ?, 'd', 'mobile')", (_now(),))
        c.commit()
    r = app.build_reports(30)
    src = {(x["source"], x["medium"]): x["visitors"] for x in r["sources"]}
    assert src[("google.com", "referral")] == 1 and src[("facebook", "paid")] == 1
    assert {x["device"]: x["visitors"] for x in r["devices"]}["mobile"] == 3
    # /chart/12 и /chart/99 са един ред — иначе таблицата е списък с хора.
    assert {p["path"]: p["views"] for p in r["top_pages"]}["/chart/…"] == 2


class _Req:
    def __init__(self, referer="", ua="Mozilla/5.0 (iPhone)", ip="1.2.3.4"):
        self.headers = {"referer": referer, "user-agent": ua, "host": "astrokarta.bg"}
        self.client = type("C", (), {"host": ip})()
        self.cookies = {}
        self.query_params = {}


def test_visitor_hash_is_stable_for_the_day_and_anonymous(app):
    a, b = app.visitor_hash(_Req()), app.visitor_hash(_Req())
    assert a == b and len(a) == 16 and "1.2.3.4" not in a
    assert app.visitor_hash(_Req(ip="5.6.7.8")) != a


@pytest.mark.parametrize("ref,expected", [
    ("https://www.google.com/search?q=x", "google.com"),
    ("https://astrokarta.bg/horoskop", None),
    ("https://checkout.stripe.com/c/pay/cs_1", None),
    ("", None),
])
def test_referrer_host(app, ref, expected):
    assert app._referrer_host(_Req(referer=ref)) == expected


def test_client_events_accept_only_known_names(clean):
    app = clean
    with pytest.raises(HTTPException) as e:
        app.api_client_event(app.ClientEvent(event="purchase"), _Req())
    assert e.value.status_code == 400
    assert app.api_client_event(app.ClientEvent(event="locked_view", feature="moon"), _Req())["ok"]
    with sqlite3.connect(app.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM events WHERE event = 'locked_view'").fetchone()[0] == 1


def test_signup_method_and_last_login_are_written(app, user):
    app.note_signup(user["id"], "google")
    app.note_signup(user["id"], "register")      # първият начин остава
    row = app.get_user_by_id(user["id"])
    assert row["signup_method"] == "google" and row["last_login"]


def test_reports_endpoint_needs_admin(app):
    """Отчетът е зад require_admin като другите админ адреси."""
    import inspect
    sig = inspect.signature(app.api_admin_reports)
    assert sig.parameters["admin"].default.dependency is app.require_admin


def test_daily_summary_shows_funnel_and_abandoned(clean, user):
    app = clean
    yesterday = datetime.datetime.now(app.ZoneInfo("Europe/Sofia")).date() - datetime.timedelta(days=1)
    at = (datetime.datetime.combine(yesterday, datetime.time(12), app.ZoneInfo("Europe/Sofia"))
          .astimezone(datetime.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"))
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("INSERT INTO checkout_sessions (ref, user_id, keys, amount_cents, currency, source, created_at)"
                  " VALUES ('r1', ?, 'akashic', 900, 'EUR', 'module', ?)", (user["id"], at))
        c.execute("INSERT INTO events (at, event, user_id, feature) VALUES (?, 'locked_view', ?, 'moon')", (at, user["id"]))
        c.commit()
    text = app.build_daily_summary(yesterday)
    assert "Фуния" in text and "започнали плащане 1" in text
    assert "Започнали плащане, без да платят: 1" in text and user["email"] in text
    assert "Най-гледани заключени модули" in text
