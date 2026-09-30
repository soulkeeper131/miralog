# Miralog — АстроКарта (бивш МираСкоп)

Българско уеб приложение за астрология: натални карти, нумерология, хороскопи, PDF/аудио разчитания и AI интерпретации. Продава еднократни модули (Stripe). Production деплой през Coolify (self-hosted), GitHub: `soulkeeper131/miralog`.

## Технологии

- **FastAPI + Jinja2**, почти цялата логика е в един голям файл `app.py` (~8250 реда, ~120 рута)
- **immanuel + pyswisseph** за астрологичните изчисления; Swiss Ephemeris файлове в `ephe/` (теглят се при Docker build)
- **SQLite** в `data/persons.db` (`DB_PATH`). Схемата и всички миграции са в `init_db()` и се пускат при всяко стартиране; админ акаунт се създава автоматично, ако няма потребители
- **AI:** DeepSeek (по подразбиране `deepseek-v4-flash`, платените разчитания през `deepseek-v4-pro`), Anthropic (`claude-sonnet-4-5`) или OpenAI, виж `AI_MODELS` / `call_ai()`
- **Stripe** (еднократни плащания), **reportlab** (PDF), **edge-tts** (гласово четене), **pyotp** (2FA за админ), **sentry-sdk** (по желание)
- **Python venv**: `venv/`, на Windows: `venv/Scripts/python.exe`
- **Docker + Coolify** за production

## Структура на проекта

| Файл | Роля |
|------|------|
| `app.py` | Всичко останало: схема/миграции, auth, рутове, админ API, плащания, имейли, SEO страници, фонови задачи |
| `billing.py` | Stripe Checkout помощници (`stripe_enabled()` иска И двата ключа) |
| `pdf_report.py` | PDF на разчитания, касови бележки и фактури |
| `saft.py` | SAF-T XML за НАП (Наредба Н-18), XSD в `docs/n18/` |
| `chart_svg.py` | SVG колело на наталната карта |
| `numerology.py` | Питагорова нумерология |
| `translations.py` | Преводи и обяснения на знаци, планети, домове, аспекти |
| `bg_text.py` | Механични корекции на българския текст от AI (във/със, пунктуация) |
| `feature_pages.py` | Лендинг страниците на модулите (`/natalna-karta`, `/akashovi-zapisi`…) |
| `horoscope_signs.py`, `planet_pages.py`, `house_pages.py` | Данни за SEO страниците (зодии, планети, домове) |
| `scripts_check_paid_access.py` | Само чете: сверява плащания ↔ `feature_purchases` ↔ `unlocked_features()` |
| `scripts_reconcile_stripe.py` | Сверява платени Stripe сесии с базата; `--apply` отключва липсващото |
| `scripts/gen_og_image.py` | Генерира `static/og-image.jpg` (иска Pillow, не е в requirements) |
| `tests/` | pytest: `test_access`, `test_payments`, `test_security`, `test_edge_cases` |
| `templates/` | Jinja2 шаблони, всеки е самостоятелен (няма общ `base.html`); общи парчета: `_analytics.html`, `_critical.html` |
| `static/` | CSS/JS (`ui`, `landing`, `modules`, `locked`, `consent`), лога, favicon, og-image |
| `docs/` | `API.md`, `DEPLOY.md`, `PLAN-*.md`, `TODO.md`, `n18/`, `superpowers/{specs,plans}` |

Ориентири в `app.py` (търси по име, редовете се менят): `init_db`, `lifespan`, `admin_host_guard`, `FEATURE_CATALOGUE`, `require_feature`, `unlocked_features`, `bundle_offer`, `call_ai`, `fulfill_checkout_session`, `run_scheduled_jobs`, `build_person_pdf`.

## Ключови механизми

- **Модули и достъп:** всичко е еднократна покупка (няма абонамент). `chart` и `horoscope` се дават безплатно при регистрация (`FREE_ON_SIGNUP`); `planets`/`aspects` вървят с плана `demo` („Основен“, до 2 карти). Платените модули са `profile`, `period`, `love` (+1 карта), `akashic`, `numerology`, `moon`, а пакетът `bundle` струва 25 €. Цените са в таблица `feature_prices`, редактират се от админ панела. Заключен модул връща **402** с оферта. Админът вижда всичко.
- **Админ поддомейн:** панелът е само на `admin.<BRAND_DOMAIN>` (`ADMIN_HOST`). На основния домейн `/admin` и `/api/admin/*` връщат 404, а на админ хоста е позволено само admin/auth/static.
- **Auth:** JWT (30 дни) в `Authorization: Bearer`, в `?token=` или в бисквитката `miralog_token`; bcrypt пароли, rate-limit на входа (5 грешни опита → 15 мин блок), TOTP 2FA, Google/Facebook OAuth.
- **AI кеш:** разчитанията се пазят в `ai_cache` (per person + `cache_key`, напр. `horoscope:2026-09-29`). SEO текстовете са в отделни `*_cache` таблици. Генерирането върви във фонови нишки (`ai_job`), а страницата пита докато не е готово (`pending`).
- **Фонови задачи** (`_background_jobs_loop`, веднъж на час): дневен backup в `data/backups/` (пази `BACKUP_KEEP_DAYS`) и digest имейли. Сутрешното „затопляне“ на SEO страниците (`/api/*/warm`) се вика от **външен cron**, не от приложението.
- **Логове:** логерът `miraskop` пише в stdout и в `data/logs/app.log` (14 дни). Всяка заявка има код (`REQUEST_ID`, заглавка `X-Request-ID`), който клиентът вижда при 5xx; middleware-ът `request_context` пише реда за заявката (uvicorn access log е изключен). `ai_job` пренася кода във фоновата нишка, `call_ai` записва време и грешки без подканата, `audit()` се дублира в лога. Търсене: Админ → Логове (`/api/admin/logs`)
- **AI разходи:** всяко `call_ai` пише ред в `ai_usage` (токени от всеки отговор на доставчика, вкл. повторния опит при `finish_reason=length`; цена по `AI_PRICES` в USD, DeepSeek наполовина извън 01–04/06–10 UTC пн–пт). Източникът (`client`/`seo`/`background`/`admin`) и модулът идват от адреса на заявката през `AI_ORIGIN`. Админ → AI разходи (`/api/admin/ai-usage`). При смяна на цените на доставчик — обнови `AI_PRICES`
- **Защити:** публичните SEO API приемат `?refresh=true` само от админ (`is_admin_request`); лимити по IP в `RATE_LIMITS` (истинският IP е най-десният в X-Forwarded-For, `client_ip`); `/docs` е изключен в production; новите пароли са ≥8 знака (`check_new_password`), входът не проверява дължина
- **Настройки в базата** (таблица `settings`, през Админ → Настройки): AI ключ/провайдър/модел, SMTP (env `SMTP_*` има предимство), имейл шаблони, SEO/GA4/FB pixel, марка и лога, OAuth, юридически данни (`legal_*` за /privacy, /terms, фактури, SAF-T).

## Брандинг (ребранд МираСкоп → АстроКарта, 2026-08-14)

- `BRAND_SLUG = "AstroKarta"` в `app.py`; `brand_slug()` дава ASCII fallback за кирилица
- `brand` е **функция**, изложена глобално в Jinja. Шаблоните я викат: `{{ brand().name }}`, `{{ brand().logo }}`, `{{ brand().slug }}`. Без скобите (`brand.slug`) Jinja връща празно.
- PDF имена: `prefix = brand_slug()` в `build_person_pdf`; `templates/chart.html` JS: `a.download = '{{ brand().slug }}-' + ...`
- Името на марката се държи и в **production базата** и се задава през **Админ панел → Настройки** на сървъра. Качените лога са в `data/uploads/` (сервират се от `/uploads/`)
- Вътрешните ключове (`miralog_token`, logger `miraskop`) нарочно не са преименувани, защото смяната им ще изхвърли вписаните потребители
- Домейн: **astrokarta.bg**, админ: **admin.astrokarta.bg**

## Локално пускане и тестове

- `run.bat` или `venv/Scripts/python.exe -m uvicorn app:app --port 8000`
- Без `ENVIRONMENT=production` важат dev стойностите по подразбиране (`admin123`, демо акаунт `demo@astrokarta.bg` / `demo123`)
- `MOCK_PAYMENTS=1` (само извън production) симулира плащанията без Stripe
- Админ панелът локално: на `localhost` `/admin` дава 404, затова е нужен хост `admin.astrokarta.bg` (напр. ред в hosts файла) или `BRAND_DOMAIN=localhost` → `admin.localhost`
- Тестове: `pip install -r requirements-dev.txt` и после `python -m pytest` (всеки тест е с временна база, не пипа `data/`)
- Health check: `GET /healthz` на порт **8000**, очаква 200
- AI разчитанията искат ключ: в админ панела или env `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`

## Деплой (Coolify)

- Coolify изтегля `soulkeeper131/miralog.git:master` → **git push към master = auto-deploy**
- Docker: порт 8000, volume `/app/data` (база, backups, uploads, audio кеш), healthcheck `/healthz`
- Пълен списък на env променливите: `.env.example` и `docs/DEPLOY.md`. Задължителни в production: `ENVIRONMENT=production`, `SECRET_KEY` (≥32 знака), `ADMIN_PASSWORD`. Без тях приложението отказва да стартира
- Препоръчителни лимити: 512MB RAM, 1 CPU, 256MB swap (`AI_THREAD_LIMIT=8`)
- **Coolify:** https://coolify.blv.bg, приложение AstroKarta (uuid `vxpms670hhym05mnm419p9xd`). На същия сървър има и други проекти, пипай само AstroKarta
- Токен само за четене е в потребителската env променлива `COOLIFY_TOKEN` на Windows (в PowerShell: `[Environment]::GetEnvironmentVariable("COOLIFY_TOKEN","User")`). Никога не го показвай. API-то дава статус, деплои и логове, но **не може да чете базата**: за данни се ползва админ панелът
- Production има платили клиенти: промени само с изрично съгласие, проверка преди и след деплой

## Git / GitHub

- Remote: `https://github.com/soulkeeper131/miralog.git` (origin/master)
- Стил на комитите: **български, конвенционален префикс** (`fix:`, `feat:`, `docs:`, `chore:`)
- Ребрандът е 8 комита: `28b450c → dcf9a80 → 1ec902e → 6c30cf4 → 4f5c770 → 83e5819 → 447555c → 6d57c53` (пушнати в origin/master)

## Капани на средата (Windows)

- Конзолата е **cp1251**, затова Python скриптове с кирилица трябва да викат `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`
- В bash двойните кавички „изяждат“ PowerShell променливи (`$_`, `$m`), ползвай **единични кавички** около PowerShell команди
- Docker Desktop често не е пуснат на тази машина (Coolify не е локален)
- JSONL транскриптите на Claude Code се намират в `C:\Users\vladi\.claude\projects\`. Редовете са огромни, за търсене ползвай Python, не grep

## Работни конвенции

- Комуникацията е **на български**
- Планови артефакти: `docs/superpowers/specs|plans` и `docs/PLAN-*.md` (проектът НЕ е инициализиран като GSD проект, няма `.planning/`)
- Потребителят държи комитите локално, освен ако изрично не каже да се пушнат (push към master = деплой)
