# -*- coding: utf-8 -*-
"""Фаза 2, пакет ux2: изглед „Лесно / За напреднали“, дневното писмо (линк, човек,
резюме и есенция) и предварителното писане на личния хороскоп."""
import datetime
import re
import secrets
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
SOFIA = ZoneInfo("Europe/Sofia")

READING = (
    "---SUMMARY---\n"
    '{"mood": "Съсредоточен", "energy": "Средна", "do": ["разговори"], "avoid": ["спорове"],'
    ' "focus": "Работа", "caution": "Не бързай."}\n'
    "---END---\n"
    "1. **Общо усещане за деня** — Спокоен ден.\n\n"
    "2. **Разчитане на аспектите** — Дълго обяснение на аспектите, което не влиза в писмото.\n\n"
    "9. **Есенцията на деня** — Бавното днес е по-бързо от прибързаното утре. Довери се на ритъма си.\n"
)


def _client(app):
    return TestClient(app.app)


def _bearer(app, user):
    return {"Authorization": "Bearer " + app.create_token(user["id"], user["email"])}


def _person(app, user_id, name="Тест", tz="Europe/Sofia"):
    with sqlite3.connect(app.DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO persons (user_id, name, year, month, day, hour, minute, lat, lon, timezone)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)", (user_id, name, 1990, 6, 15, 12, 0, 42.7, 23.3, tz))
        c.commit()
        return cur.lastrowid


def _user(app, db, **cols):
    u = db.create_user(f"ux2-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    db.grant_signup_features(u["id"])
    if cols:
        sets = ", ".join(f"{k} = ?" for k in cols)
        with sqlite3.connect(app.DB_PATH) as c:
            c.execute(f"UPDATE users SET {sets} WHERE id = ?", (*cols.values(), u["id"]))
            c.commit()
    return db.get_user_by_id(u["id"])


# --- изглед: миграция и API -----------------------------------------------------

def test_ui_mode_column_exists_and_defaults_to_easy(app, user):
    with sqlite3.connect(app.DB_PATH) as c:
        cols = {r[1]: r for r in c.execute("PRAGMA table_info(users)")}
    assert "ui_mode" in cols
    assert cols["ui_mode"][3] == 1                       # NOT NULL
    assert user["ui_mode"] == "easy"


def test_ui_mode_migration_adds_the_column_to_an_old_database(app, tmp_path, monkeypatch):
    """Стара база без колоната: init_db я добавя и никой не губи данни."""
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as c:
        c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE,"
                  " password_hash TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        c.execute("INSERT INTO users (email, password_hash) VALUES ('star@example.com', 'x')")
        c.commit()
    monkeypatch.setattr(app, "DB_PATH", old)
    app.init_db()
    with sqlite3.connect(old) as c:
        row = c.execute("SELECT ui_mode FROM users WHERE email = 'star@example.com'").fetchone()
    assert row == ("easy",)


def test_account_returns_ui_mode_and_post_changes_it(app, user):
    client, h = _client(app), _bearer(app, user)
    assert client.get("/api/account", headers=h).json()["ui_mode"] == "easy"
    r = client.post("/api/account/ui-mode", json={"mode": "pro"}, headers=h)
    assert r.status_code == 200 and r.json() == {"ok": True, "ui_mode": "pro"}
    assert client.get("/api/account", headers=h).json()["ui_mode"] == "pro"
    assert client.post("/api/account/ui-mode", json={"mode": "easy"}, headers=h).status_code == 200
    assert client.get("/api/account", headers=h).json()["ui_mode"] == "easy"


@pytest.mark.parametrize("bad", ["", "expert", "PRO", "pro ", None, 1])
def test_ui_mode_rejects_anything_else(app, user, bad):
    r = _client(app).post("/api/account/ui-mode", json={"mode": bad}, headers=_bearer(app, user))
    assert r.status_code in (400, 422)
    with sqlite3.connect(app.DB_PATH) as c:
        assert c.execute("SELECT ui_mode FROM users WHERE id = ?", (user["id"],)).fetchone() == ("easy",)


def test_ui_mode_needs_a_signed_in_user(app):
    assert _client(app).post("/api/account/ui-mode", json={"mode": "pro"}).status_code in (401, 403)


# --- настройки и табло ------------------------------------------------------------

def test_settings_has_the_view_section_after_the_daily_email():
    html = (ROOT / "templates" / "settings.html").read_text(encoding="utf-8")
    heads = re.findall(r"<h2>([^<]+)</h2>", html)
    assert heads[:3] == ["Данни за акаунта", "Дневният хороскоп на имейла", "Изглед"]
    assert "Само най-важното, без градуси и таблици" in html
    assert "Планети, аспекти, градуси и домове" in html
    assert "/api/account/ui-mode" in html and "miralog_mode" in html


def test_dashboard_card_button_opens_the_wheel_and_today_the_horoscope():
    html = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert '/chart/${p.id}#wheel"' in html
    assert '/chart/${p.id}#horoscope"' in html
    assert "miralog_mode" in html


def test_every_template_loads_the_new_consent_files():
    for f in (ROOT / "templates").glob("*.html"):
        if f.name == "chart.html":          # на пакета за картата (ux1)
            continue
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"/static/consent\.(css|js)(\?v=\d+)?", text):
            assert m.group(0) in ("/static/consent.css?v=5", "/static/consent.js?v=10"), (f.name, m.group(0))


def test_consent_bar_is_a_thin_strip_on_phones():
    css = (ROOT / "static" / "consent.css").read_text(encoding="utf-8")
    js = (ROOT / "static" / "consent.js").read_text(encoding="utf-8")
    assert ".consent-more { display: none; }" in css
    assert "consent-ok" in js and "consent-no" in js and "Приемам" in js and "Отказвам" in js


# --- дневното писмо ----------------------------------------------------------------

def _run_digest(app, monkeypatch, now=None):
    sent = []
    monkeypatch.setattr(app, "smtp_setting", lambda k: "smtp.example.com" if k == "smtp_host" else "")
    monkeypatch.setattr(app, "try_send_template", lambda to, kind, **f: sent.append((to, f)) or True)
    app.run_digest_emails(now or datetime.datetime(2026, 10, 15, 9, 0, tzinfo=SOFIA))
    return sent


def test_digest_uses_the_oldest_card_and_links_to_the_horoscope_tab(app, db, monkeypatch):
    u = _user(app, db, digest_opt_in=1)
    first = _person(app, u["id"], name="Мария")        # най-старата, но не първа по азбука
    _person(app, u["id"], name="Ани")
    sent = _run_digest(app, monkeypatch)
    assert len(sent) == 1
    reading = sent[0][1]["reading"]
    assert f"/chart/{first}#horoscope" in reading
    assert "Мария" in reading and "Ани" not in reading


def test_digest_with_cached_text_leads_with_mood_and_essence(app, db, monkeypatch):
    u = _user(app, db, digest_opt_in=1)
    pid = _person(app, u["id"], name="Мария")
    now = datetime.datetime(2026, 10, 15, 9, 0, tzinfo=SOFIA)
    app.set_ai_cache(pid, "horoscope:2026-10-15", READING)
    reading = _run_digest(app, monkeypatch, now)[0][1]["reading"]
    assert "настроение — съсредоточен" in reading and "фокус — работа" in reading
    assert "Есенцията на деня: Бавното днес е по-бързо от прибързаното утре." in reading
    assert "Дълго обяснение на аспектите" not in reading
    # първо резюмето и есенцията, след тях линкът
    assert reading.index("настроение") < reading.index("Есенцията") < reading.index(f"/chart/{pid}#horoscope")


def test_digest_without_cache_stays_the_short_invitation(app, db, monkeypatch):
    u = _user(app, db, digest_opt_in=1)
    pid = _person(app, u["id"], name="Мария")
    reading = _run_digest(app, monkeypatch)[0][1]["reading"]
    assert "Дневният хороскоп за Мария те чака" in reading
    assert f"/chart/{pid}#horoscope" in reading and "Есенцията" not in reading


def test_day_essence_handles_the_other_heading_shapes(app):
    assert app._day_essence("## Есенцията на деня\nТихо е.\nИ добре.\n\n10. **Друго** — не").startswith("Тихо е. И добре.")
    assert app._day_essence("9. Есенцията на деня: Кратко.") == "Кратко."
    assert app._day_essence("Няма такъв раздел") == ""


# --- предварително писане ---------------------------------------------------------

@pytest.fixture
def fake_ai(app, monkeypatch):
    """Фалшив AI: брои извикванията и пази модела."""
    calls = []
    monkeypatch.setattr(app, "get_ai_config", lambda: ("key", "deepseek"))
    # Отговорът трябва да мине проверката за качество преди кеша (9-те раздела, резюме).
    from tests.test_horoscope_quality import make_reply
    reply = make_reply(app.PERSONAL_SECTION_TITLES, teaser=False)
    monkeypatch.setattr(app, "call_ai", lambda *a, **k: calls.append(k.get("model")) or reply)
    monkeypatch.setattr(app, "HOROSCOPE_PREWARM_MAX", 100)
    return calls


NOW = datetime.datetime(2026, 10, 15, 8, 0, tzinfo=SOFIA)
RECENT = "2026-10-14T20:00:00"


def _clean_state(app):
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("DELETE FROM settings WHERE key LIKE 'prewarm:%'")
        c.execute("UPDATE users SET last_seen = NULL, last_login = NULL, digest_opt_in = 0")
        c.commit()
    with app._AI_JOBS_LOCK:
        app._AI_JOBS.clear()


def test_prewarm_starts_exactly_for_the_eligible_accounts(app, db, fake_ai):
    _clean_state(app)
    digest = _user(app, db, digest_opt_in=1)                       # писмо по имейл
    seen = _user(app, db, last_seen=RECENT)                        # отварял вчера
    login = _user(app, db, last_login="2026-10-12 10:00:00")       # влязъл преди 3 дни
    old = _user(app, db, last_seen="2026-09-01T10:00:00")          # отдавна не е идвал
    never = _user(app, db)                                         # никога
    blocked = _user(app, db, last_seen=RECENT, is_blocked=1)
    nocard = _user(app, db, last_seen=RECENT)                      # без карта
    ids = {n: _person(app, u["id"]) for n, u in
           dict(digest=digest, seen=seen, login=login, old=old, never=never, blocked=blocked).items()}

    assert app.run_horoscope_prewarm(NOW) == 3
    assert len(fake_ai) == 3
    for name in ("digest", "seen", "login"):
        assert app.get_ai_cache(ids[name], "horoscope:2026-10-15"), name
    for name in ("old", "never", "blocked"):
        assert not app.get_ai_cache(ids[name], "horoscope:2026-10-15"), name


def test_prewarm_does_not_repeat_what_is_cached(app, db, fake_ai):
    _clean_state(app)
    u = _user(app, db, last_seen=RECENT)
    pid = _person(app, u["id"])
    assert app.run_horoscope_prewarm(NOW) == 1
    assert app.run_horoscope_prewarm(NOW + datetime.timedelta(hours=1)) == 0
    assert len(fake_ai) == 1
    # човек, който вече има кеширан текст (отворил е картата сам), не се пише пак
    other = _user(app, db, last_seen=RECENT)
    oid = _person(app, other["id"])
    app.set_ai_cache(oid, "horoscope:2026-10-15", READING)
    assert app.run_horoscope_prewarm(NOW) == 0
    assert len(fake_ai) == 1 and pid


def test_prewarm_only_for_the_oldest_card_of_an_account(app, db, fake_ai):
    _clean_state(app)
    u = _user(app, db, last_seen=RECENT)
    first = _person(app, u["id"], name="Мария")
    second = _person(app, u["id"], name="Ани")
    assert app.run_horoscope_prewarm(NOW) == 1
    assert app.get_ai_cache(first, "horoscope:2026-10-15")
    assert not app.get_ai_cache(second, "horoscope:2026-10-15")


def test_prewarm_waits_for_five_in_the_persons_own_timezone(app, db, fake_ai):
    _clean_state(app)
    u = _user(app, db, last_seen=RECENT)
    pid = _person(app, u["id"], tz="America/New_York")
    # 8:00 в София = 1:00 в Ню Йорк — там още е нощ
    assert app.run_horoscope_prewarm(datetime.datetime(2026, 10, 15, 8, 0, tzinfo=SOFIA)) == 0
    # 12:00 в София = 5:00 в Ню Йорк
    assert app.run_horoscope_prewarm(datetime.datetime(2026, 10, 15, 12, 0, tzinfo=SOFIA)) == 1
    assert app.get_ai_cache(pid, "horoscope:2026-10-15")


def test_prewarm_needs_the_horoscope_module(app, db, fake_ai, monkeypatch):
    _clean_state(app)
    _person(app, _user(app, db, last_seen=RECENT)["id"])
    monkeypatch.setattr(app, "unlocked_features", lambda user: [])
    assert app.run_horoscope_prewarm(NOW) == 0
    assert not fake_ai


def test_prewarm_respects_the_daily_cap_even_after_a_restart(app, db, fake_ai, monkeypatch):
    _clean_state(app)
    monkeypatch.setattr(app, "HOROSCOPE_PREWARM_MAX", 2)
    for _ in range(4):
        _person(app, _user(app, db, last_seen=RECENT)["id"])
    assert app.run_horoscope_prewarm(NOW) == 2
    # „рестарт“: паметта е празна, бройката е в базата
    with app._AI_JOBS_LOCK:
        app._AI_JOBS.clear()
    assert app.run_horoscope_prewarm(NOW) == 0
    assert len(fake_ai) == 2
    # нов ден — нов таван
    assert app.run_horoscope_prewarm(NOW + datetime.timedelta(days=1)) == 2


def test_prewarm_uses_the_same_model_rule_as_the_page(app, db, fake_ai):
    _clean_state(app)
    free = _user(app, db, last_seen=RECENT)
    paid = _user(app, db, last_seen=RECENT)
    app.grant_feature_purchase(paid["id"], "numerology", 400, "EUR", None)
    _person(app, free["id"])
    _person(app, paid["id"])
    assert app.run_horoscope_prewarm(NOW) == 2
    assert sorted(m is None for m in fake_ai) == [False, True]
    assert app.PAID_MODEL in fake_ai


def test_prewarm_does_nothing_without_an_ai_key(app, db, monkeypatch):
    _clean_state(app)
    monkeypatch.setattr(app, "get_ai_config", lambda: (None, "deepseek"))
    _person(app, _user(app, db, last_seen=RECENT)["id"])
    assert app.run_horoscope_prewarm(NOW) == 0
