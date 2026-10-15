"""Static HTML twin of the Power BI report — same pages, layout, palette and metric definitions.

    uv run python src/build_preview.py      -> reports/dashboard.html (self-contained, opens offline)

Power BI Desktop is Windows-only; this lets the dashboard be reviewed anywhere.
Every number is computed in DuckDB with the same rules as the DAX measures in build_model.py
(revenue = item price of valid orders, late = delivered after the estimate, customer = customer_unique_id).
"""
import json
from pathlib import Path

import duckdb
import numpy as np
from plotly.offline import get_plotlyjs

from build_model import SYNONYMS
from build_report import BAD, DEFS, GOOD, INK, INK2, MUTED, NAVY, PALETTE

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "dashboard.html"
con = duckdb.connect(str(ROOT / "data" / "warehouse" / "olist.duckdb"), read_only=True)
con.execute("use mart")
BLUE, ORANGE = PALETTE[0], PALETTE[1]

PAGES = [("overview", "Executive Overview",
          "Revenue grew fast through 2017 and plateaued in 2018 — where it comes from, and how customers pay.", "Category group"),
         ("sales-products", "Sales & Products", "Which categories earn the money, how they rate, and when people shop.", "Category group"),
         ("customers-retention", "Customers & Retention",
          "97% of customers buy once. Who is worth keeping, and how fast cohorts fade.", "Customer state"),
         ("delivery-satisfaction", "Delivery & Satisfaction",
          "Late orders are the #1 driver of bad reviews — where and when the delivery promise breaks.", "Customer state"),
         ("sellers-logistics", "Sellers & Logistics",
          "A few sellers make half the revenue; distance decides what customers pay for freight.", "Category group"),
         ("why-reviews", "Why Reviews Dropped",
          "1 the KPI → 2 late deliveries by promise month → 3 where (decomposition tree) → 4 what drives it.", "Customer state"),
         ("about", "About & Definitions", "What the numbers mean, the words Q&A understands, and how to ask your own question.",
          "Category group"),
         # hidden in Power BI (drillthrough / tooltip pages): previewed here with their top seller / category
         ("seller-detail", "Seller Detail", "Drillthrough page · previewed for the top seller by revenue.", "Category group"),
         ("tt-category", "TT Category", "Report-page tooltip of the category bars (360 × 260) · previewed for the top category.",
          "Category group")]
HIDDEN = {"seller-detail", "tt-category"}

V = "where i.is_valid"
ITEMS = "from fact_order_items i"


def rows(sql):
    cur = con.execute(sql)
    names, data = [d[0] for d in cur.description], cur.fetchall()
    return {n: [float(r[i]) if hasattr(r[i], "as_tuple") else r[i] for r in data] for i, n in enumerate(names)}


def one(sql):
    return [float(v) if hasattr(v, "as_tuple") else v for v in con.execute(sql).fetchone()]


# ------------------------------------------------------------------ formats
def brl(v, d=0):
    for div, unit in ((1e9, "bn"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"R${v / div:,.1f}{unit}"
    return f"R${v:,.{d}f}"


pct = lambda v, d=1: f"{v * 100:.{d}f}%"
num = lambda v, d=0: f"{v:,.{d}f}"

# ------------------------------------------------------------------ figures
AXIS = dict(showgrid=False, zeroline=False, linecolor="#E6E4DD", tickfont=dict(color=MUTED, size=10), title=None)
YAXIS = dict(showgrid=True, gridcolor="#ECEBE6", zeroline=False, tickfont=dict(color=MUTED, size=10), title=None)
LEGEND = dict(orientation="h", y=1.12, x=0, font=dict(color=INK2, size=10), bgcolor="rgba(0,0,0,0)")


def fig(traces, **layout):
    base = dict(margin=dict(l=56, r=12, t=8, b=36), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(family="Segoe UI, system-ui, -apple-system, sans-serif", color=INK2, size=11),
                xaxis=AXIS, yaxis=YAXIS, showlegend=False, hoverlabel=dict(bgcolor="#fff", bordercolor="#E6E4DD",
                                                                          font=dict(color=INK)))
    for k, v in layout.items():
        base[k] = {**base[k], **v} if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return {"data": traces, "layout": base}


def bars(x, y, fmt=",.0f", horizontal=False, colors=BLUE, prefix="", **layout):
    t = dict(type="bar", marker=dict(color=colors, cornerradius=4), texttemplate=f"{prefix}%{{{'x' if horizontal else 'y'}:{fmt}}}",
             textposition="outside", textfont=dict(size=10, color=INK2), cliponaxis=False)
    if horizontal:
        t.update(x=y, y=x, orientation="h", hovertemplate=f"%{{y}}: {prefix}%{{x:{fmt}}}<extra></extra>")
        return fig([t], **{"yaxis": dict(autorange="reversed", showgrid=False, tickfont=dict(color=INK2, size=10)),
                          "xaxis": dict(visible=False), "margin": dict(l=150, r=56, t=8, b=8), **layout})
    t.update(x=x, y=y, hovertemplate=f"%{{x}}: {prefix}%{{y:{fmt}}}<extra></extra>")
    return fig([t], **{"yaxis": dict(visible=False), "margin": dict(l=12, r=12, t=20, b=36), **layout})


def donut(labels, values):
    text = [pct(v / sum(values)) if v / sum(values) >= 0.04 else "" for v in values]  # small slices stay unlabeled
    return fig([dict(type="pie", hole=0.58, labels=labels, values=values, sort=False, marker=dict(colors=PALETTE),
                     text=text, textinfo="text", textposition="inside", textfont=dict(size=11), hovertemplate="%{label}: R$%{value:,.0f}<extra></extra>")],
               showlegend=True, legend=dict(orientation="v", x=1.02, y=0.5, font=dict(color=INK2, size=10)),
               margin=dict(l=8, r=8, t=8, b=8))


def switch(**figs):
    """Chip-switched panel: stands in for a field-parameter slicer or a bookmark toggle in Power BI."""
    return {"switch": {"labels": [k.replace("_", " ") for k in figs], "figs": list(figs.values())}}


def status(r):
    return "▲ High" if r >= .12 else "● Watch" if r >= .08 else "✓ OK"


def kpi(label, value, note=None):
    return {"label": label, "value": value, "note": note}


# ------------------------------------------------------------------ pages
def page_overview():
    rev, orders, items_rev = one(f"select sum(price), count(distinct order_id), avg(review_score) {ITEMS} {V}")
    late = one("select count(*) filter (where is_late) / count(*) filter (where is_late is not null) from fact_order_items")[0]
    trend = rows(f"""
        with m as (select date_trunc('month', order_date)::date m, sum(price) rev {ITEMS} {V} group by 1),
        last as (select max(order_date) d {ITEMS} {V})
        select m.m, m.rev,
               (select sum(price) {ITEMS} {V} and order_date >= m.m - interval 1 year
                  and order_date < least(m.m + interval 1 month, (select d from last) + interval 1 day) - interval 1 year) py
        from m order by 1""")
    py = [v if v else None for v in trend["py"]]
    mo = rows(f"""select date_trunc('month', order_date)::date m, count(*) n, avg(items_value) aov,
                         count(*) filter (where is_late) / nullif(count(*) filter (where is_delivered), 0) late, avg(review_score) review
                  from fact_orders where is_valid group by 1 having count(*) >= 100 order by 1""")
    line = lambda y, fmt, name, **ax: fig([dict(type="scatter", mode="lines", x=mo["m"], y=y, line=dict(color=BLUE, width=2),
                                               hovertemplate=f"%{{x|%b %Y}}: %{{y:{fmt}}}<extra>{name}</extra>")],
                                          xaxis=dict(tickformat="%b %Y"), yaxis=ax)
    brk = lambda sql: rows(f"select {sql} g, sum(price) rev {ITEMS} {V} group by 1 order by 2 desc")
    grp = rows(f"select category_group g, sum(price) rev {ITEMS} join dim_product using (product_id) {V} group by 1 order by 2 desc")
    tree = rows(f"""select region, state, sum(price) rev {ITEMS} join dim_customer c using (customer_unique_id) {V}
                    group by 1, 2 order by 3 desc""")
    regions = rows(f"""select region, sum(price) rev {ITEMS} join dim_customer c using (customer_unique_id) {V}
                       group by 1 order by 2 desc""")
    pay = rows("select payment_type, sum(payment_value) v from fact_payments where is_valid group by 1 order by 2 desc")
    wd = rows(f"""select d.weekday_name, count(distinct i.order_id) n {ITEMS} join dim_date d on d.date = i.order_date {V}
                  group by d.weekday_name, d.weekday_no order by d.weekday_no""")
    return {"kpis": [kpi("Revenue", brl(rev), "vs prior year: select one year"),
                     kpi("Order Count", num(orders), "vs prior year: select one year"),
                     kpi("AOV", brl(rev / orders, 2)), kpi("Item Avg Review", num(items_rev, 2)), kpi("Seller Late Rate %", pct(late))],
            "rows": [[("Monthly trend of the metric picked above", "Months with ≥ 100 orders (except Revenue) · Black Friday 2017 is the revenue peak", 630,
                       switch(Revenue=fig([dict(type="scatter", mode="lines", name="Revenue", x=trend["m"], y=trend["rev"], line=dict(color=BLUE, width=2),
                                 hovertemplate="%{x|%b %Y}: R$%{y:,.0f}<extra>Revenue</extra>"),
                            dict(type="scatter", mode="lines", name="Revenue PY", x=trend["m"], y=py, line=dict(color=ORANGE, width=2),
                                 hovertemplate="%{x|%b %Y}: R$%{y:,.0f}<extra>Revenue PY</extra>")],
                           showlegend=True, legend=LEGEND, yaxis=dict(tickprefix="R$", tickformat="~s"), xaxis=dict(tickformat="%b %Y")),
                              Order_Count=line(mo["n"], ",.0f", "Orders"), AOV=line(mo["aov"], ",.2f", "AOV", tickprefix="R$"),
                              **{"Late_Rate_%": line(mo["late"], ".1%", "Late rate", tickformat=".0%")},
                              Avg_Review=line(mo["review"], ".2f", "Avg review"))),
                      ("Revenue by the breakdown picked above", "Category group · customer region · seller tier · distance band", None,
                       switch(**{k: bars(d["g"], d["rev"], fmt=",.3s", horizontal=True, prefix="R$", margin=dict(l=170, r=56, t=8, b=8))
                                 for k, d in (("Category_Group", grp),
                                              ("Customer_Region", brk("(select region from dim_customer c where c.customer_unique_id = i.customer_unique_id)")),
                                              ("Seller_Tier", brk("(select pareto_tier from dim_seller s where s.seller_id = i.seller_id)")),
                                              ("Distance_Band", brk("distance_band")))}))],
                     [("Where customers are", "Revenue by region → state (click to drill) · Southeast dominates", None,
                       fig([dict(type="treemap", labels=regions["region"] + tree["state"], parents=[""] * len(regions["region"]) + tree["region"],
                                 ids=regions["region"] + [f"{r}/{s}" for r, s in zip(tree["region"], tree["state"])],
                                 values=regions["rev"] + tree["rev"], branchvalues="total", texttemplate="%{label}<br>R$%{value:,.3s}",
                                 marker=dict(colors=[PALETTE[i % 8] for i in range(len(regions["region"]))] + [None] * len(tree["state"])),
                                 hovertemplate="%{label}: R$%{value:,.0f}<extra></extra>", pathbar=dict(visible=False),
                                 tiling=dict(pad=1), textfont=dict(color="#fff"))],
                           margin=dict(l=0, r=0, t=0, b=0))),
                      ("How customers pay", "Share of payment value", None, donut(pay["payment_type"], pay["v"])),
                      ("Orders by weekday", "Shoppers buy on Mon–Tue, not weekends", None,
                       bars(wd["weekday_name"], wd["n"], fmt=",.0f"))]]}


def page_sales():
    rev, freight, items = one(f"select sum(price), sum(freight), count(*) {ITEMS} {V}")
    ytd = one(f"""select sum(price) {ITEMS} {V} and year(order_date) = (select max(year(order_date)) {ITEMS} {V})""")[0]
    cat = rows(f"""select category c, sum(price) rev, count(*) n, sum(price) / count(*) price, avg(review_score) review
                   {ITEMS} join dim_product using (product_id) {V} group by 1 order by 2 desc""")
    hours = rows("""select d.weekday_name wd, o.order_hour h, count(*) n from fact_orders o join dim_date d on d.date = o.order_date
                    where o.is_valid group by d.weekday_name, d.weekday_no, h order by d.weekday_no, h""")
    days = list(dict.fromkeys(hours["wd"]))
    grid = {(w, h): n for w, h, n in zip(hours["wd"], hours["h"], hours["n"])}
    area = rows(f"""select date_trunc('month', order_date)::date m, category_group g, sum(price) rev
                    {ITEMS} join dim_product using (product_id) {V} group by 1, 2 order by 1""")
    groups = rows(f"select category_group g {ITEMS} join dim_product using (product_id) {V} group by 1 order by sum(price) desc")["g"]
    months = sorted(set(area["m"]))
    by = {(m, g): r for m, g, r in zip(area["m"], area["g"], area["rev"])}
    size = [n ** 0.5 for n in cat["n"]]
    top = rows(f"""select category c, category_group g, sum(price) rev, count(distinct customer_unique_id) buyers,
                          sum(price) / sum(sum(price)) over (partition by category_group) grp_share
                   {ITEMS} join dim_product using (product_id) {V} group by 1, 2 order by 3 desc limit 10""")
    return {"kpis": [kpi("GMV", brl(rev + freight)), kpi("Items Sold", num(items)), kpi("Avg Item Price", brl(rev / items, 2)),
                     kpi("Freight Share %", pct(freight / (rev + freight))), kpi("Revenue YTD", brl(ytd))],
            "rows": [[("Top 10 categories by revenue", "Top N filter · hover: share of its group, customers buying", 400,
                       top_bars(top)),
                      ("Price vs satisfaction by category", "Bubble size = items sold · expensive categories don't rate better", None,
                       fig([dict(type="scatter", mode="markers", x=cat["price"], y=cat["review"], text=cat["c"],
                                 marker=dict(size=size, sizemode="area", sizeref=2 * max(size) / 38 ** 2, sizemin=3, color=BLUE,
                                             opacity=0.6, line=dict(color="#fff", width=1)),
                                 customdata=cat["n"],
                                 hovertemplate="%{text}<br>Avg price R$%{x:,.2f} · review %{y:.2f} · %{customdata:,} items<extra></extra>")],
                           xaxis=dict(tickprefix="R$", showgrid=True, gridcolor="#ECEBE6", title=dict(text="Avg Item Price", font=dict(size=10))),
                           yaxis=dict(title=dict(text="Item Avg Review", font=dict(size=10))), margin=dict(l=56, r=16, t=8, b=44)))],
                     [("When orders are placed", "Orders by weekday × hour of purchase", 640,
                       {"columns": [""] + [str(h) for h in range(24)],
                        "rows": [[w] + [str(grid.get((w, h), 0)) for h in range(24)] for w in days],
                        "heat": list(range(1, 25)), "colors": ["#CDE2FB", NAVY], "cls": "dense hours"}),
                      ("Category mix over time", "Monthly revenue stacked by category group", None,
                       fig([dict(type="scatter", mode="lines", stackgroup="one", name=g, x=months, y=[by.get((m, g), 0) for m in months],
                                 line=dict(width=0.5, color=PALETTE[i % 8]), hovertemplate=f"{g} %{{x|%b %Y}}: R$%{{y:,.0f}}<extra></extra>")
                            for i, g in enumerate(groups)],
                           showlegend=True, legend=dict(orientation="v", x=1.01, y=1, font=dict(color=INK2, size=9)),
                           yaxis=dict(tickprefix="R$", tickformat="~s"), xaxis=dict(tickformat="%b %Y"), margin=dict(l=56, r=8, t=8, b=36)))],
                     [("Revenue through the Time Calc group", "One measure, six calculation items · 2018 months with ≥ 100 orders", None,
                       time_calc_matrix(), "scroll")],
                     sales_row5()]}


def top_bars(top):
    f = bars(top["c"], top["rev"], fmt=",.3s", horizontal=True, prefix="R$", margin=dict(l=170, r=56, t=8, b=8))
    f["data"][0].update(customdata=list(zip(top["g"], top["grp_share"], top["buyers"])),
                        hovertemplate="%{y}: R$%{x:,.0f}<br>%{customdata[1]:.1%} of %{customdata[0]} · %{customdata[2]:,} customers<extra></extra>")
    return f


def time_calc_matrix():
    """Same maths as the 'Time Calc' calculation items applied to [Revenue] (Current, PY, YoY %, YTD, MoM %, 3M Avg)."""
    t = rows(f"""with m as (select date_trunc('month', order_date)::date m, sum(price) rev, count(distinct order_id) n {ITEMS} {V} group by 1)
                 select m, rev, lag(rev, 12) over w py, sum(rev) over (partition by year(m) order by m) ytd, lag(rev) over w pm,
                        avg(rev) over (order by m rows 2 preceding) avg3, n from m window w as (order by m) order by m""")
    sign = lambda v: f"{v * 100:+.1f}%"
    out = [[m.strftime("%b %Y"), brl(r), brl(py), sign(r / py - 1), brl(ytd), sign(r / pm - 1), brl(a)]
           for m, r, py, ytd, pm, a, n in zip(*t.values()) if m.year == 2018 and n >= 100]
    return {"columns": ["Month", "Current", "PY", "YoY %", "YTD", "MoM %", "3M Avg"], "rows": out}


def page_customers():
    rev, cust = one(f"select sum(price), count(distinct customer_unique_id) {ITEMS} {V}")
    new, rep, valid = one("""select count(*) filter (where customer_order_seq = 1), count(*) filter (where customer_order_seq > 1),
                             count(*) from fact_orders where is_valid""")
    # rule: under "All" every customer is new at some point -> count new/returning in the last quarter with data (same as the DAX)
    lastq = one("select date_trunc('quarter', max(order_date)) from fact_orders where is_valid")[0]
    qnew, qret = one(f"""select count(*) filter (where customer_order_seq = 1), count(distinct customer_unique_id) filter (where customer_order_seq > 1)
                        from fact_orders where is_valid and date_trunc('quarter', order_date) = '{lastq}'""")
    qlabel = f"Q{(lastq.month - 1) // 3 + 1} {lastq.year}"
    rcr = one("select count(*) filter (where valid_orders > 1) / count(*) filter (where valid_orders > 0) from dim_customer")[0]
    rfm = rows("""select rfm_segment s, count(*) filter (where valid_orders > 0) n, sum(lifetime_value) ltv from dim_customer
                  group by s, rfm_segment_order having n > 0 order by rfm_segment_order""")
    rep_m = rows("""select date_trunc('month', order_date)::date m, count(*) filter (where customer_order_seq > 1) / count(*) r
                    from fact_orders where is_valid group by 1 having count(*) >= 100 order by 1""")  # Sep 2018 tail has a handful of orders
    coh = rows("select cohort_label, cohort_month, month_offset, month_offset_label, retention_rate from cohort_retention order by 2, 3")
    labels = list(dict.fromkeys(coh["cohort_label"]))
    offs = list(dict.fromkeys(l for _, l in sorted(set(zip(coh["month_offset"], coh["month_offset_label"])))))
    cell = {(c, o): r for c, o, r in zip(coh["cohort_label"], coh["month_offset_label"], coh["retention_rate"])}
    st = rows(f"""select c.state_name s, count(distinct i.customer_unique_id) n {ITEMS} join dim_customer c using (customer_unique_id)
                  {V} group by 1 order by 2 desc""")
    return {"kpis": [kpi("Customers", num(cust)), kpi("New Customers", num(qnew)), kpi("Returning Customers", num(qret)),
                     kpi("Period Counted", qlabel, "last quarter with data (Year = All)"), kpi("Repeat Order %", pct(rep / valid)),
                     kpi("Revenue per Customer", brl(rev / cust, 2)), kpi("Repeat Customer Rate", pct(rcr))],
            "rows": [[("Customers by RFM segment", "Recency · frequency · monetary", None,
                       bars(rfm["s"], rfm["n"], fmt=",.0f", horizontal=True)),
                      ("Lifetime value by RFM segment", "New high-value + at-risk big spenders hold most revenue", None,
                       bars(rfm["s"], rfm["ltv"], fmt=",.3s", horizontal=True, prefix="R$")),
                      ("Repeat order share", "Orders from customers who bought before", None,
                       fig([dict(type="bar", x=rep_m["m"], y=rep_m["r"], marker=dict(color=BLUE, cornerradius=2),
                                 hovertemplate="%{x|%b %Y}: %{y:.1%}<extra></extra>")],
                           yaxis=dict(tickformat=".0%"), xaxis=dict(tickformat="%b %y"), margin=dict(l=44, r=8, t=8, b=36)))],
                     [("Cohort retention", "Share of each first-purchase cohort ordering again N months later", 700,
                       {"columns": ["Cohort"] + offs,
                        "rows": [[c] + [pct(cell[(c, o)], 2) if (c, o) in cell else "" for o in offs] for c in labels],
                        "heat": list(range(1, len(offs) + 1)), "colors": ["#FFFFFF", BLUE], "cls": "dense cohort"}, "scroll"),
                      ("Customers by state", "São Paulo alone is ~40% of the base", None,
                       bars(st["s"], st["n"], fmt=",.0f", horizontal=True, height=22 * len(st["s"]) + 16,
                            margin=dict(l=150, r=56, t=8, b=8)), "scroll")],
                     customers_row4()]}


def late_col(r):
    return BAD if r >= .12 else "#EC835A" if r >= .08 else BLUE


def page_delivery():
    ontime, days, promised, review, bad = one("""
        select 1 - count(*) filter (where is_late) / count(*) filter (where is_delivered),
               avg(delivery_days) filter (where is_delivered), avg(promised_days) filter (where is_delivered),
               avg(review_score), count(*) filter (where review_score <= 2) / count(review_score) from fact_orders
        where is_valid and is_delivered""")  # report filter (valid) + page filter (delivered), as in Power BI
    bucket = rows("""select delivery_bucket b, avg(review_score) r from fact_orders where is_valid and is_delivered
                     group by b, delivery_bucket_order
                     having r is not null order by delivery_bucket_order""")
    lo, hi = min(bucket["r"]), max(bucket["r"])
    mix = lambda t: "#" + "".join(f"{round(int(BAD[i:i + 2], 16) + (int(BLUE[i:i + 2], 16) - int(BAD[i:i + 2], 16)) * t):02x}" for i in (1, 3, 5))
    st = rows("""select c.state s, count(*) filter (where o.is_late) / count(*) filter (where o.is_delivered) r,
                        avg(c.lat) lat, avg(c.lng) lng, count(*) filter (where o.is_delivered) n
                 from fact_orders o join dim_customer c using (customer_unique_id) where o.is_valid group by 1
                 having count(*) filter (where o.is_delivered) > 0 order by 2 desc""")
    lanes = rows("""with p as (select customer_state s, quantile_cont(delivery_days, 0.9) p90 from fact_orders
                                where is_valid and is_delivered and delivery_days is not null group by 1)
                    select lane, items, avg_freight, avg_delivery_days, late_rate, p90 from freight_lanes l
                    join p on p.s = l.customer_state where items >= 30 order by items desc""")  # Lane State P90 (10-s14)
    by_month = lambda col: rows(f"""select date_trunc('month', {col})::date m, count(*) filter (where is_late) / count(*) r from fact_orders
                                     where is_valid and is_delivered group by 1 having count(*) >= 100 order by 1""")
    late_b, late_p = by_month("order_date"), by_month("estimated_date")
    back = rows("select date d, in_transit n from fact_backlog_daily order by 1")
    return {"kpis": [kpi("On-Time Rate %", pct(ontime)), kpi("Avg Delivery Days", num(days, 1)), kpi("Avg Promised Days", num(promised, 1)),
                     kpi("Avg Review", num(review, 2)), kpi("Bad Review %", pct(bad))],
            "rows": [[("Average review by delivery outcome", "Late > 7 days averages 1.7★ vs 4.3★ when early", 500,
                       bars(bucket["b"], bucket["r"], fmt=".2f", colors=[mix((r - lo) / ((hi - lo) or 1)) for r in bucket["r"]])),
                      ("Late-delivery rate by month", "Months with ≥ 100 delivered orders · spikes follow Nov 2017 and Feb–Mar 2018", None,
                       fig([dict(type="scatter", mode="lines", x=late_b["m"], y=late_b["r"], line=dict(color=BLUE, width=2),
                                 hovertemplate="%{x|%b %Y}: %{y:.1%}<extra></extra>"), trend_line(late_b["m"], late_b["r"])],
                           yaxis=dict(tickformat=".0%"), xaxis=dict(tickformat="%b %Y"),
                           shapes=[dict(type="line", xref="paper", x0=0, x1=1, y0=.08, y1=.08, line=dict(color=BAD, width=1, dash="dash"))],
                           annotations=[dict(xref="paper", x=1, y=.08, text="Target 8%", showarrow=False, yanchor="bottom",
                                             xanchor="right", font=dict(size=9, color=BAD))]))],
                     [("Late rate by customer state", "▲ ≥ 12% red · ● ≥ 8% orange · bars / map", 400,
                       switch(Bars=bars(st["s"], st["r"], fmt=".1%", horizontal=True, height=22 * len(st["s"]) + 16,
                                        margin=dict(l=40, r=56, t=8, b=8), colors=[late_col(r) for r in st["r"]]),
                              Map=fig([dict(type="scatter", mode="markers+text", x=st["lng"], y=st["lat"], text=st["s"],
                                            textposition="middle center", textfont=dict(size=8, color="#fff"),
                                            marker=dict(size=[max(14, n ** 0.5 / 2.2) for n in st["n"]], color=[late_col(r) for r in st["r"]],
                                                        line=dict(color="#fff", width=1)),
                                            customdata=[f"{pct(r)} · {status(r)}" for r in st["r"]],
                                            hovertemplate="%{text}: %{customdata}<extra></extra>")],
                                      xaxis=dict(visible=False), yaxis=dict(visible=False, scaleanchor="x"),
                                      margin=dict(l=4, r=4, t=4, b=4))), "scroll"),
                      ("Busiest freight lanes (seller → customer state)", "Visual filter: lanes with ≥ 30 items · status = ▲ / ● / ✓", None,
                       {"columns": ["Lane", "Items", "Avg Freight", "Avg Delivery Days", "Late Rate %", "Status", "State P90", "Outlier"],
                        "rows": [[l, num(n), brl(f, 2), num(d, 1), pct(r), status(r), num(p9, 1), "▲ Above state P90" if d > p9 else "–"]
                                 for l, n, f, d, r, p9 in zip(lanes["lane"], lanes["items"], lanes["avg_freight"], lanes["avg_delivery_days"],
                                                              lanes["late_rate"], lanes["p90"])],
                        "heat": [4], "colors": ["#FFFFFF", "#F19A99"], "bar": 1}, "scroll")],
                     [("Late rate: month bought vs month promised", "Same orders, two dates (inactive relationship) · months with ≥ 100 delivered", None,
                       fig([dict(type="scatter", mode="lines", name=n, x=d["m"], y=d["r"], line=dict(color=c, width=2, dash=dash),
                                 hovertemplate=n + " %{x|%b %Y}: %{y:.1%}<extra></extra>")
                            for n, d, c, dash in (("By purchase month", late_b, BLUE, None), ("By promise month", late_p, ORANGE, "dot"))],
                           showlegend=True, legend=LEGEND, yaxis=dict(tickformat=".0%"), xaxis=dict(tickformat="%b %Y"))),
                      ("Orders in transit (end of day)", f"Bought, not yet delivered · peak {num(max(back['n']))} after Black Friday 2017", None,
                       fig([dict(type="scatter", mode="lines", x=back["d"], y=back["n"], line=dict(color=NAVY, width=1.5), fill="tozeroy",
                                 fillcolor="rgba(205,226,251,.5)", hovertemplate="%{x|%d %b %Y}: %{y:,} in transit<extra></extra>")],
                           xaxis=dict(tickformat="%b %Y")))],
                     delivery_row5()]}


def page_sellers():
    rev, sellers, top, dist = one(f"""select sum(price), count(distinct seller_id),
        sum(price) filter (where s.pareto_tier = 'A · Top 50% revenue') / sum(price), avg(distance_km)
        {ITEMS} join dim_seller s using (seller_id) {V}""")
    late = one("select count(*) filter (where is_late) / count(*) filter (where is_late is not null) from fact_order_items")[0]
    sel = rows(f"""select s.seller_code code, s.state, s.pareto_tier tier, sum(price) rev, count(distinct order_id) n,
                   avg(review_score) review, count(*) filter (where i.is_late) / nullif(count(*) filter (where i.is_late is not null), 0) late
                   {ITEMS} join dim_seller s using (seller_id) {V} group by 1, 2, 3 order by rev desc""")
    risk = one(f"""select sum(price) {ITEMS} {V} and seller_id in
                   (select seller_id from fact_order_items where is_valid group by 1 having avg(review_score) < 3)""")[0]
    tiers = sorted(set(sel["tier"]))
    p90 = float(np.percentile(sel["rev"], 90))  # PERCENTILEX.INC = linear interpolation
    tier = rows(f"select s.pareto_tier t, sum(price) rev {ITEMS} join dim_seller s using (seller_id) {V} group by 1 order by 1")
    band = rows("""select distance_band b, sum(freight) / (sum(price) + sum(freight)) r from fact_order_items where is_valid
                   group by b, distance_band_order order by distance_band_order""")
    top_n = 60
    geo = rows(f"""select s.seller_code c, s.lat, s.lng, s.pareto_tier t, sum(price) rev {ITEMS} join dim_seller s using (seller_id)
                   {V} and s.lat between -34 and 6 and s.lng between -74 and -34 group by 1, 2, 3, 4""")
    flow = rows("""with r as (select distinct state, region from dim_customer)
                   select 'Seller · ' || rs.region src, 'Customer · ' || rc.region dst, sum(items) n
                   from freight_lanes l join r rs on rs.state = l.seller_state join r rc on rc.state = l.customer_state
                   group by 1, 2 order by 3 desc""")
    nodes = list(dict.fromkeys(flow["src"] + flow["dst"]))
    return {"kpis": [kpi("Active Sellers", num(sellers)), kpi("Revenue per Seller", brl(rev / sellers)),
                     kpi("Top-Tier Revenue Share %", pct(top)), kpi("Seller Late Rate %", pct(late)),
                     kpi("Revenue at Risk", brl(risk), f"{pct(risk / rev)} from sellers under 3★"), kpi("Avg Distance km", num(dist))],
            "rows": [[("Every seller: revenue vs rating", "Colour = Pareto tier · size = orders", 620,
                       fig([dict(type="scatter", mode="markers", name=t,
                                 x=[r for r, tt in zip(sel["rev"], sel["tier"]) if tt == t],
                                 y=[v for v, tt in zip(sel["review"], sel["tier"]) if tt == t],
                                 text=[c for c, tt in zip(sel["code"], sel["tier"]) if tt == t],
                                 marker=dict(color=PALETTE[i], opacity=0.55, sizemode="area", sizemin=2,
                                             size=[n ** 0.5 for n, tt in zip(sel["n"], sel["tier"]) if tt == t],
                                             sizeref=2 * max(sel["n"]) ** 0.5 / 26 ** 2, line=dict(width=0)),
                                 hovertemplate="%{text}<br>R$%{x:,.0f} · %{y:.2f}★<extra>" + t + "</extra>")
                            for i, t in enumerate(tiers)],
                           showlegend=True, legend=LEGEND, xaxis=dict(tickprefix="R$", tickformat="~s", showgrid=True, gridcolor="#ECEBE6"),
                           yaxis=dict(range=[0.8, 5.2]),
                           # 17-s17: Seller Revenue P90 as an X reference line
                           shapes=[dict(type="line", yref="paper", y0=0, y1=1, x0=p90, x1=p90, line=dict(color=BAD, width=1, dash="dash"))],
                           annotations=[dict(yref="paper", y=1, x=p90, text=f"P90 {brl(p90)}", showarrow=False, xanchor="left",
                                             font=dict(size=9, color=BAD))])),
                      ("Revenue by seller tier", "Tier A = ~4% of sellers", None, donut(tier["t"], tier["rev"]))],
                     [("Seller leaderboard", f"Sorted by revenue in the current selection · top {top_n} of {len(sel['code']):,}", 620,
                       {"columns": ["Seller", "State", "Tier", "Revenue", "Orders", "Avg Review", "Late Rate %"],
                        "rows": [[c, s, t, brl(r), num(n), num(v, 2), pct(l) if l is not None else ""] for c, s, t, r, n, v, l in
                                 list(zip(sel["code"], sel["state"], sel["tier"], sel["rev"], sel["n"], sel["review"], sel["late"]))[:top_n]],
                        "bar": 3}, "scroll"),
                      ("Freight share by distance", "Freight ÷ (price + freight) · 10% under 100 km → 19% past 2,000 km", None,
                       bars(band["b"], band["r"], fmt=".1%"))],
                     [("Where sellers are", "Bubble per seller at its zip-prefix centroid · size = revenue · colour = tier (no basemap here)", 620,
                       fig([dict(type="scatter", mode="markers", name=t,
                                 x=[x for x, tt in zip(geo["lng"], geo["t"]) if tt == t], y=[y for y, tt in zip(geo["lat"], geo["t"]) if tt == t],
                                 text=[c for c, tt in zip(geo["c"], geo["t"]) if tt == t],
                                 marker=dict(color=PALETTE[i], opacity=0.5, sizemode="area", sizemin=2, line=dict(width=0),
                                             size=[r ** 0.5 for r, tt in zip(geo["rev"], geo["t"]) if tt == t],
                                             sizeref=2 * max(geo["rev"]) ** 0.5 / 24 ** 2),
                                 hovertemplate="%{text}<extra>" + t + "</extra>") for i, t in enumerate(tiers)],
                           showlegend=True, legend=LEGEND, xaxis=dict(visible=False), yaxis=dict(visible=False, scaleanchor="x"),
                           margin=dict(l=4, r=4, t=24, b=4))),
                      ("Where goods flow", "Sankey · seller region → customer region · items on lanes", None,
                       fig([dict(type="sankey", arrangement="snap",
                                 node=dict(label=nodes, pad=10, thickness=12, color=[NAVY if n.startswith("Seller") else BLUE for n in nodes]),
                                 link=dict(source=[nodes.index(x) for x in flow["src"]], target=[nodes.index(x) for x in flow["dst"]],
                                           value=flow["n"], color="rgba(42,120,214,.25)"))],
                           margin=dict(l=8, r=8, t=8, b=8)))],
                     sellers_row5(sel)]}


def page_about():
    li = lambda items: "<ul>" + "".join(f"<li><b>{k}</b> — {v}</li>" for k, v in items) + "</ul>"
    return {"kpis": [],
            "rows": [[("Ask a question", "Q&A visual — answers only inside Power BI; these are questions it is set up for", 520,
                       {"text": li([("sales by state", "Revenue, synonym “sales”"), ("late rate by month", "Late Rate % on the calendar"),
                                    ("rating by category", "Avg Review, synonym “rating”"), ("orders by seller", "Seller Code, synonym “seller”")])}),
                      ("Definitions", "Same text as the info buttons and the overlay on Overview", None,
                       {"text": li(DEFS) + "<p>Revenue and orders count valid orders only (report filter, hidden + locked); "
                                           "the Delivery page adds a page filter for delivered orders.</p>"})],
                     [("Words Q&A understands", "Synonyms in cultures/en-US.tmdl", None,
                       {"text": li([(n, ", ".join(w)) for (_, n), w in SYNONYMS.items()])})]]}


def page_seller_detail():
    code, sid = one(f"select s.seller_code, s.seller_id {ITEMS} join dim_seller s using (seller_id) {V} group by 1, 2 order by sum(price) desc limit 1")
    rev, n, review, late, fs = one(f"""select sum(price), count(distinct order_id), avg(review_score),
        count(*) filter (where is_late) / nullif(count(*) filter (where is_late is not null), 0), sum(freight) / (sum(price) + sum(freight))
        {ITEMS} {V} and seller_id = '{sid}'""")
    m = rows(f"select month_start m, revenue, orders, late_rate, avg_review from seller_monthly where seller_id = '{sid}' order by 1")
    cat = rows(f"""select category c, sum(price) rev {ITEMS} join dim_product using (product_id) {V} and seller_id = '{sid}'
                   group by 1 order by 2 desc limit 8""")
    ln = lambda y, name, c, axis="y", fmt=",.0f": dict(type="scatter", mode="lines", name=name, x=m["m"], y=y, yaxis=axis,
                                                     line=dict(color=c, width=2), hovertemplate=f"%{{x|%b %Y}}: %{{y:{fmt}}}<extra>{name}</extra>")
    return {"kpis": [kpi("Seller", code, "drillthrough filter"), kpi("Revenue", brl(rev)), kpi("Order Count", num(n)),
                     kpi("Item Avg Review", num(review, 2)), kpi("Seller Late Rate %", pct(late)), kpi("Freight Share %", pct(fs))],
            "rows": [[("Revenue and orders per month", "From the Seller Monthly snapshot table", 630,
                       fig([ln(m["revenue"], "Revenue", BLUE), ln(m["orders"], "Orders", ORANGE, "y2")], showlegend=True, legend=LEGEND,
                           yaxis2=dict(overlaying="y", side="right", showgrid=False, tickfont=dict(color=MUTED, size=10)),
                           xaxis=dict(tickformat="%b %Y"), margin=dict(l=56, r=44, t=8, b=36))),
                      ("What the seller sells", "Revenue by category", None,
                       bars(cat["c"], cat["rev"], fmt=",.3s", horizontal=True, prefix="R$", margin=dict(l=170, r=56, t=8, b=8)))],
                     [("Late rate and review per month", "Late rate (left axis) against the average review (right axis)", None,
                       fig([ln(m["late_rate"], "Late rate", BAD, fmt=".1%"), ln(m["avg_review"], "Avg review", BLUE, "y2", ".2f")],
                           showlegend=True, legend=LEGEND, yaxis=dict(tickformat=".0%"),
                           yaxis2=dict(overlaying="y", side="right", showgrid=False, range=[1, 5], tickfont=dict(color=MUTED, size=10)),
                           xaxis=dict(tickformat="%b %Y"), margin=dict(l=56, r=44, t=8, b=36)))]]}


def page_tt_category():
    c, g = one(f"select category, category_group {ITEMS} join dim_product using (product_id) {V} group by 1, 2 order by sum(price) desc limit 1")
    rev, review, late, buyers = one(f"""select sum(price), avg(review_score),
        count(*) filter (where is_late) / nullif(count(*) filter (where is_late is not null), 0), count(distinct customer_unique_id)
        {ITEMS} join dim_product using (product_id) {V} and category = '{c}'""")
    grp = one(f"select sum(price) {ITEMS} join dim_product using (product_id) {V} and category_group = '{g}'")[0]
    m = rows(f"""select date_trunc('month', order_date)::date m, sum(price) rev {ITEMS} join dim_product using (product_id) {V}
                 and category = '{c}' group by 1 order by 1 desc limit 12""")
    return {"kpis": [kpi("Revenue", brl(rev), c), kpi("Item Avg Review", num(review, 2)), kpi("Seller Late Rate %", pct(late)),
                     kpi("Category Share in Group %", pct(rev / grp), g), kpi("Customers Buying Category", num(buyers))],
            "rows": [[("Last 12 months", f"{c} · revenue per month", 360,
                       fig([dict(type="scatter", mode="lines", x=m["m"], y=m["rev"], line=dict(color=BLUE, width=2),
                                 hovertemplate="%{x|%b %Y}: R$%{y:,.0f}<extra></extra>")],
                           yaxis=dict(visible=False), xaxis=dict(tickformat="%b %y"), margin=dict(l=8, r=8, t=8, b=28)))]]}


# ------------------------------------------------------------------ phase D panels (same rules as the D measures in build_model.py)
def trend_line(x, y):
    """Analytics-pane trend line: least-squares fit over the plotted points."""
    k = np.arange(len(y))
    a, b = np.polyfit(k, np.asarray(y, float), 1)
    return dict(type="scatter", mode="lines", x=x, y=list(a * k + b), line=dict(color=MUTED, width=1, dash="dot"), hoverinfo="skip")


def sales_row5():
    # 10-s13: Revenue YoY Δ for Year = 2018: 2018 revenue minus the same dates of 2017, up to the last date with data
    wf = rows(f"""with l as (select max(order_date) d {ITEMS} {V})
                  select category_group g, sum(price) filter (where year(order_date) = 2018)
                         - sum(price) filter (where year(order_date) = 2017 and order_date <= (select d - interval 1 year from l)) dlt
                  {ITEMS} join dim_product using (product_id) {V} group by 1 order by 2 desc""")
    # 10-s15: ETS forecast table (src/predict.py) next to the actual Jan 2017 - Aug 2018 line
    act = rows(f"""select date_trunc('month', order_date)::date m, sum(price) rev {ITEMS} {V}
                   and order_date between date '2017-01-01' and date '2018-08-31' group by 1 order by 1""")
    fc = rows("select month_start m, forecast_revenue f, lower_80 lo, upper_80 hi from revenue_forecast order by 1")
    band = dict(type="scatter", mode="lines", x=fc["m"] + fc["m"][::-1], y=fc["hi"] + fc["lo"][::-1], fill="toself",
                fillcolor="rgba(42,120,214,.15)", line=dict(width=0), name="80% interval", hoverinfo="skip")
    return [("Who drove 2018 growth", "Revenue change vs same months of 2017, by category group · total = growth", None,
             fig([dict(type="waterfall", x=wf["g"] + ["Total"], y=wf["dlt"] + [sum(wf["dlt"])], measure=["relative"] * len(wf["g"]) + ["total"],
                       increasing=dict(marker=dict(color=GOOD)), decreasing=dict(marker=dict(color=BAD)), totals=dict(marker=dict(color=NAVY)),
                       texttemplate="R$%{y:,.3s}", textposition="outside", textfont=dict(size=9, color=INK2), cliponaxis=False,
                       connector=dict(line=dict(color="#E6E4DD")), hovertemplate="%{x}: R$%{y:,.0f}<extra></extra>")],
                 yaxis=dict(tickprefix="R$", tickformat="~s"), margin=dict(l=56, r=12, t=20, b=60))),
            ("Revenue forecast Sep–Dec 2018", "ETS (additive damped trend) · band = 80% prediction interval", None,
             fig([band,
                  dict(type="scatter", mode="lines", name="Actual", x=act["m"], y=act["rev"], line=dict(color=BLUE, width=2),
                       hovertemplate="%{x|%b %Y}: R$%{y:,.0f}<extra>Actual</extra>"),
                  dict(type="scatter", mode="lines+markers", name="Forecast", x=fc["m"], y=fc["f"], line=dict(color=ORANGE, width=2, dash="dot"),
                       hovertemplate="%{x|%b %Y}: R$%{y:,.0f}<extra>Forecast</extra>")],
                 showlegend=True, legend=LEGEND, yaxis=dict(tickprefix="R$", tickformat="~s"), xaxis=dict(tickformat="%b %Y")))]


SPEND_BANDS = [("Under R$100", 0, 100), ("R$100–250", 100, 250), ("R$250–500", 250, 500), ("R$500–1,000", 500, 1000),
               ("R$1,000+", 1000, 1e6)]


def customers_row4():
    rpc = rows(f"select customer_unique_id c, sum(price) r {ITEMS} {V} group by 1")["r"]
    band = [(n, sum(lo <= r < hi for r in rpc), sum(r for r in rpc if lo <= r < hi)) for n, lo, hi in SPEND_BANDS]
    churned, risk, active = one("""select count(*) filter (where recency_days >= 180), count(*) filter (where recency_days >= 90 and recency_days < 180),
                                   count(*) from dim_customer where valid_orders > 0""")
    clv = rows("""select rfm_segment s, avg(predicted_clv_12m) v from dim_customer where valid_orders > 0
                  group by s, rfm_segment_order order by rfm_segment_order""")
    big = lambda label, v: f"<div style='margin:2px 0 8px'><div style='font-size:12px;color:{MUTED}'>{label}</div><div style='font-size:22px;font-weight:600'>{v}</div></div>"
    return [("Customers by spend band", "Band = revenue per customer in the current filters (dynamic)", None,
             fig([dict(type="bar", x=[b[0] for b in band], y=[b[1] for b in band], marker=dict(color=BLUE, cornerradius=4),
                       customdata=[b[2] for b in band], texttemplate="%{y:,.0f}", textposition="outside", cliponaxis=False,
                       textfont=dict(size=10, color=INK2), hovertemplate="%{x}: %{y:,} customers · R$%{customdata:,.0f}<extra></extra>")],
                 yaxis=dict(visible=False), margin=dict(l=12, r=12, t=20, b=36))),
            ("Churn: no purchase for 180+ days", "At risk = 90–179 days since the last purchase", 420,
             {"text": big("Churn Rate %", pct(churned / active)) + big("Churned Customers", num(churned))
                      + big("At-Risk Customers (90–180d)", num(risk))
                      + "<p>97% of customers buy once, so “churn” here is mostly customers who never came back.</p>"}),
            ("Predicted 12-month value per customer", "BG/NBD × Gamma-Gamma (src/predict.py) · by RFM segment", None,
             bars(clv["s"], clv["v"], fmt=",.2f", horizontal=True, prefix="R$"))]


ODDS_LATE = 8.97  # odds ratio of late delivery in the bad-review logit (src/analysis.py), same constant as the DAX


def delivery_row5():
    # 10-s12: all orders (lifts the valid + delivered filters), each stage keeps the conditions before it
    f = one("""select count(*), count(approval_hours), count(*) filter (where approval_hours is not null and days_to_carrier is not null),
                      count(*) filter (where approval_hours is not null and days_to_carrier is not null and is_delivered),
                      count(*) filter (where approval_hours is not null and days_to_carrier is not null and is_delivered and review_score is not null)
               from fact_orders""")
    stages = ["Purchased", "Approved", "Handed to carrier", "Delivered", "Reviewed"]
    # 10-s16 what-if: page context = valid + delivered
    late, bad, p_on = one("""select count(*) filter (where is_late) / count(*), count(*) filter (where review_score <= 2) / count(review_score),
                                    count(*) filter (where review_score <= 2 and not is_late) / count(review_score) filter (where not is_late)
                             from fact_orders where is_valid and is_delivered""")
    o_late = p_on / (1 - p_on) * ODDS_LATE
    p_late = o_late / (1 + o_late)

    def whatif(pp):
        proj = max(late - pp / 100, 0)
        pbad = bad - (late - proj) * (p_late - p_on)
        return fig([dict(type="bar", x=["Late now", "Late projected", "Bad review now", "Bad review projected"],
                         y=[late, proj, bad, pbad], marker=dict(color=[MUTED, BLUE, MUTED, BLUE], cornerradius=4),
                         texttemplate="%{y:.1%}", textposition="outside", cliponaxis=False, textfont=dict(size=10, color=INK2),
                         hovertemplate="%{x}: %{y:.2%}<extra></extra>")],
                   yaxis=dict(visible=False), margin=dict(l=12, r=12, t=24, b=40),
                   title=dict(text=f"Bad Review Δ (what-if): {(pbad - bad) * 100:+.1f} pp", font=dict(size=11, color=INK2), x=0.02, y=0.98))

    bins = rows("""select floor(delay_days / 3) * 3 b, count(*) filter (where review_score <= 2) / count(review_score) r, count(*) n
                   from fact_orders where is_valid and is_delivered and delay_days is not null group by 1 having count(*) >= 100 order by 1""")
    return [("Order lifecycle funnel", "Purchased → approved → carrier → delivered → reviewed · all orders", None,
             fig([dict(type="funnel", y=stages, x=f, marker=dict(color=BLUE), texttemplate="%{value:,}<br>%{percentInitial:.0%}",
                       textfont=dict(size=10), hovertemplate="%{y}: %{x:,}<extra></extra>")], margin=dict(l=120, r=12, t=8, b=8))),
            ("What if: cut the late rate by … percentage points",
             f"Late odds × {ODDS_LATE} (logit) · on-time bad rate {pct(p_on)} vs implied late {pct(p_late)}", None,
             switch(**{f"{pp} pp": whatif(pp) for pp in (0, 2, 4, 6, 8, 10)})),
            ("Bad reviews by days vs promise (3-day bins)", "Grouping: Delay Days (bins) · < 0 = early, > 0 = late · bins with ≥ 100 orders",
             None, fig([dict(type="bar", x=bins["b"], y=bins["r"], customdata=bins["n"],
                             marker=dict(color=[BAD if b > 0 else BLUE for b in bins["b"]], cornerradius=2),
                             hovertemplate="%{x} to +2 days: %{y:.1%} bad · %{customdata:,} orders<extra></extra>")],
                       yaxis=dict(tickformat=".0%"), xaxis=dict(title=dict(text="Delay Days (bins)", font=dict(size=10))),
                       margin=dict(l=44, r=8, t=8, b=44)))]


def sellers_row5(sel):
    t = rows("select tier_2017 a, tier_2018 b, sellers n from seller_tier_transition order by tier_2017_order, tier_2018_order")
    cols = list(dict.fromkeys(t["b"]))
    tot = {a: sum(n for aa, n in zip(t["a"], t["n"]) if aa == a) for a in t["a"]}
    cell = {(a, b): n for a, b, n in zip(t["a"], t["b"], t["n"])}
    # 10-s2: with no slicer the selection is every seller, so the dynamic tier is the cumulative share over all of them
    total, cum, abc = sum(sel["rev"]), 0, []
    for r in sel["rev"]:
        cum += r
        abc.append("A" if cum / total <= .5 else "B" if cum / total <= .8 else "C")
    return [("Seller tier 2017 → 2018 (Jan–Aug)", "Rows = 2017 tier (New = no 2017 sale) · cells = sellers · share of the row", 620,
             {"columns": ["Tier 2017"] + cols,
              "rows": [[a] + [f"{cell[(a, b)]:,} · {pct(cell[(a, b)] / tot[a], 0)}" if (a, b) in cell else "" for b in cols] for a in tot],
              "heat": list(range(1, len(cols) + 1)), "colors": ["#FFFFFF", BLUE]}),
            ("Lifetime tier vs tier in this selection", "Pick a year or category: Seller ABC (dynamic) re-ranks, Pareto Tier doesn't · top 40",
             None, {"columns": ["Seller", "Pareto Tier", "Seller ABC (dynamic)", "Seller Rank (selection)", "Revenue"],
                    "rows": [[c, tt.split(" ")[0], d, str(i + 1), brl(r)] for i, (c, tt, d, r) in
                             enumerate(list(zip(sel["code"], sel["tier"], abc, sel["rev"]))[:40])], "bar": 4}, "scroll")]


def page_why():
    """10-s17. Power BI uses a decomposition tree and key influencers; the twin shows the same splits as static bars."""
    review, bad, late = one("""select avg(review_score), count(*) filter (where review_score <= 2) / count(review_score),
                                      count(*) filter (where is_late) / count(*) from fact_orders where is_valid and is_delivered""")
    by = lambda col: rows(f"""select date_trunc('month', {col})::date m, count(*) filter (where is_late) / count(*) late,
                                     count(*) filter (where review_score <= 2) / count(review_score) bad
                              from fact_orders where is_valid and is_delivered group by 1 having count(*) >= 100 order by 1""")
    om, pm = by("order_date"), by("estimated_date")
    split = lambda col, join="": rows(f"""select {col} k, count(*) filter (where review_score <= 2) / count(review_score) r, count(review_score) n
                                          from fact_orders o {join} where o.is_valid and o.is_delivered group by 1 having count(review_score) >= 200
                                          order by 2 desc""")
    cust = "join dim_customer c using (customer_unique_id)"
    sp = {"Delivery bucket": split("o.delivery_bucket"), "Region": split("c.region", cust), "Payment type": split("o.main_payment_type")}
    # key influencers style: bad-review rate of the value ÷ bad-review rate of every other order
    ki = []
    for f, d in sp.items():
        for k, r, n in zip(d["k"], d["r"], d["n"]):
            rest_n = sum(d["n"]) - n
            rest = (sum(rr * nn for rr, nn in zip(d["r"], d["n"])) - r * n) / rest_n if rest_n else None
            if rest:
                ki.append((f"{f} is {k}", r / rest))
    ki = sorted(ki, key=lambda x: -x[1])[:8]
    return {"kpis": [kpi("Avg Review", num(review, 2)), kpi("Bad Review %", pct(bad)), kpi("Late Rate %", pct(late)),
                     kpi("Late Rate % (by Promise Month)", pct(late), "same total, different month axis")],
            "rows": [[("Step 2 · bad reviews move with late deliveries",
                       "Late rate on the month the delivery was promised · the Feb–Mar 2018 spike drags reviews down", None,
                       fig([dict(type="scatter", mode="lines", name="Bad Review %", x=om["m"], y=om["bad"], line=dict(color=BAD, width=2),
                                 hovertemplate="%{x|%b %Y}: %{y:.1%}<extra>Bad Review %</extra>"),
                            dict(type="scatter", mode="lines", name="Late Rate % (by Promise Month)", x=pm["m"], y=pm["late"],
                                 line=dict(color=BLUE, width=2), hovertemplate="%{x|%b %Y}: %{y:.1%}<extra>Late (promise month)</extra>")],
                           showlegend=True, legend=LEGEND, yaxis=dict(tickformat=".0%"), xaxis=dict(tickformat="%b %Y")))],
                     [("Step 3 · where the bad reviews sit", "Decomposition tree in Power BI · here: Bad Review % by each split (≥ 200 reviews)",
                       None, switch(**{k.replace(" ", "_"): bars(d["k"], d["r"], fmt=".1%", horizontal=True,
                                                                   colors=[BAD if r >= bad * 1.5 else BLUE for r in d["r"]])
                                       for k, d in sp.items()})),
                      ("Step 4 · what makes a review bad", "Key influencers in Power BI · here: × likelihood of a bad review vs all other orders",
                       None, bars([k for k, _ in ki], [v for _, v in ki], fmt=".1f", horizontal=True, colors=BAD,
                                  margin=dict(l=230, r=40, t=8, b=8)))]]}


BUILD = {"why-reviews": page_why, "overview": page_overview, "sales-products": page_sales, "customers-retention": page_customers,
         "delivery-satisfaction": page_delivery, "sellers-logistics": page_sellers, "about": page_about,
         "seller-detail": page_seller_detail, "tt-category": page_tt_category}

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Marketplace Analytics — Olist</title>
<style>
:root{--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--hair:#E6E4DD;--surface:#FFFFFF;--plane:#F4F3EF;--navy:%(navy)s;--navy-text:#B7D3F6}
*{box-sizing:border-box}
body{margin:0;background:#E9E8E3;font-family:"Segoe UI",system-ui,-apple-system,sans-serif;color:var(--ink)}
.app{display:grid;grid-template-columns:232px 1fr;min-height:100vh;max-width:1600px;margin:0 auto;background:linear-gradient(90deg,var(--navy) 229px,#1c5cab 229px 232px,var(--plane) 0)}
aside{background:var(--navy);border-right:3px solid #1c5cab;padding:28px 12px;display:flex;flex-direction:column;position:sticky;top:0;height:100vh}
.brand{padding:0 12px 20px;border-bottom:1px solid #184f95;margin-bottom:16px}
.brand small{display:block;font-size:11px;letter-spacing:.14em;font-weight:600;color:#9EC5F4}
.brand b{font-size:23px;color:#fff;font-weight:600}
nav button{all:unset;display:block;width:100%%;box-sizing:border-box;padding:11px 16px;margin:3px 0;border-radius:8px;color:var(--navy-text);font-size:14px;cursor:pointer}
nav button:hover{background:rgba(255,255,255,.07)}
nav button[aria-current=true]{background:#1C5CAB;color:#fff;font-weight:600}
aside footer{margin-top:auto;padding:16px 12px 0;border-top:1px solid #184f95;font-size:11px;line-height:1.6;color:var(--navy-text)}
main{padding:22px 24px 24px;min-width:0}
header{display:flex;justify-content:space-between;gap:24px;border-bottom:1px solid var(--hair);padding-bottom:12px;margin-bottom:12px}
header h1{margin:0;font-size:26px;font-weight:600}
header p{margin:4px 0 0;color:var(--ink2);font-size:13px;max-width:760px}
.chips{display:flex;gap:8px;align-items:flex-start;flex-wrap:wrap}
.chip{border:1px solid var(--hair);border-radius:8px;background:var(--surface);padding:6px 10px;font-size:11px;color:var(--muted);white-space:nowrap}
.chip b{display:block;color:var(--ink);font-size:13px;font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:16px;margin-bottom:16px}
.kpi,.panel{background:var(--surface);border:1px solid var(--hair);border-radius:12px}
.kpi{padding:14px 16px}
.kpi span{font-size:12px;color:var(--muted)}
.kpi b{display:block;font-size:28px;font-weight:600;margin-top:4px;letter-spacing:-.01em}
.kpi i{display:block;font-style:normal;font-size:11px;color:var(--muted);margin-top:2px}
.row{display:flex;gap:16px;margin-bottom:16px}
.panel{padding:12px 14px 8px;min-width:0;flex:1 1 0;display:flex;flex-direction:column}
.panel.fixed{flex:none}
.panel h3{margin:0;font-size:15px;font-weight:600}
.panel p{margin:2px 0 6px;font-size:12px;color:var(--muted)}
.chart{flex:1;min-height:0}
.row.r2 .panel{height:332px}.row.r3 .panel{height:316px}
.scroll{overflow-y:auto}
.seg{display:flex;gap:6px;margin:0 0 6px}.seg button{all:unset;cursor:pointer;font-size:11px;padding:3px 10px;border:1px solid var(--hair);border-radius:6px;color:var(--ink2)}
.seg button[aria-pressed=true]{background:#2A78D6;border-color:#2A78D6;color:#fff}
.text{font-size:13px;color:var(--ink2);line-height:1.5;overflow-y:auto}.text ul{margin:4px 0;padding-left:18px}.text b{color:var(--ink)}
.side{display:flex;gap:8px;padding:12px 4px 0}.side button{all:unset;cursor:pointer;flex:1;text-align:center;font-size:12px;color:var(--navy-text);border:1px solid #2B5A94;border-radius:8px;padding:7px 0}
.info{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border:1px solid var(--hair);border-radius:50%%;font-size:12px;color:var(--ink2);cursor:pointer;margin-left:8px;vertical-align:middle}
table{width:100%%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th{font-size:11px;color:var(--muted);font-weight:600;text-align:right;padding:8px 10px;border-bottom:1px solid var(--hair);position:sticky;top:0;background:var(--surface)}
td{text-align:right;padding:7px 8px;border-bottom:1px solid #ECEBE6;white-space:nowrap}
th:first-child,td:first-child{text-align:left;font-weight:600;white-space:nowrap}
table.dense{font-size:10.5px;table-layout:fixed}table.dense th,table.dense td{padding:6px 2px;text-align:center;border-bottom:1px solid #fff}
table.dense th:first-child,table.dense td:first-child{width:36px;padding-right:4px}
table.hours{font-size:9px;letter-spacing:-.03em}table.hours th,table.hours td{padding:6px 0;border-right:1px solid #fff}
table.cohort th:first-child,table.cohort td:first-child{width:72px}
@media (max-width:1100px){.app{grid-template-columns:1fr}aside{position:static;height:auto}.row{flex-direction:column}.panel{width:100%%!important}.kpis{grid-template-columns:repeat(2,1fr)}}
</style></head>
<body><div class="app">
<aside><div class="brand"><small>OLIST</small><b>Marketplace Analytics</b></div><nav id="nav"></nav>
<div class="side"><button onclick="render('about')" title="Definitions, synonyms, Q&A">? Help</button><button onclick="render(current)" title="Reset filters (every slicer back to All)">↺ Reset</button></div>
<footer>Source: Kaggle · Olist 2016–2018<br>Pipeline: Python · DuckDB SQL · Power BI<br>Static preview of the Power BI report</footer></aside>
<main id="main"></main></div>
<script>%(plotly)s</script>
<script>
const PAGES = %(pages)s, DEFS = %(defs)s;
const nav = document.getElementById('nav'), main = document.getElementById('main');
function el(tag, attrs = {}, html = '') { const e = document.createElement(tag); Object.assign(e, attrs); e.innerHTML = html; return e; }
function mix(a, b, t) { const p = h => [1,3,5].map(i => parseInt(h.slice(i, i + 2), 16)); const x = p(a), y = p(b);
  return '#' + x.map((v, i) => Math.round(v + (y[i] - v) * t).toString(16).padStart(2, '0')).join(''); }
const val = v => parseFloat(String(v).replace(/[^0-9.\\-]/g, ''));
function table(body) {
  const heat = body.heat || [], cells = body.rows.flatMap(r => heat.map(i => val(r[i]))).filter(v => !isNaN(v));
  const lo = Math.min(...cells), hi = Math.max(...cells);
  const bmax = body.bar !== undefined ? Math.max(...body.rows.map(r => val(r[body.bar]))) : 0;
  return `<table class="${body.cls || ''}"><thead><tr>` + body.columns.map(h => `<th>${h}</th>`).join('') + '</tr></thead><tbody>' +
    body.rows.map(row => '<tr>' + row.map((v, i) => {
      let style = '';
      if (heat.includes(i) && !isNaN(val(v))) { const t = (val(v) - lo) / ((hi - lo) || 1);
        style = ` style="background:${mix(body.colors[0], body.colors[1], t)};color:${t > .6 && body.colors[1] !== '#F19A99' ? '#fff' : 'inherit'}"`; }
      if (i === body.bar) style = ` style="background:linear-gradient(90deg,#CDE2FB ${val(v) / bmax * 100}%%,transparent 0)"`;
      return `<td${style}>${v}</td>`; }).join('') + '</tr>').join('') + '</tbody></table>';
}
let current = 'overview';
function render(id) {
  current = id;
  const page = PAGES.find(p => p.id === id);
  [...nav.children].forEach(b => b.setAttribute('aria-current', b.dataset.id === id));
  main.innerHTML = '';
  const head = el('header');
  head.append(el('div', {}, `<h1>${page.name}${id === 'about' ? '' : `<span class="info" title="${DEFS}" onclick="render('about')">i</span>`}</h1><p>${page.subtitle}</p>`));
  head.append(el('div', {className: 'chips'}, ['Year', 'Customer region', page.third].map(k => `<div class="chip">${k}<b>All</b></div>`).join('')));
  main.append(head);
  const k = el('section', {className: 'kpis'});
  page.kpis.forEach(x => k.append(el('div', {className: 'kpi'}, `<span>${x.label}</span><b>${x.value}</b>` + (x.note ? `<i>${x.note}</i>` : ''))));
  if (page.kpis.length) main.append(k);
  page.rows.forEach((row, ri) => {
    const r = el('section', {className: 'row ' + (ri ? 'r3' : 'r2')});
    row.forEach(([title, sub, width, body, cls]) => {
      const p = el('div', {className: 'panel' + (width ? ' fixed' : '')}, `<h3>${title}</h3><p>${sub}</p>`);
      if (width) p.style.width = width + 'px';
      const c = el('div', {className: 'chart' + (cls === 'scroll' ? ' scroll' : '')});
      p.append(c); r.append(p);
      if (body.columns) c.innerHTML = table(body);
      else if (body.text) { c.style.overflowY = 'auto'; c.append(el('div', {className: 'text'}, body.text)); }
      else if (body.switch) {
        const seg = el('div', {className: 'seg'}), inner = el('div');
        const show = i => { [...seg.children].forEach((b, j) => b.setAttribute('aria-pressed', i === j)); const f = body.switch.figs[i];
          inner.style.height = f.layout.height ? f.layout.height + 'px' : '100%%';
          Plotly.react(inner, f.data, {...f.layout, autosize: true}, {displayModeBar: false, responsive: true}); };
        body.switch.labels.forEach((l, i) => { const b = el('button', {}, l); b.onclick = () => show(i); seg.append(b); });
        p.insertBefore(seg, c); c.append(inner); requestAnimationFrame(() => show(0)); }
      else { const inner = el('div'); if (body.layout.height) inner.style.height = body.layout.height + 'px'; else inner.style.height = '100%%';
        c.append(inner);
        requestAnimationFrame(() => Plotly.newPlot(inner, body.data, {...body.layout, autosize: true}, {displayModeBar: false, responsive: true})); }
    });
    main.append(r);
  });
  history.replaceState(null, '', '#' + id);
}
PAGES.filter(p => !p.hidden).forEach(p => { const b = el('button', {}, p.name); b.dataset.id = p.id; b.onclick = () => render(p.id); nav.append(b); });
render((location.hash || '#overview').slice(1));
</script></body></html>"""


def main():
    pages = []
    for pid, name, subtitle, third in PAGES:
        pages.append({"id": pid, "name": name, "subtitle": subtitle, "third": third, "hidden": pid in HIDDEN, **BUILD[pid]()})
        print(f"  ✓ {name}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(HTML % {"plotly": get_plotlyjs(), "pages": json.dumps(pages, default=str), "navy": NAVY,
                               "defs": json.dumps(" | ".join(f"{k}: {v}" for k, v in DEFS[:3]).replace('"', "'"))})
    print(f"  ✓ wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
