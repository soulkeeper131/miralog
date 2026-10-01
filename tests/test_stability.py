# -*- coding: utf-8 -*-
"""Стабилност: AI задачи, /healthz, модел по доставчик, рождени данни,
търсене на място, гласово четене, дневни писма, синастрия, споделяне."""
import asyncio
import datetime
import inspect
import secrets
import sqlite3
import sys
import threading
import time
import types
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from starlette.requests import Request

SOFIA = ZoneInfo("Europe/Sofia")


def _person(app, user_id, tz="Europe/Sofia"):
    with sqlite3.connect(app.DB_PATH) as c:
        cur = c.execute("INSERT INTO persons (user_id, name, year, month, day, hour, minute,"
                        " lat, lon, timezone) VALUES (?, 'Тест', 1990, 6, 15, 12, 0, 42.7, 23.3, ?)",
                        (user_id, tz))
        c.commit()
        return cur.lastrowid


# --- AI задачи ------------------------------------------------------------------------

def _wait(job):
    assert job["done"].wait(5)


def test_failed_ai_job_is_retried_after_a_while(app):
    """Досега една временна грешка оставаше до рестарт."""
    key = "test:" + secrets.token_hex(4)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("таймаут")
    job = app.ai_job(key, boom)
    _wait(job)
    assert app.ai_job_failed_recently(job)
    job["finished_at"] -= app.AI_RETRY_AFTER + 1          # минала е минута
    assert not app.ai_job_failed_recently(job)
    _wait(app.ai_job(key, boom))
    assert len(calls) == 2


def test_finished_ai_jobs_do_not_pile_up(app):
    old = time.monotonic() - app.AI_JOB_KEEP - 10
    for i in range(250):
        ev = threading.Event()
        ev.set()
        app._AI_JOBS[f"old:{i}"] = {"done": ev, "error": None, "finished_at": old}
    _wait(app.ai_job("fresh:" + secrets.token_hex(3), lambda: None))
    assert not any(k.startswith("old:") for k in app._AI_JOBS)


def test_missing_ai_key_is_an_error_not_endless_pending(app, monkeypatch):
    monkeypatch.setattr(app, "get_ai_config", lambda: (None, "deepseek"))
    with pytest.raises(app.AINotConfigured):
        app.ai_config_or_raise()
    sign = next(iter(app.ZODIAC_BY_SLUG.values()))
    job = app.ai_job("test-nokey:" + secrets.token_hex(3),
                     lambda: app._generate_sign_horoscope(sign, "01.10.2026", "2026-10-01"))
    _wait(job)
    assert job["error"], "без ключ задачата „успяваше“ без текст и страницата чакаше безкрай"


# --- /healthz и модел ---------------------------------------------------------------------

def test_health_check_does_not_need_a_worker_thread(app):
    assert inspect.iscoroutinefunction(app.health)
    assert asyncio.run(app.health()) == {"status": "ok"}


def test_paid_model_falls_back_for_other_providers(app, monkeypatch):
    """deepseek-v4-pro към Anthropic даваше 404 за всяко платено разчитане."""
    import json
    import urllib.request
    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode()))
        raise OSError("без мрежа в теста")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    for provider in ("anthropic", "openai"):
        with pytest.raises(Exception):
            app.call_ai("key", provider, "тест", max_tokens=10, model=app.PAID_MODEL)
        assert sent[-1]["model"] == app.resolve_ai_model(provider)
    with pytest.raises(Exception):
        app.call_ai("key", "deepseek", "тест", max_tokens=10, model=app.PAID_MODEL)
    assert sent[-1]["model"] == app.PAID_MODEL


# --- рождени данни ---------------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    (1990, 2, 31, 12, 0, 42.7, 23.3, "Europe/Sofia"),
    (1990, 13, 1, 12, 0, 42.7, 23.3, "Europe/Sofia"),
    (1990, 6, 15, 24, 0, 42.7, 23.3, "Europe/Sofia"),
    (1990, 6, 15, 12, 60, 42.7, 23.3, "Europe/Sofia"),
    (1990, 6, 15, 12, 0, 95.0, 23.3, "Europe/Sofia"),
    (1990, 6, 15, 12, 0, 42.7, 23.3, "Mars/Olympus"),
    (1500, 6, 15, 12, 0, 42.7, 23.3, "Europe/Sofia"),
])
def test_impossible_birth_data_is_refused(app, args):
    with pytest.raises(HTTPException) as err:
        app.validate_birth(*args)
    assert err.value.status_code == 400


def test_valid_birth_data_passes(app):
    app.validate_birth(2000, 2, 29, 23, 59, -33.9, 151.2, "Australia/Sydney")


def test_bad_date_never_reaches_the_database(app, db, user):
    """31.02 се записваше; после картата даваше 500 и заемаше място."""
    with pytest.raises(HTTPException):
        app.api_create_person(name="Х", year=1990, month=2, day=31, hour=0, minute=0,
                              lat=42.7, lon=23.3, timezone="Europe/Sofia",
                              user=(user["id"], user["email"]))
    with sqlite3.connect(app.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM persons WHERE user_id = ?",
                         (user["id"],)).fetchone()[0] == 0


def test_onboard_with_a_bad_date_creates_no_account(app, db):
    email = f"bad-{secrets.token_hex(3)}@example.com"
    data = app.OnboardRequest(email=email, name="Х", year=1990, month=2, day=30,
                              lat=42.7, lon=23.3)
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                   "query_string": b"", "scheme": "https", "server": ("astrokarta.bg", 443),
                   "client": ("8.8.8.8", 1)})
    with pytest.raises(HTTPException):
        app.api_onboard(data, req)
    assert app.get_user_by_email(email) is None


# --- търсене на място ------------------------------------------------------------------------

def test_geocode_cache_is_bounded(app, monkeypatch):
    monkeypatch.setattr(app, "_geocode_fetch", lambda q, limit: [{"label": q}])
    monkeypatch.setattr(app, "_GEOCODE_CACHE_MAX", 5)
    app._geocode_cache.clear()
    for i in range(12):
        app.geocode_place(f"място {i}")
    assert len(app._geocode_cache) == 5
    assert app.geocode_place("място 11") == [{"label": "място 11"}]
    app._geocode_cache.clear()


def test_geocode_gives_up_quickly_when_the_service_is_stuck(app, monkeypatch):
    """Увиснал Nominatim не бива да задържи всички нишки на сайта в опашка."""
    class StuckLock:
        def acquire(self, timeout=None):
            return False

        def release(self):
            raise AssertionError("не е взета")
    app._geocode_cache.clear()
    monkeypatch.setattr(app, "_GEOCODE_LOCK", StuckLock())
    with pytest.raises(HTTPException) as err:
        app.geocode_place("Пловдив")
    assert err.value.status_code == 503


def test_public_geocode_is_rate_limited(app, monkeypatch):
    monkeypatch.setattr(app, "geocode_place", lambda q: [])
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                   "query_string": b"", "scheme": "https", "server": ("astrokarta.bg", 443),
                   "client": ("8.8.8.8", 1)})
    limit = app.RATE_LIMITS["geocode"][0]
    for _ in range(limit):
        app.api_public_geocode("София", req)
    with pytest.raises(HTTPException) as err:
        app.api_public_geocode("София", req)
    assert err.value.status_code == 429


# --- гласово четене -----------------------------------------------------------------------------

def test_failed_speech_leaves_no_empty_mp3(app, monkeypatch, tmp_path):
    """Прекъснат синтез оставяше празен mp3, който после се сервираше завинаги."""
    class FailingCommunicate:
        def __init__(self, *a, **k):
            pass

        async def stream(self):
            raise RuntimeError("TTS е долу")
            yield  # pragma: no cover

    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(Communicate=FailingCommunicate))
    target = tmp_path / "x.mp3"
    with pytest.raises(RuntimeError):
        app._text_to_audio("Кратък текст.", str(target))
    assert not target.exists()
    assert not list(tmp_path.glob("*.part"))


def test_speech_writes_the_whole_file(app, monkeypatch, tmp_path):
    class Communicate:
        def __init__(self, text, voice):
            self.text = text

        async def stream(self):
            yield {"type": "audio", "data": b"ID3" + self.text.encode()}

    monkeypatch.setitem(sys.modules, "edge_tts", types.SimpleNamespace(Communicate=Communicate))
    target = tmp_path / "ok.mp3"
    app._text_to_audio("Кратък текст.", str(target))
    assert target.read_bytes().startswith(b"ID3")


# --- дневни писма --------------------------------------------------------------------------------

def _digest_user(app, db):
    u = db.create_user(f"dg-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    db.grant_signup_features(u["id"])
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE users SET digest_opt_in = 1 WHERE id = ?", (u["id"],))
        c.commit()
    _person(app, u["id"])
    return u


def test_digest_waits_until_morning_and_is_not_marked_when_it_fails(app, db, monkeypatch):
    u = _digest_user(app, db)
    monkeypatch.setattr(app, "smtp_setting", lambda k: "smtp.example.com" if k == "smtp_host" else "")
    sends = []
    monkeypatch.setattr(app, "try_send_template", lambda *a, **k: sends.append(a) or False)
    app.run_digest_emails(datetime.datetime(2026, 10, 1, 3, 0, tzinfo=SOFIA))
    assert not sends, "в 3 ч. през нощта писмото не бива да тръгне"
    app.run_digest_emails(datetime.datetime(2026, 10, 1, 9, 0, tzinfo=SOFIA))
    assert sends
    assert app.get_user_by_id(u["id"])["last_digest_on"] is None, \
        "неизпратеното се отбелязваше като изпратено"
    monkeypatch.setattr(app, "try_send_template", lambda *a, **k: True)
    app.run_digest_emails(datetime.datetime(2026, 10, 1, 10, 0, tzinfo=SOFIA))
    assert app.get_user_by_id(u["id"])["last_digest_on"] == "2026-10-01"


# --- синастрия, изтриване, споделяне, линкове -----------------------------------------------

def test_synastry_reading_of_the_partner_is_reused(app, db, user):
    app.grant_feature_purchase(user["id"], "love", 500, "EUR", None)
    a, b = _person(app, user["id"]), _person(app, user["id"])
    key = f"synastry:{min(a, b)}:{max(a, b)}"
    app.set_ai_cache(b, key, "Готова синастрия")
    res = app.api_synastry_interpretation(app.SynastryRequest(person1_id=a, person2_id=b),
                                          user=(user["id"], user["email"]))
    assert res["cached"] and res["interpretation"] == "Готова синастрия"
    assert app.get_ai_cache(a, key), "копие за PDF/споделяне от тази страница"


def test_changing_the_partner_clears_the_synastry(app, user):
    a, b = _person(app, user["id"]), _person(app, user["id"])
    key = f"synastry:{min(a, b)}:{max(a, b)}"
    app.set_ai_cache(a, key, "стара синастрия")
    app.clear_ai_cache(b)
    assert app.get_ai_cache(a, key) is None


def test_deleting_a_person_removes_readings_shares_and_audio(app, user):
    pid = _person(app, user["id"])
    app.set_ai_cache(pid, "profile", "текст")
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("INSERT INTO share_links (token, person_id, user_id, cache_key)"
                  " VALUES (?, ?, ?, 'profile')", ("tok" + secrets.token_hex(4), pid, user["id"]))
        c.commit()
    audio = app.DB_PATH.parent / "audio"
    audio.mkdir(exist_ok=True)
    mp3 = audio / f"{pid}_profile_abc.mp3"
    mp3.write_bytes(b"ID3")
    app.api_delete_person(pid, user=(user["id"], user["email"]))
    with sqlite3.connect(app.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM ai_cache WHERE person_id = ?", (pid,)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM share_links WHERE person_id = ?", (pid,)).fetchone()[0] == 0
    assert not mp3.exists()


def test_shared_reading_is_rendered_and_escaped(app, user):
    app.grant_feature_purchase(user["id"], "profile", 500, "EUR", None)   # споделя купен модул
    pid = _person(app, user["id"])
    app.set_ai_cache(pid, "profile", "1. **Заглавие** <script>alert(1)</script>")
    token = "tok" + secrets.token_hex(6)
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("INSERT INTO share_links (token, person_id, user_id, cache_key)"
                  " VALUES (?, ?, ?, 'profile')", (token, pid, user["id"]))
        c.commit()
    data = app.api_get_share(token)
    assert "<h3>" in data["content_html"] and "<script>" not in data["content_html"]
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                   "query_string": b"", "scheme": "https", "server": ("astrokarta.bg", 443)})
    assert asyncio.run(app.share_page(req, token)).status_code == 200
    assert asyncio.run(app.share_page(req, "няма-такъв")).status_code == 404


def test_links_in_production_never_come_from_the_host_header(app, monkeypatch):
    monkeypatch.setattr(app, "IS_PRODUCTION", True)
    app.set_setting("seo_site_url", "")
    req = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                   "headers": [(b"host", b"evil.example")], "scheme": "http",
                   "server": ("evil.example", 80)})
    assert app.site_base_url(req) == f"https://{app.brand()['domain']}"
    assert app.site_base_url() == f"https://{app.brand()['domain']}"


def test_oauth_signup_choice_is_remembered(app, db, user):
    res = app.api_remember_pending(app.PendingPurchase(keys=["moon", "nonsense", "chart"]),
                                   user=(user["id"], user["email"]))
    assert res["keys"] == ["moon"]
    assert app.take_pending_purchase(user["id"])["keys"] == ["moon"]
    res = app.api_remember_pending(app.PendingPurchase(bundle=True), user=(user["id"], user["email"]))
    assert res["bundle"] and len(res["keys"]) >= 2
