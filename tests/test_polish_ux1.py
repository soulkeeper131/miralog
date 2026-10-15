# -*- coding: utf-8 -*-
"""Фаза 2, пакет ux1: картата е лека за обикновения човек — режим
„Лесно / За напреднали“, хороскопът е първи, планетите и аспектите са за
напреднали, колелото без линии в „Лесно“."""
import re
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
CHART = (ROOT / "templates" / "chart.html").read_text(encoding="utf-8")


def _person(app, user_id, unknown=False):
    with sqlite3.connect(app.DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO persons (user_id, name, year, month, day, hour, minute, lat, lon,"
            " timezone, time_unknown, place) VALUES (?, 'Мария', 1990, 5, 14, ?, 30, 42.7, 23.3,"
            " 'Europe/Sofia', ?, 'София')", (user_id, 12 if unknown else 14, int(unknown)))
        c.commit()
        return cur.lastrowid


def _page(app, user, pid):
    client = TestClient(app.app)
    tok = app.create_token(user["id"], user["email"])
    client.cookies.set("miralog_token", tok)
    return client.get(f"/chart/{pid}").text


def test_html_carries_the_mode_and_defaults_to_easy(app, user):
    html = _page(app, user, _person(app, user["id"]))
    assert '<html lang="bg" data-mode="easy">' in html
    assert 'data-mode="easy"' in html and 'data-mode="pro"' in html     # сегментният контрол
    assert "/api/account/ui-mode" in html and "miralog_mode" in html


def test_pro_mode_comes_from_the_account(app, user):
    with sqlite3.connect(app.DB_PATH) as c:
        try:
            c.execute("ALTER TABLE users ADD COLUMN ui_mode TEXT DEFAULT 'easy'")
        except sqlite3.OperationalError:
            pass          # колоната вече я е добавил другият пакет
        c.execute("UPDATE users SET ui_mode = 'pro' WHERE id = ?", (user["id"],))
        c.commit()
    html = _page(app, user, _person(app, user["id"]))
    assert '<html lang="bg" data-mode="pro">' in html


def test_planets_and_aspects_are_advanced_only(app, user):
    html = _page(app, user, _person(app, user["id"]))
    strip = html.split('id="tabStrip"')[1].split("</div>\n        <button")[0]
    for name in ("planets", "aspects"):
        assert re.search(rf'class="tab adv-only" onclick="showTab\(\'{name}\'\)"', strip), name
    assert "html[data-mode=\"easy\"] .adv-only { display: none !important; }" in html
    # таблицата с планети и сбитите аспекти са в страницата за „За напреднали“
    assert 'class="ptable' in html and 'id="aspList"' in html
    assert 'data-asp-filter="major"' in html and "Още точки" in html
    assert "Дължина" not in html.split('id="tab-planets"')[1].split('id="tab-aspects"')[0]


def test_horoscope_is_the_first_tab_and_tiles_follow_the_strip(app, user):
    html = _page(app, user, _person(app, user["id"]))
    strip = html.split('id="tabStrip"')[1]
    order = re.findall(r"showTab\('(\w+)'\)", strip)
    assert order[0] == "horoscope" and order[1] == "wheel"
    # плочките се строят от табовете, значи следват същия ред
    assert "querySelectorAll('#tabStrip .tab')" in html


def test_the_bundle_strip_is_only_on_the_card_tab(app, user):
    html = _page(app, user, _person(app, user["id"]))
    assert '<body data-tab="wheel">' in html
    assert 'body:not([data-tab="wheel"]) .bundle-strip { display: none; }' in html
    assert 'class="nav-modules"' not in html          # „✦ Разчитания“ горе вече я няма


def test_daily_reading_layout_and_disclaimer():
    assert "С информативна и развлекателна цел — не е медицински или финансов съвет." in CHART
    assert "Прочети цялото разчитане" in CHART and "Есенцията" in CHART
    # заглавията на разделите се търсят по име, не по номер
    assert "/есенцията/i" in CHART and "/какво да направиш/i" in CHART
    assert "📖 Твоето разчитане" not in CHART and "✨ Ново разчитане" not in CHART
    # действията са ред икони под текста
    assert CHART.index("readingActions(data)") < CHART.index("DAILY_DISCLAIMER + '</p>'")


def test_chart_svg_marks_the_aspect_lines():
    from chart_svg import generate_chart_svg
    chart = {
        "native": {"name": "Т", "datetime": "1990-05-12 14:30"},
        "objects": {
            "1": {"name": "Sun", "sign": "Taurus", "sign_longitude": "21°28'54\"", "movement": "Direct"},
            "2": {"name": "Moon", "sign": "Sagittarius", "sign_longitude": "20°36'16\"", "movement": "Direct"},
        },
        "aspects": [{"active": "Sun", "passive": "Moon", "type": "Square"}],
    }
    svg = generate_chart_svg(chart)
    assert 'class="asp"' in svg
    assert svg.split('class="asp"')[1].split("</g>")[0].count("<line") == 1


def test_scripts_know_the_unlock_banner_and_one_bundle_on_onboarding():
    modules = (ROOT / "static" / "modules.js").read_text(encoding="utf-8")
    onboarding = (ROOT / "static" / "onboarding.js").read_text(encoding="utf-8")
    assert "setUnlockIntent" in modules and "takeUnlockIntent" in modules
    assert "Отключено: " in CHART and "започни оттук" in CHART
    assert "(b ? [] : forSale.slice(0, 1))" in onboarding and "'Продължи'" in onboarding
