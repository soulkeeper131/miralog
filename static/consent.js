/* Consent — реален избор за аналитика (GA4).

   Преди беше уведомление „без проследяване“. Сега имаме Google Analytics,
   затова този файл дава истински избор: приемам / отказвам. Проследяването
   (gtag.js) се зарежда САМО след изрично „Приемам“ и остава изключено при
   отказ. Изборът се пази в localStorage.
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

    function loadGtag() {
        if (typeof window.gtag === 'function') return; // вече зареден
        var id = window.GA_ID;
        if (!id) return; // няма настроена аналитика
        window.dataLayer = window.dataLayer || [];
        window.gtag = function () { window.dataLayer.push(arguments); };
        window.gtag('js', new Date());
        window.gtag('config', id);
        window.gtag('event', 'page_view');
        var s = document.createElement('script');
        s.async = true;
        s.src = 'https://www.googletagmanager.com/gtag/js?id=' + id;
        document.head.appendChild(s);
    }

    // Хелпър за custom събития (регистрация, вход, модул, покупка, CTA).
    // Страниците викат window.track('sign_up') и т.н.; безопасен е преди
    // съгласие (gtag още не е функция) — събитието просто се пропуска.
    window.track = function (name, params) {
        if (typeof window.gtag === 'function') {
            window.gtag('event', name, params || {});
        }
    };

    // Делегирано проследяване на кликове по CTA бутони/линкове. Хваща
    // елементи с data-track атрибут или бутон-линкове; безопасно е преди
    // съгласие, защото window.track е no-op докато gtag не е зареден.
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
                '<strong>Използваме аналитика (Google Analytics)</strong>, за да разберем ' +
                'как се ползва сайтът и да го подобряваме. Данните са анонимни и обобщени. ' +
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
            if (value === 'granted') loadGtag();
            el.classList.remove('consent-in');
            document.body.classList.remove('consent-open');
            setTimeout(function () { el.remove(); }, 220);
        }
        el.querySelector('.consent-ok').addEventListener('click', function () { choose('granted'); });
        el.querySelector('.consent-no').addEventListener('click', function () { choose('denied'); });
    }

    // Вече дадено съгласие от предишна визита → зареди gtag веднага.
    if (current() === 'granted') {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', loadGtag);
        } else {
            loadGtag();
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', show);
    } else {
        show();
    }
})();
