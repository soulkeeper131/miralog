# 🚀 Деплойване в Coolify

## Изисквания

- Coolify инстанция (self-hosted)
- Достъп до Coolify API
- Docker-capable сървър

## Конфигурация

### Environment променливи

```env
ANTHROPIC_API_KEY=*** OpenAI
DEEPSEEK_API_KEY=*** SECRET_KEY=strong-random-secret
```

### Persistent Storage

Монтирай volume за SQLite базата:

```
Mount path: /app/data
Type: persistent
```

### Health Check

```
Path: /healthz
Port: 8000
Method: GET
Expected: 200
```

### Domain

Домейнът се конфигурира през Coolify UI → app → Domains.

## Ресурси

Препоръчителни лимити за стабилна работа:

| Ресурс | Стойност |
|--------|---------|
| Memory | 512 MB |
| CPU | 1 core |
| Memory Swap | 256 MB |

## Бележки

- Swiss Ephemeris файловете (~2MB) се изтеглят при Docker build
- SQLite базата се създава автоматично при първо стартиране
- Администраторският акаунт се създава автоматично ако няма потребители
- AI интерпретациите изискват поне един API ключ (Anthropic или DeepSeek)
