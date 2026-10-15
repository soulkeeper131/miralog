/* Споделяне към всички мрежи — един бутон, една функция.

   Винаги първо нашето меню. На телефон най-горе е „Сподели линк…“ —
   системното „Сподели“ само с линка: Viber, WhatsApp и Messenger сами
   показват картинката като преглед. Досега там се прикачваше и самата
   картинка, а тогава приложенията пращаха САМО нея — без линк към сайта.
   Картинката за Stories е отделен бутон (на телефон — също през системното
   меню, като файл). Компютър: мрежите, „Копирай линка“ и „Свали картинка“. Всеки линк носи utm_source=<мрежа>&utm_medium=share,
   за да се вижда в отчетите и GA4 откъде идват хората.

   openShareSheet({ url, title, text, image, feature })
     image — адресът на картата (/og/…); ?size=story|square се добавя тук. */
(function () {
    function withUtm(url, source) {
        try {
            var u = new URL(url, location.origin);
            u.searchParams.set('utm_source', source);
            u.searchParams.set('utm_medium', 'share');
            return u.toString();
        } catch (e) { return url; }
    }
    function sized(image, size) {
        if (!image) return '';
        var u = new URL(image, location.origin);
        u.searchParams.set('size', size);
        return u.toString();
    }
    function note(method, feature) {
        if (typeof window.track === 'function') window.track('share', { method: method, content_type: feature || 'page' });
        try {
            fetch('/api/events', { method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ event: 'share_click', feature: (feature || 'page') + ':' + method }) }).catch(function () {});
        } catch (e) { /* без значение */ }
    }
    function isPhone() { return /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent || ''); }

    var NETWORKS = [
        { id: 'facebook', label: 'Facebook', href: function (u) { return 'https://www.facebook.com/sharer/sharer.php?u=' + encodeURIComponent(u); } },
        { id: 'messenger', label: 'Messenger', phoneOnly: true, href: function (u) { return 'fb-messenger://share?link=' + encodeURIComponent(u); } },
        { id: 'whatsapp', label: 'WhatsApp', href: function (u, t) { return 'https://wa.me/?text=' + encodeURIComponent(t + ' ' + u); } },
        { id: 'viber', label: 'Viber', href: function (u, t) { return 'viber://forward?text=' + encodeURIComponent(t + ' ' + u); } },
        { id: 'telegram', label: 'Telegram', href: function (u, t) { return 'https://t.me/share/url?url=' + encodeURIComponent(u) + '&text=' + encodeURIComponent(t); } },
        { id: 'x', label: 'X', href: function (u, t) { return 'https://x.com/intent/post?url=' + encodeURIComponent(u) + '&text=' + encodeURIComponent(t); } },
    ];

    function injectStyle() {
        if (document.getElementById('share-sheet-style')) return;
        var st = document.createElement('style');
        st.id = 'share-sheet-style';
        st.textContent =
            '.share-ovl{position:fixed;inset:0;background:rgba(5,4,18,.6);display:flex;align-items:flex-end;justify-content:center;z-index:1000}' +
            '@media(min-width:600px){.share-ovl{align-items:center}}' +
            '.share-box{background:var(--bg-card,#14122e);color:var(--text,#e6e2f4);border:1px solid var(--border,#2b2752);border-radius:18px 18px 0 0;padding:20px;width:100%;max-width:460px}' +
            '@media(min-width:600px){.share-box{border-radius:18px}}' +
            '.share-box h3{margin:0 0 4px;font-size:1.05rem}.share-box p{margin:0 0 14px;font-size:.85rem;opacity:.75}' +
            '.share-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}' +
            '.share-grid a,.share-grid button,.share-row button{display:flex;align-items:center;justify-content:center;min-height:44px;border-radius:10px;border:1px solid var(--border,#2b2752);background:transparent;color:inherit;font:inherit;font-size:.82rem;text-decoration:none;cursor:pointer;padding:6px}' +
            '.share-row{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}' +
            '.share-row .share-copy{grid-column:1/-1}' +
            '.share-native{display:flex;align-items:center;justify-content:center;width:100%;min-height:50px;margin-bottom:12px;border-radius:12px;border:0;background:var(--accent,#7c3aed);color:#fff;font:inherit;font-weight:600;cursor:pointer}' +
            '.share-close{width:100%;min-height:42px;border:0;background:transparent;color:inherit;opacity:.7;cursor:pointer;font:inherit}';
        document.head.appendChild(st);
    }

    function sheet(o) {
        injectStyle();
        var ovl = document.createElement('div');
        ovl.className = 'share-ovl';
        var box = document.createElement('div');
        box.className = 'share-box';
        box.setAttribute('role', 'dialog');
        box.setAttribute('aria-label', 'Сподели');
        var phone = isPhone();
        var grid = NETWORKS.filter(function (n) { return phone || !n.phoneOnly; }).map(function (n) {
            var url = withUtm(o.url, n.id);
            return '<a href="' + n.href(url, o.title || '', sized(o.image, 'og')).replace(/"/g, '&quot;') +
                '" target="_blank" rel="noopener" data-net="' + n.id + '">' + n.label + '</a>';
        }).join('');
        var native = phone && navigator.share;
        box.innerHTML = '<h3>Сподели</h3><p>' + (o.title || '').replace(/</g, '&lt;') + '</p>' +
            (native ? '<button type="button" class="share-native">Сподели линк…</button>' : '') +
            '<div class="share-grid">' + grid + '</div>' +
            '<div class="share-row"><button type="button" class="share-copy">Копирай линка</button>' +
            (o.image ? '<button type="button" data-dl="story">Картинка за Stories</button>' +
                       '<button type="button" data-dl="square">Квадратна картинка</button>' : '') +
            '</div><button type="button" class="share-close">Затвори</button>';
        ovl.appendChild(box);
        document.body.appendChild(ovl);
        function close() { ovl.remove(); document.removeEventListener('keydown', onKey); }
        function onKey(e) { if (e.key === 'Escape') close(); }
        document.addEventListener('keydown', onKey);
        ovl.addEventListener('click', function (e) { if (e.target === ovl) close(); });
        box.querySelector('.share-close').addEventListener('click', close);
        var nb = box.querySelector('.share-native');
        if (nb) nb.addEventListener('click', async function () {
            try {
                // Само линкът: с прикачен файл приложенията пращат картинката без него.
                await navigator.share({ title: o.title || '', text: o.text || o.title || '',
                                        url: withUtm(o.url, 'native') });
                note('native', o.feature);
                close();
            } catch (e) { /* затворил е системното меню — нашето остава */ }
        });
        box.querySelectorAll('[data-net]').forEach(function (a) {
            a.addEventListener('click', function () { note(a.dataset.net, o.feature); });
        });
        box.querySelector('.share-copy').addEventListener('click', function () {
            var url = withUtm(o.url, 'copy');
            (navigator.clipboard ? navigator.clipboard.writeText(url) : Promise.reject())
                .then(function () {
                    var b = box.querySelector('.share-copy');
                    if (b) b.textContent = 'Копирано ✓';
                    if (typeof uiToast === 'function') uiToast('Линкът е копиран', 'ok');
                })
                .catch(function () { window.prompt('Копирай линка:', url); });
            note('copy', o.feature);
        });
        box.querySelectorAll('[data-dl]').forEach(function (btn) {
            btn.addEventListener('click', async function () {
                // Телефон: картинката като файл през системното меню (Instagram Stories);
                // ако браузърът не може — сваляне.
                if (native && navigator.canShare) {
                    try {
                        var blob = await fetch(sized(o.image, btn.dataset.dl)).then(function (r) { return r.ok ? r.blob() : null; });
                        var file = blob && new File([blob], 'astrokarta.jpg', { type: 'image/jpeg' });
                        if (file && navigator.canShare({ files: [file] })) {
                            await navigator.share({ files: [file] });
                            note('image_' + btn.dataset.dl, o.feature);
                            return;
                        }
                    } catch (e) { if (e && e.name === 'AbortError') return; }
                }
                var a = document.createElement('a');
                a.href = sized(o.image, btn.dataset.dl);
                a.download = 'astrokarta-' + btn.dataset.dl + '.jpg';
                document.body.appendChild(a); a.click(); a.remove();
                note('download_' + btn.dataset.dl, o.feature);
            });
        });
    }

    window.openShareSheet = function (o) {
        sheet(o || {});
    };

    // Бутони в страниците: <button class="share-btn" data-url data-title data-image data-feature>
    document.addEventListener('click', function (e) {
        var btn = e.target.closest && e.target.closest('.share-btn');
        if (!btn) return;
        e.preventDefault();
        window.openShareSheet({ url: btn.dataset.url || location.href, title: btn.dataset.title || document.title,
                                image: btn.dataset.image, feature: btn.dataset.feature });
    });
})();
