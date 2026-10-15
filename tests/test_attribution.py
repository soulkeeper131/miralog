# -*- coding: utf-8 -*-
"""Откъде идва човекът: от клика по рекламата до регистрацията и плащането,
и събитията към Meta/Google от сървъра."""
import datetime
import json
import secrets
import sqlite3
import urllib.parse

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

BROWSER = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit/605 Mobile Safari",
           "accept-language": "bg-BG,bg;q=0.9"}
GUEST = {"name": "Мария", "year": 1990, "month": 5, "day": 17, "hour": 14, "minute": 30,
         "lat": 42.15, "lon": 24.75, "timezone": "Europe/Sofia"}


def _req(path="/", query="", headers=None, cookies=""):
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        hdrs.append((b"cookie", cookies.encode()))
    return Request({"type": "http", "method": "GET", "path": path, "root_path": "",
                    "scheme": "https", "server": ("astrokarta.bg", 443),
                    "query_string": urllib.parse.quote(query, safe="=&").encode(), "headers": hdrs, "client": ("8.8.8.8", 1)})


@pytest.fixture
def client(app, db):
    with sqlite3.connect(app.DB_PATH) as c:
        for t in ("page_views", "events", "checkout_sessions"):
            c.execute(f"DELETE FROM {t}")
    c = TestClient(app.app)
    c.headers.update(BROWSER)
    return c


# --- кой е входът ------------------------------------------------------------

@pytest.mark.parametrize("query,referer,expected", [
    ("utm_source=Facebook&utm_medium=Paid_Social&utm_campaign=Есен-2026&utm_content=video1&utm_term=25-34",
     "", {"s": "facebook", "m": "paid_social", "c": "Есен-2026", "ct": "video1", "t": "25-34"}),
    ("gclid=abc123", "", {"s": "google", "m": "cpc", "k": "gclid"}),
    ("fbclid=xyz", "https://l.facebook.com/", {"s": "facebook", "m": "social", "k": "fbclid"}),
    ("", "https://www.google.com/", {"s": "google", "m": "organic"}),
    ("", "https://m.facebook.com/story", {"s": "facebook", "m": "social"}),
    ("", "https://zodii.bg/statia", {"s": "zodii.bg", "m": "referral"}),
])
def test_touch_from_request(app, query, referer, expected):
    t = app.touch_from_request(_req("/horoskop/oven", query, {"referer": referer} if referer else {}))
    for k, v in expected.items():
        assert t[k] == v, (k, t)
    assert t["lp"] == "/horoskop/oven"


@pytest.mark.parametrize("path,referer", [
    ("/", ""),                                   # директно
    ("/", "https://astrokarta.bg/horoskop"),     # вътрешно
    ("/", "https://www.astrokarta.bg/"),
    ("/api/auth/facebook/callback", "https://www.facebook.com/"),   # връщане от вход
])
def test_no_touch(app, path, referer):
    assert app.touch_from_request(_req(path, "", {"referer": referer} if referer else {})) is None


# --- бисквитката и регистрацията ------------------------------------------------

def test_ad_click_is_remembered_until_signup(client, app):
    r = client.get("/?utm_source=facebook&utm_medium=paid_social&utm_campaign=autumn&utm_content=reel2")
    assert r.status_code == 200 and app.ATTR_COOKIE in client.cookies
    # Обикаля сайта (без UTM) — входът не се губи.
    client.get("/horoskop")
    r = client.post("/api/onboard", json={**GUEST, "email": f"ad-{secrets.token_hex(3)}@example.com"})
    assert r.status_code == 200, r.text
    assert r.json()["event_id"].startswith("reg-")
    with sqlite3.connect(app.DB_PATH) as c:
        row = c.execute("SELECT acq_source, acq_medium, acq_campaign, acq_content, acq_landing, acq_first"
                        " FROM users WHERE email LIKE 'ad-%' ORDER BY id DESC").fetchone()
    assert row[:5] == ("facebook", "paid_social", "autumn", "reel2", "/")
    assert json.loads(row[5])["c"] == "autumn"
    # Посещението от рекламата е и в прегледите.
    with sqlite3.connect(app.DB_PATH) as c:
        pv = c.execute("SELECT utm_source, utm_campaign, utm_content FROM page_views"
                       " WHERE utm_source IS NOT NULL").fetchall()
    assert pv == [("facebook", "autumn", "reel2")]


def test_first_touch_stays_last_touch_moves(client, app):
    client.get("/?utm_source=google&utm_medium=cpc&utm_campaign=brand")
    client.get("/horoskop?utm_source=facebook&utm_medium=paid_social&utm_campaign=retarget")
    attr = app.read_attribution(_req(cookies=f"{app.ATTR_COOKIE}={client.cookies[app.ATTR_COOKIE]}"))
    assert attr["f"]["c"] == "brand" and attr["l"]["c"] == "retarget"


def test_gclid_without_utm_counts_as_google_ads(client, app):
    client.get("/?gclid=Cj0KCQ")
    with sqlite3.connect(app.DB_PATH) as c:
        assert c.execute("SELECT utm_source, utm_medium, click FROM page_views").fetchone() == ("google", "cpc", "gclid")


def test_broken_cookie_is_ignored(app):
    assert app.read_attribution(_req(cookies=f"{app.ATTR_COOKIE}=%%%не-е-base64")) == {}


def test_guest_chart_carries_the_campaign(client, app):
    client.get("/start?utm_source=facebook&utm_medium=paid_social&utm_campaign=autumn")
    r = client.post("/api/guest/chart", json=GUEST)
    assert r.status_code == 200 and r.json()["event_id"].startswith("lead-")
    with sqlite3.connect(app.DB_PATH) as c:
        meta = c.execute("SELECT meta FROM events WHERE event = 'guest_chart'").fetchone()[0]
    assert json.loads(meta)["src"] == {"s": "facebook", "m": "paid_social", "c": "autumn"}


# --- плащането -----------------------------------------------------------------

def test_checkout_keeps_campaign_and_meta_context_only_with_consent(app, db):
    cookie = app.attribution_cookie_value(_req(), {"s": "google", "m": "cpc", "c": "brand", "lp": "/"})
    with_consent = _req(headers=BROWSER, cookies=f"{app.ATTR_COOKIE}={cookie}; _fbp=fb.1.1.2; _fbc=fb.1.1.abc")
    t = app.checkout_tracking(with_consent)
    assert t["utm_campaign"] == "brand" and t["_fb"]["fbp"] == "fb.1.1.2"
    app.record_checkout(t, user_id=1, keys=["profile"], amount_cents=499, currency="EUR", source="module")
    with sqlite3.connect(app.DB_PATH) as c:
        row = c.execute("SELECT acq_source, acq_campaign, fb_ctx FROM checkout_sessions WHERE ref = ?",
                        (t["checkout_ref"],)).fetchone()
    assert row[0] == "google" and row[1] == "brand" and json.loads(row[2])["fbc"] == "fb.1.1.abc"
    # Контекстът за Meta се чете веднъж и се изтрива.
    assert app.checkout_fb_context(t["checkout_ref"])["fbp"] == "fb.1.1.2"
    assert app.checkout_fb_context(t["checkout_ref"]) == {}
    # Без съгласие (няма _fbp) — нищо за Meta.
    assert app.checkout_tracking(_req(headers=BROWSER))["_fb"] == {}


def test_private_keys_do_not_go_to_stripe():
    import billing
    params = {}
    billing._add_metadata(params, {"ga_cid": "1.2", "utm_campaign": "brand",
                                   "_fb": {"ip": "1.2.3.4"}, "_touch": {"s": "x"}})
    assert params["metadata"] == {"ga_cid": "1.2", "utm_campaign": "brand"}


# --- Meta Conversions API --------------------------------------------------------

@pytest.fixture
def meta_on(app, monkeypatch):
    app.set_setting("fb_pixel_id", "1234567890")
    app.set_setting("fb_capi_token", "TOKEN")
    sent = []

    class _Resp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        sent.append((req.full_url, json.loads(req.data.decode())))
        return _Resp()

    monkeypatch.setattr(app.urllib.request, "urlopen", fake_urlopen)
    yield sent
    app.set_setting("fb_pixel_id", "")
    app.set_setting("fb_capi_token", "")


def test_meta_event_has_dedup_id_and_hashed_email(app, meta_on):
    ctx = {"fbp": "fb.1.1.2", "fbc": "fb.1.1.abc", "ip": "5.6.7.8", "ua": "Mozilla/5.0", "url": "https://astrokarta.bg/"}
    assert app.send_fb_event("Purchase", "cs_test_1", ctx, email=" Maria@Example.com ", user_id=7,
                             custom={"value": 4.99, "currency": "EUR"})
    url, body = meta_on[0]
    assert "/1234567890/events" in url and "access_token=TOKEN" in url
    ev = body["data"][0]
    assert ev["event_name"] == "Purchase" and ev["event_id"] == "cs_test_1"
    assert ev["action_source"] == "website" and ev["custom_data"]["value"] == 4.99
    ud = ev["user_data"]
    assert ud["em"] == [app._sha256("maria@example.com")] and "maria" not in json.dumps(ud).lower()
    assert ud["client_ip_address"] == "5.6.7.8" and ud["fbp"] == "fb.1.1.2"


def test_meta_needs_consent_token_and_pixel(app, meta_on):
    assert not app.send_fb_event("Lead", "x", {})                        # без съгласие
    app.set_setting("fb_capi_token", "")
    assert not app.send_fb_event("Lead", "x", {"fbp": "fb.1.1.2"})       # без токен
    assert meta_on == []


def test_signup_sends_complete_registration_with_consent(client, app, meta_on):
    client.cookies.set("_fbp", "fb.1.1.99")
    r = client.post("/api/onboard", json={**GUEST, "email": f"meta-{secrets.token_hex(3)}@example.com"})
    names = [b["data"][0]["event_name"] for _, b in meta_on]
    assert "CompleteRegistration" in names
    ev = next(b["data"][0] for _, b in meta_on if b["data"][0]["event_name"] == "CompleteRegistration")
    assert ev["event_id"] == r.json()["event_id"]


# --- отчетът по кампании ------------------------------------------------------------

def test_campaign_report_follows_the_money(client, app):
    now = datetime.datetime.utcnow().isoformat(timespec="seconds")
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("INSERT INTO page_views (path, viewed_at, visitor, utm_source, utm_medium, utm_campaign)"
                  " VALUES ('/', ?, 'v1', 'facebook', 'paid_social', 'autumn')", (now,))
        c.execute("INSERT INTO events (at, event, meta) VALUES (?, 'guest_chart', ?)",
                  (now, json.dumps({"src": {"s": "facebook", "m": "paid_social", "c": "autumn"}})))
        uid = c.execute("INSERT INTO users (email, password_hash, acq_source, acq_medium, acq_campaign)"
                        " VALUES (?, 'x', 'facebook', 'paid_social', 'autumn')",
                        (f"camp-{secrets.token_hex(3)}@example.com",)).lastrowid
        c.execute("INSERT INTO payments (user_id, amount_cents, currency, method, paid_at)"
                  " VALUES (?, 2500, 'EUR', 'stripe', ?)", (uid, now))
        c.commit()
    rows = app.build_reports(30)["campaigns"]
    autumn = next(r for r in rows if r["campaign"] == "autumn")
    assert (autumn["visits"], autumn["guest_charts"], autumn["signups"], autumn["buyers"],
            autumn["revenue_cents"]) == (1, 1, 1, 1, 2500)


# --- Google ----------------------------------------------------------------------------

def test_google_ads_tag_and_passthrough(client, app):
    html = client.get("/").text
    assert "url_passthrough" in html and "GOOGLE_ADS" not in html
    app.set_setting("google_ads_id", "AW-123456789")
    app.set_setting("google_ads_purchase_label", "AbC-123_x")
    app.set_setting("google_ads_signup_label", "bad label!")      # отхвърля се
    try:
        html = client.get("/").text
        assert '"id": "AW-123456789"' in html and '"purchase": "AbC-123_x"' in html
        assert "sign_up" not in app.google_ads_config()["labels"]
    finally:
        for k in ("google_ads_id", "google_ads_purchase_label", "google_ads_signup_label"):
            app.set_setting(k, "")
