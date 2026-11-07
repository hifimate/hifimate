// The README screenshots (assets/screenshots) of the demo (tools/screenshots/demo.py), desktop and phone, as WebP.
//   node tools/screenshots/shoot.mjs [out-dir] [base-url]
// Needs playwright-core and a Chromium: PLAYWRIGHT_CORE (module path) and CHROMIUM (executable).
// Chromium itself scales and encodes the images (canvas → WebP), so nothing else is needed.
import { createRequire } from 'node:module';
import fs from 'node:fs';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_CORE || 'playwright-core');
const out = process.argv[2] || 'assets/screenshots';
const base = process.argv[3] || 'http://127.0.0.1:5099';
fs.mkdirSync(out, { recursive: true });

const DESKTOP = { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 };
const PHONE = { viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true };
const WIDTH = { desktop: 1600, phone: 780 };      // README sizes: sharp on 2x screens, small files

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined });
const errors = [];
// One login (the ctrl service rate-limits them); its cookie goes to every page that needs it.
const session = await browser.newContext();
const login = await session.request.post(base + '/ctrl/api/login', { data: { password: 'demo-pass' }, headers: { Origin: base } });
if (!login.ok()) throw Error('login failed: ' + login.status());
const loggedIn = await session.storageState();
await session.close();
const encoder = await (await browser.newContext()).newPage();

async function page(device, path, { login = false, wait = 1500 } = {}) {
  const context = await browser.newContext({ ...device, ...(login ? { storageState: loggedIn } : {}) });
  const p = await context.newPage();
  p.on('pageerror', e => errors.push(`${path}: ${e}`));
  await p.goto(base + path);
  await p.waitForTimeout(wait);
  return p;
}
async function shot(p, name, kind, options = {}) {
  const png = await p.screenshot(options);
  const webp = await encoder.evaluate(async ({ data, width }) => {
    const image = new Image();
    image.src = 'data:image/png;base64,' + data;
    await image.decode();
    const canvas = document.createElement('canvas');
    canvas.width = Math.min(width, image.width);
    canvas.height = Math.round(image.height * canvas.width / image.width);
    const ctx = canvas.getContext('2d');
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL('image/webp', 0.88).split(',')[1];
  }, { data: png.toString('base64'), width: WIDTH[kind] });
  fs.writeFileSync(`${out}/${name}.webp`, Buffer.from(webp, 'base64'));
  console.log('wrote', `${out}/${name}.webp`, Math.round(Buffer.from(webp, 'base64').length / 1024), 'KB');
}

let p = await page(DESKTOP, '/ctrl/', { wait: 6000 });
// Signal path, now playing with the spectrum, services; the event log below is left out.
await shot(p, 'ctrl-desktop', 'desktop', { fullPage: true, clip: { x: 0, y: 0, width: 1440, height: 1236 } });
await p.locator('#hifimate-spectrum-overlay button', { hasText: '全屏' }).first().click();
await p.waitForTimeout(3000);
await shot(p, 'spectrum-desktop', 'desktop');
await p.context().close();

p = await page(PHONE, '/ctrl/', { wait: 3000 });
await shot(p, 'ctrl-phone', 'phone');
await p.context().close();
p = await page(PHONE, '/ctrl/#spectrum', { wait: 5000 });
await shot(p, 'spectrum-phone', 'phone');
await p.context().close();

p = await page(DESKTOP, '/ctrl/eq/', { login: true, wait: 2500 });
await shot(p, 'eq-desktop', 'desktop');
await p.context().close();
p = await page(PHONE, '/ctrl/eq/', { login: true, wait: 2500 });
await shot(p, 'eq-phone', 'phone');
await p.context().close();

p = await page(PHONE, '/ctrl/roomfit/', { login: true, wait: 2500 });
await shot(p, 'roomfit-phone', 'phone');
await p.context().close();
p = await page(PHONE, '/ctrl/roomfit/#manage', { login: true, wait: 2500 });
await shot(p, 'roomfit-versions-phone', 'phone');
await p.context().close();

await browser.close();
if (errors.length) { console.error('page errors:', errors); process.exit(1); }
