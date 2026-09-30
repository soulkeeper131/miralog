# 📡 API Reference

АстроКарта използва REST API с JSON отговори. Базов URL: `https://astrokarta.bg`
(админ API-то е само на `https://admin.astrokarta.bg`).

Интерактивна схема (FastAPI) има на `/docs` и `/openapi.json` **само извън production**.
В production те са изключени.

Всеки отговор носи заглавка `X-Request-ID` (код от 6 знака). При грешка 5xx
кодът е и в съобщението, а по него заявката се намира в Админ → Логове.

---

## Общи правила

### Автентикация

Повечето endpoint-и искат JWT токен (валиден 30 дни). Приема се от:

```
Authorization: Bearer eyJhbGci...
```

HTML страниците и `reading-audio` приемат токена и от бисквитката
`miralog_token` или от `?token=`, защото `<audio>` не може да праща заглавки.

### Нива на достъп

В таблиците по-долу колоната „Достъп“ означава:

| Стойност | Значение |
|----------|----------|
| публичен | без токен |
| вход | валиден токен |
| модул `X` | токен + отключен модул `X`; иначе **402** с оферта |
| админ | токен на акаунт с `role = admin` **и** хост `admin.<домейн>` |

Модулите са `chart`, `planets`, `aspects`, `horoscope` (безплатни при
регистрация), `profile`, `period`, `love`, `akashic`, `numerology`, `moon`
(платени). Админът има достъп до всичко.

### Грешки

Всички грешки връщат JSON с `detail`:

```json
{ "detail": "Описание на грешката" }
```

| Код | Описание |
|-----|---------|
| 400 | Невалидни входни данни |
| 401 | Липсваща/изтекла сесия или грешна парола |
| 402 | Модулът не е отключен. `detail` е обект: `{reason: "locked", feature, feature_name, message, offer, bundle}` |
| 403 | Блокиран акаунт или липса на админ права |
| 404 | Ресурсът не е намерен (включително `/api/admin/*` извън админ хоста) |
| 409 | Имейлът вече е регистриран (`/api/onboard` връща `{reason: "account_exists", message}`) |
| 429 | Твърде много опити: вход (5 грешни → 15 мин блок), регистрации (20/час от IP), безплатни карти (30/10 мин от IP), писма за нова парола (3/час към адрес, 10/час от IP) |
| 503 | Stripe не е конфигуриран |

### AI разчитания (асинхронни)

Endpoint-ите `.../interpretation`, `daily-horoscope` и публичните SEO API-та
пускат генерирането във фонов процес. Докато текстът не е готов, те връщат
`{"pending": true, ...}` и клиентът трябва да пита отново след няколко секунди.
Готовият текст се кешира. `?refresh=true` генерира нов само при заявка от
админ. За всички останали параметърът се игнорира (личният дневен хороскоп и
публичните SEO API-та), за да не се харчат AI кредити в цикъл.

---

## Автентикация и акаунт

| Метод | Път | Достъп | Описание |
|-------|-----|--------|----------|
| POST | `/api/auth/login` | публичен | Вход, връща токен |
| POST | `/api/auth/register` | публичен | Регистрация с имейл и парола (≥8 знака) |
| POST | `/api/onboard` | публичен | Регистрация + първа карта наведнъж (от началната страница) |
| GET | `/api/auth/me` | вход | Текущ потребител, план, отключени модули, оферти |
| GET | `/api/auth/{provider}/start` | публичен | Google/Facebook вход (`provider` = `google` \| `facebook`), `?next=` |
| GET | `/api/auth/{provider}/callback` | публичен | OAuth callback |
| POST | `/api/auth/forgot-password` | публичен | `{email}` → писмо с линк |
| POST | `/api/auth/reset-password` | публичен | `{token, new_password}` (≥8 знака) |
| GET | `/api/account` | вход | Профилът на потребителя |
| POST | `/api/account` | вход | `{display_name?, email?}` |
| POST | `/api/account/password` | вход | `{current_password, new_password}` |
| POST | `/api/account/digest` | вход | `{digest_opt_in: bool}`, ежедневен имейл с хороскопа |
| GET | `/api/account/export` | вход | Експорт на всички лични данни (GDPR) |
| DELETE | `/api/account` | вход | Изтриване на акаунта и данните |

### POST /api/auth/login

```json
// Request (totp_code само при включена 2FA)
{ "email": "user@example.com", "password": "...", "totp_code": "123456" }

// Response 200
{
  "token": "eyJhbGci...",
  "user": { "id": 1, "email": "user@example.com", "role": "user" }
}
```

### POST /api/onboard

```json
// Request (password е по желание: без него идва имейл за задаване на парола;
// wanted = модули за незабавно плащане, "bundle" = пакетът)
{
  "email": "maria@example.com", "name": "Мария",
  "year": 1988, "month": 10, "day": 3, "hour": 8, "minute": 15,
  "lat": 42.6977, "lon": 23.3219, "timezone": "Europe/Sofia",
  "password": "...", "wanted": ["love"]
}

// Response 200
{
  "ok": true, "person_id": 12, "token": "eyJhbGci...", "chose_password": true,
  "chart_url": "/chart/12?token=...",
  "checkout_url": "https://checkout.stripe.com/...",   // ако има wanted и Stripe
  "checkout_error": "...",                              // ако плащането не може да се подготви
  "wanted": ["love"], "bundle": false
}
```

### GET /api/auth/me

```json
{
  "id": 1, "email": "user@example.com", "role": "user",
  "is_admin": false, "is_blocked": false,
  "plan": { "key": "demo", "name": "Основен", "max_persons": 2, "features": ["planets", "aspects"] },
  "features": ["chart", "horoscope", "planets", "aspects"],
  "purchased": ["chart", "horoscope"],
  "offers": [ { "key": "profile", "name": "...", "price_cents": 500, "currency": "EUR" } ]
}
```

---

## Публични помощни

| Метод | Път | Достъп | Описание |
|-------|-----|--------|----------|
| GET | `/api/public/config` | публичен | `{mock_payments, stripe}` |
| GET | `/api/public/catalogue` | публичен | Модули, цени и пакет за лендинга |
| POST | `/api/guest/chart` | публичен | Карта без регистрация (нищо не се пази): `{chart, profile, svg}` |
| GET | `/api/public/geocode?q=` | публичен | Търсене на място → координати и часова зона |
| GET | `/api/geocode?q=` | вход | Същото, за вписани потребители |
| GET | `/api/zodiac-signs` | вход | Списък със знаците (за любовния модул) |
| GET | `/healthz` | публичен | `{"status": "ok"}` |

---

## Хора (карти)

| Метод | Път | Достъп | Описание |
|-------|-----|--------|----------|
| GET | `/api/persons` | вход | Картите на потребителя |
| GET | `/api/persons/{id}` | вход | Една карта |
| POST | `/api/persons` | вход | Нова карта (**form data**, не JSON) |
| DELETE | `/api/persons/{id}` | вход | Изтриване → `{"deleted": id}` |
| POST | `/api/persons/{id}/natal` | модул `chart` | Промяна на рождените данни (JSON), изчиства AI кеша |

### POST /api/persons

`application/x-www-form-urlencoded` или `multipart/form-data` с полета
`name`, `year`, `month`, `day`, `hour` (0), `minute` (0), `lat`, `lon`,
`timezone` (`Europe/Sofia`). Отговор: `{"id": 2, "name": "Мария", "user_id": 1}`.

Броят карти е ограничен от плана (по подразбиране 2), а модулът `love`
дава +1. При достигнат лимит се връща грешка с обяснение.

---

## Натална карта и профил

| Метод | Път | Достъп | Описание |
|-------|-----|--------|----------|
| GET | `/api/persons/{id}/natal` | модул `chart` | Натална карта (JSON) |
| GET | `/api/persons/{id}/natal.txt` | модул `chart` | Текстово представяне |
| GET | `/api/persons/{id}/chart.svg` | модул `chart` | SVG колело |
| GET | `/api/persons/{id}/teaser` | модул `chart` | Кратък безплатен откъс |
| GET | `/api/persons/{id}/profile` | модул `chart` | Данните на астро портрета |
| GET | `/api/persons/{id}/profile/interpretation` | модул `profile` | AI пълен профил |
| GET | `/api/persons/{id}/akashic` | модул `akashic` | Кармични точки + нумерология |
| GET | `/api/persons/{id}/akashic/interpretation` | модул `akashic` | AI акашови записи |
| GET | `/api/persons/{id}/numerology` | модул `numerology` | Нумерологични числа |
| GET | `/api/persons/{id}/numerology/interpretation` | модул `numerology` | AI нумерология |

### GET /api/persons/{id}/natal

Пълна натална карта. Изисква модул `chart` (даден безплатно при регистрация).

```json
{
  "native": {
    "name": "Иван Петров",
    "datetime": "1990-05-15 14:30",
    "lat": 42.6977,
    "lon": 23.3219,
    "timezone": "Europe/Sofia"
  },
  "house_system": "Placidus",
  "house_system_bg": "Плацидус",
  "shape": "Bundle",
  "shape_bg": "Сноп",
  "shape_meaning": "Сноп — всички планети са концентрирани...",
  "diurnal": true,
  "moon_phase": "Waxing Crescent",
  "moon_phase_bg": "Растящ сърп",
  "moon_phase_meaning": "Растящ сърп — първи стъпки...",
  "objects": {
    "0": {
      "name": "Sun",
      "name_bg": "Слънце",
      "icon": "☀️",
      "sign": "Taurus",
      "sign_bg": "Телец",
      "sign_longitude": "24°35'",
      "house": "1st House",
      "house_bg": "1-ви дом",
      "movement": "Direct",
      "movement_bg": "Директен",
      "name_meaning": "Слънцето е ядрото на идентичността...",
      "sign_meaning": "Земен, фиксиран знак. Стабилност...",
      "house_meaning": "Дом на личността, тялото..."
    }
    // ... останалите планети и точки
  },
  "aspects": [
    {
      "type": "Trine",
      "type_bg": "Тригон",
      "active": "Sun",
      "active_bg": "Слънце",
      "passive": "Mars",
      "passive_bg": "Марс",
      "icon": "△",
      "aspect_class": "harmony",
      "orb": 2.5,
      "distance": "122°30'",
      "type_meaning": "Тригон — лек, хармоничен поток..."
    }
    // ... останалите аспекти
  ],
  "houses": [
    {
      "number": 1,
      "sign": "Taurus",
      "sign_bg": "Телец",
      "sign_longitude": "15°20'",
      "longitude": 45.33
    }
    // ... 12 дома
  ]
}
```

### GET /api/persons/{id}/natal.txt

Текстово представяне на наталната карта (plain text). Ползва се за AI промптовете.

### GET /api/persons/{id}/chart.svg

SVG изображение: зодиакално колело с планети, домове и аспектни линии.

---

## Хороскопи и транзити

| Метод | Път | Достъп | Описание |
|-------|-----|--------|----------|
| GET | `/api/persons/{id}/daily-horoscope` | модул `horoscope` | AI дневен хороскоп (асинхронно) |
| POST | `/api/transits` | модул `horoscope` | Транзити за дата |
| POST | `/api/period-influence` | модул `period` | Дни с настъпващи/напускащи аспекти (макс. 62 дни) |
| POST | `/api/period-interpretation` | модул `period` | AI разчитане на периода |
| GET | `/api/lunar-calendar?year=&month=` | модул `moon` | Лунен календар за месец |

### GET /api/persons/{id}/daily-horoscope

```json
// Докато се генерира
{ "pending": true, "date": "29.09.2026", "cache_key": "horoscope:2026-09-29" }

// Готово
{ "interpretation": "## Общо усещане за деня...", "summary": { ... }, "date": "29.09.2026", ... }
```

### POST /api/transits

```json
// Request
{ "person_id": 1, "target_date": "2026-08-15T12:00:00" }
```

### POST /api/period-influence / /api/period-interpretation

```json
// Request
{ "person_id": 1, "start_date": "2026-08-01", "end_date": "2026-08-31" }
```

---

## Любов и синастрия

Всички искат модул `love`.

| Метод | Път | Описание |
|-------|-----|----------|
| POST | `/api/love-match` | Съвместимост по зодия или по пълни данни на партньора |
| POST | `/api/love-match/interpretation` | AI любовен хороскоп |
| POST | `/api/synastry` | Синастрия между две карти от профила |
| POST | `/api/synastry/interpretation` | AI разчитане на синастрията |

```json
// /api/love-match: само по зодия
{ "person_id": 1, "partner_sign": "Taurus" }

// /api/love-match: по пълни рождени данни
{
  "person_id": 1, "partner_name": "Иван",
  "partner_year": 1987, "partner_month": 4, "partner_day": 20,
  "partner_hour": 12, "partner_minute": 0,
  "partner_lat": 42.15, "partner_lon": 24.75, "partner_timezone": "Europe/Sofia"
}

// /api/synastry
{ "person1_id": 1, "person2_id": 2 }
```

---

## Разчитания: PDF, аудио, имейл, споделяне

`key` е ключът на кешираното разчитане (`ai_cache.cache_key`), напр. `profile`,
`akashic`, `numerology:2026`, `love:Taurus`, `period:2026-08-01:2026-08-31`,
`synastry:1:2`, `horoscope:2026-09-29`. Правото се проверява по частта преди
първото `:` (`love-full` се брои към `love`).

| Метод | Път | Достъп | Описание |
|-------|-----|--------|----------|
| GET | `/api/persons/{id}/reading.pdf?key=` | вход | PDF на разчитането |
| GET | `/api/persons/{id}/reading-audio?key=` | вход (и бисквитка) | MP3, прочетено на български (edge-tts, кешира се в `data/audio/`) |
| POST | `/api/persons/{id}/email-reading` | вход | `{key, to?}` → PDF по имейл |
| POST | `/api/persons/{id}/share` | вход | `{cache_key}` → `{token, url}` |
| GET | `/api/share/{token}` | публичен | Споделеното разчитане |

---

## Модули и плащания

| Метод | Път | Достъп | Описание |
|-------|-----|--------|----------|
| GET | `/api/features` | вход | Каталог на модулите за този акаунт (отключени, цени, пакет) |
| POST | `/api/features/{key}/request` | вход | Купуване на модул: Stripe Checkout, ако е включен, иначе имейл до админа |
| POST | `/api/features/bundle/request` | вход | Същото за пакета „Всички модули“ |
| GET | `/api/billing/status` | вход | `{stripe_enabled, plan_key, purchased, digest_opt_in}` |
| POST | `/api/billing/checkout/feature/{key}` | вход | Stripe Checkout сесия → URL (`key` може да е `bundle`) |
| GET | `/api/billing/session/{session_id}` | вход | Приключва плащането веднага след връщане от Stripe |
| POST | `/api/stripe/webhook` | Stripe подпис | `checkout.session.completed` → отключва модулите |
| POST | `/api/dev/mock-pay` | вход | `{keys: [...]}`. Тестово плащане, само при `MOCK_PAYMENTS=1` извън production |

Плащането се приключва по два пътя (webhook и `billing/session`). Който
пристигне пръв, отключва; вторият не прави нищо.

---

## Админ API

Само на `admin.<домейн>` и само за админ. Всички пътища започват с `/api/admin`.

| Метод | Път | Описание |
|-------|-----|----------|
| GET | `/overview` | Табло: приходи, потребители, активност, backups |
| GET / POST | `/users` | Списък (`?q=`) / нов потребител |
| PATCH / DELETE | `/users/{id}` | Промяна (план, роля, блокиране, парола) / изтриване |
| GET | `/plans` | Планове |
| PUT / DELETE | `/plans/{key}` | Запис / изтриване на план |
| GET / POST | `/payments` | Дневник на плащанията (`?user_id=`) / ръчно плащане |
| DELETE | `/payments/{id}` | Изтриване на плащане |
| GET | `/feature-prices` | Цените на модулите |
| PUT | `/feature-prices/{key}` | `{price_cents, currency, is_purchasable}` |
| GET / POST | `/feature-purchases` | Покупки (`?user_id=`) / ръчно даване на модул |
| DELETE | `/feature-purchases/{user_id}/{key}` | Отнемане на модул |
| GET | `/audit` | Одит лог (`?event=&user_id=&limit=&offset=`) |
| GET | `/saft?year=&month=` | SAF-T XML за НАП (windows-1251) |
| GET | `/logs?q=&level=&limit=` | Търсене в `data/logs` по код на заявка, `user=ID` или текст; `level` за „само проблеми“ |
| GET | `/ai-usage?days=30` | AI разходи: токени и цена по източник, модул, модел, ден и клиент (`days=0` = всичко) |
| GET | `/daily-summary` | Текстът на сутрешното обобщение за вчера (само показва, не праща) |
| GET | `/2fa/status` | Статус на 2FA |
| POST | `/2fa/setup` · `/2fa/confirm` · `/2fa/disable` | Включване/изключване на TOTP |
| GET / POST | `/settings` | AI, SMTP, шаблони, SEO, марка, OAuth, известия, юридически данни |
| POST | `/settings/logo` | Качване на лого (form: `file`, `slot`) |
| POST | `/settings/logo/reset` | Връщане на оригиналното лого |
| POST | `/settings/test-email` | Тестов имейл |
| POST | `/templates/preview` | Преглед на имейл шаблон |

---

## SEO страници (публични)

HTML страниците и техните API-та за съдържание. Съдържанието се генерира от AI
при първо отваряне и се кешира (дневният хороскоп по зодия се обновява всеки ден).
Хороскопите по зодия приложението генерира само до 10 мин след полунощ
(София, `_horoscope_warm_loop`), без външен cron. `/warm` endpoint-ите пускат
ръчно генерирането на всичко липсващо. Генерира се само липсващото, затова
повторното извикване не харчи токени.

| Страница | API | Warm |
|----------|-----|------|
| `/horoskop`, `/horoskop/{знак}` | `/api/horoskop/{знак}` | `/api/horoskop/warm` |
| `/{планета}-v-{знак}` (напр. `/luna-v-skorpion`) | `/api/planeta/{планета}-v-{знак}` | `/api/planeta/warm` |
| `/{планета}-v-{N}-dom` (напр. `/luna-v-7-dom`) | `/api/dom/{планета}-v-{N}` | `/api/dom/warm` |
| `/zodia/{знак}` | `/api/zodia/{знак}` | `/api/zodia/warm` |
| `/savmestimost`, `/savmestimost/{знак}-{знак}` | `/api/savmestimost/{двойка}` | `/api/savmestimost/warm` |

Лендинги на модулите (от `feature_pages.py`): `/natalna-karta`, `/planeti`,
`/aspekti`, `/dneven-horoskop`, `/astrologicheski-profil`, `/horoskop-za-period`,
`/lyubovna-savmestimost`, `/akashovi-zapisi`, `/numerologia`, `/lunen-kalendar`.

Други: `/robots.txt`, `/sitemap.xml`, `/llms.txt`.

---

## HTML страници на приложението

| Път | Описание |
|-----|----------|
| `/` | Начална (лендинг) |
| `/start` | Безплатна карта за гости |
| `/welcome` | След регистрация |
| `/register`, `/login`, `/forgot-password`, `/reset-password` | Акаунт |
| `/dashboard` | Табло с картите |
| `/chart/{id}` | Натална карта и всички модули |
| `/synastry` | Синастрия |
| `/moon` | Лунен календар |
| `/settings` | Настройки на акаунта |
| `/share/{token}` | Споделено разчитане |
| `/privacy`, `/terms` | Политика и общи условия |
| `/admin` | Админ панел (само на админ хоста) |
