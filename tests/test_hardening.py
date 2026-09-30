# -*- coding: utf-8 -*-
"""Защити срещу злоупотреба: чужди харчат AI кредити, изтичащи токени,
заливане с регистрации и имейли, издаване на вътрешни подробности.

Всяка защита е проверена и в двете посоки: спира злоупотребата, но не пречи
на нормалния клиент.
"""
import asyncio
import logging
import secrets
import threading

import pytest
from fastapi import HTTPException
from starlette.requests import Request


def _req(headers=None, client=("8.8.8.8", 5000)):
    """Истинска Starlette заявка, каквато вижда приложението."""
    return Request({
        "type": "http", "method": "GET", "path": "/", "root_path": "",
        "scheme": "https", "server": ("astrokarta.bg", 443), "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
    })


def _admin_request(app, db):
    row = db.create_user(f"adm-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    import sqlite3
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE users SET role = 'admin' WHERE id = ?", (row["id"],))
        c.commit()
    return _req({"Authorization": "Bearer " + app.create_token(row["id"], row["email"])})


# --- 1. ?refresh=true не харчи AI кредити за анонимни --------------------------

def _endpoints(app):
    """Петте публични SEO адреса: (извикване, име на функцията за кеша)."""
    sign = next(iter(app.ZODIAC_BY_SLUG))
    planet = next(iter(app.PLANETS_BY_SLUG))
    pair = next(iter(app.COMPAT_BY_SLUG))
    house = next(iter(app.HOUSES_BY_NUM))
    return [
        (lambda r: app.api_horoskop(sign, request=r, refresh=True), "get_sign_horoscope"),
        (lambda r: app.api_planet_sign(planet, sign, request=r, refresh=True), "get_planet_sign"),
        (lambda r: app.api_sign_profile(sign, request=r, refresh=True), "get_sign_profile"),
        (lambda r: app.api_compatibility(pair, request=r, refresh=True), "get_compatibility"),
        (lambda r: app.api_planet_house(planet, house, request=r, refresh=True), "get_planet_house"),
    ]


def _count_ai_jobs(app, monkeypatch):
    started = []

    def fake_job(key, fn):
        started.append(key)
        done = threading.Event()
        done.set()
        return {"done": done, "error": None}

    monkeypatch.setattr(app, "ai_job", fake_job)
    return started


@pytest.mark.parametrize("idx", range(5))
def test_anonymous_refresh_serves_the_cache(app, monkeypatch, idx):
    """Иначе всеки може да вика адреса в цикъл и да харчи парите ни."""
    call, getter = _endpoints(app)[idx]
    monkeypatch.setattr(app, getter, lambda *a, **k: "Кеширан текст.")
    started = _count_ai_jobs(app, monkeypatch)
    call(_req())
    assert started == [], "анонимен ?refresh=true пусна ново AI генериране"


@pytest.mark.parametrize("idx", range(5))
def test_admin_can_still_refresh(app, db, monkeypatch, idx):
    """Админът трябва да може да прегенерира лош текст."""
    call, getter = _endpoints(app)[idx]
    monkeypatch.setattr(app, getter, lambda *a, **k: "Кеширан текст.")
    started = _count_ai_jobs(app, monkeypatch)
    call(_admin_request(app, db))
    assert len(started) == 1


@pytest.mark.parametrize("idx", range(5))
def test_missing_text_is_still_generated(app, monkeypatch, idx):
    """Нормалният път не бива да се счупи: липсващ текст се генерира."""
    call, getter = _endpoints(app)[idx]
    monkeypatch.setattr(app, getter, lambda *a, **k: None)
    started = _count_ai_jobs(app, monkeypatch)
    call(_req())
    assert len(started) == 1


def test_a_forged_token_is_not_admin(app):
    forged = _req({"Authorization": "Bearer not-a-real-token"})
    assert app.is_admin_request(forged) is False


def test_a_regular_user_is_not_admin(app, user):
    r = _req({"Authorization": "Bearer " + app.create_token(user["id"], user["email"])})
    assert app.is_admin_request(r) is False


# --- 2. Токените не остават в логовете ---------------------------------------

def _access_record(path):
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                             '%s - "%s %s HTTP/%s" %d',
                             ("1.2.3.4:5000", "GET", path, "1.1", 200), None)


def test_token_is_hidden_in_the_access_log(app):
    rec = _access_record("/chart/5?token=eyJhbGciOi.secret.part&paid=1")
    for f in logging.getLogger("uvicorn.access").filters:
        f.filter(rec)
    line = rec.getMessage()
    assert "eyJhbGciOi" not in line, "токенът е записан в лога"
    assert "/chart/5?token=***&paid=1" in line, "останалата част от адреса е изгубена"


def test_reset_link_token_is_hidden_too(app):
    rec = _access_record("/reset-password?token=abc123")
    for f in logging.getLogger("uvicorn.access").filters:
        f.filter(rec)
    assert "abc123" not in rec.getMessage()


def test_addresses_without_a_token_are_untouched(app):
    rec = _access_record("/horoskop/oven?utm_source=google")
    for f in logging.getLogger("uvicorn.access").filters:
        f.filter(rec)
    assert "/horoskop/oven?utm_source=google" in rec.getMessage()


# --- 3. Истинският IP зад проксито ------------------------------------------

def test_real_ip_is_taken_from_the_proxy(app):
    """Coolify праща всичко през прокси — без това всички са един IP."""
    r = _req({"X-Forwarded-For": "5.6.7.8"}, client=("10.0.1.7", 5000))
    assert app.client_ip(r) == "5.6.7.8"


def test_spoofed_forwarded_header_is_ignored(app):
    """Проксито добавя истинския IP най-отдясно; лявото го пише клиентът."""
    r = _req({"X-Forwarded-For": "1.1.1.1, 5.6.7.8"}, client=("10.0.1.7", 5000))
    assert app.client_ip(r) == "5.6.7.8"


def test_direct_visitor_cannot_fake_an_ip(app):
    r = _req({"X-Forwarded-For": "1.1.1.1"}, client=("8.8.8.8", 5000))
    assert app.client_ip(r) == "8.8.8.8"


def test_unknown_ip_behind_proxy_is_not_guessed(app):
    """Без заглавката не знаем кой е — по-добре без ограничение, отколкото
    всички посетители да делят един брояч и да се спрат взаимно."""
    r = _req({}, client=("10.0.1.7", 5000))
    assert app.client_ip(r) == ""


# --- 4. Ограничения на опитите ------------------------------------------------

def test_forgot_password_mails_one_address_at_most_three_times(app, user, monkeypatch):
    sent = []
    monkeypatch.setattr(app, "smtp_setting", lambda k: "smtp.example.com" if k == "smtp_host" else None)
    monkeypatch.setattr(app, "try_send_template", lambda *a, **k: sent.append(a))
    for i in range(6):
        r = _req({}, client=(f"8.8.8.{i}", 5000))        # и от различни IP-та
        assert app.api_forgot_password(app.ForgotPasswordRequest(email=user["email"]), r) == {"ok": True}
    assert len(sent) == app.RATE_LIMITS["reset_email"][0]


def test_forgot_password_answer_never_changes(app, monkeypatch):
    """Ограничението не бива да издава дали имейлът съществува."""
    monkeypatch.setattr(app, "try_send_template", lambda *a, **k: None)
    for _ in range(15):
        assert app.api_forgot_password(
            app.ForgotPasswordRequest(email="nyama-takav@example.com"), _req()) == {"ok": True}


def _onboard(app, email, request):
    data = app.OnboardRequest(
        email=email, password="parola123", wanted=[],
        name="Тест", year=1990, month=5, day=14, hour=8, minute=30,
        lat=42.7, lon=23.3, timezone="Europe/Sofia")
    return app.api_onboard(data, request)


def test_mass_signup_from_one_ip_is_stopped(app, db):
    limit = app.RATE_LIMITS["signup"][0]
    r = _req({}, client=("9.9.9.9", 5000))
    for _ in range(limit):
        _onboard(app, f"s-{secrets.token_hex(4)}@example.com", r)
    with pytest.raises(HTTPException) as err:
        _onboard(app, f"s-{secrets.token_hex(4)}@example.com", r)
    assert err.value.status_code == 429


def test_signup_limit_is_per_visitor(app, db):
    """Един, който злоупотребява, не бива да спре останалите."""
    limit = app.RATE_LIMITS["signup"][0]
    spammer = _req({}, client=("9.9.9.9", 5000))
    for _ in range(limit):
        _onboard(app, f"s-{secrets.token_hex(4)}@example.com", spammer)
    assert _onboard(app, f"ok-{secrets.token_hex(4)}@example.com",
                    _req({}, client=("7.7.7.7", 5000)))["ok"] is True


def test_register_shares_the_signup_limit(app, db):
    limit = app.RATE_LIMITS["signup"][0]
    r = _req({}, client=("9.9.9.9", 5000))
    for _ in range(limit):
        _onboard(app, f"s-{secrets.token_hex(4)}@example.com", r)
    with pytest.raises(HTTPException) as err:
        app.api_register(app.AuthRequest(email=f"r-{secrets.token_hex(4)}@example.com",
                                         password="parola123"), r)
    assert err.value.status_code == 429


def test_guest_chart_is_limited(app):
    limit = app.RATE_LIMITS["guest_chart"][0]
    r = _req({}, client=("9.9.9.9", 5000))
    for _ in range(limit):
        assert app.rate_allowed("guest_chart", app.client_ip(r))
    data = app.GuestChartRequest(name="Гост", year=1990, month=5, day=14,
                                 lat=42.7, lon=23.3, timezone="Europe/Sofia")
    with pytest.raises(HTTPException) as err:
        app.api_guest_chart(data, r)
    assert err.value.status_code == 429


# --- 5. Дребните -------------------------------------------------------------

def test_api_docs_are_off_in_production(app):
    assert app.api_docs_settings(production=True) == {
        "docs_url": None, "redoc_url": None, "openapi_url": None}
    assert app.api_docs_settings(production=False) == {}


def test_webhook_error_does_not_leak_internals(app, monkeypatch):
    def boom(payload, sig):
        raise ValueError("вътрешен детайл на библиотеката")
    monkeypatch.setattr(app.billing, "construct_webhook_event", boom)

    class _R:
        headers = {"stripe-signature": "x"}
        async def body(self):
            return b"{}"

    with pytest.raises(HTTPException) as err:
        asyncio.run(app.api_stripe_webhook(_R()))
    assert err.value.status_code == 400
    assert "вътрешен" not in str(err.value.detail)


def test_short_new_password_is_refused(app, db):
    with pytest.raises(HTTPException) as err:
        app.api_register(app.AuthRequest(email=f"p-{secrets.token_hex(4)}@example.com",
                                         password="1234567"), _req())
    assert err.value.status_code == 400


def test_old_short_password_still_logs_in(app, db):
    """Вече регистрираните с 6 знака не бива да бъдат заключени отвън."""
    email = f"old-{secrets.token_hex(4)}@example.com"
    db.create_user(email, db.hash_password("123456"))
    out = app.api_login(app.AuthRequest(email=email, password="123456"), _req())
    assert out["token"]


def test_package_versions_are_reported(app):
    versions = app.package_versions()
    assert versions.get("fastapi")
    assert versions.get("stripe")
