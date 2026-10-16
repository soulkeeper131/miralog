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


# --- SEO и скорост (одит 15.10) -------------------------------------------------

def test_trailing_slash_is_a_permanent_relative_redirect(app, db):
    c = TestClient(app.app, base_url="https://astrokarta.bg")
    r = c.get("/horoskop/?utm_source=x", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/horoskop?utm_source=x"
    r = c.get("//evil.com/", follow_redirects=False)       # не е отворено пренасочване
    assert not r.headers.get("location", "").startswith("//")


def test_favicon_ico_and_register_noindex(app, db):
    c = TestClient(app.app, base_url="https://astrokarta.bg")
    assert c.get("/favicon.ico", follow_redirects=False).status_code == 301
    assert '<meta name="robots" content="noindex">' in c.get("/register").text


def test_www_pages_point_canonical_to_the_bare_domain(app, db):
    c = TestClient(app.app, base_url="https://www.astrokarta.bg")
    html = c.get("/natalna-karta", headers={"accept-language": "bg"}).text
    assert '<link rel="canonical" href="https://astrokarta.bg/natalna-karta">' in html


def test_vav_before_v_and_f(app):
    assert app.v_before("Водолей") == "във" and app.v_before("Везни") == "във"
    assert app.v_before("Овен") == "в" and app.v_before("Рак") == "в"


def test_meta_pixel_script_waits_for_consent(app, db, monkeypatch):
    app.set_setting("fb_pixel_id", "1234567890")
    try:
        html = _home(app)
        assert "fbevents.js" not in html                  # тегли го consent.js след „Приемам“
    finally:
        app.set_setting("fb_pixel_id", "")
    assert "fbevents.js" in open("static/consent.js", encoding="utf-8").read()


def test_critical_css_holds_the_list_hero_and_toggle_rules():
    crit = open("templates/_critical.html", encoding="utf-8").read()
    assert ".hero--list .hero-glyph{width:68px" in crit
    assert "@media (min-width:601px) and (max-width:1000px){.theme-toggle{top:auto" in crit


def test_tomorrow_has_its_own_indexable_page(app, db, monkeypatch):
    import datetime
    from zoneinfo import ZoneInfo
    c = TestClient(app.app, base_url="https://astrokarta.bg")
    r = c.get("/horoskop/oven?den=utre", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/horoskop/oven/utre"
    monkeypatch.setattr(app, "get_sign_horoscope", lambda sign, day: None)
    assert c.get("/horoskop/oven/utre", follow_redirects=False).status_code == 302   # не е готов
    monkeypatch.setattr(app, "get_sign_horoscope", lambda sign, day: "## 1. Общо\n\nТекст за деня.")
    html = c.get("/horoskop/oven/utre", headers={"accept-language": "bg"}).text
    assert '<link rel="canonical" href="https://astrokarta.bg/horoskop/oven/utre">' in html
    assert "noindex" not in html and "Хороскоп за Овен утре" in html
    assert "/horoskop/oven/utre" in c.get("/sitemap.xml").text


def test_public_fonts_have_cyrillic():
    import glob
    for f in glob.glob("templates/*.html"):
        s = open(f, encoding="utf-8").read()
        assert "family=Fraunces" not in s and "family=Outfit" not in s, f
