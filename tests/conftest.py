# -*- coding: utf-8 -*-
"""Обща подготовка за тестовете.

Всеки тест работи върху собствена празна база в временна директория —
DB_PATH се задава ПРЕДИ app да се внесе, защото пътят се чете при внасяне.
Така тестовете никога не пипат data/persons.db.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Пътищата трябва да са зададени, преди app да се внесе за първи път.
_TMP = Path(tempfile.mkdtemp(prefix="astrokarta-tests-"))
os.environ["DB_PATH"] = str(_TMP / "test.db")
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["ENVIRONMENT"] = "development"      # без строгите production проверки
os.environ.pop("STRIPE_SECRET_KEY", None)      # плащанията се симулират
os.environ.pop("STRIPE_WEBHOOK_SECRET", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module                        # noqa: E402

app_module.init_db()


@pytest.fixture
def db():
    """Празни таблиците между тестовете, за да не си влияят."""
    import sqlite3
    with sqlite3.connect(app_module.DB_PATH) as conn:
        for table in ("feature_purchases", "payments", "oauth_accounts",
                      "persons", "ai_cache", "audit_log"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                pass
        conn.execute("DELETE FROM users WHERE role != 'admin'")
        conn.commit()
    yield app_module


@pytest.fixture
def user(db):
    """Нов потребител с безплатните функции, както след регистрация."""
    import secrets
    email = f"t-{secrets.token_hex(4)}@example.com"
    row = db.create_user(email, db.hash_password("parola123"))
    db.grant_signup_features(row["id"])
    return db.get_user_by_id(row["id"])


@pytest.fixture
def app():
    return app_module
