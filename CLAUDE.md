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
| `saft.py` | SAF-T XML за НАП (Наредба Н-18), XSD в `n18/` (копие в `docs/n18/`) |
| `chart_svg.py` | SVG колело на наталната карта |
| `numerology.py` | Питагорова нумерология |
| `translations.py` | Преводи и обяснения на знаци, планети, домове, аспекти |
| `bg_text.py` | Механични корекции на българския текст от AI (във/със, пунктуация) |
| `feature_pages.py` | Лендинг страниците на модулите (`/natalna-karta`, `/akashovi-zapisi`…) |
| `horoscope_signs.py`, `planet_pages.py`, `house_pages.py` | Данни за SEO страниците (зодии, планети, домове) |
| `scripts_check_paid_access.py` | Само чете: сверява плащания ↔ `feature_purchases` ↔ `unlocked_features()` |
| `scripts_reconcile_stripe.py` | Сверява Stripe с базата: платени сесии без отключване и връщания без запис; `--apply` поправя |
| `scripts/gen_og_image.py` | Генерира `static/og-image.jpg` (иска Pillow, не е в requirements) |
| `tests/` | pytest: `test_access`, `test_payments`, `test_security`, `test_edge_cases` |
| `templates/` | Jinja2 шаблони, всеки е самостоятелен (няма общ `base.html`); общи парчета: `_analytics.html`, `_critical.html` |
| `static/` | CSS/JS (`ui`, `landing`, `modules`, `locked`, `consent`), лога, favicon, og-image |
| `docs/` | `API.md`, `DEPLOY.md`, `PLAN-*.md` (вкл. `PLAN-deploy-n18.md` — стъпките за деплоя на Н-18), `TODO.md`, `n18/`, `superpowers/{specs,plans}` |

Ориентири в `app.py` (търси по име, редовете се менят): `init_db`, `lifespan`, `admin_host_guard`, `FEATURE_CATALOGUE`, `require_feature`, `unlocked_features`, `bundle_offer`, `call_ai`, `fulfill_checkout_session`, `run_scheduled_jobs`, `build_person_pdf`.

## Ключови механизми

- **Модули и достъп:** всичко е еднократна покупка (няма абонамент). `chart` и `horoscope` се дават безплатно при регистрация (`FREE_ON_SIGNUP`); `planets`/`aspects` вървят с плана `demo` („Основен“, до 2 карти). Платените модули са `profile`, `period`, `love` (+1 карта), `akashic`, `numerology`, `moon`, а пакетът `bundle` струва 25 €. Цените са в таблица `feature_prices`, редактират се от админ панела. Заключен модул връща **402** с оферта. Админът вижда всичко.
- **Плащания (Stripe) и Н-18:** `fulfill_checkout_session` е устойчив на повторения и на едновременни извиквания (webhook + връщането на клиента): записът и номерът на документа са в една транзакция (`record_stripe_sale`, `write_transaction` = `BEGIN IMMEDIATE`), отключване докато не мине (`payments.granted_at`), документи точно веднъж (`documents_at`, във фона). Реално платеното по редове е в `payment_items` (пакетът е един ред; промо кодът се разпределя с `allocate_cents`; ДДС-ът на поръчката се разпределя по редовете — `split_vat_over_lines`, закръглянето е едно: `saft.vat_cents_of`). Плащане **не се трие**: анулира се (`voided_at`; по анулирано няма връщане и то не влиза в одиторския файл) или се отбелязва връщане в `payment_refunds` (Stripe `charge.refunded` го прави сам, с датата на връщането/събитието, не на продажбата). Документът за клиента (`build_sale_receipt`, чл. 52о) е с 10-цифрен номер от `sale_documents`: следващият е над `sale_doc_seed` и над всяко онлайн плащане без нов номер (`_NEXT_SALE_DOC_NUMBER`) — такова е записано от по-стар код (вкл. стар контейнер по време на деплой), документът му носи id на плащането и то не получава нито редове, нито нов документ. Данъчна група „Б“, номер на поръчката (= id на плащането), `pi_…` и QR по Прил. 18а (`sale_qr_data`). Ръчно плащане с метод `stripe` се отказва. Одиторският файл (`build_month_saft` → `saft.py`, XSD в `n18/`) е по месец в часовата зона на София; `run_saft_automation` минава веднъж за месеца (отметка `auto_at` в `settings` `saft:ГГГГ-ММ`) на 1-во число след 6:00 и праща файла на собственика; месец без продажби и връщания няма файл (`no_sales`, схемата иска поне една поръчка) — едно писмо, без напомняне. Подаването в НАП е ръчно с КЕП до 15-о. Фактурата пази номера и датата си (`invoices.issued_at`) и при „изпрати пак“.
- **Админ поддомейн:** панелът е само на `admin.<BRAND_DOMAIN>` (`ADMIN_HOST`). На основния домейн `/admin` и `/api/admin/*` връщат 404, а на админ хоста е позволено само admin/auth/static.
- **Auth:** JWT (30 дни) в `Authorization: Bearer`, в `?token=` или в бисквитката `miralog_token`; bcrypt пароли (отрязани до 72 байта, както при bcrypt < 5), TOTP 2FA, Google/Facebook OAuth. Всички пътища минават през `user_for_token()`: подпис + акаунтът съществува + не е блокиран + `tv` в токена == `users.token_version`. `bump_token_version()` отменя всички сесии — вика се при смяна/нова парола, блокиране, смяна на имейл и първо свързване на Google към заварен акаунт; токен без `tv` (отпреди) се чете като 0. Входът: имейл без значение от регистъра (`get_user_by_email`), лимит по реален IP (`client_ip`): 5 грешни за имейл+IP и 30 за IP → 15 мин; грешните 2FA кодове се броят по акаунт. 2FA важи и за Google/Facebook (еднократно предизвикателство → `/api/auth/totp`). OAuth свързва заварен акаунт по имейл само при `email_verified` от Google; Facebook не свързва автоматично (`OAuthRefused`). Админ права: само първоначално, когато няма нито един админ (не при всеки старт по `ADMIN_EMAIL`).
- **AI кеш:** разчитанията се пазят в `ai_cache` (per person + `cache_key`, напр. `horoscope:2026-09-29`). SEO текстовете са в отделни `*_cache` таблици. Генерирането върви във фонови нишки (`ai_job`), а страницата пита докато не е готово (`pending`). Грешка се показва `AI_RETRY_AFTER` (60 s), после се опитва наново; приключилите задачи се чистят след час. Без AI ключ генераторите хвърлят `AINotConfigured` (не „успяват“ без текст). `/healthz` е async — не чака заетите от AI нишки.
- **Фонови задачи** (`_background_jobs_loop`, веднъж на час): дневен backup в `data/backups/` (пази `BACKUP_KEEP_DAYS`) и digest имейли. `_horoscope_warm_loop` (на 10 мин) пише 12-те хороскопа по зодия след полунощ по София — външен cron няма и не е нужен.
- **Модели:** платените разчитания → `PAID_MODEL` (Pro). Дневният личен хороскоп → Pro само за платили поне един модул (`is_paying_customer`), иначе Flash. Фоновите задачи за личен хороскоп са с ключ на човек (`horoscope:{person_id}:{дата}`), не общ.
- **Логове:** логерът `miraskop` пише в stdout и в `data/logs/app.log` (14 дни). Всяка заявка има код (`REQUEST_ID`, заглавка `X-Request-ID`), който клиентът вижда при 5xx; middleware-ът `request_context` пише реда за заявката (uvicorn access log е изключен). `ai_job` пренася кода във фоновата нишка, `call_ai` записва време и грешки без подканата, `audit()` се дублира в лога. Търсене: Админ → Логове (`/api/admin/logs`)
- **AI разходи:** всяко `call_ai` пише ред в `ai_usage` (токени от всеки отговор на доставчика, вкл. повторния опит при `finish_reason=length`; цена по `AI_PRICES` в USD, DeepSeek наполовина извън 01–04/06–10 UTC пн–пт). Източникът (`client`/`seo`/`background`/`admin`) и модулът идват от адреса на заявката през `AI_ORIGIN`. Админ → AI разходи (`/api/admin/ai-usage`). При смяна на цените на доставчик — обнови `AI_PRICES`
- **Известия до собственика (имейл):** `notify_owner(kind, …)` — видове `new_users`, `payments`, `problems`, `daily`, всеки с превключвател `notify_{kind}` в Админ → Настройки → Известия. Пращат се във фона (`NOTIFY_ASYNC`; в тестовете е изключено). Проблемите (`report_problem`: срив, отказан webhook, 3 AI грешки за 30 мин) — най-много едно писмо на `PROBLEM_COOLDOWN` за вид. Сутрешното обобщение (`build_daily_summary`) тръгва от часовия цикъл след 8:00 и се отбелязва в `daily_summary_sent`; прегледът е на `/api/admin/daily-summary`
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
