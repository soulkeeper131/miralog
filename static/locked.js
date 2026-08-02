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

function money(cents, currency) {
    const value = (Number(cents) || 0) / 100;
    const symbol = (currency || 'EUR') === 'EUR' ? '€' : currency;
    return value.toFixed(2).replace('.00', '') + ' ' + symbol;
}

// Filler text sitting under the blur — long enough to look like a real
// reading, vague enough to give nothing away.
function teaserText(featureName) {
    const para = 'Разчитането е изготвено на база точните позиции в картата ти и обяснява какво означават те за теб — къде са силните ти страни, какво те дърпа назад и кои периоди работят във твоя полза.';
    return `<div class="locked-teaser">
        <h4>${escapeHtml(featureName || 'Разчитане')}</h4>
        <p>${para}</p>
        <h4>Какво ти помага</h4>
        <p>${para}</p>
        <h4>За какво да внимаваш</h4>
        <p>${para}</p>
    </div>`;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text === null || text === undefined ? '' : String(text);
    return div.innerHTML;
}

// Renders the blurred teaser plus the price, into the panel that failed.
function renderLocked(el, detail, retry) {
    const offer = detail.offer;
    const name = detail.feature_name || (offer && offer.name) || 'Тази функция';
    const priceBlock = offer
        ? `<div class="locked-price">${money(offer.price_cents, offer.currency)}
               <small>еднократно, остава завинаги</small></div>
           <button class="locked-btn" data-feature="${escapeHtml(offer.key)}">Отключи „${escapeHtml(offer.name)}“</button>`
        : `<a class="locked-btn" href="/settings?token=${encodeURIComponent(getToken())}"
               style="text-decoration:none;">Виж пакетите</a>`;

    el.classList.add('locked');
    el.innerHTML = teaserText(name) + `
        <div class="locked-overlay">
            <span class="locked-badge">🔒 Заключено</span>
            <div class="locked-title">${escapeHtml(name)}</div>
            <div class="locked-note">${escapeHtml(detail.message || '')}</div>
            ${priceBlock}
        </div>`;

    const btn = el.querySelector('.locked-btn[data-feature]');
    if (btn) btn.addEventListener('click', () => requestUnlock(btn, btn.dataset.feature, retry));
}

async function requestUnlock(btn, featureKey, retry) {
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = 'Изпращане…';
    try {
        const resp = await fetch('/api/features/' + encodeURIComponent(featureKey) + '/request', {
            method: 'POST', headers: authHeaders(),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || ('Грешка ' + resp.status));
        if (data.already && retry) { retry(); return; }
        alert('Заявката е изпратена. Ще се свържем с теб, за да уредим плащането — '
            + 'след това функцията се отключва за постоянно.');
        btn.textContent = 'Заявката е изпратена';
    } catch (e) {
        alert('Заявката не можа да се изпрати: ' + e.message);
        btn.disabled = false;
        btn.textContent = original;
    }
}
