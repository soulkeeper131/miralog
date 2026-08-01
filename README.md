# 🔮 МираСкоп (Miraskop)

**Персонален астролог с изкуствен интелект** — изчислява натални карти, синастрия, транзити и нумерология с професионален астрологичен енджин и AI интерпретации на български език.

🌐 **https://miralog.blv.bg**

---

## ✨ Възможности

| Категория | Функция |
|-----------|---------|
| 🔐 **Достъп** | Един фиксиран администраторски акаунт, JWT автентикация |
| 👤 **Хора** | Добавяне, редакция и изтриване на хора с рождени данни |
| 🔮 **Натална карта** | Пълна карта с планети, аспекти, домове, визуализация |
| 🎨 **SVG колело** | Графична визуализация на картата с планети и аспекти |
| 💫 **Синастрия** | Сравнение между двама души + AI четене на съвместимостта |
| 🪐 **Транзити** | Текущи планетарни транзити за произволна дата |
| 📅 **Дневен хороскоп** | AI-генериран хороскоп базиран на днешните транзити |
| 📊 **Периоден анализ** | Проверка на транзитни промени в избран период |
| 🔢 **Нумерология** | Питагоров анализ: жизнен път, съдба, душа, личност |
| 🤖 **AI Интерпретации** | Claude 3.5 Sonnet / DeepSeek — натална карта, нумерология, съвместимост |
| ⚡ **Кеширане** | AI отговорите се кешират — не се харчат токени повторно |
| 🌓 **Тъмна/светла тема** | Автоматично според системата, с ръчен превключвател |
| 🇧🇬 **Български** | Целият интерфейс и обясненията са на български |
| 📱 **Мобилен** | Адаптивен дизайн с safe-area insets за iOS |

---

## 🧠 AI Интерпретации

МираСкоп поддържа два AI провайдъра за генериране на персонализирани астрологични четения:

- **Anthropic Claude 3.5 Sonnet** (препоръчителен) — `ANTHROPIC_API_KEY`
- **DeepSeek** — `DEEPSEEK_API_KEY`

Интерпретациите включват:
- 🪐 **Натална карта** — пълен анализ на всички планети, знаци, домове и аспекти
- 🔢 **Нумерология** — значение на числата в контекста на хороскопа
- 💕 **Съвместимост** — 7-аспектен анализ на връзката между двама души
- 📅 **Дневен хороскоп** — базиран на реалните транзитни аспекти за деня

---

## 🛠 Технологии

| Слой | Технология |
|------|-----------|
| **Бекенд** | Python 3.11 + FastAPI |
| **Астрология** | [immanuel](https://github.com/astronomancy/immanuel) + Swiss Ephemeris |
| **База данни** | SQLite (persons.db) |
| **AI** | Anthropic Claude / DeepSeek (OpenAI-съвместим API) |
| **Автентикация** | JWT + bcrypt |
| **Шаблони** | Jinja2 |
| **Визуализация** | SVG (ръчно генерирана натална карта) |
| **Контейнеризация** | Docker (python:3.11-slim) |
| **Хостинг** | Self-hosted (Coolify) |

---

## 📂 Структура на проекта

```
mirolog/
├── app.py              # FastAPI приложение — всички endpoints и бизнес логика
├── chart_svg.py        # SVG генератор на натална карта (колело с планети и аспекти)
├── translations.py     # Преводи и обяснителни значения на български
├── numerology.py       # Питагорова нумерология (жизнен път, съдба, душа, личност)
├── Dockerfile          # Многостепенен Docker build с Swiss Ephemeris
├── requirements.txt    # Python зависимости
├── .gitignore
├── static/             # Статични файлове (създава се при build)
├── ephe/               # Swiss Ephemeris файлове (изтеглят се при build)
├── data/               # SQLite база данни (монтира се като volume)
└── templates/
    ├── index.html      # Начална страница — списък с хора
    ├── login.html      # Вход
    ├── dashboard.html  # Табло след вход
    ├── chart.html      # Натална карта (всички табове)
    ├── synastry.html   # Синастрия между двама души
    ├── add.html        # Добавяне на човек
    └── settings.html   # Настройки (AI ключове)
```

---

## 🚀 Инсталация и стартиране

### Локално (development)

```bash
# 1. Клонирай репото
git clone https://github.com/soulkeeper131/miralog.git
cd mirolog

# 2. Създай виртуална среда
python3 -m venv venv
source venv/bin/activate

# 3. Инсталирай зависимостите
pip install -r requirements.txt

# 4. Създай нужните директории
mkdir -p data static ephe

# 5. Изтегли Swiss Ephemeris файлове
curl -sL -o ephe/seas_18.se1 https://raw.githubusercontent.com/aloistr/swisseph/master/ephe/seas_18.se1
curl -sL -o ephe/sepl_18.se1 https://raw.githubusercontent.com/aloistr/swisseph/master/ephe/sepl_18.se1
curl -sL -o ephe/semo_18.se1 https://raw.githubusercontent.com/aloistr/swisseph/master/ephe/semo_18.se1
curl -sL -o ephe/sefstars.txt https://raw.githubusercontent.com/aloistr/swisseph/master/ephe/sefstars.txt
curl -sL -o ephe/seorbel.txt https://raw.githubusercontent.com/aloistr/swisseph/master/ephe/seorbel.txt

# 6. Създай .env файл (опионално)
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
echo "DEEPSEEK_API_KEY=sk-..." >> .env

# 7. Стартирай
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Отвори **http://localhost:8000**

### Docker

```bash
docker build -t miraskop .
docker run -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  miraskop
```

---

## 🔑 Environment променливи

| Променлива | Описание | Default |
|-----------|----------|---------|
| `SE_EPHE_PATH` | Път до Swiss Ephemeris файлове | `/app/ephe` |
| `SECRET_KEY` | JWT secret ключ | `change-me-in-production...` |
| `ADMIN_EMAIL` | Имейл на администратора | `admin@miralog.bg` |
| `ADMIN_PASSWORD` | Парола на администратора | `admin123` |
| `ANTHROPIC_API_KEY` | API ключ за Claude | — |
| `DEEPSEEK_API_KEY` | API ключ за DeepSeek | — |
| `HOSTNAME` | Хост за FastAPI (Docker) | `0.0.0.0` |

---

## 🔌 API Endpoints

### Автентикация
| Метод | Път | Описание |
|-------|-----|---------|
| POST | `/api/auth/login` | Вход с email/password → JWT токен |
| GET | `/api/auth/me` | Информация за текущия потребител |

### Хора
| Метод | Път | Описание |
|-------|-----|---------|
| GET | `/api/persons` | Списък с хора |
| POST | `/api/persons` | Добавяне на човек |
| GET | `/api/persons/{id}` | Детайли за човек |
| DELETE | `/api/persons/{id}` | Изтриване на човек |

### Натална карта
| Метод | Път | Описание |
|-------|-----|---------|
| GET | `/api/persons/{id}/natal` | Натална карта (JSON) |
| GET | `/api/persons/{id}/natal.txt` | Текстово представяне |
| GET | `/api/persons/{id}/chart.svg` | SVG колело |
| GET | `/api/persons/{id}/interpretation` | AI интерпретация на наталната карта |

### Синастрия & Транзити
| Метод | Път | Описание |
|-------|-----|---------|
| POST | `/api/synastry` | Синастрия между двама души |
| POST | `/api/synastry/interpretation` | AI четене на съвместимостта |
| POST | `/api/transits` | Транзити за дата |
| POST | `/api/period-influence` | Транзитни промени в период |

### Хороскоп & Нумерология
| Метод | Път | Описание |
|-------|-----|---------|
| GET | `/api/persons/{id}/daily-horoscope` | Дневен хороскоп |
| GET | `/api/persons/{id}/numerology` | Нумерологични числа |
| GET | `/api/persons/{id}/numerology/interpretation` | AI интерпретация на числата |

### Страници (HTML)
| Път | Описание |
|-----|---------|
| `/` | Начална страница |
| `/login` | Вход |
| `/dashboard` | Табло |
| `/chart/{id}` | Натална карта |
| `/synastry` | Синастрия |
| `/settings` | Настройки |
| `/healthz` | Health check |

---

## 🔒 Сигурност

- **JWT токени** с 30-дневна валидност
- **bcrypt** хеширане на пароли
- **API ключ** защита за POST/PUT/DELETE ендпойнти
- **GET ендпойнти** за натални карти са публични (достъпни без токен)
- **Един администраторски акаунт** — без публична регистрация
- **AI ключове** се съхраняват в SQLite базата, не в environment

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
