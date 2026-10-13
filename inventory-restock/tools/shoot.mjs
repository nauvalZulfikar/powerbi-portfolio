// Screenshot twin pages and check layout: node tools/shoot.mjs <phase> <page-id>...
// Writes evidence/preview-<phase>-<page>.png; prints panels that scroll sideways, clipped text and the KPI cards.
import { createRequire } from "module";
import { fileURLToPath } from "url";
import path from "path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT ||
  "/home/user/.npm/_npx/705bc6b22212b352/node_modules/playwright");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const [phase, ...pages] = process.argv.slice(2);

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
let bad = 0;
for (const id of pages) {
  await page.goto(`file://${root}/reports/dashboard.html#${id}`);
  await page.waitForTimeout(1500);
  const r = await page.evaluate(() => {
    const over = (e) => e.scrollWidth > e.clientWidth + 1;
    const name = (e) => (e.closest(".panel")?.querySelector("h3")?.textContent || e.tagName).slice(0, 50);
    return {
      scrolling: [...document.querySelectorAll(".chart, table")].filter(over).map(name),
      clipped: [...document.querySelectorAll("h3, h4, p, th, td, .kpi span, .kpi b")]
        .filter((e) => over(e) && getComputedStyle(e).overflow !== "visible").map((e) => e.textContent.slice(0, 50)),
      kpis: [...document.querySelectorAll(".kpi")].map((k) => k.innerText.replace(/\n/g, ": ")),
    };
  });
  const file = path.join(root, "evidence", `preview-${phase}-${id}.png`);
  await page.screenshot({ path: file, fullPage: true });
  bad += r.scrolling.length + r.clipped.length;
  console.log(`${id}: ${file}\n  scrolling: ${r.scrolling.length} ${r.scrolling.join(" | ")}\n` +
    `  clipped: ${r.clipped.length} ${r.clipped.join(" | ")}\n  kpis: ${r.kpis.join(" · ")}`);
}
await browser.close();
process.exit(bad ? 1 : 0);
