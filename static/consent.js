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
        if (typeof window.fbq === 'function' && window.FB_PIXEL_ID) {
            window.fbq('consent', 'grant');
            window.fbq('init', window.FB_PIXEL_ID);
            window.fbq('track', 'PageView');
        }
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
    var FB_EVENTS = {
        begin_checkout: 'InitiateCheckout',
        purchase: 'Purchase',
        sign_up: 'CompleteRegistration',
        login: 'Login',                    // custom — Meta няма стандартен еквивалент
        view_item: 'ViewContent',
        generate_natal_chart: 'Lead',      // безплатна натална карта = мек lead
        generate_synastry: 'ViewContent',  // преглед/генериране на синастрия
        generate_numerology: 'ViewContent',// преглед/генериране на нумерология
        click_cta: 'ClickCTA',             // custom
    };

    window.track = function (name, params) {
        if (current() !== 'granted') return;
        params = params || {};

        if (typeof window.gtag === 'function') {
            window.gtag('event', name, params);
        }

        if (typeof window.fbq === 'function') {
            var fbName = Object.prototype.hasOwnProperty.call(FB_EVENTS, name)
                ? FB_EVENTS[name] : name;
            if (fbName) {
                var payload = {};
                // Meta expects value/currency spelled its own way.
                if (params.value != null) payload.value = params.value;
                if (params.currency) payload.currency = params.currency;
                if (params.transaction_id) payload.order_id = params.transaction_id;
                if (params.item_name) payload.content_name = params.item_name;
                if (params.content_type) payload.content_type = params.content_type;
                if (params.content_name) payload.content_name = params.content_name;
                var standard = ['InitiateCheckout', 'Purchase',
                                'CompleteRegistration', 'ViewContent', 'Lead'];
                // Meta dedup: уникален ключ се предава като опция, не в payload.
                var opts = params.event_id ? { eventID: String(params.event_id) } : undefined;
                if (standard.indexOf(fbName) !== -1) {
                    window.fbq('track', fbName, payload, opts);
                } else {
                    window.fbq('trackCustom', fbName, payload);
                }
            }
        }
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
        if (info.name) params.item_name = info.name;
        if (info.keys) params.items = info.keys;
        // Meta dedup: транзакцията е най-стабилният уникален ключ.
        if (info.transaction_id) params.event_id = info.transaction_id;
        window.track(kind, params);
    };

    // Делегирано проследяване на кликове по CTA бутони/линкове.
    document.addEventListener('click', function (e) {
        var el = e.target.closest('a.btn, button.btn, a[data-track], button[data-track], .entry-btn');
        if (!el) return;
        var name = el.getAttribute('data-track')
            || (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 48);
        if (name) window.track('click_cta', { cta_name: name });
    }, true);

    function build() {
        var wrap = document.createElement('div');
        wrap.className = 'consent';
        wrap.setAttribute('role', 'dialog');
        wrap.setAttribute('aria-live', 'polite');
        wrap.setAttribute('aria-label', 'Съгласие за аналитика');
        wrap.innerHTML =
            '<div class="consent-text">' +
                '<strong>Използваме аналитика</strong>, за да разберем как се ползва ' +
                'сайтът и да го подобряваме. Ако откажеш, нищо не се събира. ' +
                '<a href="/privacy">Подробности</a>' +
            '</div>' +
            '<div class="consent-actions">' +
                '<button type="button" class="consent-no">Отказвам</button>' +
                '<button type="button" class="consent-ok">Приемам</button>' +
            '</div>';
        return wrap;
    }

    function show() {
        if (decided()) return;
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

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', show);
    } else {
        show();
    }
})();
