# -*- coding: utf-8 -*-
"""Страницата /horoskop/<знак> — 90% от трафика: един основен бутон над текста,
личен блок под него (мини форма за гост, откъс за вписан), пакетът като спокоен ред."""
import asyncio
import datetime
import re
import sqlite3
from zoneinfo import ZoneInfo

import pytest
from starlette.requests import Request


def _req(token=None):
    headers = [(b"host", b"astrokarta.bg")]
    if token:
        headers.append((b"cookie", f"miralog_token={token}".encode()))
    return Request({"type": "http", "method": "GET", "path": "/horoskop/oven", "headers": headers,
                    "query_string": b"", "scheme": "https", "server": ("astrokarta.bg", 443),
                    "client": ("1.2.3.4", 1)})


def _person(app, user_id, tz="Europe/Sofia"):
    with sqlite3.connect(app.DB_PATH) as c:
        cur = c.execute("INSERT INTO persons (user_id, name, year, month, day, hour, minute, lat, lon,"
                        " timezone) VALUES (?, 'Мария Петрова', 1990, 5, 14, 8, 30, 42.7, 23.3, ?)",
                        (user_id, tz))
        c.commit()
        return cur.lastrowid


def _render(app, token=None):
    resp = asyncio.run(app.horoskop_sign(_req(token), "oven"))
    assert resp.status_code == 200
    return resp.body.decode("utf-8")


def _hero(html):
    return html[html.index('<header class="hero'):html.index("</header>")]


@pytest.fixture(autouse=True)
def _no_ai(app, monkeypatch):
    """Страницата по зодия никога не бива да вика AI — нито за гост, нито за вписан."""
    def boom(*a, **k):
        raise AssertionError("call_ai е извикан от страницата по зодия")
    monkeypatch.setattr(app, "call_ai", boom)


def test_guest_hero_has_one_primary_button(app, db):
    hero = _hero(_render(app))
    assert hero.count("btn-primary") == 1
    assert 'href="#lichen"' in hero and "Получи личния си хороскоп" in hero
    assert 'data-cta="horoscope_sign:hero"' in hero
    # „Сподели“ е малка икона с достъпно име, не втори голям бутон; без връзка към /start.
    assert 'class="btn btn-ghost btn-icon share-btn"' in hero and 'aria-label="Сподели"' in hero
    assert 'href="/start"' not in hero and "/dneven-horoskop" not in hero


def test_guest_gets_the_mini_form_under_the_text(app, db):
    html = _render(app)
    assert html.count('id="lichen"') == 1
    block = html[html.index('id="lichen"'):]
    assert 'id="lichenForm"' in block
    for field in ("lfName", "lfDay", "lfMonth", "lfYear", "lfHour", "lfMinute", "lfUnknown", "lfPlace"):
        assert f'id="{field}"' in block
    assert "Не знам часа на раждане" in block and "Без регистрация" not in html
    assert 'data-cta="horoscope_sign:form"' in block
    # Връзва се към съществуващия път: геокодер → localStorage → /start.
    assert "/api/public/geocode" in html and "miralog_guest_birth" in html
    assert "signup_prompt_view" in html and "IntersectionObserver" in html


def test_bundle_line_uses_the_db_price_and_leaves_the_faq(app, db, monkeypatch):
    html = _render(app)
    price = app.landing_pricing()["bundle"]["price"]
    m = re.search(r'<div class="bundle-card">(.*?)</a>', html, re.S)
    assert m and price in m.group(1)
    assert 'href="/start?want=bundle"' in m.group(1) and 'data-cta="horoscope_sign:bundle"' in m.group(1)
    assert 'data-cta="faq:bundle"' in html            # въпросът си остава

    # Цената е от landing_pricing(), не текст в шаблона.
    monkeypatch.setattr(app, "BUNDLE_PRICE_CENTS", 1234)
    assert app.price_label(1234) in re.search(r'<div class="bundle-card">(.*?)</a>', _render(app), re.S).group(1)


def test_no_bundle_line_when_bundle_is_not_offered(app, db, monkeypatch):
    monkeypatch.setattr(app, "landing_pricing", lambda: {"rows": [], "bundle": None})
    html = _render(app)
    assert 'class="bundle-card"' not in html and 'id="lichen"' in html


def test_logged_in_sees_a_link_not_the_form(app, db, user):
    pid = _person(app, user["id"])
    html = _render(app, app.create_token(user["id"], user["email"]))
    assert 'id="lichenForm"' not in html and "lfName" not in html
    hero = _hero(html)
    assert hero.count("btn-primary") == 1
    assert f'href="/chart/{pid}#horoscope"' in hero
    # Един бутон към личния хороскоп в съдържанието: без втори в блока и без
    # размазания „Още N раздела“ (лентата долу на телефон е отделно).
    assert html.split('id="horoSticky"')[0].count(f'href="/chart/{pid}#horoscope"') == 1
    assert "Отвори днешния си личен хороскоп" in hero


def test_logged_in_sees_the_first_sentences_of_todays_cached_horoscope(app, db, user):
    pid = _person(app, user["id"])
    today = datetime.datetime.now(ZoneInfo("Europe/Sofia")).date().isoformat()
    app.set_ai_cache(pid, f"horoscope:{today}", (
        '---SUMMARY---\n{"mood": "СЕКРЕТНО_РЕЗЮМЕ"}\n---END---\n'
        "1. **Общо усещане**\nДнес Луната ти помага да довършиш старото. Не бързай с отговорите. "
        "Третото изречение не бива да се вижда.\n"))
    html = _render(app, app.create_token(user["id"], user["email"]))
    block = html[html.index('id="lichen"'):]
    assert "Днес Луната ти помага да довършиш старото. Не бързай с отговорите." in block
    assert f'href="/chart/{pid}#horoscope"' in block and 'data-cta="horoscope_sign:personal"' in block
    assert "Третото изречение" not in html and "СЕКРЕТНО_РЕЗЮМЕ" not in html
    assert "---SUMMARY---" not in html and "**" not in block


def test_no_teaser_without_cache_and_nothing_is_generated(app, db, user):
    _person(app, user["id"])
    html = _render(app, app.create_token(user["id"], user["email"]))     # call_ai гърми, ако се вика
    assert 'class="lichen-teaser"' not in html
    # Без откъс няма втори блок: само бутонът в началото, без „общото за всички“.
    assert 'id="lichen"' not in html and "Това е общото за всички" not in html
    assert "Отвори днешния си личен хороскоп" in _hero(html)


def test_several_charts_have_no_teaser_and_go_to_the_dashboard(app, db, user):
    a = _person(app, user["id"])
    _person(app, user["id"])
    today = datetime.datetime.now(ZoneInfo("Europe/Sofia")).date().isoformat()
    app.set_ai_cache(a, f"horoscope:{today}", "Тайно изречение. Още едно.")
    html = _render(app, app.create_token(user["id"], user["email"]))
    assert "Тайно изречение" not in html
    assert 'href="/dashboard"' in _hero(html)


def test_seo_content_is_intact(app, db):
    html = _render(app)
    assert '"@type": "FAQPage"' in html and "Често задавани въпроси" in html
    assert "<h1>Дневен хороскоп за Овен</h1>" in html


# --- разчитане по темата на раздела и лента долу -----------------------------

def test_section_offers_follow_love_and_work(app):
    html = ("<h3><span>1</span> Общо усещане за деня</h3><p>а</p>"
            "<h3><span>2</span> Любов и отношения</h3><p>б</p>"
            "<h3><span>3</span> Работа и пари</h3><p>в</p>")
    guest = {"logged_in": False}
    out = app.section_offers_html(html, guest)
    love, period = out.index('data-offer="love"'), out.index('data-offer="period"')
    assert out.index("Любов и отношения") < love < out.index("Работа и пари") < period
    assert "/start?want=love" in out and "/start?want=period" in out
    assert "€" in out and "безплатната ти карта" in out


def test_no_offer_for_what_the_reader_already_owns(app):
    html = "<h3>Любов</h3><p>б</p><h3>Работа</h3><p>в</p>"
    viewer = {"logged_in": True, "person_count": 1, "chart_url": "/chart/7",
              "unlocked": ["chart", "horoscope", "love"]}
    out = app.section_offers_html(html, viewer)
    assert 'data-offer="love"' not in out and 'data-offer="period"' in out
    assert "/chart/7?unlock=period" in out and "безплатната ти карта" not in out


def test_horoscope_pages_have_the_sticky_bar_and_picker():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "templates"
    sign = (root / "horoscope_sign.html").read_text(encoding="utf-8")
    hub = (root / "horoscope_index.html").read_text(encoding="utf-8")
    assert '_horo_sticky.html' in sign and '_horo_sticky.html' in hub
    assert 'class="hub-picker"' in hub and 'horoscope_index:personal' in hub
    assert '_bundle_card.html' in sign and '_bundle_card.html' in hub
