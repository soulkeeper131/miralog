# -*- coding: utf-8 -*-
"""Проверка: всеки, който е платил, вижда ли това, за което е платил.

САМО ЧЕТЕ. Не пише, не променя и не отключва нищо — може да се пусне на
production спокойно.

Сравнява три неща, които трябва да съвпадат:
  1. Плащанията в дневника (payments)
  2. Отключените модули (feature_purchases)
  3. Какво реално връща unlocked_features() за този човек

Пуска се на сървъра:   python scripts_check_paid_access.py
"""
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402


def money(cents, currency="EUR"):
    return f"{(cents or 0) / 100:.2f} {currency}"


def main():
    print("=" * 74)
    print("ПРОВЕРКА НА ДОСТЪПА НА ПЛАТИЛИТЕ КЛИЕНТИ")
    print(f"база: {app.DB_PATH}")
    print("=" * 74)

    with sqlite3.connect(app.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        charts = conn.execute("SELECT COUNT(*) c FROM persons").fetchone()["c"]
        payments = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount_cents), 0) s FROM payments"
        ).fetchone()
        real = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount_cents), 0) s"
            " FROM payments WHERE method = 'stripe'"
        ).fetchone()

        print()
        print(f"потребители         : {users}")
        print(f"натални карти       : {charts}")
        print(f"плащания (всички)   : {payments['c']}  на стойност {money(payments['s'])}")
        print(f"от тях през Stripe  : {real['c']}  на стойност {money(real['s'])}")

        # Анулирани и изцяло върнати плащания не очакват отключване. Колоните
        # ги има след първия старт на новата версия; без тях — като досега.
        pay_cols = {r[1] for r in conn.execute("PRAGMA table_info(payments)")}
        has_refunds = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'payment_refunds'").fetchone()
        settled = ""
        if "voided_at" in pay_cols:
            settled += " AND p.voided_at IS NULL"
        if has_refunds:
            settled += (" AND p.amount_cents > (SELECT COALESCE(SUM(r.amount_cents), 0)"
                        " FROM payment_refunds r WHERE r.payment_id = p.id)")

        # --- всеки, който е платил през Stripe --------------------------------
        payers = conn.execute(
            "SELECT DISTINCT p.user_id, u.email, u.is_blocked"
            " FROM payments p JOIN users u ON u.id = p.user_id"
            " WHERE p.method = 'stripe' ORDER BY p.user_id"
        ).fetchall()

        print()
        print("-" * 74)
        print(f"ПЛАТИЛИ КЛИЕНТИ: {len(payers)}")
        print("-" * 74)

        problems = []
        for row in payers:
            uid, email = row["user_id"], row["email"]
            paid_rows = conn.execute(
                "SELECT amount_cents, currency, note, paid_at"
                " FROM payments p WHERE p.user_id = ? AND p.method = 'stripe'" + settled +
                " ORDER BY p.paid_at", (uid,)).fetchall()
            granted = [r["feature_key"] for r in conn.execute(
                "SELECT feature_key FROM feature_purchases WHERE user_id = ?", (uid,))]

            user = app.get_user_by_id(uid)
            visible = app.unlocked_features(user) if user else []

            total = sum(r["amount_cents"] for r in paid_rows)
            print()
            print(f"  {email}  (id {uid})")
            print(f"    платил      : {money(total)} в {len(paid_rows)} плащане(ия)")
            for r in paid_rows:
                print(f"       · {money(r['amount_cents'], r['currency'])}  {r['note'] or ''}")
            print(f"    отключено   : {', '.join(sorted(granted)) or '—'}")
            print(f"    реално вижда: {', '.join(sorted(visible)) or '—'}")

            if row["is_blocked"]:
                problems.append(f"{email}: акаунтът е БЛОКИРАН, но е платил")

            # Платил е, но нищо не е отключено.
            if total > 0 and not granted:
                problems.append(f"{email}: платил {money(total)}, но НИЩО не е отключено")

            # Отключено, но не се вижда — значи проверката за достъп го спира.
            missing = [k for k in granted if k not in visible]
            if missing:
                problems.append(
                    f"{email}: {', '.join(missing)} е записано като купено, но не се вижда")

            # Безплатното, което всеки трябва да има.
            for free in ("chart", "horoscope"):
                if free not in visible:
                    problems.append(f"{email}: липсва безплатното „{free}“")

        # --- хора с отключени модули, но без плащане --------------------------
        print()
        print("-" * 74)
        print("ОТКЛЮЧЕНО БЕЗ ПЛАЩАНЕ (админски подаръци, тестове, безплатни)")
        print("-" * 74)
        rows = conn.execute(
            "SELECT u.email, fp.feature_key, fp.price_cents, fp.payment_id"
            " FROM feature_purchases fp JOIN users u ON u.id = fp.user_id"
            " WHERE fp.feature_key NOT IN ('chart', 'horoscope')"
            "   AND (fp.payment_id IS NULL OR fp.price_cents = 0)"
            " ORDER BY u.email").fetchall()
        if rows:
            for r in rows:
                print(f"  {r['email']:<38} {r['feature_key']:<12} {money(r['price_cents'])}")
        else:
            print("  няма")

        # --- плащания без отключване -----------------------------------------
        print()
        print("-" * 74)
        print("ПЛАЩАНИЯ БЕЗ ОТКЛЮЧВАНЕ (най-важното)")
        print("-" * 74)
        orphan = conn.execute(
            "SELECT p.id, u.email, p.amount_cents, p.currency, p.note, p.paid_at"
            " FROM payments p JOIN users u ON u.id = p.user_id"
            " WHERE p.method = 'stripe' AND p.amount_cents > 0" + settled +
            "   AND NOT EXISTS (SELECT 1 FROM feature_purchases fp"
            "                   WHERE fp.payment_id = p.id)"
            " ORDER BY p.paid_at DESC").fetchall()
        if orphan:
            for r in orphan:
                print(f"  ⚠ {r['email']:<34} {money(r['amount_cents'], r['currency'])}"
                      f"  {r['paid_at']}  {r['note'] or ''}")
        else:
            print("  няма — всяко плащане е отключило нещо ✓")

        # --- чакащи избори, чието плащане не е тръгнало -----------------------
        pending = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'pending_purchase_%'"
            " AND value != ''").fetchall()
        if pending:
            print()
            print("-" * 74)
            print("ИЗБРАЛИ МОДУЛИ, НО ПЛАЩАНЕТО НЕ Е ТРЪГНАЛО")
            print("-" * 74)
            for r in pending:
                uid = r["key"].replace("pending_purchase_", "")
                u = app.get_user_by_id(int(uid)) if uid.isdigit() else None
                print(f"  {(u or {}).get('email', 'id ' + uid):<38} {r['value']}")

    print()
    print("=" * 74)
    if problems:
        print(f"НАМЕРЕНИ ПРОБЛЕМИ: {len(problems)}")
        for p in problems:
            print(f"  ⚠ {p}")
    else:
        print("ВСИЧКО Е НАРЕД — всеки платил вижда това, за което е платил ✓")
    print("=" * 74)


if __name__ == "__main__":
    main()
