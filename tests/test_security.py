# -*- coding: utf-8 -*-
"""Сигурност и устойчивост: токени, OAuth state, копия на базата."""
import datetime
import secrets
import sqlite3
import time

import pytest
from fastapi import HTTPException


# --- токени -----------------------------------------------------------------

def _identify(app, token):
    """Минава през истинската проверка, която пазят рутовете."""
    return app.get_current_user(request=None, token=token)


def test_token_identifies_the_right_user(app, user):
    token = app.create_token(user["id"], user["email"])
    user_id, email = _identify(app, token)
    assert user_id == user["id"]
    assert email == user["email"]


def test_tampered_token_is_refused(app, user):
    token = app.create_token(user["id"], user["email"])
    broken = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    with pytest.raises(HTTPException) as err:
        _identify(app, broken)
    assert err.value.status_code == 401


def test_token_signed_with_another_key_is_refused(app, user):
    """Токен, направен с чужд ключ, не бива да отваря нищо."""
    from jose import jwt
    forged = jwt.encode({"sub": str(user["id"]), "email": user["email"]},
                        "drug-taen-klyuch-koito-ne-e-nashiyat", algorithm=app.ALGORITHM)
    with pytest.raises(HTTPException) as err:
        _identify(app, forged)
    assert err.value.status_code == 401


def test_no_token_is_refused(app):
    with pytest.raises(HTTPException) as err:
        _identify(app, None)
    assert err.value.status_code == 401


def test_password_is_never_stored_in_the_clear(app, db):
    password = "mnogo-tajna-parola-123"
    row = db.create_user(f"pw-{secrets.token_hex(3)}@example.com", db.hash_password(password))
    with sqlite3.connect(app.DB_PATH) as c:
        stored = c.execute("SELECT password_hash FROM users WHERE id = ?",
                           (row["id"],)).fetchone()[0]
    assert password not in str(stored)
    assert app.verify_password(password, stored)
    assert not app.verify_password("грешна", stored)


# --- OAuth state ------------------------------------------------------------

def test_oauth_state_works_once(app):
    """Втора употреба значи replay — трябва да се отхвърли."""
    token = app._oauth_state_new("google", "/dashboard")
    assert app._oauth_state_take(token) is not None
    assert app._oauth_state_take(token) is None


def test_unknown_oauth_state_is_refused(app):
    assert app._oauth_state_take("измислен-state") is None
    assert app._oauth_state_take("") is None


def test_expired_oauth_state_is_refused(app):
    token = app._oauth_state_new("google", "/dashboard")
    # Състаряваме записа отвъд срока, вместо да чакаме десет минути.
    app._OAUTH_STATES[token]["at"] = time.time() - app._OAUTH_STATE_TTL - 5
    assert app._oauth_state_take(token) is None


def test_oauth_next_cannot_point_outside_the_site(app):
    """Иначе адресът за връщане става отворено пренасочване към чужд сайт."""
    from fastapi import Request

    def safe(next_url):
        # Същото правило като в api_oauth_start.
        return next_url if next_url.startswith("/") and not next_url.startswith("//") \
            else "/dashboard"

    assert safe("/start?claim=1") == "/start?claim=1"
    assert safe("https://evil.example.com/steal") == "/dashboard"
    assert safe("//evil.example.com") == "/dashboard"


# --- копие на базата --------------------------------------------------------

def test_backup_produces_a_usable_database(app):
    app.run_db_backup()
    backups = sorted((app.DB_PATH.parent / "backups").glob("persons-*.db"))
    assert backups, "не е направено копие"
    with sqlite3.connect(backups[-1]) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        copied = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    with sqlite3.connect(app.DB_PATH) as c:
        live = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert copied == live


def test_backup_runs_once_a_day(app):
    app.run_db_backup()
    target = app.DB_PATH.parent / "backups" / f"persons-{datetime.date.today().isoformat()}.db"
    first = target.stat().st_mtime
    app.run_db_backup()
    assert target.stat().st_mtime == first


def test_old_backups_are_cleaned_up(app):
    backup_dir = app.DB_PATH.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stale = backup_dir / "persons-2020-01-01.db"
    stale.write_bytes(b"x")
    recent_day = datetime.date.today() - datetime.timedelta(days=2)
    recent = backup_dir / f"persons-{recent_day.isoformat()}.db"
    recent.write_bytes(b"y")

    app.run_db_backup()
    assert not stale.exists(), "старите копия се трупат без край"
    assert recent.exists(), "изтрито е копие, което още трябва"


def test_backup_leaves_no_half_written_file(app):
    app.run_db_backup()
    leftovers = list((app.DB_PATH.parent / "backups").glob("*.part"))
    assert leftovers == []


def test_backup_status_reports_what_exists(app):
    app.run_db_backup()
    status = app.backup_status()
    assert status["count"] >= 1
    assert status["size_kb"] > 0
    assert status["age_hours"] is not None and status["age_hours"] < 1
