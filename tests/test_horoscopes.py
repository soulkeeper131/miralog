# -*- coding: utf-8 -*-
"""Как се генерират хороскопите: навреме, с правилния модел и без излишни разходи."""
import datetime
import secrets
import sqlite3
import threading
import time

import pytest


def _done_job():
    ev = threading.Event()
    ev.set()
    return {"done": ev, "error": None}


# --- хороскопите по зодия се пишат сами след полунощ -------------------------

def _clear_signs(app):
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("DELETE FROM sign_horoscope")
        c.commit()


def test_warm_starts_every_missing_sign(app, monkeypatch):
    _clear_signs(app)
    started = []
    monkeypatch.setattr(app, "ai_job", lambda key, fn: started.append(key) or _done_job())
    # Днес + утре (ден напред) = 2 дни × 12 зодии
    assert app.warm_sign_horoscopes() == 24
    assert len(started) == 24


def test_warm_skips_signs_already_written(app, monkeypatch):
    """Повторното викане на всеки 10 минути не бива да харчи нищо."""
    _clear_signs(app)
    today = app.sofia_today()
    for d in (today, today + datetime.timedelta(days=1)):
        for s in app.ZODIAC_SIGNS:
            app.set_sign_horoscope(s["sign"], d.isoformat(), "Готов текст.")
    started = []
    monkeypatch.setattr(app, "ai_job", lambda key, fn: started.append(key) or _done_job())
    assert app.warm_sign_horoscopes() == 0
    assert started == []


def test_scheduled_warm_is_counted_as_seo(app, monkeypatch):
    """Разходът за тези 12 текста е на SEO страниците, не „фонов“."""
    _clear_signs(app)
    seen = []

    def fake_generate(sign, date_bg, date_iso):
        seen.append(app._ai_source(app.AI_ORIGIN.get()))

    monkeypatch.setattr(app, "_generate_sign_horoscope", fake_generate)
    app.run_horoscope_warm()
    deadline = time.time() + 5
    while len(seen) < 12 and time.time() < deadline:
        time.sleep(0.05)
    assert seen and all(s[:2] == ("seo", "sign_horoscope") for s in seen)


def test_warm_loop_is_started_with_the_app(app):
    import inspect
    assert "_horoscope_warm_loop" in inspect.getsource(app.lifespan)


# --- личният дневен хороскоп: Pro за платилите, Flash за останалите ------------

def _person(app, user_id):
    with sqlite3.connect(app.DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO persons (user_id, name, year, month, day, hour, minute,"
            " lat, lon, timezone) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, "Тест Тестов", 1990, 5, 14, 8, 30, 42.7, 23.3, "Europe/Sofia"))
        c.commit()
        return cur.lastrowid


def _model_used(app, monkeypatch, user_row):
    used = []
    monkeypatch.setattr(app, "get_ai_config", lambda: ("key", "deepseek"))
    monkeypatch.setattr(app, "call_ai", lambda *a, **k: used.append(k.get("model")) or "Текст.")
    pid = _person(app, user_row["id"])
    out = app.api_daily_horoscope(pid, user=(user_row["id"], user_row["email"]))
    if out.get("pending"):
        with app._AI_JOBS_LOCK:
            jobs = [j for j in app._AI_JOBS.values() if not j["done"].is_set()]
        for job in jobs:
            job["done"].wait(30)
    assert used, "хороскопът не беше генериран"
    return used[0]


def test_free_user_gets_the_fast_model(app, user, monkeypatch):
    assert _model_used(app, monkeypatch, user) != app.PAID_MODEL


def test_paying_customer_gets_the_stronger_model(app, user, monkeypatch):
    app.grant_feature_purchase(user["id"], "numerology", 699, "EUR", None)
    assert _model_used(app, monkeypatch, app.get_user_by_id(user["id"])) == app.PAID_MODEL


def test_free_modules_alone_do_not_count_as_paying(app, user):
    """chart и horoscope се дават на всички при регистрация."""
    assert app.is_paying_customer(user["id"]) is False


def test_bundle_buyer_is_paying(app, user):
    for key in ("profile", "akashic"):
        app.grant_feature_purchase(user["id"], key, 0, "EUR", None)
    assert app.is_paying_customer(user["id"]) is True


# --- един човек не чака друг ---------------------------------------------------

def test_two_people_generate_in_parallel(app, db, monkeypatch):
    """Задачата беше с ключ „horoscope:ДАТА“ — обща за всички. Докато се пише
    хороскопът на един, всеки друг получаваше „пише се…“ и чакаше."""
    a = db.create_user(f"a-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    b = db.create_user(f"b-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    db.grant_signup_features(a["id"])
    db.grant_signup_features(b["id"])
    pa, pb = _person(app, a["id"]), _person(app, b["id"])

    release = threading.Event()
    calls = []

    def slow_ai(*args, **kwargs):
        calls.append(1)
        release.wait(10)
        return "Текст."

    monkeypatch.setattr(app, "get_ai_config", lambda: ("key", "deepseek"))
    monkeypatch.setattr(app, "call_ai", slow_ai)
    try:
        app.api_daily_horoscope(pa, user=(a["id"], a["email"]))
        app.api_daily_horoscope(pb, user=(b["id"], b["email"]))
        deadline = time.time() + 10
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert len(calls) == 2, "вторият човек чака първия"
    finally:
        release.set()


def test_one_persons_failure_does_not_reach_another(app, db, monkeypatch):
    a = db.create_user(f"a-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    b = db.create_user(f"b-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    db.grant_signup_features(a["id"])
    db.grant_signup_features(b["id"])
    pa, pb = _person(app, a["id"]), _person(app, b["id"])
    monkeypatch.setattr(app, "get_ai_config", lambda: ("key", "deepseek"))

    def fail(*a_, **k):
        raise app.AIError("доставчикът е долу")
    monkeypatch.setattr(app, "call_ai", fail)
    out = app.api_daily_horoscope(pa, user=(a["id"], a["email"]))
    with app._AI_JOBS_LOCK:
        jobs = [j for j in app._AI_JOBS.values() if not j["done"].is_set()]
    for j in jobs:
        j["done"].wait(30)

    monkeypatch.setattr(app, "call_ai", lambda *a_, **k: "Текстът на Б.")
    out = app.api_daily_horoscope(pb, user=(b["id"], b["email"]))
    if out.get("pending"):
        time.sleep(0.5)
        out = app.api_daily_horoscope(pb, user=(b["id"], b["email"]))
    assert out.get("interpretation") != app.AI_UNAVAILABLE, "грешката на А стигна до Б"


# --- ?refresh=true на личния хороскоп ------------------------------------------

def test_user_refresh_serves_todays_text(app, user, monkeypatch):
    """Бутонът е махнат, но адресът го приемаше — Pro в цикъл от скрипт."""
    pid = _person(app, user["id"])
    key = f"horoscope:{app.sofia_today().isoformat()}"
    app.set_ai_cache(pid, key, "Днешният текст.")
    started = []
    monkeypatch.setattr(app, "ai_job", lambda k, fn: started.append(k) or _done_job())
    out = app.api_daily_horoscope(pid, refresh=True, user=(user["id"], user["email"]))
    assert started == []
    assert "Днешният текст." in out["interpretation"]


def test_admin_can_still_refresh_a_horoscope(app, db, monkeypatch):
    admin = db.create_user(f"adm-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE users SET role = 'admin' WHERE id = ?", (admin["id"],))
        c.commit()
    pid = _person(app, admin["id"])
    app.set_ai_cache(pid, f"horoscope:{app.sofia_today().isoformat()}", "Стар текст.")
    started = []
    monkeypatch.setattr(app, "ai_job", lambda k, fn: started.append(k) or _done_job())
    app.api_daily_horoscope(pid, refresh=True, user=(admin["id"], admin["email"]))
    assert len(started) == 1
