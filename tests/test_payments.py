# -*- coding: utf-8 -*-
"""Плащания: какво се отключва, кога, и какво НЕ се отключва.

Всеки тест описва положение, което може да струва пари — на клиента или
на нас — ако логиката се счупи.
"""
import secrets
import sqlite3


def _session(user_id, keys, amount, session_id=None):
    """Stripe сесия, каквато webhook-ът получава след успешно плащане."""
    return {
        "id": session_id or ("cs_test_" + secrets.token_hex(8)),
        "amount_total": amount,
        "currency": "eur",
        "payment_status": "paid",
        "customer": "cus_" + secrets.token_hex(6),
        "metadata": {"kind": "features", "user_id": str(user_id),
                     "feature_keys": ",".join(keys)},
    }


def _payments(app, user_id):
    with sqlite3.connect(app.DB_PATH) as c:
        return c.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_cents), 0) FROM payments WHERE user_id = ?",
            (user_id,)).fetchone()


# --- какво получава новият потребител ---------------------------------------

def test_new_user_gets_chart_and_horoscope(app, user):
    """Картата е причината да дойде, хороскопът — да се върне. И двете безплатни."""
    unlocked = app.unlocked_features(user)
    assert "chart" in unlocked
    assert "horoscope" in unlocked


def test_new_user_has_no_paid_modules(app, user):
    """Нищо платено не бива да е отключено само защото се е регистрирал."""
    unlocked = set(app.unlocked_features(user))
    for key in ("profile", "akashic", "numerology", "love", "period", "moon"):
        assert key not in unlocked, f"{key} е отключен без плащане"


def test_oauth_signup_gets_the_same_free_features(app, db):
    """Влезлите през Google минават по друг път — но получават същото.

    Точно това беше счупено: акаунт без достъп до собствената си карта.
    """
    email = f"g-{secrets.token_hex(4)}@example.com"
    row = app._oauth_link_or_create("google", "sub-" + secrets.token_hex(6), email, "Тест")
    unlocked = app.unlocked_features(app.get_user_by_id(row["id"]))
    assert "chart" in unlocked
    assert "horoscope" in unlocked


# --- отключване след плащане ------------------------------------------------

def test_payment_unlocks_exactly_what_was_bought(app, user):
    app.fulfill_checkout_session(_session(user["id"], ["profile"], 499))
    unlocked = set(app.unlocked_features(app.get_user_by_id(user["id"])))
    assert "profile" in unlocked
    assert "akashic" not in unlocked, "платил е един модул, отключили са се повече"


def test_payment_is_recorded_once(app, user):
    app.fulfill_checkout_session(_session(user["id"], ["profile"], 499))
    count, total = _payments(app, user["id"])
    assert (count, total) == (1, 499)


def test_same_session_delivered_three_times_charges_once(app, user):
    """Webhook и връщането от Stripe могат да пристигнат заедно, а Stripe
    преповтаря доставките. Дневникът трябва да остане с един запис."""
    sess = _session(user["id"], ["profile"], 499)
    for _ in range(3):
        app.fulfill_checkout_session(sess)
    count, total = _payments(app, user["id"])
    assert count == 1, "същата сесия е записана повече от веднъж"
    assert total == 499, "сумата се е удвоила"


def test_one_persons_payment_never_unlocks_for_another(app, db):
    a = db.create_user(f"a-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    b = db.create_user(f"b-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    app.fulfill_checkout_session(_session(a["id"], ["profile"], 499))
    assert "profile" in app.unlocked_features(app.get_user_by_id(a["id"]))
    assert "profile" not in app.unlocked_features(app.get_user_by_id(b["id"]))


def test_session_without_user_id_is_ignored(app, user):
    """Повредена или чужда сесия не бива да отключва нищо."""
    sess = _session(user["id"], ["profile"], 499)
    sess["metadata"]["user_id"] = ""
    sess.pop("client_reference_id", None)
    app.fulfill_checkout_session(sess)
    assert _payments(app, user["id"])[0] == 0


# --- пакетът ----------------------------------------------------------------

def test_bundle_covers_every_sellable_module(app, user):
    bundle = app.bundle_offer(user)
    sellable = {f["key"] for f in app.FEATURE_CATALOGUE
                if not f.get("included") and app.feature_offer(f["key"])}
    assert set(bundle["keys"]) == sellable


def test_bundle_is_cheaper_than_buying_separately(app, user):
    bundle = app.bundle_offer(user)
    assert bundle["price_cents"] < bundle["full_price_cents"]
    assert bundle["saving_cents"] == bundle["full_price_cents"] - bundle["price_cents"]


def test_bundle_excludes_what_is_already_owned(app, user):
    app.fulfill_checkout_session(_session(user["id"], ["profile"], 499))
    bundle = app.bundle_offer(app.get_user_by_id(user["id"]))
    assert "profile" not in bundle["keys"], "пакетът иска пари за вече купено"


def test_bundle_disappears_when_it_would_cost_more(app, user):
    """Никой не бива да бъде подмамен да плати пакет, който е по-скъп."""
    for key in ("akashic", "numerology", "love", "period"):
        offer = app.feature_offer(key)
        app.grant_feature_purchase(user["id"], key, offer["price_cents"], "EUR", None)
    fresh = app.get_user_by_id(user["id"])
    bundle = app.bundle_offer(fresh)
    if bundle:
        assert bundle["price_cents"] < bundle["full_price_cents"]


def test_owner_of_everything_gets_no_bundle(app, user):
    for f in app.FEATURE_CATALOGUE:
        if app.feature_offer(f["key"]):
            app.grant_feature_purchase(user["id"], f["key"], 0, "EUR", None)
    assert app.bundle_offer(app.get_user_by_id(user["id"])) is None


# --- цените -----------------------------------------------------------------

def test_free_modules_are_never_for_sale(app):
    """chart/horoscope/planets/aspects са безплатни — не бива да имат цена."""
    for key in ("chart", "horoscope", "planets", "aspects"):
        assert app.feature_offer(key) is None, f"{key} се предлага за продажба"


def test_every_locked_module_can_be_bought(app, user):
    """Заключен модул без начин за купуване е задънена улица за клиента."""
    unlocked = set(app.unlocked_features(user))
    bundle = app.bundle_offer(user)
    buyable = set(bundle["keys"]) if bundle else set()
    for f in app.FEATURE_CATALOGUE:
        key = f["key"]
        if key in unlocked:
            continue
        assert app.feature_offer(key) or key in buyable, \
            f"{key} е заключен, но не може да се купи по никакъв начин"


# --- когато плащането не тръгне ----------------------------------------------

class _Req:
    """Достатъчно от Request, колкото ползва api_onboard."""
    headers = {"host": "astrokarta.bg"}
    base_url = "https://astrokarta.bg/"
    url = type("U", (), {"scheme": "https"})()

    class client:
        host = "1.2.3.4"


def _onboard(app, email, wanted):
    data = app.OnboardRequest(
        email=email, password="parola123", wanted=wanted,
        name="Тест", year=1990, month=5, day=14, hour=8, minute=30,
        lat=42.7, lon=23.3, timezone="Europe/Sofia")
    return app.api_onboard(data, _Req())


def test_failed_checkout_still_creates_the_account(app, db, monkeypatch):
    """Счупен Stripe не бива да спира регистрацията."""
    monkeypatch.setattr(app, "MOCK_PAYMENTS", False)
    monkeypatch.setattr(app.billing, "stripe_enabled", lambda: True)
    monkeypatch.setattr(app.billing, "create_features_checkout",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("Stripe е долу")))
    result = _onboard(app, f"f-{secrets.token_hex(3)}@example.com", ["profile"])
    assert result["ok"] is True
    assert result.get("checkout_url") is None


def test_failed_checkout_tells_the_customer(app, db, monkeypatch):
    """Мълчаливият провал беше същинският проблем: човекът се озоваваше на
    картата си, без изобщо да го питат за пари."""
    monkeypatch.setattr(app, "MOCK_PAYMENTS", False)
    monkeypatch.setattr(app.billing, "stripe_enabled", lambda: True)
    monkeypatch.setattr(app.billing, "create_features_checkout",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("Stripe е долу")))
    result = _onboard(app, f"f-{secrets.token_hex(3)}@example.com", ["profile"])
    assert result.get("checkout_error"), "клиентът не научава, че плащането е пропаднало"


def test_failed_checkout_remembers_the_choice(app, db, monkeypatch):
    """Изборът не бива да се губи — иначе трябва да се прави наново."""
    monkeypatch.setattr(app, "MOCK_PAYMENTS", False)
    monkeypatch.setattr(app.billing, "stripe_enabled", lambda: True)
    monkeypatch.setattr(app.billing, "create_features_checkout",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("Stripe е долу")))
    email = f"f-{secrets.token_hex(3)}@example.com"
    _onboard(app, email, ["profile", "akashic"])

    with sqlite3.connect(app.DB_PATH) as c:
        uid = c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()[0]

    pending = app.api_my_features(user=(uid, email))["pending"]
    assert pending and set(pending["keys"]) == {"profile", "akashic"}


def test_pending_choice_is_delivered_once(app, db, monkeypatch):
    """Второ отваряне не бива да показва същото предложение пак."""
    monkeypatch.setattr(app, "MOCK_PAYMENTS", False)
    monkeypatch.setattr(app.billing, "stripe_enabled", lambda: True)
    monkeypatch.setattr(app.billing, "create_features_checkout",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("Stripe е долу")))
    email = f"f-{secrets.token_hex(3)}@example.com"
    _onboard(app, email, ["profile"])
    with sqlite3.connect(app.DB_PATH) as c:
        uid = c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()[0]

    assert app.api_my_features(user=(uid, email))["pending"] is not None
    assert app.api_my_features(user=(uid, email))["pending"] is None


def test_failed_checkout_unlocks_nothing(app, db, monkeypatch):
    """Най-важното: провалено плащане не бива да дава достъп."""
    monkeypatch.setattr(app, "MOCK_PAYMENTS", False)
    monkeypatch.setattr(app.billing, "stripe_enabled", lambda: True)
    monkeypatch.setattr(app.billing, "create_features_checkout",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("Stripe е долу")))
    email = f"f-{secrets.token_hex(3)}@example.com"
    _onboard(app, email, ["profile"])
    with sqlite3.connect(app.DB_PATH) as c:
        uid = c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()[0]
    assert "profile" not in app.unlocked_features(app.get_user_by_id(uid))
