# 🔮 АстроКарта (AstroKarta)

**Персонален астролог с изкуствен интелект** — изчислява натални карти, синастрия, транзити и нумерология с професионален астрологичен енджин и AI интерпретации на български език.

🌐 **https://astrokarta.bg**

---

## ✨ Възможности

| Категория | Функция |
|-----------|---------|
| 🔐 **Акаунти** | Регистрация с имейл/парола или Google/Facebook, JWT, забравена парола, 2FA за админ |
| 🎁 **Без регистрация** | Безплатна натална карта за гости (`/start`), нищо не се пази |
| 🔮 **Натална карта** | Планети, аспекти, домове (Плацидус), SVG колело, астро портрет |
| 📅 **Дневен хороскоп** | AI хороскоп по реалните транзити за деня (безплатен към картата) |
| 🪐 **Хороскоп за период** | Транзитни промени до 60 дни напред + AI разчитане |
| 💕 **Любовен хороскоп** | Съвместимост по зодия или по пълни рождени данни, синастрия |
| ☊ **Акашови записи** | Кармичен анализ (възли, Хирон, Сатурн, Плутон, Лилит + нумерология) |
| 🔢 **Нумерология** | Питагоров анализ + AI разчитане |
| ◐ **Лунен календар** | Месечен лунен календар със съвети |
| 📄 **PDF / 🔊 аудио / ✉️ имейл** | Всяко разчитане се сваля като PDF, чете се на глас или се праща по имейл |
| 🔗 **Споделяне** | Публичен линк към конкретно разчитане |
| 💳 **Плащания** | Еднократни покупки на модули през Stripe, пакет „Всички модули“, касови бележки и фактури |
| 🛠 **Админ панел** | На `admin.<домейн>`: потребители, плащания, цени, настройки, имейл шаблони, SEO, марка, одит, SAF-T |
| 🌐 **SEO страници** | Дневен хороскоп по зодии, планета в знак/дом, профили на зодиите, съвместимост, лендинги на модулите, sitemap, `llms.txt` |
| ⚡ **Кеширане** | AI отговорите се кешират, не се харчат токени повторно |
| 🇧🇬 **Български** | Целият интерфейс и текстове са на български |

### Модули и цени

Всичко е **еднократна покупка**, без абонамент. Цените се сменят от
админ панела (таблица `feature_prices`), затова реалните в production може
да се различават — текущите връща `GET /api/public/catalogue`. Стойностите,
с които се създава нова база, са:

| Модул | Ключ | Цена |
|-------|------|------|
| Натална карта, планети, аспекти, дневен хороскоп | `chart`, `planets`, `aspects`, `horoscope` | безплатно при регистрация |
| Пълен астрологически профил | `profile` | 5 € |
| Хороскоп за период | `period` | 5 € |
| Любовен хороскоп (+1 място за карта) | `love` | 5 € |
| Нумерология | `numerology` | 4 € |
| Акашови записи | `akashic` | 9 € |
| Лунен календар | `moon` | 2.99 € |
| Пакет „Всички модули“ | `bundle` | 25 € |

---

## 🧠 AI Интерпретации

Провайдърът, моделът и ключът се задават от **Админ → Настройки**
(записват се в базата). Ако там няма ключ, се ползва първата налична
env променлива: `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`.

| Провайдър | Модели |
|-----------|--------|
| DeepSeek (по подразбиране) | `deepseek-v4-flash` (дефолт), `deepseek-v4-pro` (ползва се за платените разчитания) |
| Anthropic | `claude-sonnet-4-5` |
| OpenAI | `gpt-4o-mini`, `gpt-4o` |

---

## 🛠 Технологии

| Слой | Технология |
|------|-----------|
| **Бекенд** | Python 3.11 + FastAPI |
| **Астрология** | [immanuel](https://github.com/astronomancy/immanuel) + Swiss Ephemeris (pyswisseph) |
| **База данни** | SQLite (`data/persons.db`) |
| **AI** | DeepSeek / Anthropic Claude / OpenAI |
| **Автентикация** | JWT (python-jose) + bcrypt, TOTP (pyotp), Google/Facebook OAuth |
| **Плащания** | Stripe Checkout |
| **PDF / глас** | reportlab / edge-tts |
| **Шаблони** | Jinja2 |
| **Грешки** | Sentry (по желание, `SENTRY_DSN`) |
| **Контейнеризация** | Docker (python:3.11-slim) |
| **Хостинг** | Self-hosted (Coolify) |

---

## 📂 Структура на проекта

```
miralog/
├── app.py                 # FastAPI приложение — рутове, схема на базата, бизнес логика
├── billing.py             # Stripe Checkout
├── pdf_report.py          # PDF разчитания, касови бележки, фактури
├── saft.py                # SAF-T файл за НАП (Наредба Н-18)
├── chart_svg.py           # SVG колело на наталната карта
├── numerology.py          # Питагорова нумерология
├── translations.py        # Преводи и значения на български
├── bg_text.py             # Корекции на българския текст от AI
├── feature_pages.py       # Лендинг страници на модулите
├── horoscope_signs.py     # Данни за зодиите (SEO)
├── planet_pages.py        # Данни за планетите (SEO)
├── house_pages.py         # Данни за домовете (SEO)
├── scripts_check_paid_access.py   # Проверка: платилите виждат ли каквото са купили
├── scripts_reconcile_stripe.py    # Сверка на Stripe сесии с базата
├── scripts/gen_og_image.py        # Генератор на og:image
├── tests/                 # pytest
├── templates/             # Jinja2 шаблони (landing, start, chart, dashboard, admin, SEO страници…)
├── static/                # CSS, JS, лога, favicon, og-image
├── docs/                  # API, деплой, планове
├── ephe/                  # Swiss Ephemeris (тегли се, не е в git)
└── data/                  # SQLite, backups, uploads, audio (volume, не е в git)
```

---

## 🚀 Инсталация и стартиране

### Локално (development)

```bash
# 1. Клонирай репото
git clone https://github.com/soulkeeper131/miralog.git
cd miralog

# 2. Създай виртуална среда
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 3. Инсталирай зависимостите (dev включва pytest)
pip install -r requirements-dev.txt

# 4. Изтегли Swiss Ephemeris файлове
mkdir -p ephe
for f in seas_18.se1 sepl_18.se1 semo_18.se1 sefstars.txt seorbel.txt; do
  curl -sL -o ephe/$f https://raw.githubusercontent.com/aloistr/swisseph/master/ephe/$f
done

# 5. Настройки (по желание) — виж .env.example
cp .env.example .env               # и махни ENVIRONMENT=production за локално

# 6. Стартирай
uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

Отвори **http://localhost:8000**. Без `ENVIRONMENT=production` се ползват
dev стойностите: админ `admin@astrokarta.bg` / `admin123`, демо
`demo@astrokarta.bg` / `demo123`. С `MOCK_PAYMENTS=1` покупките минават без Stripe.

> Админ панелът се отваря само на хост `admin.<BRAND_DOMAIN>`. Локално пусни
> с `BRAND_DOMAIN=localhost` и отвори **http://admin.localhost:8000/admin**.

### Тестове

```bash
python -m pytest
```

Всеки тест работи с временна база и не пипа `data/`.

### Docker

```bash
docker build -t astrokarta .
docker run -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -e ENVIRONMENT=production \
  -e SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))") \
  -e ADMIN_PASSWORD=... \
  astrokarta
```

---

## 🔑 Environment променливи

Пълният коментиран списък е в [`.env.example`](.env.example). AI ключът,
SMTP, SEO, марката, OAuth и юридическите данни могат да се зададат и от
админ панела (пазят се в базата).

| Променлива | Описание | Default |
|-----------|----------|---------|
| `ENVIRONMENT` | `production` включва строгите проверки при старт | `development` |
| `SECRET_KEY` | Подписва JWT; в production ≥32 знака | `change-me-in-production...` |
| `ADMIN_EMAIL` | Първият админ (създава се при празна база) | `admin@${BRAND_DOMAIN}` |
| `ADMIN_PASSWORD` | Парола на първия админ; в production задължителна | `admin123` |
| `DEMO_EMAIL` / `DEMO_PASSWORD` | Демо акаунт; в production само ако има `DEMO_PASSWORD` (≥8 знака) | `demo@…` / `demo123` (dev) |
| `BRAND_NAME` | Име на приложението | `АстроКарта` |
| `BRAND_TAGLINE` | Подзаглавие във футъра | `Астрология с точността на астрономията` |
| `BRAND_DOMAIN` | Домейн; админът е на `admin.<домейн>` | `astrokarta.bg` |
| `DB_PATH` | Път до базата | `data/persons.db` |
| `UPLOAD_DIR` | Качени лога | `data/uploads` |
| `SE_EPHE_PATH` | Swiss Ephemeris файлове | `./ephe` (`/app/ephe` в Docker) |
| `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` | AI ключ, ако не е зададен в админ панела | — |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | Stripe; без **двата** плащанията са изключени | — |
| `STRIPE_SUCCESS_URL` / `STRIPE_CANCEL_URL` | Къде се връща клиентът след плащане (`session_id` се добавя автоматично) | `/settings?paid=1` / `/settings?paid=0`; при покупка още при регистрацията — картата (`/chart/{id}?paid=1`) |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` / `SMTP_USE_TLS` | Имейли; имат предимство пред админ панела | — |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `FACEBOOK_APP_ID` / `FACEBOOK_APP_SECRET` | Социален вход; имат предимство пред админ панела | — |
| `SENTRY_DSN` | Следене на грешки (личните данни се чистят) | — |
| `MOCK_PAYMENTS` | `1` = тестови плащания без Stripe (игнорира се в production) | — |
| `AI_THREAD_LIMIT` | Едновременни тежки заявки (AI, PDF, TTS) | `8` |
| `BACKUP_KEEP_DAYS` | Колко дневни копия на базата се пазят | `7` |
| `TTS_CHUNKS` | На колко части се дели текстът за гласовото четене (синтезират се паралелно) | `5` |
| `SIGN_FREE_SECTIONS` | Колко раздела от хороскопа по зодия се четат свободно (останалите са размазани) | `3` |

---

## 🏷️ Смяна на името и логото

Името, подзаглавието, домейнът и двете лога се сменят от **Админ панел →
Настройки → Марка**, без ново пускане. Записаното там бие `BRAND_*`
променливите, а те от своя страна бият стойностите по подразбиране в кода.

Шаблоните ползват `{{ brand().name }}`, `{{ brand().logo }}`,
`{{ brand().logo_full }}` и `{{ brand().slug }}`; имейлите и PDF-ите четат същия източник, така че
преименуването не изисква редакция на markup. В текстовете на имейлите и в
SEO заглавието може да се пише `{brand}` — заменя се при четене.

Качените лога отиват в `data/uploads/` (сервират се от `/uploads/`) и не пипат
оригиналите в `static/`, затова „Върни оригинала“ винаги работи. Папката е на
същия том като базата, така че преживява деплойте.

> Вътрешните ключове (`miralog_token` за бисквитката и localStorage) нарочно
> не се сменят с марката — смяната им би извадила всички вписани потребители.

---

## 🔌 API

Пълното описание на endpoint-ите е в [`docs/API.md`](docs/API.md), а за деплоя виж
[`docs/DEPLOY.md`](docs/DEPLOY.md).

---

## 🔒 Сигурност

- **JWT токени** с 30-дневна валидност, **bcrypt** пароли
- Ограничение на входа: 5 грешни опита → 15 минути блокиране (по имейл + IP)
- **2FA (TOTP)** за администраторите
- Всички данни за карти и разчитания изискват вход; платените модули връщат **402**, докато не се купят
- Админ панелът е изолиран на `admin.<домейн>`; на основния домейн `/admin` и `/api/admin/*` връщат 404
- В production приложението **отказва да стартира** с примерните `SECRET_KEY` / `ADMIN_PASSWORD`
- Защитни HTTP заглавки (X-Frame-Options, nosniff, Referrer-Policy, Permissions-Policy, HSTS в production)
- Stripe webhook-ът се проверява с подпис; модул се отключва само при `payment_status = paid`
- Статистиката на прегледите не пази IP и User-Agent; Sentry чисти личните данни
- AI ботове и скрейпъри са блокирани в `robots.txt`

---

## 🌍 Часова зона

Всички астрологични изчисления използват **Europe/Sofia** (UTC+2/UTC+3). Часовата зона може да се зададе индивидуално за всеки човек.

---

## 📝 Лиценз

MIT License — свободно използване, модификация и разпространение.

---

## 🤝 Благодарности

- [immanuel](https://github.com/astronomancy/immanuel) — Python библиотека за астрологични изчисления
- [Swiss Ephemeris](https://www.astro.com/swisseph/) — астрономически ефемериди
- [Nous Research](https://nousresearch.com/) — Hermes Agent
- [Coolify](https://coolify.io/) — self-hosted PaaS

---

*Създадено с ❤️ и много звезди 🌟*
