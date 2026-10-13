"""Saved DAX queries for Power BI's DAX query view, plus the result each one should return.

build_model.py writes QUERIES to SalesTarget.SemanticModel/DAXQueries/<tab>.dax (one file per query-view tab,
tab order in DAXQueries/.pbi/daxQueries.json). build_preview.py shows the same code next to EXPECTED(),
computed here from data/marts with DuckDB and from the generated TMDL, so each tab can be checked against it.
"""
import re
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
MARTS = ROOT / "data" / "marts"
TMDL = ROOT / "powerbi" / "SalesTarget.SemanticModel" / "definition" / "tables"

con = duckdb.connect()
for t in ("sales", "targets", "reps", "territories", "category_plan", "products"):
    con.execute(f"create view {t} as select * from '{MARTS / t}.parquet'")
LAST = con.execute("select max(order_date) from sales").fetchone()[0]
NET, ORDERS = con.execute("select sum(amount), count(distinct order_number) from sales").fetchone()
TARGET = con.execute(f"select sum(daily_quota) from targets where date <= '{LAST}'").fetchone()[0]
NET, TARGET = round(NET, 2), round(TARGET, 2)
ON_TARGET = con.execute(f"""select count(*) from (select r.rep_key, sum(t.daily_quota) q from reps r
                            join targets t on t.rep_key = r.rep_key::int and t.date <= '{LAST}' group by 1) q
                            join (select rep_key, sum(amount) v from sales group by 1) s on s.rep_key = q.rep_key::int
                            where s.v >= q.q""").fetchone()[0]

BRANCH_ROLES = {"EU": "Branch Europe", "NA": "Branch North America", "PAC": "Branch Pacific"}

QUERIES = {
    "Sales by region": """// Report query: what the overview's region bars show, as a table.
// Target uses the rep's home region (the Quota measure's TREATAS branch).
EVALUATE
SUMMARIZECOLUMNS (
    Territories[Region],
    "Net Sales", [Net Sales],
    "Target", [Target],
    "Attainment", [Attainment]
)
ORDER BY [Net Sales] DESC
""",
    "Test key measures": f"""// Test: key measures against totals computed from data/marts with DuckDB
// (src/dax_queries.py writes the expected values in at build time). Every row should say PASS.
DEFINE
    MEASURE '_Measures'[Expected Net Sales] = {NET}
    MEASURE '_Measures'[Expected Orders] = {ORDERS}
    MEASURE '_Measures'[Expected Target] = {TARGET}
    MEASURE '_Measures'[Expected Attainment] = DIVIDE ( [Expected Net Sales], [Expected Target] )
    MEASURE '_Measures'[Expected Reps On Target] = {ON_TARGET}
    VAR _checks =
        UNION (
            ROW ( "Check", "Net Sales", "Actual", [Net Sales], "Expected", [Expected Net Sales] ),
            ROW ( "Check", "Orders", "Actual", [Orders], "Expected", [Expected Orders] ),
            ROW ( "Check", "Target", "Actual", [Target], "Expected", [Expected Target] ),
            ROW ( "Check", "Attainment", "Actual", [Attainment], "Expected", [Expected Attainment] ),
            ROW ( "Check", "Reps On Target", "Actual", [Reps On Target], "Expected", [Expected Reps On Target] )
        )
EVALUATE
ADDCOLUMNS ( _checks, "Result", IF ( ABS ( [Actual] - [Expected] ) < 0.01, "PASS", "FAIL" ) )
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
    "Test security": """// Test: security. The query view cannot run as a role (use Modeling > View as in Desktop for that), so
// this checks the two things a query can: (1) the three static branch roles split the sales with nothing left
// over or counted twice: each row is the role's filter Territories[Branch] = "<code>"; (2) which roles carry
// table filters and which columns are hidden by object-level security (INFO functions read the model).
EVALUATE
VAR _branches =
    ADDCOLUMNS (
        VALUES ( Territories[Branch] ),
        "Role", SWITCH ( Territories[Branch], "EU", "Branch Europe", "NA", "Branch North America",
                         "PAC", "Branch Pacific" ),
        "Net Sales", CALCULATE ( [Net Sales] )
    )
RETURN
    UNION (
        _branches,
        ROW ( "Branch", "ALL", "Role", "Sum of the roles = Net Sales?",
              "Net Sales", IF ( ABS ( SUMX ( _branches, [Net Sales] ) - [Net Sales] ) < 0.01, 1, 0 ) )
    )

EVALUATE
VAR _roles = SELECTCOLUMNS ( INFO.ROLES (), "Role ID", [ID], "Role", [Name] )
VAR _columns = SELECTCOLUMNS ( INFO.COLUMNS (), "Column ID", [ID], "Column", [ExplicitName] )
RETURN
    SELECTCOLUMNS (
        INFO.COLUMNPERMISSIONS (),
        "Role", VAR _r = [RoleID] RETURN MAXX ( FILTER ( _roles, [Role ID] = _r ), [Role] ),
        "Hidden column", VAR _c = [ColumnID] RETURN MAXX ( FILTER ( _columns, [Column ID] = _c ), [Column] )
    )
""",
    "Limited relationship": """// Limited relationship: Category Plan relates many-to-many to Products[Category] and Date[Fiscal Quarter].
// It filters like a normal relationship, but the engine adds no blank row for unmatched values and RELATED
// cannot cross it (only TREATAS / the relationship itself). Category Target is SYNTHETIC: the quarter's quota
// split by the prior fiscal year's category mix.
EVALUATE
SUMMARIZECOLUMNS (
    Products[Category],
    "Net Sales", [Net Sales],
    "Category Target", [Category Target],
    "Category Attainment", [Category Attainment]
)
ORDER BY [Net Sales] DESC
""",
}

# pbi-kit DAX tests (tools/pbi-kit/dax-tests/daxtest.py): a sibling <tab>.assert.json turns a saved query into a value
# test that CI runs online against the Test workspace (14-s9). Expected values are the DuckDB totals above, the same
# numbers tests/test_clean.py reconciles against the source files.
_REG = con.execute("""select t.region, sum(s.amount) from sales s join territories t on t.territory_key::int = s.territory_key
                      group by 1 order by 2 desc""").fetchall()
_KEY = [("Net Sales", NET, 0.01), ("Orders", ORDERS, 0), ("Target", TARGET, 0.01),
        ("Attainment", round(NET / TARGET, 6), 0.0001), ("Reps On Target", ON_TARGET, 0)]
ASSERTS = {
    "Test key measures": {"rows": len(_KEY), "checks":
        [c for i, (name, v, tol) in enumerate(_KEY) for c in (
            {"column": "[Check]", "row": i, "equals": name},
            {"column": "[Actual]", "row": i, "equals": v, "tolerance": tol},
            {"column": "[Result]", "row": i, "equals": "PASS"})]},
    "Sales by region": {"checks": [
        {"column": "Territories[Region]", "row": 0, "equals": _REG[0][0]},
        {"column": "[Net Sales]", "row": 0, "equals": round(_REG[0][1], 2), "tolerance": 0.01}]},
}


def usd(v):
    return "" if v is None else f"${v:,.2f}"


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
    reg = con.execute(f"""select t.region, s.v, q.v from territories t
        left join (select territory_key, sum(amount) v from sales group by 1) s on s.territory_key = t.territory_key::int
        left join (select r.territory_key, sum(daily_quota) v from targets x join reps r on r.rep_key::int = x.rep_key
                   where x.date <= '{LAST}' group by 1) q on q.territory_key = t.territory_key
        where s.v is not null or q.v is not null order by s.v desc nulls last""").fetchall()
    tm = _tmdl()
    br = con.execute("""select t.branch, sum(s.amount) v from sales s join territories t
                        on t.territory_key::int = s.territory_key group by 1 order by 1""").fetchall()
    cp = con.execute("""with fq as (select distinct fiscal_quarter from category_plan),
        sq as (select p.category, s.amount,
                 'FY' || (year(order_date) + (month(order_date) >= 7)::int) || ' Q' || ((month(order_date) + 5) % 12 // 3 + 1) q
               from sales s join products p using (product_code))
        select sq.category, sum(amount) v, (select sum(plan_amount) from category_plan c where c.category = sq.category) t,
               sum(amount) filter (where q in (select * from fq)) vp
        from sq group by 1 order by v desc""").fetchall()
    att = NET / TARGET
    checks = [("Net Sales", usd(NET)), ("Orders", f"{ORDERS:,}"), ("Target", usd(TARGET)),
              ("Attainment", f"{att:.4f}"), ("Reps On Target", f"{ON_TARGET}")]
    meas = sorted(((t, f, m, d) for t, _, _, ms in tm for m, f, d in ms), key=lambda r: [x.lower() for x in r[:3]])
    tabs = sorted(((t, c, str(h).upper(), len(ms)) for t, c, h, ms in tm), key=lambda r: (-r[3], r[0].lower()))
    return {
        "Sales by region": (["Territories[Region]", "[Net Sales]", "[Target]", "[Attainment]"],
                            [[r, usd(v), usd(q), "" if not q or v is None else f"{v / q:.4f}"] for r, v, q in reg]),
        "Test key measures": (["[Check]", "[Actual]", "[Expected]", "[Result]"],
                              [[c, v, v, "PASS"] for c, v in checks]),
        "Model measures": (["[Table]", "[Measure]", "[Folder]", "[Description]"], [[t, m, f, d] for t, f, m, d in meas]),
        "Model tables": (["[Table]", "[Category]", "[Hidden]", "[Measures]"], [list(r) for r in tabs]),
        "Test security": (["Territories[Branch]", "[Role]", "[Net Sales]"],
                          [[b, BRANCH_ROLES[b], usd(v)] for b, v in br]
                          + [["ALL", "Sum of the roles = Net Sales?", "1 (PASS)"],
                             ["— 2nd EVALUATE —", "Sales Manager", "Standard Cost"]]),
        "Limited relationship": (["Products[Category]", "[Net Sales]", "[Category Target]", "[Category Attainment]"],
                                 [[c, usd(v), usd(t), "" if not t else f"{vp / t:.4f}"] for c, v, t, vp in cp]),
    }
