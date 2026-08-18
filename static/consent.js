/* Consent notice.

   The site stores three things in the browser: the login token, the chosen
   theme and the email used to sign in. All three are functional — without
   them you cannot stay signed in or keep your theme — and none of them track
   anybody across sites. There is no analytics, no advertising pixel and no
   third-party script on these pages.

   That is why this is a notice with an acknowledgement rather than a
   reject/accept pair: there is nothing optional to switch off. If tracking
   is ever added, this file must grow a real choice, and the tracking must
   stay dormant until that choice is given.
*/

(function () {
    var KEY = 'miralog_consent';
    var VERSION = '1';   // bump when the wording or what we store changes

    function alreadyAnswered() {
        try {
            return localStorage.getItem(KEY) === VERSION;
        } catch (e) {
            // Private mode with storage blocked: showing a storage notice we
            // cannot remember would nag on every page load, so stay quiet.
            return true;
        }
    }

    function remember() {
        try { localStorage.setItem(KEY, VERSION); } catch (e) { /* nothing to do */ }
    }

    function build() {
        var wrap = document.createElement('div');
        wrap.className = 'consent';
        wrap.setAttribute('role', 'dialog');
        wrap.setAttribute('aria-live', 'polite');
        wrap.setAttribute('aria-label', 'Съобщение за съхранявани данни');
        wrap.innerHTML =
            '<div class="consent-text">' +
                '<strong>Пазим само необходимото</strong> — вход и тема. ' +
                'Без реклами и без проследяване. ' +
                '<a href="/privacy">Подробности</a>' +
            '</div>' +
            '<button type="button" class="consent-ok">Разбрах</button>';
        return wrap;
    }

    function show() {
        if (alreadyAnswered()) return;
        var el = build();
        document.body.appendChild(el);
        // On a phone the notice is tall enough to cover real controls, so the
        // page makes room for it instead of floating over them. The class is
        // what the stylesheet keys that padding on.
        document.body.classList.add('consent-open');
        // Let the element land before animating, so the transition runs.
        requestAnimationFrame(function () { el.classList.add('consent-in'); });

        el.querySelector('.consent-ok').addEventListener('click', function () {
            remember();
            el.classList.remove('consent-in');
            document.body.classList.remove('consent-open');
            setTimeout(function () { el.remove(); }, 220);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', show);
    } else {
        show();
    }
})();
