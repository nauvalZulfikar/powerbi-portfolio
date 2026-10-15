"""Saved DAX queries for Power BI's DAX query view, each with the result it should return.

build_model.py writes QUERIES to F1.SemanticModel/DAXQueries/<tab>.dax (tab order in DAXQueries/.pbi/daxQueries.json).
The expected numbers in the comments are computed here from the warehouse with DuckDB, so a run in Desktop can be
checked against them (none of them is typed by hand).
"""
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
con = duckdb.connect(str(ROOT / "data" / "warehouse" / "f1.duckdb"), read_only=True)
one = lambda sql: con.execute(sql).fetchone()  # noqa: E731

POLE_PODIUMS, NONPOLE_PODIUMS = one("""select count(*) filter (where is_pole and is_podium),
                                              count(*) filter (where not is_pole and is_podium) from mart.fact_results""")
HAM = "(select driver_id from mart.dim_driver where driver_name = 'Lewis Hamilton')"
H20, H21 = one(f"""select sum(points_modern) filter (where year = 2020), sum(points_modern) filter (where year = 2021)
                   from mart.fact_results where driver_id = {HAM}""")
AS_DRIVER, AS_MATE = one(f"""select sum(points_delta) filter (where driver_id = {HAM}),
                                    sum(points_delta) filter (where teammate_id = {HAM}) from mart.teammate_h2h""")
BEST, SEASONS_WON, HOME, PER_RACE = one(f"""
    with r as (select f.*, d.country dc, c.country cc from mart.fact_results f join mart.dim_driver d using (driver_id)
               join mart.dim_circuit c using (circuit_id) where f.driver_id = {HAM})
    select (select max(p) from (select sum(points_modern) p from r group by year)),
           count(distinct year) filter (where is_win), count(*) filter (where is_win and dc = cc),
           (select avg(p) from (select sum(points_modern) p from r group by race_id)) from r""")

QUERIES = {
    "KEEPFILTERS vs replace": f"""// Podiums from pole, split by Results[Is Pole].
// The plain version REPLACES the Is Pole filter of each row, the KEEPFILTERS version intersects with it.
// Expected: TRUE row  -> Podiums {POLE_PODIUMS}, Podiums From Pole {POLE_PODIUMS}, (KF) {POLE_PODIUMS}
//           FALSE row -> Podiums {NONPOLE_PODIUMS}, Podiums From Pole {POLE_PODIUMS} (filter replaced), (KF) blank
EVALUATE
SUMMARIZECOLUMNS (
    Results[Is Pole],
    "Podiums", [Podiums],
    "Podiums From Pole", [Podiums From Pole],
    "Podiums From Pole (KF)", [Podiums From Pole (KF)]
)
""",
    "Season-to-date resets each year": f"""// Calendar-based time intelligence: 'F1 Calendar' tags Year / Month / Date, so TOTALYTD restarts every Year.
// Lewis Hamilton, 2020-2021, by month. Expected: December 2020 STD = {H20:.0f}, December 2021 STD = {H21:.0f};
// January 2021 is blank again (STD restarts; the season had not begun). Both STD columns agree.
EVALUATE
CALCULATETABLE (
    SUMMARIZECOLUMNS (
        'Calendar'[Year], 'Calendar'[Month Number], 'Calendar'[Month],
        "Points", [Points (Modern Scale)],
        "Points STD (Cal)", [Points STD (Cal)],
        "Points STD", [Points STD]
    ),
    Driver[Driver Name] = "Lewis Hamilton",
    'Calendar'[Year] IN {{ 2020, 2021 }}
)
ORDER BY 'Calendar'[Year], 'Calendar'[Month Number]
""",
    "Role-playing team-mate": f"""// Driver plays two roles in 'Teammate H2H': the driver (active relationship) and the team-mate (inactive,
// switched on by USERELATIONSHIP inside [Teammate Points Delta]).
// Expected for Lewis Hamilton: as driver {AS_DRIVER:+.0f} (his points minus his team-mates'),
// as team-mate {AS_MATE:+.0f} (the same pairs read from the other side).
EVALUATE
CALCULATETABLE (
    ROW (
        "Points Delta (as driver)", SUM ( 'Teammate H2H'[Points Delta] ),
        "Teammate Points Delta", [Teammate Points Delta]
    ),
    Driver[Driver Name] = "Lewis Hamilton"
)
""",
    "Season Lens check": f"""// The Season Lens KPIs for Lewis Hamilton. Expected: Best Season Points {BEST:.0f}, Seasons With a Win {SEASONS_WON},
// Home Wins {HOME}, Wins at Home Circuits {HOME} (one driver: same thing), Avg Points per Race {PER_RACE:.1f}.
EVALUATE
CALCULATETABLE (
    ROW (
        "Best Season Points", [Best Season Points],
        "Seasons With a Win", [Seasons With a Win],
        "Home Wins", [Home Wins],
        "Wins at Home Circuits", [Wins at Home Circuits],
        "Avg Points per Race", [Avg Points per Race]
    ),
    Driver[Driver Name] = "Lewis Hamilton"
)
""",
}

# pbi-kit CI tests (tools/pbi-kit/dax-tests/daxtest.py): one EVALUATE each, expected values in ASSERTS -> <tab>.assert.json.
RACES, STARTS, WINS, MODERN = one("""select count(distinct race_id), count(*) filter (where status_group <> 'Did not start'),
                                         count(*) filter (where is_win), sum(points_modern) from mart.fact_results""")
SEASONS = one("select count(*) from mart.dim_season")[0]
CHAMPIONS = {"Juan Fangio": 5, "Michael Schumacher": 7, "Lewis Hamilton": 7, "Max Verstappen": 4}  # same as sql/90_tests.sql

QUERIES |= {
    "ci-champions": "// pbi-kit CI test: Driver Titles Won for four known champions (same list as sql/90_tests.sql)\n"
                    "// plus one title per season. Expected values: ci-champions.assert.json.\nEVALUATE\nROW (\n"
                    + "".join(f'    "{n}", CALCULATE ( [Driver Titles Won], Driver[Driver Name] = "{n}" ),\n' for n in CHAMPIONS)
                    + '    "Titles Awarded", CALCULATE ( [Driver Titles Won], REMOVEFILTERS ( Driver ) ),\n'
                    + '    "Seasons", COUNTROWS ( Season )\n)\n',
    "ci-key-measures": """// pbi-kit CI test: headline measures vs the DuckDB marts (all-time, no filters).
EVALUATE
ROW (
    "Races", [Races],
    "Starts", [Starts],
    "Wins", [Wins],
    "Points (Modern Scale)", [Points (Modern Scale)]
)
""",
    "ci-data-integrity": """// pbi-kit CI test: every count must be 0 (mirrors sql/90_tests.sql inside the model).
EVALUATE
ROW (
    "Duplicate Result IDs", COUNTROWS ( Results ) - DISTINCTCOUNT ( Results[Result Id] ),
    "Results Without Driver", COUNTROWS ( FILTER ( Results, ISBLANK ( RELATED ( Driver[Driver Name] ) ) ) ),
    "Results Without Race", COUNTROWS ( FILTER ( Results, ISBLANK ( RELATED ( Race[Race Year] ) ) ) ),
    "Seasons Without Champion", COUNTROWS ( FILTER ( Season, ISBLANK ( Season[Champion] ) ) ),
    "Percentile Out Of Range", COUNTROWS ( FILTER ( Results, Results[Finish Percentile Value] < 0 || Results[Finish Percentile Value] > 1 ) )
)
""",
}


def _checks(values, tol=0):
    return {"rows": 1, "checks": [{"column": f"[{k}]", "equals": v, "tolerance": tol} for k, v in values.items()]}


ASSERTS = {
    "ci-champions": _checks(CHAMPIONS | {"Titles Awarded": SEASONS, "Seasons": SEASONS}),
    "ci-key-measures": _checks({"Races": RACES, "Starts": STARTS, "Wins": WINS, "Points (Modern Scale)": float(MODERN)}, 0.5),
    "ci-data-integrity": _checks({k: 0 for k in ("Duplicate Result IDs", "Results Without Driver", "Results Without Race",
                                                  "Seasons Without Champion", "Percentile Out Of Range")}),
}
