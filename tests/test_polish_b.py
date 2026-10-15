# -*- coding: utf-8 -*-
"""Пакет B: пътят до акаунта — /start, вход, регистрация, нова парола."""
import datetime
import re

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def client(app):
    return TestClient(app.app)


GUEST = {"name": "Мария", "year": 1990, "month": 5, "day": 17, "hour": 14, "minute": 30,
         "lat": 42.15, "lon": 24.75, "timezone": "Europe/Sofia"}


# --- дата и име ----------------------------------------------------------------

def test_future_birth_date_is_refused_with_a_clear_message(client):
    year = datetime.date.today().year + 1
    r = client.post("/api/guest/chart", json={**GUEST, "year": year})
    assert r.status_code == 400
    assert r.json()["detail"] == "Датата е в бъдещето — провери годината."


def test_far_future_year_and_tomorrow_are_refused(app):
    for args in ((2099, 1, 1), ((datetime.date.today() + datetime.timedelta(days=2)).timetuple()[:3])):
        with pytest.raises(HTTPException) as err:
            app.validate_birth(*args, 12, 0, 42.7, 23.3, "Europe/Sofia")
        assert err.value.status_code == 400 and "бъдещето" in err.value.detail


def test_range_message_matches_the_check(app):
    with pytest.raises(HTTPException) as err:
        app.validate_birth(1700, 1, 1, 12, 0, 42.7, 23.3, "Europe/Sofia")
    assert f"между 1800 и {datetime.date.today().year}" in err.value.detail


def test_today_is_a_valid_birth_date(app):
    t = datetime.date.today()
    app.validate_birth(t.year, t.month, t.day, 0, 0, 42.7, 23.3, "Europe/Sofia")


def test_overlong_name_is_refused_on_both_paths(client):
    r = client.post("/api/guest/chart", json={**GUEST, "name": "А" * 61})
    assert r.status_code == 400 and "до 60" in r.json()["detail"]
    r = client.post("/api/onboard", json={**GUEST, "name": "А" * 61, "email": "dolgo@example.com"})
    assert r.status_code == 400


# --- колелото без час ----------------------------------------------------------

def test_wheel_without_birth_time_has_no_angles_or_houses(client, app):
    known = client.post("/api/guest/chart", json=GUEST).json()["svg"]
    assert ">AS<" in known and ">MC<" in known

    unknown = client.post("/api/guest/chart", json={**GUEST, "time_unknown": True}).json()["svg"]
    for label in (">AS<", ">MC<", ">IC<", ">DS<"):
        assert label not in unknown
    assert "1990-05-17<" in unknown and "12:00" not in unknown
    # няма и номера на домове (малките цифри 1–12 по колелото)
    assert not re.search(r'fill="#555577"', unknown)


def test_wheel_escapes_the_name(app):
    from chart_svg import generate_chart_svg
    svg = generate_chart_svg({"native": {"name": "<img src=x onerror=1>", "datetime": "1990-05-17 12:00"},
                              "objects": {}, "aspects": [], "houses": []})
    assert "<img" not in svg and "&lt;img" in svg


# --- геокодирането -------------------------------------------------------------

def _item(name, lat, lon, **addr):
    return {"lat": str(lat), "lon": str(lon), "name": name, "display_name": f"{name}, 4000, България",
            "address": {"country": "България", "country_code": "bg", **addr}}


def test_geocode_results_have_no_duplicates_postcodes_or_latin_twins(app):
    raw = [
        _item("Plovdiv", 42.15, 24.75, city="Plovdiv", postcode="4000"),        # латинско
        _item("Пловдив", 42.15, 24.75, city="Пловдив", postcode="4000", state="Пловдив област"),
        _item("Пловдив", 42.1501, 24.7501, city="Пловдив", state="Пловдив област"),   # дубъл
        _item("Пловдив", 38.0, -90.0, city="Пловдив", country="САЩ", country_code="us", state="Мисури"),
    ]
    out = app._shape_geocode_results(raw, 6, tz_lookup=lambda a, b: "Europe/Sofia")
    labels = [r["label"] for r in out]
    assert labels == ["Пловдив, България", "Пловдив, Мисури, САЩ"]     # България първа, без дубли
    assert not any(re.search(r"\d{4}", x) for x in labels)
    assert all(r["place"] == "Пловдив" for r in out)


def test_bulgaria_comes_first(app):
    raw = [_item("Лондон", 51.5, -0.12, city="Лондон", country="Великобритания", country_code="gb"),
           _item("Лондон", 42.0, 25.0, village="Лондон")]
    out = app._shape_geocode_results(raw, 6, tz_lookup=lambda a, b: None)
    assert out[0]["country"] == "България" and out[0]["timezone"] == "Europe/Sofia"


def test_geocode_service_failure_is_not_reported_as_unknown_place(app, monkeypatch):
    import urllib.request
    app._geocode_cache.clear()

    def boom(*a, **k):
        raise OSError("няма мрежа")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(app, "_geocode_last_call", [0.0])
    with pytest.raises(HTTPException) as err:
        app.geocode_place("Пловдив")
    assert err.value.status_code == 502 and err.value.detail == app.GEOCODE_DOWN_MSG


# --- откъсът -------------------------------------------------------------------

def test_teaser_highlights_do_not_repeat_sun_and_moon(client):
    t = client.post("/api/guest/chart", json=GUEST).json()["teaser"]
    assert len(t["highlights"]) >= 2
    labels = {h["label"] for h in t["highlights"]}
    assert not labels & {"Слънце", "Луна", "Асцендент"}
    assert [r["label"] for r in t["rows"]][:2] == ["Слънце", "Луна"]      # за въвеждането в картата


# --- страниците ----------------------------------------------------------------

def test_welcome_redirects(client):
    r = client.get("/welcome", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/start"


def test_welcome_redirects_signed_in_user_to_dashboard(client, user, app):
    token = app.create_token(user["id"], user["email"])
    r = client.get("/welcome", follow_redirects=False, cookies={"miralog_token": token})
    assert r.status_code == 302 and r.headers["location"] == "/dashboard"


def test_reset_password_without_a_link_offers_a_new_one(client):
    for url in ("/reset-password", "/reset-password?token=izmislen"):
        html = client.get(url).text
        assert 'href="/forgot-password"' in html and "Поискай нов линк" in html
        assert "<form" not in html and 'name="robots" content="noindex"' in html
        assert 'href="/login"' in html


def test_reset_password_with_a_valid_link_shows_the_form(client, user, app):
    token = app.create_password_reset(user["id"])
    html = client.get(f"/reset-password?token={token}").text
    assert 'id="form"' in html and "Запази паролата" in html
    # проверката не изразходва линка
    assert app.password_reset_valid(token)


def test_start_page_basics(client):
    html = client.get("/start").text
    assert "Swiss Ephemeris" not in html and "астрономически данни" in html
    assert 'maxlength="60"' in html and f'max="{datetime.date.today().year}"' in html
    assert 'id="saveForm"' in html and 'type="submit" class="btn" id="saveBtn"' in html
    assert 'placeholder="чч"' in html and 'placeholder="мм"' in html
    assert 'id="fHour" value=' not in html
    assert "Не знам часа" in html and "Данни" in html and "Запази я" in html
    assert 'id="stickyBar"' in html and "Какво ще узнаеш" not in html


def test_register_page_links_and_no_dead_plan(client):
    html = client.get("/register").text
    assert 'href="/start">Започни без регистрация' in html
    assert "subscribe=1" not in html and "plan=full" not in html


def test_onboard_without_password_marks_the_chart_url(client, app, db):
    r = client.post("/api/onboard", json={**GUEST, "email": "bez-parola@example.com"})
    assert r.status_code == 200
    assert r.json()["chart_url"].endswith("&saved=1")
    r = client.post("/api/onboard", json={**GUEST, "email": "s-parola@example.com", "password": "parola1234"})
    assert "saved=1" not in r.json()["chart_url"]
