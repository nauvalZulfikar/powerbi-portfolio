"""Static HTML twin of the Power BI report — same pages, layout, palette and measure definitions.

    uv run python src/build_preview.py      -> reports/dashboard.html (self-contained, opens offline)

Power BI Desktop is Windows-only; this lets the report be reviewed anywhere. It shows the
report's default state: FY2013, all entities, compared with budget, outlook as of Jun 2013.
Every number is computed in DuckDB from the same fact table, with the same rules as the DAX.
"""
import json
from pathlib import Path

import duckdb
from plotly.offline import get_plotlyjs

from build_report import (ACTUAL, BAD, BOOKMARKS, CCY, COMPARE, GOOD, HIDDEN_PAGES, INK, INK2, MUTED, OUTLOOK, PAGES,
                          SIDE, SIDE_LINE, SIDE_TEXT, TOTAL)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "dashboard.html"
con = duckdb.connect(str(ROOT / "data" / "warehouse" / "finance.duckdb"), read_only=True)

YEAR, ASOF = 2013, "2013-06-01"
ACT = "sum(f.amount_usd) filter (where f.scenario = 'Actual')"
BUD = "coalesce(sum(f.amount_usd) filter (where f.scenario = 'Budget'), 0)"
ABR = "sum(f.amount_usd_budget_rate) filter (where f.scenario = 'Actual')"
FROM = ("from mart.fact_gl f join mart.bridge_line_account b using (account_key) join mart.dim_pnl_line l using (line_id) "
        "join mart.dim_entity e using (entity_key) join mart.dim_department d using (department_key)")
FY = f"year(f.period) = {YEAR}"
OPER = "l.line_kind = 'detail' and l.is_operating"


def rows(sql):
    cur = con.execute(sql)
    names, data = [d[0] for d in cur.description], cur.fetchall()
    return {n: [r[i] for r in data] for i, n in enumerate(names)}


def one(sql):
    return con.execute(sql).fetchone()


def line(lid, expr=ACT, where=FY):
    return one(f"select {expr} {FROM} where {where} and l.line_id = '{lid}'")[0] or 0


# ------------------------------------------------------------------ formats
def usd(v, signed=False):
    """$13.79M; costs in brackets; signed=True gives +/− for variances."""
    if v is None:
        return "—"
    v = 0 if abs(v) < 0.5 else v
    a = abs(v)
    body = next((f"${a / d:,.{p}f}{u}" for d, u, p in ((1e6, "M", 2), (1e3, "K", 0)) if a >= d * 0.9995), f"${a:,.0f}")
    if signed:
        return ("+" if v >= 0 else "−") + body
    return f"({body})" if v < 0 else body


pct = lambda v, signed=True: "—" if v is None or abs(v) > 50 else f"{'+' if signed and v >= 0 else ''}{v * 100:.1f}%".replace("-", "−")
tone = lambda v: "good" if (v or 0) >= 0 else "bad"
cell = lambda text, v: {"v": text, "t": tone(v)}
# [Variance Arrow]: ▲/▼ + variance %, falling back to $ when there is no comparison to divide by.
arrow = lambda v, b: cell(("▲ " if v >= 0 else "▼ ") + (pct(v / abs(b)) if abs(b or 0) >= 1 else usd(v, True)), v)

# ------------------------------------------------------------------ figures
AXIS = dict(showgrid=False, zeroline=False, linecolor="#E6E4DD", tickfont=dict(color=MUTED, size=10), title=None)
YAXIS = dict(showgrid=True, gridcolor="#ECEBE6", zeroline=True, zerolinecolor="#D9D7CF", tickfont=dict(color=MUTED, size=10),
             title=None, tickprefix="$", tickformat="~s")
LEGEND = dict(orientation="h", y=1.14, x=0, font=dict(color=INK2, size=10), bgcolor="rgba(0,0,0,0)")


def fig(traces, **layout):
    base = dict(margin=dict(l=52, r=12, t=8, b=32), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(family="Segoe UI, system-ui, -apple-system, sans-serif", color=INK2, size=11),
                xaxis=AXIS, yaxis=YAXIS, showlegend=False,
                hoverlabel=dict(bgcolor="#fff", bordercolor="#E6E4DD", font=dict(color=INK)))
    for k, v in layout.items():
        base[k] = {**base[k], **v} if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return {"data": traces, "layout": base}


def hbars(labels, values, colors, text=None, left=150, pad=(1.4, 1.3)):
    """pad: axis room beyond the longest negative / positive bar for the outside labels."""
    t = dict(type="bar", orientation="h", y=labels, x=values, marker=dict(color=colors, cornerradius=4),
             text=text or [usd(v, True) for v in values], textposition="outside", cliponaxis=False,
             textfont=dict(size=10, color=INK2), hovertemplate="%{y}: %{text}<extra></extra>")
    return fig([t], yaxis=dict(autorange="reversed", showgrid=False, zeroline=False, tickprefix="", tickformat="",
                               tickfont=dict(color=INK2, size=10)),
               xaxis=dict(visible=False, zeroline=True, range=[min(0, *values) * pad[0], max(0, *values) * pad[1]]),
               margin=dict(l=left, r=16, t=4, b=8))


def lines(x, series, **layout):
    return fig([dict(type="scatter", mode="lines+markers" if len(x) <= 12 else "lines", name=name, x=x, y=y,
                     line=dict(color=col, width=2.2, dash=dash), marker=dict(size=5, color=col),
                     hovertemplate=f"{name} · %{{x|%b %Y}}: {fmt}<extra></extra>")
                for name, y, col, dash, fmt in series],
               showlegend=True, legend=LEGEND, xaxis=dict(tickformat="%b", dtick="M1"), **layout)


def table(columns, body, shade=(), left=(0,), compact=False, flag=()):
    """shade = subtotal rows; flag = rows with the [Outlier Fill] conditional-formatting background."""
    return {"columns": columns, "rows": body, "shade": list(shade), "left": list(left), "compact": compact, "flag": list(flag)}


def wrap(t, n=13):
    """Break a long axis label at the space nearest its middle."""
    if len(t) <= n or " " not in t:
        return t
    i = min((i for i, ch in enumerate(t) if ch == " "), key=lambda i: abs(i - len(t) / 2))
    return t[:i] + "<br>" + t[i + 1:]


def dense_rank(values, reverse=False):
    """RANKX(..., DENSE): 1 = smallest value (worst variance) unless reverse."""
    order = sorted(set(values), reverse=reverse)
    return [order.index(v) + 1 for v in values]


def svg_bar(v, max_abs):
    """Same picture as the Svg.VarianceBar DAX function: centred on zero, teal right / orange left."""
    w = round(abs(v) / max_abs * 58, 1) if max_abs else 0
    x = 60 if v >= 0 else 60 - w
    return (f'<svg width="120" height="16" style="vertical-align:middle"><rect x="{x}" y="3" width="{w}" height="10" rx="2" '
            f'fill="{GOOD if v >= 0 else BAD}"/><line x1="60" y1="0" x2="60" y2="16" stroke="#8C959F"/></svg>')


def running(values):
    return [sum(values[:i + 1]) for i in range(len(values))]


def moving_avg(values, n):
    """MOVINGAVERAGE(x, n): mean of the current row and up to n-1 rows before it."""
    return [sum(values[max(0, i - n + 1):i + 1]) / len(values[max(0, i - n + 1):i + 1]) for i in range(len(values))]


def kpis(items):
    return [{"label": k, "value": v, "t": t} for k, v, *t in [(*i, None) if len(i) == 2 else i for i in items]]


# -------------------------------------------------------------------- pages
def months():
    return [f"{YEAR}-{m:02d}-01" for m in range(1, 13)]


def opex_accounts():
    """Opex accounts (everything under S3) for the year: name, variance vs budget, spend (positive)."""
    return rows(f"""select a.account, {ACT} - {BUD} v, -{ACT} s {FROM} join mart.dim_account a on a.account_key = f.account_key
                    where {FY} and l.line_id = 'S3' group by 1 order by 2""")


def pareto():
    """[Opex Overrun] by account, largest first, with [Overrun Cumulative %] and [Accounts to 80% Overrun]."""
    acc = opex_accounts()
    o = sorted(((n, -v) for n, v in zip(acc["account"], acc["v"]) if v < 0), key=lambda r: -r[1])
    total = sum(v for _, v in o)
    cum = [sum(w for _, w in o if w >= x) / total for _, x in o]
    to80 = sum(sum(w for _, w in o if w > x) / total < 0.8 for _, x in o)
    return o, cum, to80, total


def spend_class():
    """[Spend Class]: A = accounts making up the first 70% of opex, B = the next 20%, C = the rest."""
    acc = opex_accounts()
    sp = sorted(((n, v) for n, v in zip(acc["account"], acc["s"]) if v > 0), key=lambda r: -r[1])
    total = sum(v for _, v in sp)
    cls = lambda x: (lambda p: "A" if p < 0.7 else "B" if p < 0.9 else "C")(sum(w for _, w in sp if w > x) / total)
    return [(n, v, cls(v)) for n, v in sp]


BANDS = [("Big miss", -1e9, -0.10), ("Miss", -0.10, -0.02), ("On plan", -0.02, 0.02), ("Beat", 0.02, 1e9)]


def bands():
    """[Entities in Band]: entities by operating profit variance % (blank when the entity has no budget)."""
    e = rows(f"select e.entity, {ACT} a, {BUD} b {FROM} where {FY} and l.line_id = 'S4' group by 1")
    ps = [(a - b) / abs(b) for a, b in zip(e["a"], e["b"]) if abs(b or 0) >= 0.5]
    return [sum(lo <= p < hi for p in ps) for _, lo, hi in BANDS]


def opex_trend():
    """[Opex Trend Forecast]: opex actuals to As Of + a least-squares line (LINESTX) fitted on the 12 months to As Of."""
    h = rows(f"""select year(f.period) * 12 + month(f.period) x, -{ACT} y {FROM} where l.line_id = 'S3'
                 and f.period > date '{ASOF}' - interval 12 month and f.period <= date '{ASOF}' group by 1 order by 1""")
    x, y = h["x"], h["y"]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    b = sum((i - mx) * (j - my) for i, j in zip(x, y)) / sum((i - mx) ** 2 for i in x)
    a = my - b * mx
    asof_m = int(ASOF[5:7])
    actual = -one(f"select {ACT} {FROM} where {FY} and l.line_id = 'S3' and f.period <= date '{ASOF}'")[0]
    return actual + sum(a + b * (YEAR * 12 + m) for m in range(asof_m + 1, 13))


def page_overview():
    ns, op, opb = line("S1"), line("S4"), line("S4", BUD)
    var = op - opb
    nsv, opxv = ns - line("S1", BUD), line("S3") - line("S3", BUD)
    m = rows(f"select f.period::varchar p, {ACT} a, {BUD} b {FROM} where {FY} and l.line_id = 'S4' group by 1 order by 1")
    outlook = [a if p <= ASOF else b for p, a, b in zip(m["p"], m["a"], m["b"])]
    full_outlook = sum(outlook)
    ent = rows(f"""select e.entity, e.currency_code, {ACT} a, {BUD} b {FROM}
                   where {FY} and l.line_id = 'S4' and e.entity_key <> 99 group by all order by {ACT} - {BUD}""")
    ev = [a - b for a, b in zip(ent["a"], ent["b"])]
    erank, emax = dense_rank(ev), max(abs(v) for v in ev)
    eregion = dict(con.execute("select entity, region from mart.dim_entity").fetchall())
    reg_var = {}
    for e, v in zip(ent["entity"], ev):
        reg_var[eregion[e]] = reg_var.get(eregion[e], 0) + v
    status = lambda v, b: "Off track" if b and v / abs(b) < -0.05 else "Watch" if b and v < 0 else "On track" if b else "—"
    worst = min(zip(ev, ent["entity"]))
    by_actual = sorted(zip(ent["entity"], ent["a"], ent["b"]), key=lambda r: -r[1])
    chart_view = fig([dict(type="bar", orientation="h", name=n, y=[r[0] for r in by_actual], x=[r[i] for r in by_actual],
                           marker=dict(color=c, cornerradius=3), hovertemplate=f"{n} · %{{y}}: %{{x:$,.0f}}<extra></extra>")
                      for n, i, c in (("Actual", 1, ACTUAL), ("Comparison", 2, COMPARE))],
                     barmode="group", showlegend=True, legend=LEGEND,
                     yaxis=dict(autorange="reversed", showgrid=False, zeroline=False, tickprefix="", tickformat="",
                                tickfont=dict(color=INK2, size=10)),
                     margin=dict(l=150, r=16, t=8, b=24))
    headline = (f"Operating profit {usd(op)}, {usd(abs(var))} {'ahead of' if var >= 0 else 'behind'} budget. "
                f"Net sales beat it by {usd(nsv)}, operating expenses overran by {usd(-opxv)}."
                + (f" Biggest miss: {worst[1]}." if worst[0] < 0 else ""))
    opx_m = rows(f"select f.period::varchar p, {ACT} a, {BUD} b {FROM} where {FY} and l.line_id = 'S3' group by 1 order by 1")
    opex_outlook = -sum(a if p <= ASOF else b for p, a, b in zip(opx_m["p"], opx_m["a"], opx_m["b"]))
    band_n = bands()
    metric_chips = '<span class="steps" style="margin:4px 0 0">' + "".join(
        f'<span class="{"on" if k == "Actual" else ""}">{k}</span>' for k in
        ("Actual", "Comparison", "Outlook", "Variance", "FX Effect", "Operational Variance", "Prior month")) + "</span>"
    return {
        "kpis": kpis([("Net Sales", usd(ns)), ("Operating Profit", usd(op)), ("Operating Profit Comparison", usd(opb)),
                      ("Operating Profit Variance", usd(var, True), tone(var)), ("Operating Profit Variance %", pct(var / opb), tone(var)),
                      ("Operating Margin %", pct(op / ns, False)),
                      ("Entities Behind Budget", str(sum(v < 0 for v in ev)))]),
        "rows": [
            [("Monthly operating profit", "Line 1 = the metric picked here (Metric field parameter, single select); "
              "line 2 = the comparison" + metric_chips, 880,
              lines(m["p"], [("Metric Value", m["a"], ACTUAL, "solid", "%{y:$,.0f}"),
                             ("Operating Profit Comparison", m["b"], COMPARE, "solid", "%{y:$,.0f}")])),
             ("In one sentence", f"Outlook as of Jun {YEAR}: actuals Jan–Jun, budget Jul–Dec", 424,
              {"html": f'<div class="headline">{headline}</div><div class="split"><div class="chip">As Of<b>Jun {YEAR}</b></div>'
                       f'<div class="mini"><span>Operating Profit Outlook</span><b>{usd(full_outlook)}</b>'
                       f'<span>Opex Outlook</span><b>{usd(opex_outlook)}</b>'
                       f'<span>Opex Trend Forecast</span><b>{usd(opex_trend())}</b></div></div>'})],
            [("Operating variance by entity", "Analyze by: Entity · teal = favourable · hover → tooltip page", 400,
              hbars(ent["entity"], [a - b for a, b in zip(ent["a"], ent["b"])],
                    [GOOD if a - b >= 0 else BAD for a, b in zip(ent["a"], ent["b"])], left=124, pad=(2.1, 1.7))),
             ("Entities by variance band", "Operating profit variance %: <−10% · to −2% · ±2% · >+2%", 224,
              fig([dict(type="bar", x=[b[0] for b in BANDS], y=band_n, marker=dict(color=ACTUAL, cornerradius=3),
                        text=band_n, textposition="outside", cliponaxis=False, textfont=dict(size=10, color=INK2),
                        hovertemplate="%{x}: %{y} entities<extra></extra>")],
                  yaxis=dict(visible=False, range=[0, max(band_n) * 1.25]), xaxis=dict(tickfont=dict(size=10, color=MUTED)),
                  margin=dict(l=8, r=8, t=16, b=28))),
             ("Entity scorecard — operating profit", "Rank 1 = worst miss (RANK); ▲/▼ = favourable/unfavourable; "
              "share of the region's variance. Buttons = the two report bookmarks", 664,
              {"toggle": [
                  (BOOKMARKS[0][1], table(["Entity", "Rank", "Status", "Variance", "Variance arrow", "Share of region", "Variance bar"],
                                          [[e, r, status(a - b, b), cell(usd(a - b, True), a - b), arrow(a - b, b),
                                            pct((a - b) / reg_var[eregion[e]], False) if reg_var[eregion[e]] else "—",
                                            svg_bar(a - b, emax)]
                                           for e, r, a, b in zip(ent["entity"], erank, ent["a"], ent["b"])], compact=True)),
                  (BOOKMARKS[1][1], chart_view)]})],
        ]}


def page_pnl():
    pl = rows(f"select l.line_id, l.line, l.line_kind k, {ACT} a, {BUD} b {FROM} where {FY} group by all order by min(l.line_sort)")
    growth = dict(con.execute(f"select line_id, growth from mart.budget_drivers where fiscal_year = {YEAR}").fetchall())
    mv = rows(f"select f.period::varchar p, {ACT} - {BUD} v {FROM} where {FY} and {OPER} group by 1 order by 1")
    src = rows(f"""select l.line_id, l.line, {BUD} b {FROM}
                   where {FY} and l.line_kind = 'detail' and f.scenario = 'Budget' group by all order by min(l.line_sort)""")
    # The matrix opens with the worst operating-expense line expanded to its accounts (P&L Drill hierarchy, + button).
    opx = [(a - b, lid) for lid, k, a, b in zip(pl["line_id"], pl["k"], pl["a"], pl["b"]) if "L05" <= lid <= "L11"]
    worst = min(opx)[1]
    acc = rows(f"""select a.account, {ACT} a, {BUD} b from mart.fact_gl f join mart.dim_account a using (account_key)
                   where {FY} and a.line_id = '{worst}' group by 1 order by 1""")
    fmt = lambda name, a, b: [name, usd(a), usd(b), cell(usd(a - b, True), a - b),
                              cell(pct((a - b) / abs(b)) if abs(b) >= 1 else "—", a - b)]
    body, shade = [], []
    for lid, name, k, a, b in zip(pl["line_id"], pl["line"], pl["k"], pl["a"], pl["b"]):
        if k != "detail":
            shade.append(len(body))
        body.append(fmt(("⊟ " if lid == worst else "⊞ " if k == "detail" else "") + name, a, b))
        if lid == worst:
            body += [fmt("  " + n, x, y) for n, x, y in zip(acc["account"], acc["a"], acc["b"])]
    key = [(lid, name, a, b) for lid, name, a, b in zip(pl["line_id"], pl["line"], pl["a"], pl["b"])
           if lid in ("S1", "S2", "S3", "S4", "S6")]
    return {
        "kpis": [],
        "rows": [[
            ("Profit & loss", "Shaded rows are subtotals. + drills a line down to its accounts; right-click → Drill "
             "through → Account detail", 808,
             table(["Line", "Actual", "Comparison", "Variance", "Variance %"], body, shade, compact=True), "full"),
            ("", "", 496, {"stack": [
                ("Key lines", "▲ favourable / ▼ unfavourable vs the comparison", 236,
                 table(["Line", "Actual", "Variance", "Variance arrow"],
                       [[n, usd(a), cell(usd(a - b, True), a - b), arrow(a - b, b)] for _, n, a, b in sorted(key, key=lambda r: r[1])],
                       compact=True)),
                ("Monthly operating variance", "Fiscal Calendar hierarchy: drill up to quarter or year; line = cumulative "
                 "(visual calculation RUNNINGSUM)", 244,
                 fig([dict(type="bar", name="Variance", x=mv["p"], y=mv["v"],
                           marker=dict(color=[GOOD if v >= 0 else BAD for v in mv["v"]], cornerradius=3),
                           hovertemplate="%{x|%b %Y}: %{y:$,.0f}<extra></extra>"),
                      dict(type="scatter", mode="lines+markers", name="Cumulative variance", x=mv["p"], y=running(mv["v"]),
                           line=dict(color=INK2, width=2), marker=dict(size=4, color=INK2),
                           hovertemplate="Cumulative · %{x|%b %Y}: %{y:$,.0f}<extra></extra>")],
                     xaxis=dict(tickformat="%b", dtick="M1"), showlegend=True, legend=LEGEND)),
                ("Budget assumptions — operating expenses", "Prior-year actual × (1 + growth), via TREATAS; hidden from "
                 "the auditor role", None,
                 table(["Line", "Budget", "Driver growth"],
                       [[ln, usd(b), pct(growth[lid]) if lid in growth else ""]
                        for lid, ln, b in sorted(zip(src["line_id"], src["line"], src["b"]), key=lambda r: r[1])
                        if "L05" <= lid <= "L11"], compact=True))]},
             "full")]]}


def decomp_tree():
    """Decomposition tree as Desktop opens it after three + clicks on the worst bar: Operational variance (at budget rates, excl. FX, as the card) → Entity → Line → Department."""
    path, where, levels = [], f"{FY} and {OPER}", []
    total = one(f"select coalesce({ABR}, 0) - {BUD} {FROM} where {where}")[0]
    for label, col in (("Entity", "e.entity"), ("P&L line", "l.line"), ("Department", "d.department")):
        r = rows(f"select {col} k, coalesce({ABR}, 0) - {BUD} v {FROM} where {where} group by 1 order by 2 limit 5")
        levels.append((label, r))
        where += f" and {col} = '" + r["k"][0].replace("'", "''") + "'"
    mx = max(abs(v) for _, r in levels for v in r["v"])
    bar = lambda v, on: (f'<div class="dt{" on" if on else ""}"><i style="width:{abs(v) / mx * 100:.0f}%;'
                         f'background:{GOOD if v >= 0 else BAD}"></i></div>')
    html = f'<div class="droot">Operational variance (excl. FX) <b>{usd(total, True)}</b></div><div class="dtree">'
    for label, r in levels:
        html += f'<div class="dcol"><h4>{label}</h4>' + "".join(
            f'<span title="{k}">{k}</span><em>{usd(v, True)}</em>{bar(v, i == 0)}'
            for i, (k, v) in enumerate(zip(r["k"], r["v"]))) + "</div>"
    return {"html": html + "</div>"}


def page_drivers():
    bud, fx, opv, act = line("S4", BUD), line("S4", f"{ACT} - {ABR}"), line("S4", f"{ABR} - {BUD}"), line("S4")
    steps = rows(f"select l.line, {ABR} - {BUD} v {FROM} where {FY} and {OPER} group by all order by min(l.line_sort)")
    top = rows(f"""select e.entity, l.line, {ACT} a, {BUD} b, {ACT} - {ABR} fx, {ABR} - {BUD} ov {FROM}
                   where {FY} and {OPER} group by all order by ov limit 9""")
    overrun = one(f"""select sum(v) filter (where v < 0) from (select a.account, sum(f.amount_usd) filter (where f.scenario = 'Actual')
                        - coalesce(sum(f.amount_usd) filter (where f.scenario = 'Budget'), 0) v from mart.fact_gl f
                        join mart.dim_account a using (account_key) join mart.dim_pnl_line l on l.line_id = a.line_id
                        where {FY} and {OPER} group by 1)""")[0]
    ccy = rows(f"""select f.currency_code c, {ACT} - {ABR} v {FROM} where {FY} and {OPER} and f.currency_code <> 'USD'
                   group by 1 order by v""")
    po, pcum, to80, ototal = pareto()
    x = ["Budget", "FX translation"] + steps["line"] + ["Total"]
    y = [bud, fx] + steps["v"] + [0]
    running = [sum(y[:i + 1]) for i in range(len(y) - 1)]
    return {
        "kpis": kpis([("Operating Profit Budget", usd(bud)), ("Operating Profit FX Effect", usd(fx, True), tone(fx)),
                      ("Operational variance (excl. FX)", usd(opv, True), tone(opv)), ("Operating Profit", usd(act)),
                      ("All misses, revenue + cost (no netting)", usd(overrun, True), tone(overrun))]),
        "rows": [
            [("Budget → actual operating profit bridge", "Always vs budget. The last bar (Total) is actual operating profit", 880,
              fig([dict(type="waterfall", x=x, y=y, measure=["absolute"] + ["relative"] * (len(x) - 2) + ["total"],
                        text=[usd(bud)] + [usd(v, True) for v in y[1:-1]] + [usd(act)], textposition="outside", cliponaxis=False,
                        textfont=dict(size=10, color=INK2), connector=dict(line=dict(color="#C3C2B7", width=1)),
                        increasing=dict(marker=dict(color=GOOD)), decreasing=dict(marker=dict(color=BAD)),
                        totals=dict(marker=dict(color=TOTAL)), hovertemplate="%{x}: %{text}<extra></extra>")],
                  xaxis=dict(tickfont=dict(size=10, color=MUTED), tickangle=-35), yaxis=dict(range=[0, max(running) * 1.12]),
                  margin=dict(l=52, r=12, t=16, b=96))),
             ("Where the operational variance sits (excl. FX)", "At budget rates, so the root equals the card; + splits by Entity → P&L line → Department", 424, decomp_tree())],
            [("Biggest operational overruns", "Entity × line, at budget rates; worst first. Australia had no budget", 552,
              table(["Entity", "Line", "Actual", "Budget", "FX", "Operational var."],
                    [[e, ln, usd(a), usd(b), usd(f, True), cell(usd(o, True), o)]
                     for e, ln, a, b, f, o in zip(top["entity"], top["line"], top["a"], top["b"], top["fx"], top["ov"])],
                    left=(0, 1), compact=True)),
             (f"{to80} of {len(po)} accounts = 80% of the {usd(ototal)} opex overrun",
              "Opex accounts only (the 'all misses' card adds revenue + cost of sales); line = cumulative %", 456,
              fig([dict(type="bar", x=[n for n, _ in po], y=[v for _, v in po], marker=dict(color=BAD, cornerradius=2),
                        hovertemplate="%{x}: %{y:$,.0f}<extra></extra>"),
                   dict(type="scatter", mode="lines", x=[n for n, _ in po], y=pcum, yaxis="y2", line=dict(color=INK2, width=2),
                        hovertemplate="%{x}: %{y:.0%} cumulative<extra></extra>")],
                  xaxis=dict(showticklabels=False), yaxis2=dict(overlaying="y", side="right", range=[0, 1.05], tickformat=".0%", tickvals=[0, 0.25, 0.5, 0.75, 1],
                                                                 showgrid=False, tickfont=dict(color=MUTED, size=10)),
                  margin=dict(l=48, r=40, t=8, b=12))),
             ("FX effect by currency", "Operating lines at actual vs budget rate", 280,
              hbars(ccy["c"], ccy["v"], [GOOD if v >= 0 else BAD for v in ccy["v"]], left=48))],
        ]}


def cgroup(entity):
    """Entity[Currency Group]: a Desktop group (calculated column) over Entity[Entity]."""
    return "Non-USD entities" if entity in ("Australia", "Canadian Division", "France", "Germany") else "USD entities"


def trend(y):
    """Least-squares straight line through y (the Analytics pane trend line)."""
    n = len(y)
    xm, ym = (n - 1) / 2, sum(y) / n
    b = sum((i - xm) * (v - ym) for i, v in enumerate(y)) / sum((i - xm) ** 2 for i in range(n))
    return [ym + b * (i - xm) for i in range(n)]


def page_entities():
    ni, fca = line("S6"), line("S6", where=f"{FY} and f.currency_code <> 'USD'")
    ic = -line("L02", where=f"{FY} and f.is_elimination")
    hasb = {k for (k,) in con.execute("select distinct entity_key from mart.fact_gl where scenario = 'Budget'").fetchall()}
    own = rows(f"""select e.region, e.entity_key k, e.entity, e.currency_code, e.effective_ownership o, e.first_period, {ACT} ni {FROM}
                   where {FY} and l.line_id = 'S6' group by all order by min(e.entity_sort)""")
    gs = sum(n * o for n, o in zip(own["ni"], own["o"]))
    fx = rows(f"select currency_code c, period::varchar p, avg_rate r, budget_rate br from mart.fx_rates where year(period) = {YEAR} order by 1, 2")
    move = {c: (sum(b for cc, b in zip(fx["c"], fx["br"]) if cc == c) / sum(r for cc, r in zip(fx["c"], fx["r"]) if cc == c)) - 1
            for c in CCY}
    op, op_fca = line("S4"), line("S4", where=f"{FY} and f.currency_code <> 'USD'")
    shock = 0.10
    ctry = rows(f"select e.country, {ACT} - {BUD} v {FROM} where {FY} and {OPER} and e.country <> 'Group' group by 1 order by 1")
    return {
        "kpis": kpis([("Net Income", usd(ni)), ("Group Share", usd(gs)), ("Non-Controlling Interest", usd(ni - gs)),
                      ("Intercompany Eliminated", usd(ic)), ("Foreign Currency Actual", usd(fca))]),
        "rows": [
            [("Ownership & minority interest", "Net income per entity; group share = net income × effective ownership", 760,
              table(["Entity", "Currency group", "Ownership", "Consolidated from", "Has budget", "Net income",
                     "Group share", "NCI"],
                    [[e, cgroup(e), f"{o:.1%}", p.strftime("%b %Y"), "True" if k in hasb else "False", usd(n), usd(n * o),
                      usd(n - n * o)]
                     for k, e, c, o, p, n in zip(own["k"], own["entity"], own["currency_code"], own["o"],
                                                 own["first_period"], own["ni"])], compact=True)),
             ("Exchange rates", "Local currency per USD, monthly average; dashed = trend line (Analytics pane)", 544,
              lines(months(), [(f"{c} per USD", [r for cc, r in zip(fx["c"], fx["r"]) if cc == c], col, "solid", "%{y:.4f}")
                               for c, col in CCY.items()]
                    + [(f"{c} trend", trend([r for cc, r in zip(fx["c"], fx["r"]) if cc == c]), col, "dash", "%{y:.4f}")
                       for c, col in CCY.items()], yaxis=dict(tickprefix="", tickformat=".2f")))],
            [("", "", 280, {"html": '<h3>What-if: move every non-USD currency</h3><div class="steps">'
                                    + "".join(f'<span class="{"on" if s == 10 else ""}">{s:+d}%</span>' for s in range(-20, 25, 5))
                                    + '</div><h4>How to read it</h4><p class="note">+10% = euro, Canadian and Australian dollar all 10% '
                                      'stronger against USD. Translated profit moves by the same share of the foreign-currency '
                                      'result.</p>'}),
             ("Operating profit under the shock", "Preview shows the +10% setting", 340,
              {"html": "".join(f'<div class="mini"><span>{k}</span><b>{v}</b></div>' for k, v in [
                  ("FX Shock Value", pct(shock)), ("Operating Profit", usd(op)),
                  ("Operating Profit at FX Shock", usd(op + shock * op_fca)), ("FX Shock Impact", usd(shock * op_fca, True))])}),
             ("Currency move vs budget rate", "+ = local currency stronger than planned → translates to more USD", 324,
              hbars(list(move), list(move.values()), ACTUAL, text=[pct(v) for v in move.values()], left=48)),
             ("Operating variance by country", "Azure Maps, filled by country; teal = ahead of the comparison", 328,
              fig([dict(type="choropleth", locationmode="country names", locations=ctry["country"], z=[1 if v >= 0 else 0
                        for v in ctry["v"]], text=[usd(v, True) for v in ctry["v"]], zmin=0, zmax=1, showscale=False,
                        colorscale=[[0, BAD], [1, GOOD]], marker=dict(line=dict(color="#FFFFFF", width=0.6)),
                        hovertemplate="%{location}: %{text}<extra></extra>")],
                  geo=dict(projection=dict(type="natural earth"), showframe=False, showcoastlines=False, showland=True,
                           landcolor="#ECEBE6", bgcolor="rgba(0,0,0,0)", lataxis=dict(range=[-48, 72]),
                           lonaxis=dict(range=[-135, 160])), margin=dict(l=0, r=0, t=0, b=0)))],
        ]}


def page_costs():
    opx, opxv, ns = -line("S3"), line("S3") - line("S3", BUD), line("S1")
    sal = -line("L05")
    hc = rows(f"""select period::varchar p, sum(headcount) h from mart.fact_headcount
                  where year(period) in ({YEAR - 1}, {YEAR}) group by 1 order by 1""")
    cur = hc["h"][12:]
    ahc = sum(cur) / len(cur)
    dept = rows(f"""select d.department, -{ACT} spend, {ACT} - {BUD} v {FROM} where {FY} and l.line_id = 'S3'
                    group by all order by spend desc""")
    dhc = rows(f"""select d.department, avg(h) filter (where y = {YEAR}) h, avg(h) filter (where y = {YEAR - 1}) py from (
                     select department_key, year(period) y, period, sum(headcount) h from mart.fact_headcount group by all)
                   join mart.dim_department d using (department_key) group by 1""")
    hcd = {d: (h, py) for d, h, py in zip(dhc["department"], dhc["h"], dhc["py"])}
    lv = rows(f"""select l.line, {ACT} - {BUD} v {FROM} where {FY} and l.line_id between 'L05' and 'L11'
                  group by all order by v""")
    # Opex Rolling 12M = DATESINPERIOD(-12 months) from the last date in context (Dec 2013) = the 12 FY months here.
    roll = -one(f"select {ACT} {FROM} where l.line_id = 'S3' and f.period > date '{YEAR}-12-01' - interval 12 month "
                f"and f.period <= date '{YEAR}-12-01'")[0]
    # Headcount Plan is per region; Entity[Region] reaches it through a many-to-many relationship, so only regions
    # that exist on the Entity side appear (the planned "Asia Operations" region does not).
    hp = rows(f"""with c as (select e.region, sum(h.headcount) c from mart.fact_headcount h join mart.dim_entity e using (entity_key)
                             where h.period = (select max(period) from mart.fact_headcount where year(period) = {YEAR}) group by 1),
                   p as (select region, sum(planned_heads) p from mart.headcount_plan where fiscal_year = {YEAR} group by 1)
                 select region, c, p from c left join p using (region) order by region""")
    share_total = sum(dept["spend"])
    plan_total = one(f"select sum(planned_heads) from mart.headcount_plan where fiscal_year = {YEAR}")[0]
    return {
        "kpis": kpis([("Opex Spend", usd(opx)), ("Operating Expenses Variance", usd(opxv, True), tone(opxv)),
                      ("Avg Headcount", f"{ahc:,.0f}"), ("Cost per Head", usd(opx / ahc)), ("Salary per Head", usd(sal / ahc)),
                      ("Sales per Head", usd(ns / ahc)), ("Opex Rolling 12M", usd(roll))]),
        "rows": [
            [("Operating expenses by department", "USD, actual", 420,
              hbars(dept["department"], dept["spend"], ACTUAL, text=[usd(v) for v in dept["spend"]], left=200)),
             ("Headcount", "Month-end heads vs last year; dotted = visual calculation MOVINGAVERAGE (3 months)", 500,
              lines(months(), [("Avg Headcount", cur, ACTUAL, "solid", "%{y:,.0f}"),
                               ("Headcount PY", hc["h"][:12], COMPARE, "solid", "%{y:,.0f}"),
                               ("3-month average", moving_avg(cur, 3), OUTLOOK, "dot", "%{y:,.1f}")],
                    yaxis=dict(tickprefix="", tickformat=",.0f", rangemode="tozero"))),
             ("Heads vs plan", "Closing heads vs year-end plan (SYNTHETIC), by region; total includes planned Asia Operations", 368,
              table(["Region", "Heads closing", "Heads plan", "Heads vs plan"],
                    [[r, f"{c:,.0f}", f"{p:,.0f}" if p is not None else "", f"{c - p:+,.0f}".replace("-", "−")
                      if p is not None else ""] for r, c, p in zip(hp["region"], hp["c"], hp["p"])]
                    + [["Total", f"{sum(hp['c']):,.0f}", f"{plan_total:,.0f}", f"{sum(hp['c']) - plan_total:+,.0f}"]],
                    shade=[len(hp["region"])]))],
            [("Cost variance by line", "Orange = overspend; right-click a bar: drill through", 360,
              hbars(lv["line"], lv["v"], [GOOD if v >= 0 else BAD for v in lv["v"]], left=170, pad=(1.9, 1.3))),
             ("Department scorecard", "Rank = visual calculation RANK; share = of all departments (REMOVEFILTERS)", 600,
              table(["Department", "Avg heads", "Heads PY", "Opex spend", "Share", "Cost/head", "Opex var.", "Rank"],
                    [[d, f"{hcd.get(d, (0, 0))[0] or 0:,.0f}", f"{hcd.get(d, (0, 0))[1] or 0:,.0f}", usd(s),
                      pct(s / share_total, False),
                      usd(s / hcd[d][0]) if hcd.get(d, (0,))[0] else "—", cell(usd(v, True), v), r]
                     for d, s, v, r in zip(dept["department"], dept["spend"], dept["v"],
                                           dense_rank(dept["spend"], reverse=True))])),
             ("Accounts by spend class (ABC)", "A = top 70% of opex, B = next 20%, C = rest; follows slicers", 328,
              table(["Account", "Opex spend", "Spend class"], [[n, usd(v), c] for n, v, c in spend_class()], compact=True))],
        ]}


def page_forecast():
    fop = "select {g} sum(fc.amount_usd) v from mart.forecast fc join mart.dim_pnl_line l using (line_id) " \
          f"where fc.fiscal_year = {YEAR} and l.is_operating {{w}} group by all"
    fm = dict(one_row for one_row in con.execute(fop.format(g="fc.period::varchar p,", w="")).fetchall())
    act = rows(f"select f.period::varchar p, {ACT} a, {BUD} b {FROM} where {FY} and l.line_id = 'S4' group by 1 order by 1")
    op, fc = line("S4"), sum(fm.values())
    subs = rows(f"""select fc.entity_code, fc.submitted, fc.is_late_submission late, sum(fc.amount_usd) v
                    from mart.forecast fc join mart.dim_pnl_line l using (line_id)
                    where fc.fiscal_year = {YEAR} and l.is_operating group by all order by 1""")
    ent = rows(f"""with a as (select e.entity, e.entity_key, {ACT} a {FROM} where {FY} and l.line_id = 'S4' group by all),
                        f as (select entity_key, sum(amount_usd) f from mart.forecast join mart.dim_pnl_line using (line_id)
                              where fiscal_year = {YEAR} and is_operating group by 1)
                   select entity, coalesce(a, 0) - coalesce(f, 0) v from a left join f using (entity_key) order by v""")
    late = sum({c: l for c, l in zip(subs["entity_code"], subs["late"])}.values())
    return {
        "kpis": kpis([("Operating Profit Forecast", usd(fc)), ("Operating Profit", usd(op)),
                      ("Operating Profit vs Forecast", usd(op - fc, True), tone(op - fc)),
                      ("Entities Submitted", str(len(set(subs["entity_code"])))), ("Late Submissions", str(late))]),
        "rows": [
            [("Monthly operating profit: actual, budget, reforecast", "Reforecast exists for FY2013 only", 880,
              lines(months(), [("Operating Profit", act["a"], ACTUAL, "solid", "%{y:$,.0f}"),
                               ("Operating Profit Budget", act["b"], COMPARE, "solid", "%{y:$,.0f}"),
                               ("Operating Profit Forecast", [fm.get(m) for m in months()], OUTLOOK, "solid", "%{y:$,.0f}")])),
             ("Submissions", "One row per entity block; deadline 10 Jan", 424,
              table(["Entity", "Submitted", "Late", "OP forecast"],
                    [[c, d.strftime("%d %b"), str(bool(l)), usd(v)]
                     for c, d, l, v in zip(subs["entity_code"], subs["submitted"], subs["late"], subs["v"])], compact=True))],
            [("Actual vs reforecast by entity", "Operating profit; teal = beat the reforecast", 760,
              hbars(ent["entity"], ent["v"], [GOOD if v >= 0 else BAD for v in ent["v"]])),
             ("", "", 544, {"html": '<h4>How the files get in (Power Query)</h4><p class="note">Folder.Files over '
                                    '<code>seeds/forecast</code> → one parse function per file: skip 2 title rows, promote '
                                    'headers, fill down the entity, "n/a"/"-" → null, drop Total, unpivot Jan–Dec, type with '
                                    'the file\'s locale (Germany = de-DE, decimal comma) → append → split "CA01 - Canadian '
                                    'Division" → merge to Entity and P&amp;L Line.</p><p class="note" style="margin-top:8px">'
                                    'The files are SYNTHETIC: prior-year actual × a seeded adjustment (Australia: FY2013 '
                                    'actual, no prior year).</p>'})],
        ]}


def page_account():
    """Drillthrough page, shown for the operating-expense line with the worst variance (the first bar a user
    would right-click on Costs & Headcount)."""
    worst = rows(f"""select l.line_id, l.line, {ACT} - {BUD} v {FROM} where {FY} and l.line_id between 'L05' and 'L11'
                     group by all order by v limit 1""")
    lid, lname = worst["line_id"][0], worst["line"][0]
    acc = f"{FROM} join mart.dim_account a on a.account_key = f.account_key"
    where = f"{FY} and l.line_id = '{lid}'"
    act, bud = line(lid), line(lid, BUD)
    var = act - bud
    ac = rows(f"""select a.account_code, a.account, {ACT} a, {BUD} b {acc} where {where} group by all
                  order by {ACT} - {BUD}""")
    av = [a - b for a, b in zip(ac["a"], ac["b"])]
    mv = rows(f"select f.period::varchar p, {ACT} - {BUD} v {FROM} where {where} group by 1 order by 1")
    ent = rows(f"select e.entity, {ACT} - {BUD} v {FROM} where {where} and e.entity_key <> 99 group by 1 order by v")
    by_post = one(f"select {ACT} {FROM} where year(f.posting_date) = {YEAR} and l.line_id = '{lid}'")[0] or 0
    posted = one(f"select count(distinct f.account_key) filter (where f.scenario = 'Actual') {FROM} where {where}")[0]
    jr = rows(f"""select j.journal_id, j.posting_date, e.entity, a.account, j.amount_usd from mart.gl_journal j
                  join mart.bridge_line_account b using (account_key) join mart.dim_entity e using (entity_key)
                  join mart.dim_account a using (account_key) where year(j.period) = {YEAR} and b.line_id = '{lid}'
                  order by j.amount_usd limit 12""")
    # [Is Variance Outlier]: |variance %| above the P95 of every account × entity × month cell under the drilled line.
    g = rows(f"""select a.account, e.entity, f.period p, {ACT} a, {BUD} b {acc} where {where} group by all""")
    cells = [(n, e, p, (a or 0) - b, ((a or 0) - b) / abs(b)) for n, e, p, a, b in zip(g["account"], g["entity"], g["p"], g["a"], g["b"])
             if abs(b) >= 0.5]
    ps = sorted(abs(c[4]) for c in cells)
    k = (len(ps) - 1) * 0.95  # PERCENTILEX.INC: linear interpolation between ranks
    p95 = ps[int(k)] + (ps[min(int(k) + 1, len(ps) - 1)] - ps[int(k)]) * (k - int(k))
    outl = sorted((c for c in cells if abs(c[4]) > p95), key=lambda c: c[3])
    per_acc = {}  # [Outlier Cells] per account row
    for c in outl:
        per_acc[c[0]] = per_acc.get(c[0], 0) + 1
    return {
        "note": f'<b>Drillthrough page.</b> In Power BI it is hidden from the navigation and opened by right-clicking a P&amp;L '
                f'line → <i>Drill through → Account detail</i>; the line arrives as the page filter. This preview shows it for '
                f'<b>{lname}</b>, the operating-expense line furthest behind budget. '
                f'<a href="#costs">← Back</a> (the report page has a Back button).',
        "kpis": kpis([("Actual", usd(act)), ("Comparison", usd(bud)), ("Variance", usd(var, True), tone(var)),
                      ("Variance %", pct(var / abs(bud)) if bud else "—", tone(var)),
                      ("Cut-off Drift", usd(act - by_post, True)), ("Accounts Posted", str(posted))]),
        "rows": [[
            (f"Accounts under {lname}",
             "Rank 1 = worst miss (RANKX); red row = holds a P95 outlier cell (conditional formatting); bar = SVG", 760,
             table(["Code", "Account", "Rank", "Actual", "Comparison", "Variance", "Outliers", "Variance bar"],
                   [[c, n, r, usd(a), usd(b), cell(usd(v, True), v), str(per_acc.get(n, 0)), svg_bar(v, max(map(abs, av)))]
                    for c, n, r, a, b, v in zip(ac["account_code"], ac["account"], dense_rank(av), ac["a"], ac["b"], av)],
                   left=(0, 1), compact=True, flag=[i for i, n in enumerate(ac["account"]) if per_acc.get(n)])),
            ("Variance by month", "Columns: month (teal = favourable); line: running total (WINDOW measure)", 544,
             fig([dict(type="bar", name="Variance", x=mv["p"], y=mv["v"],
                       marker=dict(color=[GOOD if v >= 0 else BAD for v in mv["v"]], cornerradius=3),
                       hovertemplate="%{x|%b %Y}: %{y:$,.0f}<extra></extra>"),
                  dict(type="scatter", mode="lines+markers", name="Variance Running Total", x=mv["p"], y=running(mv["v"]),
                       line=dict(color=INK2, width=2), marker=dict(size=4, color=INK2),
                       hovertemplate="Running total · %{x|%b %Y}: %{y:$,.0f}<extra></extra>")],
                 xaxis=dict(tickformat="%b", dtick="M1"), showlegend=True, legend=LEGEND))],
            [("Journal postings", "GL Journal: one row per posting (DirectQuery in Fabric); largest debit first", 520,
              table(["Journal ID", "Posting date", "Entity", "Account", "Journal amount"],
                    [[i, d.strftime("%d %b %Y"), e, a, usd(v)] for i, d, e, a, v in
                     zip(jr["journal_id"], jr["posting_date"], jr["entity"], jr["account"], jr["amount_usd"])],
                    left=(1, 2, 3), compact=True)),
             (f"Variance outliers (above P95 = {p95 * 100:.0f}%)",
              f"|Var %| > P95 of {len(cells)} account × entity × month cells; {min(6, len(outl))} of {len(outl)} shown (scrolls)", 416,
              table(["Account", "Entity", "Month", "Variance", "Variance %"],
                    [[n, e, p.strftime("%b %Y"), cell(usd(v, True), v), cell(pct(r), v)] for n, e, p, v, r in outl[:6]],
                    left=(0, 1, 2), compact=True)),
             ("Variance by entity", "Worst first", 352,
              hbars(ent["entity"], ent["v"], [GOOD if v >= 0 else BAD for v in ent["v"]], left=124, pad=(2.0, 1.9)))]]}


def page_tooltip():
    """Report-page tooltip (320 × 240), shown for the entity with the worst operating variance."""
    e = rows(f"""select e.entity, {ACT} a, {BUD} b {FROM} where {FY} and l.line_id = 'S4' and e.entity_key <> 99
                 group by 1 order by {ACT} - {BUD} limit 1""")
    name, a, b = e["entity"][0], e["a"][0], e["b"][0]
    v = a - b
    cards = [("Actual", usd(a), ""), ("Comparison", usd(b), ""), ("Variance", usd(v, True), tone(v)),
             ("Variance %", pct(v / abs(b)) if b else "—", tone(v))]
    tip = ('<div class="tooltip-page"><h4>Variance detail · ' + name + '</h4><div class="tt-grid">'
           + "".join(f'<div class="mini"><span>{k}</span><b class="{t}">{x}</b></div>' for k, x, t in cards)
           + "</div></div>")
    return {
        "note": "<b>Tooltip page</b> (320 × 240, page type Tooltip, hidden). Power BI shows it instead of the default "
                "tooltip when you hover a bar in <i>Operating variance by …</i> (Overview), the entity chart view, or "
                "<i>Cost variance by line</i> (Costs). The bar's filter flows in, so the four cards are for that bar. "
                f"Shown here for <b>{name}</b>, the entity furthest behind budget.",
        "kpis": [],
        "rows": [[("As it appears on hover", "", 352, {"html": tip}),
                  ("Wiring", "", 968, {"html": '<p class="note">Each source visual carries <code>visualTooltip: {type: '
                                              '"Canvas", section: "tooltip"}</code>; the page has <code>pageBinding.type = '
                                              '"Tooltip"</code> and <code>visibility = "HiddenInViewMode"</code>.</p>'})]]}


BUILD = {"overview": page_overview, "pnl": page_pnl, "drivers": page_drivers, "entities": page_entities, "costs": page_costs, "forecast": page_forecast,
         "account": page_account, "tooltip": page_tooltip}

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Group Finance — Actual vs Budget</title>
<style>
:root{--ink:%(ink)s;--ink2:%(ink2)s;--muted:%(muted)s;--hair:#E6E4DD;--surface:#FCFCFB;--plane:#F4F4F1;
--side:%(side)s;--side-text:%(side_text)s;--side-line:%(side_line)s;--good:%(good)s;--bad:%(bad)s}
*{box-sizing:border-box}
body{margin:0;background:#E9E8E3;font-family:"Segoe UI",system-ui,-apple-system,sans-serif;color:var(--ink)}
.app{display:grid;grid-template-columns:232px 1fr;min-height:100vh;max-width:1600px;margin:0 auto;background:var(--plane)}
aside{background:var(--side);border-right:3px solid var(--good);padding:28px 12px;display:flex;flex-direction:column;position:sticky;top:0;height:100vh}
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
.good{color:var(--good)!important}.bad{color:var(--bad)!important}
.row{display:flex;gap:16px;margin-bottom:16px}
.panel{padding:12px 14px 8px;min-width:0;display:flex;flex-direction:column}
.panel h3{margin:0;font-size:15px;font-weight:600}
.panel>p{margin:2px 0 6px;font-size:12px;color:var(--muted)}
.chart{flex:1;min-height:0;overflow:auto}
.row.r2 .panel{height:332px}.row.r3 .panel{height:316px}.row .panel.full{height:776px}
.stack{display:flex;flex-direction:column;gap:16px;background:none;border:0;padding:0}
.stack .panel{flex:none}.stack .panel.grow{flex:1;min-height:0}
.dtree{display:flex;gap:12px;font-size:11px}.dcol{flex:1;min-width:0;display:flex;flex-direction:column;gap:0}
.droot{font-size:12px;color:var(--ink2);margin-bottom:6px}.droot b{font-size:15px;color:var(--bad);margin-left:4px}.dcol span{overflow-wrap:anywhere;line-height:1.25;color:var(--ink2)}
.dcol em{font-style:normal;font-size:10px;color:var(--muted)}.dt{height:7px;background:#F1F0EC;border-radius:3px;margin:2px 0 3px}
.dt i{display:block;height:100%%;border-radius:3px}.dt.on{outline:1.5px solid var(--ink);outline-offset:1px}
table{width:100%%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th{font-size:11px;color:var(--muted);font-weight:600;white-space:nowrap;text-align:right;padding:7px 6px;border-bottom:1px solid var(--hair);position:sticky;top:0;background:var(--surface)}
td{text-align:right;padding:7px 6px;border-bottom:1px solid #ECEBE6}
th:first-child,td:first-child{font-weight:600}td:not(:first-child){white-space:nowrap}td.l,th.l{text-align:left}
tr.shade td{background:#EAF0EE;font-weight:600}tr.flag td{background:#FBE3DF}
table.compact td{padding:4px 6px}
.headline{font-size:17px;line-height:1.45;margin:6px 0 14px}
.split{display:flex;gap:16px;align-items:stretch;margin-top:auto}.split>*{flex:1}
.mini{border:1px solid var(--hair);border-radius:10px;padding:10px 12px;margin-bottom:8px}
.mini span{display:block;font-size:11px;color:var(--muted)}.mini b{font-size:20px;font-weight:600}
.steps{display:flex;flex-wrap:wrap;gap:4px;margin:10px 0 14px}.steps span{border:1px solid var(--hair);border-radius:6px;padding:3px 7px;font-size:11px;color:var(--ink2)}
.steps span.on{background:var(--side);color:#fff;border-color:var(--side)}
h4{margin:0 0 4px;font-size:12px}.note{font-size:12px;color:var(--ink2);line-height:1.5;margin:0}
.banner{background:#EAF0EE;border:1px solid #CFE0DB;border-radius:10px;padding:10px 14px;font-size:13px;line-height:1.5;margin-bottom:16px;color:var(--ink2)}
.banner a{color:var(--good);font-weight:600}
nav small{display:block;margin:18px 16px 4px;font-size:10px;letter-spacing:.12em;color:var(--side-text);opacity:.7}
nav button.hidden-page{font-size:13px;opacity:.8}
.toggle{display:flex;gap:6px;margin:0 0 8px}.toggle button{all:unset;cursor:pointer;border:1px solid var(--hair);border-radius:6px;padding:3px 10px;font-size:12px;color:var(--ink2);background:var(--plane)}
.toggle button[aria-pressed=true]{background:var(--side);border-color:var(--side);color:#fff}
.tooltip-page{width:320px;height:240px;border:1px solid var(--hair);border-radius:8px;background:#fff;padding:10px 12px;box-shadow:0 4px 14px rgba(0,0,0,.08)}
.tt-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:6px}.tt-grid .mini{margin:0;height:88px}
code{font-size:12px;background:var(--plane);padding:1px 4px;border-radius:4px}
@media (max-width:1100px){.app{grid-template-columns:1fr}aside{position:static;height:auto}.row{flex-direction:column}.panel{width:100%%!important}.kpis{grid-auto-flow:row;grid-template-columns:repeat(2,1fr)}}
</style></head>
<body><div class="app">
<aside><div class="brand"><small>ADVENTURE WORKS</small><b>Group Finance</b></div><nav id="nav"></nav>
<footer>Source: Microsoft AdventureWorksDW finance ledger (MIT)<br>Pipeline: Python · DuckDB SQL · Power BI (TMDL + PBIR)<br>Static preview of the Power BI report</footer></aside>
<main id="main"></main></div>
<script>%(plotly)s</script>
<script>
const PAGES = %(pages)s;
const CHIPS = [['Year','%(year)s'],['Month','All'],['Entity','All'],['Compare To','Budget'],['Time View','Period']];
const nav = document.getElementById('nav'), main = document.getElementById('main');
function el(tag, attrs = {}, html = '') { const e = document.createElement(tag); Object.assign(e, attrs); e.innerHTML = html; return e; }
function tableHtml(b) {
  return '<table class="' + (b.compact ? 'compact' : '') + '"><thead><tr>' + b.columns.map((h, j) => `<th class="${b.left.includes(j) ? 'l' : ''}">${h}</th>`).join('') + '</tr></thead><tbody>' +
    b.rows.map((row, i) => `<tr class="${b.shade.includes(i) ? 'shade' : ''}${(b.flag || []).includes(i) ? ' flag' : ''}">` +
      row.map((v, j) => { const l = b.left.includes(j) ? ' l' : '';
        return v && v.v !== undefined ? `<td class="${v.t}${l}">${v.v}</td>` : `<td class="${l}">${v}</td>`; }).join('') + '</tr>').join('') +
    '</tbody></table>';
}
function panel(title, sub, body, cls) {
  const p = el('div', {className: 'panel ' + (cls || '')}, (title ? `<h3>${title}</h3>` : '') + (sub ? `<p>${sub}</p>` : ''));
  const c = el('div', {className: 'chart'}); p.append(c);
  if (body.toggle) {
    const bar = el('div', {className: 'toggle'}), view = el('div', {className: 'chart'});
    const show = i => { [...bar.children].forEach((b, j) => b.setAttribute('aria-pressed', i === j)); view.innerHTML = '';
      const b = body.toggle[i][1];
      if (b.columns) view.innerHTML = tableHtml(b);
      else requestAnimationFrame(() => Plotly.newPlot(view, b.data, {...b.layout, autosize: true}, {displayModeBar: false, responsive: true})); };
    body.toggle.forEach(([label], i) => { const b = el('button', {}, label); b.onclick = () => show(i); bar.append(b); });
    c.style.display = 'flex'; c.style.flexDirection = 'column'; c.append(bar, view); show(0);
  } else if (body.columns) c.innerHTML = tableHtml(body);
  else if (body.html) { c.innerHTML = body.html; c.style.display = 'flex'; c.style.flexDirection = 'column'; }
  else requestAnimationFrame(() => Plotly.newPlot(c, body.data, {...body.layout, autosize: true}, {displayModeBar: false, responsive: true}));
  return p;
}
function render(id) {
  const page = PAGES.find(p => p.id === id);
  [...nav.children].forEach(b => b.setAttribute('aria-current', b.dataset.id === id));
  main.innerHTML = '';
  const head = el('header');
  head.append(el('div', {}, `<h1>${page.title}</h1><p>${page.subtitle}</p>`));
  head.append(el('div', {className: 'chips'}, CHIPS.map(([k, v]) => `<div class="chip">${k}<b>${v}</b></div>`).join('')));
  main.append(head);
  if (page.note) main.append(el('div', {className: 'banner'}, page.note));
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
  history.replaceState(null, '', '#' + id);
}
PAGES.forEach(p => {
  if (p.hidden && !nav.querySelector('small')) nav.append(el('small', {}, 'HIDDEN IN POWER BI'));
  const b = el('button', {className: p.hidden ? 'hidden-page' : ''}, p.name); b.dataset.id = p.id; b.onclick = () => render(p.id); nav.append(b);
});
addEventListener('hashchange', () => render(location.hash.slice(1)));
render((location.hash || '#overview').slice(1));
</script></body></html>"""


def main():
    pages = []
    for pid, name, title, subtitle, *ptype in PAGES + HIDDEN_PAGES:
        pages.append({"id": pid, "name": name, "title": title or name, "subtitle": subtitle or "", "hidden": bool(ptype),
                      **BUILD[pid]()})
        print(f"  ✓ {name}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(HTML % {"plotly": get_plotlyjs(), "pages": json.dumps(pages, default=str), "year": YEAR,
                           "ink": INK, "ink2": INK2, "muted": MUTED, "side": SIDE, "side_text": SIDE_TEXT,
                           "side_line": SIDE_LINE, "good": GOOD, "bad": BAD})
    print(f"  ✓ wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
