# -*- coding: utf-8 -*-
"""Пътят до плащането: основният бутон според разглеждащия, дълбоки връзки,
измерване на бутоните."""
import asyncio
import sqlite3

import pytest
from starlette.requests import Request


def _req(path="/", token=None, query=b""):
    headers = [(b"host", b"astrokarta.bg")]
    if token:
        headers.append((b"cookie", f"miralog_token={token}".encode()))
    return Request({"type": "http", "method": "GET", "path": path, "headers": headers,
                    "query_string": query, "scheme": "https", "server": ("astrokarta.bg", 443),
                    "client": ("1.2.3.4", 1)})


def _person(app, user_id, name="Мария Петрова"):
    with sqlite3.connect(app.DB_PATH) as c:
        cur = c.execute("INSERT INTO persons (user_id, name, year, month, day, hour, minute, lat, lon,"
                        " timezone) VALUES (?, ?, 1990, 5, 14, 8, 30, 42.7, 23.3, 'Europe/Sofia')",
                        (user_id, name))
        c.commit()
        return cur.lastrowid


def _token(app, user):
    return app.create_token(user["id"], user["email"])


# --- основният бутон ----------------------------------------------------------

def test_guest_goes_to_start_with_the_wanted_module(app):
    g = app.viewer_info(_req())
    assert app.cta_for(g, "akashic")["href"] == "/start?want=akashic"
    assert app.cta_for(g, "bundle")["href"] == "/start?want=bundle"
    assert app.cta_for(g)["href"] == "/start"
    assert app.cta_for(g, "<script>")["href"] == "/start"           # непознат ключ се игнорира
    assert app.cta_for(g, "chart", "Виж картата")["label"] == "Виж картата"


def test_logged_in_with_one_chart_goes_straight_to_it(app, user):
    pid = _person(app, user["id"])
    v = app.viewer_info(_req(token=_token(app, user)))
    assert v["logged_in"] and v["chart_url"] == f"/chart/{pid}"
    assert app.cta_for(v)["href"] == f"/chart/{pid}"
    locked = app.cta_for(v, "akashic")
    assert locked["href"] == f"/chart/{pid}?unlock=akashic" and "Отключи" in locked["label"]
    assert app.cta_for(v, "horoscope")["href"] == f"/chart/{pid}#horoscope"   # безплатен
    assert app.cta_for(v, "bundle")["href"] == f"/chart/{pid}?unlock=bundle"


def test_owned_module_opens_its_tab(app, user):
    pid = _person(app, user["id"])
    app.grant_feature_purchase(user["id"], "akashic", 900, "EUR", None)
    app.grant_feature_purchase(user["id"], "moon", 299, "EUR", None)
    v = app.viewer_info(_req(token=_token(app, user)))
    assert app.cta_for(v, "akashic")["href"] == f"/chart/{pid}#akashic"
    assert app.cta_for(v, "moon")["href"] == "/moon"


def test_several_charts_go_to_the_dashboard(app, user):
    _person(app, user["id"])
    _person(app, user["id"], "Иван Иванов")
    v = app.viewer_info(_req(token=_token(app, user)))
    assert app.cta_for(v, "akashic")["href"] == "/dashboard"


def test_bad_token_is_just_a_guest(app):
    v = app.viewer_info(_req(token="боклук"))
    assert v["logged_in"] is False


# --- /start за вписан -----------------------------------------------------------

def test_start_sends_logged_in_people_to_their_chart(app, user):
    pid = _person(app, user["id"])
    tok = _token(app, user)
    resp = asyncio.run(app.start_page(_req("/start", tok)))
    assert resp.status_code == 302 and resp.headers["location"] == f"/chart/{pid}"
    resp = asyncio.run(app.start_page(_req("/start", tok, b"want=love")))
    assert resp.headers["location"] == f"/chart/{pid}?unlock=love"


def test_start_shows_the_form_after_oauth_claim(app, user):
    """?claim=1 е връщането от Google с гост-карта — формата трябва да се покаже."""
    _person(app, user["id"])
    resp = asyncio.run(app.start_page(_req("/start", _token(app, user), b"claim=1")))
    assert resp.status_code == 200


def test_start_for_guest_is_the_form(app):
    assert asyncio.run(app.start_page(_req("/start"))).status_code == 200


# --- шаблоните ------------------------------------------------------------------

def test_feature_landing_button_depends_on_viewer(app, user):
    page = app.FEATURE_PAGES_BY_SLUG["akashovi-zapisi"]
    render = lambda req: app.templates.get_template("feature_landing.html").render(
        {"request": req, **app._feature_context(req, page)})
    guest = render(_req("/akashovi-zapisi"))
    assert 'href="/start?want=akashic"' in guest and 'data-cta="feature_akashic:main"' in guest
    pid = _person(app, user["id"])
    mine = render(_req("/akashovi-zapisi", _token(app, user)))
    assert f'href="/chart/{pid}?unlock=akashic"' in mine and 'href="/start' not in mine


def test_horoscope_page_buttons_follow_the_viewer(app, user):
    pid = _person(app, user["id"])
    resp = asyncio.run(app.horoskop_sign(_req("/horoskop/oven", _token(app, user)), "oven"))
    html = resp.body.decode()
    assert f'href="/chart/{pid}"' in html and 'href="/start"' not in html


# --- измерване -------------------------------------------------------------------

def test_cta_clicks_are_counted_with_charts(app, db):
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("DELETE FROM events")
        c.commit()
    app.api_client_event(app.ClientEvent(event="cta_click", feature="landing:start"), _req())
    app.api_client_event(app.ClientEvent(event="cta_click", feature="landing:start"), _req())
    app.record_event("guest_chart", request=_req())
    rows = {r["place"]: r for r in app.build_reports(30)["ctas"]}
    assert rows["landing:start"]["clicks"] == 2
    assert rows["landing:start"]["people"] == 1 and rows["landing:start"]["charts"] == 1


@pytest.mark.parametrize("event", ["pricing_view", "signup_prompt_view", "onboarding_step"])
def test_new_client_events_are_allowed(app, event):
    assert app.api_client_event(app.ClientEvent(event=event, feature="x"), _req())["ok"]
