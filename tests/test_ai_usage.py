# -*- coding: utf-8 -*-
"""Колко токена гори всяко AI извикване, колко струва и за кого е.

Доставчикът се подменя — тестовете не харчат нищо. Броят се отговорите
на доставчика, а не крайният текст: повторният опит при отрязан текст също
е платен.
"""
import datetime
import json
import sqlite3
import threading

import pytest


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _deepseek(text="Готов текст.", prompt=1000, hit=0, completion=500, finish="stop"):
    return {"model": "deepseek-v4-flash",
            "choices": [{"message": {"content": text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                      "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": prompt - hit}}


def _provider(monkeypatch, *responses):
    """Подменя HTTP извикването към доставчика с готови отговори."""
    queue = list(responses)
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


def _rows(app):
    with sqlite3.connect(app.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM ai_usage ORDER BY id")]


@pytest.fixture(autouse=True)
def _clean_usage(app):
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("DELETE FROM ai_usage")
        c.commit()


def _origin(app, path, user_id=None):
    return app.AI_ORIGIN.set((path, user_id))


# --- броене на токените ------------------------------------------------------

def test_tokens_are_recorded_from_the_provider(app, monkeypatch):
    _provider(monkeypatch, _deepseek(prompt=1200, hit=200, completion=800))
    app.call_ai("key", "deepseek", "подкана")
    row = _rows(app)[0]
    assert (row["input_tokens"], row["cached_tokens"], row["output_tokens"]) == (1000, 200, 800)
    assert row["ok"] == 1


def test_a_truncated_retry_is_paid_twice(app, monkeypatch):
    """Отрязан текст → втори опит с двоен лимит. И двата се плащат."""
    _provider(monkeypatch,
              _deepseek(prompt=1000, completion=4000, finish="length"),
              _deepseek(prompt=1000, completion=6000, finish="stop"))
    app.call_ai("key", "deepseek", "подкана", max_tokens=4000)
    row = _rows(app)[0]
    assert row["input_tokens"] == 2000
    assert row["output_tokens"] == 10000
    assert row["attempts"] == 2


def test_anthropic_usage_is_read(app, monkeypatch):
    _provider(monkeypatch, {"content": [{"text": "Текст."}],
                            "usage": {"input_tokens": 900, "output_tokens": 300,
                                      "cache_read_input_tokens": 100}})
    app.call_ai("key", "anthropic", "подкана", model="claude-sonnet-4-5")
    row = _rows(app)[0]
    assert (row["input_tokens"], row["cached_tokens"], row["output_tokens"]) == (900, 100, 300)


def test_openai_cached_tokens_are_separated(app, monkeypatch):
    _provider(monkeypatch, {"choices": [{"message": {"content": "Текст."}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 1000, "completion_tokens": 100,
                                      "prompt_tokens_details": {"cached_tokens": 400}}})
    app.call_ai("key", "openai", "подкана", model="gpt-4o-mini")
    row = _rows(app)[0]
    assert (row["input_tokens"], row["cached_tokens"]) == (600, 400)


def test_a_failed_call_is_recorded_too(app, monkeypatch):
    import urllib.error
    _provider(monkeypatch, urllib.error.URLError("няма мрежа"))
    with pytest.raises(app.AIError):
        app.call_ai("key", "deepseek", "подкана")
    row = _rows(app)[0]
    assert row["ok"] == 0 and row["cost_usd"] == 0


def test_recording_never_breaks_the_reading(app, monkeypatch):
    """Ако записът се провали, клиентът пак трябва да получи текста си."""
    _provider(monkeypatch, _deepseek(text="Разчитането."))
    monkeypatch.setattr(app, "DB_PATH", app.DB_PATH.parent / "няма" / "такава.db")
    assert app.call_ai("key", "deepseek", "подкана", model="deepseek-v4-flash") == "Разчитането."


# --- цена --------------------------------------------------------------------

def _utc(y, m, d, h):
    return datetime.datetime(y, m, d, h, 0, tzinfo=datetime.timezone.utc)


def test_deepseek_peak_price(app):
    # сряда 08:00 UTC — дневна тарифа
    cost = app.ai_cost_usd("deepseek", "deepseek-v4-pro", 1_000_000, 0, 1_000_000, _utc(2026, 9, 30, 8))
    assert cost == pytest.approx(1.32 + 3.96)


def test_deepseek_off_peak_is_half(app):
    # сряда 12:00 UTC и събота 08:00 UTC — нощна/уикенд тарифа
    for when in (_utc(2026, 9, 30, 12), _utc(2026, 10, 3, 8)):
        cost = app.ai_cost_usd("deepseek", "deepseek-v4-pro", 1_000_000, 0, 1_000_000, when)
        assert cost == pytest.approx((1.32 + 3.96) / 2)


def test_cache_hits_are_cheap(app):
    cost = app.ai_cost_usd("deepseek", "deepseek-v4-flash", 0, 1_000_000, 0, _utc(2026, 9, 30, 8))
    assert cost == pytest.approx(0.006)


def test_other_providers_have_no_time_discount(app):
    cost = app.ai_cost_usd("anthropic", "claude-sonnet-4-5", 1_000_000, 0, 1_000_000, _utc(2026, 9, 30, 12))
    assert cost == pytest.approx(3 + 15)


def test_unknown_model_has_no_made_up_price(app):
    assert app.ai_cost_usd("deepseek", "nyakakav-model", 1000, 0, 1000, _utc(2026, 9, 30, 8)) is None


# --- откъде е дошло ----------------------------------------------------------

@pytest.mark.parametrize("path,source,feature", [
    ("/api/persons/5/profile/interpretation", "client", "profile"),
    ("/api/persons/5/akashic/interpretation", "client", "akashic"),
    ("/api/persons/5/numerology/interpretation", "client", "numerology"),
    ("/api/persons/5/daily-horoscope", "client", "horoscope"),
    ("/api/period-interpretation", "client", "period"),
    ("/api/love-match/interpretation", "client", "love"),
    ("/api/synastry/interpretation", "client", "synastry"),
    ("/api/horoskop/oven", "seo", "sign_horoscope"),
    ("/api/planeta/luna-v-oven", "seo", "planet_sign"),
    ("/api/dom/luna-v-7", "seo", "planet_house"),
    ("/api/zodia/oven", "seo", "sign_profile"),
    ("/api/savmestimost/oven-bik", "seo", "compatibility"),
    ("/api/admin/templates/preview", "admin", "admin"),
])
def test_source_is_taken_from_the_request(app, monkeypatch, path, source, feature):
    _provider(monkeypatch, _deepseek())
    token = _origin(app, path, 7)
    try:
        app.call_ai("key", "deepseek", "подкана")
    finally:
        app.AI_ORIGIN.reset(token)
    row = _rows(app)[0]
    assert (row["source"], row["feature"]) == (source, feature)


def test_client_reading_names_the_customer(app, monkeypatch):
    _provider(monkeypatch, _deepseek())
    token = _origin(app, "/api/persons/5/profile/interpretation", 42)
    try:
        app.call_ai("key", "deepseek", "подкана")
    finally:
        app.AI_ORIGIN.reset(token)
    assert _rows(app)[0]["user_id"] == 42


def test_no_request_means_background(app, monkeypatch):
    _provider(monkeypatch, _deepseek())
    app.call_ai("key", "deepseek", "подкана")
    assert _rows(app)[0]["source"] == "background"


def test_background_generation_keeps_the_origin(app, monkeypatch):
    """Генерирането върви в отделна нишка, но разходът е на клиента."""
    _provider(monkeypatch, _deepseek())
    token = _origin(app, "/api/persons/5/profile/interpretation", 42)
    try:
        job = app.ai_job("usage-test", lambda: app.call_ai("key", "deepseek", "подкана"))
    finally:
        app.AI_ORIGIN.reset(token)
    job["done"].wait(5)
    row = _rows(app)[0]
    assert (row["source"], row["user_id"]) == ("client", 42)


# --- отчетът в админа --------------------------------------------------------

def _seed(app, rows):
    with sqlite3.connect(app.DB_PATH) as c:
        for r in rows:
            c.execute(
                "INSERT INTO ai_usage (at, provider, model, source, feature, user_id,"
                " input_tokens, cached_tokens, output_tokens, cost_usd, ok, attempts,"
                " duration_ms, request_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
        c.commit()


def test_admin_report_splits_clients_and_system(app):
    now = datetime.datetime.utcnow().isoformat(timespec="seconds")
    old = (datetime.datetime.utcnow() - datetime.timedelta(days=40)).isoformat(timespec="seconds")
    _seed(app, [
        (now, "deepseek", "deepseek-v4-pro", "client", "profile", 42, 3000, 0, 5000, 0.02, 1, 1, 9000, "AAA111"),
        (now, "deepseek", "deepseek-v4-pro", "client", "akashic", 42, 3000, 0, 5000, 0.03, 1, 1, 9000, "AAA112"),
        (now, "deepseek", "deepseek-v4-flash", "seo", "sign_horoscope", None, 1000, 0, 1500, 0.002, 1, 1, 4000, "AAA113"),
        (now, "deepseek", "deepseek-v4-flash", "seo", "sign_horoscope", None, 1000, 0, 0, 0.0, 0, 1, 180000, "AAA114"),
        (old, "deepseek", "deepseek-v4-pro", "client", "profile", 42, 3000, 0, 5000, 5.0, 1, 1, 9000, "OLD000"),
    ])
    rep = app.api_admin_ai_usage(days=30, admin={"id": 1})
    assert rep["totals"]["calls"] == 4, "стар запис влиза в периода"
    assert rep["totals"]["failed"] == 1
    assert rep["totals"]["cost_usd"] == pytest.approx(0.052)
    by_source = {s["source"]: s for s in rep["by_source"]}
    assert by_source["client"]["cost_usd"] == pytest.approx(0.05)
    assert by_source["seo"]["calls"] == 2
    top = rep["top_users"][0]
    assert top["user_id"] == 42 and top["cost_usd"] == pytest.approx(0.05)


def test_admin_report_all_time(app):
    old = (datetime.datetime.utcnow() - datetime.timedelta(days=400)).isoformat(timespec="seconds")
    _seed(app, [(old, "deepseek", "deepseek-v4-pro", "client", "profile", 42, 1, 0, 1, 1.5, 1, 1, 1, "X")])
    assert app.api_admin_ai_usage(days=0, admin={"id": 1})["totals"]["cost_usd"] == pytest.approx(1.5)


def test_admin_report_lists_the_prices_it_uses(app):
    rep = app.api_admin_ai_usage(days=30, admin={"id": 1})
    assert rep["prices"]["deepseek-v4-pro"]["output"] == pytest.approx(3.96)
