/* Shared dialogs and toasts, so no screen falls back to the browser's own
   alert/confirm/prompt boxes — those ignore the site's theme entirely.

   Every function returns a promise, so callers read the same way the native
   ones did:  if (!await uiConfirm('…')) return;
*/

(function () {
    if (window.uiAlert) return;   // already loaded on this page

    function ensureRoot() {
        let root = document.getElementById('ui-dialog-root');
        if (!root) {
            root = document.createElement('div');
            root.id = 'ui-dialog-root';
            document.body.appendChild(root);
        }
        return root;
    }

    function esc(text) {
        const d = document.createElement('div');
        d.textContent = text == null ? '' : String(text);
        return d.innerHTML;
    }

    // Focus is moved into the dialog and returned to where it was on close, so
    // keyboard users are not dropped back at the top of the page.
    function openDialog({ title, message, tone, fields, buttons }) {
        return new Promise(resolve => {
            const root = ensureRoot();
            const previous = document.activeElement;

            const overlay = document.createElement('div');
            overlay.className = 'ui-overlay';
            overlay.innerHTML = `
                <div class="ui-dialog${tone ? ' ui-dialog-' + tone : ''}" role="dialog"
                     aria-modal="true" aria-labelledby="ui-dialog-title">
                    <div class="ui-dialog-head">
                        <span class="ui-dialog-mark" aria-hidden="true">${tone === 'danger' ? '⚠' : '✦'}</span>
                        <h3 class="ui-dialog-title" id="ui-dialog-title">${esc(title)}</h3>
                    </div>
                    ${message ? `<p class="ui-dialog-msg">${esc(message)}</p>` : ''}
                    ${(fields || []).map(f => `
                        <label class="ui-field">
                            <span class="ui-field-label">${esc(f.label)}</span>
                            <input class="ui-field-input" type="${f.type || 'text'}"
                                   name="${esc(f.name)}" value="${esc(f.value || '')}"
                                   placeholder="${esc(f.placeholder || '')}">
                        </label>`).join('')}
                    <div class="ui-dialog-actions">
                        ${buttons.map((b, i) => `
                            <button type="button" data-idx="${i}"
                                    class="ui-btn${b.primary ? ' ui-btn-primary' : ''}${b.danger ? ' ui-btn-danger' : ''}">
                                ${esc(b.label)}
                            </button>`).join('')}
                    </div>
                </div>`;
            root.appendChild(overlay);
            requestAnimationFrame(() => overlay.classList.add('ui-open'));

            const dialog = overlay.querySelector('.ui-dialog');
            const inputs = [...overlay.querySelectorAll('.ui-field-input')];

            function close(result) {
                overlay.classList.remove('ui-open');
                setTimeout(() => {
                    overlay.remove();
                    if (previous && previous.focus) previous.focus();
                }, 160);
                document.removeEventListener('keydown', onKey);
                resolve(result);
            }

            function values() {
                const out = {};
                inputs.forEach(i => { out[i.name] = i.value.trim(); });
                return out;
            }

            function onKey(e) {
                if (e.key === 'Escape') { e.preventDefault(); close(null); return; }
                if (e.key === 'Enter' && inputs.length) {
                    const primary = buttons.findIndex(b => b.primary);
                    if (primary >= 0) { e.preventDefault(); close({ index: primary, values: values() }); }
                    return;
                }
                if (e.key !== 'Tab') return;
                // Keep tabbing inside the dialog while it is open.
                const focusables = [...dialog.querySelectorAll('button, input')];
                if (!focusables.length) return;
                const first = focusables[0], last = focusables[focusables.length - 1];
                if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
                else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
            }

            overlay.addEventListener('click', e => { if (e.target === overlay) close(null); });
            overlay.querySelectorAll('.ui-dialog-actions button').forEach(btn => {
                btn.addEventListener('click', () => close({ index: +btn.dataset.idx, values: values() }));
            });
            document.addEventListener('keydown', onKey);

            (inputs[0] || overlay.querySelector('.ui-btn-primary') ||
             overlay.querySelector('.ui-dialog-actions button')).focus();
        });
    }

    window.uiAlert = function (message, opts) {
        opts = opts || {};
        return openDialog({
            title: opts.title || 'Съобщение',
            message,
            tone: opts.tone,
            buttons: [{ label: opts.ok || 'Разбрах', primary: true }],
        }).then(() => undefined);
    };

    window.uiConfirm = function (message, opts) {
        opts = opts || {};
        return openDialog({
            title: opts.title || 'Потвърждение',
            message,
            tone: opts.danger ? 'danger' : undefined,
            buttons: [
                { label: opts.cancel || 'Откажи' },
                { label: opts.ok || 'Продължи', primary: !opts.danger, danger: opts.danger },
            ],
        }).then(r => !!r && r.index === 1);
    };

    window.uiPrompt = function (message, opts) {
        opts = opts || {};
        return openDialog({
            title: opts.title || 'Въведи стойност',
            message,
            fields: [{ name: 'value', label: opts.label || '', type: opts.type || 'text',
                       value: opts.value || '', placeholder: opts.placeholder || '' }],
            buttons: [
                { label: opts.cancel || 'Откажи' },
                { label: opts.ok || 'Готово', primary: true },
            ],
        }).then(r => (r && r.index === 1) ? r.values.value : null);
    };

    // Short confirmations that do not need a decision from the reader.
    window.uiToast = function (message, tone) {
        let stack = document.getElementById('ui-toast-stack');
        if (!stack) {
            stack = document.createElement('div');
            stack.id = 'ui-toast-stack';
            document.body.appendChild(stack);
        }
        const el = document.createElement('div');
        el.className = 'ui-toast' + (tone ? ' ui-toast-' + tone : '');
        el.textContent = message;
        stack.appendChild(el);
        requestAnimationFrame(() => el.classList.add('ui-open'));
        setTimeout(() => {
            el.classList.remove('ui-open');
            setTimeout(() => el.remove(), 200);
        }, 3600);
    };
})();
