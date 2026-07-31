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
        conn.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

templates = Jinja2Templates(directory="templates")
# Fix for Jinja2 3.1.6 + Starlette 1.0.1: request object is not hashable
templates.env.cache_size = 0

app = FastAPI(title="Миралог", lifespan=lifespan)
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
        result[str(obj.index)] = {
            "name": obj.name,
            "type": obj.type.name if hasattr(obj.type, 'name') else str(obj.type),
            "sign": obj.sign.name,
            "sign_longitude": obj.sign_longitude.formatted,
            "longitude": obj.longitude.formatted,
            "house": obj.house.name if hasattr(obj.house, 'name') else str(obj.house.number),
            "house_number": obj.house.number,
            "speed": obj.speed if hasattr(obj, 'speed') else None,
            "movement": obj.movement.formatted if hasattr(obj, 'movement') and obj.movement else None,
        }
    return result

def serialize_aspects(aspects: dict) -> list:
    """Serialize chart aspects to JSON-friendly format.
    Aspects are nested: {active_id: {passive_id: Aspect}}"""
    result = []
    for active_id, passive_dict in aspects.items():
        for passive_id, aspect in passive_dict.items():
            result.append({
                "type": aspect.type if isinstance(aspect.type, str) else aspect.type.name,
                "active": aspect._active_name if hasattr(aspect, '_active_name') else str(aspect.active),
                "passive": aspect._passive_name if hasattr(aspect, '_passive_name') else str(aspect.passive),
                "aspect_angle": aspect.aspect if hasattr(aspect, 'aspect') else None,
                "orb": aspect.orb if hasattr(aspect, 'orb') else None,
                "distance": aspect.distance.formatted if hasattr(aspect, 'distance') and aspect.distance else None,
                "difference": aspect.difference.formatted if hasattr(aspect, 'difference') and aspect.difference else None,
                "movement": aspect.movement.formatted if hasattr(aspect, 'movement') and aspect.movement else None,
                "condition": aspect.condition.formatted if hasattr(aspect, 'condition') and aspect.condition else None,
            })
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
        "shape": natal.shape if hasattr(natal, 'shape') else None,
        "diurnal": natal.diurnal if hasattr(natal, 'diurnal') else None,
        "moon_phase": natal.moon_phase.formatted if hasattr(natal, 'moon_phase') and natal.moon_phase else None,
        "objects": serialize_objects(natal.objects),
        "aspects": serialize_aspects(natal.aspects),
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
        "shape": composite.shape if hasattr(composite, 'shape') else None,
        "diurnal": composite.diurnal if hasattr(composite, 'diurnal') else None,
        "moon_phase": composite.moon_phase.formatted if hasattr(composite, 'moon_phase') and composite.moon_phase else None,
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
        "shape": transit_chart.shape if hasattr(transit_chart, 'shape') else None,
        "diurnal": transit_chart.diurnal if hasattr(transit_chart, 'diurnal') else None,
        "moon_phase": transit_chart.moon_phase.formatted if hasattr(transit_chart, 'moon_phase') and transit_chart.moon_phase else None,
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

@app.get("/api/persons/{person_id}/interpretation")
def api_interpretation(person_id: int, user: Tuple[int, str] = Depends(get_current_user)):
    """Generate AI interpretation of a natal chart."""
    user_id, email = user
    p = get_person(person_id, user_id)
    if not p:
        raise HTTPException(404, "Person not found")

    chart_data = compute_natal(p)

    # Build prompt for AI
    sun = moon = rising = "Unknown"
    planets_summary = []
    for oid, obj in chart_data["objects"].items():
        name = obj["name"]
        s = f"{name} в {obj['sign']} ({obj['sign_longitude']}), дом {obj['house_number']}"
        planets_summary.append(s)
        if name == "Sun": sun = s
        if name == "Moon": moon = s
        if name == "Asc": rising = s

    aspects_summary = []
    for a in chart_data["aspects"]:
        aspects_summary.append(f"{a['active']} {a['type']} {a['passive']}")

    prompt = f"""Ти си професионален астролог. Интерпретирай следната натална карта на български език.

Име: {chart_data['native']['name']}
Дата и час на раждане: {chart_data['native']['datetime']}

Слънце: {sun}
Луна: {moon}
Асцендент: {rising}

Всички планети и точки:
{chr(10).join(planets_summary)}

Основни аспекти:
{chr(10).join(aspects_summary) if aspects_summary else "Няма данни"}

Форма на хороскопа: {chart_data.get('shape', 'N/A')}
Лунна фаза: {chart_data.get('moon_phase', 'N/A')}
Дневно/Нощно раждане: {'Дневно' if chart_data.get('diurnal') else 'Нощно'}

Моля, направи пълна интерпретация включваща:
1. Обща характеристика на личността
2. Слънце, Луна и Асцендент - как си взаимодействат
3. Основни силни страни и предизвикателства
4. Любов и взаимоотношения
5. Кариера и призвание
6. Кармични уроци (Лунни възли)
7. Ключови аспекти и какво означават

Пиши на български, с професионален но разбираем език."""

    # Try to call AI (DeepSeek/OpenAI)
    ai_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if ai_key:
        try:
            import urllib.request
            ai_response = urllib.request.Request(
                "https://api.deepseek.com/v1/chat/completions",
                data=json.dumps({
                    "model": "deepseek-chat",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                    "max_tokens": 3000
                }).encode(),
                headers={
                    "Authorization": f"Bearer {ai_key}",
                    "Content-Type": "application/json"
                },
                method="POST"
            )
            with urllib.request.urlopen(ai_response, timeout=60) as resp:
                result = json.loads(resp.read())
                return {"interpretation": result["choices"][0]["message"]["content"]}
        except Exception as e:
            return {"interpretation": f"⚠️ AI интерпретацията не можа да се генерира: {str(e)}. Моля, проверете API ключа."}

    return {"interpretation": "⚠️ Няма конфигуриран AI API ключ. Задайте DEEPSEEK_API_KEY или OPENAI_API_KEY в environment променливите."}

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
    # Try to get user from Bearer token in request
    user_id = None
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
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

@app.get("/healthz")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
