# Ребранд: МираСкоп → АстроКарта

Дата: 2026-08-14
Статус: одобрен от потребителя (Подход А — пълен ребранд)

## Цел

Ребрандиране на приложението навсякъде: име **АстроКарта**, домейн **astrokarta.bg**,
ново лого от `C:\Users\vladi\Downloads\AstroKarta_logo.png` (1254×1254, RGBA, PNG).
Домейнът ще бъде насочен в Coolify по-късно от потребителя — кодът се подготвя сега.

## Решения, взети с потребителя

1. Ново име: **АстроКарта**
2. Нов таглайн: **„Астрология с точността на астрономията“**
3. Едно лого за всички места (хедър, пълно лого, фавикони) — генерира се от AstroKarta_logo.png
4. Вътрешните ключове `miralog_token` / `miralog_theme` / `miraskop_modules_seen` **не се сменят** — смяната би изхвърлила всички вписани потребители
5. Домейнът astrokarta.bg се насочва по-късно; `seo_site_url` се задава предварително

## Част 1: Лого файлове

Генериране с Python/PIL от `AstroKarta_logo.png`, оптимизирани размери:

| Файл | Размер | Употреба |
|------|--------|----------|
| `static/logo-header.png` | 180×180 | Хедъри, login, dashboard (CSS: object-fit) |
| `static/logo-full.png` | 600×600 | Hero секция на landing страницата |
| `static/favicon-32.png` | 32×32 | Икона в таба на браузъра |
| `static/favicon-180.png` | 180×180 | Apple touch icon |

Забележка: сегашните файлове са портретни (180×240, 450×600); новите са квадратни.
CSS-ът ползва `object-fit: contain` с фиксирани размери — квадратът пасва без промени.
Единствено hero секцията на landing.html (въртящо се зодиакално колело) е позиционирана
за портретно лого — да се провери визуалното подравняване и да се коригира CSS само ако се налага.

Оригиналът в Downloads не се пипа.

## Част 2: Код по подразбиране

1. `app.py`:
   - ред 76: `BRAND_DOMAIN` default `miralog.bg` → `astrokarta.bg`
   - ред 530-535 (`BRAND_DEFAULTS`): `brand_name` → `АстроКарта`, `brand_tagline` → `Астрология с точността на астрономията`, `brand_domain` → `astrokarta.bg`
   - ред 2955: User-Agent `MiraSkop/1.0 (astrology chart app)` → `AstroKarta/1.0 (astrology chart app)`
   - ред 331: legacy SEO миграция **запазва** стария низ „МираСкоп — твоята натална карта…“ — това е еднократна миграция за стари инсталации; промяна би я счупила
2. `billing.py` редове 84, 124: default `brand="МираСкоп"` → `brand="АстроКарта"`
3. `pdf_report.py` ред 201: default `brand="МираСкоп"` → `brand="АстроКарта"`
4. `templates/chart.html` ред 1806: име на свален PDF `MiraSkop-…pdf` → `AstroKarta-…pdf`
5. `templates/admin.html` ред 1470: placeholder `https://miraskop.bg` → `https://astrokarta.bg`

Умишлено НЕ се сменят (видими само в кода, не за потребителите):
- `miralog_token`, `miralog_theme` (всички шаблони), `miraskop_modules_seen` (static/modules.js:11)
- логер име `miraskop` (app.py:94)

## Част 3: База данни (data/persons.db, таблица settings)

Стойностите в базата бият кода — без тази стъпка живото приложение остава „МираСкоп“:

- `brand_name` → `АстроКарта`
- `brand_tagline` → `Астрология с точността на астрономията`
- `brand_domain` → `astrokarta.bg`
- `brand_logo` → празно (пада към новия вграден `/static/logo-header.png`)
- `brand_logo_full` → празно (пада към новия вграден `/static/logo-full.png`)
- `seo_site_url` → `https://astrokarta.bg` (канонични URL-и, sitemap, share линкове)
- `seo_title` вече съдържа `{brand}` — обновява се автоматично при четене; без промяна
- Останалите SEO полета (description, keywords) са неутрални — без промяна

## Част 4: Документация

- `README.md`: заглавие „МираСкоп (Miraskop)“ → „АстроКарта (AstroKarta)“; URL `https://miralog.blv.bg` → `https://astrokarta.bg`; клониращ линк и име на репо (github.com/soulkeeper131/…) — ако репото се преименува; env таблица (BRAND_NAME, BRAND_TAGLINE, BRAND_DOMAIN); docker build таг `miraskop` → `astrokarta`; секция „Смяна на името и логото“ да отразява новите стойности
- `docs/API.md`: базов URL → `https://astrokarta.bg`; примерни имейли `admin@miralog.bg` → `admin@astrokarta.bg`; „МираСкоп използва…“ → „АстроКарта използва…“
- `.env.example`: коментар на ред 1; `ADMIN_EMAIL=admin@miraskop.bg` → `admin@astrokarta.bg`; `DEMO_EMAIL=demo@miraskop.bg` → `demo@astrokarta.bg`
- `docs/PLAN-pricing.md` — да се провери за препратки към марката

## Част 5: Оперативен чеклист за домейна (извън кода, за потребителя)

Когато потребителят реши да насочи домейна:
1. DNS A запис за `astrokarta.bg` → IP на сървъра (+ `www`)
2. Coolify: добавяне на custom domain за приложението
3. HTTPS сертификат (Let's Encrypt през Coolify)
4. `seo_site_url` в Настройки → `https://astrokarta.bg` (вече зададено в базата)

## Тестване

1. Локално стартиране (`run.bat` / uvicorn) — landing, login, dashboard показват новото лого и име
2. Фавиконът в таба е новият
3. PDF отчетът се казва `AstroKarta-….pdf`
4. Админ панел → Настройки → Марка показва АстроКарта / astrokarta.bg / новите лога
5. Имейл шаблоните с `{brand}` се попълват с АстроКарта
6. Вписаните потребители остават вписани (ключовете не са сменени)
