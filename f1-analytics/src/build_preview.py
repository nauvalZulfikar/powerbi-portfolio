"""Static HTML twin of the Power BI report — same pages, visuals, palette, slicer defaults and measure rules.

    uv run python src/build_preview.py      -> reports/dashboard.html (self-contained, opens offline)

Power BI Desktop is Windows-only; this lets the dashboard be reviewed anywhere.
Every number is computed in DuckDB over data/marts with the same rules as the DAX measures
(Starts exclude did-not-start, modern-scale points share, routine pit stops ≤ 60 s, car-adjusted skill).
"""
import json
from pathlib import Path

import duckdb
from plotly.offline import get_plotlyjs

from build_model import POINTS_SYSTEMS, SLOTS
from build_report import F1_RED, GRID, HAIR, INK, INK2, MUTED, OUTCOME_COLORS, PALETTE, PLANE, SEQ_HI, SEQ_LO, SURFACE
from build_report import PAGES as REPORT_PAGES

HELP = {name: help_text for name, _, _, help_text in REPORT_PAGES}

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "dashboard.html"
con = duckdb.connect()
for t in ("dim_circuit", "dim_constructor", "dim_driver", "dim_race", "dim_season", "driver_skill", "fact_lap_times",
          "fact_pit_stops", "fact_results", "fact_standings", "grid_conversion", "team_season", "teammate_h2h"):
    con.execute(f"create view {t} as select * from '{ROOT / 'data' / 'marts' / t}.parquet'")

# Power Query twins: sprint results straight from the raw CSV, and the Enter Data points systems, unpivoted.
con.execute(f"""create view sprint as select s.raceId race_id, s.driverId driver_id, s.points, st.status
                from read_csv('{ROOT / 'data' / 'raw' / 'sprint_results.csv'}', nullstr = '\\N', header = true) s
                left join read_csv('{ROOT / 'data' / 'raw' / 'status.csv'}', header = true) st using (statusId)""")
_era = None
_ps = []
for era, system, first, *pts in POINTS_SYSTEMS:
    _era = era or _era
    _ps += [(_era, system, int(first), i + 1, float(v)) for i, v in enumerate(pts) if v]
con.execute("create table points_systems (era varchar, system varchar, first_season int, position int, system_points double)")
con.executemany("insert into points_systems values (?, ?, ?, ?, ?)", _ps)
DEFAULT_SYSTEM = "2010–"
assert len(SLOTS) == 10

STARTS = "count(*) filter (where status_group <> 'Did not start')"
RATE = lambda cond: f"count(*) filter (where {cond}) / nullif({STARTS}, 0)"
CRASH = "status_group = 'Crash / driver'"
R = "fact_results r join dim_season s using (year)"
DEFAULT_SEASON, DEFAULT_RACE = 2021, "2021 · R22 · Abu Dhabi GP"


def rows(sql):
    cur = con.execute(sql)
    names, data = [d[0] for d in cur.description], cur.fetchall()
    return {n: [r[i] for r in data] for i, n in enumerate(names)}


def one(sql):
    return con.execute(sql).fetchone()


pct = lambda v, d=1: "—" if v is None else f"{v * 100:.{d}f}%"
num = lambda v: f"{v:,.0f}"


def mix(a, b, t):
    x, y = (tuple(int(h[i:i + 2], 16) for i in (1, 3, 5)) for h in (a, b))
    return "#" + "".join(f"{round(p + (q - p) * t):02x}" for p, q in zip(x, y))


def heat(vals):
    """Gradient fill SEQ_LO → SEQ_HI, like the report's conditional-format rules."""
    nums = [v for v in vals if v]
    hi = max(nums) if nums else 1
    return [{"v": num(v) if v else "", "bg": mix(SEQ_LO, SEQ_HI, v / hi) if v else ""} for v in vals]


def databar(vals, fmt=num):
    hi = max(vals) or 1
    return [{"v": fmt(v), "bar": v / hi} for v in vals]


def skill_color(s):
    if s is None:
        return "#383835"
    return "#3987E5" if s >= 0.08 else "#86B6EF" if s >= 0 else "#F19A99" if s > -0.08 else "#E66767"


# ------------------------------------------------------------------ figures
AXIS = dict(showgrid=False, zeroline=False, linecolor=HAIR, tickfont=dict(color=MUTED, size=10), title=None)
YAXIS = dict(showgrid=True, gridcolor=GRID, zeroline=False, tickfont=dict(color=MUTED, size=10), title=None)
LEGEND = dict(orientation="h", y=1.1, x=0, font=dict(color=INK2, size=10), bgcolor="rgba(0,0,0,0)")


def fig(traces, **layout):
    base = dict(margin=dict(l=44, r=12, t=8, b=32), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(family="Segoe UI, system-ui, -apple-system, sans-serif", color=INK2, size=11),
                xaxis=AXIS, yaxis=YAXIS, showlegend=False,
                hoverlabel=dict(bgcolor=SURFACE, bordercolor=HAIR, font=dict(color=INK)))
    for k, v in layout.items():
        base[k] = {**base[k], **v} if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return {"data": traces, "layout": base}


def bars(x, y, fmt=",.0f", horizontal=False, colors=PALETTE[0], **layout):
    t = dict(type="bar", marker=dict(color=colors, cornerradius=3), texttemplate=f"%{{{'x' if horizontal else 'y'}:{fmt}}}",
             textposition="outside", textfont=dict(size=10, color=INK2), cliponaxis=False)
    if horizontal:
        t.update(x=y, y=x, orientation="h", hovertemplate=f"%{{y}}: %{{x:{fmt}}}<extra></extra>")
        return fig([t], **{"yaxis": dict(autorange="reversed", showgrid=False, tickfont=dict(color=INK2, size=10)),
                          "xaxis": dict(visible=False), "margin": dict(l=130, r=48, t=4, b=4), **layout})
    t.update(x=x, y=y, hovertemplate=f"%{{x}}: %{{y:{fmt}}}<extra></extra>")
    return fig([t], **{"yaxis": dict(visible=False), "margin": dict(l=12, r=12, t=18, b=32), **layout})


def line(name, x, y, color, fmt):
    return dict(type="scatter", mode="lines", name=name, x=x, y=y, line=dict(color=color, width=2),
                hovertemplate=f"{name} · %{{x}}: %{{y:{fmt}}}<extra></extra>")


def table(columns, body, align=None):
    return {"columns": columns, "rows": body, "align": align}


def ols(x, y):
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    b = sum((a - mx) * (c - my) for a, c in zip(x, y)) / sum((a - mx) ** 2 for a in x)
    return my - b * mx, b



# -------------------------------------------------------------------- pages
def page_grid():
    seasons, races = one("select count(*), sum(races) from dim_season")
    drivers, teams, circuits = one("select count(distinct driver_id), count(distinct constructor_id), count(distinct circuit_id) from fact_results")
    geo = rows("select circuit_name, country, lat, lng, races_hosted from dim_circuit where races_hosted > 0 order by races_hosted desc")
    per = rows("select year, races, top_team_points_share sh, avg(top_team_points_share) over (order by year rows 2 preceding) ma "
               "from dim_season order by year")  # MOVINGAVERAGE ( [Avg Top Team Share], 3 ): current + 2 previous seasons
    champs = rows("select year, champion, coalesce(constructor_champion, '—') cc, champion_wins w, title_margin m "
                  "from dim_season order by year desc")
    top2 = list(zip(geo["circuit_name"][:2], geo["races_hosted"][:2]))
    return {
        "kpis": [("Seasons", num(seasons)), ("Races", num(races)), ("Drivers", num(drivers)),
                 ("Constructors", num(teams)), ("Circuits Used", num(circuits))],
        "rows": [
            [("Where F1 has raced", f"{len(geo['lat'])} circuits · bubble = Grands Prix hosted · {top2[0][0]} and {top2[1][0]} lead with {top2[0][1]} and {top2[1][1]}", 740,
              fig([dict(type="scattergeo", lat=geo["lat"], lon=geo["lng"], text=geo["circuit_name"], customdata=geo["races_hosted"],
                        marker=dict(size=[4 + v ** 0.5 * 2.6 for v in geo["races_hosted"]], color=PALETTE[0], opacity=0.75,
                                    line=dict(color=PLANE, width=0.6)),
                        hovertemplate="%{text}: %{customdata} races<extra></extra>")],
                  geo=dict(bgcolor="rgba(0,0,0,0)", showland=True, landcolor="#2A2A33", showcountries=True, countrycolor="#3A3A44",
                           showocean=False, showframe=False, coastlinecolor="#3A3A44", projection=dict(type="natural earth"),
                           lataxis=dict(range=[-50, 72])),
                  margin=dict(l=0, r=0, t=0, b=0))),
             ("Grands Prix per season", f"{per['races'][0]} races in {per['year'][0]} → {per['races'][-1]} in {per['year'][-1]}", 524,
              fig([dict(type="bar", x=per["year"], y=per["races"], marker=dict(color=PALETTE[0], cornerradius=2),
                        hovertemplate="%{x}: %{y} races<extra></extra>")], bargap=0.25))],
            [("Roll of champions", "Newest first · constructors' title awarded from 1958", 640,
              table(["Year", "Champion", "Constructor Champion", "Champion Wins", "Title Margin"],
                    [[y, c, cc, w, f"{m:,.1f}".rstrip("0").rstrip(".")] for y, c, cc, w, m in
                     zip(champs["year"], champs["champion"], champs["cc"], databar(champs["w"]), champs["m"])]), "scroll"),
             ("How dominant was the best team?", "Best constructor's share of the season's points (modern scale) · 3-season average = visual calculation", 624,
              fig([line("Top team share", per["year"], per["sh"], PALETTE[0], ".1%"), line("3-season average", per["year"], per["ma"], PALETTE[1], ".1%")],
                  showlegend=True, legend=LEGEND, yaxis=dict(tickformat=".0%")))],
        ]}


def page_dynasties():
    wins, teams, share, winners = one(f"select count(*) filter (where is_win), count(distinct constructor_id), "
                                      f"(select avg(top_team_points_share) from dim_season), "
                                      f"count(distinct driver_id) filter (where is_win) from fact_results")
    top8 = [r[0] for r in con.execute("select constructor_id from fact_results group by 1 order by sum(points_modern) desc limit 8").fetchall()]
    ids = ",".join(map(str, top8))
    ribbon = rows(f"""select s.decade, c.constructor_name team, c.constructor_id cid,
                      sum(t.points_modern) / sum(sum(t.points_modern)) over (partition by s.decade) sh
                      from team_season t join dim_season s using (year) join dim_constructor c using (constructor_id)
                      group by 1, 2, 3 order by 1""")
    decades = sorted(set(ribbon["decade"]))
    names = {cid: n for cid, n in zip(ribbon["cid"], ribbon["team"])}
    traces = []
    for i, cid in enumerate(top8):
        y = [next((s for d2, c2, s in zip(ribbon["decade"], ribbon["cid"], ribbon["sh"]) if d2 == d and c2 == cid), 0) for d in decades]
        traces.append(dict(type="bar", name=names[cid], x=decades, y=y, marker=dict(color=PALETTE[i], line=dict(color=SURFACE, width=1)),
                           hovertemplate=f"{names[cid]} · %{{x}}: %{{y:.1%}}<extra></extra>"))
    leaders = [max(((s, t) for d2, t, s in zip(ribbon["decade"], ribbon["team"], ribbon["sh"]) if d2 == d))[1] for d in decades]
    chain = " → ".join(t for i, t in enumerate(leaders) if i == 0 or t != leaders[i - 1])
    top_w = rows("select c.constructor_name k, count(*) v from fact_results r join dim_constructor c using (constructor_id) "
                 "where is_win group by 1 order by 2 desc limit 10")
    mat = rows(f"""pivot (select c.constructor_name team, s.decade, count(*) filter (where is_win) w
                   from {R} join dim_constructor c using (constructor_id)
                   where constructor_id in (select constructor_id from fact_results where is_win group by 1 order by count(*) desc limit 8)
                   group by all) on decade using sum(w) order by team""")
    dec_cols = [c for c in mat if c != "team"]
    order = sorted(range(len(mat["team"])), key=lambda i: -sum(mat[c][i] or 0 for c in dec_cols))
    cols_heat = {c: heat([mat[c][i] or 0 for i in order]) for c in dec_cols}
    haul = rows("select constructor_name, constructor_titles, driver_titles, win_rate from dim_constructor "
                "where constructor_titles > 0 or driver_titles > 0 order by constructor_titles desc, driver_titles desc")
    return {
        "slicer": [("Ribbon metric", "Points share"), ("Era", "All")],
        "kpis": [("Wins", num(wins)), ("Constructors", num(teams)), ("Avg Top Team Share", pct(share)),
                 ("Different Winners", num(winners))],
        "rows": [
            [("Dynasties by decade", f"Metric picked above (default: share of all points) · top 8 by points · drill up to era, down to season · decade leaders by share: {chain}", 1280,
              fig(traces, barmode="stack", bargap=0.18, showlegend=True, legend=LEGEND, yaxis=dict(tickformat=".0%")))],
            [("Most race wins", "Top 10 constructors", 400, bars(top_w["k"], top_w["v"], horizontal=True, margin=dict(l=100, r=40, t=4, b=4))),
             ("Wins by decade", "Heat = wins · who owned which decade", 470,
              table(["Constructor"] + [d.removesuffix("s") + "s" for d in dec_cols],
                    [[mat["team"][i]] + [cols_heat[c][j] for c in dec_cols] for j, i in enumerate(order)])),
             ("Championship haul", "Constructors' and drivers' titles, all time", 410,
              table(["Constructor", "Constructor Titles", "Driver Titles", "Win Rate"],
                    [[n, t, d, pct(w)] for n, t, d, w in zip(haul["constructor_name"], databar(haul["constructor_titles"]),
                                                             haul["driver_titles"], haul["win_rate"])]), "scroll")],
        ]}


def page_legends():
    drivers, winners, titles, debut = one("select count(*), count(*) filter (where wins > 0), sum(titles), avg(debut_age) from dim_driver")
    hof = rows("""select d.driver_name, d.country, d.titles, d.wins, d.poles, d.podiums, d.win_rate, k.skill
                  from dim_driver d left join driver_skill k using (driver_id) order by d.wins desc limit 10""")
    sc = rows("""select driver_name, starts, win_rate, titles, wins, case when wins = 0 then 'No wins' when win_rate < 0.05 then 'Under 5%'
                 when win_rate < 0.2 then '5–19%' else '20%+' end band from dim_driver order by wins desc limit 60""")
    ta, tb = ols(sc["starts"], sc["win_rate"])
    band_c = {"Under 5%": PALETTE[1], "5–19%": PALETTE[0], "20%+": PALETTE[2], "No wins": MUTED}
    sk = rows("select d.driver_name, k.skill from driver_skill k join dim_driver d using (driver_id) where is_ranked order by skill desc limit 15")
    nat = rows("select country, sum(wins) w from dim_driver where wins > 0 group by 1 order by 2 desc")
    fangio = next(s / w for n, s, w in zip(sc["driver_name"], sc["starts"], sc["wins"]) if n.endswith("Fangio"))
    return {
        "slicer": [("Treemap by", "Nationality"), ("Achievement", "All")],
        "kpis": [("Drivers", num(drivers)), ("Different Winners", num(winners)), ("Driver Titles Won", num(titles)),
                 ("Avg Debut Age", f"{debut:.1f}")],
        "rows": [
            [("Hall of fame", "Sorted by wins · car-adjusted skill: + beats an average driver in the same car", 730,
              table(["Driver", "Country", "Titles", "Wins", "Poles", "Podiums", "Win Rate", "Car-Adj. Skill"],
                    [[n, c, t, w, p, pd, pct(wr), {"v": "" if s is None else f"{s:+.3f}", "fg": skill_color(s)}]
                     for n, c, t, w, p, pd, wr, s in zip(hof["driver_name"], hof["country"], hof["titles"], databar(hof["wins"]),
                                                         hof["poles"], hof["podiums"], hof["win_rate"], hof["skill"])]), "scroll"),
             ("Longevity vs strike rate", f"Top 60 winners · colour = win-rate band · bubble = titles · dotted = trend · Fangio wins 1 in {fangio:.1f} starts", 534,
              fig([dict(type="scatter", mode="markers", name=b, x=[x for x, g in zip(sc["starts"], sc["band"]) if g == b],
                        y=[y for y, g in zip(sc["win_rate"], sc["band"]) if g == b], text=[n for n, g in zip(sc["driver_name"], sc["band"]) if g == b],
                        customdata=[t for t, g in zip(sc["titles"], sc["band"]) if g == b],
                        marker=dict(size=[8 + t * 5 for t, g in zip(sc["titles"], sc["band"]) if g == b], color=band_c[b], opacity=0.7,
                                    line=dict(color=PLANE, width=1)),
                        hovertemplate="%{text}: %{x} starts · %{y:.1%} wins · %{customdata} titles<extra></extra>")
                   for b in ["Under 5%", "5–19%", "20%+"] if b in sc["band"]] + [
                   dict(type="scatter", mode="lines", name="Trend", x=[min(sc["starts"]), max(sc["starts"])],
                        y=[ta + tb * min(sc["starts"]), ta + tb * max(sc["starts"])], line=dict(color=INK2, width=1.5, dash="dot"),
                        hoverinfo="skip"),
                   dict(type="scatter", mode="text", x=[x for x, t in zip(sc["starts"], sc["titles"]) if t >= 4],
                        y=[y for y, t in zip(sc["win_rate"], sc["titles"]) if t >= 4],
                        text=[n.split()[-1] for n, t in zip(sc["driver_name"], sc["titles"]) if t >= 4],
                        textposition="top center", textfont=dict(size=10, color=INK2), hoverinfo="skip", showlegend=False)],
                  xaxis=dict(title=dict(text="Starts", font=dict(size=10, color=MUTED)), showgrid=True, gridcolor=GRID),
                  yaxis=dict(tickformat=".0%"), showlegend=True, legend=LEGEND, margin=dict(l=44, r=16, t=8, b=40)))],
            [("Best drivers once the car is removed", "Regression: finish percentile ~ driver + team-season · 40+ race careers", 640,
              bars(sk["driver_name"], sk["skill"], fmt="+.3f", horizontal=True, colors=[skill_color(s) for s in sk["skill"]])),
             ("Wins, grouped by the field picked above", f"Default nationality: {nat['country'][0]} leads; {', '.join(nat['country'][1:4])} follow", 624,
              fig([dict(type="treemap", labels=nat["country"], parents=[""] * len(nat["country"]), values=nat["w"],
                        marker=dict(colors=[PALETTE[i % len(PALETTE)] for i in range(len(nat["w"]))], line=dict(color=SURFACE, width=2)),
                        texttemplate="%{label}<br>%{value}", textfont=dict(color="#fff"),
                        hovertemplate="%{label}: %{value} wins<extra></extra>")], margin=dict(l=0, r=0, t=0, b=0)))],
        ]}


def page_racecraft():
    pole, fin, mech, crash = one(f"select count(*) filter (where is_pole and is_win) / count(*) filter (where is_pole), "
                                 f"{RATE('is_finished')}, {RATE('is_mechanical_dnf')}, {RATE(CRASH)} "
                                 f"from fact_results")
    gc = rows("select grid, sum(wins) / sum(starts) p from grid_conversion where grid between 1 and 20 group by 1 order by 1")
    by_era = dict(con.execute("select era, wins / starts from grid_conversion where grid = 1").fetchall())
    turbo = next(v for k, v in by_era.items() if "turbo" in k)
    hybrid = next(v for k, v in by_era.items() if "Hybrid" in k)
    rel = rows(f"""with y as (select year, {RATE('is_mechanical_dnf')} mech, {RATE(CRASH)} crash, {RATE('is_dnf')} dnf
                   from fact_results group by 1)
                   select *, avg(dnf) over (order by year range between 4 preceding and current row) dnf5 from y order by year""")
    out = rows(f"select s.decade, status_group g, count(*) / sum(count(*)) over (partition by s.decade) sh "
               f"from {R} where status_group <> 'Did not start' group by 1, 2 order by 1")
    decs = sorted(set(out["decade"]))
    stack = [dict(type="bar", name=g, x=decs, marker=dict(color=c),
                  y=[next((s for d2, g2, s in zip(out["decade"], out["g"], out["sh"]) if d2 == d and g2 == g), 0) for d in decs],
                  texttemplate="%{y:.0%}", textposition="inside", insidetextanchor="middle", textfont=dict(size=9, color="#fff"),
                  hovertemplate=f"{g} · %{{x}}: %{{y:.1%}}<extra></extra>") for g, c in OUTCOME_COLORS.items()]
    fin_by = {d: sum(s for d2, g2, s in zip(out["decade"], out["g"], out["sh"]) if d2 == d and g2.startswith("Finished")) for d in decs}
    early = [v for d, v in fin_by.items() if d <= "1990s"]
    circ = rows("select circuit_name, country, races_hosted, pole_win_rate, coalesce(lap_record, '—') rec, "
                "coalesce(lap_record_holder, '—') who from dim_circuit order by races_hosted desc limit 30")
    lo, hi = min(circ["pole_win_rate"]), max(circ["pole_win_rate"])
    return {
        "slicer": ("Era (grid chart)", "All"),
        "buttons": ["↺  Reset filters"],
        "kpis": [("Pole Conversion", pct(pole)), ("Finish Rate", pct(fin)), ("Mechanical DNF Rate", pct(mech)), ("Crash DNF Rate", pct(crash))],
        "rows": [
            [("Does qualifying decide the race?",
              f"Chance of winning by starting slot · pole won {pct(turbo, 0)} of turbo-era races, {pct(hybrid, 0)} of hybrid-era races", 620,
              fig([dict(type="bar", x=gc["grid"], y=gc["p"], marker=dict(color=PALETTE[0], cornerradius=3),
                        texttemplate="%{y:.0%}", textposition="outside", textfont=dict(size=10, color=INK2), cliponaxis=False,
                        hovertemplate="P%{x}: %{y:.1%}<extra></extra>")],
                  xaxis=dict(tickmode="linear", dtick=1, title=dict(text="Starting slot", font=dict(size=10, color=MUTED))),
                  yaxis=dict(visible=False), margin=dict(l=12, r=12, t=18, b=40))),
             ("The reliability revolution", "Share of starts ending in a breakdown vs a crash · all retirements, 5-season average (DATESINPERIOD) · dotted = trend", 644,
              fig([line("Mechanical DNF Rate", rel["year"], rel["mech"], PALETTE[1], ".1%"),
                   line("Crash DNF Rate", rel["year"], rel["crash"], PALETTE[0], ".1%"),
                   line("All DNFs, 5-season avg", rel["year"], rel["dnf5"], PALETTE[2], ".1%")] +
                  [dict(type="scatter", mode="lines", x=[rel["year"][0], rel["year"][-1]], showlegend=False, hoverinfo="skip",
                        y=[(a := ols(rel["year"], rel[k]))[0] + a[1] * rel["year"][0], a[0] + a[1] * rel["year"][-1]],
                        line=dict(color=c, width=1, dash="dot")) for k, c in (("mech", PALETTE[1]), ("crash", PALETTE[0]), ("dnf5", PALETTE[2]))],
                  showlegend=True, legend=LEGEND, yaxis=dict(tickformat=".0%")))],
            [("How races ended, by decade",
              f"Share of starts · {pct(min(early), 0)[:-1]}–{pct(max(early), 0)} finished in each decade up to the 1990s, {pct(fin_by[decs[-1]], 0)} in the {decs[-1]}", 620,
              fig(stack, barmode="stack", bargap=0.2, showlegend=True, legend=dict(LEGEND, y=1.14), yaxis=dict(tickformat=".0%"),
                  uniformtext=dict(minsize=8, mode="hide"),
                  margin=dict(l=40, r=12, t=30, b=28))),
             ("Circuit almanac", "Pole win rate · fastest race lap on record (2004+ timing)", 644,
              table(["Circuit", "Country", "Races", "Pole Win Rate", "Lap Record", "Holder"],
                    [[n, c, h, {"v": pct(p, 0), "bg": mix(SEQ_LO, SEQ_HI, (p - lo) / ((hi - lo) or 1))}, r, w]
                     for n, c, h, p, r, w in zip(*circ.values())]), "scroll")],
        ]}


def page_titlefight(year=DEFAULT_SEASON, win_points=25):
    champ, cc = one(f"select champion, constructor_champion from dim_season where year = {year}")
    last = one(f"select max(round) from fact_standings where year = {year}")[0]
    top5 = rows(f"""select st.driver_id, d.driver_name from fact_standings st join dim_driver d using (driver_id)
                    where year = {year} and round = {last} order by points desc limit 5""")
    cum = rows(f"select driver_id, round, points, position from fact_standings where year = {year} order by round")
    per = lambda did, k: [v for d, v in zip(cum["driver_id"], cum[k]) if d == did]  # noqa: E731
    traces = [line(n.split()[-1], per(did, "round"), per(did, "points"), PALETTE[i], ",.1f")
              for i, (did, n) in enumerate(zip(top5["driver_id"], top5["driver_name"]))]
    bump = [dict(type="scatter", mode="lines+markers", name=n.split()[-1], x=per(did, "round"), y=per(did, "position"),
                 line=dict(color=PALETTE[i], width=2), marker=dict(size=5),
                 hovertemplate=f"{n.split()[-1]} · round %{{x}}: P%{{y}}<extra></extra>")
            for i, (did, n) in enumerate(zip(top5["driver_id"], top5["driver_name"]))]
    # What-if Points = modern-scale points + (win value - 25) × wins; Re-scored = every finish under the chosen system
    st = rows(f"""with sp as (select driver_id, sum(points) sp from sprint join dim_race using (race_id) where year = {year} group by 1)
                  select d.driver_name, sum(r.points) pts, coalesce(any_value(sp.sp), 0) sp, sum(r.points) + coalesce(any_value(sp.sp), 0) tot,
                  count(*) filter (where is_win) w, coalesce(sum(ps.system_points), 0) rescored,
                  sum(r.points_modern) + ({win_points} - 25) * count(*) filter (where is_win) whatif
                  from fact_results r join dim_driver d using (driver_id) left join sp using (driver_id)
                  left join points_systems ps on ps.system = '{DEFAULT_SYSTEM}' and ps.position = r.finish_position
                  where year = {year} group by 1 order by tot desc limit 20""")
    # 'Re-scored / What-if Champion': the leader on that column, and whether the official champion changes
    leader = lambda k: (lambda n: f"{n.split()[-1]} · {'unchanged' if n == champ else 'changed'}")(  # noqa: E731
        max(zip(st[k], st["driver_name"]))[1])
    return {
        "slicer": [("What-if: points for a win", str(win_points)), ("Re-score with points system", DEFAULT_SYSTEM), ("Season", str(year))],
        "kpis": [("Champions (driver · team)", f"{champ} · {cc or 'no constructors’ title'}"),
                 ("Re-scored Champion", leader("rescored")), ("What-if Champion", leader("whatif"))],
        "wide": True,
        "rows": [
            [("The title fight, round by round", f"Official cumulative points of the season's top 5 · {year}", 1280,
              fig(traces, showlegend=True, legend=LEGEND, xaxis=dict(tickmode="linear", dtick=1, title=dict(text="Round", font=dict(size=10, color=MUTED))),
                  margin=dict(l=44, r=12, t=8, b=40)), None, ["Actual points", "Modern points"])],
            [("Final standings", "Official = GP + sprint · Re-scored = every finish under the chosen system · What-if = modern scale, win worth the slider value", 620,
              table(["Driver", "GP Points", "Sprint Points", "Total Points", "Wins", "Re-scored", "What-if"],
                    [[n, f"{p:,.1f}".removesuffix(".0"), f"{sp:,.0f}", t, w, f"{rs:,.0f}", f"{wi:,.0f}"] for n, p, sp, t, w, rs, wi in
                     zip(st["driver_name"], st["pts"], st["sp"], databar(st["tot"], lambda v: f"{v:,.1f}".removesuffix(".0")),
                         st["w"], st["rescored"], st["whatif"])]), "scroll"),
             ("Championship position, round by round", "Deneb (Vega-Lite) bump chart · top 5 at season end · P1 at the top", 644,
              fig(bump, showlegend=True, legend=dict(LEGEND, y=-0.18),
                  xaxis=dict(tickmode="linear", dtick=1), yaxis=dict(autorange="reversed", dtick=1, tickprefix="P"),
                  margin=dict(l=36, r=12, t=8, b=56)))],
        ]}


def page_pitwall(label=DEFAULT_RACE):
    rid, winner, pole = one(f"select race_id, winner, pole_sitter from dim_race where race_label = '{label}'")
    stops, med = one(f"select count(*), median(pit_seconds) filter (where is_routine_stop) from fact_pit_stops where race_id = {rid}")
    best = one(f"select min(lap_seconds) from fact_lap_times where race_id = {rid}")[0]
    top8 = rows(f"""select r.driver_id, d.driver_code from fact_results r join dim_driver d using (driver_id)
                    where race_id = {rid} order by finish_percentile desc nulls last, position_order limit 8""")
    laps = rows(f"select driver_id, lap, position from fact_lap_times where race_id = {rid} order by lap")
    traces = [line(code, [l for d, l in zip(laps["driver_id"], laps["lap"]) if d == did],
                   [p for d, p in zip(laps["driver_id"], laps["position"]) if d == did], PALETTE[i], "d")
              for i, (did, code) in enumerate(zip(top8["driver_id"], top8["driver_code"]))]
    low = max(max(t["y"]) for t in traces)
    led = rows(f"select d.driver_name k, count(*) v from fact_lap_times l join dim_driver d using (driver_id) "
               f"where race_id = {rid} and position = 1 group by 1 order by 2 desc")
    pit = rows(f"select c.constructor_name k, median(pit_seconds) v from fact_pit_stops p join dim_constructor c using (constructor_id) "
               f"where race_id = {rid} and is_routine_stop group by 1 order by 2")
    pace = rows(f"""select d.driver_code, median(lap_seconds) med, count(*) n, dense_rank() over (order by median(lap_seconds)) rk
                     from fact_lap_times l join dim_driver d using (driver_id)
                     where race_id = {rid} and not is_pit_lap group by 1 order by 2""")
    return {
        "slicer": ("Race (1996+ lap data)", label),
        "kpis": [("Race", f"Winner: {winner} · Pole: {pole}"), ("Pit Stops Made", num(stops)),
                 ("Median Pit Lane Time", f"{med:.1f} s"), ("Best Lap Time", f"{int(best // 60)}:{best % 60:06.3f}")],
        "wide": True,
        "rows": [
            [("Race story, lap by lap", "Running position of the top 8 finishers · P1 at the top · dips are pit stops", 1280,
              fig(traces, showlegend=True, legend=LEGEND, yaxis=dict(range=[low + 0.5, 0.5], tickvals=list(range(1, low + 1)), ticktext=[f"P{i}" for i in range(1, low + 1)]),
                  xaxis=dict(title=dict(text="Lap", font=dict(size=10, color=MUTED))), margin=dict(l=44, r=12, t=8, b=40)))],
            [("Laps led", "Who controlled the race", 410, bars(led["k"], led["v"], horizontal=True, bargap=0.55)),
             ("Pit lane time by team", "Median entry-to-exit, routine stops · shorter is better", 430,
              bars(pit["k"], pit["v"], fmt=".1f", horizontal=True, colors=PALETTE[1])),
             ("Race pace", "Median racing lap (in/out laps dropped, Power Query Group By) · rank = visual calculation", 410,
              table(["Driver", "Median Lap", "Laps Timed", "Pace Rank"],
                    [[c, f"{int(m // 60)}:{m % 60:06.3f}", n, k] for c, m, n, k in zip(pace["driver_code"], pace["med"], pace["n"], pace["rk"])]), "scroll")],
        ]}


DEFAULT_DRIVER = "Lewis Hamilton"
blank = lambda v, f: "" if v is None else f(v)  # noqa: E731  (DAX blanks stay empty cells)


def page_seasonlens(driver=DEFAULT_DRIVER, year=DEFAULT_SEASON):
    did = one(f"select driver_id from dim_driver where driver_name = '{driver}'")[0]
    best, per_race, won, home = one(f"""
        with r as (select f.*, d.country dc, c.country cc from fact_results f join dim_driver d using (driver_id)
                   join dim_circuit c using (circuit_id) where f.driver_id = {did})
        select (select max(p) from (select sum(points_modern) p from r group by year)),
               (select avg(p) from (select sum(points_modern) p from r group by race_id)),
               count(distinct year) filter (where is_win), count(*) filter (where is_win and dc = cc) from r""")
    ss = rows(f"""with y as (select year, sum(points_modern) pts, count(*) filter (where is_win) w,
                             {STARTS} starts from fact_results where driver_id = {did} group by 1),
                  allw as (select year, count(*) filter (where is_win) tw from fact_results group by 1)
                  select y.year, y.pts, p.pts py, y.w, y.w / allw.tw shr, sum(y.w) over (order by y.year) run,
                         y.w / y.starts wr, p.w / p.starts pwr,
                         sum(y.w) over (order by y.year) / sum(y.starts) over (order by y.year) cwr
                  from y join allw using (year) left join y p on p.year = y.year - 1 order by y.year""")
    nz = lambda v: v or None  # noqa: E731  (COUNTROWS over no rows = blank)
    yoy = lambda a, b: None if not b else (a or 0) / b - 1  # noqa: E731  DIVIDE ( cur - py, py )
    rnd = rows(f"""with r as (select race_id, round, sum(points_modern) p from fact_results where driver_id = {did} and year = {year} group by 1, 2)
                   select r.round, r.p, sum(r.p) over w run, avg(r.p) over (order by r.round rows 2 preceding) avg3,
                          st.points std, lag(st.points) over w prev
                   from r left join fact_standings st on st.race_id = r.race_id and st.driver_id = {did}
                   window w as (order by r.round) order by r.round""")
    rk = rows(f"""select d.driver_name, count(*) filter (where is_win) w,
                  count(*) filter (where is_win and grid in (1, 2)) fr, count(*) filter (where is_win and grid > 10) out10
                  from fact_results r join dim_driver d using (driver_id) where year = {year} group by 1 having w > 0 order by w desc, 1""")
    tot = sum(rk["w"])
    ranks = [sorted(set(rk["w"]), reverse=True).index(w) + 1 for w in rk["w"]]
    sign = lambda v: f"{v * 100:+.0f}%"  # noqa: E731
    one1 = lambda v: f"{v:,.1f}"  # noqa: E731
    return {
        "slicer": [("Season (bottom row)", str(year)), ("Driver", driver)],
        "kpis": [("Best Season Points", num(best)), ("Avg Points per Race", f"{per_race:.1f}"), ("Seasons With a Win", num(won)),
                 ("Home Wins", num(home))],
        "rows": [
            [("Career, season by season", "Modern-scale points · PY = SAMEPERIODLASTYEAR · share = of all wins that season (ALLEXCEPT) · running wins = visual calc", 760,
              table(["Year", "Points", "Points PY", "YoY", "Wins", "Share of Wins", "Running Wins"],
                    [[y, num(p), blank(py, num), blank(yoy(p, py), sign), blank(nz(w), num), blank(nz(sh), pct), num(rn)]
                     for y, p, py, w, sh, rn in zip(ss["year"], ss["pts"], ss["py"], ss["w"], ss["shr"], ss["run"])]), "scroll"),
             ("Win rate through the Period calculation group", "One measure, four lenses: current, previous season, YoY change, career to date", 504,
              table(["Year", "Current", "Prev Season", "YoY %", "Career-to-date"],
                    [[y, blank(nz(wr), pct), blank(nz(pw), pct), blank(None if not pw else (wr or 0) / pw - 1, sign), blank(nz(cw), pct)]
                     for y, wr, pw, cw in zip(ss["year"], ss["wr"], ss["pwr"], ss["cwr"])]), "scroll")],
            [("Season picked above, round by round", "Running and 3-race average: WINDOW, modern scale · standings: official points, previous round via OFFSET", 620,
              table(["Round", "Points", "Running", "3-Race Avg", "Standings (official)", "Prev Round (official)"],
                    [[r, num(p), num(rn), one1(a), blank(sd, one1), blank(pv, one1)]
                     for r, p, rn, a, sd, pv in zip(*rnd.values())]), "scroll"),
             ("Who won the season picked above", "All drivers (ignores the driver slicer) · rank: RANK DENSE · front row: KEEPFILTERS", 644,
              table(["Driver", "Wins", "Rank", "Share", "From Front Row", "From P11+"],
                    [[n, w, k, pct(w / tot), blank(nz(fr), num), blank(nz(o), num)]
                     for n, w, k, fr, o in zip(rk["driver_name"], rk["w"], ranks, rk["fr"], rk["out10"])]), "scroll")],
        ]}


def page_driver_profile(driver=DEFAULT_DRIVER):
    did, titles, nat, first, last, teams, url = one(f"""select driver_id, titles, nationality, first_season, last_season, teams_driven_for,
                                                        wikipedia_url from dim_driver where driver_name = '{driver}'""")
    wins, pods = one(f"select count(*) filter (where is_win), count(*) filter (where is_podium) from fact_results where driver_id = {did}")
    skill = one(f"select skill from driver_skill where driver_id = {did}")[0]
    car = rows(f"""select r.year, c.constructor_name, sum(points_modern) pts, count(*) filter (where is_win) w,
                   count(*) filter (where is_podium) pod, count(*) filter (where is_pole) pol
                   from fact_results r join dim_constructor c using (constructor_id) where driver_id = {did} group by 1, 2 order by 1""")
    h2h = rows(f"""select h.year, c.constructor_name, sum(quali_wins) / sum(races_together) q, sum(race_wins) / nullif(sum(comparable_races), 0) r
                   from teammate_h2h h join dim_constructor c using (constructor_id) where driver_id = {did} group by 1, 2 order by 1""")
    return {
        "back": True,
        "kpis": [("Driver", driver), ("Driver Titles Won", num(titles)), ("Wins", num(wins)), ("Podiums", num(pods)),
                 ("Car-Adjusted Skill", f"{skill:+.3f}")],
        "rows": [
            [("Career, season by season", "Team driven for · modern-scale points", 760,
              table(["Year", "Team", "Points", "Wins", "Podiums", "Poles"],
                    [[y, c, p, w, pd, pl] for y, c, p, w, pd, pl in
                     zip(car["year"], car["constructor_name"], databar(car["pts"]), car["w"], car["pod"], car["pol"])]), "scroll"),
             ("Team-mate duels", "Share of races ahead of the team-mate · qualifying and race", 504,
              table(["Year", "Team", "Quali", "Race"],
                    [[y, c, pct(q, 0), pct(r, 0)] for y, c, q, r in zip(h2h["year"], h2h["constructor_name"], h2h["q"], h2h["r"])]), "scroll")],
            [("Read more", "Wikipedia link from the Ergast driver file (column categorised as Web URL)", 1024,
              table(["Driver", "Nationality", "First", "Last", "Teams", "Wikipedia"],
                    [[driver, nat, first, last, teams, {"v": f'<a href="{url}" style="color:{PALETTE[0]}">🔗 {url.split("/")[-1]}</a>'}]])),
             ("", "", 240, {"logo": True})],
        ]}


def page_market():
    """Driver market: Pareto, ABC, win bands, rookies/returning, cohort retention, churn, team transitions.
    Period cards: Era = All, so they fall back to 2023 like the DAX (2024's leavers are not known yet)."""
    ref = 2023
    inp, rook, leave = one(f"""with d as (select distinct r.driver_id, dd.first_season f, dd.last_season l
                                from fact_results r join dim_driver dd using (driver_id) where r.year = {ref})
                                select count(*), count(*) filter (where f = {ref}), count(*) filter (where l = {ref}) from d""")
    # Pareto, same tie rule as the DAX: a driver counts while the wins of everyone strictly ahead stay under 80%
    w = rows("""select d.driver_name, count(*) filter (where is_win) w from fact_results r join dim_driver d using (driver_id)
                group by 1 having w > 0 order by w desc, 1""")
    tot = sum(w["w"])
    n80 = sum(1 for x in w["w"] if sum(y for y in w["w"] if y > x) < 0.8 * tot)
    drivers = one("select count(distinct driver_id) from fact_results")[0]
    rank = {v: i + 1 for i, v in enumerate(sorted(set(w["w"]), reverse=True))}
    cum = [sum(y for y in w["w"] if rank[y] <= rank[x]) / tot for x in w["w"]]
    top = 40
    abc = rows("""select k.constructor_name, sum(r.points_modern) p from fact_results r join dim_constructor k using (constructor_id)
                  group by 1 having p > 0 order by p desc, 1""")
    ptot = sum(abc["p"])
    abc_rows = []
    for name, p in zip(abc["constructor_name"], abc["p"]):
        c = sum(q for q in abc["p"] if q >= p) / ptot
        before = c - p / ptot
        abc_rows.append([name, num(p), pct(c, 0), "A" if before < 0.7 else "B" if before < 0.9 else "C"])
    classes = {k: sum(1 for r in abc_rows if r[3] == k) for k in "ABC"}
    per = rows("""with d as (select distinct r.year, r.driver_id, dd.first_season f from fact_results r join dim_driver dd using (driver_id))
                  select year, count(*) filter (where f = year) rookies, count(*) filter (where f < year) ret from d group by 1 order by 1""")
    churn = rows(f"""with d as (select distinct r.year, r.driver_id, dd.last_season l from fact_results r join dim_driver dd using (driver_id))
                     select year, count(*) filter (where l = year) / count(*) c from d where year < 2024 group by 1 order by 1""")
    # cohort retention: debut decade × seasons since debut, cohorts too recent to observe season N left out
    coh = rows("""with dd as (select driver_id, first_season f, cast(floor(first_season / 10) * 10 as int) || 's' cohort from dim_driver
                              where driver_id in (select driver_id from fact_results)),
                       ys as (select distinct driver_id, year from fact_results), n as (select range n from range(1, 9))
                  select cohort, n, count(*) filter (where ys.year is not null) / count(*) r
                  from dd cross join n left join ys on ys.driver_id = dd.driver_id and ys.year = dd.f + n.n
                  where dd.f + n.n <= 2024 group by 1, 2 order by 1, 2""")
    decs = sorted(set(coh["cohort"]))
    cell_of = {(d, n): r for d, n, r in zip(coh["cohort"], coh["n"], coh["r"])}
    bands = rows("""with d as (select driver_id, count(*) filter (where status_group <> 'Did not start') s,
                               count(*) filter (where is_win) w from fact_results group by 1)
                    select case when w = 0 then 'No wins' when w / s < 0.05 then 'Under 5%' when w / s < 0.2 then '5–19%' else '20%+' end b,
                    count(*) n from d where s > 0 group by 1""")
    order = ["No wins", "Under 5%", "5–19%", "20%+"]
    bn = dict(zip(bands["b"], bands["n"]))
    tr = rows("""with t as (select year, constructor_id, case when season_rank <= 3 then 1 when season_rank <= 7 then 2 else 3 end b from team_season)
                 select a.b, coalesce(n.b, 4) nb, count(*) c from t a left join t n on n.constructor_id = a.constructor_id and n.year = a.year + 1
                 where a.year < (select max(year) from team_season) group by 1, 2""")
    lab = {1: "Top 3", 2: "Midfield 4–7", 3: "Back 8+", 4: "Left F1"}
    tm = {(a, b): c for a, b, c in zip(tr["b"], tr["nb"], tr["c"])}
    tr_rows = []
    for a in (1, 2, 3):
        row_tot = sum(tm.get((a, b), 0) for b in (1, 2, 3, 4))
        vals = [tm.get((a, b), 0) / row_tot for b in (1, 2, 3, 4)]
        tr_rows.append([lab[a]] + [{"v": pct(v, 0), "bg": mix(SEQ_LO, SEQ_HI, v)} for v in vals])
    stay = tm[(1, 1)] / sum(tm.get((1, b), 0) for b in (1, 2, 3, 4))
    return {
        "slicer": ("Era", "All"),
        "kpis": [("Period Counted", f"{ref} (default)"), ("Rookies", num(rook)), ("Returning Drivers", num(inp - rook)),
                 ("Churn Rate", pct(leave / inp)), ("Drivers for 80% of Wins", num(n80))],
        "rows": [
            [("Rookies vs returning drivers", "Drivers per season · rookie = first season that year · cards above: the period counted", 380,
              fig([dict(type="bar", name="Rookies", x=per["year"], y=per["rookies"], marker=dict(color=PALETTE[1]),
                        hovertemplate="%{x}: %{y} rookies<extra></extra>"),
                   dict(type="bar", name="Returning Drivers", x=per["year"], y=per["ret"], marker=dict(color=PALETTE[0]),
                        hovertemplate="%{x}: %{y} returning<extra></extra>")],
                  barmode="stack", bargap=0.15, showlegend=True, legend=LEGEND)),
             ("Pareto: who holds the wins",
              f"Top {top} winners · bars = wins · line = cumulative share · {n80} of {num(drivers)} drivers ({pct(n80 / drivers)}) take 80% of {num(tot)} wins", 460,
              fig([dict(type="bar", name="Wins", x=w["driver_name"][:top], y=w["w"][:top],
                        marker=dict(color=[F1_RED if i == n80 - 1 else PALETTE[0] for i in range(top)]),
                        hovertemplate="%{x}: %{y} wins<extra></extra>"),
                   dict(type="scatter", mode="lines", name="Cumulative Win Share", x=w["driver_name"][:top], y=cum[:top], yaxis="y2",
                        line=dict(color=F1_RED, width=2), hovertemplate="%{x}: %{y:.0%}<extra></extra>")],
                  xaxis=dict(showticklabels=False), yaxis2=dict(overlaying="y", side="right", tickformat=".0%", range=[0, 1.02],
                                                                tickvals=[0, .2, .4, .6, .8, 1], showgrid=False,
                                                                tickfont=dict(color=MUTED, size=10)),
                  # 80% reference on the cumulative axis + the bar where the line crosses it (twin only: PBIR has no Y2 constant line)
                  shapes=[dict(type="line", xref="paper", x0=0, x1=1, yref="y2", y0=.8, y1=.8, line=dict(color=MUTED, width=1, dash="dash"))],
                  annotations=[dict(x=w["driver_name"][n80 - 1], y=w["w"][n80 - 1], yref="y", text=f"#{n80}: 80%", showarrow=True,
                                    arrowhead=0, ax=0, ay=-28, font=dict(color=F1_RED, size=10))],
                  showlegend=True, legend=LEGEND, margin=dict(l=36, r=40, t=8, b=12))),
             ("ABC classes of constructors",
              f"Modern-scale points · A = first 70%, B = next 20%, C = last 10% · all eras: {classes['A']} A, {classes['B']} B, {classes['C']} C", 384,
              table(["Constructor", "Points (Modern)", "Cumulative", "Class"], abc_rows), "scroll")],
            [("Cohort retention: % still on the grid",
              "Row = debut decade · column = seasons after debut · ignores the era slicer", 428,
              table(["Debut decade"] + [str(n) for n in range(1, 9)],
                    [[d] + [{"v": pct(cell_of[(d, n)], 0), "bg": mix(SEQ_LO, SEQ_HI, cell_of[(d, n)])} if (d, n) in cell_of else ""
                            for n in range(1, 9)] for d in decs])),
             ("Churn per season", "Share of the season's drivers who never raced again · 2024 left out (leavers not known yet)", 250,
              fig([line("Churn Rate", churn["year"], churn["c"], PALETTE[0], ".1%")], yaxis=dict(tickformat=".0%"))),
             ("Drivers by win-rate band", "Disconnected 'Win Bands' table · follows the era slicer", 250,
              bars(order, [bn.get(b, 0) for b in order], horizontal=True)),
             ("Where teams land next season",
              f"Constructors' rank band in season N (row) → N+1 (column) · % of the row · Top 3 kept {pct(stay, 0)}", 284,
              table(["Season N", "Top 3", "Mid 4–7", "Back 8+", "Left F1"], tr_rows))],
        ]}


def page_why(team="Red Bull", year=2024):
    """Diagnosis for one team-season. Key influencers / decomposition tree / forecast are AI visuals: the twin shows
    the same data as plain bars and lines (DNF rate per factor vs the average; points by era → team; a linear projection)."""
    pts = rows(f"""select r.year, d.driver_name, sum(r.points_modern) p from fact_results r join dim_driver d using (driver_id)
                   join dim_constructor k using (constructor_id) where k.constructor_name = '{team}' and r.year in ({year - 1}, {year})
                   group by all""")
    cur = {n: p for y, n, p in zip(*pts.values()) if y == year}
    prev = {n: p for y, n, p in zip(*pts.values()) if y == year - 1}
    names = sorted(set(cur) | set(prev), key=lambda n: (cur.get(n, 0) - prev.get(n, 0)))
    delta = [cur.get(n, 0) - prev.get(n, 0) for n in names]
    tp = lambda y: f"""(select {{}} from fact_results r join dim_constructor k using (constructor_id)
                       where k.constructor_name = '{team}' and r.year = {y})"""  # noqa: E731
    dnf = [one(tp(y).format(RATE("is_dnf")))[0] for y in (year, year - 1)]
    grid = [one(tp(y).format("avg(grid)"))[0] for y in (year, year - 1)]
    fun = one(f"""select count(*), {STARTS}, count(*) filter (where is_finished), count(*) filter (where is_finished and is_points_finish),
                  count(*) filter (where is_finished and is_points_finish and is_podium),
                  count(*) filter (where is_finished and is_points_finish and is_podium and is_win)
                  from fact_results r join dim_constructor k using (constructor_id) where k.constructor_name = '{team}' and r.year = {year}""")
    stages = ["Entered", "Started", "Finished", "Scored points", "Podium", "Win"]
    avg_dnf = one(f"select {RATE('is_dnf')} from fact_results")[0]
    era = rows(f"select d.era k, {RATE('is_dnf')} v from fact_results r join dim_race d using (race_id) group by 1 order by 2 desc")
    gb = rows(f"select grid_band k, {RATE('is_dnf')} v from fact_results where grid_band <> 'Pit lane / none' group by 1 order by 2 desc")
    ki = [(f"Era: {k}", v) for k, v in zip(era["k"][:3], era["v"][:3])] + [(f"Grid: {k}", v) for k, v in zip(gb["k"][:2], gb["v"][:2])]
    dec = rows("""select s.era, sum(points_modern) p from fact_results r join dim_season s using (year) group by 1 order by 2 desc""")
    top_era = dec["era"][0]
    dt = rows(f"""select k.constructor_name, sum(points_modern) p from fact_results r join dim_season s using (year)
                  join dim_constructor k using (constructor_id) where s.era = '{top_era}' group by 1 order by 2 desc limit 5""")
    pit = rows("select year, median(pit_seconds) v from fact_pit_stops where is_routine_stop group by 1 order by 1")
    lap = rows('select year(race_date) as "year", avg(lap_seconds) v from fact_lap_times group by 1 order by 1')

    def forecast(d, name, fmt):
        a, b = ols(d["year"], d["v"])
        last = d["year"][-1]
        xs = [last, last + 1, last + 2, last + 3]
        return fig([line(name, d["year"], d["v"], PALETTE[0], fmt),
                    dict(type="scatter", mode="lines", name="Linear projection", x=xs, y=[d["v"][-1]] + [a + b * x for x in xs[1:]],
                         line=dict(color=F1_RED, width=2, dash="dot"), hovertemplate=f"%{{x}}: %{{y:{fmt}}}<extra></extra>")],
                   showlegend=True, legend=LEGEND), b

    pit_fig, pit_slope = forecast(pit, "Median Pit Lane Time", ".2f")
    lap_fig, _ = forecast(lap, "Avg Lap Time", ".1f")
    sign = lambda v, f: ("+" if v > 0 else "") + format(v, f)  # noqa: E731
    return {
        "slicer": [("Season", str(year)), ("Constructor", team)],
        "kpis": [("Points (Modern Scale)", num(sum(cur.values()))), ("Points Change vs PY", sign(sum(delta), ",.0f")),
                 ("DNF Rate Change vs PY", sign((dnf[0] - dnf[1]) * 100, ".1f") + "%"),
                 ("Avg Grid Change vs PY", sign(grid[0] - grid[1], ".1f"))],
        "rows": [
            [("What moved the team's points", f"Change in modern-scale points vs last season, per driver · {team} {year}: {sign(sum(delta), ',.0f')}", 400,
              fig([dict(type="waterfall", x=names + ["Total"], y=delta + [sum(delta)], measure=["relative"] * len(names) + ["total"],
                        decreasing=dict(marker=dict(color=F1_RED)), increasing=dict(marker=dict(color=PALETTE[0])),
                        totals=dict(marker=dict(color=MUTED)), connector=dict(line=dict(color=HAIR)),
                        texttemplate="%{y:+,.0f}", textposition="outside", textfont=dict(size=10, color=INK2), cliponaxis=False)],
                  yaxis=dict(visible=False), margin=dict(l=12, r=12, t=20, b=32))),
             ("What makes a DNF more likely", f"Key influencers (AI visual) · all seasons · DNF rate vs the {pct(avg_dnf, 0)} average · "
              f"twin: plain rates, not the model", 464,
              bars([k for k, _ in ki], [v for _, v in ki], fmt=".0%", horizontal=True,
                   **{"shapes": [dict(type="line", x0=avg_dnf, x1=avg_dnf, y0=-0.5, y1=len(ki) - 0.5, line=dict(color=MUTED, dash="dot"))],
                      "margin": dict(l=230, r=48, t=4, b=4)})),
             ("Race funnel", f"Entries → wins · {team} {year} · each stage inside the one before", 384,
              fig([dict(type="funnel", y=stages, x=list(fun), marker=dict(color=PALETTE[0]), textinfo="value+percent initial",
                        textfont=dict(color="#fff", size=11), connector=dict(fillcolor=HAIR))],
                  yaxis=dict(tickfont=dict(color=INK2, size=11)), margin=dict(l=100, r=12, t=4, b=4)))],
            [("Where the points came from", f"Decomposition tree (AI visual): era → constructor → driver · all seasons · "
              f"twin: top era, {top_era}, opened on its top 5 teams", 500,
              bars(dt["constructor_name"], dt["p"], horizontal=True)),
             ("Median pit lane time + forecast", f"Routine stops, 2011–2024 · LINESTX trend {pit_slope:+.3f} s per season · "
              f"twin draws a linear projection, Power BI's forecast is exponential smoothing", 368, pit_fig),
             ("Average lap time + forecast", "1996–2024 · calendar mix changes every year, read the level loosely · "
              "twin: linear projection", 356, lap_fig)],
        ]}


PAGES = [("grid", "Grid", "FORMULA 1 · THE GRID", "75 years of Formula 1 · 1950–2024 · every Grand Prix, driver, team and circuit", page_grid),
         ("dynasties", "Dynasties", "DYNASTIES", "Which teams ruled which era, and how hard they dominated", page_dynasties),
         ("legends", "Legends", "LEGENDS", "The greatest drivers by record, and by skill once the car is taken out", page_legends),
         ("race-craft", "Race Craft", "RACE CRAFT", "Grid position, reliability and circuits: what actually decides a Grand Prix", page_racecraft),
         ("title-fight", "Title Fight", "TITLE FIGHT", "Pick a season: how the championship swung round by round", page_titlefight),
         ("pit-wall", "Pit Wall", "PIT WALL", "Pick a race: positions lap by lap, pit stops and race control", page_pitwall),
         ("season-lens", "Season Lens", "SEASON LENS", "Pick a driver: a career season by season, and one season round by round", page_seasonlens),
         ("market", "Market", "MARKET", "The driver market: who holds the wins, who arrives, who stays, who leaves", page_market),
         ("why", "Why", "WHY", "Why did a team's points fall? Change vs last season, then the drivers behind it", page_why),
         ("driver-profile", "Driver Profile ↳", "DRIVER PROFILE",
          "Drill-through target (right-click any driver) · career, team-mate duels, links · sample: Lewis Hamilton",
          page_driver_profile)]

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Formula 1 Analytics</title>
<style>
:root{--plane:%(plane)s;--surface:%(surface)s;--hair:%(hair)s;--ink:%(ink)s;--ink2:%(ink2)s;--muted:%(muted)s;--red:%(red)s}
*{box-sizing:border-box}
body{margin:0;background:#0B0B0F;font-family:"Segoe UI",system-ui,-apple-system,sans-serif;color:var(--ink)}
.app{max-width:1600px;margin:0 auto;background:var(--plane);min-height:100vh;padding:20px 24px 24px;border-top:4px solid var(--red)}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;margin-bottom:16px}
header h1{margin:0;font-size:26px;font-weight:700;letter-spacing:.04em}
header p{margin:4px 0 0;color:var(--ink2);font-size:13px}
nav{display:flex;gap:6px;flex-wrap:wrap}
nav button{all:unset;padding:8px 14px;border-radius:6px;border:1px solid var(--hair);background:var(--surface);color:var(--ink2);font-size:13px;cursor:pointer}
nav button[aria-current=true]{background:var(--red);border-color:var(--red);color:#fff;font-weight:600}
.kpis{display:flex;gap:16px;margin-bottom:16px}
.kpi,.panel,.slicer{background:var(--surface);border:1px solid var(--hair);border-radius:8px}
.kpi{flex:1;padding:14px 16px;min-width:0}
.kpi span,.slicer span{font-size:12px;color:var(--muted)}
.kpi b{display:block;font-size:28px;font-weight:600;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpis.wide .kpi:first-child{flex:1.8}.kpi.txt b{font-size:15px;margin-top:12px}
.slicer{flex:0 0 300px;padding:14px 16px}.slicer.narrow{flex:0 0 210px}
th,td{white-space:nowrap}
.slicer b{display:flex;justify-content:space-between;margin-top:8px;padding:6px 10px;border:1px solid var(--hair);border-radius:6px;font-size:14px;font-weight:600}
.slicer b:after{content:"▾";color:var(--muted)}
.row{display:flex;gap:16px;margin-bottom:16px}
.side{flex:0 0 300px;display:flex;flex-direction:column;gap:8px}.side .slicer{flex:1 1 auto}
.btn{background:var(--surface);border:1px solid var(--hair);border-radius:8px;color:var(--ink2);font-size:13px;padding:7px 12px;text-align:center;white-space:nowrap}
.tabs{position:absolute;right:14px;top:10px;display:flex;gap:6px}.tabs span{font-size:11px;padding:4px 10px;border:1px solid var(--hair);border-radius:5px;color:var(--ink2);white-space:nowrap}
.tabs span.on{background:var(--hair);color:var(--ink);font-weight:600}
.info{display:inline-block;width:20px;height:20px;line-height:20px;text-align:center;border:1px solid var(--muted);border-radius:50%%;font-size:12px;color:var(--ink2);margin-left:10px;cursor:help;vertical-align:middle;font-weight:400;letter-spacing:0}
.back{display:inline-block;margin-right:10px;color:var(--ink2);cursor:pointer}
.logo{flex:1;display:flex;align-items:center;justify-content:center}.logo div{width:96px;height:96px;background:repeating-conic-gradient(#fff 0 25%%,#111116 0 50%%) 0 0/24px 24px;border-radius:4px}
.panel{padding:12px 14px 8px;min-width:0;display:flex;flex-direction:column;height:376px;position:relative}
.row:last-child .panel{height:400px}
.panel h3{margin:0;font-size:15px;font-weight:600}
.panel p{margin:2px 0 6px;font-size:12px;color:var(--muted)}
.chart{flex:1;min-height:0}
.scroll{overflow-y:auto}
table{width:100%%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
th{position:sticky;top:0;background:var(--surface);font-size:11px;color:var(--muted);font-weight:600;text-align:right;padding:7px 8px;border-bottom:1px solid var(--hair)}
td{text-align:right;padding:6px 8px;border-bottom:1px solid #24242C;color:var(--ink2);position:relative}
th:first-child,td:first-child,th.l,td.l{text-align:left}td:first-child{font-weight:600;color:var(--ink)}
td .bar{position:absolute;right:8px;top:5px;bottom:5px;background:%(bar)s;opacity:.35;border-radius:2px}
td .t{position:relative}
@media (max-width:1100px){.row,.kpis{flex-direction:column}.panel{width:100%%!important;flex:none!important}header{flex-direction:column}}
</style></head>
<body><div class="app">
<header><div id="head"></div><nav id="nav"></nav></header>
<main id="main"></main>
<p style="color:var(--muted);font-size:11px;margin:4px 0 0">Source: Ergast / Jolpica F1 archive 1950–2024 · Pipeline: Python · DuckDB SQL · Power BI · static preview of the Power BI report</p>
</div>
<script>%(plotly)s</script>
<script>
const PAGES = %(pages)s;
const nav = document.getElementById('nav'), main = document.getElementById('main'), head = document.getElementById('head');
function el(tag, attrs = {}, html = '') { const e = document.createElement(tag); Object.assign(e, attrs); e.innerHTML = html; return e; }
function cell(v, i) {
  if (v === null || typeof v !== 'object') return `<td>${v ?? ''}</td>`;
  const st = (v.bg ? `background:${v.bg};color:#fff;` : '') + (v.fg ? `color:${v.fg};font-weight:600;` : '');
  const bar = v.bar !== undefined ? `<span class="bar" style="width:calc(${(v.bar * 70).toFixed(1)}%% - 8px)"></span>` : '';
  return `<td style="${st}">${bar}<span class="t">${v.v}</span></td>`;
}
function render(id) {
  const page = PAGES.find(p => p.id === id);
  [...nav.children].forEach(b => b.setAttribute('aria-current', b.dataset.id === id));
  const info = page.help ? `<span class="info" title="${page.help.replace(/"/g, '&quot;')}">i</span>` : '';
  head.innerHTML = `<h1>${page.back ? '<span class="back" title="Back">←</span>' : ''}${page.title}${info}</h1><p>${page.subtitle}</p>`;
  main.innerHTML = '';
  const k = el('section', {className: 'kpis' + (page.wide ? ' wide' : '')});
  page.kpis.forEach(([l, v]) => k.append(el('div', {className: 'kpi' + (/[a-z]{3}/i.test(v) ? ' txt' : ''), title: v}, `<span>${l}</span><b>${v}</b>`)));
  const sl = page.slicer ? (Array.isArray(page.slicer[0]) ? page.slicer : [page.slicer]) : [];
  const box = page.buttons ? el('div', {className: 'side'}) : k;
  sl.forEach(([l, v]) => box.append(el('div', {className: 'slicer' + (sl.length > 1 ? ' narrow' : '')}, `<span>${l}</span><b>${v}</b>`)));
  (page.buttons || []).forEach(b => box.append(el('div', {className: 'btn'}, b)));
  if (box !== k) k.append(box);
  main.append(k);
  page.rows.forEach(row => {
    const r = el('section', {className: 'row'});
    row.forEach(([title, sub, width, body, cls, tabs]) => {
      const p = el('div', {className: 'panel'}, title ? `<h3>${title}</h3><p>${sub}</p>` : '');
      if (tabs) p.append(el('div', {className: 'tabs'}, tabs.map((t, i) => `<span class="${i ? '' : 'on'}">${t}</span>`).join('')));
      p.style.flex = `${width} 1 0`;
      const c = el('div', {className: 'chart' + (cls === 'scroll' ? ' scroll' : '')});
      p.append(c); r.append(p);
      if (body.logo) {
        c.className = 'logo'; c.innerHTML = '<div role="img" aria-label="Project logo: chequered flag"></div>';
      } else if (body.columns) {
        c.innerHTML = '<table><thead><tr>' + body.columns.map(h => `<th>${h}</th>`).join('') + '</tr></thead><tbody>' +
          body.rows.map(row => '<tr>' + row.map(cell).join('') + '</tr>').join('') + '</tbody></table>';
        const nonNum = new Set(body.rows[0].map((v, i) => i).filter(i => body.rows.some(r => typeof r[i] === 'string' && isNaN(parseFloat(r[i])) && r[i] !== '—' && r[i] !== '')));
        c.querySelectorAll('tr').forEach(tr => [...tr.children].forEach((td, i) => { if (nonNum.has(i)) td.classList.add('l'); }));
      } else {
        requestAnimationFrame(() => Plotly.newPlot(c, body.data, {...body.layout, autosize: true}, {displayModeBar: false, responsive: true}));
      }
    });
    main.append(r);
  });
  history.replaceState(null, '', '#' + id);
}
PAGES.forEach(p => { const b = el('button', {}, p.name); b.dataset.id = p.id; b.onclick = () => render(p.id); nav.append(b); });
render((location.hash || '#grid').slice(1));
</script></body></html>"""


def main():
    pages = []
    for pid, name, title, subtitle, build in PAGES:
        pages.append({"id": pid, "name": name, "title": title, "subtitle": subtitle, "help": HELP.get(name), **build()})
        print(f"  ✓ {name}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(HTML % {"plotly": get_plotlyjs(), "pages": json.dumps(pages, default=str), "plane": PLANE, "surface": SURFACE,
                           "hair": HAIR, "ink": INK, "ink2": INK2, "muted": MUTED, "red": F1_RED, "bar": PALETTE[0]})
    print(f"  ✓ wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
