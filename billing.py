# -*- coding: utf-8 -*-
"""Stripe Checkout helpers for subscriptions and one-off feature unlocks.

Configured via env:
  STRIPE_SECRET_KEY       — required to enable checkout
  STRIPE_WEBHOOK_SECRET   — required to verify webhooks
  STRIPE_SUCCESS_URL      — optional override (default: {base}/settings?paid=1)
  STRIPE_CANCEL_URL       — optional override (default: {base}/settings?paid=0)
"""
from __future__ import annotations

import os
from typing import Optional

STRIPE_SECRET_KEY = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
STRIPE_WEBHOOK_SECRET = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()


def stripe_enabled() -> bool:
    return bool(STRIPE_SECRET_KEY)


def get_stripe():
    """Lazy-import stripe so the app still boots when the package is unused."""
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY не е зададен.")
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    return stripe


def create_subscription_checkout(
    *,
    customer_email: str,
    customer_id: Optional[str],
    user_id: int,
    amount_cents: int,
    currency: str,
    product_name: str,
    success_url: str,
    cancel_url: str,
) -> str:
    """Return a Checkout Session URL for the monthly full plan."""
    stripe = get_stripe()
    params = {
        "mode": "subscription",
        "line_items": [{
            "price_data": {
                "currency": (currency or "eur").lower(),
                "unit_amount": int(amount_cents),
                "recurring": {"interval": "month"},
                "product_data": {"name": product_name},
            },
            "quantity": 1,
        }],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": str(user_id),
        "metadata": {"kind": "subscription", "user_id": str(user_id), "plan_key": "full"},
        "subscription_data": {
            "metadata": {"user_id": str(user_id), "plan_key": "full"},
        },
        "allow_promotion_codes": True,
    }
    if customer_id:
        params["customer"] = customer_id
    else:
        params["customer_email"] = customer_email
    session = stripe.checkout.Session.create(**params)
    return session.url


def create_feature_checkout(
    *,
    customer_email: str,
    customer_id: Optional[str],
    user_id: int,
    feature_key: str,
    feature_name: str,
    amount_cents: int,
    currency: str,
    success_url: str,
    cancel_url: str,
) -> str:
    """Return a Checkout Session URL for a one-off feature unlock."""
    stripe = get_stripe()
    params = {
        "mode": "payment",
        "line_items": [{
            "price_data": {
                "currency": (currency or "eur").lower(),
                "unit_amount": int(amount_cents),
                "product_data": {"name": f"МираСкоп — {feature_name}"},
            },
            "quantity": 1,
        }],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": str(user_id),
        "metadata": {
            "kind": "feature",
            "user_id": str(user_id),
            "feature_key": feature_key,
        },
        "allow_promotion_codes": True,
    }
    if customer_id:
        params["customer"] = customer_id
    else:
        params["customer_email"] = customer_email
    session = stripe.checkout.Session.create(**params)
    return session.url


def cancel_subscription_at_period_end(subscription_id: str) -> dict:
    stripe = get_stripe()
    sub = stripe.Subscription.modify(subscription_id, cancel_at_period_end=True)
    return {
        "id": sub.id,
        "cancel_at_period_end": bool(sub.cancel_at_period_end),
        "current_period_end": getattr(sub, "current_period_end", None),
    }


def construct_webhook_event(payload: bytes, sig_header: str):
    if not STRIPE_WEBHOOK_SECRET:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET не е зададен.")
    stripe = get_stripe()
    return stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
