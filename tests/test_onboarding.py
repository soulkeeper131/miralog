# -*- coding: utf-8 -*-
"""Запис на гост-картата и въвеждането в три стъпки (етап D):
размазан откъс без AI, флаг „видяно“ на сървъра (по акаунт), превключвател
за дневния хороскоп на имейл, контейнер на въвеждането в страницата."""
import asyncio
import secrets
import sqlite3

import pytest
from starlette.requests import Request


def _req(path="/", token=None, query=b""):
    headers = [(b"host", b"astrokarta.bg")]
    if token:
        headers.append((b"cookie", f"miralog_token={token}".encode()))
    return Request({"type": "http", "method": "GET", "path": path, "headers": headers,
                    "query_string": query, "scheme": "https", "server": ("astrokarta.bg", 443),
                    "client": ("1.2.3.4", 1)})


def _guest(app, **over):
    body = dict(name="Мария Петрова", year=1988, month=1, day=8, hour=12, minute=0,
                lat=43.2, lon=27.9, timezone="Europe/Sofia")
    body.update(over)
    return app.api_guest_chart(app.GuestChartRequest(**body), _req())


def _new_user(app):
    email = f"o-{secrets.token_hex(4)}@example.com"
    row = app.create_user(email, app.hash_password("parola123"))
    app.grant_signup_features(row["id"])
    return row["id"], email


# --- откъс за госта -----------------------------------------------------------

def test_guest_chart_has_a_teaser_built_without_ai(app, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("AI не бива да се вика при гост-картата")
    monkeypatch.setattr(app, "call_ai", boom)
    data = _guest(app)
    t = data["teaser"]
    assert t["name"] == "Мария"
    labels = [r["label"] for r in t["rows"]]
    assert labels == ["Слънце", "Луна", "Асцендент"]
    assert all(r["position"] and r["meaning"] for r in t["rows"])
    assert t["element"] and t["element"]["name"]


def test_guest_teaser_drops_the_ascendant_without_birth_time(app):
    t = _guest(app, time_unknown=True)["teaser"]
    assert [r["label"] for r in t["rows"]] == ["Слънце", "Луна"]


def test_saved_person_teaser_uses_the_same_builder(app, user):
    with sqlite3.connect(app.DB_PATH) as c:
        cur = c.execute("INSERT INTO persons (user_id, name, year, month, day, hour, minute,"
                        " lat, lon, timezone) VALUES (?, 'Мария Петрова', 1988, 1, 8, 12, 0,"
                        " 43.2, 27.9, 'Europe/Sofia')", (user["id"],))
        c.commit()
        pid = cur.lastrowid
    saved = app.api_teaser(pid, (user["id"], user["email"]))
    assert saved == _guest(app)["teaser"]


# --- флагът „въвеждането е видяно“ -------------------------------------------

def test_new_account_has_not_seen_onboarding(app):
    uid, email = _new_user(app)
    state = app.api_onboarding_state((uid, email))
    assert state == {"done": False, "digest_opt_in": False}   # изключено по подразбиране


def test_onboarding_flag_is_per_user_and_sticks(app):
    a, a_mail = _new_user(app)
    b, b_mail = _new_user(app)
    assert app.api_onboarding_done((a, a_mail))["done"] is True
    assert app.api_onboarding_state((a, a_mail))["done"] is True
    assert app.api_onboarding_state((b, b_mail))["done"] is False
    with sqlite3.connect(app.DB_PATH) as c:
        first = c.execute("SELECT onboarded_at FROM users WHERE id = ?", (a,)).fetchone()[0]
    app.api_onboarding_done((a, a_mail))            # повторно: датата не се променя
    with sqlite3.connect(app.DB_PATH) as c:
        assert c.execute("SELECT onboarded_at FROM users WHERE id = ?", (a,)).fetchone()[0] == first


def test_migration_marks_existing_accounts_as_onboarded(app, tmp_path, monkeypatch):
    """Стар акаунт (преди колоната) не бива да види въвеждането след деплоя."""
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE,"
                  " password_hash TEXT)")
        c.execute("INSERT INTO users (email, password_hash) VALUES ('old@example.com', 'x')")
        c.commit()
    monkeypatch.setattr(app, "DB_PATH", str(db))
    app.init_db()
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT onboarded_at FROM users WHERE email='old@example.com'").fetchone()[0]


# --- превключвателят за хороскопа на имейл -------------------------------------

def test_digest_switch_toggles_the_account_setting(app):
    uid, email = _new_user(app)
    r = app.api_account_digest(app.DigestUpdate(digest_opt_in=True), (uid, email))
    assert r["digest_opt_in"] is True
    assert app.api_onboarding_state((uid, email))["digest_opt_in"] is True
    app.api_account_digest(app.DigestUpdate(digest_opt_in=False), (uid, email))
    assert app.get_user_by_id(uid)["digest_opt_in"] == 0


# --- страницата -----------------------------------------------------------------

def test_chart_page_carries_the_onboarding_container(app, user):
    with sqlite3.connect(app.DB_PATH) as c:
        cur = c.execute("INSERT INTO persons (user_id, name, year, month, day, hour, minute,"
                        " lat, lon, timezone) VALUES (?, 'Мария', 1990, 5, 14, 8, 30, 42.7, 23.3,"
                        " 'Europe/Sofia')", (user["id"],))
        c.commit()
        pid = cur.lastrowid
    tok = app.create_token(user["id"], user["email"])
    resp = asyncio.run(app.view_chart(_req(f"/chart/{pid}", tok), pid))
    html = resp.body.decode()
    assert 'id="onboarding-root"' in html
    assert "/static/onboarding.js" in html and "maybeStartOnboarding" in html
    # старият прозорец не изскача вече сам
    assert "setTimeout(maybeShowModulePicker" not in html


def test_start_page_has_the_single_save_ask(app):
    html = asyncio.run(app.start_page(_req("/start"))).body.decode()
    assert "Запази картата си — безплатно. Личният ти хороскоп те чака всяка сутрин." in html
    assert 'id="termsAgree"' in html                    # съгласието с условията остава
    assert "чл. 57, т. 13" in html                       # и съгласието за 14-дневния отказ
    assert "signup_prompt_view" in html
    assert "Пълното тълкуване е в платените разчитания" in html
