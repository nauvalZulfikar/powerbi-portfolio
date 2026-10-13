"""Static HTML twin of the Power BI report — same pages, layout, palette and measure definitions.

    uv run python src/build_preview.py      -> reports/dashboard.html (self-contained, opens offline)

Power BI Desktop is Windows-only; this lets the report be reviewed anywhere. It shows the
report's default state: all cities, vendors, categories and ABC classes, Analyze By = Vendor,
service level 95%, review every 7 days. Every number is computed in DuckDB
from the same marts, with the same rules as the DAX.

Two things are approximations, not copies: Power BI's analytics-pane forecast (exponential smoothing)
and anomaly detection (a Microsoft service) run inside Power BI, so the twin re-creates them with a
Holt-Winters model fitted here in Python. The phone views (#mobile, #restock_mobile) use the positions
build_report.py writes to mobile.json.

Also approximated: the AI visuals on Diagnose (key influencers = out-of-stock share per factor value vs the
rest; decomposition tree = expanded along its largest branch; Q&A = one example question and its answer),
the bookmark toggles (shown in their default state, the other state as a tab label) and the report-page
tooltips (folded into the hover text). Drillthrough pages show one example item / vendor.

Report filter: store 46 (closed in 2016) is left out of every table that carries a store, exactly like the
report-level filter Store[Closed 2016] = FALSE. Forecast backtest tables have no store key, so they keep it.
"""
import datetime as dt
import itertools
import json
import math
from pathlib import Path

import duckdb
from plotly.offline import get_plotlyjs

import build_report
from build_model import OVERSTOCK_DAYS, PACK, PACK_LAST, PACK_UNKNOWN, TIME_ITEMS
from build_report import (ACCENT, AMBER, DETAIL_PAGES, DUE_C, HELP, INK, INK2, MUTED, PAGES, PHONE_W, SIDE, SIDE_LINE,
                          SIDE_TEXT, SLOW_C)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "dashboard.html"
# The warehouse is attached read-only; mart.* here are views that apply the report-level filter (no store 46).
con = duckdb.connect()
con.execute(f"attach '{ROOT / 'data' / 'warehouse' / 'inventory.duckdb'}' as w (read_only)")
con.execute("create schema mart")
OPEN = "store in (select store from w.mart.dim_store where not closed_2016)"
REPORT_FILTER = {"dim_store": "where not closed_2016", "fact_stock": f"where {OPEN}", "fact_sales_daily": f"where {OPEN}",
                 "fact_receipts": f"where {OPEN}", "fact_shipment": f"where {OPEN}",
                 "pq_stock_counts": 'where "Count Store" in (select store from w.mart.dim_store where not closed_2016)'}
for (t,) in con.execute("select table_name from information_schema.tables where table_catalog = 'w' and table_schema = 'mart'").fetchall():
    con.execute(f"create view mart.{t} as select * from w.mart.{t} {REPORT_FILTER.get(t, '')}")


def rows(sql):
    cur = con.execute(sql)
    names, data = [d[0] for d in cur.description], cur.fetchall()
    return {n: [r[i] for r in data] for i, n in enumerate(names)}


def one(sql):
    return con.execute(sql).fetchone()


# ------------------------------------------------------------------ formats
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


def hbars(labels, values, colors, text=None, left=150):
    """Horizontal bars, first item on top, value labels outside."""
    t = dict(type="bar", orientation="h", y=labels, x=values, marker=dict(color=colors, cornerradius=4),
             text=text or [usd(v, True) for v in values], textposition="outside", cliponaxis=False,
             textfont=dict(size=10, color=INK2), hovertemplate="%{y}: %{text}<extra></extra>")
    return fig([t], yaxis=dict(autorange="reversed", showgrid=False, zeroline=False, tickprefix="", tickformat="",
                               tickfont=dict(color=INK2, size=10)),
               xaxis=dict(visible=False, zeroline=True, range=[min(0, *values) * 1.4, max(0, *values) * 1.3]),
               margin=dict(l=left, r=16, t=4, b=8))


def lines(x, series, **layout):
    return fig([dict(type="scatter", mode="lines+markers" if len(x) <= 12 else "lines", name=name, x=x, y=y,
                     line=dict(color=col, width=2.2, dash=dash), marker=dict(size=5, color=col),
                     hovertemplate=f"{name} · %{{x|%b %Y}}: {fmt}<extra></extra>")
                for name, y, col, dash, fmt in series],
               showlegend=True, legend=LEGEND, xaxis=dict(tickformat="%b", dtick="M1"), **layout)


def table(columns, body, shade=(), left=(0,)):
    return {"columns": columns, "rows": body, "shade": list(shade), "left": list(left)}


def wrap(t, n=13):
    """Break a long axis label at the space nearest its middle."""
    if len(t) <= n or " " not in t:
        return t
    i = min((i for i, ch in enumerate(t) if ch == " "), key=lambda i: abs(i - len(t) / 2))
    return t[:i] + "<br>" + t[i + 1:]


STATUS_STYLE = {"Out of stock": ("✖", DUE_C), "Reorder now": ("!", AMBER), "Healthy": ("✓", INK),
                "Overstock": ("⚑", SLOW_C), "Dead stock": ("⚑", SLOW_C), "Inactive": ("", MUTED)}


def stcell(status):
    """Status is never colour alone: icon + label, coloured (the report's rules + icon conditional formatting)."""
    icon, c = STATUS_STYLE[status]
    return f'<span style="color:{c};font-weight:600;white-space:nowrap">{icon} {status}</span>'


def gradient(text, v, top):
    """Two-colour background gradient (SURFACE -> #F3B48F) on a measure column."""
    f = 0 if not top else max(0, min(1, v / top))
    mix = lambda a, b: round(a + (b - a) * f)
    rgb = [mix(int("FCFCFB"[i:i + 2], 16), int("F3B48F"[i:i + 2], 16)) for i in (0, 2, 4)]
    return {"v": text, "t": "", "s": f"background:rgb({rgb[0]},{rgb[1]},{rgb[2]})"}


def databar(text, v, top):
    w = 0 if not top or v is None else max(0, min(100, 100 * v / top))
    return {"v": text, "t": "", "s": f"background:linear-gradient(to right,#CFC3EE {w:.0f}%,transparent {w:.0f}%)"}


def arrow(rate):
    """KPI icon on Out of Stock Rate: ▲ under 15%, ► 15–20%, ▼ above 20% (shape + colour)."""
    if rate is None:
        return "—"
    icon, c = ("▲", ACCENT) if rate < 0.15 else ("►", AMBER) if rate <= 0.2 else ("▼", DUE_C)
    return f'<span style="color:{c}">{icon}</span> {rate:.1%}'


def kpis(items):
    return [{"label": k, "value": v, "t": t} for k, v, *t in [(*i, None) if len(i) == 2 else i for i in items]]



def vbars(labels, values, color_, text, **layout):
    t = dict(type="bar", x=labels, y=values, marker=dict(color=color_, cornerradius=3), text=text,
             textposition="outside", cliponaxis=False, textfont=dict(size=10, color=INK2),
             hovertemplate="%{x}: %{text}<extra></extra>")
    return fig([t], **layout)


def grouped(x, series, **layout):
    return fig([dict(type="bar", name=n, x=x, y=y, marker=dict(color=c, cornerradius=3),
                     hovertemplate=f"{n} · %{{x}}: %{{y:,}}<extra></extra>") for n, y, c in series],
               barmode="group", showlegend=True, legend=LEGEND, **layout)


NUM_Y = dict(tickprefix="", tickformat=",.0f")
num = lambda v: f"{v:,.0f}"


F = "mart.fact_stock f"
DUE = "status in ('Out of stock', 'Reorder now')"
EXCESS = f"case when daily_forecast > 0 then greatest(0, end_units - {OVERSTOCK_DAYS} * daily_forecast) else end_units end * f.unit_cost"
DIMS = "join mart.dim_store s using (store) join mart.dim_item i using (brand) join mart.dim_vendor v on v.vendor_no = f.vendor_no"

# Pack Size: the same bands as the DAX calculated column on Item (build_model.CALC_COLUMNS).
_ML = "try_cast(i.volume_ml as double)"
PACK_SQL = (f"case when {_ML} is null then '{PACK_UNKNOWN}' "
            + " ".join(f"when {_ML} <= {v} then '{n}'" for v, n in PACK) + f" else '{PACK_LAST}' end")
PACK_ORDER_SQL = (f"case when {_ML} is null then {len(PACK) + 1} "
                  + " ".join(f"when {_ML} <= {v} then {i}" for i, (v, _) in enumerate(PACK)) + f" else {len(PACK)} end")


def base():
    """The headline measures every page shares, in the report's default state."""
    r = one(f"""select count(*) n, sum(end_units) units, sum(stock_cost) sv, sum(sold_units * unit_cost) cogs,
                       count(*) filter (where status = 'Out of stock') oos, count(*) filter (where daily_forecast > 0) selling,
                       count(*) filter (where {DUE}) due_n, sum(order_units) due_u, sum(order_cost) due_v,
                       sum(safety_units * unit_cost) ss_v, sum({EXCESS}) excess, sum(daily_forecast) fpd,
                       sum(stock_cost) filter (where status = 'Overstock') over_v,
                       sum(stock_cost) filter (where status = 'Dead stock') dead_v
                from {F}""")
    k = dict(zip(["n", "units", "sv", "cogs", "oos", "selling", "due_n", "due_u", "due_v", "ss_v", "excess", "fpd",
                  "over_v", "dead_v"], r))
    # Order Shelf Value 2017: Item[List Price 2017] comes from the Power Query merge with the price list.
    k["shelf17"] = one(f"""select sum(order_units * coalesce(p."List Price", f.unit_price)) from {F}
                          left join mart.pq_price_list_2017 p on p."Brand No" = f.brand where {DUE}""")[0]
    k["urgent"] = one(f"select sum(order_cost) from {F} where status = 'Out of stock'")[0]
    # Units On Hand (Closing): LASTNONBLANK date of the Power Query stock counts.
    k["closing"] = one("""select sum("Counted Units") from mart.pq_stock_counts
                          where "Count Date" = (select max("Count Date") from mart.pq_stock_counts)""")[0]
    k["turns"], k["oos_rate"], k["cover"] = k["cogs"] / k["sv"], k["oos"] / k["selling"], k["units"] / k["fpd"]
    j = one("select sum(units), sum(dollars) from mart.fact_sales_daily")
    k["jan_u"], k["jan_v"] = j
    return k


K = base()
money = lambda v: usd(v)


def page_overview():
    st = rows(f"""select s.status, count(f.status) n from mart.dim_status s left join {F} using (status)
                  group by s.status, s.status_sort order by s.status_sort""")
    by = rows(f"""select v.vendor, sum(order_cost) o from {F} {DIMS} group by 1""")
    o = sorted(zip(by["vendor"], by["o"]), key=lambda t: -t[1])[:10]
    px = rows(f"""select {PACK_SQL} pack, sum({EXCESS}) x from {F} join mart.dim_item i using (brand)
                  group by 1 order by 2 desc limit 10""")
    x = list(zip(px["pack"], px["x"]))
    headline = (f"{K['due_n']:,} lines need an order now: {K['due_u']:,.0f} units, {usd(K['due_v'])} at cost, for a 95% "
                f"service level. Meanwhile {usd(K['excess'])} sits in stock beyond {OVERSTOCK_DAYS} days of cover or never sells.")
    return {
        "kpis": kpis([("Stock Value", usd(K["sv"])), ("Stock Turns", f"{K['turns']:.1f}"),
                      ("Out of Stock Rate", f"{K['oos_rate']:.1%}", "bad"), ("Lines To Order", num(K["due_n"])),
                      ("Order Value", usd(K["due_v"])), ("Excess Stock Value", usd(K["excess"]), "bad")]),
        "rows": [
            [("Store × item lines by stock status", "At 31 Dec 2016; a line is one item in one store", 880,
              vbars(st["status"], st["n"], [DUE_C, DUE_C, ACCENT, SLOW_C, SLOW_C, "#B9B8B0"], [num(v) for v in st["n"]],
                    yaxis=NUM_Y)),
             ("In one sentence", "Follows the slicers", 424,
              {"html": f'<div class="headline">{headline}</div><div class="split">'
                       f'<div class="mini"><span>Overstock Value</span><b>{usd(K["over_v"])}</b></div>'
                       f'<div class="mini"><span>Dead Stock Value</span><b>{usd(K["dead_v"])}</b></div></div>'})],
            [("Order value due now, by vendor", "Analyze by: Vendor (also Store / City / Category / ABC class) · top 10", 652,
              hbars([wrap(n, 30) for n, _ in o], [v for _, v in o], DUE_C, text=[usd(v) for _, v in o], left=210)),
             ("Excess stock by pack size", "Drillable: Category ▸ Pack Size ▸ Item, opens on pack size; cost beyond "
              "180 days of cover plus dead stock", 652,
              hbars([n for n, _ in x], [v for _, v in x], SLOW_C, text=[usd(v) for _, v in x], left=120))],
        ]}


def share_tooltip(f, shares):
    """Report-page tooltip field: the hover adds the share (ALLSELECTED measure)."""
    t = f["data"][0]
    t["customdata"] = [f"{x:.1%}" for x in shares]
    t["hovertemplate"] = "%{y}: %{text} · %{customdata} of the selection<extra></extra>"
    return f


RESTOCK_PAGE = "status not in ('Dead stock', 'Inactive')"  # the Restock page filter


def page_restock():
    ve = rows(f"""select v.vendor, sum(order_cost) filter (where {DUE}) o, sum(safety_units) ss, sum(reorder_point) rop,
                         sum(order_units) filter (where {DUE}) u, count(*) filter (where {DUE}) n
                  from {F} {DIMS} where {RESTOCK_PAGE} group by 1 order by o desc nulls last limit 12""")
    li = rows(f"""select s.store_label, i.description, i.size, v.vendor, status, end_units, daily_forecast, f.lead_days,
                         order_units, order_cost
                  from {F} {DIMS} where {DUE} order by order_cost desc limit 12""")
    note = ('<h4>How the order is sized</h4><p class="note">Safety stock = z × √(lead days × daily spread² + daily demand² × '
            'lead-time spread²). Reorder point = demand over the lead time + safety stock. A line at or below it is topped up '
            'to demand over lead time + review period + safety stock. Higher service level = fewer stock-outs, more cash in '
            'stock.</p>')
    return {
        "kpis": kpis([("Lines To Order", num(K["due_n"])), ("Units To Order", num(K["due_u"])),
                      ("Order Value", usd(K["due_v"])), ("Order Shelf Value 2017", usd(K["shelf17"])),
                      ("Safety Stock Value", usd(K["ss_v"])), ("Urgent Order Value", usd(K["urgent"]), "bad")]),
        "rows": [
            [("Order value by vendor", "One purchase order per vendor; hover a bar for its safety stock, reorder point and "
              "units (Line Tip page) · top 12", 880,
              line_tip(hbars(ve["vendor"], ve["o"], DUE_C, text=[usd(v) for v in ve["o"]], left=250), ve)),
             ("", "", 424, {"html": '<div class="split" style="margin:0 0 12px">'
                                    '<div class="chip">What-if: service level<b>95%</b></div>'
                                    '<div class="chip">What-if: review every (days)<b>7</b></div></div>' + note +
                                    '<div class="seg" style="margin-top:auto"><span class="on">Order now</span>'
                                    '<span>Excess</span></div>'})],
            [("Restock list", "Every line due at the selected settings, largest first; % = share of the order shown "
              f"(first 12 of {K['due_n']:,}) · bookmark “Excess” swaps in the excess list", 1320,
              table(["Store", "Item", "Size", "Vendor", "Status", "On hand", "Daily forecast", "Lead days", "Units to order",
                     "Order value", "% of order"],
                    [[s, d, z, v, stcell(st), num(e), f"{fc:.2f}", f"{ld:.1f}", num(u), gradient(usd(c), c, li["order_cost"][0]),
                      f"{c / K['due_v']:.2%}"]
                     for s, d, z, v, st, e, fc, ld, u, c in zip(*li.values())], left=(0, 1, 2, 3, 4)))],
        ]}


def line_tip(f, ve):
    """Report-page tooltip Line Tip: safety stock, reorder point, units and lines to order for the hovered vendor."""
    t = f["data"][0]
    t["customdata"] = [[num(a), num(b), num(c or 0), num(d)] for a, b, c, d in zip(ve["ss"], ve["rop"], ve["u"], ve["n"])]
    t["hovertemplate"] = ("<b>%{y}</b><br>Safety stock units %{customdata[0]}<br>Reorder point units %{customdata[1]}"
                          "<br>Units to order %{customdata[2]}<br>Lines to order %{customdata[3]}<extra></extra>")
    return f


def page_health():
    cb = rows(f"""select b.cover_band, coalesce(sum(f.stock_cost), 0) v from mart.dim_cover_band b
                  left join {F} using (cover_band) group by b.cover_band, b.cover_sort order by b.cover_sort""")
    ab = rows(f"""select a.abc_label, count(*) n, sum(stock_cost) sv,
                         count(*) filter (where status = 'Out of stock') / count(*) filter (where daily_forecast > 0) oos,
                         sum(stock_cost) filter (where status = 'Overstock') ov, sum(stock_cost) filter (where status = 'Dead stock') dv,
                         count(*) filter (where daily_forecast > 0 and cover_days > t.target_cover_days) over_t
                  from {F} join mart.dim_abc a using (abc)
                       join mart.category_target t on t.category = f.category and t.abc = f.abc
                  group by a.abc_label, a.abc_sort order by a.abc_sort""")
    sl = rows(f"""select s.store_label, i.description, i.size, v.vendor, status, end_units, sold_units, cover_days, stock_cost
                  from {F} {DIMS} where status in ('Overstock', 'Dead stock') order by stock_cost desc limit 9""")
    colors = [DUE_C, ACCENT, ACCENT, ACCENT, ACCENT, SLOW_C, SLOW_C, "#B9B8B0"]
    pk = rows(f"""select i.category, {PACK_SQL} pack, {PACK_ORDER_SQL} pack_order, sum(stock_cost) sv, sum({EXCESS}) x
                  from {F} join mart.dim_item i using (brand) group by all order by 1, 3""")
    matrix = []
    for cat in sorted(set(pk["category"])):
        sub = [(n, v, x) for c, n, _, v, x in zip(*pk.values()) if c == cat]
        matrix.append([f"⊟ {cat}", usd(sum(v for _, v, _ in sub)), usd(sum(x for _, _, x in sub))])
        matrix += [[f'<span class="ind">⊞ {n}</span>', usd(v), usd(x)] for n, v, x in sub]
    head = [i for i, r in enumerate(matrix) if r[0].startswith("⊟")]
    br = one(f"select sum(begin_units * unit_cost), sum(received_units * unit_cost), sum(sold_units * unit_cost) from {F}")
    bridge = fig([dict(type="waterfall", x=["Opening stock", "Received", "Sold", "Closing stock"],
                       measure=["relative", "relative", "relative", "total"], y=[br[0], br[1], -br[2], 0],
                       text=[usd(br[0]), usd(br[1]), usd(-br[2]), usd(br[0] + br[1] - br[2])], textposition="outside",
                       cliponaxis=False, textfont=dict(size=10, color=INK2),
                       increasing=dict(marker=dict(color=ACCENT)), decreasing=dict(marker=dict(color=DUE_C)),
                       totals=dict(marker=dict(color=INK2)), connector=dict(line=dict(color="#D9D7CF", width=1)),
                       hovertemplate="%{x}: %{text}<extra></extra>")],
                 yaxis=dict(range=[0, max(br[0] + br[1], br[0]) * 1.15]), margin=dict(l=44, r=8, t=16, b=28))
    top_cover = max((c or 0) for c in sl["cover_days"])
    return {
        "kpis": kpis([("Stock Value", usd(K["sv"])), ("Days of Cover", f"{K['cover']:.0f}"),
                      ("Overstock Value", usd(K["over_v"]), "bad"), ("Dead Stock Value", usd(K["dead_v"]), "bad"),
                      ("Excess Stock Value", usd(K["excess"]), "bad"), ("Out of Stock Lines", num(K["oos"])),
                      ("Units On Hand (Closing)", num(K["closing"]))]),
        "rows": [
            [("Stock value by days of cover", "Units on hand ÷ forecast daily demand", 440,
              vbars(cb["cover_band"], cb["v"], colors, [usd(v) for v in cb["v"]],
                    xaxis=dict(tickangle=-35, tickfont=dict(size=9)), margin=dict(l=44, r=8, t=16, b=64))),
             ("", "", 424, {"stack": [
                 ("", "", 40, {"html": '<div class="seg"><span class="on">By pack size</span><span>By ABC class</span></div>'}),
                 ("Stock by category and pack size", "Bookmark view; the other view is the ABC class table", None,
                  table(["Category / pack size", "Stock value", "Excess stock"], matrix, shade=head))]}, "tight"),
             ("Stock bridge, 1 Jan → 31 Dec 2016", "Opening + received − sold = closing, at unit cost", 424, bridge)],
            [("Overstock and dead stock, largest first", "Candidates for transfers to other stores, markdowns or returns", 1320,
              table(["Store", "Item", "Size", "Vendor", "Status", "On hand", "Sold 2016", "Cover days", "Stock cost"],
                    [[s, d, z, v, stcell(st), num(e), num(so), databar("—" if c is None else num(c), c, top_cover), usd(sc)]
                     for s, d, z, v, st, e, so, c, sc in zip(*sl.values())], left=(0, 1, 2, 3, 4)))],
        ]}


def weekday_tooltip(f, units):
    """January Units by Weekday (TREATAS) as the tooltip field."""
    t = f["data"][0]
    t["customdata"] = [num(u) for u in units]
    t["hovertemplate"] = "%{x}: index %{text} · %{customdata} January units<extra></extra>"
    return f


def page_demand():
    fd = rows("select date, series, units from mart.forecast_daily order by series_sort, date")
    ser = lambda name: ([d for d, s in zip(fd["date"], fd["series"]) if s == name],
                        [u for u, s in zip(fd["units"], fd["series"]) if s == name])
    wd = rows("""select w.weekday, w.weekday_index, coalesce(sum(s.units), 0) u from mart.weekday_profile w
                 left join mart.fact_sales_daily s on strftime(s.date, '%a') = w.weekday
                 group by w.weekday, w.weekday_index, w.weekday_sort order by w.weekday_sort""")
    sc = rows("select method_label, wape_line_week, wape_chain_day, bias from mart.forecast_scores order by method_sort")
    used = "Annual rate × season × weekday"
    series = [("Actual", INK, "solid"), (used, ACCENT, "solid"), ("Average × weekday", DUE_C, "dot"),
              ("Same day last week", MUTED, "dot")]
    fm = rows('select "Forecast Method" m, "Metric" k, "Metric Value" v from mart.pq_forecast_metric order by "Method Order", "Metric"')
    return {
        "kpis": kpis([("January Units Sold", num(K["jan_u"])), ("January Sales", usd(K["jan_v"])),
                      ("Avg Selling Price", f"${K['jan_v'] / K['jan_u']:.2f}"), ("Forecast Units per Day", num(K["fpd"]))]),
        "rows": [
            [("Backtest: chain units per day, January 2016", "Methods trained on 1–24 Jan, scored on 25–31 Jan", 880,
              fig([dict(type="scatter", mode="lines+markers", name=n, x=ser(n)[0], y=ser(n)[1],
                        line=dict(color=c, width=2.2, dash=d), marker=dict(size=4, color=c),
                        hovertemplate=f"{n} · %{{x|%d %b}}: %{{y:,.0f}}<extra></extra>") for n, c, d in series],
                  showlegend=True, legend=LEGEND, xaxis=dict(tickformat="%d %b", dtick=86400000 * 3), yaxis=NUM_Y)),
             ("Weekday index", "Friday sells 2.5× a Monday; tooltip = January units that weekday", 424,
              weekday_tooltip(vbars(wd["weekday"], wd["weekday_index"], ACCENT, [f"{v:.2f}" for v in wd["weekday_index"]],
                                    yaxis=dict(tickprefix="", tickformat=".1f")), wd["u"]))],
            [("Forecast error by method", "The annual method is the only one not biased low (−1%), so the restock list uses it", 560,
              table(["Method", "Line-week error", "Chain-day error", "Forecast bias"],
                    [[m + (" (restock)" if m == used else ""), f"{w:.0%}", f"{c:.0%}", cell(pct(b), 0 if abs(b) < 0.05 else -1)]
                     for m, w, c, b in zip(*sc.values())], shade=[sc["method_label"].index(used)])),
             ("The same scores, one panel per method", "Small multiples on the unpivoted score table; bias is forecast ÷ actual − 1",
              744, small_multiples(fm))],
        ]}


def small_multiples(fm):
    """Power BI small multiples (2 × 2): one horizontal bar panel per forecast method, shared metric axis."""
    methods = list(dict.fromkeys(fm["m"]))
    traces, layout = [], {}
    for i, m in enumerate(methods):
        r, c = divmod(i, 2)
        k = [x for x, mm in zip(fm["k"], fm["m"]) if mm == m]
        v = [x for x, mm in zip(fm["v"], fm["m"]) if mm == m]
        ax = "" if i == 0 else str(i + 1)
        traces.append(dict(type="bar", orientation="h", y=k, x=v, xaxis=f"x{ax}", yaxis=f"y{ax}", text=[f"{x:.0%}" for x in v],
                           textposition="outside", cliponaxis=False, textfont=dict(size=9, color=INK2),
                           marker=dict(color=[DUE_C if x < 0 else ACCENT for x in v], cornerradius=3),
                           hovertemplate=f"{m} · %{{y}}: %{{text}}<extra></extra>"))
        y1 = 1 - r * 0.5
        layout[f"xaxis{ax}"] = dict(domain=[c * 0.5 + 0.17, c * 0.5 + 0.47], range=[-0.6, 1.9], visible=False)
        layout[f"yaxis{ax}"] = dict(domain=[y1 - 0.38, y1 - 0.06], autorange="reversed", showgrid=False, zeroline=False,
                                    tickfont=dict(color=INK2, size=9), anchor=f"x{ax}")
        layout.setdefault("annotations", []).append(dict(text=f"<b>{m}</b>", x=c * 0.5, y=y1, xref="paper", yref="paper",
                                                         xanchor="left", yanchor="top", showarrow=False,
                                                         font=dict(size=10, color=INK)))
    f = fig(traces, margin=dict(l=4, r=8, t=4, b=4))
    f["layout"].update(layout)
    return f


def page_vendors():
    po = one("""select count(*), sum(dollars), sum(freight), avg(days_to_pay), count(*) filter (where not po_matches_receipts)
                from mart.fact_po""")
    lead = one("select avg(lead_days) from mart.dim_vendor where not lead_estimated")[0]
    sp = rows("""select vendor, spend, lead_p90, freight_pct, days_to_pay, coalesce(sh.n, 0) n from mart.dim_vendor
                 left join (select vendor_no, sum(shipments) n from mart.fact_shipment group by 1) sh using (vendor_no)
                 where spend > 0 order by spend desc limit 12""")
    rc = rows("""with r as (select month_start m, sum(dollars) v from mart.fact_receipts group by 1),
                      p as (select date_trunc('month', pay_date)::date m, sum(dollars) paid from mart.fact_po group by 1)
                 select m month_start, r.v, p.paid from r full join p using (m) order by m""")
    vt = rows(f"""with sup as (select s.vendor_no, count(distinct s.brand) items,
                                      sum(f.stock_cost) supplied_sv
                               from mart.item_supplier s left join {F} on f.brand = s.brand group by 1)
                  , rv as (select vendor_no, sum(dollars) rv from mart.fact_receipts group by 1)
                  select v.vendor, coalesce(c."Catalog Items", 0) listed, v.lead_days, v.lead_p90, rv.rv,
                         sup.items, sup.supplied_sv, coalesce(sum(f.order_cost), 0) o
                  from mart.dim_vendor v left join {F} on f.vendor_no = v.vendor_no
                       left join sup on sup.vendor_no = v.vendor_no
                       left join rv on rv.vendor_no = v.vendor_no
                       left join mart.pq_vendor_catalog c on c."Catalog Vendor No" = v.vendor_no
                  group by all order by rv.rv desc nulls last limit 8""")
    ms = rows("""select i.description, v.vendor, s.units, s.dollars from mart.item_supplier s
                 join mart.dim_item i using (brand) join mart.dim_vendor v on v.vendor_no = s.vendor_no
                 where s.multi_sourced order by s.dollars desc limit 10""")
    n_ms = one("select count(distinct brand), count(*) from mart.item_supplier where multi_sourced")
    # Both 3-month averages skip blank months (AVERAGEX ignores blanks); the WINDOW measure is blank where
    # nothing was received; the visual calculation runs over the chart's own rows.
    def avg3(vals, i):
        w = [v for v in vals[max(0, i - 2):i + 1] if v is not None]
        return sum(w) / len(w) if w and vals[i] is not None else None
    ma = [avg3(rc["v"], i) for i in range(len(rc["v"]))]
    ma_paid = [avg3(rc["paid"], i) for i in range(len(rc["paid"]))]
    return {
        "kpis": kpis([("Purchase Spend", usd(po[1])), ("Purchase Orders", num(po[0])), ("Lead Time", f"{lead:.1f}"),
                      ("Freight Share", f"{po[2] / po[1]:.2%}"), ("Payment Days", f"{po[3]:.1f}"),
                      ("Unmatched POs", num(po[4]), "bad")]),
        "rows": [
            [("Purchase spend by vendor", "2016 invoices; hover a vendor for lead time, freight and payment days (Vendor "
              "Tip page) · top 12", 640, vendor_tip(hbars(sp["vendor"], sp["spend"], ACCENT,
                                                        text=[usd(v) for v in sp["spend"]], left=230), sp)),
             ("Received vs paid per month",
              "Paid = invoices by payment date; lines = 3-month averages; drill up to quarter / year", 664,
              fig([dict(type="bar", name="Received value", x=rc["month_start"], y=rc["v"],
                        marker=dict(color=ACCENT, cornerradius=3), hovertemplate="Received · %{x|%b %Y}: %{y:$,.0f}<extra></extra>"),
                   dict(type="bar", name="Paid dollars", x=rc["month_start"], y=rc["paid"],
                        marker=dict(color=INK2, cornerradius=3), hovertemplate="Paid · %{x|%b %Y}: %{y:$,.0f}<extra></extra>"),
                   dict(type="scatter", mode="lines+markers", name="Received 3M (WINDOW)", x=rc["month_start"], y=ma,
                        line=dict(color=ACCENT, width=2), marker=dict(size=5, color=ACCENT),
                        hovertemplate="Received 3M · %{x|%b %Y}: %{y:$,.0f}<extra></extra>"),
                   dict(type="scatter", mode="lines+markers", name="Paid 3M avg (visual calc)", x=rc["month_start"],
                        y=ma_paid, line=dict(color=INK, width=2, dash="dot"), marker=dict(size=5, color=INK),
                        hovertemplate="Paid 3M · %{x|%b %Y}: %{y:$,.0f}<extra></extra>")],
                  barmode="group", showlegend=True, legend=LEGEND, xaxis=dict(tickformat="%b", dtick="M1")))],
            [("Vendor scorecard", "No invoice fields, so Store Staff (object-level security) sees it too; lead days = PO to "
              "receipt · top 8 by received value", 860,
              table(["Vendor", "Listed items", "Avg lead days", "Lead days P90", "Received value",
                     "Items supplied", "Stock of supplied items", "Order value"],
                    [[v, num(s), f"{l:.1f}", "—" if p is None else f"{p:.0f}", usd(r),
                      num(it or 0), usd(ss), usd(o)] for v, s, l, p, r, it, ss, o in zip(*vt.values())])),
             ("Items bought from more than one vendor", f"2016 purchases through the Item Supplier bridge · {n_ms[0]} items, "
              f"{n_ms[1]} item × vendor pairs · top 10", 444,
              table(["Item", "Vendor", "Units", "Spend"],
                    [[d, v, num(u), usd(x)] for d, v, u, x in zip(*ms.values())], left=(0, 1)))],
        ]}


def vendor_tip(f, sp):
    """Report-page tooltip Vendor Tip: lead days P90, freight %, days to pay, shipments for the hovered vendor."""
    t = f["data"][0]
    t["customdata"] = [["—" if p is None else f"{p:.0f}", f"{fp:.2%}", f"{d:.1f}", num(n)]
                       for p, fp, d, n in zip(sp["lead_p90"], sp["freight_pct"], sp["days_to_pay"], sp["n"])]
    t["hovertemplate"] = ("<b>%{y}</b><br>Lead days P90 %{customdata[0]}<br>Freight %{customdata[1]}"
                          "<br>Days to pay %{customdata[2]}<br>Shipments %{customdata[3]}<extra></extra>")
    return f


def page_stores():
    sa = rows("""select s.store_label, sum(dollars) v from mart.fact_sales_daily join mart.dim_store s using (store)
                 group by 1 order by v desc""")
    st = rows(f"""select s.store_label, s.city, sum(stock_cost) sv, sum(end_units) / sum(daily_forecast) cover,
                         count(*) filter (where status = 'Out of stock') / count(*) filter (where daily_forecast > 0) oos,
                         count(*) filter (where {DUE}) due, sum(order_cost) o, sum({EXCESS}) x
                  from {F} join mart.dim_store s using (store) group by all""")
    sales = dict(zip(sa["store_label"], sa["v"]))
    tbl = sorted(zip(*st.values()), key=lambda r: -(sales.get(r[0]) or 0))
    total_sv = sum(st["sv"])
    # RANK ( DENSE, ORDERBY ( [January Sales], DESC ) ): ties share a rank; stores with no sales rank last together.
    dense = {v: i + 1 for i, v in enumerate(sorted({sales.get(r[0]) or 0 for r in tbl}, reverse=True))}
    oos = sorted(zip(st["store_label"], st["oos"]), key=lambda t: -(t[1] or 0))[:15]
    sc = [(s, c, o) for s, c, o in zip(st["store_label"], st["cover"], st["o"]) if c is not None]
    scatter = fig([dict(type="scatter", mode="markers", x=[c for _, c, _ in sc], y=[o for _, _, o in sc],
                        text=[s for s, _, _ in sc], marker=dict(color=ACCENT, size=8, opacity=.75, line=dict(color="#fff", width=1)),
                        hovertemplate="%{text}<br>Days of cover %{x:.0f} · order %{y:$,.0f}<extra></extra>")],
                  xaxis=dict(showgrid=True, gridcolor="#ECEBE6", title=dict(text="Days of cover", font=dict(size=10))),
                  margin=dict(l=52, r=12, t=8, b=40))
    return {
        "kpis": kpis([("January Sales", usd(K["jan_v"])), ("Stock Value", usd(K["sv"])),
                      ("Out of Stock Rate", f"{K['oos_rate']:.1%}", "bad"), ("Order Value", usd(K["due_v"])),
                      ("Excess Stock Value", usd(K["excess"]), "bad")]),
        "rows": [
            [("Stores by metric: January Sales", "Field parameter (dropdown); click a store to filter the other two · top 15", 440,
              hbars(sa["store_label"][:15], sa["v"][:15], ACCENT, text=[usd(v) for v in sa["v"][:15]], left=170)),
             ("Out-of-stock rate by store", "Store size slicer: All · top 15", 424,
              hbars([s for s, _ in oos], [o for _, o in oos], DUE_C, text=[f"{o:.1%}" for _, o in oos], left=170)),
             ("Days of cover vs order due, per store", "Top left = thin stock and a large order due", 424, scatter)],
            [("Store scorecard", "Sales Rank is a visual calculation; arrows: ▲ under 15% out of stock, ► 15–20%, ▼ over 20%", 1320,
              table(["Sales rank", "Store", "City", "January sales", "Stock value", "Share of stock value", "Days of cover",
                     "Out-of-stock rate", "Lines to order", "Order value", "Excess stock"],
                    [[dense[sales.get(s) or 0], s, c, usd(sales.get(s)), usd(sv), f"{sv / total_sv:.1%}",
                      "—" if cv is None else f"{cv:.0f}", arrow(o),
                      num(d), usd(ov), usd(x)] for s, c, sv, cv, o, d, ov, x in tbl], left=(1, 2)))],
        ]}


# ------------------------------------------------------- analytics pane (Python stand-in)
def holt_winters(y, m=7, grid=(0.05, 0.1, 0.2, 0.3, 0.5)):
    """Additive Holt-Winters (level + trend + weekly season), parameters picked by one-step-ahead squared error.
    Power BI's forecast is also exponential smoothing, but its exact fitting is internal: this is an approximation."""
    def run(a, b, g):
        level, trend = sum(y[:m]) / m, (sum(y[m:2 * m]) - sum(y[:m])) / m ** 2
        season = [v - level for v in y[:m]]
        fitted = [None] * m
        for t in range(m, len(y)):
            pred = level + trend + season[t % m]
            fitted.append(pred)
            prev = level
            level = a * (y[t] - season[t % m]) + (1 - a) * (level + trend)
            trend = b * (level - prev) + (1 - b) * trend
            season[t % m] = g * (y[t] - level) + (1 - g) * season[t % m]
        sse = sum((v - f) ** 2 for v, f in zip(y[m:], fitted[m:]))
        return sse, fitted, level, trend, season
    best = min((run(a, b, g) + ((a, b, g),) for a, b, g in itertools.product(grid, grid, grid)), key=lambda r: r[0])
    sse, fitted, level, trend, season, (a, b, g) = best
    sigma = math.sqrt(sse / (len(y) - m))
    return dict(fitted=fitted, sigma=sigma, alpha=a, beta=b, gamma=g,
                ahead=lambda h: level + h * trend + season[(len(y) + h - 1) % m],
                # h-step variance of additive Holt-Winters (Hyndman et al., class 1 state-space model)
                spread=lambda h: sigma * math.sqrt(1 + sum((a + a * b * j + (g if j % m == 0 else 0)) ** 2
                                                           for j in range(1, h))))


def band_traces(x, low, high, color_, name):
    """A shaded band: upper edge, then lower edge filled up to it."""
    return [dict(type="scatter", mode="lines", x=x, y=high, line=dict(width=0), hoverinfo="skip", showlegend=False),
            dict(type="scatter", mode="lines", x=x, y=low, line=dict(width=0), fill="tonexty", fillcolor=color_,
                 name=name, hoverinfo="skip")]


def page_trends():
    d = rows("select date, sum(units) u, sum(dollars) v from mart.fact_sales_daily group by 1 order by 1")
    received = one("select sum(dollars) from mart.fact_receipts")[0]

    # Forecast: 7 days past the last actual, 95% band.
    hw = holt_winters(d["u"])
    last = d["date"][-1]
    ahead = [last + dt.timedelta(days=h) for h in range(1, 8)]
    fc = [hw["ahead"](h) for h in range(1, 8)]
    lo = [max(0, f - 1.96 * hw["spread"](h)) for h, f in enumerate(fc, 1)]
    hi = [f + 1.96 * hw["spread"](h) for h, f in enumerate(fc, 1)]
    band_x = [last] + ahead
    forecast = fig([dict(type="scatter", mode="lines", name="Units sold", x=d["date"], y=d["u"],
                         line=dict(color=ACCENT, width=2.2), hovertemplate="%{x|%a %d %b}: %{y:,.0f}<extra></extra>"),
                    *band_traces(band_x, [d["u"][-1]] + lo, [d["u"][-1]] + hi, "rgba(47,125,98,.18)", "95% band"),
                    dict(type="scatter", mode="lines+markers", name="Forecast", x=band_x, y=[d["u"][-1]] + fc,
                         line=dict(color=ACCENT, width=2, dash="dash"), marker=dict(size=4, color=ACCENT),
                         hovertemplate="Forecast %{x|%a %d %b}: %{y:,.0f}<extra></extra>")],
                   showlegend=True, legend=LEGEND, yaxis=NUM_Y, xaxis=dict(tickformat="%d %b", dtick=86400000 * 7))

    # Anomalies: actual vs the one-step-ahead expectation; flagged when outside the band.
    hs = holt_winters(d["v"])
    z = 2.5  # the band width that stands in for Power BI's 70% sensitivity
    xs = d["date"][7:]
    exp_ = hs["fitted"][7:]
    flagged = [(x, v) for x, v, e in zip(xs, d["v"][7:], exp_) if abs(v - e) > z * hs["sigma"]]
    anomaly = fig([*band_traces(xs, [max(0, e - z * hs["sigma"]) for e in exp_], [e + z * hs["sigma"] for e in exp_],
                                "rgba(137,135,129,.16)", "Expected range"),
                   dict(type="scatter", mode="lines", name="Sales", x=d["date"], y=d["v"], line=dict(color=INK2, width=2),
                        hovertemplate="%{x|%a %d %b}: %{y:$,.0f}<extra></extra>"),
                   dict(type="scatter", mode="markers", name="Anomaly", x=[x for x, _ in flagged], y=[v for _, v in flagged],
                        marker=dict(size=11, color=DUE_C, line=dict(color="#fff", width=2)),
                        hovertemplate="Anomaly %{x|%a %d %b}: %{y:$,.0f}<extra></extra>")],
                  showlegend=True, legend=LEGEND, xaxis=dict(tickformat="%d %b", dtick=86400000 * 7))

    # Time Intelligence calculation group on Received Value, one row per month (2016 is the only year).
    rc = rows("""select d.month, d.month_sort, d.quarter, coalesce(sum(r.dollars), 0) v
                 from (select distinct month, month_sort, quarter from mart.dim_date where year = 2016) d
                 left join mart.fact_receipts r on strftime(r.month_start, '%b %Y') = d.month
                 group by all order by d.month_sort""")
    body = []
    for i, (mo, _, q, v) in enumerate(zip(*rc.values())):
        pm = rc["v"][i - 1] if i else None
        qtd = sum(x for x, qq in zip(rc["v"][:i + 1], rc["quarter"][:i + 1]) if qq == q)
        vals = {"Current": usd(v), "Prior Month": usd(pm) if pm is not None else "",
                "MoM %": cell(pct(v / pm - 1), v / pm - 1) if pm else "", "MTD": usd(v), "QTD": usd(qtd),
                "YTD": usd(sum(rc["v"][:i + 1])), "Prior Year": "", "YoY %": ""}
        body.append([mo] + [vals[name] for name, _, _ in TIME_ITEMS])

    cum = list(itertools.accumulate(d["v"]))
    tiles = [("January Sales", usd(K["jan_v"])), ("January Units Sold", num(K["jan_u"])),
             ("Avg Selling Price", f"${K['jan_v'] / K['jan_u']:.2f}"), ("Received Value", usd(received))]
    cats = [r[0] for r in con.execute("select distinct category from mart.dim_item order by 1").fetchall()]
    top = ('<div class="cardviz" style="width:1000px">' + "".join(
               f'<div class="tile"><b>{v}</b><span>{k}</span></div>' for k, v in tiles) + '</div>'
           '<div class="btnslicer" style="width:304px"><h3>Category</h3><div class="btns">' + "".join(
               f'<button type="button" aria-pressed="false">{c}</button>' for c in cats) +
           '</div><p>Button slicer, synced with the Category dropdown on the other pages</p></div>')
    return {
        "kpis": [], "top": top,
        "rows": [
            [("Units per day, with a 7-day forecast",
              f"Analytics pane forecast: weekly seasonality, 95% band · twin uses Holt-Winters (α {hw['alpha']}, "
              f"β {hw['beta']}, γ {hw['gamma']})", 652, forecast),
             ("Sales per day, with anomalies flagged",
              f"Analytics pane anomaly detection, sensitivity 70% · {len(flagged)} day(s) outside the expected range",
              652, anomaly)],
            [("Received value across calendar periods",
              "Columns are the Time Intelligence calculation group; Prior Year and YoY % are blank (one year of data)", 760,
              table(["Month"] + [name for name, _, _ in TIME_ITEMS], body)),
             ("Cumulative January sales", "RUNNINGSUM visual calculation over the chart's own rows", 544,
              fig([dict(type="scatter", mode="lines", x=d["date"], y=cum, line=dict(color=ACCENT, width=2.4),
                        fill="tozeroy", fillcolor="rgba(47,125,98,.08)",
                        hovertemplate="%{x|%d %b}: %{y:$,.0f}<extra></extra>")],
                  xaxis=dict(tickformat="%d %b", dtick=86400000 * 7)))],
        ]}


DORMANT = "end_units > 0 and jan_units = 0 and (last_receipt is null or last_receipt < date '2016-12-31' - 120)"


def short(t, n=20):
    return t if len(t) <= n else t[:n - 1] + "…"


def page_assortment():
    """Pareto, static vs dynamic ABC, new vs carried-over lines, dormant lines."""
    it = rows(f"""select i.description item, sum(sales_value) s from {F} join mart.dim_item i using (brand)
                  group by 1 having sum(sales_value) > 0 order by 2 desc""")
    total = sum(it["s"])
    cum, run = [], 0
    for v in it["s"]:
        run += v
        cum.append(run / total)
    n80 = next(i for i, c in enumerate(cum) if c >= 0.8) + 1
    x = [short(t) for t in it["item"][:40]]
    pareto = fig([dict(type="bar", name="Sales 2016", x=x, y=it["s"][:40], marker=dict(color=ACCENT, cornerradius=2),
                       customdata=it["item"][:40], hovertemplate="%{customdata}: %{y:$,.0f}<extra></extra>"),
                  dict(type="scatter", mode="lines", name="Cumulative Sales Share", x=x, y=cum[:40], yaxis="y2",
                       line=dict(color=INK, width=2), hovertemplate="%{x}: %{y:.1%} of sales<extra></extra>"),
                  dict(type="scatter", mode="lines", name="Pareto 80% Line", x=x, y=[0.8] * len(x), yaxis="y2",
                       line=dict(color=DUE_C, width=1.5, dash="dash"), hoverinfo="skip")],
                 showlegend=True, legend=LEGEND, xaxis=dict(tickangle=-50, tickfont=dict(size=8)),
                 yaxis2=dict(overlaying="y", side="right", range=[0, 1], tickformat=".0%", showgrid=False,
                             tickfont=dict(color=MUTED, size=10)),
                 margin=dict(l=48, r=40, t=8, b=96))
    # ABC Dynamic: brands re-ranked inside the selection (default state = every open store) vs the stored class.
    ab = rows(f"""with b as (select brand, sum(sales_value) s from {F} group by 1 having sum(sales_value) > 0),
                  r as (select brand, s, sum(s) over (order by s desc, brand rows unbounded preceding) / sum(s) over () c from b)
                  select r.brand, i.description, s, i.abc, case when c <= 0.8 then 'A' when c <= 0.95 then 'B' else 'C' end dyn
                  from r join mart.dim_item i using (brand) order by s desc""")
    moved = sum(a != d for a, d in zip(ab["abc"], ab["dyn"]))
    city = "Hardersfield"
    cm = one(f"""with b as (select brand, sum(sales_value) s from {F} join mart.dim_store st using (store)
                            where st.city = '{city}' group by 1 having sum(sales_value) > 0),
                 r as (select brand, sum(s) over (order by s desc, brand rows unbounded preceding) / sum(s) over () c from b)
                 select count(*) filter (where i.abc <> case when c <= 0.8 then 'A' when c <= 0.95 then 'B' else 'C' end),
                        count(*) from r join mart.dim_item i using (brand)""")
    abc_rows = [[str(b), short(d, 24), usd(s), a, f"<b>{y}</b>" if y != a else y]
                for b, d, s, a, y in list(zip(*ab.values()))[:8]]
    og = rows(f"""select line_origin, count(*) n,
                         count(*) filter (where status = 'Out of stock') / nullif(count(*) filter (where daily_forecast > 0), 0) oos,
                         coalesce(sum(stock_cost) filter (where status = 'Dead stock'), 0) dv, sum(stock_cost) sv
                  from {F} group by 1 order by n desc""")
    rate = lambda w: one(f"select count(*) filter (where status = 'Out of stock') / count(*) filter (where daily_forecast > 0) from {F} where {w}")[0]
    new_r, old_r = rate("new_line_2016"), rate("line_origin = 'Carried over'")
    dn = one(f"select count(*), sum(stock_cost) from {F} where {DORMANT}")
    dm = rows(f"""select s.store_label, i.description, i.size, end_units, last_receipt, stock_cost
                  from {F} {DIMS} where {DORMANT} order by stock_cost desc limit 8""")
    return {
        "kpis": kpis([("Sales 2016", usd(total)), ("Lines Going Dormant", num(dn[0]), "bad"),
                      ("Dormant Stock Value", usd(dn[1]), "bad"), ("New Line OOS Rate", f"{new_r:.1%}"),
                      ("Carried Line OOS Rate", f"{old_r:.1%}", "bad")]),
        "rows": [
            [("Pareto: 2016 sales by item", f"Top 40 items drawn; the line is the running share of all {num(len(cum))} "
              f"items · {num(n80)} items make 80% of sales", 880, pareto),
             ("ABC: static vs re-ranked in the selection",
              f"No slicer: {moved} of {num(len(ab['abc']))} brands change class (store 46 is filtered out) · with City = "
              f"{city}: {num(cm[0])} of {num(cm[1])}", 424,
              table(["Brand", "Item", "Sales 2016", "Static", "Dynamic"], abc_rows, left=(0, 1)), "dense")],
            [("New lines vs carried over", "New in 2016 = closing count only; received, not counted = bought in 2016 "
              "but in neither count, so empty at year-end", 440,
              table(["Line origin", "Lines", "OOS rate", "Dead stock", "Stock value"],
                    [[o, num(n), "—" if r_ is None else f"{r_:.1%}", usd(d), usd(v)] for o, n, r_, d, v in zip(*og.values())])),
             ("Lines going dormant, largest first",
              "Stock on hand, no January sales, no receipt after 2 Sep 2016 (blank = none in 2016)", 864,
              table(["Store", "Item", "Size", "On hand", "Last receipt", "Stock cost"],
                    [[s_, d, z, num(e), "—" if lr is None else lr.strftime("%-d %b %Y"), usd(c)]
                     for s_, d, z, e, lr, c in zip(*dm.values())], left=(0, 1, 2)))],
        ]}


def page_movement():
    """Category waterfall, opening state × status matrix, PO funnel, table-driven cover bands, cover outliers."""
    ch = rows(f"select category, sum((end_units - begin_units) * unit_cost) v from {F} group by 1 order by 1")
    tot = sum(ch["v"])
    wf = fig([dict(type="waterfall", x=ch["category"] + ["Total"], measure=["relative"] * len(ch["v"]) + ["total"],
                   y=ch["v"] + [0], text=[f"{usd(v, True)} · {v / tot:.0%}" for v in ch["v"]] + [usd(tot, True)],
                   textposition="outside", cliponaxis=False, textfont=dict(size=10, color=INK2),
                   increasing=dict(marker=dict(color=SLOW_C)), decreasing=dict(marker=dict(color=ACCENT)),
                   totals=dict(marker=dict(color=INK2)), connector=dict(line=dict(color="#D9D7CF", width=1)),
                   hovertemplate="%{x}: %{text}<extra></extra>")],
             yaxis=dict(range=[min(0, tot) * 1.2, max(0, tot) * 1.2]), margin=dict(l=44, r=8, t=16, b=28))
    statuses = rows("select status from mart.dim_status order by status_sort")["status"]
    tm = rows(f"select opening_state, status, count(*) n from {F} group by all")
    cellv = {(o, s_): n for o, s_, n in zip(*tm.values())}
    states = ["Stocked 1 Jan", "Empty 1 Jan"]
    matrix = [[s_] + [num(cellv.get((o, s_), 0)) for o in states] + [num(sum(cellv.get((o, s_), 0) for o in states))]
              for s_ in statuses]
    matrix.append(["Total"] + [num(sum(cellv.get((o, s_), 0) for s_ in statuses)) for o in states] + [num(sum(cellv.values()))])
    fp = one("""select count(*), count(*) filter (where po_matches_receipts),
                       count(*) filter (where po_matches_receipts and approved),
                       count(*) filter (where po_matches_receipts and approved and pay_date is not null) from mart.fact_po""")
    steps = ["PO raised", "Invoiced", "Matches receipts", "Approved", "Paid"]
    vals = [fp[0], fp[0], fp[1], fp[2], fp[3]]
    funnel = fig([dict(type="funnel", y=steps, x=vals, marker=dict(color=ACCENT), textinfo="value+percent initial",
                       textfont=dict(size=10), connector=dict(line=dict(color="#D9D7CF", width=1)),
                       hovertemplate="%{y}: %{x:,} POs<extra></extra>")],
                 yaxis=dict(showgrid=False, zeroline=False, tickprefix="", tickformat="", tickfont=dict(color=INK2, size=10)),
                 margin=dict(l=112, r=12, t=8, b=8))
    bd = rows(f"""select b.cover_band, b.min_days, b.max_days,
                         (select count(*) from {F} where case when b.min_days is null then cover_days is null
                                 else cover_days > b.min_days and cover_days <= b.max_days end) dyn,
                         (select count(*) from {F} where f.cover_band = b.cover_band) n_band
                  from mart.dim_cover_band b order by b.cover_sort""")
    p95 = rows(f"select category, quantile_cont(cover_days, 0.95) p from {F} where cover_days is not null group by 1")
    P = dict(zip(*p95.values()))
    allp = one(f"select quantile_cont(cover_days, 0.95) from {F} where cover_days is not null")[0]
    case = " ".join(f"when '{c}' then {v}" for c, v in P.items())
    out_w = f"cover_days > case f.category {case} end"
    on = one(f"select count(*), sum(stock_cost) from {F} where {out_w}")
    ol = rows(f"""select s.store_label, i.description, f.category, cover_days, stock_cost
                  from {F} {DIMS} where {out_w} order by stock_cost desc limit 8""")
    return {
        "kpis": kpis([("Stock Value Change", usd(tot, True), "bad"), ("Cover P95 by Category", f"{allp:,.0f}"),
                      ("Outlier Lines", num(on[0]), "bad"), ("Outlier Stock Value", usd(on[1]), "bad"),
                      ("Lines", num(K["n"]))]),
        "rows": [
            [("Stock value change by category", "Closing − opening at unit cost; total bar = whole change", 424, wf),
             ("From 1 Jan to 31 Dec", "Lines by year-end status (rows) and opening state (columns)", 440,
              table(["Status 31 Dec"] + states + ["Total"], matrix, shade=[len(statuses)]), "dense"),
             ("Procurement funnel (purchase orders)", "Each step keeps the conditions of the steps before", 424, funnel)],
            [("Cover bands from a table", "Dynamic = counted from the Min / Max Days edges; Lines = the stored band", 500,
              table(["Cover band", "Min days", "Max days", "Lines (dynamic)", "Lines"],
                    [[b, "" if lo is None else num(lo), "" if hi is None else num(hi), num(d), num(st)]
                     for b, lo, hi, d, st in zip(*bd.values())]), "dense"),
             ("Cover outliers, largest stock value first", "Lines with cover above their category's 95th percentile "
              "(" + ", ".join(f"{c} {v:,.0f} days" for c, v in sorted(P.items())) + ")", 804,
              table(["Store", "Item", "Category", "Cover days", "Category P95", "Stock cost"],
                    [[s_, d, c, num(cd), num(P[c]), usd(sc)] for s_, d, c, cd, sc in zip(*ol.values())], left=(0, 1, 2)))],
        ]}


def diagnosis_text():
    """Same arithmetic as the Diagnosis DAX measure, in the default state."""
    rate = lambda w: one(f"select count(*) filter (where status = 'Out of stock') / count(*) filter (where daily_forecast > 0) "
                         f"from {F} where {w}")[0]
    med = one(f"select quantile_cont(lead_days, 0.5) from {F} where daily_forecast > 0")[0]
    a, c, sh, lo = rate("abc = 'A'"), rate("abc = 'C'"), rate(f"lead_days <= {med}"), rate(f"lead_days > {med}")
    tail = ("The stock-outs follow the item class, not the lead time: look at the C items in the tree, then drill through "
            "to one of them." if abs(c - a) > 2 * abs(lo - sh) else
            "Item class and lead time both matter here: check the vendors in the tree.")
    return (f"{K['oos_rate']:.0%} of the lines with demand were empty at the year-end count. Class C lines ran out "
            f"{c:.0%} of the time against {a:.0%} for class A ({(c - a) * 100:.0f} points), while vendors slower than the "
            f"median lead time of {med:.1f} days add {(lo - sh) * 100:.0f} points ({lo:.0%} vs {sh:.0%}). {tail}")


def page_diagnose():
    """Diagnose: the AI visuals approximated. Key influencers = out-of-stock share of lines with a factor value vs the
    lines without it; decomposition tree = order value split along its largest branch; Q&A = one example answer."""
    facts = [("ABC class", "a.abc_label"), ("Category", "f.category"), ("Pack size", PACK_SQL), ("City", "s.city")]
    lift = []
    for name, expr in facts:
        r = rows(f"""with t as (select {expr} v, (status = 'Out of stock')::int o from {F} join mart.dim_store s using (store)
                                join mart.dim_item i using (brand) join mart.dim_abc a on a.abc = f.abc)
                     select v, avg(o) p, (sum(sum(o)) over () - sum(o)) / (sum(count(*)) over () - count(*)) q, count(*) n
                     from t group by v""")
        lift += [(f"{name} is {v}", p / q, p, n) for v, p, q, n in zip(*r.values()) if q and n >= 500]
    lift = sorted(lift, key=lambda t: -t[1])[:8]
    ki = hbars([l for l, *_ in lift], [x for _, x, _, _ in lift], ACCENT, text=[f"{x:.2f}×" for _, x, _, _ in lift], left=230)
    ki["data"][0]["customdata"] = [f"{p:.1%} of {num(n)} lines" for _, _, p, n in lift]
    ki["data"][0]["hovertemplate"] = "%{y}: %{text} as likely · %{customdata} out of stock<extra></extra>"

    def top(where, by):
        r = one(f"select {by}, sum(order_cost) o from {F} {DIMS} where {DUE} {where} group by 1 order by 2 desc limit 1")
        return r
    v = top("", "v.vendor")
    c = top(f"and v.vendor = '{v[0]}'", "f.category")
    st = top(f"and v.vendor = '{v[0]}' and f.category = '{c[0]}'", "s.store_label")
    lvls = [("Order Value", "Total", K["due_v"]), ("Vendor", v[0], v[1]), ("Category", c[0], c[1]), ("Store", st[0], st[1])]
    tree = '<div class="tree">' + "".join(
        f'<div class="node"><span>{lv}</span><b>{label}</b><i style="width:{100 * val / K["due_v"]:.1f}%"></i>'
        f'<em>{usd(val)} · {val / K["due_v"]:.1%} of the order</em></div>' for lv, label, val in lvls) + '</div>'
    qa = rows(f"""select s.city, count(*) filter (where status = 'Out of stock') / count(*) filter (where daily_forecast > 0) r
                  from {F} join mart.dim_store s using (store) group by 1 order by 2 desc limit 6""")
    qna = ('<div class="ask">stockout rate by city</div><p class="note" style="margin:6px 0">Q&amp;A reads “stockout rate” '
           'as the Out of Stock Rate measure (model synonym) and “city” as Store[City]; top 6 shown:</p>' +
           "".join(f'<div class="qa"><span>{c_}</span><b>{r_:.1%}</b></div>' for c_, r_ in zip(*qa.values())))
    note = (f'<h4>Diagnosis</h4><p class="narr">{diagnosis_text()}</p><p class="note" style="margin-top:10px">Then split '
            'the order in the tree, check the traits on the left, and right-click an item or vendor › Drill through for '
            'its detail page (examples in the menu on the left). The paragraph is a DAX measure in the report; here it is '
            'the same arithmetic in SQL. The AI visuals are approximated: Power BI fits its own key-influencer model, and '
            'the tree here follows its largest branch.</p>')
    return {
        "kpis": kpis([("Out of Stock Rate", f"{K['oos_rate']:.1%}", "bad"), ("Out of Stock Lines", num(K["oos"])),
                      ("Selling Lines", num(K["selling"])), ("Order Value", usd(K["due_v"])),
                      ("Urgent Order Value", usd(K["urgent"]), "bad")]),
        "rows": [
            [("What makes a line run out of stock", "Key influencers: how many times more likely a line is out of stock "
              "with this trait than without it · top 8", 652, ki),
             ("Where the order comes from", "Decomposition tree: order value → vendor → category → store (largest branch)",
              652, {"html": tree})],
            [("Ask a question", "Q&A uses the model synonyms: supplier, branch, stockout rate, reorder quantity, days of "
              "supply", 652, {"html": qna}),
             ("", "", 652, {"html": note})],
        ]}


def page_item_detail():
    """Drillthrough example: the item with the largest order value across the stores."""
    brand, item = one(f"""select f.brand, i.description || ' ' || i.size from {F} join mart.dim_item i using (brand)
                          where {DUE} group by all order by sum(order_cost) desc limit 1""")
    k = one(f"""select sum(end_units), sum(stock_cost), count(*) filter (where {DUE}), sum(order_cost) filter (where {DUE})
                from {F} where brand = {brand}""")
    jan = one(f"select sum(units) from mart.fact_sales_daily where brand = {brand}")[0] or 0
    sold = one(f"select sum(sold_units) from {F} where brand = {brand}")[0]
    late = f" It sold {sold:,} units in 2016 but {jan:,} in January: a line that took off after January." if jan < sold / 100 else ""
    b = rows(f"""select s.store_label, s.city, status, end_units, daily_forecast, reorder_point, order_up_to,
                        case when {DUE} then order_units else 0 end u, case when {DUE} then order_cost else 0 end o
                 from {F} join mart.dim_store s using (store) where brand = {brand} order by order_up_to desc""")
    n = 15
    bullet = fig([dict(type="bar", orientation="h", y=b["store_label"][:n], x=b["order_up_to"][:n], width=.8,
                       marker=dict(color="#D9D7CF"), name="Order-up-to", hovertemplate="%{y}: order-up-to %{x:,}<extra></extra>"),
                  dict(type="bar", orientation="h", y=b["store_label"][:n], x=b["end_units"][:n], width=.4,
                       marker=dict(color=ACCENT), name="On hand", hovertemplate="%{y}: on hand %{x:,}<extra></extra>"),
                  dict(type="scatter", mode="markers", y=b["store_label"][:n], x=b["reorder_point"][:n], name="Reorder point",
                       marker=dict(symbol="line-ns", size=11, line=dict(color=AMBER, width=3)),
                       hovertemplate="%{y}: reorder point %{x:,}<extra></extra>")],
                 barmode="overlay", showlegend=True,
                 yaxis=dict(autorange="reversed", showgrid=False, zeroline=False, tickprefix="", tickformat="", dtick=1,
                            tickfont=dict(color=INK2, size=9)),
                 legend={**LEGEND, "y": 1.08}, xaxis=dict(showgrid=True, gridcolor="#ECEBE6"), margin=dict(l=160, r=16, t=20, b=22))
    ve = rows(f"""select v.vendor, v.lead_days, v.lead_p90, count(*) filter (where {DUE}) n, sum(order_cost) filter (where {DUE}) o
                  from {F} join mart.dim_vendor v on v.vendor_no = f.vendor_no where brand = {brand} group by all""")
    return {
        "kpis": [{"label": "Back button", "value": "← Back", "t": None}] + kpis(
            [("Units On Hand", num(k[0])), ("Stock Value", usd(k[1])), ("January Units Sold", num(jan or 0)),
             ("Lines To Order", num(k[2])), ("Order Value", usd(k[3]))]),
        "detail": item, "note": late,
        "rows": [
            [("On hand vs reorder point vs order-up-to, by store", "Deneb bullet (Vega-Lite) in the report; green = on hand, "
              "grey = order-up-to, orange tick = reorder point; top 15 stores", 880, bullet),
             ("Vendor", "Who supplies the item and how long it takes", 424,
              table(["Vendor", "Avg lead days", "Lead days P90", "Lines to order", "Order value"],
                    [[v, f"{l:.1f}", "—" if p is None else f"{p:.0f}", num(n_), usd(o)] for v, l, p, n_, o in zip(*ve.values())]))],
            [("Every store stocking the item", f"Reorder point and order-up-to at a 95% service level; first 8 of "
              f"{len(b['store_label'])} stores by order-up-to", 1320,
              table(["Store", "City", "Status", "On hand", "Daily forecast", "Reorder point", "Order-up-to", "Units to order",
                     "Order value"],
                    [[s_, c_, stcell(st), num(e), f"{fc:.2f}", num(r), num(u_), num(u), usd(o)]
                     for s_, c_, st, e, fc, r, u_, u, o in list(zip(*b.values()))[:8]], left=(0, 1, 2)))],
        ]}


def page_vendor_detail():
    """Drillthrough example: the vendor with the largest order value."""
    vno, vendor = one(f"""select f.vendor_no, v.vendor from {F} {DIMS} where {DUE} group by all order by sum(order_cost) desc limit 1""")
    v = one(f"select lead_days, spend, freight_pct, days_to_pay from mart.dim_vendor where vendor_no = {vno}")
    sh = rows(f"select lead_days, sum(shipments) n from mart.fact_shipment where vendor_no = {vno} group by 1 order by 1")
    um = rows(f"""select po_number, po_date, invoice_date, dollars, freight from mart.fact_po
                  where vendor_no = {vno} and not po_matches_receipts order by dollars desc""")
    ms = rows(f"""select i.description || ' ' || i.size item, b.vendors_for_item, b.units, b.dollars from mart.item_supplier b
                  join mart.dim_item i using (brand) where b.vendor_no = {vno} and b.multi_sourced order by b.dollars desc limit 7""")
    od = rows(f"""select i.description || ' ' || i.size, s.store_label, status, order_units, order_cost from {F} {DIMS}
                  where f.vendor_no = {vno} and {DUE} order by order_cost desc limit 5""")
    return {
        "kpis": [{"label": "Back button", "value": "← Back", "t": None}] + kpis(
            [("Lead Time", f"{v[0]:.1f}"), ("Vendor Shipments", num(sum(sh["n"]))), ("Purchase Spend", usd(v[1])),
             ("Freight Share", f"{v[2]:.2%}"), ("Payment Days", f"{v[3]:.1f}"), ("Unmatched POs", num(len(um["po_number"])))]),
        "detail": vendor,
        "rows": [
            [("Lead-time spread", "Shipments by days from PO to receipt", 652,
              vbars(sh["lead_days"], sh["n"], ACCENT, [num(x) for x in sh["n"]], yaxis=NUM_Y,
                    xaxis=dict(dtick=1, title=dict(text="Lead days", font=dict(size=10))))),
             ("Items also bought from other vendors", "2016 purchases through the Item Supplier bridge · top 7", 652,
              table(["Item", "Vendors per item", "Units", "Spend"],
                    [[i, num(n), num(u), usd(d)] for i, n, u, d in zip(*ms.values())]))],
            [("Invoices without a matching receipt", f"PO number never seen on this vendor's receiving lines "
              f"({len(um['po_number'])} POs; largest 7)", 652,
              table(["PO", "PO date", "Invoice date", "Invoice dollars", "Freight"],
                    [[str(n), str(a), str(b_), usd(d), usd(f)] for n, a, b_, d, f in list(zip(*um.values()))[:7]])),
             ("Items to order from this vendor", "Lines due at the Restock page's what-if settings · top 5", 652,
              table(["Item", "Store", "Status", "Units to order", "Order value"],
                    [[i, s_, stcell(st), num(u), usd(o)] for i, s_, st, u, o in zip(*od.values())], left=(0, 1, 2)))],
        ]}


def phone(pid, page, body, titles):
    """A page's phone layout: same visuals, positions from build_report's mobile.json."""
    pg = build_report.Page(pid, "", "", "")
    getattr(build_report, pid)(pg)
    for i, k in enumerate(page["kpis"]):
        body[f"kpi_{i}"] = {"kpi": k}
    items = [{**pos, "id": name, "title": titles.get(name, ""), "body": body[name]} for name, pos in pg.mobile.items()]
    return {"kpis": [], "rows": [], "phone": {"of": page["name"], "width": PHONE_W,
                                             "height": max(i["y"] + i["height"] for i in items), "items": items}}


def short_bars(f, left=128):
    t = {**f["data"][0]}
    t.update(y=[str(v).replace("<br>", " ")[:22] for v in t["y"][:5]], x=t["x"][:5], text=t["text"][:5])
    t.pop("customdata", None); t["hovertemplate"] = "%{y}: %{text}<extra></extra>"
    return fig([t], yaxis=f["layout"]["yaxis"], xaxis=dict(visible=False, range=[0, max(t["x"]) * 1.45]),
               margin=dict(l=left, r=8, t=4, b=8))


def page_restock_phone(restock):
    (t1, _, _, order), _ = restock["rows"][0]
    tbl = restock["rows"][1][0][3]
    body = {"title": {"html": '<div class="ptitle"><b>Restock list</b><span>What to order now, per vendor</span></div>'},
            "bar_order_vendor": short_bars(order, left=150),
            "slicer_service": {"html": '<div class="chip">Service level<b>95%</b></div>'},
            "slicer_review": {"html": '<div class="chip">Review every (days)<b>7</b></div>'},
            "table_order": table(["Store", "Item", "Units", "Value"],
                                 [[r[0][:14], r[1][:16], r[8], r[9]["v"]] for r in tbl["rows"][:9]], left=(0, 1))}
    return phone("restock", restock, body, {"bar_order_vendor": "Order value by vendor · top 5",
                                            "table_order": "Restock list · largest 9"})


def page_phone(overview):
    """The Overview page's phone layout."""
    (st_title, st_sub, _, status), (_, _, _, headline) = overview["rows"][0]
    (t1, _, _, order), (t2, _, _, excess) = overview["rows"][1]
    body = {"title": {"html": '<div class="ptitle"><b>Stock &amp; restock</b><span>Where stock is short, where cash is '
                              'tied up</span></div>'},
            "card_headline": {"html": headline["html"].split('<div class="split">')[0]},
            "col_status": status,
            "slicer_analyze": {"html": '<div class="seg">' + "".join(
                f'<span class="{"on" if a == "Vendor" else ""}">{a}</span>'
                for a in ("Vendor", "Store", "City", "Category", "ABC")) + '</div>'},
            "bar_order_by": short_bars(order), "bar_excess_by": short_bars(excess, left=100)}
    return phone("overview", overview, body, {"col_status": "Lines by stock status", "bar_order_by": "Order value due now · top 5",
                                              "bar_excess_by": "Excess stock · top 5", "card_headline": "In one sentence"})


BUILD = {"overview": page_overview, "restock": page_restock, "health": page_health, "demand": page_demand,
         "trends": page_trends, "vendors": page_vendors, "stores": page_stores, "assortment": page_assortment,
         "movement": page_movement, "diagnose": page_diagnose,
         "item_detail": page_item_detail, "vendor_detail": page_vendor_detail}

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stock &amp; Restock</title>
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
.good{color:var(--good)!important}.bad{color:var(--bad)!important}
.row{display:flex;gap:16px;margin-bottom:16px}
.panel{padding:12px 14px 8px;min-width:0;display:flex;flex-direction:column}
.panel h3{margin:0;font-size:15px;font-weight:600}
.panel>p{margin:2px 0 6px;font-size:12px;color:var(--muted)}
.chart{flex:1;min-height:0;overflow:auto}
.row.r2 .panel{height:332px}.row.r3 .panel{height:316px}.row .panel.full{height:776px}
.stack{display:flex;flex-direction:column;gap:16px;background:none;border:0;padding:0}
.narr{font-size:14px;line-height:1.45;color:%(ink)s;margin:6px 0 0}.stack .panel{flex:none}.stack .panel.grow{flex:1;min-height:0}
.row.r2 .stack.tight{height:332px;gap:8px}.stack.tight>.panel:first-child{padding:6px 8px;justify-content:center}.stack.tight>.panel:first-child .chart{flex:none;overflow:visible}
table{width:100%%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th{font-size:11px;color:var(--muted);font-weight:600;text-align:right;padding:7px 10px;border-bottom:1px solid var(--hair);position:sticky;top:0;background:var(--surface)}
td{text-align:right;padding:7px 10px;border-bottom:1px solid #ECEBE6}
th:first-child,td:first-child{font-weight:600}td.l,th.l{text-align:left}
tr.shade td{background:#E8F1EC;font-weight:600}
.panel.dense table{font-size:12px}.panel.dense th,.panel.dense td{padding:4px 7px}
.headline{font-size:17px;line-height:1.45;margin:6px 0 14px}
.split{display:flex;gap:16px;align-items:stretch;margin-top:auto}.split>*{flex:1}
.mini{border:1px solid var(--hair);border-radius:10px;padding:10px 12px;margin-bottom:8px}
.mini span{display:block;font-size:11px;color:var(--muted)}.mini b{font-size:20px;font-weight:600}
.steps{display:flex;flex-wrap:wrap;gap:4px;margin:10px 0 14px}.steps span{border:1px solid var(--hair);border-radius:6px;padding:3px 7px;font-size:11px;color:var(--ink2)}
.steps span.on{background:var(--side);color:#fff;border-color:var(--side)}
h4{margin:0 0 4px;font-size:12px}.note{font-size:12px;color:var(--ink2);line-height:1.5;margin:0}
.ind{display:inline-block;padding-left:18px;font-weight:400}
.top{display:flex;gap:16px;margin-bottom:16px;height:96px}
.cardviz{display:flex;background:var(--surface);border:1px solid var(--hair);border-radius:12px;padding:12px 0}
.tile{flex:1;border-left:4px solid var(--accent);margin-left:14px;padding:2px 12px;border-right:1px solid var(--hair)}
.tile:last-child{border-right:0}
.tile b{display:block;font-size:24px;font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.tile span{font-size:11px;color:var(--muted)}
.btnslicer{background:var(--surface);border:1px solid var(--hair);border-radius:12px;padding:8px 12px}
.btnslicer h3{margin:0 0 6px;font-size:13px;font-weight:600}.btnslicer p{margin:4px 0 0;font-size:10px;color:var(--muted)}
.btns{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.btns button{all:unset;text-align:center;padding:5px 0;border:1px solid var(--hair);border-left:3px solid var(--accent);border-radius:8px;background:var(--plane);font-size:12px;color:var(--ink)}
.phonewrap{display:flex;gap:32px;align-items:flex-start}
.phone{border:10px solid #1d1d1b;border-radius:34px;padding:14px 10px 18px;background:var(--plane);box-shadow:0 8px 24px rgba(0,0,0,.12)}
.phone .canvas{position:relative}
.phone .panel,.phone .kpi{position:absolute;padding:8px 10px 4px}
.phone .panel h3{font-size:12px}.phone .kpi b{font-size:18px}.phone .kpi span{font-size:10px}
.phone .headline{font-size:13px;margin:2px 0}
.ptitle b{display:block;font-size:17px}.ptitle span{font-size:11px;color:var(--ink2)}
.phone .bare{background:none;border:0;padding:0}
.seg{display:flex;gap:4px}.seg span{flex:1;text-align:center;border:1px solid var(--hair);border-radius:6px;padding:5px 0;font-size:10px;color:var(--ink2)}
.seg span.on{background:var(--side);color:#fff;border-color:var(--side)}
.phone table{font-size:10px}.phone th,.phone td{padding:4px 5px}.phone .chip{white-space:normal}
header .chips{position:relative}
details.help summary,.reset{list-style:none;cursor:pointer;border:1px solid var(--hair);border-radius:8px;background:var(--surface);width:34px;height:42px;display:flex;align-items:center;justify-content:center;font-size:16px;font-weight:600;color:var(--ink)}
details.help summary::-webkit-details-marker{display:none}
details.help>div{position:absolute;right:0;top:50px;width:420px;z-index:5;background:var(--surface);border:1px solid var(--hair);border-radius:10px;padding:12px 14px;font-size:12px;line-height:1.5;color:var(--ink2);box-shadow:0 6px 18px rgba(0,0,0,.1)}
nav small{display:block;padding:14px 16px 4px;font-size:10px;letter-spacing:.12em;font-weight:600;color:var(--side-text)}
.tree{display:flex;flex-direction:column;gap:10px;margin-top:4px}
.tree{gap:6px}.node{border:1px solid var(--hair);border-radius:10px;padding:5px 12px}.node span{font-size:10px;color:var(--muted);margin-right:8px}
.node b{font-size:13px}.node i{display:block;height:7px;border-radius:4px;background:var(--bad);margin:4px 0 2px}.node em{font-style:normal;font-size:11px;color:var(--ink2)}
.ask{border:1px solid var(--accent);border-radius:8px;padding:8px 12px;font-size:14px}
.qa{display:flex;justify-content:space-between;border-bottom:1px solid #ECEBE6;padding:5px 2px;font-size:13px}
@media (max-width:1100px){.app{grid-template-columns:1fr}aside{position:static;height:auto}.row{flex-direction:column}.panel{width:100%%!important}.kpis{grid-auto-flow:row;grid-template-columns:repeat(2,1fr)}}
</style></head>
<body><div class="app">
<aside><div class="brand"><small>BIBITOR LLC</small><b>Stock &amp; Restock</b></div><nav id="nav"></nav>
<footer>Source: Bibitor LLC inventory case data, 2016 (Kaggle mirror, Apache 2.0)<br>Pipeline: CSV · DuckDB SQL · Power BI (TMDL + PBIR)<br>Static preview of the Power BI report</footer></aside>
<main id="main"></main></div>
<script>%(plotly)s</script>
<script>
const PAGES = %(pages)s;
const CHIPS = [['City','All'],['Vendor','All'],['Category','All'],['ABC class','All']];
const nav = document.getElementById('nav'), main = document.getElementById('main');
function el(tag, attrs = {}, html = '') { const e = document.createElement(tag); Object.assign(e, attrs); e.innerHTML = html; return e; }
function tableHtml(b) {
  return '<table><thead><tr>' + b.columns.map((h, j) => `<th class="${b.left.includes(j) ? 'l' : ''}">${h}</th>`).join('') + '</tr></thead><tbody>' +
    b.rows.map((row, i) => `<tr class="${b.shade.includes(i) ? 'shade' : ''}">` +
      row.map((v, j) => { const l = b.left.includes(j) ? ' l' : '';
        return v && v.v !== undefined ? `<td class="${v.t}${l}" style="${v.s || ''}">${v.v}</td>` : `<td class="${l}">${v}</td>`; }).join('') + '</tr>').join('') +
    '</tbody></table>';
}
function panel(title, sub, body, cls) {
  const p = el('div', {className: 'panel ' + (cls || '')}, (title ? `<h3>${title}</h3>` : '') + (sub ? `<p>${sub}</p>` : ''));
  const c = el('div', {className: 'chart'}); p.append(c);
  if (body.columns) c.innerHTML = tableHtml(body);
  else if (body.html) { c.innerHTML = body.html; c.style.display = 'flex'; c.style.flexDirection = 'column'; }
  else requestAnimationFrame(() => Plotly.newPlot(c, body.data, {...body.layout, autosize: true}, {displayModeBar: false, responsive: true}));
  return p;
}
function render(id) {
  const page = PAGES.find(p => p.id === id);
  [...nav.children].forEach(b => b.setAttribute('aria-current', b.dataset.id === id));
  main.innerHTML = '';
  const head = el('header');
  head.append(el('div', {}, `<h1>${page.title}${page.detail ? ': ' + page.detail : ''}</h1><p>${page.subtitle}${page.note || ''}</p>`));
  head.append(el('div', {className: 'chips'}, CHIPS.map(([k, v]) => `<div class="chip">${k}<b>${v}</b></div>`).join('') +
    (page.help ? `<div class="reset" title="Reset slicers (bookmark)">↺</div><details class="help"><summary title="How this page works">?</summary><div>${page.help}</div></details>` : '')));
  main.append(head);
  if (page.top) main.append(el('section', {className: 'top'}, page.top));
  if (page.phone) {
    const ph = page.phone, wrap = el('section', {className: 'phonewrap'});
    const frame = el('div', {className: 'phone'}), canvas = el('div', {className: 'canvas'});
    canvas.style.width = ph.width + 'px'; canvas.style.height = ph.height + 'px';
    ph.items.forEach(it => {
      const box = it.body.kpi ? el('div', {className: 'kpi'}, `<span>${it.body.kpi.label}</span><b class="${it.body.kpi.t || ''}">${it.body.kpi.value}</b>`)
        : panel(it.title, '', it.body, it.id === 'title' || it.id === 'slicer_analyze' ? 'bare' : '');
      Object.assign(box.style, {left: it.x + 'px', top: it.y + 'px', width: it.width + 'px', height: it.height + 'px'});
      canvas.append(box);
    });
    frame.append(canvas); wrap.append(frame);
    wrap.append(el('div', {className: 'panel', style: 'width:420px'}, `<h3>Phone layout</h3><p>Power BI's mobile layout for the ${ph.of} page</p><div class="note">Each visual on this frame has a <b>mobile.json</b> next to its visual.json: a ${ph.width} px column, ${ph.items.length} visuals stacked in reading order. The phone app shows this layout in portrait; navigation, header slicers and the footer stay off it (the phone app has its own page list and filter pane). The positions here are read from the same Python that writes mobile.json.</div>`));
    main.append(wrap);
  }
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
  if (p.section) nav.append(el('small', {}, p.section));
  const b = el('button', {}, p.name); b.dataset.id = p.id; b.onclick = () => render(p.id); nav.append(b);
});
addEventListener('hashchange', () => render(location.hash.slice(1)));
render((location.hash || '#overview').slice(1));
</script></body></html>"""


def main():
    pages = []
    for pid, name, title, subtitle in PAGES:
        pages.append({"id": pid, "name": name, "title": title, "subtitle": subtitle, "help": HELP.get(pid), **BUILD[pid]()})
        print(f"  ✓ {name}")
    for i, (pid, name, title, subtitle, _) in enumerate(DETAIL_PAGES):
        pages.append({"id": pid, "name": name, "title": title, "subtitle": subtitle + " Example: the largest order value.",
                      "help": HELP.get(pid), "section": "DRILLTHROUGH (EXAMPLES)" if i == 0 else None,
                      **BUILD[pid]()})
        print(f"  ✓ {name}")
    by_id = {p["id"]: p for p in pages}
    pages.append({"id": "mobile", "name": "Overview · phone", "title": "Overview on a phone", "section": "PHONE LAYOUT",
                  "subtitle": "The mobile layout Power BI shows in its phone app (portrait).",
                  **page_phone(by_id["overview"])})
    pages.append({"id": "restock_mobile", "name": "Restock · phone", "title": "Restock list on a phone",
                  "subtitle": "The mobile layout Power BI shows in its phone app (portrait).",
                  **page_restock_phone(by_id["restock"])})
    print("  ✓ phone layouts")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(HTML % {"plotly": get_plotlyjs(), "pages": json.dumps(pages, default=str),
                           "ink": INK, "ink2": INK2, "muted": MUTED, "side": SIDE, "side_text": SIDE_TEXT,
                           "side_line": SIDE_LINE, "good": ACCENT, "bad": DUE_C, "accent": ACCENT})
    print(f"  ✓ wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
