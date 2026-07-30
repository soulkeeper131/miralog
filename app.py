import os, json, sqlite3, datetime
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

# Swiss Ephemeris path
import swisseph as swe
_ephe_path = os.environ.get("SE_EPHE_PATH", str(Path(__file__).resolve().parent / "ephe"))
if os.path.isdir(_ephe_path):
    swe.set_ephe_path(_ephe_path)
    os.environ["SE_EPHE_PATH"] = _ephe_path

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

# --- Auth ---
def verify_token(request: Request):
    """Verify API key for POST/PUT/DELETE requests. GET is allowed without token."""
    token = request.headers.get("X-API-Key") or request.cookies.get("api_token")
    if token != API_SECRET:
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

def update_person(person_id: int, data: BirthDataUpdate) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """UPDATE persons SET year=?, month=?, day=?, hour=?, minute=?,
               lat=?, lon=?, timezone=? WHERE id=?""",
            (data.year, data.month, data.day, data.hour, data.minute,
             data.lat, data.lon, data.timezone, person_id)
        )
        conn.commit()
        return cur.rowcount > 0

def make_subject(person: dict) -> charts.Subject:
    """Create an immanuel Subject from a person dict."""
    return charts.Subject(
        datetime.datetime(person["year"], person["month"], person["day"],
                          person["hour"], person["minute"], 0),
        person["lat"], person["lon"]
    )

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
    _auth: bool = Depends(verify_token),
):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO persons (name, year, month, day, hour, minute, lat, lon, timezone) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, year, month, day, hour, minute, lat, lon, timezone)
        )
        conn.commit()
        return {"id": cur.lastrowid, "name": name}

@app.delete("/api/persons/{person_id}")
def api_delete_person(person_id: int, _auth: bool = Depends(verify_token)):
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

@app.post("/api/persons/{person_id}/natal")
def api_natal_chart_update(person_id: int, data: BirthDataUpdate, _auth: bool = Depends(verify_token)):
    """Update birth data and return recalculated natal chart."""
    p = get_person(person_id)
    if not p:
        raise HTTPException(404, "Person not found")
    if not update_person(person_id, data):
        raise HTTPException(500, "Failed to update person")
    # Fetch updated person
    p = get_person(person_id)
    return compute_natal(p)

@app.get("/api/persons/{person_id}/natal.txt")
def api_natal_chart_text(person_id: int):
    """Return natal chart as plain text."""
    p = get_person(person_id)
    if not p:
        raise HTTPException(404, "Person not found")
    chart_data = compute_natal(p)
    text = natal_to_text(p, chart_data)
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(text, media_type="text/plain; charset=utf-8")

@app.post("/api/synastry")
def api_synastry(data: SynastryRequest, _auth: bool = Depends(verify_token)):
    """Compute synastry (composite) chart between two persons."""
    p1 = get_person(data.person1_id)
    if not p1:
        raise HTTPException(404, f"Person 1 (id={data.person1_id}) not found")
    p2 = get_person(data.person2_id)
    if not p2:
        raise HTTPException(404, f"Person 2 (id={data.person2_id}) not found")
    return compute_composite(p1, p2)

@app.post("/api/transits")
def api_transits(data: TransitsRequest, _auth: bool = Depends(verify_token)):
    """Compute transits for a person at a given target date."""
    p = get_person(data.person_id)
    if not p:
        raise HTTPException(404, f"Person (id={data.person_id}) not found")
    try:
        target_date = datetime.datetime.fromisoformat(data.target_date)
    except ValueError:
        raise HTTPException(400, "Invalid target_date format. Use ISO format: YYYY-MM-DDTHH:MM:SS")
    return compute_transits(p, target_date)

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
    return HTMLResponse(templates.get_template("index.html").render({"request": request, "persons": persons}))

@app.get("/chart/{person_id}", response_class=HTMLResponse)
async def view_chart(request: Request, person_id: int):
    p = get_person(person_id)
    if not p:
        raise HTTPException(404, "Person not found")
    chart_data = compute_natal(p)
    return HTMLResponse(templates.get_template("chart.html").render({
        "request": request,
        "person": p,
        "chart": chart_data,
    }))

@app.get("/add", response_class=HTMLResponse)
async def add_person_form(request: Request):
    return HTMLResponse(templates.get_template("add.html").render({"request": request}))

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
