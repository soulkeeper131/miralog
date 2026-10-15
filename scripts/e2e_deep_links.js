/* Браузърна проверка на дълбоките връзки до плащането (етап A+C от
   docs/PLAN-conversion.md): гост от лендинг → /start?want= → модулът е
   отметнат; вписан → /chart/<id>?unlock= отваря плащането; #таб отваря таба.

   Срещу локално копие с тестови плащания и празна база:
     MOCK_PAYMENTS=1 uvicorn app:app --port 8000
     node scripts/e2e_deep_links.js /tmp/e2e     (снимките: /tmp/e2e/*.png)
   Ползва демо акаунта (demo@astrokarta.bg / demo123). */
const { chromium } = require('playwright');
const OUT = process.argv[2], B = 'http://localhost:8000';
const log = []; const ok = (c, m) => log.push((c ? '✅ ' : '❌ ') + m);
(async () => {
  const b = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined, args: ['--no-sandbox', '--no-proxy-server'] });
  // 1) гост: лендинг → /start?want=akashic → гост-карта → акашовите отметнати
  let ctx = await b.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, locale: 'bg-BG' });
  await ctx.addInitScript(() => { try { localStorage.setItem('miralog_consent', 'granted'); } catch (e) {} });
  // sendBeacon при напускане на страницата не се вижда като заявка — записваме извикването.
  await ctx.addInitScript(() => {
    const orig = navigator.sendBeacon && navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = function (url, data) {
      try { data.text().then(t => sessionStorage.setItem('e2e_beacon', (sessionStorage.getItem('e2e_beacon') || '') + t)); } catch (e) {}
      return orig ? orig(url, data) : true;
    };
  });
  let p = await ctx.newPage(); p.on('pageerror', e => log.push('❌ JS: ' + e.message));
  await p.goto(B + '/akashovi-zapisi', { waitUntil: 'networkidle' });
  await p.locator('a[data-cta="feature_akashic:main"]').first().click();
  await p.waitForURL(/\/start\?want=akashic/, { timeout: 8000 }).catch(() => {});
  ok(p.url().includes('/start?want=akashic'), 'гост: главният бутон на /akashovi-zapisi → ' + p.url().replace(B, ''));
  ok(await p.evaluate(() => sessionStorage.getItem('e2e_beacon') || '').then(x => x.includes('cta_click')),
     'натискането е отчетено (cta_click)');
  await p.fill('#fName', 'Гост Тест'); await p.fill('#fDay', '3'); await p.fill('#fMonth', '7'); await p.fill('#fYear', '1991'); await p.fill('#fHour', '9'); await p.fill('#fMinute', '30');
  await p.fill('#fPlace', 'Варна'); await p.waitForSelector('.suggestion', { timeout: 15000 }); await p.locator('.suggestion').first().click();
  await p.click('#submitBtn'); await p.waitForSelector('#offerGrid .offer-check', { timeout: 30000, state: 'attached' });
  ok(await p.isChecked('.offer-check[value="akashic"]'), 'гост: след картата „Акашови записи“ е отметнат');
  await p.screenshot({ path: OUT + '/deep-guest-want.png' });
  await ctx.close();

  // 2) вписан (демо): лендинг → /chart/1?unlock=akashic → тестово плащане минава
  ctx = await b.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, locale: 'bg-BG' });
  await ctx.addInitScript(() => { try { localStorage.setItem('miralog_consent', 'granted'); } catch (e) {} });
  p = await ctx.newPage(); p.on('pageerror', e => log.push('❌ JS: ' + e.message));
  const tok = (await (await p.request.post(B + '/api/auth/login', { data: { email: process.env.DEMO_EMAIL || 'demo@astrokarta.bg', password: 'demo123' } })).json()).token;
  await ctx.addCookies([{ name: 'miralog_token', value: tok, url: B }]);
  await p.goto(B + '/login'); await p.evaluate(t => localStorage.setItem('miralog_token', t), tok);
  await p.goto(B + '/akashovi-zapisi', { waitUntil: 'networkidle' });
  const label = await p.locator('a[data-cta="feature_akashic:main"]').first().innerText();
  const href = await p.locator('a[data-cta="feature_akashic:main"]').first().getAttribute('href');
  ok(/\/chart\/\d+\?unlock=akashic/.test(href), `вписан: бутонът е „${label}“ → ${href}`);
  await p.goto(B + '/start', { waitUntil: 'networkidle' });
  ok(/\/chart\/\d+$/.test(p.url()), 'вписан: /start → ' + p.url().replace(B, ''));
  await p.goto(B + href, { waitUntil: 'networkidle' }); await p.waitForTimeout(4000);
  const feats = await p.evaluate(async () => (await (await fetch('/api/features', {
      headers: { Authorization: 'Bearer ' + localStorage.getItem('miralog_token') } })).json()));
  const owned = (feats.catalogue || []).find(x => x.key === 'akashic');
  ok(owned && owned.unlocked, 'вписан: ?unlock=akashic мина през плащането (тестово) и модулът е отключен');
  await p.screenshot({ path: OUT + '/deep-unlock.png' });
  ok(!p.url().includes('unlock='), 'адресът се изчиства от ?unlock=');
  await p.goto(B + '/dashboard', { waitUntil: 'networkidle' });
  await p.goto(B + href.split('?')[0] + '#horoscope', { waitUntil: 'networkidle' }); await p.waitForTimeout(800);
  ok((await p.locator('#tab-horoscope.active').count()) > 0, 'вписан: #horoscope отваря дневния хороскоп');
  await b.close();
  console.log(log.join('\n'));
})().catch(e => { console.log(log.join('\n')); console.log('СРИВ', e.message.slice(0, 300)); process.exit(1); });
