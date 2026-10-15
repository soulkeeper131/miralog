/* Shared paywall behaviour: turns a 402 into a blurred teaser with a price.
   Depends on getToken() and authHeaders() being defined by the page. */

/* ---------- locked features ---------- */

// Thrown by guardedFetch when the server answers 402; carries the offer
// so the caller can render the teaser instead of an error.
class LockedFeature extends Error {
    constructor(detail) {
        super((detail && detail.message) || 'Функцията не е отключена.');
        this.detail = detail || {};
    }
}

// Every panel fetch goes through this, so a 402 always lands as a
// LockedFeature rather than a generic "HTTP 402" string.
async function guardedFetch(url, options) {
    const resp = await fetch(url, options);
    if (resp.status === 402) {
        const body = await resp.json().catch(() => ({}));
        const detail = body.detail;
        throw new LockedFeature(typeof detail === 'object' ? detail : { message: detail });
    }
    return resp;
}

// Платените разчитания се пишат във фона: адресът връща {pending:true},
// докато текстът не е готов. Питаме отново, докато не дойде — първата заявка
// може да носи refresh (само за админ), повторните не, за да не рестартират.
// Връща обект с .ok/.status/.json(), за да не се пипа кодът, който го ползва.
async function readingFetch(url, options) {
    const pollUrl = url.replace(/([?&])refresh=true&?/, '$1').replace(/[?&]$/, '');
    const deadline = Date.now() + 8 * 60 * 1000;
    let current = url;
    for (;;) {
        const resp = await guardedFetch(current, options);
        if (!resp.ok) return resp;
        const raw = await resp.text();
        let data;
        try { data = JSON.parse(raw); }
        catch (e) {
            // HTML от проксито при прекъсната връзка — разчитането продължава
            // на сървъра и ще е готово при следващо отваряне.
            throw new Error('Връзката прекъсна, но разчитането продължава. Отвори го пак след минута.');
        }
        if (!data.pending) {
            if (data.error) throw new Error(data.interpretation || 'Разчитането не се получи. Опитай пак след малко.');
            return { ok: true, status: 200, json: async () => data };
        }
        if (Date.now() > deadline) {
            throw new Error('Разчитането отнема повече от обичайното. Отвори го пак след няколко минути — ще те чака готово.');
        }
        current = pollUrl;
        await new Promise(r => setTimeout(r, 3000));
    }
}

function money(cents, currency) {
    const value = (Number(cents) || 0) / 100;
    const symbol = (currency || 'EUR') === 'EUR' ? '€' : currency;
    // Със запетая, както в цените на сървъра (price_label): „2,99 €“, „25 €“.
    return value.toFixed(2).replace('.00', '').replace('.', ',') + ' ' + symbol;
}

// Едно изречение за всеки модул под заглавието на заключения екран.
// Сървърното „… още не е отключено. Можеш да го отключиш …“ повтаряше
// заглавието и бутона, затова го заменяме с това, което модулът дава.
const LOCKED_BLURBS = {
    profile: 'Писмено разчитане по твоята карта: силни и слаби страни, темперамент и насоки за растеж.',
    period: 'Какво да очакваш до 60 дни напред и кога да планираш важните неща.',
    love: 'Има ли истинско привличане, кое ви свързва и имате ли дългосрочен потенциал.',
    numerology: 'Числата от името и датата ти и какво казват за пътя ти и за годината напред.',
    akashic: 'Какво носиш от миналото и накъде растеш — по лунните възли, Хирон и Сатурн.',
    moon: 'Фазите на Луната и какво е подходящо всеки ден.',
};

// Резервен текст, докато (или ако) истинското превю не се зареди — и за
// страниците без човек (лунният календар). Не се преструва на разчитане:
// отгоре е изрично казано, че е примерен, и не обещава конкретни числа.
function teaserText(featureName) {
    return `<div class="locked-teaser">
        <h4>${escapeHtml(featureName || 'Разчитане')}</h4>
        <p>Разчитането започва от позициите в твоята карта и ги превежда на
           ясен език — какво означават за теб, а не общи думи за зодията.</p>
        <h4>Какво ти помага</h4>
        <p>Тук се описват страните, на които можеш да разчиташ: къде идва
           увереността ти и кои умения ти се удават без усилие.</p>
        <h4>За какво да внимаваш</h4>
        <p>А тук — местата, които искат внимание: кое те дърпа назад и как да
           подходиш към него, вместо да го приемаш за случайност.</p>
    </div>`;
}

// Заменя резервния текст с истинския откъс, щом данните пристигнат. Откъсът
// е свързан с модула: числото на съдбата за нумерологията, Венера и Марс
// за любовта, лунните възли за акашовите записи — не едно и също навсякъде.
async function fillTeaser(el, featureName, featureKey) {
    const personId = (window.PERSON_ID !== undefined && window.PERSON_ID !== null)
        ? window.PERSON_ID : null;
    if (personId === null) return;
    try {
        const resp = await fetch('/api/persons/' + encodeURIComponent(personId) + '/teaser'
                                 + (featureKey ? '?feature=' + encodeURIComponent(featureKey) : ''),
                                 { headers: authHeaders() });
        if (!resp.ok) return;
        const data = await resp.json();
        const hasRows = data.rows && data.rows.length;
        const hasOutline = data.outline && data.outline.length;
        if (!hasRows && !hasOutline) return;

        const host = el.querySelector('.locked-teaser');
        if (!host) return;

        const rows = (data.rows || []).map(r => `
            <div class="teaser-row">
                <div class="teaser-row-head">
                    <span class="teaser-label">${escapeHtml(r.label)}</span>
                    <span class="teaser-pos">${escapeHtml(r.position)}</span>
                </div>
                <p>${escapeHtml(r.meaning)}</p>
            </div>`).join('');

        const outline = hasOutline
            ? `<ul class="teaser-outline">${data.outline.map(t => `<li>${escapeHtml(t)}</li>`).join('')}</ul>`
            : '';

        // Общото превю (без модул): стихията, която доминира.
        const element = !data.feature && data.element && data.element.name
            ? `<div class="teaser-row">
                   <div class="teaser-row-head">
                       <span class="teaser-label">Стихия</span>
                       <span class="teaser-pos">${escapeHtml(data.element.name)}</span>
                   </div>
                   <p>${escapeHtml(data.element.meaning)}</p>
               </div>`
            : '';

        // „…и още N аспекта“ е само в профила; другаде сървърът дава свой ред.
        const moreText = data.more
            || (!data.feature && data.aspect_count
                ? `…и още ${data.aspect_count} силни аспекта, разчетени в пълния текст.` : '');
        const more = moreText ? `<p class="teaser-more">${escapeHtml(moreText)}</p>` : '';

        const intro = data.intro
            || ('Ето какво показва твоята карта' + (data.name ? ', ' + data.name : '') + ':');
        host.innerHTML = `<h4>${escapeHtml(featureName || 'Разчитане')}</h4>
            <p class="teaser-intro">${escapeHtml(intro)}</p>
            ${rows}${outline}${element}${more}`;
        // Класът превключва оформлението: първите редове стават четими, а
        // наслагването с цената слиза под тях.
        host.classList.add('has-excerpt');
        el.classList.add('has-excerpt');
    } catch (e) { /* остава резервният текст */ }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text === null || text === undefined ? '' : String(text);
    return div.innerHTML;
}

// Renders the blurred teaser plus the price, into the panel that failed.
// За отчета „кои заключени модули се гледат“: повечето заключени табове не
// викат сървъра, така че той не ги вижда. Едно събитие на модул на страница.
const _lockedSeen = {};
function reportLockedView(feature) {
    if (!feature || _lockedSeen[feature]) return;
    _lockedSeen[feature] = true;
    try {
        fetch('/api/events', {
            method: 'POST', keepalive: true,
            headers: Object.assign({ 'Content-Type': 'application/json' },
                                   typeof authHeaders === 'function' ? authHeaders() : {}),
            body: JSON.stringify({ event: 'locked_view', feature: feature }),
        }).catch(function () {});
    } catch (e) { /* отчетът никога не бива да чупи страницата */ }
    if (typeof window.track === 'function') {
        window.track('view_item', { items: [{ item_id: feature, item_category: 'module' }] });
    }
}

function renderLocked(el, detail, retry) {
    const offer = detail.offer;
    const name = detail.feature_name || (offer && offer.name) || 'Това разчитане';
    const featureKey = detail.feature || (offer && offer.key) || '';
    reportLockedView(featureKey);
    // Общото „още не е отключено…“ от сървъра повтаря заглавието и бутона.
    const note = (!detail.message || /още не е отключено/.test(detail.message))
        ? (LOCKED_BLURBS[featureKey] || '') : detail.message;

    /* Пакетът се показва само когато наистина е по-изгоден от този модул
       плюс останалите поотделно — и то като втори, по-тих ред под основния
       бутон. Човекът е дошъл за едно конкретно разчитане; предлагаме му
       алтернатива, не му я натрапваме вместо това, което иска. */
    const b = detail.bundle;
    const bundleWorthIt = b && b.keys && b.keys.length >= 2
        && b.saving_cents > 0
        && offer && b.keys.indexOf(offer.key) !== -1;
    const bundleBlock = bundleWorthIt
        ? `<div class="locked-alt">
               <span>или ${b.credit_cents > 0 ? 'останалите' : 'всичките'} ${b.keys.length} за ${money(b.price_cents, b.currency)}${b.credit_cents > 0 ? ' (вече платеното е приспаднато)' : ''}</span>
               <button class="locked-alt-btn" data-bundle="1">Вземи всички</button>
           </div>`
        : '';

    const priceBlock = offer
        ? `<div class="locked-price">${money(offer.price_cents, offer.currency)}
               <small>еднократно, остава завинаги</small></div>
           <button class="locked-btn" data-feature="${escapeHtml(offer.key)}">Отключи „${escapeHtml(offer.name)}“</button>
           <p class="locked-perk">Платените разчитания са по-дълги и написани
              точно по твоята карта.</p>
           ${bundleBlock}`
        : `<a class="locked-btn" href="/settings"
               style="text-decoration:none;">Виж цените</a>`;

    el.classList.add('locked');
    el.innerHTML = teaserText(name) + `
        <div class="locked-overlay">
            <span class="teaser-sample">Примерен откъс</span>
            <span class="locked-badge">🔒 Заключено</span>
            <div class="locked-title">${escapeHtml(name)}</div>
            <div class="locked-note">${escapeHtml(note)}</div>
            ${priceBlock}
        </div>`;

    // By default a locked feature takes over its whole tab: leaving the other
    // sections behind would show empty headings for content the visitor cannot
    // have. Panels that are only partly paid (the astro portrait, whose data is
    // free and whose reading is not) pass keepSiblings and stay in place.
    if (!detail.keepSiblings) hideSiblingSections(el);

    fillTeaser(el, name, featureKey);

    const btn = el.querySelector('.locked-btn[data-feature]');
    if (btn) btn.addEventListener('click', () => requestUnlock(btn, btn.dataset.feature, retry));

    const alt = el.querySelector('.locked-alt-btn');
    if (alt) alt.addEventListener('click', () => requestUnlock(alt, 'bundle', retry));
}

// Collapse every section in the tab except the one holding the teaser, and
// strip the heading above it so the panel reads as a single locked page.
function hideSiblingSections(el) {
    // Re-locking the same panel must not stack up hidden nodes.
    restoreLockedSections(el);
    const panel = el.closest('.tab-content');
    if (!panel) return;
    const own = el.closest('.section');
    panel.querySelectorAll(':scope > .section').forEach(section => {
        if (section === own) return;
        section.dataset.lockedHidden = '1';
        section.hidden = true;
    });
    if (own) {
        own.querySelectorAll(':scope > h2, :scope > p').forEach(node => {
            node.dataset.lockedHidden = '1';
            node.hidden = true;
        });
    }
}

// Call this from the page when a panel renders real content: it clears the
// takeover so the tab's other sections come back without a reload.
function unlockPanel(el) {
    if (!el) return;
    el.classList.remove('locked');
    restoreLockedSections(el);
}

// Put back whatever hideSiblingSections took away, so a successful unlock
// restores the full tab without a reload.
function restoreLockedSections(panelOrEl) {
    const panel = panelOrEl && panelOrEl.closest
        ? (panelOrEl.closest('.tab-content') || panelOrEl)
        : document;
    panel.querySelectorAll('[data-locked-hidden="1"]').forEach(node => {
        node.hidden = false;
        delete node.dataset.lockedHidden;
    });
}

// Whether this build can complete a purchase without a processor.
let lockedMockPay = null;

async function lockedCanMockPay() {
    if (lockedMockPay !== null) return lockedMockPay;
    try {
        const r = await fetch('/api/public/config');
        lockedMockPay = r.ok ? !!(await r.json()).mock_payments : false;
    } catch (e) { lockedMockPay = false; }
    return lockedMockPay;
}

async function requestUnlock(btn, featureKey, retry) {
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = 'Момент…';
    try {
        // Test builds unlock straight away, so the paywall can be walked
        // through end to end before Stripe is wired up.
        if (await lockedCanMockPay()) {
            const r = await fetch('/api/dev/mock-pay', {
                method: 'POST',
                headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
                body: JSON.stringify({ keys: [featureKey] }),
            });
            if (r.ok) {
                // Картата отваря купения таб след презареждането (modules.js).
                if (typeof window.setUnlockIntent === 'function') window.setUnlockIntent([featureKey]);
                uiToast('Отключено (тестово плащане).', 'ok');
                setTimeout(() => location.reload(), 900);
                return;
            }
        }

        // ЗЗП чл. 57 т. 13 — същото съгласие като в избора на модули; без
        // него отказът от 14-дневното право в условията не важи.
        if (typeof uiConfirm === 'function') {
            const agreed = await uiConfirm(
                'Разчитането се генерира веднага след плащането. Съгласен ли си ' +
                'изпълнението да започне сега и потвърждаваш ли, че с това губиш ' +
                'правото си на 14-дневен отказ по чл. 57, т. 13 от ЗЗП?',
                { title: 'Съгласие за незабавно изпълнение',
                  ok: 'Съгласен съм, продължи', cancel: 'Назад' });
            if (!agreed) { btn.disabled = false; btn.textContent = original; return; }
        }

        // След плащане обратно на тази страница (картата), не в Настройки.
        const resp = await fetch('/api/features/' + encodeURIComponent(featureKey) + '/request'
                                 + '?return_to=' + encodeURIComponent(location.pathname), {
            method: 'POST', headers: authHeaders(),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            const d = data.detail;
            throw new Error(typeof d === 'string' ? d : (d && d.message) || ('Грешка ' + resp.status));
        }
        if (data.already && retry) { retry(); return; }
        if (data.checkout_url) {
            // Основното място за покупка не пращаше begin_checkout изобщо.
            if (typeof trackPurchase === 'function' && data.offer) {
                trackPurchase('begin_checkout', {
                    price_cents: data.offer.price_cents, currency: data.offer.currency,
                    name: data.offer.name,
                    items: [{ item_id: featureKey, item_name: data.offer.name, item_category: 'module',
                              price: data.offer.price_cents / 100, quantity: 1 }],
                });
            }
            window.location.href = data.checkout_url;
            return;
        }
        // Без онлайн плащане (Stripe не е настроен): заявката е стигнала до собственика.
        uiAlert('Онлайн плащането в момента не е включено. Записахме заявката ти — '
              + 'разчитането ще бъде отключено, щом плащането бъде уредено.', { title: 'Заявката е записана' });
        btn.textContent = 'Заявката е записана';
    } catch (e) {
        uiAlert(friendlyError(e),
                { title: 'Нещо се обърка', tone: 'danger' });
        btn.disabled = false;
        btn.textContent = original;
    }
}
