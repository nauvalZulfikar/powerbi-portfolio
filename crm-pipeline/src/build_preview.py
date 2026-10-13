"""Static HTML twin of the Power BI report — same pages, layout, palette and measure definitions.

    uv run python src/build_preview.py      -> reports/dashboard.html (self-contained, opens offline)

Power BI Desktop is Windows-only; this lets the report be reviewed anywhere. It shows the
report's default state: all years, regions and products, Date Basis = Closed, Analyze By = Manager,
stale after 104 days, win probability = historical win rate. Every number is computed in DuckDB
from the same marts, with the same rules as the DAX.
"""
import json
from datetime import date
from pathlib import Path

import duckdb
from plotly.offline import get_plotlyjs

from build_report import ACCENT, INK, INK2, LOST, MUTED, OPEN, PAGES, SIDE, SIDE_LINE, SIDE_TEXT, STALE, WON

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "dashboard.html"
con = duckdb.connect(str(ROOT / "data" / "warehouse" / "crm.duckdb"), read_only=True)

STALE_DAYS = 104
F = "mart.fact_opportunity"
OPEN_STAGES = "stage_key in ('engaging', 'prospecting')"


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


def databar(share):
    """Table cell with a conditional-formatting data bar, like the PBIR columnFormatting.dataBars."""
    return f'<span class="db"><i style="width:{share * 50:.0f}px;background:{STALE}"></i>{share:.0%}</span>'


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


def base():
    """The headline measures every page shares, in the report's default state."""
    r = one(f"""select count(*) filter (where stage_key = 'closedwon') won, count(*) filter (where stage_key = 'closedlost') lost,
                       sum(close_value) filter (where stage_key = 'closedwon') rev,
                       sum(list_price) filter (where stage_key = 'closedwon') won_list,
                       avg(cycle_days) cyc, count(*) n,
                       count(*) filter (where {OPEN_STAGES}) open_n, sum(open_value) open_v,
                       count(*) filter (where stage_key = 'engaging' and age_days > {STALE_DAYS}) stale_n,
                       sum(open_value) filter (where stage_key = 'engaging' and age_days > {STALE_DAYS}) stale_v,
                       count(*) filter (where {OPEN_STAGES} and not has_account) no_acct
                from {F}""")
    k = dict(zip(["won", "lost", "rev", "won_list", "cyc", "n", "open_n", "open_v", "stale_n", "stale_v", "no_acct"], r))
    k["wr"] = k["won"] / (k["won"] + k["lost"])
    k["real"] = k["rev"] / k["won_list"]
    k["forecast"] = (k["open_v"] - k["stale_v"]) * k["wr"] * k["real"]
    return k


K = base()


def page_overview():
    m = rows(f"""select date_trunc('month', closed_date)::varchar p, count(*) filter (where stage_key = 'closedwon') w,
                        count(*) filter (where stage_key = 'closedlost') l
                 from {F} where closed_date is not null group by 1 order by 1""")
    mg = rows(f"""select a.manager, sum(close_value) filter (where stage_key = 'closedwon') rev,
                         count(*) filter (where stage_key = 'closedwon') / count(*) filter (where stage_key like 'closed%') wr
                  from {F} join mart.dim_agent a using (owner_id) group by 1 order by rev desc""")
    by_wr = sorted(zip(mg["manager"], mg["wr"]), key=lambda t: -t[1])
    headline = (f"Won {usd(K['rev'])} at a {K['wr']:.0%} win rate. {K['stale_n']:,} of {K['open_n']:,} open deals are "
                f"older than {STALE_DAYS} days, holding {K['stale_v'] / K['open_v']:.0%} of the open pipeline.")
    return {
        "kpis": kpis([("Won Revenue", usd(K["rev"])), ("Won Deals", num(K["won"])), ("Win Rate", f"{K['wr']:.1%}"),
                      ("Avg Deal Size", f"${K['rev'] / K['won']:,.0f}"), ("Open Pipeline", usd(K["open_v"])),
                      ("Stale Share", f"{K['stale_v'] / K['open_v']:.0%}", "bad")]),
        "rows": [
            [("Deals closed per month", "Won vs lost; switch Date Basis to see when they were engaged instead", 880,
              grouped(m["p"], [("Won Deals", m["w"], WON), ("Lost Deals", m["l"], LOST)],
                      xaxis=dict(tickformat="%b %y", dtick="M1"), yaxis=NUM_Y)),
             ("In one sentence", "Follows the slicers", 424,
              {"html": f'<div class="headline">{headline}</div><div class="split">'
                       f'<div class="mini"><span>Weighted Forecast</span><b>{usd(K["forecast"])}</b></div>'
                       f'<div class="mini"><span>Open Without Account</span><b>{K["no_acct"]:,}</b></div></div>'})],
            [("Won revenue by manager", "Field parameters: Analyze by = Manager, Metric = Won revenue (also win rate, deal size, cycle, deals)", 652,
              hbars(mg["manager"], mg["rev"], WON, text=[usd(v) for v in mg["rev"]], left=130)),
             ("Win rate by the same dimension", "Revenue differs a lot; win rate barely moves", 652,
              hbars([m for m, _ in by_wr], [w for _, w in by_wr], ACCENT, text=[f"{w:.1%}" for _, w in by_wr], left=130))],
        ]}


def page_funnel():
    fn = rows("select step, count(*) n from mart.fact_funnel group by step, step_sort order by step_sort")
    cb = rows(f"""select cycle_band b, count(*) filter (where stage_key = 'closedwon') w, count(*) filter (where stage_key = 'closedlost') l
                  from {F} join mart.dim_cycle_band using (cycle_band) group by b, cycle_sort order by cycle_sort""")
    cm = rows(f"""select date_trunc('month', closed_date)::varchar p, avg(cycle_days) c from {F}
                  where closed_date is not null group by 1 order by 1""")
    wr = [w / (w + l) for w, l in zip(cb["w"], cb["l"])]
    return {
        "kpis": kpis([("Deals", num(K["n"])), ("Closed Deals", num(K["won"] + K["lost"])), ("Win Rate", f"{K['wr']:.1%}"),
                      ("Avg Cycle Days", f"{K['cyc']:.0f}")]),
        "rows": [
            [("Deal funnel", "Deals reaching each step; Date Basis = Created shows a cohort", 640,
              fig([dict(type="funnel", y=fn["step"], x=fn["n"], marker=dict(color=WON), texttemplate="%{value:,}<br>%{percentInitial:.0%}",
                        textfont=dict(color="#fff", size=12), connector=dict(line=dict(color="#E6E4DD")),
                        hovertemplate="%{y}: %{x:,}<extra></extra>")],
                  yaxis=dict(tickprefix="", tickformat="", showgrid=False, zeroline=False, tickfont=dict(color=INK2, size=11)),
                  margin=dict(l=70, r=12, t=8, b=8))),
             ("Win rate by days from engage to close", "Low 0–14 bar is a data quirk: Apr, Jul and Oct closes all took ~1 week and won 49%", 664,
              vbars(cb["b"], wr, ACCENT, [f"{v:.0%}" for v in wr], yaxis=dict(tickprefix="", tickformat=".0%", range=[0, 0.85])))],
            [("Closed deals by cycle length", "Outside Apr, Jul and Oct, quick closes win 67%, like every other band", 640,
              grouped(cb["b"], [("Won Deals", cb["w"], WON), ("Lost Deals", cb["l"], LOST)], yaxis=NUM_Y)),
             ("Average cycle by close month ⓘ", "Days from engage to close; Apr, Jul and Oct dip to ~6 days in the source data", 664,
              lines(cm["p"], [("Avg Cycle Days", cm["c"], ACCENT, "solid", "%{y:.0f} days"),
                              ("Stale after 104 days", [STALE_DAYS] * len(cm["p"]), STALE, "dash", "%{y:.0f} days"),
                              ("Average", [K["cyc"]] * len(cm["p"]), INK2, "dot", "%{y:.0f} days")], yaxis=NUM_Y))],
        ]}


def page_pipeline():
    sn = rows(f"""select date_trunc('month', snapshot_date)::varchar p, sum(open_value) o,
                         coalesce(sum(open_value) filter (where stage_key = 'engaging' and age_days > {STALE_DAYS}), 0) s
                  from mart.fact_pipeline_snapshot group by 1 order by 1""")
    ab = rows(f"""select b.age_band, coalesce(sum(f.open_value), 0) v from mart.dim_age_band b
                  left join {F} f using (age_band) group by b.age_band, b.age_sort order by b.age_sort""")
    ag = rows(f"""select a.agent, a.manager, count(*) filter (where {OPEN_STAGES}) o,
                         count(*) filter (where stage_key = 'engaging' and age_days > {STALE_DAYS}) s,
                         coalesce(sum(open_value) filter (where stage_key = 'engaging' and age_days > {STALE_DAYS}), 0) sv,
                         sum(open_value) ov
                  from {F} join mart.dim_agent a using (owner_id) group by all order by sv desc limit 7""")
    note = ('<h4>How to read it <span class="info" title="Stale = engaging deal older than 104 days (P90 of the cycle)">i</span></h4><p class="note">Stale = an engaging deal older than the threshold. 104 days is how long '
            '90% of closed deals took; past that, a deal rarely closes. Weighted forecast = live (non-stale) pipeline × win '
            'probability × historical price realisation. Leave win probability empty to use the historical win rate.</p>')
    return {
        "kpis": kpis([("Open Deals", num(K["open_n"])), ("Open Pipeline", usd(K["open_v"])),
                      ("Stale Deals", num(K["stale_n"]), "bad"), ("Stale Pipeline", usd(K["stale_v"]), "bad"),
                      ("Weighted Forecast", usd(K["forecast"])), ("Open Without Account", num(K["no_acct"]))]),
        "rows": [
            [("Open pipeline at each month-end sync", "List value; the stale part grows every month", 880,
              lines(sn["p"], [("Snapshot Open Pipeline", sn["o"], OPEN, "solid", "%{y:$,.0f}"),
                              ("Snapshot Stale Pipeline", sn["s"], STALE, "solid", "%{y:$,.0f}")],
                    yaxis=dict(rangemode="tozero"))),
             ("", "", 424, {"html": '<div class="split" style="margin:0 0 12px">'
                                    f'<div class="chip">What-if: stale after (days)<b>{STALE_DAYS}</b></div>'
                                    f'<div class="chip">What-if: win probability<b>{K["wr"]:.0%} (historical)</b></div></div>' + note})],
            [("Open pipeline by deal age", "Days since the deal was engaged, at the last sync", 640,
              vbars(ab["age_band"], ab["v"], OPEN, [usd(v) for v in ab["v"]])),
             ("", "", 664, {"stack": [
                 ("", "", 54, {"html": '<div class="chips"><span class="tog on">Stale only</span><span class="tog">All open</span>'
                                       '<span class="note">bookmarks swap the two grouped tables</span></div>'}),
                 ("Who holds the stale deals", "Right-click an agent → Drill through → Deal Detail for the call list", 0,
                  table(["Sales agent", "Manager", "Open deals", "Stale deals", "Stale pipeline", "Stale share"],
                        [[a, m, num(o), num(s), usd(sv), databar(sv / ov)]
                         for a, m, o, s, sv, ov in zip(ag["agent"], ag["manager"], ag["o"], ag["s"], ag["sv"], ag["ov"])],
                        left=(0, 1)))]})],
        ]}


def page_team():
    mg = rows(f"""select a.regional_office r, a.manager m, sum(close_value) filter (where stage_key = 'closedwon') rev,
                         count(*) filter (where stage_key = 'closedwon') w, count(*) filter (where stage_key like 'closed%') c,
                         sum(open_value) ov
                  from {F} join mart.dim_agent a using (owner_id) group by all order by rev desc""")
    # Region Win Rate (ALLEXCEPT Agent but Region): won ÷ closed over the whole region
    rw = {}
    for r, w, c in zip(mg["r"], mg["w"], mg["c"]):
        rw.setdefault(r, [0, 0]); rw[r][0] += w; rw[r][1] += c
    rw = {r: w / c for r, (w, c) in rw.items()}
    ag = rows(f"""select a.agent, sum(close_value) filter (where stage_key = 'closedwon') rev
                  from {F} join mart.dim_agent a using (owner_id) group by 1 having rev > 0 order by rev desc""")
    return {
        "kpis": kpis([("Won Revenue", usd(K["rev"])), ("Win Rate", f"{K['wr']:.1%}"),
                      ("Avg Deal Size", f"${K['rev'] / K['won']:,.0f}"), ("Avg Cycle Days", f"{K['cyc']:.0f}")]),
        "rows": [
            [("Won revenue by manager", "", 560, hbars(mg["m"], mg["rev"], WON, text=[usd(v) for v in mg["rev"]], left=130)),
             ("Manager scorecard", "Win rates sit within two points of each other; vs region (green above, red below) uses ALLEXCEPT", 744,
              table(["Region", "Manager", "Won revenue", "Won deals", "Win rate", "vs region", "Avg deal size", "Open pipeline"],
                    [[r, m, usd(v), num(w), f"{w / c:.1%}", {"v": f"{w / c - rw[r]:+.1%}", "t": "up" if w / c >= rw[r] else "dn"},
                      f"${v / w:,.0f}", usd(o)]
                     for r, m, v, w, c, o in zip(mg["r"], mg["m"], mg["rev"], mg["w"], mg["c"], mg["ov"])], left=(0, 1)))],
            [("Won revenue by sales agent", "Report filter Has Deals = true hides the five agents with no deals. Hover a bar for the agent tooltip", 1320,
              vbars([wrap(a, 10) for a in ag["agent"]], ag["rev"], WON, [usd(v) for v in ag["rev"]],
                    xaxis=dict(tickfont=dict(size=9, color=MUTED), tickangle=0), margin=dict(l=52, r=12, t=16, b=44)))],
        ]}


def page_products():
    pr = rows(f"""select p.product, p.series, p.list_price lp, count(*) filter (where stage_key = 'closedwon') w,
                         count(*) filter (where stage_key like 'closed%') c,
                         sum(close_value) filter (where stage_key = 'closedwon') rev,
                         sum(f.list_price) filter (where stage_key = 'closedwon') wl
                  from {F} f join mart.dim_product p using (product_id) group by all order by rev desc""")
    se = rows(f"""select a.sector, sum(close_value) filter (where stage_key = 'closedwon') rev from {F}
                  join mart.dim_account a using (account_id) where a.account_id <> '0' group by 1 order by rev desc""")
    ac = rows(f"""select a.account, a.sector, a.country, a.parent_company,
                         sum(close_value) filter (where stage_key = 'closedwon') rev,
                         count(*) filter (where stage_key = 'closedwon') / count(*) filter (where stage_key like 'closed%') wr
                  from {F} join mart.dim_account a using (account_id) group by all order by rev desc limit 8""")
    return {
        "kpis": kpis([("Won Revenue", usd(K["rev"])), ("Avg Deal Size", f"${K['rev'] / K['won']:,.0f}"),
                      ("Price Realisation", f"{K['real']:.1%}"), ("Win Rate", f"{K['wr']:.1%}")]),
        "rows": [
            [("Won revenue by product", "", 560, hbars(pr["product"], pr["rev"], WON, text=[usd(v) for v in pr["rev"]], left=110)),
             ("Product scorecard", "Price realisation = won revenue ÷ list price; deals close both above and below list", 744,
              table(["Product", "Series", "List price", "Won deals", "Win rate", "Price realisation", "Won revenue"],
                    [[p, s, f"${lp:,.0f}", num(w), f"{w / c:.1%}", f"{v / wl:.1%}", usd(v)]
                     for p, s, lp, w, c, v, wl in zip(pr["product"], pr["series"], pr["lp"], pr["w"], pr["c"], pr["rev"], pr["wl"])],
                    left=(0, 1)))],
            [("Won revenue by sector", "", 560, hbars(se["sector"], se["rev"], WON, text=[usd(v) for v in se["rev"]], left=120)),
             ("Accounts", "Parent company groups subsidiaries (e.g. Acme Corporation owns four)", 744,
              table(["Account", "Sector", "Country", "Parent company", "Won revenue", "Win rate", "Revenue share"],
                    [[a, s, c, pc, usd(v), f"{w:.1%}", f"{v / K['rev']:.1%}"]
                     for a, s, c, pc, v, w in zip(ac["account"], ac["sector"], ac["country"], ac["parent_company"], ac["rev"], ac["wr"])],
                    left=(0, 1, 2, 3)))],
        ]}


RAW = ROOT / "data" / "raw"


def reconciliation():
    """DuckDB twin of the Power Query table Source Reconciliation (Group By, Append, Merge)."""
    return rows(f"""
        with raw as (select deal_stage stage, count(*) n, sum(close_value) v
                     from read_csv('{RAW / "sales_pipeline.csv"}', header = true) group by 1),
             mart as (select s.stage, count(*) n, sum(f.close_value) v from {F} f join mart.dim_stage s using (stage_key) group by 1),
             appended as (select 'Raw CSV' src, * from raw union all select 'Mart (API syncs)', * from mart)
        select a.src, a.stage, a.n, a.v, a.n - r.n dn, coalesce(a.v, 0) - coalesce(r.v, 0) dv
        from appended a join raw r using (stage) join mart.dim_stage s on s.stage = a.stage
        order by s.stage_sort, a.src desc""")


def login_check():
    """DuckDB twin of the Power Query table Agent Login Check (split name, rebuild login, compare)."""
    return one(f"""
        with t as (select trim(sales_agent) agent, trim(manager) manager from read_csv('{RAW / "sales_teams.csv"}', header = true)),
             c as (select t.*, lower(split_part(t.agent, ' ', 1) || '.' || split_part(t.agent, ' ', 2)) || '@maven-crm.example' login,
                          a.email, exists (select 1 from mart.manager_access m where m.manager = t.manager) covered
                   from t left join mart.dim_agent a on a.agent = t.agent)
        select count(*), count(*) filter (where login is distinct from email or not covered) from c""")


def page_sync():
    s = rows("select * from mart.sync_runs order by as_of")
    tot = {k: sum(s[k]) for k in ("requests", "retries", "rate_limited", "server_errors", "deal_versions_added")}
    rec = reconciliation()
    gaps = len({st for st, dn, dv in zip(rec["stage"], rec["dn"], rec["dv"]) if dn or dv})
    agents, mismatches = login_check()
    note = ('<h3>How the extract works</h3><p class="note" style="margin:8px 0">Reference objects (owners, companies, products) '
            'reload in full. Deals are pulled with a modified-since search from the last watermark, and every new version is '
            'kept, so the warehouse can rebuild the pipeline as it stood at each sync.</p><p class="note">429 responses wait '
            'for Retry-After; 5xx responses back off exponentially; other 4xx fail fast. Power BI then refreshes '
            f'incrementally: only partitions whose latest modified date changed.</p><p class="note" style="margin-top:8px">'
            f'Login check: {agents} agents, first.last@ login rebuilt from the seed CSV in Power Query; {mismatches} differ '
            'from the model email or lack a manager row in the RLS table.</p>')
    x = [d.strftime("%d %b") for d in s["as_of"]]
    first = [rq - rt for rq, rt in zip(s["requests"], s["retries"])]
    stacked = fig([dict(type="bar", name=n, x=x, y=y, marker=dict(color=c), hovertemplate=f"{n} · %{{x}}: %{{y:,}}<extra></extra>")
                   for n, y, c in (("Answered first time", first, ACCENT), ("Rate Limited 429", s["rate_limited"], OPEN),
                                   ("Server Errors 5xx", s["server_errors"], STALE))],
                  barmode="stack", showlegend=True, legend=LEGEND, yaxis=NUM_Y)
    return {
        "kpis": kpis([("Sync Count", num(len(s["as_of"]))), ("API Requests", num(tot["requests"])),
                      ("Throttled Requests", num(tot["rate_limited"])), ("Server Error Responses", num(tot["server_errors"])),
                      ("Reconciliation Gaps", num(gaps)), ("Login Mismatches", num(mismatches))]),
        "rows": [
            [("Deal versions loaded per sync", "First run is a full load; the rest pull only what changed", 420,
              vbars(x, s["deal_versions_added"], ACCENT, [num(v) for v in s["deal_versions_added"]], yaxis=NUM_Y)),
             ("API responses per sync", "Stacked: answered first time, 429 and 5xx add up to the requests sent", 448, stacked),
             ("", "", 420, {"html": note})],
            [("Seed CSV vs warehouse, per stage",
              "Power Query: Group By, Append, Merge. Zero difference = nothing lost in the API syncs", 560,
              table(["Source", "Stage", "Deal rows", "Close value", "Deals diff", "Value diff"],
                    [[src, st, num(n), usd(v), f"{dn:+,}", f"{dv:+,.0f}"]
                     for src, st, n, v, dn, dv in zip(rec["src"], rec["stage"], rec["n"], rec["v"], rec["dn"], rec["dv"])],
                    left=(0, 1))),
             ("Run log", "", 744,
              table(["Sync date", "Mode", "Requests", "429", "5xx", "Deals seen", "Versions added", "Seconds"],
                    [[d.strftime("%d %b %Y"), m, rq, rl, se, num(ds), num(va), f"{sec:.1f}"]
                     for d, m, rq, rl, se, ds, va, sec in zip(s["as_of"], s["mode"], s["requests"], s["rate_limited"],
                                                              s["server_errors"], s["deals_seen"], s["deal_versions_added"],
                                                              s["seconds"])],
                    left=(0, 1)))],
        ]}


# ------------------------------------------------------------------ Trends & Drivers
WON_M = f"""select date_trunc('month', closed_date)::date m, sum(close_value) v from {F}
            where stage_key = 'closedwon' group by 1 order by 1"""


def css_bars(labels, values, color_, fmt, label_w=78):
    """Tiny horizontal bar list in plain HTML (used where Plotly does not fit: the phone frame, the tree)."""
    top = max(values) or 1
    return "".join(f'<div class="hb"><span style="width:{label_w}px">{l}</span><i style="width:{max(2, v / top * 100):.1f}%;'
                   f'background:{color_}"></i><em>{fmt(v)}</em></div>' for l, v in zip(labels, values))


def trend_forecast(months, v, ahead=3):
    """Twin of Revenue Trend Forecast: least squares of monthly won revenue on a month index (LINESTX), extended `ahead` months."""
    xs = [d.year * 12 + d.month for d in months]
    n, mx, my = len(xs), sum(xs) / len(xs), sum(v) / len(v)
    b = sum((a - mx) * (c - my) for a, c in zip(xs, v)) / sum((a - mx) ** 2 for a in xs)
    out = [date(d.year + (d.month + k - 1) // 12, (d.month + k - 1) % 12 + 1, 1) for d in months[-1:] for k in range(1, ahead + 1)]
    allm = list(months) + out
    return [d.isoformat() for d in allm], [my + b * (d.year * 12 + d.month - mx) for d in allm]


def page_trends():
    m = rows(WON_M)
    x, v = [d.isoformat() for d in m["m"]], m["v"]
    cum = [sum(v[:i + 1]) for i in range(len(v))]
    mov = [sum(v[max(0, i - 2):i + 1]) / len(v[max(0, i - 2):i + 1]) for i in range(len(v))]
    best = max(range(len(v)), key=lambda i: v[i])
    fx, fy = trend_forecast(m["m"], v)
    win = []
    for i, d in enumerate(m["m"]):
        prev = v[i - 1] if i else None
        win.append([d.strftime("%b %Y"), usd(v[i]), usd(prev), cell(pct((v[i] - prev) / prev), v[i] - prev) if prev else "—",
                    usd(sum(v[max(0, i - 2):i + 1]))])
    # small multiples: one panel per manager, shared y axis
    sm = rows(f"""select a.manager, date_trunc('month', closed_date)::date m, sum(close_value) v from {F}
                  join mart.dim_agent a using (owner_id) where stage_key = 'closedwon' group by all order by 1, 2""")
    mgrs = list(dict.fromkeys(sm["manager"]))
    ymax = max(sm["v"]) * 1.1
    traces, lay = [], {"grid": dict(rows=2, columns=3, pattern="independent", xgap=0.08, ygap=0.22), "annotations": []}
    for i, mg in enumerate(mgrs[:6]):
        k = "" if i == 0 else str(i + 1)
        xs = [d.isoformat() for g, d in zip(sm["manager"], sm["m"]) if g == mg]
        ys = [val for g, val in zip(sm["manager"], sm["v"]) if g == mg]
        traces.append(dict(type="scatter", mode="lines", x=xs, y=ys, xaxis="x" + k, yaxis="y" + k, line=dict(color=WON, width=2),
                           fill="tozeroy", fillcolor="rgba(0,0,0,0.04)", hovertemplate=f"{mg} · %{{x|%b %Y}}: %{{y:$,.0f}}<extra></extra>"))
        lay["xaxis" + k] = {**AXIS, "tickformat": "%b", "dtick": "M3", "showticklabels": i >= 3}
        lay["yaxis" + k] = {**YAXIS, "range": [0, ymax], "showticklabels": i % 3 == 0, "nticks": 3}
        lay["annotations"].append(dict(text=f"<b>{mg}</b>", xref=f"x{k} domain", yref=f"y{k} domain", x=0, y=1.16,
                                       showarrow=False, xanchor="left", font=dict(size=11, color=INK2)))
    # Time Intelligence calc group in a quarter context (Gregorian calendar, one year of closes)
    q = rows(f"""select quarter(closed_date) q, month(closed_date) mo, sum(close_value) v from {F}
                 where stage_key = 'closedwon' group by all order by 1, 2""")
    by_m = {mo: val for mo, val in zip(q["mo"], q["v"])}
    ti = []
    for qq in sorted(set(q["q"])):
        months = range(3 * qq - 2, 3 * qq + 1)
        cur = sum(by_m.get(i, 0) for i in months)
        prior = sum(by_m.get(i - 1, 0) for i in months)  # DATEADD -1 MONTH over the quarter's dates
        ytd = sum(val for mo, val in by_m.items() if mo <= 3 * qq)
        ti.append([f"Q{qq}", usd(cur), usd(cur), usd(ytd), cell(pct((cur - prior) / prior), cur - prior) if prior else "—"])
    ti.append(["Total", usd(sum(v)), usd(sum(by_m.get(i, 0) for i in (10, 11, 12))), usd(sum(v)), "—"])
    return {
        "kpis": kpis([("Won Revenue Compact", f"${K['rev'] / 1e6:.1f}M"), ("Best Month", m["m"][best].strftime("%b %Y")),
                      ("Best Month Revenue", usd(v[best])), ("Avg Deal Size", f"${K['rev'] / K['won']:,.0f}")]),
        "rows": [
            [("Won revenue per month, running total and 3-month average",
              "Visual calculations: RUNNINGSUM and MOVINGAVERAGE over the months on the axis", 880,
              fig([dict(type="bar", name="Won Revenue", x=x, y=v, marker=dict(color=WON, cornerradius=3),
                        hovertemplate="Won · %{x|%b %Y}: %{y:$,.0f}<extra></extra>"),
                   dict(type="scatter", mode="lines+markers", name="Moving average, 3 months", x=x, y=mov,
                        line=dict(color=OPEN, width=2.2), marker=dict(size=5),
                        hovertemplate="3-month average · %{x|%b %Y}: %{y:$,.0f}<extra></extra>"),
                   dict(type="scatter", mode="lines", name="Linear trend (Jan–Mar 2018 = projection)", x=fx, y=fy,
                        line=dict(color=MUTED, width=2, dash="dash"),
                        hovertemplate="Trend · %{x|%b %Y}: %{y:$,.0f}<extra></extra>"),
                   dict(type="scatter", mode="lines+markers", name="Cumulative won", x=x, y=cum, yaxis="y2",
                        line=dict(color=ACCENT, width=2.2), marker=dict(size=5),
                        hovertemplate="Cumulative · %{x|%b %Y}: %{y:$,.0f}<extra></extra>")],
                  showlegend=True, legend=LEGEND, xaxis=dict(tickformat="%b %y", dtick="M1"),
                  yaxis2=dict(overlaying="y", side="right", showgrid=False, range=[0, max(cum) * 1.08], tickvals=[0, 2.5e6, 5e6, 7.5e6, 10e6], ticktext=["$0", "$2.5M", "$5M", "$7.5M", "$10M"],
                              tickfont=dict(color=MUTED, size=10), rangemode="tozero"),
                  margin=dict(l=52, r=48, t=8, b=32))),
             ("Window functions, month by month", "OFFSET = previous month, WINDOW = this + two before", 424,
              table(["Month", "Won revenue", "Prev month", "MoM change", "Rolling 3M"], win, shade=[best]))],
            [("Won revenue per month, one panel per manager", "Small multiples share one axis: the teams rise and fall together", 880,
              fig(traces, **lay, margin=dict(l=44, r=8, t=22, b=24))),
             ("Won revenue by quarter, four ways",
              "Columns come from the Time Intelligence calculation group; MoM % switches its own format string", 424,
              table(["Quarter", "Current", "QTD", "YTD", "MoM %"], ti, shade=[len(ti) - 1]))],
            [("Overview on a phone", "The mobile layout (mobile.json) reflows the Overview page into one column", 1320,
              {"html": phone()}, "full")],
        ]}


def phone():
    mg = rows(f"""select a.manager, sum(close_value) filter (where stage_key = 'closedwon') rev
                  from {F} join mart.dim_agent a using (owner_id) group by 1 order by rev desc""")
    m = rows(f"""select strftime(date_trunc('month', closed_date), '%b') p, count(*) filter (where stage_key = 'closedwon') w
                 from {F} where closed_date is not null group by date_trunc('month', closed_date), 1 order by date_trunc('month', closed_date)""")
    tiles = [("Won Revenue", usd(K["rev"])), ("Won Deals", num(K["won"])), ("Win Rate", f"{K['wr']:.1%}"),
             ("Avg Deal Size", f"${K['rev'] / K['won']:,.0f}"), ("Open Pipeline", usd(K["open_v"])),
             ("Stale Share", f"{K['stale_v'] / K['open_v']:.0%}")]
    top = max(m["w"])
    cols_ = "".join(f'<div><i style="height:{w / top * 100:.0f}%;background:{WON}"></i><span>{p}</span></div>' for p, w in zip(m["p"], m["w"]))
    headline = (f"Won {usd(K['rev'])} at a {K['wr']:.0%} win rate. {K['stale_n']:,} of {K['open_n']:,} open deals are "
                f"older than {STALE_DAYS} days.")
    return ('<div class="phone"><div class="screen"><h5>Sales pipeline</h5><div class="tiles">'
            + "".join(f'<div class="mini"><span>{k}</span><b>{v}</b></div>' for k, v in tiles)
            + f'</div><div class="mini"><span>In one sentence</span><p>{headline}</p></div>'
            + f'<div class="mini"><span>Won deals per month</span><div class="cols">{cols_}</div></div>'
            + f'<div class="mini"><span>Won revenue by manager</span>{css_bars(mg["manager"], mg["rev"], WON, usd)}</div>'
            + '</div></div>')


def page_drivers():
    closed = f"{F} f join mart.dim_agent a using (owner_id) join mart.dim_product p using (product_id) " \
             f"left join mart.dim_account c on c.account_id = f.account_id"
    # decomposition tree: each level splits the biggest node of the level before (what "High value" AI split picks)
    path, levels, where = [], [], "stage_key = 'closedwon'"
    for label, col in (("Region", "a.regional_office"), ("Manager", "a.manager"), ("Product", "p.product"),
                       ("Sector", "coalesce(c.sector, '(no account)')")):
        r = rows(f"select {col} k, sum(close_value) v from {closed} where {where} group by 1 order by v desc limit 6")
        levels.append((label, r["k"], r["v"]))
        path.append(r["k"][0])
        where += f" and {col} = '{r['k'][0]}'"
    tree = f'<div class="tree"><div class="lvl"><h6>Won Revenue</h6><div class="node on"><span>Total</span><b>{usd(K["rev"])}</b>' \
           f'<i style="width:100%;background:{WON}"></i></div></div>'
    for label, ks, vs in levels:
        tot = sum(vs)
        tree += f'<div class="lvl"><h6>{label}</h6>' + "".join(
            f'<div class="node{" on" if j == 0 else ""}"><span>{k}</span><b>{usd(val)}</b>'
            f'<i style="width:{val / vs[0] * 100:.0f}%;background:{WON if j == 0 else "#B9C2D0"}"></i></div>'
            for j, (k, val) in enumerate(zip(ks, vs))) + "</div>"
    tree += "</div>"
    # key influencers: P(won | segment) ÷ P(won | everything else), closed deals only
    inf = []
    for label, col in (("Product", "p.product"), ("Sector", "c.sector"), ("Region", "a.regional_office"),
                       ("Size Band", "c.size_band"), ("Cycle Band", "f.cycle_band")):
        r = rows(f"""select {col} k, count(*) n, count(*) filter (where stage_key = 'closedwon') w from {closed}
                     where stage_key like 'closed%' and {col} is not null group by 1""")
        N, W = sum(r["n"]), sum(r["w"])
        for k, n, w in zip(r["k"], r["n"], r["w"]):
            if n >= 100 and N - n:
                inf.append((label, k, w / n, (W - w) / (N - n), n))
    inf.sort(key=lambda t: -t[2] / t[3])
    top_inf = inf[:6]
    imax = max(a / b for _, _, a, b, _ in top_inf)
    ki = "".join(f'<div class="infl"><div><b>{l} is {k}</b><span>{a:.1%} won vs {b:.1%} elsewhere · {n:,} closed deals</span></div>'
                 f'<i style="width:{(a / b) / imax * 100:.0f}%"></i><em>{a / b:.2f}x</em></div>' for l, k, a, b, n in top_inf)
    low = min(inf, key=lambda t: t[2] / t[3])
    ki += (f'<p class="note" style="margin-top:8px">Lift = win rate in the segment ÷ win rate outside it. Every factor sits within '
           f'{max(abs(a / b - 1) for _, _, a, b, _ in inf):.0%} of 1.00x (weakest: {low[0]} {low[1]}, {low[2] / low[3]:.2f}x): '
           f'nothing in this data predicts a win strongly. Power BI fits its own model; these are plain ratios computed in Python.</p>')
    # many-to-many funnel bridge
    st = rows(f"""select step, step_sort, count(*) n, sum(o.list_price) val, avg(o.list_price) av
                  from mart.fact_funnel join {F} o using (opportunity_id) group by 1, 2 order by 2""")
    m2m = [[s, num(n), f"{n / st['n'][i - 1]:.1%}" if i else "—", usd(val), f"${av:,.0f}"]
           for i, (s, n, val, av) in enumerate(zip(st["step"], st["n"], st["val"], st["av"]))]
    # ISINSCOPE share of parent: region → manager
    sp = rows(f"""select a.regional_office r, a.manager m, sum(close_value) filter (where stage_key = 'closedwon') rev,
                         count(*) filter (where stage_key = 'closedwon') w, count(*) filter (where stage_key like 'closed%') c
                  from {F} join mart.dim_agent a using (owner_id) group by all""")
    body, shade = [], []
    for reg in sorted(set(sp["r"]), key=lambda r: -sum(v for x, v in zip(sp["r"], sp["rev"]) if x == r)):
        idx = [i for i, x in enumerate(sp["r"]) if x == reg]
        rv, w, c = (sum(sp[k][i] for i in idx) for k in ("rev", "w", "c"))
        shade.append(len(body))
        body.append([reg, usd(rv), f"{rv / K['rev']:.1%}", f"{w / c:.1%}"])
        for i in sorted(idx, key=lambda i: -sp["rev"][i]):
            body.append(["&nbsp;&nbsp;&nbsp;" + sp["m"][i], usd(sp["rev"][i]), f"{sp['rev'][i] / rv:.1%}", f"{sp['w'][i] / sp['c'][i]:.1%}"])
    body.append(["Total", usd(K["rev"]), "100.0%", f"{K['wr']:.1%}"])
    shade.append(len(body) - 1)
    return {
        "kpis": kpis([("Win Rate", f"{K['wr']:.1%}"), ("Closed Deals", num(K["won"] + K["lost"])), ("Won Revenue", usd(K["rev"])),
                      ("Avg Deal Size", f"${K['rev'] / K['won']:,.0f}")]),
        "rows": [
            [("Where the won revenue comes from",
              "Decomposition tree: each level splits the largest node of the one before (" + " → ".join(path) + ")", 880,
              {"html": tree}),
             ("The funnel as a many-to-many bridge", "Steps filters deals through Funnel (CROSSFILTER both); conversion uses OFFSET", 424,
              table(["Step", "Funnel deals", "Step conversion", "Value reaching", "Avg value"], m2m))],
            [("What makes a deal more likely to be won", "Key influencers on closed deals (won vs lost), top 6 by lift", 470,
              {"html": ki}),
             ("Where deals go", "Created → engaged → won, lost or still open (Sankey custom visual)", 380, sankey()),
             ("Share of parent: region → manager → agent",
              "ISINSCOPE picks the level: a manager row is a share of its region, a region row a share of the total", 438,
              table(["Region / manager", "Won revenue", "Share of parent", "Win rate"], body, shade=shade))],
        ]}


def sankey():
    """Twin of the Stage Flow calc table + Flow Deals measure: one link per From → To."""
    f = rows(f"""select count(*) n, count(*) filter (where engaged_date is not null) e,
                        count(*) filter (where stage_key = 'prospecting') p, count(*) filter (where stage_key = 'closedwon') w,
                        count(*) filter (where stage_key = 'closedlost') l, count(*) filter (where stage_key = 'engaging') g from {F}""")
    nodes = ["Created", "Engaged", "Still prospecting", "Won", "Lost", "Still engaging"]
    links = [(0, 1, f["e"][0]), (0, 2, f["p"][0]), (1, 3, f["w"][0]), (1, 4, f["l"][0]), (1, 5, f["g"][0])]
    return fig([dict(type="sankey", arrangement="snap",
                     node=dict(label=nodes, pad=14, thickness=14, color=[ACCENT, ACCENT, OPEN, WON, LOST, OPEN],
                               line=dict(width=0)),
                     link=dict(source=[a for a, _, _ in links], target=[b for _, b, _ in links],
                               value=[v for *_, v in links], color="rgba(74,111,214,0.18)",
                               hovertemplate="%{source.label} → %{target.label}: %{value:,}<extra></extra>"))],
               margin=dict(l=4, r=4, t=4, b=4))


def page_about():
    import base64
    png = ROOT / "powerbi" / "SalesPipeline.Report" / "StaticResources" / "RegisteredResources" / "dataflow.png"
    img = base64.b64encode(png.read_bytes()).decode()
    dd = rows(f"""select case lower(trim("Table")) when 'accounts' then 'Account' when 'products' then 'Product'
                        when 'sales_teams' then 'Agent' when 'sales_pipeline' then 'Opportunities' else '?' end m,
                        trim("Field") f, trim("Description") d
                 from read_csv_auto('{ROOT / "data" / "raw" / "data_dictionary.csv"}') order by 1, 2""")
    text = ('<h4>About this report</h4><p class="note">Data: Maven Analytics CRM Sales Opportunities (public domain), served '
            'through a mock CRM API. The sync history, rate limits and errors are synthetic: produced by the local API '
            'simulator.</p><p class="note">Stale = an engaging deal older than 104 days (P90 of engage-to-close). Weighted '
            'forecast = live pipeline × win probability × price realisation.</p>'
            f'<img alt="Data flow: CRM API, DuckDB, Parquet, Power BI" src="data:image/png;base64,{img}" '
            'style="width:100%;margin-top:12px;border:1px solid #E6E4DD;border-radius:8px">')
    return {"kpis": [], "rows": [[("", "", 640, {"html": text}, "full"),
                                  ("Data dictionary", "Loaded by Power Query from data_dictionary.csv (trimmed, mapped to model tables)",
                                   664, table(["Model table", "Source field", "Description"],
                                              [list(r) for r in zip(dd["m"], dd["f"], dd["d"])], left=(0, 1, 2)), "full")]]}


def page_account_value():
    """Twin of Account Cumulative Share / Account ABC / Accounts to 80% and the Account Segment bands (no slicers)."""
    r = rows(f"""select c.account, sum(close_value) v from {F} f join mart.dim_account c using (account_id)
                 where stage_key = 'closedwon' group by 1 order by v desc, 1""")
    tot, cum, body, shares = sum(r["v"]), 0, [], []
    for a, v in zip(r["account"], r["v"]):
        cum += v
        sh = cum / tot
        shares.append(sh)
        cls = "A" if sh <= 0.8 else "B" if sh <= 0.95 else "C"
        body.append([a, usd(v), f'<span class="db"><i style="width:{sh * 50:.0f}px;background:{WON}"></i>{sh:.1%}</span>',
                     {"A": "● A", "B": "◐ B", "C": "○ C"}[cls]])
    n80 = sum(sh <= 0.8 for sh in shares)
    top10 = sum(r["v"][:10])
    bands = [("Under $50k", 0, 5e4), ("$50k–100k", 5e4, 1e5), ("$100k–150k", 1e5, 1.5e5), ("$150k+", 1.5e5, 1e12)]
    seg = [(b, sum(lo <= v < hi for v in r["v"]), sum(v for v in r["v"] if lo <= v < hi)) for b, lo, hi in bands]
    x = list(range(len(r["v"])))
    pareto = fig([dict(type="bar", name="Won Revenue", x=x, y=r["v"], marker=dict(color=WON), customdata=r["account"],
                       hovertemplate="%{customdata}: %{y:$,.0f}<extra></extra>"),
                  dict(type="scatter", mode="lines", name="Account Cumulative Share", x=x, y=shares, yaxis="y2",
                       line=dict(color=STALE, width=2), hovertemplate="%{y:.1%}<extra></extra>")],
                 showlegend=True, legend=LEGEND, xaxis=dict(showticklabels=False, title=dict(text=f"{len(x)} accounts, largest first")),
                 yaxis2=dict(overlaying="y", side="right", range=[0, 1.02], tickformat=".0%", showgrid=False,
                             tickfont=dict(color=MUTED, size=10)),
                 shapes=[dict(type="line", xref="paper", x0=0, x1=1, yref="y2", y0=0.8, y1=0.8, line=dict(color=MUTED, dash="dot", width=1))],
                 margin=dict(l=52, r=44, t=8, b=32))
    return {
        "kpis": kpis([("Won Revenue", usd(tot)), ("Active Accounts", num(len(x))), ("Accounts to 80%", num(n80)),
                      ("Share of Accounts to 80%", f"{n80 / len(x):.0%}"), ("Top 10 Accounts Revenue", usd(top10))]),
        "rows": [
            [("Pareto: won revenue per account and cumulative share",
              f"Largest account first; {n80} of {len(x)} accounts make 80% — revenue is spread, not concentrated", 880, pareto),
             ("ABC class per account", "A = first 80% of revenue, B = next 15%, C = rest", 424,
              table(["Account", "Won revenue", "Cumulative share", "ABC"], body))],
            [("Accounts per revenue band", "Disconnected Account Segment table: the band is decided at query time", 640,
              vbars([b for b, *_ in seg], [n for _, n, _ in seg], WON, [num(n) for _, n, _ in seg], yaxis=NUM_Y)),
             ("Revenue bands", "Dynamic segmentation: same bands, recounted for whatever is selected", 664,
              table(["Segment", "Accounts in segment", "Segment revenue"], [[b, num(n), usd(v)] for b, n, v in seg]))],
        ]}


def bridge(q_prev, q_cur):
    """Volume / mix / price per product, same formulas as the DAX measures (Q = won deals, P = avg deal size)."""
    r = rows(f"""select p.product, quarter(closed_date) q, count(*) n, sum(close_value) v from {F} join mart.dim_product p using (product_id)
                 where stage_key = 'closedwon' and year(closed_date) = 2017 and quarter(closed_date) in ({q_prev}, {q_cur})
                 group by all""")
    d = {(pr, q): (n, v) for pr, q, n, v in zip(r["product"], r["q"], r["n"], r["v"])}
    prods = sorted({pr for pr, _ in d}, key=lambda pr: -(d.get((pr, q_cur), (0, 0))[1] - d.get((pr, q_prev), (0, 0))[1]))
    Qc = sum(d.get((pr, q_cur), (0, 0))[0] for pr in prods)
    Qp = sum(d.get((pr, q_prev), (0, 0))[0] for pr in prods)
    out = []
    for pr in prods:
        nc, vc = d.get((pr, q_cur), (0, 0))
        np_, vp = d.get((pr, q_prev), (0, 0))
        pc, pp = (vc / nc if nc else 0), (vp / np_ if np_ else 0)
        vol, mix, price = (Qc - Qp) * np_ / Qp * pp, Qc * (nc / Qc - np_ / Qp) * pp, nc * (pc - pp)
        out.append((pr, vp, vc, vol, mix, price, vc - vp - vol - mix - price))
    return out


def page_diagnosis():
    m = rows(WON_M)
    x, v = [d.isoformat() for d in m["m"]], m["v"]
    mom = [None] + [(v[i] - v[i - 1]) / v[i - 1] for i in range(1, len(v))]
    b = bridge(3, 4)
    tot = [sum(t[i] for t in b) for i in range(1, 7)]
    assert abs(tot[5]) < 0.01, "bridge does not close"
    wf = fig([dict(type="waterfall", x=[t[0] for t in b] + ["Total"], y=[t[2] - t[1] for t in b] + [None],
                   measure=["relative"] * len(b) + ["total"], text=[usd(t[2] - t[1], True) for t in b] + [usd(tot[1] - tot[0], True)],
                   textposition="outside", cliponaxis=False, increasing=dict(marker=dict(color=WON)),
                   decreasing=dict(marker=dict(color=STALE)), totals=dict(marker=dict(color=INK2)),
                   connector=dict(line=dict(color="#D9D7CF")))],
             xaxis=dict(tickangle=-30, tickfont=dict(size=9, color=MUTED)), margin=dict(l=52, r=12, t=16, b=60))
    # decomposition (Oct vs Sep by region, then manager inside the region that fell most) — what the tree's first two levels show
    dm = rows(f"""select a.regional_office r, a.manager mg,
                         sum(close_value) filter (where month(closed_date) = 10) - sum(close_value) filter (where month(closed_date) = 9) d
                  from {F} join mart.dim_agent a using (owner_id) where stage_key = 'closedwon' and year(closed_date) = 2017
                  group by rollup (1, 2) having r is not null order by d""")
    reg = [(r, d) for r, mg, d in zip(dm["r"], dm["mg"], dm["d"]) if mg is None]
    worst = reg[0][0]
    mgr = [(mg, d) for r, mg, d in zip(dm["r"], dm["mg"], dm["d"]) if r == worst and mg is not None]
    labels = [f"Region · {r}" for r, _ in reg] + [f"{worst} · {mg}" for mg, _ in mgr]
    vals = [d for _, d in reg] + [d for _, d in mgr]
    # transition matrix at the Oct month-end snapshot
    t = rows("""select from_stage f, from_sort fs, to_stage t, to_sort ts, count(*) n from mart.fact_stage_transition
                where snapshot_date = date '2017-10-31' group by all""")
    froms = sorted(set(zip(t["fs"], t["f"])))
    tos = sorted(set(zip(t["ts"], t["t"])))
    cnt = {(f, to): n for f, to, n in zip(t["f"], t["t"], t["n"])}
    tm = [[f] + [num(cnt.get((f, to), 0)) for _, to in tos] for _, f in froms]
    # agent stale z-score (population SD over agents that hold open pipeline)
    z = rows(f"""with s as (select a.agent, sum(open_value) filter (where stage_key = 'engaging' and age_days > {STALE_DAYS}) / sum(open_value) sh
                            from {F} join mart.dim_agent a using (owner_id) group by 1 having sum(open_value) > 0)
                 select agent, coalesce(sh, 0) sh, (coalesce(sh, 0) - avg(coalesce(sh, 0)) over ()) / stddev_pop(coalesce(sh, 0)) over () z
                 from s order by z desc limit 8""")
    zt = [[a, databar(sh), {"v": f"{zz:+.2f}", "t": "bad" if zz > 2 else ""}] for a, sh, zz in zip(z["agent"], z["sh"], z["z"])]
    return {
        "kpis": kpis([("Won Revenue PQ (Q3)", usd(tot[0])), ("Won Revenue (Q4)", usd(tot[1])),
                      ("Revenue Change QoQ", usd(tot[1] - tot[0], True), tone(tot[1] - tot[0])),
                      ("Volume Effect", usd(tot[2], True), tone(tot[2])), ("Mix Effect", usd(tot[3], True), tone(tot[3])),
                      ("Price Effect", usd(tot[4], True), tone(tot[4]))]),
        "rows": [
            [("1 · Won revenue and month-on-month change", "Oct 2017 falls 41% after September; Nov and Dec recover", 520,
              fig([dict(type="bar", name="Won Revenue", x=x, y=v, marker=dict(color=WON, cornerradius=3),
                        hovertemplate="%{x|%b %Y}: %{y:$,.0f}<extra></extra>"),
                   dict(type="scatter", mode="lines+markers", name="MoM Change", x=x, y=mom, yaxis="y2",
                        line=dict(color=STALE, width=2), hovertemplate="%{x|%b %Y}: %{y:+.1%}<extra></extra>")],
                  showlegend=True, legend=LEGEND, xaxis=dict(tickformat="%b", dtick="M1"),
                  yaxis2=dict(overlaying="y", side="right", tickformat="+.0%", showgrid=False, zeroline=True,
                              zerolinecolor="#D9D7CF", tickfont=dict(color=MUTED, size=10)),
                  margin=dict(l=52, r=44, t=8, b=32))),
             ("2 · Q4 vs Q3 change, by product", "Waterfall: which products took the quarter down", 400, wf),
             ("2 · Volume, mix, price per product", "Q4 vs Q3. Price effect near zero = prices held; Bridge Check = 0", 368,
              table(["Product", "Volume", "Mix", "Price", "Check"],
                    [[pr, cell(usd(vo, True), vo), cell(usd(mi, True), mi), cell(usd(pz, True), pz), usd(ck)]
                     for pr, _, _, vo, mi, pz, ck in b]
                    + [["Total", cell(usd(tot[2], True), tot[2]), cell(usd(tot[3], True), tot[3]),
                        cell(usd(tot[4], True), tot[4]), usd(tot[5])]], shade=[len(b)]))],
            [("3 · Where the October drop sits", f"Revenue Change MoM, Oct vs Sep: by region, then managers in {worst}", 504,
              hbars(labels, vals, [STALE if val < 0 else WON for val in vals], left=190)),
             ("3 · Stage at end of Sep → end of Oct", "Transition matrix from the month-end snapshots", 400,
              table(["From → To"] + [to for _, to in tos], tm)),
             ("4 · Agents whose open book is stale", "Z-score above 2 = outlier; top 8. In Power BI: select a row → Open deals (drillthrough)", 384,
              table(["Agent", "Stale share", "Z-score"], zt))],
        ]}


BUILD = {"overview": page_overview, "funnel": page_funnel, "pipeline": page_pipeline, "team": page_team,
         "products": page_products, "sync": page_sync,
         "trends": page_trends, "drivers": page_drivers, "about": page_about,
         "account_value": page_account_value, "diagnosis": page_diagnosis}

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sales Pipeline — CRM to Power BI</title>
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
.up{color:#1E7B34!important;font-weight:600}.dn{color:#B42318!important;font-weight:600}
.db{display:inline-flex;align-items:center;gap:6px;justify-content:flex-end;min-width:90px}.db i{display:inline-block;height:10px;border-radius:2px;max-width:50px}
.tog{border:1px solid var(--hair);border-radius:8px;padding:5px 14px;font-size:12px;font-weight:600;color:var(--ink2);background:#fff}
.tog.on{background:var(--accent);border-color:var(--accent);color:#fff}
.info{display:inline-grid;place-items:center;width:16px;height:16px;border:1.5px solid var(--ink2);border-radius:50%%;font-size:10px;font-style:italic;color:var(--ink2);margin-left:4px}
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
td{text-align:right;padding:7px 10px;border-bottom:1px solid #ECEBE6}
th:first-child,td:first-child{font-weight:600}td.l,th.l{text-align:left}
tr.shade td{background:#EAEEF7;font-weight:600}
.headline{font-size:17px;line-height:1.45;margin:6px 0 14px}
.split{display:flex;gap:16px;align-items:stretch;margin-top:auto}.split>*{flex:1}
.mini{border:1px solid var(--hair);border-radius:10px;padding:10px 12px;margin-bottom:8px}
.mini span{display:block;font-size:11px;color:var(--muted)}.mini b{font-size:20px;font-weight:600}
.steps{display:flex;flex-wrap:wrap;gap:4px;margin:10px 0 14px}.steps span{border:1px solid var(--hair);border-radius:6px;padding:3px 7px;font-size:11px;color:var(--ink2)}
.steps span.on{background:var(--side);color:#fff;border-color:var(--side)}
h4{margin:0 0 4px;font-size:12px}.note{font-size:12px;color:var(--ink2);line-height:1.5;margin:0}
.hb{display:flex;align-items:center;gap:6px;font-size:11px;margin:3px 0}.hb span{flex:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink2)}
.hb i{height:9px;border-radius:3px}.hb em{font-style:normal;color:var(--muted);font-variant-numeric:tabular-nums}
.phone{margin:4px auto;width:340px;height:700px;border:10px solid #23262B;border-radius:38px;background:#23262B;box-shadow:0 8px 24px rgba(0,0,0,.18)}
.phone .screen{height:100%%;overflow:auto;background:var(--plane);border-radius:28px;padding:18px 12px}
.phone h5{margin:0 0 8px;font-size:17px}.phone .tiles{display:grid;grid-template-columns:1fr 1fr;gap:0 8px}
.phone .mini{background:var(--surface)}.phone .mini b{font-size:17px}.phone .mini p{margin:4px 0 0;font-size:13px;line-height:1.4}
.phone .cols{display:flex;align-items:flex-end;gap:3px;height:90px;margin-top:6px}.phone .cols div{flex:1;height:100%%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center}
.phone .cols i{width:100%%;border-radius:2px 2px 0 0}.phone .cols span{font-size:9px;color:var(--muted)}
.tree{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;height:100%%}
.lvl h6{margin:0 0 6px;font-size:11px;color:var(--muted);font-weight:600;border-bottom:1px solid var(--hair);padding-bottom:4px}
.node{position:relative;border:1px solid var(--hair);border-radius:8px;padding:5px 8px 9px;margin-bottom:6px;font-size:11px;background:#fff}
.node span{display:block;color:var(--ink2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.node b{font-size:13px}
.node i{position:absolute;left:8px;bottom:4px;height:3px;border-radius:2px;max-width:calc(100%% - 16px)}
.node.on{border-color:var(--accent);box-shadow:inset 3px 0 0 var(--accent)}
.lvl+.lvl{position:relative}.lvl+.lvl:before{content:"›";position:absolute;left:-10px;top:26px;color:var(--muted)}
.infl{display:grid;grid-template-columns:1fr 150px 48px;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid #ECEBE6;font-size:12px}
.infl span{display:block;color:var(--muted);font-size:11px}.infl i{height:10px;border-radius:3px;background:var(--accent)}
.infl em{font-style:normal;font-weight:600;text-align:right;font-variant-numeric:tabular-nums}
@media (max-width:1100px){.app{grid-template-columns:1fr}aside{position:static;height:auto}.row{flex-direction:column}.panel{width:100%%!important}.kpis{grid-auto-flow:row;grid-template-columns:repeat(2,1fr)}}
</style></head>
<body><div class="app">
<aside><div class="brand"><small>MAVEN TECH</small><b>Sales Pipeline</b></div><nav id="nav"></nav>
<footer>Source: Maven Analytics CRM Sales Opportunities (public domain)<br>Pipeline: CRM API · DuckDB SQL · Power BI (TMDL + PBIR)<br>Static preview of the Power BI report</footer></aside>
<main id="main"></main></div>
<script>%(plotly)s</script>
<script>
const PAGES = %(pages)s;
const CHIPS = [['Year','All'],['Region','All'],['Product','All'],['Date Basis','Closed']];
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
  history.replaceState(null, '', '#' + id);
}
PAGES.forEach(p => { const b = el('button', {}, p.name); b.dataset.id = p.id; b.onclick = () => render(p.id); nav.append(b); });
addEventListener('hashchange', () => render(location.hash.slice(1)));
render((location.hash || '#overview').slice(1));
</script></body></html>"""


def main():
    pages = []
    for pid, name, title, subtitle in PAGES:
        pages.append({"id": pid, "name": name, "title": title, "subtitle": subtitle, **BUILD[pid]()})
        print(f"  ✓ {name}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(HTML % {"plotly": get_plotlyjs(), "pages": json.dumps(pages, default=str),
                           "ink": INK, "ink2": INK2, "muted": MUTED, "side": SIDE, "side_text": SIDE_TEXT,
                           "side_line": SIDE_LINE, "good": ACCENT, "bad": STALE, "accent": ACCENT})
    print(f"  ✓ wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
