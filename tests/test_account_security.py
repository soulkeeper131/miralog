# -*- coding: utf-8 -*-
"""Сигурност на акаунтите: кой е админ, блокиране, отменени токени, вход,
2FA, вход през Google/Facebook, смяна на имейл.

Всяка защита е проверена и в двете посоки: спира злоупотребата, но не пречи
на нормалния потребител — вкл. на вече вписаните с токени отпреди промяната.
"""
import re
import secrets
import sqlite3

import pyotp
import pytest
from fastapi import HTTPException
from jose import jwt
from starlette.requests import Request


def _req(client=("8.8.8.8", 5000), headers=None, path="/"):
    return Request({
        "type": "http", "method": "POST", "path": path, "root_path": "",
        "scheme": "https", "server": ("astrokarta.bg", 443), "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
    })


def _new_user(db, password="parola-123", email=None):
    email = email or f"u-{secrets.token_hex(4)}@example.com"
    row = db.create_user(email, db.hash_password(password))
    db.grant_signup_features(row["id"])
    return db.get_user_by_id(row["id"])


def _login(app, email, password, client=("8.8.8.8", 5000), totp_code=None):
    return app.api_login(app.AuthRequest(email=email, password=password, totp_code=totp_code),
                         _req(client=client))


def _identify(app, token):
    return app.get_current_user(request=None, token=token)


@pytest.fixture(autouse=True)
def _clean_login_state(app):
    app._LOGIN_FAILURES.clear()
    app._TOTP_CHALLENGES.clear()
    yield
    app._LOGIN_FAILURES.clear()
    app._TOTP_CHALLENGES.clear()


# --- 1. кой става админ ------------------------------------------------------

def test_taking_admin_email_does_not_make_you_admin(app, db, monkeypatch):
    """Досега всеки старт даваше админ права на онзи, който държи ADMIN_EMAIL,
    а имейлът се сменя без потвърждение."""
    attacker = _new_user(db)
    monkeypatch.setattr(app, "ADMIN_EMAIL", attacker["email"])
    app.init_db()
    assert app.get_user_by_id(attacker["id"])["role"] == "user"


def test_first_admin_is_still_bootstrapped_when_there_is_none(app, db, monkeypatch):
    """Празна инсталация (няма нито един админ) пак получава своя админ."""
    owner = _new_user(db)
    with sqlite3.connect(app.DB_PATH) as c:
        admins = [r[0] for r in c.execute("SELECT id FROM users WHERE role = 'admin'")]
        c.execute("UPDATE users SET role = 'user' WHERE role = 'admin'")
        c.commit()
    try:
        monkeypatch.setattr(app, "ADMIN_EMAIL", owner["email"].upper())
        app.init_db()
        assert app.get_user_by_id(owner["id"])["role"] == "admin"
    finally:
        with sqlite3.connect(app.DB_PATH) as c:
            c.execute("UPDATE users SET role = 'user' WHERE id = ?", (owner["id"],))
            for admin_id in admins:
                c.execute("UPDATE users SET role = 'admin' WHERE id = ?", (admin_id,))
            c.commit()


def test_existing_admin_keeps_the_role_across_restarts(app, db):
    with sqlite3.connect(app.DB_PATH) as c:
        before = sorted(r[0] for r in c.execute("SELECT id FROM users WHERE role = 'admin'"))
    app.init_db()
    app.init_db()
    with sqlite3.connect(app.DB_PATH) as c:
        after = sorted(r[0] for r in c.execute("SELECT id FROM users WHERE role = 'admin'"))
    assert before == after and before


# --- 2. блокиране ------------------------------------------------------------

def _block(app, user_id):
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE users SET is_blocked = 1 WHERE id = ?", (user_id,))
        c.commit()


def test_blocked_user_cannot_log_in(app, db):
    user = _new_user(db)
    _block(app, user["id"])
    with pytest.raises(HTTPException) as err:
        _login(app, user["email"], "parola-123")
    assert err.value.status_code == 403


def test_blocked_user_token_stops_working_everywhere(app, db):
    user = _new_user(db)
    token = app.create_token(user["id"], user["email"])
    assert _identify(app, token)[0] == user["id"]
    _block(app, user["id"])
    with pytest.raises(HTTPException) as err:
        _identify(app, token)
    assert err.value.status_code == 403
    # и през бисквитката/аудиото
    req = _req(headers={"Cookie": "miralog_token=" + token})
    with pytest.raises(HTTPException):
        app.get_current_user_flex(req)


def test_blocked_admin_loses_the_panel(app, db):
    admin = _new_user(db)
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE users SET role = 'admin' WHERE id = ?", (admin["id"],))
        c.commit()
    token = app.create_token(admin["id"], admin["email"])
    _block(app, admin["id"])
    with pytest.raises(HTTPException) as err:
        app.require_admin(_identify(app, token))
    assert err.value.status_code == 403


def test_admin_block_revokes_tokens_so_unblock_does_not_revive_them(app, db):
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    user = _new_user(db)
    token = app.create_token(user["id"], user["email"])
    app.api_admin_update_user(user["id"], app.AdminUserUpdate(is_blocked=True), admin=admin)
    app.api_admin_update_user(user["id"], app.AdminUserUpdate(is_blocked=False), admin=admin)
    with pytest.raises(HTTPException) as err:
        _identify(app, token)
    assert err.value.status_code == 401
    # но новият вход работи нормално
    assert _login(app, user["email"], "parola-123")["token"]


# --- 3. отменени токени -------------------------------------------------------

def test_tokens_from_before_this_release_still_work(app, db):
    """Токен без версия (издаден преди деплоя) не бива да изхвърля никого."""
    user = _new_user(db)
    legacy = jwt.encode({"sub": str(user["id"]), "email": user["email"]},
                        app.SECRET_KEY, algorithm=app.ALGORITHM)
    assert _identify(app, legacy)[0] == user["id"]


def test_password_change_revokes_old_tokens_but_keeps_this_session(app, db):
    user = _new_user(db)
    old = app.create_token(user["id"], user["email"])
    res = app.api_change_password(
        app.PasswordChange(current_password="parola-123", new_password="nova-parola-456"),
        user=(user["id"], user["email"]))
    with pytest.raises(HTTPException) as err:
        _identify(app, old)
    assert err.value.status_code == 401
    assert _identify(app, res["token"])[0] == user["id"]


def test_password_reset_revokes_old_tokens(app, db):
    user = _new_user(db)
    old = app.create_token(user["id"], user["email"])
    reset = app.create_password_reset(user["id"])
    app.api_reset_password(app.ResetPasswordRequest(token=reset, new_password="nova-parola-456"))
    with pytest.raises(HTTPException):
        _identify(app, old)
    assert _login(app, user["email"], "nova-parola-456")["token"]


def test_admin_setting_a_password_revokes_the_users_sessions(app, db):
    admin = db.get_user_by_email(app.ADMIN_EMAIL)
    user = _new_user(db)
    old = app.create_token(user["id"], user["email"])
    app.api_admin_update_user(user["id"], app.AdminUserUpdate(password="zadadena-ot-admin"),
                              admin=admin)
    with pytest.raises(HTTPException):
        _identify(app, old)


def test_chart_page_refuses_a_revoked_token(app, db):
    user = _new_user(db)
    token = app.create_token(user["id"], user["email"])
    app.bump_token_version(user["id"])
    with pytest.raises(HTTPException):
        app.user_for_token(token)


# --- 4. вход ------------------------------------------------------------------

def test_login_ignores_case_and_spaces_in_email(app, db):
    user = _new_user(db)
    res = _login(app, "  " + user["email"].upper() + " ", "parola-123")
    assert res["user"]["id"] == user["id"]


def test_wrong_password_is_still_refused(app, db):
    user = _new_user(db)
    with pytest.raises(HTTPException) as err:
        _login(app, user["email"], "greshna-parola")
    assert err.value.status_code == 401


def test_lockout_is_per_real_ip_not_shared(app, db):
    """Досега ключът беше адресът на проксито — общ за всички посетители —
    и всеки можеше да заключи чужд акаунт, вкл. на админа."""
    user = _new_user(db)
    attacker = ("8.8.4.4", 4000)
    for _ in range(app._LOGIN_MAX_FAILS):
        with pytest.raises(HTTPException):
            _login(app, user["email"], "greshna", client=attacker)
    with pytest.raises(HTTPException) as err:
        _login(app, user["email"], "parola-123", client=attacker)
    assert err.value.status_code == 429
    # истинският собственик от своя адрес влиза нормално
    assert _login(app, user["email"], "parola-123", client=("1.1.1.1", 5000))["token"]


def test_lockout_behind_the_coolify_proxy_uses_the_forwarded_ip(app, db):
    """Както в продукция: прекият адрес е вътрешният на проксито."""
    user = _new_user(db)
    def via_proxy(real_ip, password):
        req = _req(client=("10.0.1.5", 4000), headers={"X-Forwarded-For": real_ip})
        return app.api_login(app.AuthRequest(email=user["email"], password=password), req)
    for _ in range(app._LOGIN_MAX_FAILS):
        with pytest.raises(HTTPException):
            via_proxy("8.8.4.4", "greshna")
    with pytest.raises(HTTPException) as err:
        via_proxy("8.8.4.4", "parola-123")
    assert err.value.status_code == 429
    assert via_proxy("1.1.1.1", "parola-123")["token"]


def test_one_ip_cannot_spray_many_accounts(app, db):
    sprayer = ("9.9.9.9", 4000)
    for i in range(app._LOGIN_IP_MAX_FAILS):
        with pytest.raises(HTTPException):
            _login(app, f"nobody-{i}@example.com", "x", client=sprayer)
    user = _new_user(db)
    with pytest.raises(HTTPException) as err:
        _login(app, user["email"], "parola-123", client=sprayer)
    assert err.value.status_code == 429


def test_long_password_does_not_crash(app, db):
    """bcrypt 5 хвърля грешка над 72 байта — това беше 500 при вход."""
    user = _new_user(db)
    with pytest.raises(HTTPException) as err:
        _login(app, user["email"], "ж" * 80)
    assert err.value.status_code == 401
    with pytest.raises(HTTPException) as err:
        app.check_new_password("ж" * 80)
    assert err.value.status_code == 400


def test_old_truncated_long_password_hash_still_logs_in(app, db):
    """Хешовете от bcrypt < 5 са по първите 72 байта — входът трябва да ги приема."""
    import bcrypt
    password = "дълга-парола-" + "я" * 40
    old_style = bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()
    email = f"long-{secrets.token_hex(3)}@example.com"
    db.create_user(email, old_style)
    assert _login(app, email, password)["token"]


# --- 5. 2FA -------------------------------------------------------------------

def _with_totp(app, db):
    user = _new_user(db)
    secret = pyotp.random_base32()
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE users SET totp_secret = ? WHERE id = ?", (secret, user["id"]))
        c.commit()
    return user, secret


def test_totp_account_is_asked_for_the_code(app, db):
    user, secret = _with_totp(app, db)
    with pytest.raises(HTTPException) as err:
        _login(app, user["email"], "parola-123")
    assert err.value.status_code == 401
    assert err.value.detail["reason"] == "totp_required"
    res = _login(app, user["email"], "parola-123", totp_code=pyotp.TOTP(secret).now())
    assert res["token"]


def test_totp_code_is_not_revealed_to_someone_without_the_password(app, db):
    user, _ = _with_totp(app, db)
    with pytest.raises(HTTPException) as err:
        _login(app, user["email"], "greshna")
    assert err.value.detail == "Грешен имейл или парола."


def test_wrong_totp_codes_are_counted(app, db):
    """Грешните кодове не се броеха — 6 цифри се налучкваха без край."""
    user, secret = _with_totp(app, db)
    for _ in range(app._LOGIN_MAX_FAILS):
        with pytest.raises(HTTPException) as err:
            _login(app, user["email"], "parola-123", totp_code="000000")
        assert err.value.detail["reason"] == "totp_invalid"
    with pytest.raises(HTTPException) as err:
        _login(app, user["email"], "parola-123", totp_code=pyotp.TOTP(secret).now())
    assert err.value.status_code == 429


def test_disabling_2fa_needs_a_valid_code(app, db):
    admin, secret = _with_totp(app, db)
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE users SET role = 'admin' WHERE id = ?", (admin["id"],))
        c.commit()
    row = app.get_user_by_id(admin["id"])
    with pytest.raises(HTTPException):
        app.api_admin_2fa_disable({"code": "000000"}, admin=row)
    assert app.get_user_by_id(admin["id"])["totp_secret"] == secret
    app.api_admin_2fa_disable({"code": pyotp.TOTP(secret).now()}, admin=row)
    assert not app.get_user_by_id(admin["id"])["totp_secret"]


def test_two_admins_setting_up_2fa_do_not_swap_secrets(app, db):
    a = _new_user(db)
    b = _new_user(db)
    sa = app.api_admin_2fa_setup(admin=a)["secret"]
    sb = app.api_admin_2fa_setup(admin=b)["secret"]
    app.api_admin_2fa_confirm({"code": pyotp.TOTP(sa).now()}, admin=a)
    app.api_admin_2fa_confirm({"code": pyotp.TOTP(sb).now()}, admin=b)
    assert app.get_user_by_id(a["id"])["totp_secret"] == sa
    assert app.get_user_by_id(b["id"])["totp_secret"] == sb


# --- 6. Google / Facebook -----------------------------------------------------

def test_unverified_google_email_does_not_open_an_existing_account(app, db):
    victim = _new_user(db)
    with pytest.raises(app.OAuthRefused) as err:
        app._oauth_link_or_create("google", "sub-" + secrets.token_hex(6),
                                  victim["email"], "", email_verified=False)
    assert err.value.reason == "unverified"


def test_facebook_does_not_auto_link_to_an_existing_account(app, db):
    victim = _new_user(db)
    with pytest.raises(app.OAuthRefused) as err:
        app._oauth_link_or_create("facebook", "fb-" + secrets.token_hex(6),
                                  victim["email"], "")
    assert err.value.reason == "exists"


def test_facebook_still_creates_new_accounts_and_reuses_links(app, db):
    email = f"fb-{secrets.token_hex(4)}@example.com"
    fb_id = "fb-" + secrets.token_hex(6)
    first = app._oauth_link_or_create("facebook", fb_id, email, "Фейсбук")
    second = app._oauth_link_or_create("facebook", fb_id, email, "Фейсбук")
    assert first["id"] == second["id"]


def test_first_google_link_cuts_sessions_of_whoever_made_the_account(app, db):
    """Някой прави акаунт с чужд имейл (без потвърждение), после истинският
    собственик влиза с Google: старият токен на натрапника спира."""
    squatter = _new_user(db)
    squatter_token = app.create_token(squatter["id"], squatter["email"])
    owner = app._oauth_link_or_create("google", "sub-" + secrets.token_hex(6),
                                      squatter["email"], "", email_verified=True)
    assert owner["id"] == squatter["id"]
    with pytest.raises(HTTPException):
        _identify(app, squatter_token)


def _fake_google(app, monkeypatch, profile):
    monkeypatch.setattr(app, "oauth_providers", lambda: {"google": True, "facebook": True})
    monkeypatch.setattr(app, "oauth_config", lambda: {
        "google_client_id": "id", "google_client_secret": "sec",
        "facebook_app_id": "fb", "facebook_app_secret": "fbsec"})
    monkeypatch.setattr(app, "_oauth_post", lambda url, data: {"access_token": "at"})
    monkeypatch.setattr(app, "_oauth_get", lambda url, token: profile)


def test_google_login_does_not_bypass_2fa(app, db, monkeypatch):
    user, secret = _with_totp(app, db)
    sub = "sub-" + secrets.token_hex(6)
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("INSERT INTO oauth_accounts (provider, provider_user_id, user_id, email)"
                  " VALUES ('google', ?, ?, ?)", (sub, user["id"], user["email"]))
        c.commit()
    _fake_google(app, monkeypatch, {"sub": sub, "email": user["email"], "email_verified": True})
    state = app._oauth_state_new("google", "/dashboard")
    page = app.api_oauth_callback("google", _req(), code="c", state=state).body.decode()
    assert "eyJ" not in page, "токен е издаден преди кода"
    challenge = re.search(r'var challenge = "([^"]+)"', page).group(1)

    with pytest.raises(HTTPException):
        app.api_auth_totp(app.TotpChallengeRequest(challenge=challenge, code="000000"))
    res = app.api_auth_totp(app.TotpChallengeRequest(
        challenge=challenge, code=pyotp.TOTP(secret).now()))
    assert _identify(app, res["token"])[0] == user["id"]
    assert res["next"] == "/dashboard"
    # предизвикателството е еднократно
    with pytest.raises(HTTPException):
        app.api_auth_totp(app.TotpChallengeRequest(
            challenge=challenge, code=pyotp.TOTP(secret).now()))


def test_google_login_without_2fa_still_signs_in_directly(app, db, monkeypatch):
    user = _new_user(db)
    sub = "sub-" + secrets.token_hex(6)
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("INSERT INTO oauth_accounts (provider, provider_user_id, user_id, email)"
                  " VALUES ('google', ?, ?, ?)", (sub, user["id"], user["email"]))
        c.commit()
    _fake_google(app, monkeypatch, {"sub": sub, "email": user["email"], "email_verified": True})
    state = app._oauth_state_new("google", "/chart/1")
    page = app.api_oauth_callback("google", _req(), code="c", state=state).body.decode()
    token = re.search(r'finishLogin\("([^"]+)"', page).group(1)
    assert _identify(app, token)[0] == user["id"]


@pytest.mark.parametrize("value,expected", [
    ("/dashboard", "/dashboard"),
    ("/chart/5?paid=1", "/chart/5?paid=1"),
    ("//evil.com", "/dashboard"),
    ("/\\evil.com", "/dashboard"),
    ("https://evil.com", "/dashboard"),
    ("/\tevil", "/dashboard"),
    ("dashboard", "/dashboard"),
])
def test_next_after_oauth_stays_on_our_site(app, value, expected):
    assert app.safe_next_path(value) == expected


# --- 7. имейли ---------------------------------------------------------------

@pytest.mark.parametrize("email,ok", [
    ("ivan@example.com", True),
    ("ivan.petrov+astro@mail.example.bg", True),
    ("o'brien@example.ie", True),
    ("a@x.com,victim@y.com", False),
    ("a@x.com; b@y.com", False),
    ("Ivan <ivan@example.com>", False),
    ("ivan@example", False),
    ("ivan@@example.com", False),
    ("ivan@example..com", False),
    ("", False),
])
def test_email_validation(app, email, ok):
    assert app.valid_email(email) is ok


def test_register_refuses_an_email_list(app, db):
    with pytest.raises(HTTPException) as err:
        app.api_register(app.AuthRequest(email="a@x.com,victim@y.com", password="parola-123"),
                         _req(client=("198.51.100.20", 1)))
    assert err.value.status_code == 400


def test_register_refuses_a_case_variant_of_an_existing_email(app, db):
    user = _new_user(db)
    with pytest.raises(HTTPException) as err:
        app.api_register(app.AuthRequest(email=user["email"].upper(), password="parola-123"),
                         _req(client=("198.51.100.21", 1)))
    assert err.value.status_code == 409


# --- 8. смяна на имейл --------------------------------------------------------

def test_email_change_needs_the_current_password(app, db):
    user = _new_user(db)
    new_email = f"new-{secrets.token_hex(4)}@example.com"
    with pytest.raises(HTTPException) as err:
        app.api_update_account(app.AccountUpdate(email=new_email),
                               user=(user["id"], user["email"]))
    assert err.value.status_code == 403
    assert err.value.detail["reason"] == "password_required"
    assert app.get_user_by_id(user["id"])["email"] == user["email"]


def test_email_change_with_password_works_and_rotates_the_session(app, db):
    user = _new_user(db)
    old = app.create_token(user["id"], user["email"])
    new_email = f"new-{secrets.token_hex(4)}@example.com"
    res = app.api_update_account(
        app.AccountUpdate(email=new_email, current_password="parola-123"),
        user=(user["id"], user["email"]))
    assert res["email"] == new_email
    with pytest.raises(HTTPException):
        _identify(app, old)
    assert _identify(app, res["token"]) == (user["id"], new_email)


def test_saving_the_same_email_needs_no_password(app, db):
    """Формата праща имейла винаги — смяна на името не бива да иска парола."""
    user = _new_user(db)
    token = app.create_token(user["id"], user["email"])
    res = app.api_update_account(
        app.AccountUpdate(display_name="Иван", email=user["email"].upper()),
        user=(user["id"], user["email"]))
    assert res["display_name"] == "Иван"
    assert _identify(app, token)[0] == user["id"], "сесията е прекъсната без причина"


# --- 9. изтриване на акаунт ---------------------------------------------------

def test_account_deletion_removes_provider_links(app, db):
    email = f"del-{secrets.token_hex(4)}@example.com"
    user = app._oauth_link_or_create("google", "sub-" + secrets.token_hex(6), email, "",
                                     email_verified=True)
    app.api_delete_account(user=(user["id"], user["email"]))
    with sqlite3.connect(app.DB_PATH) as c:
        left = c.execute("SELECT COUNT(*) FROM oauth_accounts WHERE user_id = ?",
                         (user["id"],)).fetchone()[0]
    assert left == 0
