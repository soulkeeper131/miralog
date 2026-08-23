# Интеграция: Google Analytics (GA4) + Google Search Console — AstroKarta

**Дата:** 2026-08-23
**Сайт:** https://astrokarta.bg
**Цел:** Проследяване на трафика (GA4) и SEO видимостта (GSC) за astrokarta.bg.
**GA4 измервателен ID:** `G-CY4NT2QLFX`

## Текущо състояние

- SEO настройките вече се държат в SQLite `settings` и се управляват от админ панела
  (`/api/admin/settings`): `seo_title`, `seo_description`, `seo_keywords`,
  `seo_robots`, `seo_verification`, `seo_og_image`, `seo_site_url`.
- `templates/landing.html` вече има `<meta name="google-site-verification">` (празен).
- **Няма GA4/gtag код никъде** — това е основната липса.
- `consent.js` е само уведомление „без проследяване“ — **трябва да стане реален
  избор** (accept/reject) и проследяването да е изключено докато не се приеме.
- Няма споделен `base.html` — всеки шаблон е самостоятелен. `brand` е Jinja
  global (`templates.env.globals["brand"]`), така че `analytics_id` може да се
  изложи по същия начин без да се пипа всеки route.
- `sitemap.xml` и `robots.txt` вече съществуват.
- Google Cloud service account вече съществува за SEO (blv-seo-bot) — ще се
  преизползва за astrokarta.bg (само добавяне като user към новите property).

## Събития за проследяване (GA4 events)

| Събитие | Кога | Параметри |
|---|---|---|
| `page_view` | автоматично от gtag config | път, заглавие |
| `sign_up` | успешна регистрация | method |
| `login` | вход | method |
| `generate_natal_chart` | генерирана натална карта | — |
| `generate_synastry` | генерирана синастрия | — |
| `generate_numerology` | генерирана нумерология | — |
| `begin_checkout` | клик към плащане | value, currency, items |
| `purchase` | успешно плащане (Stripe) | value, currency, transaction_id |
| `click_cta` | клик по CTA | cta_name |

Източник на трафик (`sessionSourceMedium`) и анонимен посетителски ID
(`client_id`) GA4 ги хваща сам — не се пипат ръчно.

## Съгласие (consent)

- `consent.js` става **accept/reject** избор (не само „Разбрах“).
- gtag/GA4 се зарежда **само след accept**; отказът оставя сайта без проследяване.
- Изборът се пази в `localStorage`; версията се bump-ва при промяна на текста/обхвата.
- `privacy.html` се обновява да отразява реалното проследяване.

## Фаза 1 — Кодова интеграция (Hermes, автономно)

1. **GA4 ID като настройка** — добавям `analytics_id` в `SEO_DEFAULTS`
   (default: `G-CY4NT2QLFX`), записва се през `/api/admin/settings` (вече
   приема всички ключове от `SEO_DEFAULTS`).
2. **Jinja global** — `templates.env.globals["analytics_id"]` (или `ga_id`),
   чете се от `seo_settings()` → достъпен във всеки шаблон без да пипам route-ове.
3. **gtag snippet** в `<head>` на всички публични шаблони чрез partial
   `templates/_analytics.html`, gate-нат от consent (зарежда се само при accept).
4. **Custom events** — `gtag('event', ...)` на: регистрация, вход, генериране на
   модул, checkout, purchase, CTA кликове.
5. **GSC верификация навсякъде** — `seo_verification` meta tag из всички шаблони.
6. **Полета в админ панела** — GA4 measurement ID + GSC verification token в
   секция „SEO“ на `admin.html`.
7. Deploy + verify.

## Фаза 2 — Google конзоли (Влади, ръчно — няма API за създаване на property)

1. **GA4:** ✅ property създадено → `G-CY4NT2QLFX`.
2. **GSC:** добавяне на astrokarta.bg като property → верификация (meta tag или DNS).
3. **Service account:** добавяне на `blv-seo-bot@blv-seo.iam.gserviceaccount.com`
   като user към двете property (Viewer в GA4, Restricted в GSC).

## Фаза 3 — Deploy (13:00 off-peak)

- `git push` → Coolify auto-deploy → `curl https://astrokarta.bg/healthz` = ok.
- Проверка: gtag snippet видим, GSC meta tag в `<head>`, consent работи.

## Блокери / нужно от Влади

- [ ] GSC верификация: meta tag или DNS (кой метод?) → да ми даде token/избор
- [ ] Потвърждение за deploy в 13:00

## Проверка на успеха

1. `curl -s https://astrokarta.bg/ | grep -o "G-[A-Z0-9]*"` → връща `G-CY4NT2QLFX`.
2. GA4 realtime показва page_view след отваряне + accept на consent.
3. Клик по CTA → `click_cta` се появява в GA4 realtime.
4. GSC показва astrokarta.bg като верифициран property.
