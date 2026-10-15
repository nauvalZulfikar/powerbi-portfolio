"""Statistical deep-dive on the F1 marts -> reports/insights.html

    uv run python src/analysis.py
"""
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
import statsmodels.formula.api as smf
from plotly.offline import get_plotlyjs
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parents[1]
con = duckdb.connect(str(ROOT / "data" / "warehouse" / "f1.duckdb"), read_only=True)
q = lambda sql: con.execute(sql).df()  # noqa: E731

# ------------------------------------------------------------------ design tokens (dark, validated palette)
PLANE, SURFACE, GRID, AXIS = "#111116", "#1c1c24", "#2c2c36", "#383845"
INK, INK2, MUTED = "#ffffff", "#c3c2b7", "#898781"
F1_RED = "#e10600"
BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN, VIOLET, RED = (
    "#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767")
SEQ = ["#1c1c24", "#184f95", "#256abf", "#3987e5", "#6da7ec", "#b7d3f6"]

pio.templates["f1"] = go.layout.Template(layout=dict(
    font=dict(family="system-ui, -apple-system, 'Segoe UI', sans-serif", size=13, color=INK2),
    paper_bgcolor=SURFACE, plot_bgcolor=SURFACE, colorway=[BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN, VIOLET, RED],
    margin=dict(l=56, r=24, t=16, b=44), hovermode="closest",
    hoverlabel=dict(bgcolor="#0d0d10", bordercolor=AXIS, font=dict(color=INK, size=12)),
    xaxis=dict(gridcolor=GRID, linecolor=AXIS, zeroline=False, tickfont=dict(color=MUTED), title_font=dict(color=MUTED)),
    yaxis=dict(gridcolor=GRID, linecolor=AXIS, zeroline=False, tickfont=dict(color=MUTED), title_font=dict(color=MUTED)),
    legend=dict(orientation="h", y=1.02, yanchor="bottom", x=0, font=dict(color=INK2), bgcolor="rgba(0,0,0,0)"),
))
pio.templates.default = "f1"

ERAS = [(1950, 1960, "Front-engine"), (1961, 1976, "Rear-engine & wings"), (1977, 1988, "Ground effect & turbo"),
        (1989, 2005, "V10"), (2006, 2013, "V8"), (2014, 2021, "Hybrid"), (2022, 2024, "Ground effect II")]


def era_bands(fig, label_y=1.0):
    for i, (a, b, name) in enumerate(ERAS):
        if i % 2:
            fig.add_vrect(x0=a - 0.5, x1=b + 0.5, fillcolor="#ffffff", opacity=0.025, line_width=0, layer="below")
        fig.add_annotation(x=(a + b) / 2, y=label_y, yref="paper", text=name, showarrow=False,
                           font=dict(size=10, color=MUTED), yanchor="bottom")


SECTIONS = []


def section(kicker, title, lede, fig=None, height=380, note=None, after=""):
    body = ""
    if fig is not None:
        fig.update_layout(height=height)
        body += pio.to_html(fig, include_plotlyjs=False, full_html=False, config={"displayModeBar": False, "responsive": True})
    SECTIONS.append(f"""
<section>
  <div class="kicker">{kicker}</div>
  <h2>{title}</h2>
  <p class="lede">{lede}</p>
  <div class="card">{body}</div>
  {after}
  {f'<p class="note">{note}</p>' if note else ''}
</section>""")


# ================================================================== headline numbers
k = q("""select (select count(*) from mart.dim_season) seasons, (select count(*) from mart.dim_race) races,
         (select count(*) from mart.dim_driver) drivers, (select count(*) from mart.dim_constructor) teams,
         (select count(*) from mart.dim_circuit) circuits, (select count(*) from mart.fact_lap_times) laps""").iloc[0]
tiles = "".join(f'<div class="tile"><div class="v">{v:,}</div><div class="l">{l}</div></div>' for v, l in [
    (k.seasons, "seasons"), (k.races, "Grands Prix"), (k.drivers, "drivers"), (k.teams, "constructors"),
    (k.circuits, "circuits"), (k.laps, "timed laps")])

# ================================================================== 1. dominance
dom = q("""select s.year, s.top_team_points_share as pts_share, s.champion, ts.constructor_name as best_team
           from mart.dim_season s
           join (select t.year, c.constructor_name from mart.team_season t join mart.dim_constructor c using (constructor_id)
                 qualify row_number() over (partition by t.year order by t.points_modern desc) = 1) ts using (year)
           order by year""")
fig = go.Figure(go.Scatter(x=dom.year, y=dom.pts_share, mode="lines+markers", line=dict(color=BLUE, width=2),
                           marker=dict(size=6, color=BLUE, line=dict(color=SURFACE, width=2)),
                           customdata=dom[["best_team", "champion"]],
                           hovertemplate="<b>%{x}</b><br>%{customdata[0]}: %{y:.0%} of points<br>Champion: %{customdata[1]}<extra></extra>"))
for yr, txt in [(1952, "Ferrari"), (1988, "McLaren-Honda<br>15 of 16 wins"), (2002, "Ferrari"), (2016, "Mercedes"), (2023, "Red Bull<br>21 of 22 wins")]:
    r = dom[dom.year == yr].iloc[0]
    fig.add_annotation(x=yr, y=r.pts_share, text=txt, showarrow=True, arrowhead=0, arrowcolor=AXIS, ax=0, ay=-34,
                       font=dict(size=11, color=INK), align="center")
era_bands(fig)
fig.update_yaxes(tickformat=".0%", range=[0.1, 0.52], title="best team's share of points")
fig.update_layout(margin=dict(t=36))
dec = dom.assign(decade=dom.year // 10 * 10).groupby("decade").pts_share.mean()
section("Dynasties", "Dominance bottomed out in the 1970s and climbed back for forty years",
        f"The best team's share of each season's points, re-scored on today's 25-18-15 system so every year is comparable. "
        f"The average fell from {dec[1950]:.0%} in the 1950s to {dec[1970]:.0%} in the 1970s, then rose decade by decade to "
        f"{dec[2010]:.0%} in the 2010s ({dec[2020]:.0%} so far in the 2020s). Each new rulebook resets the order, and within a few seasons one team runs away with it.", fig,
        note="1950s shares run high partly because works teams entered four or five cars in a field of twenty.")

# ================================================================== 2. grid vs win, logistic model
g = q("""select fr.year, grid, is_win::int win, is_podium::int podium from mart.fact_results fr join mart.dim_race r using (race_id)
         where grid between 1 and 20 and not r.is_indy500""")
g["modern"] = (g.year >= 1989).astype(int)
g["log_grid"] = np.log(g.grid)
logit = smf.logit("win ~ log_grid * modern", data=g).fit(disp=0)
slots = pd.DataFrame({"grid": np.arange(1, 21)})
curves = []
for m, name, color in [(0, "1950–1988", ORANGE), (1, "1989–2024", BLUE)]:
    d = slots.assign(log_grid=np.log(slots.grid), modern=m)
    pred = logit.get_prediction(d).summary_frame(alpha=0.05)
    obs = g[g.modern == m].groupby("grid").win.mean()
    curves.append((name, color, d.grid, pred, obs))
fig = go.Figure()
for name, color, x, pred, obs in curves:
    fig.add_trace(go.Scatter(x=list(x) + list(x[::-1]), y=list(pred.ci_upper) + list(pred.ci_lower[::-1]), fill="toself",
                             fillcolor=color, opacity=0.15, line=dict(width=0), hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(x=x, y=pred.predicted, mode="lines", name=f"{name} model", line=dict(color=color, width=2),
                             hovertemplate="P%{x}: %{y:.1%} modelled<extra>" + name + "</extra>"))
    fig.add_trace(go.Scatter(x=obs.index, y=obs.values, mode="markers", name=f"{name} observed",
                             marker=dict(color=color, size=8, line=dict(color=SURFACE, width=2)),
                             hovertemplate="P%{x}: %{y:.1%} observed<extra>" + name + "</extra>"))
fig.update_xaxes(title="starting position", dtick=1)
fig.update_yaxes(title="probability of winning", tickformat=".0%")
p_modern = logit.pvalues["log_grid:modern"]
pole_old, pole_new = curves[0][4].loc[1], curves[1][4].loc[1]
p5_old, p5_new = curves[0][4].loc[5], curves[1][4].loc[5]
section("Race craft", "Qualifying has become destiny",
        f"Pole position wins {pole_new:.0%} of modern races (1989+) against {pole_old:.0%} before. The drop-off behind is steeper too: "
        f"P5 won {p5_old:.1%} of the time before 1989 and only {p5_new:.1%} since. A logistic model, win ~ log(grid) × era, "
        f"puts the era difference in slope at p = {p_modern:.1g}. Lines show the model with 95% intervals, dots show observed rates.", fig, 400)

# ================================================================== 3. predictability: spearman per season
base = q("""select fr.year, fr.race_id, fr.grid, fr.position_order, fr.finish_position from mart.fact_results fr
            join mart.dim_race r using (race_id)
            where fr.grid is not null and fr.status_group <> 'Did not start' and not r.is_indy500""")


def season_rho(df, col):
    return pd.Series({yr: np.nanmean([spearmanr(x.grid, x[col]).statistic for _, x in d.groupby("race_id") if len(x) >= 6])
                      for yr, d in df.groupby("year")})


rho = pd.DataFrame({"all": season_rho(base, "position_order"),
                    "finishers": season_rho(base.dropna(subset=["finish_position"]), "finish_position")})
dec_rho = rho.groupby(rho.index // 10 * 10).mean()
fig = go.Figure()
for col, name, color in [("all", "every starter (retirements ranked last)", BLUE), ("finishers", "classified finishers only", ORANGE)]:
    fig.add_trace(go.Scatter(x=rho.index, y=rho[col], mode="markers", marker=dict(color=color, size=6, opacity=0.4),
                             showlegend=False, hovertemplate="%{x}: ρ = %{y:.2f}<extra>" + name + "</extra>"))
    fig.add_trace(go.Scatter(x=rho.index, y=rho[col].rolling(5, center=True, min_periods=3).mean(), mode="lines", name=name,
                             line=dict(color=color, width=2), hoverinfo="skip"))
era_bands(fig)
fig.update_yaxes(title="grid ↔ result rank correlation (ρ)", range=[0.1, 0.95])
fig.update_layout(margin=dict(t=36), legend=dict(y=-0.2, yanchor="top"))
section("Race craft", "Races are twice as predictable as in the 1980s, because cars stopped breaking",
        f"Average Spearman correlation between starting and finishing order, per race. Counting every starter, it rose from "
        f"{dec_rho.loc[1980, 'all']:.2f} in the 1980s to {dec_rho.loc[2020, 'all']:.2f} in the 2020s. Among cars that finished, it barely "
        f"moved ({dec_rho.loc[1980, 'finishers']:.2f} → {dec_rho.loc[2020, 'finishers']:.2f}). So the grid order always held for the "
        "cars that survived; what changed is that almost all of them now survive.", fig, 400)

# ================================================================== 4. reliability revolution
out = q("""select decade, status_group, count(*) n from mart.fact_results fr join mart.dim_season s using (year)
           where status_group <> 'Did not start' group by 1, 2""")
out["share"] = out.n / out.groupby("decade").n.transform("sum")
order = [("Finished", BLUE), ("Finished (lapped)", AQUA), ("Mechanical failure", ORANGE), ("Crash / driver", YELLOW),
         ("Retired (other)", VIOLET), ("Disqualified", MAGENTA)]
fig = go.Figure([go.Bar(x=d.decade, y=d.share, name=s, marker=dict(color=c, line=dict(color=SURFACE, width=2)),
                        hovertemplate="%{x}: %{y:.0%}<extra>" + s + "</extra>")
                 for s, c in order if len(d := out[out.status_group == s].sort_values("decade"))])
fig.update_layout(barmode="stack", bargap=0.18)
fig.update_yaxes(tickformat=".0%", title="share of starters")
mech = out[out.status_group == "Mechanical failure"].set_index("decade").share
section("Reliability", "The car used to be the biggest opponent",
        f"In the {mech.idxmax()}, {mech.max():.0%} of starters retired with a mechanical failure. In the 2020s it is "
        f"{mech.get('2020s', 0):.0%}. Finishing on the lead lap went from the exception to the rule.", fig, 380)

# ================================================================== 5. car-adjusted skill
sk = q("""select d.driver_name, d.titles, s.skill, s.skill_se, s.races_modelled, d.first_season, d.last_season
          from mart.driver_skill s join mart.dim_driver d using (driver_id)
          where s.is_ranked order by s.skill desc limit 25""").iloc[::-1]
fig = go.Figure(go.Scatter(
    x=sk.skill, y=sk.driver_name, mode="markers",
    error_x=dict(type="data", array=1.96 * sk.skill_se, color=AXIS, thickness=2, width=0),
    marker=dict(size=10, color=[BLUE if t else MUTED for t in sk.titles], line=dict(color=SURFACE, width=2)),
    customdata=sk[["titles", "races_modelled", "first_season", "last_season"]],
    hovertemplate="<b>%{y}</b><br>skill %{x:+.3f}<br>%{customdata[0]} titles · %{customdata[1]} races (%{customdata[2]}–%{customdata[3]})<extra></extra>"))
fig.add_vline(x=0, line=dict(color=AXIS, width=1))
fig.update_xaxes(title="finish-percentile points above an average driver in the same car")
fig.update_yaxes(tickfont=dict(color=INK2, size=12), gridcolor="rgba(0,0,0,0)")
fig.update_layout(margin=dict(l=150))
top = sk.iloc[::-1].iloc[:5].driver_name.tolist()
section("Legends", "Take the car away and the 1960s greats rise to the top",
        "A fixed-effects regression, finish percentile ~ driver + team-season, over 17,745 results. The team-season term absorbs "
        "how good that year's car was, so a driver scores only by beating team-mates who in turn beat theirs. "
        f"Top five: {', '.join(top)}. Blue = world champion, grey = never won the title. Whiskers show ±1.96 standard errors.",
        fig, 720,
        note="Caveat: early-era fields were small and unreliable, which widens the spread of finishing percentiles, so read cross-era gaps as indicative. "
             "Within an era the ranking is robust; only drivers with 40+ modelled races are ranked.")

# ================================================================== 6. title fights
tf = q("""select s.year, d.driver_name, st.round, st.points
          from mart.fact_standings st join mart.dim_driver d using (driver_id) join mart.dim_season s using (year)
          where s.year in (2008, 2021) and st.driver_id in (
              select driver_id from mart.fact_standings x where x.year = s.year and x.round = s.races and x.position <= 2)
          order by 1, 3""")
from plotly.subplots import make_subplots
fig = make_subplots(rows=1, cols=2, subplot_titles=["2008 · decided by 1 point", "2021 · level on points before the finale"],
                    horizontal_spacing=0.08)
for col, yr, pair in [(1, 2008, [("Lewis Hamilton", BLUE), ("Felipe Massa", ORANGE)]),
                      (2, 2021, [("Lewis Hamilton", BLUE), ("Max Verstappen", AQUA)])]:
    for name, color in pair:
        d = tf[(tf.year == yr) & (tf.driver_name == name)]
        fig.add_trace(go.Scatter(x=d["round"], y=d.points, mode="lines", name=name, legendgroup=name, showlegend=col == 2 or name != "Lewis Hamilton",
                                 line=dict(color=color, width=2), hovertemplate="Round %{x}: %{y} pts<extra>" + f"{yr} · {name}" + "</extra>"),
                      row=1, col=col)
        last = d.iloc[-1]
        rival = tf[(tf.year == yr) & (tf.driver_name != name) & (tf["round"] == last["round"])].points.max()
        fig.add_annotation(x=last["round"], y=last.points, text=f"{name.split()[-1]} {last.points:g}", showarrow=False,
                           xanchor="left", xshift=6, yshift=9 if last.points >= rival else -9,
                           font=dict(size=11, color=INK2), row=1, col=col)
fig.update_annotations(selector=dict(text="2008 · decided by 1 point"), font=dict(color=INK, size=13))
fig.update_annotations(selector=dict(text="2021 · level on points before the finale"), font=dict(color=INK, size=13))
fig.update_layout(showlegend=False, margin=dict(t=40, r=110))
fig.update_xaxes(dtick=2, gridcolor=GRID, linecolor=AXIS, tickfont=dict(color=MUTED))
fig.update_yaxes(gridcolor=GRID, linecolor=AXIS, tickfont=dict(color=MUTED))
fig.update_yaxes(title="championship points", col=1)
fig.update_xaxes(range=[0.5, 18.5], col=1)
fig.update_xaxes(range=[0.5, 22.5], col=2)
margins = q("select year, champion, runner_up, title_margin from mart.dim_season order by title_margin, year limit 6")
rows = "".join(f"<tr><td>{r.year}</td><td>{r.champion}</td><td>{r.runner_up}</td><td class='num'>{r.title_margin:g}</td></tr>"
               for r in margins.itertuples())
section("Title fights", "Two of the closest finishes ever",
        "Cumulative points after each round, each season on its own scale. Hamilton took 2008 by one point from Massa; "
        "in 2021 he and Verstappen arrived at the final round on exactly 369.5 points each. Below: the six narrowest title margins ever.", fig, 400,
        after=f"""<div class="card table"><table><thead><tr><th>Season</th><th>Champion</th><th>Runner-up</th>
  <th class='num'>Margin (pts)</th></tr></thead><tbody>{rows}</tbody></table></div>""")

# ================================================================== 7. pit lane
pit = q("""select year, quantile_cont(pit_seconds, 0.25) q1, median(pit_seconds) med, quantile_cont(pit_seconds, 0.75) q3
           from mart.fact_pit_stops where is_routine_stop group by 1 order by 1""")
fig = go.Figure([
    go.Scatter(x=list(pit.year) + list(pit.year[::-1]), y=list(pit.q3) + list(pit.q1[::-1]), fill="toself", fillcolor=BLUE,
               opacity=0.18, line=dict(width=0), name="middle 50% of stops", hoverinfo="skip"),
    go.Scatter(x=pit.year, y=pit.med, mode="lines+markers", name="median", line=dict(color=BLUE, width=2),
               marker=dict(size=7, line=dict(color=SURFACE, width=2)), hovertemplate="%{x}: %{y:.1f} s median<extra></extra>"),
])
fig.update_yaxes(title="pit-lane time, entry to exit (s)")
fig.update_xaxes(dtick=1)
section("Pit wall", "Faster crews, same pit lane",
        "Median time from pit entry to exit has stayed between 22 and 25 seconds every year since 2011, even as crews got "
        "faster at changing tyres. The stationary stop is a small slice of the total: most of it is driving the pit lane at the speed limit.", fig, 340)

# ================================================================== 8. teammates
h2h = q("""select d.driver_name, t.driver_name teammate, h.year, h.quali_h2h_rate, h.race_h2h_rate, h.races_together
           from mart.teammate_h2h h join mart.dim_driver d using (driver_id) join mart.dim_driver t on t.driver_id = h.teammate_id
           where (d.driver_name, t.driver_name, h.year) in (
               ('Ayrton Senna', 'Alain Prost', 1988), ('Ayrton Senna', 'Alain Prost', 1989),
               ('Lewis Hamilton', 'Fernando Alonso', 2007), ('Lewis Hamilton', 'Nico Rosberg', 2016),
               ('Max Verstappen', 'Daniel Ricciardo', 2018), ('Charles Leclerc', 'Sebastian Vettel', 2019),
               ('Lando Norris', 'Oscar Piastri', 2024), ('George Russell', 'Lewis Hamilton', 2024))
           order by h.year""")
h2h["pair"] = h2h.year.astype(str) + " · " + h2h.driver_name + " vs " + h2h.teammate
fig = go.Figure([
    go.Bar(y=h2h.pair, x=h2h[col] - 0.5, base=0.5, orientation="h", name=name,
           marker=dict(color=color, line=dict(color=SURFACE, width=2)),
           text=[f"{v:.0%}" for v in h2h[col]], textposition="outside", textfont=dict(color=INK2, size=11), cliponaxis=False,
           hovertemplate="%{y}: %{text}<extra>" + name + "</extra>")
    for col, name, color in [("quali_h2h_rate", "started ahead", BLUE), ("race_h2h_rate", "finished ahead", ORANGE)]
])
fig.add_vline(x=0.5, line=dict(color=AXIS, width=1))
fig.update_layout(barmode="group", bargap=0.3, margin=dict(l=280))
fig.update_xaxes(tickformat=".0%", range=[0, 1], title="share of head-to-heads won (50% = dead even)")
fig.update_yaxes(tickfont=dict(color=INK2, size=12), gridcolor="rgba(0,0,0,0)")
section("Team-mates", "The only fair fight is against the other car in the garage",
        "Share of races a driver started ahead of their team-mate (grid, after penalties) and finished ahead (mechanical retirements excluded). "
        "Bars start from the 50% line, so the length is the size of the edge.", fig, 440)

# ================================================================== page
html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Formula 1, 1950–2024 · Insights</title>
<script>{get_plotlyjs()}</script>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; background: {PLANE}; color: {INK2}; font: 15px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ max-width: 1040px; margin: 0 auto; padding: 0 16px 80px; }}
  header {{ padding: 56px 0 24px; border-bottom: 1px solid {GRID}; }}
  .brand {{ display: flex; align-items: center; gap: 10px; color: {INK}; font-weight: 700; letter-spacing: .14em; font-size: 12px; }}
  .brand::before {{ content: ""; width: 28px; height: 4px; background: {F1_RED}; border-radius: 2px; }}
  h1 {{ color: {INK}; font-size: clamp(30px, 5vw, 46px); line-height: 1.1; margin: 14px 0 12px; letter-spacing: -.01em; }}
  header p {{ max-width: 720px; margin: 0; }}
  .tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin: 28px 0 8px; }}
  .tile {{ background: {SURFACE}; border: 1px solid {GRID}; border-radius: 10px; padding: 14px 16px; }}
  .tile .v {{ color: {INK}; font-size: 28px; font-weight: 650; line-height: 1.1; }}
  .tile .l {{ color: {MUTED}; font-size: 12px; text-transform: uppercase; letter-spacing: .08em; margin-top: 4px; }}
  section {{ padding: 48px 0 0; }}
  .kicker {{ color: {F1_RED}; font-size: 12px; font-weight: 700; letter-spacing: .14em; text-transform: uppercase; }}
  h2 {{ color: {INK}; font-size: clamp(22px, 3vw, 28px); line-height: 1.2; margin: 6px 0 8px; }}
  .lede {{ max-width: 760px; margin: 0 0 16px; }}
  .card {{ background: {SURFACE}; border: 1px solid {GRID}; border-radius: 12px; padding: 12px; overflow-x: auto; }}
  .card.table {{ margin-top: 12px; padding: 4px 16px; }}
  .note {{ color: {MUTED}; font-size: 13px; max-width: 760px; }}
  table {{ width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }}
  th, td {{ text-align: left; padding: 9px 8px; border-bottom: 1px solid {GRID}; }}
  th {{ color: {MUTED}; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }}
  td {{ color: {INK2}; }} tr:last-child td {{ border-bottom: 0; }} .num {{ text-align: right; }}
  footer {{ margin-top: 56px; color: {MUTED}; font-size: 13px; border-top: 1px solid {GRID}; padding-top: 16px; }}
  code {{ color: {INK2}; }}
</style></head><body><main>
<header>
  <div class="brand">FORMULA 1 · DATA DEEP-DIVE</div>
  <h1>75 years of Formula 1, measured</h1>
  <p>Every Grand Prix from Silverstone 1950 to Abu Dhabi 2024, rebuilt as a warehouse with SQL, modelled in Python, and served to Power BI.
     Eight findings, each tested against the data.</p>
  <div class="tiles">{tiles}</div>
</header>
{''.join(SECTIONS)}
<footer>Data: <a style="color:{INK2}" href="https://www.kaggle.com/datasets/rohanrao/formula-1-world-championship-1950-2020">Formula 1 World Championship (1950–2024)</a>, Kaggle, sourced from the Ergast API.
Pipeline: DuckDB SQL marts → statsmodels / scipy → Plotly. Points compared across eras use today's 25-18-15-12-10-8-6-4-2-1 scale.</footer>
</main></body></html>"""
out_path = ROOT / "reports" / "insights.html"
out_path.parent.mkdir(exist_ok=True)
out_path.write_text(html)
print(f"→ {out_path.relative_to(ROOT)}  ({len(SECTIONS)} sections, {out_path.stat().st_size / 1024:.0f} KB)")
