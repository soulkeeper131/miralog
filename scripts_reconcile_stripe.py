# -*- coding: utf-8 -*-
"""Сверява платените Stripe сесии срещу отключеното в базата.

Заради липсващ webhook secret е възможно клиент да е платил, а модулът
да не се е отключил. Скриптът минава през сесиите в Stripe и допълва
каквото липсва.

Употреба:
    python scripts_reconcile_stripe.py            # само показва
    python scripts_reconcile_stripe.py --apply    # и отключва
"""
import sys
import app
import billing


def main(apply_changes: bool) -> int:
    if not billing.checkout_key_present():
        print("STRIPE_SECRET_KEY не е зададен — няма какво да се сверява.")
        return 1

    stripe = billing.get_stripe()
    sessions = stripe.checkout.Session.list(limit=100)

    missing, checked = [], 0
    for s in sessions.auto_paging_iter():
        if s.get("payment_status") != "paid":
            continue
        checked += 1
        meta = dict(s.get("metadata") or {})
        try:
            uid = int(meta.get("user_id") or s.get("client_reference_id") or 0)
        except (TypeError, ValueError):
            continue
        if not uid:
            continue

        keys = []
        if meta.get("kind") == "features" and meta.get("feature_keys"):
            keys = [k.strip() for k in meta["feature_keys"].split(",") if k.strip()]
        elif meta.get("kind") == "feature" and meta.get("feature_key"):
            keys = [meta["feature_key"]]
        if not keys:
            continue

        owned = set(app.purchased_features(uid))
        gap = [k for k in keys if k not in owned]
        if gap:
            missing.append((s.get("id"), uid, gap, s.get("amount_total", 0)))

    print(f"Проверени платени сесии: {checked}")
    if not missing:
        print("Всичко платено е отключено. Няма пропуски.")
        return 0

    print(f"\nНамерени {len(missing)} платени, но неотключени:")
    for sid, uid, gap, amount in missing:
        u = app.get_user_by_id(uid)
        who = (u or {}).get("email", f"user {uid}")
        print(f"  {sid}  {who}  ->  {', '.join(gap)}  ({amount/100:.2f})")

    if not apply_changes:
        print("\nПробен режим. Пусни с --apply, за да се отключат.")
        return 0

    for sid, uid, gap, amount in missing:
        session = stripe.checkout.Session.retrieve(sid)
        app.fulfill_checkout_session(dict(session))
        print(f"  отключено: {sid}")
    print(f"\nГотово. Обработени {len(missing)} сесии.")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
