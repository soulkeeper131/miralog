# -*- coding: utf-8 -*-
"""Началната: ценовите карти са оферти, а менюто се връща при скрол нагоре."""
import re

from fastapi.testclient import TestClient


def _home(app):
    c = TestClient(app.app, base_url="https://astrokarta.bg")
    r = c.get("/", headers={"accept-language": "bg"})
    assert r.status_code == 200
    return r.text


def test_price_cards_have_a_solid_button_and_the_once_note(app, db):
    html = _home(app)
    cards = re.findall(r'<li class="price-card">(.*?)</li>', html, re.S)
    assert cards, "няма ценови карти"
    for card in cards:
        assert "btn btn-primary" in card and "еднократно" in card
    # Пакетът показва глифа на всеки модул, който съдържа.
    glyphs = re.search(r'<div class="bundle-glyphs"[^>]*>(.*?)</div>', html, re.S)
    assert glyphs and glyphs.group(1).count("<span") == len(cards)


def test_site_nav_sticks_and_hides_while_scrolling_down(app, db):
    html = _home(app)
    assert "nav-away" in html and "nav-stuck" in html
    crit = open("templates/_critical.html", encoding="utf-8").read()
    assert ".site-nav{position:sticky;top:0;" in crit
    # overflow-x:hidden на body прави body скролиращ контейнер и sticky не работи.
    assert "overflow-x:clip" in crit
    css = open("static/landing.css", encoding="utf-8").read()
    assert css == open("static/landing.min.css", encoding="utf-8").read()
