/* Въвеждане в три стъпки при първото отваряне на картата след запис.

   ① Това е твоята карта (Слънце, Луна, Асцендент)
   ② Личният ти хороскоп всеки ден (+ превключвател за имейл, изключен)
   ③ Какво още може да прочетеш (пакетът първи)

   Заменя прозореца „Какво още искаш да четеш?“, който изскачаше веднага.
   Показва се веднъж на акаунт: флагът е на сървъра (users.onboarded_at), за да
   не се връща на друго устройство. Нужни са ui.js (uiToast), modules.js
   (loadFeatures, unlockModule, openModulePicker) и страница с authHeaders().
*/
(function () {
    if (window.maybeStartOnboarding) return;

    function esc(text) {
        const d = document.createElement('div');
        d.textContent = text == null ? '' : String(text);
        return d.innerHTML;
    }
    function money(cents, currency) {
        const v = (Number(cents) || 0) / 100;
        const sym = (currency || 'EUR') === 'EUR' ? '€' : currency;
        return v.toFixed(2).replace('.00', '').replace('.', ',') + ' ' + sym;
    }

    // Измерване: feature е „1“, „2“, „3“, „skip“ или „done“. Не бива да чупи нищо.
    function ev(feature) {
        try {
            fetch('/api/events', {
                method: 'POST', keepalive: true,
                headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
                body: JSON.stringify({ event: 'onboarding_step', feature: String(feature) }),
            }).catch(function () {});
        } catch (e) { /* без значение */ }
    }

    function markDone() {
        try {
            return fetch('/api/account/onboarding', { method: 'POST', headers: authHeaders(), keepalive: true })
                .catch(function () {});
        } catch (e) { return Promise.resolve(); }
    }

    /* Дали акаунтът вече е минал въвеждането. Една заявка на зареждане —
       картата я ползва, за да реши кой таб да отвори (върналият се човек идва
       за хороскопа), и самото въвеждане. null при грешка. */
    let stateOnce = null;
    window.getOnboardingState = function () {
        if (!stateOnce) {
            stateOnce = fetch('/api/account/onboarding', { headers: authHeaders() })
                .then(function (r) { return r.ok ? r.json() : null; })
                .catch(function () { return null; });
        }
        return stateOnce;
    };

    // Дълбока връзка към плащане (?unlock=): човекът знае какво иска — не му
    // показваме въвеждането и после.
    window.markOnboardingDone = markDone;

    // Едно просто изречение за всяка точка: какво означава, без жаргон.
    const PLAIN = {
        'Слънце': 'Слънцето е същността ти — това, което си отвътре, и към какво се стремиш.',
        'Луна': 'Луната показва как усещаш нещата — емоциите ти и какво ти дава спокойствие.',
        'Асцендент': 'Асцендентът е първото впечатление — как те виждат другите и как влизаш в нова среда.',
    };

    async function loadCore() {
        try {
            const r = await fetch('/api/persons/' + encodeURIComponent(window.PERSON_ID) + '/teaser',
                                  { headers: authHeaders() });
            if (!r.ok) return [];
            const d = await r.json();
            return (d.rows || []).filter(function (x) { return PLAIN[x.label]; });
        } catch (e) { return []; }
    }

    window.startOnboarding = async function (opts) {
        opts = opts || {};
        const pending = opts.pending || null;
        const root = document.getElementById('onboarding-root') || (function () {
            const el = document.createElement('div');
            el.id = 'onboarding-root';
            document.body.appendChild(el);
            return el;
        })();

        const overlay = document.createElement('div');
        overlay.className = 'onb-overlay';
        overlay.innerHTML = '<div class="onb-sheet" role="dialog" aria-modal="true" aria-labelledby="onb-title"></div>';
        root.appendChild(overlay);
        const sheet = overlay.firstChild;
        requestAnimationFrame(function () { overlay.classList.add('onb-open'); });

        const previous = document.activeElement;
        let step = 1, finished = false;
        const core = await loadCore();
        let features = null;

        function close(reason) {
            if (finished) return;
            finished = true;
            ev(reason);
            overlay.classList.remove('onb-open');
            document.removeEventListener('keydown', onKey);
            setTimeout(function () {
                overlay.remove();
                if (previous && previous.focus) previous.focus();
            }, 180);
            // Пропуснал е, преди да види оферта: избраното при записа не се губи.
            if (reason === 'skip' && step < 3 && pending && typeof openModulePicker === 'function') {
                openModulePicker({ firstTime: true, preselect: pending });
            }
        }
        function onKey(e) { if (e.key === 'Escape') close('skip'); }
        document.addEventListener('keydown', onKey);

        function chrome(title, lede, body, last) {
            const dots = [1, 2, 3].map(function (n) {
                return '<span class="onb-dot' + (n <= step ? ' on' : '') + '"></span>';
            }).join('');
            sheet.innerHTML =
                '<div class="onb-dots" aria-hidden="true">' + dots +
                '<span class="onb-count">' + step + ' от 3</span></div>' +
                '<h3 class="onb-title" id="onb-title">' + title + '</h3>' +
                (lede ? '<p class="onb-lede">' + lede + '</p>' : '') +
                body +
                '<div class="onb-actions">' +
                '<button type="button" class="onb-next" id="onb-next">' + (last ? 'Продължи' : 'Напред') + '</button>' +
                '<button type="button" class="onb-skip" id="onb-skip">Пропусни</button></div>';
            sheet.querySelector('#onb-next').addEventListener('click', function () {
                if (last) close('done'); else { step += 1; render(); }
            });
            sheet.querySelector('#onb-skip').addEventListener('click', function () { close('skip'); });
            sheet.scrollTop = 0;
            sheet.querySelector('#onb-next').focus({ preventScroll: true });
        }

        function stepOne() {
            const rows = core.map(function (r) {
                return '<div class="onb-point"><div class="onb-point-head">' +
                    '<span class="onb-point-label">' + esc(r.label) + '</span>' +
                    '<span class="onb-point-pos">' + esc(r.position) + '</span></div>' +
                    '<p>' + esc(PLAIN[r.label]) + '</p></div>';
            }).join('');
            // Без час асцендентът не се смята — бекендът не го връща и редът отпада.
            chrome('Това е твоята карта',
                   rows ? 'Три неща, от които да започнеш:' : 'Наталната ти карта е готова и е безплатна.',
                   rows, false);
        }

        function stepTwo(state) {
            chrome('Личният ти хороскоп всеки ден',
                   'Всяка сутрин има нов хороскоп, съставен по твоята карта — безплатен е.',
                   '<a class="onb-link" id="onb-daily" href="#horoscope">Отвори дневния хороскоп</a>' +
                   '<label class="onb-switch"><input type="checkbox" id="onb-digest" role="switch"' +
                   (state && state.digest_opt_in ? ' checked' : '') + '>' +
                   '<span class="onb-track" aria-hidden="true"></span>' +
                   '<span>Изпращай ми го на имейл всяка сутрин</span></label>' +
                   '<p class="onb-hint">Изключено е, докато не го включиш. Можеш да го спреш по всяко време от Настройки.</p>',
                   false);
            sheet.querySelector('#onb-daily').addEventListener('click', function () {
                close('done');   // отива към таба; въвеждането е видяно
            });
            const box = sheet.querySelector('#onb-digest');
            box.addEventListener('change', async function () {
                const want = box.checked;
                try {
                    const r = await fetch('/api/account/digest', {
                        method: 'POST',
                        headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
                        body: JSON.stringify({ digest_opt_in: want }),
                    });
                    if (!r.ok) throw await apiError(r);
                    if (typeof uiToast === 'function') {
                        uiToast(want ? 'Ще ти пращаме хороскопа на имейл.' : 'Хороскопът на имейл е изключен.', 'ok');
                    }
                } catch (e) {
                    box.checked = !want;
                    if (typeof uiToast === 'function') uiToast('Не се запази. Опитай пак.', 'err');
                }
            });
        }

        /* Само пакетът и „Продължи“: списъкът с всички модули тук беше трета
           оферта на един екран — табовете с катинар са мястото за отделните. */
        function stepThree() {
            const data = features || { catalogue: [], bundle: null };
            const forSale = (data.catalogue || []).filter(function (i) {
                return !i.included && !i.unlocked && i.offer;
            });
            const wantKeys = (pending && pending.keys) || [];
            let html = '';
            if (pending && wantKeys.length) {
                const names = wantKeys.map(function (k) {
                    const m = forSale.find(function (i) { return i.key === k; });
                    return m ? m.name : k;
                });
                html += '<div class="onb-pending"><strong>Избра ги при регистрацията:</strong> ' + esc(names.join(', ')) +
                        '. Плащането тогава не тръгна — вземи ги с пакета или от менюто на картата.</div>';
            }
            const b = data.bundle;
            if (b) {
                html += '<div class="onb-mod onb-bundle' + (pending && pending.bundle ? ' onb-wanted' : '') + '">' +
                    '<div class="onb-mod-text"><div class="onb-mod-name">' + esc(b.name) + '</div>' +
                    '<div class="onb-mod-note">Всички ' + b.keys.length + ' разчитания с едно плащане. Поотделно: ' +
                    money(b.full_price_cents, b.currency) + '.</div></div>' +
                    '<span class="onb-mod-price">' + money(b.price_cents, b.currency) + '</span>' +
                    '<button type="button" class="onb-mod-btn" data-key="bundle" aria-label="Вземи пакета за ' +
                    esc(money(b.price_cents, b.currency)) + '">Вземи всички</button></div>';
            }
            // Без пакет (остава едно разчитане) — него, а не списък.
            html += (b ? [] : forSale.slice(0, 1)).map(function (i) {
                return '<div class="onb-mod' + (wantKeys.indexOf(i.key) >= 0 ? ' onb-wanted' : '') + '">' +
                    '<div class="onb-mod-text"><div class="onb-mod-name">' + esc(i.name) + '</div>' +
                    // Какво получава, накратко („Какво ще узнаеш?“ стоеше под всеки модул и не казваше нищо).
                    ((i.bullets || []).length ? '<div class="onb-mod-note">' + esc(i.bullets.slice(0, 3).join(' · ')) + '</div>' : '') + '</div>' +
                    '<span class="onb-mod-price">' + money(i.offer.price_cents, i.offer.currency) + '</span>' +
                    '<button type="button" class="onb-mod-btn" data-key="' + esc(i.key) + '" aria-label="Отключи „' +
                    esc(i.name) + '“ за ' + esc(money(i.offer.price_cents, i.offer.currency)) + '">Отключи</button></div>';
            }).join('');
            if (!html) html = '<p class="onb-lede">В момента няма какво да се добави.</p>';
            chrome('Какво още може да прочетеш',
                   'Плащаш веднъж и ти остава завинаги — не е абонамент. Разчитанията с катинар са в менюто на картата и можеш да ги вземеш и по-късно.',
                   html, true);
            sheet.querySelectorAll('.onb-mod-btn').forEach(function (btn) {
                btn.addEventListener('click', function () {
                    // Същият път като навсякъде: съгласие за 14-дневния отказ и плащане.
                    if (typeof unlockModule === 'function') unlockModule(btn.dataset.key);
                });
            });
        }

        let digestState = opts.state || null;
        async function render() {
            ev(step);
            if (step === 1) stepOne();
            else if (step === 2) stepTwo(digestState);
            else {
                if (!features && typeof loadFeatures === 'function') {
                    try { features = await loadFeatures(); } catch (e) { features = null; }
                }
                stepThree();
            }
        }
        render();
    };

    /* Решава дали да покаже въвеждането. Вика се от картата след зареждане.
       Дълбоките връзки (?unlock=, #таб) го заобикалят — там човекът е дошъл
       с конкретна цел. */
    window.maybeStartOnboarding = async function () {
        const q = new URLSearchParams(location.search);
        const state = await window.getOnboardingState();
        if (!state) return;   // неуспешна проверка не бива да изкарва прозорец

        let pending = null;
        try { pending = (await loadFeatures()).pending || null; } catch (e) { /* без избор */ }

        if (state.done) {
            // Избор от записа, чието плащане не е тръгнало, не се губи и после.
            if (pending && typeof openModulePicker === 'function') {
                openModulePicker({ firstTime: true, preselect: pending });
            }
            return;
        }
        // Връщане от плащане: купувачът познава продукта — не го водим отначало.
        if (q.get('paid') === '1') { markDone(); return; }
        if (q.get('paid') === '0') return;
        markDone();   // „веднъж на акаунт“ — и ако затвори раздела по средата
        window.startOnboarding({ pending: pending, state: state });
    };
})();
