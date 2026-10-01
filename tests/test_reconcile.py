# -*- coding: utf-8 -*-
"""scripts_reconcile_stripe.py срещу Stripe обекти от stripe-python 13+
(не са dict: .get() и dict() гърмят) — без мрежа."""
import datetime
import secrets
import sqlite3
from zoneinfo import ZoneInfo

import pytest
import stripe

import scripts_reconcile_stripe as R

SOFIA = ZoneInfo("Europe/Sofia")


def _ts(*args):
    return int(datetime.datetime(*args, tzinfo=SOFIA).timestamp())


class _Pager:
    def __init__(self, items):
        self.items = items

    def auto_paging_iter(self):
        return iter(self.items)


class _FakeStripe:
    """Колкото ползва скриптът: Session.list/retrieve и Refund.list."""

    def __init__(self, sessions, refunds):
        by_id = {s["id"]: s for s in sessions}
        outer = self

        class _Session:
            @staticmethod
            def list(limit=100, payment_intent=None):
                objs = [stripe.checkout.Session.construct_from(s, "sk_test") for s in sessions]
                if payment_intent is None:
                    return _Pager(objs)
                data = [s for s in sessions if s.get("payment_intent") == payment_intent][:limit]
                return stripe.ListObject.construct_from({"object": "list", "data": data}, "sk_test")

            @staticmethod
            def retrieve(sid):
                outer.retrieved.append(sid)
                return stripe.checkout.Session.construct_from(by_id[sid], "sk_test")

        class _Checkout:
            Session = _Session

        class _Refund:
            @staticmethod
            def list(limit=100):
                return _Pager([stripe.Refund.construct_from(r, "sk_test") for r in refunds])

        self.checkout = _Checkout
        self.Refund = _Refund
        self.retrieved = []


def _session(user_id, key, amount, sid, intent):
    return {"id": sid, "object": "checkout.session", "amount_total": amount, "amount_subtotal": amount,
            "currency": "eur", "payment_status": "paid", "payment_intent": intent,
            "customer_details": {"email": "buyer@example.com"},
            "metadata": {"kind": "feature", "user_id": str(user_id), "feature_key": key}}


def _refund(intent, amount, created, status="succeeded"):
    return {"id": "re_" + secrets.token_hex(6), "object": "refund", "amount": amount,
            "payment_intent": intent, "created": created, "status": status, "currency": "eur"}


@pytest.fixture
def fake(app, monkeypatch):
    monkeypatch.setattr(app, "send_email", lambda *a, **k: None)
    monkeypatch.setattr(app, "send_sale_documents_for_payment", lambda pid, email: None)
    monkeypatch.setattr(R.billing, "checkout_key_present", lambda: True)

    def install(sessions, refunds):
        client = _FakeStripe(sessions, refunds)
        monkeypatch.setattr(R.billing, "get_stripe", lambda: client)
        return client
    return install


def test_paid_but_locked_session_is_unlocked(app, user, fake, capsys):
    sid, intent = "cs_lost_" + secrets.token_hex(4), "pi_lost_" + secrets.token_hex(4)
    client = fake([_session(user["id"], "akashic", 900, sid, intent)], [])
    assert R.main(False) == 0
    assert "неотключени" in capsys.readouterr().out
    assert "akashic" not in app.unlocked_features(app.get_user_by_id(user["id"]))
    R.main(True)
    assert client.retrieved == [sid]
    assert "akashic" in app.unlocked_features(app.get_user_by_id(user["id"]))
    R.main(True)
    assert client.retrieved == [sid], "второ пускане не пипа нищо"


def test_revoked_module_is_reported_not_regranted(app, db, user, fake, capsys):
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    sid, intent = "cs_rev_" + secrets.token_hex(4), "pi_rev_" + secrets.token_hex(4)
    s = _session(user["id"], "moon", 299, sid, intent)
    app.fulfill_checkout_session(s)
    app.api_admin_revoke_feature(user["id"], "moon", admin=admin)
    client = fake([s], [])
    R.main(True)
    assert "махнато" in capsys.readouterr().out
    assert client.retrieved == [] and "moon" not in app.unlocked_features(app.get_user_by_id(user["id"]))


def test_missing_stripe_refunds_are_recorded_in_their_own_month(app, user, fake):
    """Връщанията отпреди webhook-ът да слуша charge.refunded: всяко със
    своята дата — частичните в различни месеци остават в различни месеци."""
    sid, intent = "cs_ref_" + secrets.token_hex(4), "pi_ref_" + secrets.token_hex(4)
    s = _session(user["id"], "akashic", 900, sid, intent)
    app.fulfill_checkout_session(s)
    pid = app._payment_by_session(sid)["id"]
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE payments SET paid_at = '2026-09-10 09:00:00' WHERE id = ?", (pid,))
        c.commit()
    refunds = [_refund(intent, 300, _ts(2026, 9, 20, 12)), _refund(intent, 200, _ts(2026, 10, 3, 12)),
               _refund(intent, 400, _ts(2026, 10, 4, 12), status="failed")]
    fake([s], refunds)
    R.main(False)
    assert app.refunded_cents(pid) == 0, "пробният режим не записва"
    R.main(True)
    assert app.refunded_cents(pid) == 500
    with sqlite3.connect(app.DB_PATH) as c:
        dates = [app.utc_to_sofia(r[0]).strftime("%Y-%m-%d") for r in c.execute(
            "SELECT refunded_at FROM payment_refunds WHERE payment_id = ? ORDER BY refunded_at", (pid,))]
    assert dates == ["2026-09-20", "2026-10-03"]
    assert app.build_month_saft(2026, 9)["refund_cents"] == 300
    R.main(True)
    assert app.refunded_cents(pid) == 500, "второ пускане не записва наново"


def test_refund_already_recorded_by_the_webhook_is_not_doubled(app, user, fake):
    sid, intent = "cs_dup_" + secrets.token_hex(4), "pi_dup_" + secrets.token_hex(4)
    s = _session(user["id"], "moon", 299, sid, intent)
    app.fulfill_checkout_session(s)
    pid = app._payment_by_session(sid)["id"]
    app.record_stripe_refund({"payment_intent": intent, "amount_refunded": 299}, "evt_x",
                             _ts(2026, 9, 20, 12))
    fake([s], [_refund(intent, 299, _ts(2026, 9, 20, 12))])
    R.main(True)
    assert app.refunded_cents(pid) == 299
