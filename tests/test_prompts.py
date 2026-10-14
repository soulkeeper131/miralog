# -*- coding: utf-8 -*-
"""Подканите: неизвестен час на раждане, синастрия с истински аспекти, данни за профила."""
import sqlite3

import pytest


def _person(app, user_id, *, name="Мария Петрова", unknown=False, y=1990, m=5, d=14, h=8, mi=30):
    with sqlite3.connect(app.DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO persons (user_id, name, year, month, day, hour, minute, lat, lon, timezone,"
            " time_unknown) VALUES (?, ?, ?, ?, ?, ?, ?, 42.7, 23.3, 'Europe/Sofia', ?)",
            (user_id, name, y, m, d, 12 if unknown else h, 0 if unknown else mi, int(unknown)))
        c.commit()
        return cur.lastrowid


@pytest.fixture
def prompts(app, user, monkeypatch):
    """Подканите, които биха отишли към AI — без AI и без фонови нишки."""
    for key in ("profile", "akashic", "love", "period"):
        app.grant_feature_purchase(user["id"], key, 500, "EUR", None)
    sent = []
    monkeypatch.setattr(app, "start_paid_reading",
                        lambda pid, key, prompt, **kw: sent.append(prompt) or {"pending": True})
    return sent


def _u(user):
    return (user["id"], user["email"])


def test_profile_without_birth_time_has_no_angles_or_houses(app, user, prompts):
    pid = _person(app, user["id"], unknown=True)
    app.api_profile_interpretation(pid, user=_u(user))
    prompt = prompts[-1]
    assert "ЧАСЪТ НА РАЖДАНЕ Е НЕИЗВЕСТЕН" in prompt
    data = prompt.split("=== ЯДРО НА ЛИЧНОСТТА ===")[1].split("=== ЗАДАЧА ===")[0]
    assert "Асцендент" not in data and "Медиум Коели" not in data
    assert "-ти дом" not in data
    assert " в 12:00" not in prompt


def test_profile_gets_retrogrades_and_outer_planets(app, user, prompts):
    """Подканата питаше за ретроградна планета, без да я подава."""
    pid = _person(app, user["id"])
    app.api_profile_interpretation(pid, user=_u(user))
    prompt = prompts[-1]
    assert "=== РЕТРОГРАДНИ ПЛАНЕТИ ===" in prompt
    for planet in ("Юпитер", "Сатурн", "Плутон"):
        assert planet in prompt.split("=== ЗАДАЧА ===")[0]
    assert "Асцендент (как те виждат)" in prompt and "ЧАСЪТ НА РАЖДАНЕ" not in prompt


def test_profile_api_hides_angles_without_time(app, user):
    pid = _person(app, user["id"], unknown=True)
    prof = app.api_profile(pid, user=_u(user))
    assert prof["core"]["ascendant"] is None and prof["core"]["mc"] is None
    assert prof["emphasised_houses"] == [] and prof["time_unknown"] is True
    assert all(not p["house_bg"] for p in prof["personal_planets"])


def test_akashic_without_birth_time(app, user, prompts):
    pid = _person(app, user["id"], unknown=True)
    app.api_akashic_interpretation(pid, user=_u(user))
    prompt = prompts[-1]
    positions = prompt.split("=== ВСИЧКИ ПОЗИЦИИ В КАРТАТА")[1].split("=== КАК СЕ ЧЕТАТ")[0]
    assert "Асцендент" not in positions and "дом" not in positions
    assert "двойката знаци на лунните възли" in prompt


def test_synastry_uses_real_cross_aspects(app, user, prompts):
    """Досега „аспекти между тях“ бяха аспектите вътре в композитната карта."""
    a = _person(app, user["id"], name="Мария Петрова")
    b = _person(app, user["id"], name="Иван Иванов", y=1987, m=11, d=3, h=21, mi=10)
    app.api_synastry_interpretation(app.SynastryRequest(person1_id=a, person2_id=b), user=_u(user))
    prompt = prompts[-1]
    assert "Основни аспекти между тях" not in prompt
    cross = prompt.split("=== АСПЕКТИ МЕЖДУ ДВЕТЕ КАРТИ")[1].split("===")[1]
    assert " на Мария " in cross and " на Иван" in cross
    assert "КОМПОЗИТНА КАРТА" in prompt
    assert "Петрова" not in prompt and "Иванов" not in prompt
    assert "„вие“" in prompt


def test_synastry_drops_ascendant_of_person_without_time(app, user, prompts):
    a = _person(app, user["id"], name="Мария Петрова", unknown=True)
    b = _person(app, user["id"], name="Иван Иванов", y=1987, m=11, d=3, h=21, mi=10)
    app.api_synastry_interpretation(app.SynastryRequest(person1_id=a, person2_id=b), user=_u(user))
    prompt = prompts[-1]
    first = prompt.split("ПЪРВИ ЧОВЕК")[1].split("ВТОРИ ЧОВЕК")[0]
    assert "Асцендент" not in first and "дом" not in first
    assert "Часът на раждане на Мария е неизвестен" in prompt


def test_love_partner_without_time(app, user, prompts):
    pid = _person(app, user["id"])
    req = app.LoveMatchRequest(person_id=pid, partner_name="Иван", partner_year=1987, partner_month=11,
                               partner_day=3, partner_lat=42.1, partner_lon=24.7, partner_time_unknown=True)
    m = app.resolve_love_match(req, app.get_person(pid, user["id"]))
    assert all("Асцендент" not in p["label"] for p in m["partner_points"])
    assert all(not p["house_bg"] for p in m["partner_points"])
    app.api_love_match_interpretation(req, user=_u(user))
    assert "асцендент и домове не са известни" in prompts[-1]


def test_period_prompt_has_bulgarian_aspect_names(app, user, prompts):
    pid = _person(app, user["id"])
    app.api_period_interpretation(app.PeriodRequest(person_id=pid, start_date="2026-10-01",
                                                    end_date="2026-10-31"), user=_u(user))
    events = prompts[-1].split("=== ТРАНЗИТНИ СЪБИТИЯ ПО ДНИ ===")[1].split("=== ЗАДАЧА ===")[0]
    for english in ("Square", "Trine", "Conjunction", "Opposition", "Sextile", "Saturn", "Moon"):
        assert english not in events


def test_saving_the_same_birth_data_keeps_paid_readings(app, user):
    """„Запази“ без промяна триеше платените разчитания и пускаше ново генериране."""
    pid = _person(app, user["id"])
    app.set_ai_cache(pid, "profile", "Платено разчитане")
    same = app.BirthDataUpdate(year=1990, month=5, day=14, hour=8, minute=30, lat=42.7, lon=23.3)
    app.api_natal_chart_update(pid, same, user=_u(user))
    assert app.get_ai_cache(pid, "profile")
    changed = app.BirthDataUpdate(year=1990, month=5, day=14, hour=9, minute=30, lat=42.7, lon=23.3)
    app.api_natal_chart_update(pid, changed, user=_u(user))
    assert not app.get_ai_cache(pid, "profile")


def test_marking_time_unknown_stores_noon(app, user):
    pid = _person(app, user["id"])
    upd = app.BirthDataUpdate(year=1990, month=5, day=14, hour=3, minute=15, lat=42.7, lon=23.3,
                              time_unknown=True)
    chart = app.api_natal_chart_update(pid, upd, user=_u(user))
    p = app.get_person(pid, user["id"])
    assert (p["hour"], p["minute"], p["time_unknown"]) == (12, 0, 1)
    assert chart["time_unknown"] is True


def test_guest_chart_without_time(app):
    class Req:
        headers, cookies, query_params = {"host": "astrokarta.bg"}, {}, {}
        client = type("C", (), {"host": "9.9.9.9"})()
    res = app.api_guest_chart(app.GuestChartRequest(name="Гост", year=1990, month=5, day=14, hour=3,
                                                    lat=42.7, lon=23.3, time_unknown=True), Req())
    assert res["profile"]["core"]["ascendant"] is None
    assert res["chart"]["native"]["datetime"].endswith("12:00")


@pytest.mark.parametrize("raw,expected", [
    ("Виж https://astrokarta.bg/horoskop за повече", "Виж https://astrokarta.bg/horoskop за повече"),
    ("**Слънце:** в Овен", "**Слънце:** в Овен"),
    ("Пример:текст", "Пример: текст"),
    ("с зодия", "със зодия"),
])
def test_bg_text_keeps_links_and_bold(raw, expected):
    from bg_text import clean_bg
    assert clean_bg(raw) == expected
