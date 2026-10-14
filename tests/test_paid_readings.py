# -*- coding: utf-8 -*-
"""Платените разчитания се пишат във фона, проверени, без двойни генерирания."""
import sqlite3
import threading
import time

import pytest


GOOD = ("1. **Кой си ти**\n" + "Текст на разчитането. " * 30 +
        "\n\n2. **Силните ти страни**\n" + "Още текст. " * 20)


def _person(app, user_id):
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute(
            "INSERT INTO persons (user_id, name, year, month, day, hour, minute,"
            " lat, lon, timezone) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, "Тест Човек", 1990, 5, 14, 8, 30, 42.7, 23.3, "Europe/Sofia"))
        c.commit()
        return c.execute("SELECT id FROM persons WHERE user_id = ? ORDER BY id DESC",
                         (user_id,)).fetchone()[0]


def _wait_jobs(app, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with app._AI_JOBS_LOCK:
            jobs = list(app._AI_JOBS.values())
        if all(j["done"].is_set() for j in jobs):
            return
        time.sleep(0.02)
    raise AssertionError("AI задачата не приключи")


@pytest.fixture
def buyer(app, user, monkeypatch):
    app.grant_feature_purchase(user["id"], "profile", 500, "EUR", None)
    monkeypatch.setattr(app, "get_ai_config", lambda: ("key", "deepseek"))
    with app._AI_JOBS_LOCK:
        app._AI_JOBS.clear()
    yield user
    with app._AI_JOBS_LOCK:
        app._AI_JOBS.clear()


def _profile(app, user, pid):
    return app.api_profile_interpretation(pid, refresh=False, user=(user["id"], user["email"]))


def test_paid_reading_is_written_in_the_background(app, buyer, monkeypatch):
    """Заявката не чака AI: проксито прекъсваше дългите и клиентът виждаше грешка."""
    gate = threading.Event()
    monkeypatch.setattr(app, "call_ai", lambda *a, **k: gate.wait(5) and GOOD)
    pid = _person(app, buyer["id"])
    first = _profile(app, buyer, pid)
    assert first.get("pending") is True and "interpretation" not in first
    gate.set()
    _wait_jobs(app)
    done = _profile(app, buyer, pid)
    assert done.get("cached") is True and "Кой си ти" in done["interpretation"]


def test_reload_while_writing_does_not_pay_twice(app, buyer, monkeypatch):
    """Второ отваряне докато тече генериране чака същото, не плаща второ Pro."""
    gate, calls = threading.Event(), []

    def slow(*a, **k):
        calls.append(1)
        gate.wait(5)
        return GOOD
    monkeypatch.setattr(app, "call_ai", slow)
    pid = _person(app, buyer["id"])
    assert _profile(app, buyer, pid)["pending"]
    assert _profile(app, buyer, pid)["pending"]
    gate.set()
    _wait_jobs(app)
    assert len(calls) == 1


def test_paid_reading_uses_the_paid_model(app, buyer, monkeypatch):
    models = []
    monkeypatch.setattr(app, "call_ai", lambda *a, **k: models.append(k.get("model")) or GOOD)
    pid = _person(app, buyer["id"])
    _profile(app, buyer, pid)
    _wait_jobs(app)
    assert models == [app.PAID_MODEL]


@pytest.mark.parametrize("reply", ["", "   ", "Кратко.", "Дълъг текст без раздели. " * 40])
def test_broken_reply_is_not_cached(app, buyer, monkeypatch, reply):
    """Кешът е завинаги: отрязан или празен текст оставаше на платилия клиент."""
    monkeypatch.setattr(app, "call_ai", lambda *a, **k: reply)
    pid = _person(app, buyer["id"])
    _profile(app, buyer, pid)
    _wait_jobs(app)
    assert app.get_ai_cache(pid, "profile") is None
    after = _profile(app, buyer, pid)
    assert after.get("error") is True


def test_empty_cached_text_counts_as_missing(app, user):
    pid = _person(app, user["id"])
    app.set_ai_cache(pid, "profile", "")
    assert app.get_ai_cache(pid, "profile") is None


def test_period_reading_uses_the_paid_model(app, user, monkeypatch):
    """„Период“ вървеше на Flash въпреки правилото платено → Pro."""
    app.grant_feature_purchase(user["id"], "period", 500, "EUR", None)
    monkeypatch.setattr(app, "get_ai_config", lambda: ("key", "deepseek"))
    models = []
    monkeypatch.setattr(app, "call_ai", lambda *a, **k: models.append(k.get("model")) or GOOD)
    with app._AI_JOBS_LOCK:
        app._AI_JOBS.clear()
    pid = _person(app, user["id"])
    req = app.PeriodRequest(person_id=pid, start_date="2026-10-01", end_date="2026-11-30")
    res = app.api_period_interpretation(req, refresh=False, user=(user["id"], user["email"]))
    if res.get("pending"):
        _wait_jobs(app)
        assert models == [app.PAID_MODEL]


class _Resp:
    def __init__(self, payload):
        import json
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _reply(text, finish="stop", reasoning=None):
    msg = {"content": text}
    if reasoning:
        msg["reasoning_content"] = reasoning
    return {"model": "deepseek-v4-flash", "choices": [{"message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10}}


def test_reasoning_only_reply_is_an_error(app, monkeypatch):
    """Само „мисли“ без отговор се записваха като готово разчитане."""
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _Resp(_reply("", reasoning="Мисля си…")))
    with pytest.raises(app.AIError):
        app.call_ai("key", "deepseek", "тест", max_tokens=10)


def test_cut_twice_is_an_error(app, monkeypatch):
    """И при двоен лимит отрязан текст не е годен за кеша."""
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _Resp(_reply("Половин текст", finish="length")))
    with pytest.raises(app.AIError):
        app.call_ai("key", "deepseek", "тест", max_tokens=10)
