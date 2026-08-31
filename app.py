# -*- coding: utf-8 -*-
import os, re, sys, json, sqlite3, datetime, urllib.parse, urllib.request, urllib.error, secrets, hashlib, asyncio, threading, time
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

# Swiss Ephemeris path
import swisseph as swe
_ephe_path = os.environ.get("SE_EPHE_PATH", str(Path(__file__).resolve().parent / "ephe"))
if os.path.isdir(_ephe_path):
    swe.set_ephe_path(_ephe_path)
    os.environ["SE_EPHE_PATH"] = _ephe_path

from fastapi import FastAPI, Request, Form, File, UploadFile, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordBearer
from immanuel import charts
from immanuel.const import chart, names
from pydantic import BaseModel
from jose import jwt, JWTError
import bcrypt
try:
    import pyotp
except ImportError:  # dev без pyotp — 2FA просто не е налична
    pyotp = None
from translations import (
    tr_sign, tr_object, tr_aspect, tr_moon_phase, tr_movement, tr_shape, tr_house_system, tr_house,
    meaning_sign, meaning_object, meaning_house, meaning_aspect, meaning_movement, meaning_shape, meaning_moon_phase,
    sign_symbol, sign_element, sign_modality,
    sign_aspect, element_pair_meaning, modality_pair_meaning,
    moon_phase_advice, moon_sign_advice,
    ELEMENTS_BG, MODALITIES_BG, ELEMENT_MEANINGS, MODALITY_MEANINGS,
    SIGNS, ZODIAC_ORDER,
)
from numerology import compute_numerology
from bg_text import clean_bg
from pdf_report import build_reading_pdf, build_receipt_pdf, build_invoice_pdf
import billing
import saft
from feature_pages import FEATURE_PAGES, FEATURE_PAGES_BY_SLUG
from horoscope_signs import ZODIAC_SIGNS, ZODIAC_BY_SLUG
from planet_pages import PLANETS, PLANETS_BY_KEY, PLANETS_BY_SLUG
from house_pages import HOUSES, HOUSES_BY_NUM

# --- App Setup ---
BASE_DIR = Path(__file__).parent
# Overridable so a deployment can point the database at a mounted volume;
# without that the file lives inside the container and dies with it.
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "data" / "persons.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
# Uploaded logos live beside the database rather than in static/, so they sit
# on the same persistent volume. Putting them under static/ would mean a new
# deploy wipes the file while the database still points at it.
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", DB_PATH.parent / "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = 60 * 24 * 30  # 30 days

# ENVIRONMENT=production refuses to start on an insecure default, so a live
# deployment can never silently run with the credentials published in the repo.
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT in ("production", "prod")

# Lets the whole purchase flow be walked through without a payment processor:
# the button grants the modules and records a payment marked as a test. Refused
# in production so a live site can never hand out paid modules for free.
MOCK_PAYMENTS = (
    os.environ.get("MOCK_PAYMENTS", "").strip().lower() in ("1", "true", "yes")
    and not IS_PRODUCTION
)

DEV_SECRET_KEY = "change-me-in-production-secret-key"
DEV_ADMIN_PASSWORD = "admin123"
DEV_DEMO_PASSWORD = "demo123"

SECRET_KEY = os.environ.get("SECRET_KEY", DEV_SECRET_KEY)
# The mail domain for the built-in accounts. Everything below derives from it,
# so moving to a new domain is one variable rather than a search-and-replace.
BRAND_DOMAIN = os.environ.get("BRAND_DOMAIN", "astrokarta.bg").strip() or "astrokarta.bg"
# Админ панелът живее на собствен поддомейн — отделен от потребителската част.
# Извежда се от BRAND_DOMAIN, за да не е твърдо кодиран при смяна на домейн.
ADMIN_HOST = f"admin.{BRAND_DOMAIN}".strip().lower()
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", f"admin@{BRAND_DOMAIN}")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", DEV_ADMIN_PASSWORD)
# A standing demo account, so the locked/paywalled views can be checked without
# touching a real user. Set DEMO_EMAIL="" to skip creating it in production.
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "").strip()
# In production the demo account is opt-in: it appears only when a password is
# supplied. Relying on DEMO_EMAIL="" would not work, because some platforms
# (Coolify among them) drop empty environment variables entirely.
if IS_PRODUCTION:
    DEMO_EMAIL = (os.environ.get("DEMO_EMAIL", "").strip() or f"demo@{BRAND_DOMAIN}") \
        if DEMO_PASSWORD else ""
else:
    DEMO_EMAIL = os.environ.get("DEMO_EMAIL", f"demo@{BRAND_DOMAIN}").strip()
    DEMO_PASSWORD = DEMO_PASSWORD or DEV_DEMO_PASSWORD


import logging
log = logging.getLogger("miraskop")

# A Windows console defaults to cp1251 and raises on Cyrillic. Reconfigure the
# streams where possible so startup messages are readable instead of fatal.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class ConfigError(RuntimeError):
    """Raised when the deployment is configured in a way that is not safe to run."""


def check_config() -> list:
    """Validate the environment. Returns warnings; raises on anything unsafe.

    Only production is strict — development keeps working with the defaults so
    nobody has to set variables just to run the app locally.
    """
    problems, warnings = [], []

    def demand(name, value, insecure, hint):
        if value == insecure:
            (problems if IS_PRODUCTION else warnings).append(
                f"{name} е с примерната стойност от кода. {hint}")

    demand("SECRET_KEY", SECRET_KEY, DEV_SECRET_KEY,
           "Задай дълъг случаен низ — иначе всеки може да си направи валиден токен "
           "и да влезе като администратор.")
    demand("ADMIN_PASSWORD", ADMIN_PASSWORD, DEV_ADMIN_PASSWORD,
           "Паролата „admin123“ е публикувана в кода на проекта.")

    if len(SECRET_KEY) < 32 and SECRET_KEY != DEV_SECRET_KEY:
        (problems if IS_PRODUCTION else warnings).append(
            "SECRET_KEY е по-къс от 32 знака. Използвай поне 32 случайни знака.")

    # The account is opt-in above, so the only thing left to guard is a weak
    # password on an account somebody deliberately turned on.
    if IS_PRODUCTION and DEMO_EMAIL:
        if DEMO_PASSWORD == DEV_DEMO_PASSWORD:
            problems.append(
                "DEMO_PASSWORD е „demo123“ — паролата е публикувана в кода. "
                "Задай друга или премахни DEMO_PASSWORD, за да няма демо акаунт.")
        elif len(DEMO_PASSWORD) < 8:
            problems.append(
                "DEMO_PASSWORD е по-къса от 8 знака. Демо акаунтът е публично "
                "достъпен — дай му истинска парола.")

    if problems:
        lines = "\n".join(f"  • {p}" for p in problems)
        raise ConfigError(
            "Приложението не може да стартира с тези настройки:\n\n"
            f"{lines}\n\n"
            "Задай променливите в средата (в Coolify: Environment Variables) и рестартирай.\n"
            "Виж .env.example за пълния списък. Генериране на ключ:\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
        )
    return warnings

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        # Users table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Create default admin if no users exist
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                (ADMIN_EMAIL, hash_password(ADMIN_PASSWORD))
            )
        # Persons table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS persons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                name TEXT NOT NULL,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                day INTEGER NOT NULL,
                hour INTEGER DEFAULT 0,
                minute INTEGER DEFAULT 0,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                timezone TEXT DEFAULT 'Europe/Sofia',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migration: add user_id column if missing, delete orphan persons
        cols = [r[1] for r in conn.execute("PRAGMA table_info(persons)").fetchall()]
        if "user_id" not in cols:
            # Recreate persons table with user_id
            conn.execute("DELETE FROM persons")
            conn.execute("""
                CREATE TABLE persons_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    name TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    day INTEGER NOT NULL,
                    hour INTEGER DEFAULT 0,
                    minute INTEGER DEFAULT 0,
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    timezone TEXT DEFAULT 'Europe/Sofia',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("DROP TABLE persons")
            conn.execute("ALTER TABLE persons_new RENAME TO persons")
        # Settings table (single row of app-wide key/value config, e.g. AI API key)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # AI interpretation cache: avoids re-spending tokens on every tab open.
        # cache_key examples: "natal", "numerology", "horoscope:2026-07-31"
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_cache (
                person_id INTEGER NOT NULL REFERENCES persons(id),
                cache_key TEXT NOT NULL,
                content TEXT NOT NULL,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (person_id, cache_key)
            )
        """)
        # Daily horoscope per zodiac sign (SEO pages, /horoskop/{slug}).
        # One row per sign per calendar day — regenerated each morning.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sign_horoscope (
                sign TEXT NOT NULL,
                date TEXT NOT NULL,
                content TEXT NOT NULL,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (sign, date)
            )
        """)
        # Evergreen "planet in sign" SEO pages (e.g. /luna-v-skorpion).
        # Generated once by AI, cached forever (the meaning never changes).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS planet_sign_cache (
                planet TEXT NOT NULL,
                sign TEXT NOT NULL,
                content TEXT NOT NULL,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (planet, sign)
            )
        """)
        # Evergreen "zodiac sign profile" SEO pages (e.g. /zodia/oven).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sign_profile_cache (
                sign TEXT NOT NULL PRIMARY KEY,
                content TEXT NOT NULL,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Evergreen "sign compatibility" SEO pages (e.g. /savmestimost/oven-telec).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS compatibility_cache (
                sign_a TEXT NOT NULL,
                sign_b TEXT NOT NULL,
                content TEXT NOT NULL,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (sign_a, sign_b)
            )
        """)
        # Evergreen "planet in house" SEO pages (e.g. /luna-v-7-dom).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS planet_house_cache (
                planet TEXT NOT NULL,
                house TEXT NOT NULL,
                content TEXT NOT NULL,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (planet, house)
            )
        """)

        # --- Accounts, plans and billing ---
        # A plan is a named bundle of features; a user points at one and has an
        # expiry date. Everything below is administered by hand for now.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                price_cents INTEGER NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'EUR',
                period TEXT NOT NULL DEFAULT 'month',
                max_persons INTEGER NOT NULL DEFAULT 1,
                features TEXT NOT NULL DEFAULT '[]',
                is_active INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0
            )
        """)
        # One-off purchases: a user buys a single feature outright, on top of
        # whatever plan they hold. Unlike a plan these never expire.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feature_purchases (
                user_id INTEGER NOT NULL REFERENCES users(id),
                feature_key TEXT NOT NULL,
                price_cents INTEGER NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'EUR',
                purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                payment_id INTEGER REFERENCES payments(id),
                PRIMARY KEY (user_id, feature_key)
            )
        """)
        # Per-feature one-off price list, keyed by the FEATURE_CATALOGUE keys.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feature_prices (
                feature_key TEXT PRIMARY KEY,
                price_cents INTEGER NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'EUR',
                is_purchasable INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                plan_key TEXT,
                amount_cents INTEGER NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'EUR',
                method TEXT,
                note TEXT,
                paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                recorded_by INTEGER REFERENCES users(id)
            )
        """)

        # Which social account belongs to which user. Kept in its own table so
        # one person can link both Google and Facebook without either column
        # sitting empty on every password user.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS oauth_accounts (
                provider TEXT NOT NULL,
                provider_user_id TEXT NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id),
                email TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (provider, provider_user_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_oauth_user ON oauth_accounts(user_id)")

        # Stripe redelivers a webhook whenever it is unsure the first attempt
        # landed, and the customer's own return from checkout fulfils the same
        # session. Without a key to recognise a session already handled, one
        # payment lands in the ledger several times.
        pay_cols = [r[1] for r in conn.execute("PRAGMA table_info(payments)").fetchall()]
        if "stripe_session_id" not in pay_cols:
            conn.execute("ALTER TABLE payments ADD COLUMN stripe_session_id TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_session"
            " ON payments(stripe_session_id) WHERE stripe_session_id IS NOT NULL")

        # Фактури по ЗДДС — поредната номерация (10 цифри, чл. 113 ЗДДС) се
        # генерира от AUTOINCREMENT; фактури никога не се трият/преизползват.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT UNIQUE,
                payment_id INTEGER REFERENCES payments(id),
                user_id INTEGER REFERENCES users(id),
                issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Columns added to users after the first release.
        user_cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        for col, ddl in [
            ("role", "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'"),
            ("plan_key", "ALTER TABLE users ADD COLUMN plan_key TEXT DEFAULT 'demo'"),
            ("plan_expires", "ALTER TABLE users ADD COLUMN plan_expires TIMESTAMP"),
            ("is_blocked", "ALTER TABLE users ADD COLUMN is_blocked INTEGER NOT NULL DEFAULT 0"),
            ("note", "ALTER TABLE users ADD COLUMN note TEXT"),
            ("last_login", "ALTER TABLE users ADD COLUMN last_login TIMESTAMP"),
            ("display_name", "ALTER TABLE users ADD COLUMN display_name TEXT"),
            ("stripe_customer_id", "ALTER TABLE users ADD COLUMN stripe_customer_id TEXT"),
            ("stripe_subscription_id", "ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT"),
            ("digest_opt_in", "ALTER TABLE users ADD COLUMN digest_opt_in INTEGER NOT NULL DEFAULT 0"),
            ("totp_secret", "ALTER TABLE users ADD COLUMN totp_secret TEXT"),
            ("lifecycle_expiring_for", "ALTER TABLE users ADD COLUMN lifecycle_expiring_for TEXT"),
            ("lifecycle_expired_for", "ALTER TABLE users ADD COLUMN lifecycle_expired_for TEXT"),
            ("last_digest_on", "ALTER TABLE users ADD COLUMN last_digest_on TEXT"),
        ]:
            if col not in user_cols:
                conn.execute(ddl)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS password_resets (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS share_links (
                token TEXT PRIMARY KEY,
                person_id INTEGER NOT NULL REFERENCES persons(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                cache_key TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id),
                actor_email TEXT,
                event TEXT NOT NULL,
                detail TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id)")

        # The seeded admin predates the role column, so claim it here.
        conn.execute("UPDATE users SET role = 'admin' WHERE email = ? AND role != 'admin'",
                     (ADMIN_EMAIL,))

        # Installations from before the brand became configurable have the old
        # name baked into their saved SEO title. Swap it for the {brand}
        # placeholder so a rename reaches the search results too; a title an
        # admin has since rewritten by hand is left exactly as it is.
        legacy_seo_title = "МираСкоп — твоята натална карта, разчетена на разбираем език"
        conn.execute("UPDATE settings SET value = ? WHERE key = 'seo_title' AND value = ?",
                     (SEO_DEFAULTS["seo_title"], legacy_seo_title))
        # Same for a share image still pointing at the bundled logo: blank means
        # "follow the logo", which is what an uploaded mark should replace.
        conn.execute("UPDATE settings SET value = '' "
                     "WHERE key = 'seo_og_image' AND value = '/static/logo-header.png'")
        # Logos uploaded before they moved onto the data volume are unreachable
        # at their old path, so clear them rather than serve a broken image.
        conn.execute("UPDATE settings SET value = '' "
                     "WHERE key IN ('brand_logo', 'brand_logo_full') "
                     "AND value LIKE '/static/uploads/%'")

        # A demo account on the demo plan, for checking what a paying customer
        # does and does not see. It is deliberately never an admin.
        if DEMO_EMAIL:
            exists = conn.execute("SELECT COUNT(*) FROM users WHERE email = ?",
                                  (DEMO_EMAIL,)).fetchone()[0]
            if not exists:
                cur = conn.execute(
                    "INSERT INTO users (email, password_hash, role, plan_key, note)"
                    " VALUES (?, ?, 'user', 'demo', ?)",
                    (DEMO_EMAIL, hash_password(DEMO_PASSWORD),
                     "Тестов акаунт за проверка на заключените функции."))
                # Give it a chart so every tab has something to render.
                conn.execute(
                    "INSERT INTO persons (user_id, name, year, month, day, hour, minute,"
                    " lat, lon, timezone) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (cur.lastrowid, "Демо Профил", 1990, 6, 15, 12, 30,
                     42.6977, 23.3219, "Europe/Sofia"))

        # Seed the one-off price list. Everything in the paid plan can also be
        # bought on its own, at a price that only makes sense for one feature.
        if conn.execute("SELECT COUNT(*) FROM feature_prices").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO feature_prices (feature_key, price_cents, currency, is_purchasable)"
                " VALUES (?, ?, 'EUR', ?)",
                [
                    ("profile", 500, 1),
                    # Free with every chart: the daily reading is what brings
                    # somebody back, so it is the hook, not a product.
                    ("horoscope", 0, 0),
                    ("period", 500, 1),
                    ("synastry", 700, 1),
                    ("love", 500, 1),
                    ("akashic", 900, 1),
                    ("moon", 300, 1),
                    # The chart is granted free at onboarding, so it is never
                    # offered for sale; planets and aspects ride along with it.
                    ("chart", 0, 0),
                    ("planets", 0, 0),
                    ("aspects", 0, 0),
                    ("numerology", 400, 1),
                ])

        # Older installs gave the demo plan a free chart and numerology.
        conn.execute(
            "UPDATE plans SET name = 'Основен', features = ?"
            " WHERE key = 'demo' AND features LIKE '%chart%'",
            (json.dumps(["planets", "aspects"]),))

        # Accounts created before the chart became free were never granted it,
        # so their own chart page would 402. Give it to anyone who has a person.
        conn.execute(
            "INSERT OR IGNORE INTO feature_purchases (user_id, feature_key, price_cents, currency)"
            " SELECT DISTINCT user_id, 'chart', 0, 'EUR' FROM persons")

        # Synastry folded into the love module: one purchase covers both. Anybody
        # who bought it separately keeps their access through that key.
        conn.execute(
            "UPDATE feature_prices SET price_cents = 0, is_purchasable = 0"
            " WHERE feature_key = 'synastry'")
        conn.execute(
            "INSERT OR IGNORE INTO feature_purchases (user_id, feature_key, price_cents, currency)"
            " SELECT user_id, 'love', 0, 'EUR' FROM feature_purchases"
            " WHERE feature_key = 'synastry'")

        # The daily horoscope used to cost 3 EUR; it now comes with the chart.
        conn.execute(
            "UPDATE feature_prices SET price_cents = 0, is_purchasable = 0"
            " WHERE feature_key = 'horoscope'")
        conn.execute(
            "INSERT OR IGNORE INTO feature_purchases (user_id, feature_key, price_cents, currency)"
            " SELECT DISTINCT user_id, 'horoscope', 0, 'EUR' FROM persons")

        # „Пълно разчитане“ was withdrawn: the module, its endpoint and its
        # price are gone, so clear the leftover rows rather than leave a key
        # nothing can serve.
        conn.execute("DELETE FROM feature_prices WHERE feature_key = 'interpretation'")
        conn.execute("DELETE FROM feature_purchases WHERE feature_key = 'interpretation'")
        for plan_key, feats_json in conn.execute(
                "SELECT key, features FROM plans WHERE features LIKE '%interpretation%'").fetchall():
            try:
                feats = [f for f in json.loads(feats_json) if f != "interpretation"]
            except Exception:
                continue
            conn.execute("UPDATE plans SET features = ? WHERE key = ?",
                         (json.dumps(feats), plan_key))

        # The monthly plan is gone: everything is a one-off purchase now.
        # Anybody who paid for a subscription keeps what they paid for, turned
        # into permanent purchases — an expiry date would otherwise lock them
        # out of modules they already bought. This runs before the plan rows
        # are rewritten, so it still sees what each plan granted.
        paid_plans = {
            key: feats for key, feats in conn.execute(
                "SELECT key, features FROM plans WHERE key != 'demo'").fetchall()
        }
        for user_id, plan_key in conn.execute(
                "SELECT id, plan_key FROM users WHERE plan_key IS NOT NULL"
                " AND plan_key != 'demo'").fetchall():
            try:
                feats = json.loads(paid_plans.get(plan_key) or "[]")
            except Exception:
                continue
            for key in feats:
                conn.execute(
                    "INSERT OR IGNORE INTO feature_purchases"
                    " (user_id, feature_key, price_cents, currency)"
                    " VALUES (?, ?, 0, 'EUR')", (user_id, key))
        # With the modules now owned outright, the expiry date has no meaning.
        conn.execute("UPDATE users SET plan_expires = NULL WHERE plan_expires IS NOT NULL")
        # The lifecycle emails announced an expiry that can no longer happen.
        conn.execute("DELETE FROM settings WHERE key IN"
                     " ('tpl_expiring_subject', 'tpl_expiring_body',"
                     "  'tpl_expired_subject', 'tpl_expired_body')")
        # „Пълен достъп“ was the monthly plan. Everyone keeps their modules via
        # the purchases written above, so the plan row itself is retired and
        # every account returns to the shared baseline.
        conn.execute("UPDATE users SET plan_key = 'demo' WHERE plan_key != 'demo'")
        conn.execute("DELETE FROM plans WHERE key != 'demo'")

        # The chart was briefly sold for 9 EUR; it is now granted at signup,
        # so any install that seeded the old price must stop offering it.
        conn.execute(
            "UPDATE feature_prices SET price_cents = 0, is_purchasable = 0"
            " WHERE feature_key = 'chart'")

        # Features added after a plan was first seeded do not appear in existing
        # rows, so the paid plan would silently lose access to them.
        for key, feature in []:
            row = conn.execute("SELECT features FROM plans WHERE key = ?", (key,)).fetchone()
            if not row:
                continue
            try:
                feats = json.loads(row[0])
            except Exception:
                continue
            if feature not in feats:
                feats.append(feature)
                conn.execute("UPDATE plans SET features = ? WHERE key = ?",
                             (json.dumps(feats), key))

        # One baseline every account shares. There are no tiers to sell any
        # more: every reading is bought outright, so this row only fixes how
        # many charts an account may hold. "planets" and "aspects" ride along
        # with a chart, which is granted free at signup.
        if conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO plans (key, name, price_cents, currency, period, max_persons, features, sort_order)"
                " VALUES ('demo', 'Основен', 0, 'EUR', 'once', 2, ?, 0)",
                (json.dumps(["planets", "aspects"]),))

        # The first account created is the administrator. Admins bypass every
        # gate by role, so the plan they sit on does not matter.
        conn.execute(
            "UPDATE users SET role = 'admin' WHERE email = ?",
            (ADMIN_EMAIL,)
        )
        conn.commit()

# Колко тежки заявки да вървят едновременно. Съобразено е с 1 CPU / 512MB;
# на по-голям контейнер се вдига през променлива на средата.
AI_THREAD_LIMIT = int(os.environ.get("AI_THREAD_LIMIT", "8"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail loudly before serving a single request rather than running insecurely.
    # Logging, not print(): a Windows console defaults to cp1251 and would
    # raise UnicodeEncodeError on Cyrillic.
    for warning in check_config():
        log.warning(warning)
    if not IS_PRODUCTION:
        log.info("ENVIRONMENT=%s - proverkite za produkciya sa izklyucheni.", ENVIRONMENT)
    if billing.stripe_enabled():
        log.info("Stripe checkout е активен.")
    else:
        log.info("Stripe не е конфигуриран — плащанията остават ръчни / заявка.")
    init_db()
    clear_smtp_db_settings()

    # Синхронните рутове (AI разчитания, TTS, PDF) вървят в нишковия пул на
    # anyio. Дефолтът е 40 — при 512MB и 1 CPU толкова паралелни тежки заявки
    # изяждат паметта, вместо да се редят на опашка. Малък пул значи по-дълго
    # чакане при пик, но контейнерът остава жив.
    try:
        from anyio.to_thread import current_default_thread_limiter
        limiter = current_default_thread_limiter()
        limiter.total_tokens = AI_THREAD_LIMIT
        log.info("Нишков пул: %d едновременни заявки.", AI_THREAD_LIMIT)
    except Exception as exc:  # anyio смени API-то → продължаваме с дефолта
        log.warning("Нишковият пул остана по подразбиране: %s", exc)

    job_task = asyncio.create_task(_background_jobs_loop())
    try:
        yield
    finally:
        job_task.cancel()
        try:
            await job_task
        except asyncio.CancelledError:
            pass

# --- Brand ---
# The app's name and logo live here, not scattered through the templates.
# Three tiers, most specific first: what an admin saved, then the environment,
# then the built-in default. Renaming the app is one field in the admin panel.
BRAND_DEFAULTS = {
    "brand_name": os.environ.get("BRAND_NAME", "АстроКарта").strip() or "АстроКарта",
    "brand_tagline": os.environ.get("BRAND_TAGLINE", "Астрология с точността на астрономията").strip(),
    "brand_domain": BRAND_DOMAIN,
    "brand_logo": "/static/logo-header.png",
    "brand_logo_full": "/static/logo-full.webp",
}

# Юридически данни за Политиката за поверителност и Общите условия.
# ⚠️ ВАЖНО: това са ПЛЕЙСХОЛДЪРИ (маркирани с [[...]]). Преди публично пускане
# попълни реалните данни ТУК (едно място) — сменят се във всички документи.
# Ключовете се четат и от settings (legal_*), така че могат да се зададат
# и от админ панела без промяна в кода.
LEGAL_DEFAULTS = {
    "company_name": "[[НАИМЕНОВАНИЕ И ПРАВНА ФОРМА НА ТЪРГОВЕЦА]]",
    "company_id": "[[ЕИК]]",
    # ДДС номер по чл. 94, ал. 2 ЗДДС — „BG" + ЕИК (регистриран по ЗДДС).
    "vat_number": "[[ДДС НОМЕР (BG...)]]",
    "address": "[[АДРЕС НА СЕДАЛИЩЕ]]",
    "privacy_email": "[[ИМЕЙЛ ЗА ЗАЩИТА НА ЛИЧНИТЕ ДАННИ]]",
    # Длъжностно лице по защита на данните (DPO) — празно = „няма назначено“.
    "dpo": "",
    # Наредба № Н-18 — данни за Стандартизирания одиторски файл (SAF-T).
    # e_shop_n се получава при регистрация на е-магазина в НАП (Приложение № 33).
    "e_shop_n": "[[НОМЕР НА Е-МАГАЗИН ОТ НАП (RF...)]]",
    # e_shop_type: 1 = собствен сайт, 2 = продажби през маркетплейс.
    "e_shop_type": "1",
}

def legal() -> dict:
    """Юридическите данни за документите, с fallback към плейсхолдърите."""
    return {k: (get_setting(f"legal_{k}") or default)
            for k, default in LEGAL_DEFAULTS.items()}

# Social sign-in. Empty credentials mean the button is not shown at all —
# an OAuth button that cannot complete is worse than no button.
OAUTH_DEFAULTS = {
    "google_client_id": "",
    "google_client_secret": "",
    "facebook_app_id": "",
    "facebook_app_secret": "",
}

def oauth_config() -> dict:
    """Credentials, with the environment winning over the database.

    Secrets belong in the deployment's environment; the database entries exist
    so a small install can be configured from the admin panel instead.
    """
    out = {}
    for key in OAUTH_DEFAULTS:
        out[key] = (os.environ.get(key.upper()) or get_setting(f"oauth_{key}") or "").strip()
    return out

def oauth_providers() -> dict:
    """Which buttons to show. Both halves of a pair are required."""
    cfg = oauth_config()
    return {
        "google": bool(cfg["google_client_id"] and cfg["google_client_secret"]),
        "facebook": bool(cfg["facebook_app_id"] and cfg["facebook_app_secret"]),
    }

def brand() -> dict:
    """The current brand, with saved values overriding the defaults.

    Exposed to every template as `brand`, so a rename never means editing
    markup. Uploaded logos fall back to the bundled files when unset.
    """
    values = {key: (get_setting(key) or default)
              for key, default in BRAND_DEFAULTS.items()}
    return {
        "name": values["brand_name"],
        "tagline": values["brand_tagline"],
        "domain": values["brand_domain"],
        "logo": values["brand_logo"],
        "logo_full": values["brand_logo_full"],
        "slug": brand_slug(values["brand_name"]),
    }

# ASCII fallback for file names: Cyrillic brand names sanitize to an empty
# string, so the slug cannot always be derived from them.
BRAND_SLUG = "AstroKarta"

def brand_slug(name: Optional[str] = None) -> str:
    """ASCII slug of the brand name for file names; falls back to BRAND_SLUG."""
    return re.sub(r"[^0-9A-Za-z-]+", "-",
                  name if name is not None else brand_name()).strip("-") or BRAND_SLUG

def brand_name() -> str:
    """Shorthand for the places that only need the name (emails, PDFs)."""
    return get_setting("brand_name") or BRAND_DEFAULTS["brand_name"]

templates = Jinja2Templates(directory="templates")
# Fix for Jinja2 3.1.6 + Starlette 1.0.1: request object is not hashable
templates.env.cache_size = 0
# `brand` is a global rather than per-route context: every template needs it,
# and it is a callable so an admin's rename shows up without a restart.
templates.env.globals["brand"] = brand
templates.env.globals["oauth_providers"] = oauth_providers
# Юридически данни за /privacy и /terms — глобал, за да се попълват от
# едно място (LEGAL_DEFAULTS) и да се виждат във всички документи.
templates.env.globals["legal"] = legal
# GA4 measurement id, resolved lazily so an admin can change it without a
# restart. Empty string means "no analytics" — the consent layer keeps gtag
# dormant until the visitor opts in anyway.
templates.env.globals["ga_id"] = lambda: (seo_settings().get("analytics_id") or "").strip()
templates.env.globals["fb_pixel_id"] = lambda: (seo_settings().get("fb_pixel_id") or "").strip()
# Админ поддомейн — login.html го ползва, за да пренасочи админа към панела.
templates.env.globals["admin_host"] = ADMIN_HOST
# Основният (потребителски) домейн — за линкове „обратно към сайта/таблото“.
templates.env.globals["main_domain"] = BRAND_DOMAIN

app = FastAPI(title=BRAND_DEFAULTS["brand_name"], lifespan=lifespan)
# .webp не е в mimetypes по подразбиране на някои среди → сервира се като
# octet-stream и някои клиенти отказват да го рендерират. Регистрираме го.
import mimetypes as _mimetypes
_mimetypes.add_type("image/webp", ".webp")
app.mount("/static", StaticFiles(directory="static"), name="static")
# Admin-uploaded files (logos) are served from their own mount because they
# live on the data volume, not in the image.
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

# --- Админ изолация по хост ---
# Админ панелът се обслужва САМО на admin.<домейн>. На потребителския домейн
# /admin и /api/admin/* връщат 404 (скрит surface), а на админ поддомейна всичко
# освен админ + auth + static пътища е блокирано — чиста изолация без втори процес.
_ADMIN_HOST_ALLOWED_EXACT = {
    "/", "/admin", "/login", "/healthz", "/api/auth/login", "/api/auth/me",
}
_ADMIN_HOST_ALLOWED_PREFIXES = ("/api/admin/", "/static/", "/uploads/")


@app.middleware("http")
async def admin_host_guard(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    path = request.url.path or "/"
    is_admin_host = host == ADMIN_HOST
    is_admin_path = path == "/admin" or path.startswith("/api/admin/")

    if is_admin_path and not is_admin_host:
        # Админът не се вижда от потребителския домейн.
        return JSONResponse({"detail": "Не е намерено."}, status_code=404)

    if is_admin_host:
        allowed = (
            path in _ADMIN_HOST_ALLOWED_EXACT
            or any(path.startswith(p) for p in _ADMIN_HOST_ALLOWED_PREFIXES)
        )
        if not allowed:
            return JSONResponse({"detail": "Не е намерено."}, status_code=404)
        if path == "/":
            return RedirectResponse("/admin")

    return await call_next(request)


# --- Кеширане на статични ресурси (performance) ---
# Bundled static файловете (CSS/JS/лога в static/) не се менят между deploy-и,
# затова се кешират дълго. При промяна на файл се bump-ва версията в шаблоните
# (?v=N), което сменя URL-а и браузърът тегли наново.
_STATIC_CACHE = "public, max-age=31536000, immutable"
_UPLOADS_CACHE = "public, max-age=86400"


@app.middleware("http")
async def cache_static(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path or "/"
    if path.startswith("/static/"):
        response.headers["Cache-Control"] = _STATIC_CACHE
    elif path.startswith("/uploads/"):
        response.headers["Cache-Control"] = _UPLOADS_CACHE
    return response


async def _background_jobs_loop():
    """Hourly lifecycle + digest emails. Failures are logged, never crash the app."""
    await asyncio.sleep(15)
    while True:
        try:
            await asyncio.to_thread(run_scheduled_jobs)
        except Exception:
            log.exception("Фоновите задачи за имейли се провалиха")
        await asyncio.sleep(3600)

# --- Pydantic Models ---
class BirthDataUpdate(BaseModel):
    year: int
    month: int
    day: int
    hour: int = 0
    minute: int = 0
    lat: float
    lon: float
    timezone: str = "Europe/Sofia"

class SynastryRequest(BaseModel):
    person1_id: int
    person2_id: int

class LoveMatchRequest(BaseModel):
    person_id: int
    # Sign-only mode: all we know is the partner's sun sign.
    partner_sign: Optional[str] = None  # English sign name, e.g. "Taurus"
    # Full-chart mode: real birth data, so the reading can use their whole chart.
    partner_name: Optional[str] = None
    partner_year: Optional[int] = None
    partner_month: Optional[int] = None
    partner_day: Optional[int] = None
    partner_hour: Optional[int] = 12
    partner_minute: Optional[int] = 0
    partner_lat: Optional[float] = None
    partner_lon: Optional[float] = None
    partner_timezone: Optional[str] = "Europe/Sofia"

    def has_full_chart(self) -> bool:
        return None not in (self.partner_year, self.partner_month, self.partner_day,
                            self.partner_lat, self.partner_lon)

    def as_person(self) -> dict:
        return {
            "name": (self.partner_name or "Партньор").strip() or "Партньор",
            "year": self.partner_year, "month": self.partner_month, "day": self.partner_day,
            "hour": self.partner_hour or 0, "minute": self.partner_minute or 0,
            "lat": self.partner_lat, "lon": self.partner_lon,
            "timezone": self.partner_timezone or "Europe/Sofia",
        }

class TransitsRequest(BaseModel):
    person_id: int
    target_date: str  # ISO format: "2026-08-15T12:00:00"

class PeriodRequest(BaseModel):
    person_id: int
    start_date: str  # ISO date: "2026-08-01"
    end_date: str    # ISO date: "2026-08-31"

class AuthRequest(BaseModel):
    email: str
    password: str
    totp_code: Optional[str] = None

# --- Auth Helpers ---
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())

def create_token(user_id: int, email: str) -> str:
    expire = datetime.datetime.utcnow() + datetime.timedelta(minutes=TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": expire
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

# --- Rate limiting за login (brute-force защита, in-memory) ---
import time as _time
_LOGIN_FAILURES: dict = {}
_LOGIN_WINDOW = 900      # прозорец 15 мин
_LOGIN_MAX_FAILS = 5     # max провалени опита
_LOGIN_LOCKOUT = 900     # блокиране 15 мин

def _login_blocked(key: str) -> bool:
    now = _time.monotonic()
    fails = [t for t in _LOGIN_FAILURES.get(key, []) if now - t < _LOGIN_WINDOW]
    return len(fails) >= _LOGIN_MAX_FAILS

def _login_record_failure(key: str) -> None:
    now = _time.monotonic()
    _LOGIN_FAILURES[key] = [t for t in _LOGIN_FAILURES.get(key, []) if now - t < _LOGIN_WINDOW]
    _LOGIN_FAILURES[key].append(now)

def _login_clear(key: str) -> None:
    _LOGIN_FAILURES.pop(key, None)

# --- TOTP (2FA) ---
def generate_totp_secret() -> str:
    if pyotp is None:
        raise HTTPException(500, "Двуфакторната автентикация не е налична (липсва pyotp).")
    return pyotp.random_base32()

def verify_totp(secret: str, code: str) -> bool:
    if not pyotp or not secret or not code:
        return False
    try:
        return pyotp.TOTP(secret).verify(str(code).strip(), valid_window=1)
    except Exception:
        return False

def totp_uri(secret: str, email: str, issuer: str) -> str:
    if not pyotp:
        return ""
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)

def get_current_user(request: Request, token: Optional[str] = Depends(oauth2_scheme)) -> Tuple[int, str]:
    """Dependency that returns (user_id, email) from valid JWT token."""
    if not token:
        raise HTTPException(401, "Не си влязъл в профила си. Влез отново.")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
        email = payload["email"]
        return user_id, email
    except JWTError:
        raise HTTPException(401, "Сесията изтече. Влез отново.")

def get_current_user_flex(request: Request) -> Tuple[int, str]:
    """JWT от Authorization header, ?token= или miralog_token cookie.

    Нужен за <audio>/<img> тагове (напр. гласово четене), които не могат да
    слагат Authorization header — там токенът идва през cookie.
    """
    token = _token_from_request(request)
    if not token:
        raise HTTPException(401, "Не си влязъл в профила си. Влез отново.")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload["sub"]), payload["email"]
    except JWTError:
        raise HTTPException(401, "Сесията изтече. Влез отново.")

def get_user_by_id(user_id: int) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

def get_plan(plan_key: Optional[str]) -> Optional[dict]:
    if not plan_key:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM plans WHERE key = ?", (plan_key,)).fetchone()
        if not row:
            return None
        plan = dict(row)
        try:
            plan["features"] = json.loads(plan["features"])
        except Exception:
            plan["features"] = []
        return plan

def effective_plan(user: dict) -> dict:
    """The plan in force. Nothing expires any more — modules are bought outright.

    The plan survives only as the baseline every account starts from; what a
    customer paid for lives in feature_purchases and never lapses.
    """
    return get_plan(user.get("plan_key")) or get_plan("demo") or {
        "key": "demo", "name": "Основен", "max_persons": 2,
        "features": ["planets", "aspects"],
    }

def purchased_features(user_id: int) -> list:
    """Feature keys the user bought outright. These never expire."""
    with sqlite3.connect(DB_PATH) as conn:
        return [r[0] for r in conn.execute(
            "SELECT feature_key FROM feature_purchases WHERE user_id = ?", (user_id,))]

# Modules that need more than one chart to be usable carry their own allowance.
# The love reading compares two people, so buying it while capped at two would
# leave the customer unable to add the partner they bought it for.
FEATURE_PERSON_GRANTS = {"love": 1}

# One payment for every module, cheaper than buying them one by one. It is not
# a plan: it grants the same individual purchases, so there is still only one
# way an account can own something.
BUNDLE_KEY = "bundle"
BUNDLE_NAME = "Всички модули"
BUNDLE_PRICE_CENTS = 2500

def bundle_offer(user: dict) -> Optional[dict]:
    """The bundle as it applies to this account, or None when it cannot help.

    Somebody who already owns everything has nothing to buy; somebody holding
    one module still sees the full price, because the bundle is a fixed offer
    rather than a running total.
    """
    unlocked = set(unlocked_features(user))
    missing = [f["key"] for f in FEATURE_CATALOGUE
               if not f.get("included") and f["key"] not in unlocked
               and feature_offer(f["key"])]
    if len(missing) < 2:
        return None          # one module left is cheaper on its own
    full_price = sum(feature_offer(k)["price_cents"] for k in missing)
    if full_price <= BUNDLE_PRICE_CENTS:
        return None          # never offer a "discount" that costs more
    return {
        "key": BUNDLE_KEY,
        "name": BUNDLE_NAME,
        "keys": missing,
        "price_cents": BUNDLE_PRICE_CENTS,
        "full_price_cents": full_price,
        "saving_cents": full_price - BUNDLE_PRICE_CENTS,
        "currency": "EUR",
    }

def public_bundle() -> Optional[dict]:
    """The bundle over every sellable module, for visitors with no account yet.

    A brand-new visitor has nothing unlocked, so the bundle is simply "all the
    paid modules together". It only exists when that actually saves money —
    the same rule the account-aware `bundle_offer` applies.
    """
    keys = [f["key"] for f in FEATURE_CATALOGUE
            if not f.get("included") and feature_offer(f["key"])]
    if len(keys) < 2:
        return None
    full_price = sum(feature_offer(k)["price_cents"] for k in keys)
    if full_price <= BUNDLE_PRICE_CENTS:
        return None
    return {
        "key": BUNDLE_KEY,
        "name": BUNDLE_NAME,
        "keys": keys,
        "price_cents": BUNDLE_PRICE_CENTS,
        "full_price_cents": full_price,
        "saving_cents": full_price - BUNDLE_PRICE_CENTS,
        "currency": "EUR",
    }

def bundle_line_items(keys: list) -> list:
    """Split the bundle price across the keys so Stripe charges exactly the
    bundle price while the webhook still sees every key it can grant."""
    share = BUNDLE_PRICE_CENTS // len(keys)
    remainder = BUNDLE_PRICE_CENTS - share * len(keys)
    items = []
    for i, key in enumerate(keys):
        offer = feature_offer(key)
        items.append({
            "key": key,
            "name": offer["name"],
            "amount_cents": share + (remainder if i == 0 else 0),
            "currency": offer["currency"],
        })
    return items

def person_limit(user: dict) -> Optional[int]:
    """How many charts this account may keep. None means unlimited.

    The plan sets the floor; modules that compare people raise it, so a
    purchase never lands the customer against a wall it created.
    """
    if user.get("role") == "admin":
        return None
    base = effective_plan(user).get("max_persons") or 0
    if not base:
        return None
    extra = sum(FEATURE_PERSON_GRANTS.get(key, 0)
                for key in set(purchased_features(user["id"])))
    return base + extra

def unlocked_features(user: dict) -> list:
    """Everything the user may reach: the plan's features plus one-off purchases.

    Admins get the whole catalogue.
    """
    if user.get("role") == "admin":
        return [f["key"] for f in FEATURE_CATALOGUE]
    keys = list(effective_plan(user).get("features", []))
    for key in purchased_features(user["id"]):
        if key not in keys:
            keys.append(key)
    return keys

def get_feature_prices() -> dict:
    """The one-off price list, keyed by feature. Missing rows mean 'not for sale'."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return {r["feature_key"]: dict(r) for r in
                conn.execute("SELECT * FROM feature_prices")}

def feature_offer(feature_key: str) -> Optional[dict]:
    """What a single feature costs, or None when it isn't sold separately."""
    row = get_feature_prices().get(feature_key)
    if not row or not row["is_purchasable"] or row["price_cents"] <= 0:
        return None
    meta = next((f for f in FEATURE_CATALOGUE if f["key"] == feature_key), {})
    return {
        "key": feature_key,
        "name": meta.get("name", feature_key),
        "note": meta.get("note", ""),
        "price_cents": row["price_cents"],
        "currency": row["currency"],
    }

def require_admin(user: Tuple[int, str] = Depends(get_current_user)) -> dict:
    """Dependency for the admin area."""
    row = get_user_by_id(user[0])
    if not row or row.get("role") != "admin":
        raise HTTPException(403, "Нужни са администраторски права.")
    return row

def require_feature(feature: str):
    """Dependency factory gating a feature behind the user's plan."""
    def _check(user: Tuple[int, str] = Depends(get_current_user)) -> Tuple[int, str]:
        row = get_user_by_id(user[0])
        if not row:
            raise HTTPException(401, "Невалиден акаунт.")
        if row.get("is_blocked"):
            raise HTTPException(403, "Акаунтът е блокиран.")
        if row.get("role") == "admin":
            return user
        if feature not in unlocked_features(row):
            # 402 carries the offer, so the UI can show the price on the blurred
            # panel instead of a bare refusal.
            offer = feature_offer(feature)
            meta = next((f for f in FEATURE_CATALOGUE if f["key"] == feature), {})
            # A withdrawn module has no catalogue entry, so there is no Bulgarian
            # name to show and nothing to sell. Naming the raw key would leak
            # English at the customer; say plainly that it is unavailable.
            name = meta.get("name") or (offer or {}).get("name")
            if not name:
                message = "Тази възможност не е достъпна в момента."
            elif not offer:
                message = f"„{name}“ не е включена в пакета ти."
            else:
                message = (f"„{name}“ не е включена в пакета ти, "
                           f"но можеш да я отключиш еднократно.")
            detail = {
                "reason": "locked",
                "feature": feature,
                "feature_name": name or "",
                "message": message,
                "offer": offer,
            }
            raise HTTPException(402, detail)
        return user
    return _check

# --- DB Helpers ---
def get_user_by_email(email: str) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None

def create_user(email: str, password_hash: str) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            (email, password_hash)
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row) if row else {}

def get_person(person_id: int, user_id: int) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM persons WHERE id = ? AND user_id = ?",
            (person_id, user_id)
        ).fetchone()
        return dict(row) if row else None

def get_all_persons(user_id: int) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM persons WHERE user_id = ? ORDER BY name", (user_id,)
        ).fetchall()]

def get_setting(key: str) -> Optional[str]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

def set_setting(key: str, value: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )
        conn.commit()

# SMTP настройките се четат първо от env vars (Coolify), после от DB settings.
# Така паролата може да стои само в Coolify env, не в базата данни.
_SMTP_ENV = {
    "smtp_host": "SMTP_HOST",
    "smtp_port": "SMTP_PORT",
    "smtp_user": "SMTP_USER",
    "smtp_password": "SMTP_PASSWORD",
    "smtp_from": "SMTP_FROM",
    "smtp_use_tls": "SMTP_USE_TLS",
}

def smtp_setting(key: str) -> Optional[str]:
    """SMTP настройка с приоритет на env var (Coolify) пред DB settings."""
    env_name = _SMTP_ENV.get(key)
    if env_name:
        env_val = os.environ.get(env_name)
        if env_val:
            return env_val
    return get_setting(key)

def smtp_from_env() -> bool:
    """Дали SMTP настройките идват от env vars (Coolify), а не от DB."""
    return any(os.environ.get(name) for name in _SMTP_ENV.values())

def clear_smtp_db_settings() -> None:
    """Изтрива SMTP ключовете от DB, когато настройките идват от env.

    Когато SMTP е зададен отвън (Coolify env), DB стойностите са излишни и
    объркващи — премахваме ги, за да няма два източника на истина. Вика се при
    стартиране (lifespan), така че базата се самоизчиства от остарели записи.
    """
    if not smtp_from_env():
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany("DELETE FROM settings WHERE key = ?",
                         [(k,) for k in _SMTP_ENV.keys()])
        conn.commit()

def get_ai_cache(person_id: int, cache_key: str) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT content, generated_at FROM ai_cache WHERE person_id = ? AND cache_key = ?",
            (person_id, cache_key)
        ).fetchone()
        return {"content": row[0], "generated_at": row[1]} if row else None

def set_ai_cache(person_id: int, cache_key: str, content: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO ai_cache (person_id, cache_key, content, generated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(person_id, cache_key) DO UPDATE SET content = excluded.content, generated_at = CURRENT_TIMESTAMP",
            (person_id, cache_key, content)
        )
        conn.commit()

def clear_ai_cache(person_id: int) -> None:
    """Invalidate all cached AI interpretations for a person (e.g. after birth data changes)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM ai_cache WHERE person_id = ?", (person_id,))
        conn.commit()

# Shown when the AI service is not configured or fails. Customers cannot fix
# either, so the message says what it means for them, not what is broken.
AI_UNAVAILABLE = (
    "Разчитането не се получи този път. Позициите в картата ти са изчислени "
    "и запазени — опитай пак след няколко минути."
)

def ai_failure_message(exc: Exception) -> str:
    """A customer-facing message for a failed AI call.

    The real error goes to the log for whoever runs the service; the reader
    gets something honest and actionable instead of a stack trace.
    """
    log.warning("AI call failed: %s: %s", type(exc).__name__, exc)
    return AI_UNAVAILABLE

# Allowed chat models per provider. First entry is the default when unset/invalid.
AI_MODELS = {
    "deepseek": [
        # Flash first: той е бързият модел (~2.5x по-бърз от Pro) и не мисли по
        # подразбиране, затова е дефолтът за дневния хороскоп и другите дълги
        # разчитания. Pro остава като опция за по-голяма дълбочина, но е бавен.
        ("deepseek-v4-flash", "DeepSeek V4 Flash"),
        ("deepseek-v4-pro", "DeepSeek V4 Pro"),
    ],
    "openai": [
        ("gpt-4o-mini", "GPT-4o mini"),
        ("gpt-4o", "GPT-4o"),
    ],
    "anthropic": [
        ("claude-sonnet-4-5", "Claude Sonnet 4.5"),
    ],
}

# Платените разчитания минават през Pro (по-дълбоко, по-бавно, по-скъпо),
# безплатните и SEO страниците — през Flash (бърз и евтин). Дефолтът е Flash.
PAID_MODEL = "deepseek-v4-pro"

def resolve_ai_model(provider: str) -> str:
    """Model id from admin settings, falling back to the provider default."""
    options = AI_MODELS.get(provider) or AI_MODELS["deepseek"]
    allowed = {m for m, _ in options}
    saved = (get_setting("ai_model") or "").strip()
    if saved in allowed:
        return saved
    return options[0][0]

def get_ai_config() -> Tuple[Optional[str], str]:
    """Returns (api_key, provider) where provider is 'deepseek', 'openai' or 'anthropic'.
    DB setting takes priority over environment variables."""
    key = get_setting("ai_api_key")
    provider = get_setting("ai_provider")
    if key and provider:
        return key, provider
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"], "anthropic"
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"], "deepseek"
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"], "openai"
    return None, provider or "deepseek"

def update_person(person_id: int, user_id: int, data: BirthDataUpdate) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """UPDATE persons SET year=?, month=?, day=?, hour=?, minute=?,
               lat=?, lon=?, timezone=? WHERE id=? AND user_id=?""",
            (data.year, data.month, data.day, data.hour, data.minute,
             data.lat, data.lon, data.timezone, person_id, user_id)
        )
        conn.commit()
        return cur.rowcount > 0

def make_subject(person: dict) -> charts.Subject:
    """Create an immanuel Subject from a person dict, using their timezone."""
    tz_name = person.get("timezone", "Europe/Sofia")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Sofia")
    dt = datetime.datetime(person["year"], person["month"], person["day"],
                          person["hour"], person["minute"], 0, tzinfo=tz)
    return charts.Subject(dt, person["lat"], person["lon"])

def serialize_objects(objects: dict) -> dict:
    """Serialize chart objects to JSON-friendly format."""
    icons = {
        'Sun': '☀️', 'Moon': '🌙', 'Mercury': '☿', 'Venus': '♀', 'Mars': '♂',
        'Jupiter': '♃', 'Saturn': '♄', 'Uranus': '⛢', 'Neptune': '♆', 'Pluto': '♇',
        'Asc': '⬆', 'Desc': '⬇', 'MC': '🏛️', 'IC': '🏠',
        'Chiron': '⚷', 'North Node': '☊', 'South Node': '☋',
        'True North Node': '☊', 'True South Node': '☋',
        'Part of Fortune': '⊕', 'Vertex': '⩒', 'Lilith': '⚸', 'True Lilith': '⚸',
        'Ceres': '⚳', 'Pallas': '⚴', 'Juno': '⚵', 'Vesta': '⚶',
    }
    result = {}
    for obj in objects.values():
        name = obj.name
        sign = obj.sign.name
        house = obj.house.name if hasattr(obj.house, 'name') else str(obj.house.number)
        movement = obj.movement.formatted if hasattr(obj, 'movement') and obj.movement else None
        result[str(obj.index)] = {
            "name": name,
            "name_bg": tr_object(name),
            "name_meaning": meaning_object(name),
            "type": obj.type.name if hasattr(obj.type, 'name') else str(obj.type),
            "icon": icons.get(name, '🪐'),
            "sign": sign,
            "sign_bg": tr_sign(sign),
            "sign_symbol": sign_symbol(sign),
            "sign_meaning": meaning_sign(sign),
            "sign_longitude": obj.sign_longitude.formatted,
            "longitude": obj.longitude.formatted,
            "house": house,
            "house_bg": tr_house(house),
            "house_meaning": meaning_house(house),
            "house_number": obj.house.number,
            "speed": obj.speed if hasattr(obj, 'speed') else None,
            "movement": movement,
            "movement_bg": tr_movement(movement),
            "movement_meaning": meaning_movement(movement),
        }
    return result

def serialize_aspects(aspects: dict) -> list:
    """Serialize chart aspects to JSON-friendly format.
    Aspects are nested: {active_id: {passive_id: Aspect}}"""
    icons = {
        'Conjunction': '☌', 'Opposition': '☍', 'Square': '□', 'Trine': '△',
        'Sextile': '⚹', 'Semisquare': '∠', 'Sesquisquare': '⚼',
        'Semisextile': '⚺', 'Quincunx': '⚻', 'Quintile': '⬠', 'Biquintile': '⬟'
    }
    aspect_class = {
        'Conjunction': 'major', 'Opposition': 'challenge', 'Square': 'challenge',
        'Trine': 'harmony', 'Sextile': 'harmony',
        'Semisquare': 'minor', 'Sesquisquare': 'minor',
        'Semisextile': 'minor', 'Quincunx': 'minor',
        'Quintile': 'minor', 'Biquintile': 'minor'
    }
    result = []
    for active_id, passive_dict in aspects.items():
        for passive_id, aspect in passive_dict.items():
            aspect_type = aspect.type if isinstance(aspect.type, str) else aspect.type.name
            active = aspect._active_name if hasattr(aspect, '_active_name') else str(aspect.active)
            passive = aspect._passive_name if hasattr(aspect, '_passive_name') else str(aspect.passive)
            result.append({
                "type": aspect_type,
                "type_bg": tr_aspect(aspect_type),
                "type_meaning": meaning_aspect(aspect_type),
                "active": active,
                "active_bg": tr_object(active),
                "passive": passive,
                "passive_bg": tr_object(passive),
                "icon": icons.get(aspect_type, '◇'),
                "aspect_class": aspect_class.get(aspect_type, 'minor'),
                "aspect_angle": aspect.aspect if hasattr(aspect, 'aspect') else None,
                "orb": aspect.orb if hasattr(aspect, 'orb') else None,
                "distance": aspect.distance.formatted if hasattr(aspect, 'distance') and aspect.distance else None,
                "difference": aspect.difference.formatted if hasattr(aspect, 'difference') and aspect.difference else None,
                "movement": aspect.movement.formatted if hasattr(aspect, 'movement') and aspect.movement else None,
                "condition": aspect.condition.formatted if hasattr(aspect, 'condition') and aspect.condition else None,
            })
    return result

def serialize_houses(houses: dict) -> list:
    """Serialize house cusps (1st-12th) to a simple ordered list with absolute longitude."""
    result = []
    for house in houses.values():
        result.append({
            "number": house.number,
            "sign": house.sign.name,
            "sign_bg": tr_sign(house.sign.name),
            "sign_longitude": house.sign_longitude.formatted,
            "longitude": house.longitude.raw if hasattr(house.longitude, 'raw') else None,
        })
    result.sort(key=lambda h: h["number"])
    return result

def compute_natal(person: dict) -> dict:
    """Compute natal chart for a person using immanuel."""
    native = make_subject(person)
    natal = charts.Natal(native)

    return {
        "native": {
            "name": person["name"],
            "datetime": f"{person['year']}-{person['month']:02d}-{person['day']:02d} "
                       f"{person['hour']:02d}:{person['minute']:02d}",
            "lat": person["lat"],
            "lon": person["lon"],
            "timezone": person.get("timezone", "Europe/Sofia"),
        },
        "house_system": natal.house_system if hasattr(natal, 'house_system') else "Placidus",
        "house_system_bg": tr_house_system(natal.house_system if hasattr(natal, 'house_system') else "Placidus"),
        "shape": natal.shape if hasattr(natal, 'shape') else None,
        "shape_bg": tr_shape(natal.shape if hasattr(natal, 'shape') else None),
        "shape_meaning": meaning_shape(natal.shape if hasattr(natal, 'shape') else None),
        "diurnal": natal.diurnal if hasattr(natal, 'diurnal') else None,
        "moon_phase": natal.moon_phase.formatted if hasattr(natal, 'moon_phase') and natal.moon_phase else None,
        "moon_phase_bg": tr_moon_phase(natal.moon_phase.formatted if hasattr(natal, 'moon_phase') and natal.moon_phase else None),
        "moon_phase_meaning": meaning_moon_phase(natal.moon_phase.formatted if hasattr(natal, 'moon_phase') and natal.moon_phase else None),
        "objects": serialize_objects(natal.objects),
        "aspects": serialize_aspects(natal.aspects),
        "houses": serialize_houses(natal.houses) if hasattr(natal, 'houses') else [],
    }

def compute_composite(person1: dict, person2: dict) -> dict:
    """Compute composite (synastry) chart for two persons."""
    subj1 = make_subject(person1)
    subj2 = make_subject(person2)
    composite = charts.Composite(subj1, subj2)

    return {
        "chart_type": "Composite (Synastry)",
        "native": {
            "name": person1["name"],
            "datetime": f"{person1['year']}-{person1['month']:02d}-{person1['day']:02d} "
                       f"{person1['hour']:02d}:{person1['minute']:02d}",
            "lat": person1["lat"],
            "lon": person1["lon"],
        },
        "partner": {
            "name": person2["name"],
            "datetime": f"{person2['year']}-{person2['month']:02d}-{person2['day']:02d} "
                       f"{person2['hour']:02d}:{person2['minute']:02d}",
            "lat": person2["lat"],
            "lon": person2["lon"],
        },
        "house_system": composite.house_system if hasattr(composite, 'house_system') else "Placidus",
        "house_system_bg": tr_house_system(composite.house_system if hasattr(composite, 'house_system') else "Placidus"),
        "shape": composite.shape if hasattr(composite, 'shape') else None,
        "shape_bg": tr_shape(composite.shape if hasattr(composite, 'shape') else None),
        "diurnal": composite.diurnal if hasattr(composite, 'diurnal') else None,
        "moon_phase": composite.moon_phase.formatted if hasattr(composite, 'moon_phase') and composite.moon_phase else None,
        "moon_phase_bg": tr_moon_phase(composite.moon_phase.formatted if hasattr(composite, 'moon_phase') and composite.moon_phase else None),
        "objects": serialize_objects(composite.objects),
        "aspects": serialize_aspects(composite.aspects),
    }

# --- Ranking transits for a reading -------------------------------------
# The ephemeris returns every aspect within a generous orb, which for a single
# day is around 75 of them — most too wide to mean anything. Handing that to a
# model produces a reading that says a little about everything and nothing with
# conviction, because nothing in the list says what matters. These rules do the
# job an astrologer does before writing: throw out the noise, then rank.

MAJOR_ASPECTS = {"Conjunction", "Sextile", "Square", "Trine", "Opposition"}

# The chart angles rotate a full circle every day, so "MC conjunct natal Saturn"
# is true for roughly forty minutes and says nothing about the day. The same
# goes for the minor points, which need context a daily reading cannot give.
TRANSIT_EXCLUDED = {
    "Asc", "Desc", "MC", "IC", "Vertex", "True Lilith", "Lilith",
    "Part of Fortune", "Syzygy",
}

# How close to exact an aspect must be to count, by how fast the transiting
# body moves. These are real deviations, not the library's allowance: the Moon
# moves ~13° a day so a 3° orb is still the same afternoon, while Pluto can
# hold 1° for months and only a tight hit marks a particular day.
TRANSIT_ORB_LIMITS = {
    "Moon": 3.0,
    "Sun": 2.5, "Mercury": 2.5, "Venus": 2.5, "Mars": 2.5,
    "Jupiter": 2.0, "Saturn": 2.0,
    "Uranus": 1.5, "Neptune": 1.5, "Pluto": 1.5, "Chiron": 1.5,
    "True North Node": 1.5, "True South Node": 1.5,
}
TRANSIT_ORB_DEFAULT = 1.5

def aspect_deviation(aspect: dict) -> Optional[float]:
    """How far an aspect is from exact, in degrees.

    The library's `orb` field is the allowance it permits for that body, not
    the actual deviation — it only ever holds a handful of configured values.
    The real figure is in `difference`, formatted as `-00°11'49"`.
    """
    text = (aspect.get("difference") or "").strip()
    match = re.match(r"^-?(\d+)°(\d+)'([\d.]+)\"?$", text)
    if not match:
        return None
    return (int(match.group(1))
            + int(match.group(2)) / 60
            + float(match.group(3)) / 3600)

def rank_transit_aspects(aspects: list, limit: int = 12) -> list:
    """Keep the aspects worth writing about, tightest first.

    Returns dicts carrying the original aspect plus the true deviation and a
    Bulgarian `strength` label, so the prompt can tell the model what to lead
    with instead of presenting every line as equally important.
    """
    kept = []
    for a in aspects or []:
        if a.get("type") not in MAJOR_ASPECTS:
            continue
        active, passive = a.get("active"), a.get("passive")
        if active in TRANSIT_EXCLUDED or passive in TRANSIT_EXCLUDED:
            continue
        deviation = aspect_deviation(a)
        if deviation is None:
            continue
        if deviation > TRANSIT_ORB_LIMITS.get(active, TRANSIT_ORB_DEFAULT):
            continue
        kept.append({**a, "deviation": deviation})

    # Tightest first; that ordering is itself the signal of what matters.
    kept.sort(key=lambda a: a["deviation"])

    # The lunar nodes are one axis, 180° apart: an aspect to the North Node is
    # always mirrored on the South. Keeping both says the same thing twice and
    # costs a slot, so only the tighter of the pair survives.
    seen_axis = set()
    deduped = []
    for a in kept:
        passive = a.get("passive")
        axis = "Nodes" if passive in ("True North Node", "True South Node") else passive
        key = (a.get("active"), axis)
        if passive in ("True North Node", "True South Node"):
            if key in seen_axis:
                continue
            seen_axis.add(key)
        deduped.append(a)
    kept = deduped

    for a in kept:
        a["strength"] = ("силен" if a["deviation"] <= 1.0
                         else "умерен" if a["deviation"] <= 2.5
                         else "слаб")
    return kept[:limit]

def format_transit_aspects(ranked: list) -> str:
    """The aspect block as the prompt sees it, strength included."""
    if not ranked:
        return "Няма значими активни аспекти днес — денят е спокоен астрологически."
    return "\n".join(
        f"- {a['active']} (транзит) {a['type']} {a['passive']} (натал)"
        f" — {a['strength']}, отклонение {a['deviation']:.1f}°"
        for a in ranked
    )

def compute_transits(person: dict, target_date: datetime.datetime) -> dict:
    """Compute transit chart for a person at a specific date.
    Uses a Natal chart for the target date with aspects_to the person's natal chart."""
    native = make_subject(person)
    natal = charts.Natal(native)

    lat = person["lat"]
    lon = person["lon"]
    tz = person.get("timezone", "Europe/Sofia")

    # Create a chart for the target date with aspects to natal
    target_subject = charts.Subject(target_date, lat, lon)
    transit_chart = charts.Natal(target_subject, aspects_to=natal)

    return {
        "chart_type": "Transits",
        "native": {
            "name": person["name"],
            "birth_datetime": f"{person['year']}-{person['month']:02d}-{person['day']:02d} "
                             f"{person['hour']:02d}:{person['minute']:02d}",
            "lat": lat,
            "lon": lon,
            "timezone": tz,
        },
        "transit_datetime": target_date.isoformat(),
        "house_system": transit_chart.house_system if hasattr(transit_chart, 'house_system') else "Placidus",
        "house_system_bg": tr_house_system(transit_chart.house_system if hasattr(transit_chart, 'house_system') else "Placidus"),
        "shape": transit_chart.shape if hasattr(transit_chart, 'shape') else None,
        "shape_bg": tr_shape(transit_chart.shape if hasattr(transit_chart, 'shape') else None),
        "diurnal": transit_chart.diurnal if hasattr(transit_chart, 'diurnal') else None,
        "moon_phase": transit_chart.moon_phase.formatted if hasattr(transit_chart, 'moon_phase') and transit_chart.moon_phase else None,
        "moon_phase_bg": tr_moon_phase(transit_chart.moon_phase.formatted if hasattr(transit_chart, 'moon_phase') and transit_chart.moon_phase else None),
        "transit_objects": serialize_objects(transit_chart.objects),
        "transit_aspects_to_natal": serialize_aspects(transit_chart.aspects),
    }

def natal_to_text(person: dict, chart_data: dict) -> str:
    """Generate a text representation of a natal chart."""
    lines = []
    lines.append("=" * 60)
    lines.append(f"НАТАЛНА КАРТА — {chart_data['native']['name']}")
    lines.append("=" * 60)
    lines.append(f"Дата и час: {chart_data['native']['datetime']}")
    lines.append(f"Координати: {chart_data['native']['lat']}, {chart_data['native']['lon']}")
    lines.append(f"Часова зона: {chart_data['native']['timezone']}")
    lines.append(f"Домова система: {chart_data['house_system']}")
    lines.append(f"Форма: {chart_data.get('shape', 'N/A')}")
    lines.append(f"Дневно/Нощно: {'Дневно' if chart_data.get('diurnal') else 'Нощно'}")
    lines.append(f"Лунна фаза: {chart_data.get('moon_phase', 'N/A')}")
    lines.append("")
    lines.append("-" * 60)
    lines.append("ПЛАНЕТИ И ТОЧКИ")
    lines.append("-" * 60)
    lines.append(f"{'Обект':<20} {'Знак':<15} {'Позиция':<12} {'Дом':<6} {'Тип':<10}")
    lines.append("-" * 60)
    for oid, obj in chart_data["objects"].items():
        lines.append(f"{obj['name']:<20} {obj['sign']:<15} {obj['sign_longitude']:<12} {obj['house_number']:<6} {obj['type']:<10}")
    lines.append("")
    lines.append("-" * 60)
    lines.append("АСПЕКТИ")
    lines.append("-" * 60)
    for a in chart_data["aspects"]:
        lines.append(f"  {a['active']} {a['type']} {a['passive']} (орб: {a['orb']}°)")
    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)

# --- Auth API Routes ---
@app.post("/api/auth/login")
def api_login(data: AuthRequest, request: Request):
    """Login with email/password (+TOTP при активирана 2FA). Rate-limited."""
    email_key = (data.email or "").strip().lower()
    ip = request.client.host if request.client else ""
    key = f"{email_key}|{ip}"
    if _login_blocked(key):
        raise HTTPException(429, "Твърде много неуспешни опити. Опитай отново след 15 минути.")

    user = get_user_by_email(data.email)
    if not user or not verify_password(data.password, user["password_hash"]):
        _login_record_failure(key)
        raise HTTPException(401, "Грешен имейл или парола.")

    if user.get("totp_secret"):
        if not data.totp_code or not verify_totp(user["totp_secret"], data.totp_code):
            raise HTTPException(401, "Невалиден код за двуфакторна автентикация.")

    _login_clear(key)
    token = create_token(user["id"], user["email"])
    audit("login", f"Вход: {user['email']}", user_id=user["id"], actor=user["email"])
    return {
        "token": token,
        "user": {"id": user["id"], "email": user["email"], "role": user.get("role")}
    }

class GuestChartRequest(BaseModel):
    """Birth details only. No email, no account — this is the free look."""
    name: str
    year: int
    month: int
    day: int
    hour: int = 12
    minute: int = 0
    lat: float
    lon: float
    timezone: str = "Europe/Sofia"


class MockPayRequest(BaseModel):
    """Which modules to hand over in a test purchase."""
    keys: list


@app.post("/api/dev/mock-pay")
def api_mock_pay(data: MockPayRequest,
                 user: Tuple[int, str] = Depends(get_current_user)):
    """Grant modules as if they had been paid for. Test builds only.

    Every grant is recorded as a payment with method "тест", so the admin
    ledger never mistakes it for real income.
    """
    if not MOCK_PAYMENTS:
        raise HTTPException(404, "Няма такъв ресурс.")

    user_id, _ = user
    row = get_user_by_id(user_id)
    if not row:
        raise HTTPException(401, "Невалиден акаунт.")

    requested = list(data.keys or [])
    # "bundle" is not a feature: it stands for everything still missing, at
    # the bundle price rather than the sum of the parts.
    if BUNDLE_KEY in requested:
        bundle = bundle_offer(row)
        if not bundle:
            return {"ok": True, "granted": [], "note": "Няма достатъчно модули за пакет."}
        pay_id = record_payment(
            user_id, plan_key=None, amount_cents=bundle["price_cents"],
            currency=bundle["currency"], method="тест",
            note=f"mock-bundle:{','.join(bundle['keys'])}")
        for key in bundle["keys"]:
            grant_feature_purchase(user_id, key, 0, bundle["currency"], pay_id)
        return {"ok": True, "granted": bundle["keys"],
                "amount_cents": bundle["price_cents"], "currency": bundle["currency"],
                "bundle": True}

    keys, total, currency = [], 0, "EUR"
    for key in requested:
        offer = feature_offer(key)
        if not offer or key in unlocked_features(row):
            continue
        keys.append(key)
        total += offer["price_cents"]
        currency = offer["currency"]

    if not keys:
        return {"ok": True, "granted": [], "note": "Нищо за отключване."}

    pay_id = record_payment(
        user_id, plan_key=None, amount_cents=total, currency=currency,
        method="тест", note=f"mock:{','.join(keys)}")
    for key in keys:
        offer = feature_offer(key)
        grant_feature_purchase(user_id, key, offer["price_cents"],
                               offer["currency"], pay_id)

    log.info("Тестово плащане: user=%s модули=%s", user_id, keys)
    return {"ok": True, "granted": keys, "amount_cents": total, "currency": currency}


@app.get("/api/public/config")
def api_public_config():
    """What the front end needs to know before anybody signs in."""
    return {
        "mock_payments": MOCK_PAYMENTS,
        "stripe": billing.stripe_enabled(),
    }


def price_label(cents: int, currency: str = "EUR") -> str:
    """A price the way a customer reads it: `5 €`, `6.99 €`, `2.97 €`."""
    value = (cents or 0) / 100
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    symbol = "€" if (currency or "EUR").upper() == "EUR" else currency
    return f"{text} {symbol}"

# Wording for the landing table. The catalogue's bullets are written for the
# module picker, where each one gets its own card; the table needs one flowing
# sentence per row, so the copy lives here and only the price comes from the DB.
LANDING_PRICE_COPY = {
    "moon": "Как Луната влияе на ежедневието ти и кои периоди "
            "са благоприятни за начинания.",
    "numerology": "Символиката на числата ти, жизнената ти мисия "
                  "и коя е твоята лична година.",
    "profile": "Същността, призванието, темпераментът ти — и къде да "
               "насочиш енергията си.",
    "period": "Какво да очакваш до 60 дни напред и кога да "
              "планираш важните неща.",
    "love": "Има ли истинско привличане, кое ви свързва и "
            "имате ли дългосрочен потенциал.",
    "akashic": "Твоята мисия, кармичните уроци и как да развиеш "
               "потенциала си.",
}
LANDING_PRICE_EXTRA = {
    "love": "Включва съвпадения по рождени данни или зодия "
            "и още една карта, за да добавиш партньора си.",
}

def landing_pricing() -> dict:
    """Rows for the landing price table, priced from the database.

    The table used to carry its prices as text, which drifted the moment an
    admin edited one — the page advertised a figure the checkout did not
    charge. Everything numeric here comes from `feature_prices`.
    """
    rows, total = [], 0
    for f in FEATURE_CATALOGUE:
        if f.get("included"):
            continue
        offer = feature_offer(f["key"])
        if not offer:
            continue
        total += offer["price_cents"]
        rows.append({
            "key": f["key"],
            "name": f["name"],
            "glyph": f.get("glyph", "✦"),
            "blurb": LANDING_PRICE_COPY.get(f["key"], f.get("note", "")),
            "extra": LANDING_PRICE_EXTRA.get(f["key"], ""),
            "price": price_label(offer["price_cents"], offer["currency"]),
            "price_cents": offer["price_cents"],
        })
    rows.sort(key=lambda r: r["price_cents"])

    bundle = None
    # The bundle only earns a row when it actually saves money against the
    # current price list — the same rule the picker applies.
    if len(rows) >= 2 and total > BUNDLE_PRICE_CENTS:
        bundle = {
            "name": BUNDLE_NAME,
            "count": len(rows),
            "price": price_label(BUNDLE_PRICE_CENTS),
            "full_price": price_label(total),
            "saving": price_label(total - BUNDLE_PRICE_CENTS),
        }
    return {"rows": rows, "bundle": bundle}

@app.get("/api/public/catalogue")
def api_public_catalogue():
    """The module list with prices, for visitors who have no account yet.

    Same data the signed-in picker uses, minus anything account-specific.
    A price list is public information, so this needs no token.
    """
    out = []
    for f in FEATURE_CATALOGUE:
        if f.get("included"):
            continue
        offer = feature_offer(f["key"])
        if not offer:
            continue
        out.append({
            "key": f["key"],
            "name": f["name"],
            "note": f.get("note", ""),
            "glyph": f.get("glyph", "✦"),
            "bullets": f.get("bullets", []),
            "price_cents": offer["price_cents"],
            "currency": offer["currency"],
        })
    return {"catalogue": out, "bundle": public_bundle()}

@app.post("/api/guest/chart")
def api_guest_chart(data: GuestChartRequest):
    """Compute a chart for somebody who has not signed up yet.

    Nothing is stored: the browser keeps the birth details and asks again on
    the next visit. Asking for an email before showing anything is the point
    where casual visitors leave, so the chart comes first and the account
    comes after they have seen it.
    """
    if not (data.name or "").strip():
        raise HTTPException(400, "Моля, въведи име.")
    try:
        person = {
            "name": data.name.strip(),
            "year": data.year, "month": data.month, "day": data.day,
            "hour": data.hour, "minute": data.minute,
            "lat": data.lat, "lon": data.lon,
            "timezone": data.timezone or "Europe/Sofia",
        }
        chart_data = compute_natal(person)
    except Exception as e:
        log.warning("Guest chart failed: %s", e)
        raise HTTPException(400, "Картата не можа да се изчисли. Провери датата и мястото.")

    from chart_svg import generate_chart_svg
    return {
        "ok": True,
        "chart": chart_data,
        "profile": build_profile(chart_data),
        "svg": generate_chart_svg(chart_data),
    }


class OnboardRequest(BaseModel):
    """Birth details plus an email, gathered before any account exists.

    `password` is optional: somebody who types one is signed in straight away,
    while somebody who leaves it blank gets a set-password link by email.
    `wanted` carries the modules picked before signing up, so checkout can
    start from the same click.
    """
    email: str
    name: str
    year: int
    month: int
    day: int
    hour: int = 12
    minute: int = 0
    lat: float
    lon: float
    timezone: str = "Europe/Sofia"
    password: Optional[str] = None
    wanted: Optional[list] = None


@app.post("/api/onboard")
def api_onboard(data: OnboardRequest, request: Request):
    """Create an account and its first chart, then sign the visitor straight in.

    Nothing is charged here. The chart, the astro portrait, the planets and the
    aspects come free: somebody has to see what the product is before deciding
    whether to buy a reading of it. The add-ons are offered afterwards, from
    the chart page itself.
    """
    email = (data.email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "Моля, въведи валиден имейл адрес.")
    if not (data.name or "").strip():
        raise HTTPException(400, "Моля, въведи име.")

    existing = get_user_by_email(email)
    if existing:
        # Never silently attach a chart to somebody else's account.
        raise HTTPException(409, {
            "reason": "account_exists",
            "message": "Вече има акаунт с този имейл. Влез и създай картата оттам.",
        })

    chose_password = bool((data.password or "").strip())
    if chose_password and len((data.password or "").strip()) < 6:
        raise HTTPException(400, "Паролата трябва да е поне 6 символа.")

    # Without a typed password the account gets an unusable one: the visitor is
    # signed in by token now and sets a real one from the emailed link.
    user = create_user(
        email,
        hash_password(data.password.strip() if chose_password
                      else secrets.token_urlsafe(32)))

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO persons (user_id, name, year, month, day, hour, minute,"
            " lat, lon, timezone) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], data.name.strip(), data.year, data.month, data.day,
             data.hour, data.minute, data.lat, data.lon, data.timezone))
        person_id = cur.lastrowid
        conn.commit()

    # The chart is what they came for, so it is theirs from the start — and
    # the daily reading with it, since that is what brings people back.
    for free_key in ("chart", "horoscope"):
        grant_feature_purchase(user["id"], free_key, 0, "EUR", None)

    if not chose_password:
        send_welcome_set_password(user["id"])

    token = create_token(user["id"], user["email"])
    result = {
        "ok": True,
        "person_id": person_id,
        "token": token,
        "chose_password": chose_password,
        "chart_url": f"/chart/{person_id}?token={token}",
    }

    # Modules picked before signing up go straight to checkout, so the visitor
    # does not have to find and click them a second time. "bundle" means every
    # paid module at once, at the bundle price rather than the sum of parts.
    is_bundle = BUNDLE_KEY in (data.wanted or [])
    wanted = [k for k in (data.wanted or []) if feature_offer(k)]
    if is_bundle:
        bundle = public_bundle()
        if bundle:
            wanted = bundle["keys"]
        else:
            is_bundle = False

    if wanted and MOCK_PAYMENTS:
        # Test builds complete the purchase immediately, so the whole flow can
        # be walked end to end without a payment processor.
        total = (BUNDLE_PRICE_CENTS if is_bundle
                 else sum(feature_offer(k)["price_cents"] for k in wanted))
        pay_id = record_payment(
            user["id"], plan_key=None, amount_cents=total, currency="EUR",
            method="тест",
            note=("mock-onboard-bundle" if is_bundle
                  else f"mock-onboard:{','.join(wanted)}"))
        if is_bundle:
            for item in bundle_line_items(wanted):
                grant_feature_purchase(user["id"], item["key"],
                                       item["amount_cents"], item["currency"], pay_id)
        else:
            for k in wanted:
                o = feature_offer(k)
                grant_feature_purchase(user["id"], k, o["price_cents"], o["currency"], pay_id)
        result["mock_paid"] = wanted
    elif wanted and billing.stripe_enabled():
        base = site_base_url(request)
        try:
            if is_bundle:
                items = bundle_line_items(wanted)
            else:
                items = [{"key": k, "name": feature_offer(k)["name"],
                          "amount_cents": feature_offer(k)["price_cents"],
                          "currency": feature_offer(k)["currency"]} for k in wanted]
            total = sum(it["amount_cents"] for it in items)
            cur = (items[0]["currency"] if items else "EUR")
            result["amount_cents"] = total
            result["currency"] = cur
            result["checkout_url"] = billing.create_features_checkout(
                customer_email=email,
                customer_id=None,
                user_id=user["id"],
                items=items,
                success_url=f"{base}/chart/{person_id}?paid=1&session_id={{CHECKOUT_SESSION_ID}}&amount_cents={total}&currency={cur}",
                cancel_url=f"{base}/chart/{person_id}?paid=0",
                brand=brand_name(),
            )
        except Exception as e:
            log.warning("Onboarding checkout за %s се провали: %s", email, e)
    result["wanted"] = wanted
    result["bundle"] = is_bundle
    return result


@app.post("/api/auth/register")
def api_register(data: AuthRequest, request: Request):
    """Create an account. Each user only ever sees their own people."""
    email = (data.email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "Моля, въведете валиден имейл адрес.")
    if len(data.password or "") < 6:
        raise HTTPException(400, "Паролата трябва да е поне 6 символа.")
    if get_user_by_email(email):
        raise HTTPException(409, "Вече съществува акаунт с този имейл.")

    user = create_user(email, hash_password(data.password))
    token = create_token(user["id"], user["email"])
    audit("register", f"Нова регистрация: {email}", user_id=user["id"], actor=email)
    try_send_template(
        email, "welcome",
        name=email.split("@")[0],
        link=f"{site_base_url(request)}/dashboard",
        expires="",
    )
    return {"token": token, "user": {"id": user["id"], "email": user["email"]}}

@app.get("/api/auth/me")
def api_me(user: Tuple[int, str] = Depends(get_current_user)):
    """Current account, with the plan and features the UI should honour."""
    user_id, email = user
    row = get_user_by_id(user_id)
    if not row:
        raise HTTPException(401, "Невалиден акаунт.")
    plan = effective_plan(row)
    is_admin = row.get("role") == "admin"
    return {
        "id": user_id,
        "email": email,
        "role": row.get("role", "user"),
        "is_admin": is_admin,
        "is_blocked": bool(row.get("is_blocked")),
        "plan": {
            "key": plan.get("key"),
            "name": plan.get("name"),
            # The effective cap, not the plan's raw number: modules that compare
            # people raise it, and the UI must show what actually applies.
            "max_persons": person_limit(row),
            # Admins are never gated by plan.
            "features": [f["key"] for f in FEATURE_CATALOGUE] if is_admin else plan.get("features", []),
        },
        # What the account may actually open, and what the rest would cost.
        "features": unlocked_features(row),
        "purchased": purchased_features(user_id),
        "offers": [] if is_admin else [
            offer for offer in (feature_offer(f["key"]) for f in FEATURE_CATALOGUE)
            if offer and offer["key"] not in unlocked_features(row)
        ],
    }

# Everything a plan can unlock. Keys are what require_feature() checks against.
FEATURE_CATALOGUE = [
    # "bullets" and "glyph" drive the module picker; "included" marks what a
    # chart already carries, so the picker never offers it for sale.
    {"key": "chart", "name": "Натална карта", "note": "Колелото и позициите",
     "glyph": "⊕", "included": True,
     "bullets": ["Колелото с домовете по Плацидус",
                 "Всяка планета и точка с обяснение",
                 "Аспектите и какво носят"]},
    {"key": "planets", "name": "Планети", "note": "Списък с обяснения",
     "glyph": "☿", "included": True, "bullets": []},
    {"key": "aspects", "name": "Аспекти", "note": "Аспектите в картата",
     "glyph": "△", "included": True, "bullets": []},

    {"key": "profile", "name": "Пълен астрологически профил",
     "note": "Какво ще узнаеш?", "glyph": "☉",
     "bullets": ["Своята същност",
                 "Твоето призвание",
                 "Как те виждат отстрани",
                 "Твоят емоционален свят и темпераментът ти",
                 "Къде да насочиш енергията си"]},

    {"key": "horoscope", "name": "Дневен хороскоп",
     "note": "Какво ще узнаеш?", "glyph": "☽", "included": True,
     "bullets": ["Какви са активните транзитни аспекти",
                 "Какво да правиш и какво да избягваш",
                 "Какви емоции ще ти донесе денят",
                 "Какъв ще бъде денят ти"]},

    {"key": "period", "name": "Хороскоп за конкретен период",
     "note": "Какво ще узнаеш?", "glyph": "♃",
     "bullets": ["Какво да очакваш до 60 дни напред",
                 "Как са ти повлияли минали събития",
                 "Кога да планираш важни събития",
                 "Как да елиминираш неприятни ситуации",
                 "Къде да насочиш енергията си"]},

    {"key": "love", "name": "Любовен хороскоп и емоционална съвместимост",
     "note": "Какво ще узнаеш?", "glyph": "♀",
     "bullets": ["Дали между вас има истинско привличане",
                 "Кое ви свързва и кое ви дели",
                 "Къде да подходите предпазливо",
                 "Имате ли дългосрочен потенциал",
                 "Съвпадения по рождени данни или зодия — и още една карта "
                 "за партньора ти"]},

    {"key": "akashic", "name": "Акашови записи",
     "note": "Какво ще узнаеш?", "glyph": "☊",
     "bullets": ["Твоята мисия",
                 "Кармичните уроци, които трябва да научиш",
                 "Какво носиш в душата си",
                 "Как да развиеш потенциала си"]},

    {"key": "numerology", "name": "Нумерология",
     "note": "Какво ще узнаеш?", "glyph": "7",
     "bullets": ["Каква е символиката на числата, свързани с раждането ти",
                 "Каква е жизнената ти мисия",
                 "Какво е влиянието на цифрите върху живота и съдбата ти",
                 "Коя е твоята лична година"]},

    {"key": "moon", "name": "Лунен хороскоп календар",
     "note": "Какво ще узнаеш?", "glyph": "◐",
     "bullets": ["Как Луната влияе върху ежедневието ти",
                 "Защо понякога нещата не се получават, въпреки усилията ти",
                 "Ежедневни съвети за здраве, дом и красота",
                 "Благоприятни периоди за диети и други начинания"]},
]



# Default wording for the automated emails; admins can edit these.
# {brand} is filled in from the brand settings, so renaming the app does not
# mean rewriting every template by hand.
EMAIL_TEMPLATES = {
    "welcome_subject": "Добре дошъл в {brand}",
    "welcome_body": (
        "Здравей, {name}!\n\n"
        "Акаунтът ти в {brand} е готов. Влез и създай първата си натална карта.\n\n"
        "{link}\n\nПоздрави,\nЕкипът на {brand}"
    ),
    "set_password_subject": "Картата ти е готова — задай парола",
    "set_password_body": (
        "Здравей!\n\n"
        "Плащането мина и наталната ти карта е изчислена.\n"
        "Задай парола, за да влизаш в профила си:\n\n"
        "{link}\n\n"
        "Връзката е валидна 2 часа. Ако изтече, използвай „Забравена парола“ "
        "на страницата за вход.\n\n— {brand}"
    ),
    "reset_password_subject": "Нулиране на парола — {brand}",
    "reset_password_body": (
        "Здравей, {name}!\n\n"
        "Заяви нулиране на паролата си. Линкът е валиден 2 часа:\n\n"
        "{link}\n\n"
        "Ако не си го заявил/а, игнорирай това писмо.\n\n{brand}"
    ),
    "digest_subject": "Денят ти в {brand} — {date}",
    "digest_body": (
        "Здравей, {name}!\n\n"
        "{reading}\n\n"
        "Можеш да спреш тези писма от Настройки.\n\n"
        "Поздрави,\n{brand}"
    ),
    "share_subject": "{title} — {person_name}",
    "share_body": (
        "Здравей!\n\n"
        "Прикачено е разчитането „{title}“ за {name}, изготвено от {brand}.\n"
        "Позициите в него са изчислени със Swiss Ephemeris.\n\n"
        "Приятно четене!\n— {brand}"
    ),
    # Касов документ (Н-18, чл. 52а) и фактура (ЗДДС) се издават като PDF —
    # имейлът е кратко придружително писмо, а документът е прикачен файл.
    "receipt_subject": "Касов документ от {brand} — №{unp}",
    "receipt_body": (
        "Здравей!\n\n"
        "Благодарим за покупката! Касовият документ за продажбата ти "
        "е прикачен към това писмо като PDF.\n\n"
        "Поздрави,\n{brand}"
    ),
    "unlock_request_subject": "Заявка за отключване: {name}",
    "unlock_request_body": (
        "Потребител {email} (ID {user_id}) иска да отключи "
        "„{name}“ за {price} {currency}."
    ),
    "bundle_request_subject": "Заявка за пакет: {bundle_name}",
    "bundle_request_body": (
        "Потребител {email} (ID {user_id}) иска пакета "
        "({keys}) за {price} EUR."
    ),
    # Фактура по ЗДДС (чл. 114) — издава се за ВСЯКА продажба, защото търговецът
    # е регистриран по ЗДДС. Номерът е 10-цифрен пореден (чл. 113) и НЕ се редактира.
    "invoice_subject": "Фактура №{invoice_number} — {brand}",
    "invoice_body": (
        "Здравей!\n\n"
        "Фактурата за покупката ти е прикачена към това писмо като PDF.\n\n"
        "Поздрави,\n{brand}"
    ),
}

# Search-engine settings the admin can edit; these are the defaults the public
# pages fall back to when nothing has been saved yet.
SEO_DEFAULTS = {
    "seo_site_url": "",
    # {brand} is substituted at read time, so renaming the app does not leave
    # a stale title in the search results.
    "seo_title": "{brand} — твоята натална карта, разчетена на разбираем език",
    "seo_description": (
        "Точна натална карта по Swiss Ephemeris, разчетена на български: кой си, "
        "какво ти предстои днес, кармичните ти теми и нумерологията ти."
    ),
    "seo_keywords": "натална карта, хороскоп, астрология, зодия, нумерология, лунен календар",
    "seo_og_image": "",
    "seo_robots": "index,follow",
    "seo_verification": "",
    "analytics_id": "G-CY4NT2QLFX",
    # Meta/Facebook pixel. Empty means no pixel is loaded at all.
    "fb_pixel_id": "",
}

def seo_settings() -> dict:
    """Current SEO values, falling back to the defaults for anything unset."""
    name = brand_name()
    values = {key: (get_setting(key) or default)
              for key, default in SEO_DEFAULTS.items()}
    # Admins write {brand} in their own titles too, so substitute after reading.
    for key in ("seo_title", "seo_description"):
        values[key] = values[key].replace("{brand}", name)
    # An unset share image follows the logo, uploaded or bundled.
    if not values["seo_og_image"]:
        logo = brand()["logo"]
        # The bundled logo is 180x180 — too small for social cards. Ship the
        # dedicated 1200x630 card instead (an uploaded logo still wins).
        if logo == "/static/logo-header.png":
            logo = "/static/og-image.jpg"
        values["seo_og_image"] = logo
    return values

_SKY_CACHE_TTL = 600  # секунди — позициите са "в момента", но за лентата е достатъчно точно
_SKY_CACHE = {"t": 0.0, "data": None}


def sky_today() -> list:
    """Where the main bodies actually are right now, for the landing strip.

    The point of the strip is that these are live figures, not decoration —
    so a failure returns nothing and the strip is simply left out.

    The ephemeris computation is cached for a few minutes: planetary degrees
    drift far slower than the strip's rounding, so recomputing on every
    request only adds latency to the landing page (TTFB) without making the
    figures any more accurate.
    """
    now_ts = _time.time()
    cached = _SKY_CACHE
    if cached["data"] is not None and now_ts - cached["t"] < _SKY_CACHE_TTL:
        return cached["data"]
    try:
        now = datetime.datetime.now(ZoneInfo("Europe/Sofia"))
        subject = charts.Subject(
            date_time=now.replace(tzinfo=None),
            latitude=42.6977, longitude=23.3219, timezone="Europe/Sofia",
        )
        chart_now = charts.Natal(subject)
        wanted = ["Sun", "Moon", "Mercury", "Venus", "Mars", "Jupiter", "Saturn"]
        found = {}
        for obj in chart_now.objects.values():
            name = getattr(obj, "name", None)
            if name in wanted and name not in found:
                sign = str(obj.sign.name)
                found[name] = {
                    "name": tr_object(name),
                    "symbol": sign_symbol(sign),
                    "sign": tr_sign(sign),
                    "degree": int(obj.sign_longitude.degrees),
                    "retrograde": getattr(obj, "movement", None)
                                  and str(obj.movement) == "Retrograde",
                }
        result = [found[n] for n in wanted if n in found]
        cached["t"] = _time.time()
        cached["data"] = result
        return result
    except Exception:
        log.warning("sky_today failed; the landing strip will be omitted", exc_info=True)
        return []


# --- Дневен хороскоп по зодия (SEO страници /horoskop/{slug}) ---
# Транзитните позиции и основните аспекти за днешния ден, на базата на които
# се пише хороскопът за всеки знак. Кешира се за кратко, за да не се смята
# Swiss Ephemeris на всяка заявка.
_DAILY_SKY_CACHE = {"t": 0.0, "data": None}
_DAILY_SKY_BODIES = ["Sun", "Moon", "Mercury", "Venus", "Mars",
                     "Jupiter", "Saturn", "Uranus", "Neptune", "Pluto"]


def daily_sky() -> dict:
    """Today's transit positions + major aspects, for the sign horoscopes."""
    now_ts = _time.time()
    cached = _DAILY_SKY_CACHE
    if cached["data"] is not None and now_ts - cached["t"] < _SKY_CACHE_TTL:
        return cached["data"]
    try:
        now = datetime.datetime.now(ZoneInfo("Europe/Sofia"))
        subject = charts.Subject(
            date_time=now.replace(tzinfo=None),
            latitude=42.6977, longitude=23.3219, timezone="Europe/Sofia",
        )
        chart_now = charts.Natal(subject)
        positions = []
        for obj in chart_now.objects.values():
            name = getattr(obj, "name", None)
            if name in _DAILY_SKY_BODIES:
                sign = str(obj.sign.name)
                positions.append({
                    "name": name,
                    "name_bg": tr_object(name),
                    "symbol": sign_symbol(sign),
                    "sign_bg": tr_sign(sign),
                    "degree": int(obj.sign_longitude.degrees),
                    "retrograde": getattr(obj, "movement", None)
                                  and str(obj.movement) == "Retrograde",
                })
        aspects = []
        for a in serialize_aspects(chart_now.aspects):
            if a.get("type") not in MAJOR_ASPECTS:
                continue
            dev = aspect_deviation(a)
            if dev is None or dev > 3.0:
                continue
            a["deviation"] = dev
            aspects.append(a)
        aspects.sort(key=lambda a: a["deviation"])
        result = {
            "positions": positions,
            "aspects": aspects,
            "moon_phase_bg": tr_moon_phase(chart_now.moon_phase.formatted if hasattr(chart_now, "moon_phase") and chart_now.moon_phase else None),
            "shape_bg": tr_shape(chart_now.shape if hasattr(chart_now, "shape") else None),
        }
        cached["t"] = _time.time()
        cached["data"] = result
        return result
    except Exception:
        log.warning("daily_sky failed; the sign horoscope will be text-only", exc_info=True)
        return {"positions": [], "aspects": [], "moon_phase_bg": "", "shape_bg": ""}


def get_sign_horoscope(sign: str, date_iso: str) -> Optional[str]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT content FROM sign_horoscope WHERE sign = ? AND date = ?",
            (sign, date_iso)
        ).fetchone()
        return row[0] if row else None


def set_sign_horoscope(sign: str, date_iso: str, content: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO sign_horoscope (sign, date, content, generated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(sign, date) DO UPDATE SET content = excluded.content, generated_at = CURRENT_TIMESTAMP",
            (sign, date_iso, content)
        )
        conn.commit()


def _sky_context_text(sky: dict) -> str:
    """The day's sky as a compact block the prompt can reason over."""
    if not sky.get("positions"):
        return "(Позициите на планетите не са налични.)"
    lines = []
    for p in sky["positions"]:
        retro = " (ретрограден)" if p["retrograde"] else ""
        lines.append(f"- {p['name_bg']} в {p['sign_bg']} на {p['degree']}°{retro}")
    if sky.get("aspects"):
        lines.append("")
        lines.append("Основни аспекти на деня (подредени по сила):")
        for a in sky["aspects"]:
            lines.append(f"- {tr_object(a['active'])} {tr_aspect(a['type'])} {tr_object(a['passive'])} — отклонение {a['deviation']:.1f}°")
    if sky.get("moon_phase_bg"):
        lines.append(f"Лунна фаза: {sky['moon_phase_bg']}")
    return "\n".join(lines)


def _md_to_html(raw: str) -> str:
    """Server-side markdown-ish -> HTML for the horoscope body.

    The model replies in a loose markdown (numbered headings like
    ``1. **Заглавие**``, ``- bullet`` lists, ``**bold**``). Search engines need
    that rendered into the page's HTML, not built client-side after load, so we
    do the same conversion here that the chart page does in JS.
    """
    import html as _html
    if not raw:
        return ""
    out = []
    list_items = []
    para = []

    def flush_list():
        if list_items:
            out.append("<ul>" + "".join(f"<li>{li}</li>" for li in list_items) + "</ul>")
            list_items.clear()

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    def inline(text):
        t = _html.escape(text)
        t = re.sub(r"\*\*([^*]+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"(^|[^*])\*([^*\n]+?)\*(?!\*)", r"\1<em>\2</em>", t)
        return t

    for raw_line in raw.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            flush_list()
            flush_para()
            continue
        if re.match(r"^(-{3,}|_{3,}|\*{3,})$", line):
            flush_list()
            flush_para()
            out.append("<hr>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush_list()
            flush_para()
            out.append(f"<h3>{inline(m.group(2).rstrip(':'))}</h3>")
            continue
        num = re.match(r"^(\d+)[.)]\s*\*\*(.+?)\*\*[:：]?\s*(.*)$", line)
        if num:
            flush_list()
            flush_para()
            out.append(f"<h3><span>{num.group(1)}</span>{inline(num.group(2))}</h3>")
            if num.group(3):
                para.append(inline(num.group(3)))
            continue
        if re.match(r"^\*\*[^*]+\*\*[:：]?$", line):
            flush_list()
            flush_para()
            title = re.sub(r"^\*\*|\*\*[:：]?$", "", line)
            out.append(f"<h3>{inline(title)}</h3>")
            continue
        b = re.match(r"^[-•]\s+(.*)$", line) or re.match(r"^\*(?!\*)\s+(.*)$", line)
        if b:
            flush_para()
            list_items.append(inline(b.group(1)))
            continue
        ni = re.match(r"^(\d+)[.)]\s+(.+)$", line)
        if ni:
            flush_para()
            list_items.append(f"<strong>{ni.group(1)}.</strong> {inline(ni.group(2))}")
            continue
        flush_list()
        para.append(inline(line))

    flush_list()
    flush_para()
    return "".join(out)


def _generate_sign_horoscope(sign_data: dict, date_bg: str, date_iso: str) -> Optional[str]:
    """Write and cache today's horoscope for one zodiac sign. Returns the raw reply."""
    sky = daily_sky()
    sign_name = sign_data["name"]
    prompt = f"""Ти си професионален астролог. Напиши ДНЕВЕН ХОРОСКОП ЗА ЗОДИЯ {sign_name} за {date_bg}, стриктно базиран на реалните астрономически данни по-долу (изчислени със Swiss Ephemeris). Не измисляй позиции или аспекти извън изброените — обясни само какво ОЗНАЧАВАТ за хората, родени под знака {sign_name} (слънчев знак).

За знака {sign_name}: стихия {sign_data['element']}, модалност {sign_data['modality']}, управител {sign_data['ruler']}, период {sign_data['dates']}.

=== НЕБЕТО ДНЕС ===
{_sky_context_text(sky)}

=== ЗАДАЧА ===
Отговорът ти се състои от ДВЕ части, в този ред.

ЧАСТ 1 — резюме за карти. Започни отговора си с JSON блок между маркерите ---SUMMARY--- и ---END--- точно в този формат:
---SUMMARY---
{{"mood": "една дума за настроението на деня", "energy": "Висока|Средна|Ниска", "do": ["3 кратки неща за правене, по 2-4 думи всяко"], "avoid": ["2-3 кратки неща за избягване, по 2-4 думи всяко"], "focus": "фокусът на деня", "caution": "едно кратко изречение в какво да внимава"}}
---END---

ЧАСТ 2 — разгърнатият текст, веднага след ---END---, със следните заглавия, номерирани:
1. **Общо усещане за деня** — 2-3 изречения обобщение на енергията на деня за {sign_name}.
2. **Любов и отношения** — какво носи денят за личния живот, базирано на позицията на Венера и Луната днес.
3. **Работа и финанси** — базирано на Слънцето, Меркурий и Марс днес.
4. **Здраве и енергия** — къде е енергията днес и какво да поддържаш.
5. **Късмет и възможности** — къде денят отваря врата, базирано на Юпитер и активните аспекти.
6. **Какво да направиш днес** — 3-4 конкретни, изпълними действия.
7. **Какво да избягваш** — 2-3 конкретни поведения или решения, които днешните аспекти правят рискови.
8. **В какво да внимаваш** — 2-3 предупреждения според напрегнатите аспекти.
9. **Есенцията на деня** — 1-2 изречения обобщение.

=== КАК ДА ПИШЕШ ===
- Пиши на български, топло и практично, все едно говориш директно на читателя.
- ФОРМАТ: всяко от деветте заглавия започва на нов ред във вида `1. **Заглавие**`. Под него — текст на отделни редове. Изброяванията с тирета (`- нещо`), едно на ред. Не слепвай изброявания в един дълъг абзац.
- ЛОГИКА: съветите в секции 6-8 трябва да следват пряко от позициите и аспектите по-горе.
- ДЪЛЖИНА: бъди подробен. Всяка секция с по няколко изречения реално съдържание.
- Бъди конкретен — избягвай клишета от типа "бъди позитивен". Ако някой аспект е слаб или неутрален, кажи го честно.
- Основавай се единствено на изброените данни, без да добавяш измислени детайли."""

    ai_key, provider = get_ai_config()
    if not ai_key:
        return None
    raw = call_ai(ai_key, provider, prompt, max_tokens=6000)
    set_sign_horoscope(sign_data["sign"], date_iso, raw)
    return raw


# --- Вечнозелени SEO страници „планета в знак" (/luna-v-skorpion и т.н.) ---
def get_planet_sign(planet: str, sign: str) -> Optional[str]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT content FROM planet_sign_cache WHERE planet = ? AND sign = ?",
            (planet, sign)
        ).fetchone()
        return row[0] if row else None


def set_planet_sign(planet: str, sign: str, content: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO planet_sign_cache (planet, sign, content, generated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(planet, sign) DO UPDATE SET content = excluded.content, generated_at = CURRENT_TIMESTAMP",
            (planet, sign, content)
        )
        conn.commit()


def _generate_planet_sign(planet_data: dict, sign_data: dict) -> Optional[str]:
    """Напиши и кеширай краткия тизър за „{планета} в {знак}". Вечнозелено — веднъж.

    Това е ТИЗЪР, не пълно разчитане: общият случай е безплатен за SEO,
    а персоналното (дом, аспекти, градуси) си остава в пакета.
    """
    planet_key = planet_data["key"]
    planet_name = planet_data["name"]
    sign_name = sign_data["name"]

    prompt = f"""Ти си астролог. Напиши КРАТКО обяснение какво означава {planet_name} в знака {sign_name} (по рождената карта).

КОНТЕКСТ:
- {planet_name}: {meaning_object(planet_key)}
- Знак {sign_name}: {meaning_sign(sign_data['sign'])}

ВАЖНО: Това е ТИЗЪР за SEO страница — НЕ пълно персонално разчитане. Пиши само ОБЩИЯ случай (какво значи за повечето хора с тази позиция). НЕ навлизай в домове, аспекти или конкретни градуси — това е част от персоналното разчитане, което читателят получава отделно. Целта е да дадеш ясна обща представа, която да накара читателя да поиска по-задълбочения анализ.

Структура (всяко заглавие на собствен ред, обградено с **звезди**):
**Общо значение** — 2-3 изречения.
**Любов и отношения** — 2-3 изречения.
**Работа и финанси** — 2-3 изречения.
**Как да използваш тази енергия** — 1-2 изречения.

Пиши на български, ясно и практично, без жаргон. Общо ~250-350 думи. НЕ използвай маркери SUMMARY и НЕ изброявай с тирета."""

    ai_key, provider = get_ai_config()
    if not ai_key:
        return None
    raw = call_ai(ai_key, provider, prompt, max_tokens=1200)
    set_planet_sign(planet_key, sign_data["sign"], raw)
    return raw


# --- Вечнозелени SEO страници „характеристика на знак" (/zodia/{slug}) ---
def get_sign_profile(sign: str) -> Optional[str]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT content FROM sign_profile_cache WHERE sign = ?", (sign,)
        ).fetchone()
        return row[0] if row else None


def set_sign_profile(sign: str, content: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO sign_profile_cache (sign, content, generated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(sign) DO UPDATE SET content = excluded.content, generated_at = CURRENT_TIMESTAMP",
            (sign, content)
        )
        conn.commit()


def _generate_sign_profile(sign_data: dict) -> Optional[str]:
    """Напиши и кеширай характеристиката на един знак. Вечнозелено — веднъж."""
    sign_name = sign_data["name"]

    prompt = f"""Ти си астролог. Напиши ХАРАКТЕРИСТИКА на зодия {sign_name}.

КОНТЕКСТ:
- Знак {sign_name}: {meaning_sign(sign_data['sign'])}
- Стихия: {sign_data['element']}, модалност: {sign_data['modality']}, управител: {sign_data['ruler']}, период: {sign_data['dates']}.

ВАЖНО: Това е ТИЗЪР за SEO страница — НЕ пълно персонално разчитане. Пиши само ОБЩИЯ случай (какво е типично за повечето хора с този слънчев знак). НЕ навлизай в домове, аспекти или конкретни градуси. Целта е ясна обща представа, която да накара читателя да поиска персоналния анализ.

Структура (всяко заглавие на собствен ред, обградено с **звезди**):
**Характер** — 3-4 изречения.
**Силни страни** — 3-4 кратки, с тирета.
**Слаби страни** — 3-4 кратки, с тирета.
**Любов и отношения** — 2-3 изречения.
**Работа и кариера** — 2-3 изречения.
**Пари и финанси** — 1-2 изречения.
**Здраве** — 1-2 изречения.

Пиши на български, ясно и практично, без жаргон. Общо ~400-500 думи. НЕ използвай маркери SUMMARY."""

    ai_key, provider = get_ai_config()
    if not ai_key:
        return None
    raw = call_ai(ai_key, provider, prompt, max_tokens=1500)
    set_sign_profile(sign_data["sign"], raw)
    return raw


# --- Вечнозелени SEO страници „съвместимост по зодии" (/savmestimost/{a}-{b}) ---
# Каноничният ред е зодиакален: по-ранният знак винаги е първи, за да няма
# дублиращи се URL-и за една и съща двойка („овен-телец" = „телец-овен").
COMPAT_PAIRS = []          # (sign_a, sign_b, slug) — 78 двойки вкл. знак-сам-със-себе-си
COMPAT_BY_SLUG = {}
for _i, _sa in enumerate(ZODIAC_SIGNS):
    for _sb in ZODIAC_SIGNS[_i:]:
        _slug = f"{_sa['slug']}-{_sb['slug']}"
        COMPAT_PAIRS.append((_sa, _sb, _slug))
        COMPAT_BY_SLUG[_slug] = (_sa, _sb)


def get_compatibility(sign_a: str, sign_b: str) -> Optional[str]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT content FROM compatibility_cache WHERE sign_a = ? AND sign_b = ?",
            (sign_a, sign_b)
        ).fetchone()
        return row[0] if row else None


def set_compatibility(sign_a: str, sign_b: str, content: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO compatibility_cache (sign_a, sign_b, content, generated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(sign_a, sign_b) DO UPDATE SET content = excluded.content, generated_at = CURRENT_TIMESTAMP",
            (sign_a, sign_b, content)
        )
        conn.commit()


def _generate_compatibility(sign_a: dict, sign_b: dict) -> Optional[str]:
    """Напиши и кеширай тизъра за съвместимостта между два знака. Вечнозелено — веднъж."""
    name_a, name_b = sign_a["name"], sign_b["name"]
    same = sign_a["sign"] == sign_b["sign"]
    relation = "двама представители на един и същи знак" if same else "двама души с тези слънчеви знаци"

    prompt = f"""Ти си астролог. Напиши КРАТКО обяснение на съвместимостта между {name_a} и {name_b} (в любовта и отношенията).

КОНТЕКСТ:
- Знак {name_a}: {meaning_sign(sign_a['sign'])}. Стихия {sign_a['element']}, модалност {sign_a['modality']}, управител {sign_a['ruler']}.
- Знак {name_b}: {meaning_sign(sign_b['sign'])}. Стихия {sign_b['element']}, модалност {sign_b['modality']}, управител {sign_b['ruler']}.
- Пишеш за {relation}.

ВАЖНО: Това е ТИЗЪР за SEO страница — НЕ пълно персонално разчитане. Пиши само ОБЩИЯ случай (какво е типично за повечето двойки с тези слънчеви знаци). НЕ навлизай в домове, аспекти или конкретни градуси — това е част от персоналния анализ (синастрия), който читателят получава отделно. Целта е ясна обща представа, която да накара читателя да поиска персоналния анализ.

Структура (всяко заглавие на собствен ред, обградено с **звезди**):
**Общо съвпадение** — 2-3 изречения.
**Любов и емоции** — 2-3 изречения.
**Комуникация и интелект** — 2-3 изречения.
**Предизвикателства** — 2-3 изречения.
**Как да работи тази връзка** — 1-2 изречения.

Пиши на български, ясно и практично, без жаргон. Общо ~300-400 думи. НЕ използвай маркери SUMMARY."""

    ai_key, provider = get_ai_config()
    if not ai_key:
        return None
    raw = call_ai(ai_key, provider, prompt, max_tokens=1500)
    set_compatibility(sign_a["sign"], sign_b["sign"], raw)
    return raw


# --- Вечнозелени SEO страници „планета в дом" (/luna-v-7-dom и т.н.) ---
BODY_PLANETS = [p for p in PLANETS if p["key"] != "Asc"]  # 10 планети × 12 дома = 120


def get_planet_house(planet: str, house: str) -> Optional[str]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT content FROM planet_house_cache WHERE planet = ? AND house = ?",
            (planet, house)
        ).fetchone()
        return row[0] if row else None


def set_planet_house(planet: str, house: str, content: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO planet_house_cache (planet, house, content, generated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(planet, house) DO UPDATE SET content = excluded.content, generated_at = CURRENT_TIMESTAMP",
            (planet, house, content)
        )
        conn.commit()


def _generate_planet_house(planet_data: dict, house_data: dict) -> Optional[str]:
    """Напиши и кеширай краткия тизър за „{планета} в {дом}". Вечнозелено — веднъж."""
    planet_name = planet_data["name"]
    house_name = house_data["name"]
    house_short = house_data["short"]

    prompt = f"""Ти си астролог. Напиши КРАТКО обяснение какво означава {planet_name} в {house_name} (по рождената карта).

КОНТЕКСТ:
- {planet_name}: {meaning_object(planet_data['key'])}
- {house_name} ({house_short}): {meaning_house(house_data['key'])}

ВАЖНО: Това е ТИЗЪР за SEO страница — НЕ пълно персонално разчитане. Пиши само ОБЩИЯ случай (какво значи за повечето хора с тази позиция). НЕ навлизай в аспекти, конкретни градуси или знака на върха на дома — това е част от персоналното разчитане, което читателят получава отделно. Целта е да дадеш ясна обща представа, която да накара читателя да поиска по-задълбочения анализ.

Структура (всяко заглавие на собствен ред, обградено с **звезди**):
**Общо значение** — 2-3 изречения.
**Любов и отношения** — 2-3 изречения.
**Работа и финанси** — 2-3 изречения.
**Как да използваш тази енергия** — 1-2 изречения.

Пиши на български, ясно и практично, без жаргон. Общо ~250-350 думи. НЕ използвай маркери SUMMARY и НЕ изброявай с тирета."""

    ai_key, provider = get_ai_config()
    if not ai_key:
        return None
    raw = call_ai(ai_key, provider, prompt, max_tokens=1200)
    set_planet_house(planet_data["key"], house_data["key"], raw)
    return raw


def public_base_url(request: Optional[Request] = None) -> str:
    """The address visitors actually use, as https wherever possible.

    Behind Coolify the app is served plain HTTP and the proxy terminates TLS,
    so `request.base_url` comes back as `http://` — which then went into the
    canonical link and og:url. Search engines treat that as a different site
    from the https one people visit, and some networks refuse to load an
    http image on an https page.
    """
    configured = (seo_settings().get("seo_site_url") or "").rstrip("/")
    if configured:
        return configured
    if request is None:
        return "http://127.0.0.1:8000"
    base = str(request.base_url).rstrip("/")
    # Trust the proxy's own header before rewriting anything.
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if proto == "https" and base.startswith("http://"):
        return "https://" + base[len("http://"):]
    # A public host reached over plain http is the proxy case above without
    # the header; localhost genuinely is http and must stay that way.
    host = request.url.hostname or ""
    if base.startswith("http://") and host not in ("localhost", "127.0.0.1", "::1"):
        return "https://" + base[len("http://"):]
    return base

def seo_context(request: Request, *, path: str = "/") -> dict:
    """Everything the public templates need to render their meta tags."""
    seo = seo_settings()
    base = public_base_url(request)
    image = seo["seo_og_image"] or ""
    if image.startswith("/"):
        image = base + image
    return {
        "seo_title": seo["seo_title"],
        "seo_description": seo["seo_description"],
        "seo_keywords": seo["seo_keywords"],
        "seo_robots": seo["seo_robots"],
        "seo_verification": seo["seo_verification"],
        "seo_image": image,
        "seo_url": base + path,
    }

# --- Admin API (ADMIN ONLY) ---
class AdminUserCreate(BaseModel):
    email: str
    password: str
    plan_key: Optional[str] = "demo"
    plan_expires: Optional[str] = None  # ISO date
    role: str = "user"
    note: Optional[str] = None

class AdminUserUpdate(BaseModel):
    plan_key: Optional[str] = None
    plan_expires: Optional[str] = None  # ISO date, or "" to clear
    role: Optional[str] = None
    is_blocked: Optional[bool] = None
    note: Optional[str] = None
    password: Optional[str] = None      # set a new password

class AdminPlanUpsert(BaseModel):
    key: str
    name: str
    price_cents: int = 0
    currency: str = "EUR"
    period: str = "month"
    max_persons: int = 1
    features: list = []
    is_active: bool = True
    sort_order: int = 0

class AdminPaymentCreate(BaseModel):
    user_id: int
    plan_key: Optional[str] = None
    amount_cents: int
    currency: str = "EUR"
    method: Optional[str] = None
    note: Optional[str] = None
    extend_months: int = 0  # also push the user's expiry out by this many months

@app.get("/api/admin/overview")
def api_admin_overview(admin: dict = Depends(require_admin)):
    """Headline numbers for the admin dashboard."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        blocked = conn.execute("SELECT COUNT(*) c FROM users WHERE is_blocked = 1").fetchone()["c"]
        persons = conn.execute("SELECT COUNT(*) c FROM persons").fetchone()["c"]
        by_plan = [dict(r) for r in conn.execute(
            "SELECT COALESCE(plan_key, 'demo') AS plan_key, COUNT(*) AS c FROM users GROUP BY 1"
        )]
        revenue = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) s FROM payments"
        ).fetchone()["s"]
        month_start = datetime.date.today().replace(day=1).isoformat()
        revenue_month = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) s FROM payments WHERE paid_at >= ?",
            (month_start,)
        ).fetchone()["s"]
        # Nothing expires any more, so the old "expiring soon" list would always
        # be empty. What an admin can act on instead is which modules sell.
        names = {f["key"]: f["name"] for f in FEATURE_CATALOGUE}
        top_modules = [
            {"key": r["feature_key"],
             "name": names.get(r["feature_key"], r["feature_key"]),
             "sold": r["sold"], "revenue_cents": r["revenue"]}
            for r in conn.execute(
                "SELECT feature_key, COUNT(*) sold,"
                " COALESCE(SUM(price_cents), 0) revenue"
                " FROM feature_purchases WHERE price_cents > 0"
                " GROUP BY feature_key ORDER BY sold DESC, revenue DESC LIMIT 10")
        ]
        recent = [dict(r) for r in conn.execute(
            "SELECT p.id, p.amount_cents, p.currency, p.paid_at, p.plan_key, u.email "
            "FROM payments p JOIN users u ON u.id = p.user_id "
            "ORDER BY p.paid_at DESC LIMIT 10"
        )]
    return {
        "users": users, "blocked": blocked, "persons": persons,
        "by_plan": by_plan,
        "revenue_cents": revenue, "revenue_month_cents": revenue_month,
        "top_modules": top_modules, "recent_payments": recent,
        # Checkout without a webhook secret takes money and unlocks nothing,
        # which is invisible from outside — so it is reported here.
        "payments_health": {
            "checkout_key": billing.checkout_key_present(),
            "webhook_secret": billing.webhook_secret_present(),
            "ready": billing.stripe_enabled(),
        },
    }

@app.get("/api/admin/users")
def api_admin_users(q: Optional[str] = None, admin: dict = Depends(require_admin)):
    """All accounts, with their plan and usage."""
    sql = ("SELECT u.id, u.email, u.role, u.plan_key, u.plan_expires, u.is_blocked, u.note, "
           "u.created_at, u.last_login, "
           "(SELECT COUNT(*) FROM persons p WHERE p.user_id = u.id) AS persons, "
           "(SELECT COALESCE(SUM(amount_cents),0) FROM payments pm WHERE pm.user_id = u.id) AS paid_cents "
           "FROM users u")
    params: list = []
    if q:
        sql += " WHERE u.email LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY u.created_at DESC"
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, params)]
    return {"users": rows}

@app.post("/api/admin/users")
def api_admin_create_user(data: AdminUserCreate, admin: dict = Depends(require_admin)):
    """Create an account by hand, with its plan set straight away."""
    email = (data.email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "Моля, въведете валиден имейл адрес.")
    if len((data.password or "").strip()) < 6:
        raise HTTPException(400, "Паролата трябва да е поне 6 символа.")
    if get_user_by_email(email):
        raise HTTPException(409, "Вече съществува акаунт с този имейл.")
    if data.role not in ("user", "admin"):
        raise HTTPException(400, "Ролята трябва да е 'user' или 'admin'.")
    if data.plan_key and not get_plan(data.plan_key):
        raise HTTPException(400, "Няма такъв пакет.")

    expires = (data.plan_expires or "").strip() or None
    if expires:
        try:
            datetime.date.fromisoformat(expires)
        except ValueError:
            raise HTTPException(400, "Датата трябва да е във формат ГГГГ-ММ-ДД.")

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, role, plan_key, plan_expires, note)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (email, hash_password(data.password.strip()), data.role,
             data.plan_key or "demo", expires, (data.note or "").strip() or None)
        )
        conn.commit()
        return {"ok": True, "id": cur.lastrowid, "email": email}

@app.patch("/api/admin/users/{user_id}")
def api_admin_update_user(user_id: int, data: AdminUserUpdate, admin: dict = Depends(require_admin)):
    """Change a user's plan, role, block state, note or password."""
    target = get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "Потребителят не е намерен.")

    sets, params = [], []
    if data.plan_key is not None:
        if not get_plan(data.plan_key):
            raise HTTPException(400, "Няма такъв пакет.")
        sets.append("plan_key = ?"); params.append(data.plan_key)
    if data.plan_expires is not None:
        value = data.plan_expires.strip() or None
        if value:
            try:
                datetime.date.fromisoformat(value)
            except ValueError:
                raise HTTPException(400, "Датата трябва да е във формат ГГГГ-ММ-ДД.")
        sets.append("plan_expires = ?"); params.append(value)
    if data.role is not None:
        if data.role not in ("user", "admin"):
            raise HTTPException(400, "Ролята трябва да е 'user' или 'admin'.")
        # Don't let the last administrator demote themselves out of the panel.
        if target["role"] == "admin" and data.role != "admin":
            with sqlite3.connect(DB_PATH) as conn:
                admins = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
            if admins <= 1:
                raise HTTPException(400, "Това е единственият администратор.")
        sets.append("role = ?"); params.append(data.role)
    if data.is_blocked is not None:
        if target["id"] == admin["id"] and data.is_blocked:
            raise HTTPException(400, "Не можеш да блокираш собствения си акаунт.")
        sets.append("is_blocked = ?"); params.append(1 if data.is_blocked else 0)
    if data.note is not None:
        sets.append("note = ?"); params.append(data.note.strip() or None)
    if data.password is not None and data.password.strip():
        if len(data.password.strip()) < 6:
            raise HTTPException(400, "Паролата трябва да е поне 6 символа.")
        sets.append("password_hash = ?"); params.append(hash_password(data.password.strip()))

    if not sets:
        return {"ok": True, "changed": False}

    params.append(user_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
    audit("user_updated", f"Променен потребител {target['email']} (id={user_id})",
          user_id=user_id, actor=admin["email"])
    return {"ok": True, "changed": True}

@app.delete("/api/admin/users/{user_id}")
def api_admin_delete_user(user_id: int, admin: dict = Depends(require_admin)):
    """Remove an account together with everything it owns."""
    if user_id == admin["id"]:
        raise HTTPException(400, "Не можеш да изтриеш собствения си акаунт.")
    target = get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "Потребителят не е намерен.")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM ai_cache WHERE person_id IN (SELECT id FROM persons WHERE user_id = ?)",
            (user_id,))
        conn.execute("DELETE FROM persons WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM payments WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    audit("user_deleted", f"Изтрит потребител {target['email']} (id={user_id})",
          user_id=user_id, actor=admin["email"])
    return {"ok": True}

@app.get("/api/admin/plans")
def api_admin_plans(admin: dict = Depends(require_admin)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = []
        for r in conn.execute("SELECT * FROM plans ORDER BY sort_order, key"):
            p = dict(r)
            try:
                p["features"] = json.loads(p["features"])
            except Exception:
                p["features"] = []
            p["users"] = conn.execute(
                "SELECT COUNT(*) FROM users WHERE COALESCE(plan_key,'demo') = ?", (p["key"],)
            ).fetchone()[0]
            rows.append(p)
    return {"plans": rows, "all_features": FEATURE_CATALOGUE}

@app.put("/api/admin/plans/{plan_key}")
def api_admin_upsert_plan(plan_key: str, data: AdminPlanUpsert, admin: dict = Depends(require_admin)):
    """Create or update a plan and what it unlocks."""
    unknown = [f for f in data.features if f not in {f["key"] for f in FEATURE_CATALOGUE}]
    if unknown:
        raise HTTPException(400, f"Непознати функции: {', '.join(unknown)}")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO plans (key, name, price_cents, currency, period, max_persons, features, is_active, sort_order)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET name=excluded.name, price_cents=excluded.price_cents,"
            " currency=excluded.currency, period=excluded.period, max_persons=excluded.max_persons,"
            " features=excluded.features, is_active=excluded.is_active, sort_order=excluded.sort_order",
            (plan_key, data.name, data.price_cents, data.currency, data.period,
             data.max_persons, json.dumps(data.features), 1 if data.is_active else 0, data.sort_order)
        )
        conn.commit()
    return {"ok": True}

@app.delete("/api/admin/plans/{plan_key}")
def api_admin_delete_plan(plan_key: str, admin: dict = Depends(require_admin)):
    if plan_key == "demo":
        raise HTTPException(400, "Демо пакетът не може да се изтрие — той е резервният.")
    with sqlite3.connect(DB_PATH) as conn:
        in_use = conn.execute("SELECT COUNT(*) FROM users WHERE plan_key = ?", (plan_key,)).fetchone()[0]
        if in_use:
            raise HTTPException(400, f"Пакетът се ползва от {in_use} потребител(и).")
        conn.execute("DELETE FROM plans WHERE key = ?", (plan_key,))
        conn.commit()
    return {"ok": True}

@app.get("/api/admin/payments")
def api_admin_payments(user_id: Optional[int] = None, admin: dict = Depends(require_admin)):
    sql = ("SELECT p.*, u.email FROM payments p JOIN users u ON u.id = p.user_id")
    params: list = []
    if user_id:
        sql += " WHERE p.user_id = ?"
        params.append(user_id)
    sql += " ORDER BY p.paid_at DESC LIMIT 200"
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, params)]
    return {"payments": rows}

@app.post("/api/admin/payments")
def api_admin_record_payment(data: AdminPaymentCreate, admin: dict = Depends(require_admin)):
    """Log a payment, optionally extending the user's plan at the same time."""
    target = get_user_by_id(data.user_id)
    if not target:
        raise HTTPException(404, "Потребителят не е намерен.")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO payments (user_id, plan_key, amount_cents, currency, method, note, recorded_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (data.user_id, data.plan_key, data.amount_cents, data.currency,
             data.method, data.note, admin["id"])
        )
        if data.extend_months > 0:
            # Extend from the current expiry if it is still ahead, otherwise from today.
            base = datetime.date.today()
            if target.get("plan_expires"):
                try:
                    current = datetime.date.fromisoformat(str(target["plan_expires"])[:10])
                    base = max(base, current)
                except ValueError:
                    pass
            month = base.month - 1 + data.extend_months
            new_date = base.replace(
                year=base.year + month // 12,
                month=month % 12 + 1,
                day=min(base.day, [31, 29 if (base.year + month // 12) % 4 == 0 else 28,
                                   31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month % 12]),
            )
            sets = ["plan_expires = ?"]
            params: list = [new_date.isoformat()]
            if data.plan_key:
                sets.append("plan_key = ?"); params.append(data.plan_key)
            params.append(data.user_id)
            conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
    audit("payment_recorded", f"Ръчно плащане за {target['email']}: {data.amount_cents} {data.currency}",
          user_id=data.user_id, actor=admin["email"])
    return {"ok": True}

@app.delete("/api/admin/payments/{payment_id}")
def api_admin_delete_payment(payment_id: int, admin: dict = Depends(require_admin)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM payments WHERE id = ?", (payment_id,))
        conn.commit()
    audit("payment_deleted", f"Изтрито плащане id={payment_id}", actor=admin["email"])
    return {"ok": True}

# --- One-off feature purchases ---

class FeaturePriceUpdate(BaseModel):
    price_cents: int = 0
    currency: str = "EUR"
    is_purchasable: bool = True

class FeatureGrant(BaseModel):
    user_id: int
    feature_key: str
    price_cents: Optional[int] = None
    note: Optional[str] = None

@app.get("/api/admin/feature-prices")
def api_admin_feature_prices(admin: dict = Depends(require_admin)):
    """The one-off price list, with every catalogue feature represented."""
    prices = get_feature_prices()
    return {"features": [
        {
            **f,
            "price_cents": prices.get(f["key"], {}).get("price_cents", 0),
            "currency": prices.get(f["key"], {}).get("currency", "EUR"),
            "is_purchasable": bool(prices.get(f["key"], {}).get("is_purchasable", 0)),
        }
        for f in FEATURE_CATALOGUE
    ]}

@app.put("/api/admin/feature-prices/{feature_key}")
def api_admin_set_feature_price(feature_key: str, data: FeaturePriceUpdate,
                                admin: dict = Depends(require_admin)):
    """Set what a single feature costs as a one-off unlock."""
    if not any(f["key"] == feature_key for f in FEATURE_CATALOGUE):
        raise HTTPException(404, "Няма такава функция.")
    if data.price_cents < 0:
        raise HTTPException(400, "Цената не може да е отрицателна.")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO feature_prices (feature_key, price_cents, currency, is_purchasable)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(feature_key) DO UPDATE SET price_cents = excluded.price_cents,"
            " currency = excluded.currency, is_purchasable = excluded.is_purchasable",
            (feature_key, data.price_cents, data.currency, 1 if data.is_purchasable else 0))
        conn.commit()
    audit("price_changed", f"Цена на {feature_key}: {data.price_cents} {data.currency}",
          actor=admin["email"])
    return {"ok": True}

@app.get("/api/admin/feature-purchases")
def api_admin_feature_purchases(user_id: Optional[int] = None,
                                admin: dict = Depends(require_admin)):
    """Who bought what."""
    sql = ("SELECT fp.*, u.email FROM feature_purchases fp"
           " JOIN users u ON u.id = fp.user_id")
    params: list = []
    if user_id:
        sql += " WHERE fp.user_id = ?"
        params.append(user_id)
    sql += " ORDER BY fp.purchased_at DESC"
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return {"purchases": [dict(r) for r in conn.execute(sql, params)]}

@app.post("/api/admin/feature-purchases")
def api_admin_grant_feature(data: FeatureGrant, admin: dict = Depends(require_admin)):
    """Unlock a feature for a user and log the payment behind it."""
    target = get_user_by_id(data.user_id)
    if not target:
        raise HTTPException(404, "Потребителят не е намерен.")
    meta = next((f for f in FEATURE_CATALOGUE if f["key"] == data.feature_key), None)
    if not meta:
        raise HTTPException(404, "Няма такава функция.")

    price_row = get_feature_prices().get(data.feature_key, {})
    amount = data.price_cents if data.price_cents is not None else price_row.get("price_cents", 0)
    currency = price_row.get("currency", "EUR")

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO payments (user_id, plan_key, amount_cents, currency, method, note, recorded_by)"
            " VALUES (?, NULL, ?, ?, ?, ?, ?)",
            (data.user_id, amount, currency, "еднократно",
             data.note or f"Еднократно отключване: {meta['name']}", admin["id"]))
        conn.execute(
            "INSERT INTO feature_purchases (user_id, feature_key, price_cents, currency, payment_id)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, feature_key) DO UPDATE SET price_cents = excluded.price_cents,"
            " currency = excluded.currency, payment_id = excluded.payment_id,"
            " purchased_at = CURRENT_TIMESTAMP",
            (data.user_id, data.feature_key, amount, currency, cur.lastrowid))
        conn.commit()
    audit("feature_unlocked", f"Админ отключи {data.feature_key} за {target['email']}",
          user_id=data.user_id, actor=admin["email"])
    return {"ok": True}

@app.delete("/api/admin/feature-purchases/{user_id}/{feature_key}")
def api_admin_revoke_feature(user_id: int, feature_key: str,
                             admin: dict = Depends(require_admin)):
    """Take a one-off unlock back. The payment record stays for the books."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM feature_purchases WHERE user_id = ? AND feature_key = ?",
                     (user_id, feature_key))
        conn.commit()
    audit("feature_revoked", f"Админ отне {feature_key} от потребител id={user_id}",
          user_id=user_id, actor=admin["email"])
    return {"ok": True}

@app.get("/api/admin/audit")
def api_admin_audit(event: Optional[str] = None, user_id: Optional[int] = None,
                    limit: int = 100, offset: int = 0,
                    admin: dict = Depends(require_admin)):
    """Admin audit log, newest first, with optional filters."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    where, params = [], []
    if event:
        where.append("a.event = ?"); params.append(event)
    if user_id:
        where.append("a.user_id = ?"); params.append(user_id)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT a.*, u.email AS user_email FROM audit_log a"
            " LEFT JOIN users u ON u.id = a.user_id" + cond +
            " ORDER BY a.id DESC LIMIT ? OFFSET ?", params + [limit, offset])]
        total = conn.execute("SELECT COUNT(*) FROM audit_log" + cond, params).fetchone()[0]
        event_types = [r[0] for r in conn.execute(
            "SELECT DISTINCT event FROM audit_log ORDER BY event")]
    return {"events": rows, "total": total, "event_types": event_types}

def _parse_payment_note(note: str):
    """От note ('features:k1,k2 sess_...' или 'feature:k sess_...') връща (keys, session_id)."""
    note = (note or "").strip()
    keys, session_id = [], ""
    if " " in note:
        head, session_id = note.split(" ", 1)
    else:
        head = note
    if head.startswith("features:"):
        keys = [k.strip() for k in head[len("features:"):].split(",") if k.strip()]
    elif head.startswith("feature:"):
        keys = [head[len("feature:"):].strip()]
    return keys, session_id.strip()

@app.get("/api/admin/saft")
def api_admin_saft(year: int, month: int, admin: dict = Depends(require_admin)):
    """Генерира Стандартизиран одиторски файл (SAF-T) за даден месец.

    Наредба № Н-18, чл. 3, ал. 17. Изход: windows-1251 XML за подаване в НАП.
    Плейсхолдъри ([[...]]) за ЕИК и e_shop_n се попълват в LEGAL_DEFAULTS.
    """
    from fastapi.responses import Response
    if not (1 <= int(month) <= 12):
        raise HTTPException(400, "Месецът трябва да е между 1 и 12.")
    if int(year) < 2020:
        raise HTTPException(400, "Годината трябва да е поне 2020.")

    lg = legal()
    start = f"{int(year):04d}-{int(month):02d}-01"
    end = f"{int(year):04d}-{int(month):02d}-31"
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM payments WHERE method = 'stripe'"
            " AND paid_at >= ? AND paid_at < ? ORDER BY id",
            (start, end))]

    orders = []
    for p in rows:
        keys, session_id = _parse_payment_note(p.get("note") or "")
        if not keys:
            keys = ["feature"]
        total2 = float(p["amount_cents"]) / 100.0
        per = total2 / len(keys)
        items = []
        for k in keys:
            offer = feature_offer(k)
            name = (offer or {}).get("name") or k
            net, vat = saft.split_vat(per)
            items.append({
                "name": name, "quant": 1, "price": round(net, 2),
                "vat_rate": saft.VAT_RATE, "vat": vat, "total": round(per, 2),
            })
        net_total, vat_total = saft.split_vat(total2)
        paid = (p.get("paid_at") or "")[:10]
        orders.append({
            "ord_n": str(p["id"]),
            "ord_d": paid,
            "doc_n": p["id"],
            "doc_date": paid,
            "items": items,
            "total1": net_total,
            "disc": 0,
            "vat": vat_total,
            "total2": total2,
            "paym": saft.PAYM_PSP,
            "pos_n": "",
            "trans_n": session_id,
            "proc_id": "stripe",
        })

    xml_bytes = saft.build_saft_xml(
        eik=lg.get("company_id", ""),
        e_shop_n=lg.get("e_shop_n", ""),
        domain_name=BRAND_DOMAIN,
        e_shop_type=lg.get("e_shop_type", "1"),
        month=month, year=year,
        orders=orders,
    )
    filename = f"saft-{int(year)}-{int(month):02d}.xml"
    return Response(
        content=xml_bytes,
        media_type="application/xml; charset=windows-1251",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.get("/api/admin/2fa/status")
def api_admin_2fa_status(admin: dict = Depends(require_admin)):
    row = get_user_by_id(admin["id"])
    return {"enabled": bool(row and row.get("totp_secret"))}

@app.post("/api/admin/2fa/setup")
def api_admin_2fa_setup(admin: dict = Depends(require_admin)):
    """Генерира TOTP secret + otpauth URI (за сканиране). Активира се след confirm."""
    secret = generate_totp_secret()
    set_setting("totp_pending_secret", secret)
    uri = totp_uri(secret, admin["email"], brand_name())
    audit("2fa_setup", "Генериран secret за 2FA", user_id=admin["id"], actor=admin["email"])
    return {"secret": secret, "uri": uri}

@app.post("/api/admin/2fa/confirm")
def api_admin_2fa_confirm(data: dict, admin: dict = Depends(require_admin)):
    """Потвърждава с код от аппа и активира 2FA за админа."""
    pending = get_setting("totp_pending_secret")
    code = str(data.get("code") or "").strip()
    if not pending or not verify_totp(pending, code):
        raise HTTPException(400, "Невалиден код. Опитай отново.")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET totp_secret = ? WHERE id = ?", (pending, admin["id"]))
        conn.commit()
    set_setting("totp_pending_secret", "")
    audit("2fa_enabled", "2FA активирана", user_id=admin["id"], actor=admin["email"])
    return {"ok": True}

@app.post("/api/admin/2fa/disable")
def api_admin_2fa_disable(admin: dict = Depends(require_admin)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET totp_secret = NULL WHERE id = ?", (admin["id"],))
        conn.commit()
    set_setting("totp_pending_secret", "")
    audit("2fa_disabled", "2FA деактивирана", user_id=admin["id"], actor=admin["email"])
    return {"ok": True}

@app.get("/api/features")
def api_my_features(user: Tuple[int, str] = Depends(get_current_user)):
    """What the signed-in account can open, and the price of everything else."""
    user_id, _ = user
    row = get_user_by_id(user_id)
    if not row:
        raise HTTPException(401, "Невалиден акаунт.")
    unlocked = unlocked_features(row)
    return {
        "unlocked": unlocked,
        "purchased": purchased_features(user_id),
        "bundle": bundle_offer(row),
        "catalogue": [
            {
                **f,
                "unlocked": f["key"] in unlocked,
                "offer": None if f["key"] in unlocked else feature_offer(f["key"]),
            }
            for f in FEATURE_CATALOGUE
        ],
    }

@app.post("/api/features/bundle/request")
def api_request_bundle(request: Request, user: Tuple[int, str] = Depends(get_current_user)):
    """Buy every remaining module in one payment, at the bundle price.

    The discount is applied by splitting it across the line items, so Stripe
    charges the bundle price while the webhook still sees the individual keys
    it already knows how to grant.
    """
    user_id, email = user
    row = get_user_by_id(user_id)
    if not row:
        raise HTTPException(401, "Невалиден акаунт.")

    bundle = bundle_offer(row)
    if not bundle:
        raise HTTPException(400, "Няма достатъчно модули за пакет.")

    keys = bundle["keys"]
    if billing.stripe_enabled():
        # Spread the discount over the items, giving the remainder to the
        # first one so the line items add up to the bundle price exactly.
        share = BUNDLE_PRICE_CENTS // len(keys)
        remainder = BUNDLE_PRICE_CENTS - share * len(keys)
        items = []
        for i, key in enumerate(keys):
            offer = feature_offer(key)
            items.append({
                "key": key,
                "name": offer["name"],
                "amount_cents": share + (remainder if i == 0 else 0),
                "currency": offer["currency"],
            })
        base = site_base_url(request)
        success = stripe_success_url(f"{base}/settings?paid=1", amount_cents=BUNDLE_PRICE_CENTS, currency=(items[0]["currency"] if items else "EUR"))
        cancel = os.environ.get("STRIPE_CANCEL_URL") or f"{base}/settings?paid=0"
        try:
            url = billing.create_features_checkout(
                customer_email=email,
                customer_id=row.get("stripe_customer_id"),
                user_id=user_id,
                items=items,
                success_url=success,
                cancel_url=cancel,
                brand=brand_name(),
            )
            return {"ok": True, "checkout_url": url, "bundle": bundle}
        except Exception as e:
            log.warning("Stripe checkout за пакета се провали: %s", e)

    to = get_setting("smtp_from") or get_setting("smtp_user")
    if to:
        try_send_template(
            to, "bundle_request",
            email=email, user_id=user_id, keys=", ".join(keys),
            bundle_name=BUNDLE_NAME, price=f"{BUNDLE_PRICE_CENTS / 100:.2f}",
        )
    return {"ok": True, "bundle": bundle, "manual": True}

@app.post("/api/features/{feature_key}/request")
def api_request_feature(feature_key: str, request: Request,
                        user: Tuple[int, str] = Depends(get_current_user)):
    """Start Stripe checkout when configured; otherwise email the admin."""
    user_id, email = user
    row = get_user_by_id(user_id)
    if not row:
        raise HTTPException(401, "Невалиден акаунт.")
    if feature_key in unlocked_features(row):
        return {"ok": True, "already": True}

    offer = feature_offer(feature_key)
    if not offer:
        raise HTTPException(404, "Тази функция не се продава отделно.")

    if billing.stripe_enabled():
        base = site_base_url(request)
        success = stripe_success_url(f"{base}/settings?paid=1", amount_cents=offer["price_cents"], currency=offer["currency"])
        cancel = os.environ.get("STRIPE_CANCEL_URL") or f"{base}/settings?paid=0"
        try:
            url = billing.create_feature_checkout(
                customer_email=email,
                customer_id=row.get("stripe_customer_id"),
                user_id=user_id,
                feature_key=feature_key,
                feature_name=offer["name"],
                amount_cents=offer["price_cents"],
                currency=offer["currency"],
                success_url=success,
                cancel_url=cancel,
                brand=brand_name(),
            )
            return {"ok": True, "checkout_url": url, "offer": offer}
        except Exception as e:
            log.warning("Stripe checkout за %s се провали: %s", feature_key, e)

    to = get_setting("smtp_from") or get_setting("smtp_user")
    if to:
        try_send_template(
            to, "unlock_request",
            email=email, user_id=user_id, name=offer["name"],
            price=f"{offer['price_cents'] / 100:.2f}", currency=offer["currency"],
        )
    return {"ok": True, "offer": offer, "manual": True}

@app.get("/api/admin/settings")
def api_admin_settings(admin: dict = Depends(require_admin)):
    """App-wide settings: AI key status, SMTP and email templates."""
    ai_key = get_setting("ai_api_key")
    provider = get_setting("ai_provider") or "deepseek"
    return {
        "ai": {
            "provider": provider,
            "model": resolve_ai_model(provider),
            "models": {
                p: [{"id": mid, "label": label} for mid, label in opts]
                for p, opts in AI_MODELS.items()
            },
            "key_set": bool(ai_key),
            "key_masked": ("•" * 8 + ai_key[-4:]) if ai_key and len(ai_key) > 4 else None,
        },
        "smtp": {
            # smtp_setting() чете първо env (Coolify), после DB — така панелът
            # показва СЪЩОТО, което реално ползва send_email(), а не стар запис.
            "host": smtp_setting("smtp_host") or "",
            "port": smtp_setting("smtp_port") or "587",
            "user": smtp_setting("smtp_user") or "",
            "from": smtp_setting("smtp_from") or "",
            "use_tls": (smtp_setting("smtp_use_tls") or "1") == "1",
            "password_set": bool(smtp_setting("smtp_password")),
            "source": "env" if any(os.environ.get(n) for n in _SMTP_ENV.values()) else "db",
        },
        "templates": {
            key: get_setting(f"tpl_{key}") or default
            for key, default in EMAIL_TEMPLATES.items()
        },
        "seo": seo_settings(),
        "brand": {key: (get_setting(key) or default)
                  for key, default in BRAND_DEFAULTS.items()},
        # Secrets are never echoed back — only whether one is stored, so the
        # panel can say "configured" without handing the value to the browser.
        "oauth": {
            "google_client_id": oauth_config()["google_client_id"],
            "google_secret_set": bool(oauth_config()["google_client_secret"]),
            "facebook_app_id": oauth_config()["facebook_app_id"],
            "facebook_secret_set": bool(oauth_config()["facebook_app_secret"]),
            "from_env": {
                "google": bool(os.environ.get("GOOGLE_CLIENT_ID")),
                "facebook": bool(os.environ.get("FACEBOOK_APP_ID")),
            },
        },
        # Values behind the terms, the privacy policy and the N-18 documents.
        "legal": {key: (get_setting(f"legal_{key}") or default)
                  for key, default in LEGAL_DEFAULTS.items()},
    }

@app.post("/api/admin/settings")
def api_admin_save_settings(payload: dict, admin: dict = Depends(require_admin)):
    """Save whichever settings were supplied; blank values leave secrets alone."""
    ai = payload.get("ai") or {}
    if ai.get("provider"):
        set_setting("ai_provider", ai["provider"])
    if ai.get("model"):
        provider = ai.get("provider") or get_setting("ai_provider") or "deepseek"
        allowed = {m for m, _ in AI_MODELS.get(provider, [])}
        model = str(ai["model"]).strip()
        if model in allowed:
            set_setting("ai_model", model)
    if (ai.get("key") or "").strip():
        set_setting("ai_api_key", ai["key"].strip())

    smtp = payload.get("smtp") or {}
    # Когато SMTP идва от env (Coolify), не пишем в DB — env печели и DB запис
    # би бил мъртва данна (ще я изчистим при следващо стартиране).
    if not smtp_from_env():
        for field, key in [("host", "smtp_host"), ("port", "smtp_port"),
                           ("user", "smtp_user"), ("from", "smtp_from")]:
            if field in smtp:
                set_setting(key, str(smtp[field] or "").strip())
        if "use_tls" in smtp:
            set_setting("smtp_use_tls", "1" if smtp["use_tls"] else "0")
        if (smtp.get("password") or "").strip():
            set_setting("smtp_password", smtp["password"].strip())

    for key, value in (payload.get("templates") or {}).items():
        if key in EMAIL_TEMPLATES:
            set_setting(f"tpl_{key}", value)

    for key, value in (payload.get("seo") or {}).items():
        if key in SEO_DEFAULTS:
            set_setting(key, str(value or "").strip())

    # A blank brand field means "go back to the default", so it is stored empty
    # and brand() falls through — that is why this does not skip empty values.
    for key, value in (payload.get("brand") or {}).items():
        if key in BRAND_DEFAULTS:
            set_setting(key, str(value or "").strip())

    for key, value in (payload.get("oauth") or {}).items():
        if key not in OAUTH_DEFAULTS:
            continue
        text = str(value or "").strip()
        # A blank secret means "leave it alone", so saving the form without
        # retyping the secret does not wipe it. Ids are cleared normally.
        if key.endswith("_secret") and not text:
            continue
        set_setting(f"oauth_{key}", text)

    for key, value in (payload.get("legal") or {}).items():
        if key in LEGAL_DEFAULTS:
            set_setting(f"legal_{key}", str(value or "").strip())

    sections = [k for k in ("ai", "smtp", "templates", "seo", "brand") if payload.get(k)]
    audit("settings_changed", f"Смени секции: {', '.join(sections) or '—'}",
          actor=admin["email"])
    return {"ok": True}

def _brand_logo_url() -> str:
    """Absolute URL на логото за имейли (https + бранд домейн)."""
    domain = brand().get("domain") or "astrokarta.bg"
    logo = brand().get("logo") or "/static/logo-header.png"
    if logo.startswith(("http://", "https://")):
        return logo
    return f"https://{domain}{logo}"


def _text_to_html(text: str) -> str:
    """Plain text → HTML: escape, авто-линкове на URL, нови редове → <br>."""
    import html as _html
    out = _html.escape(text or "")
    # Авто-линк на https?://… адреси (вече escaped, така че & е &amp;).
    out = re.sub(r"(https?://[^\s<>\"']+)",
                 r'<a href="\1" style="color:#8659a3;text-decoration:underline;">\1</a>', out)
    return out.replace("\n", "<br>")


def _email_html(body_text: str) -> str:
    """Обвива plain текст в стилизиран HTML имейл с лого и бранд цветове.

    Цветовете следват light theme на приложението (виолетов акцент #8659a3).
    Inline CSS, защото имейл клиентите не поддържат <style> навсякъде.
    """
    b = brand()
    name = b.get("name") or "АстроКарта"
    domain = b.get("domain") or "astrokarta.bg"
    logo = _brand_logo_url()
    body = _text_to_html(body_text)
    return (
        '<!DOCTYPE html><html lang="bg"><body style="margin:0;padding:0;'
        'background:#f4eff7;font-family:Arial,Helvetica,sans-serif;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f4eff7;padding:24px 0;"><tr><td align="center">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'style="max-width:600px;width:100%;">'
        '<tr><td align="center" style="padding:8px 0 20px;">'
        f'<img src="{logo}" alt="{name}" width="44" height="44" '
        'style="display:block;border:0;border-radius:10px;">'
        f'<div style="font-size:18px;font-weight:bold;color:#6d4a89;margin-top:10px;">{name}</div>'
        '</td></tr>'
        '<tr><td style="background:#ffffff;border:1px solid #e5d4ec;border-radius:12px;'
        'padding:32px 36px;color:#2d2438;font-size:15px;line-height:1.7;">'
        f'{body}'
        '</td></tr>'
        '<tr><td align="center" style="padding:20px 0;color:#7a6d8a;font-size:12px;line-height:1.6;">'
        f'{name} &middot; <a href="https://{domain}" '
        f'style="color:#8659a3;text-decoration:none;">{domain}</a>'
        '</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def send_email(to: str, subject: str, body: str, attachment: Optional[tuple] = None,
               html: Optional[str] = None) -> None:
    """Send a message over the configured SMTP server.

    `attachment` is an optional (filename, bytes, mimetype) triple.
    `html` is an optional HTML body; `body` stays the plain-text fallback.
    Raises HTTPException with a readable message on failure.
    """
    import smtplib
    from email.message import EmailMessage

    host = smtp_setting("smtp_host")
    if not host:
        raise HTTPException(400, "SMTP сървърът не е конфигуриран. Задай го в Настройки.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = (smtp_setting("smtp_from") or smtp_setting("smtp_user")
                   or f"noreply@{brand()['domain']}")
    msg["To"] = to
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    if attachment:
        filename, data, mimetype = attachment
        maintype, _, subtype = mimetype.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream",
                           filename=filename)

    user = smtp_setting("smtp_user")
    password = smtp_setting("smtp_password") or ""
    port = int(smtp_setting("smtp_port") or 587)
    use_tls = (smtp_setting("smtp_use_tls") or "1") == "1"

    try:
        if use_tls:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls()
                if user:
                    s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                if user:
                    s.login(user, password)
                s.send_message(msg)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Изпращането се провали: {e}")

# Logos are written to UPLOAD_DIR rather than over the bundled files, so a bad
# upload never destroys the originals and reverting is a matter of clearing the
# field. They are served from /uploads, mounted separately from /static.
BRAND_LOGO_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}
BRAND_LOGO_MAX_BYTES = 2 * 1024 * 1024

@app.post("/api/admin/settings/logo")
async def api_admin_upload_logo(file: UploadFile = File(...),
                                slot: str = Form("brand_logo"),
                                admin: dict = Depends(require_admin)):
    """Replace one of the two logos. `slot` picks the header or the full mark."""
    if slot not in ("brand_logo", "brand_logo_full"):
        raise HTTPException(400, "Непознато място за лого.")

    suffix = BRAND_LOGO_TYPES.get((file.content_type or "").lower())
    if not suffix:
        raise HTTPException(400, "Логото трябва да е PNG, JPG, WebP или SVG.")

    data = await file.read()
    if not data:
        raise HTTPException(400, "Файлът е празен.")
    if len(data) > BRAND_LOGO_MAX_BYTES:
        raise HTTPException(400, "Логото е над 2 MB. Смали го и опитай пак.")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # The random suffix busts caches: browsers and Coolify's proxy both hold
    # onto static assets aggressively, and a reused name would show the old mark.
    name = f"{slot}-{secrets.token_hex(4)}{suffix}"
    (UPLOAD_DIR / name).write_bytes(data)

    previous = get_setting(slot) or ""
    set_setting(slot, f"/uploads/{name}")
    _remove_upload(previous)

    return {"ok": True, "url": f"/uploads/{name}"}

def _remove_upload(url: str) -> None:
    """Delete an uploaded logo, ignoring the bundled defaults and stray paths."""
    if not url.startswith("/uploads/"):
        return
    name = Path(url).name  # never trust the URL for anything but the filename
    try:
        target = UPLOAD_DIR / name
        if target.is_file():
            target.unlink()
    except OSError:
        log.warning("could not remove the logo %s", url, exc_info=True)

@app.post("/api/admin/settings/logo/reset")
def api_admin_reset_logo(payload: dict, admin: dict = Depends(require_admin)):
    """Drop an uploaded logo and fall back to the bundled file."""
    slot = (payload.get("slot") or "").strip()
    if slot not in ("brand_logo", "brand_logo_full"):
        raise HTTPException(400, "Непознато място за лого.")
    current = get_setting(slot) or ""
    set_setting(slot, "")
    _remove_upload(current)
    return {"ok": True, "url": BRAND_DEFAULTS[slot]}

@app.post("/api/admin/settings/test-email")
def api_admin_test_email(payload: dict, admin: dict = Depends(require_admin)):
    """Send a test message through the configured SMTP server."""
    to = (payload.get("to") or "").strip()
    if "@" not in to:
        raise HTTPException(400, "Въведи валиден имейл адрес.")
    body = "Това е тестово съобщение. Ако го получаваш, SMTP настройките работят."
    send_email(to, f"Тестов имейл от {brand_name()}", body, html=_email_html(body))
    return {"ok": True}

def _template_preview_data() -> list:
    """Тестови данни за преглед на обикновените имейл темплейти (без receipt/invoice)."""
    return [
        ("welcome", dict(name="Иван", link="https://astrokarta.bg/dashboard")),
        ("set_password", dict(link="https://astrokarta.bg/reset-password?token=DEMO_TOKEN_123")),
        ("reset_password", dict(name="Иван", link="https://astrokarta.bg/reset-password?token=DEMO_TOKEN_123")),
        ("digest", dict(
            name="Иван", date="24.08.2026",
            reading="Дневното разчитане за Иван Петров:\n\nСлънцето в Дева подсказва ден за подреждане на делата. Внимателен с обещанията в късния следобед.\n\nПълният текст: https://astrokarta.bg/chart/42")),
        ("share", dict(title="Натална карта", person_name="Иван Петров", name="Иван")),
        ("unlock_request", dict(email="ivan@example.com", user_id=42,
                                name="Нумерология", price="4.20", currency="EUR")),
        ("bundle_request", dict(email="ivan@example.com", user_id=42,
                                keys="numerology, synastry", bundle_name="Пълен пакет",
                                price="20.00")),
    ]

@app.post("/api/admin/templates/preview")
def api_admin_templates_preview(payload: dict, admin: dict = Depends(require_admin)):
    """Изпраща всички имейл темплейти (HTML) + касов документ/фактура (PDF)."""
    to = (payload.get("to") or "").strip()
    if "@" not in to:
        raise HTTPException(400, "Въведи валиден имейл адрес.")
    if not smtp_setting("smtp_host"):
        raise HTTPException(400, "SMTP сървърът не е конфигуриран.")
    sent = 0
    for kind, fields in _template_preview_data():
        subject, body = render_email_template(kind, **fields)
        send_email(to, subject, body, html=_email_html(body))
        sent += 1
    # Касов документ (Н-18) и фактура (ЗДДС) — реалният резултат е PDF attachment.
    items = [{"name": "Натална карта", "net": 8.40, "vat": 1.68,
              "total": 10.08, "vat_rate": 20}]
    send_receipt_email(to, items=items, total2=10.08, unp=4201, stripe_id="cs_test_preview")
    send_invoice_email(to, items=items, total2=10.08, invoice_number="0000000001")
    sent += 2
    audit("templates_preview", f"Изпратени {sent} темплейта за преглед до {to}",
          actor=admin["email"])
    return {"ok": True, "sent": sent, "to": to}

# --- Site URL, lifecycle emails, billing fulfillment, password reset, share ---

def site_base_url(request: Optional[Request] = None) -> str:
    configured = (seo_settings().get("seo_site_url") or "").rstrip("/")
    if configured:
        return configured
    if request is not None:
        return str(request.base_url).rstrip("/")
    return "http://127.0.0.1:8000"

_TPL_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _fill_template(template: str, fields: dict) -> str:
    """Substitute {key} placeholders brace-safely.

    Unlike str.format this never treats { or } inside a field value as a
    placeholder (AI text can contain braces), and unknown placeholders are
    left untouched rather than raising.
    """
    def repl(m: "re.Match") -> str:
        value = fields.get(m.group(1))
        return "" if value is None else str(value)
    return _TPL_PLACEHOLDER_RE.sub(repl, template)


def render_email_template(kind: str, **fields) -> Tuple[str, str]:
    """Return (subject, body) for a template kind, overridable from DB settings.

    Kinds: welcome | set_password | reset_password | digest | share | receipt |
    unlock_request | bundle_request. Admins edit them in Настройки → Шаблони,
    stored as tpl_<kind>_subject / tpl_<kind>_body settings.
    """
    subject = get_setting(f"tpl_{kind}_subject") or EMAIL_TEMPLATES[f"{kind}_subject"]
    body = get_setting(f"tpl_{kind}_body") or EMAIL_TEMPLATES[f"{kind}_body"]
    safe = {k: ("" if v is None else str(v)) for k, v in fields.items()}
    # Every template may reference {brand}; a caller-supplied value wins.
    safe.setdefault("brand", brand_name())
    return _fill_template(subject, safe), _fill_template(body, safe)

def try_send_template(to: str, kind: str, **fields) -> bool:
    """Send a templated email; returns False when SMTP is missing or send fails."""
    if not to or not smtp_setting("smtp_host"):
        return False
    subject, body = render_email_template(kind, **fields)
    try:
        send_email(to, subject, body, html=_email_html(body))
        return True
    except Exception as e:
        log.warning("Неуспешен %s имейл до %s: %s", kind, to, e)
        return False

def audit(event: str, detail: str = "", *, user_id: Optional[int] = None,
          actor: str = "system") -> None:
    """Append a row to the admin audit log. Never raises."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO audit_log (user_id, actor_email, event, detail) VALUES (?, ?, ?, ?)",
                (user_id, actor or "system", event, detail))
            conn.commit()
    except Exception:
        log.exception("audit log write failed")


def record_payment(user_id: int, *, plan_key: Optional[str], amount_cents: int,
                   currency: str, method: str, note: str,
                   session_id: Optional[str] = None) -> Optional[int]:
    """Write one payment to the ledger.

    Returns the new row id, or None when this Stripe session was already
    recorded — the caller should read that as "already fulfilled", not as a
    failure.
    """
    with sqlite3.connect(DB_PATH) as conn:
        if session_id:
            existing = conn.execute(
                "SELECT id FROM payments WHERE stripe_session_id = ?",
                (session_id,)).fetchone()
            if existing:
                return None
        try:
            cur = conn.execute(
                "INSERT INTO payments (user_id, plan_key, amount_cents, currency,"
                " method, note, stripe_session_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, plan_key, amount_cents, currency, method, note, session_id))
        except sqlite3.IntegrityError:
            # Two deliveries raced; the other one won.
            return None
        conn.commit()
        return cur.lastrowid

def grant_feature_purchase(user_id: int, feature_key: str, price_cents: int,
                           currency: str = "EUR", payment_id: Optional[int] = None) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO feature_purchases (user_id, feature_key, price_cents, currency, payment_id)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, feature_key) DO UPDATE SET"
            " price_cents = excluded.price_cents, currency = excluded.currency,"
            " payment_id = COALESCE(excluded.payment_id, feature_purchases.payment_id)",
            (user_id, feature_key, price_cents, currency, payment_id))
        conn.commit()

def _strip_stripe(obj):
    """Recursively flatten Stripe's StripeObject into plain dicts/lists.

    A StripeObject is neither a dict nor iterable — `dict(obj)` raises
    TypeError — so it must be flattened through its own `to_dict_recursive()` /
    `to_dict()` first. The older shallow `to_dict()` leaves nested StripeObjects
    behind, hence the recursion over dict/list values.
    """
    if hasattr(obj, "to_dict_recursive"):
        obj = obj.to_dict_recursive()
    elif hasattr(obj, "to_dict"):
        obj = obj.to_dict()
    if isinstance(obj, dict):
        return {k: _strip_stripe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_stripe(v) for v in obj]
    return obj

def stripe_success_url(default_path: str, amount_cents: Optional[int] = None, currency: str = "") -> str:
    """Success URL for Checkout, always carrying the session id.

    Stripe substitutes {CHECKOUT_SESSION_ID} on redirect. The page uses it to
    settle the purchase immediately instead of waiting for the webhook, so an
    override that forgets the placeholder would quietly reintroduce the bug
    where a paid module still looks locked.

    When amount/currency are known they ride along too, so the redirect page
    can fire a value-carrying purchase event for GA4 and Meta Pixel — without
    them the reports count sales but cannot total the revenue.
    """
    url = (os.environ.get("STRIPE_SUCCESS_URL") or "").strip() or default_path
    if "CHECKOUT_SESSION_ID" not in url:
        url += ("&" if "?" in url else "?") + "session_id={CHECKOUT_SESSION_ID}"
    if amount_cents is not None:
        url += ("&" if "?" in url else "?") + f"amount_cents={amount_cents}"
        if currency:
            url += f"&currency={currency.upper()}"
    return url


def fulfill_checkout_session(session: dict) -> None:
    """Apply a completed Stripe Checkout session to the local DB."""
    meta = session.get("metadata") or {}
    kind = meta.get("kind") or ""
    try:
        user_id = int(meta.get("user_id") or session.get("client_reference_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    if not user_id:
        log.warning("Stripe session без user_id: %s", session.get("id"))
        return

    amount = int(session.get("amount_total") or 0)
    currency = (session.get("currency") or "eur").upper()
    customer_id = session.get("customer")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")

    session_id = session.get("id")

    # A basket of modules bought in one go, from the signup flow.
    if kind == "features" and meta.get("feature_keys"):
        keys = [k.strip() for k in meta["feature_keys"].split(",") if k.strip()]
        pay_id = record_payment(
            user_id, plan_key=None, amount_cents=amount, currency=currency,
            method="stripe", note=f"features:{','.join(keys)} {session_id}",
            session_id=session_id)
        if pay_id is None:
            # Already handled: a redelivered webhook, or the customer's own
            # return beat it here. Repeating the unlocks is harmless, but the
            # ledger and the documents must not double up.
            log.info("Stripe сесия %s вече е обработена", session_id)
            return
        for key in keys:
            offer = feature_offer(key)
            grant_feature_purchase(
                user_id, key,
                offer["price_cents"] if offer else 0,
                offer["currency"] if offer else currency,
                pay_id)
        if customer_id:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE users SET stripe_customer_id = COALESCE(stripe_customer_id, ?) WHERE id = ?",
                    (customer_id, user_id))
                conn.commit()
        log.info("Stripe отключване %s за user=%s", keys, user_id)
        audit("payment_succeeded", f"Stripe {amount} {currency} за модули {', '.join(keys)} ({session.get('id')})",
              user_id=user_id, actor="stripe")
        audit("feature_unlocked", f"Модули отключени: {', '.join(keys)}", user_id=user_id, actor="stripe")
        # Електронен документ (Н-18) — изпраща се на купувача по имейл.
        email = _session_email(session)
        if email:
            per = amount / 100.0 / len(keys)
            items = []
            for key in keys:
                o = feature_offer(key)
                name = (o or {}).get("name") or key
                net, vat = saft.split_vat(per)
                items.append({"name": name, "net": net, "vat": vat,
                              "total": per, "vat_rate": saft.VAT_RATE})
            send_sale_documents(email, items=items, total2=amount / 100.0,
                                unp=pay_id, stripe_id=session.get("id") or "",
                                user_id=user_id)
        return

    feature_key = meta.get("feature_key")
    if kind == "feature" and feature_key:
        pay_id = record_payment(
            user_id, plan_key=None, amount_cents=amount, currency=currency,
            method="stripe", note=f"feature:{feature_key} {session_id}",
            session_id=session_id)
        if pay_id is None:
            log.info("Stripe сесия %s вече е обработена", session_id)
            return
        grant_feature_purchase(user_id, feature_key, amount, currency, pay_id)
        if customer_id:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE users SET stripe_customer_id = COALESCE(stripe_customer_id, ?) WHERE id = ?",
                    (customer_id, user_id))
                conn.commit()
        log.info("Stripe отключване %s за user=%s", feature_key, user_id)
        audit("payment_succeeded", f"Stripe {amount} {currency} за {feature_key} ({session.get('id')})",
              user_id=user_id, actor="stripe")
        audit("feature_unlocked", f"Модул отключен: {feature_key}", user_id=user_id, actor="stripe")
        # Електронен документ (Н-18) — изпраща се на купувача по имейл.
        email = _session_email(session)
        if email:
            net, vat = saft.split_vat(amount / 100.0)
            name = (feature_offer(feature_key) or {}).get("name") or feature_key
            send_sale_documents(email, items=[{"name": name, "net": net, "vat": vat,
                                             "total": amount / 100.0, "vat_rate": saft.VAT_RATE}],
                                total2=amount / 100.0, unp=pay_id,
                                stripe_id=session.get("id") or "",
                                user_id=user_id)
        # A chart bought through onboarding belongs to an account that has no
        # usable password yet; this is the visitor's way in.
        if feature_key == "chart":
            send_welcome_set_password(user_id)

def _session_email(session: dict) -> str:
    """Имейлът на купувача от Stripe Checkout session."""
    cd = session.get("customer_details") or {}
    if isinstance(cd, dict):
        return (cd.get("email") or session.get("customer_email") or "").strip()
    return (session.get("customer_email") or "").strip()

def send_receipt_email(email: str, *, items, total2, unp, stripe_id) -> bool:
    """Изпраща електронен документ за продажба (Н-18, чл. 52а) като PDF."""
    if not email or not smtp_setting("smtp_host"):
        return False
    lg = legal()
    now = datetime.datetime.now(ZoneInfo("Europe/Sofia")).strftime("%d.%m.%Y %H:%M:%S")
    net_total = sum(float(it.get("net", 0)) for it in items)
    vat_total = sum(float(it.get("vat", 0)) for it in items)

    logo = BASE_DIR / "static" / "logo-header.png"
    try:
        pdf_bytes = build_receipt_pdf(
            brand=brand_name(),
            company_name=lg.get("company_name") or "",
            company_id=lg.get("company_id") or "",
            items=items, net_total=net_total, vat_total=vat_total,
            total=total2, vat_rate=saft.VAT_RATE,
            datetime_str=now, unp=unp, stripe_id=stripe_id,
            logo_path=str(logo) if logo.exists() else None)
    except Exception as e:
        log.warning("PDF за касов документ се провали: %s", e)
        return False

    subject, body = render_email_template("receipt", unp=unp)
    filename = f"{brand_slug()}-kasov-dokument-{unp}.pdf"
    try:
        send_email(email, subject, body,
                   attachment=(filename, pdf_bytes, "application/pdf"),
                   html=_email_html(body))
        return True
    except Exception as e:
        log.warning("Неуспешен receipt имейл до %s: %s", email, e)
        return False

def issue_invoice(user_id: int, payment_id: Optional[int]) -> str:
    """Издава фактура (ЗДДС) и връща 10-цифрен пореден номер (чл. 113 ЗДДС).

    Номерът идва от AUTOINCREMENT — монотонно нараства и никога не се
    преизползва, дори след изтриване на редове.
    """
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO invoices (payment_id, user_id) VALUES (?, ?)",
            (payment_id, user_id))
        inv_id = cur.lastrowid
        number = f"{inv_id:010d}"
        conn.execute("UPDATE invoices SET number = ? WHERE id = ?", (number, inv_id))
        conn.commit()
        return number

def send_invoice_email(email: str, *, items, total2, invoice_number) -> bool:
    """Изпраща фактура по ЗДДС (чл. 114) като PDF."""
    if not email or not smtp_setting("smtp_host"):
        return False
    lg = legal()
    now = datetime.datetime.now(ZoneInfo("Europe/Sofia")).strftime("%d.%m.%Y %H:%M:%S")
    net_total = sum(float(it.get("net", 0)) for it in items)
    vat_total = sum(float(it.get("vat", 0)) for it in items)

    logo = BASE_DIR / "static" / "logo-header.png"
    try:
        pdf_bytes = build_invoice_pdf(
            brand=brand_name(),
            company_name=lg.get("company_name") or "",
            company_id=lg.get("company_id") or "",
            vat_number=lg.get("vat_number") or "",
            address=lg.get("address") or "",
            invoice_number=invoice_number, issued_at=now,
            items=items, net_total=net_total, vat_total=vat_total,
            vat_rate=saft.VAT_RATE, total=total2,
            logo_path=str(logo) if logo.exists() else None)
    except Exception as e:
        log.warning("PDF за фактура се провали: %s", e)
        return False

    subject, body = render_email_template("invoice", invoice_number=invoice_number)
    filename = f"{brand_slug()}-faktura-{invoice_number}.pdf"
    try:
        send_email(email, subject, body,
                   attachment=(filename, pdf_bytes, "application/pdf"),
                   html=_email_html(body))
        return True
    except Exception as e:
        log.warning("Неуспешен invoice имейл до %s: %s", email, e)
        return False

def send_sale_documents(email: str, *, items, total2, unp, stripe_id, user_id) -> None:
    """Издава касов документ (Н-18) + фактура (ЗДДС) за една продажба.

    Касовият документ покрива фискализацията (чл. 52а Н-18); фактурата е
    задължителна, защото търговецът е регистриран по ЗДДС.
    """
    send_receipt_email(email, items=items, total2=total2, unp=unp, stripe_id=stripe_id)
    invoice_number = issue_invoice(user_id, unp)
    send_invoice_email(email, items=items, total2=total2, invoice_number=invoice_number)

def hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def create_password_reset(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.datetime.utcnow() + datetime.timedelta(hours=2)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))
        conn.execute(
            "INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (hash_reset_token(token), user_id, expires.isoformat()))
        conn.commit()
    return token

def send_welcome_set_password(user_id: int) -> None:
    """Email a set-password link to someone who just bought their first chart.

    Silent on failure: the payment already went through, and a missing email
    must not look like a failed purchase. The visitor can still use
    "forgot password" to get in.
    """
    row = get_user_by_id(user_id)
    if not row:
        return
    try:
        token = create_password_reset(user_id)
        base = (get_setting("seo_site_url") or "").rstrip("/")
        link = base + "/reset-password?token=" + token
        try_send_template(row["email"], "set_password", link=link)
    except Exception as e:
        log.warning("Welcome mail за user=%s се провали: %s", user_id, e)


def consume_password_reset(token: str) -> Optional[int]:
    th = hash_reset_token(token)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT user_id, expires_at FROM password_resets WHERE token_hash = ?",
            (th,)).fetchone()
        if not row:
            return None
        user_id, expires_at = row
        try:
            exp = datetime.datetime.fromisoformat(str(expires_at))
        except ValueError:
            exp = datetime.datetime.utcnow()
        conn.execute("DELETE FROM password_resets WHERE token_hash = ?", (th,))
        conn.commit()
        if exp < datetime.datetime.utcnow():
            return None
        return int(user_id)

def run_digest_emails() -> None:
    """Opt-in daily nudge. Uses cached horoscope text when available; never calls AI."""
    today = datetime.date.today().isoformat()
    base = site_base_url()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        users = [dict(r) for r in conn.execute(
            "SELECT id, email, display_name, last_digest_on, plan_key, plan_expires, role"
            " FROM users WHERE digest_opt_in = 1 AND is_blocked = 0")]
    for u in users:
        if u.get("last_digest_on") == today:
            continue
        if "horoscope" not in unlocked_features(u):
            continue
        persons = get_all_persons(u["id"])
        if not persons:
            continue
        person = persons[0]
        cache_key = f"horoscope:{today}"
        cached = get_ai_cache(person["id"], cache_key)
        name = u.get("display_name") or (u.get("email") or "").split("@")[0]
        chart_link = f"{base}/chart/{person['id']}"
        if cached and cached.get("content"):
            _, prose = split_summary(cached["content"])
            excerpt = (prose or cached["content"]).strip()
            if len(excerpt) > 900:
                excerpt = excerpt[:900].rsplit(" ", 1)[0] + "…"
            reading = (f"Дневното разчитане за {person['name']}:\n\n{excerpt}\n\n"
                       f"Пълният текст: {chart_link}")
        else:
            reading = (f"Дневният хороскоп за {person['name']} те чака в "
                       f"{brand_name()}:\n{chart_link}")
        if not smtp_setting("smtp_host"):
            return
        try:
            try_send_template(u["email"], "digest", name=name, reading=reading,
                              date=today)
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("UPDATE users SET last_digest_on = ? WHERE id = ?",
                             (today, u["id"]))
                conn.commit()
        except Exception as e:
            log.warning("Digest до %s се провали: %s", u["email"], e)

def run_scheduled_jobs() -> None:
    run_digest_emails()

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

class DigestUpdate(BaseModel):
    digest_opt_in: bool

class ShareCreate(BaseModel):
    cache_key: str

# --- Social sign-in -------------------------------------------------------
# Authorisation-code flow, exchanged server-side. The browser never sees the
# client secret, and a token forged by a hostile page cannot be replayed here
# because we ask the provider ourselves who the code belongs to.

OAUTH_ENDPOINTS = {
    "google": {
        "auth": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "profile": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
    },
    "facebook": {
        "auth": "https://www.facebook.com/v19.0/dialog/oauth",
        "token": "https://graph.facebook.com/v19.0/oauth/access_token",
        "profile": "https://graph.facebook.com/me?fields=id,name,email",
        "scope": "email public_profile",
    },
}

# Pending OAuth states, so a callback cannot be replayed or forged (CSRF).
# In-process is enough: the window is one redirect and a restart only costs
# the visitor a second attempt.
_OAUTH_STATES: dict = {}
_OAUTH_STATE_TTL = 600      # seconds


def _oauth_state_new(provider: str, next_url: str) -> str:
    token = secrets.token_urlsafe(24)
    now = time.time()
    # Opportunistic cleanup keeps the dict from growing without bound.
    for key, value in list(_OAUTH_STATES.items()):
        if now - value["at"] > _OAUTH_STATE_TTL:
            _OAUTH_STATES.pop(key, None)
    _OAUTH_STATES[token] = {"provider": provider, "next": next_url, "at": now}
    return token


def _oauth_state_take(token: str) -> Optional[dict]:
    """One-shot: a state is valid once, which stops replay."""
    data = _OAUTH_STATES.pop(token or "", None)
    if not data or time.time() - data["at"] > _OAUTH_STATE_TTL:
        return None
    return data


def _oauth_redirect_uri(request: Request, provider: str) -> str:
    return f"{public_base_url(request)}/api/auth/{provider}/callback"


def _oauth_post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _oauth_get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _oauth_link_or_create(provider: str, provider_user_id: str,
                          email: str, display_name: str) -> dict:
    """Find the account this identity belongs to, creating one if needed.

    Three cases, in order: the identity is already linked; the email matches
    an existing account, so the identity is attached to it rather than making
    a second account for the same person; or nobody is known and we create.
    """
    email = (email or "").strip().lower()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT user_id FROM oauth_accounts WHERE provider = ? AND provider_user_id = ?",
            (provider, provider_user_id)).fetchone()
    if row:
        user = get_user_by_id(row["user_id"])
        if user:
            return user

    user = get_user_by_email(email) if email else None
    if not user:
        if not email:
            # Facebook can withhold the email; without one there is no way to
            # reach the person or to merge later, so we stop rather than make
            # an unreachable account.
            raise HTTPException(400,
                "Профилът не върна имейл адрес. Влез с имейл и парола или "
                "разреши достъпа до имейла си.")
        # No usable password: this account is reached through the provider,
        # and "forgot password" still works because the email is real.
        user = create_user(email, hash_password(secrets.token_urlsafe(32)))
        if display_name:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("UPDATE users SET display_name = ? WHERE id = ?",
                             (display_name[:80], user["id"]))
                conn.commit()
        audit("sign_up", f"Регистрация през {provider}: {email}",
              user_id=user["id"], actor=email)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO oauth_accounts"
            " (provider, provider_user_id, user_id, email) VALUES (?, ?, ?, ?)",
            (provider, provider_user_id, user["id"], email))
        conn.commit()
    return user


@app.get("/api/auth/{provider}/start")
def api_oauth_start(provider: str, request: Request, next: str = "/dashboard"):
    """Send the visitor to the provider's consent screen."""
    if provider not in OAUTH_ENDPOINTS or not oauth_providers().get(provider):
        raise HTTPException(404, "Този начин за вход не е активен.")
    cfg = oauth_config()
    client_id = cfg["google_client_id"] if provider == "google" else cfg["facebook_app_id"]

    # Only our own paths, so the callback cannot be used as an open redirect.
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/dashboard"
    state = _oauth_state_new(provider, safe_next)
    params = {
        "client_id": client_id,
        "redirect_uri": _oauth_redirect_uri(request, provider),
        "response_type": "code",
        "scope": OAUTH_ENDPOINTS[provider]["scope"],
        "state": state,
    }
    if provider == "google":
        # Ask for a fresh account choice rather than silently reusing one.
        params["prompt"] = "select_account"
    url = OAUTH_ENDPOINTS[provider]["auth"] + "?" + urllib.parse.urlencode(params)
    return RedirectResponse(url, status_code=302)


@app.get("/api/auth/{provider}/callback", response_class=HTMLResponse)
def api_oauth_callback(provider: str, request: Request,
                       code: str = "", state: str = "", error: str = ""):
    """Where the provider sends the visitor back."""
    if provider not in OAUTH_ENDPOINTS or not oauth_providers().get(provider):
        raise HTTPException(404, "Този начин за вход не е активен.")

    if error or not code:
        # The visitor cancelled, which is not a failure worth an error page.
        return RedirectResponse("/login?oauth=cancelled", status_code=302)

    saved = _oauth_state_take(state)
    if not saved or saved["provider"] != provider:
        raise HTTPException(400, "Изтекла или невалидна заявка. Опитай пак.")

    cfg = oauth_config()
    if provider == "google":
        client_id, client_secret = cfg["google_client_id"], cfg["google_client_secret"]
    else:
        client_id, client_secret = cfg["facebook_app_id"], cfg["facebook_app_secret"]

    try:
        token_data = _oauth_post(OAUTH_ENDPOINTS[provider]["token"], {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": _oauth_redirect_uri(request, provider),
            "grant_type": "authorization_code",
        })
        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError("no access_token in response")
        profile = _oauth_get(OAUTH_ENDPOINTS[provider]["profile"], access_token)
    except Exception as e:
        log.warning("OAuth %s се провали: %s", provider, e)
        raise HTTPException(502, "Влизането през външния профил не се получи. Опитай пак.")

    provider_user_id = str(profile.get("sub") or profile.get("id") or "")
    if not provider_user_id:
        raise HTTPException(502, "Профилът не върна идентификатор.")

    user = _oauth_link_or_create(
        provider, provider_user_id,
        profile.get("email") or "",
        profile.get("name") or "")

    if user.get("is_blocked"):
        raise HTTPException(403, "Акаунтът е блокиран.")

    token = create_token(user["id"], user["email"])
    audit("login", f"Вход през {provider}: {user['email']}",
          user_id=user["id"], actor=user["email"])

    # The token is handed to the page rather than put in the URL, where it
    # would land in history and in any referrer header.
    return HTMLResponse(templates.get_template("oauth_done.html").render({
        "request": request,
        "token": token,
        "email": user["email"],
        "next_url": saved["next"],
    }))


@app.post("/api/auth/forgot-password")
def api_forgot_password(data: ForgotPasswordRequest, request: Request):
    """Always returns ok to avoid email enumeration. Sends a reset link when possible."""
    email = (data.email or "").strip().lower()
    user = get_user_by_email(email) if email else None
    if user:
        token = create_password_reset(user["id"])
        link = f"{site_base_url(request)}/reset-password?token={urllib.parse.quote(token)}"
        name = user.get("display_name") or email.split("@")[0]
        if smtp_setting("smtp_host"):
            try_send_template(email, "reset_password", name=name, link=link)
    return {"ok": True}

@app.post("/api/auth/reset-password")
def api_reset_password(data: ResetPasswordRequest):
    if len(data.new_password or "") < 6:
        raise HTTPException(400, "Паролата трябва да е поне 6 символа.")
    user_id = consume_password_reset((data.token or "").strip())
    if not user_id:
        raise HTTPException(400, "Линкът е невалиден или е изтекъл. Заяви нов.")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (hash_password(data.new_password), user_id))
        conn.commit()
    return {"ok": True}

@app.get("/api/billing/status")
def api_billing_status(user: Tuple[int, str] = Depends(get_current_user)):
    row = get_user_by_id(user[0])
    if not row:
        raise HTTPException(401, "Невалиден акаунт.")
    return {
        "stripe_enabled": billing.stripe_enabled(),
        "plan_key": row.get("plan_key"),
        "purchased": purchased_features(user[0]),
        "digest_opt_in": bool(row.get("digest_opt_in")),
    }

@app.post("/api/billing/checkout/feature/{feature_key}")
def api_checkout_feature(feature_key: str, request: Request,
                         user: Tuple[int, str] = Depends(get_current_user)):
    if not billing.stripe_enabled():
        raise HTTPException(503, "Онлайн плащанията още не са включени.")
    user_id, email = user
    row = get_user_by_id(user_id)
    if not row:
        raise HTTPException(401, "Невалиден акаунт.")
    if feature_key in unlocked_features(row):
        return {"ok": True, "already": True}
    offer = feature_offer(feature_key)
    if not offer:
        raise HTTPException(404, "Тази функция не се продава отделно.")
    base = site_base_url(request)
    success = stripe_success_url(f"{base}/settings?paid=1", amount_cents=offer["price_cents"], currency=offer["currency"])
    cancel = os.environ.get("STRIPE_CANCEL_URL") or f"{base}/settings?paid=0"
    try:
        url = billing.create_feature_checkout(
            customer_email=email,
            customer_id=row.get("stripe_customer_id"),
            user_id=user_id,
            feature_key=feature_key,
            feature_name=offer["name"],
            amount_cents=offer["price_cents"],
            currency=offer["currency"],
            success_url=success,
            cancel_url=cancel,
            brand=brand_name(),
        )
    except Exception as e:
        raise HTTPException(502, f"Stripe грешка: {e}") from e
    return {"url": url}

@app.get("/api/billing/session/{session_id}")
def api_billing_session(session_id: str,
                        user: Tuple[int, str] = Depends(get_current_user)):
    """Settle a checkout session the customer has just come back from.

    The webhook is the durable path, but it arrives on Stripe's schedule and
    can lag the redirect by seconds — or never arrive if the endpoint is
    misconfigured. Either way the customer is staring at a module they just
    paid for, which is what makes people pay a second time. This asks Stripe
    directly and fulfils the same session through the same code: whichever
    path lands first wins, the other is a no-op.
    """
    user_id, _ = user
    if not billing.checkout_key_present():
        raise HTTPException(503, "Онлайн плащанията не са включени.")
    try:
        stripe = billing.get_stripe()
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        log.warning("Stripe сесия %s не се прочете: %s", session_id, e)
        raise HTTPException(502, "Плащането не можа да се провери. Опитай пак.")

    # Never let one account settle another's session.
    raw = _strip_stripe(session) if "_strip_stripe" in globals() else dict(session)
    meta = dict(raw.get("metadata") or {})
    try:
        owner = int(meta.get("user_id") or raw.get("client_reference_id") or 0)
    except (TypeError, ValueError):
        owner = 0
    if owner != user_id:
        raise HTTPException(403, "Тази поръчка не е твоя.")

    paid = raw.get("payment_status") == "paid"
    if paid:
        fulfill_checkout_session(raw)

    row = get_user_by_id(user_id)
    # The page fires the purchase event, and an event without a value produces
    # a report that counts sales but cannot total them — so the amount, the
    # currency and the order id travel back with the answer.
    keys = []
    if meta.get("feature_keys"):
        keys = [k.strip() for k in meta["feature_keys"].split(",") if k.strip()]
    elif meta.get("feature_key"):
        keys = [meta["feature_key"]]
    names = {f["key"]: f["name"] for f in FEATURE_CATALOGUE}
    return {
        "paid": paid,
        "status": raw.get("payment_status"),
        "unlocked": unlocked_features(row) if row else [],
        "purchase": {
            "transaction_id": session_id,
            "price_cents": int(raw.get("amount_total") or 0),
            "currency": (raw.get("currency") or "eur").upper(),
            "keys": keys,
            "name": ", ".join(names.get(k, k) for k in keys),
        } if paid else None,
    }


@app.post("/api/stripe/webhook")
async def api_stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = billing.construct_webhook_event(payload, sig)
    except Exception as e:
        raise HTTPException(400, f"Webhook грешка: {e}") from e

    etype = event["type"]
    audit("webhook_received", f"Stripe event: {etype}", actor="stripe")
    obj = event["data"]["object"]
    # StripeObject-ът няма dict методи (.get) — изравни рекурсивно в чист dict,
    # иначе fulfill_checkout_session хвърля грешка.
    obj = _strip_stripe(obj)
    try:
        # Only one-off purchases exist now, so the subscription events that
        # used to arrive here have nothing left to update.
        if etype == "checkout.session.completed":
            # A session can "complete" while the money is still moving with
            # delayed payment methods; only grant once Stripe says it is paid.
            if obj.get("payment_status") == "paid":
                fulfill_checkout_session(obj)
            else:
                log.warning("Stripe session %s приключи без paid статус: %s",
                            obj.get("id"), obj.get("payment_status"))
                audit("webhook_skipped",
                      f"Stripe session {obj.get('id')} не е paid ({obj.get('payment_status')})",
                      actor="stripe")
    except Exception:
        log.exception("Обработка на Stripe event %s се провали", etype)
        audit("webhook_error", f"Stripe event {etype} се провали", actor="stripe")
        raise HTTPException(500, "Вътрешна грешка при webhook.")
    return {"ok": True}

@app.post("/api/account/digest")
def api_account_digest(data: DigestUpdate, user: Tuple[int, str] = Depends(get_current_user)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET digest_opt_in = ? WHERE id = ?",
                     (1 if data.digest_opt_in else 0, user[0]))
        conn.commit()
    return {"ok": True, "digest_opt_in": bool(data.digest_opt_in)}

@app.post("/api/persons/{person_id}/share")
def api_create_share(person_id: int, data: ShareCreate, request: Request,
                     user: Tuple[int, str] = Depends(get_current_user)):
    user_id, _ = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")
    cache_key = (data.cache_key or "").strip()
    if not cache_key or not get_ai_cache(person_id, cache_key):
        raise HTTPException(404, "Няма запазено разчитане за споделяне.")
    token = secrets.token_urlsafe(24)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO share_links (token, person_id, user_id, cache_key) VALUES (?, ?, ?, ?)",
            (token, person_id, user_id, cache_key))
        conn.commit()
    base = site_base_url(request)
    return {"token": token, "url": f"{base}/share/{token}"}

@app.get("/api/share/{token}")
def api_get_share(token: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT s.*, p.name AS person_name FROM share_links s"
            " JOIN persons p ON p.id = s.person_id WHERE s.token = ?",
            (token,)).fetchone()
    if not row:
        raise HTTPException(404, "Линкът за споделяне не е намерен.")
    cached = get_ai_cache(row["person_id"], row["cache_key"])
    if not cached:
        raise HTTPException(404, "Разчитането вече не е налично.")
    summary, prose = split_summary(cached["content"])
    title_key = row["cache_key"].split(":")[0]
    title = READING_TITLES.get(title_key, READING_TITLES.get(row["cache_key"], "Разчитане"))
    return {
        "person_name": row["person_name"],
        "title": title,
        "cache_key": row["cache_key"],
        "summary": summary,
        "content": prose or cached["content"],
        "generated_at": cached.get("generated_at"),
    }

# --- Account settings (the signed-in user's own profile) ---
# The AI provider and key are installation-wide and live in the admin panel;
# nothing here may touch them.

class AccountUpdate(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None

class PasswordChange(BaseModel):
    current_password: str
    new_password: str

@app.get("/api/account")
def api_get_account(user: Tuple[int, str] = Depends(get_current_user)):
    """The signed-in user's own profile and plan."""
    user_id, email = user
    row = get_user_by_id(user_id)
    if not row:
        raise HTTPException(401, "Невалиден акаунт.")
    plan = effective_plan(row)
    return {
        "id": user_id,
        "email": row.get("email") or email,
        "display_name": row.get("display_name") or "",
        "role": row.get("role", "user"),
        "is_admin": row.get("role") == "admin",
        "created_at": row.get("created_at"),
        "plan": {
            "key": plan.get("key"),
            "name": plan.get("name"),
            "max_persons": person_limit(row),
        },
        # What the account actually owns, by name — purchases never expire, so
        # this is the whole story of what was paid for.
        "owned_modules": [
            {"key": f["key"], "name": f["name"]}
            for f in FEATURE_CATALOGUE
            if not f.get("included") and f["key"] in set(purchased_features(user_id))
        ],
        "digest_opt_in": bool(row.get("digest_opt_in")),
        "stripe_enabled": billing.stripe_enabled(),
    }

@app.post("/api/account")
def api_update_account(data: AccountUpdate, user: Tuple[int, str] = Depends(get_current_user)):
    """Update the user's own name and email. Only supplied fields change."""
    user_id, _ = user

    fields, values = [], []
    if data.display_name is not None:
        fields.append("display_name = ?")
        values.append(data.display_name.strip()[:80])

    new_email = None
    if data.email is not None and data.email.strip():
        new_email = data.email.strip().lower()
        if "@" not in new_email or "." not in new_email.split("@")[-1]:
            raise HTTPException(400, "Моля, въведи валиден имейл адрес.")
        existing = get_user_by_email(new_email)
        if existing and existing["id"] != user_id:
            raise HTTPException(409, "Вече съществува акаунт с този имейл.")
        fields.append("email = ?")
        values.append(new_email)

    if not fields:
        return {"ok": True}

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", (*values, user_id))
        conn.commit()

    # Changing the email invalidates the old token's claim, so issue a fresh one.
    row = get_user_by_id(user_id)
    result = {"ok": True, "email": row.get("email"), "display_name": row.get("display_name") or ""}
    if new_email:
        result["token"] = create_token(user_id, row["email"])
    return result

@app.post("/api/account/password")
def api_change_password(data: PasswordChange, user: Tuple[int, str] = Depends(get_current_user)):
    """Change the user's own password, verifying the current one first."""
    user_id, _ = user
    row = get_user_by_id(user_id)
    if not row:
        raise HTTPException(401, "Невалиден акаунт.")
    if not verify_password(data.current_password or "", row["password_hash"]):
        raise HTTPException(403, "Текущата парола не е вярна.")
    if len(data.new_password or "") < 6:
        raise HTTPException(400, "Новата парола трябва да е поне 6 символа.")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (hash_password(data.new_password), user_id))
        conn.commit()
    # The old token stays valid; it carries no password claim.
    return {"ok": True}

@app.get("/api/account/export")
def api_export_account(user: Tuple[int, str] = Depends(get_current_user)):
    """GDPR чл. 20 — преносимост: пълно копие на данните в машинночетим формат."""
    from datetime import datetime, timezone
    user_id, email = user
    row = get_user_by_id(user_id)
    if not row:
        raise HTTPException(401, "Невалиден акаунт.")
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        persons = [dict(r) for r in conn.execute(
            "SELECT id, name, year, month, day, hour, minute, lat, lon, timezone, created_at"
            " FROM persons WHERE user_id = ? ORDER BY id", (user_id,))]
        payments = [dict(r) for r in conn.execute(
            "SELECT id, plan_key, amount_cents, currency, method, note, paid_at"
            " FROM payments WHERE user_id = ? ORDER BY id", (user_id,))]
        purchases = [dict(r) for r in conn.execute(
            "SELECT feature_key, price_cents, currency, purchased_at"
            " FROM feature_purchases WHERE user_id = ?", (user_id,))]
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "account": {
            "email": row.get("email"),
            "display_name": row.get("display_name") or "",
            "created_at": row.get("created_at"),
            "plan_key": row.get("plan_key"),
            "digest_opt_in": bool(row.get("digest_opt_in")),
        },
        "persons": persons,
        "payments": payments,
        "feature_purchases": purchases,
    }

@app.delete("/api/account")
def api_delete_account(user: Tuple[int, str] = Depends(get_current_user)):
    """GDPR чл. 17 — право на изтриване. Заличава акаунта и всички свързани данни."""
    user_id, email = user
    row = get_user_by_id(user_id)
    if not row:
        raise HTTPException(401, "Невалиден акаунт.")
    if row.get("role") == "admin":
        raise HTTPException(403, "Администраторският акаунт не може да се изтрие от тук.")

    # Най-напред спираме евентуален активен абонамент в Stripe, за да не
    # продължи таксуването след изтриването (best-effort, никога не блокира).
    sub_id = row.get("stripe_subscription_id")
    if sub_id and billing.stripe_enabled():
        try:
            billing.cancel_subscription_at_period_end(sub_id)
        except Exception as e:
            log.warning("Неуспешно анулиране на Stripe абонамент %s: %s", sub_id, e)

    with sqlite3.connect(DB_PATH) as conn:
        person_ids = [r[0] for r in conn.execute(
            "SELECT id FROM persons WHERE user_id = ?", (user_id,))]
        if person_ids:
            qs = ",".join("?" * len(person_ids))
            conn.execute(f"DELETE FROM ai_cache WHERE person_id IN ({qs})", person_ids)
            conn.execute(f"DELETE FROM share_links WHERE person_id IN ({qs})", person_ids)
        conn.execute("DELETE FROM persons WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM feature_purchases WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM payments WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM audit_log WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()

    audit("account_deleted", "Изтрит акаунт по искане на потребителя (GDPR чл. 17).")
    return {"ok": True, "deleted": user_id}

# --- Geocoding (place name -> coordinates, via OpenStreetMap Nominatim) ---
_geocode_cache: dict = {}
_geocode_last_call: list = [0.0]  # mutable holder so the helper can update it

def geocode_place(query: str, limit: int = 6) -> list:
    """Look up a place name and return candidate locations with coordinates.

    Nominatim's usage policy requires an identifying User-Agent and at most one
    request per second, so results are cached and calls are spaced out.
    """
    import time
    import urllib.parse
    import urllib.request

    key = query.strip().lower()
    if not key:
        return []
    if key in _geocode_cache:
        return _geocode_cache[key]

    # Respect Nominatim's 1 request/second limit.
    elapsed = time.monotonic() - _geocode_last_call[0]
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)

    params = urllib.parse.urlencode({
        "q": query,
        "format": "json",
        "limit": limit,
        "addressdetails": 1,
        "accept-language": "bg",
    })
    req = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers={"User-Agent": "AstroKarta/1.0 (astrology chart app)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read())
    except Exception as e:
        raise HTTPException(502, f"Грешка при търсене на място: {e}")
    finally:
        _geocode_last_call[0] = time.monotonic()

    try:
        from timezonefinder import TimezoneFinder
        tf = TimezoneFinder()
    except Exception:
        tf = None

    results = []
    for item in raw:
        addr = item.get("address", {})
        place = (addr.get("city") or addr.get("town") or addr.get("village")
                 or addr.get("municipality") or addr.get("county") or item.get("name", ""))
        country = addr.get("country", "")
        lat, lon = float(item["lat"]), float(item["lon"])
        tz = None
        if tf:
            try:
                tz = tf.timezone_at(lat=lat, lng=lon)
            except Exception:
                tz = None
        results.append({
            "label": item.get("display_name", ""),
            "place": place,
            "country": country,
            "lat": lat,
            "lon": lon,
            "timezone": tz or "Europe/Sofia",
        })

    _geocode_cache[key] = results
    return results

@app.get("/api/geocode")
def api_geocode(q: str, user: Tuple[int, str] = Depends(get_current_user)):
    """Search for a place by name and return matching coordinates."""
    if len(q.strip()) < 2:
        return {"results": []}
    return {"results": geocode_place(q)}

@app.get("/api/public/geocode")
def api_public_geocode(q: str):
    """Same lookup for the pre-signup chart form, which has no token yet.

    Results are cached and rate-limited inside geocode_place, and a place name
    reveals nothing about anyone, so this is safe to leave open.
    """
    if len(q.strip()) < 2:
        return {"results": []}
    return {"results": geocode_place(q)}

# --- API Routes (AUTH REQUIRED) ---
@app.get("/api/persons")
def api_list_persons(user: Tuple[int, str] = Depends(get_current_user)):
    user_id, email = user
    persons = get_all_persons(user_id)
    row = get_user_by_id(user_id)
    is_admin = bool(row and row.get("role") == "admin")
    limit = person_limit(row) if row else None
    return {
        "persons": persons,
        "quota": {
            "used": len(persons),
            "limit": limit,  # null means unlimited
            "can_add": is_admin or not limit or len(persons) < limit,
        },
    }

@app.get("/api/persons/{person_id}")
def api_get_person(person_id: int, user: Tuple[int, str] = Depends(get_current_user)):
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")
    return p

@app.post("/api/persons")
def api_create_person(
    name: str = Form(...),
    year: int = Form(...),
    month: int = Form(...),
    day: int = Form(...),
    hour: int = Form(0),
    minute: int = Form(0),
    lat: float = Form(...),
    lon: float = Form(...),
    timezone: str = Form("Europe/Sofia"),
    user: Tuple[int, str] = Depends(get_current_user),
):
    user_id, email = user
    row = get_user_by_id(user_id)
    if not row:
        raise HTTPException(401, "Невалиден акаунт.")
    if row.get("is_blocked"):
        raise HTTPException(403, "Акаунтът е блокиран.")

    # Plans cap how many people an account may keep; admins are exempt.
    if row.get("role") != "admin":
        limit = person_limit(row)
        with sqlite3.connect(DB_PATH) as conn:
            used = conn.execute(
                "SELECT COUNT(*) FROM persons WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
        if limit and used >= limit:
            # Point at the way out that actually applies: somebody who has not
            # bought the love reading gains a chart with it, so say so instead
            # of sending them to a bigger plan that no longer exists.
            owns_love = "love" in purchased_features(user_id)
            way_out = ("Изтрий някоя, за да добавиш нова."
                       if owns_love else
                       "Изтрий някоя или отключи „Любовен хороскоп“ — "
                       "той носи още една карта.")
            raise HTTPException(
                402,
                f"Профилът ти позволява до {limit} "
                f"{'карта' if limit == 1 else 'карти'}. {way_out}"
            )

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO persons (user_id, name, year, month, day, hour, minute, lat, lon, timezone) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user_id, name, year, month, day, hour, minute, lat, lon, timezone)
        )
        conn.commit()
        return {"id": cur.lastrowid, "name": name, "user_id": user_id}

@app.delete("/api/persons/{person_id}")
def api_delete_person(person_id: int, user: Tuple[int, str] = Depends(get_current_user)):
    user_id, email = user
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "DELETE FROM persons WHERE id = ? AND user_id = ?",
            (person_id, user_id)
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Този човек не е намерен в профила ти.")
    return {"deleted": person_id}

@app.get("/api/persons/{person_id}/natal")
def api_natal_chart(person_id: int,
                    user: Tuple[int, str] = Depends(require_feature("chart"))):
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")
    return compute_natal(p)

@app.post("/api/persons/{person_id}/natal")
def api_natal_chart_update(
    person_id: int,
    data: BirthDataUpdate,
    user: Tuple[int, str] = Depends(require_feature("chart")),
):
    """Update birth data and return recalculated natal chart."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")
    if not update_person(person_id, user_id, data):
        raise HTTPException(500, "Данните не можаха да се запазят. Опитай пак.")
    clear_ai_cache(person_id)
    p = get_person(person_id, user_id)
    return compute_natal(p)

@app.get("/api/persons/{person_id}/natal.txt")
def api_natal_chart_text(person_id: int,
                         user: Tuple[int, str] = Depends(require_feature("chart"))):
    """Return natal chart as plain text."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")
    chart_data = compute_natal(p)
    text = natal_to_text(p, chart_data)
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")

@app.get("/api/persons/{person_id}/chart.svg")
def api_chart_svg(person_id: int,
                  user: Tuple[int, str] = Depends(require_feature("chart"))):
    """Return natal chart as SVG."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")
    chart_data = compute_natal(p)
    from chart_svg import generate_chart_svg
    svg = generate_chart_svg(chart_data)
    return Response(content=svg, media_type="image/svg+xml")

# Shared tail for every AI prompt: the UI renders markdown headings, bullet lists
# and **bold**, so the model is asked to emit exactly that structure.
STYLE_RULES = """
=== КАК ДА ПИШЕШ ===
- Пиши на български, топло и практично, все едно говориш директно на човека.
- ОБРЪЩЕНИЕ: обръщай се на "ти" и САМО с малкото име (то е подадено като "Малко име"). Никога не използвай фамилията и не пиши на "Вие".
- ФОРМАТ: всяко номерирано заглавие започва на нов ред във вида `1. **Заглавие**`. Където изброяваш неща, ползвай тирета (`- нещо`), едно на ред. Не слепвай изброявания в един дълъг абзац.
- СТРУКТУРА: всяка секция да е самостоятелна и завършена. Не повтаряй едно и също през различните секции.
- ЗАВЪРШЕК: разчитането винаги завършва със завършено изречение и кратка заключителна мисъл. Никога не оставяй текста обрязан по средата на дума или изречение.
- ЛОГИКА: върви от общото към конкретното, така че читателят да вижда връзката между данните и изводите.
- ДЪЛЖИНА: бъди подробен — всяка секция с по няколко изречения реално съдържание, а изброяванията с кратко обяснение защо, не само голи думи.
- Бъди конкретен, избягвай клишета. Обяснявай астрологичните термини накратко, за да е разбираемо и за човек без познания.
- Основавай се единствено на подадените данни, без да добавяш измислени детайли.
- ГЛАС: пиши като астролог, който чете конкретната карта пред себе си. Не се
  представяй, не описвай процеса си и не споменавай, че си модел, асистент или
  програма. Никакви уводи от рода на "като изкуствен интелект", "въз основа на
  предоставените данни ще генерирам" или "надявам се това да е полезно".
- Започвай направо с разчитането. Без "Разбира се", "Ето", "С удоволствие"."""

def first_name(full_name: str) -> str:
    """First name only — the readings address the person informally."""
    return (full_name or "").strip().split()[0] if (full_name or "").strip() else ""

def split_summary(raw: str) -> Tuple[Optional[dict], str]:
    """Split an AI reply into its ---SUMMARY--- JSON block and the prose that follows.

    The summary drives the little cards above the text; if the model skipped it or
    emitted invalid JSON, the prose is still returned unchanged.
    """
    import re
    if not raw:
        return None, raw or ""
    match = re.search(r"---SUMMARY---\s*(.*?)\s*---END---\s*", raw, re.DOTALL)
    if not match:
        return None, raw
    body = raw[match.end():].lstrip()
    try:
        summary = json.loads(match.group(1))
    except Exception:
        return None, body
    return summary, body

PERSONAL_PLANETS = {"Sun", "Moon", "Mercury", "Venus", "Mars"}

def build_profile(chart_data: dict) -> dict:
    """Summarise a natal chart into a readable 'about me' profile:
    key points, element/modality balance, house emphasis and strongest aspects."""
    objects = chart_data.get("objects", {})
    by_name = {o["name"]: o for o in objects.values()}

    # Element and modality balance, counted over the personal + social planets
    # plus the Ascendant, which is what actually colours the temperament.
    counted = ["Sun", "Moon", "Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Asc"]
    elements: dict = {}
    modalities: dict = {}
    for name in counted:
        obj = by_name.get(name)
        if not obj:
            continue
        el = sign_element(obj["sign"])
        mo = sign_modality(obj["sign"])
        if el:
            elements[el] = elements.get(el, 0) + 1
        if mo:
            modalities[mo] = modalities.get(mo, 0) + 1

    def top_key(counts: dict):
        return max(counts, key=counts.get) if counts else None

    dominant_el = top_key(elements)
    dominant_mo = top_key(modalities)

    # Which houses hold the most planets — the life areas the chart emphasises.
    house_counts: dict = {}
    for obj in objects.values():
        if obj["name"] in PERSONAL_PLANETS or obj["name"] in {"Jupiter", "Saturn", "Uranus", "Neptune", "Pluto"}:
            hn = obj.get("house_number")
            if hn:
                house_counts[hn] = house_counts.get(hn, 0) + 1
    emphasised = sorted(house_counts.items(), key=lambda kv: kv[1], reverse=True)[:3]

    # Tightest aspects (smallest orb) between the meaningful bodies.
    aspect_bodies = PERSONAL_PLANETS | {"Jupiter", "Saturn", "Uranus", "Neptune", "Pluto", "Asc", "MC"}
    scored = [
        a for a in chart_data.get("aspects", [])
        if a.get("orb") is not None
        and a["active"] in aspect_bodies and a["passive"] in aspect_bodies
        and a["type"] in {"Conjunction", "Sextile", "Square", "Trine", "Opposition"}
    ]
    scored.sort(key=lambda a: abs(a["orb"]))
    seen = set()
    key_aspects = []
    for a in scored:
        pair = tuple(sorted((a["active"], a["passive"])))
        if pair in seen:
            continue
        seen.add(pair)
        key_aspects.append(a)
        if len(key_aspects) >= 6:
            break

    def point(name):
        o = by_name.get(name)
        if not o:
            return None
        return {
            "name_bg": o["name_bg"],
            "sign_bg": o["sign_bg"],
            "sign_symbol": o["sign_symbol"],
            "house_bg": o["house_bg"],
            "meaning": o.get("name_meaning", ""),
            "sign_meaning": o.get("sign_meaning", ""),
        }

    return {
        "core": {
            "sun": point("Sun"),
            "moon": point("Moon"),
            "ascendant": point("Asc"),
            "mc": point("MC"),
        },
        "personal_planets": [point(n) for n in ("Mercury", "Venus", "Mars") if point(n)],
        "elements": {
            "counts": {ELEMENTS_BG[k]: v for k, v in elements.items()},
            "dominant": ELEMENTS_BG.get(dominant_el) if dominant_el else None,
            "dominant_meaning": ELEMENT_MEANINGS.get(dominant_el, "") if dominant_el else "",
        },
        "modalities": {
            "counts": {MODALITIES_BG[k]: v for k, v in modalities.items()},
            "dominant": MODALITIES_BG.get(dominant_mo) if dominant_mo else None,
            "dominant_meaning": MODALITY_MEANINGS.get(dominant_mo, "") if dominant_mo else "",
        },
        "emphasised_houses": [
            {"house": h, "count": c, "meaning": meaning_house(f"{h}{'st' if h == 1 else 'nd' if h == 2 else 'rd' if h == 3 else 'th'} House")}
            for h, c in emphasised
        ],
        "key_aspects": [
            {
                "active_bg": a["active_bg"], "passive_bg": a["passive_bg"],
                "type_bg": a["type_bg"], "type_meaning": a.get("type_meaning", ""),
                "orb": round(a["orb"], 1),
            }
            for a in key_aspects
        ],
        "shape_bg": chart_data.get("shape_bg"),
        "shape_meaning": chart_data.get("shape_meaning"),
        "moon_phase_bg": chart_data.get("moon_phase_bg"),
        "moon_phase_meaning": chart_data.get("moon_phase_meaning"),
        "diurnal": chart_data.get("diurnal"),
    }

@app.get("/api/persons/{person_id}/profile")
def api_profile(person_id: int, user: Tuple[int, str] = Depends(require_feature("chart"))):
    """Computed 'about me' profile — deterministic, no AI."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")
    return build_profile(compute_natal(p))

@app.get("/api/persons/{person_id}/profile/interpretation")
def api_profile_interpretation(person_id: int, refresh: bool = False,
                               user: Tuple[int, str] = Depends(require_feature("profile"))):
    """AI 'about me' reading — strengths, weaknesses and what makes this chart distinctive."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")

    cache_key = "profile"
    if not refresh:
        cached = get_ai_cache(person_id, cache_key)
        if cached:
            return {"interpretation": cached["content"], "cached": True,
                    "generated_at": cached["generated_at"], "cache_key": cache_key}

    chart_data = compute_natal(p)
    prof = build_profile(chart_data)

    def fmt_point(label, pt):
        return f"{label}: {pt['name_bg']} в {pt['sign_bg']}, {pt['house_bg']}" if pt else f"{label}: няма данни"

    aspects_txt = "\n".join(
        f"- {a['active_bg']} {a['type_bg']} {a['passive_bg']} (орб {a['orb']}°)"
        for a in prof["key_aspects"]
    )
    houses_txt = ", ".join(f"{h['house']}-ти дом ({h['count']} планети)" for h in prof["emphasised_houses"])
    el_txt = ", ".join(f"{k}: {v}" for k, v in prof["elements"]["counts"].items())
    mo_txt = ", ".join(f"{k}: {v}" for k, v in prof["modalities"]["counts"].items())

    prompt = f"""Ти си професионален астролог. Напиши раздел "ЗА МЕН" — личен портрет на човека, СТРИКТНО базиран на точните данни от наталната му карта по-долу (изчислени със Swiss Ephemeris). Не измисляй позиции — обясни какво ОЗНАЧАВАТ.

Име: {p['name']}
Малко име (обръщай се само с него): {first_name(p['name'])}
Роден: {p['day']}.{p['month']}.{p['year']} в {p['hour']:02d}:{p['minute']:02d}

=== ЯДРО НА ЛИЧНОСТТА ===
{fmt_point('Слънце (същност)', prof['core']['sun'])}
{fmt_point('Луна (емоции)', prof['core']['moon'])}
{fmt_point('Асцендент (как те виждат)', prof['core']['ascendant'])}
{fmt_point('Медиум Коели (призвание)', prof['core']['mc'])}

=== ЛИЧНИ ПЛАНЕТИ ===
{chr(10).join(f"- {pt['name_bg']} в {pt['sign_bg']}, {pt['house_bg']}" for pt in prof['personal_planets'])}

=== БАЛАНС НА СТИХИИТЕ ===
{el_txt} — доминира: {prof['elements']['dominant']}

=== БАЛАНС НА КАЧЕСТВАТА ===
{mo_txt} — доминира: {prof['modalities']['dominant']}

=== НАЙ-АКЦЕНТИРАНИ ДОМОВЕ ===
{houses_txt}

=== НАЙ-СИЛНИ АСПЕКТИ (най-малък орб = най-точен и осезаем) ===
{aspects_txt}

=== ДРУГИ ===
Форма на картата: {prof['shape_bg']}
Лунна фаза при раждане: {prof['moon_phase_bg']}
Раждане: {'дневно' if prof['diurnal'] else 'нощно'}

=== ЗАДАЧА ===
Напиши личен портрет в следната структура (обръщай се на "ти", топло и директно):

1. **Кой си ти в едно изречение** — есенцията на характера, уловена кратко и запомнящо се.
2. **Твоята същност** — Слънце, Луна и Асцендент: кой си отвътре, какво чувстваш и как те виждат другите. Обясни разликите между трите, ако има такива.
3. **Силните ти страни** — 4-5 конкретни, изведени от реалните аспекти и позиции. За всяка обясни КАК се проявява в ежедневието.
4. **Слабите ти места** — 3-4 честни, но доброжелателни. Не плаши — обясни какъв е урокът и как се работи с тях.
5. **Твоят темперамент** — какво значи доминацията на стихията и качеството за начина, по който живееш.
6. **Къде е фокусът на живота ти** — акцентираните домове и какви теми носят.
7. **Интересни особености** — 3-4 любопитни детайла от картата: рядка конфигурация, необичайно силен аспект, ретроградна планета, форма на картата, лунна фаза, дневно/нощно раждане. Направи ги наистина интересни, не банални.
8. **Какво да развиваш** — 2-3 конкретни насоки за растеж.
""" + STYLE_RULES

    ai_key, provider = get_ai_config()
    if ai_key:
        try:
            interpretation = call_ai(ai_key, provider, prompt, max_tokens=6000, model=PAID_MODEL)
            set_ai_cache(person_id, cache_key, interpretation)
            return {"interpretation": interpretation, "cached": False, "cache_key": cache_key}
        except AIError as e:
            return {"interpretation": ai_failure_message(e)}
        except Exception as e:
            return {"interpretation": ai_failure_message(e)}

    return {"interpretation": AI_UNAVAILABLE}

KARMIC_POINTS = ("True North Node", "True South Node", "Chiron", "Saturn", "Pluto", "True Lilith")

def build_karmic(chart_data: dict, numerology: dict) -> dict:
    """Collect the chart's traditionally karmic markers — lunar nodes, Chiron,
    Saturn, Pluto, Lilith, 12th-house tenants and retrogrades — plus the
    numerology life path. These are the factual basis the akashic reading uses."""
    objects = chart_data.get("objects", {})
    by_name = {o["name"]: o for o in objects.values()}

    def pt(name):
        o = by_name.get(name)
        if not o:
            return None
        return {
            "name_bg": o["name_bg"], "sign_bg": o["sign_bg"], "sign_symbol": o["sign_symbol"],
            "house_bg": o["house_bg"], "house_number": o.get("house_number"),
            "retrograde": o.get("movement") == "Retrograde",
            "meaning": o.get("name_meaning", ""),
        }

    twelfth = [
        {"name_bg": o["name_bg"], "sign_bg": o["sign_bg"], "sign_symbol": o["sign_symbol"]}
        for o in objects.values()
        if o.get("house_number") == 12 and o["name"] not in ("Asc", "Desc", "MC", "IC")
    ]
    retrogrades = [
        {"name_bg": o["name_bg"], "sign_bg": o["sign_bg"], "sign_symbol": o["sign_symbol"],
         "house_bg": o["house_bg"]}
        for o in objects.values()
        if o.get("movement") == "Retrograde"
        and o["name"] in ("Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune", "Pluto", "Chiron")
    ]

    return {
        "points": {k: pt(k) for k in KARMIC_POINTS if pt(k)},
        "twelfth_house": twelfth,
        "retrogrades": retrogrades,
        "life_path": numerology["life_path"]["number"],
        "moon_phase_bg": chart_data.get("moon_phase_bg"),
        "diurnal": chart_data.get("diurnal"),
    }

@app.get("/api/persons/{person_id}/akashic")
def api_akashic(person_id: int, user: Tuple[int, str] = Depends(require_feature("akashic"))):
    """The karmic markers the akashic reading is built on (computed, no AI)."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")
    numerology = compute_numerology(p["name"], p["year"], p["month"], p["day"])
    return build_karmic(compute_natal(p), numerology)

@app.get("/api/persons/{person_id}/akashic/interpretation")
def api_akashic_interpretation(person_id: int, refresh: bool = False,
                               user: Tuple[int, str] = Depends(require_feature("akashic"))):
    """Akashic-records style reading of the chart's karmic markers.

    Framed as contemplative interpretation, not as retrieved record: there is no
    data source for akashic records, so the reading stays anchored to the chart.
    """
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")

    cache_key = "akashic"
    if not refresh:
        cached = get_ai_cache(person_id, cache_key)
        if cached:
            return {"interpretation": cached["content"], "cached": True,
                    "generated_at": cached["generated_at"], "cache_key": cache_key}

    chart_data = compute_natal(p)
    numerology = compute_numerology(p["name"], p["year"], p["month"], p["day"])
    k = build_karmic(chart_data, numerology)

    def line(label, point):
        if not point:
            return f"{label}: няма данни"
        retro = " (ретрограден)" if point["retrograde"] else ""
        return f"{label}: {point['name_bg']} в {point['sign_bg']}, {point['house_bg']}{retro}"

    twelfth_txt = ", ".join(f"{o['name_bg']} в {o['sign_bg']}" for o in k["twelfth_house"]) or "празен"
    retro_txt = ", ".join(f"{o['name_bg']} в {o['sign_bg']} ({o['house_bg']})" for o in k["retrogrades"]) or "няма"

    # Aspects touching the karmic points give the reading far more to work with
    # than the bare positions alone.
    karmic_bg = {tr_object(n) for n in KARMIC_POINTS}
    karmic_aspects = [
        f"- {a['active_bg']} {a['type_bg']} {a['passive_bg']}"
        + (f" (орб {a['orb']:.1f}°)" if a.get("orb") is not None else "")
        for a in chart_data.get("aspects", [])
        if a["type"] in {"Conjunction", "Sextile", "Square", "Trine", "Opposition"}
        and (a["active_bg"] in karmic_bg or a["passive_bg"] in karmic_bg)
    ]
    karmic_aspects_txt = "\n".join(karmic_aspects[:18]) or "няма значими аспекти към кармичните точки"

    houses_txt = "\n".join(
        f"- {h['number']}-ти дом започва в {h['sign_bg']} {h['sign_longitude']}"
        for h in chart_data.get("houses", [])
    ) or "няма данни"

    all_positions = "\n".join(
        f"- {o['name_bg']}: {o['sign_bg']} {o['sign_longitude']}, {o['house_bg']}"
        + (" (ретрограден)" if o.get("movement") == "Retrograde" else "")
        for o in chart_data.get("objects", {}).values()
    )

    prompt = f"""Ти си водач при четене на Акашови записи. Работиш съзерцателно: вглеждаш се в кармичните маркери на наталната карта и ги разчиташ като следи от пътя на душата.

Име: {p['name']}
Малко име (обръщай се само с него): {first_name(p['name'])}
Роден: {p['day']}.{p['month']}.{p['year']} в {p['hour']:02d}:{p['minute']:02d}

=== КАРМИЧНИ ТОЧКИ (точно изчислени със Swiss Ephemeris) ===
{line('Северен възел (посока на растеж)', k['points'].get('True North Node'))}
{line('Южен възел (наследено от миналото)', k['points'].get('True South Node'))}
{line('Хирон (раната, която лекува)', k['points'].get('Chiron'))}
{line('Сатурн (уроците и структурата)', k['points'].get('Saturn'))}
{line('Плутон (дълбоката трансформация)', k['points'].get('Pluto'))}
{line('Лилит (потиснатото и автентичното)', k['points'].get('True Lilith'))}

Планети в 12-ти дом (домът на подсъзнанието и наследеното): {twelfth_txt}
Ретроградни планети (енергия, обърната навътре — недовършена работа): {retro_txt}
Лунна фаза при раждане: {k['moon_phase_bg']}
Раждане: {'дневно' if k['diurnal'] else 'нощно'}
Форма на картата: {chart_data.get('shape_bg', 'няма данни')}
Число на съдбата (нумерология): {k['life_path']}

=== АСПЕКТИ КЪМ КАРМИЧНИТЕ ТОЧКИ (по-малък орб = по-силно изразен) ===
{karmic_aspects_txt}

=== ВСИЧКИ ПОЗИЦИИ В КАРТАТА (за контекст) ===
{all_positions}

=== ДОМОВЕ ===
{houses_txt}

=== КАК СЕ ЧЕТАТ АКАШОВИТЕ ЗАПИСИ ===
В тази традиция Акашовите записи се разбират като поле на паметта на душата. Не се "четат" като книга с факти, а се съзерцават чрез символите, които душата е оставила в наталната карта. Ключовите ориентири са:
- Южният възел — какво душата вече владее до втръсване; зоната на комфорт, която в този живот вече не храни.
- Северният възел — посоката, която отначало е неудобна, но носи израстване; обратният полюс на Южния.
- Осите на възлите през домовете — двойката области от живота, между които се люлее развитието.
- Хирон — раната, която не се лекува докрай, но точно затова прави човека способен да лекува същото у другите.
- Сатурн — къде животът поставя условия, забавя и изисква зрялост; уроците, които се повтарят, докато не бъдат научени.
- Плутон — където се случват необратимите смъртта-и-прераждане процеси на личността.
- Лилит — това, което е било потискано и иска да бъде върнато без срам.
- 12-ти дом — колективното, наследеното, неосъзнатото; всичко, което действа зад кулисите.
- Ретроградните планети — енергии, които се проявяват навътре, преди да могат навън; често усещане за "недовършено".

=== ЗАДАЧА ===
Напиши задълбочено четене на Акашовите записи в следната структура:

1. **Отваряне на записа** — 2-3 изречения въведение: настройка към момента, спокойно и с уважение. Без театралност.
2. **Какво носи душата от преди** — Южният възел, 12-ти дом и ретроградните планети: какви модели, дарби и навици идват като наследство. Обвържи ги конкретно с изброените позиции и обясни защо точно този знак и дом дават този модел.
3. **Раната, която се лекува** — Хирон: къде е болката, откъде идва, как се проявява в ежедневието и как точно се превръща в дарба за другите. Ползвай и аспектите към Хирон, ако има такива.
4. **Договорът на този живот** — Северният възел и Сатурн: към какво се движи душата, каква е задачата ѝ, какви са условията на израстването и какво се иска да бъде оставено зад гърба.
5. **Силата на трансформацията** — Плутон и Лилит: къде живее най-дълбоката промяна, какво е било потиснато и какво иска да бъде върнато.
6. **Оста на развитието** — двойката домове на лунните възли: между кои две области от живота се движи растежът и как изглежда балансът между тях.
7. **Кармичните възли** — 3-4 повтарящи се теми, които вероятно се връщат в живота, докато не бъдат осъзнати. За всяка посочи от коя точка в картата произтича.
8. **Какво иска душата да чуе сега** — 4-5 конкретни насоки за освобождаване и движение напред.
9. **Затваряне на записа** — 2-3 изречения спокойно обобщение.

ВАЖНО ЗА ТОНА:
- Пиши поетично и съзерцателно, с образи и метафори, но БЕЗ да твърдиш конкретни факти за минали животи (не измисляй имена, епохи, държави, професии или събития). Говори за модели, теми и енергии — не за биографии.
- Всяко твърдение трябва да стъпва на изброените по-горе точки — читателят да вижда връзката с реалната карта.
- Не плаши и не предсказвай нещастия. Кармата тук е урок, не наказание.
- Бъди щедър в дължината: това е основният текст на раздела, разгърни всяка секция пълноценно.
""" + STYLE_RULES

    ai_key, provider = get_ai_config()
    if ai_key:
        try:
            interpretation = call_ai(ai_key, provider, prompt, max_tokens=7000, model=PAID_MODEL)
            set_ai_cache(person_id, cache_key, interpretation)
            return {"interpretation": interpretation, "cached": False, "cache_key": cache_key}
        except AIError as e:
            return {"interpretation": ai_failure_message(e)}
        except Exception as e:
            return {"interpretation": ai_failure_message(e)}

    return {"interpretation": AI_UNAVAILABLE}

@app.get("/api/persons/{person_id}/numerology")
def api_numerology(person_id: int, user: Tuple[int, str] = Depends(require_feature("numerology"))):
    """Compute the Pythagorean numerology profile for a person (deterministic, no AI)."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")
    return compute_numerology(p["name"], p["year"], p["month"], p["day"])

@app.get("/api/persons/{person_id}/numerology/interpretation")
def api_numerology_interpretation(person_id: int, refresh: bool = False, user: Tuple[int, str] = Depends(require_feature("numerology"))):
    """Generate AI interpretation of a person's numerology profile. Cached per year — pass ?refresh=true to regenerate."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")

    current_year = datetime.date.today().year
    cache_key = f"numerology:{current_year}"
    if not refresh:
        cached = get_ai_cache(person_id, cache_key)
        if cached:
            return {"interpretation": cached["content"], "cached": True,
                    "generated_at": cached["generated_at"], "cache_key": cache_key}

    profile = compute_numerology(p["name"], p["year"], p["month"], p["day"])

    prompt = f"""Ти си професионален нумеролог. Интерпретирай СТРИКТНО следния питагоров нумерологичен профил, изчислен математически от името и датата на раждане. Не измисляй и не променяй числата — те са точен резултат от изчислението. Обясни само какво ОЗНАЧАВАТ.

Име: {p['name']}
Малко име (обръщай се само с него): {first_name(p['name'])}
Дата на раждане: {p['day']}.{p['month']}.{p['year']}

Число на съдбата (Life Path): {profile['life_path']['number']}
Число на изразяването (от пълното име): {profile['expression']['number']}
Число на душевния копнеж (гласни от името): {profile['soul_urge']['number']}
Число на личността (съгласни от името): {profile['personality']['number']}
Число на рождения ден: {profile['birthday']['number']}
Лично число за {profile['personal_year']['year']} година: {profile['personal_year']['number']}

Моля, направи пълна интерпретация със следните секции:
1. **Число на съдбата** — основен жизнен път и цел
2. **Число на изразяването** — таланти и как се проявяват навън
3. **Душевен копнеж** — вътрешни желания и мотивация
4. **Личност** — как те възприемат другите
5. **Лична година** — на какво да наблегнеш тази година
6. **Как числата си взаимодействат** — хармония или напрежение между тях
""" + STYLE_RULES

    ai_key, provider = get_ai_config()
    if ai_key:
        try:
            interpretation = call_ai(ai_key, provider, prompt, max_tokens=6000, model=PAID_MODEL)
            set_ai_cache(person_id, cache_key, interpretation)
            return {"interpretation": interpretation, "cached": False, "cache_key": cache_key}
        except AIError as e:
            return {"interpretation": ai_failure_message(e)}
        except Exception as e:
            return {"interpretation": ai_failure_message(e)}

    return {"interpretation": AI_UNAVAILABLE}


LOVE_POINTS = ("Sun", "Moon", "Venus", "Mars", "Asc")

@app.get("/api/lunar-calendar")
def api_lunar_calendar(year: Optional[int] = None, month: Optional[int] = None,
                       user: Tuple[int, str] = Depends(require_feature("moon"))):
    """Moon phase and sign for every day of a month, with what each favours.

    Computed from ephemeris data, so it holds for any month, past or future.
    """
    tz = ZoneInfo("Europe/Sofia")
    today = datetime.datetime.now(tz).date()
    year = year or today.year
    month = month or today.month
    if not (1 <= month <= 12):
        raise HTTPException(400, "Невалиден месец.")
    if not (1900 <= year <= 2100):
        raise HTTPException(400, "Невалидна година.")

    import calendar as _cal
    days_in_month = _cal.monthrange(year, month)[1]

    # Sofia is used as the reference location; the Moon's sign barely moves
    # across European longitudes, and the phase does not depend on place at all.
    lat, lon = 42.6977, 23.3219

    days = []
    prev_phase = None
    for day in range(1, days_in_month + 1):
        dt = datetime.datetime(year, month, day, 12, 0, tzinfo=tz)
        chart = charts.Natal(charts.Subject(dt, lat, lon))
        phase = chart.moon_phase.formatted if getattr(chart, "moon_phase", None) else None
        moon = next((o for o in chart.objects.values() if o.name == "Moon"), None)
        sign = moon.sign.name if moon else None
        advice = moon_phase_advice(phase) or {}
        days.append({
            "date": f"{year}-{month:02d}-{day:02d}",
            "day": day,
            "weekday": dt.weekday(),
            "is_today": dt.date() == today,
            "phase": phase,
            "phase_bg": tr_moon_phase(phase),
            "phase_changed": phase != prev_phase,
            "phase_meaning": meaning_moon_phase(phase),
            "moon_sign": sign,
            "moon_sign_bg": tr_sign(sign),
            "moon_symbol": sign_symbol(sign),
            "moon_sign_advice": moon_sign_advice(sign),
            "do": advice.get("do", []),
            "avoid": advice.get("avoid", []),
            "note": advice.get("note", ""),
        })
        prev_phase = phase

    return {"year": year, "month": month, "days": days}

@app.get("/api/zodiac-signs")
def api_zodiac_signs(user: Tuple[int, str] = Depends(get_current_user)):
    """The twelve signs, for the partner picker."""
    return {"signs": [
        {"key": s, "name_bg": tr_sign(s), "symbol": sign_symbol(s),
         "element_bg": ELEMENTS_BG.get(sign_element(s)),
         "modality_bg": MODALITIES_BG.get(sign_modality(s))}
        for s in ZODIAC_ORDER
    ]}

def build_love_match(person: dict, partner_sign: str) -> dict:
    """Compare the person's love-relevant placements against a partner's sun sign.

    Only the partner's sign is known here — no birth time — so this compares
    sign to sign rather than computing a full synastry chart.
    """
    chart_data = compute_natal(person)
    by_name = {o["name"]: o for o in chart_data["objects"].values()}

    pairs = []
    labels = {
        "Sun": "Слънце (същност)",
        "Moon": "Луна (емоции)",
        "Venus": "Венера (любов)",
        "Mars": "Марс (страст)",
        "Asc": "Асцендент (първо впечатление)",
    }
    for name in LOVE_POINTS:
        o = by_name.get(name)
        if not o:
            continue
        asp = sign_aspect(o["sign"], partner_sign)
        pairs.append({
            "label": labels[name],
            "name_bg": o["name_bg"],
            "sign_bg": o["sign_bg"],
            "sign_symbol": o["sign_symbol"],
            "aspect": asp[0] if asp else None,
            "aspect_meaning": asp[1] if asp else "",
        })

    sun = by_name.get("Sun")
    venus = by_name.get("Venus")
    sun_sign = sun["sign"] if sun else None

    el_a, el_b = sign_element(sun_sign), sign_element(partner_sign)
    mo_a, mo_b = sign_modality(sun_sign), sign_modality(partner_sign)

    return {
        "partner": {
            "sign": partner_sign,
            "sign_bg": tr_sign(partner_sign),
            "symbol": sign_symbol(partner_sign),
            "element_bg": ELEMENTS_BG.get(el_b),
            "modality_bg": MODALITIES_BG.get(mo_b),
            "sign_meaning": meaning_sign(partner_sign),
        },
        "you": {
            "sun_bg": tr_sign(sun_sign) if sun_sign else None,
            "sun_symbol": sign_symbol(sun_sign) if sun_sign else None,
            "venus_bg": tr_sign(venus["sign"]) if venus else None,
            "venus_symbol": sign_symbol(venus["sign"]) if venus else None,
            "element_bg": ELEMENTS_BG.get(el_a),
            "modality_bg": MODALITIES_BG.get(mo_a),
        },
        "sun_aspect": (lambda a: {"name": a[0], "meaning": a[1]} if a else None)(
            sign_aspect(sun_sign, partner_sign) if sun_sign else None),
        "venus_aspect": (lambda a: {"name": a[0], "meaning": a[1]} if a else None)(
            sign_aspect(venus["sign"], partner_sign) if venus else None),
        "elements": element_pair_meaning(el_a, el_b),
        "modalities": modality_pair_meaning(mo_a, mo_b),
        "points": pairs,
    }

def build_love_match_full(person: dict, partner: dict) -> dict:
    """Compatibility when the partner's full birth data is known.

    Compares the two charts placement by placement and reports the real
    cross-aspects between their love-relevant points, not just sign to sign.
    """
    my_chart = compute_natal(person)
    their_chart = compute_natal(partner)
    mine = {o["name"]: o for o in my_chart["objects"].values()}
    theirs = {o["name"]: o for o in their_chart["objects"].values()}

    labels = {
        "Sun": "Слънце (същност)",
        "Moon": "Луна (емоции)",
        "Venus": "Венера (любов)",
        "Mars": "Марс (страст)",
        "Asc": "Асцендент (първо впечатление)",
    }

    def deg(obj):
        """Absolute ecliptic longitude, parsed from the formatted value."""
        try:
            parts = obj["longitude"].replace("°", " ").replace("'", " ").replace('"', " ").split()
            return float(parts[0]) + float(parts[1]) / 60 + float(parts[2]) / 3600
        except Exception:
            return None

    # Cross-aspects: every love point of one chart against every love point of the other.
    orbs = {0: ("Съвпад", 8), 60: ("Секстил", 5), 90: ("Квадрат", 6),
            120: ("Тригон", 7), 180: ("Опозиция", 8)}
    cross = []
    for a_name in LOVE_POINTS:
        a = mine.get(a_name)
        if not a:
            continue
        a_deg = deg(a)
        if a_deg is None:
            continue
        for b_name in LOVE_POINTS:
            b = theirs.get(b_name)
            if not b:
                continue
            b_deg = deg(b)
            if b_deg is None:
                continue
            sep = abs(a_deg - b_deg) % 360
            if sep > 180:
                sep = 360 - sep
            for angle, (asp_bg, max_orb) in orbs.items():
                orb = abs(sep - angle)
                if orb <= max_orb:
                    cross.append({
                        "mine_bg": a["name_bg"], "mine_sign_bg": a["sign_bg"],
                        "mine_symbol": a["sign_symbol"],
                        "theirs_bg": b["name_bg"], "theirs_sign_bg": b["sign_bg"],
                        "theirs_symbol": b["sign_symbol"],
                        "aspect": asp_bg, "orb": round(orb, 1),
                        "meaning": meaning_aspect(
                            {"Съвпад": "Conjunction", "Секстил": "Sextile", "Квадрат": "Square",
                             "Тригон": "Trine", "Опозиция": "Opposition"}[asp_bg]),
                    })
                    break
    cross.sort(key=lambda c: c["orb"])

    my_sun = mine.get("Sun")
    their_sun = theirs.get("Sun")
    my_venus, their_venus = mine.get("Venus"), theirs.get("Venus")
    el_a = sign_element(my_sun["sign"]) if my_sun else None
    el_b = sign_element(their_sun["sign"]) if their_sun else None
    mo_a = sign_modality(my_sun["sign"]) if my_sun else None
    mo_b = sign_modality(their_sun["sign"]) if their_sun else None

    return {
        "mode": "full",
        "partner": {
            "name": partner["name"],
            "sign_bg": their_sun["sign_bg"] if their_sun else None,
            "symbol": their_sun["sign_symbol"] if their_sun else "✦",
            "moon_bg": theirs["Moon"]["sign_bg"] if theirs.get("Moon") else None,
            "venus_bg": their_venus["sign_bg"] if their_venus else None,
            "asc_bg": theirs["Asc"]["sign_bg"] if theirs.get("Asc") else None,
            "element_bg": ELEMENTS_BG.get(el_b),
            "modality_bg": MODALITIES_BG.get(mo_b),
        },
        "you": {
            "sun_bg": my_sun["sign_bg"] if my_sun else None,
            "sun_symbol": my_sun["sign_symbol"] if my_sun else "✦",
            "venus_bg": my_venus["sign_bg"] if my_venus else None,
            "venus_symbol": my_venus["sign_symbol"] if my_venus else "✦",
            "element_bg": ELEMENTS_BG.get(el_a),
            "modality_bg": MODALITIES_BG.get(mo_a),
        },
        "sun_aspect": (lambda a: {"name": a[0], "meaning": a[1]} if a else None)(
            sign_aspect(my_sun["sign"], their_sun["sign"]) if my_sun and their_sun else None),
        "elements": element_pair_meaning(el_a, el_b),
        "modalities": modality_pair_meaning(mo_a, mo_b),
        "cross_aspects": cross[:14],
        "partner_points": [
            {"label": labels[n], "name_bg": theirs[n]["name_bg"],
             "sign_bg": theirs[n]["sign_bg"], "sign_symbol": theirs[n]["sign_symbol"],
             "house_bg": theirs[n]["house_bg"]}
            for n in LOVE_POINTS if theirs.get(n)
        ],
    }

def resolve_love_match(data: "LoveMatchRequest", person: dict) -> dict:
    """Pick full-chart or sign-only compatibility based on what was supplied."""
    if data.has_full_chart():
        return build_love_match_full(person, data.as_person())
    if data.partner_sign not in ZODIAC_ORDER:
        raise HTTPException(400, "Изберете зодия или въведете пълни данни за партньора.")
    m = build_love_match(person, data.partner_sign)
    m["mode"] = "sign"
    return m

@app.post("/api/love-match")
def api_love_match(data: LoveMatchRequest, user: Tuple[int, str] = Depends(require_feature("love"))):
    """Love compatibility — full charts when birth data is given, otherwise sign to sign."""
    user_id, email = user
    p = get_person(data.person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")
    return resolve_love_match(data, p)

@app.post("/api/love-match/interpretation")
def api_love_match_interpretation(data: LoveMatchRequest, refresh: bool = False,
                                  user: Tuple[int, str] = Depends(require_feature("love"))):
    """AI love reading — uses the partner's full chart when available."""
    user_id, email = user
    p = get_person(data.person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")

    full = data.has_full_chart()
    if full:
        cache_key = (f"love-full:{data.partner_year}-{data.partner_month}-{data.partner_day}"
                     f"-{data.partner_hour}-{data.partner_minute}"
                     f"-{round(data.partner_lat or 0, 3)}-{round(data.partner_lon or 0, 3)}")
    else:
        if data.partner_sign not in ZODIAC_ORDER:
            raise HTTPException(400, "Изберете зодия или въведете пълни данни за партньора.")
        cache_key = f"love:{data.partner_sign}"

    if not refresh:
        cached = get_ai_cache(data.person_id, cache_key)
        if cached:
            return {"interpretation": cached["content"], "cached": True,
                    "generated_at": cached["generated_at"], "cache_key": cache_key}

    m = resolve_love_match(data, p)

    if full:
        partner_txt = "\n".join(
            f"- {pt['label']}: {pt['name_bg']} в {pt['sign_bg']}, {pt['house_bg']}"
            for pt in m["partner_points"]
        )
        cross_txt = "\n".join(
            f"- твоят {c['mine_bg']} ({c['mine_sign_bg']}) {c['aspect']} неговата/нейната "
            f"{c['theirs_bg']} ({c['theirs_sign_bg']}), орб {c['orb']}° — {c['meaning']}"
            for c in m["cross_aspects"]
        ) or "няма аспекти в рамките на орба"
        context = f"""=== ТВОЯТА КАРТА (точно изчислена) ===
Слънце: {m['you']['sun_bg']} · Венера: {m['you']['venus_bg']}
Стихия: {m['you']['element_bg']} · Качество: {m['you']['modality_bg']}

=== КАРТАТА НА ПАРТНЬОРА ({m['partner']['name']}) — точно изчислена ===
{partner_txt}
Стихия: {m['partner']['element_bg']} · Качество: {m['partner']['modality_bg']}

=== РЕАЛНИ АСПЕКТИ МЕЖДУ ДВЕТЕ КАРТИ (по-малък орб = по-силен) ===
{cross_txt}

Стихии: {m['elements']}
Качества: {m['modalities']}

Имаш пълните рождени данни и на двамата, затова говори конкретно за техните карти — не общо за зодиите."""
    else:
        points_txt = "\n".join(
            f"- {pt['label']}: твоят {pt['name_bg']} е в {pt['sign_bg']} → {pt['aspect']} спрямо {m['partner']['sign_bg']}"
            f" ({pt['aspect_meaning']})"
            for pt in m["points"] if pt["aspect"]
        )
        context = f"""=== ТВОЯТА КАРТА (точно изчислена) ===
Слънце: {m['you']['sun_bg']} · Венера: {m['you']['venus_bg']}
Стихия: {m['you']['element_bg']} · Качество: {m['you']['modality_bg']}

=== ПАРТНЬОРЪТ ===
Зодия: {m['partner']['sign_bg']}
Стихия: {m['partner']['element_bg']} · Качество: {m['partner']['modality_bg']}
Характер на знака: {m['partner']['sign_meaning']}

=== АСПЕКТИ МЕЖДУ ЗНАЦИТЕ ===
{points_txt or "няма изчислени аспекти"}

Стихии: {m['elements']}
Качества: {m['modalities']}

ВАЖНО: знаем само зодията на партньора, не и точния му час на раждане. Затова говори за тенденции на ниво знак, а не за неговата пълна карта. Ако някъде е нужно повече, кажи честно, че за по-точен прочит трябват и неговите час и място на раждане."""

    prompt = f"""Ти си професионален астролог, специализиран в отношения. Направи ЛЮБОВЕН ХОРОСКОП — анализ на съвместимостта между двама души.

Малко име (обръщай се само с него): {first_name(p['name'])}

{context}

=== ЗАДАЧА ===
Напиши любовен хороскоп в следната структура:

1. **Общата картина** — каква е динамиката между вас в две-три изречения.
2. **Какво ви свързва** — 3-4 конкретни неща, изведени от аспектите и стихиите по-горе. За всяко посочи от какво произтича.
3. **Къде ще има търкания** — 3-4 честни точки на напрежение и защо се появяват.
4. **Как да го подхождаш** — 4-5 конкретни съвета какво ДА правиш с този партньор: как да общуваш, какво го печели, кога да отстъпиш.
5. **С какво да внимаваш** — 3-4 неща, които е добре да избягваш в тази връзка, с обяснение защо точно тук са рискови.
6. **Емоционална съвместимост** — Луната и Венера: как се разбирате на ниво чувства и нежност.
7. **Страст и привличане** — Марс и Слънце: каква е химията между вас.
8. **Дългосрочен потенциал** — какво е нужно, за да проработи в дългосрочен план.
9. **Едно изречение накрая** — есенцията на тази двойка.

Бъди честен: ако комбинацията е трудна, кажи го, но покажи и как се работи с нея. Не превръщай всичко в розово.
""" + STYLE_RULES

    ai_key, provider = get_ai_config()
    if ai_key:
        try:
            interpretation = call_ai(ai_key, provider, prompt, max_tokens=6000, model=PAID_MODEL)
            set_ai_cache(data.person_id, cache_key, interpretation)
            return {"interpretation": interpretation, "cached": False, "cache_key": cache_key}
        except AIError as e:
            return {"interpretation": ai_failure_message(e)}
        except Exception as e:
            return {"interpretation": ai_failure_message(e)}

    return {"interpretation": AI_UNAVAILABLE}


@app.post("/api/synastry")
def api_synastry(data: SynastryRequest, user: Tuple[int, str] = Depends(require_feature("love"))):
    """Compute synastry (composite) chart between two persons."""
    user_id, email = user
    p1 = get_person(data.person1_id, user_id)
    if not p1:
        raise HTTPException(404, f"Person 1 (id={data.person1_id}) not found")
    p2 = get_person(data.person2_id, user_id)
    if not p2:
        raise HTTPException(404, f"Person 2 (id={data.person2_id}) not found")
    return compute_composite(p1, p2)


@app.post("/api/synastry/interpretation")
def api_synastry_interpretation(data: SynastryRequest, refresh: bool = False, user: Tuple[int, str] = Depends(require_feature("love"))):
    """Generate an AI interpretation of synastry between two persons."""
    user_id, email = user
    p1 = get_person(data.person1_id, user_id)
    if not p1:
        raise HTTPException(404, f"Person 1 (id={data.person1_id}) not found")
    p2 = get_person(data.person2_id, user_id)
    if not p2:
        raise HTTPException(404, f"Person 2 (id={data.person2_id}) not found")

    # Cache key: sort IDs to be order-independent
    cache_key = f"synastry:{min(data.person1_id, data.person2_id)}:{max(data.person1_id, data.person2_id)}"
    person_id = data.person1_id  # arbitrary, for cache table FK

    if not refresh:
        cached = get_ai_cache(person_id, cache_key)
        if cached:
            return {"interpretation": cached["content"], "cached": True}

    # Compute the composite chart
    composite = compute_composite(p1, p2)

    # Build prompt
    planets1 = []
    planets2 = []
    for oid, obj in composite["objects"].items():
        name = obj.get("name_bg", obj.get("name", ""))
        s = f"{name} в {obj.get('sign_bg', obj.get('sign', ''))} ({obj.get('sign_longitude', '')})"
        planets1.append(s)
        planets2.append(s)

    aspects_text = []
    for a in composite.get("aspects", []):
        aspects_text.append(f"{a.get('active_bg', a.get('active', ''))} {a.get('type_bg', a.get('type', ''))} {a.get('passive_bg', a.get('passive', ''))}")

    prompt = f"""Ти си професионален астролог. Направи интерпретация на съвместимостта между двама души на български език.

ПЪРВИ ЧОВЕК:
Малко име (използвай само него): {first_name(p1['name'])}
Дата на раждане: {p1['year']}-{p1['month']:02d}-{p1['day']:02d} {p1['hour']:02d}:{p1['minute']:02d}

ВТОРИ ЧОВЕК:
Малко име (използвай само него): {first_name(p2['name'])}
Дата на раждане: {p2['year']}-{p2['month']:02d}-{p2['day']:02d} {p2['hour']:02d}:{p2['minute']:02d}

Форма на съвместимостта: {composite.get('shape_bg', composite.get('shape', 'N/A'))}
Лунна фаза: {composite.get('moon_phase_bg', composite.get('moon_phase', 'N/A'))}

Основни аспекти между тях:
{chr(10).join(aspects_text) if aspects_text else "Няма данни"}

Моля, направи пълна интерпретация включваща:
1. **Обща характеристика на връзката** — каква е динамиката между двамата
2. **Емоционална съвместимост** — как се разбират на чувствено ниво
3. **Комуникация и интелектуална връзка** — как общуват и мислят заедно
4. **Силни страни на връзката** — какво ги сближава и прави добър екип
5. **Предизвикателства** — къде може да има търкания и как да ги преодолеят
6. **Романтична и физическа химия**
7. **Дългосрочен потенциал** — какво показват аспектите за бъдещето им

Обърни се директно към тях (използвай имената им).
""" + STYLE_RULES

    ai_key, provider = get_ai_config()
    if ai_key:
        try:
            # Любовният хороскоп има 7 секции — 3000 токена често не стигаха
            # и текстът спираше по средата. Повече място = по-малко продължения.
            interpretation = call_ai(ai_key, provider, prompt, max_tokens=6000, model=PAID_MODEL)
            set_ai_cache(person_id, cache_key, interpretation)
            return {"interpretation": interpretation, "cached": False, "cache_key": cache_key}
        except AIError as e:
            return {"interpretation": ai_failure_message(e)}
        except Exception as e:
            return {"interpretation": ai_failure_message(e)}

    return {"interpretation": AI_UNAVAILABLE}

@app.post("/api/transits")
def api_transits(data: TransitsRequest, user: Tuple[int, str] = Depends(require_feature("horoscope"))):
    """Compute transits for a person at a given target date."""
    user_id, email = user
    p = get_person(data.person_id, user_id)
    if not p:
        raise HTTPException(404, f"Person (id={data.person_id}) not found")
    try:
        target_date = datetime.datetime.fromisoformat(data.target_date)
        # Attach person's timezone to naive datetime
        tz_name = p.get("timezone", "Europe/Sofia")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("Europe/Sofia")
        if target_date.tzinfo is None:
            target_date = target_date.replace(tzinfo=tz)
    except ValueError:
        raise HTTPException(400, "Невалидна дата. Очакваният формат е ГГГГ-ММ-ДД или ГГГГ-ММ-ДДTЧЧ:ММ:СС.")
    return compute_transits(p, target_date)

# --- Background AI generation (спира 504 таймаутите при дълги разчитания) ---
# Дългите AI разчитания (дневен хороскоп и др.) отнемат 30–90 сек. Ако се правят
# синхронно в HTTP заявката, проксито (Cloudflare ~100s) връща 504 Gateway Timeout.
# Затова генерирането става в background нишка: ендпойнтът връща {pending:true}
# веднага, а фронтенда poll-ва докато резултатът се запише в кеша.
_AI_JOBS = {}          # cache_key -> {"done": threading.Event, "error": str|None}
_AI_JOBS_LOCK = threading.Lock()

def ai_job(cache_key: str, fn):
    """Стартира fn() в background нишка (ако вече не тече). Връща job dict."""
    with _AI_JOBS_LOCK:
        job = _AI_JOBS.get(cache_key)
        if job and not job["done"].is_set():
            return job
        job = {"done": threading.Event(), "error": None}
        _AI_JOBS[cache_key] = job
    def _run():
        try:
            fn()
        except Exception as e:
            job["error"] = str(e)
        finally:
            job["done"].set()
    threading.Thread(target=_run, daemon=True).start()
    return job

@app.get("/api/persons/{person_id}/daily-horoscope")
def api_daily_horoscope(person_id: int, refresh: bool = False, user: Tuple[int, str] = Depends(require_feature("horoscope"))):
    """Generate an AI-written interpretation of today's transits to the person's natal chart.
    Cached per calendar day — pass ?refresh=true to force a new generation for today."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")

    tz_name = p.get("timezone", "Europe/Sofia")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Sofia")
    now = datetime.datetime.now(tz)
    cache_key = f"horoscope:{now.date().isoformat()}"
    date_bg = now.strftime("%d.%m.%Y")

    if not refresh:
        # Ако генериране вече тече (напр. от „Разчети наново"), изчакай го,
        # вместо да връщаш стария кеш.
        with _AI_JOBS_LOCK:
            running = _AI_JOBS.get(cache_key)
        if running and not running["done"].is_set():
            return {"pending": True, "date": date_bg, "cache_key": cache_key}
        cached = get_ai_cache(person_id, cache_key)
        if cached:
            summary, body = split_summary(cached["content"])
            return {"interpretation": body, "summary": summary,
                    "date": date_bg, "cached": True, "cache_key": cache_key}
        # Няма кеш, а предишен опит е завършил с грешка — покажи я, не рестартирай.
        if running and running["done"].is_set() and running["error"]:
            return {"interpretation": AI_UNAVAILABLE, "date": date_bg}

    def _generate():
        transit_data = compute_transits(p, now)

        ranked = rank_transit_aspects(
            transit_data.get("transit_aspects_to_natal", []), limit=10)
        aspects_block = format_transit_aspects(ranked)

        prompt = f"""Ти си професионален астролог. Направи ДНЕВЕН ХОРОСКОП за {date_bg} за конкретния човек, СТРИКТНО базиран на точните транзитни данни по-долу (изчислени астрономически със Swiss Ephemeris). Не измисляй позиции или аспекти извън изброените — обясни само какво ОЗНАЧАВАТ.

Име: {p['name']}
Малко име (обръщай се само с него): {first_name(p['name'])}
Дата на анализа: {date_bg}

=== ФОН НА ДЕНЯ ===
Форма на транзитната карта: {transit_data.get('shape', 'N/A')}
Лунна фаза днес: {transit_data.get('moon_phase', 'N/A')}

=== АКТИВНИ ТРАНЗИТНИ АСПЕКТИ КЪМ НАТАЛНАТА КАРТА ===
Подредени са по сила — първите тежат най-много днес. Стъпи основно на
силните и умерените; слабите спомени само ако допълват картината.
{aspects_block}

=== ЗАДАЧА ===
Отговорът ти се състои от ДВЕ части, в този ред.

ЧАСТ 1 — резюме за карти. Започни отговора си с JSON блок между маркерите ---SUMMARY--- и ---END--- точно в този формат (без допълнителен текст в блока):
---SUMMARY---
{{"mood": "една дума за настроението на деня (напр. Съсредоточен, Емоционален, Динамичен)",
"energy": "Висока|Средна|Ниска",
"do": ["3 кратки неща за правене, по 2-4 думи всяко"],
"avoid": ["2-3 кратки неща за избягване, по 2-4 думи всяко"],
"focus": "една дума/кратка фраза за фокуса на деня",
"caution": "едно кратко изречение в какво да внимава"}}
---END---

ЧАСТ 2 — разгърнатият текст, веднага след ---END---, в следната структура. Използвай точно тези заглавия, номерирани:

1. **Общо усещане за деня** — 2-3 изречения обобщение на енергията на деня.
2. **Разчитане на аспектите** — разгърни силните и умерените аспекти по един по един: какво конкретно носи всеки. Слабите обедини в едно-две изречения накрая или ги пропусни, ако не добавят нищо. По-добре три обяснени задълбочено, отколкото десет изброени повърхностно. Обяснявай термините накратко (напр. "квадрат — напрежение, което подтиква към действие").
3. **Благоприятно е за** — 3-5 конкретни неща, за които днешните аспекти дават попътен вятър (напр. разговори, преговори, творчество, почивка, финансови решения, физическа активност, срещи). За всяко посочи кой аспект го подкрепя.
4. **Не е благоприятно за** — 3-4 неща, които по-добре да се отложат днес, и защо според аспектите.
5. **Какво да направиш днес** — 3-4 конкретни, изпълними действия (не общи фрази — реални неща, които човек може да свърши днес).
6. **Какво да избягваш** — 2-3 конкретни поведения или решения, които днешните транзити правят рискови.
7. **В какво да внимаваш** — 2-3 предупреждения: къде е рискът от недоразумение, прибързаност, преумора или конфликт, според напрегнатите аспекти (квадрати, опозиции).
8. **Емоции и настроение** — базирано на транзитите към Луната и личните планети.
9. **Есенцията на деня** — 1-2 изречения обобщение.

=== КАК ДА ПИШЕШ ===
- Пиши на български, топло и практично, все едно говориш директно на човека.
- ФОРМАТ: всяко от деветте заглавия започва на нов ред във вида `1. **Заглавие**`. Под него — текст на отделни редове. Където изброяваш неща, ползвай тирета (`- нещо`), едно на ред. Не слепвай изброявания в един дълъг абзац.
- СТРУКТУРА: всяка секция да е самостоятелна и завършена. Не повтаряй едно и също през различните секции — ако вече си обяснил аспект в секция 2, в следващите само се позовавай на него накратко.
- ЛОГИКА: върви от общото към конкретното. Секции 3-7 трябва да следват пряко от аспектите, обяснени в секция 2 — читателят да вижда връзката "този аспект → затова този съвет".
- ДЪЛЖИНА: бъди подробен. Всяка секция с по няколко изречения реално съдържание, а изброяванията с кратко обяснение защо, не само голи думи.
- Бъди конкретен — избягвай клишета от типа "бъди позитивен". Ако някой аспект е слаб или неутрален, кажи го честно.
- Основавай се единствено на изброените по-горе аспекти, без да добавяш измислени детайли."""

        ai_key, provider = get_ai_config()
        if not ai_key:
            return  # няма ключ — кешът остава празен, следващият poll ще върне грешка
        raw = call_ai(ai_key, provider, prompt, max_tokens=6000, model=PAID_MODEL)
        set_ai_cache(person_id, cache_key, raw)

    job = ai_job(cache_key, _generate)
    if job["done"].is_set():
        # Генерирането е приключило още преди да се върне този отговор.
        cached = get_ai_cache(person_id, cache_key)
        if cached:
            summary, body = split_summary(cached["content"])
            return {"interpretation": body, "summary": summary,
                    "date": date_bg, "cached": False, "cache_key": cache_key}
        return {"interpretation": AI_UNAVAILABLE, "date": date_bg}
    return {"pending": True, "date": date_bg, "cache_key": cache_key}

MAJOR_ASPECT_TYPES = {"Conjunction", "Sextile", "Square", "Trine", "Opposition"}
# Fast-moving transit bodies (Moon, and daily-recalculated angles like Asc/MC) create
# a new "aspect" almost every day, drowning out the slower, more meaningful transits.
# The period view only tracks transiting bodies from Mercury outward.
PERIOD_TRANSIT_BODIES = {
    "Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune", "Pluto", "Chiron",
}

@app.post("/api/period-influence")
def api_period_influence(data: PeriodRequest, user: Tuple[int, str] = Depends(require_feature("period"))):
    """Scan a date range day-by-day and report only days where a major transit
    aspect to the natal chart newly forms or dissolves (changes vs. the previous day)."""
    user_id, email = user
    p = get_person(data.person_id, user_id)
    if not p:
        raise HTTPException(404, f"Person (id={data.person_id}) not found")

    try:
        start = datetime.date.fromisoformat(data.start_date)
        end = datetime.date.fromisoformat(data.end_date)
    except ValueError:
        raise HTTPException(400, "Невалидна дата. Очакваният формат е ГГГГ-ММ-ДД.")

    if start > end:
        raise HTTPException(400, "Началната дата трябва да е преди крайната.")
    if (end - start).days > 62:
        raise HTTPException(400, "Периодът е твърде дълъг. Максимумът е 62 дни — раздели го на части.")

    tz_name = p.get("timezone", "Europe/Sofia")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Sofia")

    native = make_subject(p)
    natal = charts.Natal(native)

    def active_pairs(day: datetime.date) -> dict:
        dt = datetime.datetime(day.year, day.month, day.day, 12, 0, tzinfo=tz)
        target_subject = charts.Subject(dt, p["lat"], p["lon"])
        transit_chart = charts.Natal(target_subject, aspects_to=natal)
        aspects = serialize_aspects(transit_chart.aspects)
        pairs = {}
        for a in aspects:
            if a["type"] not in MAJOR_ASPECT_TYPES:
                continue
            if a["active"] not in PERIOD_TRANSIT_BODIES:
                continue
            key = (a["active"], a["type"], a["passive"])
            pairs[key] = a
        return pairs

    days = []
    d = start
    while d <= end:
        days.append(d)
        d += datetime.timedelta(days=1)

    prev_pairs = active_pairs(start - datetime.timedelta(days=1))
    results = []
    for day in days:
        curr_pairs = active_pairs(day)
        entering = [a for key, a in curr_pairs.items() if key not in prev_pairs]
        leaving = [a for key, a in prev_pairs.items() if key not in curr_pairs]
        if entering or leaving:
            results.append({
                "date": day.isoformat(),
                "entering": entering,
                "leaving": leaving,
            })
        prev_pairs = curr_pairs

    return {"start_date": data.start_date, "end_date": data.end_date, "days": results}

@app.post("/api/period-interpretation")
def api_period_interpretation(data: PeriodRequest, refresh: bool = False,
                              user: Tuple[int, str] = Depends(require_feature("period"))):
    """AI reading of a date range's transits. Cached per person + date range."""
    user_id, email = user
    p = get_person(data.person_id, user_id)
    if not p:
        raise HTTPException(404, f"Person (id={data.person_id}) not found")

    cache_key = f"period:{data.start_date}:{data.end_date}"
    if not refresh:
        cached = get_ai_cache(data.person_id, cache_key)
        if cached:
            return {"interpretation": cached["content"], "cached": True,
                    "generated_at": cached["generated_at"], "cache_key": cache_key}

    period = api_period_influence(data, user)
    days = period.get("days", [])

    if not days:
        return {"interpretation": "През избрания период няма настъпващи или отпадащи значими транзити.",
                "cached": False}

    lines = []
    for day in days:
        parts = []
        for a in day.get("entering", []):
            parts.append(f"започва {a['active']} {a['type']} {a['passive']} (натал)")
        for a in day.get("leaving", []):
            parts.append(f"приключва {a['active']} {a['type']} {a['passive']} (натал)")
        lines.append(f"- {day['date']}: " + "; ".join(parts))

    prompt = f"""Ти си професионален астролог. Направи РАЗЧИТАНЕ НА ПЕРИОД за конкретен човек, СТРИКТНО базирано на точните транзитни данни по-долу (изчислени със Swiss Ephemeris). Не измисляй позиции или аспекти извън изброените — обясни какво ОЗНАЧАВАТ.

Име: {p['name']}
Малко име (обръщай се само с него): {first_name(p['name'])}
Период: {data.start_date} до {data.end_date}

=== ТРАНЗИТНИ СЪБИТИЯ ПО ДНИ ===
{chr(10).join(lines)}

=== ЗАДАЧА ===
Напиши свързан, разбираем разказ за периода (НЕ просто списък), в следната структура:

1. **Общ характер на периода** — каква е основната тема и енергия на тези седмици, като цялост.
2. **Ключовите моменти** — 3-5 най-значими дати от списъка и какво конкретно носи всяка (по-бавните планети — Юпитер, Сатурн, Уран, Нептун, Плутон — тежат повече от бързите като Меркурий и Венера; отбележи това).
3. **Възможности** — къде периодът дава отворени врати и какво си струва да се предприеме.
4. **Предизвикателства** — кои дни изискват внимание или търпение и защо.
5. **Практични съвети** — 3-4 конкретни препоръки, изведени пряко от аспектите.
6. **Обобщение** — 2-3 изречения есенция на периода.
""" + STYLE_RULES

    ai_key, provider = get_ai_config()
    if ai_key:
        try:
            interpretation = call_ai(ai_key, provider, prompt, max_tokens=6000)
            set_ai_cache(data.person_id, cache_key, interpretation)
            return {"interpretation": interpretation, "cached": False, "cache_key": cache_key}
        except AIError as e:
            return {"interpretation": ai_failure_message(e)}
        except Exception as e:
            return {"interpretation": ai_failure_message(e)}

    return {"interpretation": AI_UNAVAILABLE}

class AIError(Exception):
    """Raised with a user-facing Bulgarian explanation of what went wrong with an AI call."""
    pass

def _explain_http_error(provider: str, e) -> str:
    import urllib.error
    if not isinstance(e, urllib.error.HTTPError):
        return str(e)
    body = ""
    try:
        body = e.read().decode("utf-8", errors="ignore")
    except Exception:
        pass
    code = e.code
    provider_name = {"openai": "OpenAI", "deepseek": "DeepSeek", "anthropic": "Anthropic"}.get(provider, provider)
    if code == 401:
        return f"{provider_name} отказа ключа (401 Unauthorized) — ключът е невалиден или изтрит."
    if code == 429:
        # Both "no billing/quota" and "too many requests" surface as 429 on most providers.
        hint = "Най-честата причина: акаунтът няма зареден billing/quota (при OpenAI новите ключове изискват добавена платежна карта дори за минимални тестове), или е ударен реален rate limit."
        return f"{provider_name} върна 429 Too Many Requests / изчерпана квота. {hint}"
    if code == 404:
        return f"{provider_name} върна 404 — моделът не е наличен за този ключ/акаунт."
    if code >= 500:
        return f"{provider_name} има временен сървърен проблем ({code}). Опитайте отново след малко."
    return f"{provider_name} върна грешка {code}: {body[:200]}"

# Споделени граматически правила, добавяни към всеки AI prompt — иначе моделът
# често греши по падежи, членуване и съгласуване на български.
BG_GRAMMAR_RULES = """=== ЕЗИКОВИ ПРАВИЛА (задължителни за целия текст) ===
Пиши на граматически безупречен, книжовен български. Провери и коригирай:
1. ЧЛЕНУВАНЕ: пълен член (‑ът/‑ят) за подлог — „денят започва", „планетата е силна"; кратък член (‑а/‑я) за допълнение — „през деня", „виждам промяната".
2. СЪГЛАСУВАНЕ ПО РОД И ЧИСЛО: „напрегнатият аспект", „емоционалната сфера", „скритите напрежения".
3. МЕСТОИМЕННИ ПАДЕЖИ: винителен „го/я/ги/те" и дателен „му/ѝ/им/ти/ми" на правилното място — „аспектът ти дава...", „помага ти", „казва ѝ". Не повтаряй „на него/на нея" там, където е нужна кратката форма.
4. БРОЙНА ФОРМА след числителни: „два дни", „три аспекта", „четири съвета".
5. СЛОВОРЕД: естествен български (подлог–сказуемо–допълнение). Без английски словоред и буквални преводи.
6. ПРЕДЛОЗИ: „в"/„във" — пълната форма „във" се пише САМО пред думи, започващи с „в" или „ф" (във въздуха, във фокуса), иначе винаги „в" (в дома, в знака, в картата). „с"/„със" — пълната форма „със" се пише САМО пред „с" или „з" (със Сатурн, със знанието), иначе „с" (с Луната, с търпение, с хората).
7. ЗАПЕТАИ: не пропускай запетаята пред „който", „която", „което", „които", „че", „но", „а".
8. Избягвай двойно членуване и несъгласувани окончания.
Накрая прочети текста веднъж само за граматика и поправи всяка грешка."""

def call_ai(api_key: str, provider: str, prompt: str, max_tokens: int = 4000,
            model: Optional[str] = None) -> str:
    """Call the configured AI provider's chat completion endpoint and return the text.

    `model` надделява над модела от настройките: платените разчитания подават
    `model=PAID_MODEL` (deepseek-v4-pro), безплатните ползват дефолта (Flash)."""
    import urllib.request
    import urllib.error

    # Граматичните правила се добавят към всяко разчитане, без значение от модела.
    prompt = BG_GRAMMAR_RULES + "\n\n" + prompt

    model = model or resolve_ai_model(provider)
    try:
        if provider == "anthropic":
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps({
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                }).encode(),
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                result = json.loads(resp.read())
                return clean_bg(result["content"][0]["text"])

        if provider == "deepseek":
            url = "https://api.deepseek.com/chat/completions"
            use_thinking_disable = True
        else:
            url = "https://api.openai.com/v1/chat/completions"
            use_thinking_disable = False

        # Ако моделът спре заради лимита на токените (finish_reason == "length"),
        # регенерираме веднъж с двоен лимит. НЕ използваме „продължи оттам“ — то
        # кара модела да повтори началото, вместо да довърши текста.
        content = ""
        for mt in (max_tokens, max_tokens * 2):
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": mt,
            }
            if use_thinking_disable:
                # v4 моделите мислят (reasoning) по подразбиране и харчат max_tokens
                # за скрити разсъждения, вместо за отговора. Изключваме го, за да
                # се върне съдържанието директно, както старият deepseek-chat.
                payload["thinking"] = {"type": "disabled"}
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                result = json.loads(resp.read())
            msg = result["choices"][0]["message"]
            content = msg.get("content") or ""
            # Fallback: ако все пак моделът е мислил и content е празен, вземи
            # разсъждението, за да не се губи генерираният текст.
            if not content and msg.get("reasoning_content"):
                content = msg["reasoning_content"]
            finish = result["choices"][0].get("finish_reason")
            # Единственият надежден признак за отрязване е finish_reason ==
            # "length". Регенерирането е скъпо (двоен лимит = още толкова
            # токени), затова се пуска само за него.
            if finish != "length":
                # Разчитане, което не свършва на препинателен знак, обикновено
                # пак е цяло — завършва с двоеточие, цифра или скоба. Логваме
                # го, за да се види, ако наистина зачести, но не плащаме втора
                # генерация заради това.
                last = content.strip()[-1:] if content.strip() else ""
                if last and last not in ".!?…»\"”)":
                    log.info("AI отговорът завършва на %r (finish=%s) — приемаме го.",
                             last, finish)
                break
        return clean_bg(content)
    except urllib.error.HTTPError as e:
        raise AIError(_explain_http_error(provider, e)) from e
    except TimeoutError:
        raise AIError(f"{provider} отне прекалено дълго да отговори (над 3 минути). Опитайте отново — генерирането на дълъг текст понякога отнема повече време.")
    except urllib.error.URLError as e:
        raise AIError(f"Няма връзка с {provider}: {e.reason}") from e

# --- PDF export and email delivery ---

# Every reading the user can export. The label becomes the PDF's title, and the
# cache key is either fixed or a prefix the client completes (date, period, sign).
READING_TITLES = {
    "profile":    "Личен портрет",
    "akashic":    "Акашови записи",
    "numerology": "Нумерологичен анализ",
    "horoscope":  "Дневен хороскоп",
    "period":     "Анализ на период",
    "love":       "Любовен хороскоп",
    "love-full":  "Любовен хороскоп",
}

def reading_title(cache_key: str) -> str:
    """Human title for a cache key, which may carry a ':suffix' (date, period, sign)."""
    base = (cache_key or "").split(":", 1)[0]
    return READING_TITLES.get(base, "Разчитане")

def reading_subtitle(cache_key: str) -> str:
    """Turn the cache key's suffix into a readable line under the title."""
    base, _, rest = (cache_key or "").partition(":")
    if not rest:
        return ""
    if base == "horoscope":
        return f"за {bg_date(rest)}"
    if base == "period":
        start, _, end = rest.partition(":")
        return f"за периода {bg_date(start)} – {bg_date(end)}" if end else ""
    if base == "numerology":
        return f"за {rest} г."
    if base == "love":
        return f"съвместимост с {SIGNS.get(rest, rest)}"
    if base == "love-full":
        return "съвместимост по пълни рождени данни"
    return ""

def bg_date(iso: str) -> str:
    """YYYY-MM-DD -> DD.MM.YYYY, leaving anything unexpected untouched."""
    try:
        return datetime.datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return iso

def build_person_pdf(person: dict, cache_key: str) -> Tuple[bytes, str]:
    """Render a cached reading as a PDF. Returns (bytes, filename)."""
    cached = get_ai_cache(person["id"], cache_key)
    if not cached:
        raise HTTPException(404, "Това разчитане още не е генерирано. Отвори го в приложението и опитай пак.")

    summary, body = split_summary(cached["content"])

    # The summary block feeds the little cards; without one, fall back to the
    # chart's own headline positions so the cover page is never empty.
    facts = []
    if isinstance(summary, dict):
        for k, v in list(summary.items())[:4]:
            if v:
                facts.append((str(k), str(v)))
    if not facts:
        try:
            by_name = {o["name"]: o for o in compute_natal(person)["objects"].values()}
            for label, name in (("Слънце", "Sun"), ("Луна", "Moon"), ("Асцендент", "Asc")):
                if name in by_name:
                    facts.append((label, by_name[name]["sign"]))
        except Exception:
            pass

    birth = f"{person['day']}.{person['month']}.{person['year']} г., " \
            f"{person['hour']:02d}:{person['minute']:02d} ч."
    subtitle = reading_subtitle(cache_key)
    subtitle = f"{subtitle} · {birth}" if subtitle else birth

    logo = BASE_DIR / "static" / "logo-header.png"
    pdf = build_reading_pdf(
        title=reading_title(cache_key),
        person_name=person["name"],
        subtitle=subtitle,
        facts=facts,
        body=body,
        logo_path=str(logo) if logo.exists() else None,
        brand=brand_name(),
    )

    safe = re.sub(r"[^0-9A-Za-zА-Яа-я]+", "-", person["name"]).strip("-") or "razchitane"
    # Only the first key segment goes in the filename; suffixes like a partner's
    # full birth data would make it unreadable.
    base, _, rest = cache_key.partition(":")
    slug = base if base in ("love-full", "profile", "akashic") else \
        re.sub(r"[^0-9A-Za-z-]+", "-", cache_key).strip("-")
    # The filename follows the brand, so a rename does not keep shipping PDFs
    # named after the old one. ASCII only — some mail clients mangle the rest.
    prefix = brand_slug()
    return pdf, f"{prefix}-{safe}-{slug}.pdf"

@app.get("/api/persons/{person_id}/reading.pdf")
def api_reading_pdf(person_id: int, key: str, user: Tuple[int, str] = Depends(get_current_user)):
    """Download one cached reading as a PDF."""
    user_id, _ = user
    person = get_person(person_id, user_id)
    if not person:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")

    pdf, filename = build_person_pdf(person, key)
    # The filename holds Cyrillic, so it goes out RFC 5987-encoded.
    quoted = urllib.parse.quote(filename)
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


def _text_for_speech(text: str) -> str:
    """Премахва markdown маркерите, за да чете гладко българският TTS."""
    import re
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)      # **bold**
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)  # # заглавия
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)    # - bullet
    text = re.sub(r"^\s*\d+\.\s*", "", text, flags=re.M)   # 1. номерация
    text = re.sub(r"\*([^*\n]+)\*", r"\1", text)       # *italic*
    text = re.sub(r"[_`]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Колко части да се синтезират едновременно. Измерено: 5 части свалят
# 87 секунди до 29 (3x). Над това Microsoft тротлва и няма полза.
TTS_CHUNKS = int(os.environ.get("TTS_CHUNKS", "5"))


def _split_for_tts(text: str, parts: int) -> list:
    """Разделя текста на приблизително равни части по границите на изреченията.

    Реже само след ".", "!", "?" или нов ред — така никоя част не започва
    по средата на изречение и интонацията на гласа остава естествена."""
    if parts <= 1 or len(text) < 1500:
        return [text]

    import re
    # Изреченията остават заедно със своя препинателен знак.
    sentences = re.findall(r"[^.!?" + "\n" + r"]+[.!?]*\s*", text) or [text]
    target = max(1, len(text) // parts)

    chunks, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) > target and len(chunks) < parts - 1:
            chunks.append(cur)
            cur = s
        else:
            cur += s
    if cur.strip():
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def _text_to_audio(text: str, path: str) -> None:
    """Генерира mp3 с българския глас Kalina (безплатен Microsoft Edge TTS).

    Дългите разчитания се синтезират на части едновременно и се слепват.
    mp3 е поток от кадри, така че конкатенацията дава валиден файл — при
    26 минути аудио това сваля чакането от ~3.5 минути на около минута."""
    import asyncio
    import edge_tts

    chunks = _split_for_tts(text, TTS_CHUNKS)

    async def _gen():
        if len(chunks) == 1:
            await edge_tts.Communicate(text, "bg-BG-KalinaNeural").save(path)
            return

        async def one(idx: int, part: str) -> bytes:
            buf = bytearray()
            async for item in edge_tts.Communicate(part, "bg-BG-KalinaNeural").stream():
                if item["type"] == "audio":
                    buf.extend(item["data"])
            return bytes(buf)

        blobs = await asyncio.gather(*(one(i, c) for i, c in enumerate(chunks)))
        # Записва се наведнъж, за да не остане половин файл, ако нещо гръмне.
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            for b in blobs:
                fh.write(b)
        os.replace(tmp, path)

    asyncio.run(_gen())


@app.get("/api/persons/{person_id}/reading-audio")
def api_reading_audio(person_id: int, key: str,
                      user: Tuple[int, str] = Depends(get_current_user_flex)):
    """Чете кеширано разчитане на глас (mp3, български)."""
    user_id, _ = user
    person = get_person(person_id, user_id)
    if not person:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")

    cached = get_ai_cache(person["id"], key)
    if not cached:
        raise HTTPException(404, "Това разчитане още не е генерирано. Отвори го и опитай пак.")

    _, body = split_summary(cached["content"])
    speech = _text_for_speech(body)
    if not speech:
        raise HTTPException(404, "Няма текст за четене.")

    audio_dir = DB_PATH.parent / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z-]+", "-", key).strip("-") or "razchitane"
    # Хешът на текста влиза в името: регенерирано разчитане дава друго име и
    # значи ново аудио. Без него старото mp3 се преизползва завинаги и човекът
    # слуша предишната версия на разчитането си.
    digest = hashlib.sha1(speech.encode("utf-8")).hexdigest()[:10]
    mp3_path = audio_dir / f"{person_id}_{safe}_{digest}.mp3"

    if not mp3_path.exists():
        # Старите версии на същото разчитане вече не трябват на никого.
        for stale in audio_dir.glob(f"{person_id}_{safe}_*.mp3"):
            try:
                stale.unlink()
            except OSError:
                pass
        _text_to_audio(speech, str(mp3_path))

    # FileResponse стриймва файла и поддържа Range — превъртането в плейъра не
    # тегли всичко отначало, а и mp3-то не минава цялото през паметта.
    return FileResponse(
        mp3_path,
        media_type="audio/mpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


class EmailReadingRequest(BaseModel):
    key: str
    to: Optional[str] = None

@app.post("/api/persons/{person_id}/email-reading")
def api_email_reading(person_id: int, data: EmailReadingRequest,
                      user: Tuple[int, str] = Depends(get_current_user)):
    """Email one cached reading as a PDF attachment."""
    user_id, email = user
    person = get_person(person_id, user_id)
    if not person:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")

    to = (data.to or email or "").strip()
    if "@" not in to:
        raise HTTPException(400, "Въведи валиден имейл адрес.")

    pdf, filename = build_person_pdf(person, data.key)
    title = reading_title(data.key)
    name = first_name(person["name"]) or person["name"]

    subject, body = render_email_template(
        "share", title=title, person_name=person["name"], name=name)
    send_email(to, subject, body, attachment=(filename, pdf, "application/pdf"),
               html=_email_html(body))
    return {"ok": True, "to": to}

# --- Web UI Routes ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Landing page. Client-side JS sends already-signed-in visitors to the dashboard."""
    return HTMLResponse(templates.get_template("landing.html").render(
        {"request": request, "sky": sky_today(), "pricing": landing_pricing(),
         "features": FEATURE_PAGES, "zodiac_signs": ZODIAC_SIGNS,
         **seo_context(request, path="/")}))

@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt(request: Request):
    """Crawler rules. The private app pages are never worth indexing."""
    seo = seo_settings()
    base = public_base_url(request)
    if "noindex" in (seo["seo_robots"] or ""):
        body = "User-agent: *\nDisallow: /\n"
    else:
        body = (
            "User-agent: *\n"
            "Allow: /$\n"
            "Disallow: /dashboard\n"
            "Disallow: /chart/\n"
            "Disallow: /settings\n"
            "Disallow: /admin\n"
            "Disallow: /synastry\n"
            "Disallow: /api/\n"
            f"\nSitemap: {base}/sitemap.xml\n"
        )
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")

@app.get("/sitemap.xml")
def sitemap_xml(request: Request):
    """Only the publicly reachable pages belong in the sitemap."""
    seo = seo_settings()
    base = public_base_url(request)
    today = datetime.date.today().isoformat()
    entries = [
        ("/", "weekly", "1.0"),
        ("/register", "monthly", "0.6"),
        ("/login", "monthly", "0.3"),
    ]
    # Every module gets its own landing page — the long-tail content that the
    # search engines actually find people through.
    entries += [(f"/{p['slug']}", "monthly", "0.8") for p in FEATURE_PAGES]
    # Daily horoscopes per zodiac sign (12) + the hub. These refresh daily, so
    # they get a high change frequency and priority — they are the freshest,
    # most-searched content on the site.
    entries.append(("/horoskop", "daily", "0.9"))
    entries += [(f"/horoskop/{s['slug']}", "daily", "0.9") for s in ZODIAC_SIGNS]
    # Evergreen "planet in sign" pages — the long-tail backbone.
    for pl in PLANETS:
        entries += [(f"/{pl['slug']}-v-{s['slug']}", "monthly", "0.7")
                    for s in ZODIAC_SIGNS]
    # Zodiac sign profiles — the highest-volume searches ("характеристика на ...").
    entries += [(f"/zodia/{s['slug']}", "monthly", "0.8") for s in ZODIAC_SIGNS]
    # Sign compatibility — 78 pairs, long-tail "съвместимост овен телец" searches.
    entries.append(("/savmestimost", "monthly", "0.8"))
    entries += [(f"/savmestimost/{slug}", "monthly", "0.7") for _, _, slug in COMPAT_PAIRS]
    # Planet in house — 120 pages, long-tail "луна в 7 дом" searches.
    for pl in BODY_PLANETS:
        entries += [(f"/{pl['slug']}-v-{h['num']}-dom", "monthly", "0.7") for h in HOUSES]
    urls = "".join(
        f"<url><loc>{base}{path}</loc><lastmod>{today}</lastmod>"
        f"<changefreq>{freq}</changefreq><priority>{prio}</priority></url>"
        for path, freq, prio in entries
    )
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f"{urls}</urlset>")
    return Response(content=xml, media_type="application/xml")

@app.get("/start", response_class=HTMLResponse)
async def start_page(request: Request):
    """Birth details + email, before any account exists."""
    return HTMLResponse(templates.get_template("start.html").render({"request": request}))

@app.get("/welcome", response_class=HTMLResponse)
async def welcome_page(request: Request):
    """Landing spot after a successful first purchase."""
    return HTMLResponse(templates.get_template("welcome.html").render({"request": request}))

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return HTMLResponse(templates.get_template("register.html").render({"request": request}))

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return HTMLResponse(templates.get_template("login.html").render({"request": request}))

@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return HTMLResponse(templates.get_template("forgot_password.html").render({"request": request}))

@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request):
    return HTMLResponse(templates.get_template("reset_password.html").render({"request": request}))

@app.get("/share/{token}", response_class=HTMLResponse)
async def share_page(request: Request, token: str):
    return HTMLResponse(templates.get_template("share.html").render({
        "request": request,
        "token": token,
    }))

@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return HTMLResponse(templates.get_template("privacy.html").render({"request": request}))

@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request):
    return HTMLResponse(templates.get_template("terms.html").render({"request": request}))

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Dashboard — client-side JS handles auth check via localStorage token."""
    return HTMLResponse(templates.get_template("dashboard.html").render({"request": request}))

def _token_from_request(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # ?token= wins over the cookie: it is the freshly issued one, handed out by
    # onboarding or by a login link. A stale cookie from a previous account
    # would otherwise make the visitor look at somebody else's session and get
    # a 404 on their own chart.
    return request.query_params.get("token") or request.cookies.get("miralog_token") or None

@app.get("/chart/{person_id}", response_class=HTMLResponse)
async def view_chart(request: Request, person_id: int):
    """Chart view — JWT from cookie, Authorization header, or legacy ?token=."""
    user_id = None
    token = _token_from_request(request)
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            user_id = int(payload["sub"])
        except JWTError:
            pass
    if not user_id:
        # Fallback: redirect to login (chart page needs auth)
        return RedirectResponse("/login", status_code=302)

    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")
    chart_data = compute_natal(p)
    return HTMLResponse(templates.get_template("chart.html").render({
        "request": request,
        "person": p,
        "chart": chart_data,
    }))

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings page — client-side JS handles auth check via localStorage token."""
    return HTMLResponse(templates.get_template("settings.html").render({"request": request}))

@app.get("/synastry", response_class=HTMLResponse)
async def synastry_page(request: Request):
    """Synastry page — client-side JS handles auth check via localStorage token."""
    return HTMLResponse(templates.get_template("synastry.html").render({"request": request}))

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Admin panel — the API behind it enforces the admin role."""
    return HTMLResponse(templates.get_template("admin.html").render({"request": request}))

@app.get("/moon", response_class=HTMLResponse)
async def moon_page(request: Request):
    """Lunar calendar — client-side JS handles auth check via localStorage token."""
    return HTMLResponse(templates.get_template("moon.html").render({"request": request}))

# --- Landing pages for each module ---
def _feature_context(request: Request, page: dict) -> dict:
    """SEO context + related-page cross-links for a feature landing page."""
    ctx = seo_context(request, path=f"/{page['slug']}")
    name = brand_name()
    ctx["seo_title"] = page["seo_title"].replace("{brand}", name)
    ctx["seo_description"] = page["seo_description"].replace("{brand}", name)
    ctx["seo_keywords"] = page["seo_keywords"]
    ctx["page"] = page
    ctx["related_pages"] = [
        FEATURE_PAGES_BY_SLUG[s] for s in page.get("related", [])
        if s in FEATURE_PAGES_BY_SLUG
    ]
    return ctx


def _feature_route(page: dict):
    async def handler(request: Request):
        return HTMLResponse(templates.get_template("feature_landing.html").render(
            {"request": request, **_feature_context(request, page)}))

    return handler


for _fp in FEATURE_PAGES:
    app.add_api_route(
        f"/{_fp['slug']}", _feature_route(_fp),
        methods=["GET"], response_class=HTMLResponse,
        name=f"feature_{_fp['key']}", include_in_schema=False,
    )


# --- Дневен хороскоп по зодия (SEO страници) ---
_SIGN_FAQ = [
    {"q": "Колко често се актуализира дневният хороскоп?",
     "a": "Всеки ден. Хороскопът се пише наново всяка сутрин на база на реалните позиции на планетите за деня — не е предварително написан текст."},
    {"q": "Точен ли е хороскопът само по слънчевия знак?",
     "a": "Той е общ за всички, родени под този знак. За максимално точна прогноза, която стъпва на твоята лична натална карта, използвай персоналния дневен хороскоп."},
    {"q": "Каква е разликата с персоналния дневен хороскоп?",
     "a": "Тук прогнозата е по слънчевия знак — обща за милиони. Персоналният хороскоп се изчислява от транзитите спрямо точно твоята натална карта и е уникален за теб."},
    {"q": "Къде да получа пълното разчитане на картата си?",
     "a": "Пълният астрологически профил събира всички планети, домове и аспекти в един структуриран разказ — или вземи пакета „Всички модули“ с отстъпка. Еднократно плащане, остава завинаги.",
     "a_html": "Пълният <a href=\"/astrologicheski-profil\">астрологически профил</a> събира всички планети, домове и аспекти в един структуриран разказ — или вземи <a href=\"/start\">пакета „Всички модули“</a> с отстъпка. Еднократно плащане, остава завинаги."},
]


def _sign_seo_context(request: Request, sign: dict, date_bg: str) -> dict:
    """SEO context for one sign's horoscope page."""
    ctx = seo_context(request, path=f"/horoskop/{sign['slug']}")
    ctx["seo_title"] = f"Дневен хороскоп за {sign['name']} днес — {date_bg} | {brand_name()}"
    ctx["seo_description"] = (f"Дневният хороскоп за {sign['name']} за днес ({date_bg}): "
                              f"любов, работа, здраве и пари според днешните транзити. Актуализира се всеки ден.")
    ctx["seo_keywords"] = sign["keywords"]
    return ctx


@app.get("/horoskop", response_class=HTMLResponse)
async def horoskop_hub(request: Request):
    """Hub listing all 12 signs."""
    ctx = seo_context(request, path="/horoskop")
    ctx["seo_title"] = f"Дневен хороскоп за всички зодии — днес | {brand_name()}"
    ctx["seo_description"] = ("Дневен хороскоп за всичките 12 зодии: Овен, Телец, Близнаци, Рак, Лъв, Дева, "
                              "Везни, Скорпион, Стрелец, Козирог, Водолей и Риби. Актуализира се всеки ден.")
    ctx["signs"] = ZODIAC_SIGNS
    return HTMLResponse(templates.get_template("horoscope_index.html").render(ctx))


@app.get("/horoskop/{sign_slug}", response_class=HTMLResponse)
async def horoskop_sign(request: Request, sign_slug: str):
    """Daily horoscope for one zodiac sign."""
    sign = ZODIAC_BY_SLUG.get(sign_slug)
    if not sign:
        raise HTTPException(404, "Няма такъв знак.")
    now = datetime.datetime.now(ZoneInfo("Europe/Sofia"))
    date_iso = now.date().isoformat()
    date_bg = now.strftime("%d.%m.%Y")

    cached = get_sign_horoscope(sign["sign"], date_iso)
    ctx = _sign_seo_context(request, sign, date_bg)
    ctx.update({
        "sign": sign,
        "signs": ZODIAC_SIGNS,
        "date_bg": date_bg,
        "date_iso": date_iso,
        "sky": daily_sky(),
        "faq": _SIGN_FAQ,
    })
    if cached:
        summary, body = split_summary(cached)
        ctx["summary"] = summary
        ctx["body_html"] = _md_to_html(body)
    return HTMLResponse(templates.get_template("horoscope_sign.html").render(ctx))


@app.get("/api/horoskop/warm")
def api_horoskop_warm():
    """Start generation for every sign that has no cache for today. Called by the
    morning cron so the pages are ready before search engines crawl them."""
    now = datetime.datetime.now(ZoneInfo("Europe/Sofia"))
    date_iso = now.date().isoformat()
    date_bg = now.strftime("%d.%m.%Y")
    started = 0
    for sign in ZODIAC_SIGNS:
        if get_sign_horoscope(sign["sign"], date_iso):
            continue
        cache_key = f"sign:{sign['sign']}:{date_iso}"
        with _AI_JOBS_LOCK:
            running = _AI_JOBS.get(cache_key)
        if running and not running["done"].is_set():
            continue
        ai_job(cache_key, lambda s=sign: _generate_sign_horoscope(s, date_bg, date_iso))
        started += 1
    return {"started": started, "date": date_bg}


@app.get("/api/horoskop/{sign_slug}")
def api_horoskop(sign_slug: str, refresh: bool = False):
    """Generate (or return cached) today's horoscope for a sign. Polled by the page."""
    sign = ZODIAC_BY_SLUG.get(sign_slug)
    if not sign:
        raise HTTPException(404, "Няма такъв знак.")
    now = datetime.datetime.now(ZoneInfo("Europe/Sofia"))
    date_iso = now.date().isoformat()
    date_bg = now.strftime("%d.%m.%Y")
    cache_key = f"sign:{sign['sign']}:{date_iso}"

    if not refresh:
        with _AI_JOBS_LOCK:
            running = _AI_JOBS.get(cache_key)
        if running and not running["done"].is_set():
            return {"pending": True, "date": date_bg}
        cached = get_sign_horoscope(sign["sign"], date_iso)
        if cached:
            summary, body = split_summary(cached)
            return {"summary": summary, "body": body, "date": date_bg, "cached": True}
        if running and running["done"].is_set() and running["error"]:
            return {"body": AI_UNAVAILABLE, "date": date_bg}

    job = ai_job(cache_key, lambda: _generate_sign_horoscope(sign, date_bg, date_iso))
    if job["done"].is_set():
        cached = get_sign_horoscope(sign["sign"], date_iso)
        if cached:
            summary, body = split_summary(cached)
            return {"summary": summary, "body": body, "date": date_bg, "cached": False}
        return {"body": AI_UNAVAILABLE, "date": date_bg}
    return {"pending": True, "date": date_bg}


# --- Вечнозелени SEO страници „планета в дом" (/luna-v-7-dom) ---
# Регистрирани ПРЕДИ „планета в знак": и двата шаблона са /{planet}-v-{...},
# но „-dom" накрая + int конвертор правят дома недвусмислен.
_HOUSE_FAQ = [
    {"q": "Това точно ли е значението за мен?",
     "a": "Това е общото значение за всички, родени с тази позиция. Конкретно за теб то зависи от знака на върха на дома, аспектите и останалите планети в картата ти — затова е нужен персонален анализ."},
    {"q": "Каква е разликата с наталната карта?",
     "a": "Тук виждаш една-единствена позиция извън контекст. Наталната карта показва как всички планети и домове си взаимодействат заедно и какво значи това лично за теб."},
    {"q": "Как да разбера в кой дом е моята планета?",
     "a": "Създай безплатната си натална карта — тя изчислява позицията на всяка планета по дом до градус и я обяснява конкретно за теб."},
    {"q": "Къде да получа пълното разчитане на картата си?",
     "a": "Пълният астрологически профил събира всички планети, домове и аспекти в един структуриран разказ — или вземи пакета „Всички модули“ с отстъпка. Еднократно плащане, остава завинаги.",
     "a_html": "Пълният <a href=\"/astrologicheski-profil\">астрологически профил</a> събира всички планети, домове и аспекти в един структуриран разказ — или вземи <a href=\"/start\">пакета „Всички модули“</a> с отстъпка. Еднократно плащане, остава завинаги."},
]


@app.get("/{planet_slug}-v-{house_num:int}-dom", response_class=HTMLResponse)
async def planet_house_page(request: Request, planet_slug: str, house_num: int):
    planet = PLANETS_BY_SLUG.get(planet_slug)
    house = HOUSES_BY_NUM.get(house_num)
    if not planet or not house:
        raise HTTPException(404, "Няма такава страница.")

    cached = get_planet_house(planet["key"], house["key"])
    ctx = seo_context(request, path=f"/{planet_slug}-v-{house_num}-dom")
    ctx["seo_title"] = f"{planet['name']} в {house['short']} — какво означава | {brand_name()}"
    ctx["seo_description"] = (f"{planet['name']} в {house['short']}: общото значение за характера, любовта и работата. "
                              f"Виж какво значи конкретно в твоята натална карта.")
    ctx["seo_keywords"] = f"{planet['name'].lower()} в {house['short']}, {planet['name'].lower()} в {house['name'].lower()}"
    ctx["planet"] = planet
    ctx["house"] = house
    ctx["planets"] = BODY_PLANETS
    ctx["houses"] = HOUSES
    ctx["faq"] = _HOUSE_FAQ
    if cached:
        ctx["body_html"] = _md_to_html(cached)
    return HTMLResponse(templates.get_template("planet_house.html").render(ctx))


@app.get("/api/dom/warm")
def api_planet_house_warm():
    """Генерира всички планета×дом комбинации без кеш (120 страници, еднократно)."""
    started = 0
    for p in BODY_PLANETS:
        for h in HOUSES:
            if get_planet_house(p["key"], h["key"]):
                continue
            cache_key = f"planet_house:{p['key']}:{h['key']}"
            with _AI_JOBS_LOCK:
                running = _AI_JOBS.get(cache_key)
            if running and not running["done"].is_set():
                continue
            ai_job(cache_key, lambda pp=p, hh=h: _generate_planet_house(pp, hh))
            started += 1
    return {"started": started}


@app.get("/api/dom/{planet_slug}-v-{house_num:int}")
def api_planet_house(planet_slug: str, house_num: int, refresh: bool = False):
    """Генерира (или връща кеширан) тизъра за „{планета} в {дом}"."""
    planet = PLANETS_BY_SLUG.get(planet_slug)
    house = HOUSES_BY_NUM.get(house_num)
    if not planet or not house:
        raise HTTPException(404, "Няма такава страница.")
    cache_key = f"planet_house:{planet['key']}:{house['key']}"

    if not refresh:
        with _AI_JOBS_LOCK:
            running = _AI_JOBS.get(cache_key)
        if running and not running["done"].is_set():
            return {"pending": True}
        cached = get_planet_house(planet["key"], house["key"])
        if cached:
            return {"body": cached, "cached": True}

    job = ai_job(cache_key, lambda: _generate_planet_house(planet, house))
    if job["done"].is_set():
        cached = get_planet_house(planet["key"], house["key"])
        if cached:
            return {"body": cached, "cached": False}
        return {"body": AI_UNAVAILABLE}
    return {"pending": True}


# --- Вечнозелени SEO страници „планета в знак" ---
_PLANET_FAQ = [
    {"q": "Това точно ли е значението за мен?",
     "a": "Това е общото значение за всички, родени с тази позиция. Конкретно за теб то зависи от дома, аспектите и останалите планети в твоята карта — затова е нужен персонален анализ."},
    {"q": "Каква е разликата с наталната карта?",
     "a": "Тук виждаш една-единствена позиция извън контекст. Наталната карта показва как всички планети си взаимодействат заедно и какво значи това лично за теб."},
    {"q": "Как да разбера точната си позиция?",
     "a": "Създай безплатната си натална карта — тя изчислява позицията на всяка планета до градус и я обяснява конкретно за теб."},
    {"q": "Къде да получа пълното разчитане на всичките си позиции?",
     "a": "Пълният астрологически профил събира всички планети, домове и аспекти от картата ти в един структуриран разказ — или вземи пакета „Всички модули“ с отстъпка. Еднократно плащане, остава завинаги.",
     "a_html": "Пълният <a href=\"/astrologicheski-profil\">астрологически профил</a> събира всички планети, домове и аспекти от картата ти в един структуриран разказ — или вземи <a href=\"/start\">пакета „Всички модули“</a> с отстъпка. Еднократно плащане, остава завинаги."},
]


@app.get("/{planet_slug}-v-{sign_slug}", response_class=HTMLResponse)
async def planet_sign_page(request: Request, planet_slug: str, sign_slug: str):
    planet = PLANETS_BY_SLUG.get(planet_slug)
    sign = ZODIAC_BY_SLUG.get(sign_slug)
    if not planet or not sign:
        raise HTTPException(404, "Няма такава страница.")

    cached = get_planet_sign(planet["key"], sign["sign"])
    ctx = seo_context(request, path=f"/{planet_slug}-v-{sign_slug}")
    ctx["seo_title"] = f"{planet['name']} в {sign['name']} — какво означава | {brand_name()}"
    ctx["seo_description"] = (f"{planet['name']} в {sign['name']}: общото значение за характера, любовта и работата. "
                              f"Виж какво значи конкретно в твоята натална карта.")
    ctx["seo_keywords"] = f"{planet['name'].lower()} в {sign['name'].lower()}, {planet['name'].lower()} в знак {sign['name'].lower()}"
    ctx["planet"] = planet
    ctx["sign"] = sign
    ctx["planets"] = PLANETS
    ctx["signs"] = ZODIAC_SIGNS
    ctx["faq"] = _PLANET_FAQ
    if cached:
        ctx["body_html"] = _md_to_html(cached)
    return HTMLResponse(templates.get_template("planet_sign.html").render(ctx))


@app.get("/api/planeta/{planet_slug}-v-{sign_slug}")
def api_planet_sign(planet_slug: str, sign_slug: str, refresh: bool = False):
    """Generate (or return cached) the evergreen "planet in sign" teaser."""
    planet = PLANETS_BY_SLUG.get(planet_slug)
    sign = ZODIAC_BY_SLUG.get(sign_slug)
    if not planet or not sign:
        raise HTTPException(404, "Няма такава страница.")
    cache_key = f"planet:{planet['key']}:{sign['sign']}"

    if not refresh:
        with _AI_JOBS_LOCK:
            running = _AI_JOBS.get(cache_key)
        if running and not running["done"].is_set():
            return {"pending": True}
        cached = get_planet_sign(planet["key"], sign["sign"])
        if cached:
            return {"body": cached, "cached": True}

    job = ai_job(cache_key, lambda: _generate_planet_sign(planet, sign))
    if job["done"].is_set():
        cached = get_planet_sign(planet["key"], sign["sign"])
        if cached:
            return {"body": cached, "cached": False}
        return {"body": AI_UNAVAILABLE}
    return {"pending": True}


@app.get("/api/planeta/warm")
def api_planet_warm():
    """Generate every planet×sign combo that has no cache yet (132 pages, one-off)."""
    started = 0
    for p in PLANETS:
        for s in ZODIAC_SIGNS:
            if get_planet_sign(p["key"], s["sign"]):
                continue
            cache_key = f"planet:{p['key']}:{s['sign']}"
            with _AI_JOBS_LOCK:
                running = _AI_JOBS.get(cache_key)
            if running and not running["done"].is_set():
                continue
            ai_job(cache_key, lambda pp=p, ss=s: _generate_planet_sign(pp, ss))
            started += 1
    return {"started": started}


# --- Вечнозелени SEO страници „характеристика на знак" (/zodia/{slug}) ---
_SIGN_PROFILE_FAQ = [
    {"q": "Тази характеристика важи ли за всички, родени под този знак?",
     "a": "Да, тя описва общия случай — типичното за повечето хора с този слънчев знак. Точният ти портрет зависи от Луната, Асцендента, домовете и аспектите в твоята карта."},
    {"q": "Каква е разликата с наталната карта?",
     "a": "Слънчевият знак е само едно парче от пъзела. Наталната карта показва всички планети заедно и какво значи това лично за теб — много по-точно от една обща характеристика."},
    {"q": "Къде да получа пълното си разчитане?",
     "a": "Пълният астрологически профил събира всички позиции в един структуриран разказ — или вземи пакета „Всички модули“ с отстъпка. Еднократно плащане, остава завинаги.",
     "a_html": "Пълният <a href=\"/astrologicheski-profil\">астрологически профил</a> събира всички позиции в един структуриран разказ — или вземи <a href=\"/start\">пакета „Всички модули“</a> с отстъпка. Еднократно плащане, остава завинаги."},
]


@app.get("/zodia/{sign_slug}", response_class=HTMLResponse)
async def sign_profile_page(request: Request, sign_slug: str):
    sign = ZODIAC_BY_SLUG.get(sign_slug)
    if not sign:
        raise HTTPException(404, "Няма такава страница.")

    cached = get_sign_profile(sign["sign"])
    ctx = seo_context(request, path=f"/zodia/{sign_slug}")
    ctx["seo_title"] = f"Характеристика на {sign['name']} — зодия {sign['name']} | {brand_name()}"
    ctx["seo_description"] = (f"Характеристика на зодия {sign['name']}: характер, силни и слаби страни, любов, работа и пари. "
                              f"Виж какво значи конкретно в твоята натална карта.")
    ctx["seo_keywords"] = f"характеристика на {sign['name'].lower()}, зодия {sign['name'].lower()}"
    ctx["sign"] = sign
    ctx["signs"] = ZODIAC_SIGNS
    ctx["planets"] = PLANETS
    ctx["faq"] = _SIGN_PROFILE_FAQ
    if cached:
        ctx["body_html"] = _md_to_html(cached)
    return HTMLResponse(templates.get_template("sign_profile.html").render(ctx))


@app.get("/api/zodia/warm")
def api_sign_profile_warm():
    """Generate every sign profile that has no cache yet (12 pages, one-off)."""
    started = 0
    for s in ZODIAC_SIGNS:
        if get_sign_profile(s["sign"]):
            continue
        cache_key = f"profile:{s['sign']}"
        with _AI_JOBS_LOCK:
            running = _AI_JOBS.get(cache_key)
        if running and not running["done"].is_set():
            continue
        ai_job(cache_key, lambda ss=s: _generate_sign_profile(ss))
        started += 1
    return {"started": started}


@app.get("/api/zodia/{sign_slug}")
def api_sign_profile(sign_slug: str, refresh: bool = False):
    """Generate (or return cached) the evergreen sign profile."""
    sign = ZODIAC_BY_SLUG.get(sign_slug)
    if not sign:
        raise HTTPException(404, "Няма такава страница.")
    cache_key = f"profile:{sign['sign']}"

    if not refresh:
        with _AI_JOBS_LOCK:
            running = _AI_JOBS.get(cache_key)
        if running and not running["done"].is_set():
            return {"pending": True}
        cached = get_sign_profile(sign["sign"])
        if cached:
            return {"body": cached, "cached": True}

    job = ai_job(cache_key, lambda: _generate_sign_profile(sign))
    if job["done"].is_set():
        cached = get_sign_profile(sign["sign"])
        if cached:
            return {"body": cached, "cached": False}
        return {"body": AI_UNAVAILABLE}
    return {"pending": True}


# --- Вечнозелени SEO страници „съвместимост по зодии" ---
_COMPAT_FAQ = [
    {"q": "Тази съвместимост важи ли за всички двойки от тези знаци?",
     "a": "Тя описва общия случай — типичното за повечето двойки с тези слънчеви знаци. Истинската съвместимост зависи от Луната, Асцендента и аспектите между двете натални карти."},
    {"q": "Каква е разликата със синастрията?",
     "a": "Съвместимостта по слънчев знак е само първата стъпка. Синастрията сравнява двете пълни натални карти — всички планети, домове и аспекти — и показва какво значи конкретно за вашата връзка."},
    {"q": "Как да разбера истинската ни съвместимост?",
     "a": "Създай наталната си карта и тази на партньора — сравнението на двете карти показва реалната ви съвместимост до градус."},
    {"q": "Къде да получа пълния анализ на връзката?",
     "a": "Пълният астрологически профил събира всички планети, домове и аспекти в един структуриран разказ — или вземи пакета „Всички модули“ с отстъпка. Еднократно плащане, остава завинаги.",
     "a_html": "Пълният <a href=\"/astrologicheski-profil\">астрологически профил</a> събира всички планети, домове и аспекти в един структуриран разказ — или вземи <a href=\"/start\">пакета „Всички модули“</a> с отстъпка. Еднократно плащане, остава завинаги."},
]


def _compat_slug(a: dict, b: dict) -> str:
    """Каноничен slug за двойка — по-ранният зодиакален знак е първи."""
    ia = ZODIAC_SIGNS.index(a)
    ib = ZODIAC_SIGNS.index(b)
    if ia <= ib:
        return f"{a['slug']}-{b['slug']}"
    return f"{b['slug']}-{a['slug']}"


@app.get("/savmestimost", response_class=HTMLResponse)
async def compatibility_hub(request: Request):
    """Матрица 12×12 със съвместимостта между всички знаци."""
    ctx = seo_context(request, path="/savmestimost")
    ctx["seo_title"] = f"Съвместимост по зодии — всички двойки | {brand_name()}"
    ctx["seo_description"] = ("Съвместимост между всички 12 зодии: Овен, Телец, Близнаци, Рак, Лъв, Дева, Везни, "
                              "Скорпион, Стрелец, Козирог, Водолей и Риби. Виж съвместимостта на всяка двойка.")
    ctx["seo_keywords"] = "съвместимост по зодии, зодиакална съвместимост, съвместимост на зодиите"
    ctx["signs"] = ZODIAC_SIGNS
    # Матрица 12×12: редове = знак A, колони = знак B, клетка = каноничен slug.
    ctx["matrix"] = [
        [{"a": row_sign, "b": col_sign, "slug": _compat_slug(row_sign, col_sign)} for col_sign in ZODIAC_SIGNS]
        for row_sign in ZODIAC_SIGNS
    ]
    return HTMLResponse(templates.get_template("compatibility_index.html").render(ctx))


@app.get("/savmestimost/{pair_slug}", response_class=HTMLResponse)
async def compatibility_page(request: Request, pair_slug: str):
    pair = COMPAT_BY_SLUG.get(pair_slug)
    if not pair:
        raise HTTPException(404, "Няма такава страница.")
    sign_a, sign_b = pair
    cached = get_compatibility(sign_a["sign"], sign_b["sign"])
    ctx = seo_context(request, path=f"/savmestimost/{pair_slug}")
    ctx["seo_title"] = f"Съвместимост {sign_a['name']} и {sign_b['name']} — по зодии | {brand_name()}"
    ctx["seo_description"] = (f"Съвместимост между {sign_a['name']} и {sign_b['name']}: любов, комуникация и "
                              f"предизвикателства. Виж какво значи конкретно за вашата връзка.")
    ctx["seo_keywords"] = (f"съвместимост {sign_a['name'].lower()} {sign_b['name'].lower()}, "
                           f"{sign_a['name'].lower()} и {sign_b['name'].lower()} съвместимост")
    ctx["sign_a"] = sign_a
    ctx["sign_b"] = sign_b
    ctx["pair_slug"] = pair_slug
    ctx["signs"] = ZODIAC_SIGNS
    ctx["faq"] = _COMPAT_FAQ
    ctx["related_a"] = [{"sign": s, "slug": _compat_slug(sign_a, s)} for s in ZODIAC_SIGNS if s["sign"] != sign_a["sign"]]
    ctx["related_b"] = [{"sign": s, "slug": _compat_slug(sign_b, s)} for s in ZODIAC_SIGNS if s["sign"] != sign_b["sign"]]
    if cached:
        ctx["body_html"] = _md_to_html(cached)
    return HTMLResponse(templates.get_template("compatibility.html").render(ctx))


@app.get("/api/savmestimost/warm")
def api_compatibility_warm():
    """Генерира всички двойки без кеш (78 страници, еднократно)."""
    started = 0
    for sa, sb, slug in COMPAT_PAIRS:
        if get_compatibility(sa["sign"], sb["sign"]):
            continue
        cache_key = f"compat:{sa['sign']}:{sb['sign']}"
        with _AI_JOBS_LOCK:
            running = _AI_JOBS.get(cache_key)
        if running and not running["done"].is_set():
            continue
        ai_job(cache_key, lambda a=sa, b=sb: _generate_compatibility(a, b))
        started += 1
    return {"started": started}


@app.get("/api/savmestimost/{pair_slug}")
def api_compatibility(pair_slug: str, refresh: bool = False):
    """Генерира (или връща кеширан) тизъра за съвместимостта на една двойка."""
    pair = COMPAT_BY_SLUG.get(pair_slug)
    if not pair:
        raise HTTPException(404, "Няма такава страница.")
    sign_a, sign_b = pair
    cache_key = f"compat:{sign_a['sign']}:{sign_b['sign']}"

    if not refresh:
        with _AI_JOBS_LOCK:
            running = _AI_JOBS.get(cache_key)
        if running and not running["done"].is_set():
            return {"pending": True}
        cached = get_compatibility(sign_a["sign"], sign_b["sign"])
        if cached:
            return {"body": cached, "cached": True}

    job = ai_job(cache_key, lambda: _generate_compatibility(sign_a, sign_b))
    if job["done"].is_set():
        cached = get_compatibility(sign_a["sign"], sign_b["sign"])
        if cached:
            return {"body": cached, "cached": False}
        return {"body": AI_UNAVAILABLE}
    return {"pending": True}


@app.get("/healthz")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
