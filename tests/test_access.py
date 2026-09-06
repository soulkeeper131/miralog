# -*- coding: utf-8 -*-
"""Достъп: кой какво вижда, и по-важното — какво НЕ вижда.

Тестовете тук пазят чужди данни и платено съдържание.
"""
import secrets
import sqlite3

import pytest
from fastapi import HTTPException


def _person(app, user_id, name="Тест"):
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute(
            "INSERT INTO persons (user_id, name, year, month, day, hour, minute,"
            " lat, lon, timezone) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, 1990, 5, 14, 8, 30, 42.7, 23.3, "Europe/Sofia"))
        c.commit()
        return c.execute("SELECT id FROM persons WHERE user_id = ? ORDER BY id DESC",
                         (user_id,)).fetchone()[0]


def _check(app, feature, user_row):
    """Пуска проверката за достъп както при истинска заявка."""
    dep = app.require_feature(feature)
    return dep(user=(user_row["id"], user_row["email"]))


# --- чужди карти ------------------------------------------------------------

def test_a_persons_chart_is_invisible_to_others(app, db):
    a = db.create_user(f"a-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    b = db.create_user(f"b-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    pid = _person(app, a["id"])
    assert app.get_person(pid, a["id"]) is not None
    assert app.get_person(pid, b["id"]) is None, "чужда карта е достъпна"


def test_missing_person_returns_nothing(app, user):
    assert app.get_person(999999, user["id"]) is None


# --- заключени функции ------------------------------------------------------

def test_locked_feature_raises_402_with_a_price(app, user):
    with pytest.raises(HTTPException) as err:
        _check(app, "profile", user)
    assert err.value.status_code == 402
    detail = err.value.detail
    assert detail["offer"]["price_cents"] > 0
    assert detail["feature_name"]


def test_locked_feature_also_offers_the_bundle(app, user):
    """Пакетът пътува с отказа — иначе човекът научава за него твърде късно."""
    with pytest.raises(HTTPException) as err:
        _check(app, "profile", user)
    bundle = err.value.detail.get("bundle")
    assert bundle and "profile" in bundle["keys"]


def test_free_feature_passes_without_payment(app, user):
    assert _check(app, "chart", user) is not None
    assert _check(app, "horoscope", user) is not None


def test_paid_feature_passes_after_payment(app, user):
    offer = app.feature_offer("profile")
    app.grant_feature_purchase(user["id"], "profile", offer["price_cents"], "EUR", None)
    assert _check(app, "profile", app.get_user_by_id(user["id"])) is not None


def test_blocked_account_is_refused_everywhere(app, user):
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE users SET is_blocked = 1 WHERE id = ?", (user["id"],))
        c.commit()
    blocked = app.get_user_by_id(user["id"])
    with pytest.raises(HTTPException) as err:
        _check(app, "chart", blocked)          # дори безплатното
    assert err.value.status_code == 403


def test_admin_reaches_everything(app, db):
    admin = db.create_user(f"adm-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE users SET role = 'admin' WHERE id = ?", (admin["id"],))
        c.commit()
    row = app.get_user_by_id(admin["id"])
    for f in app.FEATURE_CATALOGUE:
        assert _check(app, f["key"], row) is not None


# --- вход през доставчик ----------------------------------------------------

def test_same_google_identity_reuses_the_account(app, db):
    """Втори вход не бива да прави втори акаунт."""
    email = f"g-{secrets.token_hex(4)}@example.com"
    sub = "sub-" + secrets.token_hex(6)
    first = app._oauth_link_or_create("google", sub, email, "Тест")
    second = app._oauth_link_or_create("google", sub, email, "Тест")
    assert first["id"] == second["id"]


def test_google_links_to_an_existing_email_account(app, db):
    """Който има парола и после влезе с Google, остава един и същ човек."""
    email = f"m-{secrets.token_hex(4)}@example.com"
    existing = db.create_user(email, db.hash_password("parola123"))
    linked = app._oauth_link_or_create("google", "sub-" + secrets.token_hex(6), email, "Тест")
    assert linked["id"] == existing["id"], "направен е втори акаунт за същия имейл"


def test_provider_without_email_is_refused(app, db):
    """Facebook може да скрие имейла. Акаунт без имейл е недостижим."""
    with pytest.raises(HTTPException) as err:
        app._oauth_link_or_create("facebook", "fb-" + secrets.token_hex(6), "", "Без Имейл")
    assert err.value.status_code == 400


def test_purchases_survive_linking_a_provider(app, db):
    """Купил е с имейл и парола, после влиза с Google — модулите остават."""
    email = f"p-{secrets.token_hex(4)}@example.com"
    row = db.create_user(email, db.hash_password("parola123"))
    app.grant_feature_purchase(row["id"], "profile", 499, "EUR", None)
    linked = app._oauth_link_or_create("google", "sub-" + secrets.token_hex(6), email, "")
    assert "profile" in app.unlocked_features(app.get_user_by_id(linked["id"]))


# --- изключване на доставчик от админа --------------------------------------

def test_disabling_a_provider_hides_it_but_keeps_the_keys(app):
    app.set_setting("oauth_google_client_id", "id.apps.googleusercontent.com")
    app.set_setting("oauth_google_client_secret", "sec")
    app.set_setting("oauth_google_enabled", "1")
    assert app.oauth_providers()["google"] is True

    app.set_setting("oauth_google_enabled", "0")
    assert app.oauth_providers()["google"] is False
    assert app.oauth_config()["google_client_id"], "ключът е изтрит при изключване"

    app.set_setting("oauth_google_enabled", "1")
    assert app.oauth_providers()["google"] is True
    for key in ("oauth_google_client_id", "oauth_google_client_secret",
                "oauth_google_enabled"):
        app.set_setting(key, "")


def test_missing_flag_means_enabled(app):
    """След обновяване няма стойност — вече работещият вход не бива да гасне."""
    app.set_setting("oauth_google_client_id", "id.apps.googleusercontent.com")
    app.set_setting("oauth_google_client_secret", "sec")
    app.set_setting("oauth_google_enabled", "")
    assert app.oauth_providers()["google"] is True
    for key in ("oauth_google_client_id", "oauth_google_client_secret"):
        app.set_setting(key, "")
