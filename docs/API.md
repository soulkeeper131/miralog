# 📡 API Reference

АстроКарта използва REST API с JSON отговори. Базов URL: `https://astrokarta.bg`

---

## Автентикация

### POST /api/auth/login

Вход с имейл и парола. Връща JWT токен.

```json
// Request
{
  "email": "admin@astrokarta.bg",
  "password": "admin123"
}

// Response 200
{
  "access_token": "eyJhbGci...",
  "token_type": "bearer",
  "user_id": 1,
  "email": "admin@astrokarta.bg"
}
```

**Използване на токена:**
```
Authorization: Bearer eyJhbGci...
```

### GET /api/auth/me

Информация за текущия логнат потребител.

```json
// Response 200
{
  "user_id": 1,
  "email": "admin@astrokarta.bg"
}
```

---

## Хора

### GET /api/persons

Списък с всички хора на текущия потребител.

```json
// Response 200
[
  {
    "id": 1,
    "name": "Иван Петров",
    "year": 1990,
    "month": 5,
    "day": 15,
    "hour": 14,
    "minute": 30,
    "lat": 42.6977,
    "lon": 23.3219,
    "timezone": "Europe/Sofia"
  }
]
```

### POST /api/persons

Добавяне на нов човек.

```json
// Request
{
  "name": "Мария",
  "year": 1988,
  "month": 10,
  "day": 3,
  "hour": 8,
  "minute": 15,
  "lat": 42.6977,
  "lon": 23.3219,
  "timezone": "Europe/Sofia"
}

// Response 200
{
  "id": 2,
  "name": "Мария",
  "message": "Person added"
}
```

### DELETE /api/persons/{id}

Изтриване на човек.

```json
// Response 200
{
  "message": "Person deleted"
}
```

---

## Натална карта

### GET /api/persons/{id}/natal

Пълна натална карта. **Публичен endpoint** (без токен).

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

Текстово представяне на наталната карта (plain text). Полезно за AI промптове.

### GET /api/persons/{id}/chart.svg

SVG изображение на наталната карта — зодиакално колело с планети, домове и аспектни линии.

---

## Синастрия

### POST /api/synastry

Сравнение между двама души.

```json
// Request
{
  "person1_id": 1,
  "person2_id": 2
}

// Response 200
{
  "chart_type": "Composite (Synastry)",
  "person1": { "name": "Иван", ... },
  "person2": { "name": "Мария", ... },
  "objects": { ... },
  "aspects": [ ... ]
}
```

### POST /api/synastry/interpretation

AI-генерирано четене на съвместимостта.

```json
// Request
{
  "person1_id": 1,
  "person2_id": 2
}

// Response 200
{
  "interpretation": "## Анализ на съвместимостта...",
  "cached": false
}
```

---

## Транзити

### POST /api/transits

Транзитни аспекти за конкретна дата.

```json
// Request
{
  "person_id": 1,
  "target_date": "2026-08-15T12:00:00"
}

// Response 200
{
  "transit_objects": { ... },
  "transit_aspects_to_natal": [
    {
      "active": "Jupiter",
      "type": "Trine",
      "passive": "Sun",
      "orb": 1.2
    }
  ],
  "moon_phase": "Full",
  "shape": "Bundle"
}
```

### POST /api/period-influence

Проверка на транзитни промени в избран период (показва само дните с настъпващи или напускащи аспекти).

```json
// Request
{
  "person_id": 1,
  "start_date": "2026-08-01",
  "end_date": "2026-08-31"
}
```

---

## Хороскоп

### GET /api/persons/{id}/daily-horoscope

AI-генериран дневен хороскоп на базата на днешните транзити.

```json
// Response 200
{
  "interpretation": "## Общо усещане за деня...",
  "date": "30.07.2026",
  "cached": true
}
```

Параметри:
- `?refresh=true` — генерира нов хороскоп (игнорира кеша)

---

## Нумерология

### GET /api/persons/{id}/numerology

Питагорови нумерологични числа.

```json
// Response 200
{
  "life_path": { "number": 7, "meaning": "..." },
  "destiny": { "number": 5, "meaning": "..." },
  "soul_urge": { "number": 3, "meaning": "..." },
  "personality": { "number": 11, "meaning": "... (мастер число)" }
}
```

### GET /api/persons/{id}/numerology/interpretation

AI интерпретация на нумерологичните числа.

```json
// Response 200
{
  "interpretation": "## Твоят нумерологичен профил...",
  "cached": false
}
```

---

## AI Интерпретация

### GET /api/persons/{id}/interpretation

Пълна AI интерпретация на наталната карта. Комбинира всички планети, знаци, домове и аспекти в едно свързано четене.

```json
// Response 200
{
  "interpretation": "## Пълна интерпретация...",
  "cached": false
}
```

Параметри:
- `?refresh=true` — игнорира кеша

---

## Настройки

### GET /api/settings

```json
// Response 200
{
  "ai_api_key_set": true,
  "ai_provider": "anthropic"
}
```

### POST /api/settings

```json
// Request
{
  "ai_api_key": "sk-ant-...",
  "ai_provider": "anthropic"
}
```

---

## Грешки

Всички грешки връщат JSON:

```json
{
  "detail": "Описание на грешката"
}
```

| Код | Описание |
|-----|---------|
| 400 | Невалидни входни данни |
| 401 | Липсваща или невалидна автентикация |
| 404 | Ресурсът не е намерен |
| 500 | Вътрешна грешка |
