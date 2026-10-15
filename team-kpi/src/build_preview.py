"""Static HTML twin of the Power BI report: same pages, layout, palette and measure definitions.

    uv run python src/build_preview.py      -> reports/dashboard.html (self-contained, opens offline)

Power BI Desktop is Windows-only; this lets the report be reviewed anywhere. It shows the report's
default state (all quarters, teams, priorities and types; SLA Scale 100%;
Analyze By = Team), computed by preview_pages.py from data/marts with the same rules as the DAX.
"""
import json
from pathlib import Path

from plotly.offline import get_plotlyjs

from build_report import ACCENT, BEHIND_C, HIDDEN_PAGES, INK, INK2, MUTED, ON_C, PAGES, RISK_C, SIDE, SIDE_LINE, SIDE_TEXT
from preview_pages import BUILD

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "dashboard.html"

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Team Delivery KPIs</title>
<style>
:root{--ink:%(ink)s;--ink2:%(ink2)s;--muted:%(muted)s;--hair:#E6E4DD;--surface:#FCFCFB;--plane:#F4F4F1;
--side:%(side)s;--side-text:%(side_text)s;--side-line:%(side_line)s;--good:%(good)s;--bad:%(bad)s;--accent:%(accent)s}
*{box-sizing:border-box}
body{margin:0;background:#E9E8E3;font-family:"Segoe UI",system-ui,-apple-system,sans-serif;color:var(--ink)}
.app{display:grid;grid-template-columns:232px 1fr;min-height:100vh;max-width:1600px;margin:0 auto;background:var(--plane)}
aside{background:var(--side);border-right:3px solid var(--accent);padding:28px 12px;display:flex;flex-direction:column;position:sticky;top:0;height:100vh}
.brand{padding:0 12px 20px;border-bottom:1px solid var(--side-line);margin-bottom:16px}
.brand small{display:block;font-size:11px;letter-spacing:.14em;font-weight:600;color:var(--side-text)}
.brand b{font-size:23px;color:#fff;font-weight:600}
nav button{all:unset;display:block;width:100%%;box-sizing:border-box;padding:11px 16px;margin:3px 0;border-radius:8px;color:var(--side-text);font-size:14px;cursor:pointer}
nav button:hover{background:rgba(255,255,255,.07)}
nav button[aria-current=true]{background:rgba(255,255,255,.14);color:#fff;font-weight:600}
aside footer{margin-top:auto;padding:16px 12px 0;border-top:1px solid var(--side-line);font-size:11px;line-height:1.6;color:var(--side-text)}
main{padding:22px 24px 24px}
header{display:flex;justify-content:space-between;gap:24px;border-bottom:1px solid var(--hair);padding-bottom:12px;margin-bottom:12px}
header h1{margin:0;font-size:26px;font-weight:600}
header p{margin:4px 0 0;color:var(--ink2);font-size:13px;max-width:640px}
.chips{display:flex;gap:8px;align-items:flex-start;flex-wrap:wrap}
.chip{border:1px solid var(--hair);border-radius:8px;background:var(--surface);padding:6px 10px;font-size:11px;color:var(--muted);white-space:nowrap}
.chip b{display:block;color:var(--ink);font-size:13px;font-weight:600}
.kpis{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:16px;margin-bottom:16px}
.kpi,.panel{background:var(--surface);border:1px solid var(--hair);border-radius:12px}
.kpi{padding:14px 16px}
.kpi span{font-size:12px;color:var(--muted)}
.kpi b{display:block;font-size:26px;font-weight:600;margin-top:4px;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.good{color:var(--good)!important}.bad{color:var(--bad)!important}.risk{color:%(risk)s!important}
.row{display:flex;gap:16px;margin-bottom:16px}
.panel{padding:12px 14px 8px;min-width:0;display:flex;flex-direction:column}
.panel h3{margin:0;font-size:15px;font-weight:600}
.panel>p{margin:2px 0 6px;font-size:12px;color:var(--muted)}
.chart{flex:1;min-height:0;overflow:auto}
.row.r2 .panel{height:332px}.row.r3 .panel{height:316px}.row .panel.full{height:776px}
.stack{display:flex;flex-direction:column;gap:16px;background:none;border:0;padding:0}
.stack .panel{flex:none}.stack .panel.grow{flex:1;min-height:0}
table{width:100%%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th{font-size:11px;color:var(--muted);font-weight:600;text-align:right;padding:7px 10px;border-bottom:1px solid var(--hair);position:sticky;top:0;background:var(--surface)}
td{text-align:right;padding:7px 10px;border-bottom:1px solid #ECEBE6;white-space:nowrap}td:last-child{white-space:normal}th{white-space:nowrap}
th:first-child,td:first-child{font-weight:600}td.l,th.l{text-align:left}
tr.shade td{background:#E8F1EC;font-weight:600}
.headline{font-size:17px;line-height:1.45;margin:6px 0 14px}
.split{display:flex;gap:16px;align-items:stretch;margin-top:auto}.split>*{flex:1}
.mini{border:1px solid var(--hair);border-radius:10px;padding:10px 12px;margin-bottom:8px}
.mini span{display:block;font-size:11px;color:var(--muted)}.mini b{font-size:20px;font-weight:600}
.steps{display:flex;flex-wrap:wrap;gap:4px;margin:10px 0 14px}.steps span{border:1px solid var(--hair);border-radius:6px;padding:3px 7px;font-size:11px;color:var(--ink2)}
.steps span.on{background:var(--side);color:#fff;border-color:var(--side)}
h4{margin:0 0 4px;font-size:12px}.note{font-size:12px;color:var(--ink2);line-height:1.5;margin:0}
.row .panel.auto{height:auto}.row .panel.auto .chart{overflow:visible}
.toggle{position:absolute;top:10px;right:14px;display:flex;gap:8px}.panel{position:relative}
.toggle button{all:unset;cursor:pointer;border:1px solid var(--hair);background:var(--surface);border-radius:8px;padding:5px 14px;font-size:12px;font-weight:600;color:var(--ink2)}
.toggle button[aria-pressed=true]{background:var(--side);border-color:var(--side);color:#fff}
.dax{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;align-items:start}
.dax pre{margin:0;background:#1F2A36;color:#E8EDF2;border-radius:8px;padding:12px 14px;font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap}
.dax .res{max-height:420px;overflow:auto}.dax td{font-weight:400!important}
@media (max-width:1100px){.app{grid-template-columns:1fr}aside{position:static;height:auto}.row{flex-direction:column}.panel{width:100%%!important}.kpis{grid-auto-flow:row;grid-template-columns:repeat(2,1fr)}}
</style></head>
<body><div class="app">
<aside><div class="brand"><small>APACHE JIRA</small><b>Team Delivery</b></div><nav id="nav"></nav>
<footer>Source: issues.apache.org Jira (Hive, Cassandra, HBase, Artemis), public<br>Pipeline: Jira REST · DuckDB · Parquet · Power BI (TMDL + PBIR)<br>Static preview of the Power BI report</footer></aside>
<main id="main"></main></div>
<script>%(plotly)s</script>
<script>
const PAGES = %(pages)s;
const CHIPS = [['Quarter','All'],['Team','All'],['Priority','All'],['Type','All']];
const nav = document.getElementById('nav'), main = document.getElementById('main');
function el(tag, attrs = {}, html = '') { const e = document.createElement(tag); Object.assign(e, attrs); e.innerHTML = html; return e; }
function tableHtml(b) {
  return '<table><thead><tr>' + b.columns.map((h, j) => `<th class="${b.left.includes(j) ? 'l' : ''}">${h}</th>`).join('') + '</tr></thead><tbody>' +
    b.rows.map((row, i) => `<tr class="${b.shade.includes(i) ? 'shade' : ''}">` +
      row.map((v, j) => { const l = b.left.includes(j) ? ' l' : '';
        return v && v.v !== undefined ? `<td class="${v.t}${l}">${v.v}</td>` : `<td class="${l}">${v}</td>`; }).join('') + '</tr>').join('') +
    '</tbody></table>';
}
function panel(title, sub, body, cls) {
  const p = el('div', {className: 'panel ' + (cls || '')}, (title ? `<h3>${title}</h3>` : '') + (sub ? `<p>${sub}</p>` : ''));
  const c = el('div', {className: 'chart'}); p.append(c);
  if (body.toggle) {  // two bookmarks over the same data: chart or table
    const bar = el('div', {className: 'toggle'}), views = body.toggle.map(([label, b]) => {
      const v = el('div', {className: 'chart'}); v.style.height = '100%%';
      if (b.columns) v.innerHTML = tableHtml(b); else requestAnimationFrame(() => Plotly.newPlot(v, b.data, {...b.layout, autosize: true}, {displayModeBar: false, responsive: true}));
      return v; });
    const show = i => { views.forEach((v, j) => v.style.display = i === j ? '' : 'none');
      [...bar.children].forEach((b, j) => b.setAttribute('aria-pressed', i === j));
      if (views[i].data) Plotly.Plots.resize(views[i]);
      history.replaceState(null, '', '#' + VIEW.id + (i ? ':' + body.toggle[i][0].toLowerCase() : '')); };
    body.toggle.forEach(([label], i) => { const b = el('button', {}, label); b.onclick = () => show(i); bar.append(b); });
    p.append(bar); c.remove(); views.forEach(v => p.append(v));
    requestAnimationFrame(() => show(Math.max(0, body.toggle.findIndex(([l]) => l.toLowerCase() === VIEW.arg))));
    return p;
  }
  if (body.columns) c.innerHTML = tableHtml(body);
  else if (body.html) { c.innerHTML = body.html; c.style.display = 'flex'; c.style.flexDirection = 'column'; }
  else requestAnimationFrame(() => Plotly.newPlot(c, body.data, {...body.layout, autosize: true}, {displayModeBar: false, responsive: true}));
  return p;
}
let VIEW = {};
function render(hash) {
  const [id, arg] = decodeURIComponent(hash).split(':');
  const page = PAGES.find(p => p.id === id) || PAGES[0];
  VIEW = {id: page.id, arg};
  [...nav.children].forEach(b => b.setAttribute('aria-current', b.dataset.id === page.id));
  main.innerHTML = '';
  const head = el('header');
  head.append(el('div', {}, `<h1>${page.title}</h1><p>${page.subtitle}</p>`));
  head.append(el('div', {className: 'chips'}, CHIPS.map(([k, v]) => `<div class="chip">${k}<b>${v}</b></div>`).join('')));
  main.append(head);
  if (page.kpis.length) {
    const k = el('section', {className: 'kpis'});
    page.kpis.forEach(x => k.append(el('div', {className: 'kpi'}, `<span>${x.label}</span><b class="${x.t || ''}">${x.value}</b>`)));
    main.append(k);
  }
  page.rows.forEach((row, ri) => {
    const r = el('section', {className: 'row ' + (ri ? 'r3' : 'r2')});
    row.forEach(([title, sub, width, body, cls]) => {
      let p;
      if (body.stack) {
        p = el('div', {className: 'stack ' + (cls || '')});
        body.stack.forEach(([t, s, h, b]) => { const q = panel(t, s, b, h ? '' : 'grow'); if (h) q.style.height = h + 'px'; p.append(q); });
      } else p = panel(title, sub, body, cls);
      p.style.width = width + 'px'; r.append(p);
    });
    main.append(r);
  });
  if (!arg) history.replaceState(null, '', '#' + page.id);
}
PAGES.forEach(p => { const b = el('button', {}, p.name); b.dataset.id = p.id; b.onclick = () => render(p.id); nav.append(b); });
addEventListener('hashchange', () => render(location.hash.slice(1)));
render((location.hash || '#overview').slice(1));
</script></body></html>"""


# The DAX query view tabs saved with the semantic model (not a report page).
EXTRA = [*[(pid, f"{name} (hidden)", title or "Team Tip", subtitle or "The report-page tooltip shown on hover over "
          "team bars (Overview, SLA)") for pid, name, title, subtitle in HIDDEN_PAGES],
         ("dax_queries", "DAX queries", "DAX query view",
          "The saved queries in TeamKPI.SemanticModel/DAXQueries, with the result each should return "
          "(computed from data/marts and the model files)")]


def main():
    pages = []
    for pid, name, title, subtitle in [*PAGES, *EXTRA]:
        pages.append({"id": pid, "name": name, "title": title, "subtitle": subtitle, **BUILD[pid]()})
        print(f"  ✓ {name}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(HTML % {"plotly": get_plotlyjs(), "pages": json.dumps(pages, default=str),
                           "ink": INK, "ink2": INK2, "muted": MUTED, "side": SIDE, "side_text": SIDE_TEXT,
                           "side_line": SIDE_LINE, "good": ON_C, "bad": BEHIND_C, "risk": RISK_C, "accent": ACCENT})
    print(f"  ✓ wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
