"""Page builders for the HTML twin (build_preview.py): the report's default state, computed in DuckDB
with the same rules as the DAX. All slicers on All, Target Stretch 0%, Analyze By = Region.

Rules mirrored from build_model.py:
- Quota counts daily quota up to the last sale date; by region it uses the rep's home territory.
- Status: on target from 100%, at risk from 90%, behind below that (UDF Sales.Status).
- With no date filter, DATESYTD on the Fiscal calendar is the last fiscal year in the Date table
  (Jul–Dec 2013) and DATEADD(-1 year) shifts the whole range back a year.
"""
from datetime import date
from pathlib import Path

import duckdb

from build_report import ACCENT, BEHIND_C, INK, INK2, MUTED, NO_STATUS, ON_C, RISK_C, TARGET_C

ROOT = Path(__file__).resolve().parents[1]
MARTS = ROOT / "data" / "marts"
con = duckdb.connect()
for t in ("sales", "targets", "reps", "territories", "products", "resellers", "cleaning_log", "category_plan"):
    con.execute(f"create view {t} as select * from '{MARTS / t}.parquet'")
con.execute("""create view s as select f.*, t.region, p.subcategory, p.category, p.product, r.business_type,
                      f.amount - f.quantity * p.standard_cost::double gm
               from sales f left join territories t on t.territory_key::int = f.territory_key
               left join products p using (product_code) left join resellers r using (reseller_code)""")
LAST = con.execute("select max(order_date) from sales").fetchone()[0]
FY_START = date(LAST.year + (LAST.month >= 7) - 1, 7, 1)  # last fiscal year in the Date table
con.execute(f"create view tq as select * from targets where date <= '{LAST}'")
Q = "tq"  # quota up to the last sale date


def rows(sql):
    cur = con.execute(sql)
    names, data = [d[0] for d in cur.description], cur.fetchall()
    return {n: [r[i] for r in data] for i, n in enumerate(names)}


def one(sql):
    return con.execute(sql).fetchone()


# ------------------------------------------------------------------ formats
def usd(v, signed=False):
    if v is None:
        return "—"
    a = abs(v)
    body = next((f"${a / d:,.{p}f}{u}" for d, u, p in ((1e6, "M", 2), (1e3, "K", 0)) if a >= d * 0.9995), f"${a:,.0f}")
    return ("+" if v >= 0 else "−") + body if signed else body


pct = lambda v, d=0, signed=False: "—" if v is None else (f"{v:+.{d}%}" if signed else f"{v:.{d}%}").replace("-", "−")
num = lambda v: "—" if v is None else f"{v:,.0f}"
div = lambda a, b: None if not b or a is None else a / b
status = lambda a: None if a is None else "On target" if a >= 1 else "At risk" if a >= 0.9 else "Behind"
COLOR = {"On target": ON_C, "At risk": RISK_C, "Behind": BEHIND_C, None: NO_STATUS}
TONE = {"On target": "good", "At risk": "risk", "Behind": "bad", None: ""}
scolor = lambda a: COLOR[status(a)]
cell = lambda text, a: {"v": text, "t": TONE[status(a)]}
LABEL = {k: v.replace(" ", "\u00a0") for k, v in {"On target": "▲ On target", "At risk": "● At risk", "Behind": "▼ Behind"}.items()}  # [Status Label]: shape + colour

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


def hbars(labels, values, colors, text, left=150):
    t = dict(type="bar", orientation="h", y=labels, x=values, marker=dict(color=colors, cornerradius=4), text=text,
             textposition="outside", cliponaxis=False, textfont=dict(size=10, color=INK2),
             hovertemplate="%{y}: %{text}<extra></extra>")
    vals = [v or 0 for v in values]
    return fig([t], yaxis=dict(autorange="reversed", showgrid=False, zeroline=False, tickprefix="", tickformat="",
                               tickfont=dict(color=INK2, size=10)),
               xaxis=dict(visible=False, zeroline=True, range=[min(0, *vals) * 1.4, max(0, *vals) * 1.25]),
               margin=dict(l=left, r=16, t=4, b=8))


def vbars(labels, values, colors, text, **layout):
    t = dict(type="bar", x=labels, y=values, marker=dict(color=colors, cornerradius=3), text=text,
             textposition="outside", cliponaxis=False, textfont=dict(size=10, color=INK2),
             hovertemplate="%{x}: %{text}<extra></extra>")
    return fig([t], **layout)


def lines(x, series):
    return fig([dict(type="scatter", mode="lines", name=n, x=x, y=y, line=dict(color=c, width=2.2, dash=d),
                     hovertemplate=f"{n} · %{{x|%b %Y}}: %{{y:$,.0f}}<extra></extra>") for n, y, c, d in series],
               showlegend=True, legend=LEGEND, xaxis=dict(tickformat="%b %Y", dtick="M6"))


def y2(f, name, x, y, hex_, fmt):
    """Add a series on a secondary (right) axis, like a line chart's Y2 role."""
    f["data"].append(dict(type="scatter", mode="lines", name=name, x=x, y=y, yaxis="y2",
                          line=dict(color=hex_, width=1.8),
                          hovertemplate=f"{name} · %{{x|%b %Y}}: %{{y:{fmt}}}<extra></extra>"))
    f["layout"]["yaxis2"] = dict(overlaying="y", side="right", showgrid=False, zeroline=False, tickformat=fmt,
                                 tickfont=dict(color=MUTED, size=10), title=None)
    f["layout"]["margin"]["r"] = 48
    return f


def vlines(f, marks):
    """Constant lines on a bar chart's value axis (Analytics pane: X-axis constant line)."""
    f["layout"]["shapes"] = [dict(type="line", xref="x", yref="paper", x0=v, x1=v, y0=0, y1=1,
                                  line=dict(color=c, width=1.4, dash="dot")) for v, c in marks]
    return f


def trend(x, y):
    """Least-squares line over the non-blank points, like the Analytics pane trend line."""
    pts = [(i, v) for i, v in enumerate(y) if v is not None]
    n = len(pts); mx = sum(i for i, _ in pts) / n; my = sum(v for _, v in pts) / n
    b = sum((i - mx) * (v - my) for i, v in pts) / sum((i - mx) ** 2 for i, _ in pts)
    return [my + b * (i - mx) for i in range(len(x))]


def table(columns, body, left=(0,)):
    return {"columns": columns, "rows": body, "shade": [], "left": list(left)}


def kpis(items):
    return [{"label": k, "value": v, "t": t} for k, v, *t in [(*i, None) if len(i) == 2 else i for i in items]]


NO_Y = dict(visible=False)


# ------------------------------------------------------------------ measures
def by_rep():
    """Per rep: sales, quota to the last sale date, attainment (the Reps On Target iteration)."""
    r = rows(f"""select r.rep_key::int k, r.rep, r.title, r.manager_key, t.region, coalesce(s.v, 0) sales, q.v quota
                 from reps r left join territories t on t.territory_key = r.territory_key
                 left join (select rep_key, sum(amount) v from sales group by 1) s on s.rep_key = r.rep_key::int
                 left join (select rep_key, sum(daily_quota) v from {Q} group by 1) q on q.rep_key = r.rep_key::int""")
    out = [dict(zip(r, v)) for v in zip(*r.values())]
    for x in out:
        x["att"] = div(x["sales"], x["quota"])
    return out


REPS = by_rep()
SALES, TARGET = one("select sum(amount) from sales")[0], one(f"select sum(daily_quota) from {Q}")[0]
ATT = div(SALES, TARGET)
WITH_T = sum(x["quota"] is not None for x in REPS)
ON_T = sum((x["att"] or 0) >= 1 for x in REPS)
FYTD_S = one(f"select sum(amount) from sales where order_date >= '{FY_START}'")[0]
FYTD_T = one(f"select sum(daily_quota) from {Q} where date >= '{FY_START}'")[0]
FYTD_PY = one(f"select sum(amount) from sales where order_date between '{FY_START.replace(year=FY_START.year - 1)}' "
              f"and '{date(FY_START.year, 12, 31)}'::date - interval 1 year")[0]
# Whole range shifted back a year: everything up to Dec of the year before the last.
PY = one(f"select sum(amount) from sales where order_date <= '{date(LAST.year - 1, 12, 31)}'")[0]
DATE_END = date(LAST.year, 12, 31)  # Date table runs to the end of the last order or target month (targets: Dec)
TOP_REP = max((x for x in REPS if x["att"] is not None), key=lambda x: x["att"])["rep"]


def months_back(d, n):
    y, m = divmod(d.year * 12 + d.month - 1 - n, 12)
    return date(y, m + 1, 1)


def monthly(where="true"):
    """Months of the Date table (first sale month to Dec of the last year) with sales and quota."""
    return rows(f"""with m as (select unnest(generate_series(date_trunc('month', (select min(order_date) from sales)),
                                                        '{DATE_END}'::date, interval 1 month))::date m)
                    select m.m, s.v, q.q from m
                    left join (select date_trunc('month', order_date)::date m, sum(amount) v from sales where {where}
                               group by 1) s using (m)
                    left join (select date_trunc('month', date)::date m, sum(daily_quota) q from {Q} where {where}
                               group by 1) q using (m) order by 1""")


def avg3(v):
    """WINDOW(-2, REL, 0, REL) then AVERAGEX: blanks are skipped."""
    out = []
    for i in range(len(v)):
        w = [x for x in v[max(0, i - 2):i + 1] if x is not None]
        out.append(sum(w) / len(w) if w else None)
    return out


def page_overview():
    m = rows(f"""select date_trunc('month', d)::date m, sum(v) v, sum(q) q from (
                    select order_date d, amount v, null q from sales union all
                    select date, null, daily_quota from {Q}) group by 1 order by 1""")
    cum = lambda xs: [sum(v or 0 for v in xs[:i + 1]) if any(v is not None for v in xs[:i + 1]) else None
                      for i in range(len(xs))]
    reg = rows(f"""select t.region, s.v, q.v q from territories t
                   left join (select territory_key, sum(amount) v from sales group by 1) s on s.territory_key = t.territory_key::int
                   left join (select r.territory_key, sum(daily_quota) v from {Q} x join reps r on r.rep_key::int = x.rep_key
                              group by 1) q on q.territory_key = t.territory_key
                   order by s.v desc nulls last""")
    att = [div(v, q) for v, q in zip(reg["v"], reg["q"])]
    gap = [(v or 0) - (q or 0) for v, q in zip(reg["v"], reg["q"])]
    gap.append(SALES - TARGET - sum(gap))  # quota of managers with no home territory: (Blank) region
    order = sorted(range(len(att)), key=lambda i: -(att[i] or -1))
    headline = (f"Sales {usd(SALES)} against a target of {usd(TARGET)}: {pct(ATT)}, {status(ATT).lower()}. "
                f"{ON_T} of {WITH_T} reps are on target. Best attainment: {TOP_REP}. "
                f"On the monthly trend, {fy_label(LAST)} ends the quota months (to Dec 2013) at {pct(PROJ[0])} of target.")
    return {
        "kpis": kpis([("Net Sales", usd(SALES)), ("Target", usd(TARGET)), ("Attainment", pct(ATT), TONE[status(ATT)]),
                      ("Gap to Target", usd(SALES - TARGET, True), "good" if SALES >= TARGET else "bad"),
                      ("Attainment FYTD", pct(div(FYTD_S, FYTD_T)), TONE[status(div(FYTD_S, FYTD_T))]),
                      ("Reps On Target", f"{ON_T}")]),
        "rows": [
            [("Cumulative sales vs cumulative target", "Visual calculations: RUNNINGSUM over the months · Chart/Table buttons swap in the same numbers as a table (bookmarks)", 880,
              lines(m["m"], [("Cumulative Sales", cum(m["v"]), ACCENT, "solid"),
                             ("Cumulative Target", cum(m["q"]), TARGET_C, "dash")])),
             ("", "", 424, {"stack": [
                 ("In one sentence", "Follows the slicers", 220, {"html": f'<div class="headline">{headline}</div>'}),
                 ("What-if: stretch the target", "Quota × (1 + stretch)", 96,
                  {"html": '<div class="steps">' + "".join(f'<span class="{"on" if i == 0 else ""}">{i}%</span>'
                                                           for i in range(0, 30, 5)) + "</div>"})]})],
            [("The metric picked above, by the dimension picked above",
              "Metric: Net Sales (also Target / Gap / Gross Margin / Orders) · Analyze by: Region · tooltip: share of visible total",
              652, hbars(reg["region"], reg["v"], ACCENT, [usd(v) for v in reg["v"]], left=120)),
             ("Attainment by the same dimension", "Target follows the rep's home region; colour = status", 652,
              hbars([reg["region"][i] for i in order], [att[i] for i in order], [scolor(att[i]) for i in order],
                    [pct(att[i]) for i in order], left=120))],
            [("Where the gap to target comes from, region by region",
              "Each bar adds one region's sales − target; managers' quota has no home region and shows as (Blank); "
              "the last bar is the total gap", 1320,
              fig([dict(type="waterfall", x=[*reg["region"], "(Blank)", "Total"], measure=["relative"] * len(gap) + ["total"],
                        y=[*gap, None], text=[usd(g, True) for g in gap] + [usd(sum(gap), True)], textposition="outside",
                        cliponaxis=False, textfont=dict(size=10, color=INK2), connector=dict(line=dict(color="#D9D7CF")),
                        increasing=dict(marker=dict(color=ON_C)), decreasing=dict(marker=dict(color=BEHIND_C)),
                        totals=dict(marker=dict(color=INK2)), hovertemplate="%{x}: %{text}<extra></extra>")])),],
        ]}


def org_levels():
    by_key = {int(x["k"]): x for x in REPS}
    mgr = lambda k: (int(float(by_key[k]["manager_key"])) if by_key[k]["manager_key"] not in (None, "") else None)
    for x in REPS:
        path, k = [x["k"]], mgr(x["k"])
        while k in by_key:
            path.insert(0, k)
            k = mgr(k)
        name = lambda n: by_key[path[n]]["rep"] if len(path) > n else x["rep"]
        x["l2"], x["l3"] = name(0), name(1)  # Org Level 1 (manager) and Org Level 2


def tooltip_page(f):
    """Swap Plotly's hover label for the Rep tooltip page (JS shows TIPS[rep]; a click drills through)."""
    f["data"][0].update(hoverinfo="none", hovertemplate=None)
    return {**f, "tip": True}


def bullet(reps):
    reps = sorted(reps, key=lambda x: -x["quota"])
    names, q = [x["rep"] for x in reps], [x["quota"] for x in reps]
    band = lambda w, c: dict(type="bar", orientation="h", y=names, x=w, marker=dict(color=c), width=0.8, hoverinfo="skip")
    f = fig([band([v * 0.9 for v in q], "#EEEDE8"), band([v * 0.1 for v in q], "#E2E0D8"),
             dict(type="bar", orientation="h", y=names, x=[x["sales"] for x in reps], width=0.36, base=0,
                  marker=dict(color=[scolor(x["att"]) for x in reps]), text=[pct(x["att"]) for x in reps],
                  hovertemplate="%{y}: %{x:$,.0f} (%{text})<extra></extra>", textposition="none"),
             dict(type="scatter", mode="markers", y=names, x=q, marker=dict(symbol="line-ns-open", size=16,
                  color=INK, line=dict(width=2.5)), hovertemplate="%{y} target: %{x:$,.0f}<extra></extra>")],
            barmode="stack", yaxis=dict(autorange="reversed", dtick=1, showgrid=False, zeroline=False, tickprefix="", tickformat="",
                                        tickfont=dict(color=INK2, size=10)),
            xaxis=dict(tickprefix="$", tickformat="~s"), margin=dict(l=150, r=16, t=4, b=24))
    # stacked bands, then the sales bar overlays them from 0
    f["layout"]["barmode"] = "overlay"
    f["data"][1]["base"] = [v * 0.9 for v in q]
    return f


def page_reps():
    org_levels()
    ranked = sorted(REPS, key=lambda x: -(x["att"] if x["att"] is not None else -1))
    has = [x for x in ranked if x["att"] is not None]
    groups = {}
    for x in REPS:
        g = groups.setdefault(x["l2"], {"sales": 0, "quota": 0, "kids": []})
        g["sales"] += x["sales"]; g["quota"] += x["quota"] or 0
        if x["l3"] != x["l2"]:
            g["kids"].append(x)
    body = []
    for name, g in sorted(groups.items(), key=lambda t: -t[1]["sales"]):
        a = div(g["sales"], g["quota"])
        body.append([f"<b>{name}</b>", usd(g["sales"]), usd(g["quota"] or None), cell(pct(a), a)])
        kids = {}
        for x in g["kids"]:
            k = kids.setdefault(x["l3"], [0, 0]); k[0] += x["sales"]; k[1] += x["quota"] or 0
        for n, (v, q) in sorted(kids.items(), key=lambda t: -t[1][0]):
            body.append([f'<span style="padding-left:18px;font-weight:400">{n}</span>', usd(v), usd(q or None),
                         cell(pct(div(v, q)), div(v, q))])
    # Attainment in the fiscal quarter before the one holding the last sale (OFFSET -1).
    q0 = date(LAST.year, (LAST.month - 1) // 3 * 3 + 1, 1)
    pq0, pq1 = months_back(q0, 3), q0
    prev_q = {r: div(v, q) for r, v, q in con.execute(f"""select r.rep_key::int, s.v, t.q from reps r
        left join (select rep_key, sum(amount) v from sales where order_date >= '{pq0}' and order_date < '{pq1}' group by 1) s
          on s.rep_key = r.rep_key::int
        left join (select rep_key, sum(daily_quota) q from {Q} where date >= '{pq0}' and date < '{pq1}' group by 1) t
          on t.rep_key = r.rep_key::int""").fetchall()}
    team = {}
    for x in REPS:
        team[x["l2"]] = team.get(x["l2"], 0) + x["sales"]
    leader = {}
    for x in REPS:
        if x["att"] is not None and (x["region"] not in leader or x["att"] > leader[x["region"]]["att"]):
            leader[x["region"]] = x
    at_risk = sum(x["sales"] for x in REPS if status(x["att"]) == "At risk")
    rank, prev, card = 0, None, []
    for x in ranked:
        if x["att"] != prev:
            rank, prev = rank + 1, x["att"]
        st = status(x["att"])
        card.append([rank, x["rep"], x["title"], x["region"] or "—", usd(x["sales"]), usd(x["quota"]),
                     usd(x["sales"] - x["quota"], True) if x["quota"] else "—", pct(x["att"]), cell(LABEL.get(st, "—"), x["att"]),
                     pct(prev_q.get(int(x["k"]))), pct(div(x["sales"], team[x["l2"]]), 1),
                     leader[x["region"]]["rep"] if x["region"] in leader else "—"])
    return {
        "kpis": kpis([("Reps With Target", f"{WITH_T}"), ("Reps On Target", f"{ON_T}"),
                      ("Attainment", pct(ATT), TONE[status(ATT)]),
                      ("Gap to Target", usd(SALES - TARGET, True), "good" if SALES >= TARGET else "bad"),
                      ("Sales Share", "100%"), ("Sales from At-Risk Reps", usd(at_risk))]),
        "rows": [
            [("Attainment by rep", "Green ≥ 100% · amber 90–99% · red below 90% · dotted lines at 100% and 90%", 640,
              tooltip_page(vlines(hbars([x["rep"] for x in has], [x["att"] for x in has], [scolor(x["att"]) for x in has],
                                        [pct(x["att"]) for x in has], left=150), [(1, ON_C), (0.9, RISK_C)]))),
             ("By manager", "Org levels come from PATH() over the manager key; RLS uses the same path", 664,
              table(["Manager", "Net sales", "Target", "Attainment"], body))],
            [("Rep scorecard", "Rank is a visual calculation; previous fiscal quarter via OFFSET, team share via ALLEXCEPT",
              1320, table(["Rank", "Rep", "Title", "Region", "Net sales", "Target", "Gap to target", "Attainment", "Status",
                           f"Prev FQ ({fy_label(pq0)} Q{((pq0.month + 5) % 12) // 3 + 1})", "Share of team",
                           "Top rep in region"], card, left=(1, 2, 3, 8, 11)))],
            [("Sales against quota, rep by rep",
              "Bullet Chart custom visual in Power BI · here: bar = net sales, marker = target, bands at 90% and 100%", 1320,
              bullet(has))],
        ]}


def page_time():
    m = monthly()
    ma = avg3(m["v"])
    r12 = []
    for i in range(len(m["m"])):
        v = [x for x in m["v"][max(0, i - 11):i + 1] if x is not None]
        q = [x for x in m["q"][max(0, i - 11):i + 1] if x is not None]
        r12.append(div(sum(v) if v else None, sum(q) if q else None))
    vs = [v for v in m["v"] if v is not None]
    avg_m = sum(vs) / len(vs)
    ship_days = one("select avg(ship_date - order_date) from sales")[0]
    eop = one(f"select count(distinct rep_key) from targets where date = '{DATE_END}'")[0]
    fy = f"year(d) + (month(d) >= 7)::int"
    fa = rows(f"""select 'FY' || fy f, sum(v) v, sum(q) q from (
                    select {fy.replace('d', 'order_date')} fy, amount v, null q from sales union all
                    select {fy.replace('d', 'date')}, null, daily_quota from {Q}) group by 1 order by 1""")
    att = [div(v, q) for v, q in zip(fa["v"], fa["q"])]
    sq = dict(con.execute("select date_trunc('quarter', order_date)::date, sum(amount) from sales group by 1").fetchall())
    sm = dict(con.execute("select date_trunc('month', order_date)::date, sum(amount) from sales group by 1").fetchall())
    body = []
    for qs in sorted(sq):
        fyn = qs.year + (qs.month >= 7)
        fq = ((qs.month + 5) % 12) // 3 + 1
        last_m = date(qs.year, qs.month + 2, 1)
        ytd = sum(v for k, v in sq.items() if k.year == qs.year and k <= qs)
        fytd = sum(v for k, v in sq.items() if date(fyn - 1, 7, 1) <= k <= qs)
        py = sq.get(qs.replace(year=qs.year - 1))
        pm = sum(sm.get(months_back(qs, k), 0) for k in (1, 0, -1)) or None  # the quarter shifted back one month
        pq = sq.get(months_back(qs, 3))
        body.append([f"FY{fyn} Q{fq}", usd(sq[qs]), usd(sm.get(last_m)), usd(sq[qs]), usd(ytd), usd(fytd), usd(py),
                     pct(div(sq[qs] - py, py), 1, True) if py else "—", usd(pm),
                     pct(div(sq[qs] - pm, pm), 1, True) if pm else "—", usd(pq),
                     pct(div(sq[qs] - pq, pq), 1, True) if pq else "—"])
    return {
        "kpis": kpis([("Sales FYTD", usd(FYTD_S)), ("Target FYTD", usd(FYTD_T)),
                      ("Attainment FYTD", pct(div(FYTD_S, FYTD_T)), TONE[status(div(FYTD_S, FYTD_T))]),
                      ("Sales FYTD PY", usd(FYTD_PY)), ("Sales YoY %", pct(div(SALES - PY, PY), 1, True)),
                      ("Net Sales by Ship Date", usd(SALES)), ("Avg Days to Ship", f"{ship_days:.1f}"),
                      ("Active Reps (End of Period)", f"{eop}")]),
        "rows": [
            [("Net sales per month, 3-month average and rolling 12-month attainment",
              "3M average: WINDOW · R12M: DATESINPERIOD, right axis · constant line = [Avg Monthly Sales] · trend line", 880,
              y2(lines(m["m"], [("Net Sales", m["v"], TARGET_C, "solid"), ("Sales 3M Avg", ma, ACCENT, "solid"),
                                ("Monthly average", [avg_m] * len(m["m"]), INK2, "dot"),
                                ("Trend", trend(m["m"], m["v"]), MUTED, "dash"),
                                ("Sales Linear Trend", [TREND_A + TREND_B * (d.year * 12 + d.month) for d in m["m"]],
                                 RISK_C, "dot")]),
                 "Attainment R12M", m["m"], r12, RISK_C, ".0%")),
             ("Attainment by fiscal year", "Fiscal year runs July to June; FY2014 to the last sale", 424,
              vbars(fa["f"], att, [scolor(a) for a in att], [pct(a) for a in att], yaxis=NO_Y))],
            [("One measure, eleven views",
              "Columns come from the Time Intelligence calculation group; YoY % switches its own format string", 1320,
              table(["Fiscal quarter", "Current", "MTD", "QTD", "YTD", "Fiscal YTD", "Prior Year", "YoY %",
                     "Prior Month", "MoM %", "Prior Quarter", "QoQ %"], body))],
            [("Net sales per month with the built-in forecast",
              "Power BI computes the forecast (exponential smoothing) when the visual renders, so this preview shows the "
              "actuals and the trend line only, no forecast", 880,
              lines(m["m"], [("Net Sales", m["v"], ACCENT, "solid"), ("Trend", trend(m["m"], m["v"]), MUTED, "dash")])),
             ("New vs returning resellers", "New = first order ever in that fiscal year (ALLEXCEPT on the reseller)", 424,
              new_returning())],
        ]}


def page_products():
    u, o, nres, gm = one("select sum(quantity), count(distinct order_number), count(distinct reseller_code), sum(gm) from s")
    ctry = rows("select r.country k, sum(amount) v from sales f join resellers r using (reseller_code) group by 1 order by 2 desc")
    ncity = one("select count(distinct (r.city, r.country)) from sales f join resellers r using (reseller_code)")[0]
    sc = rows("select subcategory k, sum(amount) v from s group by 1 order by 2 desc limit 15")
    bt = rows("select business_type k, sum(amount) v from s group by 1 order by 2 desc")
    pr = rows(f"""select product, subcategory, category, sum(amount) v, sum(quantity) u,
                         sum(amount) filter (where order_date <= '{date(LAST.year - 1, 12, 31)}') py, sum(gm) gm
                  from s group by all order by v desc limit 12""")
    fq = "'FY' || (year(order_date) + (month(order_date) >= 7)::int) || ' Q' || ((month(order_date) + 5) % 12 // 3 + 1)"
    cp = rows(f"""select category, sum(amount) v, (select sum(plan_amount) from category_plan c where c.category = s.category) t,
                         sum(amount) filter (where {fq} in (select fiscal_quarter from category_plan)) vp
                  from s group by 1 order by v desc""")
    return {
        "kpis": kpis([("Net Sales", usd(SALES)), ("Units", num(u)), ("Orders", num(o)), ("Avg Order Value", usd(SALES / o)),
                      ("Sales YoY %", pct(div(SALES - PY, PY), 1, True)), ("Active Resellers", num(nres)),
                      ("Gross Margin %", pct(div(gm, SALES), 1))]),
        "rows": [
            [("Net sales by subcategory", "Top 15", 652, hbars(sc["k"], sc["v"], ACCENT, [usd(v) for v in sc["v"]], left=130)),
             ("Net sales by reseller type", "", 652, hbars(bt["k"], bt["v"], ACCENT, [usd(v) for v in bt["v"]], left=150))],
            [("Products, largest first", "Prior year: DAX UDF · gross margin: SUMX with RELATED standard cost",
              880, table(["Product", "Subcategory", "Category", "Net sales", "Units", "Sales PY", "Sales YoY %",
                          "Gross margin", "GM %"],
                         [[p, s_, c, usd(v), num(un), usd(py), pct(div(v - py, py), 1, True) if py else "—",
                           usd(g, True), pct(div(g, v), 1)]
                          for p, s_, c, v, un, py, g in zip(*pr.values())], left=(0, 1, 2))),
             ("Category plan (synthetic)", "Quota split by last year's category mix; many-to-many to Products", 424,
              table(["Category", "Net sales", "Category target", "Attainment"],
                    [[c, usd(v), usd(t), cell(pct(div(vp, t)), div(vp, t))] for c, v, t, vp in zip(*cp.values())]))],
            [("Where the sales come from",
              f"Power BI: Azure Maps bubble per reseller city ({ncity} cities, size = net sales, colour = region). "
              "This offline preview has no map tiles, so it lists net sales by country", 1320,
              hbars(ctry["k"], ctry["v"], ACCENT, [usd(v) for v in ctry["v"]], left=130))],
            [(f"Pareto: {pct(PARETO_SHARE)} of the products sold make 80% of net sales",
              "Bars: net sales per product, largest first · line: cumulative share (right axis), read off where it crosses 80%",
              880, pareto()),
             ("Resellers by size", "Disconnected band table · pick a fiscal year to band that year's sales", 424, size_bands())],
        ]}


def page_quality():
    src, ok, amt = one("""select sum(rows), sum(rows) filter (where outcome = 'Loaded'),
                                 sum(amount) filter (where outcome not in ('Loaded', 'Subtotal row')) from cleaning_log""")
    oc = rows("select outcome, sum(rows) n from cleaning_log group by 1 order by 2 desc")
    lg = rows("""select branch, file, outcome, rows, case when outcome not in ('Loaded', 'Subtotal row') then amount end a from cleaning_log
                 order by rows desc""")
    note = ('<h4>What Power Query does with the three branch formats</h4><p class="note" style="margin-bottom:8px">'
            'North America sends a workbook per year with a sheet per month, a title block, a total row and the odd pasted '
            'duplicate sheet. Europe sends a workbook per quarter in German number and date formats, with reps as '
            '"LASTNAME, First". Pacific sends a system export with order and line in one cell, Excel serial dates and '
            'Windows logins.</p><p class="note" style="margin-bottom:8px">One reader function per format maps all three to '
            'one schema. Text numbers are converted, reps resolve through an alias list, lines sent twice are kept once, and '
            'every row that fails a rule is counted here with its reason instead of being dropped silently.</p>'
            '<p class="note">Sales managers cannot see this page\'s data: object-level security hides the Cleaning Log.</p>')
    return {
        "kpis": kpis([("Source Rows", num(src)), ("Rows Loaded", num(ok)), ("Rows Set Aside", num(src - ok), "bad"),
                      ("Amount Set Aside", usd(amt)), ("Load Rate", pct(ok / src, 2)),
                      ("Order P95", usd(P95)), ("Order P99", usd(P99))]),
        "rows": [
            [("Rows by outcome", "Every row read is either loaded or set aside with a reason", 760,
              vbars(oc["outcome"], oc["n"], [ACCENT if o == "Loaded" else BEHIND_C for o in oc["outcome"]],
                    [num(v) for v in oc["n"]], yaxis=dict(type="log", visible=False))),
             ("", "", 544, {"html": note})],
            [("Cleaning log by file", "", 1320,
              table(["Branch", "File", "Outcome", "Source rows", "Amount set aside"],
                    [[b, f, o, num(n), usd(a)] for b, f, o, n, a in zip(*lg.values())], left=(0, 1, 2)))],
            [("Unusual orders: above the 95th percentile of order value, P99 flagged",
              "Order Amount Z = (order − mean) ÷ standard deviation · P95 / P99 with PERCENTILEX.INC over every order "
              "in the selection · check these against the branch file before trusting them", 1320, outliers())],
        ]}


# ------------------------------------------------- drillthrough, tooltip, DAX queries
def fy_label(d):
    return f"FY{d.year + (d.month >= 7)}"


def rep_detail(x):
    """The Rep detail drillthrough page for one rep (the page binding filters Reps[Rep])."""
    k = int(x["k"])
    mt = monthly(f"rep_key = {k}")
    run, acc, fy = [], 0, None
    for d, v in zip(mt["m"], mt["v"]):  # WINDOW 1 ABS .. 0 REL, PARTITIONBY fiscal year
        if fy_label(d) != fy:
            fy, acc = fy_label(d), 0
        acc += v or 0
        run.append(acc)
    cat = rows(f"select category k, sum(amount) v from s where rep_key = {k} group by 1 order by 2 desc")
    rs = rows(f"""select r.reseller, r.business_type, s.region, sum(s.amount) v, count(distinct s.order_number) o,
                         sum(s.quantity) u from s join resellers r using (reseller_code) where s.rep_key = {k}
                  group by all order by v desc limit 40""")
    orders = one(f"select count(distinct order_number) from sales where rep_key = {k}")[0]
    gap = x["sales"] - x["quota"] if x["quota"] else None
    return {
        "subtitle": f"Drillthrough on Reps[Rep] = {x['rep']} · {x['title']} · {x['region'] or 'no home region'}",
        "kpis": kpis([("Net Sales", usd(x["sales"])), ("Target", usd(x["quota"])),
                      ("Attainment", pct(x["att"]), TONE[status(x["att"])]),
                      ("Gap to Target", usd(gap, True) if gap is not None else "—", "good" if (gap or 0) >= 0 else "bad"),
                      ("Orders", num(orders))]),
        "rows": [
            [("Net sales vs target per month, 3-month average and fiscal-year running total",
              "Only the rep you drilled through from · WINDOW measures shared with Time & Fiscal", 880,
              y2(lines(mt["m"], [("Net Sales", mt["v"], ACCENT, "solid"), ("Target", mt["q"], TARGET_C, "dash"),
                                 ("Sales 3M Avg", avg3(mt["v"]), INK2, "dot")]),
                 "Sales Running FY", mt["m"], run, RISK_C, "$.3s")),
             ("Net sales by category", "", 424, hbars(cat["k"], cat["v"], ACCENT, [usd(v) for v in cat["v"]], left=96))],
            [("The rep's resellers, largest first", f"Top {len(rs['reseller'])} of the rep's resellers", 1320,
              table(["Reseller", "Business type", "Region", "Net sales", "Orders", "Units", "Avg order value"],
                    [[a, b, c, usd(v), num(o), num(u), usd(v / o)] for a, b, c, v, o, u in zip(*rs.values())],
                    left=(0, 1, 2)))],
            [("Account plan", "RFM segment as of the last sale (quintile scores on recency and order count) and what to do next",
              880, account_plan(k)),
             ("Lapsing resellers", "No order for over 90 days: at risk to 180, churned after", 424, lapsing(k))],
        ]}


def rep_tip(x):
    """The 320×240 Rep tooltip page: four cards and attainment per fiscal year."""
    k = int(x["k"])
    fy = rows(f"""select f, sum(v) v, sum(t) t from (
                    select year(order_date) + (month(order_date) >= 7)::int f, amount v, null t from sales where rep_key = {k}
                    union all select year(date) + (month(date) >= 7)::int, null, daily_quota from {Q} where rep_key = {k})
                  group by 1 having sum(t) > 0 order by 1""")
    att = [div(v, t) for v, t in zip(fy["v"], fy["t"])]
    top = max([a or 0 for a in att] + [1])
    gap = x["sales"] - (x["quota"] or 0)
    cards = "".join(f'<div class="tc"><span>{l}</span><b class="{t}">{v}</b></div>' for l, v, t in [
        ("Net Sales", usd(x["sales"]), ""), ("Target", usd(x["quota"]), ""),
        ("Attainment", pct(x["att"]), TONE[status(x["att"])]), ("Gap to Target", usd(gap, True), "good" if gap >= 0 else "bad")])
    bars = "".join(f'<div class="tb"><i style="height:{(a or 0) / top * 46:.0f}px;background:{scolor(a)}"></i>'
                   f'<em>{pct(a)}</em><small>FY{f}</small></div>' for f, a in zip(fy["f"], att))
    return f'<div class="tip"><h5>{x["rep"]}</h5><div class="tcs">{cards}</div><div class="tbs">{bars}</div></div>'




def page_rep_detail():
    return {"detail": True, "default": TOP_REP, "back": "reps", **DETAIL[TOP_REP]}


def page_rep_tooltip():
    ranked = sorted((x for x in REPS if x["quota"]), key=lambda x: -x["att"])
    grid = '<div class="tipgrid">' + "".join(TIPS[x["rep"]] for x in ranked) + "</div>"
    return {"kpis": [], "rows": [[("Rep tooltip, one per rep", "Report-page tooltip (320×240) on Attainment by rep. "
                                   "Hover a bar on the Reps page to see it there; click a bar to drill through.", 1320,
                                   {"html": grid}, "auto")]]}


def page_dax():
    import html
    from dax_queries import QUERIES, expected

    exp, out = expected(), []
    for tab, code in QUERIES.items():
        cols, body = exp[tab]
        t = ("<table><thead><tr>" + "".join(f'<th class="l">{html.escape(c)}</th>' for c in cols) + "</tr></thead><tbody>"
             + "".join("<tr>" + "".join(f'<td class="l{" good" if v == "PASS" else ""}">{html.escape(str(v))}</td>'
                                        for v in r) + "</tr>" for r in body) + "</tbody></table>")
        out.append([(f"{tab}.dax", f"DAXQueries/{tab}.dax · expected result: {len(body)} rows", 1320,
                     {"html": f'<div class="dax"><pre>{html.escape(code)}</pre><div class="res">{t}</div></div>'}, "auto")])
    return {"kpis": [], "rows": out}

def page_why():
    org_levels()
    reg = rows(f"""select t.region k, s.v, q.v q from territories t
                   left join (select territory_key, sum(amount) v from sales group by 1) s on s.territory_key = t.territory_key::int
                   left join (select r.territory_key, sum(daily_quota) v from {Q} x join reps r on r.rep_key::int = x.rep_key
                              group by 1) q on q.territory_key = t.territory_key
                   order by coalesce(s.v, 0) - coalesce(q.v, 0)""")
    gap = [(v or 0) - (q or 0) for v, q in zip(reg["v"], reg["q"])] + [0]
    gap[-1] = SALES - TARGET - sum(gap)
    reg["k"] = [*reg["k"], "(Blank)"]
    worst = reg["k"][0]
    # Second level: sales booked in the region by each rep against the quota of reps whose home is that region.
    lv2 = rows(f"""select r.rep k, coalesce(s.v, 0) - coalesce(q.v, 0) g from reps r
                   left join (select rep_key, sum(amount) v from sales f join territories t on t.territory_key::int = f.territory_key
                              where t.region = '{worst}' group by 1) s on s.rep_key = r.rep_key::int
                   left join (select x.rep_key, sum(daily_quota) v from {Q} x join reps r2 on r2.rep_key::int = x.rep_key
                              join territories t on t.territory_key = r2.territory_key where t.region = '{worst}'
                              group by 1) q on q.rep_key = r.rep_key::int
                   where s.v is not null or q.v is not null order by 2""")
    tr = rows("""with y as (select reseller_code, sum(amount) filter (where order_date between '2012-07-01' and '2013-06-30') cur,
                                sum(amount) filter (where order_date between '2011-07-01' and '2012-06-30') prev
                         from sales group by 1)
                 select r.business_type k, r.country c,
                        case when cur is null and prev is null then 'No sales FY2012-13'
                             when coalesce(cur, 0) < coalesce(prev, 0) and cur is not null then 'Declining'
                             when cur is null then 'Declining' else 'Growing or new' end t
                 from resellers r left join y using (reseller_code)""")
    def share(key):
        g = {}
        for k, t in zip(tr[key], tr["t"]):
            a = g.setdefault(k, [0, 0]); a[1] += 1; a[0] += t == "Declining"
        base = sum(a[0] for a in g.values()) / sum(a[1] for a in g.values())
        return [[k, num(n), pct(d / n), f"{d / n / base:.2f}×"] for k, (d, n) in sorted(g.items(), key=lambda t: -t[1][0] / t[1][1])]
    at_risk = sum(x["sales"] for x in REPS if status(x["att"]) == "At risk")
    return {
        "kpis": kpis([("Gap to Target", usd(SALES - TARGET, True), "good" if SALES >= TARGET else "bad"),
                      ("Attainment", pct(ATT), TONE[status(ATT)]), ("Reps On Target", f"{ON_T}"),
                      ("Sales from At-Risk Reps", usd(at_risk))]),
        "rows": [
            [("Decomposition tree: gap to target",
              f"Power BI: interactive tree (region → rep → category). Preview: the gap by region, then the reps of the "
              f"largest negative region ({worst})", 652,
              hbars(reg["k"], gap, [ON_C if g >= 0 else BEHIND_C for g in gap], [usd(g, True) for g in gap], left=130)),
             (f"{worst}: gap by rep", "Second level of the tree: sales booked in the region − quota of reps based there", 652,
              hbars(lv2["k"], lv2["g"], [ON_C if g >= 0 else BEHIND_C for g in lv2["g"]],
                    [usd(g, True) for g in lv2["g"]], left=150))],
            [("Key influencers: which resellers buy less than last year",
              "Power BI ranks the factors itself; preview: share of resellers whose FY2013 sales fell below FY2012 "
              "(no sales in FY2013 counts as declining), by business type", 652,
              table(["Business type", "Resellers", "Declining", "vs average"], share("k"))),
             ("… and by country", "Same rule, split by country", 652,
              table(["Country", "Resellers", "Declining", "vs average"], share("c")))],
            [("United Kingdom: net sales per month with anomaly detection",
              "Power BI computes the expected range and flags anomalies when the visual renders; the preview shows the "
              "monthly actuals only", 880, uk_monthly()),
             ("", "", 424, {"html": FINDING})],
        ]}



# ------------------------------------------------- phase D: customer analytics (mirrors build_model.py)
SEGMENTS = ["Champions", "Loyal", "Promising", "Needs attention", "At risk", "Lost", "No orders"]


def segment(r, f):
    """RFM_RULES, top-down."""
    return ("Champions" if r >= 4 and f >= 4 else "Loyal" if r >= 3 and f >= 3 else "Promising" if r >= 4
            else "At risk" if f >= 3 else "Lost" if r == 1 else "Needs attention")


def rfm(cut=None):
    """Recency / frequency / monetary per reseller as of `cut` (default: everything), quintile scores."""
    where = f"where order_date <= '{cut}'" if cut else ""
    r = rows(f"""select r.reseller_code c, max(order_date) l, count(distinct order_number) f, sum(amount) m
                 from resellers r left join (select * from sales {where}) s using (reseller_code) group by 1""")
    have = {c: (l, f, m) for c, l, f, m in zip(*r.values()) if l is not None}
    n, out = len(have), {}
    for c, (l, f, m) in have.items():
        rs = 5 - int(5 * sum(x[0] > l for x in have.values()) / n)
        fs = 1 + int(5 * sum(x[1] < f for x in have.values()) / n)
        ms = 1 + int(5 * sum(x[2] < m for x in have.values()) / n)
        out[c] = dict(rd=(LAST - l).days, f=f, m=m, r=rs, fs=fs, ms=ms, seg=segment(rs, fs))
    for c in r["c"]:
        out.setdefault(c, dict(rd=None, f=None, m=None, r=None, fs=None, ms=None, seg="No orders"))
    return out


RFM = rfm()
con.execute("create or replace temp table rfm_t as select * from (values " +
            ", ".join(f"('{c}', '{v['seg']}', {v['rd'] if v['rd'] is not None else 'null'})" for c, v in RFM.items()) +
            ") t(reseller_code, seg, rd)")

ACTIONS = {"Champions": "Thank them: early access to new models, ask for a referral",
           "Loyal": "Upsell accessories and clothing on the next call",
           "Promising": "Recent but few orders: follow up for the second order this month",
           "At risk": "Used to buy often, quiet now: call this week",
           "Lost": "Win-back offer or take off the call list",
           "Needs attention": "Check in this month before they drift", "No orders": "Prospect: no orders yet"}


def ranked(key, where="true"):
    """[(key, sales, cum share incl., share before)] largest first — Product/Reseller Cum Share."""
    r = rows(f"select {key} k, sum(amount) v from s where {where} group by 1 having sum(amount) is not null order by 2 desc")
    tot, acc, out = sum(r["v"]), 0, []
    for k, v in zip(r["k"], r["v"]):
        out.append((k, v, (acc + v) / tot, acc / tot))
        acc += v
    return out


def abc(before):
    return "A" if before < 0.7 else "B" if before < 0.9 else "C"


PROD = ranked("product")
PARETO_SHARE = sum(b < 0.8 for *_, b in PROD) / len(PROD)  # [Product Share for 80% of Sales]
RES = ranked("reseller_code")
RES_CLASS = {k: abc(b) for k, _, _, b in RES}

FY = "year(order_date) + (month(order_date) >= 7)::int"


def churn():
    """Annual churn FY2012 → FY2013 per business type (None = all), lifetime, CLV on margin and on sales."""
    r = rows(f"""with a as (select reseller_code, business_type, {FY} fy, sum(amount) v, sum(gm) g from s group by all)
                 select business_type k,
                        count(*) filter (where fy = 2012) prev,
                        count(*) filter (where fy = 2012 and reseller_code not in (select reseller_code from a where fy = 2013)) lost,
                        count(*) filter (where fy = 2013) n13, sum(v) filter (where fy = 2013) v13, sum(g) filter (where fy = 2013) g13
                 from a group by rollup (business_type) order by k nulls last""")
    out = []
    for k, prev, lost, n, v, g in zip(*r.values()):
        rate = div(lost, prev)
        life = div(1, rate)
        out.append(dict(k=k or "Total", rate=rate, life=life, n=n, spr=div(v, n), mpr=div(g, n),
                        clv=div(g, n) * life if life else None, clv_s=div(v, n) * life if life else None))
    return out


CHURN = churn()


def days_since(where="true"):
    r = rows(f"select reseller_code c, max(order_date) d from sales where {where} group by 1")
    return {c: (LAST - d).days for c, d in zip(r["c"], r["d"])}


DAYS = days_since()
CHURNED = sum(d > 180 for d in DAYS.values())
AT_RISK = sum(90 < d <= 180 for d in DAYS.values())
churn_status = lambda d: None if d is None else "Churned" if d > 180 else "At risk" if d > 90 else "Active"


def trend_fit(where="true"):
    """LINESTX over monthly net sales up to the last sale: x = year * 12 + month."""
    m = rows(f"select year(order_date) * 12 + month(order_date) x, sum(amount) y from s where {where} group by 1 order by 1")
    n = len(m["x"]); mx = sum(m["x"]) / n; my = sum(m["y"]) / n
    b = sum((x - mx) * (y - my) for x, y in zip(m["x"], m["y"])) / sum((x - mx) ** 2 for x in m["x"])
    return my - b * mx, b


TREND_A, TREND_B = trend_fit()


def projected():
    """[Projected FY Attainment]: FY so far + trend to the last quota month, ÷ quota for the same months."""
    q_end = one("select max(date) from targets")[0]
    actual = one(f"select sum(amount) from sales where order_date between '{FY_START}' and '{LAST}'")[0]
    fut = sum(TREND_A + TREND_B * x for x in range(LAST.year * 12 + LAST.month + 1, q_end.year * 12 + q_end.month + 1))
    quota = one(f"select sum(daily_quota) from targets where date between '{FY_START}' and '{q_end}'")[0]
    return (actual + fut) / quota, actual, fut, quota


PROJ = projected()


def heat(v):
    """HEAT: the Retention / Transition Color measures."""
    bg = None if v is None else "#7FAAD3" if v >= 0.75 else "#A9C8E4" if v >= 0.5 else "#CFE0F0" if v >= 0.25 else "#EEF3F8"
    return {"v": pct(v), "bg": bg}


def new_returning():
    r = rows(f"""with f as (select reseller_code, min(order_date) d from sales group by 1),
                      a as (select distinct reseller_code, {FY} fy from sales)
                 select 'FY' || a.fy f, count(*) filter (where {FY.replace('order_date', 'f.d')} = a.fy) n, count(*) t
                 from a join f using (reseller_code) group by 1 order by 1""")
    ret = [t - n for n, t in zip(r["n"], r["t"])]
    bar = lambda name, y, c: dict(type="bar", name=name, x=r["f"], y=y, marker=dict(color=c, cornerradius=3), text=[num(v) for v in y],
                                  textposition="outside", cliponaxis=False, textfont=dict(size=10, color=INK2),
                                  hovertemplate=f"{name} · %{{x}}: %{{y:,}}<extra></extra>")
    return fig([bar("New Resellers", r["n"], ON_C), bar("Returning Resellers", ret, ACCENT)], showlegend=True, legend=LEGEND,
               yaxis=NO_Y, barmode="group")


def pareto():
    f = vbars([k for k, *_ in PROD], [v for _, v, *_ in PROD], ACCENT, [usd(v) for _, v, *_ in PROD],
              xaxis=dict(visible=False), margin=dict(l=52, r=48, t=8, b=8))
    f["data"][0].update(textposition="none")
    f["data"].append(dict(type="scatter", mode="lines", name="Product Cum Share", x=[k for k, *_ in PROD],
                          y=[c for _, _, c, _ in PROD], yaxis="y2", line=dict(color=RISK_C, width=2),
                          hovertemplate="%{x}: %{y:.0%}<extra></extra>"))
    f["layout"]["yaxis2"] = dict(overlaying="y", side="right", showgrid=False, zeroline=False, tickformat=".0%",
                                 range=[0, 1.02], dtick=0.2, tickfont=dict(color=MUTED, size=10), title=None)
    return f


def size_bands():
    bands = [("Under $50K", 0, 5e4), ("$50K-250K", 5e4, 2.5e5), ("$250K-1M", 2.5e5, 1e6), ("Over $1M", 1e6, 1e12)]
    tot = [v for _, v, *_ in RES]
    n = [sum(lo <= v < hi for v in tot) for _, lo, hi in bands]
    return vbars([b for b, *_ in bands], n, ACCENT, [num(x) for x in n], yaxis=NO_Y)


ORDERS = rows("""select order_number o, min(order_date) d, any_value(r.reseller) r, any_value(branch) b, sum(amount) v
                 from sales left join resellers r using (reseller_code) group by 1""")
P95, P99, MEAN, SD = one("""select quantile_cont(v, 0.95), quantile_cont(v, 0.99), avg(v), stddev_pop(v)
                            from (select sum(amount) v from sales group by order_number)""")


def outliers():
    out = sorted((x for x in zip(*ORDERS.values()) if x[4] >= P95), key=lambda x: -x[4])
    return table(["Order", "Order date", "Reseller", "Branch", "Net sales", "Order Amount Z", "Order outlier"],
                 [[o, f"{d:%d %b %Y}", r, b, usd(v), f"{(v - MEAN) / SD:.2f}",
                   {"v": "Above P99" if v >= P99 else "Above P95", "t": "bad" if v >= P99 else "risk"}]
                  for o, d, r, b, v in out], left=(0, 2, 3, 6))


def uk_monthly():
    m = rows(f"""with m as (select unnest(generate_series(date_trunc('month', (select min(order_date) from sales)),
                                                     '{DATE_END}'::date, interval 1 month))::date m)
                 select m.m, s.v from m left join (select date_trunc('month', order_date)::date m, sum(amount) v from s
                                                   where region = 'United Kingdom' group by 1) s using (m) order by 1""")
    return lines(m["m"], [("Net Sales", m["v"], ACCENT, "solid")])


def _finding():
    jae = one("""select count(*), sum(amount), string_agg(distinct t.region, ', ') from sales f
                 join reps r on r.rep_key::int = f.rep_key join territories t on t.territory_key::int = f.territory_key
                 where r.rep = 'Jae Pak'""")
    uk = next(x for x in REPS if x["region"] == "United Kingdom")
    return ('<h4>Finding: why the United Kingdom is at 43%</h4>'
            '<p class="note" style="margin-bottom:8px">Tree: the UK has the largest negative gap (−$5.8M). Split by rep: '
            'Jae Pak −$10.1M (his whole UK quota, no UK sales), José Saraiva +$3.8M, Amy Alberts +$0.4M.</p>'
            f'<p class="note" style="margin-bottom:8px">Rep detail: {uk["rep"]}\'s home territory is the UK, but all '
            f'{jae[0]:,} of his order lines are booked to {jae[2]} ({usd(jae[1])}). José Saraiva, based in Canada, '
            'books $3.8M in the UK.</p>'
            '<p class="note">So the UK quota follows Jae Pak while his sales land in Canada: a territory-assignment '
            'artefact in the source data, not UK resellers buying less. Fix the assignment before coaching anyone.</p>')


FINDING = _finding()


def account_plan(k):
    r = rows(f"""select r.reseller_code c, r.reseller n, sum(amount) v from sales f join resellers r using (reseller_code)
                 where rep_key = {k} group by all order by v desc""")
    return table(["Reseller", "RFM segment", "RFM action", "Recency days", "Frequency", "Net sales"],
                 [[n, RFM[c]["seg"], ACTIONS[RFM[c]["seg"]], num(RFM[c]["rd"]), num(RFM[c]["f"]), usd(v)]
                  for c, n, v in zip(*r.values())], left=(0, 1, 2))


def lapsing(k):
    d = days_since(f"rep_key = {k}")
    names = dict(con.execute("select reseller_code, reseller from resellers").fetchall())
    out = sorted(((names[c], v) for c, v in d.items() if v > 90), key=lambda t: t[1])
    tone = {"Churned": "bad", "At risk": "risk"}
    return table(["Reseller", "Days since last order", "Churn status"],
                 [[n, num(v), {"v": churn_status(v), "t": tone[churn_status(v)]}] for n, v in out])


def cohort():
    act = rows("""select distinct reseller_code c, (year(order_date) + (month(order_date) >= 7)::int) * 4
                         + (month(order_date) + 5) % 12 // 3 + 1 i from sales""")
    first = {}
    for c, i in zip(act["c"], act["i"]):
        first[c] = min(first.get(c, i), i)
    cells = {}
    for c, i in zip(act["c"], act["i"]):
        cells[(first[c], i - first[c])] = cells.get((first[c], i - first[c]), 0) + 1
    coh = sorted({f for f, _ in cells}); qn = sorted({n for _, n in cells})
    body = [[f"FY{(f - 1) // 4} Q{(f - 1) % 4 + 1}"] + [heat(div(cells.get((f, n)), cells[(f, 0)])) if (f, n) in cells
                                                         else "" for n in qn] for f in coh]
    return table(["Cohort", *map(str, qn)], body)


def transition():
    a, b = rfm("2012-06-30"), rfm("2013-06-30")
    cnt = {}
    for c, v in a.items():
        if b[c]["seg"] != "No orders":
            cnt[(v["seg"], b[c]["seg"])] = cnt.get((v["seg"], b[c]["seg"]), 0) + 1
    frm = [x for x in SEGMENTS if any(f == x for f, _ in cnt)]
    to = [x for x in SEGMENTS if any(t == x for _, t in cnt)]
    tot = {f: sum(n for (g, _), n in cnt.items() if g == f) for f in frm}
    return table(["From \\ To", *to], [[f] + [heat(div(cnt[(f, t)], tot[f])) if (f, t) in cnt else "" for t in to] for f in frm])


def basket():
    r = rows("""with os as (select distinct order_number o, subcategory s from s),
                     n as (select count(distinct o) n from os), sub as (select s, count(*) c from os group by 1)
                select a.s a, b.s b, count(*) t, count(*) / any_value(n.n) sup, count(*) / any_value(sa.c) ab,
                       count(*) / any_value(sb.c) ba, count(*) * any_value(n.n) / (any_value(sa.c) * any_value(sb.c)) lift
                from os a join os b on a.o = b.o and a.s < b.s, n, sub sa, sub sb
                where sa.s = a.s and sb.s = b.s group by 1, 2 having count(*) >= 30 order by lift desc""")
    return table(["Subcategory A", "Subcategory B", "Orders together", "Support", "Confidence A→B", "Confidence B→A", "Lift"],
                 [[a, b, num(t), pct(s_, 1), pct(x), pct(y), f"{l:.2f}"] for a, b, t, s_, x, y, l in zip(*r.values())],
                 left=(0, 1))


def page_resellers():
    by_cls = {c: [0, 0] for c in "ABC"}
    for k, v, *_ in RES:
        by_cls[RES_CLASS[k]][0] += v; by_cls[RES_CLASS[k]][1] += 1
    names = dict(con.execute("select reseller_code, reseller from resellers").fetchall())
    bt = dict(con.execute("select reseller_code, business_type from resellers").fetchall())
    tot = next(x for x in CHURN if x["k"] == "Total")
    clv = [[x["k"] if x["k"] != "Total" else "<b>Total</b>", pct(x["rate"], 1), f'{x["life"]:.1f}' if x["life"] else "—",
            usd(x["spr"]), usd(x["clv_s"]), usd(x["clv"], True) if x["clv"] is not None else "—"] for x in CHURN]
    tone = {"Churned": "bad", "At risk": "risk", "Active": "good"}
    # New / returning in the latest fiscal quarter with sales (the DAX fallback when no date is picked).
    q0 = date(LAST.year, (LAST.month - 1) // 3 * 3 + 1, 1)
    fq = f"FY{LAST.year + (LAST.month >= 7)} Q{((LAST.month + 5) % 12) // 3 + 1}"
    act, new = con.execute(f"""with f as (select reseller_code, min(order_date) d from sales group by 1)
        select count(distinct s.reseller_code), count(distinct s.reseller_code) filter (where f.d >= '{q0}')
        from sales s join f using (reseller_code) where s.order_date >= '{q0}'""").fetchone()
    abc_rows = [[names[k], bt[k], usd(v), pct(c), RES_CLASS[k], RFM[k]["seg"],
                 {"v": churn_status(DAYS.get(k)), "t": tone[churn_status(DAYS.get(k))]}] for k, v, c, _ in RES]
    return {
        "kpis": kpis([("Period Counted", f'<span style="font-size:14px;white-space:nowrap">{fq} (latest)</span>'), ("Resellers in Period", num(act)),
                      ("New Resellers", num(new)), ("Returning Resellers", num(act - new)),
                      ("At-Risk Resellers", num(AT_RISK), "risk"), ("Churned Resellers", num(CHURNED), "bad"),
                      ("Annual Churn Rate", pct(tot["rate"], 1)), ("Reseller CLV on Sales", usd(tot["clv_s"]))]),
        "rows": [
            [("Resellers by ABC class, largest first",
              "Class follows the slicers: A = first 70% of net sales, B = next 20%, C = last 10% · RFM segment as of the last sale",
              880, table(["Reseller", "Business type", "Sales in class", "Cum share", "ABC class", "RFM segment", "Churn status"],
                         abc_rows, left=(0, 1, 4, 5, 6))),
             ("", "", 424, {"stack": [
                 ("", "", 64, {"html": '<div style="font-size:12px;color:var(--muted)">ABC class · slicer on the '
                  'disconnected ABC Class table</div><div class="steps" style="margin:6px 0 0">'
                  + "".join(f'<span class="on">{c}</span>' for c in "ABC") + "</div>"}),
                 ("Net sales by ABC class", "Few A resellers, most of the sales", 200,
                  vbars(list("ABC"), [by_cls[c][0] for c in "ABC"], ACCENT,
                        [f"{usd(by_cls[c][0])} · {by_cls[c][1]} resellers" for c in "ABC"], yaxis=NO_Y))]})],
            [("Cohort retention: share of each first-order quarter still ordering N quarters later",
              "Calculated table Reseller Cohort (fiscal quarters, static) · darker = more of the cohort ordered", 880, cohort()),
             ("Lifetime value by reseller type",
              "Sales/res. = FY2013 net sales per reseller · Life = 1 ÷ churn FY2012→FY2013 · CLV GM is negative: "
              "standard cost exceeds the reseller price in this sample", 424,
              {**table(["Type", "Churn", "Life (yr)", "Sales/res.", "CLV sales", "CLV GM"], clv),
               "shade": [len(clv) - 1]})],
            [("Where FY2012's segments ended FY2013", "Rows: RFM segment on 30 Jun 2012 · columns: on 30 Jun 2013 · share of the row",
              652, transition()),
             ("Bought together: subcategory pairs in one order",
              "Lift above 1 = bought together more often than chance · pairs in at least 30 orders", 652, basket())],
        ]}


BUILD = {"overview": page_overview, "reps": page_reps, "time": page_time, "products": page_products,
         "resellers": page_resellers,
         "quality": page_quality, "rep_detail": page_rep_detail, "rep_tooltip": page_rep_tooltip,
         "dax_queries": page_dax, "why": page_why}

TIPS = {x["rep"]: rep_tip(x) for x in REPS if x["quota"]}
DETAIL = {x["rep"]: rep_detail(x) for x in REPS if x["sales"]}
