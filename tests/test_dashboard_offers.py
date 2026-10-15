# -*- coding: utf-8 -*-
"""Таблото и картата за вписани: място вместо координати, оферта за пакета."""
import asyncio
import re
import sqlite3
from pathlib import Path

from starlette.requests import Request

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"


def _req(path="/", token=None):
    headers = [(b"host", b"astrokarta.bg")]
    if token:
        headers.append((b"cookie", f"miralog_token={token}".encode()))
    return Request({"type": "http", "method": "GET", "path": path, "headers": headers,
                    "query_string": b"", "scheme": "https", "server": ("astrokarta.bg", 443),
                    "client": ("1.2.3.4", 1)})


class _OnboardReq:
    """Достатъчно от Request, колкото ползва api_onboard."""
    headers = {"host": "astrokarta.bg"}
    base_url = "https://astrokarta.bg/"
    url = type("U", (), {"scheme": "https"})()

    class client:
        host = "1.2.3.4"


def _create(app, user, name="Мария", place=None, **extra):
    """api_create_person се вика директно: пропуснатите Form(...) аргументи
    тогава са обекти, а не стойности, затова се подават всички."""
    return app.api_create_person(
        name=name, year=1990, month=5, day=14, hour=8, minute=30,
        lat=43.2141, lon=27.9147, timezone="Europe/Sofia",
        time_unknown=extra.get("time_unknown", False), place=place,
        user=(user["id"], user["email"]))


def _place_of(app, pid):
    with sqlite3.connect(app.DB_PATH) as c:
        return c.execute("SELECT place FROM persons WHERE id = ?", (pid,)).fetchone()[0]


# --- колоната и записът -----------------------------------------------------------

def test_migration_adds_place_column(app):
    with sqlite3.connect(app.DB_PATH) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(persons)").fetchall()]
    assert "place" in cols


def test_migration_is_safe_on_an_old_database(app, tmp_path, monkeypatch):
    """Стара база без `place`: init_db я добавя и не губи картите."""
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as c:
        c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE,"
                  " password_hash TEXT)")
        c.execute("CREATE TABLE persons (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,"
                  " name TEXT NOT NULL, year INTEGER NOT NULL, month INTEGER NOT NULL, day INTEGER NOT NULL,"
                  " hour INTEGER DEFAULT 0, minute INTEGER DEFAULT 0, lat REAL NOT NULL, lon REAL NOT NULL,"
                  " timezone TEXT DEFAULT 'Europe/Sofia', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO persons (user_id, name, year, month, day, lat, lon) VALUES (1, 'Стар', 1980, 1, 1, 42.7, 23.3)")
        c.commit()
    monkeypatch.setattr(app, "DB_PATH", old)
    app.init_db()
    with sqlite3.connect(old) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(persons)").fetchall()]
        row = c.execute("SELECT name, place FROM persons").fetchone()
    assert "place" in cols
    assert row == ("Стар", None)


def test_create_person_with_and_without_place(app, user):
    with_place = _create(app, user, "Варненец", place="  Варна  ")
    without = _create(app, user, "Без място", place=None)
    assert _place_of(app, with_place["id"]) == "Варна"      # подрязано
    assert _place_of(app, without["id"]) is None            # старите клиенти не го пращат


def test_place_is_cleaned_and_capped(app):
    assert app.clean_place(None) is None
    assert app.clean_place("   ") is None
    assert app.clean_place("Нова   Загора") == "Нова Загора"
    assert len(app.clean_place("а" * 300)) == 80


def test_onboard_stores_place(app, db):
    data = app.OnboardRequest(
        email="varna-onb@example.com", password="parola123", name="Тест",
        year=1990, month=5, day=14, hour=8, minute=30, lat=43.2, lon=27.9,
        timezone="Europe/Sofia", place="Варна")
    res = app.api_onboard(data, _OnboardReq())
    assert _place_of(app, res["person_id"]) == "Варна"


def test_onboard_still_works_without_place(app, db):
    data = app.OnboardRequest(
        email="no-place-onb@example.com", password="parola123", name="Тест",
        year=1990, month=5, day=14, hour=8, minute=30, lat=43.2, lon=27.9,
        timezone="Europe/Sofia")
    res = app.api_onboard(data, _OnboardReq())
    assert _place_of(app, res["person_id"]) is None


def test_listing_returns_place(app, user):
    pid = _create(app, user, place="Пловдив")["id"]
    persons = app.api_list_persons(user=(user["id"], user["email"]))["persons"]
    assert [p["place"] for p in persons if p["id"] == pid] == ["Пловдив"]


def test_changing_coordinates_drops_a_stale_place(app, user):
    """Старото име не бива да остане при други координати."""
    pid = _create(app, user, place="Варна")["id"]

    def update(**kw):
        base = dict(year=1990, month=5, day=14, hour=8, minute=30,
                    lat=43.2141, lon=27.9147, timezone="Europe/Sofia")
        base.update(kw)
        assert app.update_person(pid, user["id"], app.BirthDataUpdate(**base))
        return _place_of(app, pid)

    assert update() == "Варна"                                  # същите координати
    assert update(lat=42.7, lon=23.3) is None                   # други, без ново име
    assert update(lat=42.1, lon=24.7, place="Пловдив") == "Пловдив"


# --- таблото не взима запомнения избор -------------------------------------------

def test_features_peek_keeps_the_pending_choice(app, user):
    """Таблото гледа /api/features?peek=1: картата още трябва да види избора."""
    app.set_setting(f"pending_purchase_{user['id']}", '{"keys": ["akashic"], "bundle": false}')
    who = (user["id"], user["email"])
    assert app.api_my_features(peek=True, user=who)["pending"] is None
    assert app.api_my_features(peek=False, user=who)["pending"] == {"keys": ["akashic"], "bundle": False}
    assert app.api_my_features(peek=False, user=who)["pending"] is None    # веднъж


def test_features_offer_what_the_dashboard_needs(app, user):
    data = app.api_my_features(peek=True, user=(user["id"], user["email"]))
    sellable = [c for c in data["catalogue"] if not c.get("included")]
    assert {"key", "name", "glyph", "unlocked", "offer"} <= set(sellable[0])
    assert data["bundle"] and {"price_cents", "full_price_cents", "keys", "name"} <= set(data["bundle"])


# --- шаблоните -------------------------------------------------------------------

def test_dashboard_template_never_prints_coordinates():
    html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    # JS не бива да слепва p.lat/p.lon за показване (формата за добавяне ги
    # праща, но не ги показва)
    assert not re.search(r"\$\{\s*p\.lat\s*\}|\$\{\s*p\.lon\s*\}", html)
    assert "p.place" in html and "escapeHtml(p.place)" in html
    # „Днес“ към дневния хороскоп и данни за отчета на бутоните
    assert "#horoscope" in html and 'data-cta="dashboard:today"' in html
    assert "dashboard:bundle" in html and "dashboard:unlock_" in html and "dashboard:open_" in html
    assert "/api/features?peek=1" in html
    assert "?unlock=" in html and "consent.js" in html


def test_chart_page_shows_place_not_coordinates(app, user):
    pid = _create(app, user, place="Варна <b>")["id"]
    tok = app.create_token(user["id"], user["email"])
    html = asyncio.run(app.view_chart(_req(f"/chart/{pid}", tok), pid)).body.decode()
    meta = re.search(r'<div class="chart-meta">(.*?)</div>', html, re.S).group(1)
    assert "Варна &lt;b&gt;" in meta            # екранирано
    assert "43.2141" not in meta and "27.9147" not in meta


def test_chart_page_without_place_prints_no_coordinates(app, user):
    pid = _create(app, user, place=None)["id"]
    tok = app.create_token(user["id"], user["email"])
    html = asyncio.run(app.view_chart(_req(f"/chart/{pid}", tok), pid)).body.decode()
    meta = re.search(r'<div class="chart-meta">(.*?)</div>', html, re.S).group(1)
    assert "43.2141" not in meta and "27.9147" not in meta
    assert "•  •" not in meta                    # без празна точка-разделител


def test_chart_template_has_the_bundle_strip():
    html = (TEMPLATES / "chart.html").read_text(encoding="utf-8")
    assert 'id="bundleStrip"' in html and "hidden" in html.split('id="bundleStrip"')[1][:40]
    assert "unlockModule('bundle')" in html
    assert 'data-cta="chart:bundle_strip"' in html
    assert "renderBundleStrip(data)" in html
    # само при оферта и поне две заключени
    assert "!b || locked.length < 2" in html
    assert "consent.js" in html
    # лентата е под менюто, преди първия панел
    assert html.index('id="secGrid"') < html.index('id="bundleStrip"') < html.index('id="tab-wheel"')


def test_start_sends_the_place_name():
    """Госът записва картата със името на мястото; таблото го показва."""
    html = (TEMPLATES / "start.html").read_text(encoding="utf-8")
    assert "place: picked.place" in html
    dash = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    assert "fd.append('place'" in dash and "formData.append('place'" in dash
