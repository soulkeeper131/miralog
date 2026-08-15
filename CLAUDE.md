# Miralog — АстроКарта (бивш МираСкоп)

Българско уеб приложение за астрология: натални карти, нумерология, PDF доклади и AI интерпретации. Production деплой през Coolify (self-hosted), GitHub: `soulkeeper131/miralog`.

## Технологии

- **FastAPI + Jinja2** — цялото приложение е основно в един голям файл `app.py` (~4500 реда)
- **SQLite** база в `data/` (създава се автоматично при първо стартиране; админ акаунт се създава автоматично ако няма потребители)
- **Swiss Ephemeris** файлове в `ephe/` (~2MB, теглят се при Docker build)
- **Python venv**: `venv/` — на Windows: `venv/Scripts/python.exe`
- **Docker + Coolify** за production

## Структура на проекта

| Файл | Роля |
|------|------|
| `app.py` | Главно приложение: рутове, брандинг система, PDF генериране (`build_person_pdf` ~ред 4549) |
| `bg_text.py` | Български текстове |
| `billing.py` | Плащания/биллинг |
| `chart_svg.py` | Чертаене на астрологични карти |
| `numerology.py` | Нумерология |
| `pdf_report.py` | PDF доклади |
| `translations.py` | Преводи |
| `templates/` | Jinja2 шаблони (`chart.html` и др.) |
| `static/` | Лога, favicon, изображения |
| `data/` | SQLite база (gitignored) |
| `docs/` | `API.md`, `DEPLOY.md`, `PLAN-pricing.md`, `superpowers/{specs,plans}` |

## Брандинг (ребранд МираСкоп → АстроКарта, 2026-08-14)

- `BRAND_SLUG = "AstroKarta"` в `app.py`; помощна функция `brand_slug()` (ASCII fallback за кирилица)
- `brand()` dict (с ключ `slug`) е изложен глобално в Jinja като `brand` → шаблоните ползват `{{ brand.slug }}`, `{{ brand.brand_name }}` и т.н.
- PDF имена: `prefix = brand_slug()` в `app.py` (~ред 4549); `templates/chart.html` JS: `a.download = '{{ brand.slug }}-' + ...`
- Името на марката се държи и в **production базата** → задава се през **Админ панел → Настройки** на сървъра
- Домейн: **astrokarta.bg**

## Локално пускане

- `run.bat` или `venv/Scripts/python.exe app.py`
- Health check: `GET /healthz` на порт **8000**, очаква 200
- AI интерпретациите искат поне един API ключ (`ANTHROPIC_API_KEY` или `DEEPSEEK_API_KEY`)

## Деплой (Coolify)

- Coolify изтегля `soulkeeper131/miralog.git:master` → **git push към GitHub = auto-deploy**
- Docker: порт 8000, volume `/app/data`, healthcheck `/healthz`
- Env променливи: `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `SECRET_KEY`
- Препоръчителни лимити: 512MB RAM, 1 CPU, 256MB swap
- **Coolify UI адресът и API токенът не са записани никъде** — питай потребителя при нужда (Settings → API tokens)

## Git / GitHub

- Remote: `https://github.com/soulkeeper131/miralog.git` (origin/master)
- Стил на комитите: **български, конвенционален префикс** (`fix:`, `feat:`, `docs:`)
- Ребрандът е 8 комита: `28b450c → dcf9a80 → 1ec902e → 6c30cf4 → 4f5c770 → 83e5819 → 447555c → 6d57c53` (пушнати в origin/master)

## Капани на средата (Windows)

- Конзолата е **cp1251** — Python скриптове с кирилица трябва да викат `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`
- В bash двойните кавички „изяждат“ PowerShell променливи (`$_`, `$m`) — ползвай **единични кавички** около PowerShell команди
- Docker Desktop често не е пуснат на тази машина (Coolify не е локален)
- JSONL транскриптите на Claude Code се намират в `C:\Users\vladi\.claude\projects\` — редовете са огромни, за търсене ползвай Python, не grep

## Работни конвенции

- Комуникацията е **на български**
- Планови артефакти: `docs/superpowers/specs|plans` (проектът НЕ е инициализиран като GSD проект — няма `.planning/`)
- Потребителят държи комитите локално освен ако изрично не каже да се пушнат
