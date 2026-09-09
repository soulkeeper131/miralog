# -*- coding: utf-8 -*-
"""Ръбове: положения, които не се случват при нормална употреба.

Тези тестове търсят счупено, не потвърждават работещото.
"""
import secrets
import sqlite3

import pytest
from fastapi import HTTPException


def _session(user_id, keys, amount, session_id=None):
    return {
        "id": session_id or ("cs_" + secrets.token_hex(8)),
        "amount_total": amount, "currency": "eur", "payment_status": "paid",
        "customer": None,
        "metadata": {"kind": "features", "user_id": str(user_id),
                     "feature_keys": ",".join(keys)},
    }


def _count(app, user_id):
    with sqlite3.connect(app.DB_PATH) as c:
        return c.execute("SELECT COUNT(*) FROM payments WHERE user_id = ?",
                         (user_id,)).fetchone()[0]


# --- повредени данни от Stripe ----------------------------------------------

def test_unknown_feature_key_does_not_crash(app, user):
    """Изтеглен от продажба модул не бива да събаря обработката."""
    app.fulfill_checkout_session(_session(user["id"], ["nesushtestvuvasht"], 499))
    assert _count(app, user["id"]) == 1        # плащането се записва


def test_empty_feature_list_records_nothing(app, user):
    sess = _session(user["id"], [], 499)
    sess["metadata"]["feature_keys"] = ""
    app.fulfill_checkout_session(sess)
    assert _count(app, user["id"]) == 0


def test_deleting_an_account_removes_its_purchases(app, db):
    """Иначе покупките остават като сираци — и ако ново id съвпадне със
    старото, новият човек наследява платените модули безплатно."""
    row = db.create_user(f"del-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    uid = row["id"]
    app.grant_feature_purchase(uid, "profile", 499, "EUR", None)

    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("DELETE FROM persons WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM payments WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM feature_purchases WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM oauth_accounts WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM share_links WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM users WHERE id = ?", (uid,))
        c.commit()
        left = c.execute("SELECT COUNT(*) FROM feature_purchases WHERE user_id = ?",
                         (uid,)).fetchone()[0]
    assert left == 0, "покупките остават след изтриване на акаунта"


def test_recycled_id_inherits_nothing(app, db):
    """Пряката последица от горното, проверена от страната на достъпа."""
    row = db.create_user(f"rec-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    uid = row["id"]
    app.grant_feature_purchase(uid, "profile", 499, "EUR", None)
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("DELETE FROM feature_purchases WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM users WHERE id = ?", (uid,))
        c.execute("INSERT INTO users (id, email, password_hash, role)"
                  " VALUES (?, ?, ?, 'user')",
                  (uid, f"new-{secrets.token_hex(3)}@example.com", app.hash_password("y")))
        c.commit()
    assert "profile" not in app.unlocked_features(app.get_user_by_id(uid))


def test_zero_amount_session_still_unlocks(app, user):
    """100% отстъпка е валидна поръчка — модулът трябва да се отключи."""
    app.fulfill_checkout_session(_session(user["id"], ["profile"], 0))
    assert "profile" in app.unlocked_features(app.get_user_by_id(user["id"]))


def test_two_different_sessions_are_both_recorded(app, user):
    """Обратното на дублирането: две истински покупки не бива да се слеят."""
    app.fulfill_checkout_session(_session(user["id"], ["profile"], 499))
    app.fulfill_checkout_session(_session(user["id"], ["akashic"], 899))
    assert _count(app, user["id"]) == 2
    unlocked = set(app.unlocked_features(app.get_user_by_id(user["id"])))
    assert {"profile", "akashic"} <= unlocked


def test_buying_the_same_module_twice_keeps_one_purchase_row(app, user):
    """Ако някой плати два пъти за същото, достъпът остава един запис."""
    app.fulfill_checkout_session(_session(user["id"], ["profile"], 499))
    app.fulfill_checkout_session(_session(user["id"], ["profile"], 499))
    with sqlite3.connect(app.DB_PATH) as c:
        rows = c.execute(
            "SELECT COUNT(*) FROM feature_purchases WHERE user_id = ? AND feature_key = 'profile'",
            (user["id"],)).fetchone()[0]
    assert rows == 1


# --- потребителски данни ----------------------------------------------------

def test_email_is_matched_case_insensitively(app, db):
    """Иначе Google връща Ivan@ и се прави втори акаунт до ivan@."""
    email = f"MiXeD-{secrets.token_hex(3)}@Example.COM"
    created = db.create_user(email.lower(), db.hash_password("x"))
    linked = app._oauth_link_or_create("google", "sub-" + secrets.token_hex(6), email, "")
    assert linked["id"] == created["id"]


def test_duplicate_registration_is_refused(app, db):
    email = f"dup-{secrets.token_hex(3)}@example.com"
    db.create_user(email, db.hash_password("x"))
    with pytest.raises(Exception):
        db.create_user(email, db.hash_password("x"))


# --- текст за четене на глас ------------------------------------------------

def test_speech_text_strips_markdown(app):
    raw = "# Заглавие\n\n**Удебелено** и *наклонено*\n- точка\n1. номер\n`код`"
    speech = app._text_for_speech(raw)
    for marker in ("#", "**", "*", "`", "- ", "1."):
        assert marker not in speech, f"{marker!r} остава и се чете на глас"
    assert "Заглавие" in speech and "Удебелено" in speech


def test_speech_split_keeps_every_word(app):
    text = " ".join(f"Изречение номер {i} от текста." for i in range(1, 200))
    parts = app._split_for_tts(text, 5)
    assert "".join(parts) == text, "части от текста се губят при разделянето"


def test_short_text_is_not_split(app):
    """Под 1500 знака паралелизмът струва повече, отколкото спестява."""
    assert len(app._split_for_tts("Кратко изречение.", 5)) == 1


def test_split_never_starts_mid_sentence(app):
    text = " ".join(f"Изречение номер {i} от текста." for i in range(1, 200))
    parts = app._split_for_tts(text, 5)
    for part in parts[1:]:
        assert part.strip()[0].isupper() or part.strip()[0].isdigit(), \
            "част започва по средата на изречение — интонацията се чупи"


# --- цени -------------------------------------------------------------------

def test_no_module_is_sold_for_free(app):
    """Цена 0 с включена продажба значи безплатна покупка."""
    for key, row in app.get_feature_prices().items():
        if row["is_purchasable"]:
            assert row["price_cents"] > 0, f"{key} се продава за 0"


def test_catalogue_and_prices_agree(app):
    """Ред в цените без запис в каталога е остатък, който никой не поддържа."""
    catalogue = {f["key"] for f in app.FEATURE_CATALOGUE}
    for key, row in app.get_feature_prices().items():
        if row["is_purchasable"]:
            assert key in catalogue, f"{key} се продава, но липсва в каталога"


def test_bundle_costs_less_than_its_parts(app, user):
    bundle = app.bundle_offer(user)
    parts = sum(app.feature_offer(k)["price_cents"] for k in bundle["keys"])
    assert bundle["price_cents"] < parts


def test_admin_delete_endpoint_cleans_everything(app, db):
    """Пази самия рут, а не само SQL-а: ако някой махне ред от изтриването,
    този тест пада."""
    row = db.create_user(f"adm-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    uid = row["id"]
    app.grant_feature_purchase(uid, "profile", 499, "EUR", None)
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("INSERT OR IGNORE INTO oauth_accounts"
                  " (provider, provider_user_id, user_id, email)"
                  " VALUES ('google', ?, ?, ?)",
                  ("sub-" + secrets.token_hex(4), uid, row["email"]))
        c.commit()

    admin = db.create_user(f"root-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE users SET role = 'admin' WHERE id = ?", (admin["id"],))
        c.commit()

    app.api_admin_delete_user(uid, admin=app.get_user_by_id(admin["id"]))

    with sqlite3.connect(app.DB_PATH) as c:
        for table in ("feature_purchases", "oauth_accounts", "payments", "persons"):
            left = c.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?",
                             (uid,)).fetchone()[0]
            assert left == 0, f"{table} остава след изтриване на акаунта"


# --- откъсите на общата страница за хороскопи --------------------------------

def test_excerpt_drops_the_numbered_heading(app):
    """Разчитанията започват с „1. Общо усещане за деня“. Ако заглавието
    остане, всичките 12 карти започват еднакво и изглеждат счупени."""
    raw = ("1. Общо усещане за деня\n\nДенят носи усещане за преосмисляне. "
           "Луната е в балсамична фаза.\n\n2. Любов\n\nРазговорите вървят леко.")
    out = app._first_sentences(raw)
    assert "Общо усещане" not in out
    assert not out.lstrip()[:2].strip().isdigit()
    assert out.startswith("Денят носи")


def test_excerpt_drops_markdown_headings(app):
    out = app._first_sentences("## Общо усещане\n\nДнес нещата се подреждат. Втора мисъл.")
    assert "#" not in out
    assert "Общо усещане" not in out


def test_excerpt_drops_colon_labels(app):
    out = app._first_sentences("Любов:\nДнес партньорът ще те изненада. И още едно.")
    assert not out.startswith("Любов")


def test_excerpt_stays_short_enough_for_the_card(app):
    """Картите са с фиксирана височина — прекалено дълъг откъс се реже."""
    long_text = "Много дълго изречение, което продължава без край. " * 20
    out = app._first_sentences(long_text)
    assert len(out) <= 191, f"откъсът е {len(out)} знака"


def test_excerpt_survives_empty_and_heading_only(app):
    assert app._first_sentences("") == ""
    assert app._first_sentences("1. Общо усещане за деня") == ""


# --- частично разчитане по зодия ---------------------------------------------

def test_sign_reading_is_partial(app):
    """Целият хороскоп даром не оставя причина човек да продължи към картата."""
    html = "".join(f"<h3><span>{i}</span> Раздел {i}</h3><p>Текст {i}.</p>"
                   for i in range(1, 10))
    free, hidden, count = app._split_reading(html)
    assert free.count("<h3") == app.SIGN_FREE_SECTIONS
    assert hidden.count("<h3") == 9 - app.SIGN_FREE_SECTIONS
    assert count == 9 - app.SIGN_FREE_SECTIONS


def test_split_loses_nothing(app):
    """Скритото остава в HTML-а — Google трябва да вижда целия текст."""
    html = "".join(f"<h3>Р{i}</h3><p>Т{i}</p>" for i in range(1, 10))
    free, hidden, _ = app._split_reading(html)
    assert free + hidden == html


def test_short_reading_is_not_split(app):
    """Под пет раздела блурът само дразни, без да остави какво да се чака."""
    html = "".join(f"<h3>Р{i}</h3><p>Т{i}</p>" for i in range(1, 4))
    free, hidden, count = app._split_reading(html)
    assert hidden == "" and count == 0
    assert free == html


def test_split_handles_empty_input(app):
    assert app._split_reading("") == ("", "", 0)
    assert app._split_reading(None) == ("", "", 0)


def test_section_number_is_separated_from_the_title(app):
    """Иначе излиза „1Общо усещане“ вместо „1 Общо усещане“."""
    out = app._md_to_html("1. **Общо усещане за деня**\n\nТекст.")
    assert "</span> " in out, "номерът е слепен със заглавието"


# --- съветите след заглавието „Благоприятно е за:“ ---------------------------

def test_imperative_advice_becomes_a_noun_phrase(app):
    """„Благоприятно е за: Провери интуицията си“ не е български."""
    assert app.normalise_advice("Провери интуицията си") == "Проверка на интуицията"
    assert app.normalise_advice("Изчакай преди решения") == "Изчакване преди решения"
    assert app.normalise_advice("Подреди дома си") == "Подреждане на дома"


def test_reflexive_phrase_stays_whole(app):
    """„себе си“ се чупи, ако „си“ отпадне като обикновено притежателно."""
    assert app.normalise_advice("Фокусирай се върху себе си") == "Фокус върху себе си"


def test_noun_phrases_are_left_alone(app):
    """Новите разчитания вече идват както трябва — не ги пипаме."""
    for text in ("Конфликти вкъщи", "Бързи ангажименти", "Претоварване с работа"):
        assert app.normalise_advice(text) == text


def test_advice_handles_empty_and_punctuation(app):
    assert app.normalise_advice("") == ""
    assert app.normalise_advice(None) == ""
    assert app.normalise_advice("почини си.") == "Почивка"


def test_advice_capitalises_the_result(app):
    assert app.normalise_advice("спокойни разговори")[0].isupper()
