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

### Сутрешен cron (SEO страници)

Дневните хороскопи по зодия и вечнозелените SEO страници се генерират при
първо отваряне. За да са готови преди ботовете, външен cron (Coolify
Scheduled Task или друг) трябва да вика сутрин:

```bash
curl -s https://astrokarta.bg/api/horoskop/warm
```

При нужда и `/api/planeta/warm`, `/api/dom/warm`, `/api/zodia/warm`,
`/api/savmestimost/warm`. Те генерират само липсващото, затова повторно
извикване не харчи токени.

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

## Проверки след деплой

```bash
# платилите клиенти виждат ли каквото са купили (само чете)
python scripts_check_paid_access.py

# платени Stripe сесии, които не са отключили нищо
python scripts_reconcile_stripe.py            # показва
python scripts_reconcile_stripe.py --apply    # и отключва
```

Пускат се в контейнера (Coolify → app → Terminal) от `/app`.
