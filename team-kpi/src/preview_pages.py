"""Page builders for the HTML twin (build_preview.py): the report's default state, computed in DuckDB
with the same rules as the DAX. All slicers on All, SLA Scale 100%, Analyze By = Team.

Rules mirrored from build_model.py:
- Created counts by created date; Resolved / Delivered by resolved date (the inactive relationship).
- Workload measures are as of 30 Sep 2026 whatever the date filter (REMOVEFILTERS on Date).
- Status: on track from 90%, at risk from 75%, off track below that (UDF Kpi.Status).
- Response SLA counts a ticket once it has a response or its target has passed while waiting.
- The calc group matrix is filtered to W39 2026 (21–27 Sep): MTD / QTD / YTD run up to 27 Sep,
  Prior Year = 21–27 Sep 2025, Previous Week = W38, Last 4 Weeks = the 28 days to 27 Sep.
"""
from datetime import timedelta
from pathlib import Path

import duckdb

from build_report import ACCENT, BEHIND_C, INK, INK2, MUTED, ON_C, RISK_C, TARGET_C

ROOT = Path(__file__).resolve().parents[1]
MARTS = ROOT / "data" / "marts"
SCALE = 1.0
con = duckdb.connect()
for t in ("dim_date", "dim_team", "dim_person", "dim_priority", "dim_type", "dim_stage", "fact_ticket",
          "fact_stage_time", "fact_stage_transition", "fact_backlog_daily", "status_map", "data_checks", "dim_component", "bridge_ticket_component"):
    con.execute(f"create view {t} as select * from '{MARTS / t}.parquet'")
# The Power Query steps in Tickets.pq, mirrored: title-case status, trimmed summary, age band, days past target.
con.execute("""create view t as select f.* replace (
                          array_to_string(list_transform(string_split(f.status, ' '), w -> upper(w[1]) || lower(w[2:])), ' ') as status,
                          trim(regexp_replace(f.summary, '[[:cntrl:]]', '', 'g')) as summary),
                      tm.team, p.priority, p.response_hours target_h, p.resolution_days target_d,
                      pe.person, pe.home_team, f.age_days - p.resolution_days days_past_target,
                      case when f.age_days is null then null when f.age_days <= 30 then '0–30 days'
                           when f.age_days <= 90 then '31–90 days' when f.age_days <= 180 then '91–180 days'
                           else 'Over 180 days' end age_band,
                      'https://issues.apache.org/jira/browse/' || f.ticket_key jira_url,
                      -- the DAX calculated column Lead Time Band (and its sort order)
                      case when f.lead_days is null then null when f.lead_days <= 1 then 'Up to 1 day'
                           when f.lead_days <= 7 then '2–7 days' when f.lead_days <= 30 then '8–30 days'
                           when f.lead_days <= 90 then '31–90 days' else 'Over 90 days' end lead_band,
                      case when f.lead_days is null then null when f.lead_days <= 1 then 1 when f.lead_days <= 7 then 2
                           when f.lead_days <= 30 then 3 when f.lead_days <= 90 then 4 else 5 end lead_band_order
               from fact_ticket f join dim_team tm using (team_key) join dim_priority p using (priority_key)
               join dim_person pe using (person_key)""")
LAST = con.execute("select max(date) from dim_date").fetchone()[0]
WEEKS = con.execute("select count(distinct year_week) from dim_date").fetchone()[0]


def rows(sql):
    cur = con.execute(sql)
    names, data = [d[0] for d in cur.description], cur.fetchall()
    return {n: [r[i] for r in data] for i, n in enumerate(names)}


def one(sql):
    return con.execute(sql).fetchone()


# ------------------------------------------------------------------ formats
pct = lambda v, d=0, signed=False: "—" if v is None else (f"{v:+.{d}%}" if signed else f"{v:.{d}%}").replace("-", "−")
num = lambda v, d=0: "—" if v is None else f"{v:,.{d}f}"
signed = lambda v: "—" if v is None else f"{v:+,.0f}".replace("-", "−")
div = lambda a, b: None if not b or a is None else a / b
status = lambda a: None if a is None else "On track" if a >= 0.9 else "At risk" if a >= 0.75 else "Off track"
COLOR = {"On track": ON_C, "At risk": RISK_C, "Off track": BEHIND_C, None: "#8C959F"}
TONE = {"On track": "good", "At risk": "risk", "Off track": "bad", None: ""}
scolor = lambda a: COLOR[status(a)]
cell = lambda text, a: {"v": text, "t": TONE[status(a)]}
STAGE_C = {"Backlog": TARGET_C, "In Progress": ACCENT, "In Review": "#86b6ef"}

AXIS = dict(showgrid=False, zeroline=False, linecolor="#E6E4DD", tickfont=dict(color=MUTED, size=10), title=None)
YAXIS = dict(showgrid=True, gridcolor="#ECEBE6", zeroline=True, zerolinecolor="#D9D7CF", tickfont=dict(color=MUTED, size=10),
             title=None, tickformat="~s")
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
    return fig([t], yaxis=dict(autorange="reversed", showgrid=False, zeroline=False, tickformat="",
                               tickfont=dict(color=INK2, size=10)),
               xaxis=dict(visible=False, zeroline=True, range=[min(0, *vals) * 1.4, max(0, *vals) * 1.25]),
               margin=dict(l=left, r=16, t=4, b=8))


def vbars(labels, values, colors, text, **layout):
    t = dict(type="bar", x=labels, y=values, marker=dict(color=colors, cornerradius=3), text=text,
             textposition="outside", cliponaxis=False, textfont=dict(size=10, color=INK2),
             hovertemplate="%{x}: %{text}<extra></extra>")
    return fig([t], **layout)


def lines(x, series, fmt="%b %Y", dtick="M3"):
    return fig([dict(type="scatter", mode="lines", name=n, x=x, y=y, line=dict(color=c, width=2.2, dash=d),
                     hovertemplate=f"{n} · %{{x|{fmt}}}: %{{y:,.0f}}<extra></extra>") for n, y, c, d in series],
               showlegend=True, legend=LEGEND, xaxis=dict(tickformat="%b %Y", dtick=dtick))


def table(columns, body, left=(0,)):
    return {"columns": columns, "rows": body, "shade": [], "left": list(left)}


def kpis(items):
    return [{"label": k, "value": v, "t": t} for k, v, *t in [(*i, None) if len(i) == 2 else i for i in items]]


NO_Y = dict(visible=False)

# ------------------------------------------------------------------ measures
DELIVERED = "outcome = 'Delivered'"
RES_MET = f"lead_days <= target_d * {SCALE}"
RESP_MET = f"response_hours is not null and wait_hours <= target_h * {SCALE}"
RESP_DUE = f"response_hours is not null or wait_hours > target_h * {SCALE}"
OPEN = "resolved_at is null"
BREACHED = f"{OPEN} and age_days > target_d * {SCALE}"

CREATED, RESOLVED, DELIV, LEAD, OPEN_N, RES_SLA, BREACH = one(f"""
    select count(*), count(resolved_at), count(*) filter (where {DELIVERED}),
           median(lead_days) filter (where {DELIVERED}), count(*) filter (where {OPEN}),
           count(*) filter (where resolved_at is not null and {RES_MET}) / count(resolved_at),
           count(*) filter (where {BREACHED}) from t""")


def page_overview():
    m = rows("""select d.month_start m, c.n created, r.n resolved from
                  (select distinct date_trunc('month', date)::date month_start from dim_date) d
                  left join (select date_trunc('month', created_date)::date m, count(*) n from t group by 1) c on c.m = d.month_start
                  left join (select date_trunc('month', resolved_date)::date m, count(*) n from t where resolved_at is not null
                             group by 1) r on r.m = d.month_start order by 1""")
    cum = lambda xs: [sum(v or 0 for v in xs[:i + 1]) for i in range(len(xs))]
    tm = rows(f"""select team, count(*) filter (where {DELIVERED}) d,
                         count(*) filter (where resolved_at is not null and {RES_MET}) / count(resolved_at) s
                  from t group by 1 order by d desc""")
    so = sorted(range(len(tm["s"])), key=lambda i: -tm["s"][i])
    net = CREATED - RESOLVED
    headline = (f"{num(CREATED)} tickets in, {num(RESOLVED)} out: the backlog "
                f"{'grew by ' + num(net) if net > 0 else 'shrank by ' + num(-net)}. {pct(RES_SLA)} were resolved "
                f"within SLA; median lead time {num(LEAD, 1)} days. {num(BREACH)} open tickets are past their target.")
    return {
        "kpis": kpis([("Created", num(CREATED)), ("Resolved", num(RESOLVED)),
                      ("Net Flow", signed(net), "bad" if net > 0 else "good"), ("Median Lead Time", f"{num(LEAD, 1)} d"),
                      ("Resolution SLA %", pct(RES_SLA), TONE[status(RES_SLA)]), ("Open Tickets", num(OPEN_N))]),
        "rows": [
            [("Tickets in vs tickets out, cumulative",
              "Visual calculations: RUNNINGSUM over the months on the axis. The gap is backlog growth", 880,
              lines(m["m"], [("Cumulative created", cum(m["created"]), TARGET_C, "dash"),
                             ("Cumulative resolved", cum(m["resolved"]), ACCENT, "solid")])),
             ("", "", 424, {"stack": [
                 ("In one sentence", "Follows the slicers", 220, {"html": f'<div class="headline">{headline}</div>'}),
                 ("What-if: scale every SLA target", "100% = the policy; 150% = half as strict again", 96,
                  {"html": '<div class="steps">' + "".join(f'<span class="{"on" if s == 100 else ""}">{s}%</span>'
                                                           for s in range(50, 201, 25)) + "</div>"})]})],
            [("Tickets delivered, by the dimension picked above",
              "Analyze by: Team (also Priority / Type / Engineer / Stage)", 652,
              hbars(tm["team"], tm["d"], ACCENT, [num(v) for v in tm["d"]], left=90)),
             ("Resolved within SLA, by the same dimension", "Green ≥ 90% · amber 75–89% · red below 75%", 652,
              hbars([tm["team"][i] for i in so], [tm["s"][i] for i in so], [scolor(tm["s"][i]) for i in so],
                    [pct(tm["s"][i]) for i in so], left=90))],
        ]}


def page_flow():
    p85, cyc, cov = one(f"""select quantile_cont(lead_days, 0.85) filter (where {DELIVERED}),
                               median(cycle_days) filter (where {DELIVERED} and started_at is not null),
                               count(started_at) filter (where {DELIVERED}) / count(*) filter (where {DELIVERED}) from t""")
    st = rows("""select s.stage, sum(days) d from fact_stage_time f join dim_stage s using (stage_key)
                 group by s.stage, s.stage_key order by s.stage_key""")
    eff = div(sum(d for s, d in zip(st["stage"], st["d"]) if s != "Backlog"), sum(st["d"]))
    w = rows(f"""select d.week_start w, coalesce(n, 0) n from (select distinct week_start from dim_date) d
                 left join (select dd.week_start, count(*) n from t join dim_date dd on dd.date = t.resolved_date
                            where {DELIVERED} group by 1) x using (week_start) order by 1""")
    ma = [sum(w["n"][max(0, i - 3):i + 1]) / len(w["n"][max(0, i - 3):i + 1]) for i in range(len(w["n"]))]
    # calc group items with the matrix's filter: the last full ISO week, W39 2026 (21-27 Sep)
    we = LAST - timedelta(days=3)
    ws = we - timedelta(days=6)
    rng = lambda a, b: f"resolved_date between '{a}' and '{b}'"
    periods = {"Current": rng(ws, we), "Previous Week": rng(ws - timedelta(days=7), we - timedelta(days=7)),
               "Last 4 Weeks": rng(we - timedelta(days=27), we),
               "MTD": rng(we.replace(day=1), we),
               "QTD": rng(we.replace(month=(we.month - 1) // 3 * 3 + 1, day=1), we),
               "YTD": rng(we.replace(month=1, day=1), we),
               "Prior Year": rng(ws.replace(year=ws.year - 1), we.replace(year=we.year - 1))}
    cols = ", ".join(f"count(*) filter (where {c}) \"{k}\"" for k, c in periods.items())
    mx = rows(f"select team, {cols} from t where {DELIVERED} group by 1 order by 2 desc")
    body = [[mx["team"][i], *[num(mx[k][i]) for k in periods],
             pct(div(mx["Current"][i] - mx["Prior Year"][i], mx["Prior Year"][i]), 1, True)] for i in range(len(mx["team"]))]
    lb = rows(f"select lead_band, count(*) n from t where {DELIVERED} group by 1, lead_band_order order by lead_band_order")
    sc = rows(f"select ticket_key, team, resolved_date, lead_days from t where {DELIVERED} order by resolved_date")
    p50 = one(f"select quantile_cont(lead_days, 0.5) from t where {DELIVERED}")[0]
    team_c = dict(zip(sorted(set(sc["team"])), [ACCENT, "#86b6ef", RISK_C, ON_C]))
    scatter = fig([dict(type="scattergl", mode="markers", name=tm,
                        x=[d for d, k in zip(sc["resolved_date"], sc["team"]) if k == tm],
                        y=[max(v, 0.01) for v, k in zip(sc["lead_days"], sc["team"]) if k == tm],
                        marker=dict(color=c, size=4, opacity=0.55), hovertemplate="%{y:,.1f} d<extra></extra>")
                   for tm, c in team_c.items()],
                  showlegend=True, legend=LEGEND, yaxis=dict(type="log", tickvals=[0.1, 1, 10, 100, 1000], ticktext=["0.1", "1", "10", "100", "1k"]),
                  xaxis=dict(tickformat="%b %y"),
                  shapes=[dict(type="line", xref="paper", x0=0, x1=1, y0=v, y1=v, line=dict(color=INK2, dash="dash", width=1))
                          for v in (p50, p85)],
                  annotations=[dict(xref="paper", x=0, y=__import__("math").log10(v), text=f"P{q} = {num(v)} d",
                                    showarrow=False, xanchor="left", yanchor="bottom", font=dict(size=9, color=INK2))
                               for q, v in ((50, p50), (85, p85))])
    return {
        "kpis": kpis([("Delivered", num(DELIV)), ("Weekly Throughput", num(DELIV / WEEKS, 1)),
                      ("Median Lead Time", f"{num(LEAD, 1)} d"), ("Lead Time P85", f"{num(p85, 1)} d"),
                      ("Median Cycle Time", f"{num(cyc, 1)} d"), ("Flow Efficiency", pct(eff))]),
        "rows": [
            [("Delivered per week and the 4-week moving average",
              "MOVINGAVERAGE visual calculation over the Weeks hierarchy; drill up to ISO year", 432,
              lines(w["w"], [("Delivered", w["n"], TARGET_C, "solid"), ("4-week average", ma, ACCENT, "solid")],
                    fmt="week of %d %b %Y", dtick="M6")),
             ("Lead time scatter: one dot per delivered ticket",
              "Deneb (Vega-Lite) custom visual; dashed lines = P50 and P85 (log scale here)", 432, scatter),
             ("Where the time goes", "Ticket-days spent in each open stage. Flow efficiency = in progress + in review ÷ all",
              424, vbars(st["stage"], st["d"], [STAGE_C[s] for s in st["stage"]], [num(v) for v in st["d"]], yaxis=NO_Y))],
            [("Delivered, eight ways: week of 21 Sep 2026 (the last full ISO week)",
              "Columns come from the Time Intelligence calculation group; previous week runs on the ISO-week calendar, "
              "YoY % switches its own format string", 880,
              table(["Team", *periods, "YoY %"], body)),
             ("Delivered tickets by lead time",
              "Lead Time Band is a DAX calculated column: fixed per ticket, so it can sit on an axis", 424,
              vbars(lb["lead_band"], lb["n"], ACCENT, [num(v) for v in lb["n"]], yaxis=NO_Y))],
        ]}


def page_sla():
    resp, med_resp = one(f"""select count(*) filter (where {RESP_MET}) / count(*) filter (where {RESP_DUE}),
                                    median(response_hours) from t""")
    pr = rows(f"""select priority, count(*) filter (where resolved_at is not null and {RES_MET}) / count(resolved_at) s
                  from t group by 1 order by 1""")
    mx = rows(f"""pivot (select team, priority, (count(*) filter (where resolved_at is not null and {RES_MET})
                         / nullif(count(resolved_at), 0)) s from t group by all)
                  on priority using any_value(s) group by team order by team""")
    prios = [c for c in mx if c != "team"]
    body = [[mx["team"][i], *[cell(f"{pct(mx[p][i])} · {status(mx[p][i])}", mx[p][i]) for p in prios]]
            for i in range(len(mx["team"]))]
    tm = rows(f"""select team, count(*) filter (where resolved_at is not null and {RES_MET}) / count(resolved_at) s
                  from t group by 1 order by 2 desc""")
    gauge = {"data": [dict(type="indicator", mode="gauge+number", value=RES_SLA * 100, number=dict(suffix="%", valueformat=".0f", font=dict(size=26)),
                           gauge=dict(axis=dict(range=[0, 100], tickfont=dict(size=9, color=MUTED)), bar=dict(color=ACCENT),
                                      threshold=dict(line=dict(color=INK, width=3), value=90)))],
             "layout": dict(margin=dict(l=18, r=18, t=20, b=8), paper_bgcolor="rgba(0,0,0,0)")}
    p1 = one(f"select count(*) filter (where {RES_MET}) / count(*) from t where resolved_at is not null and priority_key = 1")[0]
    br = rows(f"""select ticket_key, team, priority, status, age_days, days_past_target, jira_url, summary
                  from t where {BREACHED} order by age_days desc limit 200""")
    return {
        "kpis": kpis([("Response SLA %", pct(resp), TONE[status(resp)]),
                      ("Resolution SLA %", pct(RES_SLA), TONE[status(RES_SLA)]),
                      ("Median First Response", f"{num(med_resp, 1)} h"),
                      ("P1 Resolution SLA %", pct(p1), TONE[status(p1)]), ("Open Breached", num(BREACH), "bad"),
                      ("SLA Scale", pct(SCALE))]),
        "rows": [
            [("Resolved within SLA, by team; drill down to priority",
              "Targets: P1 7 days · P2 30 · P3 90 · P4 180 (an illustrative policy, see README)", 400,
              hbars(tm["team"], tm["s"], [scolor(v) for v in tm["s"]], [pct(v) for v in tm["s"]], left=90)),
             ("Resolution SLA vs target", "Needle: the SLA; line: the 90% target", 224, gauge),
             ("", "", 664, {"stack": [
                 ("Team × priority", "Cell colour follows the on track / at risk / off track rule; the status is also "
                  "written out", 220,
                  table(["Team", *prios], body)),
                 ("What-if: scale every SLA target", "Every SLA number on every page recomputes", 96,
                  {"html": '<div class="steps">' + "".join(f'<span class="{"on" if s == 100 else ""}">{s}%</span>'
                                                           for s in range(50, 201, 25)) + "</div>"})]})],
            [("Open tickets already past their resolution target",
              f"Oldest first ({num(BREACH)} in all). Stakeholders don't see the summary column (object-level security "
              "on Ticket Text)", 1320,
              table(["Ticket", "Team", "Priority", "Status", "Age (days)", "Days past target", "Jira URL", "Summary"],
                    [[k, tm, p, s, num(a, 0), num(dp, 0), u, sm[:80]] for k, tm, p, s, a, dp, u, sm in zip(*br.values())],
                    left=(0, 1, 2, 3, 6, 7)))],
        ]}


def page_workload():
    wip, unassigned, aged, holders, active = one(f"""
        select count(*) filter (where {OPEN} and stage in ('In Progress', 'In Review')),
               count(*) filter (where {OPEN} and person_key = 0), count(*) filter (where {OPEN} and age_days > 180),
               count(distinct person_key) filter (where {OPEN} and person_key <> 0),
               count(distinct person_key) filter (where resolved_at is not null and person_key <> 0) from t""")
    ts = rows(f"""pivot (select team, stage, count(*) n from t where {OPEN} group by all) on stage using sum(n)
                  group by team order by team""")
    order = sorted(range(len(ts["team"])), key=lambda i: -sum(ts[s][i] or 0 for s in STAGE_C if s in ts))
    traces = [dict(type="bar", orientation="h", name=s, y=[ts["team"][i] for i in order],
                   x=[ts[s][i] or 0 for i in order], marker=dict(color=c), hovertemplate=f"{s}: %{{x:,}}<extra></extra>")
              for s, c in STAGE_C.items() if s in ts]
    stacked = fig(traces, barmode="stack", showlegend=True, legend=LEGEND,
                  yaxis=dict(autorange="reversed", showgrid=False, zeroline=False, tickfont=dict(color=INK2, size=10)),
                  xaxis=dict(visible=False), margin=dict(l=90, r=16, t=30, b=8))
    en = rows(f"""with e as (select person, home_team, count(*) filter (where {OPEN}) o,
                                    count(*) filter (where {DELIVERED}) d from t where person_key <> 0 group by all)
                  select *, sum(d) over () vis,
                         case when d > 0 then dense_rank() over (partition by home_team order by d desc) end rk
                  from e order by o desc, d desc limit 60""")
    rank, prev, body = 0, None, []
    for p, h, o, d, vis, rk in zip(*en.values()):
        if o != prev:
            rank, prev = rank + 1, o
        body.append([rank, p, h, num(o), num(d), pct(d / DELIV, 1), pct(d / vis, 1), "" if rk is None else rk])
    cp = rows(f"""select c.component, count(distinct t.ticket_key) n from t join bridge_ticket_component b using (ticket_key)
                  join dim_component c using (component_key) where {OPEN} group by 1 order by 2 desc, 1 limit 12""")
    ab = rows(f"select age_band, count(*) n from t where {OPEN} group by 1 order by min(age_days)")
    bl = rows("""select max(date) d, sum(open_tickets) n from fact_backlog_daily b join dim_date using (date)
                 where date = (select max(date) from dim_date x where x.year_week = dim_date.year_week)
                 group by year_week order by 1""")
    return {
        "kpis": kpis([("Open Tickets", num(OPEN_N)), ("Work in Progress", num(wip)), ("Unassigned Open", num(unassigned)),
                      ("Open per Engineer", num(div(OPEN_N - unassigned, holders), 1)),
                      ("Aged Open (> 180 d)", num(aged), "bad"), ("Active Engineers", num(active))]),
        "rows": [
            [("Open tickets by team and stage", "As of 30 Sep 2026, whatever the date slicer says", 640, stacked),
             ("Engineers by open work", "Rank: visual calculation. Share: of all tickets (REMOVEFILTERS); visible share: "
              "of the engineers listed (ALLSELECTED); rank in team: RANK with PARTITIONBY home team", 664,
              table(["Rank", "Engineer", "Home team", "Open", "Delivered", "Share", "Visible share", "Rank in team"],
                    body, left=(1, 2)))],
            [("Open backlog at the end of each week",
              "Replayed day by day from the change history, so a ticket closed later still counts as open back then", 480,
              lines(bl["d"], [("Open backlog", bl["n"], ACCENT, "solid")], fmt="%d %b %Y")),
             ("Open tickets by component", "Through the Ticket Components bridge; a ticket in two components counts in "
              "both. Current component values (top 12 shown here; the visual scrolls)", 384,
              hbars(cp["component"], cp["n"], ACCENT, [num(v) for v in cp["n"]], left=130)),
             ("Open tickets by age", "Age Band is added in Power Query (Tickets.pq) from Age Days", 424,
              vbars(ab["age_band"], ab["n"], ACCENT, [num(v) for v in ab["n"]], yaxis=NO_Y))],
        ]}


def page_quality():
    chk = rows("select step, \"check\", sum(tickets) n from data_checks group by all order by step")
    get = lambda s: sum(n for st, n in zip(chk["step"], chk["n"]) if st == s)
    pulled, counted, nostart = get(1), get(3), get(5)
    cov = one(f"select count(started_at) filter (where {DELIVERED}) / count(*) filter (where {DELIVERED}) from t")[0]
    # Tickets in Status: TREATAS matches the mapping's status to Tickets[Status] (DAX text compare ignores case)
    sm = rows("""select tm.team, s.status, s.stage,
                        (select count(*) from t where t.team_key = s.team_key and lower(t.status) = lower(s.status)) n
                 from status_map s join dim_team tm using (team_key)
                 order by tm.team, case s.stage when 'Backlog' then 1 when 'In Progress' then 2 when 'In Review' then 3
                 else 4 end, s.tickets desc""")
    note = ('<h4>How four different Jira workflows become one</h4><p class="note" style="margin-bottom:8px">'
            'Each team names its statuses differently: Cassandra moves tickets through triage, review and "ready to '
            'commit"; Hive mostly goes straight from open to resolved. A mapping table puts every status into one of four '
            'shared stages (backlog, in progress, in review, done), and every priority into P1–P4.</p>'
            '<p class="note" style="margin-bottom:8px">Every number is as of 30 Sep 2026. The full change history is '
            "replayed, so a ticket closed in October still counts as open on that date and the figures don't drift "
            'between refreshes.</p><p class="note">First response is the first change by anyone other than the reporter; '
            "comments aren't pulled.</p>")
    return {
        "kpis": kpis([("Tickets Pulled", num(pulled)), ("Tickets Counted", num(counted)),
                      ("Tickets Excluded", num(pulled - counted)), ("No Start Recorded", num(nostart), "risk"),
                      ("Import Error Rows", "0"), ("Cycle Time Coverage", pct(cov))]),
        "rows": [
            [("What the pipeline counted", "Every pulled issue is a ticket or excluded with a reason", 760,
              hbars(chk["check"], chk["n"], ACCENT, [num(v) for v in chk["n"]], left=290)),
             ("", "", 544, {"html": note})],
            [("Status mapping, per team", "The seed file behind the shared stages. Tickets per status is live: TREATAS "
              "applies the mapping's status to Tickets[Status] without a relationship", 1320,
              table(["Team", "Jira status", "Shared stage", "Tickets in status"],
                    [[a, b, c, num(d)] for a, b, c, d in zip(*sm.values())], left=(0, 1, 2)))],
        ]}


# ------------------------------------------------- small multiples, bookmarks, DAX queries
PRIO_C = {"P1": BEHIND_C, "P2": RISK_C, "P3": ACCENT, "P4": TARGET_C}


def page_teams():
    w = rows(f"""select tm.team, d.week_start w, coalesce(x.n, 0) n
                 from (select distinct week_start from dim_date) d cross join dim_team tm
                 left join (select team_key, dd.week_start, count(*) n from t join dim_date dd on dd.date = t.resolved_date
                            where {DELIVERED} group by 1, 2) x on x.team_key = tm.team_key and x.week_start = d.week_start
                 order by 1, 2""")
    order = rows(f"select team, count(*) filter (where {DELIVERED}) d from t group by 1 order by 2 desc")["team"]
    top = max(w["n"]) * 1.1
    traces, layout = [], {"annotations": []}
    for i, team in enumerate(order, 1):  # 2 x 2 grid
        idx = [j for j, tm in enumerate(w["team"]) if tm == team]
        c, r = (i - 1) % 2, (i - 1) // 2
        x0, x1 = c / 2 + 0.02, (c + 1) / 2 - 0.02
        y0, y1 = 1 - (r + 1) / 2 + 0.06, 1 - r / 2 - 0.1
        sx = "" if i == 1 else str(i)
        traces.append(dict(type="scatter", mode="lines", name=team, x=[w["w"][j] for j in idx], y=[w["n"][j] for j in idx],
                           xaxis=f"x{sx}", yaxis=f"y{sx}", line=dict(color=ACCENT, width=1.8),
                           hovertemplate=f"{team} · week of %{{x|%d %b %Y}}: %{{y}}<extra></extra>"))
        layout[f"xaxis{sx}"] = dict(domain=[x0, x1], tickformat="%b %y", dtick="M6", showgrid=False, zeroline=False,
                                    linecolor="#E6E4DD", tickfont=dict(color=MUTED, size=9), title=None)
        layout[f"xaxis{sx}"]["anchor"] = f"y{sx}"
        layout[f"yaxis{sx}"] = dict(range=[0, top], anchor=f"x{sx}", domain=[y0, y1], showgrid=True, gridcolor="#ECEBE6",
                                    zeroline=False, showticklabels=c == 0, tickfont=dict(color=MUTED, size=10), title=None)
        layout["annotations"].append(dict(text=f"<b>{team}</b>", x=(x0 + x1) / 2, xref="paper", y=y1, yref="paper",
                                          yanchor="bottom", showarrow=False, font=dict(color=INK, size=12)))
    sm = fig(traces, margin=dict(l=36, r=8, t=26, b=28), **layout)
    op = rows(f"select team, priority, count(*) n from t where {OPEN} group by 1, 2 order by 1, 2")
    teams = rows(f"select team, count(*) n from t where {OPEN} group by 1 order by 2 desc")["team"]
    prios = sorted(set(op["priority"]))
    cnt = {(a, b): n for a, b, n in zip(*op.values())}
    bars = fig([dict(type="bar", orientation="h", name=pr, y=teams, x=[cnt.get((tm, pr), 0) for tm in teams],
                     marker=dict(color=PRIO_C.get(pr[:2], ACCENT)), text=[num(cnt.get((tm, pr))) for tm in teams],
                     textposition="inside", insidetextanchor="middle", textfont=dict(size=10, color="#fff"),
                     hovertemplate=f"%{{y}} · {pr}: %{{x}}<extra></extra>") for pr in prios],
               barmode="stack", showlegend=True, legend=LEGEND,
               yaxis=dict(autorange="reversed", showgrid=False, zeroline=False, tickformat="", tickfont=dict(color=INK2, size=11)),
               xaxis=dict(visible=False), margin=dict(l=90, r=16, t=28, b=8))
    mx = table(["Team", *prios, "Total"],
               [[tm, *[num(cnt.get((tm, pr))) for pr in prios], num(sum(cnt.get((tm, pr), 0) for pr in prios))]
                for tm in teams] + [["Total", *[num(sum(cnt.get((tm, pr), 0) for tm in teams)) for pr in prios], num(OPEN_N)]])
    mx["shade"] = [len(teams)]
    # Delivered 4W Avg / Throughput Δ: a card evaluates them at the last week in the selection (WINDOW / OFFSET)
    wk = rows(f"""select d.week_start w, coalesce(n, 0) n from (select distinct week_start from dim_date) d
                  left join (select dd.week_start, count(*) n from t join dim_date dd on dd.date = t.resolved_date
                             where {DELIVERED} group by 1) x using (week_start) order by 1""")["n"]
    goals = quarter_goals()
    team_d = dict(zip(*rows(f"select team_key, count(*) filter (where {DELIVERED}) from t group by 1").values()))
    con_ = rows(f"""with p as (select team_key, team, person, count(*) filter (where {DELIVERED}) d,
                                     count(*) filter (where {OPEN}) o from t where person_key <> 0 group by all)
                    select team_key, team, arg_max(person, d) top, max(d) / sum(d) top_share,
                           count(*) filter (where o > 20) over20
                    from p group by team_key, team order by top_share desc""")
    conc = [[tm, top, pct(sh), num(o20), pct(div(team_d[k], goals.get(k)))]
            for k, tm, top, sh, o20 in zip(*con_.values())]
    att = div(DELIV, sum(goals.values()))
    mp = rows(f"select team, count(*) filter (where {DELIVERED}) d from t group by 1 order by 1")
    return {
        "kpis": kpis([("Delivered", num(DELIV)), ("Delivered 4W Avg", num(sum(wk[-4:]) / 4, 1)),
                      ("Throughput Δ vs Prev Week", signed(wk[-1] - wk[-2])),
                      ("Goal Attainment % (synthetic goals)", pct(att), TONE[status(att)]), ("Open Tickets", num(OPEN_N)),
                      ("Open Breached", num(BREACH), "bad")]),
        "rows": [
            [("Delivered per week, one small multiple per team",
              "Small multiples split one line chart by Teams[Team] (the Rows well) on a shared y-axis", 880, sm),
             ("", "", 424, {"stack": [
                 ("Metric Picker (field parameter)", "Delivered · Median Lead Time · Resolution SLA % · Open Tickets", 60,
                  {"html": '<div class="steps"><span class="on">Delivered</span><span>Median Lead Time</span>'
                           '<span>Resolution SLA %</span><span>Open Tickets</span></div>'}),
                 ("The metric picked above, by team", "Metric Picker is a field parameter over four measures", 0,
                  table(["Team", "Delivered"], [[a, num(b)] for a, b in zip(*mp.values())]))]})],
            [("Open tickets by team and priority",
              "As of 30 Sep 2026. The buttons are bookmarks: same data, chart or table", 880,
              {"toggle": [["Chart", bars], ["Table", mx]]}),
             ("Who carries each team", "Top engineer: INDEX 1 by delivered. Overloaded: more than 20 open tickets. "
              "Goals are synthetic", 424,
              table(["Team", "Top engineer", "Top share", "Over 20", "Goal %"], conc, left=(0, 1)))],
        ]}


def quarter_goals():
    """seeds/quarter_goals.xlsx read the way Quarter Goals.pq does: skip 2, fill the team down, sum per team."""
    from openpyxl import load_workbook
    ws = load_workbook(ROOT / "seeds" / "quarter_goals.xlsx", read_only=True)["Goals"]
    out, team = {}, None
    for r in list(ws.iter_rows(values_only=True))[3:]:
        if r[1] is None:
            continue
        team = r[0] or team
        out[team] = out.get(team, 0) + sum(v for v in r[2:] if v)
    return out


NET = f"""select d.quarter q, t.team, count(*) filter (where t.created_date = d.date)
                - count(*) filter (where t.resolved_date = d.date) n
          from t join dim_date d on d.date in (t.created_date, t.resolved_date) group by all"""


def sla_of(where="true"):
    return one(f"select count(*) filter (where {RES_MET}) / nullif(count(*), 0) from t "
               f"where resolved_at is not null and {where}")[0]


def page_diagnosis():
    reopen = one("select count(*) filter (where reopened > 0) / count(*) from t where resolved_at is not null")[0]
    sm = rows(f"""select date_trunc('month', resolved_date)::date m, count(*) filter (where {RES_MET}) / count(*) s
                  from t where resolved_at is not null group by 1 order by 1""")
    q = rows(f"""select q, sum(n) n from ({NET}) x join (select quarter, min(date) d from dim_date group by 1) y
                 on y.quarter = x.q group by q order by min(y.d)""")
    sla_line = lines(sm["m"], [("Resolution SLA %", sm["s"], ACCENT, "solid"),
                               ("Target", [0.9] * len(sm["m"]), TARGET_C, "dash")], dtick="M6")
    sla_line["layout"]["yaxis"] = {**YAXIS, "tickformat": ".0%"}
    wf = {"data": [dict(type="waterfall", x=q["q"] + ["Total"], y=q["n"] + [None],
                        measure=["relative"] * len(q["q"]) + ["total"], text=[signed(v) for v in q["n"]] + [signed(sum(q["n"]))],
                        textposition="outside", increasing=dict(marker=dict(color=BEHIND_C)),
                        decreasing=dict(marker=dict(color=ON_C)), totals=dict(marker=dict(color=INK2)),
                        connector=dict(line=dict(color="#D9D7CF")))],
          "layout": fig([], yaxis=NO_Y)["layout"]}
    dt = rows(f"""select team, priority, count(*) n from t where {BREACHED} group by all order by team, n desc""")
    teams = rows(f"select team, count(*) n from t where {BREACHED} group by 1 order by 2 desc")
    tree = "".join(
        f'<div class="mini"><span>{tm}</span><b>{num(n)}</b> · ' +
        " · ".join(f"{p} {num(k)}" for t2, p, k in zip(dt["team"], dt["priority"], dt["n"]) if t2 == tm) + "</div>"
        for tm, n in zip(teams["team"], teams["n"]))
    # key influencers, mirrored crudely: breach rate of each value vs the rest (resolved tickets only)
    base = 1 - sla_of()
    ki = []
    for col, label in (("ty.type", "Type"), ("priority", "Priority"), ("team", "Team"),
                       ("case when reopened > 0 then 'Yes' else 'No' end", "Reopened")):
        r = rows(f"""select {col} v, count(*) filter (where not ({RES_MET})) / count(*) b, count(*) n
                     from t join dim_type ty using (type_key) where resolved_at is not null group by 1""")
        ki += [(f"{label} is {v}", b / base) for v, b, n in zip(r["v"], r["b"], r["n"]) if n >= 50 and b > base]
    ki = sorted(ki, key=lambda x: -x[1])[:6]
    return {
        "kpis": kpis([("Resolution SLA %", pct(RES_SLA), TONE[status(RES_SLA)]), ("Open Breached", num(BREACH), "bad"),
                      ("Net Flow", signed(CREATED - RESOLVED), "bad"), ("Open Tickets", num(OPEN_N)),
                      ("Reopen Rate", pct(reopen, 1)), ("Median Lead Time", f"{num(LEAD, 1)} d")]),
        "rows": [
            [("1 · When: resolution SLA per month", "Against the 90% target; months below it are the problem", 432,
              sla_line),
             ("2 · Where: backlog growth per quarter, by team",
              "Waterfall of Net Flow (created − resolved). Power BI splits each step by team; the twin shows the totals", 432, wf),
             ("Ask the data", "Q&A visual over the model's synonyms", 424,
              {"html": '<div class="mini"><span>Try</span><b style="font-size:14px">open breached by team</b></div>'
                       '<p class="note">Q&amp;A runs in Power BI only; this twin has no language engine.</p>'})],
            [("3 · What: the breached tickets, split", "Decomposition tree: team → priority shown (+ stage, type in Power BI)",
              560, {"html": tree}),
             ("4 · Why: what makes a resolved ticket miss its target",
              "Key influencers, approximated: breach rate of the value ÷ the overall rate (≥ 50 tickets)", 504,
              hbars([k for k, _ in ki], [v for _, v in ki], BEHIND_C, [f"{v:.2f}×" for _, v in ki], left=170)),
             ("Then act", "", 224, {"html": '<p class="note">Select a team in the tree, then press <b>Open tickets of the '
                                           'team</b> (drillthrough to Ticket List). Right-click works too.</p>'})],
        ]}


# The RFM scores and segment rule of the DAX measures, in one place.
R_SCORE = "case when r <= 30 then 5 when r <= 90 then 4 when r <= 180 then 3 when r <= 365 then 2 else 1 end"
F_SCORE = "case when f >= 7 then 5 when f >= 5 then 4 when f >= 3 then 3 when f >= 2 then 2 else 1 end"
V_SCORE = "case when v >= 200 then 5 when v >= 50 then 4 when v >= 20 then 3 when v >= 5 then 2 else 1 end"
PEOPLE = f"""with p as (select person, home_team, count(*) v, date_diff('day', max(resolved_date), DATE '{LAST}') r,
                              count(distinct year(resolved_date) * 10 + quarter(resolved_date)) f
                       from t where {DELIVERED} and person_key <> 0 group by all),
             s as (select *, {R_SCORE} rs, {F_SCORE} fs, {V_SCORE} vs from p)
             select *, rs || '-' || fs || '-' || vs rfv,
                    case when rs >= 4 and fs >= 4 then 'Core' when rs >= 4 then 'Rising' when fs >= 3 then 'Fading'
                         else 'Occasional' end seg,
                    sum(v) over (order by v desc range between unbounded preceding and current row) / sum(v) over () cum
             from s"""
SEGMENTS = [("Core", "Watch load; pair with a riser"), ("Rising", "Mentor; hand over bigger tickets"),
            ("Fading", "Check in; reassign open work"), ("Occasional", "Tag good first issues")]


def page_contributors():
    pe = rows(f"select * from ({PEOPLE}) order by v desc, person")
    n = len(pe["person"])
    churn = sum(1 for v, r in zip(pe["v"], pe["r"]) if v >= 5 and r > 90)
    risk = sum(1 for v, r in zip(pe["v"], pe["r"]) if v >= 5 and 45 < r <= 90)
    abc = [("A", 0, 0.8), ("B", 0.8, 0.95), ("C", 0.95, 1.0000001)]
    abc_n = [sum(1 for c in pe["cum"] if lo < c <= hi) for _, lo, hi in abc]
    top = abc_n[0] + 15  # past the 80% crossing, so the line visibly reaches it
    pareto = fig([dict(type="bar", x=pe["person"][:top], y=pe["v"][:top], marker=dict(color=ACCENT), name="Delivered",
                       hovertemplate="%{x}: %{y:,}<extra></extra>"),
                  dict(type="scatter", mode="lines", x=pe["person"][:top], y=pe["cum"][:top], yaxis="y2",
                       line=dict(color=INK2, width=2), name="Cumulative %", hovertemplate="%{y:.0%}<extra></extra>"),
                  dict(type="scatter", mode="lines", x=pe["person"][:top], y=[0.8] * min(top, n), yaxis="y2",
                       line=dict(color=BEHIND_C, width=1.5, dash="dash"), name="80%")],
                 showlegend=True, legend=LEGEND, xaxis=dict(showticklabels=False),
                 yaxis2=dict(overlaying="y", side="right", range=[0, 1.02], tickformat=".0%", dtick=0.2, showgrid=False,
                             tickfont=dict(color=MUTED, size=10)), margin=dict(l=52, r=44, t=8, b=16))
    rfm = [[p, h, num(r), num(f), num(v), s, g] for p, h, r, f, v, s, g in
           zip(pe["person"], pe["home_team"], pe["r"], pe["f"], pe["v"], pe["rfv"], pe["seg"])][:60]
    seg_n = {g: pe["seg"].count(g) for g, _ in SEGMENTS}
    op = rows(f"select person_key, count(*) o from t where {OPEN} and person_key <> 0 group by 1")["o"]
    bands = [("1–5", 1, 5), ("6–20", 6, 20), ("Over 20", 21, 10 ** 6)]
    band_n = [sum(1 for o in op if lo <= o <= hi) for _, lo, hi in bands]
    # cohorts: people by first-delivery quarter; share delivering again N quarters later
    co = rows("""with a as (select distinct person_key, year(resolved_date) * 4 + quarter(resolved_date) - 1 qi
                            from t where outcome = 'Delivered' and person_key <> 0),
                     c as (select person_key, first_quarter, (first_quarter_key // 10) * 4 + first_quarter_key % 10 - 1 fi
                           from dim_person where first_quarter_key is not null)
                 select first_quarter, fi, count(distinct c.person_key) size,
                        list(a.qi - fi order by a.qi) filter (where a.qi is not null) offs
                 from c left join a using (person_key) group by all order by fi""")
    last_i = one("select max(year(date) * 4 + quarter(date) - 1) from dim_date")[0]
    cbody = []
    for q, fi, size, offs in zip(co["first_quarter"], co["fi"], co["size"], co["offs"]):
        cbody.append([q, num(size)] + [pct(sum(1 for o in offs if o == k) / size) if fi + k <= last_i else ""
                                       for k in range(1, 6)])
    # new vs returning in the last quarter: what the DAX measures fall back to when no dates are selected
    lq, lq_name = one("select max(year_quarter), arg_max(quarter, year_quarter) from dim_date")
    act, new = one(f"""select count(distinct person_key), count(distinct person_key) filter (where p.first_quarter_key = {lq})
                       from t join dim_date d on d.date = t.resolved_date join dim_person p using (person_key)
                       where {DELIVERED} and person_key <> 0 and d.year_quarter = {lq}""")
    return {
        "kpis": kpis([("Period Counted", lq_name), ("Active Contributors", num(act)),
                      ("New Contributors", num(new)), ("Returning Contributors", num(act - new)),
                      ("Engineers for 80%", num(abc_n[0])), ("At Risk Contributors", num(risk), "risk"),
                      ("Churned Contributors", num(churn), "bad")]),
        "rows": [
            [("Pareto: deliveries per engineer and their running share",
              f"Top {top} of {n} engineers, sorted high to low; the line crosses 80% at the {abc_n[0]}th, the last of "
              "class A (Power BI scrolls through all)",
              760, pareto),
             ("Engineers by recency, frequency and volume",
              "RFM adapted: R = days since the last delivery, F = quarters with one, V = all-time deliveries; each scored 1–5",
              544, table(["Engineer", "Team", "Days", "Qtrs", "Delivered", "R-F-V", "Segment"], rfm,
                         left=(0, 1, 6)))],
            [("ABC classes", "A: first 80% of deliveries, B: next 15%, C: the rest", 200,
              table(["Class", "Engineers"], [[c, num(k)] for (c, *_), k in zip(abc, abc_n)])),
             ("Segments and what to do", "Each row counts the engineers whose segment matches", 384,
              table(["Segment", "Engineers", "Action"], [[g, num(seg_n[g]), a] for g, a in SEGMENTS], left=(0, 2))),
             ("Engineers by open load", "Open tickets each engineer holds on 30 Sep 2026", 256,
              vbars([b for b, *_ in bands], band_n, ACCENT, [num(v) for v in band_n], yaxis=NO_Y,
                    xaxis=dict(tickangle=0))),
             ("Cohorts: still delivering N quarters later",
              "Rows: quarter of the first delivery (Q+0 is 100%, left out); blank = not reached yet; Q+6 and later in Power BI", 432,
              table(["Cohort", "n"] + [f"Q+{k}" for k in range(1, 6)], cbody))],
        ]}


def page_flow_analysis():
    bl = rows("""select max(date) d, sum(open_tickets) n from fact_backlog_daily b join dim_date using (date)
                 where date = (select max(date) from dim_date x where x.year_week = dim_date.year_week)
                 group by year_week order by 1""")
    slope = one(f"""select regr_slope(n, epoch(d) / 86400) * 7 from (
                     select max(date) d, sum(open_tickets) n from fact_backlog_daily join dim_date using (date)
                     where date = (select max(date) from dim_date x where x.year_week = dim_date.year_week)
                     group by year_week)""")[0]
    stages = ["Backlog", "In Progress", "In Review", "Done"]
    tr = rows("""select f.stage fs, s.stage ts, count(*) n from fact_stage_transition x
                 join dim_stage f on f.stage_key = x.from_stage_key join dim_stage s on s.stage_key = x.to_stage_key
                 group by all""")
    cnt = {(a, b): n for a, b, n in zip(tr["fs"], tr["ts"], tr["n"])}
    total = sum(tr["n"])
    out_ir = sum(n for (a, _), n in cnt.items() if a == "In Review")
    bounce = div(cnt.get(("In Review", "In Progress"), 0), out_ir)
    tbody = [[a] + [num(cnt[(a, b)]) if (a, b) in cnt else "" for b in stages] for a in stages]
    fu = rows(f"""select team, count(*) c,
                         count(distinct ticket_key) filter (where ticket_key in (select ticket_key from fact_stage_time
                             join dim_stage using (stage_key) where stage = 'In Progress')) ip,
                         count(distinct ticket_key) filter (where ticket_key in (select ticket_key from fact_stage_time
                             join dim_stage using (stage_key) where stage = 'In Review')) ir,
                         count(*) filter (where {DELIVERED}) d from t group by 1 order by 1""")
    steps = ["Created", "Reached In Progress", "Reached In Review", "Delivered"]
    tot = [sum(fu[k]) for k in ("c", "ip", "ir", "d")]
    funnel = {"data": [dict(type="funnel", y=steps, x=tot, marker=dict(color=ACCENT),
                            textinfo="value+percent initial", textfont=dict(size=10))],
              "layout": fig([], margin=dict(l=130, r=12, t=8, b=8))["layout"]}
    fbody = [[tm] + [pct(v / c) for v in (ip, ir, d)] for tm, c, ip, ir, d in zip(*fu.values())]
    ns = rows(f"""select team, priority, count(*) - count(resolved_at) n from t group by all""")
    net_tot = sum(ns["n"])
    prios = sorted(set(ns["priority"]))
    nmap = {(a, b): v for a, b, v in zip(ns["team"], ns["priority"], ns["n"])}
    nbody = [[tm] + [pct(div(nmap.get((tm, p)), net_tot), signed=True) for p in prios]
             for tm in sorted(set(ns["team"]))]
    pq = rows("""with a as (select distinct t.person_key, d.year_quarter yq, d.quarter q, p.first_quarter_key fk
                            from t join dim_date d on d.date = t.resolved_date join dim_person p using (person_key)
                            where t.outcome = 'Delivered' and t.person_key <> 0)
                 select q, count(*) filter (where yq = fk) nw, count(*) filter (where yq > fk) ret from a
                 group by q, yq order by yq""")
    nr = fig([dict(type="bar", name="New", x=pq["q"], y=pq["nw"], marker=dict(color=ON_C)),
              dict(type="bar", name="Returning", x=pq["q"], y=pq["ret"], marker=dict(color=ACCENT))],
             barmode="group", showlegend=True, legend=LEGEND)
    return {
        "kpis": kpis([("Backlog Trend /wk", f"{slope:+.1f}".replace("-", "−"), "bad" if slope > 0 else "good"),
                      ("Transitions", num(total)), ("Review Bounce Rate", pct(bounce, 1)),
                      ("Net Flow", signed(CREATED - RESOLVED), "bad"), ("Created", num(CREATED)),
                      ("Delivered", num(DELIV))]),
        "rows": [
            [("Stage transitions: from (rows) → to (columns)",
              "Each move between shared stages in the replayed history; back-moves are rework", 480,
              table(["From \\ to"] + stages, tbody)),
             ("Funnel: tickets created that reached each step",
              "In Progress / In Review: any time in the stage. Many go Backlog → Done directly", 352, funnel),
             ("Funnel % by team", "Share of the team's created tickets that reached In Progress, In Review, Done", 456,
              table(["Team", "→ Prog", "→ Review", "→ Done"], fbody))],
            [("Weekly open backlog", "Card = LINESTX slope. The Power BI visual adds a built-in forecast, not drawn here", 512,
              lines(bl["d"], [("Open backlog", bl["n"], ACCENT, "solid")], fmt="%d %b %Y")),
             ("Who adds to the backlog", "Each cell's share of the total net flow (created − resolved)", 400,
              table(["Team"] + prios, nbody)),
             ("New vs returning engineers per quarter", "New: first delivery that quarter (any team in the twin)", 376,
              nr)],
        ]}


DT_TEAM = "Artemis"


def page_team_detail():
    d, lead, s, o, b = one(f"""select count(*) filter (where {DELIVERED}), median(lead_days) filter (where {DELIVERED}),
                               count(*) filter (where resolved_at is not null and {RES_MET}) / count(resolved_at),
                               count(*) filter (where {OPEN}), count(*) filter (where {BREACHED})
                               from t where team = '{DT_TEAM}'""")
    w = rows(f"""select d.week_start w, coalesce(n, 0) n from (select distinct week_start from dim_date) d
                 left join (select dd.week_start, count(*) n from t join dim_date dd on dd.date = t.resolved_date
                            where {DELIVERED} and team = '{DT_TEAM}' group by 1) x using (week_start) order by 1""")
    pr = rows(f"""select priority, count(*) filter (where {DELIVERED}) d, median(lead_days) filter (where {DELIVERED}) m,
                         count(*) filter (where resolved_at is not null and {RES_MET}) / count(resolved_at) s
                  from t where team = '{DT_TEAM}' group by 1 order by 1""")
    en = rows(f"""select person, count(*) filter (where {DELIVERED}) d, median(lead_days) filter (where {DELIVERED}) m,
                         count(*) filter (where {OPEN}) o, count(*) filter (where {BREACHED}) b
                  from t where team = '{DT_TEAM}' and person_key <> 0 group by 1 order by d desc limit 40""")
    return {
        "kpis": kpis([("Delivered", num(d)), ("Median Lead Time", f"{num(lead, 1)} d"),
                      ("Resolution SLA %", pct(s), TONE[status(s)]), ("Open Tickets", num(o)), ("Open Breached", num(b), "bad")]),
        "rows": [
            [(f"Delivered per week · drilled through from {DT_TEAM}", "Drill up to ISO year from the visual header", 880,
              lines(w["w"], [("Delivered", w["n"], ACCENT, "solid")], fmt="week of %d %b %Y")),
             ("Speed by priority, against the team", "Lead Time vs Team Median: ALLEXCEPT keeps only the team (and dates)",
              424, table(["Priority", "Delivered", "Median", "vs team", "SLA %"],
                         [[p, num(x), num(m, 1), signed(m - lead) if m is not None else "—", cell(pct(v), v)]
                          for p, x, m, v in zip(*pr.values())]))],
            [("The team's engineers", "Right-click an engineer → Drill through → Engineer Detail or Ticket List", 1320,
              table(["Engineer", "Delivered", "Median lead", "vs team", "Open", "Open breached"],
                    [[p, num(x), num(m, 1), "—" if m is None else f"{m - lead:+.1f}".replace("-", "−"), num(o2), num(b2)]
                     for p, x, m, o2, b2 in zip(*en.values())]))],
        ]}


def page_engineer_detail():
    who = one(f"select person from t where {DELIVERED} and person_key <> 0 group by 1 order by count(*) desc limit 1")[0]
    d, lead, o = one(f"""select count(*) filter (where {DELIVERED}), median(lead_days) filter (where {DELIVERED}),
                         count(*) filter (where {OPEN}) from t where person = '{who}'""")
    team_med = dict(con.execute(f"select team, median(lead_days) from t where {DELIVERED} group by 1").fetchall())
    ty = rows(f"""select ty.type, count(*) d, median(lead_days) m, any_value(team) tm from t join dim_type ty using (type_key)
                  where {DELIVERED} and person = '{who}' group by 1 order by 2 desc""")
    lb = rows(f"""select lead_band, count(*) n from t where {DELIVERED} and person = '{who}'
                  group by 1, lead_band_order order by lead_band_order""")
    op = rows(f"""select ticket_key, team, priority, status, age_days, days_past_target, jira_url from t
                  where {OPEN} and person = '{who}' order by age_days desc""")
    return {
        "kpis": kpis([("Delivered", num(d)), ("Delivered Share", pct(d / DELIV, 1)), ("Median Lead Time", f"{num(lead, 1)} d"),
                      ("Open Tickets", num(o))]),
        "rows": [
            [(f"What {who} delivered, by type", "Against the median of their team; negative = faster", 640,
              table(["Type", "Delivered", "Median lead", "vs team"],
                    [[t_, num(x), num(m, 1), f"{m - team_med[tm]:+.1f}".replace("-", "−")] for t_, x, m, tm in zip(*ty.values())])),
             ("Their delivered tickets by lead time", "", 664,
              vbars(lb["lead_band"], lb["n"], ACCENT, [num(v) for v in lb["n"]], yaxis=NO_Y))],
            [("What they hold now", "Open tickets as of 30 Sep 2026, oldest first", 1320,
              table(["Ticket", "Team", "Priority", "Status", "Age (days)", "Days past target", "Jira URL"],
                    [[k, tm, p, s, num(a), num(dp), u] for k, tm, p, s, a, dp, u in zip(*op.values())], left=(0, 1, 2, 3, 6)))],
        ]}


def page_ticket_list():
    o, b, aged, un = one(f"""select count(*), count(*) filter (where {BREACHED}), count(*) filter (where age_days > 180),
                             count(*) filter (where person_key = 0) from t where {OPEN} and team = '{DT_TEAM}'""")
    tl = rows(f"""select ticket_key, team, person, priority, status, age_days, days_past_target, jira_url, summary
                  from t where {OPEN} and team = '{DT_TEAM}' order by age_days desc limit 300""")
    return {
        "kpis": kpis([("Open Tickets", num(o)), ("Open Breached", num(b), "bad"), ("Aged Open", num(aged)),
                      ("Unassigned Open", num(un))]),
        "rows": [[(f"Open tickets of {DT_TEAM}, oldest first",
                   f"Drillthrough on team = {DT_TEAM} + locked page filter Stages[Is Open] = TRUE (top 300 of {num(o)} here)",
                   1320, table(["Ticket", "Team", "Engineer", "Priority", "Status", "Age (days)", "Days past target",
                                "Jira URL", "Summary"],
                               [[k, tm, pe, p, s, num(a), num(dp), u, sm[:70]] for k, tm, pe, p, s, a, dp, u, sm in zip(*tl.values())],
                               left=(0, 1, 2, 3, 4, 7, 8)), "full")]]}


def page_team_tip():
    out = []
    for tm in sorted(one("select list(distinct team) from t")[0]):
        lead, s, o = one(f"""select median(lead_days) filter (where {DELIVERED}),
                             count(*) filter (where resolved_at is not null and {RES_MET}) / count(resolved_at),
                             count(*) filter (where {OPEN}) from t where team = '{tm}'""")
        top = one(f"""select max(n) / sum(n) from (select person, count(*) n from t where {DELIVERED} and team = '{tm}'
                      and person_key <> 0 group by 1)""")[0]
        out.append((f"Tooltip on hover: {tm}", "320 × 240 report-page tooltip", 320, {"html": (
            f'<div class="mini"><span>Median Lead Time</span><b>{num(lead, 1)} d</b></div>'
            f'<div class="mini"><span>Resolution SLA %</span><b class="{TONE[status(s)]}">{pct(s)}</b></div>'
            f'<div class="mini"><span>Open Tickets</span><b>{num(o)}</b></div>'
            f'<div class="mini"><span>Top Engineer Share</span><b>{pct(top)}</b></div>')}))
    return {"kpis": [], "rows": [out]}


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

BUILD = {"overview": page_overview, "flow": page_flow, "sla": page_sla, "workload": page_workload,
         "teams": page_teams, "quality": page_quality, "dax_queries": page_dax, "diagnosis": page_diagnosis,
         "team_detail": page_team_detail, "engineer_detail": page_engineer_detail, "ticket_list": page_ticket_list,
         "team_tip": page_team_tip, "contributors": page_contributors, "flow_analysis": page_flow_analysis}
