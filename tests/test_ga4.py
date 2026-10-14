# -*- coding: utf-8 -*-
"""Google Analytics 4: покупката и от сървъра, без двойни, без админа, с user_id."""
import json
import secrets

import pytest


def _session(user_id, keys, amounts, *, ga_cid="123456789.1700000000"):
    meta = {"kind": "features", "user_id": str(user_id), "feature_keys": ",".join(keys),
            "feature_amounts": ",".join(f"{k}:{a}" for k, a in zip(keys, amounts))}
    if ga_cid:
        meta["ga_cid"] = ga_cid
    return {"id": "cs_test_" + secrets.token_hex(8), "amount_total": sum(amounts),
            "amount_subtotal": sum(amounts), "currency": "eur", "payment_status": "paid",
            "payment_intent": "pi_" + secrets.token_hex(8), "customer_details": {"email": "b@example.com"},
            "metadata": meta}


@pytest.fixture
def mp(app, monkeypatch):
    """Подменя изпращането към Measurement Protocol и пази заявките."""
    sent = []

    class _R:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None):
        if "mp/collect" in req.full_url:
            sent.append((req.full_url, json.loads(req.data.decode())))
        return _R()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    app.set_setting("ga_api_secret", "test-secret")
    yield sent
    app.set_setting("ga_api_secret", "")


def test_purchase_is_sent_from_the_server(app, user, mp):
    """Покупката идваше само от браузъра — губеше се при затворен раздел."""
    s = _session(user["id"], ["moon", "numerology"], [299, 400])
    app.fulfill_checkout_session(s)
    purchases = [b for u, b in mp if b["events"][0]["name"] == "purchase"]
    assert len(purchases) == 1
    body = purchases[0]
    ev = body["events"][0]["params"]
    assert body["client_id"] == "123456789.1700000000"
    assert body["user_id"] == str(user["id"])
    assert ev["transaction_id"] == s["id"] and ev["value"] == 6.99 and ev["currency"] == "EUR"
    assert [i["item_id"] for i in ev["items"]] == ["moon", "numerology"]
    assert ev["items"][0]["price"] == 2.99


def test_repeat_webhook_does_not_send_a_second_purchase(app, user, mp):
    s = _session(user["id"], ["moon"], [299])
    app.fulfill_checkout_session(s)
    app.fulfill_checkout_session(s)
    assert sum(1 for u, b in mp if b["events"][0]["name"] == "purchase") == 1


def test_no_purchase_without_consent_cookie(app, user, mp):
    """Без _ga бисквитка (без съгласие) сървърът не праща нищо."""
    app.fulfill_checkout_session(_session(user["id"], ["moon"], [299], ga_cid=None))
    assert not mp


def test_secret_never_reaches_the_url_without_being_set(app, user, monkeypatch, mp):
    app.set_setting("ga_api_secret", "")
    app.fulfill_checkout_session(_session(user["id"], ["moon"], [299]))
    assert not mp


class _Req:
    def __init__(self, cookies=None, host="astrokarta.bg", auth=None):
        self.cookies = cookies or {}
        self.headers = {"host": host}
        if auth:
            self.headers["authorization"] = auth
        self.query_params = {}


@pytest.mark.parametrize("cookie,expected", [
    ("GA1.1.123456789.1700000000", "123456789.1700000000"),
    ("GA1.2.55.66", "55.66"),
    ("", ""), ("нещо", ""), ("GA1.1.abc.def", ""),
])
def test_client_id_from_ga_cookie(app, cookie, expected):
    assert app.ga_client_id_from_request(_Req({"_ga": cookie} if cookie else {})) == expected


def test_checkout_carries_the_client_id_to_stripe():
    import billing
    params = {"metadata": {"kind": "features"}}
    billing._add_metadata(params, {"ga_cid": "1.2", "empty": ""})
    assert params["metadata"] == {"kind": "features", "ga_cid": "1.2"}


def _render_analytics(app, request):
    return app.templates.env.from_string('{% include "_analytics.html" %}').render(request=request)


def test_admin_host_has_no_tracking(app):
    """Админът пълнеше GA4 със собствените си прегледи."""
    html = _render_analytics(app, _Req(host=app.ADMIN_HOST))
    assert "googletagmanager" not in html and "window.track" in html


def test_public_page_has_tracking_and_stubs(app):
    html = _render_analytics(app, _Req())
    assert "googletagmanager" in html
    # Празните track()/trackPurchase() са преди всичко — блокиран consent.js
    # не бива да спира пренасочването към Stripe.
    assert html.index("window.track = window.track ||") < html.index("gtag('config'")
    # Запазеното съгласие важи преди config, не след него.
    assert html.index("miralog_consent") < html.index("gtag('config'")


def test_logged_in_user_gets_user_id(app, user, monkeypatch):
    token = app.create_token(user["id"], user["email"])
    monkeypatch.setattr(app, "_token_from_request", lambda request: token)
    html = _render_analytics(app, _Req())
    assert f"'user_id': '{user['id']}'" in html


def test_oauth_signup_vs_login_is_told_apart(app, db):
    email = f"g-{secrets.token_hex(3)}@example.com"
    first = app._oauth_link_or_create("google", "gid-" + secrets.token_hex(4), email, "Тест",
                                      email_verified=True)
    assert first.get("_new_account") is True
    again = app._oauth_link_or_create("google", "gid-x", email, "Тест", email_verified=True)
    assert not again.get("_new_account")
