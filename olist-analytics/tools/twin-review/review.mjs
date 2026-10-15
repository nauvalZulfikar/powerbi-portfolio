// Visual review of the HTML twin (reports/dashboard.html), every page:
//   overflowing tables, clipped or wrapped headers/numbers, empty slicer chips; optional screenshots.
//   node tools/twin-review/review.mjs [shot-prefix] [page-id ...]
// shot-prefix e.g. evidence/preview-A -> evidence/preview-A.png (first page listed) + evidence/preview-A-<page>.png
import { createRequire } from "node:module";
import * as path from "node:path";
const require = createRequire(import.meta.url);
const { chromium } = require("/Users/shaka-mac-mini/.npm/_npx/705bc6b22212b352/node_modules/playwright");

const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../..");
const url = "file://" + path.join(root, "reports/dashboard.html");
const [prefix, ...shots] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
await page.goto(url);
const ids = await page.evaluate(() => PAGES.map((p) => p.id));
let problems = 0;
for (const id of ids) {
  await page.evaluate((id) => { location.hash = id; render(id); }, id);
  await page.waitForTimeout(1200);
  const issues = await page.evaluate(() => {
    const out = [];
    const title = (el) => el.closest(".panel")?.querySelector("h3")?.textContent ?? "?";
    for (const t of document.querySelectorAll("main table")) {
      const p = t.parentElement;
      if (p.scrollWidth > p.clientWidth + 1) out.push(`table overflows sideways: ${title(t)} (${p.scrollWidth} > ${p.clientWidth})`);
      for (const c of t.querySelectorAll("th, td")) {
        if (c.scrollWidth > c.clientWidth + 1) { out.push(`clipped cell in ${title(t)}: "${c.textContent}"`); break; }
      }
      for (const c of t.querySelectorAll("th, td")) {
        const lh = parseFloat(getComputedStyle(c).lineHeight) || parseFloat(getComputedStyle(c).fontSize) * 1.3;
        const pad = parseFloat(getComputedStyle(c).paddingTop) + parseFloat(getComputedStyle(c).paddingBottom);
        if (c.textContent.trim() && c.clientHeight - pad > lh * 1.6) { out.push(`wrapped cell in ${title(t)}: "${c.textContent}"`); break; }
      }
      // a column whose every value is 100% says nothing
      const rows = [...t.querySelectorAll("tbody tr")].map((r) => [...r.children].map((c) => c.textContent.trim()));
      if (rows.length > 1) rows[0].forEach((_, i) => {
        if (rows.every((r) => r[i] === "100%" || r[i] === "100.0%")) out.push(`column always 100% in ${title(t)}`);
      });
    }
    for (const el of document.querySelectorAll(".kpi b, .kpi span, .panel h3, .chip b, header h1")) {
      if (el.scrollWidth > el.clientWidth + 1) out.push(`clipped text: "${el.textContent}"`);
    }
    for (const chip of document.querySelectorAll(".chip")) if (!chip.querySelector("b")?.textContent.trim()) out.push(`empty slicer chip: ${chip.textContent}`);
    for (const k of document.querySelectorAll(".kpi b")) if (!k.textContent.trim()) out.push(`empty KPI: ${k.parentElement.textContent}`);
    for (const p of document.querySelectorAll(".panel")) {
      const c = p.querySelector(".chart");
      if (c && !c.querySelector("table, .js-plotly-plot, .text")) out.push(`empty panel: ${p.querySelector("h3")?.textContent}`);
    }
    return out;
  });
  problems += issues.length;
  console.log(`${issues.length ? "✗" : "✓"} ${id}${issues.map((i) => "\n    " + i).join("")}`);
  if (prefix && (shots.length === 0 || shots.includes(id))) {
    const first = (shots[0] ?? ids[0]) === id;
    await page.screenshot({ path: path.join(root, `${prefix}${first ? "" : "-" + id}.png`), fullPage: true });
  }
}
await browser.close();
console.log(`${ids.length} pages, ${problems} issues`);
process.exit(problems ? 1 : 0);
