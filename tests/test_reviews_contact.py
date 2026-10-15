# -*- coding: utf-8 -*-
"""Отзиви от потребителите (със звездите за Google) и контактната форма."""
import json
import re
import secrets
import sqlite3

import pytest
from fastapi.testclient import TestClient

BROWSER = {"user-agent": "Mozilla/5.0 (iPhone) Safari", "accept-language": "bg"}


@pytest.fixture
def client(app, db):
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("DELETE FROM reviews")
        c.execute("DELETE FROM contact_messages")
    c = TestClient(app.app, base_url="https://astrokarta.bg")
    c.headers.update(BROWSER)
    return c


def _auth(app, user):
    return {"Authorization": "Bearer " + app.create_token(user["id"], user["email"])}


def _admin(app):
    with sqlite3.connect(app.DB_PATH) as c:
        row = c.execute("SELECT id, email FROM users WHERE role = 'admin' LIMIT 1").fetchone()
    return {"Authorization": "Bearer " + app.create_token(row[0], row[1])}


def _adm(app):
    """Админ API-то е само на админ поддомейна."""
    c = TestClient(app.app, base_url=f"https://{app.ADMIN_HOST}")
    c.headers.update(_admin(app))
    return c


GOOD = {"feature": "chart", "rating": 5, "title": "Точна", "body": "Много точно описа характера ми и как реагирам.",
        "author": "Мария И."}


# --- отзиви --------------------------------------------------------------------

def test_review_needs_login_and_an_unlocked_module(client, app, user):
    assert client.post("/api/reviews", json=GOOD).status_code == 401
    r = client.post("/api/reviews", json={**GOOD, "feature": "akashic"}, headers=_auth(app, user))
    assert r.status_code == 403                                   # не е отключен
    assert client.post("/api/reviews", json=GOOD, headers=_auth(app, user)).status_code == 200


@pytest.mark.parametrize("patch,needle", [
    ({"rating": 0}, "1 до 5"), ({"rating": 6}, "1 до 5"), ({"body": "кратко"}, "20 знака"),
    ({"body": "Вижте https://spam.example и купете сега нещо"}, "линкове"), ({"feature": "xx"}, "Избери"),
])
def test_review_validation(client, app, user, patch, needle):
    r = client.post("/api/reviews", json={**GOOD, **patch}, headers=_auth(app, user))
    assert r.status_code == 400 and needle in r.json()["detail"]


def test_review_waits_for_approval_then_shows_with_product_stars(client, app, user):
    client.post("/api/reviews", json=GOOD, headers=_auth(app, user))
    page = client.get("/natalna-karta").text
    assert "Много точно описа" not in page                       # още чака
    ld = json.loads(re.search(r'<script type="application/ld\+json">(\{"@context".*?)</script>', page).group(1))
    assert ld["@type"] == "Product" and "aggregateRating" not in ld and ld["offers"]["price"] == "0.00"

    pending = _adm(app).get("/api/admin/reviews?status=pending").json()["reviews"]
    rid = pending[0]["id"]
    assert _adm(app).patch(f"/api/admin/reviews/{rid}",
                           json={"status": "approved", "reply": "Благодарим!"}).status_code == 200

    page = client.get("/natalna-karta").text
    assert "Много точно описа" in page and "Благодарим!" in page and "Мария И." in page
    ld = json.loads(re.search(r'<script type="application/ld\+json">(\{"@context".*?)</script>', page).group(1))
    assert ld["aggregateRating"] == {"@type": "AggregateRating", "ratingValue": 5.0, "reviewCount": 1,
                                     "bestRating": 5, "worstRating": 1}
    assert ld["review"][0]["author"]["name"] == "Мария И." and ld["review"][0]["reviewRating"]["ratingValue"] == 5
    # И в общия списък, и на началната.
    assert "Много точно описа" in client.get("/otzivi").text
    assert "Какво казват хората" in client.get("/").text


def test_editing_a_review_sends_it_back_for_approval(client, app, user):
    client.post("/api/reviews", json=GOOD, headers=_auth(app, user))
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE reviews SET status = 'approved'")
    client.post("/api/reviews", json={**GOOD, "rating": 2, "body": "Промених си мнението след седмица."},
                headers=_auth(app, user))
    with sqlite3.connect(app.DB_PATH) as c:
        rows = c.execute("SELECT rating, status FROM reviews").fetchall()
    assert rows == [(2, "pending")]                              # един отзив на модул


def test_paid_module_review_is_marked_verified(client, app, user):
    app.grant_feature_purchase(user["id"], "profile", 499, "EUR", None)
    client.post("/api/reviews", json={**GOOD, "feature": "profile"}, headers=_auth(app, user))
    client.post("/api/reviews", json=GOOD, headers=_auth(app, user))
    with sqlite3.connect(app.DB_PATH) as c:
        assert dict(c.execute("SELECT feature_key, verified FROM reviews").fetchall()) == {"profile": 1, "chart": 0}


def test_review_xss_is_escaped(client, app, user):
    client.post("/api/reviews", json={**GOOD, "body": "<script>alert(1)</script> много хубаво разчитане"},
                headers=_auth(app, user))
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE reviews SET status = 'approved'")
    page = client.get("/otzivi").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_deleting_the_account_removes_reviews(client, app, user):
    client.post("/api/reviews", json=GOOD, headers=_auth(app, user))
    _adm(app).request("DELETE", f"/api/admin/users/{user['id']}")
    with sqlite3.connect(app.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


def test_my_reviews_lists_only_unlocked_modules(client, app, user):
    d = client.get("/api/reviews/mine", headers=_auth(app, user)).json()
    keys = {f["key"] for f in d["features"]}
    assert "chart" in keys and "akashic" not in keys


# --- контакти ----------------------------------------------------------------------

def test_contact_form_stores_and_notifies(client, app, monkeypatch):
    sent = []
    monkeypatch.setattr(app, "notify_owner", lambda kind, subj, body: sent.append((kind, subj, body)) or True)
    r = client.post("/api/contact", json={"name": "Иван", "email": "ivan@example.com", "topic": "payment",
                                          "message": "Не виждам фактурата за поръчката си."})
    assert r.status_code == 200 and r.json()["event_id"].startswith("contact-")
    assert sent and sent[0][0] == "messages" and "ivan@example.com" in sent[0][2]
    msgs = _adm(app).get("/api/admin/messages?status=new").json()["messages"]
    assert msgs[0]["topic_name"] == "Плащане или фактура"


def test_contact_honeypot_and_validation(client, app):
    r = client.post("/api/contact", json={"email": "bot@example.com", "message": "купи сега евтино", "website": "x"})
    assert r.status_code == 200
    with sqlite3.connect(app.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM contact_messages").fetchone()[0] == 0
    assert client.post("/api/contact", json={"email": "не-е-имейл", "message": "здравейте, въпрос"}).status_code == 400
    assert client.post("/api/contact", json={"email": "a@example.com", "message": "хм"}).status_code == 400


def test_admin_reply_sends_mail_and_marks_answered(client, app, monkeypatch):
    mails = []
    monkeypatch.setattr(app, "send_email", lambda to, subj, body, **kw: mails.append((to, subj, body, kw)))
    client.post("/api/contact", json={"email": "ivan@example.com", "message": "Как да сменя часа на раждане?"})
    mid = _adm(app).get("/api/admin/messages").json()["messages"][0]["id"]
    r = _adm(app).post(f"/api/admin/messages/{mid}/reply", json={"text": "От Настройки → Карти → Редактирай."})
    assert r.status_code == 200
    to, subj, body, kw = mails[0]
    assert to == "ivan@example.com" and "Редактирай" in body and "> Как да сменя" in body
    assert kw.get("campaign") == "contact_reply"
    assert _adm(app).get("/api/admin/messages?status=answered").json()["messages"][0]["id"] == mid


def test_pages_render_and_are_in_the_sitemap(client, app):
    html = client.get("/kontakti").text
    assert 'id="ctForm"' in html and '"ContactPage"' in html
    assert client.get("/otzivi?modul=chart").status_code == 200
    sm = client.get("/sitemap.xml").text
    assert "/otzivi" in sm and "/kontakti" in sm
    assert 'href="/kontakti"' in client.get("/").text
