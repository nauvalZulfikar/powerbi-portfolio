// Visual review of the HTML twin (reports/dashboard.html) with Playwright.
//   node tools/review_twin.mjs <phase> [page-id ...]
// Checks every page: table overflow (parent scrollWidth > clientWidth), clipped text in KPI values,
// panel titles/subtitles and table cells, and empty panels. Screenshots the listed pages (default: all)
// to evidence/preview-<phase>-<page>.png; the first listed page is also saved as evidence/preview-<phase>.png.
// Exit code 1 when any check fails.
import { createRequire } from 'module';
import { pathToFileURL } from 'url';
import path from 'path';
import fs from 'fs';

const require = createRequire(import.meta.url);
const { chromium } = require('/Users/shaka-mac-mini/.npm/_npx/705bc6b22212b352/node_modules/playwright');
const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const [phase = 'X', ...shots] = process.argv.slice(2);
const url = pathToFileURL(path.join(root, 'reports', 'dashboard.html')).href;
fs.mkdirSync(path.join(root, 'evidence'), { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
await page.goto(url);
const ids = await page.evaluate(() => PAGES.map(p => p.id));
let failures = 0;
for (const id of ids) {
  await page.evaluate(i => render(i), id);
  await page.waitForTimeout(900);
  const issues = await page.evaluate(() => {
    const out = [];
    document.querySelectorAll('main table').forEach(t => {
      const p = t.parentElement;
      if (p.scrollWidth > p.clientWidth + 1) out.push(`table overflow ${p.scrollWidth}>${p.clientWidth} in "${t.closest('.panel')?.querySelector('h3')?.textContent}"`);
    });
    document.querySelectorAll('.kpi b, .kpi span, .panel h3, .panel p, th, td, .chip b').forEach(e => {
      if (e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).overflow !== 'visible') out.push(`clipped: "${e.textContent.slice(0, 50)}"`);
      if (e.matches('.kpi b') && e.scrollWidth > e.parentElement.clientWidth) out.push(`kpi value wider than card: "${e.textContent}"`);
    });
    document.querySelectorAll('td').forEach(e => {
      if (/^[-+$\d.,%x—]+[kKMbn%]*$/.test(e.textContent.trim()) && e.getClientRects().length > 1) out.push(`number wraps: "${e.textContent}"`);
    });
    document.querySelectorAll('.panel').forEach(pn => {
      const c = pn.querySelector('.chart');
      if (c && !c.querySelector('svg, table, .js-plotly-plot, div')) out.push(`empty panel "${pn.querySelector('h3')?.textContent}"`);
    });
    return out;
  });
  console.log(`${issues.length ? '✗' : '✓'} ${id}${issues.length ? '\n    ' + issues.join('\n    ') : ''}`);
  failures += issues.length;
  if (!shots.length || shots.includes(id)) {
    const file = path.join(root, 'evidence', `preview-${phase}-${id}.png`);
    await page.screenshot({ path: file, fullPage: true });
    if (id === (shots[0] || ids[0])) fs.copyFileSync(file, path.join(root, 'evidence', `preview-${phase}.png`));
  }
}
await browser.close();
process.exit(failures ? 1 : 0);
