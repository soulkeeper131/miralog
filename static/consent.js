/* Consent — реален избор за аналитика (GA4) с Google Consent Mode.

   gtag.js се зарежда статично в <head> (чрез _analytics.html) с
   'consent default denied'. Този файл дава избора и при „Приемам“
   изпраща 'consent update granted' — дотогава Google не съхранява данни.
   Изборът се пази в localStorage.
*/

(function () {
    var KEY = 'miralog_consent';

    function current() {
        try { return localStorage.getItem(KEY); } catch (e) { return null; }
    }

    function remember(value) {
        try { localStorage.setItem(KEY, value); } catch (e) { /* private mode */ }
    }

    function decided() {
        var v = current();
        return v === 'granted' || v === 'denied';
    }

    function grant() {
        if (typeof window.gtag === 'function') {
            window.gtag('consent', 'update', {
                'ad_storage': 'granted',
                'ad_user_data': 'granted',
                'ad_personalization': 'granted',
                'analytics_storage': 'granted'
            });
        }
        // Meta stays revoked until here. init() is what sets _fbp, so it
        // must not run before the visitor agrees; PageView fires once, right
        // after, so an accepting visitor is still counted.
        loadRecorder();
        if (typeof window.fbq === 'function' && window.FB_PIXEL_ID) {
            if (!document.getElementById('fb-pixel-js')) {
                var fb = document.createElement('script');
                fb.id = 'fb-pixel-js';
                fb.async = true;
                fb.src = 'https://connect.facebook.net/en_US/fbevents.js';
                document.head.appendChild(fb);
            }
            window.fbq('consent', 'grant');
            window.fbq('init', window.FB_PIXEL_ID);
            window.fbq('track', 'PageView');
        }
    }

    /* Записът на сесии и топлинните карти на Umami (rrweb) — само след
       съгласие. Полетата се скриват от самия запис, а личните текстове
       (карти, разчитания) са с клас rr-mask и в записа са заменени със звездички. */
    function loadRecorder() {
        var u = window.UMAMI;
        if (!u || !u.replay || document.getElementById('umami-recorder')) return;
        var t = document.createElement('script');
        t.id = 'umami-recorder';
        t.defer = true;
        t.src = u.host + '/recorder.js';
        t.setAttribute('data-website-id', u.id);
        document.head.appendChild(t);
    }

    /* Refusing must also clear what is already there.

       A visitor who says no should not be left carrying a tracking cookie
       from a previous visit or from a tag that fired too early. These are the
       cookies GA4 and Meta set; removing them needs the same path and domain
       they were written with, so both the bare host and the dot-prefixed
       parent are tried. */
    function revoke() {
        if (typeof window.gtag === 'function') {
            window.gtag('consent', 'update', {
                'ad_storage': 'denied',
                'ad_user_data': 'denied',
                'ad_personalization': 'denied',
                'analytics_storage': 'denied'
            });
        }
        if (typeof window.fbq === 'function') window.fbq('consent', 'revoke');

        var host = location.hostname;
        var domains = ['', host, '.' + host];
        var parts = host.split('.');
        if (parts.length > 2) domains.push('.' + parts.slice(-2).join('.'));

        document.cookie.split(';').forEach(function (raw) {
            var name = raw.split('=')[0].trim();
            if (!/^(_ga|_gid|_gat|_fbp|_fbc)/.test(name)) return;
            domains.forEach(function (d) {
                document.cookie = name + '=; Max-Age=0; path=/'
                    + (d ? '; domain=' + d : '');
            });
        });
    }

    /* One call, both tags.

       GA4 and Meta name the same moments differently, so the mapping lives
       here rather than at every call site — otherwise each new button has to
       remember two vocabularies and one of them eventually gets forgotten.
       Nothing is sent without consent. */
    /* Събитията по канона на Google (GA4 recommended events) — един речник
       за всички страници. Страницата казва само „кой модул“ (item_id), а
       името, цената, категорията и марката идват от каталога AK_ITEMS, който
       сървърът слага във всяка страница. */
    var ECOMMERCE = ['view_item_list', 'select_item', 'view_item', 'add_to_cart',
                     'remove_from_cart', 'view_cart', 'begin_checkout', 'add_payment_info',
                     'purchase', 'refund', 'view_promotion', 'select_promotion'];
    // При тези value е стойността на кошницата/поръчката; при разглеждане не.
    var WITH_VALUE = ['view_item', 'add_to_cart', 'remove_from_cart', 'view_cart',
                      'begin_checkout', 'add_payment_info', 'purchase', 'refund'];

    function catalogue() { return window.AK_ITEMS || {}; }

    function enrichItems(items) {
        var cat = catalogue();
        return (items || []).map(function (it, i) {
            if (typeof it === 'string') it = { item_id: it };
            var c = cat[it.item_id] || {}, out = {};
            Object.keys(it).forEach(function (k) { if (it[k] != null && it[k] !== '') out[k] = it[k]; });
            if (!out.item_name || out.item_name === out.item_id) out.item_name = c.item_name || out.item_id;
            if (out.price == null && c.price != null) out.price = c.price;
            if (!out.item_category && c.item_category) out.item_category = c.item_category;
            if (!out.item_brand && window.AK_BRAND) out.item_brand = window.AK_BRAND;
            if (out.quantity == null) out.quantity = 1;
            if (out.index == null) out.index = i;
            return out;
        });
    }

    function normalize(name, params) {
        var p = {};
        Object.keys(params || {}).forEach(function (k) { p[k] = params[k]; });
        if (ECOMMERCE.indexOf(name) === -1) return p;
        if (name === 'view_item_list' && !p.items) {
            p.items = Object.keys(catalogue()).map(function (k) { return { item_id: k }; });
        }
        if (p.items) p.items = enrichItems(p.items);
        if (p.items && p.items.length) {
            if (p.value == null && WITH_VALUE.indexOf(name) !== -1) {
                var sum = 0;
                p.items.forEach(function (it) { sum += (Number(it.price) || 0) * (Number(it.quantity) || 1); });
                p.value = Math.round(sum * 100) / 100;
            }
            p.currency = p.currency || 'EUR';
        }
        return p;
    }

    /* Meta: само стандартните събития, по които рекламите се оптимизират и
       от които се правят аудитории за ремаркетинг. Останалото е само в GA4/Umami. */
    var FB_EVENTS = {
        view_item: 'ViewContent',
        add_to_cart: 'AddToCart',
        begin_checkout: 'InitiateCheckout',
        purchase: 'Purchase',
        sign_up: 'CompleteRegistration',
        generate_lead: 'Lead',             // безплатната карта на /start
        contact: 'Contact',                // контактната форма
        generate_synastry: 'ViewContent',
        generate_numerology: 'ViewContent',
    };

    function sendMeta(name, p) {
        if (current() !== 'granted' || typeof window.fbq !== 'function') return;
        var fbName = FB_EVENTS[name];
        if (!fbName) return;
        var payload = {};
        if (p.value != null) payload.value = p.value;
        if (p.currency) payload.currency = p.currency;
        if (p.transaction_id) payload.order_id = p.transaction_id;
        if (p.items && p.items.length) {
            payload.content_ids = p.items.map(function (it) { return it.item_id; });
            payload.contents = p.items.map(function (it) {
                return { id: it.item_id, quantity: it.quantity || 1, item_price: it.price };
            });
            payload.content_type = 'product';
            payload.num_items = p.items.length;
            payload.content_name = p.items.map(function (it) { return it.item_name; }).join(', ');
        }
        if (p.item_name) payload.content_name = p.item_name;
        if (p.content_name) payload.content_name = p.content_name;
        // Meta dedup със сървъра: уникалният ключ е опция, не част от payload.
        var opts = p.event_id ? { eventID: String(p.event_id) } : undefined;
        window.fbq('track', fbName, payload, opts);
    }

    /* Umami: без бисквитки, за всички. Събитията с пари носят revenue и
       currency — така Umami смята прихода по източник и кампания. */
    var umamiQueue = [];
    function sendUmami(name, p) {
        var data = {};
        Object.keys(p).forEach(function (k) {
            var v = p[k];
            if (k === 'event_id' || v == null) return;
            if (k === 'items') {
                data.item_ids = v.map(function (it) { return it.item_id; }).join(',');
                data.item_names = v.map(function (it) { return it.item_name; }).join(', ').slice(0, 400);
                return;
            }
            if (typeof v === 'object') return;
            data[k] = typeof v === 'string' ? v.slice(0, 400) : v;
        });
        if (name === 'purchase' && p.value != null) { data.revenue = p.value; data.currency = p.currency || 'EUR'; }
        if (window.umami && typeof window.umami.track === 'function') window.umami.track(name, data);
        else if (window.UMAMI) umamiQueue.push([name, data]);
    }
    (function flushUmami(tries) {
        if (window.umami && typeof window.umami.track === 'function') {
            while (umamiQueue.length) { var e = umamiQueue.shift(); window.umami.track(e[0], e[1]); }
            return;
        }
        if (tries < 40) setTimeout(function () { flushUmami(tries + 1); }, 250);
    })(0);

    window.track = function (name, params) {
        var p = normalize(name, params || {});

        // GA4 получава събитието винаги: Consent Mode само решава дали е с
        // бисквитки или анонимен пинг.
        if (typeof window.gtag === 'function') {
            // event_id е за Meta (да не се брои два пъти със сървъра) — GA4 не му трябва.
            var gaParams = {};
            Object.keys(p).forEach(function (k) { if (k !== 'event_id') gaParams[k] = p[k]; });
            window.gtag('event', name, gaParams);
            // Google Ads конверсия за същото събитие, ако е зададен етикет.
            var ads = window.GOOGLE_ADS;
            if (ads && ads.labels && ads.labels[name]) {
                var conv = { send_to: ads.id + '/' + ads.labels[name] };
                if (p.value != null) conv.value = p.value;
                if (p.currency) conv.currency = p.currency;
                if (p.transaction_id) conv.transaction_id = p.transaction_id;
                window.gtag('event', 'conversion', conv);
            }
        }
        sendMeta(name, p);
        sendUmami(name, p);
    };

    /* Purchases and checkouts carry money, so they get their own helper —
       a bare track('purchase') with no value produces a report that counts
       sales but cannot total them. */
    window.trackPurchase = function (kind, info) {
        info = info || {};
        var params = {};
        if (info.price_cents != null) params.value = +(info.price_cents / 100).toFixed(2);
        params.currency = info.currency || 'EUR';
        if (info.transaction_id) params.transaction_id = info.transaction_id;
        if (info.coupon) params.coupon = info.coupon;
        var items = info.items || (info.keys || []).map(function (k) { return { item_id: k }; });
        if (items.length) params.items = items;
        if (info.name) params.item_name = info.name;   // за Meta (content_name)
        // Meta dedup: транзакцията е най-стабилният уникален ключ.
        if (info.transaction_id) params.event_id = info.transaction_id;
        window.track(kind, params);
    };

    /* Бутоните. data-cta="място:ключ" — ако ключът е модул или пакетът, това
       е select_item от списъка „място“ (кой бутон продава какво); иначе
       select_content. Останалите бутони — select_content с текста им. */
    document.addEventListener('click', function (e) {
        var cta = e.target.closest && e.target.closest('[data-cta]');
        if (cta) {
            var where = (cta.getAttribute('data-cta') || '').slice(0, 60);
            var parts = where.split(':'), key = parts[parts.length - 1];
            if (parts.length > 1 && catalogue()[key]) {
                window.track('select_item', { item_list_id: parts[0], item_list_name: parts[0],
                                              items: [{ item_id: key }] });
            } else {
                window.track('select_content', { content_type: 'cta', content_id: where });
            }
            return;
        }
        var el = e.target.closest && e.target.closest('a.btn, button.btn, a[data-track], button[data-track], .entry-btn');
        if (!el || el.closest('[data-promo-id]')) return;
        var name = el.getAttribute('data-track')
            || (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 48);
        if (name) window.track('select_content', { content_type: 'button', content_id: name });
    }, true);

    /* Промоциите (пакетът, офертите в хороскопите, лентата долу): видяна
       (≥50% на екрана, веднъж) → view_promotion; натисната → select_promotion.
       Списъци с модули (data-item-list) — view_item_list, когато се видят. */
    function promoParams(el) {
        var keys = (el.getAttribute('data-promo-items') || '').split(',').filter(Boolean);
        var p = { promotion_id: el.getAttribute('data-promo-id'),
                  promotion_name: el.getAttribute('data-promo-name') || el.getAttribute('data-promo-id'),
                  creative_name: el.getAttribute('data-promo-creative') || undefined,
                  creative_slot: el.getAttribute('data-promo-slot') || location.pathname };
        if (keys.length) p.items = keys.map(function (k) { return { item_id: k }; });
        return p;
    }
    function watchSeen() {
        var nodes = document.querySelectorAll('[data-promo-id]:not([data-seen]), [data-item-list]:not([data-seen])');
        if (!nodes.length || !('IntersectionObserver' in window)) return;
        var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (en) {
                if (!en.isIntersecting || en.target.hasAttribute('data-seen')) return;
                en.target.setAttribute('data-seen', '1');
                io.unobserve(en.target);
                var t = en.target;
                if (t.hasAttribute('data-promo-id')) {
                    window.track('view_promotion', promoParams(t));
                } else {
                    var keys = (t.getAttribute('data-items') || '').split(',').filter(Boolean);
                    window.track('view_item_list', { item_list_id: t.getAttribute('data-item-list'),
                        item_list_name: t.getAttribute('data-item-list'),
                        items: keys.length ? keys.map(function (k) { return { item_id: k }; }) : undefined });
                }
            });
        }, { threshold: 0.5 });
        nodes.forEach(function (n) { io.observe(n); });
    }
    document.addEventListener('click', function (e) {
        var promo = e.target.closest && e.target.closest('[data-promo-id]');
        if (promo && e.target.closest('a, button')) window.track('select_promotion', promoParams(promo));
    }, true);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', watchSeen);
    else watchSeen();
    window.watchTrackedBlocks = watchSeen;   // за блокове, добавени по-късно

    function build() {
        var wrap = document.createElement('div');
        wrap.className = 'consent';
        wrap.setAttribute('role', 'dialog');
        wrap.setAttribute('aria-live', 'polite');
        wrap.setAttribute('aria-label', 'Съгласие за аналитика');
        wrap.innerHTML =
            '<div class="consent-text">' +
                // На телефон .consent-more е скрито: банерът е тънка лента, а
                // цялото обяснение е на „Подробности“.
                '<strong>Бисквитки за аналитика и реклама.</strong>' +
                '<span class="consent-more"> С тях разбираме как се ' +
                'ползва сайтът и откъде идват посетителите.</span> Ако откажеш, не записваме ' +
                'бисквитки<span class="consent-more"> — Google получава само анонимни сигнали ' +
                'без идентификатор</span>. ' +
                '<a href="/privacy">Подробности</a>' +
            '</div>' +
            '<div class="consent-actions">' +
                '<button type="button" class="consent-no">Отказвам</button>' +
                '<button type="button" class="consent-ok">Приемам</button>' +
            '</div>';
        return wrap;
    }

    // Политиката обещава, че изборът може да се промени — бутонът там вика това.
    window.reopenConsent = function () { show(true); };

    function show(force) {
        if (decided() && !force) return;
        if (document.querySelector('.consent')) return;
        var el = build();
        document.body.appendChild(el);
        document.body.classList.add('consent-open');
        requestAnimationFrame(function () { el.classList.add('consent-in'); });

        function choose(value) {
            remember(value);
            if (value === 'granted') grant();
            else revoke();
            el.classList.remove('consent-in');
            document.body.classList.remove('consent-open');
            setTimeout(function () { el.remove(); }, 220);
        }
        el.querySelector('.consent-ok').addEventListener('click', function () { choose('granted'); });
        el.querySelector('.consent-no').addEventListener('click', function () { choose('denied'); });
    }

    // Пази избора между визитите: съгласие активира, отказ изчиства.
    if (current() === 'granted') grant();
    else if (current() === 'denied') revoke();

    // show(false) изрично: като обработчик show получава събитието, а то
    // е „истина“ — банерът излизаше при всяко зареждане, и след „Приемам“.
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { show(false); });
    } else {
        show(false);
    }
})();

/* Кой бутон води хора: всеки елемент с data-cta="място:бутон" праща
   cta_click към /api/events (нашите отчети, без бисквитки — не иска
   съгласие). GA4/Meta/Umami получават select_item/select_content от track(). */
(function () {
    document.addEventListener('click', function (e) {
        var el = e.target.closest && e.target.closest('[data-cta]');
        if (!el) return;
        var where = (el.getAttribute('data-cta') || '').slice(0, 40);
        try {
            var body = JSON.stringify({ event: 'cta_click', feature: where });
            if (navigator.sendBeacon) {
                navigator.sendBeacon('/api/events', new Blob([body], { type: 'application/json' }));
            } else {
                fetch('/api/events', { method: 'POST', keepalive: true,
                    headers: { 'Content-Type': 'application/json' }, body: body }).catch(function () {});
            }
        } catch (err) { /* без значение */ }
    }, true);
})();
