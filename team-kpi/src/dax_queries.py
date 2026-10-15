"""Saved DAX queries for Power BI's DAX query view, plus the result each one should return.

build_model.py writes QUERIES to TeamKPI.SemanticModel/DAXQueries/<tab>.dax (one file per query-view tab,
tab order in DAXQueries/.pbi/daxQueries.json). build_preview.py shows the same code next to expected(),
computed here from data/marts with DuckDB and from the generated TMDL, so each tab can be checked against it.
"""
import re
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
MARTS = ROOT / "data" / "marts"
TMDL = ROOT / "powerbi" / "TeamKPI.SemanticModel" / "definition" / "tables"

con = duckdb.connect()
for t in ("fact_ticket", "dim_team", "dim_priority"):
    con.execute(f"create view {t} as select * from '{MARTS / t}.parquet'")
con.execute("""create view t as select f.*, tm.team, p.resolution_days target_d from fact_ticket f
               join dim_team tm using (team_key) join dim_priority p using (priority_key)""")
# Same rules as the DAX (and preview_pages.py) with the SLA Scale what-if at its default, 100%.
DELIVERED, OPEN, RES_MET = "outcome = 'Delivered'", "resolved_at is null", "lead_days <= target_d"
MEASURES = f"""count(*), count(resolved_at), count(*) filter (where {DELIVERED}),
               median(lead_days) filter (where {DELIVERED}),
               count(*) filter (where resolved_at is not null and {RES_MET}) / count(resolved_at),
               count(*) filter (where {OPEN}), count(*) filter (where {OPEN} and age_days > target_d)"""
CREATED, RESOLVED, DELIV, LEAD, SLA, OPEN_N, BREACHED = con.execute(f"select {MEASURES} from t").fetchone()
LEAD, SLA = round(LEAD, 4), round(SLA, 6)

QUERIES = {
    "Delivery by team": """// Report query: one row per team with the flow, speed, SLA and workload headline measures.
EVALUATE
SUMMARIZECOLUMNS (
    Teams[Team],
    "Created", [Created],
    "Resolved", [Resolved],
    "Delivered", [Delivered],
    "Median Lead Time", [Median Lead Time],
    "Resolution SLA %", [Resolution SLA %],
    "Open Tickets", [Open Tickets],
    "Open Breached", [Open Breached]
)
ORDER BY [Delivered] DESC
""",
    "Test key measures": f"""// Test: key measures against totals computed from data/marts with DuckDB
// (src/dax_queries.py writes the expected values in at build time). Every row should say PASS.
DEFINE
    MEASURE '_Measures'[Expected Created] = {CREATED}
    MEASURE '_Measures'[Expected Resolved] = {RESOLVED}
    MEASURE '_Measures'[Expected Delivered] = {DELIV}
    MEASURE '_Measures'[Expected Median Lead Time] = {LEAD}
    MEASURE '_Measures'[Expected Resolution SLA %] = {SLA}
    MEASURE '_Measures'[Expected Open Tickets] = {OPEN_N}
    MEASURE '_Measures'[Expected Open Breached] = {BREACHED}
    VAR _checks =
        UNION (
            ROW ( "Check", "Created", "Actual", [Created], "Expected", [Expected Created] ),
            ROW ( "Check", "Resolved", "Actual", [Resolved], "Expected", [Expected Resolved] ),
            ROW ( "Check", "Delivered", "Actual", [Delivered], "Expected", [Expected Delivered] ),
            ROW ( "Check", "Median Lead Time", "Actual", [Median Lead Time], "Expected", [Expected Median Lead Time] ),
            ROW ( "Check", "Resolution SLA %", "Actual", [Resolution SLA %], "Expected", [Expected Resolution SLA %] ),
            ROW ( "Check", "Open Tickets", "Actual", [Open Tickets], "Expected", [Expected Open Tickets] ),
            ROW ( "Check", "Open Breached", "Actual", [Open Breached], "Expected", [Expected Open Breached] )
        )
EVALUATE
ADDCOLUMNS ( _checks, "Result", IF ( ABS ( [Actual] - [Expected] ) < 0.001, "PASS", "FAIL" ) )
""",
    "KEEPFILTERS": """// 6-s3 / 9-s2: a filter argument on a column REPLACES the filter already on that column;
// KEEPFILTERS INTERSECTS with it. One row per priority, so the priority filter comes from the row:
//   replaces: every row shows the P1 figure (the row filter is overwritten)
//   KEEPFILTERS: only the P1 row has a value (P2 AND P1 is empty) - that is [P1 Resolution SLA %]
DEFINE
    MEASURE '_Measures'[P1 SLA replaces filter] = CALCULATE ( [Resolution SLA %], Priorities[Priority Key] = 1 )
EVALUATE
SUMMARIZECOLUMNS (
    Priorities[Priority],
    "Resolution SLA %", [Resolution SLA %],
    "P1 SLA replaces filter", [P1 SLA replaces filter],
    "P1 SLA with KEEPFILTERS", [P1 Resolution SLA %]
)
ORDER BY Priorities[Priority]
""",
    "Expanded table": """// 9-s1: a Tickets row "contains" every column on the one side of its relationships (the expanded
// table), so RELATED works inside FILTER ( Tickets, ... ) and a filter on Priorities reaches Tickets.
// All three numbers are as of 30 Sep 2026 with the SLA Scale at 100%.
EVALUATE
VAR _open = CALCULATETABLE ( Tickets, ISBLANK ( Tickets[Resolved At] ) )
RETURN
    ROW (
        "Open Breached (measure)", [Open Breached],
        "FILTER over Tickets with RELATED", COUNTROWS ( FILTER ( _open, Tickets[Age Days] > RELATED ( Priorities[Resolution Target Days] ) ) ),
        "Open P1 via a filter on Priorities", COUNTROWS ( CALCULATETABLE ( _open, Priorities[Priority Key] = 1 ) )
    )
""",
    "CLS test": """// 11-s14: object-level security test. Run it twice in DAX query view:
//   1. as yourself: five rows come back.
//   2. Modeling > View as > Stakeholder, then run again: it must FAIL with an error that
//      'Ticket Text'[Summary] can't be found (the column is secured for that role).
// The same goes for any query that touches the People table under Stakeholder.
EVALUATE
TOPN ( 5, SELECTCOLUMNS ( 'Ticket Text', "Ticket", 'Ticket Text'[Ticket Key], "Summary", 'Ticket Text'[Summary] ), [Ticket], ASC )
ORDER BY [Ticket]
""",
    "Model measures": """// Model documentation: every measure with its home table, folder and description.
EVALUATE
SELECTCOLUMNS (
    INFO.VIEW.MEASURES (),
    "Table", [Table],
    "Measure", [Name],
    "Folder", [DisplayFolder],
    "Description", [Description]
)
ORDER BY [Table], [Folder], [Measure]
""",
    "Model tables": """// Model documentation: tables, how many measures each hosts, and their data category.
EVALUATE
VAR _measures = SELECTCOLUMNS ( INFO.VIEW.MEASURES (), "Home", [Table] )
RETURN
    ADDCOLUMNS (
        SELECTCOLUMNS ( INFO.VIEW.TABLES (), "Table", [Name], "Category", [DataCategory], "Hidden", [IsHidden] ),
        "Measures", VAR _t = [Table] RETURN COUNTROWS ( FILTER ( _measures, [Home] = _t ) ) + 0
    )
ORDER BY [Measures] DESC, [Table]
""",
}


# pbi-kit DAX tests (tools/pbi-kit/dax-tests/daxtest.py): a sibling <tab>.assert.json turns a saved query into a
# value test that CI runs online against the Test workspace. Expected values are the DuckDB numbers above, so the
# model is asserted against the same marts the SQL tests check (Open Tickets = backlog on 30 Sep, sql/90_tests.sql).
_KEY = [("Created", CREATED, 0), ("Resolved", RESOLVED, 0), ("Delivered", DELIV, 0), ("Median Lead Time", LEAD, 0.001),
        ("Resolution SLA %", SLA, 0.001), ("Open Tickets", OPEN_N, 0), ("Open Breached", BREACHED, 0)]
ASSERTS = {
    "Test key measures": {"rows": len(_KEY), "checks":
        [c for i, (name, v, tol) in enumerate(_KEY) for c in (
            {"column": "[Check]", "row": i, "equals": name},
            {"column": "[Actual]", "row": i, "equals": v, "tolerance": tol},
            {"column": "[Result]", "row": i, "equals": "PASS"})]},
    "Expanded table": {"rows": 1, "checks": [
        {"column": "[Open Breached (measure)]", "row": 0, "equals": BREACHED},
        {"column": "[FILTER over Tickets with RELATED]", "row": 0, "equals": BREACHED}]},
}


def _tmdl():
    """(table, data category, hidden, [(measure, folder, description)]) from the generated TMDL."""
    out = []
    for f in sorted(TMDL.glob("*.tmdl")):
        lines = f.read_text().split("\n")
        name = lines[0].split(" ", 1)[1].strip("'")
        cat = next((l.split(": ", 1)[1] for l in lines if l.startswith("\tdataCategory:")), "")
        measures, desc, cur = [], "", None
        for l in lines:
            if l.startswith("\t///"):
                desc = l[5:].strip()
            elif m := re.match(r"\tmeasure ('[^']+'|\S+) =", l):
                cur = [m.group(1).strip("'"), "", desc]
                measures.append(cur)
                desc = ""
            elif cur and l.startswith("\t\tdisplayFolder:"):
                cur[1] = l.split(": ", 1)[1]
            elif l.startswith("\t") and not l.startswith("\t\t"):
                cur, desc = None, ""
        out.append((name, cat, "\tisHidden" in lines, measures))
    return out


def expected():
    """{tab: (columns, rows)}: the result each saved query should return in Power BI."""
    team = con.execute(f"select team, {MEASURES} from t group by 1 order by 4 desc").fetchall()
    tm = _tmdl()
    n = lambda v: f"{v:,}"
    prio = con.execute(f"""select priority_key, priority, count(*) filter (where {RES_MET}) / count(*) from t
                           join dim_priority using (priority_key) where resolved_at is not null group by all order by 2""").fetchall()
    p1 = next(s for k, _, s in prio if k == 1)
    open_p1 = con.execute(f"select count(*) from t where {OPEN} and priority_key = 1").fetchone()[0]
    cls = con.execute("""select ticket_key, trim(regexp_replace(summary, '[[:cntrl:]]', '', 'g')) from fact_ticket
                         order by lower(ticket_key) limit 5""").fetchall()
    checks = [("Created", n(CREATED)), ("Resolved", n(RESOLVED)), ("Delivered", n(DELIV)),
              ("Median Lead Time", f"{LEAD:.4f}"), ("Resolution SLA %", f"{SLA:.4f}"), ("Open Tickets", n(OPEN_N)),
              ("Open Breached", n(BREACHED))]
    meas = sorted(((t, f, m, d) for t, _, _, ms in tm for m, f, d in ms), key=lambda r: [x.lower() for x in r[:3]])
    tabs = sorted(((t, c, str(h).upper(), len(ms)) for t, c, h, ms in tm), key=lambda r: (-r[3], r[0].lower()))
    return {
        "Delivery by team": (["Teams[Team]", "[Created]", "[Resolved]", "[Delivered]", "[Median Lead Time]",
                              "[Resolution SLA %]", "[Open Tickets]", "[Open Breached]"],
                             [[r[0], n(r[1]), n(r[2]), n(r[3]), f"{r[4]:.4f}", f"{r[5]:.4f}", n(r[6]), n(r[7])] for r in team]),
        "Test key measures": (["[Check]", "[Actual]", "[Expected]", "[Result]"], [[c, v, v, "PASS"] for c, v in checks]),
        "KEEPFILTERS": (["Priorities[Priority]", "[Resolution SLA %]", "[P1 SLA replaces filter]", "[P1 SLA with KEEPFILTERS]"],
                        [[p, f"{s:.4f}", f"{p1:.4f}", f"{p1:.4f}" if k == 1 else ""] for k, p, s in prio]),
        "Expanded table": (["[Open Breached (measure)]", "[FILTER over Tickets with RELATED]",
                            "[Open P1 via a filter on Priorities]"], [[n(BREACHED), n(BREACHED), n(open_p1)]]),
        "CLS test": (["[Ticket]", "[Summary]"], [list(r) for r in cls]),
        "Model measures": (["[Table]", "[Measure]", "[Folder]", "[Description]"], [[t, m, f, d] for t, f, m, d in meas]),
        "Model tables": (["[Table]", "[Category]", "[Hidden]", "[Measures]"], [list(r) for r in tabs]),
    }
