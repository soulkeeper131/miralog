/* The module picker: a card per add-on, with what it gives and what it costs.

   Shown once after a first chart is created, and reachable any time from the
   chart page. Needs ui.js (for uiAlert/uiToast) and a page that defines
   getToken() and authHeaders().
*/

(function () {
    if (window.openModulePicker) return;

    const SEEN_KEY = 'miraskop_modules_seen';

    /* One trip for /api/features per page load. Three separate callers used to
       ask for the same list on the way in — the tab padlocks, the price lookup
       and the first-visit picker. They share this promise instead; a purchase
       calls invalidate() so the next read sees the new state. */
    let featuresPromise = null;
    window.loadFeatures = function (force) {
        if (force) featuresPromise = null;
        if (!featuresPromise) {
            featuresPromise = fetch('/api/features', { headers: authHeaders() })
                .then(resp => {
                    if (!resp.ok) throw new Error('Грешка ' + resp.status);
                    return resp.json();
                })
                .catch(err => { featuresPromise = null; throw err; });
        }
        return featuresPromise;
    };
    window.invalidateFeatures = function () { featuresPromise = null; };

    function esc(text) {
        const d = document.createElement('div');
        d.textContent = text == null ? '' : String(text);
        return d.innerHTML;
    }

    function money(cents, currency) {
        const value = (Number(cents) || 0) / 100;
        const symbol = (currency || 'EUR') === 'EUR' ? '€' : currency;
        return value.toFixed(2).replace('.00', '') + ' ' + symbol;
    }

    function cardHtml(item) {
        const offer = item.offer;
        const bullets = (item.bullets || []).map(b =>
            `<li>${esc(b)}</li>`).join('');
        const price = item.unlocked
            ? '<span class="mod-owned">Отключено</span>'
            : (offer
                ? `<span class="mod-price">${money(offer.price_cents, offer.currency)}</span>
                   <span class="mod-once">еднократно</span>`
                : '');
        // The visible label stays short so six cards line up, but every button
        // reads "Отключи" — a screen reader would announce six identical
        // buttons. aria-label carries the module and its price instead.
        const aria = offer
            ? `Отключи „${item.name}“ за ${money(offer.price_cents, offer.currency)}`
            : `Отключи „${item.name}“`;
        const action = item.unlocked
            ? ''
            : `<button class="mod-btn" data-key="${esc(item.key)}"
                       aria-label="${esc(aria)}">Отключи</button>`;

        return `<article class="mod-card${item.unlocked ? ' mod-card-owned' : ''}">
            <div class="mod-head">
                <span class="mod-glyph" aria-hidden="true">${esc(item.glyph || '✦')}</span>
                <div>
                    <h4 class="mod-name">${esc(item.name)}</h4>
                    ${item.note ? `<p class="mod-note">${esc(item.note)}</p>` : ''}
                </div>
            </div>
            <ul class="mod-list">${bullets}</ul>
            <div class="mod-foot">
                <div class="mod-pricing">${price}</div>
                ${action}
            </div>
        </article>`;
    }

    // Whether this build can complete a purchase without a processor.
    let mockPayments = null;

    async function canMockPay() {
        if (mockPayments !== null) return mockPayments;
        try {
            const r = await fetch('/api/public/config');
            mockPayments = r.ok ? !!(await r.json()).mock_payments : false;
        } catch (e) { mockPayments = false; }
        return mockPayments;
    }

    async function unlock(btn, key) {
        const original = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Момент…';
        try {
            // Test builds hand the module over immediately, so the whole
            // purchase path can be walked before Stripe is wired up.
            if (await canMockPay()) {
                const r = await fetch('/api/dev/mock-pay', {
                    method: 'POST',
                    headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
                    body: JSON.stringify({ keys: [key] }),
                });
                if (r.ok) {
                    btn.textContent = 'Отключено';
                    btn.disabled = true;
                    uiToast('Отключено (тестово плащане).', 'ok');
                    setTimeout(() => location.reload(), 1200);
                    return;
                }
            }

            // The bundle has its own endpoint: it works out what is still
            // missing rather than taking a single key.
            const url = key === 'bundle'
                ? '/api/features/bundle/request'
                : '/api/features/' + encodeURIComponent(key) + '/request';
            const resp = await fetch(url, { method: 'POST', headers: authHeaders() });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                const detail = data.detail;
                throw new Error(typeof detail === 'string' ? detail
                    : (detail && detail.message) || ('Грешка ' + resp.status));
            }
            if (data.checkout_url) { window.location.href = data.checkout_url; return; }
            if (data.already) { btn.textContent = 'Отключено'; return; }

            // No payment processor configured — the request was logged instead.
            btn.textContent = 'Заявено';
            uiToast('Ще се свържем с теб, за да уредим плащането.', 'ok');
        } catch (e) {
            uiAlert(e.message, { title: 'Нещо се обърка', tone: 'danger' });
            btn.disabled = false;
            btn.textContent = original;
        }
    }

    /* opts.firstTime changes the wording for someone who has just arrived. */
    window.openModulePicker = async function (opts) {
        opts = opts || {};

        const root = document.getElementById('mod-root') || (() => {
            const el = document.createElement('div');
            el.id = 'mod-root';
            document.body.appendChild(el);
            return el;
        })();

        const overlay = document.createElement('div');
        overlay.className = 'mod-overlay';
        overlay.innerHTML = `
            <div class="mod-sheet" role="dialog" aria-modal="true" aria-labelledby="mod-title">
                <button class="mod-close" aria-label="Затвори">✕</button>
                <div class="mod-intro">
                    <span class="mod-eyebrow">${opts.firstTime ? 'Картата ти е готова' : 'Добави към картата си'}</span>
                    <h3 id="mod-title">Какво още искаш да четеш?</h3>
                    <p>${opts.firstTime
                        ? 'Наталната ти карта, астро портретът, планетите и аспектите вече са отключени. Всичко по-долу се добавя поотделно — плащаш веднъж и ти остава завинаги.'
                        : 'Всяко от тези се отключва отделно. Плащаш веднъж и ти остава завинаги.'}</p>
                </div>
                <div class="mod-grid" id="mod-grid">
                    <div class="mod-loading">Зареждане…</div>
                </div>
                <div class="mod-foot-note">
                    Можеш да отключиш и по-късно — намираш ги под всяко заключено меню.
                </div>
            </div>`;
        root.appendChild(overlay);
        requestAnimationFrame(() => overlay.classList.add('mod-open'));

        const previous = document.activeElement;
        function close() {
            overlay.classList.remove('mod-open');
            setTimeout(() => {
                overlay.remove();
                if (previous && previous.focus) previous.focus();
            }, 180);
            document.removeEventListener('keydown', onKey);
        }
        function onKey(e) { if (e.key === 'Escape') close(); }
        document.addEventListener('keydown', onKey);
        overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
        overlay.querySelector('.mod-close').addEventListener('click', close);

        try {
            const data = await loadFeatures();

            // Everything a bought chart already carries is not for sale here.
            const sellable = (data.catalogue || []).filter(item =>
                !item.included && (item.offer || item.unlocked));

            const grid = overlay.querySelector('#mod-grid');
            grid.innerHTML = sellable.length
                ? sellable.map(cardHtml).join('')
                : '<div class="mod-loading">Няма допълнителни модули в момента.</div>';

            // The bundle sits above the grid: it is a different kind of choice
            // from "pick this one module", and burying it among the cards
            // would make it read as a seventh module.
            if (data.bundle) {
                const b = data.bundle;
                const banner = document.createElement('div');
                banner.className = 'mod-bundle';
                banner.innerHTML = `
                    <div class="mod-bundle-text">
                        <h4>${esc(b.name)}</h4>
                        <p>Всички останали ${b.keys.length} модула наведнъж.
                           Поотделно излизат ${money(b.full_price_cents, b.currency)}.</p>
                    </div>
                    <div class="mod-bundle-buy">
                        <div class="mod-bundle-price">${money(b.price_cents, b.currency)}
                            <span>спестяваш ${money(b.saving_cents, b.currency)}</span></div>
                        <button class="mod-btn mod-btn-bundle" data-bundle="1"
                                aria-label="Отключи всички модули за ${esc(money(b.price_cents, b.currency))}">Вземи всички</button>
                    </div>`;
                grid.parentNode.insertBefore(banner, grid);
                banner.querySelector('[data-bundle]')
                      .addEventListener('click', ev => unlock(ev.currentTarget, 'bundle'));
            }

            grid.querySelectorAll('.mod-btn').forEach(btn => {
                btn.addEventListener('click', () => unlock(btn, btn.dataset.key));
            });
        } catch (e) {
            overlay.querySelector('#mod-grid').innerHTML =
                '<div class="mod-loading">Модулите не се заредиха. Опитай пак по-късно.</div>';
        }

        localStorage.setItem(SEEN_KEY, '1');
    };

    /* Show the picker once, the first time somebody lands with a chart —
       but only if there is actually something left to buy. Somebody who
       already owns every module has nothing to choose from. */
    window.maybeShowModulePicker = async function () {
        if (localStorage.getItem(SEEN_KEY)) return;
        try {
            const data = await loadFeatures();
            const forSale = (data.catalogue || []).filter(item =>
                !item.included && !item.unlocked && item.offer);
            if (!forSale.length) {
                // Nothing to offer: remember that, so this check runs once.
                localStorage.setItem(SEEN_KEY, '1');
                return;
            }
        } catch (e) {
            return;   // a failed check must never pop an empty sheet
        }
        openModulePicker({ firstTime: true });
    };
})();
