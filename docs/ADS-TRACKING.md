# Проследяване на рекламите (Google Ads и Facebook/Instagram)

Как приложението разбира от коя реклама е дошъл човек и как да се настроят кампаниите.

## Какво записва приложението

| Стъпка | Къде се вижда |
|---|---|
| Клик по реклама (UTM или `gclid`/`fbclid`) | Бисквитка `miralog_src` за 30 дни (първи и последен вход) + ред в `page_views` |
| Безплатна карта (`/start`) | `events.guest_chart` с кампанията в `meta.src`; Meta **Lead** от сървъра |
| Регистрация (с карта, имейл, Google, Facebook) | `users.acq_*`; Meta **CompleteRegistration** от сървъра |
| Започнато плащане | `checkout_sessions.acq_*`; UTM и в Stripe metadata |
| Плащане | GA4 **purchase** и Meta **Purchase** от сървъра, със същия номер като браузъра |

Отчетът е в **Админ → Отчети → „Кампании и източници“**: посещения → безплатни карти → регистрации → купувачи → приход, по източник / канал / кампания. В картата на всеки потребител (Админ → Потребители) пише откъде е дошъл.

Към Google и Meta нищо не тръгва без съгласие за бисквитките. Изключение е анонимният режим на Google Consent Mode, който няма идентификатор. Бисквитката `miralog_src` е само наша и в нея няма идентификатор.

## Събитията (по канона на Google — GA4 recommended events)

Всички тръгват от `track()` в `static/consent.js` и стигат до GA4, Umami и (стандартните) Meta. Страницата казва само ключа на модула, а името, цената, категорията и марката идват от каталога `AK_ITEMS`.

| Събитие | Кога | Meta |
|---|---|---|
| `view_item_list` | видян ценоразпис на началната (`landing_prices`), офертата под безплатната карта (`start_offers`), изборът на модули (`modules`) | — |
| `view_promotion` / `select_promotion` | видяна (≥50%) / натисната промоция: пакетът (`bundle`), офертите в хороскопите (`horo_offer_*`), лентата долу (`sticky_personal_horoscope`) | — |
| `select_item` | бутон за конкретен модул (`data-cta="място:ключ"`) | — |
| `select_content` | всеки друг бутон/призив | — |
| `view_item` | заключен модул на картата | ViewContent |
| `add_to_cart` / `remove_from_cart` | избор на модул преди регистрацията | AddToCart |
| `begin_checkout` | тръгва към Stripe | InitiateCheckout |
| `purchase` | платено (браузър + сървър, един transaction_id) | Purchase (+ сървър) |
| `refund` | върнати пари (от сървъра) | — |
| `generate_lead` | безплатна карта | Lead (+ сървър) |
| `sign_up` / `login` | регистрация / вход | CompleteRegistration (+ сървър) |
| `tutorial_begin` / `tutorial_complete` | въвеждането в 3 стъпки | — |
| `share` | споделяне (метод, какво) | — |

Вписаните получават в GA4 `user_id` и потребителско свойство `customer_type` = `registered` / `paying`. С него се правят аудитории „регистрирани без покупка“ и „купували“.

## Umami (analytics.blv.bg)

Umami е втори, независим отчет без бисквитки, който брои всички посетители. В него има посещения, източници, UTM, събитията (покупката с `revenue`) и скоростта при истинските посетители (LCP, INP, CLS; `data-performance`). Записът на сесии и топлинните карти (`recorder.js`) тръгват **само след съгласие**. Полетата не се записват, а личните текстове (картата, таблото, настройките, споделените разчитания, резултатът на безплатната карта) са с клас `rr-mask` и в записа са скрити. Настройките са в `SEO_DEFAULTS` (`umami_host`, `umami_website_id`, `umami_replay`).

## Аудитории за ремаркетинг (идеи)

- Безплатна карта (`generate_lead`) без `sign_up` за 7 дни: напомняне „картата те чака“.
- `view_item` или `add_to_cart` без `purchase` за 14 дни: офертата за пакета.
- `customer_type = paying`: изключи ги от рекламите за придобиване и им покажи другите модули.
- Посетили `/horoskop/*` 3+ пъти: личен хороскоп безплатно.

## Източници без UTM

Освен UTM, кликовете (`gclid`, `fbclid`) и обикновените сайтове/търсачки, приложението разпознава:
- уеб пощите (Gmail, ABV, Outlook…) → `email`;
- Android приложенията (`android-app://…`: Gmail, Google, Facebook, Instagram, Viber…);
- вградените браузъри на Facebook, Instagram, Messenger, TikTok, Viber, Telegram и Google app по User-Agent.

Нашите имейли носят `utm_source=email&utm_campaign=<вид писмо>`, а споделените линкове — `utm_medium=share&utm_source=<мрежа>&utm_campaign=share_<какво>`. „Директно“ остават само написаният адрес, отметките и линковете, пратени ръчно в чат без UTM.

## UTM — винаги малки букви, без интервали

| Параметър | Какво е | Пример |
|---|---|---|
| `utm_source` | мрежата | `google`, `facebook`, `instagram` |
| `utm_medium` | вид трафик | `cpc` (Google търсене), `paid_social` (FB/IG реклама) |
| `utm_campaign` | кампанията | `natalna-karta-oktomvri` |
| `utm_content` | конкретната реклама / визия | `video-1`, `karusel-zodii` |
| `utm_term` | ключова дума / аудитория | `{keyword}`, `zheni-25-44` |

### Google Ads

Включи **автоматичното маркиране (auto-tagging)**, за да носи всеки клик `gclid`. После в **Акаунт → Настройки → Суфикс на крайния URL адрес** сложи:

```
utm_source=google&utm_medium=cpc&utm_campaign={_campaign}&utm_content={creative}&utm_term={keyword}
```

`{_campaign}` е персонализиран параметър. Задай го във всяка кампания (Настройки на кампанията → Опции за URL адреса на кампанията → `_campaign` = `natalna-karta-oktomvri`). Ако не искаш да го правиш, пиши името направо: `utm_campaign=natalna-karta-oktomvri`.

### Facebook / Instagram (Meta Ads Manager)

На ниво реклама: **Проследяване → Параметри на URL адреса**:

```
utm_source={{site_source_name}}&utm_medium=paid_social&utm_campaign={{campaign.name}}&utm_content={{ad.name}}&utm_term={{adset.name}}
```

`{{site_source_name}}` дава `fb`, `ig`, `msg` или `an`, тоест показва дали кликът е от Facebook, Instagram или Messenger. Имената на кампаниите и рекламите в Ads Manager трябва да са на латиница и без интервали, защото те стават UTM.

## Настройки, които трябва да се направят веднъж

### Google
1. **GA4 → Администриране → Събития**: маркирай като **ключови събития** `purchase`, `sign_up` и `generate_lead`.
2. **GA4 → Администриране → Връзки с продукти → Google Ads**: свържи акаунта.
3. **Google Ads → Цели → Конверсии → Нова → Импортиране → Google Analytics 4**: импортирай трите.
4. По желание, за по-точно отчитане директно в Google Ads: създай конверсии в Google Ads и попълни в **Админ → Настройки → SEO** полетата `Google Ads ID` (AW-…) и етикетите за покупка, регистрация и безплатна карта. Тогава **не импортирай** същите събития от GA4, защото ще се броят двойно.
5. `GA4 Measurement Protocol API secret` в Админ → Настройки трябва да е попълнен. С него покупката стига до GA4 и при затворена страница.

### Meta
1. **Events Manager → пикселът → Настройки → Conversions API → Генерирай токен за достъп**. Сложи токена в **Админ → Настройки → SEO → Meta Conversions API токен**.
2. За проверка: Events Manager → **Test events**. Копирай кода (TEST…) в полето „Meta тестов код“, направи безплатна карта и регистрация на сайта и виж, че идват събития и от браузъра, и от сървъра (Deduplicated). **После изтрий тестовия код.**
3. **Events Manager → Настройки → Домейни**: потвърди `astrokarta.bg`.
4. В кампанията за оптимизация избери събитие **Purchase** (или **CompleteRegistration**/**Lead** в началото, докато покупките са малко).

## Преди старта на кампанията

- [ ] Отвори `https://astrokarta.bg/?utm_source=test&utm_medium=test&utm_campaign=proba` и направи безплатна карта и регистрация с тестов имейл.
- [ ] Админ → Потребители → новият акаунт → „Откъде е дошъл: test · test · proba“.
- [ ] Админ → Отчети → „Кампании и източници“ → ред `test / test / proba`.
- [ ] Изтрий тестовия акаунт.
