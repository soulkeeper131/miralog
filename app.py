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
            "sign": sign,
            "sign_bg": tr_sign(sign),
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
Дата на раждане: {p['day']}.{p['month']}.{p['year']}

Число на съдбата (Life Path): {profile['life_path']['number']}
Число на изразяването (от пълното име): {profile['expression']['number']}
Число на душевния копнеж (гласни от името): {profile['soul_urge']['number']}
Число на личността (съгласни от името): {profile['personality']['number']}
Число на рождения ден: {profile['birthday']['number']}
Лично число за {profile['personal_year']['year']} година: {profile['personal_year']['number']}

Моля, направи пълна интерпретация включваща:
1. Число на съдбата — основен жизнен път и цел
2. Число на изразяването — таланти и как се проявяват навън
3. Душевен копнеж — вътрешни желания и мотивация
4. Личност — как те възприемат другите
5. Лична година — на какво да наблегне тази година
6. Как числата си взаимодействат — хармония или напрежение между тях

Пиши на български, с ясен и практичен език. Основавай се единствено на изброените по-горе числа."""

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
Име: {p1['name']}
Дата на раждане: {p1['year']}-{p1['month']:02d}-{p1['day']:02d} {p1['hour']:02d}:{p1['minute']:02d}

ВТОРИ ЧОВЕК:
Име: {p2['name']}
Дата на раждане: {p2['year']}-{p2['month']:02d}-{p2['day']:02d} {p2['hour']:02d}:{p2['minute']:02d}

Форма на съвместимостта: {composite.get('shape_bg', composite.get('shape', 'N/A'))}
Лунна фаза: {composite.get('moon_phase_bg', composite.get('moon_phase', 'N/A'))}

Основни аспекти между тях:
{chr(10).join(aspects_text) if aspects_text else "Няма данни"}

Моля, направи пълна интерпретация включваща:
1. Обща характеристика на връзката — каква е динамиката между двамата
2. Емоционална съвместимост — как се разбират на чувствено ниво
3. Комуникация и интелектуална връзка — как общуват и мислят заедно
4. Силни страни на връзката — какво ги сближава и прави добър екип
5. Предизвикателства — къде може да има търкания и как да ги преодолеят
6. Романтична и физическа химия
7. Дългосрочен потенциал — какво показват звездите за бъдещето им

Пиши на български, с топъл и разбираем език. Обърни се директно към тях (използвай имената им)."""

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
            return {"interpretation": cached["content"], "date": now.strftime("%d.%m.%Y"), "cached": True}

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
Дата на анализа: {date_bg}

=== ФОН НА ДЕНЯ ===
Форма на транзитната карта: {transit_data.get('shape', 'N/A')}
Лунна фаза днес: {transit_data.get('moon_phase', 'N/A')}

=== АКТИВНИ ТРАНЗИТНИ АСПЕКТИ КЪМ НАТАЛНАТА КАРТА ===
{chr(10).join(aspects_lines) if aspects_lines else "Няма значими активни аспекти днес."}

=== ЗАДАЧА ===
Напиши подробен, практичен дневен хороскоп в следната структура:
1. **Общо усещане за деня** — 2-3 изречения обобщение на енергията на деня.
2. **Разчитане на всеки значим транзитен аспект** — за всеки от списъка обясни конкретно какво носи (възможности, предизвикателства, теми, които изникват).
3. **На какво да обърне внимание** — 2-3 практични съвета за деня, изведени пряко от аспектите.
4. **Емоции и настроение** — базирано на транзитите към Луната и личните планети.
5. **Кратко обобщение** — 1-2 изречения "essence" на деня.

Пиши на български, топло и практично, все едно говориш директно на човека. Основавай се единствено на изброените по-горе аспекти, без да добавяш измислени детайли."""

    ai_key, provider = get_ai_config()
    if ai_key:
        try:
            interpretation = call_ai(ai_key, provider, prompt, max_tokens=3000)
            set_ai_cache(person_id, cache_key, interpretation)
            return {"interpretation": interpretation, "date": date_bg, "cached": False}
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

Пиши на български, с топъл но професионален и практичен език — все едно говориш директно на човека. Основавай се единствено на изброените по-горе данни, без да добавяш измислени детайли. Целта е дълъг, наситен с конкретика текст, не кратко обобщение."""

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
    """Root: redirect to /dashboard if token cookie exists, else /login"""
    # Check for a simple cookie hint or just serve login — JS handles token check
    return HTMLResponse(templates.get_template("login.html").render({"request": request}))

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
