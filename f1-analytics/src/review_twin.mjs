// Visual review of the HTML twin, every page: no horizontal table scroll, no clipped KPI / header text,
// no empty slicer cards, no table column that is 100% on every row. Screenshots to evidence/.
//   node src/review_twin.mjs <fase> [main page id]
import { createRequire } from "node:module";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("/Users/shaka-mac-mini/.npm/_npx/705bc6b22212b352/node_modules/playwright");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const fase = process.argv[2] || "x";
const main = process.argv[3];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
await page.goto("file://" + path.join(root, "reports", "dashboard.html"));
const ids = await page.evaluate(() => PAGES.map((p) => p.id));
let problems = 0;
for (const id of ids) {
  await page.evaluate((id) => render(id), id);
  await page.waitForTimeout(700);
  const issues = await page.evaluate(() => {
    const out = [];
    document.querySelectorAll("table").forEach((t) => {
      const box = t.parentElement;
      if (box.scrollWidth > box.clientWidth + 1) out.push(`table scrolls sideways: ${box.closest(".panel").querySelector("h3").textContent}`);
      const heads = [...t.querySelectorAll("th")].map((h) => h.textContent);
      const body = [...t.querySelectorAll("tbody tr")].map((r) => [...r.children].map((c) => c.textContent.trim()));
      heads.forEach((h, i) => {
        if (body.length > 1 && body.every((r) => r[i] === "100%" || r[i] === "100.0%")) out.push(`column always 100%: ${h}`);
      });
    });
    document.querySelectorAll(".kpi b, th, .panel h3").forEach((e) => {
      if (e.scrollWidth > e.clientWidth + 1) out.push(`clipped text: ${e.textContent.slice(0, 60)}`);
    });
    document.querySelectorAll(".slicer b").forEach((e) => { if (!e.textContent.trim()) out.push("empty slicer card"); });
    document.querySelectorAll(".chart").forEach((c) => {
      if (!c.querySelector("table") && !c.querySelector(".plot-container") && !c.textContent.trim())
        out.push(`empty chart: ${c.closest(".panel").querySelector("h3").textContent}`);
    });
    return out;
  });
  problems += issues.length;
  console.log(`${issues.length ? "✗" : "✓"} ${id}${issues.length ? "\n    " + issues.join("\n    ") : ""}`);
  await page.screenshot({ path: path.join(root, "evidence", `preview-${fase}-${id}.png`), fullPage: true });
  if (id === (main || ids[0])) await page.screenshot({ path: path.join(root, "evidence", `preview-${fase}.png`), fullPage: true });
}
await browser.close();
console.log(`${ids.length} pages, ${problems} problems`);
process.exit(problems ? 1 : 0);
