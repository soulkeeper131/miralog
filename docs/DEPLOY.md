# 🚀 Деплойване в Coolify

Coolify следи `soulkeeper131/miralog.git`, клон `master`. **Всеки push към
master е автоматичен деплой** на https://astrokarta.bg.

## Изисквания

- Coolify инстанция (self-hosted) с Docker-capable сървър
- DNS записи за `astrokarta.bg` **и** `admin.astrokarta.bg` към сървъра
- Build pack: Dockerfile (в корена на репото)

## Конфигурация

### Environment променливи

Пълният коментиран списък е в [`.env.example`](../.env.example).

**Задължителни.** С `ENVIRONMENT=production` приложението отказва да
стартира, ако някоя липсва или е с примерната стойност от кода:

```env
ENVIRONMENT=production
SECRET_KEY=<поне 32 случайни знака>
ADMIN_PASSWORD=<силна парола>
ADMIN_EMAIL=admin@astrokarta.bg
```

Генериране на `SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`ADMIN_EMAIL` и `ADMIN_PASSWORD` създават акаунта само при празна база.
Смяна на паролата после става от админ панела.

**Плащания (Stripe).** Нужни са **и двата** ключа, иначе checkout е изключен:

```env
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
```

Webhook endpoint в Stripe: `https://astrokarta.bg/api/stripe/webhook`,
събитие `checkout.session.completed`.

Endpoint-ът трябва да е създаден в **Live** режима на Stripe Dashboard, а
`STRIPE_WEBHOOK_SECRET` да е неговият signing secret. Test и Live имат
отделни endpoint-и и отделни `whsec_` ключове. Тестов ключ при истински
плащания значи, че всяка доставка се отхвърля с 400.

Плащането се отключва и когато клиентът се върне на сайта (приложението пита
Stripe директно), затова счупен webhook не личи веднага. Проверка: в
Админ → Активност всяко Stripe плащане трябва да има и `webhook_received`.
Ако има `payment_succeeded` без `webhook_received`, webhook-ът не стига.

**По желание:**

| Група | Променливи |
|-------|-----------|
| AI (ако не е зададен в админ панела) | `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` |
| Имейл (имат предимство пред админ панела) | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_USE_TLS` |
| Социален вход | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `FACEBOOK_APP_ID`, `FACEBOOK_APP_SECRET` |
| Следене на грешки | `SENTRY_DSN` |
| Демо акаунт | `DEMO_PASSWORD` (≥8 знака), `DEMO_EMAIL` |
| Настройка | `AI_THREAD_LIMIT` (8), `BACKUP_KEEP_DAYS` (7), `TTS_CHUNKS` (5), `STRIPE_SUCCESS_URL`, `STRIPE_CANCEL_URL` |

Когато SMTP идва от env, старите SMTP записи в базата се изтриват при
стартиране, за да няма два източника. Админ → Настройки показва кои
`SMTP_*` променливи реално вижда процесът.

За OAuth redirect URI в Google/Facebook конзолата задай
`https://astrokarta.bg/api/auth/google/callback` (съответно `.../facebook/callback`).
Адресът се строи от „SEO → адрес на сайта“ в админ панела. Ако той е празен,
се взима хостът на заявката, и тогава за вход от админ панела трябва да се
разреши и `https://admin.astrokarta.bg/api/auth/.../callback`.

### Persistent Storage

Монтирай постоянен volume:

```
Mount path: /app/data
Type: persistent
```

В него живеят:

| Път | Съдържание |
|-----|-----------|
| `/app/data/persons.db` | SQLite базата: потребители, карти, плащания, настройки, AI кеш |
| `/app/data/backups/` | Дневни копия `persons-ГГГГ-ММ-ДД.db` (пазят се `BACKUP_KEEP_DAYS` дни) |
| `/app/data/uploads/` | Качени от админ панела лога |
| `/app/data/audio/` | Кеш на гласовите четения (mp3) |
| `/app/data/logs/` | Дневни логове `app.log` (пазят се `LOG_KEEP_DAYS`, по подразбиране 14 дни) |

Без volume всеки деплой стартира с празна база и губи потребители и плащания.

### Health Check

```
Path: /healthz
Port: 8000
Method: GET
Expected: 200
```

### Domains

В Coolify UI → app → Domains добави и двата:

- `https://astrokarta.bg` (потребителската част)
- `https://admin.astrokarta.bg` (админ панелът работи **само** на този хост)

### Хороскопите по зодия (без cron)

Приложението само пише дванадесетте дневни хороскопа до 10 минути след
полунощ по българско време (`_horoscope_warm_loop`). Проверката е на всеки
10 минути и генерира само липсващото, затова не харчи повече от 12 текста на
ден. Външен cron не е нужен.

Вечнозелените SEO страници се генерират при първо отваряне; при нужда
`/api/planeta/warm`, `/api/dom/warm`, `/api/zodia/warm`,
`/api/savmestimost/warm` пускат всичко липсващо наведнъж.

## Ресурси

Препоръчителни лимити за стабилна работа:

| Ресурс | Стойност |
|--------|---------|
| Memory | 512 MB |
| CPU | 1 core |
| Memory Swap | 256 MB |

`AI_THREAD_LIMIT` (по подразбиране 8) ограничава едновременните тежки заявки
(AI, PDF, TTS) според тези лимити. На по-голям контейнер може да се вдигне.

## Бележки

- Swiss Ephemeris файловете (~2MB) се изтеглят при Docker build
- Схемата на базата и миграциите се прилагат автоматично при всяко стартиране (`init_db()`)
- Фонов цикъл веднъж на час прави backup на базата и праща digest имейлите
- AI разчитанията искат поне един ключ (в админ панела или през env)
- AI ключът, моделът, SEO, марката, имейл шаблоните и юридическите данни се
  задават от **Админ → Настройки** и се пазят в базата (без рестарт)

## Логове и помощ на клиенти

Всяка заявка получава код от 6 знака (заглавка `X-Request-ID`). При грешка
клиентът вижда „…посочи код 7F3A2C“. Този код стои пред всеки ред от
заявката, включително от фоновото AI генериране.

- **Админ → Логове**: търсене по код, по `user=42` или по текст, с филтър
  „само проблеми“. Чете от `/app/data/logs`, затова вижда и отпреди деплоя.
- **Coolify → Logs**: същите редове на живо, но само от последния деплой.
- `/healthz` и статичните файлове не се записват, освен ако не върнат 5xx.
- Токените в адресите се записват като `token=***`. AI подканите (с
  рождените данни) не се записват.

## Проверки след деплой

```bash
# платилите клиенти виждат ли каквото са купили (само чете)
python scripts_check_paid_access.py

# платени Stripe сесии, които не са отключили нищо
python scripts_reconcile_stripe.py            # показва
python scripts_reconcile_stripe.py --apply    # и отключва
```

Пускат се в контейнера (Coolify → app → Terminal) от `/app`.
