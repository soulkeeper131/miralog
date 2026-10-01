# -*- coding: utf-8 -*-
"""Сверява Stripe с базата: платени сесии без отключване и връщания без запис.

1. Заради липсващ webhook secret е възможно клиент да е платил, а модулът
   да не се е отключил. Скриптът минава през сесиите в Stripe и допълва
   каквото липсва.
2. Връщане, направено в Stripe, преди webhook-ът да слуша `charge.refunded`
   (или докато събитието не стига), липсва в базата — и значи в одиторския
   файл (Н-18). Скриптът го записва с датата на самото връщане, за да влезе
   в месеца, в който парите са върнати.

Употреба:
    python scripts_reconcile_stripe.py            # само показва
    python scripts_reconcile_stripe.py --apply    # и поправя
"""
import datetime
import sqlite3
import sys

import app
import billing

# Stripe обектите от stripe-python 13+ не са dict: .get() и dict() гърмят.
plain = app._strip_stripe


def reconcile_sessions(stripe, apply_changes: bool) -> None:
    sessions = stripe.checkout.Session.list(limit=100)

    missing, checked = [], 0
    for obj in sessions.auto_paging_iter():
        s = plain(obj)
        if not app.session_is_paid(s):
            continue
        checked += 1
        meta = s.get("metadata") or {}
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
        payment = app._payment_by_session(s.get("id"))
        if not gap or (payment and payment.get("voided_at")):
            continue
        if payment and app.refunded_cents(payment["id"]) >= int(payment["amount_cents"] or 0) > 0:
            continue                      # парите са върнати — модулът не се дължи
        # Отключвано веднъж и после липсва: най-често отнето нарочно от админа.
        # --apply не го връща — решава се в админ панела.
        revoked = bool(payment and payment.get("granted_at"))
        missing.append((s.get("id"), uid, gap, s.get("amount_total", 0), revoked))

    print(f"Проверени платени сесии: {checked}")
    if not missing:
        print("Всичко платено е отключено. Няма пропуски.")
        return

    print(f"\nНамерени {len(missing)} платени, но неотключени:")
    for sid, uid, gap, amount, revoked in missing:
        u = app.get_user_by_id(uid)
        who = (u or {}).get("email", f"user {uid}")
        note = "  (отключвано е и после махнато — ако не е нарочно, отключи от админа)" if revoked else ""
        print(f"  {sid}  {who}  ->  {', '.join(gap)}  ({amount/100:.2f}){note}")

    todo = [m for m in missing if not m[4]]
    if not apply_changes:
        if todo:
            print("\nПробен режим. Пусни с --apply, за да се отключат.")
        return

    for sid, uid, gap, amount, revoked in todo:
        app.fulfill_checkout_session(plain(stripe.checkout.Session.retrieve(sid)))
        print(f"  отключено: {sid}")
    print(f"Готово. Обработени {len(todo)} сесии.")


def _payment_for_intent(stripe, intent: str):
    with sqlite3.connect(app.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM payments WHERE payment_intent = ?", (intent,)).fetchone()
    if row:
        return dict(row)
    # По-стари записи нямат payment_intent — търсим сесията му в Stripe.
    found = plain(stripe.checkout.Session.list(payment_intent=intent, limit=1)).get("data") or []
    return app._payment_by_session(found[0].get("id")) if found else None


def reconcile_refunds(stripe, apply_changes: bool) -> None:
    refunds = [plain(r) for r in stripe.Refund.list(limit=100).auto_paging_iter()]
    # pending/succeeded: парите се връщат; failed/canceled се отменят и в Stripe.
    refunds = [r for r in refunds if r.get("status") in ("succeeded", "pending") and r.get("payment_intent")]
    refunds.sort(key=lambda r: (int(r.get("created") or 0), r.get("id") or ""))

    cumulative, missing, unknown = {}, [], []
    for r in refunds:
        intent = r["payment_intent"]
        if isinstance(intent, dict):
            intent = intent.get("id")
        cumulative[intent] = cumulative.get(intent, 0) + int(r.get("amount") or 0)
        payment = _payment_for_intent(stripe, intent)
        if payment is None:
            unknown.append(r)
        elif cumulative[intent] > app.refunded_cents(payment["id"]):
            missing.append((r, intent, cumulative[intent], payment))

    print(f"\nПроверени връщания в Stripe: {len(refunds)}")
    for r in unknown:
        print(f"  ⚠ {r.get('id')}: плащането ({r.get('payment_intent')}) не е в базата — провери ръчно")
    if not missing:
        print("Всички връщания са записани. Няма пропуски.")
        return

    print(f"\nВръщания, които липсват в базата: {len(missing)}")
    for r, intent, cum, payment in missing:
        when = app.utc_to_sofia(datetime.datetime.utcfromtimestamp(int(r["created"])))
        voided = "  (анулирано плащане — извън одиторския файл)" if payment.get("voided_at") else ""
        print(f"  {r['id']}  плащане #{payment['id']}  {int(r['amount']) / 100:.2f}"
              f"  на {when.strftime('%d.%m.%Y %H:%M')}{voided}")

    if not apply_changes:
        print("\nПробен режим. Пусни с --apply, за да се запишат (с датата на връщането).")
        return

    for r, intent, cum, payment in missing:
        # Натрупаната сума до това връщане: записва се само липсващото, така
        # че вече отбелязаното (от webhook-а или ръчно) не се брои втори път.
        app.record_stripe_refund({"payment_intent": intent, "amount_refunded": cum},
                                 r["id"], int(r["created"]))
        print(f"  записано: {r['id']}")
    print(f"Готово. Записани {len(missing)} връщания. Генерирай наново одиторските файлове "
          "за засегнатите месеци (Админ → НАП).")


def main(apply_changes: bool) -> int:
    if not billing.checkout_key_present():
        print("STRIPE_SECRET_KEY не е зададен — няма какво да се сверява.")
        return 1
    stripe = billing.get_stripe()
    reconcile_sessions(stripe, apply_changes)
    reconcile_refunds(stripe, apply_changes)
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
