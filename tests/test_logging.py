# -*- coding: utf-8 -*-
"""Логове, с които може да се помогне на клиент.

Клиентът вижда кратък код при грешка; по него намираме точно неговата заявка
и всичко, което е станало в нея — включително във фоновото AI генериране.
"""
import asyncio
import json
import secrets
import threading
import urllib.parse

import pytest
from fastapi import HTTPException


class _Response:
    def __init__(self, status, headers, body):
        self.status_code = status
        self.headers = {k.decode().lower(): v.decode() for k, v in headers}
        self.text = body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)


class _Client:
    """Вика ASGI приложението директно, както го вика uvicorn — без httpx."""

    def __init__(self, asgi):
        self.asgi = asgi

    def get(self, url, headers=None):
        parts = urllib.parse.urlsplit(url)
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "https", "path": parts.path,
            "raw_path": parts.path.encode(), "root_path": "",
            "query_string": parts.query.encode(),
            "headers": [(b"host", b"astrokarta.bg")] + [
                (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "client": ("8.8.8.8", 5000), "server": ("astrokarta.bg", 443),
        }
        sent = {"status": 0, "headers": [], "body": b""}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                sent["status"] = message["status"]
                sent["headers"] = message.get("headers", [])
            elif message["type"] == "http.response.body":
                sent["body"] += message.get("body", b"")

        asyncio.run(self.asgi(scope, receive, send))
        return _Response(sent["status"], sent["headers"], sent["body"])


def _log_text(app):
    for h in app.log.handlers:
        h.flush()
    return "".join(p.read_text(encoding="utf-8") for p in sorted(app.LOG_DIR.glob("app.log*")))


@pytest.fixture(scope="module")
def client():
    import app as app_module

    # Тестови адреси, които гърмят нарочно.
    if not any(getattr(r, "path", "") == "/__test/boom" for r in app_module.app.routes):
        @app_module.app.get("/__test/boom")
        def _boom():
            raise RuntimeError("нарочен срив за теста")

        @app_module.app.get("/api/__test/boom")
        def _api_boom():
            raise RuntimeError("нарочен срив в API")

        @app_module.app.get("/api/__test/bad-gateway")
        def _bad_gateway():
            raise HTTPException(502, "Плащането не можа да се провери.")

        @app_module.app.get("/api/__test/payment-required")
        def _payment_required():
            raise HTTPException(402, {"offer": {"price_cents": 499}})

    return _Client(app_module.app)


# --- код на заявката ---------------------------------------------------------

def test_every_response_carries_a_request_code(client):
    r = client.get("/horoskop")
    code = r.headers.get("x-request-id")
    assert code and len(code) == 6


def test_request_line_is_logged_with_the_code(app, client):
    r = client.get("/horoskop")
    code = r.headers["x-request-id"]
    line = [l for l in _log_text(app).splitlines() if f"[{code}]" in l]
    assert line, "заявката не е в лога"
    assert "GET /horoskop" in line[0] and "200" in line[0]


def test_healthchecks_do_not_flood_the_log(app, client):
    """Coolify проверява /healthz постоянно — 987 от 1000 реда бяха това."""
    code = client.get("/healthz").headers["x-request-id"]
    assert f"[{code}]" not in _log_text(app)


def test_signed_in_user_is_named_in_the_log(app, client, user):
    token = app.create_token(user["id"], user["email"])
    code = client.get("/api/auth/me", headers={"Authorization": "Bearer " + token}).headers["x-request-id"]
    line = [l for l in _log_text(app).splitlines() if f"[{code}]" in l][0]
    assert f"user={user['id']}" in line


def test_token_never_reaches_the_log(app, client):
    code = client.get("/reset-password?token=SUPERSECRET123").headers["x-request-id"]
    text = _log_text(app)
    assert f"[{code}]" in text
    assert "SUPERSECRET123" not in text


# --- сривове -----------------------------------------------------------------

def test_crash_on_a_page_shows_the_code(app, client):
    r = client.get("/__test/boom")
    code = r.headers["x-request-id"]
    assert r.status_code == 500
    assert code in r.text, "клиентът не вижда кода, който да ни прати"
    assert "RuntimeError" not in r.text, "вътрешната грешка изтича към клиента"


def test_crash_in_the_api_returns_the_code_as_json(app, client):
    r = client.get("/api/__test/boom")
    code = r.headers["x-request-id"]
    assert r.status_code == 500
    assert code in r.json()["detail"]


def test_crash_is_logged_with_traceback_under_the_code(app, client):
    code = client.get("/api/__test/boom").headers["x-request-id"]
    text = _log_text(app)
    block = text[text.index(f"[{code}]"):]
    assert "нарочен срив в API" in block
    assert "Traceback" in block


def test_server_side_errors_carry_the_code(app, client):
    r = client.get("/api/__test/bad-gateway")
    assert r.status_code == 502
    assert r.headers["x-request-id"] in r.json()["detail"]
    assert "Плащането не можа да се провери." in r.json()["detail"]


def test_client_errors_are_left_as_they_were(app, client):
    """402 носи офертата като обект — страницата за покупка разчита на нея."""
    r = client.get("/api/__test/payment-required")
    assert r.status_code == 402
    assert r.json()["detail"] == {"offer": {"price_cents": 499}}


# --- фоновата работа ---------------------------------------------------------

def test_background_ai_failure_is_logged_under_the_request(app):
    """Генерирането върви в отделна нишка — без това грешката изчезваше."""
    marker = "ai-" + secrets.token_hex(4)
    token = app.REQUEST_ID.set("ABC123")
    try:
        job = app.ai_job(marker, lambda: (_ for _ in ()).throw(RuntimeError("AI е долу")))
    finally:
        app.REQUEST_ID.reset(token)
    job["done"].wait(5)
    lines = [l for l in _log_text(app).splitlines() if marker in l]
    assert lines and "[ABC123]" in lines[0]
    assert job["error"] == "AI е долу"


def test_ai_call_is_timed_and_failures_logged(app, monkeypatch):
    def fail(*a, **k):
        raise app.AIError("deepseek върна 429")
    monkeypatch.setattr(app, "_call_ai_unlogged", fail)
    with pytest.raises(app.AIError):
        app.call_ai("key", "deepseek", "подкана")
    assert "deepseek върна 429" in _log_text(app)


def test_prompt_is_not_logged(app, monkeypatch):
    """Подканата съдържа рождените данни на клиента."""
    monkeypatch.setattr(app, "_call_ai_unlogged", lambda *a, **k: "отговор")
    app.call_ai("key", "deepseek", "РОДЕН-1990-05-14-СОФИЯ")
    assert "РОДЕН-1990-05-14-СОФИЯ" not in _log_text(app)


def test_audit_events_also_reach_the_log(app, user):
    marker = "събитие-" + secrets.token_hex(4)
    app.audit("login", marker, user_id=user["id"])
    assert marker in _log_text(app)


# --- четене от админа ---------------------------------------------------------

def test_admin_can_find_a_request_by_its_code(app, client):
    code = client.get("/api/__test/boom").headers["x-request-id"]
    found = app.api_admin_logs(q=code, level=None, limit=100, admin={"id": 1})
    assert found["lines"], "кодът от клиента не се намира"
    assert all(code in l for l in found["lines"] if l.startswith("20"))


def test_admin_can_filter_problems_only(app, client):
    client.get("/horoskop")
    client.get("/api/__test/boom")
    found = app.api_admin_logs(q=None, level="warning", limit=500, admin={"id": 1})
    heads = [l for l in found["lines"] if l[:4].isdigit()]
    assert heads and all((" WARNING " in l or " ERROR " in l) for l in heads)


def test_log_limit_is_capped(app):
    found = app.api_admin_logs(q=None, level=None, limit=10 ** 9, admin={"id": 1})
    assert len(found["lines"]) <= app.ADMIN_LOG_MAX_LINES
