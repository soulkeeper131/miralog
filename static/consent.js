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
    }

    // Хелпър за custom събития (регистрация, вход, модул, покупка, CTA).
    // Изпраща само при дадено съгласие.
    window.track = function (name, params) {
        if (typeof window.gtag === 'function' && current() === 'granted') {
            window.gtag('event', name, params || {});
        }
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
            if (value === 'granted') grant();
            el.classList.remove('consent-in');
            document.body.classList.remove('consent-open');
            setTimeout(function () { el.remove(); }, 220);
        }
        el.querySelector('.consent-ok').addEventListener('click', function () { choose('granted'); });
        el.querySelector('.consent-no').addEventListener('click', function () { choose('denied'); });
    }

    // Вече дадено съгласие от предишна визита → активирай аналитиката сега.
    if (current() === 'granted') grant();

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', show);
    } else {
        show();
    }
})();
