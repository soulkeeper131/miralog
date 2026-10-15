# -*- coding: utf-8 -*-
"""Шлифовка: множествено число на зодиите, цена с запетая, менюто на телефон."""
import asyncio

import pytest
from starlette.requests import Request


def _req(path="/", token=None):
    headers = [(b"host", b"astrokarta.bg")]
    if token:
        headers.append((b"cookie", f"miralog_token={token}".encode()))
    return Request({"type": "http", "method": "GET", "path": path, "headers": headers,
                    "query_string": b"", "scheme": "https", "server": ("astrokarta.bg", 443),
                    "client": ("1.2.3.4", 1)})


# --- множествено число на зодиите ---------------------------------------------------

EXPECTED_PLURALS = {
    "oven": "Овни", "telec": "Телци", "bliznaci": "Близнаци", "rak": "Раци",
    "lav": "Лъвове", "deva": "Деви", "vezni": "Везни", "skorpion": "Скорпиони",
    "strelec": "Стрелци", "kozirog": "Козирози", "vodoley": "Водолеи", "ribi": "Риби",
}


def test_every_sign_has_its_plural():
    from horoscope_signs import ZODIAC_SIGNS
    assert {s["slug"]: s["plural"] for s in ZODIAC_SIGNS} == EXPECTED_PLURALS


def test_sign_page_says_all_the_signs_in_plural(app):
    resp = asyncio.run(app.horoskop_sign(_req("/horoskop/oven"), "oven"))
    assert resp.status_code == 200
    html = resp.body.decode("utf-8")
    assert "Това е общото за всички Овни" in html
    assert "Това е общото за всички Овен" not in html


# --- цената с запетая ---------------------------------------------------------------

@pytest.mark.parametrize("cents, text", [
    (299, "2,99 €"),
    (2500, "25 €"),
    (690, "6,90 €"),      # не „6,9 €“
    (1234, "12,34 €"),
    (100, "1 €"),
    (0, "0 €"),
    (None, "0 €"),
])
def test_price_label_uses_a_comma(app, cents, text):
    assert app.price_label(cents) == text


def test_price_label_keeps_non_eur_currency_code(app):
    assert app.price_label(1250, "USD") == "12,50 USD"


# --- менюто на телефон ----------------------------------------------------------------

def test_nav_has_a_short_label_for_phones(app):
    html = asyncio.run(app.index(_req("/"))).body.decode("utf-8")
    nav = html[html.index('<nav class="site-nav"'):html.index("</nav>")]
    # на телефон „Любов“ и „Акашови“; на компютър пълните имена
    assert 'class="nav-short">Любов<' in nav
    assert "Любовна съвместимост" in nav
    assert ">Цени<" in nav and ">Вход<" in nav
