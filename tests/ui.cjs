/* Uses only the fictional demo data. Run: python demo/build_demo.py --no-icons; npm test */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const root = path.resolve(__dirname, '..');
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css' };
const server = http.createServer((req, res) => {
  const file = path.resolve(root, '.' + new URL(req.url, 'http://localhost').pathname);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404).end(); return;
  }
  res.setHeader('Content-Type', mime[path.extname(file)] || 'application/octet-stream');
  fs.createReadStream(file).pipe(res);
});
const results = [];
const check = (label, value) => { assert.ok(value, label); results.push(label); };

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_PATH ? { executablePath: process.env.BROWSER_PATH } : {}) });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
    const errors = [];
    page.on('pageerror', error => {
      // Browser integrations can inject code on external search destinations.
      // Keep this check scoped to our own application pages.
      if (page.url().startsWith(origin + '/')) errors.push(error.stack || error.message);
    });
    await page.goto(origin + '/demo/index.html');
    await page.waitForSelector('.card');
    check('Demo opens on 20 frequent bookmarks', await page.locator('.card').count() === 20);
    check('Autofocus keeps search history closed', await page.locator('#whist').isHidden());
    await page.locator('#tree .item.top').first().click();
    check('All 60 fictional bookmarks are present', await page.locator('.sec:not(.hot) .card').count() === 60);
    await page.locator('#q').fill('bilibili');
    check('Bookmark search finds the correct destination', await page.locator('.card').count() > 0 && await page.locator('.card').first().getAttribute('href').then(x => x.includes('bilibili')));
    await page.locator('#q').fill('nothing-matches-874e');
    check('Empty search has a readable state', await page.locator('.empty').isVisible());
    await page.keyboard.press('Escape');
    check('Escape resets bookmark search', await page.locator('#q').inputValue() === '');
    await page.locator('#tree .star').click();
    await page.locator('#wq').click();
    check('Search history opens on demand', await page.locator('#whist').isVisible());
    await page.keyboard.press('Escape');
    await page.locator('#engine').selectOption('baidu');
    await page.reload();
    check('Selected search engine persists', await page.locator('#engine').inputValue() === 'baidu');
    await page.locator('#engine').selectOption('bing');
    await page.locator('#wq').fill('apple design');
    await page.route('https://www.bing.com/**', route => route.fulfill({ body: '<title>Search intercepted by test</title>' }));
    await Promise.all([page.waitForURL('https://www.bing.com/search?q=apple%20design'), page.locator('#ws button').click()]);
    await page.goto(origin + '/demo/index.html');
    await page.locator('#wq').click();
    check('Submitting search records history', await page.locator('#whist .wh .n').first().textContent() === 'apple design');
    await page.keyboard.press('Escape');
    await page.locator('#wq').fill('github.com');
    await page.route('https://github.com/**', route => route.fulfill({ body: '<title>Destination intercepted by test</title>' }));
    await Promise.all([page.waitForURL('https://github.com/'), page.locator('#ws button').click()]);
    check('Direct address input navigates without search parameters', page.url() === 'https://github.com/');
    await page.goto(origin + '/demo/index.html');
    await page.locator('#wq').blur();
    await page.screenshot({ path: path.join(root, 'docs/landing-light.png'), animations: 'disabled' });
    await page.locator('#wq').click();
    await page.screenshot({ path: path.join(root, 'docs/search-history-light.png'), animations: 'disabled' });
    await page.keyboard.press('Escape');
    for (const width of [760, 390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      await page.reload();
      check(`${width}px: no horizontal overflow`, await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      check(`${width}px: hidden sidebar is excluded from keyboard focus`, await page.locator('#sidebar').evaluate(el => el.inert));
      await page.locator('.menu-toggle').click();
      await page.waitForFunction(() => document.activeElement.id === 'q');
      check(`${width}px: drawer opens and focuses search`, await page.locator('#q').evaluate(el => el === document.activeElement));
      await page.keyboard.press('Shift+Tab');
      check(`${width}px: keyboard focus stays in drawer`, await page.locator('#sidebar').evaluate(el => el.contains(document.activeElement)));
      await page.locator('#tree .item.top').first().click();
      check(`${width}px: selecting category closes drawer`, await page.locator('.menu-toggle').getAttribute('aria-expanded') === 'false');
      await page.keyboard.press('/');
      await page.waitForFunction(() => document.activeElement.id === 'q');
      check(`${width}px: slash shortcut opens bookmark search`, await page.locator('#q').evaluate(el => el === document.activeElement));
      await page.keyboard.press('Escape');
      check(`${width}px: Escape closes drawer`, await page.locator('.menu-toggle').getAttribute('aria-expanded') === 'false');
      if (width === 390) await page.screenshot({ path: path.join(root, 'docs/mobile.png'), animations: 'disabled' });
    }
    await page.emulateMedia({ reducedMotion: 'reduce' });
    check('Reduced-motion preference disables hover animation', await page.locator('.card').first().evaluate(el => getComputedStyle(el).transitionDuration === '0s'));
    assert.deepEqual(errors, [], 'No browser runtime errors');
    check('No browser runtime errors', errors.length === 0);
    console.log(results.map(x => 'PASS ' + x).join('\n'));
    console.log(`\n${results.length} checks passed.`);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
