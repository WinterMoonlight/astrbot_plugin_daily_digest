import {createRequire} from 'node:module';
import {access, readFile, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {build, root} from './build.mjs';

// Standalone development tool; not imported by AstrBot or the plugin.
const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PLAYWRIGHT_MODULE_ROOT ? path.join(process.env.PLAYWRIGHT_MODULE_ROOT, 'playwright') : 'playwright');
const source = path.resolve(process.argv[2] || path.join(root, 'samples/standard.json'));
const prefix = path.resolve(process.argv[3] || path.join(root, 'previews/standard'));
const {data, theme} = await build(source, prefix + '.html');
let executablePath = process.env.BROWSER_PATH;
if (!executablePath && process.platform === 'win32') {
  for (const candidate of ['C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe']) {
    try { await access(candidate); executablePath = candidate; break; } catch {}
  }
}
const browser = await chromium.launch({headless: true, ...(executablePath ? {executablePath} : {})});
try {
  const page = await browser.newPage({viewport: {width: theme.width, height: theme.viewportHeight}, deviceScaleFactor: 1});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route(/^https?:/, route => route.abort()); // Offline fonts/assets only.
  await page.goto(pathToFileURL(prefix + '.html').href);
  await page.waitForFunction(() => window.__RENDER_READY__ || window.__RENDER_ERROR__, null, {timeout: 15000});
  const report = await page.evaluate(() => {
    const rect = node => { const r = node.getBoundingClientRect(); return {x:r.x, y:r.y, width:r.width, height:r.height}; };
    return {
      error: window.__RENDER_ERROR__,
      pages: document.querySelectorAll('.report-page').length,
      canvas: rect(document.querySelector('.report-page')),
      panel: rect(document.querySelector('.content-panel')),
      footer: rect(document.querySelector('.report-footer')),
      closing: rect(document.querySelector('.character-layer')),
      portrait: rect(document.querySelector('.character-portrait')),
      closingText: rect(document.querySelector('.character-caption')),
      closingCopy: {title:document.querySelector('.closing-title').textContent, note:document.querySelector('.closing-note').textContent},
      scrollable: document.documentElement.scrollHeight > window.innerHeight,
      text: [...document.querySelectorAll('.card-body')].map(node => node.textContent).join(''),
      cards: [...document.querySelectorAll('.digest-card')].map(node => {
        const range = document.createRange(); range.selectNodeContents(node.querySelector('.card-body'));
        return {title:node.querySelector('h2').textContent, body:rect(node.querySelector('.card-body')), bodyPanel:rect(node.querySelector('.body-panel')), heading:rect(node.querySelector('.card-heading')), headingCopy:rect(node.querySelector('.card-heading-copy')), sticker:rect(node.querySelector('.chibi')), rect:rect(node), footer:rect(node.closest('.report-page').querySelector('.report-footer')),
          textRects:[...range.getClientRects()].map(r => ({x:r.x,y:r.y,width:r.width,height:r.height}))};
      }),
      fontFamily: getComputedStyle(document.querySelector('.card-body')).fontFamily,
      stickerImages: [...document.querySelectorAll('.chibi')].map(img => ({expression:img.dataset.expression, width:img.naturalWidth, height:img.naturalHeight, png:img.src.startsWith('data:image/png;')})),
      imagesOK: [...document.images].every(img => img.complete && img.naturalWidth > 0),
      horizontalOverflow: document.documentElement.scrollWidth > 1080,
    };
  });
  if (report.error || errors.length) throw new Error(report.error || errors.join('\n'));
  if (report.pages !== 1 || report.cards.length !== data.sections.length) throw new Error('Long-image mode must not split sections or pages');
  if (report.text !== data.sections.map(item => item.body).join('')) throw new Error('Rendered text changed or was lost');
  if (report.closingCopy.title !== data.closing.title || report.closingCopy.note !== data.closing.note) throw new Error('Closing pair changed');
  if (!report.imagesOK || report.horizontalOverflow) throw new Error('Image load or horizontal overflow failed');
  for (const item of report.cards) {
    if (item.bodyPanel.y < item.heading.y + item.heading.height + 14) throw new Error('Body panel overlaps heading');
    if (item.headingCopy.x + item.headingCopy.width > item.sticker.x) throw new Error('Heading overlaps sticker');
    if (item.body.x < item.bodyPanel.x || item.body.x + item.body.width > item.bodyPanel.x + item.bodyPanel.width || item.body.y + item.body.height > item.bodyPanel.y + item.bodyPanel.height) throw new Error('Text outside inner panel');
    if (item.rect.x < report.panel.x || item.rect.x + item.rect.width > report.panel.x + report.panel.width || item.rect.y < report.panel.y || item.rect.y + item.rect.height > report.panel.y + report.panel.height) throw new Error('Card outside outer panel');
    if (item.rect.y + item.rect.height > item.footer.y - 17) throw new Error('Card overlaps footer');
    for (const line of item.textRects) if (line.x < item.sticker.x + item.sticker.width && line.x + line.width > item.sticker.x && line.y < item.sticker.y + item.sticker.height && line.y + line.height > item.sticker.y) throw new Error('Text overlaps sticker');
  }
  if (report.footer.y < report.panel.y + report.panel.height + 17) throw new Error('Footer overlaps content panel');
  if (report.closing.y < report.panel.y + report.panel.height + 23 || report.footer.y < report.closing.y + report.closing.height + 17) throw new Error('Closing card overlaps adjacent sections');
  if (report.portrait.x + report.portrait.width > report.closingText.x) throw new Error('Closing portrait overlaps text');
  if (!report.stickerImages.every(img => img.png && img.width === 300 && img.height === 300)) throw new Error('Expected static first-frame PNGs');
  report.footerReachable = await page.evaluate(() => {
    window.scrollTo(0, document.documentElement.scrollHeight);
    const footer = document.querySelector('.report-footer').getBoundingClientRect();
    return footer.top >= 0 && footer.bottom <= window.innerHeight;
  });
  if (!report.footerReachable) throw new Error('Cannot scroll to the document footer');
  await page.evaluate(() => window.scrollTo(0, 0));
  for (let i = 0; i < report.pages; i++) await page.locator('.report-page').nth(i).screenshot({path: `${prefix}-${i+1}.png`});
  if (process.argv.includes('--detail')) await page.locator('.digest-card').first().screenshot({path: prefix + '-detail.png'});
  if (process.argv.includes('--detail')) await page.locator('.character-layer').screenshot({path: prefix + '-closing.png'});
  if (process.argv.includes('--detail')) await page.locator('.report-header').screenshot({path: prefix + '-header.png'});
  const png = await readFile(prefix + '-1.png');
  report.imageSize = {width:png.readUInt32BE(16), height:png.readUInt32BE(20)};
  if (report.imageSize.width !== theme.width || report.imageSize.height < Math.floor(report.canvas.height)) throw new Error('Long-image screenshot was cropped');
  await writeFile(prefix + '.checks.json', JSON.stringify({...report, errors}, null, 2));
  console.log(JSON.stringify({pages:report.pages, cards:report.cards.length, ...report.imageSize, scrollable:report.scrollable, textPreserved:true, imagesOK:report.imagesOK, output:prefix}));
} finally { await browser.close(); }
