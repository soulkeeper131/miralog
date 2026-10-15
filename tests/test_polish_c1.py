# -*- coding: utf-8 -*-
"""Картата отвътре (пакет C1): аспекти без дубли и по точност, неизвестен час
без ъгли, поправка на рождените данни, превю на заключените модули по модул."""
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def _person(app, user_id, name="Мария Петрова", unknown=False, hour=14):
    with sqlite3.connect(app.DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO persons (user_id, name, year, month, day, hour, minute, lat, lon,"
            " timezone, time_unknown, place) VALUES (?, ?, 1990, 5, 14, ?, 30, 42.7, 23.3,"
            " 'Europe/Sofia', ?, 'София')",
            (user_id, name, 12 if unknown else hour, int(unknown)))
        c.commit()
        return cur.lastrowid


def _client(app, user):
    client = TestClient(app.app)
    client.headers.update({"Authorization": "Bearer " + app.create_token(user["id"], user["email"])})
    client.cookies.set("miralog_token", app.create_token(user["id"], user["email"]))
    return client


def _fake_aspect(kind, active, passive, diff):
    return SimpleNamespace(
        type=kind, _active_name=active, _passive_name=passive, orb=10.0, aspect=0.0,
        distance=SimpleNamespace(formatted=diff), difference=SimpleNamespace(formatted=diff),
        movement=None, condition=None)


# --- аспекти -------------------------------------------------------------------

def test_natal_aspects_have_no_duplicates_and_are_sorted_by_real_deviation(app):
    chart = app.compute_natal({"name": "Т", "year": 1990, "month": 5, "day": 12, "hour": 14,
                               "minute": 30, "lat": 42.69, "lon": 23.32, "timezone": "Europe/Sofia"})
    aspects = chart["aspects"]
    keys = [(frozenset((a["active"], a["passive"])), a["type"]) for a in aspects]
    assert len(keys) == len(set(keys)) and len(aspects) > 10
    devs = [a["deviation"] for a in aspects if a["deviation"] is not None]
    assert devs == sorted(devs)
    assert aspects[0]["deviation_label"].startswith(("точен до", "почти точен"))


def test_exact_label_is_a_human_phrase(app):
    assert app.exact_label(1.234) == "точен до 1,2°"
    assert app.exact_label(0.01) == "почти точен"
    assert app.exact_label(None) == ""


def test_symmetric_flag_only_for_one_persons_chart(app):
    """Транзит Слънце → натална Луна и транзит Луна → натално Слънце са различни."""
    both = {1: {2: _fake_aspect("Trine", "Sun", "Moon", "01°00'00\"")},
            2: {1: _fake_aspect("Trine", "Moon", "Sun", "01°00'00\"")}}
    assert len(app.serialize_aspects(both)) == 2
    assert len(app.serialize_aspects(both, symmetric=True)) == 1
    # Транзитите не се пипат: две посоки с еднакви имена са два различни аспекта
    # (транзитна Луна – натален Юпитер и транзитен Юпитер – натална Луна).
    same_names = {1: {2: _fake_aspect("Opposition", "Moon", "Jupiter", "00°01'48\"")},
                  3: {4: _fake_aspect("Opposition", "Moon", "Jupiter", "00°09'00\"")}}
    assert len(app.serialize_aspects(same_names)) == 2


def test_profile_key_aspects_use_the_real_deviation(app):
    chart = app.compute_natal({"name": "Т", "year": 1990, "month": 5, "day": 12, "hour": 14,
                               "minute": 30, "lat": 42.69, "lon": 23.32, "timezone": "Europe/Sofia"})
    key = app.build_profile(chart)["key_aspects"]
    orbs = [a["orb"] for a in key]
    assert orbs == sorted(orbs) and all(a["exact"] for a in key)
    assert max(orbs) < 5      # „орб 10°“ беше допустимата граница, не силата


# --- страница ------------------------------------------------------------------

def test_unknown_time_chart_page_has_no_angles_or_houses(app, user):
    pid = _person(app, user["id"], "Георги", unknown=True)
    html = _client(app, user).get(f"/chart/{pid}").text
    titles = re.findall(r'class="(?:planet|aspect)-title-name">\s*([^<]+?)\s*<', html)
    assert titles, "няма карти"
    for angle in ("Асцендент", "Десцендент", "Медиум Коели", "Имум Коели"):
        assert not any(angle in t for t in titles), angle
    assert 'class="planet-detail-label">Дом<' not in html
    assert "Раждане</div>" not in html            # „Дневно/Нощно“ зависи от часа
    assert "час неизвестен" in html


def test_known_time_chart_page_keeps_the_angles(app, user):
    pid = _person(app, user["id"])
    html = _client(app, user).get(f"/chart/{pid}").text
    titles = re.findall(r'class="planet-title-name">\s*([^<]+?)\s*<', html)
    assert "Асцендент" in titles and "Слънце" in titles
    assert titles.index("Слънце") < titles.index("Асцендент")      # планетите първи
    assert ">POINT<" not in html and "• Europe/Sofia" not in html
    assert "Поправи данните" in html


def test_profile_tab_has_no_padlock_but_moon_tab_exists(app, user):
    pid = _person(app, user["id"])
    html = _client(app, user).get(f"/chart/{pid}").text
    assert re.search(r'id="tabbtn-profile">', html)        # без data-feature: няма катинар
    assert 'href="/moon"' in html and 'data-feature="moon"' in html


# --- поправка на данните -------------------------------------------------------

def _body(**over):
    base = {"year": 1990, "month": 5, "day": 14, "hour": 14, "minute": 30,
            "lat": 42.7, "lon": 23.3, "timezone": "Europe/Sofia",
            "time_unknown": False, "place": "София"}
    base.update(over)
    return base


def _cache_rows(app, pid):
    with sqlite3.connect(app.DB_PATH) as c:
        return c.execute("SELECT COUNT(*) FROM ai_cache WHERE person_id = ?", (pid,)).fetchone()[0]


def test_fixing_birth_data_clears_the_readings(app, user):
    pid = _person(app, user["id"])
    app.set_ai_cache(pid, "profile", "стар текст")
    r = _client(app, user).post(f"/api/persons/{pid}/natal", json=_body(day=15))
    assert r.status_code == 200
    assert _cache_rows(app, pid) == 0
    saved = app.get_person(pid, user["id"])
    assert saved["day"] == 15 and saved["place"] == "София"


def test_saving_the_same_data_keeps_the_readings(app, user):
    pid = _person(app, user["id"])
    app.set_ai_cache(pid, "profile", "текст")
    r = _client(app, user).post(f"/api/persons/{pid}/natal", json=_body())
    assert r.status_code == 200
    assert _cache_rows(app, pid) == 1


def test_adding_the_birth_time_later_works_and_clears(app, user):
    pid = _person(app, user["id"], unknown=True)
    app.set_ai_cache(pid, "akashic", "текст")
    r = _client(app, user).post(f"/api/persons/{pid}/natal", json=_body(hour=9, minute=5))
    assert r.status_code == 200
    saved = app.get_person(pid, user["id"])
    assert not saved["time_unknown"] and saved["hour"] == 9
    assert _cache_rows(app, pid) == 0


def test_impossible_date_is_refused(app, user):
    pid = _person(app, user["id"])
    r = _client(app, user).post(f"/api/persons/{pid}/natal", json=_body(month=2, day=31))
    assert r.status_code == 400


# --- превю на заключените модули ----------------------------------------------

def _teaser(app, user, pid, feature=None):
    url = f"/api/persons/{pid}/teaser" + (f"?feature={feature}" if feature else "")
    r = _client(app, user).get(url)
    assert r.status_code == 200
    return r.json()


def test_numerology_teaser_is_the_life_path_number(app, user):
    pid = _person(app, user["id"])
    t = _teaser(app, user, pid, "numerology")
    expected = app.compute_numerology("Мария Петрова", 1990, 5, 14)["life_path"]["number"]
    assert t["rows"][0]["label"] == "Число на съдбата" and t["rows"][0]["position"] == str(expected)
    assert "14.05.1990" in t["intro"]


def test_love_and_akashic_teasers_are_their_own_planets(app, user):
    pid = _person(app, user["id"])
    love = _teaser(app, user, pid, "love")
    assert [r["label"].split(" ")[0] for r in love["rows"]] == ["Венера", "Марс"]
    akashic = _teaser(app, user, pid, "akashic")
    assert [r["label"].split(" ")[0] for r in akashic["rows"]] == ["Северен", "Южен"]
    assert not any("аспекта" in (t.get("more") or "") for t in (love, akashic))


def test_profile_teaser_does_not_repeat_the_free_summary(app, user):
    pid = _person(app, user["id"])
    t = _teaser(app, user, pid, "profile")
    assert t["rows"] == [] and len(t["outline"]) >= 5
    assert "аспекта" in t["more"]          # „още N аспекта“ — само в профила


def test_period_teaser_starts_from_the_next_days(app, user):
    pid = _person(app, user["id"])
    t = _teaser(app, user, pid, "period")
    assert t["rows"] and "30 дни" in t["intro"]


def test_teaser_without_a_module_stays_the_old_shape(app, user):
    """/start и onboarding четат откъса без модул."""
    pid = _person(app, user["id"])
    t = _teaser(app, user, pid)
    assert [r["label"] for r in t["rows"]][:2] == ["Слънце", "Луна"] and "aspect_count" in t


def test_teaser_hides_houses_when_the_time_is_unknown(app, user):
    pid = _person(app, user["id"], unknown=True)
    t = _teaser(app, user, pid, "love")
    assert all("дом" not in r["position"] for r in t["rows"])


# --- статичните файлове --------------------------------------------------------

@pytest.mark.skipif(not shutil.which("node"), reason="няма node")
@pytest.mark.parametrize("name", ["modules.js", "locked.js"])
def test_scripts_parse(name):
    """modules.js имаше await в неасинхронна функция и не се зареждаше изобщо."""
    r = subprocess.run(["node", "--check", str(ROOT / "static" / name)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_copy_the_audit_asked_for():
    chart = (ROOT / "templates" / "chart.html").read_text(encoding="utf-8")
    modules = (ROOT / "static" / "modules.js").read_text(encoding="utf-8")
    for old in ("Моля, изберете", "Добави разчитания", "Друг мой профил",
                "🔊 Чети", "✉ Изпрати</button>", "Доминира ${dominant}"):
        assert old not in chart, old
    assert not re.search("Подготвям(?!е)", chart)
    for new in ("🔊 Слушай", "✉ Изпрати по имейл", "Подготвяме аудиото", "Всеки с линка ще може",
                "Друг човек от списъка ми", "Подробности за напреднали"):
        assert new in chart, new
    assert "Имаш всички разчитания" in modules and "Какво още искаш да четеш" not in modules
    # едно и също съобщение след плащане на двете места
    msg = "Плащането е получено. Ако разчитането още е заключено, опресни страницата след няколко секунди."
    assert msg in chart and msg in modules
