# -*- coding: utf-8 -*-
"""Началната страница: цените и пакетът се виждат рано, идват от базата и
всеки бутон води към /start?want=<ключ>."""
import asyncio
import re
import sqlite3

import pytest
from starlette.requests import Request


def _req(path="/", token=None):
    headers = [(b"host", b"astrokarta.bg")]
    if token:
        headers.append((b"cookie", f"miralog_token={token}".encode()))
    return Request({"type": "http", "method": "GET", "path": path, "headers": headers,
                    "query_string": b"", "scheme": "https", "server": ("astrokarta.bg", 443),
                    "client": ("1.2.3.4", 1)})


def _home(app, req=None):
    return asyncio.run(app.index(req or _req())).body.decode()


def _pricing_block(html):
    """Секцията „ceni“ — до следващата <section>."""
    start = html.index('id="ceni"')
    return html[start:html.index("</section>", start)]


@pytest.fixture
def price_of(app):
    """Сменя цена в базата и я връща след теста (базата е обща за тестовете)."""
    saved = {}

    def setter(key, cents):
        with sqlite3.connect(app.DB_PATH) as c:
            if key not in saved:
                saved[key] = c.execute("SELECT price_cents FROM feature_prices WHERE feature_key = ?",
                                       (key,)).fetchone()[0]
            c.execute("UPDATE feature_prices SET price_cents = ? WHERE feature_key = ?", (cents, key))
            c.commit()
    yield setter
    with sqlite3.connect(app.DB_PATH) as c:
        for key, cents in saved.items():
            c.execute("UPDATE feature_prices SET price_cents = ? WHERE feature_key = ?", (cents, key))
        c.commit()


# --- място и ред -------------------------------------------------------------------

def test_pricing_section_comes_before_the_all_readings_grid(app):
    html = _home(app)
    assert html.count('id="ceni"') == 1
    assert html.index('id="sample"') < html.index('id="ceni"') < html.index("Всички разчитания</h2>")


def test_bundle_card_comes_before_the_first_module_card(app):
    block = _pricing_block(_home(app))
    assert block.index('class="bundle"') < block.index('class="price-card"')
    assert "Всички 6 разчитания" in block
    # безплатната стъпка е първа
    assert block.index('class="entry"') < block.index('class="bundle"')


def test_old_price_table_is_gone(app):
    html = _home(app)
    assert "price-table" not in html and 'id="plans"' not in html


# --- бутоните ----------------------------------------------------------------------

def test_every_sellable_module_has_a_deep_link_and_a_tracked_button(app):
    html = _home(app)
    block = _pricing_block(html)
    rows = app.landing_pricing()["rows"]
    assert len(rows) >= 2
    for row in rows:
        assert f'href="/start?want={row["key"]}"' in block
        assert f'data-cta="landing_price:{row["key"]}"' in block
    assert 'href="/start?want=bundle"' in block and 'data-cta="landing_price:bundle"' in block


def test_logged_in_buttons_go_straight_to_the_chart(app, user):
    from tests.test_conversion import _person, _token  # същите помощници
    pid = _person(app, user["id"])
    block = _pricing_block(_home(app, _req(token=_token(app, user))))
    assert f'href="/chart/{pid}?unlock=bundle"' in block
    assert f'href="/chart/{pid}?unlock=akashic"' in block
    assert "/start?want=" not in block


# --- цените са от базата -------------------------------------------------------------

def test_prices_follow_the_database(app, price_of):
    price_of("akashic", 1234)
    html = _home(app)
    block = _pricing_block(html)
    assert "12.34 €" in block                      # картата на модула
    assert "12.34 €" in html.split("Всички разчитания</h2>")[1]   # и етикетът в мрежата
    # пакетът пази спестяването спрямо новата сума
    b = app.landing_pricing()["bundle"]
    assert b["full_price"] in block and b["saving"] in block and b["price"] in block


def test_bundle_disappears_when_it_no_longer_saves_money(app, price_of):
    for key in ("profile", "period", "love", "akashic", "numerology", "moon"):
        price_of(key, 100)
    html = _home(app)
    assert app.landing_pricing()["bundle"] is None
    assert 'class="bundle"' not in html and "/start?want=bundle" not in html
    assert "всички за" not in html                 # и от реда в героя


def test_no_paid_badge_but_the_price(app):
    html = _home(app)
    assert "Платено" not in html
    grid = html.split("Всички разчитания</h2>")[1].split("Дневен хороскоп по зодия")[0]
    pricing = app.landing_pricing()
    for key, price in pricing["by_key"].items():
        # всеки платен модул носи цената си като етикет
        assert re.search(r'class="mod-tag price">\s*' + re.escape(price) + r"\s*<", grid)
    assert grid.count('class="mod-tag free">Безплатно<') == 4   # картата, планети, аспекти, хороскоп


# --- героят, менюто, измерването ------------------------------------------------------

def test_hero_line_has_cheapest_and_bundle_price(app, price_of):
    price_of("moon", 199)
    html = _home(app)
    hero = html[html.index('class="hero"'):html.index('class="ephemeris"')]
    b = app.landing_pricing()["bundle"]
    assert "разчитания от 1.99 €" in hero
    assert f"всички за {b['price']}" in hero
    assert 'href="#ceni"' in hero and 'data-cta="landing_hero:prices"' in hero


def test_nav_has_a_prices_link_on_public_pages(app):
    assert 'href="/#ceni"' in _home(app)
    page = app.FEATURE_PAGES_BY_SLUG["akashovi-zapisi"]
    html = app.templates.get_template("feature_landing.html").render(
        {"request": _req("/akashovi-zapisi"), **app._feature_context(_req("/akashovi-zapisi"), page)})
    assert 'href="/#ceni"' in html


def test_pricing_view_event_is_sent_once_when_the_section_shows(app):
    html = _home(app)
    assert "IntersectionObserver" in html and "pricing_view" in html and "io.disconnect()" in html
    assert "pricing_view" in app.CLIENT_EVENTS
