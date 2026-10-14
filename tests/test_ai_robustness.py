# -*- coding: utf-8 -*-
"""AI генерирането под натоварване: временни грешки, паралелни задачи, пауза след срив."""
import io
import json
import threading
import time
import urllib.error

import pytest


class _Resp:
    def __init__(self, payload):
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


OK = {"choices": [{"message": {"content": "Готов текст."}, "finish_reason": "stop"}],
      "usage": {"prompt_tokens": 10, "completion_tokens": 10}}


def _http(code):
    return urllib.error.HTTPError("https://api.deepseek.com", code, "x", {}, io.BytesIO(b""))


def _provider(monkeypatch, *items):
    queue, calls = list(items), []

    def fake(req, timeout=None):
        calls.append(1)
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return _Resp(item)
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return calls


@pytest.mark.parametrize("error", [_http(503), _http(429), urllib.error.URLError("reset"),
                                   ConnectionResetError()])
def test_transient_error_is_retried(app, monkeypatch, error):
    """Един 503 от доставчика показваше „не се получи“ на платилия клиент."""
    calls = _provider(monkeypatch, error, OK)
    assert app.call_ai("key", "deepseek", "тест") == "Готов текст."
    assert len(calls) == 2


def test_non_json_reply_is_retried(app, monkeypatch):
    calls = _provider(monkeypatch, b"<html>502 Bad Gateway</html>", OK)
    assert app.call_ai("key", "deepseek", "тест") == "Готов текст."
    assert len(calls) == 2


def test_gives_up_after_three_attempts(app, monkeypatch):
    calls = _provider(monkeypatch, _http(503), _http(503), _http(503), OK)
    with pytest.raises(app.AIError):
        app.call_ai("key", "deepseek", "тест")
    assert len(calls) == 3


@pytest.mark.parametrize("error", [_http(401), _http(400), urllib.error.URLError(TimeoutError("timed out"))])
def test_permanent_error_and_timeout_are_not_retried(app, monkeypatch, error):
    """Грешен ключ не се оправя с повторение; таймаутът вече е чакал 3 минути."""
    calls = _provider(monkeypatch, error, OK)
    with pytest.raises(Exception):
        app.call_ai("key", "deepseek", "тест")
    assert len(calls) == 1


def test_anthropic_truncated_reply_is_an_error(app, monkeypatch):
    _provider(monkeypatch, {"content": [{"type": "text", "text": "Половин"}], "stop_reason": "max_tokens",
                            "usage": {"input_tokens": 1, "output_tokens": 1}})
    with pytest.raises(app.AIError):
        app.call_ai("key", "anthropic", "тест")


def test_usage_shows_the_model_really_used(app, monkeypatch):
    """При Anthropic отчетът пишеше deepseek-v4-pro и цената на DeepSeek."""
    import sqlite3
    _provider(monkeypatch, {"content": [{"type": "text", "text": "Текст."}], "stop_reason": "end_turn",
                            "usage": {"input_tokens": 1, "output_tokens": 1}})
    app.call_ai("key", "anthropic", "тест", model=app.PAID_MODEL)
    with sqlite3.connect(app.DB_PATH) as c:
        model = c.execute("SELECT model FROM ai_usage ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert model == app.resolve_ai_model("anthropic")


def test_ai_jobs_run_at_most_n_at_once(app, monkeypatch):
    """20 посетители пускаха 20 едновременни генерирания."""
    monkeypatch.setattr(app, "_AI_JOB_SLOTS", threading.BoundedSemaphore(2))
    running, peak, lock, release = [0], [0], threading.Lock(), threading.Event()

    def work():
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        release.wait(5)
        with lock:
            running[0] -= 1

    jobs = [app.ai_job(f"test:limit:{i}", work) for i in range(5)]
    time.sleep(0.3)
    assert peak[0] == 2
    release.set()
    for j in jobs:
        assert j["done"].wait(5) and not j["error"]
    assert peak[0] == 2


def test_seo_text_waits_after_a_failure(app, monkeypatch):
    """След срив всяка проверка на страницата (на 3 s) пускаше ново генериране."""
    slug, (a, b) = next(iter(app.COMPAT_BY_SLUG.items()))
    key = f"compat:{a['sign']}:{b['sign']}"
    monkeypatch.setattr(app, "get_compatibility", lambda *x: None)
    failed = {"done": threading.Event(), "error": "503", "finished_at": time.monotonic()}
    failed["done"].set()
    started = []
    monkeypatch.setattr(app, "ai_job", lambda k, fn: started.append(k))
    monkeypatch.setitem(app._AI_JOBS, key, failed)
    res = app.api_compatibility(slug, _Req())
    assert res["error"] and not started
    failed["finished_at"] -= app.AI_RETRY_AFTER + 1          # паузата мина → нов опит
    monkeypatch.setattr(app, "ai_job", lambda k, fn: started.append(k) or {"done": threading.Event()})
    assert app.api_compatibility(slug, _Req()) == {"pending": True} and started == [key]


class _Req:
    headers, cookies, query_params = {"host": "astrokarta.bg"}, {}, {}
    client = type("C", (), {"host": "1.2.3.4"})()
