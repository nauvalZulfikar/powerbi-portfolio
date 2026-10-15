"""Statistical deep-dive on the Olist marts -> reports/insights.html

    uv run python src/analysis.py
"""
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
import statsmodels.formula.api as smf
from plotly.offline import get_plotlyjs_version

ROOT = Path(__file__).resolve().parents[1]
con = duckdb.connect(str(ROOT / "data" / "warehouse" / "olist.duckdb"), read_only=True)
q = lambda sql: con.execute(sql).df()

# ------------------------------------------------------------------ design tokens
# Reference palette from the dataviz method (validated: CVD + normal-vision + band).
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
SURFACE, GRID, AXIS = "#fcfcfb", "#e1e0d9", "#c3c2b7"
BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN, VIOLET, RED = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948")
GOOD, CRITICAL, NEUTRAL = "#0ca30c", "#d03b3b", "#c3c2b7"
BLUE_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
DIVERGING = ["#e34948", "#f19a99", "#f0efec", "#86b6ef", "#2a78d6"]  # 1★ … 5★

pio.templates["olist"] = go.layout.Template(layout=dict(
    font=dict(family="system-ui, -apple-system, 'Segoe UI', sans-serif", size=13, color=INK2),
    paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
    margin=dict(l=8, r=24, t=16, b=8),
    xaxis=dict(showgrid=False, linecolor=AXIS, ticks="", zeroline=False, tickfont_color=MUTED, automargin=True),
    yaxis=dict(gridcolor=GRID, gridwidth=1, linecolor=AXIS, zeroline=False, tickfont_color=MUTED, automargin=True),
    hoverlabel=dict(bgcolor="#ffffff", bordercolor=GRID, font_color=INK, font_size=12),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font_color=INK2, title_text=""),
    colorway=[BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN, VIOLET, RED],
    bargap=0.35,
))
pio.templates.default = "olist"
BRL = lambda v: f"R${v/1e6:,.2f}M" if v >= 1e6 else f"R${v/1e3:,.0f}K"

# ------------------------------------------------------------------ headline KPIs
k = q("""
    select
        (select sum(price) from mart.fact_order_items where is_valid)                 as revenue,
        count(*) filter (where is_valid)                                             as orders,
        count(distinct customer_unique_id) filter (where is_valid)                   as customers,
        avg(review_score)                                                            as avg_review,
        avg(is_late::int) filter (where is_delivered)                                as late_rate,
        avg(delivery_days) filter (where is_delivered)                               as delivery_days,
        (select avg((valid_orders > 1)::int) from mart.dim_customer where valid_orders > 0) as repeat_rate
    from mart.fact_orders
""").iloc[0]
yoy = q("""
    select sum(price) filter (where year(order_date) = 2018) /
           sum(price) filter (where year(order_date) = 2017) - 1 as g
    from mart.fact_order_items where is_valid and month(order_date) <= 8
""").g[0]

figs = {}

# 1 ─ Growth: monthly revenue
m = q("""
    select date_trunc('month', order_date) as month, sum(price) as revenue
    from mart.fact_order_items where is_valid and order_date between '2017-01-01' and '2018-08-31'
    group by 1 order by 1
""")
f = go.Figure(go.Scatter(
    x=m.month, y=m.revenue, mode="lines+markers", line=dict(color=BLUE, width=2),
    marker=dict(size=8, color=BLUE, line=dict(color=SURFACE, width=2)),
    fill="tozeroy", fillcolor="rgba(42,120,214,0.10)",
    hovertemplate="%{x|%b %Y}<br><b>R$%{y:,.0f}</b><extra></extra>"))
peak = m.loc[m.month == "2017-11-01"].iloc[0]
f.add_annotation(x=peak.month, y=peak.revenue, text="Black Friday<br>Nov 2017", showarrow=True,
                 arrowcolor=AXIS, ax=-60, ay=-30, font=dict(color=INK2, size=12), align="right")
f.update_layout(height=340, hovermode="x unified", yaxis=dict(tickprefix="R$", tickformat="~s"))
figs["growth"] = f

# 2 ─ Delivery vs review: 100% stacked star distribution per delivery bucket
d = q("""
    select delivery_bucket, review_score, count(*) as n
    from mart.fact_orders where review_score is not null
    group by 1, 2
""")
d["share"] = d.n / d.groupby("delivery_bucket").n.transform("sum")
order = ["7+ days early", "On time", "Late ≤ 7 days", "Late > 7 days", "Not delivered"]
avg_by_bucket = q("select delivery_bucket, avg(review_score) a, count(*) n from mart.fact_orders "
                  "where review_score is not null group by 1").set_index("delivery_bucket")
labels = [f"{b}  ·  {avg_by_bucket.a[b]:.2f}★" for b in order]
f = go.Figure()
for s, color in zip(range(1, 6), DIVERGING):
    row = d[d.review_score == s].set_index("delivery_bucket").reindex(order)
    f.add_bar(y=labels, x=row.share, name=f"{s}★", orientation="h", marker=dict(color=color, line=dict(color=SURFACE, width=2)),
              hovertemplate=f"{s}★: %{{x:.1%}}<extra></extra>")
f.update_layout(barmode="stack", height=320, bargap=0.45, yaxis=dict(autorange="reversed", gridcolor="rgba(0,0,0,0)", tickfont=dict(color=INK2, size=13)),
                xaxis=dict(tickformat=".0%", showgrid=True, gridcolor=GRID), legend=dict(traceorder="normal"))
figs["delivery_reviews"] = f

# 3 ─ Logistic regression: what drives a 1–2★ review?
lr = q("""
    select (review_score <= 2)::int                    as bad_review,
           is_late::int                                as late,
           greatest(delay_days, 0)                     as days_late,
           delivery_days,
           (item_count > 1)::int                       as multi_item,
           (seller_count > 1)::int                     as multi_seller,
           freight_value / nullif(items_value + freight_value, 0) as freight_share,
           ln(items_value + 1)                         as log_value,
           (main_payment_type = 'boleto')::int         as boleto
    from mart.fact_orders
    where is_delivered and review_score is not null and items_value > 0
""").dropna()
model = smf.logit("bad_review ~ late + days_late + delivery_days + multi_item + multi_seller"
                  " + freight_share + log_value + boleto", data=lr).fit(disp=0)
ci = np.exp(model.conf_int()).rename(columns={0: "lo", 1: "hi"})
ors = pd.DataFrame({"or": np.exp(model.params)}).join(ci).drop("Intercept")
nice = {"late": "Delivered late", "days_late": "+1 day late", "delivery_days": "+1 day delivery time",
        "multi_item": "Multiple items", "multi_seller": "Multiple sellers", "freight_share": "Freight share (0→100%)",
        "log_value": "Order value (×e)", "boleto": "Paid by boleto"}
ors = ors.rename(index=nice).sort_values("or")
f = go.Figure()
f.add_vline(x=1, line=dict(color=AXIS, width=1))
f.add_scatter(x=ors["or"], y=ors.index, mode="markers",
              marker=dict(size=11, color=[CRITICAL if v > 1 else BLUE for v in ors["or"]], line=dict(color=SURFACE, width=2)),
              error_x=dict(type="data", symmetric=False, array=ors.hi - ors["or"], arrayminus=ors["or"] - ors.lo, color=MUTED, thickness=1.5, width=0),
              hovertemplate="%{y}<br>odds ×%{x:.2f}<extra></extra>")
for name, r in ors.iterrows():
    f.add_annotation(x=np.log10(r.hi), y=name, text=f"×{r['or']:.2f}", xanchor="left", xshift=8, showarrow=False, font=dict(color=INK2, size=12))
f.update_layout(height=360, xaxis=dict(type="log", tickvals=[0.5, 0.75, 1, 1.5, 2, 3, 5, 10], ticktext=["×0.5", "×0.75", "×1", "×1.5", "×2", "×3", "×5", "×10"], minor=dict(showgrid=False), title=dict(text="Odds ratio of a 1–2★ review (log scale, 95% CI)", font=dict(size=12, color=MUTED)), showgrid=True, gridcolor=GRID),
                yaxis=dict(gridcolor="rgba(0,0,0,0)", tickfont=dict(color=INK2)))
figs["drivers"] = f

# 4 ─ Promise vs reality per state (dumbbell)
st = q("""
    select dc.state, dc.state_name, avg(fo.promised_days) promised, avg(fo.delivery_days) actual,
           avg(fo.is_late::int) late_rate, count(*) n
    from mart.fact_orders fo join mart.dim_customer dc using (customer_unique_id)
    where fo.is_delivered group by 1, 2 order by actual
""")
f = go.Figure()
for _, r in st.iterrows():
    f.add_shape(type="line", x0=r.actual, x1=r.promised, y0=r.state, y1=r.state, line=dict(color=GRID, width=3))
f.add_scatter(x=st.actual, y=st.state, mode="markers", name="Actual delivery (days)",
              marker=dict(size=10, color=BLUE, line=dict(color=SURFACE, width=2)),
              customdata=st[["state_name", "late_rate", "n"]],
              hovertemplate="<b>%{customdata[0]}</b><br>Actual %{x:.1f} d · late %{customdata[1]:.1%}<br>%{customdata[2]:,} orders<extra></extra>")
f.add_scatter(x=st.promised, y=st.state, mode="markers", name="Promised (days)",
              marker=dict(size=10, color=ORANGE, symbol="diamond", line=dict(color=SURFACE, width=2)),
              hovertemplate="Promised %{x:.1f} d<extra></extra>")
f.update_layout(height=620, yaxis=dict(gridcolor="rgba(0,0,0,0)", tickfont=dict(color=INK2)), xaxis=dict(showgrid=True, gridcolor=GRID, title=dict(text="days from purchase", font=dict(size=12, color=MUTED))))
figs["states"] = f

# 5 ─ Cohort retention heatmap
c = q("select cohort_label, cohort_size, month_offset, retention_rate from mart.cohort_retention")
piv = c.pivot(index="cohort_label", columns="month_offset", values="retention_rate").sort_index()
sizes = c.drop_duplicates("cohort_label").set_index("cohort_label").cohort_size.reindex(piv.index)
f = go.Figure(go.Heatmap(
    z=piv.values * 100, x=[f"M+{i}" for i in piv.columns], y=[f"{i}  ({sizes[i]:,})" for i in piv.index],
    colorscale=[[i / 6, h] for i, h in enumerate(BLUE_RAMP)], zmin=0, zmax=1.2, xgap=2, ygap=2,
    colorbar=dict(title=dict(text="% returning", font=dict(size=11, color=MUTED)), ticksuffix="%", outlinewidth=0, thickness=10),
    hovertemplate="Cohort %{y}<br>%{x}: <b>%{z:.2f}%</b> bought again<extra></extra>"))
f.update_layout(height=560, yaxis=dict(autorange="reversed", gridcolor="rgba(0,0,0,0)", tickfont=dict(size=11)), xaxis=dict(side="top"))
figs["cohorts"] = f

# 6 ─ Seller concentration (Lorenz / Pareto)
s = q("select lifetime_revenue from mart.dim_seller where lifetime_revenue > 0 order by 1 desc")
s["cum_rev"] = s.lifetime_revenue.cumsum() / s.lifetime_revenue.sum()
s["cum_sellers"] = np.arange(1, len(s) + 1) / len(s)
p50 = s.loc[s.cum_rev >= 0.5, "cum_sellers"].iloc[0]
p80 = s.loc[s.cum_rev >= 0.8, "cum_sellers"].iloc[0]
f = go.Figure()
f.add_scatter(x=[0, 1], y=[0, 1], mode="lines", line=dict(color=AXIS, width=1), hoverinfo="skip", showlegend=False)
f.add_scatter(x=s.cum_sellers, y=s.cum_rev, mode="lines", line=dict(color=VIOLET, width=2), fill="tonexty",
              fillcolor="rgba(74,58,167,0.08)", showlegend=False,
              hovertemplate="Top %{x:.1%} of sellers<br>= <b>%{y:.1%}</b> of revenue<extra></extra>")
for share, x in [(0.5, p50), (0.8, p80)]:
    f.add_scatter(x=[x], y=[share], mode="markers", marker=dict(size=10, color=VIOLET, line=dict(color=SURFACE, width=2)), hoverinfo="skip", showlegend=False)
    f.add_annotation(x=x, y=share, text=f"<b>{x:.1%}</b> of sellers → {share:.0%} of revenue", xanchor="left", xshift=12, showarrow=False, font=dict(color=INK, size=12))
f.update_layout(height=380, xaxis=dict(tickformat=".0%", title=dict(text="share of sellers (ranked by revenue)", font=dict(size=12, color=MUTED))),
                yaxis=dict(tickformat=".0%", range=[0, 1.02]))
figs["pareto"] = f

# 7 ─ Freight burden vs distance
fr = q("""
    select least(floor(distance_km / 250) * 250, 3000) as band, median(freight_share) med,
           quantile_cont(freight_share, 0.25) q1, quantile_cont(freight_share, 0.75) q3, count(*) n
    from mart.fact_order_items where is_valid and distance_km is not null group by 1 order by 1
""")
f = go.Figure()
f.add_scatter(x=fr.band, y=fr.q3, mode="lines", line=dict(width=0), hoverinfo="skip", showlegend=False)
f.add_scatter(x=fr.band, y=fr.q1, mode="lines", line=dict(width=0), fill="tonexty", fillcolor="rgba(235,104,52,0.12)", name="Middle 50% of items", hoverinfo="skip")
f.add_scatter(x=fr.band, y=fr.med, mode="lines+markers", name="Median freight share", line=dict(color=ORANGE, width=2),
              marker=dict(size=8, color=ORANGE, line=dict(color=SURFACE, width=2)), customdata=fr.n,
              hovertemplate="%{x:,}–%{x:,}+250 km<br>median <b>%{y:.1%}</b> of basket<br>%{customdata:,} items<extra></extra>")
f.update_layout(height=340, yaxis=dict(tickformat=".0%"), xaxis=dict(title=dict(text="seller → customer distance (km)", font=dict(size=12, color=MUTED)), ticksuffix=" km"))
figs["freight"] = f

# 8 ─ When do Brazilians shop? weekday × hour
h = q("select isodow(purchased_at) dow, order_hour, count(*) n from mart.fact_orders group by 1, 2")
hp = h.pivot(index="dow", columns="order_hour", values="n").sort_index()
f = go.Figure(go.Heatmap(z=hp.values, x=[f"{i:02d}h" for i in hp.columns], y=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                         colorscale=[[i / 6, c_] for i, c_ in enumerate(BLUE_RAMP)], xgap=2, ygap=2,
                         colorbar=dict(title=dict(text="orders", font=dict(size=11, color=MUTED)), outlinewidth=0, thickness=10),
                         hovertemplate="%{y} %{x}<br><b>%{z:,}</b> orders<extra></extra>"))
f.update_layout(height=300, yaxis=dict(autorange="reversed", gridcolor="rgba(0,0,0,0)"))
figs["heat"] = f

# 9 ─ RFM segments: customers vs revenue share (small multiples, one axis each)
r = q("""
    select rfm_segment, count(*) customers, sum(lifetime_value) revenue
    from mart.dim_customer where valid_orders > 0 group by 1
""")
r["cust_share"], r["rev_share"] = r.customers / r.customers.sum(), r.revenue / r.revenue.sum()
r = r.sort_values("rev_share")
from plotly.subplots import make_subplots
f = make_subplots(rows=1, cols=2, shared_yaxes=True, horizontal_spacing=0.06, subplot_titles=("Share of customers", "Share of revenue"))
f.add_bar(y=r.rfm_segment, x=r.cust_share, orientation="h", marker=dict(color=AXIS, cornerradius=4), row=1, col=1,
          text=r.cust_share.map("{:.1%}".format), textposition="outside", textfont=dict(color=INK2, size=11), hovertemplate="%{y}: %{x:.1%} of customers<extra></extra>")
f.add_bar(y=r.rfm_segment, x=r.rev_share, orientation="h", marker=dict(color=BLUE, cornerradius=4), row=1, col=2,
          text=r.rev_share.map("{:.1%}".format), textposition="outside", textfont=dict(color=INK2, size=11), hovertemplate="%{y}: %{x:.1%} of revenue<extra></extra>")
f.update_xaxes(tickformat=".0%", range=[0, 0.42], showgrid=True, gridcolor=GRID)
f.update_yaxes(gridcolor="rgba(0,0,0,0)", tickfont=dict(color=INK2))
f.update_annotations(font=dict(size=12, color=MUTED))
f.update_layout(height=360, showlegend=False, bargap=0.4, margin=dict(t=36))
figs["rfm"] = f

# ------------------------------------------------------------------ numbers quoted in prose
late_or = np.exp(model.params["late"])
early, very_late = avg_by_bucket.a["7+ days early"], avg_by_bucket.a["Late > 7 days"]
worst_state = st.sort_values("late_rate").iloc[-1]
m1 = c[c.month_offset == 1].retention_rate.mean()
n_sellers_50 = int(round(p50 * len(s)))
far = fr[fr.band >= 2000].med.mean(); near = fr[fr.band < 250].med.mean()
top2 = r.sort_values("rev_share").tail(2)

print(f"revenue {k.revenue:,.0f} | orders {k.orders:,} | YoY Jan–Aug {yoy:+.1%} | late {k.late_rate:.1%}")
print(f"late OR {late_or:.2f} | early {early:.2f}★ vs >7d late {very_late:.2f}★ | M+1 retention {m1:.2%}")
print(f"{n_sellers_50} sellers ({p50:.1%}) = 50% revenue | freight share near {near:.1%} vs far {far:.1%}")
print(model.summary2().tables[1][["Coef.", "P>|z|"]].round(4))

# ------------------------------------------------------------------ HTML
def fig_html(key):
    return pio.to_html(figs[key], full_html=False, include_plotlyjs=False, config={"displayModeBar": False, "responsive": True})

tiles = [
    ("Revenue", BRL(k.revenue), f"{yoy:+.0%} YoY (Jan–Aug)"),
    ("Orders", f"{k.orders:,.0f}", "Sep 2016 – Aug 2018"),
    ("Customers", f"{k.customers:,.0f}", f"{k.repeat_rate:.1%} ever bought twice"),
    ("Avg review", f"{k.avg_review:.2f}★", "of 5"),
    ("Late deliveries", f"{k.late_rate:.1%}", f"avg {k.delivery_days:.1f} days to door"),
]

sections = [
    ("01 · Growth", f"Revenue grew {yoy:.0%} year-on-year, with Black Friday 2017 the single biggest month",
     "Item revenue (price excl. freight) of valid orders. Sep–Dec 2016 and Sep–Oct 2018 are excluded — Olist's export is incomplete there.", ["growth"]),
    ("02 · Delivery drives satisfaction", f"Orders more than a week late average {very_late:.1f}★ — early ones {early:.1f}★",
     "Share of each star rating within each delivery outcome. The estimated delivery date shown to the customer at checkout is the benchmark.", ["delivery_reviews"]),
    ("03 · Controlling for everything else", f"Lateness multiplies the odds of a 1–2★ review by ×{late_or:.1f}",
     f"Logistic regression on {len(lr):,} delivered, reviewed orders (pseudo-R² {model.prsquared:.2f}). Red = raises the odds of a bad review, blue = lowers them. Multi-seller orders ship in separate parcels, which is why they also hurt.", ["drivers"]),
    ("04 · Where the promise breaks", f"{worst_state.state_name} has the highest late rate ({worst_state.late_rate:.0%}); yet every state is quoted a {(st.promised - st.actual).min():.0f}–{(st.promised - st.actual).max():.0f} day buffer",
     "Average promised vs. actual delivery days per customer state, sorted by actual. The gap is the buffer Olist builds into its estimate.", ["states"]),
    ("05 · Retention is the real problem", f"Only {m1:.2%} of a cohort returns the next month — {1-k.repeat_rate:.0%} of customers buy once",
     "Share of each first-purchase cohort (size in brackets) placing another valid order N months later. Scale capped at 1.2% so the pattern is visible.", ["cohorts"]),
    ("06 · Customer value", f"{top2.rfm_segment.iloc[1]} and {top2.rfm_segment.iloc[0]} hold {top2.rev_share.sum():.0%} of revenue from {top2.cust_share.sum():.0%} of customers",
     "RFM segments. Frequency is bucketed (1 / 2 / 3+ orders) because quintiles are meaningless when 97% of customers order once.", ["rfm"]),
    ("07 · Marketplace concentration", f"{n_sellers_50} sellers — {p50:.1%} of the base — generate half of all revenue",
     "Lorenz curve of seller revenue. The shaded area between the curve and the diagonal is inequality.", ["pareto"]),
    ("08 · The cost of distance", f"Freight eats a median {far:.0%} of the basket past 2,000 km, vs {near:.0%} locally",
     "Freight ÷ (price + freight) per item, by haversine distance between seller and customer ZIP centroids.", ["freight"]),
    ("09 · Shopping rhythm", "Brazilians shop during weekday working hours — Monday–Tuesday 11h–16h is peak, with an evening wave at 21h",
     "Orders by purchase weekday and hour (local time).", ["heat"]),
]

html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Olist Marketplace — Insight Report</title>
<script src="https://cdn.plot.ly/plotly-{get_plotlyjs_version()}.min.js"></script>
<style>
:root{{--ink:{INK};--ink2:{INK2};--muted:{MUTED};--surface:{SURFACE};--plane:#f4f3ef;--line:{GRID};--blue:{BLUE}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--plane);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}}
header{{background:#0d366b;color:#fff;padding:56px 16px 96px}}
.wrap{{max-width:1120px;margin:0 auto;padding-inline:16px}}
.eyebrow{{font-size:12px;letter-spacing:.14em;text-transform:uppercase;opacity:.7}}
h1{{font-size:clamp(28px,4.2vw,44px);line-height:1.1;margin:10px 0 14px;font-weight:650;letter-spacing:-.02em;max-width:820px}}
header p{{max-width:720px;opacity:.82;margin:0}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:-64px}}
.tile{{background:var(--surface);border-radius:14px;padding:18px 20px;box-shadow:0 1px 2px rgba(0,0,0,.05),0 8px 24px rgba(13,54,107,.08)}}
.tile .l{{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}}
.tile .v{{font-size:30px;font-weight:650;letter-spacing:-.02em;margin:4px 0 2px}}
.tile .s{{font-size:13px;color:var(--ink2)}}
section{{background:var(--surface);border-radius:14px;padding:28px 28px 16px;margin:20px 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}}
section .eyebrow{{color:var(--blue);opacity:1;font-weight:600}}
h2{{font-size:clamp(19px,2.4vw,24px);line-height:1.25;margin:6px 0 6px;font-weight:620;letter-spacing:-.01em}}
.note{{color:var(--ink2);font-size:14px;margin:0 0 14px;max-width:820px}}
.method{{color:var(--ink2);font-size:14px}} .method code{{background:#efeee9;padding:1px 5px;border-radius:4px}}
footer{{color:var(--muted);font-size:13px;padding:24px 0 48px}}
@media (max-width:600px){{section{{padding:20px 12px 8px}}}}
</style></head><body>
<header><div class="wrap">
<div class="eyebrow">Brazilian E-Commerce · Olist · 2016–2018</div>
<h1>Olist grew fast — but late deliveries and one-time buyers cap how far it can go</h1>
<p>End-to-end analysis of ~100k marketplace orders from the Kaggle Olist dataset: raw CSV → DuckDB SQL star schema → statistical modelling in Python → Power BI semantic model &amp; report.</p>
</div></header>
<main class="wrap">
<div class="tiles">{''.join(f'<div class="tile"><div class="l">{l}</div><div class="v">{v}</div><div class="s">{s_}</div></div>' for l, v, s_ in tiles)}</div>
{''.join(f'<section><div class="eyebrow">{e}</div><h2>{t}</h2><p class="note">{n}</p>{"".join(fig_html(x) for x in keys)}</section>' for e, t, n, keys in sections)}
<section class="method"><div class="eyebrow">Method</div><h2>How this was built</h2>
<p><b>Data:</b> Kaggle <code>olistbr/brazilian-ecommerce</code> — 9 CSVs, 1.55M rows. <b>SQL (DuckDB):</b> staging (typing, review de-duplication, ZIP centroids from 1M geolocation points) → star schema (4 dims, 3 facts) → analytical marts (cohorts, freight lanes, seller-month) → 12 automated data-quality tests that block export on failure. <b>Python:</b> pandas + statsmodels logistic regression, Plotly charts. <b>Power BI:</b> PBIP project with TMDL semantic model (relationships, DAX measures, time intelligence) and a 5-page PBIR report.</p>
<p><b>Definitions:</b> revenue = item price of orders not canceled/unavailable; late = delivered after the estimated date shown at checkout; customer = <code>customer_unique_id</code>.</p></section>
<footer>Generated by <code>src/analysis.py</code></footer>
</main></body></html>"""

out = ROOT / "reports" / "insights.html"
out.parent.mkdir(exist_ok=True)
out.write_text(html)
print(f"→ {out.relative_to(ROOT)}")
