# -*- coding: utf-8 -*-
"""Пакет A2 на изчистването: SEO страниците (хороскоп по зодия, съвместимост,
планета в знак/дом, характеристика на знак, /share, sitemap)."""
import datetime
import re
import sqlite3
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient


def _sofia_today():
    return datetime.datetime.now(ZoneInfo("Europe/Sofia")).date()


def _client(app, token=None):
    c = TestClient(app.app, base_url="https://astrokarta.bg")
    if token:
        c.cookies.set("miralog_token", token)
    return c


def _person(app, user_id, year=1990, month=5, day=14):
    """14.05.1990 в 08:30 — Слънцето е в Телец."""
    with sqlite3.connect(app.DB_PATH) as c:
        cur = c.execute("INSERT INTO persons (user_id, name, year, month, day, hour, minute, lat, lon,"
                        " timezone) VALUES (?, 'Мария Петрова', ?, ?, ?, 8, 30, 42.7, 23.3, 'Europe/Sofia')",
                        (user_id, year, month, day))
        c.commit()
        return cur.lastrowid


def _reading(sections=6):
    parts = ['---SUMMARY---\n{"mood": "Добър ден"}\n---END---']
    for i in range(1, sections + 1):
        parts.append(f"{i}. **Раздел {i}**\nПримерен текст на раздел {i}. Още едно изречение.")
    return "\n\n".join(parts)


def _title(html):
    return re.search(r"<title>(.*?)</title>", html, re.S).group(1)


def _h1(html):
    return re.search(r"<h1>(.*?)</h1>", html, re.S).group(1)


# --- 1. мини формата --------------------------------------------------------------

def test_sign_form_is_honest_and_goes_to_start_from_horoscope(app, db):
    html = _client(app).get("/horoskop/oven").text
    assert "Без регистрация" not in html and "без регистрация" not in html
    # Честно какво следва: картата веднага, хороскопът след безплатен запис с имейл.
    assert "безплатен запис с имейл" in html
    assert "/start?from=horoscope" in html
    # Досегашният път през гост-данните остава.
    assert "miralog_guest_birth" in html and "/api/public/geocode" in html


# --- 2. вписаният на страницата на зодията ------------------------------------------

def test_logged_in_gets_one_button_no_blur_and_own_sign_badge(app, db, user):
    pid = _person(app, user["id"])
    token = app.create_token(user["id"], user["email"])
    for slug, sign in (("telec", "Taurus"), ("oven", "Aries")):
        app.set_sign_horoscope(sign, _sofia_today().isoformat(), _reading(6))
    own = _client(app, token).get("/horoskop/telec").text
    other = _client(app, token).get("/horoskop/oven").text
    guest = _client(app).get("/horoskop/telec").text

    # Зодията на вписания (една карта) е отбелязана; чуждата — не.
    assert "Това е твоята зодия" in own and "Това е твоята зодия" not in other
    assert "Това е твоята зодия" not in guest

    # Един бутон към личния хороскоп, без размазан блок и без „общото за всички“.
    assert own.split('id="horoSticky"')[0].count(f'href="/chart/{pid}#horoscope"') == 1
    assert 'class="horo-locked"' not in own and "Още 3 раздела" not in own
    assert "Това е общото за всички" not in own
    assert "Отвори днешния си личен хороскоп" in own
    # Целият общ текст се чете свободно (и скритите раздели).
    assert "Примерен текст на раздел 6" in own

    # Гостът пази размазването и формата.
    assert 'class="horo-locked"' in guest and 'id="lichenForm"' in guest


def test_sun_sign_comes_from_the_real_sun_position(app, db, user):
    pid = _person(app, user["id"], 1990, 5, 14)
    viewer = {"logged_in": True, "person_count": 1, "unlocked": [], "chart_url": f"/chart/{pid}"}
    assert app.viewer_sun_sign_slug(viewer) == "telec"
    # Няколко карти — не знаем чия е зодията.
    assert app.viewer_sun_sign_slug({**viewer, "person_count": 2}) is None
    assert app.viewer_sun_sign_slug({"logged_in": False}) is None


# --- 3. „Вход“ само за гост --------------------------------------------------------------

PAGES_WITH_FINAL_LOGIN = ["/savmestimost/oven-telec", "/zodia/oven", "/luna-v-skorpion",
                          "/venera-v-7-dom", "/savmestimost"]


@pytest.mark.parametrize("path", PAGES_WITH_FINAL_LOGIN)
def test_login_button_only_for_guests(app, db, user, path):
    _person(app, user["id"])
    token = app.create_token(user["id"], user["email"])
    login_btn = 'class="btn btn-ghost" href="/login"'
    assert login_btn in _client(app).get(path).text
    assert login_btn not in _client(app, token).get(path).text


# --- 4. съвместимост на двойка ---------------------------------------------------------------

def test_pair_page_sells_the_love_module_not_the_profile(app, db):
    app.set_compatibility("Aries", "Taurus", "1. **Общо**\nТекст за двойката.\n")
    html = _client(app).get("/savmestimost/oven-telec").text
    hero = html[html.index('<header class="hero'):html.index("</header>")]
    assert 'href="/start?want=love"' in hero and "Проверете вашата двойка" in hero
    assert "Пълен астрологически профил" not in hero and "Създай наталната си карта" not in html
    # FAQ сочи любовния модул, не профила за един човек.
    faq = html[html.index('class="faq"'):].split("</section>")[0]
    assert 'href="/lyubovna-savmestimost"' in faq and 'href="/astrologicheski-profil"' not in faq
    # „Във вашата“ (не „В вашата“), и „вие“ навсякъде на екрана.
    assert "Във <strong>вашата</strong>" in html and "В <strong>вашата</strong>" not in html


def test_pair_page_links_to_the_sign_pages_and_has_one_chip_list_for_same_sign(app, db):
    html = _client(app).get("/savmestimost/oven-telec").text
    for href in ("/zodia/oven", "/zodia/telec", "/horoskop/oven", "/horoskop/telec"):
        assert f'href="{href}"' in html
    assert html.count('class="related related--chips"') == 2
    same = _client(app).get("/savmestimost/rak-rak").text
    assert same.count('class="related related--chips"') == 1


# --- 5. матрицата на телефон -------------------------------------------------------------------

def test_matrix_has_two_selects_for_phones_and_preselects_the_sign(app, db):
    html = _client(app).get("/savmestimost?znak=rak").text
    assert 'id="pickA"' in html and 'id="pickB"' in html and ">Виж</button>" in html
    assert "Първи знак" in html and "Втори знак" in html
    assert re.search(r'<option value="rak" selected>', html)
    # Редът е зодиакален: адресът на двойката е каноничният.
    assert "order[Math.min(i, j)]" in html
    # Без бутоните „Пълен астрологически профил“ над матрицата.
    assert "Пълен астрологически профил" not in html.split("</header>")[0]
    # Клетките имат достъпно име.
    assert 'aria-label="Овен и Телец"' in html
    # Неверен знак в адреса не чупи страницата.
    assert _client(app).get("/savmestimost?znak=nyama").status_code == 200


# --- 6. решетка от чипове, съседни знаци, връзки ----------------------------------------------

def test_long_lists_are_chip_grids_and_signs_link_to_each_other(app, db):
    html = _client(app).get("/horoskop/oven").text
    assert 'class="related related--chips"' in html
    assert 'href="/horoskop/ribi"' in html and "← Риби" in html and "Телец →" in html
    assert 'href="/zodia/oven"' in html and 'href="/savmestimost?znak=oven"' in html
    # Кръгът се затваря: преди Овен е Риби, след Риби е Овен.
    assert "Овен →" in _client(app).get("/horoskop/ribi").text
    profile = _client(app).get("/zodia/lav").text
    assert 'href="/horoskop/lav"' in profile and 'href="/savmestimost?znak=lav"' in profile
    assert 'href="/horoskop/lav"' in _client(app).get("/luna-v-lav").text


# --- 7. „Днес · Утре“ ---------------------------------------------------------------------------------

def test_tomorrow_switch_only_when_tomorrow_is_ready(app, db):
    today = _sofia_today()
    app.set_sign_horoscope("Aries", today.isoformat(), _reading(5))
    html = _client(app).get("/horoskop/oven").text
    assert "day-switch" not in html.split("<main>")[1].split("</header>")[0]
    # ?den=utre без готов хороскоп просто показва днешния.
    assert "Хороскоп за Овен утре" not in _client(app).get("/horoskop/oven?den=utre").text

    app.set_sign_horoscope("Aries", (today + datetime.timedelta(days=1)).isoformat(), _reading(5))
    html = _client(app).get("/horoskop/oven").text
    assert 'class="day-switch"' in html and 'href="/horoskop/oven?den=utre"' in html
    tomorrow = _client(app).get("/horoskop/oven?den=utre").text
    assert "Хороскоп за Овен утре" in _h1(tomorrow)
    assert (today + datetime.timedelta(days=1)).strftime("%d.%m.%Y") in tomorrow
    # Същото съдържание под друг адрес: не за индексиране, каноничният е „днес“.
    assert 'content="noindex, follow"' in tomorrow
    assert '<link rel="canonical" href="https://astrokarta.bg/horoskop/oven">' in tomorrow
    assert 'content="noindex' not in html


# --- 8. „7-ми дом“ и ✦ точки --------------------------------------------------------------------------

def test_house_name_is_7th_not_7_dom_and_lists_have_stars(app, db):
    app.set_planet_house("Venus", "7th House", "1. **Общо**\n- първо\n- второ\n")
    html = _client(app).get("/venera-v-7-dom").text
    assert "7-ми дом" in _title(html) and "7 дом" not in _title(html)
    assert "7-ми дом" in _h1(html) and "7 дом" not in _h1(html)
    assert "Другите планети в 7-ми дом" in html and "Венера в другите домове" in html
    assert "10-ти дом" in html           # чиповете носят името, не „10 дом“
    assert re.search(r"white-space:\s*nowrap;\">7-ми дом", html)      # надписът не се чупи
    # ✦ вместо големи черни точки — и на двата типа страници.
    assert '.horo-reading ul li::before { content: "✦"' in html
    assert '.horo-reading ul li::before { content: "✦"' in _client(app).get("/luna-v-skorpion").text


# --- 9. /share ---------------------------------------------------------------------------------------

def test_share_uses_the_site_look_and_a_human_dead_link(app, db, user):
    dead = _client(app).get("/share/nyama-takuv-link")
    assert dead.status_code == 404
    assert "Този линк не работи" in dead.text and 'href="/"' in dead.text
    assert "Към началната страница" in dead.text
    assert "site-footer" in dead.text and "site-nav" in dead.text and "page-logo" in dead.text

    pid = _person(app, user["id"])
    key = f"horoscope:{_sofia_today().isoformat()}"
    app.set_ai_cache(pid, key, _reading(3))
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("INSERT INTO share_links (token, person_id, user_id, cache_key) VALUES ('tok123', ?, ?, ?)",
                  (pid, user["id"], key))
        c.commit()
    page = _client(app).get("/share/tok123")
    assert page.status_code == 200
    html = page.text
    assert "site-footer" in html and "site-nav" in html and "page-logo" in html
    assert "изчислява натална карта" in html                    # какво е сайтът, под бутона
    assert "Създай своята карта" in html or "Създай своята натална карта" in html
    # Дата 15.10.2026, не ISO; заглавията с „1.“.
    assert "slice(0, 10)" not in html and "bgDate" in html
    assert 'h3 span::after { content: "."; }' in html
    api = _client(app).get("/api/share/tok123").json()
    assert api["cache_key"] == key


# --- 10–11. sitemap, заглавия, описания ---------------------------------------------------------

def test_sitemap_has_no_duplicates_and_no_register(app, db):
    xml = _client(app).get("/sitemap.xml").text
    locs = re.findall(r"<loc>(.*?)</loc>", xml)
    assert len(locs) == len(set(locs))
    assert not any(l.endswith("/register") for l in locs)
    assert sum(1 for l in locs if l.endswith("/savmestimost")) == 1


def test_titles_and_meta_description(app, db):
    title = _title(_client(app).get("/zodia/oven").text)
    assert title.startswith("Овен — характеристика на зодията | ") and title.count("Овен") == 1
    hub = _client(app).get("/horoskop").text
    desc = re.search(r'<meta name="description" content="(.*?)">', hub).group(1)
    assert len(desc) <= 160, len(desc)


# --- 12–13. небето на компютър, общ футър ------------------------------------------------------------

@pytest.mark.parametrize("path", ["/horoskop", "/horoskop/oven", "/zodia/oven", "/savmestimost",
                                  "/savmestimost/oven-telec", "/luna-v-skorpion", "/venera-v-7-dom"])
def test_shared_footer_replaces_the_own_ones(app, db, path):
    html = _client(app).get(path).text
    assert 'class="site-footer"' in html and "Всички разчитания" in html
    assert html.count("<footer") == 1


def test_sky_strip_wraps_on_desktop(app, db):
    html = _client(app).get("/horoskop/oven").text
    assert ".eph-inner .eph-list { flex-wrap: wrap" in html
