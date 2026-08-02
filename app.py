import os, re, sys, json, sqlite3, datetime, urllib.parse
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

from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordBearer
from immanuel import charts
from immanuel.const import chart, names
from pydantic import BaseModel
from jose import jwt, JWTError
import bcrypt
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
from pdf_report import build_reading_pdf

# --- App Setup ---
BASE_DIR = Path(__file__).parent
# Overridable so a deployment can point the database at a mounted volume;
# without that the file lives inside the container and dies with it.
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "data" / "persons.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = 60 * 24 * 30  # 30 days

# ENVIRONMENT=production refuses to start on an insecure default, so a live
# deployment can never silently run with the credentials published in the repo.
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT in ("production", "prod")

DEV_SECRET_KEY = "change-me-in-production-secret-key"
DEV_ADMIN_PASSWORD = "admin123"
DEV_DEMO_PASSWORD = "demo123"

SECRET_KEY = os.environ.get("SECRET_KEY", DEV_SECRET_KEY)
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@miralog.bg")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", DEV_ADMIN_PASSWORD)
# A standing demo account, so the locked/paywalled views can be checked without
# touching a real user. Set DEMO_EMAIL="" to skip creating it in production.
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "").strip()
# In production the demo account is opt-in: it appears only when a password is
# supplied. Relying on DEMO_EMAIL="" would not work, because some platforms
# (Coolify among them) drop empty environment variables entirely.
if IS_PRODUCTION:
    DEMO_EMAIL = (os.environ.get("DEMO_EMAIL", "").strip() or "demo@miraskop.bg") \
        if DEMO_PASSWORD else ""
else:
    DEMO_EMAIL = os.environ.get("DEMO_EMAIL", "demo@miralog.bg").strip()
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
        ]:
            if col not in user_cols:
                conn.execute(ddl)

        # The seeded admin predates the role column, so claim it here.
        conn.execute("UPDATE users SET role = 'admin' WHERE email = ? AND role != 'admin'",
                     (ADMIN_EMAIL,))

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
                    ("horoscope", 300, 1),
                    ("period", 500, 1),
                    ("synastry", 700, 1),
                    ("love", 500, 1),
                    ("akashic", 900, 1),
                    ("moon", 300, 1),
                    # The basics come with every plan, so they are never sold.
                    ("chart", 0, 0),
                    ("planets", 0, 0),
                    ("aspects", 0, 0),
                    ("numerology", 400, 1),
                    ("interpretation", 600, 1),
                ])

        # Same for the price list: it is only seeded when empty, so later
        # features would have no price and could never be bought on their own.
        for feature_key, cents in [("interpretation", 600)]:
            conn.execute(
                "INSERT INTO feature_prices (feature_key, price_cents, currency, is_purchasable)"
                " VALUES (?, ?, 'EUR', 1) ON CONFLICT(feature_key) DO NOTHING",
                (feature_key, cents))

        # Features added after a plan was first seeded do not appear in existing
        # rows, so the paid plan would silently lose access to them.
        for key, feature in [("full", "interpretation")]:
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

        # Seed the two plans the landing page advertises.
        if conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO plans (key, name, price_cents, currency, period, max_persons, features, sort_order)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    # Two charts, so a bought "Съвместимост" has a second person
                    # to compare against.
                    ("demo", "Демо", 0, "EUR", "month", 2,
                     json.dumps(["chart", "planets", "aspects", "numerology"]), 0),
                    ("full", "Пълен достъп", 500, "EUR", "month", 50,
                     json.dumps(["chart", "planets", "aspects", "numerology", "profile",
                                 "horoscope", "period", "synastry", "love", "akashic",
                                 "moon", "interpretation"]), 1),
                ]
            )

        # The first account created is the administrator.
        conn.execute(
            "UPDATE users SET role = 'admin', plan_key = 'full' WHERE email = ?",
            (ADMIN_EMAIL,)
        )
        conn.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail loudly before serving a single request rather than running insecurely.
    # Logging, not print(): a Windows console defaults to cp1251 and would
    # raise UnicodeEncodeError on Cyrillic.
    for warning in check_config():
        log.warning(warning)
    if not IS_PRODUCTION:
        log.info("ENVIRONMENT=%s - proverkite za produkciya sa izklyucheni.", ENVIRONMENT)
    init_db()
    yield

templates = Jinja2Templates(directory="templates")
# Fix for Jinja2 3.1.6 + Starlette 1.0.1: request object is not hashable
templates.env.cache_size = 0

app = FastAPI(title="МираСкоп", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

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

def plan_is_active(user: dict) -> bool:
    """A paid plan lapses on its expiry date; demo never expires."""
    if not user.get("plan_expires"):
        return True
    try:
        expires = datetime.datetime.fromisoformat(str(user["plan_expires"]))
    except ValueError:
        return True
    return expires.date() >= datetime.date.today()

def effective_plan(user: dict) -> dict:
    """The plan actually in force — falls back to demo once a paid one expires."""
    plan = get_plan(user.get("plan_key")) if plan_is_active(user) else None
    return plan or get_plan("demo") or {
        "key": "demo", "name": "Демо", "max_persons": 2,
        "features": ["chart", "planets", "aspects", "numerology"],
    }

def purchased_features(user_id: int) -> list:
    """Feature keys the user bought outright. These never expire."""
    with sqlite3.connect(DB_PATH) as conn:
        return [r[0] for r in conn.execute(
            "SELECT feature_key FROM feature_purchases WHERE user_id = ?", (user_id,))]

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
            detail = {
                "reason": "locked",
                "feature": feature,
                "feature_name": meta.get("name", feature),
                "message": (
                    f"„{meta.get('name', feature)}“ не е включена в пакета ти."
                    if not offer else
                    f"„{offer['name']}“ не е включена в пакета ти, "
                    f"но можеш да я отключиш еднократно."
                ),
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
def api_login(data: AuthRequest):
    """Login with email/password. Returns JWT token + user info."""
    user = get_user_by_email(data.email)
    if not user or not verify_password(data.password, user["password_hash"]):
        raise HTTPException(401, "Грешен имейл или парола.")
    token = create_token(user["id"], user["email"])
    return {
        "token": token,
        "user": {"id": user["id"], "email": user["email"]}
    }

@app.post("/api/auth/register")
def api_register(data: AuthRequest):
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
            "max_persons": plan.get("max_persons"),
            # Admins are never gated by plan.
            "features": [f["key"] for f in FEATURE_CATALOGUE] if is_admin else plan.get("features", []),
            "expires": row.get("plan_expires"),
            "active": plan_is_active(row),
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
    {"key": "chart", "name": "Натална карта", "note": "Колелото и позициите"},
    {"key": "planets", "name": "Планети", "note": "Списък с обяснения"},
    {"key": "aspects", "name": "Аспекти", "note": "Аспектите в картата"},
    {"key": "numerology", "name": "Нумерология", "note": "Числата и какво означават"},
    {"key": "profile", "name": "За мен", "note": "Личен портрет от картата"},
    {"key": "horoscope", "name": "Дневен хороскоп", "note": "Разчитане на деня"},
    {"key": "period", "name": "Период", "note": "Транзити за диапазон от дати"},
    {"key": "synastry", "name": "Съвместимост", "note": "Синастрия между двама"},
    {"key": "love", "name": "Любовен хороскоп", "note": "Съвместимост по зодия"},
    {"key": "akashic", "name": "Акашови записи", "note": "Кармично разчитане"},
    {"key": "moon", "name": "Лунен календар", "note": "Фазите и какво носят"},
    {"key": "interpretation", "name": "Пълно разчитане", "note": "Цялата карта, тълкувана подробно"},
]

# Default wording for the automated emails; admins can edit these.
EMAIL_TEMPLATES = {
    "welcome_subject": "Добре дошъл в МираСкоп",
    "welcome_body": (
        "Здравей, {name}!\n\n"
        "Акаунтът ти в МираСкоп е готов. Влез и създай първата си натална карта.\n\n"
        "{link}\n\nПоздрави,\nЕкипът на МираСкоп"
    ),
    "expiring_subject": "Абонаментът ти изтича скоро",
    "expiring_body": (
        "Здравей, {name}!\n\n"
        "Абонаментът ти за МираСкоп изтича на {expires}. "
        "След това акаунтът остава активен, но с демо достъп.\n\n"
        "{link}\n\nПоздрави,\nЕкипът на МираСкоп"
    ),
    "expired_subject": "Абонаментът ти изтече",
    "expired_body": (
        "Здравей, {name}!\n\n"
        "Абонаментът ти изтече и акаунтът мина на демо достъп. "
        "Картите и разчитанията ти остават запазени.\n\n"
        "{link}\n\nПоздрави,\nЕкипът на МираСкоп"
    ),
}

# Search-engine settings the admin can edit; these are the defaults the public
# pages fall back to when nothing has been saved yet.
SEO_DEFAULTS = {
    "seo_site_url": "",
    "seo_title": "МираСкоп — твоята натална карта, разчетена на разбираем език",
    "seo_description": (
        "Точна натална карта по Swiss Ephemeris, разчетена на български: кой си, "
        "какво ти предстои днес, кармичните ти теми и нумерологията ти."
    ),
    "seo_keywords": "натална карта, хороскоп, астрология, зодия, нумерология, лунен календар",
    "seo_og_image": "/static/logo-header.png",
    "seo_robots": "index,follow",
    "seo_verification": "",
}

def seo_settings() -> dict:
    """Current SEO values, falling back to the defaults for anything unset."""
    return {key: (get_setting(key) or default) for key, default in SEO_DEFAULTS.items()}

def sky_today() -> list:
    """Where the main bodies actually are right now, for the landing strip.

    The point of the strip is that these are live figures, not decoration —
    so a failure returns nothing and the strip is simply left out.
    """
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
        return [found[n] for n in wanted if n in found]
    except Exception:
        log.warning("sky_today failed; the landing strip will be omitted", exc_info=True)
        return []

def seo_context(request: Request, *, path: str = "/") -> dict:
    """Everything the public templates need to render their meta tags."""
    seo = seo_settings()
    base = (seo["seo_site_url"] or str(request.base_url)).rstrip("/")
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
        expiring = [dict(r) for r in conn.execute(
            "SELECT id, email, plan_key, plan_expires FROM users "
            "WHERE plan_expires IS NOT NULL AND date(plan_expires) <= date('now', '+14 day') "
            "ORDER BY plan_expires LIMIT 20"
        )]
        recent = [dict(r) for r in conn.execute(
            "SELECT p.id, p.amount_cents, p.currency, p.paid_at, p.plan_key, u.email "
            "FROM payments p JOIN users u ON u.id = p.user_id "
            "ORDER BY p.paid_at DESC LIMIT 10"
        )]
    return {
        "users": users, "blocked": blocked, "persons": persons,
        "by_plan": by_plan,
        "revenue_cents": revenue, "revenue_month_cents": revenue_month,
        "expiring": expiring, "recent_payments": recent,
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
    return {"ok": True, "changed": True}

@app.delete("/api/admin/users/{user_id}")
def api_admin_delete_user(user_id: int, admin: dict = Depends(require_admin)):
    """Remove an account together with everything it owns."""
    if user_id == admin["id"]:
        raise HTTPException(400, "Не можеш да изтриеш собствения си акаунт.")
    if not get_user_by_id(user_id):
        raise HTTPException(404, "Потребителят не е намерен.")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM ai_cache WHERE person_id IN (SELECT id FROM persons WHERE user_id = ?)",
            (user_id,))
        conn.execute("DELETE FROM persons WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM payments WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
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
    return {"ok": True}

@app.delete("/api/admin/payments/{payment_id}")
def api_admin_delete_payment(payment_id: int, admin: dict = Depends(require_admin)):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM payments WHERE id = ?", (payment_id,))
        conn.commit()
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
    return {"ok": True}

@app.delete("/api/admin/feature-purchases/{user_id}/{feature_key}")
def api_admin_revoke_feature(user_id: int, feature_key: str,
                             admin: dict = Depends(require_admin)):
    """Take a one-off unlock back. The payment record stays for the books."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM feature_purchases WHERE user_id = ? AND feature_key = ?",
                     (user_id, feature_key))
        conn.commit()
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
        "catalogue": [
            {
                **f,
                "unlocked": f["key"] in unlocked,
                "offer": None if f["key"] in unlocked else feature_offer(f["key"]),
            }
            for f in FEATURE_CATALOGUE
        ],
    }

@app.post("/api/features/{feature_key}/request")
def api_request_feature(feature_key: str, user: Tuple[int, str] = Depends(get_current_user)):
    """Ask to buy a feature. There is no payment processor yet, so this emails
    the admin address and tells the user someone will get back to them."""
    user_id, email = user
    row = get_user_by_id(user_id)
    if not row:
        raise HTTPException(401, "Невалиден акаунт.")
    if feature_key in unlocked_features(row):
        return {"ok": True, "already": True}

    offer = feature_offer(feature_key)
    if not offer:
        raise HTTPException(404, "Тази функция не се продава отделно.")

    to = get_setting("smtp_from") or get_setting("smtp_user")
    if to:
        try:
            send_email(
                to,
                f"Заявка за отключване: {offer['name']}",
                f"Потребител {email} (ID {user_id}) иска да отключи "
                f"„{offer['name']}“ за {offer['price_cents'] / 100:.2f} {offer['currency']}.",
            )
        except HTTPException:
            # A missing SMTP config must not make the button look broken.
            pass
    return {"ok": True, "offer": offer}

@app.get("/api/admin/settings")
def api_admin_settings(admin: dict = Depends(require_admin)):
    """App-wide settings: AI key status, SMTP and email templates."""
    ai_key = get_setting("ai_api_key")
    smtp_pass = get_setting("smtp_password")
    return {
        "ai": {
            "provider": get_setting("ai_provider") or "deepseek",
            "key_set": bool(ai_key),
            "key_masked": ("•" * 8 + ai_key[-4:]) if ai_key and len(ai_key) > 4 else None,
        },
        "smtp": {
            "host": get_setting("smtp_host") or "",
            "port": get_setting("smtp_port") or "587",
            "user": get_setting("smtp_user") or "",
            "from": get_setting("smtp_from") or "",
            "use_tls": (get_setting("smtp_use_tls") or "1") == "1",
            "password_set": bool(smtp_pass),
        },
        "templates": {
            key: get_setting(f"tpl_{key}") or default
            for key, default in EMAIL_TEMPLATES.items()
        },
        "seo": seo_settings(),
    }

@app.post("/api/admin/settings")
def api_admin_save_settings(payload: dict, admin: dict = Depends(require_admin)):
    """Save whichever settings were supplied; blank values leave secrets alone."""
    ai = payload.get("ai") or {}
    if ai.get("provider"):
        set_setting("ai_provider", ai["provider"])
    if (ai.get("key") or "").strip():
        set_setting("ai_api_key", ai["key"].strip())

    smtp = payload.get("smtp") or {}
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

    return {"ok": True}

def send_email(to: str, subject: str, body: str, attachment: tuple = None) -> None:
    """Send a message over the configured SMTP server.

    `attachment` is an optional (filename, bytes, mimetype) triple.
    Raises HTTPException with a readable message on failure.
    """
    import smtplib
    from email.message import EmailMessage

    host = get_setting("smtp_host")
    if not host:
        raise HTTPException(400, "SMTP сървърът не е конфигуриран. Задай го в Настройки.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = get_setting("smtp_from") or get_setting("smtp_user") or "noreply@miraskop.bg"
    msg["To"] = to
    msg.set_content(body)

    if attachment:
        filename, data, mimetype = attachment
        maintype, _, subtype = mimetype.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream",
                           filename=filename)

    user = get_setting("smtp_user")
    password = get_setting("smtp_password") or ""
    port = int(get_setting("smtp_port") or 587)
    use_tls = (get_setting("smtp_use_tls") or "1") == "1"

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

@app.post("/api/admin/settings/test-email")
def api_admin_test_email(payload: dict, admin: dict = Depends(require_admin)):
    """Send a test message through the configured SMTP server."""
    to = (payload.get("to") or "").strip()
    if "@" not in to:
        raise HTTPException(400, "Въведи валиден имейл адрес.")
    send_email(to, "Тестов имейл от МираСкоп",
               "Това е тестово съобщение. Ако го получаваш, SMTP настройките работят.")
    return {"ok": True}

# --- Settings API Routes (AUTH REQUIRED) ---
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
            "max_persons": plan.get("max_persons"),
            "expires": row.get("plan_expires"),
            "active": plan_is_active(row),
        },
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
        headers={"User-Agent": "MiraSkop/1.0 (astrology chart app)"},
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

# --- API Routes (AUTH REQUIRED) ---
@app.get("/api/persons")
def api_list_persons(user: Tuple[int, str] = Depends(get_current_user)):
    user_id, email = user
    persons = get_all_persons(user_id)
    row = get_user_by_id(user_id)
    is_admin = bool(row and row.get("role") == "admin")
    limit = None if is_admin else (effective_plan(row).get("max_persons") if row else None)
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
        plan = effective_plan(row)
        limit = plan.get("max_persons") or 0
        with sqlite3.connect(DB_PATH) as conn:
            used = conn.execute(
                "SELECT COUNT(*) FROM persons WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
        if limit and used >= limit:
            raise HTTPException(
                402,
                f"Пакетът „{plan.get('name')}“ позволява до {limit} "
                f"{'карта' if limit == 1 else 'карти'}. Изтрий някоя или премини на по-голям пакет."
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
def api_natal_chart(person_id: int, user: Tuple[int, str] = Depends(get_current_user)):
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")
    return compute_natal(p)

@app.post("/api/persons/{person_id}/natal")
def api_natal_chart_update(
    person_id: int,
    data: BirthDataUpdate,
    user: Tuple[int, str] = Depends(get_current_user),
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
def api_natal_chart_text(person_id: int, user: Tuple[int, str] = Depends(get_current_user)):
    """Return natal chart as plain text."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")
    chart_data = compute_natal(p)
    text = natal_to_text(p, chart_data)
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")

@app.get("/api/persons/{person_id}/chart.svg")
def api_chart_svg(person_id: int, user: Tuple[int, str] = Depends(get_current_user)):
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
- ЛОГИКА: върви от общото към конкретното, така че читателят да вижда връзката между данните и изводите.
- ДЪЛЖИНА: бъди подробен — всяка секция с по няколко изречения реално съдържание, а изброяванията с кратко обяснение защо, не само голи думи.
- Бъди конкретен, избягвай клишета. Обяснявай астрологичните термини накратко, за да е разбираемо и за човек без познания.
- Основавай се единствено на подадените данни, без да добавяш измислени детайли.
- ГЛАС: пиши като астролог, който чете конкретната карта пред себе си. Не се
  представяй, не описвай процеса си и не споменавай, че си модел, асистент или
  програма. Никакви уводи от рода на "като изкуствен интелект", "въз основа на
  предоставените данни ще генерирам" или "надявам се това да е полезно".
- Започвай направо с разчитането. Без "Разбира се", "Ето", "С удоволствие".

=== ПРАВОПИС (задължително) ===
Пиши на книжовен български. Внимавай особено за:
- "в" / "във": пълната форма "във" се пише САМО пред думи, започващи с "в" или "ф" (във въздуха, във фокуса). Иначе винаги "в" (в дома, в знака, в картата).
- "с" / "със": пълната форма "със" се пише САМО пред думи, започващи със "с" или "з" (със Сатурн, със знанието). Иначе винаги "с" (с Луната, с търпение, с хората).
- Пълен и кратък член: пълен член (-ът, -ят) само при подлог (Сатурн е учителят); кратък (-а, -я) при допълнение (виждаш учителя).
- Пълните форми на местоименията: "него/нея" след предлог, "го/я" като кратка форма.
- Не пропускай запетаи пред "който", "която", "което", "които", "че", "но", "а".
- Внимавай с бройната форма: два/три + мъжки род = "два аспекта", "три знака" (не "аспекти"/"знакове")."""

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
def api_profile(person_id: int, user: Tuple[int, str] = Depends(require_feature("profile"))):
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
            interpretation = call_ai(ai_key, provider, prompt, max_tokens=5000)
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
            interpretation = call_ai(ai_key, provider, prompt, max_tokens=7000)
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
            interpretation = call_ai(ai_key, provider, prompt, max_tokens=4000)
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
            interpretation = call_ai(ai_key, provider, prompt, max_tokens=5000)
            set_ai_cache(data.person_id, cache_key, interpretation)
            return {"interpretation": interpretation, "cached": False, "cache_key": cache_key}
        except AIError as e:
            return {"interpretation": ai_failure_message(e)}
        except Exception as e:
            return {"interpretation": ai_failure_message(e)}

    return {"interpretation": AI_UNAVAILABLE}


@app.post("/api/synastry")
def api_synastry(data: SynastryRequest, user: Tuple[int, str] = Depends(require_feature("synastry"))):
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
def api_synastry_interpretation(data: SynastryRequest, refresh: bool = False, user: Tuple[int, str] = Depends(require_feature("synastry"))):
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
            interpretation = call_ai(ai_key, provider, prompt, max_tokens=3000)
            set_ai_cache(person_id, cache_key, interpretation)
            return {"interpretation": interpretation, "cached": False, "cache_key": cache_key}
        except AIError as e:
            return {"interpretation": ai_failure_message(e)}
        except Exception as e:
            return {"interpretation": ai_failure_message(e)}

    return {"interpretation": AI_UNAVAILABLE}

@app.post("/api/transits")
def api_transits(data: TransitsRequest, user: Tuple[int, str] = Depends(get_current_user)):
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

    if not refresh:
        cached = get_ai_cache(person_id, cache_key)
        if cached:
            summary, body = split_summary(cached["content"])
            return {"interpretation": body, "summary": summary,
                    "date": now.strftime("%d.%m.%Y"), "cached": True, "cache_key": cache_key}

    transit_data = compute_transits(p, now)

    aspects_lines = []
    for a in transit_data.get("transit_aspects_to_natal", []):
        if a["type"] not in {"Conjunction", "Sextile", "Square", "Trine", "Opposition"}:
            continue
        orb = f", орб {a['orb']:.1f}°" if a.get("orb") is not None else ""
        aspects_lines.append(f"- {a['active']} (транзит) {a['type']} {a['passive']} (натал){orb}")

    date_bg = now.strftime("%d.%m.%Y")

    prompt = f"""Ти си професионален астролог. Направи ДНЕВЕН ХОРОСКОП за {date_bg} за конкретния човек, СТРИКТНО базиран на точните транзитни данни по-долу (изчислени астрономически със Swiss Ephemeris). Не измисляй позиции или аспекти извън изброените — обясни само какво ОЗНАЧАВАТ.

Име: {p['name']}
Малко име (обръщай се само с него): {first_name(p['name'])}
Дата на анализа: {date_bg}

=== ФОН НА ДЕНЯ ===
Форма на транзитната карта: {transit_data.get('shape', 'N/A')}
Лунна фаза днес: {transit_data.get('moon_phase', 'N/A')}

=== АКТИВНИ ТРАНЗИТНИ АСПЕКТИ КЪМ НАТАЛНАТА КАРТА ===
{chr(10).join(aspects_lines) if aspects_lines else "Няма значими активни аспекти днес."}

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
2. **Разчитане на аспектите** — за всеки значим аспект от списъка обясни конкретно какво носи. Обяснявай термините накратко (напр. "квадрат — напрежение, което подтиква към действие").
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
    if ai_key:
        try:
            raw = call_ai(ai_key, provider, prompt, max_tokens=6000)
            set_ai_cache(person_id, cache_key, raw)
            summary, body = split_summary(raw)
            return {"interpretation": body, "summary": summary, "date": date_bg,
                    "cached": False, "cache_key": cache_key}
        except AIError as e:
            return {"interpretation": ai_failure_message(e), "date": date_bg}
        except Exception as e:
            return {"interpretation": ai_failure_message(e), "date": date_bg}

    return {"interpretation": AI_UNAVAILABLE, "date": date_bg}

MAJOR_ASPECT_TYPES = {"Conjunction", "Sextile", "Square", "Trine", "Opposition"}
# Fast-moving transit bodies (Moon, and daily-recalculated angles like Asc/MC) create
# a new "aspect" almost every day, drowning out the slower, more meaningful transits.
# The period view only tracks transiting bodies from Mercury outward.
PERIOD_TRANSIT_BODIES = {
    "Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune", "Pluto", "Chiron",
}

@app.post("/api/period-influence")
def api_period_influence(data: PeriodRequest, user: Tuple[int, str] = Depends(get_current_user)):
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
            interpretation = call_ai(ai_key, provider, prompt, max_tokens=4000)
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

def call_ai(api_key: str, provider: str, prompt: str, max_tokens: int = 4000) -> str:
    """Call the configured AI provider's chat completion endpoint and return the text."""
    import urllib.request
    import urllib.error

    try:
        if provider == "anthropic":
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps({
                    "model": "claude-sonnet-4-5",
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
            url = "https://api.deepseek.com/v1/chat/completions"
            model = "deepseek-chat"
        else:
            url = "https://api.openai.com/v1/chat/completions"
            model = "gpt-4o-mini"
        req = urllib.request.Request(
            url,
            data=json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": max_tokens
            }).encode(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read())
            return clean_bg(result["choices"][0]["message"]["content"])
    except urllib.error.HTTPError as e:
        raise AIError(_explain_http_error(provider, e)) from e
    except TimeoutError:
        raise AIError(f"{provider} отне прекалено дълго да отговори (над 3 минути). Опитайте отново — генерирането на дълъг текст понякога отнема повече време.")
    except urllib.error.URLError as e:
        raise AIError(f"Няма връзка с {provider}: {e.reason}") from e

@app.get("/api/persons/{person_id}/interpretation")
def api_interpretation(person_id: int, refresh: bool = False,
                       user: Tuple[int, str] = Depends(require_feature("interpretation"))):
    """Generate AI interpretation of a natal chart. Cached — pass ?refresh=true to regenerate."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Този човек не е намерен в профила ти.")

    cache_key = "natal"
    if not refresh:
        cached = get_ai_cache(person_id, cache_key)
        if cached:
            return {"interpretation": cached["content"], "cached": True,
                    "generated_at": cached["generated_at"], "cache_key": cache_key}

    chart_data = compute_natal(p)

    # Build prompt for AI
    sun = moon = rising = "Unknown"
    planets_lines = []
    for oid, obj in chart_data["objects"].items():
        name = obj["name"]
        retro = " (ретрограден)" if obj.get("movement") == "Retrograde" else ""
        line = f"- {name}: {obj['sign']} {obj['sign_longitude']}, {obj['house']}{retro}"
        planets_lines.append(line)
        if name == "Sun": sun = line
        if name == "Moon": moon = line
        if name == "Asc": rising = line

    houses_lines = [f"- {h['number']}-ти дом: начало в {h['sign']} {h['sign_longitude']}"
                     for h in chart_data.get("houses", [])]

    aspects_lines = []
    for a in chart_data["aspects"]:
        orb = f", орб {a['orb']:.1f}°" if a.get("orb") is not None else ""
        aspects_lines.append(f"- {a['active']} {a['type']} {a['passive']}{orb}")

    prompt = f"""Ти си професионален астролог с дългогодишен опит. Интерпретирай СТРИКТНО следната натална карта, изчислена астрономически точно с Swiss Ephemeris. Не измисляй, не добавяй и не променяй никакви позиции, знаци, домове или аспекти извън изброените по-долу — те са точен астрономически факт. Твоята задача е да ОБЯСНИШ подробно, задълбочено и практично какво означават дадените данни за живота на човека — не просто да ги изредиш.

=== ДАННИ ЗА ЛИЧНОСТТА ===
Име: {chart_data['native']['name']}
Малко име (обръщай се само с него): {first_name(chart_data['native']['name'])}
Дата и час на раждане: {chart_data['native']['datetime']}
Място: {chart_data['native']['lat']}, {chart_data['native']['lon']} ({chart_data['native']['timezone']})

Слънце: {sun}
Луна: {moon}
Асцендент: {rising}

=== ВСИЧКИ ПЛАНЕТИ И ТОЧКИ (точни изчислени позиции) ===
{chr(10).join(planets_lines)}

=== ДОМОВЕ (система Плацидус) ===
{chr(10).join(houses_lines) if houses_lines else "Няма данни"}

=== ВСИЧКИ АСПЕКТИ (точно изчислени, с орб) ===
{chr(10).join(aspects_lines) if aspects_lines else "Няма данни"}

=== ОБЩИ ХАРАКТЕРИСТИКИ ===
Форма на хороскопа: {chart_data.get('shape', 'N/A')}
Лунна фаза: {chart_data.get('moon_phase', 'N/A')}
Дневно/Нощно раждане: {'Дневно' if chart_data.get('diurnal') else 'Нощно'}
Домова система: {chart_data.get('house_system', 'Placidus')}

=== ЗАДАЧА ===
Направи ПОДРОБНА и ИЗЧЕРПАТЕЛНА интерпретация (не кратко резюме — реален задълбочен анализ, всеки раздел с по няколко изречения конкретен коментар, не общи фрази) в следната структура:

1. **Обща характеристика на личността** — синтез на Слънце/Луна/Асцендент триадата, темперамент, доминиращи стихии (огън/земя/въздух/вода) и качества (кардинални/фиксирани/променливи) сред планетите.
2. **Слънце, Луна и Асцендент подробно** — всяко поотделно: какво означава знакът и домът им конкретно за тази карта, после как трите си взаимодействат.
3. **Меркурий, Венера, Марс** — стил на мислене/комуникация, стил на обич и естетика, начин на действие и желание.
4. **Социалните и поколенчески планети** (Юпитер, Сатурн, Уран, Нептун, Плутон) — къде носят растеж/ограничения/трансформация в конкретните домове.
5. **Домовете** — кои области от живота (кариера, дом, взаимоотношения и т.н.) са най-акцентирани заради концентрация на планети, и какво означава това практически.
6. **Силни страни и предизвикателства** — конкретни, изведени от реалните аспекти, не общи клишета.
7. **Любов и взаимоотношения** — базирано на Венера, 7-ми дом, аспекти към тях.
8. **Кариера и призвание** — базирано на MC, 10-ти дом, Сатурн, Слънце.
9. **Кармични уроци** — Лунни възли (Северен/Южен), какво трябва да развие и какво да остави.
10. **Най-важните 5-8 аспекта** — обяснени поотделно, всеки с конкретно практическо значение.
""" + STYLE_RULES

    ai_key, provider = get_ai_config()
    if ai_key:
        try:
            interpretation = call_ai(ai_key, provider, prompt, max_tokens=6000)
            set_ai_cache(person_id, cache_key, interpretation)
            return {"interpretation": interpretation, "cached": False, "cache_key": cache_key}
        except AIError as e:
            return {"interpretation": ai_failure_message(e)}
        except Exception as e:
            return {"interpretation": ai_failure_message(e)}

    return {"interpretation": AI_UNAVAILABLE}

# --- PDF export and email delivery ---

# Every reading the user can export. The label becomes the PDF's title, and the
# cache key is either fixed or a prefix the client completes (date, period, sign).
READING_TITLES = {
    "natal":      "Тълкуване на наталната карта",
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
    )

    safe = re.sub(r"[^0-9A-Za-zА-Яа-я]+", "-", person["name"]).strip("-") or "razchitane"
    # Only the first key segment goes in the filename; suffixes like a partner's
    # full birth data would make it unreadable.
    base, _, rest = cache_key.partition(":")
    slug = base if base in ("love-full", "natal", "profile", "akashic") else \
        re.sub(r"[^0-9A-Za-z-]+", "-", cache_key).strip("-")
    return pdf, f"MiraSkop-{safe}-{slug}.pdf"

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

    send_email(
        to,
        f"{title} — {person['name']}",
        f"Здравей!\n\n"
        f"Прикачено е разчитането „{title}“ за {name}, изготвено от МираСкоп.\n"
        f"Позициите в него са изчислени със Swiss Ephemeris.\n\n"
        f"Приятно четене!\n— МираСкоп",
        attachment=(filename, pdf, "application/pdf"),
    )
    return {"ok": True, "to": to}

# --- Web UI Routes ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Landing page. Client-side JS sends already-signed-in visitors to the dashboard."""
    return HTMLResponse(templates.get_template("landing.html").render(
        {"request": request, "sky": sky_today(), **seo_context(request, path="/")}))

@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt(request: Request):
    """Crawler rules. The private app pages are never worth indexing."""
    seo = seo_settings()
    base = (seo["seo_site_url"] or str(request.base_url)).rstrip("/")
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
    base = (seo["seo_site_url"] or str(request.base_url)).rstrip("/")
    today = datetime.date.today().isoformat()
    urls = "".join(
        f"<url><loc>{base}{path}</loc><lastmod>{today}</lastmod>"
        f"<changefreq>{freq}</changefreq><priority>{prio}</priority></url>"
        for path, freq, prio in [
            ("/", "weekly", "1.0"),
            ("/register", "monthly", "0.6"),
            ("/login", "monthly", "0.3"),
        ]
    )
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f"{urls}</urlset>")
    return Response(content=xml, media_type="application/xml")

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return HTMLResponse(templates.get_template("register.html").render({"request": request}))

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return HTMLResponse(templates.get_template("login.html").render({"request": request}))

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Dashboard — client-side JS handles auth check via localStorage token."""
    return HTMLResponse(templates.get_template("dashboard.html").render({"request": request}))

@app.get("/chart/{person_id}", response_class=HTMLResponse)
async def view_chart(request: Request, person_id: int):
    """Chart view — uses token from localStorage on client side."""
    # Try to get user from Bearer token in request (header, falling back to query string for plain navigation)
    user_id = None
    token = request.headers.get("Authorization", "").replace("Bearer ", "") or request.query_params.get("token", "")
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

@app.get("/healthz")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
