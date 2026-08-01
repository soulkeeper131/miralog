import os, json, sqlite3, datetime
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
    ELEMENTS_BG, MODALITIES_BG, ELEMENT_MEANINGS, MODALITY_MEANINGS,
    SIGNS, ZODIAC_ORDER,
)
from numerology import compute_numerology

# --- App Setup ---
DB_PATH = Path(__file__).parent / "data" / "persons.db"
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production-secret-key")
ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = 60 * 24 * 30  # 30 days
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@miralog.bg")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

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
        conn.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
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

class SettingsUpdate(BaseModel):
    ai_api_key: Optional[str] = None
    ai_provider: Optional[str] = None

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
        raise HTTPException(401, "Not authenticated. Use Bearer token in Authorization header.")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
        email = payload["email"]
        return user_id, email
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")

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
        raise HTTPException(401, "Invalid email or password")
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
    """Get current authenticated user from token."""
    user_id, email = user
    return {"id": user_id, "email": email}

# --- Settings API Routes (AUTH REQUIRED) ---
@app.get("/api/settings")
def api_get_settings(user: Tuple[int, str] = Depends(get_current_user)):
    """Return current settings. The API key is masked, never sent back in full."""
    key = get_setting("ai_api_key")
    provider = get_setting("ai_provider") or "deepseek"
    masked = ("•" * 8 + key[-4:]) if key and len(key) > 4 else ("•" * 8 if key else None)
    return {"ai_provider": provider, "ai_api_key_set": bool(key), "ai_api_key_masked": masked}

@app.post("/api/settings")
def api_update_settings(data: SettingsUpdate, user: Tuple[int, str] = Depends(get_current_user)):
    """Update settings. Only non-empty fields are changed."""
    if data.ai_api_key is not None and data.ai_api_key.strip():
        set_setting("ai_api_key", data.ai_api_key.strip())
    if data.ai_provider is not None and data.ai_provider.strip():
        set_setting("ai_provider", data.ai_provider.strip())
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
    return {"persons": get_all_persons(user_id)}

@app.get("/api/persons/{person_id}")
def api_get_person(person_id: int, user: Tuple[int, str] = Depends(get_current_user)):
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Person not found")
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
            raise HTTPException(404, "Person not found")
    return {"deleted": person_id}

@app.get("/api/persons/{person_id}/natal")
def api_natal_chart(person_id: int, user: Tuple[int, str] = Depends(get_current_user)):
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Person not found")
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
        raise HTTPException(404, "Person not found")
    if not update_person(person_id, user_id, data):
        raise HTTPException(500, "Failed to update person")
    clear_ai_cache(person_id)
    p = get_person(person_id, user_id)
    return compute_natal(p)

@app.get("/api/persons/{person_id}/natal.txt")
def api_natal_chart_text(person_id: int, user: Tuple[int, str] = Depends(get_current_user)):
    """Return natal chart as plain text."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Person not found")
    chart_data = compute_natal(p)
    text = natal_to_text(p, chart_data)
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")

@app.get("/api/persons/{person_id}/chart.svg")
def api_chart_svg(person_id: int, user: Tuple[int, str] = Depends(get_current_user)):
    """Return natal chart as SVG."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Person not found")
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
def api_profile(person_id: int, user: Tuple[int, str] = Depends(get_current_user)):
    """Computed 'about me' profile — deterministic, no AI."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Person not found")
    return build_profile(compute_natal(p))

@app.get("/api/persons/{person_id}/profile/interpretation")
def api_profile_interpretation(person_id: int, refresh: bool = False,
                               user: Tuple[int, str] = Depends(get_current_user)):
    """AI 'about me' reading — strengths, weaknesses and what makes this chart distinctive."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Person not found")

    if not refresh:
        cached = get_ai_cache(person_id, "profile")
        if cached:
            return {"interpretation": cached["content"], "cached": True,
                    "generated_at": cached["generated_at"]}

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
            set_ai_cache(person_id, "profile", interpretation)
            return {"interpretation": interpretation, "cached": False}
        except AIError as e:
            return {"interpretation": f"⚠️ {str(e)}"}
        except Exception as e:
            return {"interpretation": f"⚠️ Неочаквана грешка: {str(e)}"}

    return {"interpretation": "⚠️ Няма конфигуриран AI API ключ. Задайте го в Настройки."}

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
def api_akashic(person_id: int, user: Tuple[int, str] = Depends(get_current_user)):
    """The karmic markers the akashic reading is built on (computed, no AI)."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Person not found")
    numerology = compute_numerology(p["name"], p["year"], p["month"], p["day"])
    return build_karmic(compute_natal(p), numerology)

@app.get("/api/persons/{person_id}/akashic/interpretation")
def api_akashic_interpretation(person_id: int, refresh: bool = False,
                               user: Tuple[int, str] = Depends(get_current_user)):
    """Akashic-records style reading of the chart's karmic markers.

    Framed as contemplative interpretation, not as retrieved record: there is no
    data source for akashic records, so the reading stays anchored to the chart.
    """
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Person not found")

    if not refresh:
        cached = get_ai_cache(person_id, "akashic")
        if cached:
            return {"interpretation": cached["content"], "cached": True,
                    "generated_at": cached["generated_at"]}

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
            set_ai_cache(person_id, "akashic", interpretation)
            return {"interpretation": interpretation, "cached": False}
        except AIError as e:
            return {"interpretation": f"⚠️ {str(e)}"}
        except Exception as e:
            return {"interpretation": f"⚠️ Неочаквана грешка: {str(e)}"}

    return {"interpretation": "⚠️ Няма конфигуриран AI API ключ. Задайте го в Настройки."}

@app.get("/api/persons/{person_id}/numerology")
def api_numerology(person_id: int, user: Tuple[int, str] = Depends(get_current_user)):
    """Compute the Pythagorean numerology profile for a person (deterministic, no AI)."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Person not found")
    return compute_numerology(p["name"], p["year"], p["month"], p["day"])

@app.get("/api/persons/{person_id}/numerology/interpretation")
def api_numerology_interpretation(person_id: int, refresh: bool = False, user: Tuple[int, str] = Depends(get_current_user)):
    """Generate AI interpretation of a person's numerology profile. Cached per year — pass ?refresh=true to regenerate."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Person not found")

    current_year = datetime.date.today().year
    cache_key = f"numerology:{current_year}"
    if not refresh:
        cached = get_ai_cache(person_id, cache_key)
        if cached:
            return {"interpretation": cached["content"], "cached": True, "generated_at": cached["generated_at"]}

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
            return {"interpretation": interpretation, "cached": False}
        except AIError as e:
            return {"interpretation": f"⚠️ {str(e)}"}
        except Exception as e:
            return {"interpretation": f"⚠️ Неочаквана грешка: {str(e)}"}

    return {"interpretation": "⚠️ Няма конфигуриран AI API ключ. Задайте го в Настройки."}


LOVE_POINTS = ("Sun", "Moon", "Venus", "Mars", "Asc")

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
def api_love_match(data: LoveMatchRequest, user: Tuple[int, str] = Depends(get_current_user)):
    """Love compatibility — full charts when birth data is given, otherwise sign to sign."""
    user_id, email = user
    p = get_person(data.person_id, user_id)
    if not p:
        raise HTTPException(404, "Person not found")
    return resolve_love_match(data, p)

@app.post("/api/love-match/interpretation")
def api_love_match_interpretation(data: LoveMatchRequest, refresh: bool = False,
                                  user: Tuple[int, str] = Depends(get_current_user)):
    """AI love reading — uses the partner's full chart when available."""
    user_id, email = user
    p = get_person(data.person_id, user_id)
    if not p:
        raise HTTPException(404, "Person not found")

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
                    "generated_at": cached["generated_at"]}

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
            return {"interpretation": interpretation, "cached": False}
        except AIError as e:
            return {"interpretation": f"⚠️ {str(e)}"}
        except Exception as e:
            return {"interpretation": f"⚠️ Неочаквана грешка: {str(e)}"}

    return {"interpretation": "⚠️ Няма конфигуриран AI API ключ. Задайте го в Настройки."}


@app.post("/api/synastry")
def api_synastry(data: SynastryRequest, user: Tuple[int, str] = Depends(get_current_user)):
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
def api_synastry_interpretation(data: SynastryRequest, refresh: bool = False, user: Tuple[int, str] = Depends(get_current_user)):
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
            return {"interpretation": interpretation, "cached": False}
        except AIError as e:
            return {"interpretation": f"⚠️ {str(e)}"}
        except Exception as e:
            return {"interpretation": f"⚠️ Неочаквана грешка: {str(e)}"}

    return {"interpretation": "⚠️ Няма конфигуриран AI API ключ. Задайте го в Настройки."}

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
        raise HTTPException(400, "Invalid target_date format. Use ISO format: YYYY-MM-DDTHH:MM:SS")
    return compute_transits(p, target_date)

@app.get("/api/persons/{person_id}/daily-horoscope")
def api_daily_horoscope(person_id: int, refresh: bool = False, user: Tuple[int, str] = Depends(get_current_user)):
    """Generate an AI-written interpretation of today's transits to the person's natal chart.
    Cached per calendar day — pass ?refresh=true to force a new generation for today."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Person not found")

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
                    "date": now.strftime("%d.%m.%Y"), "cached": True}

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
            return {"interpretation": body, "summary": summary, "date": date_bg, "cached": False}
        except AIError as e:
            return {"interpretation": f"⚠️ {str(e)}", "date": date_bg}
        except Exception as e:
            return {"interpretation": f"⚠️ Неочаквана грешка: {str(e)}", "date": date_bg}

    return {"interpretation": "⚠️ Няма конфигуриран AI API ключ. Задайте го в Настройки.", "date": date_bg}

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
        raise HTTPException(400, "Invalid date format. Use ISO format: YYYY-MM-DD")

    if start > end:
        raise HTTPException(400, "start_date must be before end_date")
    if (end - start).days > 62:
        raise HTTPException(400, "Period too long. Maximum range is 62 days.")

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
                              user: Tuple[int, str] = Depends(get_current_user)):
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
                    "generated_at": cached["generated_at"]}

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
            return {"interpretation": interpretation, "cached": False}
        except AIError as e:
            return {"interpretation": f"⚠️ {str(e)}"}
        except Exception as e:
            return {"interpretation": f"⚠️ Неочаквана грешка: {str(e)}"}

    return {"interpretation": "⚠️ Няма конфигуриран AI API ключ. Задайте го в Настройки."}

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
                return result["content"][0]["text"]

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
            return result["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        raise AIError(_explain_http_error(provider, e)) from e
    except TimeoutError:
        raise AIError(f"{provider} отне прекалено дълго да отговори (над 3 минути). Опитайте отново — генерирането на дълъг текст понякога отнема повече време.")
    except urllib.error.URLError as e:
        raise AIError(f"Няма връзка с {provider}: {e.reason}") from e

@app.get("/api/persons/{person_id}/interpretation")
def api_interpretation(person_id: int, refresh: bool = False, user: Tuple[int, str] = Depends(get_current_user)):
    """Generate AI interpretation of a natal chart. Cached — pass ?refresh=true to regenerate."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Person not found")

    if not refresh:
        cached = get_ai_cache(person_id, "natal")
        if cached:
            return {"interpretation": cached["content"], "cached": True, "generated_at": cached["generated_at"]}

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
            set_ai_cache(person_id, "natal", interpretation)
            return {"interpretation": interpretation, "cached": False}
        except AIError as e:
            return {"interpretation": f"⚠️ {str(e)}"}
        except Exception as e:
            return {"interpretation": f"⚠️ Неочаквана грешка: {str(e)}"}

    return {"interpretation": "⚠️ Няма конфигуриран AI API ключ. Задайте го в Настройки."}

# --- Web UI Routes ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Landing page. Client-side JS sends already-signed-in visitors to the dashboard."""
    return HTMLResponse(templates.get_template("landing.html").render({"request": request}))

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
        raise HTTPException(404, "Person not found")
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

@app.get("/healthz")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
