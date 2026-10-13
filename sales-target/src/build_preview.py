"""Static HTML twin of the Power BI report: same pages, layout, palette and measure definitions.

    uv run python src/build_preview.py      -> reports/dashboard.html (self-contained, opens offline)

Power BI Desktop is Windows-only; this lets the report be reviewed anywhere. It shows the report's
default state (all fiscal years, regions, categories and business types; Target Stretch 0%;
Analyze By = Region), computed by preview_pages.py from data/marts with the same rules as the DAX.
"""
import json
from pathlib import Path

from plotly.offline import get_plotlyjs

from build_report import ACCENT, BEHIND_C, INK, INK2, MUTED, ON_C, PAGES, RISK_C, SIDE, SIDE_LINE, SIDE_TEXT
from preview_pages import BUILD, DETAIL, TIPS

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "dashboard.html"

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sales vs Target</title>
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
th{font-size:11px;color:var(--muted);font-weight:600;text-align:right;padding:7px 6px;border-bottom:1px solid var(--hair);position:sticky;top:0;background:var(--surface)}
td{text-align:right;padding:7px 6px;border-bottom:1px solid #ECEBE6}
th,td:not(.l){white-space:nowrap}
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
.back{all:unset;cursor:pointer;border:1px solid var(--hair);background:var(--surface);border-radius:8px;padding:6px 12px;font-size:13px;font-weight:600;margin-bottom:6px;display:inline-block}
.tip{width:320px;height:240px;box-sizing:border-box;background:#fff;border:1px solid var(--hair);border-radius:10px;padding:8px;box-shadow:0 6px 18px rgba(0,0,0,.12);display:flex;flex-direction:column}
.tip h5{margin:0 0 6px;font-size:12px}.tcs{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.tc{border:1px solid #ECEBE6;border-radius:6px;padding:4px 8px}.tc span{display:block;font-size:10px;color:var(--muted)}.tc b{font-size:16px;font-weight:600}
.tbs{display:flex;gap:6px;align-items:flex-end;margin-top:auto;height:84px}.tb{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;font-size:9px;color:var(--muted)}
.tb i{display:block;width:70%%;border-radius:3px 3px 0 0}.tb em{font-style:normal;color:var(--ink2);order:-1}
.tipgrid{display:flex;flex-wrap:wrap;gap:16px}.tipgrid .tip{box-shadow:none}
#tipfloat{position:fixed;z-index:9;pointer-events:none;display:none}
.dax{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;align-items:start}
.dax pre{margin:0;background:#1F2A36;color:#E8EDF2;border-radius:8px;padding:12px 14px;font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap}
.dax .res{max-height:420px;overflow:auto}.dax td{font-weight:400!important}
@media (max-width:1100px){.app{grid-template-columns:1fr}aside{position:static;height:auto}.row{flex-direction:column}.panel{width:100%%!important}.kpis{grid-auto-flow:row;grid-template-columns:repeat(2,1fr)}}
</style></head>
<body><div class="app">
<aside><div class="brand"><small>ADVENTURE WORKS</small><b>Sales vs Target</b></div><nav id="nav"></nav>
<footer>Source: Microsoft AdventureWorks DW reseller sales &amp; quotas (MIT)<br>Pipeline: branch Excel files · Power Query (M) · Power BI (TMDL + PBIR)<br>Static preview of the Power BI report</footer></aside>
<main id="main"></main></div><div id="tipfloat"></div>
<script>%(plotly)s</script>
<script>
const PAGES = %(pages)s, DETAIL = %(detail)s, TIPS = %(tips)s;
const CHIPS = [['Fiscal year','All'],['Region','All'],['Category','All'],['Business type','All']];
const nav = document.getElementById('nav'), main = document.getElementById('main');
function el(tag, attrs = {}, html = '') { const e = document.createElement(tag); Object.assign(e, attrs); e.innerHTML = html; return e; }
function tableHtml(b) {
  return '<table><thead><tr>' + b.columns.map((h, j) => `<th class="${b.left.includes(j) ? 'l' : ''}">${h}</th>`).join('') + '</tr></thead><tbody>' +
    b.rows.map((row, i) => `<tr class="${b.shade.includes(i) ? 'shade' : ''}">` +
      row.map((v, j) => { const l = b.left.includes(j) ? ' l' : '';
        return v && v.v !== undefined ? `<td class="${v.t || ''}${l}"${v.bg ? ` style="background:${v.bg}"` : ''}>${v.v}</td>` : `<td class="${l}">${v}</td>`; }).join('') + '</tr>').join('') +
    '</tbody></table>';
}
function panel(title, sub, body, cls) {
  const p = el('div', {className: 'panel ' + (cls || '')}, (title ? `<h3>${title}</h3>` : '') + (sub ? `<p>${sub}</p>` : ''));
  const c = el('div', {className: 'chart'}); p.append(c);
  if (body.columns) c.innerHTML = tableHtml(body);
  else if (body.html) { c.innerHTML = body.html; c.style.display = 'flex'; c.style.flexDirection = 'column'; }
  else requestAnimationFrame(() => Plotly.newPlot(c, body.data, {...body.layout, autosize: true}, {displayModeBar: false, responsive: true})
    .then(() => { if (!body.tip) return;  // report-page tooltip + drillthrough on the rep bars
      const f = document.getElementById('tipfloat');
      c.on('plotly_hover', e => { const r = e.points[0].y; if (!TIPS[r]) return; f.innerHTML = TIPS[r]; f.style.display = 'block';
        f.style.left = Math.min(e.event.clientX + 16, innerWidth - 336) + 'px'; f.style.top = Math.min(e.event.clientY + 12, innerHeight - 256) + 'px'; });
      c.on('plotly_unhover', () => f.style.display = 'none');
      c.on('plotly_click', e => { f.style.display = 'none'; render('rep_detail:' + e.points[0].y); }); }));
  return p;
}
function render(hash) {
  const [id, arg] = decodeURIComponent(hash).split(':');
  let page = PAGES.find(p => p.id === id) || PAGES[0];
  if (page.detail) page = {...page, ...DETAIL[DETAIL[arg] ? arg : page.default], rep: DETAIL[arg] ? arg : page.default};
  [...nav.children].forEach(b => b.setAttribute('aria-current', b.dataset.id === page.id));
  main.innerHTML = '';
  const head = el('header'), left = el('div');
  if (page.back) { const b = el('button', {className: 'back'}, '← Back'); b.onclick = () => render(page.back); left.append(b); }
  left.append(el('div', {}, `<h1>${page.title}</h1><p>${page.subtitle}</p>`));
  head.append(left);
  const chips = page.rep ? [['Drillthrough · Rep', page.rep], ...CHIPS] : CHIPS;
  head.append(el('div', {className: 'chips'}, chips.map(([k, v]) => `<div class="chip">${k}<b>${v}</b></div>`).join('')));
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
  history.replaceState(null, '', '#' + page.id + (page.rep ? ':' + encodeURIComponent(page.rep) : ''));
}
PAGES.filter(p => !p.hidden).forEach(p => { const b = el('button', {}, p.name); b.dataset.id = p.id; b.onclick = () => render(p.id); nav.append(b); });
addEventListener('hashchange', () => render(location.hash.slice(1)));
render((location.hash || '#overview').slice(1));
</script></body></html>"""


# Pages that exist in the report but not in its page navigator (Power BI hides drillthrough and tooltip
# pages in view mode), plus the DAX query view tabs saved with the semantic model.
EXTRA = [("rep_detail", "Rep detail", "Rep detail", "", True),
         ("rep_tooltip", "Rep tooltip", "Rep tooltip", "Tooltip page bound to Reps[Rep]; hidden in view mode", True),
         ("dax_queries", "DAX queries", "DAX query view",
          "The saved queries in SalesTarget.SemanticModel/DAXQueries, with the result each should return "
          "(computed from data/marts and the model files)")]


def main():
    pages = []
    for pid, name, title, subtitle, *hidden in [*PAGES, *EXTRA]:
        pages.append({"id": pid, "name": name, "title": title, "subtitle": subtitle, "hidden": bool(hidden),
                      **BUILD[pid]()})
        print(f"  ✓ {name}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(HTML % {"plotly": get_plotlyjs(), "pages": json.dumps(pages, default=str),
                           "detail": json.dumps(DETAIL, default=str), "tips": json.dumps(TIPS),
                           "ink": INK, "ink2": INK2, "muted": MUTED, "side": SIDE, "side_text": SIDE_TEXT,
                           "side_line": SIDE_LINE, "good": ON_C, "bad": BEHIND_C, "risk": RISK_C, "accent": ACCENT})
    print(f"  ✓ wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
