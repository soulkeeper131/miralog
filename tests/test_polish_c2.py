# -*- coding: utf-8 -*-
"""Табло, настройки, луна, синастрия и PDF: поздрав без имейл, лимит на картите,
/synastry води към таба „Любов“, PDF с български знаци и име на файла."""
import re
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def _client(app):
    import app as app_module
    return TestClient(app_module.app)


def _bearer(app, user):
    return {"Authorization": "Bearer " + app.create_token(user["id"], user["email"])}


def _person(app, user, name="Мария", **kw):
    with sqlite3.connect(app.DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO persons (user_id, name, year, month, day, hour, minute, lat, lon, timezone)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user["id"], name, kw.get("year", 1990), 5, 17, 12, 0, 42.7, 23.3, "Europe/Sofia"))
        c.commit()
        return cur.lastrowid


# --- поздрав -------------------------------------------------------------------

def test_dashboard_never_greets_with_the_email():
    html = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert "userEmail" not in html
    assert "miralog_email')" not in html.replace("removeItem('miralog_email')", "")
    assert 'id="greeting">Здравей!<' in html


def test_me_returns_the_display_name_for_the_greeting(app, user):
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE users SET display_name = 'Мария' WHERE id = ?", (user["id"],))
        c.commit()
    r = _client(app).get("/api/auth/me", headers=_bearer(app, user))
    assert r.status_code == 200 and r.json()["display_name"] == "Мария"


# --- лимит на картите ----------------------------------------------------------

def test_card_limit_message_says_what_to_do(app, user):
    msg = app.person_limit_message(user["id"], 2)
    assert msg == ("Можеш да пазиш до 2 карти. Изтрий една от таблото или отключи "
                   "„Любовен хороскоп и съвместимост“ — с него получаваш място за още една.")
    assert "Профилът" not in msg


def test_card_limit_message_when_love_is_owned(app, user):
    app.grant_feature_purchase(user["id"], "love", 0, "EUR", None)
    msg = app.person_limit_message(user["id"], 3)
    assert msg == "Можеш да пазиш до 3 карти. Изтрий една от таблото, за да добавиш нова."


def test_persons_list_exposes_the_limit_before_the_form(app, user):
    limit = app.person_limit(app.get_user_by_id(user["id"]))
    for i in range(limit):
        _person(app, user, f"Човек {i}")
    q = _client(app).get("/api/persons", headers=_bearer(app, user)).json()["quota"]
    assert q["used"] == limit and q["limit"] == limit and q["can_add"] is False
    assert q["message"].startswith(f"Можеш да пазиш до {limit} карти.")
    assert q["unlock"] == "love"


def test_creating_a_card_over_the_limit_returns_the_same_message(app, user):
    limit = app.person_limit(app.get_user_by_id(user["id"]))
    for i in range(limit):
        _person(app, user, f"Човек {i}")
    r = _client(app).post("/api/persons", headers=_bearer(app, user), data={
        "name": "Още един", "year": 1990, "month": 1, "day": 1, "hour": 12, "minute": 0,
        "lat": 42.7, "lon": 23.3, "timezone": "Europe/Sofia"})
    assert r.status_code == 402
    assert r.json()["detail"].startswith("Можеш да пазиш до")


def test_dashboard_shows_the_counter_next_to_the_add_button():
    html = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert "' от ' + q.limit" in html and "btn.disabled = true" in html


# --- /synastry -----------------------------------------------------------------

def test_synastry_redirects_to_the_love_tab(app, user):
    first = _person(app, user, "Яна")
    _person(app, user, "Асен")
    r = _client(app).get("/synastry", headers=_bearer(app, user), follow_redirects=False)
    assert r.status_code == 302
    # най-старата карта, не първата по азбучен ред
    assert r.headers["location"] == f"/chart/{first}#love"


def test_synastry_without_session_cookie_serves_the_client_redirect(app):
    r = _client(app).get("/synastry")
    assert r.status_code == 200 and "#love" in r.text and "miralog_token" in r.text


# --- PDF -----------------------------------------------------------------------

def test_pdf_title_is_the_catalogue_name(app):
    names = {f["key"]: f["name"] for f in app.FEATURE_CATALOGUE}
    assert app.reading_title("profile") == "Пълен астрологически профил"
    assert app.reading_title("numerology") == names["numerology"]
    assert app.reading_title("period:2026-10-01:2026-10-31") == names["period"]
    assert app.reading_title("love-full") == names["love"]
    assert app.reading_title("horoscope:2026-10-15") == names["horoscope"]


def test_pdf_filename_is_ascii_and_readable(app):
    import datetime
    d = datetime.date(2026, 10, 15)
    assert app.pdf_filename("Мария", "profile", d) == "AstroKarta-Astrologichen-profil-Maria-2026-10-15.pdf"
    assert app.pdf_filename("Георги Йорданов", "akashic", d).isascii()
    assert app.pdf_filename("Мария", "horoscope:2026-10-20", d).endswith("-Maria-2026-10-20.pdf")
    assert app.pdf_filename("Мария", "chart", d) == "AstroKarta-Natalna-karta-Maria-2026-10-15.pdf"
    assert app.pdf_filename("", "love-full", d).isascii()


def test_pdf_cover_has_bulgarian_signs_without_summary(app, user):
    """Без блок „summary“ корицата взима знаците от картата — не „Taurus“."""
    pid = _person(app, user, "Мария")
    person = app.get_person(pid, user["id"])
    app.set_ai_cache(pid, "profile", "Просто текст без резюме.")
    captured = {}
    real = app.build_reading_pdf

    def spy(**kw):
        captured.update(kw)
        return real(**kw)

    app.build_reading_pdf = spy
    try:
        pdf, filename = app.build_person_pdf(person, "profile")
    finally:
        app.build_reading_pdf = real
    values = " ".join(v for _, v in captured["facts"])
    for english in ("Taurus", "Aries", "Gemini", "Cancer", "Leo", "Virgo", "Libra",
                    "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces"):
        assert english not in values
    assert captured["facts"], "корицата не бива да е празна"
    assert captured["title"] == "Пълен астрологически профил"
    assert filename.isascii() and filename.startswith("AstroKarta-Astrologichen-profil-Maria-")


# --- настройки и дребни текстове -----------------------------------------------

def test_settings_copy_has_no_jargon():
    html = (ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
    for bad in ("digest по", "SMTP", "GDPR", "Какво си отключил", 'забравен"'):
        assert bad not in html, bad
    assert " confirm(" not in html and "= confirm(" not in html
    assert "Искам дневния хороскоп на имейла си" in html
    assert "Губиш и платените разчитания" in html


def test_numerology_grammar():
    import numerology
    assert "със силна интуиция" in numerology.NUMBER_MEANINGS[2]
    assert not re.search(r"(?<![А-Яа-я])с с[а-я]", " ".join(numerology.NUMBER_MEANINGS.values()))


def test_moon_text_does_not_promise_chart_degrees():
    html = (ROOT / "templates" / "moon.html").read_text(encoding="utf-8")
    assert "✓ Прави" not in html and "✓ Подходящо" in html
    assert "не зависи от твоята карта" in html


# --- изтриване на акаунт -------------------------------------------------------

def test_account_deletion_needs_the_password(app, user):
    c = _client(app)
    h = _bearer(app, user)
    r = c.request("DELETE", "/api/account", headers=h)
    assert r.status_code == 403
    r = c.request("DELETE", "/api/account", headers=h, json={"confirm": "greshna"})
    assert r.status_code == 403 and app.get_user_by_id(user["id"])
    r = c.request("DELETE", "/api/account", headers=h, json={"confirm": "parola123"})
    assert r.status_code == 200 and app.get_user_by_id(user["id"]) is None


def test_email_confirms_deletion_for_every_account(app, user):
    """Акаунт от /start без парола не можеше да се изтрие — паролата му е случайна."""
    c = _client(app)
    h = _bearer(app, user)
    r = c.request("DELETE", "/api/account", headers=h, json={"confirm": "drug@example.com"})
    assert r.status_code == 403
    r = c.request("DELETE", "/api/account", headers=h, json={"confirm": " " + user["email"].upper()})
    assert r.status_code == 200 and app.get_user_by_id(user["id"]) is None
