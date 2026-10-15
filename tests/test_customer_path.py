# -*- coding: utf-8 -*-
"""Пътят на клиента: връщане след плащане, текстове, Stripe на български."""
import pytest


class _Req:
    def __init__(self, return_to=None):
        self.query_params = {"return_to": return_to} if return_to is not None else {}


@pytest.mark.parametrize("wanted,expected", [
    ("/chart/12", "/chart/12"),
    ("/settings", "/settings"),
    (None, "/settings"),
    ("https://evil.example/chart/1", "/settings"),   # само собствени пътища
    ("//evil.example", "/settings"),
    ("/chart/12?x=1", "/settings"),
    ("/admin", "/settings"),
])
def test_return_path_after_payment(app, wanted, expected):
    """Покупката връщаше всеки в Настройки, далеч от разчитането."""
    assert app.purchase_return_path(_Req(wanted)) == expected


def test_checkout_page_is_in_bulgarian(monkeypatch):
    import billing
    sent = {}

    class _S:
        url = "https://checkout.stripe.com/x"

    class _Checkout:
        class Session:
            @staticmethod
            def create(**params):
                sent.update(params)
                return _S()

    class _Stripe:
        checkout = _Checkout

    monkeypatch.setattr(billing, "get_stripe", lambda: _Stripe)
    billing.create_feature_checkout(customer_email="a@b.bg", customer_id=None, user_id=1,
                                    feature_key="moon", feature_name="Луна", amount_cents=299,
                                    currency="EUR", success_url="https://x/s", cancel_url="https://x/c",
                                    extra_metadata={"ga_cid": "1.2", "checkout_ref": "r"})
    assert sent["locale"] == "bg"
    assert sent["allowed_payment_method_types"] == ["card"]
    # API 2026-09-30.endive връща 400 за payment_method_types — всички
    # плащания спряха, когато билдът взе stripe 16.
    assert "payment_method_types" not in sent
    assert sent["metadata"]["ga_cid"] == "1.2" and sent["metadata"]["checkout_ref"] == "r"


def test_locked_message_has_no_gender_or_package(app, user):
    """„не е включена в пакета ти“ — женски род за всичко и „пакет“, какъвто няма."""
    from fastapi import HTTPException
    check = app.require_feature("akashic")
    with pytest.raises(HTTPException) as e:
        check(user=(user["id"], user["email"])) if "user" in check.__code__.co_varnames else check((user["id"], user["email"]))
    msg = e.value.detail["message"]
    assert "пакета" not in msg and "включена" not in msg


def _capture_stripe(monkeypatch):
    import billing
    sent = {}

    class _S:
        url = "https://checkout.stripe.com/x"

    class _Checkout:
        class Session:
            @staticmethod
            def create(**params):
                sent.update(params)
                return _S()

    class _Stripe:
        checkout = _Checkout

    monkeypatch.setattr(billing, "get_stripe", lambda: _Stripe)
    return billing, sent


def test_bundle_is_one_line_on_the_stripe_page(app, monkeypatch):
    """Пакетът се показваше като 6 модула по 4,16 € — като други цени, не като отстъпка."""
    billing, sent = _capture_stripe(monkeypatch)
    keys = ["profile", "period", "love", "akashic", "numerology", "moon"]
    items = app.bundle_line_items(keys)
    billing.create_features_checkout(customer_email="a@b.bg", customer_id=None, user_id=1, items=items,
                                     success_url="https://x/s", cancel_url="https://x/c", bundle=True)
    lines = sent["line_items"]
    assert len(lines) == 1
    assert lines[0]["price_data"]["unit_amount"] == app.BUNDLE_PRICE_CENTS
    assert "Всички модули (6 разчитания)" in lines[0]["price_data"]["product_data"]["name"]
    assert "Акашови записи" in lines[0]["price_data"]["product_data"]["description"]
    # По metadata се отключва и пишат документите — не се е променила.
    meta = sent["metadata"]
    assert meta["bundle"] == "1" and meta["feature_keys"].split(",") == keys
    assert sum(int(x.split(":")[1]) for x in meta["feature_amounts"].split(",")) == app.BUNDLE_PRICE_CENTS


def test_separate_modules_stay_separate_lines(app, monkeypatch):
    billing, sent = _capture_stripe(monkeypatch)
    items = [{"key": "moon", "name": "Луна", "amount_cents": 299, "currency": "EUR"},
             {"key": "love", "name": "Любов", "amount_cents": 500, "currency": "EUR"}]
    billing.create_features_checkout(customer_email="a@b.bg", customer_id=None, user_id=1, items=items,
                                     success_url="https://x/s", cancel_url="https://x/c")
    assert [l["price_data"]["unit_amount"] for l in sent["line_items"]] == [299, 500]
