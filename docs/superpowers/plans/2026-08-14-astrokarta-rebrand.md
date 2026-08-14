# АстроКарта Ребранд — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ребрандиране на приложението от МираСкоп към АстроКарта навсякъде — статични лога, кодови дефолти, шаблони, база данни и документация — без изхвърляне на вписаните потребители.

**Architecture:** Четирите вградени лога се прегенерират от новия PNG. Дефолтите в кода (app.py, billing.py, pdf_report.py) се сменят с новите стойности; стойностите в базата (които бият кода) се обновяват с еднократен SQL скрипт. Вътрешните ключове за сесии/тема остават непроменени.

**Tech Stack:** Python 3.11 (venv на проекта) + PIL 12.3.0 за логата; sqlite3 (stdlib) за базата; grep/python проверки; git (съобщения на български).

**Spec:** `docs/superpowers/specs/2026-08-14-astrokarta-rebrand-design.md` (одобрен)

**Файлова карта:**

| Файл | Действие | Отговорност |
|------|----------|-------------|
| `static/logo-header.png` (180×180) | презапис | хедър/login/dashboard |
| `static/logo-full.png` (600×600) | презапис | hero на landing |
| `static/favicon-32.png` (32×32) | презапис | таб на браузъра |
| `static/favicon-180.png` (180×180) | презапис | Apple touch |
| `app.py:76,531,532,2955` | edit | BRAND_DOMAIN, BRAND_DEFAULTS, User-Agent |
| `billing.py:84,124` | edit | дефолт `brand` за checkout |
| `pdf_report.py:201` | edit | дефолт `brand` за PDF |
| `templates/chart.html:1806` | edit | име на свален PDF |
| `templates/admin.html:1470` | edit | SEO placeholder |
| `data/persons.db` (settings) | SQL update | стойностите, които бият кода |
| `README.md`, `docs/API.md`, `.env.example` | edit | документация |

## Global Constraints

Тези правила важат за ВСИЧКИ задачи; не се повтарят във всяка.

- **Нови стойности (записват се без типографски кавички):** име `АстроКарта`, таглайн `Астрология с точността на астрономията`, домейн `astrokarta.bg`, `seo_site_url` = `https://astrokarta.bg`
- **Източник на логото:** `C:\Users\vladi\Downloads\AstroKarta_logo.png` (1254×1254 RGBA) — само за ЧЕТЕНЕ, никога не се модифицира
- **Python за всички стъпки:** `"C:\Users\vladi\Documents\Projects\Miralog\venv\Scripts\python.exe"` (има PIL 12.3.0; всички скриптове се пускат от корена на проекта)
- **НЕ се сменят (смяната чупи нещо):**
  - `miralog_token`, `miralog_theme`, `miralog_email` — във всички шаблони и `app.py:4692` (би изхвърлило вписаните потребители)
  - `'miraskop_modules_seen'` — `static/modules.js:11`
  - логер име `"miraskop"` — `app.py:94`
  - legacy SEO миграционен низ `"МираСкоп — твоята натална карта, разчетена на разбираем език"` — `app.py:331` (еднократна миграция за стари инсталации)
  - GitHub клониращ линк `https://github.com/soulkeeper131/miralog.git` — `README.md:93`
- `data/` и `persons.db` са gitignore-нати — промените в базата НЕ се комитват
- Конзолните проверки печатат само ASCII (`OK`, `FAIL`) — кирилицата в cp1251 конзола се изкривява
- Комити: git add само на изброените файлове; съобщенията са дадени дословно
- Извън обхвата на този план (остава в spec Част 5 за потребителя): DNS/Coolify насочване на astrokarta.bg

---

### Task 1: Генериране на четирите лога

**Files:**
- Modify (презапис): `static/logo-header.png`, `static/logo-full.png`, `static/favicon-32.png`, `static/favicon-180.png`

**Interfaces:**
- Consumes: `C:\Users\vladi\Downloads\AstroKarta_logo.png` (само четене)
- Produces: четирите PNG-а; Task 6 (smoke test) ги проверява по размер и през HTTP

- [ ] **Step 1: Генерирай файловете**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && venv/Scripts/python.exe - <<'PYEOF'
from PIL import Image

SRC = r"C:\Users\vladi\Downloads\AstroKarta_logo.png"
DST = r"C:\Users\vladi\Documents\Projects\Miralog\static"

img = Image.open(SRC).convert("RGBA")
assert img.size == (1254, 1254), img.size

for name, size in [
    ("logo-header.png", (180, 180)),
    ("logo-full.png", (600, 600)),
    ("favicon-32.png", (32, 32)),
    ("favicon-180.png", (180, 180)),
]:
    img.resize(size, Image.LANCZOS).save(f"{DST}\\{name}", optimize=True)
    print(name, size, "OK")
PYEOF
```

Expected: четири реда `OK`. Източникът в Downloads остава непокътнат.

- [ ] **Step 2: Провери размерите**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && venv/Scripts/python.exe - <<'PYEOF'
from PIL import Image
expected = {
    "static/logo-header.png": (180, 180),
    "static/logo-full.png": (600, 600),
    "static/favicon-32.png": (32, 32),
    "static/favicon-180.png": (180, 180),
}
for path, size in expected.items():
    got = Image.open(path).size
    assert got == size, (path, got)
print("DIMENSIONS OK")
PYEOF
```

Expected: `DIMENSIONS OK`

- [ ] **Step 3: Провери че източникът не е променен**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && venv/Scripts/python.exe - <<'PYEOF'
from PIL import Image
img = Image.open(r"C:\Users\vladi\Downloads\AstroKarta_logo.png")
assert img.size == (1254, 1254)
print("SOURCE OK")
PYEOF
```

Expected: `SOURCE OK`

- [ ] **Step 4: Commit**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && git add static/logo-header.png static/logo-full.png static/favicon-32.png static/favicon-180.png && git commit -m "feat: ново лого АстроКарта — header, full и фавикони"
```

---

### Task 2: Кодови дефолти (app.py, billing.py, pdf_report.py)

**Files:**
- Modify: `app.py:76`, `app.py:531`, `app.py:532`, `app.py:2955`, `billing.py:84`, `billing.py:124`, `pdf_report.py:201`

**Interfaces:**
- Consumes: нищо (първа кодова задача)
- Produces: `app.BRAND_DEFAULTS` и `app.BRAND_DOMAIN` с новите стойности; `billing.create_feature_checkout`/`create_features_checkout` и `pdf_report.build_reading_pdf` с дефолт `brand="АстроКарта"` — Task 6 ги асъртва

- [ ] **Step 1: app.py ред 76 — BRAND_DOMAIN**

old:
```python
BRAND_DOMAIN = os.environ.get("BRAND_DOMAIN", "miralog.bg").strip() or "miralog.bg"
```
new:
```python
BRAND_DOMAIN = os.environ.get("BRAND_DOMAIN", "astrokarta.bg").strip() or "astrokarta.bg"
```

- [ ] **Step 2: app.py ред 531 — brand_name (и двата низа на реда)**

old:
```python
    "brand_name": os.environ.get("BRAND_NAME", "МираСкоп").strip() or "МираСкоп",
```
new:
```python
    "brand_name": os.environ.get("BRAND_NAME", "АстроКарта").strip() or "АстроКарта",
```

- [ ] **Step 3: app.py ред 532 — brand_tagline**

old:
```python
    "brand_tagline": os.environ.get("BRAND_TAGLINE", "Астрология на разбираем език").strip(),
```
new:
```python
    "brand_tagline": os.environ.get("BRAND_TAGLINE", "Астрология с точността на астрономията").strip(),
```

- [ ] **Step 4: app.py ред 2955 — User-Agent**

old:
```python
        headers={"User-Agent": "MiraSkop/1.0 (astrology chart app)"},
```
new:
```python
        headers={"User-Agent": "AstroKarta/1.0 (astrology chart app)"},
```

- [ ] **Step 5: billing.py редове 84 и 124 — дефолт brand (две еднакви места)**

old (и на двата реда):
```python
    brand: str = "МираСкоп",
```
new:
```python
    brand: str = "АстроКарта",
```

- [ ] **Step 6: pdf_report.py ред 201 — дефолт brand**

old:
```python
                      logo_path: str = None, brand: str = "МираСкоп") -> bytes:
```
new:
```python
                      logo_path: str = None, brand: str = "АстроКарта") -> bytes:
```

**ВАЖНО:** `app.py:331` (legacy SEO низ) и `app.py:94` (логер `miraskop`) НЕ се пипат — виж Global Constraints.

- [ ] **Step 7: Провери с импорт-асърти**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && venv/Scripts/python.exe - <<'PYEOF'
import inspect
import app, billing, pdf_report

assert app.BRAND_DOMAIN == "astrokarta.bg"
assert app.BRAND_DEFAULTS["brand_name"] == "АстроКарта"
assert app.BRAND_DEFAULTS["brand_tagline"] == "Астрология с точността на астрономията"
assert app.BRAND_DEFAULTS["brand_domain"] == "astrokarta.bg"
assert inspect.signature(billing.create_feature_checkout).parameters["brand"].default == "АстроКарта"
assert inspect.signature(billing.create_features_checkout).parameters["brand"].default == "АстроКарта"
assert inspect.signature(pdf_report.build_reading_pdf).parameters["brand"].default == "АстроКарта"
print("CODE OK")
PYEOF
```

Expected: `CODE OK` (импортът на app.py изпълнява идемпотентни миграции върху базата — нормално).

- [ ] **Step 8: Провери че забранените места са непокътнати**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && grep -n "МираСкоп — твоята натална карта" app.py && grep -n 'getLogger("miraskop")' app.py
```

Expected: два реда — ред 331 и ред 94.

- [ ] **Step 9: Commit**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && git add app.py billing.py pdf_report.py && git commit -m "feat: дефолти на марката — АстроКарта / astrokarta.bg / нов таглайн"
```

---

### Task 3: Шаблони (chart.html, admin.html)

**Files:**
- Modify: `templates/chart.html:1806`, `templates/admin.html:1470`

**Interfaces:**
- Consumes: нищо (самостоятелни стрингове)
- Produces: PDF-ите се казват `AstroKarta-….pdf`; SEO полето в админа подсказва `https://astrokarta.bg` — Task 6 ги верифицира с grep

- [ ] **Step 1: chart.html ред 1806 — име на свален PDF**

old:
```javascript
                a.download = 'MiraSkop-' + key.replace(/[:.]/g, '-') + '.pdf';
```
new:
```javascript
                a.download = 'AstroKarta-' + key.replace(/[:.]/g, '-') + '.pdf';
```

- [ ] **Step 2: admin.html ред 1470 — SEO placeholder**

old:
```javascript
                            '<input type="url" id="seoSiteUrl" placeholder="https://miraskop.bg" value="' +
```
new:
```javascript
                            '<input type="url" id="seoSiteUrl" placeholder="https://astrokarta.bg" value="' +
```

- [ ] **Step 3: Провери че в templates не остава старата марка**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && grep -rn "MiraSkop\|miraskop" templates/
```

Expected: НИКАКЪВ изход (празно). Ключовете `miralog_token`/`miralog_theme` са с `miralog`, не `miraskop` — те остават и не излизат в този grep.

- [ ] **Step 4: Commit**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && git add templates/chart.html templates/admin.html && git commit -m "feat: PDF име и SEO placeholder — AstroKarta / astrokarta.bg"
```

---

### Task 4: База данни (data/persons.db)

**Files:**
- Modify (локално, НЕ се комитва — gitignore): `data/persons.db`, таблица `settings`

**Interfaces:**
- Consumes: нищо (самостоятелни SQL update-и)
- Produces: `brand_name`, `brand_tagline`, `brand_domain`, `seo_site_url` с новите стойности; `brand_logo`/`brand_logo_full` празни (падат към новите вградени файлове) — Task 6 асъртва `app.brand()`

Схема: `settings(key TEXT PRIMARY KEY, value TEXT)`. Текущи стойности: `brand_name='МираСкоп'`, `brand_tagline='Астрология на разбираем език'`, `brand_domain='miralog.bg'`, `brand_logo=''`, `seo_site_url=''`; ред `brand_logo_full` не съществува.

- [ ] **Step 1: Бекъп на базата**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && cp data/persons.db "data/persons.db.bak-2026-08-14"
```

Expected: файлът `data/persons.db.bak-2026-08-14` съществува. (Ако локалното приложение работи в момента — спри го първо, за да няма SQLite lock.)

- [ ] **Step 2: Приложи update-ите**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && venv/Scripts/python.exe - <<'PYEOF'
import sqlite3
db = sqlite3.connect("data/persons.db")
db.executescript("""
UPDATE settings SET value = 'АстроКарта' WHERE key = 'brand_name';
UPDATE settings SET value = 'Астрология с точността на астрономията' WHERE key = 'brand_tagline';
UPDATE settings SET value = 'astrokarta.bg' WHERE key = 'brand_domain';
UPDATE settings SET value = '' WHERE key = 'brand_logo';
INSERT INTO settings (key, value) VALUES ('brand_logo_full', '')
    ON CONFLICT(key) DO UPDATE SET value = '';
UPDATE settings SET value = 'https://astrokarta.bg' WHERE key = 'seo_site_url';
""")
db.commit()
print("DB UPDATED")
PYEOF
```

Expected: `DB UPDATED`

- [ ] **Step 3: Провери стойностите**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && venv/Scripts/python.exe - <<'PYEOF'
import sqlite3
db = sqlite3.connect("data/persons.db")
rows = dict(db.execute("SELECT key, value FROM settings"))
assert rows["brand_name"] == "АстроКарта"
assert rows["brand_tagline"] == "Астрология с точността на астрономията"
assert rows["brand_domain"] == "astrokarta.bg"
assert rows["brand_logo"] == ""
assert rows["brand_logo_full"] == ""
assert rows["seo_site_url"] == "https://astrokarta.bg"
print("DB OK")
PYEOF
```

Expected: `DB OK`. (`seo_title` съдържа `{brand}` и се попълва автоматично — не се пипа.)

- [ ] **Step 4: НЕ комитваш — но провери че git наистина игнорира базата**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && git status --short && git check-ignore data/persons.db
```

Expected: `git status` не показва `data/` файлове; `git check-ignore` изписва `data/persons.db`.

- [ ] **Step 5: Бележка за продукцията (без действие сега)**

Продукционната база живее на Coolify volume и НЕ се пипа оттук. Същите пет стойности се въвеждат на сървъра през **Админ панел → Настройки → Марка** (име, таглайн, домейн, „Върни оригинала“ за двете лога) и **Настройки → SEO** (`seo_site_url`). Локалният бекъп остава в `data/persons.db.bak-2026-08-14`.

---

### Task 5: Документация (README.md, docs/API.md, .env.example)

**Files:**
- Modify: `README.md:1,5,32,126,130,141,142,143`, `docs/API.md:3,16,25,42`, `.env.example:1,23,34,37`

**Interfaces:**
- Consumes: нищо
- Produces: документацията съответства на новите дефолти от Task 2 — Task 6 я включва в глобалния grep

- [ ] **Step 1: README.md ред 1 — заглавие**

old: `# 🔮 МираСкоп (Miraskop)`
new: `# 🔮 АстроКарта (AstroKarta)`

- [ ] **Step 2: README.md ред 5 — URL**

old: `🌐 **https://miralog.blv.bg**`
new: `🌐 **https://astrokarta.bg**`

- [ ] **Step 3: README.md ред 32 — име в текста**

old: `МираСкоп поддържа два AI провайдъра за генериране на персонализирани астрологични четения:`
new: `АстроКарта поддържа два AI провайдъра за генериране на персонализирани астрологични четения:`

- [ ] **Step 4: README.md — docker таг (редове 126 и 130)**

old: `docker build -t miraskop .`
new: `docker build -t astrokarta .`

old (последен ред на docker run блока):
```
  miraskop
```
new:
```
  astrokarta
```

- [ ] **Step 5: README.md — env таблица (редове 141-143)**

old:
```
| `BRAND_NAME` | Име на приложението | `МираСкоп` |
| `BRAND_TAGLINE` | Подзаглавие във футъра | `Астрология на разбираем език` |
| `BRAND_DOMAIN` | Домейн за служебните имейли | `miralog.bg` |
```
new:
```
| `BRAND_NAME` | Име на приложението | `АстроКарта` |
| `BRAND_TAGLINE` | Подзаглавие във футъра | `Астрология с точността на астрономията` |
| `BRAND_DOMAIN` | Домейн за служебните имейли | `astrokarta.bg` |
```

Секцията „Смяна на името и логото“ (редове 152-168) е проверена — съдържа само механизма и бележката за `miralog_token` (която остава), без стари стойности на марката; без промяна. Клониращият линк на ред 93 остава.

- [ ] **Step 6: docs/API.md ред 3 — име и базов URL**

old: `МираСкоп използва REST API с JSON отговори. Базов URL: `https://miralog.blv.bg``
new: `АстроКарта използва REST API с JSON отговори. Базов URL: `https://astrokarta.bg``

- [ ] **Step 7: docs/API.md — имейли (редове 16, 25, 42)**

old: `admin@miralog.bg` (три еднакви места)
new: `admin@astrokarta.bg`

- [ ] **Step 8: .env.example ред 1 — коментар**

old: `# МираСкоп — променливи на средата`
new: `# АстроКарта — променливи на средата`

- [ ] **Step 9: .env.example — имейли (редове 23, 34, 37)**

old: `ADMIN_EMAIL=admin@miraskop.bg`
new: `ADMIN_EMAIL=admin@astrokarta.bg`

old: `# по избор; по подразбиране е demo@miraskop.bg.`
new: `# по избор; по подразбиране е demo@astrokarta.bg.`

old: `# DEMO_EMAIL=demo@miraskop.bg`
new: `# DEMO_EMAIL=demo@astrokarta.bg`

- [ ] **Step 10: Провери остатъка от старата марка в трите файла**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && grep -n "МираСкоп\|Miraskop\|miraskop\|miralog" README.md docs/API.md .env.example
```

Expected: точно два реда, и двата в README.md — ред 93 (клониращ линк `github.com/soulkeeper131/miralog.git`) и ред ~167 (бележката за `miralog_token`). Нищо друго.

- [ ] **Step 11: Commit**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && git add README.md docs/API.md .env.example && git commit -m "docs: ребранд на README, API.md и .env.example — АстроКарта"
```

---

### Task 6: Крайна верификация (глобален sweep + runtime smoke test)

**Files:**
- Без промени — само проверки

**Interfaces:**
- Consumes: всичко от Tasks 1-5

- [ ] **Step 1: Глобален grep-sweep с allowlist**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && venv/Scripts/python.exe - <<'PYEOF'
import re, subprocess, pathlib
root = pathlib.Path(".")
tracked = subprocess.check_output(["git", "ls-files"], text=True).split()
# разрешени места за умишлено запазените низове:
allowed_miraskop = {"app.py", "static/modules.js"}          # логер + localStorage ключ
allowed_miralog = {"app.py", "README.md"} | {f for f in tracked if f.startswith("templates/")}
failures = []
for f in tracked:
    if not f.endswith((".py", ".html", ".js", ".md", ".example", ".bat", ".txt")):
        continue
    if f.startswith("docs/superpowers/"):
        continue
    text = pathlib.Path(f).read_text(encoding="utf-8")
    if "MiraSkop" in text:
        failures.append((f, "MiraSkop"))
    if re.search(r"miraskop", text, re.I) and f not in allowed_miraskop:
        failures.append((f, "miraskop"))
    if "miraskop.bg" in text:
        failures.append((f, "miraskop.bg"))
    if "МираСкоп" in text and not (f == "app.py" and "legacy_seo_title" in text):
        failures.append((f, "МираСкоп"))
    if "miralog" in text and f not in allowed_miralog:
        failures.append((f, "miralog"))
assert not failures, failures
print("SWEEP OK")
PYEOF
```

Expected: `SWEEP OK`. Ако FAIL — failures изброява файл и низ; оправи и пусни отново.

- [ ] **Step 2: Runtime smoke test — стартирай и провери страниците**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && venv/Scripts/python.exe -m uvicorn app:app --host 127.0.0.1 --port 8011 &
```

Изчакай ~5 секунди, после:

```bash
curl -s http://127.0.0.1:8011/ -o /tmp/landing.html
curl -s http://127.0.0.1:8011/login -o /tmp/login.html
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8011/static/logo-header.png
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8011/static/favicon-32.png
grep -c "АстроКарта" /tmp/landing.html
grep -c "logo-header.png" /tmp/landing.html
```

Expected: двата HTTP кода `200`; `АстроКарта` в landing ≥ 1; `logo-header.png` в landing ≥ 1. Спри сървъра:

```bash
kill %1
```

(Ако нещо друго държи порт 8011 — ползвай 8012; целта е само smoke проверка.)

- [ ] **Step 3: Асърти за brand() срещу базата (покрива spec тест 4 и 5)**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && venv/Scripts/python.exe - <<'PYEOF'
import app
b = app.brand()
assert b["name"] == "АстроКарта"
assert b["tagline"] == "Астрология с точността на астрономията"
assert b["domain"] == "astrokarta.bg"
assert b["logo"] == "/static/logo-header.png"
assert b["logo_full"] == "/static/logo-full.png"
assert app.brand_name() == "АстроКарта"
print("BRAND OK")
PYEOF
```

Expected: `BRAND OK` (потвърждава, че базата + дефолтите работят заедно и имейлите с `{brand}` ще се попълват с АстроКарта).

- [ ] **Step 4: Финален преглед на git статуса**

```bash
cd "C:\Users\vladi\Documents\Projects\Miralog" && git status --short && git log --oneline -6
```

Expected: чисто работно дърво (само `data/` бекъпа е невидим заради gitignore); последните четири комита са от Tasks 1, 2, 3, 5.

---

## Съответствие със spec тестовете

| Spec тест | Къде се проверява |
|-----------|-------------------|
| 1. Landing/login/dashboard показват новото лого и име | Task 6 Step 2 |
| 2. Фавиконът в таба е новият | Task 1 + Task 6 Step 2 (`favicon-32.png` → 200) |
| 3. PDF отчетът се казва `AstroKarta-….pdf` | Task 3 Step 3 |
| 4. Админ панел показва новите стойности | Task 4 Step 3 (базата е източникът) |
| 5. Имейл шаблоните с `{brand}` → АстроКарта | Task 6 Step 3 |
| 6. Вписаните потребители остават вписани | Global Constraints (ключовете не се пипат) + Task 6 Step 1 allowlist |
