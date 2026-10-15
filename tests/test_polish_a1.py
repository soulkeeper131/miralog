# -*- coding: utf-8 -*-
"""Пакет A1: началната и лендингите — цена на платените, честни бутони,
вписан без „гост“ останки, общо меню и футър, заглавия до 60 знака."""
import asyncio
import re

import pytest
from starlette.requests import Request

from tests.test_conversion import _person, _token

PAID = ["astrologicheski-profil", "horoskop-za-period", "lyubovna-savmestimost",
        "akashovi-zapisi", "numerologia", "lunen-kalendar"]


def _req(path="/", token=None):
    headers = [(b"host", b"astrokarta.bg")]
    if token:
        headers.append((b"cookie", f"miralog_token={token}".encode()))
    return Request({"type": "http", "method": "GET", "path": path, "headers": headers,
                    "query_string": b"", "scheme": "https", "server": ("astrokarta.bg", 443),
                    "client": ("1.2.3.4", 1)})


def _page(app, slug, req=None):
    page = app.FEATURE_PAGES_BY_SLUG[slug]
    req = req or _req("/" + slug)
    return app.templates.get_template("feature_landing.html").render(
        {"request": req, **app._feature_context(req, page)})


def _visible(html):
    """Видимата част: без <head> (meta описанията са за търсачките), скриптове и стилове."""
    html = html.split("<body", 1)[-1]
    return re.sub(r"<(script|style).*?</\1>", "", html, flags=re.S)


# --- цената на платените лендинги ---------------------------------------------------

@pytest.mark.parametrize("slug", PAID)
def test_every_paid_landing_shows_its_price_once_and_a_cost_faq(app, slug):
    page = app.FEATURE_PAGES_BY_SLUG[slug]
    price = app.price_label(app.feature_offer(page["key"])["price_cents"])
    html = _page(app, slug)
    hero = html[html.index('class="hero'):html.index('Какво ще узнаеш')]
    assert price in hero and "Еднократно плащане" in hero
    # цената е преди първия бутон, не след него
    assert hero.index(price) < hero.index('class="btn btn-primary"')
    assert "Колко струва?" in html
    assert f'Отключи' in hero and f"за {price}" in hero


def test_price_comes_from_the_database_not_from_the_text(app):
    import sqlite3
    with sqlite3.connect(app.DB_PATH) as c:
        was = c.execute("SELECT price_cents FROM feature_prices WHERE feature_key='moon'").fetchone()[0]
        c.execute("UPDATE feature_prices SET price_cents = 1234 WHERE feature_key='moon'")
        c.commit()
    try:
        html = _page(app, "lunen-kalendar")
        assert "12,34 €" in html and "Отключи календара за 12,34 €" in html
        assert "2,99" not in html
    finally:
        with sqlite3.connect(app.DB_PATH) as c:
            c.execute("UPDATE feature_prices SET price_cents = ? WHERE feature_key='moon'", (was,))
            c.commit()


def test_related_tags_are_prices_for_paid_and_free_for_included(app):
    html = _page(app, "numerologia")           # свързани: карта (безплатна), профил, акаши
    related = html[html.index('class="related"'):]
    assert related.count("Безплатно") == 1
    for key in ("profile", "akashic"):
        assert app.landing_pricing()["by_key"][key] in related


def test_free_landing_has_no_price_line(app):
    html = _page(app, "natalna-karta")
    assert "price-line" not in html.split("<main>")[1] and "Колко струва?" not in html.replace("Колко струва наталната карта?", "")


def test_a_landing_has_three_buttons_at_most(app):
    for slug in ("numerologia", "natalna-karta"):
        main = _page(app, slug).split("<main>")[1]
        assert main.count('class="btn btn-primary"') <= 3


# --- луната и нумерологията --------------------------------------------------------

def test_moon_button_for_a_guest_goes_to_start_never_to_login(app):
    html = _page(app, "lunen-kalendar")
    assert app.cta_for(app.viewer_info(_req()), "moon")["href"] == "/start?want=moon"
    assert 'href="/start?want=moon"' in html
    assert 'href="/moon"' not in html


def test_logged_in_moon_button_is_the_unlock(app, user):
    pid = _person(app, user["id"])
    html = _page(app, "lunen-kalendar", _req("/lunen-kalendar", _token(app, user)))
    assert f'href="/chart/{pid}?unlock=moon"' in html and "за 2,99 €" in html


def test_numerology_is_honest_about_the_free_chart_first(app):
    html = _visible(_page(app, "numerologia"))
    assert "Първо безплатната ти карта (дата, час, място), после нумерологията" in html
    assert "Без час, без място" not in html and "Нужна е само датата" not in html


# --- вписан --------------------------------------------------------------------------

def test_logged_in_landing_has_no_guest_leftovers(app, user):
    _person(app, user["id"])
    tok = _token(app, user)
    html = _page(app, "akashovi-zapisi", _req("/akashovi-zapisi", tok))
    body = _visible(html).split("<main>")[1]
    nav = _visible(html).split("</nav>")[0]
    assert ">Вход<" not in html.split("<footer")[0] and "Моите карти" in nav
    assert "Отнема минута" not in body and "Безплатна — нужни са" not in body
    assert 'href="/#ceni" data-cta="feature_akashic:all"' not in body    # ghost само за гост
    assert "/login" not in body


def test_guest_landing_keeps_the_pricing_shortcut(app):
    html = _page(app, "akashovi-zapisi")
    assert 'href="/#ceni" data-cta="feature_akashic:all"' in html and ">Вход<" in html


def test_home_pricing_is_reachable_for_a_logged_in_visitor(app):
    # Началната не пренасочва към таблото, когато адресът е /#ceni.
    html = asyncio.run(app.index(_req())).body.decode()
    assert "window.location.hash !== '#ceni'" in html


def test_home_for_logged_in_has_no_login_or_minute_block(app, user):
    _person(app, user["id"])
    html = asyncio.run(app.index(_req("/", _token(app, user)))).body.decode()
    body = _visible(html).split("<main>")[1]
    assert "Вход" not in body.split("</main>")[0]
    assert "Отнема минута" not in body and "Нужни са само дата" not in body


# --- началната -----------------------------------------------------------------------

def _home(app):
    return asyncio.run(app.index(_req())).body.decode()


def test_home_has_one_list_of_readings_before_the_prices(app):
    html = _home(app)
    assert "Какво ще откриеш" not in html and "cards--rail" not in html
    assert html.index('id="ceni"') < html.index("Всички разчитания</h2>")


def test_home_unlock_buttons_carry_the_price_and_a_free_chart_line(app):
    html = _home(app)
    block = html[html.index('id="ceni"'):]
    for row in app.landing_pricing()["rows"]:
        assert f"Отключи за {row['price']}" in block
    assert "Първо виждаш безплатната си карта — отнема 1 минута" in block


def test_home_jargon_is_gone_and_swiss_ephemeris_appears_once(app):
    text = _visible(_home(app))
    assert text.count("Swiss Ephemeris") == 1
    assert "Плацидус" not in text and "орб" not in text
    assert "своя натална" not in text and "Създай своя " not in text


def test_home_has_no_second_hero_button_and_one_name_for_the_main_one(app):
    hero = _home(app).split('class="cta-row"')[1].split("</div>")[0]
    assert hero.count("<a ") == 1 and "Създай своята натална карта" in hero


def test_nav_says_horoscope_by_sign_and_has_a_mobile_short_form(app):
    nav = _home(app).split('<nav class="site-nav')[1].split("</nav>")[0]
    assert 'href="/horoskop"><span class="nav-full">Хороскоп по зодия' in nav
    assert "Дневен хороскоп" not in nav
    assert nav.count("nav-short") >= 3


def test_mobile_nav_and_theme_toggle_css(app):
    css = open("templates/_critical.html", encoding="utf-8").read()
    assert "flex-wrap:nowrap" in css and ".theme-toggle{position:absolute" in css
    landing = open("static/landing.min.css", encoding="utf-8").read()
    assert "line-clamp" not in landing.split(".mod-lede")[1].split("closing cta")[0]


# --- футър, правни страници ------------------------------------------------------------

@pytest.mark.parametrize("slug", ["numerologia", "natalna-karta"])
def test_landings_use_the_shared_footer(app, slug):
    html = _page(app, slug)
    assert 'class="site-footer"' in html and "Условия за ползване" in html
    assert html.count("<footer") == 1


def test_home_uses_the_shared_footer(app):
    html = _home(app)
    assert 'class="site-footer"' in html and html.count("<footer") == 1


@pytest.mark.parametrize("name", ["terms", "privacy"])
def test_legal_pages_have_the_menu_and_the_footer(app, name):
    fn = app.terms_page if name == "terms" else app.privacy_page
    html = asyncio.run(fn(_req("/" + name))).body.decode()
    assert 'class="site-nav' in html and 'class="site-footer"' in html
    assert "mailto:[[" not in html


# --- заглавия, имена, формулировки -----------------------------------------------------

def test_every_landing_title_fits_in_google(app):
    for page in app.FEATURE_PAGES:
        title = page["seo_title"].replace("{brand}", app.brand_name())
        assert len(title) <= 60, (page["slug"], len(title))


def test_wording_is_unified_in_my_files(app):
    for page in app.FEATURE_PAGES:
        html = _visible(_page(app, page["slug"]))
        assert "Проверете" not in html and "Неговата" not in html
        assert "своя натална" not in html
        assert html.count("Swiss Ephemeris") <= 1
    love = _visible(_page(app, "lyubovna-savmestimost"))
    assert "Венерата на партньора" in love
    # шаблонните подзаглавия не лъжат за нумерология и луна
    for slug in ("numerologia", "lunen-kalendar"):
        html = _visible(_page(app, slug))
        assert "всяко от тях се чете заедно с картата ти" not in html
        assert "изведени от конкретни позиции" not in html
