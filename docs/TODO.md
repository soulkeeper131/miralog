# TODO — Проектни идеи (за обсъждане)

## ~~Меню след login (dashboard)~~ ✅ решено 2026-08-16
Долна лента с икони на телефон (bottom nav). Заглавието падна от 182px
на 64px. Десктопът е непроменен.

## ~~Цените на лендинга са твърдо написани~~ ✅ решено 2026-08-16
Таблицата се генерира от `feature_prices`. Смяна на цена от админ
панела се вижда веднага, без деплой. Редът на пакета се показва само
когато наистина спестява пари. Проверено на продукция: рекламираната
цена съвпада с таксуваната.

## ~~Stripe~~ ✅ включен 2026-08-23 (test mode)
`STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET` са зададени; webhook
endpoint (`/api/stripe/webhook`) създаден. Checkout работи, отключването
минава автоматично (fix: StripeObject → dict).
- **Отворено:** смяна на `sk_live_...` за реални плащания след тест.

## ~~Проследяване на потребителите + банер за съгласие~~ ✅ разработено 2026-08-23
GA4 (`G-CY4NT2QLFX`) + Advanced Consent Mode v2 (deny → grant) + събития
(sign_up, login, generate_*, begin_checkout, purchase, click_cta) + audit log.
`privacy.html` обновена. Админ таб „Активност“.

## Отворени точки
- **Server-side GA4 purchase (Measurement Protocol):** webhook-ът да праща
  `purchase` директно към GA4 (със сума/валута/transaction_id) — не зависи от
  consent/браузър. Нужен е Measurement Protocol API secret от GA4.
- **Имейл на админа при ново плащане:** „X плати 25 € за Y“ при
  `payment_succeeded`.
- **GSC:** домейнът е верифициран; да се следи индексирането и евентуално
  седмичен SEO отчет (GSC + GA4).
