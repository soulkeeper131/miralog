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
    assert sent["payment_method_types"] == ["card"]
    assert sent["metadata"]["ga_cid"] == "1.2" and sent["metadata"]["checkout_ref"] == "r"


def test_locked_message_has_no_gender_or_package(app, user):
    """„не е включена в пакета ти“ — женски род за всичко и „пакет“, какъвто няма."""
    from fastapi import HTTPException
    check = app.require_feature("akashic")
    with pytest.raises(HTTPException) as e:
        check(user=(user["id"], user["email"])) if "user" in check.__code__.co_varnames else check((user["id"], user["email"]))
    msg = e.value.detail["message"]
    assert "пакета" not in msg and "включена" not in msg
