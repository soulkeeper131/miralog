/* Браузърна проверка на пътя на клиента (телефон, 390 px):
   меню → гост-карта → презареждане → регистрация с модул (съгласие за
   14-дневния отказ) → карта → заключен модул → вход → карта от гост,
   запазена след вход в заварен акаунт.

   Пуска се срещу локално копие с тестови плащания:
     ENVIRONMENT=development MOCK_PAYMENTS=1 uvicorn app:app --port 8000
     node scripts/e2e_customer_path.js /tmp/e2e     (снимките: /tmp/e2e-*.png)
   Иска playwright (npm i playwright) и Chromium. Базата трябва да е празна
   (имейлът maria.e2e@example.com се регистрира). */
const { chromium } = require('playwright');
let LOG = null;
const BASE = 'http://localhost:8000', OUT = process.argv[2];
(async () => {
  const b = await chromium.launch({ args: ['--no-sandbox', '--no-proxy-server'] });
  const ctx = await b.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, locale: 'bg-BG' });
  const p = await ctx.newPage(); const log = []; LOG = log; let n = 0;
  const ok = (cond, msg) => log.push((cond ? '✅ ' : '❌ ') + msg);
  const shot = async (name) => p.screenshot({ path: `${OUT}-${String(++n).padStart(2, '0')}-${name}.png` });
  p.on('pageerror', e => log.push('❌ JS ГРЕШКА: ' + e.message));
  const dialog = async (label) => { const btn = p.locator('.ui-dialog-actions button', { hasText: label }); await btn.waitFor({ timeout: 8000 }); const txt = await p.locator('.ui-dialog').innerText().catch(() => ''); await btn.click(); return txt; };

  await p.goto(BASE + '/horoskop', { waitUntil: 'networkidle' });
  const nav = await p.$$eval('.site-nav a', as => as.map(a => a.innerText.trim()));
  ok(nav.includes('Вход'), 'общото меню на /horoskop с „Вход“: ' + nav.join(' · '));
  const acc = p.getByRole('button', { name: 'Приемам' }); if (await acc.isVisible().catch(() => false)) await acc.click();

  await p.goto(BASE + '/start', { waitUntil: 'networkidle' });
  await p.locator('.consent.consent-in .consent-ok').waitFor({ timeout: 8000 }).catch(() => {});
  await p.locator('.consent-ok').click({ timeout: 3000 }).catch(() => {});
  await p.waitForTimeout(400);
  ok(await p.evaluate(() => localStorage.getItem('miralog_consent')) === 'granted', 'бисквитки: приети');
  await p.fill('#fName', 'Мария Тестова'); await p.fill('#fDay', '17'); await p.fill('#fMonth', '5'); await p.fill('#fYear', '1990');
  await p.fill('#fHour', '14'); await p.fill('#fMinute', '30'); await p.fill('#fPlace', 'Пловдив'); await p.screenshot({ path: OUT + '-place.png' });
  await p.waitForSelector('.suggestion', { timeout: 15000 }); await p.locator('.suggestion').first().click();
  await p.click('#submitBtn'); await p.waitForSelector('#offerGrid .offer-check', { timeout: 30000, state: 'attached' });
  ok(true, 'гост-картата е изчислена'); await shot('guest-chart');

  await p.reload({ waitUntil: 'networkidle' });
  const restored = await p.waitForSelector('#offerGrid .offer-check', { timeout: 30000, state: 'attached' }).then(() => true).catch(() => false);
  ok(restored && (await p.inputValue('#fName')) === 'Мария Тестова', 'след презареждане картата се връща сама (данните не се губят)');

  await p.locator('#offerGrid label:has(input.offer-check[value="moon"])').click();   // както клиентът — по картата
  // Имейлът е винаги видим; паролата е по избор и стои сгъната под „искам да си сложа парола сега“.
  await p.fill('#fEmail', 'maria.e2e@example.com');
  if (!(await p.locator('#fPassword').isVisible())) await p.locator('details.pass-toggle summary').click();
  await p.fill('#fPassword', 'Parola-2026x');
  await p.locator('#termsAgree').check({ force: true });
  await p.locator('#saveBtn').click();
  const waiver = await dialog('Съгласен съм, продължи').catch(() => '');
  ok(/14-дневен отказ/.test(waiver), 'при избран модул се иска съгласие за 14-дневния отказ');
  const done = await dialog('Разбрах').catch(() => '');
  ok(/тестово плащане/i.test(done), 'тестовото плащане минава: ' + done.replace(/\s+/g, ' ').slice(0, 70));
  await p.waitForURL(/\/chart\//, { timeout: 20000 }); await p.waitForTimeout(1500); await shot('chart');
  ok(/\/chart\//.test(p.url()), 'след регистрацията — на картата');
  const stash = await p.evaluate(() => localStorage.getItem('miralog_guest_birth'));
  ok(!stash, 'запазените данни на госта се изчистват, щом картата е в профила');
  const back = await p.$eval('.back-link', a => a.getAttribute('href')).catch(() => null);
  ok(back === '/dashboard', '„← Моите карти“ води към таблото');
  const profileLocked = await p.$eval('#tabbtn-profile', t => t.dataset.feature).catch(() => null);
  ok(profileLocked === 'profile', 'табът „Астро портрет“ носи катинар/цена за платения профил');

  // Заключен модул → Отключи → съгласие → (тестово) отключване
  const picker = p.locator('#mod-root .mod-close, #mod-root [aria-label="Затвори"]').first();
  if (await p.locator('.onb-sheet').isVisible().catch(() => false)) { await p.locator('#onb-skip').click(); log.push('ℹ️ въвеждането в 3 стъпки се отвори при първото влизане — пропуснах го'); }
  else if (await picker.isVisible().catch(() => false)) { await picker.click(); log.push('ℹ️ изборът на модули се отвори сам при първото влизане — затворих го'); }
  else { await p.keyboard.press('Escape'); }
  await p.waitForTimeout(500);
  const card = p.locator('[data-section="akashic"], .section-card:has-text("Акашови")').first();
  if (await card.count()) await card.click(); else await p.evaluate(() => showTab('akashic'));
  const unlock = p.locator('.locked-btn').first();
  const hasLock = await unlock.waitFor({ timeout: 15000 }).then(() => true).catch(() => false);
  ok(hasLock, 'акашовите записи са заключени с бутон „Отключи“'); await shot('locked');
  if (hasLock) {
    await unlock.click();
    const w2 = await dialog('Съгласен съм, продължи').catch(() => 'тестовото плащане минава без диалог');
    log.push('ℹ️ „Отключи“: ' + w2.replace(/\s+/g, ' ').slice(0, 80));
  }
  const anon = await (await b.newContext({ viewport: { width: 390, height: 844 } })).newPage();
  await anon.goto(BASE + '/login', { waitUntil: 'networkidle' });
  await anon.screenshot({ path: OUT + '-login.png' });
  ok(await anon.$('a.home-link[href="/"]') !== null, 'входът има път към началото (логото е линк)');
  const reg = await anon.$eval('a[href="/start"]', a => a.innerText).catch(() => null);
  ok(!!reg, '„Нямаш акаунт?“ води към /start: ' + reg);
  // Гост → карта → вход в заварен акаунт → таблото предлага да запази картата.
  await anon.goto(BASE + '/start', { waitUntil: 'networkidle' });
  await anon.locator('.consent-ok').click({ timeout: 4000 }).catch(() => {});
  await anon.fill('#fName', 'Иван Гост'); await anon.fill('#fDay', '3'); await anon.fill('#fMonth', '11'); await anon.fill('#fYear', '1985');
  await anon.fill('#fPlace', 'Варна'); await anon.waitForSelector('.suggestion', { timeout: 15000 }); await anon.locator('.suggestion').first().click();
  await anon.click('#submitBtn'); await anon.waitForSelector('#offerGrid .offer-check', { timeout: 30000, state: 'attached' });
  const login = await anon.evaluate(async () => (await fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: 'maria.e2e@example.com', password: 'Parola-2026x' }) })).json());
  await anon.evaluate(t => { localStorage.setItem('miralog_token', t); document.cookie = 'miralog_token=' + t + '; path=/'; }, login.token || login.access_token);
  await anon.goto(BASE + '/dashboard', { waitUntil: 'networkidle' });
  const offer = await anon.locator('.ui-dialog').innerText({ timeout: 8000 }).catch(() => '');
  ok(/Иван Гост/.test(offer), 'таблото предлага да запази картата от преди малко: ' + offer.replace(/\s+/g, ' ').slice(0, 70));
  await anon.locator('.ui-dialog-actions button', { hasText: 'Запази' }).click().catch(() => {});
  await anon.waitForTimeout(2000);
  const people = await anon.locator('#personsList').innerText().catch(() => '');
  ok(/Иван Гост/.test(people), 'картата на госта е в профила');
  await anon.screenshot({ path: OUT + '-dashboard.png' });
  console.log(log.join('\n'));
  await b.close();
})().catch(e => { console.log((LOG || []).join('\n')); console.log('СРИВ: ' + e.message.split('\n').filter(l => /intercept|resolved|СРИВ|Timeout/.test(l)).slice(-3).join(' | ')); process.exit(1); });
