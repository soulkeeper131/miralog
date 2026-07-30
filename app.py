import os, json, sqlite3, datetime
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

# Swiss Ephemeris path
import swisseph as swe
ephe_path = os.environ.get("SE_EPHE_PATH", str(Path(__file__).parent / "ephe"))
if os.path.isdir(ephe_path):
    swe.set_ephe_path(ephe_path)

from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from immanuel import charts
from immanuel.const import chart, names
from pydantic import BaseModel

# --- App Setup ---
DB_PATH = Path(__file__).parent / "persons.db"
API_SECRET = os.environ.get("API_SECRET", "astrology-secret-key-change-me")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS persons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        conn.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

templates = Jinja2Templates(directory="templates")

app = FastAPI(title="Миралог", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- Auth ---
def verify_token(request: Request):
    token = request.headers.get("X-API-Key") or request.cookies.get("api_token")
    if token != API_SECRET:
        # Allow read-only access without token for GET requests
        if request.method == "GET":
            return True
        raise HTTPException(403, "Invalid API key")
    return True

# --- Helpers ---
def get_person(person_id: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM persons WHERE id = ?", (person_id,)).fetchone()
        return dict(row) if row else None

def get_all_persons() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM persons ORDER BY name").fetchall()]

def compute_natal(person: dict) -> dict:
    """Compute natal chart for a person using immanuel."""
    native = charts.Subject(
        datetime.datetime(person["year"], person["month"], person["day"],
                          person["hour"], person["minute"], 0),
        person["lat"], person["lon"]
    )
    natal = charts.Natal(native)

    # Build structured response
    objects = {}
    for obj in natal.objects.values():
        objects[str(obj.index)] = {
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

    # Aspects
    aspects = []
    for aspect in natal.aspects.values():
        aspects.append({
            "type": aspect.type.name,
            "active": aspect.active.formatted,
            "passive": aspect.passive.formatted,
            "aspect": aspect.aspect.name if hasattr(aspect, 'aspect') and aspect.aspect else None,
            "orb": aspect.orb if hasattr(aspect, 'orb') else None,
            "difference": aspect.difference.formatted if hasattr(aspect, 'difference') and aspect.difference else None,
        })

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
        "objects": objects,
        "aspects": aspects,
    }

# --- API Routes ---
@app.get("/api/persons")
def api_list_persons():
    return {"persons": get_all_persons()}

@app.get("/api/persons/{person_id}")
def api_get_person(person_id: int):
    p = get_person(person_id)
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
):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO persons (name, year, month, day, hour, minute, lat, lon, timezone) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, year, month, day, hour, minute, lat, lon, timezone)
        )
        conn.commit()
        return {"id": cur.lastrowid, "name": name}

@app.delete("/api/persons/{person_id}")
def api_delete_person(person_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM persons WHERE id = ?", (person_id,))
        conn.commit()
    return {"deleted": person_id}

@app.get("/api/persons/{person_id}/natal")
def api_natal_chart(person_id: int):
    p = get_person(person_id)
    if not p:
        raise HTTPException(404, "Person not found")
    return compute_natal(p)

@app.get("/api/persons/{person_id}/interpretation")
def api_interpretation(person_id: int):
    """Generate AI interpretation of a natal chart."""
    p = get_person(person_id)
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
    persons = get_all_persons()
    return templates.TemplateResponse("index.html", {"request": request, "persons": persons})

@app.get("/chart/{person_id}", response_class=HTMLResponse)
async def view_chart(request: Request, person_id: int):
    p = get_person(person_id)
    if not p:
        raise HTTPException(404, "Person not found")
    chart_data = compute_natal(p)
    return templates.TemplateResponse("chart.html", {
        "request": request,
        "person": p,
        "chart": chart_data,
    })

@app.get("/add", response_class=HTMLResponse)
async def add_person_form(request: Request):
    return templates.TemplateResponse("add.html", {"request": request})

@app.post("/add")
async def add_person_submit(
    request: Request,
    name: str = Form(...),
    year: int = Form(...),
    month: int = Form(...),
    day: int = Form(...),
    hour: int = Form(0),
    minute: int = Form(0),
    lat: float = Form(...),
    lon: float = Form(...),
    timezone: str = Form("Europe/Sofia"),
):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO persons (name, year, month, day, hour, minute, lat, lon, timezone) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, year, month, day, hour, minute, lat, lon, timezone)
        )
        conn.commit()
        return RedirectResponse(f"/chart/{cur.lastrowid}", status_code=303)

@app.get("/healthz")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
