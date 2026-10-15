"""Static HTML twin of the Power BI report — same pages, layout, palette and metric definitions.

    uv run python src/build_preview.py      -> reports/dashboard.html (self-contained, opens offline)

Power BI Desktop is Windows-only; this lets the dashboard be reviewed anywhere.
Every number is computed in DuckDB with the same rules as the DAX measures
(matured loans for default/loss/ROI, live book for expected loss).
"""
import json
from pathlib import Path

import duckdb

import glossary
from plotly.offline import get_plotlyjs

from build_report import BLUE, GRAY, HEAT, INK, INK2, MUTED, NAVY, NAVY_TEXT, ORANGE, OUTCOME, PAGES, TIER, VINTAGE, COHORT_COLORS
from build_report import GLOSSARY_TERMS, HELP, HIDDEN_PAGES

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "dashboard.html"
con = duckdb.connect(str(ROOT / "data" / "warehouse" / "lendingclub.duckdb"), read_only=True)

M = "filter (where is_resolved and is_matured)"
DR = f"sum(is_default) {M} / nullif(count(*) {M}, 0)"
ROI = f"sum(net_profit) {M} / nullif(sum(funded_amount) {M}, 0)"
LOSS = f"sum(net_loss) {M} / nullif(sum(funded_amount) {M}, 0)"
IR = "sum(funded_amount * int_rate) / sum(funded_amount)"
LGD = "sum(net_loss) / nullif(sum(exposure_at_default), 0)"
FROM = "from mart.fact_loans f join mart.dim_grade g using (sub_grade) join mart.dim_purpose p using (purpose) join mart.dim_state s using (state)"


def rows(sql):
    cur = con.execute(sql)
    names, data = [d[0] for d in cur.description], cur.fetchall()
    return {n: [r[i] for r in data] for i, n in enumerate(names)}


def one(sql):
    return con.execute(sql).fetchone()


# ------------------------------------------------------------------ formats
def usd(v):
    for div, unit in ((1e9, "bn"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:,.1f}{unit}"
    return f"${v:,.0f}"


pct = lambda v, d=1: f"{v * 100:.{d}f}%"
num = lambda v: f"{v:,.0f}"

# ------------------------------------------------------------------ figures
AXIS = dict(showgrid=False, zeroline=False, linecolor="#E6E4DD", tickfont=dict(color=MUTED, size=10), title=None)
YAXIS = dict(showgrid=True, gridcolor="#ECEBE6", zeroline=False, tickfont=dict(color=MUTED, size=10), title=None)
LEGEND = dict(orientation="h", y=1.12, x=0, font=dict(color=INK2, size=10), bgcolor="rgba(0,0,0,0)")


def fig(traces, **layout):
    base = dict(margin=dict(l=48, r=12, t=8, b=36), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(family="Segoe UI, system-ui, -apple-system, sans-serif", color=INK2, size=11),
                xaxis=AXIS, yaxis=YAXIS, showlegend=False, hoverlabel=dict(bgcolor="#fff", bordercolor="#E6E4DD",
                                                                          font=dict(color=INK)))
    for k, v in layout.items():
        base[k] = {**base[k], **v} if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return {"data": traces, "layout": base}


def bars(x, y, fmt=".1%", horizontal=False, colors=BLUE, **layout):
    t = dict(type="bar", marker=dict(color=colors, cornerradius=4), texttemplate=f"%{{{'x' if horizontal else 'y'}:{fmt}}}",
             textposition="outside", textfont=dict(size=10, color=INK2), cliponaxis=False)
    if horizontal:
        t.update(x=y, y=x, orientation="h", hovertemplate=f"%{{y}}: %{{x:{fmt}}}<extra></extra>")
        return fig([t], **{"yaxis": dict(autorange="reversed", showgrid=False, tickfont=dict(color=INK2, size=10)),
                          "xaxis": dict(visible=False), "margin": dict(l=120, r=48, t=8, b=8), **layout})
    t.update(x=x, y=y, hovertemplate=f"%{{x}}: %{{y:{fmt}}}<extra></extra>")
    return fig([t], **{"yaxis": dict(visible=False), "margin": dict(l=12, r=12, t=20, b=36), **layout})


def heat_colorscale():
    return [[0, HEAT[0]], [0.5, HEAT[1]], [1, HEAT[2]]]


# -------------------------------------------------------------------- pages
def kpi_row(items):
    return [{"label": label, "value": value} for label, value in items]


def views(d, default=None):
    """Segmented buttons that swap the panel body — the twin of a slicer / bookmark / drill level."""
    return {"views": d, "default": default or next(iter(d))}


def cum_bars(x, y, name, y2name):
    run, acc = [], 0
    for v in y:
        acc += v or 0
        run.append(acc)
    return fig([dict(type="bar", name=name, x=x, y=y, marker=dict(color=BLUE, cornerradius=3),
                     hovertemplate="%{x}: $%{y:,.3s}<extra></extra>"),
                dict(type="scatter", mode="lines", name=y2name, x=x, y=run, yaxis="y2", line=dict(color=ORANGE, width=2),
                     hovertemplate="%{x} running total: $%{y:,.3s}<extra></extra>")],
               showlegend=True, legend=LEGEND, xaxis=dict(type="category", tickfont=dict(size=9, color=MUTED)),
               yaxis=dict(tickprefix="$", exponentformat="B"),
               yaxis2=dict(overlaying="y", side="right", showgrid=False, tickprefix="$", exponentformat="B",
                           tickfont=dict(color=MUTED, size=10)),
               margin=dict(l=48, r=48, t=28, b=36))


# ------------------------------------------------ business analysis (D): same definitions as the DAX measures
def abc(funded):
    """Purpose ABC: class of the cumulative share before the item (its first dollar): A < 80%, B < 95%, C."""
    tot, before, out = sum(funded), 0, []
    for v in funded:
        out.append("A" if before / tot < 0.80 else "B" if before / tot < 0.95 else "C")
        before += v
    return out


def mix_rate():
    """Mix / Rate Effect: latest matured vintage year vs the year before, weights = share of matured loans per grade."""
    y = one(f"select max(vintage_year) {FROM} where is_matured")[0]
    t = rows(f"select g.grade, vintage_year y, count(*) {M} n, sum(is_default) {M} d {FROM} where vintage_year in ({y}, {y - 1}) group by all")
    n = {yy: sum(v for v, k in zip(t["n"], t["y"]) if k == yy) for yy in (y, y - 1)}
    w = {(g, yy): v / n[yy] for g, yy, v in zip(t["grade"], t["y"], t["n"])}
    r = {(g, yy): d / v for g, yy, v, d in zip(t["grade"], t["y"], t["n"], t["d"])}
    grades = sorted(set(t["grade"]))
    mix = sum((w[g, y] - w[g, y - 1]) * r[g, y - 1] for g in grades)
    rate = sum(w[g, y] * (r[g, y] - r[g, y - 1]) for g in grades)
    return y, mix, rate


def page_overview():
    loans, funded, dr, loss, roi = one(f"select count(*), sum(funded_amount), {DR}, sum(net_loss), {ROI} {FROM}")
    # Funded 12M Rolling: DATESINPERIOD back 12 months from the last calendar date (2019-02-28) -> Mar 2018 – Feb 2019
    last = one("select max(date) from mart.dim_date")[0]
    f12 = one(f"select sum(funded_amount) {FROM} where issue_date > date '{last}' - interval 12 month")[0]
    area = rows(f"""with m as (select distinct month_start from mart.dim_date),
                     f as (select date_trunc('month', issue_date) month_start, sum(funded_amount) v {FROM} group by 1)
                select m.month_start, f.v,
                       avg(f.v) over (order by m.month_start rows between 2 preceding and current row) avg3,
                       sum(f.v) over (partition by date_trunc('quarter', m.month_start) order by m.month_start) qtd,
                       sum(f.v) over (partition by year(m.month_start) order by m.month_start) ytd,
                       sum(f.v) over (order by m.month_start rows between 11 preceding and current row) r12,
                       f.v / lag(f.v, 12) over (order by m.month_start) - 1 yoy
                from m left join f using (month_start)
                where m.month_start >= (select min(month_start) from f) and m.month_start <= (select max(month_start) from f)
                order by 1""")
    xs = [str(d) for d in area["month_start"]]
    # analytics-pane trend = least squares on the date axis
    t = [(d - area["month_start"][0]).days for d in area["month_start"]]
    n, mt, mv = len(t), sum(t) / len(t), sum(area["v"]) / len(t)
    slope = sum((a - mt) * (b - mv) for a, b in zip(t, area["v"])) / sum((a - mt) ** 2 for a in t)
    trend = [mv + slope * (a - mt) for a in t]
    # Funded Trend (LINESTX): least squares on month index year*12+month over the months with loans
    mi = [d.year * 12 + d.month for d in area["month_start"]]
    pts = [(x, v) for x, v in zip(mi, area["v"]) if v is not None]
    mx, my = sum(x for x, _ in pts) / len(pts), sum(v for _, v in pts) / len(pts)
    b1 = sum((x - mx) * (v - my) for x, v in pts) / sum((x - mx) ** 2 for x, _ in pts)
    ftrend = [my + b1 * (x - mx) for x in mi]
    tip = [(f"{y:+.1%}" if y is not None else "—", usd(ytd), usd(r12), usd(q), usd(v), usd(a3), usd(ft))
           for y, ytd, r12, q, v, a3, ft in zip(area["yoy"], area["ytd"], area["r12"], area["qtd"], area["v"], area["avg3"], ftrend)]
    area_traces = [
        dict(type="scatter", mode="lines", name="Funded Amount", x=xs, y=area["v"], line=dict(width=2, color=BLUE),
             customdata=tip, hovertemplate="%{x|%b %Y}: $%{y:,.3s}<br>YoY %{customdata[0]} · YTD %{customdata[1]} · "
                                           "12M %{customdata[2]}<br>QTD %{customdata[3]} · MTD %{customdata[4]} · "
                                           "3M avg %{customdata[5]}<br>LINESTX trend %{customdata[6]}<extra></extra>"),
        dict(type="scatter", mode="lines", name="Trend", x=xs, y=trend, line=dict(width=1.5, color=INK2, dash="dot"),
             hoverinfo="skip")]
    out = rows(f"select outcome, count(*) n {FROM} group by 1 order by min(outcome_sort)")
    purpose = rows(f"""select purpose_label k, {DR} v, sum(is_default) {M} d, sum(funded_amount) f {FROM} group by 1 order by v desc""")
    by_f = sorted(zip(purpose["f"], purpose["k"]), reverse=True)
    cls = dict(zip([k for _, k in by_f], abc([f for f, _ in by_f])))
    pcd = [(cls[k], usd(f)) for k, f in zip(purpose["k"], purpose["f"])]
    share = [d / sum(purpose["d"]) for d in purpose["d"]]
    bridge = one(f"select -sum(funded_amount) {M}, sum(principal_paid) {M}, sum(interest_paid) {M}, sum(late_fees) {M}, sum(net_recoveries) {M} {FROM}")
    term = rows(f"select term, risk_tier, {DR} v {FROM} group by all order by 1, min(risk_tier_sort)")
    term_traces = [dict(type="bar", name=tier, marker=dict(color=col, cornerradius=4),
                        x=[t for t, r in zip(term["term"], term["risk_tier"]) if r == tier],
                        y=[v for v, r in zip(term["v"], term["risk_tier"]) if r == tier],
                        texttemplate="%{y:.0%}", textposition="outside", textfont=dict(size=10, color=INK2),
                        hovertemplate=f"{tier} · %{{x}}: %{{y:.1%}}<extra></extra>") for tier, col in TIER.items()]
    steps = ["Funded", "Principal", "Interest", "Late fees", "Recoveries"]
    purpose_traces = [
        dict(type="bar", orientation="h", name="Default Rate", y=purpose["k"], x=purpose["v"], marker=dict(color=BLUE),
             customdata=pcd, hovertemplate="%{y}: default rate %{x:.1%}<br>ABC class %{customdata[0]} · funded %{customdata[1]}<extra></extra>"),
        dict(type="bar", orientation="h", name="Share of defaults", y=purpose["k"], x=share, marker=dict(color=GRAY),
             hovertemplate="%{y}: %{x:.1%} of all defaults<extra></extra>")]
    # Time Calc matrix at year level: YTD and 12M rolling end on 31 Dec, so they equal Current except 2019
    # (dates Jan–Feb only, no loans): Current/YTD blank, PY = 2018, YoY −100%, 12M = Mar 2018 – Feb 2019.
    yr = dict(con.execute(f"select year(issue_date), sum(funded_amount) {FROM} group by 1").fetchall())
    years = sorted(set(yr) | {last.year})
    tc_rows = []
    for y in years:
        cur, py = yr.get(y), yr.get(y - 1)
        yoy = None if not py else ((cur or 0) - py) / py
        r12 = f12 if y == last.year else cur
        tc_rows.append([str(y), usd(cur) if cur else "", usd(py) if py else "", f"{yoy:+.1%}" if yoy is not None else "",
                        usd(cur) if cur else "", usd(r12) if r12 else ""])
    drill = {}
    for label, expr in (("Year", "year(issue_date)::varchar"),
                        ("Quarter", "year(issue_date) || ' Q' || quarter(issue_date)"),
                        ("Month", "strftime(issue_date, '%Y-%m')")):
        d = rows(f"select {expr} k, sum(funded_amount) v {FROM} group by 1 order by 1")
        drill[label] = cum_bars(d["k"], d["v"], "Funded Amount", "Cumulative funded")
    return {
        "kpis": kpi_row([("Loans Issued", num(loans)), ("Funded Amount", usd(funded)), ("Funded 12M Rolling", usd(f12)),
                         ("Default Rate", pct(dr)), ("Net Credit Loss", usd(loss)), ("Realised ROI", pct(roi))]),
        "rows": [
            [("Monthly originations, trend and forecast",
              "Dotted = trend line. Hover for YoY %, YTD and the LINESTX trend. Power BI adds its own forecast band; "
              "this preview does not draw it", 880,
              fig(area_traces, showlegend=True, legend=LEGEND, yaxis=dict(tickprefix="$", exponentformat="B"))),
             ("Where the money went", "Matured loans, whole book (the term slicer does not filter it)", 424,
              fig([dict(type="waterfall", x=steps + ["Net profit"], y=list(bridge) + [0],
                        measure=["relative"] * 5 + ["total"], text=[usd(v) for v in bridge] + [usd(sum(bridge))],
                        textposition="outside",
                        textfont=dict(size=10, color=INK2), connector=dict(line=dict(color="#C3C2B7", width=1)),
                        increasing=dict(marker=dict(color="#0ca30c")), decreasing=dict(marker=dict(color="#d03b3b")),
                        totals=dict(marker=dict(color=NAVY)), cliponaxis=False,
                        hovertemplate="%{x}: %{y:$,.0f}<extra></extra>")],
                  yaxis=dict(tickprefix="$", exponentformat="B"), xaxis=dict(tickfont=dict(size=9, color=MUTED), tickangle=0),
                  margin=dict(l=48, r=12, t=16, b=28)))],
            [("Where every loan ended up", "Share of loans by status. Click a slice in Power BI to highlight the charts", 430,
              fig([dict(type="pie", hole=0.64, labels=out["outcome"], values=out["n"], sort=False,
                        marker=dict(colors=[OUTCOME[o] for o in out["outcome"]], line=dict(color="#FCFCFB", width=2)),
                        textinfo="percent", textfont=dict(color="#fff", size=11), hovertemplate="%{label}: %{value:,} loans<extra></extra>")],
                  showlegend=True, legend=dict(orientation="h", y=-0.08, font=dict(color=INK2, size=10)),
                  margin=dict(l=8, r=8, t=8, b=8))),
             ("Default rate and share of all defaults, by purpose", "Share = percent of grand total. Hover a bar for its value and ABC class", 440,
              fig(purpose_traces, barmode="group", bargap=0.25, showlegend=True, legend=LEGEND,
                  yaxis=dict(autorange="reversed", showgrid=False, tickfont=dict(color=INK2, size=9)),
                  xaxis=dict(visible=False), margin=dict(l=118, r=40, t=24, b=8))),
             ("Default rate by term and tier", "60-month loans default far more often", 418,
              fig(term_traces, barmode="group", showlegend=True, legend=LEGEND, yaxis=dict(visible=False),
                  margin=dict(l=12, r=12, t=28, b=36)))],
            [("Funded amount through the Time Calc calculation group",
              "Year rows × Current, PY, YoY %, YTD, 12M Rolling. 2019 has only Jan–Feb dates and no loans, so YoY shows −100%",
              560, {"columns": ["Year", "Current", "PY", "YoY %", "YTD", "12M Rolling"], "rows": tc_rows, "compact": True},
              "-scroll"),
             ("Funded per period and running total", "Drill year → quarter → month. Line = running total (RUNNINGSUM)", 744,
              views(drill))],
        ]}


def page_risk():
    ir, dr, lgd, loss, roi, profit = one(f"select {IR}, {DR}, {LGD}, {LOSS}, {ROI}, sum(net_profit) {M} {FROM}")
    sg = rows(f"""with s as (select sub_grade, grade, risk_tier, min(sub_grade_sort) o, {IR} ir, {DR} dr, {LGD} lgd, {ROI} roi,
                                 sum(funded_amount) funded {FROM} group by all)
                  select * from s order by o""")
    avg_dr = sum(sg["dr"]) / len(sg["dr"])  # Sub Grade Avg Default Rate: AVERAGEX over the sub-grades shown
    band = rows(f"select floor(int_rate * 20)::int b, {DR} v {FROM} group by 1 having count(*) {M} > 0 order by 1")
    worst = {r[0]: r[1] for r in con.execute(f"""select grade, arg_max(sub_grade, dr) from
        (select g.grade, sub_grade, {DR} dr {FROM} group by all) group by 1""").fetchall()}
    g = rows(f"select grade, count(*) n, sum(funded_amount) funded, {IR} ir, {DR} dr, {LGD} lgd, {LOSS} loss, {ROI} roi, sum(net_profit) {M} profit {FROM} group by 1 order by 1")
    gt = rows(f"select grade, term, {DR} dr {FROM} group by all order by 1, 2")
    r60 = {gg: v for gg, t, v in zip(gt["grade"], gt["term"], gt["dr"]) if t == "60 months"}
    maxf = max(sg["funded"])
    scatter = []
    for tier, col in TIER.items():
        idx = [i for i, t in enumerate(sg["risk_tier"]) if t == tier]
        scatter.append(dict(type="scatter", mode="markers+text", name=tier,
                            x=[sg["ir"][i] for i in idx], y=[sg["dr"][i] for i in idx], text=[sg["sub_grade"][i] for i in idx],
                            textposition="top center", textfont=dict(size=9, color=INK2),
                            marker=dict(color=col, size=[8 + 34 * (sg["funded"][i] / maxf) ** 0.5 for i in idx],
                                        line=dict(color="#FCFCFB", width=2), opacity=0.9),
                            customdata=[(sg["lgd"][i], sg["roi"][i]) for i in idx],  # = the Grade Tip tooltip page
                            hovertemplate="%{text}<br>Default rate %{y:.1%} · LGD %{customdata[0]:.1%}"
                                          "<br>Realised ROI %{customdata[1]:.1%} · Avg rate %{x:.1%}<extra></extra>"))
    terms = sorted(set(gt["term"]))
    dr_of = {(gg, tt): v for gg, tt, v in zip(gt["grade"], gt["term"], gt["dr"])}
    heat_cols = [f"{t[:2]}m · {k}" for t in terms for k in ("Default", "60M KF")]
    z = [[v for t in terms for v in (dr_of[(grade, t)], r60[grade] if t == "60 months" else None)] for grade in g["grade"]]
    table = {"columns": ["Grade", "Loans", "Funded", "Avg rate", "Default rate", "Worst sub", "Loss rate", "Net profit", "ROI"],
             "rows": [[g["grade"][i], num(g["n"][i]), usd(g["funded"][i]), pct(g["ir"][i], 2), pct(g["dr"][i]),
                       worst[g["grade"][i]], pct(g["loss"][i], 2), usd(g["profit"][i]), pct(g["roi"][i])]
                      for i in range(len(g["grade"]))]}
    # Grade ROI P25 = PERCENTILEX.INC over the 7 grades (inclusive = linear interpolation)
    srt = sorted(g["roi"])
    pos = 0.25 * (len(srt) - 1)
    p25 = srt[int(pos)] + (srt[min(int(pos) + 1, len(srt) - 1)] - srt[int(pos)]) * (pos - int(pos))
    roi_views = {}
    for label, key, sort in (("Tier", "risk_tier", "risk_tier_sort"), ("Grade", "g.grade", "grade_sort"),
                             ("Sub-grade", "sub_grade", "sub_grade_sort")):
        d = rows(f"select {key} k, {ROI} v {FROM} group by 1 order by min({sort})")
        prev = [None] + [b - a for a, b in zip(d["v"], d["v"][1:])]
        roi_views[label] = fig(
            [dict(type="bar", name="Realised ROI", x=d["k"], y=d["v"],
                  marker=dict(color=["#d03b3b" if v < 0 else BLUE for v in d["v"]], cornerradius=3),
                  texttemplate="%{y:.1%}" if len(d["k"]) < 10 else None, textposition="outside",
                  textfont=dict(size=9, color=INK2), cliponaxis=False, hovertemplate="%{x}: ROI %{y:.1%}<extra></extra>"),
             dict(type="bar", name="Versus previous", x=d["k"], y=prev, marker=dict(color=GRAY, cornerradius=3),
                  hovertemplate="%{x}: %{y:+.1%} vs the one before<extra></extra>")],
            barmode="group", showlegend=True, legend=LEGEND, yaxis=dict(visible=False),
            uniformtext=dict(minsize=9, mode="show"), xaxis=dict(type="category", tickfont=dict(size=9, color=MUTED)), margin=dict(l=12, r=12, t=28, b=36),
            shapes=[dict(type="line", xref="paper", x0=0, x1=1, y0=p25, y1=p25, line=dict(color=INK2, dash="dash", width=1))],
            annotations=[dict(xref="paper", x=1, y=p25, text=f"P25 {p25:.1%}", showarrow=False, yanchor="bottom",
                              xanchor="right", font=dict(size=9, color=INK2))])
    pre = rows(f"""select g.grade, term, count(*) filter (where is_resolved and is_matured and outcome = 'Fully Paid'
                       and last_payment_date < issue_date + to_months(cast(split_part(term, ' ', 1) as int))) / count(*) {M} v
                    {FROM} group by all having count(*) {M} > 0 order by 1, 2""")
    prepay = fig([dict(type="bar", name=tm, marker=dict(color=c, cornerradius=3),
                       x=[gg for gg, tt in zip(pre["grade"], pre["term"]) if tt == tm],
                       y=[v for v, tt in zip(pre["v"], pre["term"]) if tt == tm],
                       hovertemplate=f"Grade %{{x}} · {tm}: %{{y:.1%}} prepaid<extra></extra>")
                  for tm, c in (("36 months", BLUE), ("60 months", NAVY))],
                 barmode="group", showlegend=True, legend=LEGEND, yaxis=dict(tickformat=".0%"), margin=dict(l=40, r=8, t=28, b=28))
    out = rows(f"""with b as (select sub_grade, quantile_cont(int_rate, 0.05) lo, quantile_cont(int_rate, 0.95) hi
                              from mart.fact_loans group by 1)
                   select sub_grade, any_value(lo) lo, any_value(hi) hi, count(*) filter (where int_rate < lo or int_rate > hi) n
                   from mart.fact_loans join b using (sub_grade) group by 1 order by n desc, 1""")
    n_out, n_all = sum(out["n"]), one("select count(*) from mart.fact_loans")[0]
    box = rows(f"""select g.grade, quantile_cont(int_rate, [0.05, 0.25, 0.5, 0.75, 0.95]) q {FROM} group by 1 order by 1""")
    q = list(zip(*box["q"]))
    box_traces = [
        dict(type="bar", x=box["grade"], base=q[0], y=[b - a for a, b in zip(q[0], q[4])], width=0.04,
             marker=dict(color=INK2), hoverinfo="skip"),
        dict(type="bar", x=box["grade"], base=q[1], y=[b - a for a, b in zip(q[1], q[3])], width=0.45,
             marker=dict(color=BLUE, cornerradius=2),
             customdata=list(zip(*q)), hovertemplate="Grade %{x}<br>P5 %{customdata[0]:.1%} · P25 %{customdata[1]:.1%}"
                                                     "<br>Median %{customdata[2]:.1%}<br>P75 %{customdata[3]:.1%} · "
                                                     "P95 %{customdata[4]:.1%}<extra></extra>"),
        dict(type="scatter", mode="markers", x=box["grade"], y=q[2], hoverinfo="skip",
             marker=dict(symbol="line-ew", size=40, line=dict(color="#fff", width=2)))]
    return {
        "kpis": kpi_row([("Avg Interest Rate", pct(ir, 2)), ("Default Rate", pct(dr)), ("LGD", pct(lgd)),
                         ("Loss Rate", pct(loss, 2)), ("Net Profit", usd(profit)), ("Realised ROI", pct(roi))]),
        "rows": [
            [("Interest rate vs default rate, 35 sub-grades",
              "Bubble = funded amount. Dashed = average of the sub-grades shown. Hover for the grade tip", 600,
              fig(scatter, showlegend=True, legend=LEGEND, xaxis=dict(tickformat=".0%", showgrid=True, gridcolor="#ECEBE6",
                                                                       title=dict(text="Average interest rate", font=dict(size=10, color=MUTED))),
                  yaxis=dict(tickformat=".0%", title=dict(text="Default rate", font=dict(size=10, color=MUTED))),
                  margin=dict(l=56, r=16, t=28, b=44),
                  shapes=[dict(type="line", xref="paper", x0=0, x1=1, y0=avg_dr, y1=avg_dr,
                               line=dict(color=INK2, dash="dash", width=1))],
                  annotations=[dict(xref="paper", x=0, y=avg_dr, text=f"Average {avg_dr:.1%}", showarrow=False,
                                    yanchor="bottom", xanchor="left", font=dict(size=9, color=INK2))])),
             ("Default rate by interest-rate band", "5-point bands (calculated column on Loans)", 344,
              bars([f"{b * 5}–{b * 5 + 5}%" for b in band["b"]], band["v"], fmt=".0%",
                   xaxis=dict(type="category", tickfont=dict(size=9, color=MUTED), tickangle=-45))),
             ("Realised ROI, and change vs the one before", "Drill tier ↔ grade ↔ sub-grade. Dashed = 25th percentile of the grades",
              344, views(roi_views, "Grade"))],
            [("Default-rate heatmap", "Grade × term. 60M column uses KEEPFILTERS: blank on 36-month loans", 468,
              fig([dict(type="heatmap", x=heat_cols, y=g["grade"], z=z, colorscale=heat_colorscale(), showscale=False,
                        texttemplate="%{z:.1%}", textfont=dict(size=11, color=INK), xgap=2, ygap=2,
                        hovertemplate="Grade %{y} · %{x}: %{z:.1%}<extra></extra>")],
                  yaxis=dict(autorange="reversed", showgrid=False, tickfont=dict(color=INK, size=11)),
                  xaxis=dict(side="top", tickfont=dict(color=MUTED, size=10)), margin=dict(l=32, r=8, t=24, b=8))),
             ("Grade scorecard", "Lifetime outcomes of matured loans; red ROI would mean the grade lost money", 836, table)],
            [("Interest-rate spread inside each grade (box plot)",
              "Deneb (Vega-Lite) in Power BI: whisker P5–P95, box P25–P75, white tick = median", 520,
              fig(box_traces, barmode="overlay", yaxis=dict(tickformat=".0%"),
                  xaxis=dict(type="category", tickfont=dict(size=10, color=INK2)))),
             ("Prepayment rate by grade and term",
              "Matured loans repaid in full before the term ended: the interest the lender never earned", 384, prepay),
             ("Loans priced outside the sub-grade P5–P95",
              f"Pricing Outliers card: {n_out:,} loans ({n_out / n_all:.1%}). Mostly rate-card changes over the years", 384,
              {"columns": ["Sub", "P5", "P95", "Outliers"], "compact": True,
               "rows": [[sg, pct(lo, 2), pct(hi, 2), num(n)] for sg, lo, hi, n in zip(out["sub_grade"], out["lo"], out["hi"], out["n"])]},
              "-scroll")],
        ]}


def page_vintage():
    d, ead, rec, mtd = one(f"select sum(is_default), sum(exposure_at_default), sum(net_recoveries), avg(months_to_default) {FROM}")
    rr = rec / ead
    ddr = one(f"select sum(funded_amount) filter (where is_default = 1 and is_matured) / sum(funded_amount) {M} {FROM}")[0]
    curve = rows("""select vintage, mob, cum_default_rate v, cum_default_rate_dollar vd from mart.vintage_curve
                    where grade = 'All' order by vintage_sort, mob""")
    peak = one("select max(cum_default_rate) from mart.vintage_curve where grade = 'All'")[0]
    g = rows(f"select grade, {LGD} lgd, avg(months_to_default) mtd {FROM} group by 1 order by 1")
    yg = rows(f"select vintage_year, grade, {DR} dr {FROM} where vintage_year <= 2015 group by all order by 1, 2")
    ic = rows(f"""with i as (select date_trunc('month', issue_date) m, count(*) n {FROM} group by 1),
                       c as (select date_trunc('month', last_payment_date) m, count(*) n {FROM} where is_resolved group by 1)
                  select coalesce(i.m, c.m) m, i.n issued, c.n closed from i full join c using (m)
                  where coalesce(i.m, c.m) < date '2019-03-01' order by 1""")
    book = rows("select month_end, sum(est_outstanding) v from mart.book_balance group by 1 order by 1")
    years = sorted(set(yg["vintage_year"]))
    z = [[next((v for yy, gg, v in zip(yg["vintage_year"], yg["grade"], yg["dr"]) if yy == y and gg == grade), None)
          for grade in g["grade"]] for y in years]

    def curves(col, what):
        return fig([dict(type="scatter", mode="lines", name=v, line=dict(color=c, width=2),
                         x=[m for vv, m in zip(curve["vintage"], curve["mob"]) if vv == v],
                         y=[r for vv, r in zip(curve["vintage"], curve[col]) if vv == v],
                         hovertemplate=f"{v} · month %{{x}}: %{{y:.1%}} of {what}<extra></extra>") for v, c in VINTAGE.items()],
                   showlegend=True, legend=LEGEND, yaxis=dict(tickformat=".0%"),
                   xaxis=dict(title=dict(text="Months on book", font=dict(size=10, color=MUTED)), dtick=6))
    rm = rows(f"select grade, {DR} dr, {LOSS} loss, {ROI} roi, {IR} ir {FROM} group by 1 order by 1")
    coh = rows("""select vintage_year y, k.mob, count(*) filter (where is_default = 1 and months_to_default <= k.mob) / count(*) v
                  from mart.fact_loans, range(0, 37) k(mob)
                  where term = '36 months' and is_matured and vintage_year between 2010 and 2015 group by all order by 1, 2""")
    shades = dict(zip(range(2010, 2016), COHORT_COLORS))
    cohort = fig([dict(type="scatter", mode="lines", name=str(y), line=dict(color=c, width=2),
                       x=[m for yy, m in zip(coh["y"], coh["mob"]) if yy == y], y=[v for yy, v in zip(coh["y"], coh["v"]) if yy == y],
                       hovertemplate=f"{y} · month %{{x}}: %{{y:.1%}}<extra></extra>") for y, c in shades.items()],
                 showlegend=True, legend=LEGEND, yaxis=dict(tickformat=".0%"), xaxis=dict(dtick=6), margin=dict(l=40, r=8, t=28, b=28))
    fn = one(f"""select count(*), count(*) filter (where is_matured), count(*) {M}, sum(is_default) {M},
                        count(*) filter (where is_resolved and is_matured and is_default = 1 and net_recoveries > 0) {FROM}""")
    stages = ["Issued", "Matured", "Resolved", "Defaulted", "Recovered > 0"]
    funnel = fig([dict(type="funnel", y=stages, x=list(fn), marker=dict(color=BLUE), texttemplate="%{x:,}<br>%{percentInitial:.0%}",
                       textfont=dict(size=10), connector=dict(line=dict(color="#C3C2B7", width=1)),
                       hovertemplate="%{y}: %{x:,} loans<extra></extra>")],
                 yaxis=dict(showgrid=False, tickfont=dict(color=INK2, size=10)), margin=dict(l=96, r=12, t=8, b=8))
    metric = {"Default Rate": ("dr", 1), "Loss Rate": ("loss", 2), "Realised ROI": ("roi", 1), "Avg Interest Rate": ("ir", 2)}
    metric_views = {k: {"columns": ["Grade", k], "rows": [[gg, pct(v, dp)] for gg, v in zip(rm["grade"], rm[c])],
                        "compact": True} for k, (c, dp) in metric.items()}
    return {
        "kpis": kpi_row([("Defaults", num(d)), ("Exposure at Default", usd(ead)), ("Net Recoveries", usd(rec)),
                         ("Recovery Rate", pct(rr)), ("Avg Months to Default", f"{mtd:.1f}"), ("Dollar Default Rate", pct(ddr)),
                         ("Peak Cohort Cum Default", pct(peak))]),
        "rows": [
            [("Cumulative default rate by months on book",
              "36-month loans, grouped by issue period. Count = share of loans, Dollar = share of funded dollars", 880,
              views({"Count": curves("v", "loans"), "Dollar": curves("vd", "funded dollars")})),
             ("Loss given default by grade", "≈90% of the unpaid balance is lost, whatever the grade", 424,
              bars(g["grade"], g["lgd"]))],
            [("Default rate by issue year and grade",
              "Matured loans only — loans issued from 2016 have not reached the end of their term", 520,
              fig([dict(type="heatmap", x=g["grade"], y=[str(y) for y in years], z=z, colorscale=heat_colorscale(),
                        showscale=False, texttemplate="%{z:.1%}", textfont=dict(size=10, color=INK), xgap=2, ygap=2,
                        hovertemplate="%{y} · grade %{x}: %{z:.1%}<extra></extra>")],
                  yaxis=dict(autorange="reversed", showgrid=False, type="category", tickfont=dict(color=INK, size=10)),
                  xaxis=dict(side="top", tickfont=dict(color=MUTED, size=10)), margin=dict(l=40, r=8, t=24, b=8))),
             ("Loans issued vs closed per month", "Closed = resolved, by month of last payment (inactive relationship)", 456,
              fig([dict(type="scatter", mode="lines", name="Loans Issued", x=[str(m) for m in ic["m"]], y=ic["issued"],
                        line=dict(color=BLUE, width=2), hovertemplate="%{x|%b %Y} issued: %{y:,}<extra></extra>"),
                   dict(type="scatter", mode="lines", name="Loans Closed", x=[str(m) for m in ic["m"]], y=ic["closed"],
                        line=dict(color=ORANGE, width=2), hovertemplate="%{x|%b %Y} closed: %{y:,}<extra></extra>")],
                  showlegend=True, legend=LEGEND)),
             ("Estimated book outstanding", "Month-end balance, amortisation estimate. Not split by term", 312,
              fig([dict(type="scatter", mode="lines", fill="tozeroy", x=[str(m) for m in book["month_end"]], y=book["v"],
                        line=dict(color=BLUE, width=2), hovertemplate="%{x|%b %Y}: $%{y:,.3s}<extra></extra>")],
                  yaxis=dict(tickprefix="$", exponentformat="B")))],
            [("The metric picked above, by grade", "Risk Metric field parameter: default rate, loss rate, ROI or interest rate",
              424, views(metric_views)),
             ("Cohort curves computed in DAX, by issue year",
              "Matured 36-month loans defaulted by month on book (MOB table). Pooled 2013–14 = the curve above", 432, cohort),
             ("Loan lifecycle funnel", "Issued → term ended → resolved → defaulted → anything recovered. Label = % of issued",
              432, funnel)],
        ]}


ATTRIBUTES = {"Income": ("income_band", "income_band_sort"), "Debt-to-income": ("dti_band", "dti_band_sort"),
              "Employment length": ("emp_length", "emp_length_sort"),
              "Card utilisation": ("revol_util_band", "revol_util_band_sort"),
              "Home ownership": ("home_ownership", "home_ownership")}


def page_borrowers():
    inc, dti, size, active, outstanding, dq = one(f"""select avg(annual_income), avg(dti), avg(funded_amount),
        count(*) filter (where outcome in ('Current','Delinquent')), sum(outstanding_principal),
        count(*) filter (where outcome = 'Delinquent') / count(*) filter (where outcome in ('Current','Delinquent')) {FROM}""")
    attr = {}
    for label, (col, sort) in ATTRIBUTES.items():
        r = rows(f"select {col} k, {DR} v {FROM} group by 1 having count(*) {M} > 0 order by min({sort})")
        attr[label] = bars(r["k"], r["v"], xaxis=dict(type="category", tickfont=dict(size=9, color=MUTED), tickangle=0))
    pg = rows(f"select purpose_group k, {DR} v {FROM} group by 1 order by v desc")
    seg = rows(f"select risk_tier || ' · ' || term k, {DR} v, count(*) {M} n {FROM} group by 1 order by v desc")
    home = rows(f"select home_ownership, outcome, count(*) n {FROM} group by all")
    segs = [("Under 10", 0, 10), ("10–20", 10, 20), ("20–30", 20, 30), ("30–40", 30, 40), ("40 and over", 40, 1000)]
    dseg = [one(f"select {DR}, count(*) {M} {FROM} where dti >= {lo} and dti < {hi}") for _, lo, hi in segs]
    dti_chart = bars([k for k, *_ in segs], [v for v, _ in dseg], xaxis=dict(type="category", tickfont=dict(size=9, color=MUTED), tickangle=0))
    dti_chart["data"][0].update(customdata=[n for _, n in dseg],
                                hovertemplate="DTI %{x}: %{y:.1%} default rate · %{customdata:,} matured loans<extra></extra>")
    pf = rows(f"select purpose_label k, sum(funded_amount) f {FROM} group by 1 order by f desc")
    cum = [sum(pf["f"][:i + 1]) / sum(pf["f"]) for i in range(len(pf["f"]))]
    abc_table = {"columns": ["Purpose", "Funded", "Cum share", "ABC"], "compact": True,
                 "rows": [[k, usd(f), pct(c), a] for k, f, c, a in zip(pf["k"], pf["f"], cum, abc(pf["f"]))]}
    vy, mix, rate = mix_rate()
    wf = fig([dict(type="waterfall", x=["Grade mix", "Rate within grade", "Total"], y=[mix, rate, 0],
                   measure=["relative", "relative", "total"], text=[f"{mix:+.2%}", f"{rate:+.2%}", f"{mix + rate:+.2%}"],
                   textposition="outside", textfont=dict(size=10, color=INK2), cliponaxis=False,
                   connector=dict(line=dict(color="#C3C2B7", width=1)),
                   increasing=dict(marker=dict(color="#d03b3b")), decreasing=dict(marker=dict(color="#0ca30c")),
                   totals=dict(marker=dict(color=NAVY)), hovertemplate="%{x}: %{y:+.2%}<extra></extra>")],
             yaxis=dict(visible=False), xaxis=dict(tickfont=dict(size=10, color=INK2)), margin=dict(l=12, r=12, t=24, b=28))
    homes = ["MORTGAGE", "OWN", "RENT", "OTHER"][::-1]
    tot = {h: sum(n for hh, n in zip(home["home_ownership"], home["n"]) if hh == h) for h in homes}
    stacked = [dict(type="bar", orientation="h", name=o, marker=dict(color=c), y=homes,
                    x=[sum(n for hh, oo, n in zip(home["home_ownership"], home["outcome"], home["n"]) if hh == h and oo == o) / tot[h]
                       for h in homes], hovertemplate=f"{o} · %{{y}}: %{{x:.1%}}<extra></extra>") for o, c in OUTCOME.items()]
    return {
        "kpis": kpi_row([("Avg Annual Income", usd(inc)), ("Avg DTI", f"{dti:.1f}"), ("Avg Loan Size", usd(size)),
                         ("Active Loans", num(active)), ("Outstanding Principal", usd(outstanding)), ("Delinquency Rate", pct(dq, 2))]),
        "rows": [
            [("Default rate by the borrower attribute picked above",
              "Field parameter swaps the axis: income, debt-to-income, employment, card utilisation, home", 880, views(attr)),
             ("By purpose group", "Purpose codes grouped into five families", 424,
              bars(pg["k"], pg["v"], horizontal=True, margin=dict(l=150, r=48, t=8, b=8)))],
            [("Riskiest segments", "Decomposition tree in Power BI — preview shows its first split: risk tier × term", 880,
              bars(seg["k"], seg["v"], horizontal=True, colors=[TIER[k.split(" · ")[0]] for k in seg["k"]],
                   margin=dict(l=160, r=48, t=8, b=8))),
             ("Outcome mix by home ownership", "Share of loans", 424,
              fig(stacked, barmode="stack", bargap=0.35, showlegend=True, legend={**LEGEND, "y": 1.2, "traceorder": "normal", "entrywidth": 88},
                  xaxis=dict(tickformat=".0%", showgrid=False),
                  yaxis=dict(showgrid=False, tickfont=dict(color=INK2, size=10)), margin=dict(l=80, r=12, t=48, b=28)))],
            [("Default rate by DTI segment",
              "Bands from the disconnected DTI Segments table (edit rows to re-band). 40+ is blank: none of those loans has reached term", 424, dti_chart),
             ("ABC classes of loan purpose, by funded amount",
              "A = first 80% of dollars, B = to 95%, C = tail. Follows the year / tier / term slicers", 456, abc_table, "-scroll"),
             (f"Why the default rate moved: {vy - 1} → {vy} vintage",
              "Latest matured issue year in the filter vs the year before. Total = change in default rate", 408, wf)]]}


def page_geo():
    loans, funded, dr, loss, states = one(f"select count(*), sum(funded_amount), {DR}, sum(net_loss), count(distinct state) {FROM}")
    st = rows(f"""select state, state_name, tile_col, tile_row, {DR} dr, sum(funded_amount) funded, count(*) n,
                         sum(net_loss) ncl, count(*) {M} matured, sum(is_default) {M} d {FROM}
                  group by all order by dr desc""")
    st["share"] = [d / sum(st["d"]) for d in st["d"]]
    worst = next(nm for nm, n in zip(st["state_name"], st["matured"]) if n >= 1000)
    rank = {v: i + 1 for i, v in enumerate(sorted({round(x, 12) for x in st["dr"]}, reverse=True))}
    st["rank"] = [rank[round(x, 12)] for x in st["dr"]]
    small = [n < 1000 for n in st["matured"]]  # same floor as the Worst State card
    tipdata = list(zip(st["state_name"], [f"{n:,}" + (" (under 1,000: not eligible for Worst State)" if sm else "")
                                          for n, sm in zip(st["matured"], small)], [usd(v) for v in st["ncl"]], st["rank"]))
    tiptpl = ("%{customdata[0]}<br>Default rate %{marker.color:.1%} · rank %{customdata[3]}"
              "<br>Matured loans %{customdata[1]}<br>Net credit loss %{customdata[2]}<extra></extra>")  # = the State Tip page
    tiles = fig([dict(type="scatter", mode="markers+text", x=st["tile_col"], y=[-r for r in st["tile_row"]],
                      text=[k + ("*" if sm else "") for k, sm in zip(st["state"], small)],
                      textfont=dict(size=10, color=INK, family="Segoe UI Semibold, system-ui"),
                      marker=dict(symbol="square", size=40, color=st["dr"], colorscale=heat_colorscale(), line=dict(width=0)),
                      customdata=tipdata, hovertemplate=tiptpl)],
                xaxis=dict(visible=False, range=[-0.7, 11.7]), yaxis=dict(visible=False, range=[-7.6, 0.6]),
                margin=dict(l=8, r=8, t=8, b=8))
    chor = fig([dict(type="choropleth", locationmode="USA-states", locations=st["state"], z=st["dr"],
                     colorscale=heat_colorscale(), showscale=False, marker=dict(line=dict(color="#FCFCFB", width=1)),
                     customdata=tipdata, hovertemplate="%{customdata[0]}<br>Default rate %{z:.1%}<br>Matured loans %{customdata[1]}<extra></extra>")],
               geo=dict(scope="usa", bgcolor="rgba(0,0,0,0)", showlakes=False), margin=dict(l=0, r=0, t=0, b=0))
    pl = sorted(zip(st["ncl"], st["state"]), reverse=True)
    lshare = [v / sum(st["ncl"]) for v, _ in pl]
    lcum = [sum(lshare[:i + 1]) for i in range(len(lshare))]
    n80 = sum(c <= 0.8 for c in lcum)
    pareto = fig([dict(type="bar", name="Loss Share", x=[k for _, k in pl], y=lshare, marker=dict(color=BLUE, cornerradius=2),
                       hovertemplate="%{x}: %{y:.1%} of net credit loss<extra></extra>"),
                  dict(type="scatter", mode="lines", name="Cum Loss Share", x=[k for _, k in pl], y=lcum,
                       line=dict(color=ORANGE, width=2), hovertemplate="%{x}: cumulative %{y:.1%}<extra></extra>")],
                 showlegend=True, legend=LEGEND, yaxis=dict(tickformat=".0%", range=[0, 1.05]),
                 xaxis=dict(type="category", tickfont=dict(size=8, color=MUTED), tickangle=-90),
                 margin=dict(l=40, r=12, t=28, b=36),
                 shapes=[dict(type="line", xref="paper", x0=0, x1=1, y0=0.8, y1=0.8, line=dict(color=INK2, dash="dash", width=1))],
                 annotations=[dict(xref="paper", x=1, y=0.8, text=f"80% · first {n80 + 1} states", showarrow=False,
                                   yanchor="bottom", xanchor="right", font=dict(size=9, color=INK2))])
    top = 15  # Top N visual filter
    ranked = bars([k + ("*" if sm else "") for k, sm in zip(st["state"][:top], small[:top])], st["dr"][:top], fmt=".1%", colors=st["dr"][:top],
                  xaxis=dict(type="category", tickfont=dict(size=10, color=INK2), tickangle=0))
    ranked["data"][0]["marker"].update(colorscale=heat_colorscale(), cmin=min(st["dr"][:top]), cmax=max(st["dr"][:top]))
    ranked["data"][0].update(customdata=list(zip(st["rank"][:top], st["share"][:top], st["state_name"][:top])),
                             hovertemplate="%{customdata[2]}: %{y:.1%}<br>Rank %{customdata[0]} · %{customdata[1]:.1%} of all defaults<extra></extra>")
    return {
        "kpis": kpi_row([("Loans Issued", num(loans)), ("Funded Amount", usd(funded)), ("Default Rate", pct(dr)),
                         ("Net Credit Loss", usd(loss)), ("States Lent In", num(states)), ("Worst State (1,000+ loans)", worst)]),
        "rows": [[("Default rate by state — tile grid", "Darker = more defaults. * = under 1,000 matured loans (rate unreliable, skipped by Worst State)",
                   652, tiles, "tall"),
                  ("Default rate by state — filled map", "Same shading as the tiles (map outline loads from the plotly CDN)",
                   652, chor, "tall")],
                 [("The 15 states with the highest default rate",
                   "Top N filter (15 by default rate). * = under 1,000 matured loans: IA and NE top the list on small samples", 652, ranked),
                  ("Pareto of net credit loss by state",
                   "Bars = state's share of loss, line = cumulative share; dashed = 80%", 652, pareto)]]}


def page_glossary():
    terms = "".join(f"<dt>{t}</dt><dd>{x}</dd>" for t, x in GLOSSARY_TERMS)
    fg = sorted(glossary.field_glossary(ROOT / "data" / "raw" / "LCDataDictionary.xlsx"), key=lambda r: r["Field Label"])
    sheet = lambda s: {"columns": ["Field", "Code", "Sheet", "Description"], "wrap": True,
                       "rows": [[r["Field Label"], r["Field Code"], r["Sheet"], r["Description"]] for r in fg
                                if s is None or r["Sheet"] == s]}
    return {"kpis": [], "rows": [[
        ("Report terms", "How the report's words are defined", 424, {"html": f"<div><dl class='terms'>{terms}</dl></div>"}, "full"),
        ("LendingClub data dictionary", "LCDataDictionary.xlsx, two sheets appended and cleaned in Power Query", 880,
         views({"All": sheet(None), **{s: sheet(s) for s in sorted({r["Sheet"] for r in fg})}}), "full-scroll")]]}


def page_loan_detail():
    live = "outcome in ('Current','Delinquent')"
    sub = one(f"select sub_grade from mart.fact_loans where {live} group by 1 order by sum(expected_loss) desc limit 1")[0]
    n, op, apd, el = one(f"select count(*), sum(outstanding_principal), avg(pd), sum(expected_loss) from mart.fact_loans "
                         f"where {live} and sub_grade = '{sub}'")
    t = rows(f"""select loan_id, sub_grade, purpose_label, state, term, outcome, income_band, int_rate, pd,
                        outstanding_principal, expected_loss {FROM} where {live} and sub_grade = '{sub}'
                 order by expected_loss desc limit 25""")
    return {
        "back": True,
        "kpis": kpi_row([("Active Loans", num(n)), ("Outstanding Principal", usd(op)), ("Avg PD", pct(apd, 2)),
                         ("Expected Loss", usd(el)), ("Expected Loss Rate", pct(el / op, 2))]),
        "rows": [[(f"Live loans, highest expected loss first — drilled from sub-grade {sub}",
                   f"Example drillthrough: {sub}, the sub-grade with the largest live expected loss. Top 25 of {n:,} loans",
                   1320, {"columns": ["Loan ID", "Sub", "Purpose", "State", "Term", "Outcome", "Income", "Rate", "PD",
                                      "Outstanding", "Expected loss"], "compact": True,
                          "rows": [[str(t["loan_id"][i]), t["sub_grade"][i], t["purpose_label"][i], t["state"][i],
                                    t["term"][i], t["outcome"][i], t["income_band"][i], pct(t["int_rate"][i], 2),
                                    pct(t["pd"][i]), usd(t["outstanding_principal"][i]), usd(t["expected_loss"][i])]
                                   for i in range(len(t["loan_id"]))]}, "full2-scroll")]]}


def page_model():
    m = rows("select * from mart.model_metrics")
    cal = rows("select * from mart.model_calibration order by decile")
    roc = rows("select * from mart.model_roc order by fpr, tpr")
    imp = rows("select feature, feature_label, auc_drop from mart.model_importance order by auc_drop desc")
    desc = glossary.feature_descriptions(ROOT / "data" / "raw" / "LCDataDictionary.xlsx", imp["feature"])
    top = cal["decile"].index(10)
    el = rows(f"""select pd_band, count(*) filter (where outcome in ('Current','Delinquent')) active, sum(outstanding_principal) op,
                  avg(pd) apd, sum(expected_loss) el from mart.fact_loans group by 1 order by 1""")
    el_total = sum(el["el"])
    importance = bars(imp["feature_label"][:12], imp["auc_drop"][:12], fmt=".4f", horizontal=True, margin=dict(l=150, r=56, t=8, b=8))
    importance["data"][0].update(customdata=[(desc[f] or "")[:90] for f in imp["feature"][:12]],
                                 hovertemplate="%{y}: %{x:.4f}<br><i>%{customdata}</i><extra></extra>")
    def stressed(m, sh):
        return rows(f"""select g.grade, sum(outstanding_principal * pd * l.lgd) el,
                               sum(outstanding_principal * least(1, pd * {m}) * greatest(0, least(1, l.lgd + {sh}))) sel
                        {FROM} join mart.grade_lgd l on l.grade = g.grade
                        where outcome in ('Current', 'Delinquent') group by 1 order by 1""")
    stress_views = {}
    for label, mult, sh in (("1.0x PD, +0% LGD", 1.0, 0.0), ("1.5x PD, +0% LGD", 1.5, 0.0), ("2.0x PD, +10% LGD", 2.0, 0.1)):
        d = stressed(mult, sh)
        tot, base = sum(d["sel"]), sum(d["el"])
        stress_views[label] = fig(
            [dict(type="bar", name="Expected Loss (DAX)", x=d["grade"], y=d["el"], marker=dict(color=GRAY, cornerradius=3),
                  hovertemplate="Grade %{x}: $%{y:,.3s} expected<extra></extra>"),
             dict(type="bar", name=f"Stressed {usd(tot)} ({tot / base - 1:+.1%})", x=d["grade"], y=d["sel"],
                  marker=dict(color=ORANGE, cornerradius=3), hovertemplate="Grade %{x}: $%{y:,.3s} stressed<extra></extra>")],
            barmode="group", showlegend=True, legend=LEGEND, yaxis=dict(tickprefix="$", exponentformat="B"),
            margin=dict(l=48, r=12, t=28, b=28))
    gains_x = [0] + sorted(cal["cum_population"])
    gains_y = [0] + sorted(cal["cum_default_capture"])
    return {
        # AUC / Gini / KS: one card visual with small multiples over Model Metrics Long (unpivoted in Power Query)
        "kpis": kpi_row([("AUC", f"{m['auc'][0]:.3f}"), ("Gini", f"{m['gini'][0]:.3f}"), ("KS", f"{m['ks'][0]:.3f}"),
                         ("Top Decile Capture", pct(cal["cum_default_capture"][top])), ("Top Decile Lift", f"{cal['lift'][top]:.2f}x"),
                         ("Expected Loss", usd(el_total))]),
        "rows": [
            [("ROC curve", f"2015 out-of-time test set, {m['test_loans'][0] / 1000:.0f}k matured loans", 424,
              fig([dict(type="scatter", mode="lines", name="Model", x=roc["fpr"], y=roc["tpr"], line=dict(color=BLUE, width=2),
                        hovertemplate="FPR %{x:.1%} · TPR %{y:.1%}<extra></extra>"),
                   dict(type="scatter", mode="lines", name="Random", x=[0, 1], y=[0, 1], line=dict(color=GRAY, width=1.5),
                        hoverinfo="skip")],
                  showlegend=True, legend=LEGEND, xaxis=dict(tickformat=".0%"), yaxis=dict(tickformat=".0%"))),
             ("Calibration by risk decile", "Predicted vs actual (D01 safest) — slightly under-predicts", 456,
              fig([dict(type="bar", name="Predicted PD", x=cal["decile_label"], y=cal["avg_pd"], marker=dict(color=BLUE, cornerradius=3),
                        hovertemplate="%{x} predicted %{y:.1%}<extra></extra>"),
                   dict(type="bar", name="Observed default rate", x=cal["decile_label"], y=cal["default_rate"],
                        marker=dict(color=ORANGE, cornerradius=3), hovertemplate="%{x} observed %{y:.1%}<extra></extra>")],
                  barmode="group", bargap=0.25, bargroupgap=0.08, showlegend=True, legend=LEGEND, yaxis=dict(tickformat=".0%"))),
             ("Cumulative gains", "Reject the riskiest X% of loans → catch Y% of defaults", 408,
              fig([dict(type="scatter", mode="lines+markers", x=gains_x, y=gains_y, line=dict(color=BLUE, width=2),
                        marker=dict(size=6, color=BLUE, line=dict(color="#FCFCFB", width=2)),
                        hovertemplate="Reject %{x:.0%} → catch %{y:.1%}<extra></extra>"),
                   dict(type="scatter", mode="lines", x=[0, 1], y=[0, 1], line=dict(color=GRAY, width=1.5), hoverinfo="skip")],
                  xaxis=dict(tickformat=".0%"), yaxis=dict(tickformat=".0%")))],
            [("What drives the prediction", "Permutation importance: AUC lost when the feature is shuffled. Hover for LendingClub's field definition", 560,
              importance),
             ("Expected loss on the live book", "Outstanding principal × lifetime PD × grade LGD — a conservative upper bound", 744,
              {"columns": ["PD band", "Active loans", "Outstanding", "Avg PD", "Expected loss", "EL rate"],
               "rows": [[el["pd_band"][i], num(el["active"][i]), usd(el["op"][i]), pct(el["apd"][i]), usd(el["el"][i]),
                         pct(el["el"][i] / el["op"][i]) if el["op"][i] else "—"] for i in range(len(el["pd_band"]))],
               "heat": 5}),
             ],
            [("Expected loss under stress, by grade",
              "Live book. PD × multiplier (capped at 100%), LGD + shock (within 0–100%). Power BI: two sliders; "
              "preview: three preset scenarios, total and uplift in the legend", 1320, views(stress_views))]]}


BUILD = {"overview": page_overview, "risk-return": page_risk, "vintage": page_vintage, "borrowers": page_borrowers,
         "geography": page_geo, "model": page_model, "glossary": page_glossary, "loan_detail": page_loan_detail}

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Credit Risk Lab — LendingClub</title>
<style>
:root{--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--hair:#E6E4DD;--surface:#FCFCFB;--plane:#F4F4F1;--navy:%(navy)s;--navy-text:%(navy_text)s}
*{box-sizing:border-box}
body{margin:0;background:#E9E8E3;font-family:"Segoe UI",system-ui,-apple-system,sans-serif;color:var(--ink)}
.app{display:grid;grid-template-columns:232px 1fr;min-height:100vh;max-width:1600px;margin:0 auto;background:var(--plane)}
aside{background:var(--navy);border-right:3px solid #1c5cab;padding:28px 12px;display:flex;flex-direction:column;position:sticky;top:0;height:100vh}
.brand{padding:0 12px 20px;border-bottom:1px solid #184f95;margin-bottom:16px}
.brand small{display:block;font-size:11px;letter-spacing:.14em;font-weight:600;color:var(--navy-text)}
.brand b{font-size:23px;color:#fff;font-weight:600}
nav button,.side button{all:unset;display:block;width:100%%;box-sizing:border-box;padding:11px 16px;margin:3px 0;border-radius:8px;color:var(--navy-text);font-size:14px;cursor:pointer}
nav button:hover,.side button:hover{background:rgba(255,255,255,.07)}
nav button[aria-current=true]{background:rgba(255,255,255,.14);color:#fff;font-weight:600}
.side{margin-top:12px;border-top:1px solid #184f95;padding-top:8px}
.side button{font-size:13px;padding:9px 16px}
.side small{display:block;padding:10px 16px 2px;font-size:10px;letter-spacing:.12em;color:var(--navy-text);opacity:.7}
aside footer{margin-top:auto;padding:16px 12px 0;border-top:1px solid #184f95;font-size:11px;line-height:1.6;color:var(--navy-text)}
main{padding:22px 24px 24px;position:relative}
header{display:flex;justify-content:space-between;gap:24px;border-bottom:1px solid var(--hair);padding-bottom:12px;margin-bottom:12px}
header h1{margin:0;font-size:26px;font-weight:600}
header p{margin:4px 0 0;color:var(--ink2);font-size:13px;max-width:760px}
.chips{display:flex;gap:8px;align-items:flex-start;flex-wrap:wrap}
.chip{border:1px solid var(--hair);border-radius:8px;background:var(--surface);padding:6px 10px;font-size:11px;color:var(--muted);white-space:nowrap}
.chip b{display:block;color:var(--ink);font-size:13px;font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:16px;margin-bottom:16px}
.kpi,.panel{background:var(--surface);border:1px solid var(--hair);border-radius:12px}
.kpi{padding:14px 16px}
.kpi span{font-size:12px;color:var(--muted)}
.kpi b{display:block;font-size:28px;font-weight:600;margin-top:4px;letter-spacing:-.01em}
.kpi.back{all:unset;box-sizing:border-box;border:1px solid var(--hair);border-radius:12px;background:var(--surface);display:flex;align-items:center;justify-content:center;font-size:15px;cursor:pointer}
.row{display:flex;gap:16px;margin-bottom:16px}
.panel{padding:12px 14px 8px;min-width:0;display:flex;flex-direction:column}
.panel h3{margin:0;font-size:15px;font-weight:600}
.panel p{margin:2px 0 6px;font-size:12px;color:var(--muted)}
.chart{flex:1;min-height:0;display:flex;flex-direction:column}
.chart>.body{flex:1;min-height:0}
.row.r2 .panel{height:332px}.row.r3 .panel{height:316px}.row .panel.tall{height:664px}.row .panel.full{height:776px}.row .panel.full2{height:664px}
.scroll .body,.scroll.body{overflow-y:auto}
.seg{display:flex;gap:4px;margin:0 0 6px;flex-wrap:wrap}
.seg button{all:unset;cursor:pointer;font-size:11px;padding:4px 10px;border:1px solid var(--hair);border-radius:6px;color:var(--ink2);background:#fff}
.seg button[aria-pressed=true]{background:var(--navy);border-color:var(--navy);color:#fff}
table{width:100%%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th{font-size:11px;color:var(--muted);font-weight:600;text-align:right;padding:8px 10px;border-bottom:1px solid var(--hair);position:sticky;top:0;background:var(--surface)}
td{text-align:right;padding:8px 10px;border-bottom:1px solid #ECEBE6;white-space:nowrap}
table.compact td,table.compact th{padding:4px 8px;font-size:12px}
table.wrap{table-layout:fixed}table.wrap td{white-space:normal;word-break:break-word;text-align:left;font-size:12px;padding:5px 8px;vertical-align:top}
table.wrap th{text-align:left}table.wrap col.c0{width:22%%}table.wrap col.c1{width:20%%}table.wrap col.c2{width:12%%}
th:first-child,td:first-child{text-align:left;font-weight:600}
dl.terms{margin:0;font-size:12.5px;line-height:1.45}dl.terms dt{font-weight:600;margin-top:10px}dl.terms dd{margin:2px 0 0;color:var(--ink2)}
#help{position:absolute;right:24px;top:104px;width:520px;background:var(--surface);border:1px solid var(--hair);border-radius:12px;padding:16px 18px;box-shadow:0 8px 24px rgba(0,0,0,.12);font-size:13px;line-height:1.5;color:var(--ink2);z-index:5}
#help h4{margin:0 0 6px;color:var(--ink);font-size:15px}#help button{all:unset;cursor:pointer;float:right;font-size:12px;padding:4px 12px;border-radius:6px;background:var(--navy);color:#fff;margin-top:8px}
@media (max-width:1100px){.app{grid-template-columns:1fr}aside{position:static;height:auto}.row{flex-direction:column}.panel{width:100%%!important}.kpis{grid-template-columns:repeat(2,1fr)}}
</style></head>
<body><div class="app">
<aside><div class="brand"><small>LENDINGCLUB</small><b>Credit Risk Lab</b></div><nav id="nav"></nav>
<div class="side"><button id="btn-help">ⓘ  How to read this page</button><button id="btn-reset">↺  Reset filters</button>
<small>DRILLTHROUGH</small><div id="hidden-nav"></div></div>
<footer>Source: Kaggle — LendingClub loan data 2007–2018Q3<br>Pipeline: Python · DuckDB SQL · scikit-learn · Power BI<br>Static preview of the Power BI report</footer></aside>
<main id="main"></main></div>
<script>%(plotly)s</script>
<script>
const PAGES = %(pages)s;
const HEAT = %(heat)s;
const nav = document.getElementById('nav'), main = document.getElementById('main');
let current = null;
function el(tag, attrs = {}, html = '') { const e = document.createElement(tag); Object.assign(e, attrs); e.innerHTML = html; return e; }
function mix(a, b, t) { const p = h => [1,3,5].map(i => parseInt(h.slice(i, i + 2), 16)); const x = p(a), y = p(b);
  return '#' + x.map((v, i) => Math.round(v + (y[i] - v) * t).toString(16).padStart(2, '0')).join(''); }
function table(body) {
  const vals = body.heat !== undefined ? body.rows.map(r => parseFloat(r[body.heat])).filter(v => !isNaN(v)) : [];
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const cls = [body.compact ? 'compact' : '', body.wrap ? 'wrap' : ''].join(' ');
  return `<table class="${cls}">` + (body.wrap ? body.columns.map((_, i) => `<col class="c${i}">`).join('') : '') +
    '<thead><tr>' + body.columns.map(h => `<th>${h}</th>`).join('') + '</tr></thead><tbody>' +
    body.rows.map(row => '<tr>' + row.map((v, i) => {
      let style = '';
      if (i === body.heat && !isNaN(parseFloat(v))) { const t = (parseFloat(v) - lo) / ((hi - lo) || 1);
        style = ` style="background:${t < .5 ? mix(HEAT[0], HEAT[1], t * 2) : mix(HEAT[1], HEAT[2], (t - .5) * 2)};color:${t > .75 ? '#fff' : 'inherit'}"`; }
      return `<td${style}>${v}</td>`; }).join('') + '</tr>').join('') + '</tbody></table>';
}
function draw(c, body) {
  if (body.views) {
    const seg = el('div', {className: 'seg'}), inner = el('div', {className: 'body' + (c.classList.contains('scroll') ? ' scroll' : '')});
    c.append(seg, inner);
    const show = k => { [...seg.children].forEach(b => b.setAttribute('aria-pressed', b.textContent === k));
      if (inner.classList.contains('js-plotly-plot')) Plotly.purge(inner); inner.innerHTML = ''; draw(inner, body.views[k]); };
    Object.keys(body.views).forEach(k => { const b = el('button', {}, k); b.onclick = () => show(k); seg.append(b); });
    show(body.default);
  } else if (body.columns) { c.innerHTML = table(body);
  } else if (body.html) { c.innerHTML = body.html;
  } else {
    requestAnimationFrame(() => Plotly.newPlot(c, body.data, {...body.layout, autosize: true}, {displayModeBar: false, responsive: true}));
  }
}
function render(id) {
  const page = PAGES.find(p => p.id === id);
  current = id;
  document.querySelectorAll('nav button, #hidden-nav button').forEach(b => b.setAttribute('aria-current', b.dataset.id === id));
  main.innerHTML = '';
  const head = el('header');
  head.append(el('div', {}, `<h1>${page.title}</h1><p>${page.subtitle}</p>`));
  head.append(el('div', {className: 'chips'}, ['Year','Risk tier','Term'].map(k => `<div class="chip">${k}<b>All</b></div>`).join('')));
  main.append(head);
  if (page.kpis.length) {
    const k = el('section', {className: 'kpis'});
    k.style.gridTemplateColumns = (page.back ? '96px ' : '') + `repeat(${page.kpis.length},1fr)`;
    if (page.back) { const b = el('button', {className: 'kpi back'}, '← Back'); b.onclick = () => render('overview'); k.append(b); }
    page.kpis.forEach(x => k.append(el('div', {className: 'kpi'}, `<span>${x.label}</span><b>${x.value}</b>`)));
    main.append(k);
  }
  page.rows.forEach((row, ri) => {
    const r = el('section', {className: 'row ' + (ri ? 'r3' : 'r2')});
    row.forEach(([title, sub, width, body, cls]) => {
      cls = cls || '';
      const p = el('div', {className: 'panel ' + cls.replace('-scroll', '')}, `<h3>${title}</h3><p>${sub}</p>`);
      p.style.width = width + 'px';
      const c = el('div', {className: 'chart' + (cls.includes('scroll') ? ' scroll' : '')});
      if (cls.includes('scroll') && !body.views) c.classList.add('body');
      p.append(c); r.append(p); draw(c, body);
    });
    main.append(r);
  });
  if (page.help) {
    const h = el('div', {id: 'help', hidden: true}, `<h4>How to read this page</h4>${page.help}<br><button>Close</button>`);
    h.querySelector('button').onclick = () => h.hidden = true; main.append(h);
  }
  history.replaceState(null, '', '#' + id);
}
PAGES.forEach(p => { const b = el('button', {}, p.name); b.dataset.id = p.id; b.onclick = () => render(p.id);
  (p.hidden ? document.getElementById('hidden-nav') : nav).append(b); });
document.getElementById('btn-help').onclick = () => { const h = document.getElementById('help'); if (h) h.hidden = !h.hidden; };
document.getElementById('btn-reset').onclick = () => render(current);
render((location.hash || '#overview').slice(1));
</script></body></html>"""


def main():
    pages = []
    for pid, name, title, subtitle in PAGES + [p for p in HIDDEN_PAGES if p[0] in BUILD]:
        pages.append({"id": pid, "name": name, "title": title, "subtitle": subtitle, "help": HELP.get(pid),
                      "hidden": pid not in dict((p[0], 1) for p in PAGES), **BUILD[pid]()})
        print(f"  ✓ {name}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(HTML % {"plotly": get_plotlyjs(), "pages": json.dumps(pages, default=str), "heat": json.dumps(HEAT),
                           "navy": NAVY, "navy_text": NAVY_TEXT})
    print(f"  ✓ wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
