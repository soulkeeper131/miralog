# -*- coding: utf-8 -*-
import os, re, sys, json, sqlite3, datetime, urllib.parse, urllib.request, urllib.error, secrets, hashlib, asyncio, threading, time
from pathlib import Path
from contextlib import asynccontextmanager, contextmanager
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

# Външно следене на грешките. Без SENTRY_DSN нищо не се включва и нищо не
# се праща навън — приложението работи както преди. Смисълът е да се разбира
# за проблем от инструмент, а не от клиент.
SENTRY_DSN = (os.environ.get("SENTRY_DSN") or "").strip()
if SENTRY_DSN:
    try:
        import sentry_sdk

        def _scrub(event, hint):
            """Маха личните данни, преди събитието да напусне сървъра.

            Рождените данни и имейлите са лични по смисъла на GDPR — за
            намирането на бъг стигат видът на грешката и мястото в кода.
            """
            event.pop("request", None)
            event.pop("user", None)
            for key in ("extra", "contexts"):
                value = event.get(key)
                if isinstance(value, dict):
                    value.pop("body", None)
                    value.pop("data", None)
            return event

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=ENVIRONMENT,
            # Без записи на заявките и без профилиране: интересуват ни
            # грешките, а следенето на производителност струва пари.
            traces_sample_rate=0.0,
            send_default_pii=False,
            before_send=_scrub,
        )
    except Exception as exc:      # счупен DSN не бива да спира сайта
        # print, а не log: logging се настройва по-надолу във файла и още
        # не е готов на този ред.
        print(f"Sentry не се включи: {exc}", file=sys.stderr)

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


class _HideTokensInAccessLog(logging.Filter):
    """Скрива токените от адресите в лога на uvicorn.

    Картата след регистрация, админът на поддомейна и линкът за нова парола
    носят токен в адреса. Uvicorn записва адреса целия, така че всеки с достъп
    до логовете в Coolify би взел 30-дневен вход — включително админския.
    """
    _TOKEN = re.compile(r"((?:^|[?&])(?:token|access_token)=)[^&\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._TOKEN.sub(r"\1***", a) if isinstance(a, str) else a
                                for a in record.args)
        return True


logging.getLogger("uvicorn.access").addFilter(_HideTokensInAccessLog())

# A Windows console defaults to cp1251 and raises on Cyrillic. Reconfigure the
# streams where possible so startup messages are readable instead of fatal.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# --- Логове за поддръжка ---
# Всяка заявка получава кратък код (X-Request-ID). Клиентът го вижда при
# грешка, а в лога стои пред всеки ред от тази заявка — включително от
# фоновото AI генериране. Досега info съобщенията изобщо не стигаха до лога
# (логерът нямаше handler), а логовете в Coolify изчезват при всеки деплой —
# затова се пишат и във файл в тома с данните.
import contextvars
from logging.handlers import TimedRotatingFileHandler

REQUEST_ID: contextvars.ContextVar = contextvars.ContextVar("request_id", default="-")
# (адрес, user_id) на заявката — по него AI разходът се води на клиент, SEO
# страница или админ. Извън заявка (фоновият цикъл) е None.
AI_ORIGIN: contextvars.ContextVar = contextvars.ContextVar("ai_origin", default=None)
LOG_DIR = Path(os.environ.get("LOG_DIR", DB_PATH.parent / "logs"))
LOG_KEEP_DAYS = int(os.environ.get("LOG_KEEP_DAYS", "14"))


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = REQUEST_ID.get()
        return True


def _sofia_time(seconds, *_):
    return datetime.datetime.fromtimestamp(seconds, ZoneInfo("Europe/Sofia")).timetuple()


def setup_logging() -> None:
    """Stdout (за Coolify) и дневен файл в data/logs (пази се LOG_KEEP_DAYS).
    Може да се вика повторно — не добавя handler-и втори път."""
    if getattr(log, "_astro_configured", False):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(request_id)s] %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    fmt.converter = _sofia_time
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(TimedRotatingFileHandler(
            LOG_DIR / "app.log", when="midnight", backupCount=LOG_KEEP_DAYS,
            encoding="utf-8"))
    except OSError as exc:  # без файл — поне stdout
        print(f"Логовете няма да се пазят във файл: {exc}", file=sys.stderr)
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(_RequestIdFilter())
        log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False
    log._astro_configured = True


setup_logging()

# Редът на uvicorn за всяка заявка се заменя с нашия (с код, потребител и
# време). Иначе всяка заявка би стояла два пъти, а неговият е без код.
logging.getLogger("uvicorn.access").disabled = True


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

        # Фискалната следа на една продажба (Н-18). Плащане не се трие —
        # анулира се или се отбелязва върнатата сума, иначе одиторският файл
        # за вече подаден месец би се променил тихо.
        for col, ddl in [
            ("payment_intent", "ALTER TABLE payments ADD COLUMN payment_intent TEXT"),
            ("discount_cents", "ALTER TABLE payments ADD COLUMN discount_cents INTEGER NOT NULL DEFAULT 0"),
            ("voided_at", "ALTER TABLE payments ADD COLUMN voided_at TIMESTAMP"),
            ("void_reason", "ALTER TABLE payments ADD COLUMN void_reason TEXT"),
            ("documents_at", "ALTER TABLE payments ADD COLUMN documents_at TIMESTAMP"),
            ("granted_at", "ALTER TABLE payments ADD COLUMN granted_at TIMESTAMP"),
        ]:
            if col not in [r[1] for r in conn.execute("PRAGMA table_info(payments)").fetchall()]:
                conn.execute(ddl)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_intent ON payments(payment_intent)")
        # Редовете на продажбата с реално платената сума за всеки: при пакет
        # или промо код това не е цената от ценоразписа. Документите за
        # клиента и одиторският файл четат оттук, за да съвпадат до стотинка.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payment_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id INTEGER NOT NULL REFERENCES payments(id),
                feature_key TEXT NOT NULL,
                name TEXT NOT NULL,
                list_cents INTEGER NOT NULL DEFAULT 0,
                amount_cents INTEGER NOT NULL,
                vat_rate INTEGER NOT NULL,
                vat_cents INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_items_payment ON payment_items(payment_id)")
        # Върнати суми — по една на ред, с датата на връщане: одиторският
        # файл ги показва в месеца, в който парите са върнати (Н-18).
        # method: card / account / cash / other (кодовете r_paym в XSD).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payment_refunds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id INTEGER NOT NULL REFERENCES payments(id),
                amount_cents INTEGER NOT NULL,
                refunded_at TIMESTAMP NOT NULL,
                method TEXT NOT NULL DEFAULT 'card',
                source TEXT NOT NULL DEFAULT 'admin',
                external_id TEXT UNIQUE,
                note TEXT,
                recorded_by INTEGER REFERENCES users(id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_refunds_payment ON payment_refunds(payment_id)")
        # Документ за регистриране на продажбата (Н-18, чл. 52о, ал. 1, т. 1):
        # 10-разряден номер, нараства със стъпка 1 за всяка продажба, уникален
        # за целия магазин. Отделен брояч — тестовите и ръчните плащания не
        # бива да оставят дупки в номерацията.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sale_documents (
                number INTEGER PRIMARY KEY,
                payment_id INTEGER NOT NULL UNIQUE REFERENCES payments(id),
                issued_at TIMESTAMP NOT NULL
            )
        """)

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
            ("last_seen", "ALTER TABLE users ADD COLUMN last_seen TIMESTAMP"),
            ("display_name", "ALTER TABLE users ADD COLUMN display_name TEXT"),
            ("stripe_customer_id", "ALTER TABLE users ADD COLUMN stripe_customer_id TEXT"),
            ("stripe_subscription_id", "ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT"),
            ("digest_opt_in", "ALTER TABLE users ADD COLUMN digest_opt_in INTEGER NOT NULL DEFAULT 0"),
            ("totp_secret", "ALTER TABLE users ADD COLUMN totp_secret TEXT"),
            ("lifecycle_expiring_for", "ALTER TABLE users ADD COLUMN lifecycle_expiring_for TEXT"),
            ("lifecycle_expired_for", "ALTER TABLE users ADD COLUMN lifecycle_expired_for TEXT"),
            ("last_digest_on", "ALTER TABLE users ADD COLUMN last_digest_on TEXT"),
            # Вдига се при смяна на парола, блокиране, смяна на имейл — и
            # всички издадени дотогава токени спират да важат. Токен без
            # версия се чете като 0, затова деплоят не изхвърля никого.
            ("token_version", "ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0"),
        ]:
            if col not in user_cols:
                conn.execute(ddl)
        # Входът търси имейла без значение от главните букви.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email_lower ON users(lower(email))")

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

        # Леко проследяване на прегледите (без IP/UA) — за „активност" в админ
        # дашборда. Само път + час + (опц.) user_id — обобщена анонимна статистика.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS page_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                user_id INTEGER,
                viewed_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_page_views_time ON page_views(viewed_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_page_views_path ON page_views(path)")
        # Един ред на AI извикване: токени, цена по тарифата в този час, и за
        # какво е било — клиентско разчитане, SEO страница, фоново, админ.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                provider TEXT, model TEXT,
                source TEXT, feature TEXT, user_id INTEGER,
                input_tokens INTEGER DEFAULT 0,
                cached_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd REAL,
                ok INTEGER DEFAULT 1,
                attempts INTEGER DEFAULT 1,
                duration_ms INTEGER,
                request_id TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_at ON ai_usage(at)")

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
                # Безплатното, което всеки нов акаунт получава (досега идваше
                # от попълването при всеки старт, което вече е еднократно).
                for key in ("chart", "horoscope"):
                    conn.execute(
                        "INSERT OR IGNORE INTO feature_purchases"
                        " (user_id, feature_key, price_cents, currency) VALUES (?, ?, 0, 'EUR')",
                        (cur.lastrowid, key))

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
                    ("moon", 299, 1),
                    # The chart is granted free at onboarding, so it is never
                    # offered for sale; planets and aspects ride along with it.
                    ("chart", 0, 0),
                    ("planets", 0, 0),
                    ("aspects", 0, 0),
                    ("numerology", 400, 1),
                ])

        # --- Еднократни миграции ---
        # Досега всичко по-долу вървеше при всеки старт (= всеки деплой) и
        # връщаше обратно ръчните промени на админа: отнет модул се появяваше
        # отново, създаден от админа план ставаше вечна безплатна покупка.
        # Всяка миграция вече се отбелязва в settings и минава само веднъж.
        # На първия старт след тази промяна минават за последно — точно
        # както досега — затова той не променя нищо в достъпа.
        def once(name: str) -> bool:
            key = f"migration:{name}"
            if conn.execute("SELECT 1 FROM settings WHERE key = ?", (key,)).fetchone():
                return False
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)",
                         (key, datetime.datetime.utcnow().isoformat(timespec="seconds")))
            return True

        # Плащанията отпреди тази версия вече са отключени и документите им са
        # пратени. Без отметка повторно отваряне на стар линк за успешно
        # плащане би отключило отнет модул и би издало втора фактура.
        if once("payments_state_backfill"):
            conn.execute("UPDATE payments SET granted_at = paid_at WHERE granted_at IS NULL")
            conn.execute("UPDATE payments SET documents_at = paid_at"
                         " WHERE documents_at IS NULL AND method = 'stripe'")

        # Досега документите за продажба носеха номера на плащането. Новата
        # номерация продължава след най-големия вече използван номер, за да
        # не се повтори номер от издаден документ.
        if once("sale_document_numbering"):
            seed = conn.execute("SELECT COALESCE(MAX(id), 0) FROM payments").fetchone()[0]
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sale_doc_seed', ?)",
                         (str(int(seed)),))

        # Older installs gave the demo plan a free chart and numerology.
        if once("demo_plan_without_chart"):
            conn.execute(
                "UPDATE plans SET name = 'Основен', features = ?"
                " WHERE key = 'demo' AND features LIKE '%chart%'",
                (json.dumps(["planets", "aspects"]),))

        # Accounts created before the chart became free were never granted it,
        # so their own chart page would 402. Give it to anyone who has a person.
        if once("chart_for_everyone_with_a_person"):
            conn.execute(
                "INSERT OR IGNORE INTO feature_purchases (user_id, feature_key, price_cents, currency)"
                " SELECT DISTINCT user_id, 'chart', 0, 'EUR' FROM persons")

        # Synastry folded into the love module: one purchase covers both. Anybody
        # who bought it separately keeps their access through that key.
        if once("synastry_into_love"):
            conn.execute(
                "UPDATE feature_prices SET price_cents = 0, is_purchasable = 0"
                " WHERE feature_key = 'synastry'")
            conn.execute(
                "INSERT OR IGNORE INTO feature_purchases (user_id, feature_key, price_cents, currency)"
                " SELECT user_id, 'love', 0, 'EUR' FROM feature_purchases"
                " WHERE feature_key = 'synastry'")

        # The daily horoscope used to cost 3 EUR; it now comes with the chart.
        if once("horoscope_free_with_chart"):
            conn.execute(
                "UPDATE feature_prices SET price_cents = 0, is_purchasable = 0"
                " WHERE feature_key = 'horoscope'")
            conn.execute(
                "INSERT OR IGNORE INTO feature_purchases (user_id, feature_key, price_cents, currency)"
                " SELECT DISTINCT user_id, 'horoscope', 0, 'EUR' FROM persons")

        # „Пълно разчитане“ was withdrawn: the module, its endpoint and its
        # price are gone, so clear the leftover rows rather than leave a key
        # nothing can serve.
        if once("remove_interpretation"):
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
        if once("monthly_plan_to_purchases"):
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
        if once("chart_not_for_sale"):
            conn.execute(
                "UPDATE feature_prices SET price_cents = 0, is_purchasable = 0"
                " WHERE feature_key = 'chart'")

        # Безплатните функции се раздаваха само в /api/onboard, затова всеки,
        # който е влязъл през Google, Facebook или /register, е останал без
        # достъп до собствената си натална карта. Няма как да си я купи —
        # chart и horoscope не се продават. Наваксваме за заварените акаунти.
        if once("free_features_for_all_accounts"):
            for key in ("chart", "horoscope"):
                conn.execute(
                    "INSERT OR IGNORE INTO feature_purchases"
                    " (user_id, feature_key, price_cents, currency, payment_id)"
                    " SELECT id, ?, 0, 'EUR', NULL FROM users"
                    " WHERE id NOT IN (SELECT user_id FROM feature_purchases"
                    "                  WHERE feature_key = ?)",
                    (key, key))

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

        # Първият администратор се назначава само докато няма нито един.
        # Досега всеки старт правеше админ онзи, който държи ADMIN_EMAIL — а
        # всеки потребител може да смени имейла си на свободен адрес и така
        # да стане администратор при следващия деплой.
        if not conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone():
            conn.execute("UPDATE users SET role = 'admin' WHERE lower(email) = lower(?)",
                         (ADMIN_EMAIL,))
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
    warm_task = asyncio.create_task(_horoscope_warm_loop())
    try:
        yield
    finally:
        for task in (job_task, warm_task):
            task.cancel()
            try:
                await task
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
    # Имейл или телефон, който се печата в документа за продажба (чл. 52о,
    # ал. 1, т. 2). Празно = имейлът за лични данни.
    "contact": "",
    # Доставчикът на платежни услуги в одиторския файл (proc_id): ЕИК/ДДС
    # номер и наименование по Приложение № 38.
    "psp_id": "IE3206488LH Stripe Payments Europe, Limited",
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
    # Ключът остава записан, но бутонът може да се изключи от админ панела —
    # например ако доставчикът се повреди и не искаме хората да удрят в стена.
    # По подразбиране е включено, за да не изгаснат вече работещи логини.
    "google_enabled": "1",
    "facebook_enabled": "1",
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

def oauth_enabled(provider: str) -> bool:
    """Дали бутонът е включен от админа. Липсваща стойност значи включен."""
    raw = get_setting(f"oauth_{provider}_enabled")
    if raw is None or raw == "":
        return OAUTH_DEFAULTS.get(f"{provider}_enabled", "1") == "1"
    return str(raw).strip() not in ("0", "false", "False", "")


def oauth_providers() -> dict:
    """Which buttons to show. Both halves of a pair are required, and the
    provider must not have been switched off in the admin panel."""
    cfg = oauth_config()
    return {
        "google": bool(cfg["google_client_id"] and cfg["google_client_secret"]
                       and oauth_enabled("google")),
        "facebook": bool(cfg["facebook_app_id"] and cfg["facebook_app_secret"]
                         and oauth_enabled("facebook")),
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

def api_docs_settings(production: bool) -> dict:
    """/docs, /redoc и /openapi.json са карта на цялото API, админа включително.
    Полезни са при разработка; в production не са нужни на никого отвън."""
    if production:
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {}


def package_versions() -> dict:
    """Версиите, с които реално работи сървърът. requirements.txt не ги
    заковава, а Docker кешира слоя с pip — отвън не личи какво е инсталирано."""
    from importlib import metadata
    out = {}
    for name in ("fastapi", "starlette", "uvicorn", "pydantic", "jinja2", "stripe",
                 "python-jose", "bcrypt", "immanuel", "pyswisseph", "reportlab",
                 "edge-tts", "sentry-sdk", "pyotp", "python-multipart"):
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    out["python"] = sys.version.split()[0]
    return out


app = FastAPI(title=BRAND_DEFAULTS["brand_name"], lifespan=lifespan,
              **api_docs_settings(IS_PRODUCTION))
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
    "/api/auth/totp",
}
_ADMIN_HOST_ALLOWED_PREFIXES = ("/api/admin/", "/static/", "/uploads/")


def _is_oauth_auth_path(path: str) -> bool:
    """OAuth start/callback — нужни за Google/Facebook вход от админ панела.

    OAUTH_ENDPOINTS е дефиниран по-надолу в модула; тук го четем лениво (при
    заявка), когато модулът вече е зареден изцяло.
    """
    parts = path.strip("/").split("/")
    return (
        len(parts) == 4
        and parts[0] == "api" and parts[1] == "auth"
        and parts[2] in OAUTH_ENDPOINTS
        and parts[3] in ("start", "callback")
    )


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
            or _is_oauth_auth_path(path)
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

    # Без тези заглавки чужда страница може да вгради сайта в невидим iframe
    # и да лови кликове върху бутона за плащане (clickjacking). Останалите
    # спират налучкване на типа на файла и изтичане на адреса към други сайтове.
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # Браузърът да не пита за камера, микрофон и локация от наше име.
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    if IS_PRODUCTION:
        # Само по HTTPS: казва на браузъра никога повече да не опитва http.
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


# --- Проследяване на прегледите (обобщена анонимна статистика) ---
# Записваме само път + час + (опц.) user_id. Без IP, без User-Agent, без cookie
# фингерпринт — GDPR-щадящо. Админ поддомейнът и ботовете се пропускат, за да
# не раздуват числата.
_BOT_UA = (
    "bot", "crawl", "spider", "slurp", "baidu", "yandex", "ahrefs", "semrush",
    "mj12", "petal", "bytespider", "amazonbot", "gptbot", "ccbot", "claudebot",
    "perplexity", "meta-external", "headless", "python-requests", "curl", "wget",
    "google-extended", "chatgpt", "openai", "facebookexternalhit",
)


from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor

# Една нишка само за статистиката: не взима от нишките на заявките и пише
# поред, без да се бори сама със себе си за базата.
PAGE_VIEW_WRITER = _ThreadPoolExecutor(max_workers=1, thread_name_prefix="page-views")


@app.middleware("http")
async def track_page_views(request: Request, call_next):
    response = await call_next(request)
    try:
        host = (request.headers.get("host") or "").split(":")[0].strip().lower()
        if host == ADMIN_HOST:
            return response
        if request.method != "GET":
            return response
        if response.status_code != 200:
            return response
        path = request.url.path or "/"
        if path.startswith(("/api/", "/static/", "/uploads/", "/admin", "/healthz")):
            return response
        if "text/html" not in (response.headers.get("content-type") or ""):
            return response
        ua = (request.headers.get("user-agent") or "").lower()
        if any(b in ua for b in _BOT_UA):
            return response
        user_id = None
        token = _token_from_request(request)
        if token:
            try:
                payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
                user_id = int(payload["sub"])
            except Exception:
                user_id = None
        now = datetime.datetime.utcnow().isoformat(timespec="seconds")
        # Записът е в собствена нишка и страницата не го чака: досега всяка
        # HTML заявка пишеше в SQLite направо в event loop-а и при заета база
        # (AI запис, backup) целият сайт спираше до 5 секунди.
        PAGE_VIEW_WRITER.submit(_record_page_view, path, user_id, now)
    except Exception:
        pass  # статистиката никога не трябва да чупи страница
    return response


def _record_page_view(path: str, user_id: Optional[int], now: str) -> None:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO page_views (path, user_id, viewed_at) VALUES (?, ?, ?)",
                (path, user_id, now),
            )
    except Exception:
        pass


# --- Код на заявката, ред в лога и приличен отговор при срив ---
# Регистриран последен, значи е най-външният: обхваща и останалите middleware.
_QUIET_PATHS = ("/healthz", "/static/", "/uploads/", "/favicon")


def _user_id_for_log(request: Request) -> Optional[int]:
    try:
        token = _token_from_request(request)
        if token:
            return int(jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])["sub"])
    except Exception:
        pass
    return None


def _crash_response(request: Request, code: str) -> Response:
    message = (f"Нещо се обърка от наша страна. Опитай отново след малко; ако се "
               f"повтаря, пиши ни и посочи код {code}.")
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": message}, status_code=500)
    page = ("<!doctype html><html lang='bg'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Нещо се обърка</title></head>"
            "<body style='font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;"
            "padding:0 1rem;line-height:1.6;color:#222'>"
            "<h1 style='font-size:1.4rem'>Нещо се обърка</h1>"
            f"<p>{message}</p><p><a href='/'>Към началната страница</a></p></body></html>")
    return HTMLResponse(page, status_code=500)


@app.middleware("http")
async def request_context(request: Request, call_next):
    code = secrets.token_hex(3).upper()
    ctx_token = REQUEST_ID.set(code)
    uid = _user_id_for_log(request)
    origin_token = AI_ORIGIN.set((request.url.path or "/", uid))
    started = time.monotonic()
    status = 500
    try:
        try:
            response = await call_next(request)
        except Exception as exc:
            log.exception("Срив при %s %s", request.method, request.url.path)
            report_problem("crash", "Срив на сайта",
                           f"{request.method} {request.url.path}\n{type(exc).__name__}: {exc}",
                           code=code)
            response = _crash_response(request, code)
        status = response.status_code
        response.headers["X-Request-ID"] = code
        return response
    finally:
        try:
            path = request.url.path or "/"
            if status >= 500 or not path.startswith(_QUIET_PATHS):
                query = request.url.query
                if query:
                    query = "?" + _HideTokensInAccessLog._TOKEN.sub(r"\1***", query)
                log.log(logging.WARNING if status >= 500 else logging.INFO,
                        "%s %s%s → %d за %dms%s", request.method, path, query, status,
                        (time.monotonic() - started) * 1000,
                        f" user={uid}" if uid else "")
        finally:
            AI_ORIGIN.reset(origin_token)
            REQUEST_ID.reset(ctx_token)


from starlette.exceptions import HTTPException as _StarletteHTTPException
from fastapi.exception_handlers import http_exception_handler as _default_http_handler


@app.exception_handler(_StarletteHTTPException)
async def _http_error_with_code(request: Request, exc: _StarletteHTTPException):
    """5xx от нас (Stripe не отговаря, AI е долу…) носят кода на заявката, за
    да може клиентът да ни го прати. 4xx остават непокътнати — 402 носи
    офертата като обект и страницата разчита на нея."""
    if exc.status_code >= 500 and isinstance(exc.detail, str):
        code = REQUEST_ID.get()
        log.warning("%d: %s", exc.status_code, exc.detail)
        return JSONResponse({"detail": f"{exc.detail} (код {code})"},
                            status_code=exc.status_code, headers=getattr(exc, "headers", None))
    return await _default_http_handler(request, exc)


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

def validate_birth(year, month, day, hour, minute, lat, lon, timezone) -> None:
    """Отказва невъзможни рождени данни още при въвеждане.

    Досега 31.02 се записваше; после картата даваше 500, а негодният човек
    заемаше място в лимита и се показваше в списъците.
    """
    import math
    try:
        datetime.date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        raise HTTPException(400, "Невалидна дата на раждане — провери деня и месеца.")
    if not 1800 <= int(year) <= 2200:
        raise HTTPException(400, "Годината на раждане трябва да е между 1800 и 2200.")
    try:
        hour_i, minute_i = int(hour or 0), int(minute or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "Невалиден час на раждане.")
    if not (0 <= hour_i <= 23 and 0 <= minute_i <= 59):
        raise HTTPException(400, "Невалиден час на раждане (00:00 – 23:59).")
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        raise HTTPException(400, "Невалидни координати на мястото.")
    if not (math.isfinite(lat_f) and math.isfinite(lon_f)
            and -90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        raise HTTPException(400, "Невалидни координати на мястото.")
    try:
        ZoneInfo(timezone or "Europe/Sofia")
    except Exception:
        raise HTTPException(400, "Невалидна часова зона.")


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
MIN_PASSWORD_LEN = 8


def check_new_password(password: str) -> None:
    """Само за нови пароли. Входът нарочно не я вика: вече регистрираните с
    по-къса парола трябва да могат да влязат, както досега."""
    if len((password or "").strip()) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"Паролата трябва да е поне {MIN_PASSWORD_LEN} символа.")
    if len((password or "").encode()) > _BCRYPT_MAX_BYTES:
        raise HTTPException(400, "Паролата е твърде дълга (до около 70 латински "
                                 "или 35 букви на кирилица).")


# bcrypt чете само първите 72 байта. До версия 5 отрязваше мълчаливо, а от 5
# хвърля грешка — дълга парола (над ~36 букви на кирилица) даваше 500 при вход
# и регистрация. Отрязваме сами, точно както са направени старите хешове.
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    return bcrypt.hashpw((password or "").encode()[:_BCRYPT_MAX_BYTES], bcrypt.gensalt()).decode()

def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw((password or "").encode()[:_BCRYPT_MAX_BYTES],
                              (password_hash or "").encode())
    except ValueError:
        return False


_DUMMY_HASH: Optional[str] = None

def _dummy_password_hash() -> str:
    """Хеш за сравнение, когато имейлът не съществува (изравнява времето)."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH

def _token_version(user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT token_version FROM users WHERE id = ?", (user_id,)).fetchone()
    return int(row[0] or 0) if row else 0


def create_token(user_id: int, email: str) -> str:
    expire = datetime.datetime.utcnow() + datetime.timedelta(minutes=TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": expire,
        # Версията на акаунта в момента на издаване. Щом се вдигне (нова
        # парола, блокиране, нов имейл), този токен спира да важи.
        "tv": _token_version(user_id),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def bump_token_version(user_id: int, conn: Optional[sqlite3.Connection] = None) -> None:
    """Отменя всички издадени досега токени на акаунта."""
    sql = "UPDATE users SET token_version = COALESCE(token_version, 0) + 1 WHERE id = ?"
    if conn is not None:
        conn.execute(sql, (user_id,))
        return
    with sqlite3.connect(DB_PATH) as own:
        own.execute(sql, (user_id,))
        own.commit()


def user_for_token(token: Optional[str]) -> dict:
    """Акаунтът зад токена — или HTTPException, ако не може да се ползва.

    Една проверка за всички пътища (API, страници, аудио): подписът и срокът,
    дали акаунтът още съществува, дали токенът не е отменен и дали акаунтът
    не е блокиран. Досега се гледаше само подписът — блокиран потребител и
    стар токен след смяна на паролата продължаваха да работят 30 дни.
    """
    if not token:
        raise HTTPException(401, "Не си влязъл в профила си. Влез отново.")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
    except (JWTError, KeyError, TypeError, ValueError):
        raise HTTPException(401, "Сесията изтече. Влез отново.")
    row = get_user_by_id(user_id)
    if not row:
        raise HTTPException(401, "Невалиден акаунт.")
    try:
        token_ver = int(payload.get("tv", 0) or 0)
    except (TypeError, ValueError):
        token_ver = -1
    if token_ver != int(row.get("token_version") or 0):
        raise HTTPException(401, "Сесията изтече. Влез отново.")
    if row.get("is_blocked"):
        raise HTTPException(403, "Акаунтът е блокиран.")
    return row

# --- Rate limiting за login (brute-force защита, in-memory) ---
import time as _time
_LOGIN_FAILURES: dict = {}
_LOGIN_WINDOW = 900      # прозорец 15 мин
_LOGIN_MAX_FAILS = 5     # max провалени опита
_LOGIN_LOCKOUT = 900     # блокиране 15 мин

_LOGIN_IP_MAX_FAILS = 30  # грешни опита от един IP за всички имейли (15 мин)

def _login_blocked(key: str, limit: int = _LOGIN_MAX_FAILS) -> bool:
    now = _time.monotonic()
    fails = [t for t in _LOGIN_FAILURES.get(key, []) if now - t < _LOGIN_WINDOW]
    return len(fails) >= limit

def _login_record_failure(key: str) -> None:
    now = _time.monotonic()
    # Всеки случаен имейл отваря нов ключ; без чистене речникът расте
    # безкрайно. Изхвърляме изтеклите, когато станат много.
    if len(_LOGIN_FAILURES) > 5000:
        for stale in [k for k, v in list(_LOGIN_FAILURES.items())
                      if not v or now - v[-1] >= _LOGIN_WINDOW]:
            _LOGIN_FAILURES.pop(stale, None)
    _LOGIN_FAILURES[key] = [t for t in _LOGIN_FAILURES.get(key, []) if now - t < _LOGIN_WINDOW]
    _LOGIN_FAILURES[key].append(now)

def _login_clear(key: str) -> None:
    _LOGIN_FAILURES.pop(key, None)


# --- Ограничения на опитите извън входа (in-memory) ---
# (брой, прозорец в секунди). Щедри са нарочно: мобилните оператори пускат
# много хора през един IP, а една спряна истинска регистрация струва повече
# от няколко пропуснати спам акаунта.
RATE_LIMITS = {
    "signup": (20, 3600),        # регистрации от един IP на час
    "guest_chart": (30, 600),    # безплатни карти от един IP за 10 мин
    "reset_ip": (10, 3600),      # писма за нова парола от един IP на час
    "reset_email": (3, 3600),    # писма за нова парола до един адрес на час
    "geocode": (60, 600),        # търсения на място без вход от един IP за 10 мин
}
RATE_HITS: dict = {}
_RATE_LOCK = threading.Lock()


def client_ip(request: Request) -> str:
    """IP на посетителя. Празно, когато не може да се установи със сигурност.

    Coolify праща всяка заявка през своето прокси, затова прекият адрес е
    вътрешен (10.x) и еднакъв за всички. Проксито добавя истинския IP най-
    отдясно в X-Forwarded-For; всичко вляво може да е написано от клиента.
    На заглавката се вярва само когато заявката идва от вътрешната мрежа.
    """
    import ipaddress
    peer = request.client.host if request.client else ""
    try:
        behind_proxy = ipaddress.ip_address(peer).is_private
    except ValueError:
        behind_proxy = False
    if not behind_proxy:
        return peer
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[-1].strip()
    try:
        ipaddress.ip_address(forwarded)
    except ValueError:
        # Без заглавката всички посетители биха делили един брояч и биха се
        # спрели взаимно — по-добре без ограничение по IP.
        return ""
    return forwarded


def rate_allowed(bucket: str, key: str) -> bool:
    """Отбелязва опит и казва дали е в лимита. Празен ключ никога не се спира."""
    if not key:
        return True
    limit, window = RATE_LIMITS[bucket]
    now = _time.monotonic()
    with _RATE_LOCK:
        hits = [t for t in RATE_HITS.get((bucket, key), []) if now - t < window]
        if len(hits) >= limit:
            RATE_HITS[(bucket, key)] = hits
            return False
        hits.append(now)
        RATE_HITS[(bucket, key)] = hits
        return True


def rate_limit(bucket: str, key: str) -> None:
    if not rate_allowed(bucket, key):
        raise HTTPException(429, "Твърде много опити от този адрес. Опитай отново след малко.")


def is_admin_request(request: Request) -> bool:
    """Дали заявката идва от вписан админ. Никога не хвърля грешка."""
    try:
        token = _token_from_request(request)
        if not token:
            return False
        user_id, _ = get_current_user(request=None, token=token)
        row = get_user_by_id(user_id)
        return bool(row and row.get("role") == "admin" and not row.get("is_blocked"))
    except Exception:
        return False

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

def _touch_last_seen(user_id: int) -> None:
    """Обновява last_seen (най-много веднъж на 5 мин) — за „активни потребители"."""
    now = datetime.datetime.utcnow()
    cutoff = (now - datetime.timedelta(minutes=5)).isoformat(timespec="seconds")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE users SET last_seen = ? WHERE id = ? AND (last_seen IS NULL OR last_seen < ?)",
                (now.isoformat(timespec="seconds"), user_id, cutoff),
            )
    except Exception:
        pass


def get_current_user(request: Request, token: Optional[str] = Depends(oauth2_scheme)) -> Tuple[int, str]:
    """Dependency that returns (user_id, email) from valid JWT token."""
    row = user_for_token(token)
    _touch_last_seen(row["id"])
    return row["id"], row["email"]

def get_current_user_flex(request: Request) -> Tuple[int, str]:
    """JWT от Authorization header, ?token= или miralog_token cookie.

    Нужен за <audio>/<img> тагове (напр. гласово четене), които не могат да
    слагат Authorization header — там токенът идва през cookie.
    """
    row = user_for_token(_token_from_request(request))
    _touch_last_seen(row["id"])
    return row["id"], row["email"]

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


def is_paying_customer(user_id: int) -> bool:
    """Има ли поне един модул извън даденото на всички при регистрация."""
    return any(k not in FREE_ON_SIGNUP for k in purchased_features(user_id))

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

def _is_admin_id(user_id: int) -> bool:
    row = get_user_by_id(user_id)
    return bool(row and row.get("role") == "admin")


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
            # Пакетът пътува заедно с офертата: това е моментът, в който
            # човекът вече е решил, че иска точно това разчитане. Ако другите
            # му излизат по-евтино наведнъж, редно е да го знае сега, а не да
            # го открие след като е платил един модул на пълна цена.
            bundle = bundle_offer(row) if offer else None
            detail = {
                "reason": "locked",
                "feature": feature,
                "feature_name": name or "",
                "message": message,
                "offer": offer,
                "bundle": bundle,
            }
            raise HTTPException(402, detail)
        return user
    return _check

# --- DB Helpers ---
def get_user_by_email(email: str) -> Optional[dict]:
    """Акаунтът по имейл, без значение от главните букви.

    Телефоните често пишат първата буква главна, а регистрацията пази имейла
    с малки — „Ivan@…“ даваше „грешна парола“. Точното съвпадение печели,
    ако случайно има два акаунта, различни само по регистъра.
    """
    email = (email or "").strip()
    if not email:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM users WHERE lower(email) = lower(?) ORDER BY id LIMIT 1",
                (email,)).fetchone()
        return dict(row) if row else None


_EMAIL_BAD_CHARS = set(' \t\r\n,;:<>()[]\\"')

def valid_email(email: str) -> bool:
    """Строга, но не прекалена проверка на имейл за нови акаунти.

    Старата пускаше „a@x.com,b@y.com“ — писмата после тръгваха към няколко
    адреса наведнъж.
    """
    email = (email or "").strip()
    if not email or len(email) > 254 or email.count("@") != 1:
        return False
    if any(ch in _EMAIL_BAD_CHARS for ch in email):
        return False
    local, domain = email.split("@")
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return False
    if ".." in domain or len(domain.rsplit(".", 1)[-1]) < 2:
        return False
    return True

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
    """Invalidate all cached AI interpretations for a person (e.g. after birth data changes).

    Синастрията се пази под единия от двамата — при промяна на данните на
    другия също трябва да отпадне (ключът е synastry:<по-малкия>:<по-големия>).
    """
    pid = int(person_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM ai_cache WHERE person_id = ?", (pid,))
        conn.execute("DELETE FROM ai_cache WHERE cache_key LIKE ? OR cache_key LIKE ?",
                     (f"synastry:{pid}:%", f"synastry:%:{pid}"))
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

class AINotConfigured(RuntimeError):
    """Няма AI ключ — разчитането не може да се генерира."""


def ai_config_or_raise() -> Tuple[str, str]:
    """(ключ, доставчик) — или грешка, вместо тихо „нищо“.

    Тихото връщане оставяше фоновата задача „успешна“ без текст: всяко
    запитване пускаше нова и страницата показваше „пише се…“ с минути.
    """
    key, provider = get_ai_config()
    if not key:
        raise AINotConfigured("AI ключът не е зададен (Админ → Настройки → AI).")
    return key, provider


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
    # Истинският IP, а не адресът на проксито на Coolify: с него всички
    # посетители деляха един брояч и всеки можеше да заключи чужд акаунт.
    ip = client_ip(request)
    key = f"{email_key}|{ip}"
    ip_key = f"ip|{ip}" if ip else ""
    if _login_blocked(key) or (ip_key and _login_blocked(ip_key, _LOGIN_IP_MAX_FAILS)):
        raise HTTPException(429, "Твърде много неуспешни опити. Опитай отново след 15 минути.")

    user = get_user_by_email(email_key)
    if not user:
        # Същото време като при грешна парола, за да не се познава по
        # скоростта дали имейлът е регистриран.
        verify_password(data.password or "", _dummy_password_hash())
    if not user or not verify_password(data.password or "", user["password_hash"]):
        _login_record_failure(key)
        if ip_key:
            _login_record_failure(ip_key)
        raise HTTPException(401, "Грешен имейл или парола.")

    if user.get("is_blocked"):
        raise HTTPException(403, "Този акаунт е блокиран. Пиши ни, ако смяташ, че е грешка.")

    if user.get("totp_secret"):
        # Паролата вече е вярна, затова лимитът е по акаунт, не по IP: иначе
        # 6-цифреният код се налучква от много адреси без край.
        totp_key = f"totp|{user['id']}"
        if _login_blocked(totp_key):
            raise HTTPException(429, "Твърде много грешни кодове. Опитай отново след 15 минути.")
        code = (data.totp_code or "").strip()
        if not code:
            raise HTTPException(401, {
                "reason": "totp_required",
                "message": "Въведи 6-цифрения код от приложението за удостоверяване.",
            })
        if not verify_totp(user["totp_secret"], code):
            _login_record_failure(totp_key)
            raise HTTPException(401, {
                "reason": "totp_invalid",
                "message": "Невалиден код за двуфакторна автентикация.",
            })
        _login_clear(totp_key)

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
def api_guest_chart(data: GuestChartRequest, request: Request):
    """Compute a chart for somebody who has not signed up yet.

    Nothing is stored: the browser keeps the birth details and asks again on
    the next visit. Asking for an email before showing anything is the point
    where casual visitors leave, so the chart comes first and the account
    comes after they have seen it.
    """
    # Изчислението е тежко и не иска вход — без таван един скрипт би заел
    # процесора за всички останали.
    rate_limit("guest_chart", client_ip(request))
    if not (data.name or "").strip():
        raise HTTPException(400, "Моля, въведи име.")
    validate_birth(data.year, data.month, data.day, data.hour, data.minute,
                   data.lat, data.lon, data.timezone)
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
    if not valid_email(email):
        raise HTTPException(400, "Моля, въведи валиден имейл адрес.")
    if not (data.name or "").strip():
        raise HTTPException(400, "Моля, въведи име.")
    # Преди акаунтът да се създаде — иначе грешната дата оставя акаунт без карта.
    validate_birth(data.year, data.month, data.day, data.hour, data.minute,
                   data.lat, data.lon, data.timezone)

    rate_limit("signup", client_ip(request))

    existing = get_user_by_email(email)
    if existing:
        # Never silently attach a chart to somebody else's account.
        raise HTTPException(409, {
            "reason": "account_exists",
            "message": "Вече има акаунт с този имейл. Влез и създай картата оттам.",
        })

    chose_password = bool((data.password or "").strip())
    if chose_password:
        check_new_password(data.password)

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
    grant_signup_features(user["id"])

    # Това е най-честият път за регистрация — от началната страница, а не от
    # /register. Известието стоеше само на другите два и затова не тръгваше.
    audit("register", f"Нова регистрация: {email}", user_id=user["id"], actor=email)
    notify_new_user(user["id"], email, "натална карта")

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
                bundle=is_bundle,
            )
        except Exception as e:
            # Мълчаливият провал беше по-лош от самата грешка: човекът се
            # озоваваше на картата си, без изобщо да го питат за пари, а от
            # админа изглеждаше като регистрация без поръчка. Сега клиентът
            # научава, а изборът се запомня, за да плати от картата.
            log.warning("Onboarding checkout за %s се провали: %s", email, e, exc_info=True)
            result["checkout_error"] = (
                "Профилът е готов, но плащането не можа да се подготви. "
                "Избраните разчитания те чакат в картата — опитай оттам.")
            remember_pending_purchase(user["id"], wanted, is_bundle)
    elif wanted:
        # Нито тестов режим, нито Stripe: акаунтът е направен, но няма как да
        # се плати. Изборът пак се пази, за да не се губи.
        result["checkout_error"] = (
            "Профилът е готов. Плащанията са временно изключени — "
            "избраните разчитания те чакат в картата.")
        remember_pending_purchase(user["id"], wanted, is_bundle)
    result["wanted"] = wanted
    result["bundle"] = is_bundle
    return result


@app.post("/api/auth/register")
def api_register(data: AuthRequest, request: Request):
    """Create an account. Each user only ever sees their own people."""
    email = (data.email or "").strip().lower()
    if not valid_email(email):
        raise HTTPException(400, "Моля, въведете валиден имейл адрес.")
    check_new_password(data.password or "")
    rate_limit("signup", client_ip(request))
    if get_user_by_email(email):
        raise HTTPException(409, "Вече съществува акаунт с този имейл.")

    user = create_user(email, hash_password(data.password))
    grant_signup_features(user["id"])
    token = create_token(user["id"], user["email"])
    audit("register", f"Нова регистрация: {email}", user_id=user["id"], actor=email)
    notify_new_user(user["id"], email, "имейл и парола")
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
    "receipt_subject": "Документ за продажба от {brand} — №{unp}",
    "receipt_body": (
        "Здравей!\n\n"
        "Благодарим за покупката! Документът за регистриране на продажбата "
        "е прикачен към това писмо като PDF. Можеш да го свалиш и от "
        "Настройки → Моите документи.\n\n"
        "Поздрави,\n{brand}"
    ),
    # Известие до собственика при нова регистрация. Шаблонът е тук, за да
    # може да се редактира от админ панела като всички останали.
    "new_user_subject": "Нов потребител: {email}",
    "new_user_body": (
        "Нова регистрация в {brand}.\n\n"
        "Имейл: {email}\n"
        "Начин: {method}\n"
        "Кога: {when}\n"
        "Общо потребители: {total}\n"
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
            out.append(f"<h3><span>{num.group(1)}</span> {inline(num.group(2))}</h3>")
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
{{"mood": "една дума за настроението на деня", "energy": "Висока|Средна|Ниска", "do": ["3 къси съществителни фрази по 2-4 думи — НЕЩА, не заповеди (напр. „спокойни разговори“, „важни решения“); НЕ пиши „провери“, „изчакай“"], "avoid": ["2-3 къси съществителни фрази по 2-4 думи (напр. „спорове с близки“, „прибързани обещания“)"], "focus": "фокусът на деня", "caution": "едно кратко изречение в какво да внимава"}}
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

    ai_key, provider = ai_config_or_raise()
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

    ai_key, provider = ai_config_or_raise()
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

    ai_key, provider = ai_config_or_raise()
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

    ai_key, provider = ai_config_or_raise()
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

    ai_key, provider = ai_config_or_raise()
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
            "SELECT COALESCE(SUM(amount_cents), 0) s FROM payments WHERE voided_at IS NULL"
        ).fetchone()["s"]
        month_start = datetime.date.today().replace(day=1).isoformat()
        revenue_month = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) s FROM payments"
            " WHERE paid_at >= ? AND voided_at IS NULL",
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
            "FROM payments p LEFT JOIN users u ON u.id = p.user_id "
            "WHERE p.voided_at IS NULL "
            "ORDER BY p.paid_at DESC LIMIT 10"
        )]

        # Активност: прегледи на страници + скорошни регистрации + активни потребители.
        now = datetime.datetime.utcnow()
        day_ago = (now - datetime.timedelta(days=1)).isoformat(timespec="seconds")
        week_ago = (now - datetime.timedelta(days=7)).isoformat(timespec="seconds")
        views_24h = conn.execute(
            "SELECT COUNT(*) c FROM page_views WHERE viewed_at >= ?", (day_ago,)
        ).fetchone()["c"]
        views_7d = conn.execute(
            "SELECT COUNT(*) c FROM page_views WHERE viewed_at >= ?", (week_ago,)
        ).fetchone()["c"]
        views_total = conn.execute("SELECT COUNT(*) c FROM page_views").fetchone()["c"]
        top_pages = [dict(r) for r in conn.execute(
            "SELECT path, COUNT(*) c FROM page_views WHERE viewed_at >= ? "
            "GROUP BY path ORDER BY c DESC LIMIT 10", (week_ago,)
        )]
        recent_registrations = [dict(r) for r in conn.execute(
            "SELECT u.id, u.email, u.created_at, u.last_seen, "
            "(SELECT COUNT(*) FROM persons p WHERE p.user_id = u.id) AS persons "
            "FROM users u ORDER BY u.created_at DESC LIMIT 10"
        )]
        active_users = [dict(r) for r in conn.execute(
            "SELECT id, email, last_seen FROM users "
            "WHERE last_seen IS NOT NULL AND last_seen >= ? "
            "ORDER BY last_seen DESC LIMIT 20", (day_ago,)
        )]
        active_users_7d = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE last_seen IS NOT NULL AND last_seen >= ?",
            (week_ago,)
        ).fetchone()["c"]
    return {
        "users": users, "blocked": blocked, "persons": persons,
        "by_plan": by_plan,
        "revenue_cents": revenue, "revenue_month_cents": revenue_month,
        "top_modules": top_modules, "recent_payments": recent,
        "activity": {
            "views_24h": views_24h, "views_7d": views_7d, "views_total": views_total,
            "top_pages": top_pages,
            "recent_registrations": recent_registrations,
            "active_users": active_users, "active_users_7d": active_users_7d,
        },
        # Checkout without a webhook secret takes money and unlocks nothing,
        # which is invisible from outside — so it is reported here.
        "payments_health": {
            "checkout_key": billing.checkout_key_present(),
            "webhook_secret": billing.webhook_secret_present(),
            "ready": billing.stripe_enabled(),
        },
        # Копие, което никой не проверява, е копие, което го няма. Панелът
        # показва кога е последното и колко са — иначе се разбира чак когато
        # някой поиска да възстанови нещо.
        "backups": backup_status(),
        # Без SMTP не тръгват нито фактурите, нито възстановяването на парола.
        "email_ready": bool(smtp_setting("smtp_host")),
        "versions": package_versions(),
    }

@app.get("/api/admin/users")
def api_admin_users(q: Optional[str] = None, admin: dict = Depends(require_admin)):
    """All accounts, with their plan and usage."""
    sql = ("SELECT u.id, u.email, u.role, u.plan_key, u.plan_expires, u.is_blocked, u.note, "
           "u.created_at, u.last_login, "
           "(SELECT COUNT(*) FROM persons p WHERE p.user_id = u.id) AS persons, "
           "(SELECT COALESCE(SUM(amount_cents),0) FROM payments pm"
           " WHERE pm.user_id = u.id AND pm.voided_at IS NULL) AS paid_cents "
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
    if not valid_email(email):
        raise HTTPException(400, "Моля, въведете валиден имейл адрес.")
    check_new_password(data.password or "")
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
        new_id = cur.lastrowid
    # Като всеки друг нов акаунт: картата и дневният хороскоп са безплатни.
    # Досега ги даваше само попълването при старт — до следващия деплой
    # ръчно създаденият акаунт не виждаше собствената си карта.
    grant_signup_features(new_id)
    return {"ok": True, "id": new_id, "email": email}

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
        check_new_password(data.password)
        sets.append("password_hash = ?"); params.append(hash_password(data.password.strip()))

    if not sets:
        return {"ok": True, "changed": False}

    # Нова парола или блокиране прекъсват всички сесии на акаунта: иначе
    # отключването по-късно би съживило стари (може би откраднати) токени.
    revoke = bool((data.password or "").strip()) or bool(data.is_blocked)

    params.append(user_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
        if revoke:
            bump_token_version(user_id, conn)
        conn.commit()
    audit("user_updated", f"Променен потребител {target['email']} (id={user_id})",
          user_id=user_id, actor=admin["email"])
    result = {"ok": True, "changed": True}
    if revoke and target["id"] == admin["id"]:
        result["token"] = create_token(admin["id"], admin["email"])
    return result

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
        # Плащанията и фактурите остават (виж api_delete_account) — само се
        # отделят от изтрития акаунт по номер.
        # Покупките и връзките към Google/Facebook също са на този акаунт.
        # SQLite не налага външните ключове (foreign_keys е изключен по
        # подразбиране), затова остават като сираци — а ако по-късно нов
        # акаунт получи същото id, наследява платените модули безплатно.
        conn.execute("DELETE FROM feature_purchases WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM oauth_accounts WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM share_links WHERE user_id = ?", (user_id,))
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
    # LEFT JOIN: плащанията на изтрит акаунт остават в дневника (без имейл).
    sql = ("SELECT p.*, u.email,"
           " (SELECT COALESCE(SUM(r.amount_cents), 0) FROM payment_refunds r"
           "  WHERE r.payment_id = p.id) AS refunded_cents,"
           " (SELECT number FROM invoices i WHERE i.payment_id = p.id ORDER BY i.id LIMIT 1)"
           "  AS invoice_number"
           " FROM payments p LEFT JOIN users u ON u.id = p.user_id")
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
    if (data.method or "").strip().lower() == "stripe":
        # „stripe“ значи онлайн продажба с документ по Н-18 — такава влиза в
        # одиторския файл. Ръчен запис без документ там няма място.
        raise HTTPException(400, "Методът „stripe“ е запазен за плащанията, които идват "
                                 "сами от Stripe. За ръчно плащане напиши банка, карта или в брой.")

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

class PaymentVoid(BaseModel):
    reason: Optional[str] = None


class PaymentRefund(BaseModel):
    amount_cents: Optional[int] = None      # празно = целият остатък
    method: str = "card"                     # account / card / cash / other
    note: Optional[str] = None


def _admin_payment(payment_id: int) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Плащането не е намерено.")
    return dict(row)


@app.post("/api/admin/payments/{payment_id}/void")
def api_admin_void_payment(payment_id: int, data: Optional[PaymentVoid] = None,
                           admin: dict = Depends(require_admin)):
    """Анулира грешно въведено плащане. Записът остава — само не се брои.

    Досега имаше „Изтрий“: плащането изчезваше от дневника, от вече подаден
    одиторски файл и от защитата срещу повторна обработка на Stripe сесията
    (след изтриване същата сесия отключваше модулите и издаваше нова фактура).
    За върнати пари е „Върнати пари“, не анулиране.
    """
    payment = _admin_payment(payment_id)
    if payment.get("voided_at"):
        return {"ok": True, "already": True}
    reason = ((data.reason if data else "") or "").strip()[:300] or None
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE payments SET voided_at = CURRENT_TIMESTAMP, void_reason = ?"
                     " WHERE id = ?", (reason, payment_id))
        conn.commit()
    audit("payment_voided", f"Анулирано плащане #{payment_id}: {reason or 'без причина'}",
          user_id=payment["user_id"], actor=admin["email"])
    return {"ok": True}


@app.delete("/api/admin/payments/{payment_id}")
def api_admin_delete_payment(payment_id: int, admin: dict = Depends(require_admin)):
    """Стар адрес: вече анулира, не трие (фискалният запис трябва да остане)."""
    return api_admin_void_payment(payment_id, PaymentVoid(reason="анулирано (стар бутон „Изтрий“)"),
                                  admin=admin)


@app.post("/api/admin/payments/{payment_id}/refund")
def api_admin_refund_payment(payment_id: int, data: PaymentRefund,
                             admin: dict = Depends(require_admin)):
    """Отбелязва върнати пари (напр. по банков път или в брой).

    Връщанията, направени в Stripe, се отбелязват сами от webhook-а —
    тук е за всичко останало. Модулите не се отнемат автоматично; за това е
    бутонът за отнемане.
    """
    payment = _admin_payment(payment_id)
    if payment.get("voided_at"):
        # Анулираното плащане не е продажба (не е и в одиторския файл) —
        # връщане по него би стояло във файла без поръчката си.
        raise HTTPException(400, "Плащането е анулирано — по него няма какво да се връща.")
    if data.method not in REFUND_METHODS:
        raise HTTPException(400, "Начинът на връщане трябва да е account, card, cash или other.")
    if data.amount_cents is None and "amount_cents" in (getattr(data, "model_fields_set", None) or set()):
        # Изрично null идва от неуспешно прочетена сума (NaN в JSON е null) —
        # не бива тихо да стане връщане на целия остатък. Без поле = остатъкът.
        raise HTTPException(400, "Сумата не е число. Остави полето празно за целия остатък.")
    remaining = int(payment["amount_cents"]) - refunded_cents(payment_id)
    amount = remaining if data.amount_cents is None else int(data.amount_cents)
    if amount <= 0 or amount > remaining:
        raise HTTPException(400, f"Сумата трябва да е между 0.01 и {remaining / 100:.2f}.")
    add_refund(payment_id, amount, method=data.method, source="admin",
               note=(data.note or "").strip()[:300], recorded_by=admin["id"])
    audit("payment_refunded",
          f"Отбелязано връщане {amount / 100:.2f} {payment['currency']} по плащане #{payment_id}",
          user_id=payment["user_id"], actor=admin["email"])
    return {"ok": True, "refunded_cents": refunded_cents(payment_id)}


@app.post("/api/admin/payments/{payment_id}/resend-documents")
def api_admin_resend_documents(payment_id: int, admin: dict = Depends(require_admin)):
    """Изпраща отново касовия документ и фактурата (същия номер на фактурата)."""
    payment = _admin_payment(payment_id)
    if payment.get("method") != "stripe":
        raise HTTPException(400, "Документи се изпращат само за онлайн плащания.")
    if not payment_items(payment_id):
        raise HTTPException(400, "Плащането е отпреди редовете да се пазят — документите "
                                 "не могат да се възстановят автоматично.")
    user = get_user_by_id(payment["user_id"]) or {}
    email = (user.get("email") or "").strip()
    if not email:
        raise HTTPException(400, "Акаунтът на купувача е изтрит — няма адрес за изпращане.")
    if not smtp_setting("smtp_host"):
        raise HTTPException(400, "SMTP сървърът не е конфигуриран.")
    send_sale_documents_for_payment(payment_id, email)
    audit("documents_resent", f"Документите за плащане #{payment_id} са пратени отново до {email}",
          user_id=payment["user_id"], actor=admin["email"])
    return {"ok": True, "email": email}

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

ADMIN_LOG_MAX_LINES = 2000
_LOG_ENTRY_START = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")


@app.get("/api/admin/logs")
def api_admin_logs(q: Optional[str] = None, level: Optional[str] = None,
                   limit: int = 300, admin: dict = Depends(require_admin)):
    """Търсене в логовете от data/logs — най-новото първо.

    `q` е код на заявка (от съобщението, което клиентът вижда), `user=42` или
    произволен текст. `level=warning` оставя само проблемите. Един запис е
    редът с часа плюс продълженията му (traceback), затова се връщат цели.
    """
    limit = max(1, min(int(limit or 300), ADMIN_LOG_MAX_LINES))
    needle = (q or "").strip().lower()
    problems_only = (level or "").strip().lower() in ("warning", "error", "problems")
    files = sorted(LOG_DIR.glob("app.log*"), key=lambda p: p.stat().st_mtime, reverse=True)
    picked, count = [], 0
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        entries, cur = [], []
        for line in text.splitlines():
            if _LOG_ENTRY_START.match(line) and cur:
                entries.append(cur)
                cur = [line]
            else:
                cur.append(line)
        if cur:
            entries.append(cur)
        for entry in reversed(entries):
            head = entry[0]
            if problems_only and not (" WARNING " in head or " ERROR " in head):
                continue
            if needle and needle not in "\n".join(entry).lower():
                continue
            picked.append(entry)
            count += len(entry)
            if count >= limit:
                break
        if count >= limit:
            break
    lines = [line for entry in picked for line in entry][:limit]
    return {"lines": lines, "files": len(files), "keep_days": LOG_KEEP_DAYS}


@app.get("/api/admin/daily-summary")
def api_admin_daily_summary(admin: dict = Depends(require_admin)):
    """Текстът на сутрешното писмо за вчера — само показва, нищо не праща."""
    yesterday = datetime.datetime.now(ZoneInfo("Europe/Sofia")).date() - datetime.timedelta(days=1)
    return {"text": build_daily_summary(yesterday),
            "to": notify_address(), "enabled": notify_enabled("daily")}


@app.get("/api/admin/ai-usage")
def api_admin_ai_usage(days: int = 30, admin: dict = Depends(require_admin)):
    """Колко токена и пари отиват за AI — общо, по източник, модул, модел,
    ден и клиент. `days=0` е за целия период."""
    where, params = "", []
    if days and days > 0:
        since = (datetime.datetime.utcnow() - datetime.timedelta(days=int(days)))
        where, params = " WHERE at >= ?", [since.isoformat(timespec="seconds")]
    agg = ("COUNT(*) calls, SUM(1 - ok) failed, COALESCE(SUM(input_tokens),0) input_tokens,"
           " COALESCE(SUM(cached_tokens),0) cached_tokens, COALESCE(SUM(output_tokens),0) output_tokens,"
           " COALESCE(SUM(cost_usd),0) cost_usd, COALESCE(AVG(duration_ms),0) avg_ms")
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        def rows(sql):
            return [dict(r) for r in conn.execute(sql, params)]

        totals = dict(conn.execute(f"SELECT {agg}, MIN(at) since FROM ai_usage{where}", params).fetchone())
        by_source = rows(f"SELECT source, {agg} FROM ai_usage{where} GROUP BY source ORDER BY cost_usd DESC")
        by_feature = rows(f"SELECT source, feature, {agg} FROM ai_usage{where}"
                          " GROUP BY source, feature ORDER BY cost_usd DESC")
        by_model = rows(f"SELECT provider, model, {agg} FROM ai_usage{where}"
                        " GROUP BY provider, model ORDER BY cost_usd DESC")
        by_day = rows(f"SELECT substr(at, 1, 10) day, {agg} FROM ai_usage{where}"
                      " GROUP BY day ORDER BY day DESC LIMIT 60")
        top_cond = "a.user_id IS NOT NULL AND a.source = 'client'" + (" AND a.at >= ?" if where else "")
        top_users = [dict(r) for r in conn.execute(
            "SELECT a.user_id, u.email, COUNT(*) calls, COALESCE(SUM(a.cost_usd),0) cost_usd,"
            " COALESCE(SUM(a.input_tokens + a.cached_tokens + a.output_tokens),0) tokens"
            " FROM ai_usage a LEFT JOIN users u ON u.id = a.user_id"
            f" WHERE {top_cond} GROUP BY a.user_id ORDER BY cost_usd DESC LIMIT 20", params)]
    return {
        "days": days, "totals": totals, "by_source": by_source, "by_feature": by_feature,
        "by_model": by_model, "by_day": by_day, "top_users": top_users,
        "prices": {m: {"input": p[0], "cached": p[1], "output": p[2]} for m, p in AI_PRICES.items()},
    }


# --- Одиторски файл по Н-18 (Приложение № 38): генериране и автоматизация ---
SAFT_DIR = DB_PATH.parent / "saft"
SAFT_DEADLINE_DAY = 15      # до 15-о число на следващия месец (чл. 52т, ал. 3)


def _month_key(year: int, month: int) -> str:
    return f"{int(year):04d}-{int(month):02d}"


def _parse_month_key(key: str) -> Tuple[int, int]:
    try:
        year, month = (int(x) for x in str(key).split("-"))
        if 2020 <= year <= 2100 and 1 <= month <= 12:
            return year, month
    except ValueError:
        pass
    raise HTTPException(400, "Месецът трябва да е във формат ГГГГ-ММ.")


def _month_bounds_utc(year: int, month: int) -> Tuple[str, str]:
    """Календарният месец по българско време, като граници в UTC — така се пази
    времето в базата. Досега краят беше „ГГГГ-ММ-31“ в UTC: продажбите на 31-во
    число изчезваха от файла, а тези между 00 и 03 ч. отиваха в грешен месец."""
    start = datetime.datetime(year, month, 1, tzinfo=SOFIA_TZ)
    nxt = datetime.datetime(year + (month == 12), 1 if month == 12 else month + 1, 1,
                            tzinfo=SOFIA_TZ)
    fmt = lambda d: d.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return fmt(start), fmt(nxt)


def saft_deadline(year: int, month: int) -> datetime.date:
    nxt_y, nxt_m = (year + 1, 1) if month == 12 else (year, month + 1)
    return datetime.date(nxt_y, nxt_m, SAFT_DEADLINE_DAY)


def _legacy_sale_lines(payment: dict) -> list:
    """Плащане отпреди редовете да се пазят: както в издадения тогава документ —
    сумата поравно между модулите, но вече точно до стотинка."""
    keys, _ = _parse_payment_note(payment.get("note") or "")
    names = {f["key"]: f["name"] for f in FEATURE_CATALOGUE}
    keys = keys or ["online"]
    parts = allocate_cents(int(payment["amount_cents"]), [1] * len(keys))
    vats = split_vat_over_lines(parts, saft.VAT_RATE)
    return [{"name": names.get(k) or (feature_offer(k) or {}).get("name") or "Онлайн услуга",
             "quant": 1, "sum_cents": c, "vat_rate": saft.VAT_RATE, "vat_cents": v}
            for k, c, v in zip(keys, parts, vats)]


def _month_saft_data(year: int, month: int) -> dict:
    """Какво влиза в одиторския файл за месеца — без самия XML.

    Влизат онлайн продажбите (Stripe, paym 4) без анулираните; връщанията —
    в месеца, в който парите са върнати (и само по неанулирани продажби).
    Ръчно отбелязаните плащания не влизат автоматично: изброяват се в отчета,
    за да реши собственикът/счетоводителят.
    """
    lg = legal()
    start, end = _month_bounds_utc(year, month)
    in_month = "datetime({col}) >= datetime(?) AND datetime({col}) < datetime(?)"
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        payments = [dict(r) for r in conn.execute(
            "SELECT * FROM payments WHERE method = 'stripe' AND voided_at IS NULL AND "
            + in_month.format(col="paid_at") + " ORDER BY datetime(paid_at), id", (start, end))]
        manual = [dict(r) for r in conn.execute(
            "SELECT id, amount_cents, currency, method, note, paid_at FROM payments"
            " WHERE COALESCE(method, '') NOT IN ('stripe', 'тест') AND voided_at IS NULL AND "
            + in_month.format(col="paid_at") + " ORDER BY datetime(paid_at)", (start, end))]
        refunds = [dict(r) for r in conn.execute(
            "SELECT r.* FROM payment_refunds r JOIN payments p ON p.id = r.payment_id"
            " WHERE p.method = 'stripe' AND p.voided_at IS NULL AND "
            + in_month.format(col="r.refunded_at")
            + " ORDER BY datetime(r.refunded_at), r.id", (start, end))]

    psp_id = (lg.get("psp_id") or "").strip()
    orders = []
    for p in payments:
        items = payment_items(p["id"])
        lines = ([{"name": it["name"], "quant": 1, "sum_cents": it["amount_cents"],
                   "vat_rate": it["vat_rate"], "vat_cents": it["vat_cents"]} for it in items]
                 or _legacy_sale_lines(p))
        doc = sale_document(p["id"])
        paid_local = utc_to_sofia(p["paid_at"])
        doc_local = utc_to_sofia(doc["issued_at"]) if doc else paid_local
        orders.append({
            "ord_n": str(p["id"]),
            "ord_d": paid_local.strftime("%Y-%m-%d"),
            # Онлайн плащане без нов номер е записано от по-стара версия и
            # документът му е издаден тогава с номера на плащането. Новата
            # номерация винаги минава над тези номера (_NEXT_SALE_DOC_NUMBER).
            "doc_n": doc["number"] if doc else p["id"],
            "doc_date": doc_local.strftime("%Y-%m-%d"),
            "items": lines,
            "disc_cents": 0,
            "paym": saft.PAYM_PSP,
            "pos_n": "",
            "trans_n": p.get("payment_intent") or p.get("stripe_session_id") or "",
            "proc_id": psp_id,
        })

    merged = {}
    for r in refunds:
        m = merged.setdefault(r["payment_id"], {"ord_n": str(r["payment_id"]), "amount_cents": 0})
        m["amount_cents"] += int(r["amount_cents"])
        m["date"] = utc_to_sofia(r["refunded_at"]).strftime("%Y-%m-%d")
        m["paym"] = saft.REFUND_METHOD_CODES.get(r["method"], saft.RPAYM_OTHER)
    refund_list = list(merged.values())

    # Отпечатък на данните (без датата на създаване) — показва дали нещо в
    # месеца се е променило след генерирането, т.е. дали файлът трябва наново.
    fingerprint = hashlib.sha256(json.dumps(
        {"orders": orders, "refunds": refund_list,
         "legal": [lg.get("company_id"), lg.get("e_shop_n"), psp_id]},
        sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    return {"legal": lg, "orders": orders, "refunds": refund_list, "manual": manual,
            "total_cents": sum(int(p["amount_cents"]) for p in payments),
            "fingerprint": fingerprint}


def saft_fingerprint(year: int, month: int) -> str:
    """Отпечатъкът на месеца без XML и проверка срещу схемата (бързо)."""
    return _month_saft_data(year, month)["fingerprint"]


def build_month_saft(year: int, month: int) -> dict:
    """Одиторският файл за месец + отчет: поръчки, суми, проблеми."""
    data = _month_saft_data(year, month)
    lg, orders, refund_list = data["legal"], data["orders"], data["refunds"]
    xml_bytes = saft.build_saft_xml(
        eik=(lg.get("company_id") or "").strip(),
        e_shop_n=(lg.get("e_shop_n") or "").strip(),
        domain_name=BRAND_DOMAIN,
        e_shop_type=lg.get("e_shop_type", "1") or "1",
        month=month, year=year, orders=orders, refunds=refund_list,
        creation_date=datetime.datetime.now(SOFIA_TZ).strftime("%Y-%m-%d"),
    )
    problems = saft.validate_saft(xml_bytes)
    return {
        "key": _month_key(year, month),
        "xml": xml_bytes,
        "orders": len(orders),
        "total_cents": data["total_cents"],
        "refunds": len(refund_list),
        "refund_cents": sum(r["amount_cents"] for r in refund_list),
        "manual": data["manual"],
        "problems": problems,
        "valid": not problems,
        "fingerprint": data["fingerprint"],
        "deadline": saft_deadline(year, month).isoformat(),
    }


def saft_month_activity(year: int, month: int) -> Tuple[int, int]:
    """(онлайн продажби, връщания) през месеца — без анулираните."""
    start, end = _month_bounds_utc(year, month)
    with sqlite3.connect(DB_PATH) as conn:
        sales = conn.execute(
            "SELECT COUNT(*) FROM payments WHERE method = 'stripe' AND voided_at IS NULL"
            " AND datetime(paid_at) >= datetime(?) AND datetime(paid_at) < datetime(?)",
            (start, end)).fetchone()[0]
        refunds = conn.execute(
            "SELECT COUNT(*) FROM payment_refunds r JOIN payments p ON p.id = r.payment_id"
            " WHERE p.method = 'stripe' AND p.voided_at IS NULL"
            " AND datetime(r.refunded_at) >= datetime(?) AND datetime(r.refunded_at) < datetime(?)",
            (start, end)).fetchone()[0]
    return int(sales), int(refunds)


def saft_meta(key: str) -> Optional[dict]:
    raw = get_setting(f"saft:{key}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _save_saft_meta(key: str, meta: dict) -> None:
    set_setting(f"saft:{key}", json.dumps(meta, ensure_ascii=False))


def generate_and_store_saft(year: int, month: int) -> dict:
    """Генерира файла, записва го в data/saft и пази отчета в настройките."""
    report = build_month_saft(year, month)
    key = report["key"]
    SAFT_DIR.mkdir(parents=True, exist_ok=True)
    target = SAFT_DIR / f"saft-{key}.xml"
    tmp = target.with_suffix(".part")
    tmp.write_bytes(report["xml"])
    os.replace(tmp, target)
    meta = saft_meta(key) or {}
    if report["orders"] or report["refunds"]:
        # Автоматиката е отбелязала месеца като „без продажби“, а после нещо
        # се е появило (напр. късно върнати пари) — файлът вече е истински.
        meta.pop("no_sales", None)
    meta.update({
        "generated_at": datetime.datetime.now(SOFIA_TZ).strftime("%Y-%m-%d %H:%M"),
        "orders": report["orders"], "total_cents": report["total_cents"],
        "refunds": report["refunds"], "refund_cents": report["refund_cents"],
        "manual": len(report["manual"]), "problems": report["problems"],
        "valid": report["valid"], "fingerprint": report["fingerprint"],
        "deadline": report["deadline"],
    })
    _save_saft_meta(key, meta)
    return report


def _saft_email_body(report: dict, year: int, month: int) -> str:
    key = report["key"]
    deadline = datetime.date.fromisoformat(report["deadline"]).strftime("%d.%m.%Y")
    lines = [f"Одиторски файл (Н-18) за {month:02d}.{year}",
             "",
             f"Поръчки: {report['orders']} на обща стойност {report['total_cents'] / 100:.2f} EUR",
             f"Върнати: {report['refunds']} на стойност {report['refund_cents'] / 100:.2f} EUR",
             ""]
    if report["valid"]:
        lines += ["Файлът е проверен срещу схемата на НАП и аритметиката — прикачен е към писмото.",
                  "",
                  f"Подай го до {deadline} през е-услугата на НАП „Подаване на стандартизиран "
                  "одиторски файл от лице по чл. 3, ал. 17“ с КЕП. Може да се подава "
                  "повторно до срока — важи последният.",
                  "",
                  f"След подаването го отбележи като подаден: https://{ADMIN_HOST}/admin → НАП (Н-18)."]
    else:
        lines += ["ФАЙЛЪТ НЕ Е ГОТОВ за подаване:", ""] + [f"  • {p}" for p in report["problems"]] + [
                  "", f"Оправи горното и натисни „Генерирай отново“ в https://{ADMIN_HOST}/admin → НАП (Н-18).",
                  f"Срок за подаване: {deadline}."]
    if report["manual"]:
        lines += ["", f"Ръчно отбелязани плащания през месеца (не са във файла): {len(report['manual'])}.",
                  "Провери със счетоводителя дали трябва да влязат (код на плащане 5)."]
    return "\n".join(lines)


def run_saft_automation(now: Optional[datetime.datetime] = None) -> None:
    """Всеки месец: файл за предишния месец + напомняне преди срока.

    Вика се от часовия фонов цикъл. След 6:00 на 1-во число генерира и
    проверява файла за изтеклия месец и го праща на собственика. Ако три дни
    преди срока още не е отбелязан като подаден — едно напомняне. Нищо не се
    подава автоматично: НАП приема файла само през портала с КЕП.

    Минава веднъж за месеца със собствена отметка (auto_at): файл, генериран
    ръчно, докато месецът още е течал, е непълен и не бива да я спира.
    """
    now = now or datetime.datetime.now(SOFIA_TZ)
    if now.hour < 6:
        return
    year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    key = _month_key(year, month)
    meta = saft_meta(key) or {}
    to = notify_address()
    can_mail = bool(to and smtp_setting("smtp_host") and notify_enabled("saft"))
    stamp = now.strftime("%Y-%m-%d %H:%M")

    if not meta.get("auto_at"):
        sales, refunds = saft_month_activity(year, month)
        if not sales and not refunds:
            # Схемата на НАП не допуска файл без поръчки — няма какво да се
            # генерира, а напомняне за несъществуващ файл само плаши.
            meta.update({"auto_at": stamp, "no_sales": True,
                         "deadline": saft_deadline(year, month).isoformat()})
            _save_saft_meta(key, meta)
            start, _ = _month_bounds_utc(year, month)
            with sqlite3.connect(DB_PATH) as conn:
                sold_before = conn.execute(
                    "SELECT 1 FROM payments WHERE method = 'stripe'"
                    " AND datetime(paid_at) < datetime(?) LIMIT 1", (start,)).fetchone()
            if can_mail and sold_before:
                body = (f"През {month:02d}.{year} няма онлайн продажби и връщания, затова "
                        "одиторски файл не е създаден: схемата на НАП не допуска файл без поръчки.\n\n"
                        "Потвърди със счетоводителя, че за месец без продажби не се подава нищо.")
                try:
                    send_email(to, f"{brand_name()}: няма продажби през {month:02d}.{year}",
                               body, html=_email_html(body))
                    meta["emailed_at"] = stamp
                    _save_saft_meta(key, meta)
                except Exception as e:
                    log.warning("Писмото за месец без продажби %s не тръгна: %s", key, e)
            return
        if meta.get("submitted_at"):
            # Генериран и подаден ръчно след края на месеца — не се пипа.
            meta["auto_at"] = stamp
            _save_saft_meta(key, meta)
            return
        report = generate_and_store_saft(year, month)
        meta = saft_meta(key) or {}
        meta["auto_at"] = stamp
        _save_saft_meta(key, meta)
        log.info("Одиторски файл %s: %s поръчки, валиден=%s", key, report["orders"], report["valid"])
        if can_mail:
            subject = (f"Одиторски файл за {month:02d}.{year} е готов" if report["valid"]
                       else f"Одиторският файл за {month:02d}.{year} НЕ е готов")
            body = _saft_email_body(report, year, month)
            attachment = (f"saft-{key}.xml", report["xml"], "application/xml") if report["valid"] else None
            try:
                send_email(to, f"{brand_name()}: {subject}", body, attachment=attachment,
                           html=_email_html(body))
                meta["emailed_at"] = stamp
                _save_saft_meta(key, meta)
            except Exception as e:
                log.warning("Писмото с одиторския файл %s не тръгна: %s", key, e)
        return

    if meta.get("no_sales") or not meta.get("generated_at"):
        return
    deadline = datetime.date.fromisoformat(meta.get("deadline") or saft_deadline(year, month).isoformat())
    days_left = (deadline - now.date()).days
    if (not meta.get("submitted_at") and 0 <= days_left <= 3 and not meta.get("reminded_at")
            and can_mail):
        body = (f"Одиторският файл за {month:02d}.{year} още не е отбелязан като подаден.\n\n"
                f"Срокът е {deadline.strftime('%d.%m.%Y')}. Файлът е в https://{ADMIN_HOST}/admin → "
                "НАП (Н-18); подава се през е-услугата на НАП с КЕП.\n\n"
                "Ако вече е подаден, отбележи го там и напомнянето спира.")
        try:
            send_email(to, f"{brand_name()}: напомняне — одиторски файл до "
                           f"{deadline.strftime('%d.%m.%Y')}", body, html=_email_html(body))
            meta["reminded_at"] = stamp
            _save_saft_meta(key, meta)
        except Exception as e:
            log.warning("Напомнянето за одиторския файл %s не тръгна: %s", key, e)


@app.get("/api/admin/saft")
def api_admin_saft(year: int, month: int, admin: dict = Depends(require_admin)):
    """Генерира Стандартизиран одиторски файл (SAF-T) за даден месец.

    Наредба № Н-18, чл. 3, ал. 17 и Приложение № 38. Файл с проблеми не се
    сваля (422 със списъка) — подаден в НАП, би бил отхвърлен или грешен.
    """
    if not (1 <= int(month) <= 12):
        raise HTTPException(400, "Месецът трябва да е между 1 и 12.")
    if int(year) < 2020:
        raise HTTPException(400, "Годината трябва да е поне 2020.")
    report = build_month_saft(int(year), int(month))
    if not report["valid"]:
        raise HTTPException(422, {"reason": "saft_invalid", "problems": report["problems"]})
    filename = f"saft-{report['key']}.xml"
    return Response(
        content=report["xml"],
        media_type="application/xml; charset=windows-1251",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/admin/saft/months")
def api_admin_saft_months(admin: dict = Depends(require_admin)):
    """Месеците с продажби: генериран ли е файлът, валиден ли е, подаден ли е,
    променило ли се е нещо след генерирането."""
    with sqlite3.connect(DB_PATH) as conn:
        first = conn.execute("SELECT MIN(paid_at) FROM payments WHERE method = 'stripe'").fetchone()[0]
    now = datetime.datetime.now(SOFIA_TZ)
    months = []
    if first:
        cur = utc_to_sofia(first)
        # Последните 36 месеца (най-новите, не първите): по-старите са
        # отдавна подадени, а списъкът не бива да спира да расте нагоре.
        index = max(cur.year * 12 + cur.month - 1, now.year * 12 + now.month - 1 - 35)
        y, m = divmod(index, 12)
        m += 1
        while (y, m) <= (now.year, now.month):
            key = _month_key(y, m)
            meta = saft_meta(key) or {}
            in_progress = (y, m) == (now.year, now.month)
            sales, refunds = saft_month_activity(y, m)
            entry = {"key": key, "label": f"{m:02d}.{y}", "in_progress": in_progress,
                     "deadline": saft_deadline(y, m).isoformat(), **meta,
                     "sales": sales, "refunds_count": refunds}
            if meta.get("generated_at") and not in_progress:
                # Само данните, без XML и схема — иначе табът строеше и
                # проверяваше файла на всеки месец при всяко отваряне.
                entry["stale"] = saft_fingerprint(y, m) != meta.get("fingerprint")
            entry["file"] = (SAFT_DIR / f"saft-{key}.xml").exists()
            months.append(entry)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    months.reverse()
    return {"months": months, "deadline_day": SAFT_DEADLINE_DAY}


@app.post("/api/admin/saft/{key}/generate")
def api_admin_saft_generate(key: str, admin: dict = Depends(require_admin)):
    year, month = _parse_month_key(key)
    report = generate_and_store_saft(year, month)
    audit("saft_generated", f"Одиторски файл {key}: {report['orders']} поръчки, "
                            f"валиден={report['valid']}", actor=admin["email"])
    return {k: v for k, v in report.items() if k != "xml"}


@app.get("/api/admin/saft/{key}/file")
def api_admin_saft_file(key: str, admin: dict = Depends(require_admin)):
    year, month = _parse_month_key(key)
    path = SAFT_DIR / f"saft-{_month_key(year, month)}.xml"
    if not path.exists():
        raise HTTPException(404, "Файлът още не е генериран.")
    meta = saft_meta(_month_key(year, month)) or {}
    if not meta.get("valid"):
        raise HTTPException(422, {"reason": "saft_invalid", "problems": meta.get("problems") or []})
    return Response(content=path.read_bytes(), media_type="application/xml; charset=windows-1251",
                    headers={"Content-Disposition": f'attachment; filename="{path.name}"'})


class SaftSubmitted(BaseModel):
    submitted: bool = True


@app.post("/api/admin/saft/{key}/submitted")
def api_admin_saft_submitted(key: str, data: SaftSubmitted, admin: dict = Depends(require_admin)):
    year, month = _parse_month_key(key)
    key = _month_key(year, month)
    meta = saft_meta(key) or {}
    meta["submitted_at"] = (datetime.datetime.now(SOFIA_TZ).strftime("%Y-%m-%d %H:%M")
                            if data.submitted else None)
    meta["submitted_by"] = admin["email"] if data.submitted else None
    _save_saft_meta(key, meta)
    audit("saft_submitted" if data.submitted else "saft_unsubmitted",
          f"Одиторски файл {key} отбелязан като {'подаден' if data.submitted else 'неподаден'}",
          actor=admin["email"])
    return {"ok": True, **meta}


@app.get("/api/admin/2fa/status")
def api_admin_2fa_status(admin: dict = Depends(require_admin)):
    row = get_user_by_id(admin["id"])
    return {"enabled": bool(row and row.get("totp_secret"))}

@app.post("/api/admin/2fa/setup")
def api_admin_2fa_setup(admin: dict = Depends(require_admin)):
    """Генерира TOTP secret + otpauth URI (за сканиране). Активира се след confirm."""
    secret = generate_totp_secret()
    # По един чакащ ключ на админ: с общ ключ двама админи, включващи 2FA
    # едновременно, си пренаписваха ключа и единият активираше чуждия.
    set_setting(f"totp_pending_secret:{admin['id']}", secret)
    uri = totp_uri(secret, admin["email"], brand_name())
    audit("2fa_setup", "Генериран secret за 2FA", user_id=admin["id"], actor=admin["email"])
    return {"secret": secret, "uri": uri}

@app.post("/api/admin/2fa/confirm")
def api_admin_2fa_confirm(data: dict, admin: dict = Depends(require_admin)):
    """Потвърждава с код от аппа и активира 2FA за админа."""
    pending_key = f"totp_pending_secret:{admin['id']}"
    pending = get_setting(pending_key)
    code = str(data.get("code") or "").strip()
    if not pending or not verify_totp(pending, code):
        raise HTTPException(400, "Невалиден код. Опитай отново.")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET totp_secret = ? WHERE id = ?", (pending, admin["id"]))
        conn.commit()
    set_setting(pending_key, "")
    audit("2fa_enabled", "2FA активирана", user_id=admin["id"], actor=admin["email"])
    return {"ok": True}

@app.post("/api/admin/2fa/disable")
def api_admin_2fa_disable(data: Optional[dict] = None, admin: dict = Depends(require_admin)):
    """Изключването иска текущ код: само токен (напр. откраднат) не стига,
    за да се махне втората защита."""
    row = get_user_by_id(admin["id"]) or {}
    secret = row.get("totp_secret")
    if secret:
        totp_key = f"totp|{admin['id']}"
        if _login_blocked(totp_key):
            raise HTTPException(429, "Твърде много грешни кодове. Опитай отново след 15 минути.")
        code = str((data or {}).get("code") or "").strip()
        if not verify_totp(secret, code):
            _login_record_failure(totp_key)
            raise HTTPException(400, "Въведи валиден 6-цифрен код от приложението, за да изключиш 2FA.")
        _login_clear(totp_key)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET totp_secret = NULL WHERE id = ?", (admin["id"],))
        conn.commit()
    set_setting(f"totp_pending_secret:{admin['id']}", "")
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
        # Какво е избрал при регистрация, ако плащането тогава не е тръгнало.
        # Показва се отметнато, за да не се търси наново.
        "pending": take_pending_purchase(user_id),
    }

class PendingPurchase(BaseModel):
    keys: list = []
    bundle: bool = False


@app.post("/api/features/pending")
def api_remember_pending(data: PendingPurchase, user: Tuple[int, str] = Depends(get_current_user)):
    """Запомня избраните преди вход през Google/Facebook модули.

    Изборът се правеше на началната страница, но след връщането от
    доставчика се губеше: човекът стигаше до заключена карта, без да го
    питаме за плащане. Картата отваря избора с тези модули отметнати.
    """
    user_id, _ = user
    row = get_user_by_id(user_id) or {}
    if data.bundle:
        offer = bundle_offer(row)
        keys = offer["keys"] if offer else []
    else:
        unlocked = set(unlocked_features(row))
        keys = [k for k in (data.keys or []) if isinstance(k, str)
                and feature_offer(k) and k not in unlocked]
    if keys:
        remember_pending_purchase(user_id, keys, bool(data.bundle))
    return {"ok": True, "keys": keys, "bundle": bool(data.bundle and keys)}


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
                bundle=True,
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
            # Кои променливи ги вижда самият процес. „Ключовете са в Coolify“
            # и „приложението ги получава“ са две различни неща — при празен
            # рестарт или сгрешено име тук се вижда веднага кое липсва.
            "env_seen": {name: bool(os.environ.get(name)) for name in _SMTP_ENV.values()},
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
            "google_enabled": oauth_enabled("google"),
            "facebook_enabled": oauth_enabled("facebook"),
            # Какво реално вижда посетителят — ключове И включен бутон.
            "live": oauth_providers(),
            "from_env": {
                "google": bool(os.environ.get("GOOGLE_CLIENT_ID")),
                "facebook": bool(os.environ.get("FACEBOOK_APP_ID")),
            },
        },
        # Values behind the terms, the privacy policy and the N-18 documents.
        # Известия при нова регистрация: адрес и превключвател.
        "notify": {
            "email": get_setting("notify_email") or "",
            "new_users": notify_enabled("new_users"),
            "payments": notify_enabled("payments"),
            "problems": notify_enabled("problems"),
            "daily": notify_enabled("daily"),
            "saft": notify_enabled("saft"),
            "fallback": (smtp_setting("smtp_from") or smtp_setting("smtp_user") or ""),
        },
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
        # Чекбоксовете идват като true/false — празният низ тук би значел
        # „включено“ и щеше да ги прави невъзможни за изключване.
        if key.endswith("_enabled"):
            set_setting(f"oauth_{key}", "1" if value else "0")
            continue
        text = str(value or "").strip()
        # A blank secret means "leave it alone", so saving the form without
        # retyping the secret does not wipe it. Ids are cleared normally.
        if key.endswith("_secret") and not text:
            continue
        set_setting(f"oauth_{key}", text)

    notify = payload.get("notify")
    if isinstance(notify, dict):
        if "email" in notify:
            set_setting("notify_email", str(notify.get("email") or "").strip())
        for kind in ("new_users", "payments", "problems", "daily", "saft"):
            if kind in notify:
                set_setting(f"notify_{kind}", "1" if notify.get(kind) else "0")

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
        log.warning("Имейл „%s“ до %s не тръгна: %s", subject, to, e)
        raise HTTPException(502, f"Изпращането се провали: {e}")
    log.info("Имейл „%s“ изпратен до %s", subject, to)

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
    if not valid_email(to):
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
    if not valid_email(to):
        raise HTTPException(400, "Въведи валиден имейл адрес.")
    if not smtp_setting("smtp_host"):
        raise HTTPException(400, "SMTP сървърът не е конфигуриран.")
    sent = 0
    for kind, fields in _template_preview_data():
        subject, body = render_email_template(kind, **fields)
        send_email(to, subject, body, html=_email_html(body))
        sent += 1
    # Документ за продажба (Н-18) и фактура (ЗДДС) — реалният резултат е PDF.
    items = [{"name": "Нумерология", "net": 3.33, "vat": 0.67, "total": 4.00,
              "vat_rate": 20, "tax_group": "Б", "quant": 1}]
    lg = legal()
    issued = datetime.datetime.now(SOFIA_TZ)
    pdf_bytes = build_receipt_pdf(
        brand=brand_name(), company_name=lg.get("company_name") or "",
        company_id=lg.get("company_id") or "", address=lg.get("address") or "",
        contact=(lg.get("contact") or lg.get("privacy_email") or ""),
        domain=BRAND_DOMAIN, e_shop_n=lg.get("e_shop_n") or "",
        doc_number="0000000000", issued_str=issued.strftime("%d.%m.%Y %H:%M:%S"),
        order_no="ПРИМЕР", trans_ref="pi_preview", items=items,
        net_total=3.33, vat_total=0.67, total=4.00, vat_rate=20,
        payment_method=SALE_PAYMENT_METHOD,
        qr_data=sale_qr_data(lg.get("e_shop_n") or "", "ПРИМЕР", "pi_preview", issued, 400))
    subject, body = render_email_template("receipt", unp="0000000000 (ПРИМЕР)")
    send_email(to, subject, body, attachment=("primer-dokument.pdf", pdf_bytes, "application/pdf"),
               html=_email_html(body))
    send_invoice_email(to, items=items, total2=4.00, invoice_number="0000000000")
    sent += 2
    audit("templates_preview", f"Изпратени {sent} темплейта за преглед до {to}",
          actor=admin["email"])
    return {"ok": True, "sent": sent, "to": to}

# --- Site URL, lifecycle emails, billing fulfillment, password reset, share ---

def site_base_url(request: Optional[Request] = None) -> str:
    """Адресът за линковете в имейлите и към Stripe.

    В продукция никога от заявката: зад проксито на Coolify тя е http://, а
    Host заглавката идва от клиента — линк за нова парола с чужд домейн би
    пратил токена другаде. Фоновите задачи (без заявка) получаваха
    http://127.0.0.1:8000 и линковете в писмата не водеха никъде.
    """
    configured = (seo_settings().get("seo_site_url") or "").rstrip("/")
    if configured:
        return configured
    if IS_PRODUCTION:
        domain = (brand().get("domain") or BRAND_DOMAIN).strip().strip("/")
        return f"https://{domain}"
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

def notify_new_user(user_id: int, email: str, method: str) -> None:
    """Известява собственика за нова регистрация.

    Адресът се взима от настройката „notify_email“; ако е празна, пада към
    подателя на SMTP. Никога не хвърля — регистрацията не бива да се проваля,
    защото известието не е тръгнало.
    """
    try:
        to = notify_address()
        if not to or not notify_enabled("new_users"):
            return
        with sqlite3.connect(DB_PATH) as conn:
            total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        when = datetime.datetime.now(ZoneInfo("Europe/Sofia")).strftime("%d.%m.%Y %H:%M")
        # Във фона: SMTP може да чака до 30 s, а човекът се регистрира точно сега.
        _in_background(try_send_template, to, "new_user", brand=brand_name(), email=email,
                       method=method, when=when, total=total)
    except Exception:
        log.warning("Известието за нов потребител не тръгна", exc_info=True)


# --- Известия до собственика по имейл ---
# Четири вида, всеки се изключва от Админ → Настройки → Известия:
#   new_users — нова регистрация; payments — ново плащане;
#   problems  — срив, отказан webhook, AI не отговаря (по едно писмо на 30 мин за вид);
#   daily     — сутрешно обобщение за вчерашния ден.
NOTIFY_ASYNC = True
PROBLEM_COOLDOWN = 1800
AI_FAILURE_ALERT = (3, 1800)          # толкова грешки за толкова секунди
_PROBLEM_LAST: dict = {}              # вид → (кога е пратено, колко са пропуснати)
_PROBLEM_LOCK = threading.Lock()
_AI_FAILURES: list = []


def _now_monotonic() -> float:
    return time.monotonic()


def notify_enabled(kind: str) -> bool:
    return (get_setting(f"notify_{kind}") or "1").strip() not in ("0", "false", "False")


def notify_address() -> str:
    """Адресът от настройките, иначе подателят на SMTP. Празно, ако няма валиден."""
    raw = (get_setting("notify_email") or "").strip() or \
          (smtp_setting("smtp_from") or smtp_setting("smtp_user") or "").strip()
    m = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", raw or "")
    return m.group(0) if m else ""


def _in_background(fn, *args, **kwargs) -> None:
    """Пуска fn във фонова нишка, с кода на заявката. Грешките само се логват."""
    def run():
        try:
            fn(*args, **kwargs)
        except Exception:
            log.warning("Фоновото изпращане се провали", exc_info=True)
    if not NOTIFY_ASYNC:
        run()
        return
    ctx = contextvars.copy_context()
    threading.Thread(target=lambda: ctx.run(run), daemon=True).start()


def _send_owner_mail(to: str, subject: str, body: str) -> bool:
    if not smtp_setting("smtp_host"):
        return False
    try:
        send_email(to, f"{brand_name()}: {subject}", body, html=_email_html(body))
        return True
    except Exception as e:
        log.warning("Известието „%s“ не тръгна: %s", subject, e)
        return False


def notify_owner(kind: str, subject: str, body: str) -> bool:
    """Писмо до собственика, във фона. Никога не хвърля."""
    try:
        if not notify_enabled(kind):
            return False
        to = notify_address()
        if not to or not smtp_setting("smtp_host"):
            return False
        _in_background(_send_owner_mail, to, subject, body)
        return True
    except Exception:
        log.warning("Известието „%s“ не тръгна", subject, exc_info=True)
        return False


def report_problem(key: str, title: str, detail: str, *, code: Optional[str] = None) -> bool:
    """Писмо за проблем — най-много едно на PROBLEM_COOLDOWN за един вид, за да не
    засипе пощата, ако един бъг гърми на всяка заявка. Пропуснатите се броят."""
    try:
        with _PROBLEM_LOCK:
            last, skipped = _PROBLEM_LAST.get(key, (None, 0))
            now = _now_monotonic()
            if last is not None and now - last < PROBLEM_COOLDOWN:
                _PROBLEM_LAST[key] = (last, skipped + 1)
                return False
            _PROBLEM_LAST[key] = (now, 0)
        when = datetime.datetime.now(ZoneInfo("Europe/Sofia")).strftime("%d.%m.%Y %H:%M")
        body = f"{title}\n\n{detail}\n"
        if code:
            body += f"\nКод на заявката: {code}\nНамери я в Админ → Логове по този код.\n"
        body += f"\nЧас: {when}\nАдмин: https://{ADMIN_HOST}/admin"
        if skipped:
            body += f"\n\nОт предното писмо е имало още {skipped} подобни — без отделно писмо."
        return notify_owner("problems", title, body)
    except Exception:
        log.warning("Докладът за проблем не тръгна", exc_info=True)
        return False


def _note_ai_failure(provider: str, model: str, error: Exception) -> None:
    """Една грешка може да е случайност; няколко за кратко значи, че доставчикът е долу."""
    count, window = AI_FAILURE_ALERT
    now = _now_monotonic()
    with _PROBLEM_LOCK:
        _AI_FAILURES[:] = [t for t in _AI_FAILURES if now - t < window] + [now]
        failures = len(_AI_FAILURES)
    if failures >= count:
        report_problem("ai", "AI доставчикът не отговаря",
                       f"{failures} неуспешни AI извиквания за последните {window // 60} минути.\n"
                       f"Последна грешка ({provider}/{model}): {error}\n\n"
                       "Клиентите виждат „не се получи“ вместо разчитане.")


def notify_payment(user_id: int, amount_cents: int, currency: str, keys: list,
                   session_id: Optional[str]) -> None:
    try:
        row = get_user_by_id(user_id) or {}
        names = {f["key"]: f["name"] for f in FEATURE_CATALOGUE}
        origin = AI_ORIGIN.get()
        via = ("Stripe webhook" if origin and origin[0] == "/api/stripe/webhook"
               else "при връщането на клиента на сайта")
        money = f"{amount_cents / 100:.2f} {currency}"
        body = (f"Ново плащане: {money}\n\n"
                f"Клиент: {row.get('email') or '#' + str(user_id)}\n"
                f"Модули: {', '.join(names.get(k, k) for k in keys)}\n"
                f"Потвърдено: {via}\n"
                f"Stripe сесия: {session_id or '—'}\n\n"
                f"Админ: https://{ADMIN_HOST}/admin")
        notify_owner("payments", f"Ново плащане {money}", body)
    except Exception:
        log.warning("Известието за плащане не тръгна", exc_info=True)


def _log_problem_counts(day: datetime.date) -> Tuple[int, int]:
    """(грешки, предупреждения) в лога за деня — часът в лога е по София."""
    prefix = day.isoformat()
    errors = warnings = 0
    for f in LOG_DIR.glob("app.log*"):
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.startswith(prefix):
                    continue
                if " ERROR " in line:
                    errors += 1
                elif " WARNING " in line:
                    warnings += 1
        except OSError:
            continue
    return errors, warnings


def _usd(v: float) -> str:
    v = v or 0
    return f"${v:.2f}" if v >= 0.01 or v == 0 else f"${v:.4f}"


def build_daily_summary(day: datetime.date) -> str:
    """Текстът на сутрешното писмо за деня `day` (по София)."""
    tz = ZoneInfo("Europe/Sofia")
    start = datetime.datetime.combine(day, datetime.time(0), tz).astimezone(datetime.timezone.utc)
    end = start + datetime.timedelta(days=1)
    rng = (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"))
    month_start = datetime.datetime.combine(day.replace(day=1), datetime.time(0), tz) \
        .astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    in_day = "datetime({col}) >= datetime(?) AND datetime({col}) < datetime(?)"
    names = {f["key"]: f["name"] for f in FEATURE_CATALOGUE}
    today = day + datetime.timedelta(days=1)

    with sqlite3.connect(DB_PATH) as conn:
        regs = [r[0] for r in conn.execute(
            f"SELECT email FROM users WHERE {in_day.format(col='created_at')} ORDER BY created_at", rng)]
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        views = conn.execute(
            f"SELECT COUNT(*) FROM page_views WHERE {in_day.format(col='viewed_at')}", rng).fetchone()[0]
        pays = conn.execute(
            "SELECT p.amount_cents, p.currency, p.note, u.email FROM payments p"
            " LEFT JOIN users u ON u.id = p.user_id WHERE p.method = 'stripe'"
            " AND p.voided_at IS NULL AND "
            + in_day.format(col="p.paid_at") + " ORDER BY p.paid_at", rng).fetchall()
        month = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE method = 'stripe'"
            " AND voided_at IS NULL"
            " AND datetime(paid_at) >= datetime(?) AND datetime(paid_at) < datetime(?)",
            (month_start, rng[1])).fetchone()[0]
        webhooks = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event = 'webhook_received'"
            " AND detail LIKE '%checkout.session.completed%' AND "
            + in_day.format(col="created_at"), rng).fetchone()[0]
        ai = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0), COALESCE(SUM(1 - ok), 0) FROM ai_usage"
            " WHERE " + in_day.format(col="at"), rng).fetchone()
        ai_by = conn.execute(
            "SELECT source, COALESCE(SUM(cost_usd), 0) FROM ai_usage WHERE "
            + in_day.format(col="at") + " GROUP BY source ORDER BY 2 DESC", rng).fetchall()
        signs_ready = conn.execute(
            "SELECT COUNT(*) FROM sign_horoscope WHERE date = ?", (today.isoformat(),)).fetchone()[0]
    errors, warnings = _log_problem_counts(day)

    lines = [f"Обобщение за {day:%d.%m.%Y}", ""]
    lines.append("Потребители")
    lines.append(f"- Нови регистрации: {len(regs)}" + (f" ({', '.join(regs[:10])}"
                 + (f" и още {len(regs) - 10}" if len(regs) > 10 else "") + ")" if regs else ""))
    lines.append(f"- Общо потребители: {total_users}")
    lines.append(f"- Прегледи на страници: {views}")
    lines.append("")
    lines.append("Плащания (Stripe)")
    if pays:
        day_total = sum(p[0] for p in pays)
        lines.append(f"- {len(pays)} плащания, {day_total / 100:.2f} EUR")
        for amount, currency, note, email in pays:
            keys = ((note or "").split(" ")[0].split(":", 1) + [""])[1].split(",")
            mods = ", ".join(names.get(k, k) for k in keys if k)
            lines.append(f"  · {amount / 100:.2f} {currency} — {email}" + (f" — {mods}" if mods else ""))
        if webhooks < len(pays):
            lines.append(f"- ⚠ Stripe webhook е потвърдил {webhooks} от {len(pays)}. Останалите са "
                         "отключени само при връщането на клиента — провери webhook-а в Stripe (Live).")
        else:
            lines.append(f"- Потвърдени от Stripe webhook: {webhooks} от {len(pays)} ✓")
    else:
        lines.append("- Няма плащания")
    lines.append(f"- От началото на месеца: {month / 100:.2f} EUR")
    lines.append("")
    lines.append("AI")
    labels = {"client": "клиенти", "seo": "SEO", "background": "фонови", "admin": "админ"}
    split = " · ".join(f"{labels.get(s, s)} {_usd(c)}" for s, c in ai_by)
    lines.append(f"- {ai[0]} извиквания, {_usd(ai[1])}" + (f" ({split})" if split else "")
                 + (f", {ai[2]} неуспешни" if ai[2] else ""))
    lines.append(f"- Хороскопи по зодия за {today:%d.%m}: {signs_ready}/12 готови"
                 + ("" if signs_ready >= 12 else " ⚠"))
    lines.append("")
    lines.append("Проблеми")
    lines.append(f"- Грешки: {errors} · Предупреждения: {warnings}"
                 + (" — виж Админ → Логове, „само проблеми“" if errors or warnings else ""))
    lines.append("")
    lines.append(f"Админ: https://{ADMIN_HOST}/admin")
    return "\n".join(lines)


DAILY_SUMMARY_HOUR = 8


def maybe_send_daily_summary(now: Optional[datetime.datetime] = None) -> bool:
    """Праща обобщението за вчера веднъж на ден, след 8:00 по София.
    Денят се отбелязва само при успешно изпращане — иначе следващият час пак опитва."""
    now = now or datetime.datetime.now(ZoneInfo("Europe/Sofia"))
    if now.hour < DAILY_SUMMARY_HOUR:
        return False
    today = now.date().isoformat()
    if (get_setting("daily_summary_sent") or "") == today:
        return False
    if not notify_enabled("daily"):
        return False
    to = notify_address()
    if not to or not smtp_setting("smtp_host"):
        return False
    yesterday = now.date() - datetime.timedelta(days=1)
    if _send_owner_mail(to, f"Обобщение за {yesterday:%d.%m.%Y}", build_daily_summary(yesterday)):
        set_setting("daily_summary_sent", today)
        return True
    return False


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
    # Същото и в лога — там стои до останалото от заявката, под нейния код.
    log.info("[%s] %s%s", event, detail, f" user={user_id}" if user_id else "")
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

# Картата е това, за което човекът е дошъл, а дневният хороскоп е причината
# да се върне. И двете са безплатни — но се раздаваха само в /api/onboard,
# затова влезлите през Google или Facebook оставаха с акаунт, който не може
# да види собствената си натална карта.
FREE_ON_SIGNUP = ("chart", "horoscope")


def remember_pending_purchase(user_id: int, keys: list, is_bundle: bool) -> None:
    """Запомня какво е искал човекът, когато плащането не е тръгнало.

    Без това изборът изчезва и трябва да се прави наново — а точно тогава
    повечето хора се отказват. Пази се като настройка, не като покупка:
    нищо не се отключва, само се помни.
    """
    try:
        payload = json.dumps({"keys": list(keys or []), "bundle": bool(is_bundle)},
                             ensure_ascii=False)
        set_setting(f"pending_purchase_{user_id}", payload)
    except Exception:
        log.warning("Неуспешно запомняне на избора за user=%s", user_id, exc_info=True)


def take_pending_purchase(user_id: int) -> Optional[dict]:
    """Връща запомнения избор и го изтрива — ползва се веднъж."""
    raw = get_setting(f"pending_purchase_{user_id}")
    if not raw:
        return None
    set_setting(f"pending_purchase_{user_id}", "")
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if data.get("keys") else None


def grant_signup_features(user_id: int) -> None:
    """Дава безплатните функции на нов акаунт, независимо през кой вход е минал."""
    for key in FREE_ON_SIGNUP:
        grant_feature_purchase(user_id, key, 0, "EUR", None)


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


def allocate_cents(total_cents: int, weights: list) -> list:
    """Разпределя сума по тежести, до стотинка и със същия сбор.

    Метод на най-големия остатък: при 25 € върху 6 модула или при промо код
    сборът на редовете е точно платеното — нито 25.02, нито 24.99.
    """
    n = len(weights)
    if n == 0:
        return []
    w = [max(0, int(x or 0)) for x in weights]
    if sum(w) <= 0:
        w = [1] * n
    total_w = sum(w)
    total_cents = int(total_cents or 0)
    raw = [total_cents * x / total_w for x in w]
    base = [int(r) for r in raw]
    rest = total_cents - sum(base)
    for i in sorted(range(n), key=lambda i: raw[i] - base[i], reverse=True)[:rest]:
        base[i] += 1
    return base


# Едно правило за закръгляне на ДДС — същото, с което одиторският файл
# проверява сумите.
vat_cents_of = saft.vat_cents_of


def split_vat_over_lines(amounts: list, rate: int) -> list:
    """ДДС за всеки ред, така че сборът да е ДДС-ът на цялата поръчка.

    Закръгляне по ред дава 4.18 вместо 4.17 за 25 € в 6 реда; ДДС-ът се смята
    върху общата сума (както във фактурата) и се разпределя до стотинка.
    """
    total_vat = vat_cents_of(sum(int(a) for a in amounts), rate)
    return allocate_cents(total_vat, [int(a) for a in amounts])


def _parse_feature_amounts(raw: str) -> dict:
    out = {}
    for part in (raw or "").split(","):
        key, _, cents = part.partition(":")
        try:
            out[key.strip()] = int(cents)
        except ValueError:
            continue
    return out


def sale_lines(meta: dict, kind: str, keys: list, amount_cents: int,
               subtotal_cents: int) -> Tuple[list, list]:
    """(редове за документите, [(модул, платено)] за достъпа) за една сесия.

    Пакетът е един продаден артикул — „Всички модули“ за 25 € — и така стои
    в бележката и в одиторския файл. Достъпът все пак е по модул, затова
    сумата се разпределя и по ключовете (сборът е точно платеното).
    """
    planned = _parse_feature_amounts(meta.get("feature_amounts") or "")
    weights = [planned.get(k) if planned.get(k) is not None
               else (feature_offer(k) or {}).get("price_cents", 0) for k in keys]
    per_key = allocate_cents(amount_cents, weights)
    grants = list(zip(keys, per_key))

    list_total = subtotal_cents or sum(weights) or amount_cents
    is_bundle = (meta.get("bundle") == "1"
                 or (kind == "features" and not planned and len(keys) > 1
                     and list_total == BUNDLE_PRICE_CENTS))
    names = {f["key"]: f["name"] for f in FEATURE_CATALOGUE}
    if is_bundle:
        included = ", ".join(names.get(k, k) for k in keys)
        name = f"Пакет „{BUNDLE_NAME}“: {included}"
        if len(name) > 200:
            name = name[:197].rstrip(" ,") + "…"
        lines = [{"key": BUNDLE_KEY, "name": name, "list_cents": list_total,
                  "amount_cents": int(amount_cents)}]
    else:
        lines = [{"key": k, "name": names.get(k) or (feature_offer(k) or {}).get("name") or k,
                  "list_cents": int(w or 0), "amount_cents": c}
                 for k, w, c in zip(keys, weights, per_key)]
    return lines, grants


def session_is_paid(session: dict) -> bool:
    """Платена сесия — вкл. поръчка, покрита изцяло с промо код."""
    return (session.get("payment_status") or "") in ("paid", "no_payment_required")


def _payment_by_session(session_id: str) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM payments WHERE stripe_session_id = ?",
                           (session_id,)).fetchone()
    return dict(row) if row else None


@contextmanager
def write_transaction():
    """Транзакция, която заема базата за запис още в началото (BEGIN IMMEDIATE).

    Webhook-ът и връщането на клиента идват понякога едновременно. „Провери и
    запиши“ в обикновена транзакция пуска и двамата да запишат; тук вторият
    чака първия и после вижда записаното от него.
    """
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def store_payment_items(payment_id: int, lines: list) -> None:
    """Записва редовете на продажбата — веднъж; повторно извикване не пипа нищо."""
    rate = saft.VAT_RATE
    vats = split_vat_over_lines([line["amount_cents"] for line in lines], rate)
    with write_transaction() as conn:
        if conn.execute("SELECT 1 FROM payment_items WHERE payment_id = ? LIMIT 1",
                        (payment_id,)).fetchone():
            return
        for line, vat in zip(lines, vats):
            conn.execute(
                "INSERT INTO payment_items (payment_id, feature_key, name, list_cents,"
                " amount_cents, vat_rate, vat_cents) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (payment_id, line["key"], line["name"][:200],
                 int(line.get("list_cents") or 0), int(line["amount_cents"]), rate, vat))


# Следващият номер на документ за продажба (Н-18, чл. 52о, ал. 1, т. 1:
# нараства със стъпка 1 и е уникален за цялата дейност на магазина). Старите
# документи носеха номера на плащането, затова броячът минава и над всяко
# онлайн плащане без нов номер — включително записаното от стар контейнер,
# докато върви деплой. Новите продажби получават номер в същата транзакция,
# в която се записват, така че онлайн плащане без номер е само от стар код.
_NEXT_SALE_DOC_NUMBER = (
    "SELECT MAX("
    " COALESCE((SELECT MAX(number) FROM sale_documents), 0),"
    " CAST(COALESCE((SELECT value FROM settings WHERE key = 'sale_doc_seed'), '0') AS INTEGER),"
    " COALESCE((SELECT MAX(p.id) FROM payments p WHERE p.method = 'stripe' AND p.id != ?"
    "           AND NOT EXISTS (SELECT 1 FROM sale_documents d WHERE d.payment_id = p.id)), 0)"
    ") + 1")


def _issue_sale_document(conn, payment_id: int) -> None:
    """Издава номер на документа в отворена write_transaction()."""
    now = datetime.datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    number = conn.execute(_NEXT_SALE_DOC_NUMBER, (payment_id,)).fetchone()[0]
    conn.execute("INSERT INTO sale_documents (number, payment_id, issued_at) VALUES (?, ?, ?)",
                 (int(number), payment_id, now))


def record_stripe_sale(user_id: int, *, amount_cents: int, currency: str, note: str,
                       session_id: str, intent: Optional[str],
                       discount_cents: int) -> Optional[int]:
    """Записва онлайн продажба и издава номера на документа ѝ — в една транзакция.

    Връща id на новия запис или None, ако сесията вече е записана (значи е
    обработена — не е грешка).
    """
    with write_transaction() as conn:
        if conn.execute("SELECT 1 FROM payments WHERE stripe_session_id = ?",
                        (session_id,)).fetchone():
            return None
        cur = conn.execute(
            "INSERT INTO payments (user_id, plan_key, amount_cents, currency, method, note,"
            " stripe_session_id, payment_intent, discount_cents)"
            " VALUES (?, NULL, ?, ?, 'stripe', ?, ?, ?, ?)",
            (user_id, amount_cents, currency, note, session_id, intent, max(0, int(discount_cents))))
        _issue_sale_document(conn, cur.lastrowid)
        return cur.lastrowid


def payment_items(payment_id: int) -> list:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM payment_items WHERE payment_id = ? ORDER BY id", (payment_id,))]


def fulfill_checkout_session(session: dict) -> None:
    """Apply a completed Stripe Checkout session to the local DB.

    Идва по два пътя (webhook и връщането на клиента), понякога едновременно и
    понякога повторно. Затова всяка стъпка е защитена поотделно:
      1. записът в дневника и номерът на документа — веднъж, в една
         транзакция (уникален stripe_session_id);
      2. достъпът — докато не мине успешно веднъж (granted_at). Ако първият
         опит е прекъснат след записа (напр. „database is locked“), следващият
         довършва отключването, вместо да приеме, че всичко е готово;
      3. документите за клиента — точно веднъж (documents_at се заема атомарно).
    Анулирано плащане не се отключва отново. Плащане, записано от по-стара
    версия (без номер на документ), не получава нов документ — неговият е
    пратен тогава с номера на плащането.
    """
    meta = session.get("metadata") or {}
    kind = meta.get("kind") or ""
    try:
        user_id = int(meta.get("user_id") or session.get("client_reference_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    if not user_id:
        log.warning("Stripe session без user_id: %s", session.get("id"))
        return

    if kind == "features" and meta.get("feature_keys"):
        keys = [k.strip() for k in meta["feature_keys"].split(",") if k.strip()]
        note_head = f"features:{','.join(keys)}"
    elif kind == "feature" and meta.get("feature_key"):
        keys = [meta["feature_key"].strip()]
        note_head = f"feature:{keys[0]}"
    else:
        log.warning("Stripe session %s без модули в metadata (kind=%s)", session.get("id"), kind)
        return

    amount = int(session.get("amount_total") or 0)
    subtotal = int(session.get("amount_subtotal") or amount)
    currency = (session.get("currency") or "eur").upper()
    customer_id = session.get("customer")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")
    intent = session.get("payment_intent")
    if isinstance(intent, dict):
        intent = intent.get("id")
    session_id = session.get("id")

    if not session_id:
        log.warning("Stripe session без id (user=%s) — не се записва", user_id)
        return

    lines, grants = sale_lines(meta, kind, keys, amount, subtotal)

    pay_id = record_stripe_sale(
        user_id, amount_cents=amount, currency=currency, note=f"{note_head} {session_id}",
        session_id=session_id, intent=intent, discount_cents=subtotal - amount)
    first = pay_id is not None
    payment = _payment_by_session(session_id)
    if payment is None:
        log.warning("Stripe сесия %s: няма запис в дневника след опит за запис", session_id)
        return
    pay_id = payment["id"]
    if payment.get("voided_at"):
        log.info("Stripe сесия %s е анулирана — не се отключва отново", session_id)
        return

    # Продажба на тази версия: номерът на документа е издаден заедно със
    # записа. Без номер е плащане от по-стара версия — документът му е пратен
    # тогава и то не получава нито редове, нито нов номер.
    current = sale_document(pay_id) is not None
    if not first and intent and not payment.get("payment_intent"):
        # По-старите записи нямат payment_intent — без него връщане от Stripe
        # не се свързва с плащането.
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE payments SET payment_intent = ? WHERE id = ?"
                         " AND payment_intent IS NULL", (intent, pay_id))
            conn.commit()
    if current:
        store_payment_items(pay_id, lines)

    if not payment.get("granted_at"):
        for key, cents in grants:
            grant_feature_purchase(user_id, key, cents, currency, pay_id)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE payments SET granted_at = CURRENT_TIMESTAMP"
                         " WHERE id = ? AND granted_at IS NULL", (pay_id,))
            conn.commit()
        if not first:
            log.warning("Stripe сесия %s: довършено отключване след прекъснат опит", session_id)
    elif not first:
        # Already handled: a redelivered webhook, or the customer's own
        # return beat it here.
        log.info("Stripe сесия %s вече е обработена", session_id)

    if first:
        if customer_id:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute(
                    "UPDATE users SET stripe_customer_id = COALESCE(stripe_customer_id, ?) WHERE id = ?",
                    (customer_id, user_id))
                conn.commit()
        log.info("Stripe отключване %s за user=%s", keys, user_id)
        audit("payment_succeeded",
              f"Stripe {amount} {currency} за модули {', '.join(keys)} ({session_id})",
              user_id=user_id, actor="stripe")
        audit("feature_unlocked", f"Модули отключени: {', '.join(keys)}",
              user_id=user_id, actor="stripe")
        notify_payment(user_id, amount, currency, keys, session_id)

    # Електронен документ (Н-18, чл. 52о) и фактура — точно веднъж, във фона,
    # за да не чака клиентът (и Stripe) пощенския сървър. Номерът вече е
    # издаден със записа, независимо дали имейлът ще тръгне.
    email = _session_email(session)
    if current and email and _claim_documents(pay_id):
        _in_background(send_sale_documents_for_payment, pay_id, email)

    # A chart bought through onboarding belongs to an account that has no
    # usable password yet; this is the visitor's way in.
    if first and kind == "feature" and keys == ["chart"]:
        send_welcome_set_password(user_id)


REFUND_METHODS = ("account", "card", "cash", "other")


def add_refund(payment_id: int, amount_cents: int, *, method: str, source: str,
               refunded_at: Optional[str] = None, external_id: Optional[str] = None,
               note: str = "", recorded_by: Optional[int] = None) -> bool:
    """Записва върната сума. False, ако вече е записана (същото external_id)."""
    if method not in REFUND_METHODS:
        raise ValueError(f"unknown refund method {method}")
    when = refunded_at or datetime.datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO payment_refunds (payment_id, amount_cents, refunded_at, method,"
                " source, external_id, note, recorded_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (payment_id, int(amount_cents), when, method, source, external_id,
                 note or None, recorded_by))
            conn.commit()
    except sqlite3.IntegrityError:
        return False
    return True


def refunded_cents(payment_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        return int(conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM payment_refunds WHERE payment_id = ?",
            (payment_id,)).fetchone()[0])


def _refund_time(charge: dict, event_created) -> datetime.datetime:
    """Кога са върнати парите (UTC): от самите връщания, ако Stripe ги е
    сложил в обекта, иначе от часа на събитието. Никога от charge.created —
    това е часът на продажбата и връщането би отишло в нейния месец."""
    stamps = []
    try:
        refunds = (charge.get("refunds") or {}).get("data") or []
        stamps = [int(r.get("created") or 0) for r in refunds]
    except (AttributeError, TypeError, ValueError):
        stamps = []
    for created in [max(stamps, default=0), event_created]:
        try:
            if created and int(created) > 0:
                return datetime.datetime.utcfromtimestamp(int(created))
        except (TypeError, ValueError, OverflowError, OSError):
            continue
    return datetime.datetime.utcnow()


def record_stripe_refund(charge: dict, event_id: str, event_created=None) -> None:
    """charge.refunded → разликата спрямо вече записаното, с датата на връщането.

    Stripe праща натрупаната върната сума; записваме само новото, за да не се
    дублира при частични връщания или повторно изпратено събитие. Датата е от
    връщането (или от събитието), за да влезе в одиторския файл за месеца,
    в който парите са върнати.
    """
    intent = charge.get("payment_intent")
    if isinstance(intent, dict):
        intent = intent.get("id")
    payment = None
    if intent:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM payments WHERE payment_intent = ?",
                               (intent,)).fetchone()
            payment = dict(row) if row else None
    if payment is None and intent and billing.checkout_key_present():
        # Плащане отпреди да пазим payment_intent: намираме сесията в Stripe.
        try:
            sessions = billing.get_stripe().checkout.Session.list(payment_intent=intent, limit=1)
            data = _strip_stripe(sessions).get("data") or []
            if data:
                payment = _payment_by_session(data[0].get("id"))
                if payment:
                    with sqlite3.connect(DB_PATH) as conn:
                        conn.execute("UPDATE payments SET payment_intent = ? WHERE id = ?",
                                     (intent, payment["id"]))
                        conn.commit()
        except Exception:
            log.warning("Stripe връщане: сесията за %s не се намери", intent, exc_info=True)
    if payment is None:
        log.warning("Stripe връщане за непознато плащане (payment_intent=%s)", intent)
        report_problem("refund_unmatched", "Връщане в Stripe без плащане в базата",
                       f"payment_intent={intent}. Отбележи връщането ръчно от Админ → Плащания.")
        return
    total_refunded = int(charge.get("amount_refunded") or 0)
    delta = total_refunded - refunded_cents(payment["id"])
    if delta <= 0:
        return
    when = _refund_time(charge, event_created)
    if add_refund(payment["id"], delta, method="card", source="stripe",
                  refunded_at=when.isoformat(sep=" ", timespec="seconds"),
                  external_id=event_id or None):
        # Анулирано плащане не е в одиторския файл, затова и връщането му не
        # влиза там; записва се само за да се вижда, че парите са върнати.
        voided = " (анулирано плащане — извън одиторския файл)" if payment.get("voided_at") else ""
        audit("payment_refunded",
              f"Stripe върна {delta / 100:.2f} {payment['currency']} по плащане #{payment['id']}{voided}",
              user_id=payment["user_id"], actor="stripe")


SOFIA_TZ = ZoneInfo("Europe/Sofia")


def utc_to_sofia(ts) -> datetime.datetime:
    """Записаното в базата (UTC, „ГГГГ-ММ-ДД ЧЧ:ММ:СС“) → местно време."""
    raw = str(ts or "").replace("T", " ")[:19]
    dt = datetime.datetime.fromisoformat(raw) if raw else datetime.datetime.utcnow()
    return dt.replace(tzinfo=datetime.timezone.utc).astimezone(SOFIA_TZ)


def assign_sale_document(payment_id: int) -> dict:
    """Номерът и датата на документа за продажбата — издава се точно веднъж.

    Новите продажби получават номера си още при записа (record_stripe_sale);
    тук е за всичко останало. Номерът е следващият след най-големия използван
    (вкл. старите документи с номера на плащането), затова расте със стъпка 1
    и не се повтаря.
    """
    with write_transaction() as conn:
        if not conn.execute("SELECT 1 FROM sale_documents WHERE payment_id = ?",
                            (payment_id,)).fetchone():
            _issue_sale_document(conn, payment_id)
        row = conn.execute("SELECT number, issued_at FROM sale_documents WHERE payment_id = ?",
                           (payment_id,)).fetchone()
    return {"number": int(row[0]), "issued_at": row[1]}


def sale_document(payment_id: int) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT number, issued_at FROM sale_documents WHERE payment_id = ?",
                           (payment_id,)).fetchone()
    return {"number": int(row[0]), "issued_at": row[1]} if row else None


def sale_qr_data(e_shop_n: str, order_no: str, trans_ref: str,
                 issued: datetime.datetime, total_cents: int) -> str:
    """Съдържанието на QR кода по Приложение № 18а за софтуер по чл. 52т:
    <№ на е-магазина>*< >*<№ на поръчката>*<реф. № на трансакцията>*
    <ГГГГ-ММ-ДД>*<ЧЧ:ММ:СС>*<обща сума>. Второто поле е празно по образеца.
    """
    return "*".join([str(e_shop_n or "").strip(), "", str(order_no), str(trans_ref or ""),
                     issued.strftime("%Y-%m-%d"), issued.strftime("%H:%M:%S"),
                     saft.money(total_cents)])


def _claim_documents(payment_id: int) -> bool:
    """Атомарно „заема“ изпращането на документите — само един път печели."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("UPDATE payments SET documents_at = CURRENT_TIMESTAMP"
                           " WHERE id = ? AND documents_at IS NULL", (payment_id,))
        conn.commit()
        return cur.rowcount == 1


def document_items(payment_id: int) -> list:
    """Редовете за бележката/фактурата от записаната продажба."""
    out = []
    for it in payment_items(payment_id):
        gross = it["amount_cents"] / 100.0
        vat = it["vat_cents"] / 100.0
        out.append({"name": it["name"], "net": round(gross - vat, 2), "vat": vat,
                    "total": gross, "vat_rate": it["vat_rate"]})
    return out


def _session_email(session: dict) -> str:
    """Имейлът на купувача от Stripe Checkout session."""
    cd = session.get("customer_details") or {}
    if isinstance(cd, dict):
        return (cd.get("email") or session.get("customer_email") or "").strip()
    return (session.get("customer_email") or "").strip()

SALE_PAYMENT_METHOD = "Банкова карта — неприсъствено плащане чрез Stripe"


def build_sale_receipt(payment_id: int) -> Tuple[bytes, str, str]:
    """(PDF, име на файла, номер на документа) за записана продажба.

    Всичко идва от записаното при продажбата — номерът и часът на документа,
    редовете, транзакцията — затова повторното генериране (за изпращане пак
    или за сваляне от профила) дава същия документ.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if not row:
        raise ValueError(f"няма плащане {payment_id}")
    payment = dict(row)
    stored = payment_items(payment_id)
    if not stored:
        # Плащане отпреди редовете да се пазят: документът му е издаден тогава
        # с друг номер — нов номер тук би бил втори документ за същата продажба.
        raise ValueError(f"плащане {payment_id} е отпреди документите да се пазят")
    doc = sale_document(payment_id) or assign_sale_document(payment_id)
    issued = utc_to_sofia(doc["issued_at"])
    lg = legal()
    items = []
    for it in stored:
        items.append({"name": it["name"], "tax_group": saft.tax_group(it["vat_rate"]),
                      "quant": 1, "net": (it["amount_cents"] - it["vat_cents"]) / 100.0,
                      "vat": it["vat_cents"] / 100.0, "total": it["amount_cents"] / 100.0})
    total_cents = int(payment["amount_cents"])
    vat_cents = sum(int(it["vat_cents"]) for it in stored)
    doc_number = f"{doc['number']:010d}"
    trans_ref = payment.get("payment_intent") or payment.get("stripe_session_id") or ""
    order_no = str(payment_id)
    logo = BASE_DIR / "static" / "logo-header.png"
    pdf_bytes = build_receipt_pdf(
        brand=brand_name(),
        company_name=lg.get("company_name") or "",
        company_id=lg.get("company_id") or "",
        address=lg.get("address") or "",
        contact=(lg.get("contact") or lg.get("privacy_email") or ""),
        domain=BRAND_DOMAIN, e_shop_n=lg.get("e_shop_n") or "",
        doc_number=doc_number, issued_str=issued.strftime("%d.%m.%Y %H:%M:%S"),
        order_no=order_no, trans_ref=trans_ref,
        items=items, net_total=(total_cents - vat_cents) / 100.0, vat_total=vat_cents / 100.0,
        total=total_cents / 100.0, vat_rate=saft.VAT_RATE,
        payment_method=SALE_PAYMENT_METHOD,
        qr_data=sale_qr_data(lg.get("e_shop_n") or "", order_no, trans_ref, issued, total_cents),
        logo_path=str(logo) if logo.exists() else None)
    filename = f"{brand_slug()}-dokument-za-prodazhba-{doc_number}.pdf"
    return pdf_bytes, filename, doc_number


def send_receipt_email(email: str, *, payment_id: int) -> bool:
    """Изпраща документа за регистриране на продажба (Н-18, чл. 52о, ал. 5)."""
    if not email or not smtp_setting("smtp_host"):
        return False
    try:
        pdf_bytes, filename, doc_number = build_sale_receipt(payment_id)
    except Exception as e:
        log.warning("PDF за документа за продажба %s се провали: %s", payment_id, e, exc_info=True)
        return False
    subject, body = render_email_template("receipt", unp=doc_number)
    try:
        send_email(email, subject, body,
                   attachment=(filename, pdf_bytes, "application/pdf"),
                   html=_email_html(body))
        return True
    except Exception as e:
        log.warning("Неуспешен receipt имейл до %s: %s", email, e)
        return False

def send_invoice_email(email: str, *, items, total2, invoice_number, issued_at=None) -> bool:
    """Изпраща фактура по ЗДДС (чл. 114) като PDF.

    Датата е от издаването (invoices.issued_at): изпратена повторно, фактурата
    е същият документ — със същия номер и същата дата.
    """
    if not email or not smtp_setting("smtp_host"):
        return False
    lg = legal()
    issued = utc_to_sofia(issued_at) if issued_at else datetime.datetime.now(SOFIA_TZ)
    now = issued.strftime("%d.%m.%Y %H:%M:%S")
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

def issue_invoice_for_payment(user_id: int, payment_id: int) -> Tuple[str, str]:
    """(номер, издадена на) за фактурата на плащането — нова само ако още няма.

    Номерът е 10-цифрен от AUTOINCREMENT (чл. 113 ЗДДС): расте и никога не се
    преизползва. Проверката и издаването са в една транзакция — „изпрати
    пак“ едновременно с първото изпращане не издава втора фактура.
    """
    with write_transaction() as conn:
        row = conn.execute("SELECT number, issued_at FROM invoices WHERE payment_id = ?"
                           " AND number IS NOT NULL ORDER BY id LIMIT 1", (payment_id,)).fetchone()
        if row:
            return row[0], row[1]
        cur = conn.execute("INSERT INTO invoices (payment_id, user_id) VALUES (?, ?)",
                           (payment_id, user_id))
        number = f"{cur.lastrowid:010d}"
        conn.execute("UPDATE invoices SET number = ? WHERE id = ?", (number, cur.lastrowid))
        issued_at = conn.execute("SELECT issued_at FROM invoices WHERE id = ?",
                                 (cur.lastrowid,)).fetchone()[0]
        return number, issued_at


def send_sale_documents_for_payment(payment_id: int, email: str) -> None:
    """Касов документ (Н-18) + фактура (ЗДДС) за записана продажба.

    Чете редовете от payment_items, затова документите, дневникът и
    одиторският файл показват едни и същи суми. Повторно извикване (напр.
    „изпрати пак“ от админа) не издава втора фактура за същото плащане.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if not row:
        return
    payment = dict(row)
    items = document_items(payment_id)
    if not items:
        log.warning("Плащане %s без редове — документите не са пратени", payment_id)
        return
    total2 = payment["amount_cents"] / 100.0
    sent = send_receipt_email(email, payment_id=payment_id)
    invoice_number, invoice_issued = issue_invoice_for_payment(payment["user_id"], payment_id)
    sent_invoice = send_invoice_email(email, items=items, total2=total2,
                                      invoice_number=invoice_number, issued_at=invoice_issued)
    if not (sent and sent_invoice):
        log.warning("Документите за плащане %s не тръгнаха изцяло (бележка=%s, фактура=%s)",
                    payment_id, sent, sent_invoice)
        report_problem("sale_documents", "Документите за продажба не тръгнаха",
                       f"Плащане #{payment_id}: бележка={'да' if sent else 'не'}, "
                       f"фактура={'да' if sent_invoice else 'не'}. "
                       "Изпрати ги отново от Админ → Плащания.")


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

def run_digest_emails(now: Optional[datetime.datetime] = None) -> None:
    """Opt-in daily nudge. Uses cached horoscope text when available; never calls AI.

    По българско време и след 8:00 — досега датата беше по UTC и писмото
    тръгваше около 2–3 ч. през нощта, когато хороскопът за деня още го няма.
    """
    now = now or datetime.datetime.now(ZoneInfo("Europe/Sofia"))
    if now.hour < 8:
        return
    today = now.date().isoformat()
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
        # Ключът на хороскопа е по часовата зона на човека.
        try:
            person_today = datetime.datetime.now(
                ZoneInfo(person.get("timezone") or "Europe/Sofia")).date().isoformat()
        except Exception:
            person_today = today
        cache_key = f"horoscope:{person_today}"
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
            # try_send_template не хвърля, а връща False — досега грешката се
            # пропускаше и неизпратеното се отбелязваше като изпратено.
            if not try_send_template(u["email"], "digest", name=name, reading=reading,
                                     date=today):
                log.warning("Digest до %s не тръгна — ще се опита пак в следващия час",
                            u["email"])
                continue
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("UPDATE users SET last_digest_on = ? WHERE id = ?",
                             (today, u["id"]))
                conn.commit()
        except Exception as e:
            log.warning("Digest до %s се провали: %s", u["email"], e)

# Колко дневни копия да се пазят. Базата е малка (стотици килобайти),
# затова седмица назад не тежи и покрива „вчера работеше“.
BACKUP_KEEP_DAYS = int(os.environ.get("BACKUP_KEEP_DAYS", "7"))


class _BackupExists(Exception):
    """Днешното копие вече съществува — не е грешка, само прескача записа."""


def run_db_backup() -> None:
    """Прави дневно копие на базата и трие по-старите от BACKUP_KEEP_DAYS.

    Volume-ът в Coolify пази базата между деплойте, но не пази от повредена
    база, сбъркана миграция или изтрити по погрешка данни. Копието се прави
    през sqlite3 backup API — той е консистентен дори докато се пише, за
    разлика от обикновено копиране на файла.
    """
    backup_dir = DB_PATH.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    target = backup_dir / f"persons-{today}.db"

    tmp = target.with_suffix(".part")
    try:
        if target.exists():
            raise _BackupExists          # днешното вече е направено
        # contextlib.closing, а не самото connect: `with sqlite3.connect(...)`
        # прави commit, но НЕ затваря файла — а докато е отворен, Windows не
        # позволява преименуването и копието се проваля всеки път.
        import contextlib
        with contextlib.closing(sqlite3.connect(DB_PATH)) as src,              contextlib.closing(sqlite3.connect(tmp)) as dst:
            src.backup(dst)
        os.replace(tmp, target)
        log.info("Копие на базата: %s (%.0f KB)", target.name, target.stat().st_size / 1024)
    except _BackupExists:
        pass                             # пропускаме записа, но чистим по-долу
    except Exception:
        log.exception("Копието на базата се провали")
        try:
            tmp.unlink()
        except OSError:
            pass
        return

    cutoff = datetime.date.today() - datetime.timedelta(days=BACKUP_KEEP_DAYS)
    for old_file in backup_dir.glob("persons-*.db"):
        try:
            stamp = datetime.date.fromisoformat(old_file.stem.split("persons-")[1])
        except (ValueError, IndexError):
            continue
        if stamp < cutoff:
            try:
                old_file.unlink()
            except OSError:
                pass


def backup_status() -> dict:
    """Кога е последното копие и колко се пазят — за админ панела."""
    backup_dir = DB_PATH.parent / "backups"
    files = sorted(backup_dir.glob("persons-*.db")) if backup_dir.exists() else []
    if not files:
        return {"count": 0, "latest": None, "size_kb": 0, "age_hours": None}
    latest = files[-1]
    age = (time.time() - latest.stat().st_mtime) / 3600
    return {
        "count": len(files),
        "latest": latest.stem.replace("persons-", ""),
        "size_kb": round(latest.stat().st_size / 1024),
        "age_hours": round(age, 1),
    }


def run_scheduled_jobs() -> None:
    try:
        run_saft_automation()
    except Exception:
        log.exception("Автоматичният одиторски файл (Н-18) се провали")
    run_db_backup()
    run_digest_emails()
    try:
        maybe_send_daily_summary()   # веднъж на ден, между 8 и 9 ч.
    except Exception:
        log.exception("Сутрешното обобщение не тръгна")

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


class OAuthRefused(Exception):
    """Входът през доставчика не може да продължи; `reason` отива в /login?oauth=."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _oauth_link_or_create(provider: str, provider_user_id: str,
                          email: str, display_name: str,
                          email_verified: bool = False) -> dict:
    """Find the account this identity belongs to, creating one if needed.

    Three cases, in order: the identity is already linked; the email matches
    an existing account, so the identity is attached to it rather than making
    a second account for the same person; or nobody is known and we create.

    Свързването по имейл се доверява само на потвърден от доставчика имейл
    (Google връща email_verified). Иначе всеки, който си направи профил с
    чужд адрес, влизаше в чуждия акаунт. Facebook не казва дали имейлът е
    потвърден, затова там съществуващ акаунт не се свързва автоматично.
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

    if email and provider == "google" and not email_verified:
        # Непотвърден имейл не бива нито да отваря чужд акаунт, нито да заема
        # адреса за нов.
        raise OAuthRefused("unverified")

    user = get_user_by_email(email) if email else None
    if user:
        if provider != "google":
            raise OAuthRefused("exists")
        # Първо свързване към заварен акаунт: доставчикът доказа, че имейлът
        # е на този човек, затова всички стари сесии се прекъсват — ако някой
        # е направил акаунта с чужд имейл преди собственика, губи достъпа.
        bump_token_version(user["id"])
        audit("oauth_linked", f"Свързан вход през {provider}: {email}",
              user_id=user["id"], actor=email)
        user = get_user_by_id(user["id"]) or user
    if not user:
        if not email:
            # Facebook can withhold the email; without one there is no way to
            # reach the person or to merge later, so we stop rather than make
            # an unreachable account.
            raise OAuthRefused("noemail")
        # No usable password: this account is reached through the provider,
        # and "forgot password" still works because the email is real.
        user = create_user(email, hash_password(secrets.token_urlsafe(32)))
        if display_name:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("UPDATE users SET display_name = ? WHERE id = ?",
                             (display_name[:80], user["id"]))
                conn.commit()
        grant_signup_features(user["id"])
        audit("sign_up", f"Регистрация през {provider}: {email}",
              user_id=user["id"], actor=email)
        notify_new_user(user["id"], email, provider.capitalize())

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
    safe_next = safe_next_path(next)
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


def safe_next_path(value: str, default: str = "/dashboard") -> str:
    """Само пътища в нашия сайт. „/\\evil.com“ браузърът чете като
    „//evil.com“ — чужд сайт — затова обратната наклонена черта не минава."""
    value = (value or "").strip()
    if (not value.startswith("/") or value.startswith("//") or "\\" in value
            or any(ord(ch) < 32 for ch in value)):
        return default
    parts = urllib.parse.urlsplit(value)
    if parts.scheme or parts.netloc:
        return default
    return value


def _oauth_fail(reason: str):
    """Връща човека на страницата за вход с обяснение.

    Гола JSON грешка на бяла страница е задънена улица — човекът не разбира
    какво стана и няма къде да натисне. Причината пътува като код, а текстът
    се показва от /login."""
    return RedirectResponse(f"/login?oauth={reason}", status_code=302)


@app.get("/api/auth/{provider}/callback", response_class=HTMLResponse)
def api_oauth_callback(provider: str, request: Request,
                       code: str = "", state: str = "", error: str = ""):
    """Where the provider sends the visitor back."""
    if provider not in OAUTH_ENDPOINTS or not oauth_providers().get(provider):
        return _oauth_fail("disabled")

    if error or not code:
        # The visitor cancelled, which is not a failure worth an error page.
        return _oauth_fail("cancelled")

    saved = _oauth_state_take(state)
    if not saved or saved["provider"] != provider:
        # Най-честата причина е бавно влизане (над 10 минути) или рестарт на
        # сървъра, а не атака — затова текстът кани да опита пак.
        return _oauth_fail("expired")

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
        return _oauth_fail("failed")

    provider_user_id = str(profile.get("sub") or profile.get("id") or "")
    if not provider_user_id:
        return _oauth_fail("failed")

    # Google връща email_verified като булева стойност (понякога като низ).
    verified = profile.get("email_verified")
    email_verified = verified is True or str(verified).strip().lower() == "true"
    try:
        user = _oauth_link_or_create(
            provider, provider_user_id,
            profile.get("email") or "",
            profile.get("name") or "",
            email_verified=email_verified)
    except OAuthRefused as refused:
        # Липсващ/непотвърден имейл или заварен акаунт — казваме го на страницата.
        return _oauth_fail(refused.reason)

    if user.get("is_blocked"):
        return _oauth_fail("blocked")

    if user.get("totp_secret"):
        # Входът през доставчик не бива да заобикаля 2FA: първо кодът, после
        # токенът. Предизвикателството е еднократно и живее 5 минути.
        challenge = _totp_challenge_new(user["id"], saved["next"], provider)
        return HTMLResponse(templates.get_template("oauth_done.html").render({
            "request": request,
            "token": None,
            "email": user["email"],
            "next_url": saved["next"],
            "totp_challenge": challenge,
        }))

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
        "totp_challenge": None,
    }))


# --- 2FA след вход през Google/Facebook ---
_TOTP_CHALLENGES: dict = {}
_TOTP_CHALLENGE_TTL = 300
_TOTP_CHALLENGE_TRIES = 5
_TOTP_CHALLENGE_LOCK = threading.Lock()


def _totp_challenge_new(user_id: int, next_url: str, provider: str) -> str:
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _TOTP_CHALLENGE_LOCK:
        for key, value in list(_TOTP_CHALLENGES.items()):
            if now - value["at"] > _TOTP_CHALLENGE_TTL:
                _TOTP_CHALLENGES.pop(key, None)
        _TOTP_CHALLENGES[token] = {"user_id": user_id, "next": next_url,
                                   "provider": provider, "at": now, "tries": 0}
    return token


class TotpChallengeRequest(BaseModel):
    challenge: str
    code: str


@app.post("/api/auth/totp")
def api_auth_totp(data: TotpChallengeRequest):
    """Вторият фактор след вход през доставчик: код срещу токен."""
    with _TOTP_CHALLENGE_LOCK:
        entry = _TOTP_CHALLENGES.get((data.challenge or "").strip())
        if not entry or time.time() - entry["at"] > _TOTP_CHALLENGE_TTL:
            _TOTP_CHALLENGES.pop((data.challenge or "").strip(), None)
            raise HTTPException(401, "Времето за кода изтече. Влез отново.")
    user = get_user_by_id(entry["user_id"])
    if not user or user.get("is_blocked") or not user.get("totp_secret"):
        raise HTTPException(401, "Влез отново.")
    totp_key = f"totp|{user['id']}"
    if _login_blocked(totp_key):
        raise HTTPException(429, "Твърде много грешни кодове. Опитай отново след 15 минути.")
    if not verify_totp(user["totp_secret"], data.code):
        _login_record_failure(totp_key)
        with _TOTP_CHALLENGE_LOCK:
            entry["tries"] += 1
            if entry["tries"] >= _TOTP_CHALLENGE_TRIES:
                _TOTP_CHALLENGES.pop(data.challenge.strip(), None)
        raise HTTPException(401, "Невалиден код за двуфакторна автентикация.")
    with _TOTP_CHALLENGE_LOCK:
        _TOTP_CHALLENGES.pop(data.challenge.strip(), None)
    _login_clear(totp_key)
    token = create_token(user["id"], user["email"])
    audit("login", f"Вход през {entry['provider']} с 2FA: {user['email']}",
          user_id=user["id"], actor=user["email"])
    return {"token": token, "email": user["email"], "next": entry["next"]}


@app.post("/api/auth/forgot-password")
def api_forgot_password(data: ForgotPasswordRequest, request: Request):
    """Always returns ok to avoid email enumeration. Sends a reset link when possible."""
    email = (data.email or "").strip().lower()
    # Без ограничение някой може да засипе чужда пощенска кутия с писма от
    # нашия домейн. Отговорът остава същият, за да не издава дали адресът
    # съществува — просто писмото не тръгва.
    if not (rate_allowed("reset_ip", client_ip(request))
            and rate_allowed("reset_email", email)):
        return {"ok": True}
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
    check_new_password(data.new_password or "")
    user_id = consume_password_reset((data.token or "").strip())
    if not user_id:
        raise HTTPException(400, "Линкът е невалиден или е изтекъл. Заяви нов.")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (hash_password(data.new_password), user_id))
        # Новата парола прекъсва всички стари сесии — включително на някой,
        # който е направил акаунт с чужд имейл преди истинския собственик.
        bump_token_version(user_id, conn)
        conn.commit()
    audit("password_reset", "Паролата е зададена наново по линк от имейл.", user_id=user_id)
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

    paid = session_is_paid(raw)
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


def _event_field(event, name: str):
    """Поле от Stripe събитие: stripe.Event няма dict методи, а липсващ ключ е KeyError."""
    try:
        return event[name]
    except (KeyError, TypeError):
        return None


@app.post("/api/stripe/webhook")
async def api_stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = billing.construct_webhook_event(payload, sig)
    except Exception as e:
        # Подробностите остават в лога — навън само, че е отказано.
        log.warning("Stripe webhook отказан: %s", e)
        # Само ако изглежда като от Stripe: боклук без подпис не заслужава писмо.
        if sig:
            report_problem("webhook", "Stripe webhook е отказан",
                           f"{e}\n\nПлащанията пак се отключват, когато клиентът се върне на "
                           "сайта, но ако затвори страницата преди това — няма да се отключат.\n"
                           "Провери endpoint-а в Stripe (Live) и STRIPE_WEBHOOK_SECRET в Coolify.")
        raise HTTPException(400, "Невалиден webhook.") from e

    etype = event["type"]
    audit("webhook_received", f"Stripe event: {etype}", actor="stripe")
    obj = event["data"]["object"]
    # StripeObject-ът няма dict методи (.get) — изравни рекурсивно в чист dict,
    # иначе fulfill_checkout_session хвърля грешка.
    obj = _strip_stripe(obj)
    try:
        # Only one-off purchases exist now, so the subscription events that
        # used to arrive here have nothing left to update. Работата с базата и
        # пощата върви в нишка — async рутът не бива да спира целия сайт.
        if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            # A session can "complete" while the money is still moving with
            # delayed payment methods; only grant once Stripe says it is paid
            # (then async_payment_succeeded arrives). A basket fully covered
            # by a promo code is "no_payment_required" — also a sale.
            if session_is_paid(obj):
                await asyncio.to_thread(fulfill_checkout_session, obj)
            else:
                log.warning("Stripe session %s приключи без paid статус: %s",
                            obj.get("id"), obj.get("payment_status"))
                audit("webhook_skipped",
                      f"Stripe session {obj.get('id')} не е paid ({obj.get('payment_status')})",
                      actor="stripe")
        elif etype == "charge.refunded":
            # Връщане, направено в Stripe: отбелязва се само, за да влезе в
            # одиторския файл за месеца на връщането (Н-18). Събитието е
            # stripe.Event — не е dict и .get() хвърля грешка, затова [ ].
            await asyncio.to_thread(record_stripe_refund, obj, _event_field(event, "id") or "",
                                    _event_field(event, "created"))
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
    # Споделянето изнася текста навън — като PDF-а и имейла, само при право
    # за модула (отнет или върнат модул не бива да се публикува по линк).
    require_reading_access(user_id, cache_key)
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
    # Отнет или върнат модул: линкът спира, както спират PDF-ът и имейлът.
    # Само проверка при четене — ако модулът се върне, линкът пак работи.
    if not has_reading_access(row["user_id"], row["cache_key"]):
        raise HTTPException(404, "Разчитането вече не е налично.")
    cached = get_ai_cache(row["person_id"], row["cache_key"])
    if not cached:
        raise HTTPException(404, "Разчитането вече не е налично.")
    summary, prose = split_summary(cached["content"])
    title_key = row["cache_key"].split(":")[0]
    title = READING_TITLES.get(title_key, READING_TITLES.get(row["cache_key"], "Разчитане"))
    text = prose or cached["content"]
    return {
        "person_name": row["person_name"],
        "title": title,
        "cache_key": row["cache_key"],
        "summary": summary,
        "content": text,
        # Готов HTML (текстът е escape-нат преди форматирането) — без него
        # споделеното разчитане се виждаше със сурови ** и ##.
        "content_html": _md_to_html(text),
        "generated_at": cached.get("generated_at"),
    }

# --- Account settings (the signed-in user's own profile) ---
# The AI provider and key are installation-wide and live in the admin panel;
# nothing here may touch them.

class AccountUpdate(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None
    # Нужна само при смяна на имейла.
    current_password: Optional[str] = None


def _notify_email_changed(old_email: str, new_email: str) -> None:
    """Писмо до стария адрес: ако смяната не е от собственика, той разбира веднага."""
    try:
        if not old_email or not smtp_setting("smtp_host"):
            return
        body = (f"Имейлът на профила ти в {brand_name()} беше сменен на {new_email}.\n\n"
                "Ако това си ти, няма нужда да правиш нищо. Ако не си, пиши ни "
                "веднага в отговор на това писмо.")
        _in_background(send_email, old_email, f"{brand_name()}: сменен имейл на профила",
                       body, html=_email_html(body))
    except Exception:
        log.warning("Писмото за сменен имейл не тръгна", exc_info=True)

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

    current = get_user_by_id(user_id) or {}
    old_email = current.get("email") or ""
    new_email = None
    if data.email is not None and data.email.strip():
        candidate = data.email.strip().lower()
        # Формата праща имейла винаги; смяна е само когато наистина е друг.
        if candidate != old_email.strip().lower():
            new_email = candidate
    if new_email:
        if not valid_email(new_email):
            raise HTTPException(400, "Моля, въведи валиден имейл адрес.")
        # Имейлът е ключът към акаунта: с него се пише за нова парола. Без
        # паролата откраднат токен ставаше постоянно превземане — смяна на
        # имейла, после „забравена парола“.
        if not verify_password(data.current_password or "", current.get("password_hash") or ""):
            raise HTTPException(403, {
                "reason": "password_required",
                "message": ("За смяна на имейла въведи текущата си парола. Ако "
                            "влизаш с Google/Facebook или по линк и нямаш парола, "
                            "задай си такава от „Забравена парола“."),
            })
        existing = get_user_by_email(new_email)
        if existing and existing["id"] != user_id:
            raise HTTPException(409, "Вече съществува акаунт с този имейл.")
        fields.append("email = ?")
        values.append(new_email)

    if not fields:
        return {"ok": True, "email": old_email,
                "display_name": current.get("display_name") or ""}

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", (*values, user_id))
        if new_email:
            bump_token_version(user_id, conn)
        conn.commit()

    row = get_user_by_id(user_id)
    result = {"ok": True, "email": row.get("email"), "display_name": row.get("display_name") or ""}
    if new_email:
        # Старите сесии (и на други устройства) спират; тази получава нов токен.
        result["token"] = create_token(user_id, row["email"])
        audit("email_changed", f"Имейлът е сменен от {old_email} на {new_email}",
              user_id=user_id, actor=new_email)
        _notify_email_changed(old_email, new_email)
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
    check_new_password(data.new_password or "")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (hash_password(data.new_password), user_id))
        # Сменената парола трябва да изхвърли всеки, който е влязъл със
        # старата — иначе откраднат токен работи още 30 дни.
        bump_token_version(user_id, conn)
        conn.commit()
    audit("password_changed", "Паролата е сменена от профила.", user_id=user_id,
          actor=row.get("email"))
    # Това устройство остава вписано с нов токен.
    return {"ok": True, "token": create_token(user_id, row["email"])}

@app.get("/api/account/documents")
def api_account_documents(user: Tuple[int, str] = Depends(get_current_user)):
    """Документите за продажба на профила — за сваляне по всяко време."""
    user_id, _ = user
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT p.id, p.amount_cents, p.currency, p.paid_at, d.number, d.issued_at,"
            " (SELECT number FROM invoices i WHERE i.payment_id = p.id ORDER BY i.id LIMIT 1)"
            "  AS invoice_number"
            " FROM payments p JOIN sale_documents d ON d.payment_id = p.id"
            " WHERE p.user_id = ? AND p.method = 'stripe' AND p.voided_at IS NULL"
            " ORDER BY d.number DESC", (user_id,))]
    out = []
    for r in rows:
        names = [it["name"] for it in payment_items(r["id"])]
        out.append({
            "payment_id": r["id"],
            "number": f"{int(r['number']):010d}",
            "issued_at": utc_to_sofia(r["issued_at"]).strftime("%d.%m.%Y %H:%M"),
            "amount_cents": r["amount_cents"], "currency": r["currency"],
            "items": names, "invoice_number": r.get("invoice_number"),
        })
    return {"documents": out}


@app.get("/api/account/documents/{payment_id}.pdf")
def api_account_document_pdf(payment_id: int, user: Tuple[int, str] = Depends(get_current_user)):
    user_id, _ = user
    with sqlite3.connect(DB_PATH) as conn:
        owner = conn.execute("SELECT user_id FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if not owner or owner[0] != user_id or not sale_document(payment_id):
        raise HTTPException(404, "Документът не е намерен.")
    pdf_bytes, filename, _ = build_sale_receipt(payment_id)
    quoted = urllib.parse.quote(filename)
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"})


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
        # Плащанията и фактурите остават: счетоводните документи се пазят по
        # закон (ЗСч, Н-18), а без тях одиторският файл за вече подаден месец
        # би се променил. В тях няма лични данни — имейлът е в users и си
        # отива с акаунта; остава само номерът на изтрития акаунт.
        conn.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))
        # Връзките към Google/Facebook пазят имейла — лични данни, които
        # трябва да си отидат заедно с акаунта.
        conn.execute("DELETE FROM oauth_accounts WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM audit_log WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()

    audit("account_deleted", "Изтрит акаунт по искане на потребителя (GDPR чл. 17).")
    return {"ok": True, "deleted": user_id}

# --- Geocoding (place name -> coordinates, via OpenStreetMap Nominatim) ---
from collections import OrderedDict as _OrderedDict

_GEOCODE_CACHE_MAX = 2000
_GEOCODE_MAX_QUERY = 120
_geocode_cache: "_OrderedDict[str, list]" = _OrderedDict()
_geocode_last_call: list = [0.0]  # mutable holder so the helper can update it
# Заявките към Nominatim вървят една по една: без ключалка едновременните
# търсения тръгваха наведнъж и нарушаваха правилото „1 заявка в секунда“ —
# рискът е бан на сървъра от OpenStreetMap.
_GEOCODE_LOCK = threading.Lock()
_GEOCODE_CACHE_LOCK = threading.Lock()   # бърза — само за речника
_TZ_FINDER = None
_TZ_FINDER_LOCK = threading.Lock()


def _timezone_finder():
    """Един TimezoneFinder за процеса — създаването му е бавно и тежко."""
    global _TZ_FINDER
    if _TZ_FINDER is None:
        with _TZ_FINDER_LOCK:
            if _TZ_FINDER is None:
                try:
                    from timezonefinder import TimezoneFinder
                    _TZ_FINDER = TimezoneFinder()
                except Exception:
                    _TZ_FINDER = False
    return _TZ_FINDER or None

def geocode_place(query: str, limit: int = 6) -> list:
    """Look up a place name and return candidate locations with coordinates.

    Nominatim's usage policy requires an identifying User-Agent and at most one
    request per second, so results are cached and calls are spaced out.
    """
    import time
    import urllib.parse
    import urllib.request

    query = query.strip()[:_GEOCODE_MAX_QUERY]
    key = query.lower()
    if not key:
        return []
    with _GEOCODE_CACHE_LOCK:
        if key in _geocode_cache:
            _geocode_cache.move_to_end(key)
            return _geocode_cache[key]
    # Чакаме реда си най-много няколко секунди: ако Nominatim е бавен, по-добре
    # кратък отказ, отколкото всички нишки на сайта да висят в опашката.
    if not _GEOCODE_LOCK.acquire(timeout=6):
        raise HTTPException(503, "Търсенето на място е заето. Опитай пак след малко.")
    try:
        with _GEOCODE_CACHE_LOCK:
            if key in _geocode_cache:          # някой друг току-що го е потърсил
                return _geocode_cache[key]
        results = _geocode_fetch(query, limit)
    finally:
        _GEOCODE_LOCK.release()
    with _GEOCODE_CACHE_LOCK:
        _geocode_cache[key] = results
        while len(_geocode_cache) > _GEOCODE_CACHE_MAX:
            _geocode_cache.popitem(last=False)
    return results


def _geocode_fetch(query: str, limit: int) -> list:
    """Една заявка към Nominatim — вика се само под _GEOCODE_LOCK."""
    import time
    import urllib.parse
    import urllib.request

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
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read())
    except Exception as e:
        log.warning("Търсенето на място „%s“ се провали: %s", query, e)
        raise HTTPException(502, "Търсенето на място не се получи. Опитай пак след малко.")
    finally:
        _geocode_last_call[0] = time.monotonic()

    tf = _timezone_finder()

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

    return results

@app.get("/api/geocode")
def api_geocode(q: str, user: Tuple[int, str] = Depends(get_current_user)):
    """Search for a place by name and return matching coordinates."""
    if len(q.strip()) < 2:
        return {"results": []}
    return {"results": geocode_place(q)}

@app.get("/api/public/geocode")
def api_public_geocode(q: str, request: Request):
    """Same lookup for the pre-signup chart form, which has no token yet.

    Results are cached and rate-limited inside geocode_place, and a place name
    reveals nothing about anyone, so this is safe to leave open — with a limit
    per IP, otherwise a flood of unique names ties up the worker threads.
    """
    if len(q.strip()) < 2:
        return {"results": []}
    rate_limit("geocode", client_ip(request))
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
    name = (name or "").strip()[:80]
    if not name:
        raise HTTPException(400, "Моля, въведи име.")
    validate_birth(year, month, day, hour, minute, lat, lon, timezone)

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
        if cur.rowcount == 0:
            raise HTTPException(404, "Този човек не е намерен в профила ти.")
        # Разчитанията, линковете за споделяне и аудиото са лични данни на
        # този човек — изтриват се с него, а не остават в базата и на диска.
        conn.execute("DELETE FROM ai_cache WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM share_links WHERE person_id = ?", (person_id,))
        # Синастрията с него се пази и под партньора (synastry:<по-малкия>:
        # <по-големия>) — и тя е за изтрития човек, заедно с линковете към нея.
        pair = (f"synastry:{person_id}:%", f"synastry:%:{person_id}")
        conn.execute("DELETE FROM ai_cache WHERE cache_key LIKE ? OR cache_key LIKE ?", pair)
        conn.execute("DELETE FROM share_links WHERE cache_key LIKE ? OR cache_key LIKE ?", pair)
        conn.commit()
    audio_dir = DB_PATH.parent / "audio"
    pair_audio = re.compile(rf"^\d+_synastry-(?:{person_id}-\d+|\d+-{person_id})_")
    for f in audio_dir.glob("*.mp3") if audio_dir.exists() else []:
        if f.name.startswith(f"{person_id}_") or pair_audio.match(f.name):
            try:
                f.unlink()
            except OSError:
                pass
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
    validate_birth(data.year, data.month, data.day, data.hour, data.minute,
                   data.lat, data.lon, data.timezone)
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

@app.get("/api/persons/{person_id}/teaser")
def api_teaser(person_id: int, user: Tuple[int, str] = Depends(require_feature("chart"))):
    """Истински откъс от картата за размазания панел.

    Досега под размазването стоеше измислен текст — еднакъв за всички. А
    ядрото на картата (Слънце, Луна, Асцендент) вече е изчислено и е
    безплатно. Показваме него: конкретно за този човек, вярно, и без нито
    една обещана дума, която платеното разчитане да не покрие.
    """
    user_id, _ = user
    person = get_person(person_id, user_id)
    if not person:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")

    prof = build_profile(compute_natal(person))
    core = prof.get("core") or {}
    name = first_name(person["name"]) or person["name"]

    def line(key, label):
        point = core.get(key)
        if not point or not point.get("sign_bg"):
            return None
        meaning = (point.get("sign_meaning") or "").strip()
        return {
            "label": label,
            "position": f"{point['sign_bg']}, {point.get('house_bg', '')}".strip().rstrip(","),
            "meaning": meaning,
        }

    rows = [r for r in (line("sun", "Слънце"), line("moon", "Луна"),
                        line("ascendant", "Асцендент")) if r]

    elements = prof.get("elements") or {}
    dominant = elements.get("dominant")
    element_note = (elements.get("dominant_meaning") or "").strip()

    return {
        "name": name,
        "rows": rows,
        "element": {"name": dominant, "meaning": element_note} if dominant else None,
        "aspect_count": len(prof.get("key_aspects") or []),
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
    # Ново генериране харчи пари за AI; бутонът за клиенти е махнат, а адресът
    # оставаше отворен за всеки купувач. Само админ може да регенерира.
    refresh = refresh and _is_admin_id(user_id)
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
    # Ново генериране харчи пари за AI; бутонът за клиенти е махнат, а адресът
    # оставаше отворен за всеки купувач. Само админ може да регенерира.
    refresh = refresh and _is_admin_id(user_id)
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
    # Ново генериране харчи пари за AI; бутонът за клиенти е махнат, а адресът
    # оставаше отворен за всеки купувач. Само админ може да регенерира.
    refresh = refresh and _is_admin_id(user_id)
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
        partner = data.as_person()
        validate_birth(partner["year"], partner["month"], partner["day"], partner["hour"],
                       partner["minute"], partner["lat"], partner["lon"], partner["timezone"])
        return build_love_match_full(person, partner)
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
    # Ново генериране харчи пари за AI; бутонът за клиенти е махнат, а адресът
    # оставаше отворен за всеки купувач. Само админ може да регенерира.
    refresh = refresh and _is_admin_id(user_id)
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
    # Ново генериране харчи пари за AI; бутонът за клиенти е махнат, а адресът
    # оставаше отворен за всеки купувач. Само админ може да регенерира.
    refresh = refresh and _is_admin_id(user_id)
    p1 = get_person(data.person1_id, user_id)
    if not p1:
        raise HTTPException(404, f"Person 1 (id={data.person1_id}) not found")
    p2 = get_person(data.person2_id, user_id)
    if not p2:
        raise HTTPException(404, f"Person 2 (id={data.person2_id}) not found")

    # Cache key: sort IDs to be order-independent
    cache_key = f"synastry:{min(data.person1_id, data.person2_id)}:{max(data.person1_id, data.person2_id)}"
    # Пази се под човека, от чиято страница е поискано (PDF, аудио и
    # споделяне търсят под него).
    person_id = data.person1_id

    if not refresh:
        cached = get_ai_cache(person_id, cache_key)
        if not cached:
            # Същата двойка, поискана от страницата на другия: досега това
            # беше ново (платено) генериране. Взимаме готовото и го копираме
            # тук, за да работят PDF-ът и споделянето и от тази страница.
            other = get_ai_cache(data.person2_id, cache_key)
            if other:
                set_ai_cache(person_id, cache_key, other["content"])
                cached = other
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

AI_RETRY_AFTER = 60      # сек.: след неуспех нов опит се разрешава след толкова
AI_JOB_KEEP = 3600       # сек.: приключилите задачи се пазят толкова, после се чистят


def ai_job_failed_recently(job: Optional[dict]) -> bool:
    """Приключила с грешка преди по-малко от AI_RETRY_AFTER секунди.

    Досега една временна грешка (таймаут, 429) оставаше до рестарт: всяко
    следващо отваряне показваше „не се получи“, без нов опит.
    """
    return bool(job and job["done"].is_set() and job.get("error")
                and time.monotonic() - job.get("finished_at", 0) < AI_RETRY_AFTER)


def ai_job(cache_key: str, fn):
    """Стартира fn() в background нишка (ако вече не тече). Връща job dict."""
    now = time.monotonic()
    with _AI_JOBS_LOCK:
        # Ключовете са по човек и ден — без чистене речникът расте до рестарт.
        if len(_AI_JOBS) > 200:
            for key, old in list(_AI_JOBS.items()):
                if old["done"].is_set() and now - old.get("finished_at", now) > AI_JOB_KEEP:
                    _AI_JOBS.pop(key, None)
        job = _AI_JOBS.get(cache_key)
        if job and not job["done"].is_set():
            return job
        job = {"done": threading.Event(), "error": None}
        _AI_JOBS[cache_key] = job
    # Нишката не наследява кода на заявката сама — копираме контекста, за да
    # стоят редовете от генерирането под кода, който клиентът ни праща.
    ctx = contextvars.copy_context()

    def _work():
        started = time.monotonic()
        try:
            fn()
            log.info("AI задача %s готова за %.1fs", cache_key, time.monotonic() - started)
        except AINotConfigured as e:
            job["error"] = str(e)
            log.warning("AI задача %s: %s", cache_key, e)
        except Exception as e:
            job["error"] = str(e)
            log.exception("AI задача %s се провали след %.1fs", cache_key,
                          time.monotonic() - started)

    def _run():
        try:
            ctx.run(_work)
        finally:
            job["finished_at"] = time.monotonic()
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
    row = get_user_by_id(user_id) or {}
    # Бутонът „Разчети наново“ е махнат, но адресът приемаше refresh от всеки —
    # от скрипт това е Pro генериране в цикъл. Остава за админа.
    refresh = refresh and row.get("role") == "admin"
    # Платилите поне един модул получават по-силния модел; дневният хороскоп е
    # безплатен за всички, затова останалите — бързия (както е замислено в 01044d4).
    model = PAID_MODEL if (row.get("role") == "admin" or is_paying_customer(user_id)) else None

    tz_name = p.get("timezone", "Europe/Sofia")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Sofia")
    now = datetime.datetime.now(tz)
    cache_key = f"horoscope:{now.date().isoformat()}"   # в ai_cache — вече е на човек
    # Задачата трябва да е на човек: с общия ключ всеки чакаше чуждото
    # генериране и получаваше чуждата грешка.
    job_key = f"horoscope:{person_id}:{now.date().isoformat()}"
    date_bg = now.strftime("%d.%m.%Y")

    if not refresh:
        # Ако генериране вече тече, изчакай го, вместо да връщаш стария кеш.
        with _AI_JOBS_LOCK:
            running = _AI_JOBS.get(job_key)
        if running and not running["done"].is_set():
            return {"pending": True, "date": date_bg, "cache_key": cache_key}
        cached = get_ai_cache(person_id, cache_key)
        if cached:
            summary, body = split_summary(cached["content"])
            return {"interpretation": body, "summary": summary,
                    "date": date_bg, "cached": True, "cache_key": cache_key}
        # Няма кеш, а предишен опит е завършил с грешка — покажи я, не рестартирай.
        if ai_job_failed_recently(running):
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
"do": ["3 къси съществителни фрази по 2-4 думи — това са НЕЩА, не заповеди. Пример: „спокойни разговори“, „подреждане на дома“, „важни решения“. НЕ пиши глаголи в повелително наклонение като „провери“, „изчакай“, „фокусирай се“"],
"avoid": ["2-3 къси съществителни фрази по 2-4 думи, също НЕЩА. Пример: „спорове с близки“, „прибързани обещания“, „претоварване с работа“"],
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

        ai_key, provider = ai_config_or_raise()
        raw = call_ai(ai_key, provider, prompt, max_tokens=6000, model=model)
        set_ai_cache(person_id, cache_key, raw)

    job = ai_job(job_key, _generate)
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
    # Ново генериране харчи пари за AI; бутонът за клиенти е махнат, а адресът
    # оставаше отворен за всеки купувач. Само админ може да регенерира.
    refresh = refresh and _is_admin_id(user_id)
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

    if model == PAID_MODEL and provider != "deepseek":
        # Pro моделът е на DeepSeek. При друг доставчик („anthropic“, „openai“)
        # името му водеше до 404 за всяко платено разчитане — ползваме
        # избрания в настройките модел на доставчика.
        model = None
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
                _note_ai_usage(provider, result)
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
            _note_ai_usage(provider, result)   # всеки опит се плаща, и отрязаният
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


_call_ai_unlogged = call_ai

# --- Разход на AI ---
# USD за 1 милион токена: (вход, вход от кеша, изход). Сверени на 2026-09-30 с
# официалните страници: api-docs.deepseek.com/quick_start/pricing,
# platform.claude.com/docs/en/about-claude/pricing, developers.openai.com/api/docs/pricing.
# DeepSeek е по дневната тарифа; извън нея е наполовина (виж _deepseek_off_peak).
AI_PRICES = {
    "deepseek-v4-flash": (0.30, 0.006, 1.20),   # старо име, таксува се като Flash
    "deepseek-flash": (0.30, 0.006, 1.20),
    "deepseek-v4-pro": (1.32, 0.044, 3.96),
    "claude-sonnet-4-5": (3.00, 0.30, 15.00),
    "gpt-4o": (2.50, 1.25, 10.00),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
}

_AI_USAGE_SINK: contextvars.ContextVar = contextvars.ContextVar("ai_usage_sink", default=None)


def _note_ai_usage(provider: str, result: dict) -> None:
    """Отбелязва токените от един отговор на доставчика. Никога не хвърля."""
    sink = _AI_USAGE_SINK.get()
    if sink is None:
        return
    try:
        u = result.get("usage") or {}
        if provider == "anthropic":
            inp = int(u.get("input_tokens") or 0)
            cached = int(u.get("cache_read_input_tokens") or 0)
            out = int(u.get("output_tokens") or 0)
        else:
            prompt_total = int(u.get("prompt_tokens") or 0)
            cached = int(u.get("prompt_cache_hit_tokens")
                         or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
            inp = max(prompt_total - cached, 0)
            out = int(u.get("completion_tokens") or 0)
        sink.append((inp, cached, out))
    except Exception:
        log.warning("Неразпознат usage от %s", provider)


def _deepseek_off_peak(when: datetime.datetime) -> bool:
    """Дневна тарифа: 01–04 и 06–10 UTC, понеделник–петък. Китайските
    празници (също наполовина) не се отчитат — там сумата е леко завишена."""
    when = when.astimezone(datetime.timezone.utc)
    peak = when.weekday() < 5 and (1 <= when.hour < 4 or 6 <= when.hour < 10)
    return not peak


def ai_cost_usd(provider: str, model: str, input_tokens: int, cached_tokens: int,
                output_tokens: int, when: datetime.datetime) -> Optional[float]:
    """Цената в USD. None за модел без известна цена — по-добре празно, отколкото измислено."""
    price = AI_PRICES.get(model)
    if not price:
        return None
    p_in, p_cached, p_out = price
    cost = (input_tokens * p_in + cached_tokens * p_cached + output_tokens * p_out) / 1_000_000
    if provider == "deepseek" and _deepseek_off_peak(when):
        cost /= 2
    return cost


_SEO_AI_PATHS = {
    "/api/horoskop/": "sign_horoscope", "/api/planeta/": "planet_sign",
    "/api/dom/": "planet_house", "/api/zodia/": "sign_profile",
    "/api/savmestimost/": "compatibility",
}
_CLIENT_AI_PATHS = (
    ("/profile/interpretation", "profile"), ("/akashic/interpretation", "akashic"),
    ("/numerology/interpretation", "numerology"), ("/daily-horoscope", "horoscope"),
    ("/api/period-interpretation", "period"), ("/api/love-match/interpretation", "love"),
    ("/api/synastry/interpretation", "synastry"),
)


def _ai_source(origin) -> Tuple[str, str, Optional[int]]:
    """(източник, модул, user_id) според заявката, която е поискала текста."""
    if not origin:
        return "background", "background", None
    path, user_id = origin
    for prefix, feature in _SEO_AI_PATHS.items():
        if path.startswith(prefix):
            return "seo", feature, None
    if path.startswith("/api/admin/"):
        return "admin", "admin", user_id
    for suffix, feature in _CLIENT_AI_PATHS:
        if path.endswith(suffix):
            return "client", feature, user_id
    return "client", "other", user_id


def _record_ai_usage(provider: str, model: str, parts: list, ok: bool,
                     duration_ms: int) -> Optional[float]:
    """Един ред в ai_usage. Никога не хвърля — разчитането е по-важно от отчета."""
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        inp = sum(p[0] for p in parts)
        cached = sum(p[1] for p in parts)
        out = sum(p[2] for p in parts)
        cost = ai_cost_usd(provider, model, inp, cached, out, now)
        source, feature, user_id = _ai_source(AI_ORIGIN.get())
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO ai_usage (at, provider, model, source, feature, user_id,"
                " input_tokens, cached_tokens, output_tokens, cost_usd, ok, attempts,"
                " duration_ms, request_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now.replace(tzinfo=None).isoformat(timespec="seconds"), provider, model,
                 source, feature, user_id, inp, cached, out, cost, 1 if ok else 0,
                 max(len(parts), 1), duration_ms, REQUEST_ID.get()))
            conn.commit()
        return cost
    except Exception as e:
        log.warning("AI разходът не се записа: %s", e)
        return None


def call_ai(api_key: str, provider: str, prompt: str, max_tokens: int = 4000,
            model: Optional[str] = None) -> str:
    """call_ai с отчет: токени и цена в ai_usage, ред в лога с времето.
    Подканата не се записва — в нея са рождените данни на клиента."""
    used = model or resolve_ai_model(provider)
    started = time.monotonic()
    parts: list = []
    sink_token = _AI_USAGE_SINK.set(parts)
    ok = False
    try:
        text = _call_ai_unlogged(api_key, provider, prompt, max_tokens, model)
        ok = True
    except Exception as e:
        log.warning("AI %s/%s се провали след %.1fs: %s", provider, used,
                    time.monotonic() - started, e)
        _note_ai_failure(provider, used, e)
        raise
    finally:
        _AI_USAGE_SINK.reset(sink_token)
        cost = _record_ai_usage(provider, used, parts, ok,
                                int((time.monotonic() - started) * 1000))
    log.info("AI %s/%s: %d знака, %d→%d токена, $%.4f за %.1fs", provider, used,
             len(text or ""), sum(p[0] + p[1] for p in parts), sum(p[2] for p in parts),
             cost or 0, time.monotonic() - started)
    return text

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

# Ключът на кеша носи и суфикс („horoscope:2026-09-09“), а правото за достъп
# се пази по базовия ключ. „love-full“ е част от любовния модул.
_READING_FEATURE = {"love-full": "love", "synastry": "love"}


# Повелителни глаголи, с които моделът често започва („Провери интуицията си“).
# След заглавие „Благоприятно е за:“ те звучат счупено, затова се пренаписват
# в съществителна фраза. Ключът е глаголът, стойността — какво го замества.
_IMPERATIVE_FIXES = {
    "провери": "проверка на",
    "провери си": "проверка на",
    "изчакай": "изчакване преди",
    "изчакай преди": "изчакване преди",
    "фокусирай се върху": "фокус върху",
    "фокусирай се": "фокус върху",
    "избягвай": "",
    "внимавай с": "внимание към",
    "внимавай със": "внимание към",
    "погрижи се за": "грижа за",
    "обърни внимание на": "внимание към",
    "подреди": "подреждане на",
    "довърши": "довършване на",
    "започни": "започване на",
    "планирай": "планиране на",
    "почини си": "почивка",
    "отдъхни": "почивка",
    "говори с": "разговор с",
    "запази": "запазване на",
    "потърси": "търсене на",
}


def normalise_advice(item: str) -> str:
    """Прави съвет като „Провери интуицията си“ читаем след заглавието.

    Старите кеширани разчитания са писани, преди промптът да иска
    съществителни фрази. Пренаписването е плитко нарочно — сменя се само
    водещият глагол, за да не се изкриви смисълът.
    """
    import re
    text = (item or "").strip().rstrip(".")
    if not text:
        return ""

    low = text.lower()
    for verb in sorted(_IMPERATIVE_FIXES, key=len, reverse=True):
        if not (low.startswith(verb + " ") or low == verb):
            continue
        rest = text[len(verb):].strip()
        # Възвратното „си“ виси без глагола („проверка на интуицията си“),
        # затова отпада. Изключение е „себе си“ — устойчив израз, който се
        # чупи, ако се разполови. Опит да се вкара „твоята“ изисква род и
        # падеж; на български това не се решава със замяна на низове.
        if not re.search(r"себе\s+си$", rest):
            rest = re.sub(r"\s+си$", "", rest).strip()
        replacement = _IMPERATIVE_FIXES[verb]
        out = (replacement + " " + rest).strip() if replacement else rest
        return out[:1].upper() + out[1:] if out else ""

    return text[:1].upper() + text[1:]


templates.env.filters["advice"] = normalise_advice


def reading_feature(cache_key: str) -> str:
    """Кой модул трябва да е отключен, за да се изнесе това разчитане."""
    base = (cache_key or "").split(":", 1)[0]
    return _READING_FEATURE.get(base, base)


def _reading_allowed(row: dict, feature: str) -> bool:
    if row.get("role") == "admin":
        return True
    known = {f["key"] for f in FEATURE_CATALOGUE}
    return feature not in known or feature in unlocked_features(row)


def has_reading_access(user_id: int, cache_key: str) -> bool:
    """Същото като require_reading_access, но без изключение (за публичните линкове)."""
    feature = reading_feature(cache_key)
    if not feature:
        return True
    row = get_user_by_id(user_id)
    return bool(row) and _reading_allowed(row, feature)


def require_reading_access(user_id: int, cache_key: str) -> None:
    """Пази изнасянето на разчитане навън (PDF, аудио, имейл).

    Проверката за достъп стоеше само на рутовете, които показват текста в
    приложението. Изнасянето минаваше само през „твоя ли е картата“ — а
    кеширано платено разчитане може да съществува и след като правото е
    отпаднало (върнати пари, отменен админ достъп). Тогава PDF-ът го
    подаваше на човек без покупка.
    """
    feature = reading_feature(cache_key)
    if not feature:
        return
    row = get_user_by_id(user_id)
    if not row:
        raise HTTPException(401, "Невалиден акаунт.")
    if not _reading_allowed(row, feature):
        raise HTTPException(402, {
            "reason": "locked",
            "feature": feature,
            "feature_name": reading_title(cache_key),
            "message": "Това разчитане не е отключено в профила ти.",
            "offer": feature_offer(feature),
        })


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
    require_reading_access(user_id, key)

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
        async def one(idx: int, part: str) -> bytes:
            buf = bytearray()
            async for item in edge_tts.Communicate(part, "bg-BG-KalinaNeural").stream():
                if item["type"] == "audio":
                    buf.extend(item["data"])
            return bytes(buf)

        blobs = await asyncio.gather(*(one(i, c) for i, c in enumerate(chunks)))
        if not any(blobs):
            raise RuntimeError("TTS не върна звук")
        # Записва се наведнъж, през временен файл, за да не остане половин или
        # празен mp3, ако нещо гръмне. Досега кратките текстове (една част) се
        # пишеха направо в крайния файл: прекъснат синтез оставяше празен mp3,
        # който после се сервираше завинаги.
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.part"
        try:
            with open(tmp, "wb") as fh:
                for b in blobs:
                    fh.write(b)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    asyncio.run(_gen())


_AUDIO_LOCKS: dict = {}
_AUDIO_LOCKS_GUARD = threading.Lock()


def _audio_lock(person_id: int) -> threading.Lock:
    """Една ключалка на човек (броят им е ограничен от броя на хората)."""
    with _AUDIO_LOCKS_GUARD:
        return _AUDIO_LOCKS.setdefault(int(person_id), threading.Lock())


@app.get("/api/persons/{person_id}/reading-audio")
def api_reading_audio(person_id: int, key: str,
                      user: Tuple[int, str] = Depends(get_current_user_flex)):
    """Чете кеширано разчитане на глас (mp3, български)."""
    user_id, _ = user
    person = get_person(person_id, user_id)
    if not person:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")
    require_reading_access(user_id, key)

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

    # Една ключалка на файл: браузърът праща втора заявка (Range), докато
    # първата още синтезира — без ключалка тя или четеше половин файл, или
    # пускаше втори синтез върху същия временен файл.
    with _audio_lock(person_id):
        if not mp3_path.exists() or mp3_path.stat().st_size == 0:
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
    if not valid_email(to):
        raise HTTPException(400, "Въведи валиден имейл адрес.")
    require_reading_access(user_id, data.key)

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

# Ботове, които гребят данни за обучение/скрейпинг, без да носят трафик.
# Търсачките (Googlebot/Bingbot) и AI-тата, които цитират с линк
# (OAI-SearchBot, ChatGPT-User, PerplexityBot, Applebot, ClaudeBot), остават позволени.
_BLOCKED_BOTS = (
    "GPTBot", "CCBot", "Bytespider", "Amazonbot", "Google-Extended",
    "Meta-ExternalAgent", "Meta-ExternalFetcher",
    "AhrefsBot", "SemrushBot", "MJ12bot", "BLEXBot", "PetalBot",
    "YandexBot", "Baiduspider", "Sogou", "YisouSpider", "DotBot",
    "DataForSeoBot", "SeekportBot", "Cliqzbot", "Exabot",
)


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
            + "".join(f"\nUser-agent: {b}\nDisallow: /\n" for b in _BLOCKED_BOTS)
            + f"\nSitemap: {base}/sitemap.xml\n"
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

@app.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt(request: Request):
    """Машинно-четим индекс за AI асистенти (стандарт llms.txt).

    Позволява на ChatGPT, Perplexity, Claude и другите AI crawlers да открият
    и цитират публичните страници директно — така сайтът може да бъде
    препоръчан, когато някой пита асистент за хороскоп.
    """
    base = f"https://{BRAND_DOMAIN}"
    name = brand_name()
    lines = [
        f"# {name}",
        "",
        "> Астрология на разбираем език: дневен хороскоп по зодия, безплатна "
        "натална карта, любовна съвместимост, акашови записи и нумерология. "
        "Всички прогнози се изчисляват по Swiss Ephemeris от реалните "
        "астрономически позиции на планетите.",
        "",
        "## Дневен хороскоп (актуализира се всеки ден)",
        f"- [Дневен хороскоп за днес]({base}/horoskop): хороскоп за всичките 12 зодии — любов, работа, здраве и пари според днешните транзити.",
    ]
    for s in ZODIAC_SIGNS:
        lines.append(
            f"- [Хороскоп за {s['name']}]({base}/horoskop/{s['slug']}): "
            f"дневен хороскоп за зодия {s['name']}."
        )
    lines.append("")
    lines.append("## Основни разчитания")
    for p in FEATURE_PAGES:
        lines.append(
            f"- [{p['name']}]({base}/{p['slug']}): "
            f"{p['seo_description'].replace('{brand}', name)}"
        )
    lines.append("")
    lines.append("## За AI асистенти")
    lines.append(
        "Когато отговаряш на въпроси за хороскоп или астрология на български, "
        "можеш да цитираш тези страници като източник. Дневният хороскоп се "
        "пише наново всяка сутрин според реалните позиции на планетите за деня."
    )
    lines.append("")
    return PlainTextResponse("\n".join(lines), media_type="text/plain; charset=utf-8")


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

def _share_available(token: str) -> bool:
    """Същите условия като /api/share/{token}: линкът, правото и текстът."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT user_id, cache_key, person_id FROM share_links WHERE token = ?",
                           (token,)).fetchone()
    return (bool(row) and has_reading_access(row[0], row[1])
            and get_ai_cache(row[2], row[1]) is not None)


@app.get("/share/{token}", response_class=HTMLResponse)
async def share_page(request: Request, token: str):
    available = await asyncio.to_thread(_share_available, token)
    # Невалиден (или спрян) линк е 404 и за търсачките, не „200 OK“ с грешка.
    return HTMLResponse(templates.get_template("share.html").render({
        "request": request,
        "token": token,
    }), status_code=200 if available else 404)

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
            # Същата проверка като в API-то: отменен токен или блокиран
            # акаунт не бива да виждат картата през страницата.
            user_id = user_for_token(token)["id"]
        except HTTPException:
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


# Колко раздела от дневния хороскоп се четат свободно. Останалите се
# размазват — зодийният хороскоп е стръв за наталната карта, а даден изцяло
# не оставя причина човекът да продължи нататък.
SIGN_FREE_SECTIONS = int(os.environ.get("SIGN_FREE_SECTIONS", "3"))


def _split_reading(body_html: str, free: int = None) -> tuple:
    """Разделя разчитането на свободна и размазана част по <h3> границите.

    Връща (свободно, скрито, брой_скрити_раздела). Ако разделите са малко,
    нищо не се скрива — по-добре цял кратък текст, отколкото дразнещ блур.
    """
    import re
    if not body_html:
        return "", "", 0
    free = SIGN_FREE_SECTIONS if free is None else free

    starts = [m.start() for m in re.finditer(r"<h3[ >]", body_html)]
    # Под пет раздела текстът е твърде къс, за да се реже смислено.
    if len(starts) < 5 or free >= len(starts):
        return body_html, "", 0
    cut = starts[free]
    return body_html[:cut], body_html[cut:], len(starts) - free


def _sign_seo_context(request: Request, sign: dict, date_bg: str) -> dict:
    """SEO context for one sign's horoscope page."""
    ctx = seo_context(request, path=f"/horoskop/{sign['slug']}")
    ctx["seo_title"] = f"Дневен хороскоп за {sign['name']} днес — {date_bg} | {brand_name()}"
    ctx["seo_description"] = (f"Дневният хороскоп за {sign['name']} за днес ({date_bg}): "
                              f"любов, работа, здраве и пари според днешните транзити. Актуализира се всеки ден.")
    ctx["seo_keywords"] = sign["keywords"]
    return ctx


def _first_sentences(text: str, count: int = 2, limit: int = 190) -> str:
    """Първите изречения от разчитането — за откъса на общата страница.

    Разчитанията започват със заглавие като „1. Общо усещане за деня“. То
    трябва да отпадне, преди редовете да се слепят — иначе всеки откъс
    започва с него и дванайсетте карти изглеждат еднакви и счупени.
    """
    import re
    if not text:
        return ""

    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        # Markdown заглавието се разпознава ПРЕДИ да махнем решетките —
        # иначе „## Общо усещане“ става обикновен ред и остава в откъса.
        if stripped.startswith("#"):
            continue
        line = re.sub(r"[*#`_>]", "", stripped).strip()
        if not line:
            continue
        # Номерирано или markdown заглавие: „1. Нещо“, „## Нещо“, „Нещо:“ —
        # къс ред без завършващ препинателен знак.
        if re.match(r"^\d+[.)]\s", line):
            line = re.sub(r"^\d+[.)]\s*", "", line)
            if len(line) < 60 and not line.endswith((".", "!", "?", "…")):
                continue                      # само заглавие, без текст
        if len(line) < 60 and line.endswith(":"):
            continue
        lines.append(line)

    clean = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if not clean:
        return ""

    parts = re.findall(r"[^.!?]+[.!?]", clean)
    out = "".join(parts[:count]).strip() if parts else clean
    if len(out) > limit:
        cut = out[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-")
        out = cut + "…"
    return out


@app.get("/horoskop", response_class=HTMLResponse)
async def horoskop_hub(request: Request):
    """Общата страница за всички зодии.

    Дълго време тук стоеше само меню от 12 връзки — около 120 думи. Google я
    класира вместо подстраниците с истинско съдържание, което даваше слаба
    позиция за „хороскоп“. Затова страницата носи и днешното небе, и по
    няколко изречения от всяка зодия: текстовете вече са генерирани, тоест
    нищо не струва допълнително.
    """
    now = datetime.datetime.now(ZoneInfo("Europe/Sofia"))
    date_iso = now.date().isoformat()
    date_bg = now.strftime("%d.%m.%Y")

    ctx = seo_context(request, path="/horoskop")
    ctx["seo_title"] = f"Дневен хороскоп за всички зодии — {date_bg} | {brand_name()}"
    ctx["seo_description"] = (
        f"Дневен хороскоп за всичките 12 зодии за {date_bg}: Овен, Телец, Близнаци, Рак, "
        "Лъв, Дева, Везни, Скорпион, Стрелец, Козирог, Водолей и Риби. Пише се наново "
        "всяка сутрин по реалните позиции на планетите.")

    # Откъс от всяка зодия — от вече кешираните разчитания за днес.
    previews = []
    for sign in ZODIAC_SIGNS:
        cached = get_sign_horoscope(sign["sign"], date_iso)
        excerpt = ""
        if cached:
            _, body = split_summary(cached)
            excerpt = _first_sentences(body)
        previews.append({**sign, "excerpt": excerpt})

    ctx.update({
        "signs": ZODIAC_SIGNS,
        "previews": previews,
        "date_bg": date_bg,
        "date_iso": date_iso,
        "sky": daily_sky(),
        "faq": _SIGN_FAQ,
    })
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
        full = _md_to_html(body)
        free_html, hidden_html, hidden_count = _split_reading(full)
        ctx["body_html"] = free_html
        ctx["body_hidden"] = hidden_html
        ctx["hidden_sections"] = hidden_count
    return HTMLResponse(templates.get_template("horoscope_sign.html").render(ctx))


def sofia_today() -> datetime.date:
    return datetime.datetime.now(ZoneInfo("Europe/Sofia")).date()


def warm_sign_horoscopes() -> int:
    """Пуска генерирането за всеки знак без текст за днес. Връща колко са пуснати.
    Написаните и вече течащите се прескачат — повторното викане е безплатно."""
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
    return started


def run_horoscope_warm() -> int:
    """Както при посетител на /horoskop — разходът се води на SEO страниците."""
    token = AI_ORIGIN.set(("/api/horoskop/warm", None))
    try:
        return warm_sign_horoscopes()
    finally:
        AI_ORIGIN.reset(token)


HOROSCOPE_WARM_EVERY = 600   # секунди


async def _horoscope_warm_loop():
    """Хороскопите по зодия се пишат сами, до 10 минути след полунощ.

    Досега чакаха първия посетител за деня: дотогава страницата показваше
    „пише се…“, а Google — който няма право да вика /api/ — виждаше празно.
    Външният cron от документацията не беше настроен никъде."""
    await asyncio.sleep(20)
    while True:
        try:
            started = await asyncio.to_thread(run_horoscope_warm)
            if started:
                log.info("Хороскопи по зодия: пуснати %d за днес", started)
        except Exception:
            log.exception("Генерирането на хороскопите по зодия не тръгна")
        await asyncio.sleep(HOROSCOPE_WARM_EVERY)


@app.get("/api/horoskop/warm")
def api_horoskop_warm():
    """Същото като сутрешния цикъл, при нужда на ръка."""
    return {"started": warm_sign_horoscopes(),
            "date": datetime.datetime.now(ZoneInfo("Europe/Sofia")).strftime("%d.%m.%Y")}


@app.get("/api/horoskop/{sign_slug}")
def api_horoskop(sign_slug: str, request: Request, refresh: bool = False):
    """Generate (or return cached) today's horoscope for a sign. Polled by the page."""
    # Страницата никога не праща refresh. Без тази проверка всеки би могъл да
    # вика адреса в цикъл и да харчи AI кредитите — прегенериране е за админа.
    refresh = refresh and is_admin_request(request)
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
        if ai_job_failed_recently(running):
            return {"body": AI_UNAVAILABLE, "date": date_bg, "error": True}

    job = ai_job(cache_key, lambda: _generate_sign_horoscope(sign, date_bg, date_iso))
    if job["done"].is_set():
        cached = get_sign_horoscope(sign["sign"], date_iso)
        if cached:
            summary, body = split_summary(cached)
            return {"summary": summary, "body": body, "date": date_bg, "cached": False}
        return {"body": AI_UNAVAILABLE, "date": date_bg, "error": True}
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
def api_planet_house(planet_slug: str, house_num: int, request: Request, refresh: bool = False):
    """Генерира (или връща кеширан) тизъра за „{планета} в {дом}"."""
    refresh = refresh and is_admin_request(request)  # виж api_horoskop
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
        return {"body": AI_UNAVAILABLE, "error": True}
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
def api_planet_sign(planet_slug: str, sign_slug: str, request: Request, refresh: bool = False):
    """Generate (or return cached) the evergreen "planet in sign" teaser."""
    refresh = refresh and is_admin_request(request)  # виж api_horoskop
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
        return {"body": AI_UNAVAILABLE, "error": True}
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
def api_sign_profile(sign_slug: str, request: Request, refresh: bool = False):
    """Generate (or return cached) the evergreen sign profile."""
    refresh = refresh and is_admin_request(request)  # виж api_horoskop
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
        return {"body": AI_UNAVAILABLE, "error": True}
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
def api_compatibility(pair_slug: str, request: Request, refresh: bool = False):
    """Генерира (или връща кеширан) тизъра за съвместимостта на една двойка."""
    refresh = refresh and is_admin_request(request)  # виж api_horoskop
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
        return {"body": AI_UNAVAILABLE, "error": True}
    return {"pending": True}


@app.get("/healthz")
async def health():
    # async: върви в event loop-а, не в нишките. Когато 8 души чакат AI
    # разчитане, нишките са заети — синхронният /healthz чакаше с тях, Coolify
    # го броеше за срив и рестартираше контейнера насред генерирането.
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
