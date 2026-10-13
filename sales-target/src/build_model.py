"""Generate the Power BI semantic model (TMDL) for sales vs target.

    uv run python src/build_model.py [--inbox PATH]

Every table is a Power Query (M) partition read from powerquery/*.pq, so the model and the
M files never drift apart. --inbox sets the InboxFolder parameter (default C:\\SalesInbox);
change it later in Power BI via Transform data > Edit parameters.

Beyond the star schema the model uses the 2026 feature set:
  * DAX user-defined functions (functions.tmdl): attainment, gap, status, share, prior year
  * calendar-based time intelligence: a Gregorian and a Fiscal (July-June) calendar on Date
  * a Time Intelligence calculation group with a dynamic format string on YoY %
  * dynamic format strings on the money measures ($1.2M / $850K)
  * field parameter (Analyze By) and a what-if target stretch
  * dynamic row-level security down a manager hierarchy (PATH), static branch roles, plus object-level
    security that hides the Cleaning Log and the product cost from sales managers
  * perspectives, descriptions on every measure, and synonyms for Q&A / Copilot
"""
import argparse
import json
import re
import shutil
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PQ = ROOT / "powerquery"
OUT = ROOT / "powerbi" / "SalesTarget.SemanticModel"
T = "\t"
NS = uuid.UUID("5a1e5c0d-7a26-4e11-9b0c-2f6d1a7e3b40")

USD0 = "\\$#,0;(\\$#,0);\\$0"
# PCT1/INT are the pbi-kit BPA formats (PERCENTAGE_FORMATTING / INTEGER_FORMATTING); whole percentages stay "0%" on purpose.
PCT0, PCT1, INT = "0%", "#,0.0%;-#,0.0%;#,0.0%", "#,0"
# $1.2M / $850K / $900: one format string that follows the size of the number.
MONEY_FMT = ('VAR _v = ABS ( SELECTEDMEASURE () )\nRETURN\n    SWITCH ( TRUE (),\n'
             '        _v >= 1e6, "$#,0.0,,""M"";($#,0.0,,""M"")",\n'
             '        _v >= 1e3, "$#,0,""K"";($#,0,""K"")",\n'
             '        "$#,0;($#,0)" )')

# Power BI table -> (M query file, columns). Column: (name, dataType, options)
TABLES = {
    "Sales": ("Sales", [
        ("Order Number", "string", {}), ("Order Line", "int64", {"hidden": True}),
        ("Order Date", "dateTime", {"fmt": "dd mmm yyyy"}), ("Ship Date", "dateTime", {"fmt": "dd mmm yyyy"}),
        ("Reseller Code", "string", {"hidden": True}), ("Rep Key", "int64", {"hidden": True}),
        ("Territory Key", "int64", {"hidden": True}), ("Product Code", "string", {"hidden": True}),
        ("Quantity", "int64", {"fmt": INT}), ("Unit Price", "decimal", {"fmt": USD0, "hidden": True}),
        ("Discount", "double", {"fmt": PCT1}), ("Amount", "decimal", {"fmt": USD0}),
        ("Branch", "string", {}),
    ]),
    "Targets": ("Targets", [
        ("Rep Key", "int64", {"hidden": True}), ("Date", "dateTime", {"hidden": True}),
        ("Quarter", "string", {"hidden": True}), ("Daily Quota", "decimal", {"fmt": USD0, "hidden": True, "src": "Target"}),
    ]),
    "Reps": ("Reps", [
        ("Rep Key", "int64", {"key": True, "hidden": True}), ("Rep", "string", {}),
        ("Login", "string", {"hidden": True}), ("Title", "string", {}),
        ("Territory Key", "int64", {"hidden": True}), ("Manager Key", "int64", {"hidden": True}),
        ("Email", "string", {"hidden": True}),
    ]),
    "Territories": ("Territories", [
        ("Territory Key", "int64", {"key": True, "hidden": True}), ("Region", "string", {}),
        ("Country", "string", {"category": "Country"}), ("Group", "string", {}), ("Branch", "string", {}),
    ]),
    "Products": ("Products", [
        ("Product Code", "string", {"key": True}), ("Product", "string", {}),
        ("Subcategory", "string", {}), ("Category", "string", {}),
        ("Standard Cost", "decimal", {"fmt": "\\$#,0.00", "hidden": True}),
    ]),
    "Resellers": ("Resellers", [
        ("Reseller Key", "int64", {"hidden": True}), ("Reseller Code", "string", {"key": True}),
        ("Reseller", "string", {}), ("Business Type", "string", {}),
        ("City", "string", {"category": "City"}), ("Country", "string", {"category": "Country"}),
    ]),
    "Date": ("Date", [
        ("Date", "dateTime", {"key": True, "fmt": "dd mmm yyyy"}), ("Year", "int64", {"fmt": "0"}),
        ("Year Quarter", "int64", {"hidden": True}), ("Quarter", "string", {"sort": "Year Quarter"}),
        ("Year Month", "int64", {"hidden": True}), ("Month", "string", {"sort": "Year Month"}),
        ("Month Start", "dateTime", {"fmt": "mmm yyyy"}),
        ("Month of Year", "int64", {"hidden": True}), ("Month Name", "string", {"sort": "Month of Year"}),
        ("Fiscal Year Number", "int64", {"hidden": True}), ("Fiscal Year", "string", {"sort": "Fiscal Year Number"}),
        ("Fiscal Quarter Number", "int64", {"hidden": True}),
        ("Fiscal Quarter", "string", {"sort": "Fiscal Quarter Number"}),
        ("Fiscal Month Number", "int64", {"hidden": True}),
    ]),
    # SYNTHETIC plan: quota per fiscal quarter split by the prior year's category mix (powerquery/Category Plan.pq).
    "Category Plan": ("Category Plan", [
        ("Fiscal Quarter", "string", {}), ("Category", "string", {}),
        ("Plan Amount", "decimal", {"fmt": USD0, "hidden": True}), ("Mix Basis", "string", {}),
    ]),
    "Cleaning Log": ("Cleaning Log", [
        ("Branch", "string", {}), ("File", "string", {}), ("Outcome", "string", {}),
        ("Rows", "int64", {"fmt": INT}), ("Amount", "decimal", {"fmt": USD0}),
    ]),
}
# M queries that are not tables: the parameter, helper functions and the staging query.
EXPRESSIONS = ["InboxFolder", "FiscalYearStart", "fnKey", "fnReference", "fnReadNorthAmerica", "fnReadEurope", "fnReadPacific",
               "NA Rows", "EU Rows", "Pacific Rows", "Sales Staging"]

# RFM per reseller, as of the last sale date (static: calculated at refresh, the same for every slicer).
# Context transition in a calculated column only filters the key column (Reseller Code is isKey), so the
# CALCULATE columns don't depend on each other. Scores are quintiles: the share of resellers with orders
# that score worse, in five bands.
RFM_RULES = [  # evaluated top-down: (segment, condition on R and F score, what the rep should do)
    ("Champions", "_r >= 4 && _f >= 4", "Thank them: early access to new models, ask for a referral"),
    ("Loyal", "_r >= 3 && _f >= 3", "Upsell accessories and clothing on the next call"),
    ("Promising", "_r >= 4", "Recent but few orders: follow up for the second order this month"),
    ("At risk", "_f >= 3", "Used to buy often, quiet now: call this week"),
    ("Lost", "_r = 1", "Win-back offer or take off the call list"),
    ("Needs attention", "TRUE ()", "Check in this month before they drift")]
SEGMENTS = ["Champions", "Loyal", "Promising", "Needs attention", "At risk", "Lost", "No orders"]
RFM_SWITCH = "SWITCH ( TRUE (), " + ", ".join(f'{c}, "{n}"' for n, c, _ in RFM_RULES) + " )"
SEGMENT_ORDER = lambda col: "SWITCH ( " + col + ", " + ", ".join(f'"{n}", {i + 1}' for i, n in enumerate(SEGMENTS)) + " )"
RFM_COLUMNS = [
    ("Recency Days", "int64",
     "VAR _mine = CALCULATE ( MAX ( Sales[Order Date] ) )\n"
     "RETURN IF ( NOT ISBLANK ( _mine ), DATEDIFF ( _mine, CALCULATE ( MAX ( Sales[Order Date] ), ALL ( Sales ) ), DAY ) )",
     {"fmt": "0"}),
    ("Frequency", "int64", "CALCULATE ( DISTINCTCOUNT ( Sales[Order Number] ) )", {"fmt": "0"}),
    ("Monetary", "decimal", "CALCULATE ( SUM ( Sales[Amount] ) )", {"fmt": USD0, "hidden": True}),
    ("R Score", "int64",
     "VAR _v = Resellers[Recency Days]\n"
     "VAR _n = COUNTROWS ( FILTER ( ALL ( Resellers[Reseller Code], Resellers[Recency Days] ), NOT ISBLANK ( Resellers[Recency Days] ) ) )\n"
     "VAR _better = COUNTROWS ( FILTER ( ALL ( Resellers[Reseller Code], Resellers[Recency Days] ), NOT ISBLANK ( Resellers[Recency Days] ) && Resellers[Recency Days] < _v ) )\n"
     "RETURN IF ( NOT ISBLANK ( _v ), 5 - INT ( 5 * DIVIDE ( _better, _n ) ) )", {"fmt": "0"}),
    ("F Score", "int64",
     "VAR _v = Resellers[Frequency]\n"
     "VAR _n = COUNTROWS ( FILTER ( ALL ( Resellers[Reseller Code], Resellers[Frequency] ), NOT ISBLANK ( Resellers[Frequency] ) ) )\n"
     "VAR _below = COUNTROWS ( FILTER ( ALL ( Resellers[Reseller Code], Resellers[Frequency] ), NOT ISBLANK ( Resellers[Frequency] ) && Resellers[Frequency] < _v ) )\n"
     "RETURN IF ( NOT ISBLANK ( _v ), 1 + INT ( 5 * DIVIDE ( _below, _n ) ) )", {"fmt": "0"}),
    ("M Score", "int64",
     "VAR _v = Resellers[Monetary]\n"
     "VAR _n = COUNTROWS ( FILTER ( ALL ( Resellers[Reseller Code], Resellers[Monetary] ), NOT ISBLANK ( Resellers[Monetary] ) ) )\n"
     "VAR _below = COUNTROWS ( FILTER ( ALL ( Resellers[Reseller Code], Resellers[Monetary] ), NOT ISBLANK ( Resellers[Monetary] ) && Resellers[Monetary] < _v ) )\n"
     "RETURN IF ( NOT ISBLANK ( _v ), 1 + INT ( 5 * DIVIDE ( _below, _n ) ) )", {"fmt": "0"}),
    ("RFM Segment", "string",
     "VAR _r = Resellers[R Score]\nVAR _f = Resellers[F Score]\n"
     'RETURN IF ( ISBLANK ( _r ), "No orders", ' + RFM_SWITCH + " )", {"sort": "RFM Segment Order"}),
    ("RFM Segment Order", "int64", SEGMENT_ORDER("Resellers[RFM Segment]"), {"hidden": True, "fmt": "0"}),
    ("RFM Action", "string",
     "SWITCH ( Resellers[RFM Segment], " + ", ".join(f'"{n}", "{a}"' for n, _, a in RFM_RULES)
     + ', "Prospect: no orders yet" )', {}),
]

# Parent-child org chart flattened for a hierarchy and for row-level security.
ORG_LEVEL = ("VAR _k = PATHITEM ( Reps[Manager Path], {n}, INTEGER )\n"
             "RETURN IF ( ISBLANK ( _k ), Reps[Rep], LOOKUPVALUE ( Reps[Rep], Reps[Rep Key], _k ) )")
CALC_COLUMNS = {
    "Reps": [
        ("Manager Path", "string", "PATH ( Reps[Rep Key], Reps[Manager Key] )", {"hidden": True}),
        ("Org Level 1", "string", ORG_LEVEL.format(n=1), {}),
        ("Org Level 2", "string", ORG_LEVEL.format(n=2), {}),
        ("Org Level 3", "string", ORG_LEVEL.format(n=3), {}),
        ("Home Region", "string",
         "LOOKUPVALUE ( Territories[Region], Territories[Territory Key], Reps[Territory Key] )", {}),
    ],
    "Resellers": [
        # City + country in one string so Azure Maps geocodes "Paris, France", not just "Paris".
        ("Location", "string", 'Resellers[City] & ", " & Resellers[Country]', {"category": "Place"}),
        # Target for the key influencers visual on the Why page: did the reseller buy less in FY2013 than FY2012?
        ("Sales Trend", "string",
         'VAR _cur = CALCULATE ( [Net Sales], \'Date\'[Fiscal Year] = "FY2013" )\n'
         'VAR _prev = CALCULATE ( [Net Sales], \'Date\'[Fiscal Year] = "FY2012" )\n'
         'RETURN SWITCH ( TRUE (), ISBLANK ( _cur ) && ISBLANK ( _prev ), "No sales FY2012-13",\n'
         '    _cur < _prev, "Declining", "Growing or new" )', {}),
        *RFM_COLUMNS,
    ],
}
HIERARCHIES = {
    "Reps": [("Org", ["Org Level 1", "Org Level 2", "Org Level 3"])],
    "Products": [("Product Hierarchy", ["Category", "Subcategory", "Product"])],
    "Date": [("Fiscal", ["Fiscal Year", "Fiscal Quarter", "Month"]), ("Calendar", ["Year", "Quarter", "Month"])],
}

# Calendar-based time intelligence: (calendar, [(time unit, primary column, associated column)])
CALENDARS = [
    ("Gregorian", [("year", "Year", None), ("quarter", "Year Quarter", "Quarter"), ("month", "Year Month", "Month"),
                   ("monthOfYear", "Month of Year", "Month Name"), ("date", "Date", None)]),
    ("Fiscal", [("year", "Fiscal Year Number", "Fiscal Year"), ("quarter", "Fiscal Quarter Number", "Fiscal Quarter"),
                ("month", "Fiscal Month Number", "Month"), ("date", "Date", None)]),
]

# (from table, from column, to table, to column, options). Every relationship states its cross-filter direction
# and whether it is active, so the TMDL reads without knowing the defaults. All are single-direction: the
# dimension filters the fact. options: active=False (inactive, used by USERELATIONSHIP), m2m=True (many-to-many).
RELATIONSHIPS = [
    ("Sales", "Order Date", "Date", "Date", {}),
    ("Sales", "Ship Date", "Date", "Date", {"active": False}),
    ("Sales", "Rep Key", "Reps", "Rep Key", {}),
    ("Sales", "Territory Key", "Territories", "Territory Key", {}),
    ("Sales", "Product Code", "Products", "Product Code", {}),
    ("Sales", "Reseller Code", "Resellers", "Reseller Code", {}),
    ("Targets", "Date", "Date", "Date", {}), ("Targets", "Rep Key", "Reps", "Rep Key", {}),
    # The category plan sits at Category x Fiscal Quarter, coarser than both dimensions: limited many-to-many.
    ("Category Plan", "Category", "Products", "Category", {"m2m": True}),
    ("Category Plan", "Fiscal Quarter", "Date", "Fiscal Quarter", {"m2m": True}),
]

# (name, description, signature => body)
FUNCTIONS = [
    ("Sales.Attainment", "Actual ÷ target. Blank when there is no target.",
     "( actual : NUMERIC, target : NUMERIC ) => DIVIDE ( actual, target )"),
    ("Sales.Gap", "Actual − target. Blank when there is no target.",
     "( actual : NUMERIC, target : NUMERIC ) => IF ( NOT ISBLANK ( target ), actual - target )"),
    ("Sales.Status", "On target from 100%, at risk from 90%, behind below that.",
     '( attainment : NUMERIC ) =>\n\tSWITCH ( TRUE (),\n\t\tISBLANK ( attainment ), BLANK (),\n'
     '\t\tattainment >= 1, "On target",\n\t\tattainment >= 0.9, "At risk",\n\t\t"Behind" )'),
    ("Sales.StatusColor", "Hex colour for a status, for conditional formatting.",
     '( status : STRING ) => SWITCH ( status, "On target", "#1B7F4B", "At risk", "#A35F00", "Behind", "#B42318", "#6B7280" )'),
    ("Sales.ShareOf", "A measure as a share of the same measure with the filters on one table or column removed.",
     "( value : SCALAR NUMERIC EXPR, scope : ANYREF ) => DIVIDE ( value, CALCULATE ( value, REMOVEFILTERS ( scope ) ) )"),
    ("Sales.PriorYear", "A measure one year back on the calendar passed in (Gregorian or Fiscal).",
     "( value : SCALAR NUMERIC EXPR, cal : CALENDARREF ) => CALCULATE ( value, DATEADD ( cal, -1, YEAR ) )"),
]

# Time Intelligence calculation group: (item, DAX, format string expression or None)
TIME_ITEMS = [
    ("Current", "SELECTEDMEASURE ()", None),
    ("MTD", "CALCULATE ( SELECTEDMEASURE (), DATESMTD ( 'Gregorian' ) )", None),
    ("QTD", "CALCULATE ( SELECTEDMEASURE (), DATESQTD ( 'Gregorian' ) )", None),
    ("YTD", "CALCULATE ( SELECTEDMEASURE (), DATESYTD ( 'Gregorian' ) )", None),
    ("Fiscal YTD", "CALCULATE ( SELECTEDMEASURE (), DATESYTD ( 'Fiscal' ) )", None),
    ("Prior Year", "CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, YEAR ) )", None),
    ("YoY %", "VAR _py = CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, YEAR ) )\n"
              "RETURN DIVIDE ( SELECTEDMEASURE () - _py, _py )", '"+0.0%;-0.0%;0.0%"'),
    ("Prior Month", "CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, MONTH ) )", None),
    ("MoM %", "VAR _pm = CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, MONTH ) )\n"
              "RETURN DIVIDE ( SELECTEDMEASURE () - _pm, _pm )", '"+0.0%;-0.0%;0.0%"'),
    ("Prior Quarter", "CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, QUARTER ) )", None),
    ("QoQ %", "VAR _pq = CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, QUARTER ) )\n"
              "RETURN DIVIDE ( SELECTEDMEASURE () - _pq, _pq )", '"+0.0%;-0.0%;0.0%"'),
]

# ----------------------------------------------------------------- calculated tables (static, at refresh)
# Fiscal quarter as a running index: Fiscal Quarter Number is fy * 10 + q, the index is fy * 4 + q.
FQ_IDX = "INT ( 'Date'[Fiscal Quarter Number] / 10 ) * 4 + MOD ( 'Date'[Fiscal Quarter Number], 10 )"
# Cohort = fiscal quarter of a reseller's first order; Quarter N = quarters since then (0 = the first quarter).
COHORT_DAX = f"""VAR _act = SELECTCOLUMNS ( SUMMARIZE ( Sales, Sales[Reseller Code], 'Date'[Fiscal Quarter Number] ),
    "Code", Sales[Reseller Code], "Idx", {FQ_IDX} )
VAR _first = ADDCOLUMNS ( _act, "First", VAR _c = [Code] RETURN MINX ( FILTER ( _act, [Code] = _c ), [Idx] ) )
VAR _cells = GROUPBY ( _first, [First], [Idx], "@n", SUMX ( CURRENTGROUP (), 1 ) )
RETURN SELECTCOLUMNS ( _cells,
    "Cohort", "FY" & INT ( ( [First] - 1 ) / 4 ) & " Q" & ( MOD ( [First] - 1, 4 ) + 1 ),
    "Cohort Order", [First],
    "Quarter N", [Idx] - [First],
    "Cohort Active", [@n],
    "Cohort Size", VAR _f = [First] RETURN MAXX ( FILTER ( _cells, [First] = _f && [Idx] = _f ), [@n] ) )"""


def snapshot(i, y, m, d):
    """RFM segment of every reseller as of one date, with the same quintile rules as the Resellers columns."""
    cut = f"Sales[Order Date] <= DATE ( {y}, {m}, {d} )"
    return f"""VAR _a{i} = ADDCOLUMNS ( VALUES ( Resellers[Reseller Code] ),
    "@last{i}", CALCULATE ( MAX ( Sales[Order Date] ), {cut} ),
    "@freq{i}", CALCULATE ( DISTINCTCOUNT ( Sales[Order Number] ), {cut} ) )
VAR _b{i} = FILTER ( _a{i}, NOT ISBLANK ( [@last{i}] ) )
VAR _n{i} = COUNTROWS ( _b{i} )
VAR _s{i} = ADDCOLUMNS ( _a{i}, "@seg{i}",
    VAR _l = [@last{i}]
    VAR _q = [@freq{i}]
    VAR _r = 5 - INT ( 5 * DIVIDE ( COUNTROWS ( FILTER ( _b{i}, [@last{i}] > _l ) ), _n{i} ) )
    VAR _f = 1 + INT ( 5 * DIVIDE ( COUNTROWS ( FILTER ( _b{i}, [@freq{i}] < _q ) ), _n{i} ) )
    RETURN IF ( ISBLANK ( _l ), "No orders", {RFM_SWITCH} ) )"""


# Segment at the end of FY2012 (30 Jun 2012) → segment at the end of FY2013 (30 Jun 2013).
TRANSITION_DAX = f"""{snapshot(1, 2012, 6, 30)}
{snapshot(2, 2013, 6, 30)}
VAR _j = ADDCOLUMNS ( SELECTCOLUMNS ( _s1, "Code", Resellers[Reseller Code], "From Segment", [@seg1] ),
    "To Segment", VAR _k = [Code] RETURN MAXX ( FILTER ( _s2, Resellers[Reseller Code] = _k ), [@seg2] ) )
VAR _g = GROUPBY ( FILTER ( _j, [To Segment] <> "No orders" ), [From Segment], [To Segment],
    "@n", SUMX ( CURRENTGROUP (), 1 ) )
RETURN SELECTCOLUMNS ( _g,
    "From Segment", [From Segment], "From Order", {SEGMENT_ORDER("[From Segment]")},
    "To Segment", [To Segment], "To Order", {SEGMENT_ORDER("[To Segment]")},
    "Reseller Count", [@n] )"""

# Market basket on subcategories: pairs bought in the same order. & "" drops the column lineage on both
# sides, so NATURALINNERJOIN joins the two copies on the order number by name.
PAIRS_DAX = """VAR _os = SUMMARIZE ( Sales, Sales[Order Number], Products[Subcategory] )
VAR _orders = COUNTROWS ( SUMMARIZE ( Sales, Sales[Order Number] ) )
VAR _sub = GROUPBY ( SELECTCOLUMNS ( _os, "Sub", Products[Subcategory] & "" ), [Sub], "@o", SUMX ( CURRENTGROUP (), 1 ) )
VAR _l = SELECTCOLUMNS ( _os, "Ord", Sales[Order Number] & "", "Subcategory A", Products[Subcategory] & "" )
VAR _r = SELECTCOLUMNS ( _os, "Ord", Sales[Order Number] & "", "Subcategory B", Products[Subcategory] & "" )
VAR _pairs = FILTER ( NATURALINNERJOIN ( _l, _r ), [Subcategory A] < [Subcategory B] )
VAR _g = GROUPBY ( _pairs, [Subcategory A], [Subcategory B], "@n", SUMX ( CURRENTGROUP (), 1 ) )
RETURN SELECTCOLUMNS ( FILTER ( _g, [@n] >= 30 ),
    "Subcategory A", [Subcategory A],
    "Subcategory B", [Subcategory B],
    "Orders Together", [@n],
    "Support", DIVIDE ( [@n], _orders ),
    "Confidence A to B", VAR _a = [Subcategory A] RETURN DIVIDE ( [@n], MAXX ( FILTER ( _sub, [Sub] = _a ), [@o] ) ),
    "Confidence B to A", VAR _b = [Subcategory B] RETURN DIVIDE ( [@n], MAXX ( FILTER ( _sub, [Sub] = _b ), [@o] ) ),
    "Lift", VAR _a = [Subcategory A] VAR _b = [Subcategory B]
        RETURN DIVIDE ( [@n] * _orders,
            MAXX ( FILTER ( _sub, [Sub] = _a ), [@o] ) * MAXX ( FILTER ( _sub, [Sub] = _b ), [@o] ) ) )"""


CALCULATED = {  # what-if parameter
    "Target Stretch": (
        'SELECTCOLUMNS ( GENERATESERIES ( 0, 0.25, 0.05 ), "Target Stretch", [Value] )',
        [("Target Stretch", "[Target Stretch]", "double", [f"formatString: {PCT0}"])]),
    # Disconnected tables: a slicer / axis for a class that a measure computes on the fly.
    "ABC Class": (
        'DATATABLE ( "Class", STRING, "Class Order", INTEGER, "Share From", DOUBLE, "Share To", DOUBLE,\n'
        '    { { "A", 1, 0.0, 0.7 }, { "B", 2, 0.7, 0.9 }, { "C", 3, 0.9, 1.0 } } )',
        [("Class", "[Class]", "string", ["sortByColumn: 'Class Order'"]),
         ("Class Order", "[Class Order]", "int64", ["isHidden", "formatString: 0"]),
         ("Share From", "[Share From]", "double", ["isHidden", f"formatString: {PCT0}"]),
         ("Share To", "[Share To]", "double", ["isHidden", f"formatString: {PCT0}"])]),
    "Reseller Size Bands": (
        'DATATABLE ( "Band", STRING, "Band Order", INTEGER, "Band Min", CURRENCY, "Band Max", CURRENCY,\n'
        '    { { "Under $50K", 1, 0, 50000 }, { "$50K-250K", 2, 50000, 250000 },\n'
        '      { "$250K-1M", 3, 250000, 1000000 }, { "Over $1M", 4, 1000000, 1000000000000 } } )',
        [("Band", "[Band]", "string", ["sortByColumn: 'Band Order'"]),
         ("Band Order", "[Band Order]", "int64", ["isHidden", "formatString: 0"]),
         ("Band Min", "[Band Min]", "decimal", ["isHidden", f"formatString: {USD0}"]),
         ("Band Max", "[Band Max]", "decimal", ["isHidden", f"formatString: {USD0}"])]),
    "Reseller Cohort": (COHORT_DAX, [
        ("Cohort", "[Cohort]", "string", ["sortByColumn: 'Cohort Order'"]),
        ("Cohort Order", "[Cohort Order]", "int64", ["isHidden", "formatString: 0"]),
        ("Quarter N", "[Quarter N]", "int64", ["formatString: 0"]),
        ("Cohort Active", "[Cohort Active]", "int64", ["isHidden", "formatString: 0"]),
        ("Cohort Size", "[Cohort Size]", "int64", ["isHidden", "formatString: 0"])]),
    "RFM Transition": (TRANSITION_DAX, [
        ("From Segment", "[From Segment]", "string", ["sortByColumn: 'From Order'"]),
        ("From Order", "[From Order]", "int64", ["isHidden", "formatString: 0"]),
        ("To Segment", "[To Segment]", "string", ["sortByColumn: 'To Order'"]),
        ("To Order", "[To Order]", "int64", ["isHidden", "formatString: 0"]),
        ("Reseller Count", "[Reseller Count]", "int64", ["isHidden", "formatString: 0"])]),
    "Subcategory Pairs": (PAIRS_DAX, [
        ("Subcategory A", "[Subcategory A]", "string", []),
        ("Subcategory B", "[Subcategory B]", "string", []),
        ("Orders Together", "[Orders Together]", "int64", ["formatString: #,0"]),
        ("Support", "[Support]", "double", [f"formatString: {PCT1}"]),
        ("Confidence A to B", "[Confidence A to B]", "double", [f"formatString: {PCT0}"]),
        ("Confidence B to A", "[Confidence B to A]", "double", [f"formatString: {PCT0}"]),
        ("Lift", "[Lift]", "double", ["formatString: 0.00"])]),
}

# Field parameter of measures: the Overview bars switch metric with a slicer.
METRIC = [("Net Sales", "[Net Sales]"), ("Target", "[Target]"), ("Gap to Target", "[Gap to Target]"),
          ("Gross Margin", "[Gross Margin]"), ("Orders", "[Orders]")]
ANALYZE_BY = [("Rep", "Reps[Rep]"), ("Region", "Territories[Region]"), ("Country", "Territories[Country]"),
              ("Category", "Products[Category]"), ("Business Type", "Resellers[Business Type]")]

# Calculated tables are computed at refresh, before any role applies: they hold every branch's resellers,
# so the restricted roles don't get them at all (object-level security).
STATIC_TABLES = ["Reseller Cohort", "RFM Transition", "Subcategory Pairs"]
# A manager sees everyone below them in the org chart; a rep sees only themselves.
# Object-level security: the Cleaning Log and the source file column don't exist for this role.
ROLES = {
    "Sales Manager": {
        "filters": {"Reps": "PATHCONTAINS ( Reps[Manager Path], "
                            "LOOKUPVALUE ( Reps[Rep Key], Reps[Email], USERPRINCIPALNAME () ) )"},
        "hide_tables": ["Cleaning Log", *STATIC_TABLES],
        "hide_columns": {"Products": ["Standard Cost"]},
    },
    "Data Steward": {"filters": {}, "hide_tables": [], "hide_columns": {}},
}
# Static branch roles: one per branch. Sales follow the order's territory, targets follow the rep's home
# territory, so both tables are filtered (a rep selling outside their branch drops out of that branch's view).
for _role, _code in [("Branch Europe", "EU"), ("Branch North America", "NA"), ("Branch Pacific", "PAC")]:
    ROLES[_role] = {"filters": {
        "Territories": f'Territories[Branch] = "{_code}"',
        "Reps": f'LOOKUPVALUE ( Territories[Branch], Territories[Territory Key], Reps[Territory Key] ) = "{_code}"'},
        "hide_tables": ["Cleaning Log", *STATIC_TABLES], "hide_columns": {}}

PERSPECTIVES = {
    "Sales Review": ["Sales", "Targets", "Reps", "Territories", "Products", "Resellers", "Date", "Category Plan",
                     "Time Intelligence", "Analyze By", "Metric", "Target Stretch", "ABC Class", "Reseller Size Bands",
                     "Reseller Cohort", "RFM Transition", "Subcategory Pairs", "_Measures"],
    "Data Quality": ["Cleaning Log", "Sales", "Date"],
}

# Monthly sales points for the linear trend: x = year * 12 + month, up to the last sale, slicers other than Date kept.
TREND_FIT = """VAR _pts = FILTER (
    ADDCOLUMNS (
        CALCULATETABLE ( VALUES ( 'Date'[Year Month] ), REMOVEFILTERS ( 'Date' ), 'Date'[Date] <= _last ),
        "@x", INT ( 'Date'[Year Month] / 100 ) * 12 + MOD ( 'Date'[Year Month], 100 ),
        "@y", VAR _m = 'Date'[Year Month] RETURN CALCULATE ( [Net Sales], REMOVEFILTERS ( 'Date' ), 'Date'[Year Month] = _m ) ),
    NOT ISBLANK ( [@y] ) )
VAR _fit = LINESTX ( _pts, [@y], [@x] )
VAR _a = MAXX ( _fit, [Intercept] )
VAR _b = MAXX ( _fit, [Slope1] )"""
# Every visible row ranked by net sales within the visual's selection (ALLSELECTED drops the row's own filter).
RANKED = ("VAR _s = [Net Sales]\nVAR _t = CALCULATETABLE ( ADDCOLUMNS ( VALUES ( {key} ), \"@s\", [Net Sales] ), "
          "ALLSELECTED ( {table} ) )\n")
HEAT = ('VAR _v = {m}\nRETURN SWITCH ( TRUE (), ISBLANK ( _v ), BLANK (), _v >= 0.75, "#7FAAD3", _v >= 0.5, "#A9C8E4",\n'
        '    _v >= 0.25, "#CFE0F0", "#EEF3F8" )')
# New / returning only mean something against a period: with every date in context every reseller is "new".
# With no date filter the cards fall back to the last fiscal quarter that has sales (team-kpi pattern).
PERIOD_VARS = ("VAR _full = MIN ( 'Date'[Date] ) = CALCULATE ( MIN ( 'Date'[Date] ), REMOVEFILTERS ( 'Date' ) )\n"
               "    && MAX ( 'Date'[Date] ) = CALCULATE ( MAX ( 'Date'[Date] ), REMOVEFILTERS ( 'Date' ) )\n"
               "VAR _last = CALCULATE ( MAX ( Sales[Order Date] ), REMOVEFILTERS () )\n"
               "VAR _lastq = CALCULATE ( MAX ( 'Date'[Fiscal Quarter Number] ), REMOVEFILTERS ( 'Date' ), 'Date'[Date] = _last )\n")


def in_period(expr):
    inner = "\n".join("        " + line for line in expr.split("\n"))
    return (PERIOD_VARS + "RETURN IF ( _full,\n    CALCULATE (\n" + inner +
            ",\n        REMOVEFILTERS ( 'Date' ), 'Date'[Fiscal Quarter Number] = _lastq ),\n" +
            "\n".join("    " + line for line in expr.split("\n")) + " )")


NEW_DAX = ("COUNTROWS ( FILTER ( ADDCOLUMNS ( VALUES ( Sales[Reseller Code] ),\n"
           "    \"@first\", CALCULATE ( MIN ( Sales[Order Date] ), ALLEXCEPT ( Sales, Sales[Reseller Code] ) ) ),\n"
           "    [@first] >= MIN ( 'Date'[Date] ) && [@first] <= MAX ( 'Date'[Date] ) ) )")
FY13 = "REMOVEFILTERS ( 'Date' ), 'Date'[Fiscal Year] = \"FY2013\""
CUSTOMER_MEASURES = [
    # ---------------------------------------------------------------- Pareto & ABC (follow the slicers)
    ("Pareto & ABC", "Product Cum Share", PCT0,
     "Share of net sales from this product and every product that sells more (Pareto line).",
     RANKED.format(key="Products[Product Code]", table="Products")
     + "RETURN IF ( NOT ISBLANK ( _s ), DIVIDE ( SUMX ( FILTER ( _t, [@s] >= _s ), [@s] ), SUMX ( _t, [@s] ) ) )"),
    ("Pareto & ABC", "Product Share for 80% of Sales", PCT0,
     "Share of the products sold that together make 80% of net sales.",
     "VAR _t = FILTER ( ADDCOLUMNS ( VALUES ( Products[Product Code] ), \"@s\", [Net Sales] ), NOT ISBLANK ( [@s] ) )\n"
     "VAR _total = SUMX ( _t, [@s] )\n"
     "VAR _needed = COUNTROWS ( FILTER ( _t, VAR _v = [@s] RETURN SUMX ( FILTER ( _t, [@s] > _v ), [@s] ) < 0.8 * _total ) )\n"
     "RETURN DIVIDE ( _needed, COUNTROWS ( _t ) )"),
    ("Pareto & ABC", "Pareto Text", None, "Title of the Pareto chart: the 80/20 split for the current selection.",
     '"Pareto: " & FORMAT ( [Product Share for 80% of Sales], "0%" ) & " of the products sold make 80% of net sales"'),
    ("Pareto & ABC", "Reseller Cum Share", PCT0,
     "Share of net sales from this reseller and every reseller that buys more.",
     RANKED.format(key="Resellers[Reseller Code]", table="Resellers")
     + "RETURN IF ( NOT ISBLANK ( _s ), DIVIDE ( SUMX ( FILTER ( _t, [@s] >= _s ), [@s] ), SUMX ( _t, [@s] ) ) )"),
    ("Pareto & ABC", "Reseller ABC Class", None,
     "A: resellers that make the first 70% of net sales, B: the next 20%, C: the last 10%. Follows the slicers.",
     RANKED.format(key="Resellers[Reseller Code]", table="Resellers")
     + "VAR _before = COALESCE ( DIVIDE ( SUMX ( FILTER ( _t, [@s] > _s ), [@s] ), SUMX ( _t, [@s] ) ), 0 )\n"
       "RETURN IF ( NOT ISBLANK ( _s ),\n"
       "    MAXX ( FILTER ( ALL ( 'ABC Class' ), _before >= 'ABC Class'[Share From] && _before < 'ABC Class'[Share To] ),\n"
       "        'ABC Class'[Class] ) )"),
    ("Pareto & ABC", "Sales in Class", "dynamic", "Net sales of the resellers in the ABC classes picked in the slicer.",
     "SUMX ( FILTER ( VALUES ( Resellers[Reseller Code] ), [Reseller ABC Class] IN VALUES ( 'ABC Class'[Class] ) ), [Net Sales] )"),
    ("Pareto & ABC", "Resellers in Class", INT, "Resellers in the ABC classes picked in the slicer.",
     "COUNTROWS ( FILTER ( VALUES ( Resellers[Reseller Code] ), [Reseller ABC Class] IN VALUES ( 'ABC Class'[Class] ) ) )"),
    ("Pareto & ABC", "Resellers in Band", INT,
     "Resellers whose net sales in the current selection (pick a fiscal year) fall in the size band.",
     "VAR _lo = MIN ( 'Reseller Size Bands'[Band Min] )\nVAR _hi = MAX ( 'Reseller Size Bands'[Band Max] )\n"
     "RETURN COUNTROWS ( FILTER ( VALUES ( Resellers[Reseller Code] ),\n"
     "    VAR _v = [Net Sales] RETURN NOT ISBLANK ( _v ) && _v >= _lo && _v < _hi ) )"),
    ("Pareto & ABC", "Sales in Band", "dynamic", "Net sales of the resellers in the size band.",
     "VAR _lo = MIN ( 'Reseller Size Bands'[Band Min] )\nVAR _hi = MAX ( 'Reseller Size Bands'[Band Max] )\n"
     "RETURN SUMX ( FILTER ( VALUES ( Resellers[Reseller Code] ),\n"
     "    VAR _v = [Net Sales] RETURN NOT ISBLANK ( _v ) && _v >= _lo && _v < _hi ), [Net Sales] )"),
    # ---------------------------------------------------------------- new, returning, churn, lifetime value
    ("Resellers", "Period Counted", None,
     "Period the new / returning cards count: the date selection, or the last fiscal quarter with sales when no date is picked.",
     PERIOD_VARS + "RETURN IF ( _full, CALCULATE ( MAX ( 'Date'[Fiscal Quarter] ), REMOVEFILTERS ( 'Date' ), "
     "'Date'[Fiscal Quarter Number] = _lastq ) & \" (latest)\",\n"
     "    FORMAT ( MIN ( 'Date'[Date] ), \"mmm yyyy\" ) & \" – \" & FORMAT ( MAX ( 'Date'[Date] ), \"mmm yyyy\" ) )"),
    ("Resellers", "Resellers in Period", INT,
     "Resellers buying in the period counted (latest fiscal quarter with sales when no date is picked).",
     in_period("[Active Resellers]")),
    ("Resellers", "New Resellers", INT,
     "Resellers whose first order ever falls in the period counted (latest fiscal quarter with sales when no date is picked).",
     in_period(NEW_DAX)),
    ("Resellers", "Returning Resellers", INT, "Resellers buying in the period counted who had ordered before it.",
     "[Resellers in Period] - [New Resellers]"),
    ("Resellers", "Days Since Last Order", INT,
     "Days from the reseller's last order (any date, for the selected rep or region) to the last sale date.",
     "VAR _mine = CALCULATE ( MAX ( Sales[Order Date] ), REMOVEFILTERS ( 'Date' ) )\n"
     "RETURN IF ( NOT ISBLANK ( _mine ), DATEDIFF ( _mine, [Last Sale Date], DAY ) )"),
    ("Resellers", "Churned Resellers", INT, "Resellers with no order in the last 180 days of data.",
     "COUNTROWS ( FILTER ( VALUES ( Resellers[Reseller Code] ), [Days Since Last Order] > 180 ) )"),
    ("Resellers", "At-Risk Resellers", INT, "Resellers whose last order was 91 to 180 days before the last sale date.",
     "COUNTROWS ( FILTER ( VALUES ( Resellers[Reseller Code] ),\n"
     "    VAR _d = [Days Since Last Order] RETURN _d > 90 && _d <= 180 ) )"),
    ("Resellers", "Churn Status", None, "Active (ordered in the last 90 days), At risk (91–180) or Churned (over 180).",
     "VAR _d = [Days Since Last Order]\n"
     "RETURN SWITCH ( TRUE (), ISBLANK ( _d ), BLANK (), _d > 180, \"Churned\", _d > 90, \"At risk\", \"Active\" )"),
    ("Resellers", "Annual Churn Rate", PCT1,
     "Share of the resellers that ordered in FY2012 and did not order in FY2013 (the last two full fiscal years).",
     "VAR _prev = CALCULATETABLE ( VALUES ( Sales[Reseller Code] ), REMOVEFILTERS ( 'Date' ), 'Date'[Fiscal Year] = \"FY2012\" )\n"
     "VAR _cur = CALCULATETABLE ( VALUES ( Sales[Reseller Code] ), " + FY13 + " )\n"
     "RETURN DIVIDE ( COUNTROWS ( EXCEPT ( _prev, _cur ) ), COUNTROWS ( _prev ) )"),
    ("Resellers", "Avg Reseller Lifetime", '0.0 "yrs"', "Expected years a reseller keeps buying: 1 ÷ annual churn rate.",
     "DIVIDE ( 1, [Annual Churn Rate] )"),
    ("Resellers", "Resellers FY2013", INT, "Resellers with an order in FY2013.",
     "CALCULATE ( [Active Resellers], " + FY13 + " )"),
    ("Resellers", "Sales per Reseller FY2013", USD0, "FY2013 net sales per active reseller.",
     "DIVIDE ( CALCULATE ( [Net Sales], " + FY13 + " ), [Resellers FY2013] )"),
    ("Resellers", "Margin per Reseller FY2013", USD0, "FY2013 gross margin per active reseller.",
     "DIVIDE ( CALCULATE ( [Gross Margin], " + FY13 + " ), [Resellers FY2013] )"),
    ("Resellers", "Reseller CLV", USD0, "Lifetime value on gross margin: FY2013 margin per reseller × expected lifetime.",
     "[Margin per Reseller FY2013] * [Avg Reseller Lifetime]"),
    ("Resellers", "Reseller CLV on Sales", USD0, "Lifetime value on net sales: FY2013 sales per reseller × expected lifetime.",
     "[Sales per Reseller FY2013] * [Avg Reseller Lifetime]"),
    # ---------------------------------------------------------------- cohort & transition (static tables)
    ("Resellers", "Cohort Resellers", INT, "Resellers of the cohort who ordered in quarter N.",
     "SUM ( 'Reseller Cohort'[Cohort Active] )"),
    ("Resellers", "Cohort Retention %", PCT0, "Share of the cohort that ordered again N quarters after the first order.",
     "DIVIDE ( SUM ( 'Reseller Cohort'[Cohort Active] ), SUM ( 'Reseller Cohort'[Cohort Size] ) )"),
    ("Resellers", "Retention Color", None, "Heatmap fill for the cohort matrix.", HEAT.format(m="[Cohort Retention %]")),
    ("Resellers", "Transition Resellers", INT, "Resellers that moved from the row segment to the column segment.",
     "SUM ( 'RFM Transition'[Reseller Count] )"),
    ("Resellers", "Transition Share", PCT0, "Share of the FY2012 segment that ended FY2013 in the column segment.",
     "DIVIDE ( [Transition Resellers],\n"
     "    CALCULATE ( [Transition Resellers], REMOVEFILTERS ( 'RFM Transition'[To Segment], 'RFM Transition'[To Order] ) ) )"),
    ("Resellers", "Transition Color", None, "Heatmap fill for the transition matrix.", HEAT.format(m="[Transition Share]")),
    # ---------------------------------------------------------------- outliers
    ("Outliers", "Order Amount Z", "0.00",
     "Standard scores of the order's net sales against every order in the selection.",
     "VAR _t = CALCULATETABLE ( ADDCOLUMNS ( VALUES ( Sales[Order Number] ), \"@a\", [Net Sales] ), ALLSELECTED ( Sales ) )\n"
     "RETURN IF ( HASONEVALUE ( Sales[Order Number] ),\n"
     "    DIVIDE ( [Net Sales] - AVERAGEX ( _t, [@a] ), STDEVX.P ( _t, [@a] ) ) )"),
    ("Outliers", "Order P95", USD0, "95th percentile of order value in the selection.",
     "PERCENTILEX.INC ( ADDCOLUMNS ( VALUES ( Sales[Order Number] ), \"@a\", [Net Sales] ), [@a], 0.95 )"),
    ("Outliers", "Order P99", USD0, "99th percentile of order value in the selection.",
     "PERCENTILEX.INC ( ADDCOLUMNS ( VALUES ( Sales[Order Number] ), \"@a\", [Net Sales] ), [@a], 0.99 )"),
    ("Outliers", "Order Outlier", None, "Above P99 / Above P95 for a single order, against every order in the selection.",
     "VAR _t = CALCULATETABLE ( ADDCOLUMNS ( VALUES ( Sales[Order Number] ), \"@a\", [Net Sales] ), ALLSELECTED ( Sales ) )\n"
     "VAR _v = [Net Sales]\n"
     "RETURN IF ( HASONEVALUE ( Sales[Order Number] ),\n"
     "    SWITCH ( TRUE (), _v >= PERCENTILEX.INC ( _t, [@a], 0.99 ), \"Above P99\",\n"
     "        _v >= PERCENTILEX.INC ( _t, [@a], 0.95 ), \"Above P95\" ) )"),
    # ---------------------------------------------------------------- forecast
    ("Forecast", "Sales Linear Trend", "dynamic",
     "Least-squares line (LINESTX) through monthly net sales up to the last sale, evaluated for the month on the axis.",
     "VAR _last = [Last Sale Date]\n" + TREND_FIT + "\n"
     "VAR _ym = MAX ( 'Date'[Year Month] )\n"
     "RETURN IF ( HASONEVALUE ( 'Date'[Year Month] ), _a + _b * ( INT ( _ym / 100 ) * 12 + MOD ( _ym, 100 ) ) )"),
    ("Forecast", "Projected FY Attainment", PCT0,
     "Current fiscal year: sales so far plus the linear trend for the months left until the last quota month "
     "(quotas run to Dec 2013), ÷ the quota for the same months.",
     "VAR _last = [Last Sale Date]\n" + TREND_FIT + "\n"
     "VAR _fyStart = DATE ( YEAR ( _last ) - IF ( MONTH ( _last ) < 7, 1, 0 ), 7, 1 )\n"
     "VAR _qEnd = CALCULATE ( MAX ( Targets[Date] ), REMOVEFILTERS () )\n"
     "VAR _s = YEAR ( _last ) * 12 + MONTH ( _last ) + 1\n"
     "VAR _e = YEAR ( _qEnd ) * 12 + MONTH ( _qEnd )\n"
     "VAR _n = MAX ( 0, _e - _s + 1 )\n"
     "// _a + _b * m summed over the months _s.._e: an arithmetic series (closed form, no row iteration)\n"
     "VAR _future = _n * _a + _b * ( _s + _e ) * _n * 0.5\n"
     "VAR _actual = CALCULATE ( [Net Sales], REMOVEFILTERS ( 'Date' ), 'Date'[Date] >= _fyStart && 'Date'[Date] <= _last )\n"
     "VAR _all = CALCULATE ( SUM ( Targets[Daily Quota] ), REMOVEFILTERS ( 'Date' ), 'Date'[Date] >= _fyStart && 'Date'[Date] <= _qEnd )\n"
     "VAR _home = CALCULATE ( SUM ( Targets[Daily Quota] ), REMOVEFILTERS ( 'Date' ), 'Date'[Date] >= _fyStart && 'Date'[Date] <= _qEnd,\n"
     "    TREATAS ( VALUES ( Territories[Territory Key] ), Reps[Territory Key] ) )\n"
     "VAR _quota = IF ( ISCROSSFILTERED ( Territories ), _home, _all )\n"
     "RETURN IF ( NOT ( ISCROSSFILTERED ( Products ) || ISCROSSFILTERED ( Resellers ) ),\n"
     "    DIVIDE ( _actual + _future, _quota * ( 1 + [Stretch %] ) ) )"),
]

# Measures shown in the gallery and used by the report. (folder, name, format, description, DAX)
LAST_SALE = "CALCULATE ( MAX ( Sales[Order Date] ), REMOVEFILTERS () )"
MEASURES = [
    # ---------------------------------------------------------------- Sales
    ("Sales", "Net Sales", "dynamic", "Net line amount of clean order lines.", "SUM ( Sales[Amount] )"),
    ("Sales", "Orders", INT, "Distinct order numbers.", "DISTINCTCOUNT ( Sales[Order Number] )"),
    ("Sales", "Units", INT, "Units sold.", "SUM ( Sales[Quantity] )"),
    ("Sales", "Avg Order Value", USD0, "Sales per order.", "DIVIDE ( [Net Sales], [Orders] )"),
    ("Sales", "Sales PY", "dynamic", "Sales a year earlier on the Gregorian calendar (UDF Sales.PriorYear).",
     "Sales.PriorYear ( [Net Sales], 'Gregorian' )"),
    ("Sales", "Sales YoY %", PCT1, "Change against the same period a year earlier.",
     "DIVIDE ( [Net Sales] - [Sales PY], [Sales PY] )"),
    ("Sales", "Sales Share", PCT1, "Share of all reps' sales in the current filter (UDF Sales.ShareOf).",
     "Sales.ShareOf ( [Net Sales], Reps )"),
    ("Sales", "Last Sale Date", "dd mmm yyyy", "Date of the latest order in the data.", LAST_SALE),
    ("Sales", "Net Sales by Ship Date", "dynamic",
     "Net sales dated by ship date instead of order date: activates the inactive Sales[Ship Date] relationship.",
     "CALCULATE ( [Net Sales], USERELATIONSHIP ( Sales[Ship Date], 'Date'[Date] ) )"),
    ("Sales", "Avg Days to Ship", "#,0.0", "Average days from order to shipment, per order line.",
     "AVERAGEX ( Sales, DATEDIFF ( Sales[Order Date], Sales[Ship Date], DAY ) )"),
    ("Sales", "Active Resellers", INT,
     "Resellers with at least one sale in the current filter (the Sales table as a CALCULATE filter).",
     "CALCULATE ( COUNTROWS ( Resellers ), Sales )"),
    ("Sales", "Share of Visible Total", PCT1,
     "Share of the total the visual shows: ALLSELECTED keeps the slicers and drops the visual's own rows, "
     "so it works on whichever dimension Analyze By puts on the axis.",
     "DIVIDE ( [Net Sales], CALCULATE ( [Net Sales], ALLSELECTED () ) )"),
    ("Sales", "Sales R12M", "dynamic", "Rolling 12 months to the last date in the filter (DATESINPERIOD).",
     "CALCULATE ( [Net Sales], DATESINPERIOD ( 'Date'[Date], MAX ( 'Date'[Date] ), -12, MONTH ) )"),
    ("Sales", "Sales 3M Avg", "dynamic",
     "Average of this and the two previous months on the axis (WINDOW -2 REL to 0 REL). A model measure rather "
     "than a visual calculation, so Time & Fiscal and Rep detail share it.",
     "AVERAGEX (\n    WINDOW ( -2, REL, 0, REL, ALLSELECTED ( 'Date'[Year Month] ), ORDERBY ( 'Date'[Year Month] ) ),\n"
     "    [Net Sales] )"),
    ("Sales", "Avg Monthly Sales", "dynamic",
     "Average net sales per month in the filter (months without sales skipped). Constant line on Time & Fiscal.",
     "AVERAGEX ( VALUES ( 'Date'[Year Month] ), [Net Sales] )"),
    # ---------------------------------------------------------------- Profit
    ("Profit", "Gross Margin", "dynamic",
     "Net sales less units × standard cost (Products[Standard Cost] through RELATED). Hidden from Sales Manager "
     "by object-level security on the cost column, so it errors in that role.",
     "SUMX ( Sales, Sales[Amount] - Sales[Quantity] * RELATED ( Products[Standard Cost] ) )"),
    ("Profit", "Gross Margin %", PCT1, "Gross margin ÷ net sales.", "DIVIDE ( [Gross Margin], [Net Sales] )"),
    # ---------------------------------------------------------------- Target
    ("Target", "Stretch %", PCT0, "Selected what-if stretch on top of quota; 0% by default.",
     "SELECTEDVALUE ( 'Target Stretch'[Target Stretch], 0 )"),
    ("Target", "Quota", "dynamic",
     "Quota up to the last sale date. Quotas are per rep, so territories use the rep's home territory "
     "and product or reseller filters return blank.",
     "VAR _last = " + LAST_SALE + "\n"
     "VAR _q =\n    CALCULATE ( SUM ( Targets[Daily Quota] ), KEEPFILTERS ( 'Date'[Date] <= _last ) )\n"
     "RETURN\n    SWITCH ( TRUE (),\n"
     "        ISCROSSFILTERED ( Products ) || ISCROSSFILTERED ( Resellers ), BLANK (),\n"
     "        ISCROSSFILTERED ( Territories ),\n"
     "            CALCULATE ( SUM ( Targets[Daily Quota] ), KEEPFILTERS ( 'Date'[Date] <= _last ),\n"
     "                TREATAS ( VALUES ( Territories[Territory Key] ), Reps[Territory Key] ) ),\n"
     "        _q )"),
    ("Target", "Target", "dynamic", "Quota × (1 + stretch).", "[Quota] * ( 1 + [Stretch %] )"),
    ("Target", "Gap to Target", "dynamic", "Sales − target (UDF Sales.Gap).", "Sales.Gap ( [Net Sales], [Target] )"),
    ("Target", "Attainment", PCT0, "Sales ÷ target (UDF Sales.Attainment).", "Sales.Attainment ( [Net Sales], [Target] )"),
    ("Target", "Status", None, "On target / at risk / behind (UDF Sales.Status).", "Sales.Status ( [Attainment] )"),
    ("Target", "Status Color", None, "Colour for the status, used by conditional formatting.",
     "Sales.StatusColor ( [Status] )"),
    ("Target", "Status Label", None,
     "Status with a symbol, so it reads without colour (▲ on target, ● at risk, ▼ behind).",
     'SWITCH ( [Status], "On target", "▲ On target", "At risk", "● At risk", "Behind", "▼ Behind" )'),
    ("Target", "Target at 90%", "dynamic", "The at-risk threshold: 90% of target. Band for the bullet chart.",
     "[Target] * 0.9"),
    ("Target", "Reps On Target", INT, "Reps whose attainment is 100% or more.",
     "COUNTROWS ( FILTER ( VALUES ( Reps[Rep Key] ), [Attainment] >= 1 ) )"),
    ("Target", "Reps With Target", INT, "Reps that have a target in the current filter.",
     "COUNTROWS ( FILTER ( VALUES ( Reps[Rep Key] ), NOT ISBLANK ( [Target] ) ) )"),
    ("Target", "Target R12M", "dynamic", "Target over the rolling 12 months (DATESINPERIOD).",
     "CALCULATE ( [Target], DATESINPERIOD ( 'Date'[Date], MAX ( 'Date'[Date] ), -12, MONTH ) )"),
    ("Target", "Attainment R12M", PCT0, "Rolling 12-month sales ÷ rolling 12-month target.",
     "Sales.Attainment ( [Sales R12M], [Target R12M] )"),
    ("Target", "Attainment Prev FQ", PCT0,
     "Attainment in the fiscal quarter before the latest one in the filter (OFFSET -1 over the fiscal quarters).",
     "VAR _d = MIN ( [Last Sale Date], MAX ( 'Date'[Date] ) )\n"
     "VAR _q = LOOKUPVALUE ( 'Date'[Fiscal Quarter Number], 'Date'[Date], _d )\n"
     "RETURN\n    CALCULATE (\n        CALCULATE ( [Attainment],\n"
     "            OFFSET ( -1, ALLSELECTED ( 'Date'[Fiscal Quarter Number] ), ORDERBY ( 'Date'[Fiscal Quarter Number] ) ) ),\n"
     "        'Date'[Fiscal Quarter Number] = _q )"),
    ("Target", "Rep Share of Team", PCT1,
     "A rep's sales as a share of their manager's team (Org Level 1, the manager at the top of the rep's path). "
     "ALLEXCEPT keeps the team filter; VALUES puts it back when only the rep is on the rows.",
     "VAR _team = VALUES ( Reps[Org Level 1] )\n"
     "RETURN DIVIDE ( [Net Sales], CALCULATE ( [Net Sales], ALLEXCEPT ( Reps, Reps[Org Level 1] ), _team ) )"),
    ("Target", "Sales from At-Risk Reps", "dynamic", "Net sales of the reps whose status is At risk (90–99%).",
     'CALCULATE ( [Net Sales], FILTER ( VALUES ( Reps[Rep Key] ), [Status] = "At risk" ) )'),
    ("Target", "Active Reps (End of Period)", INT,
     "Reps with a target on the last day of the filter (LASTDATE): a semi-additive head count.",
     "CALCULATE ( DISTINCTCOUNT ( Targets[Rep Key] ), LASTDATE ( 'Date'[Date] ) )"),
    ("Target", "Top Rep", None, "Rep with the highest attainment in the current selection (INDEX 1 by attainment).",
     "VAR _top = INDEX ( 1, ALLSELECTED ( Reps[Rep] ), ORDERBY ( [Attainment], DESC ) )\nRETURN MAXX ( _top, Reps[Rep] )"),
    ("Target", "Top Rep in Region", None,
     "Best attainment among the reps of the same home region (INDEX 1, PARTITIONBY Reps[Home Region]). On a rep's "
     "row it names the leader of that rep's region; with no region in context it lists every region's leader.",
     "VAR _top =\n    INDEX ( 1, ALLSELECTED ( Reps[Rep], Reps[Home Region] ), ORDERBY ( [Attainment], DESC ), KEEP,\n"
     "        PARTITIONBY ( Reps[Home Region] ) )\n"
     'RETURN CONCATENATEX ( _top, Reps[Rep], ", " )'),
    # ---------------------------------------------------------------- Plan (synthetic category plan)
    ("Plan", "Category Target", "dynamic",
     "SYNTHETIC category plan (Category Plan table). Set per category and fiscal quarter, so rep, region and "
     "reseller filters return blank, and a month shows its whole quarter.",
     "IF ( ISCROSSFILTERED ( Reps ) || ISCROSSFILTERED ( Territories ) || ISCROSSFILTERED ( Resellers ),\n"
     "    BLANK (),\n    SUM ( 'Category Plan'[Plan Amount] ) )"),
    ("Plan", "Category Attainment", PCT0, "Sales in the planned quarters ÷ category target (synthetic plan).",
     "DIVIDE (\n    CALCULATE ( [Net Sales], KEEPFILTERS ( TREATAS ( VALUES ( 'Category Plan'[Fiscal Quarter] ), "
     "'Date'[Fiscal Quarter] ) ) ),\n    [Category Target] )"),
    # ---------------------------------------------------------------- Fiscal (calendar-based TI)
    ("Fiscal", "Sales FYTD", "dynamic", "Fiscal year to date (July–June), from the Fiscal calendar.",
     "CALCULATE ( [Net Sales], DATESYTD ( 'Fiscal' ) )"),
    ("Fiscal", "Target FYTD", "dynamic", "Target, fiscal year to date.", "CALCULATE ( [Target], DATESYTD ( 'Fiscal' ) )"),
    ("Fiscal", "Attainment FYTD", PCT0, "Sales FYTD ÷ target FYTD.",
     "Sales.Attainment ( [Sales FYTD], [Target FYTD] )"),
    ("Fiscal", "Sales FYTD PY", "dynamic", "Fiscal year to date a year earlier (UDF on the Fiscal calendar).",
     "Sales.PriorYear ( [Sales FYTD], 'Fiscal' )"),
    ("Fiscal", "Sales Running FY", "dynamic",
     "Running total from the first month of each fiscal year (WINDOW 1 ABS to 0 REL, PARTITIONBY fiscal year).",
     "SUMX (\n    WINDOW ( 1, ABS, 0, REL,\n        ALLSELECTED ( 'Date'[Fiscal Year Number], 'Date'[Year Month] ),\n"
     "        ORDERBY ( 'Date'[Year Month] ), KEEP, PARTITIONBY ( 'Date'[Fiscal Year Number] ) ),\n"
     "    [Net Sales] )"),
    *CUSTOMER_MEASURES,
    # ---------------------------------------------------------------- Narrative
    ("Narrative", "Headline", None, "One-sentence summary that follows the slicers.",
     'VAR _a = [Attainment]\n'
     'VAR _p = [Projected FY Attainment]\n'
     'VAR _proj = IF ( NOT ISBLANK ( _p ), " On the monthly trend, FY" & ( YEAR ( [Last Sale Date] ) + IF ( MONTH ( [Last Sale Date] ) >= 7, 1, 0 ) )\n'
     '    & " ends the quota months (to Dec 2013) at " & FORMAT ( _p, "0%" ) & " of target." )\n'
     'RETURN\n    IF ( ISBLANK ( _a ),\n'
     '        "Sales " & FORMAT ( [Net Sales] / 1e6, "$#,0.0" ) & "M. No target applies to this selection.",\n'
     '        "Sales " & FORMAT ( [Net Sales] / 1e6, "$#,0.0" ) & "M against a target of "\n'
     '            & FORMAT ( [Target] / 1e6, "$#,0.0" ) & "M: " & FORMAT ( _a, "0%" ) & ", " & LOWER ( [Status] ) & ". "\n'
     '            & [Reps On Target] & " of " & [Reps With Target] & " reps are on target. Best attainment: "\n'
     '            & [Top Rep] & "." & _proj )'),
]
# Hosted in Cleaning Log so object-level security hides them together with the table.
LOG_MEASURES = [
    ("Data quality", "Source Rows", INT, "Every row read from the branch files.", "SUM ( 'Cleaning Log'[Rows] )"),
    ("Data quality", "Rows Loaded", INT, "Rows that passed every rule and reached Sales.",
     "CALCULATE ( SUM ( 'Cleaning Log'[Rows] ), 'Cleaning Log'[Outcome] = \"Loaded\" )"),
    ("Data quality", "Rows Set Aside", INT, "Rows a cleaning rule kept out of Sales.",
     "[Source Rows] - [Rows Loaded]"),
    ("Data quality", "Amount Set Aside", USD0, "Amount on the order lines set aside; subtotal rows are not sales, so they don't count.",
     "CALCULATE ( SUM ( 'Cleaning Log'[Amount] ), NOT 'Cleaning Log'[Outcome] IN { \"Loaded\", \"Subtotal row\" } )"),
    ("Data quality", "Load Rate", PCT1, "Rows loaded ÷ source rows.", "DIVIDE ( [Rows Loaded], [Source Rows] )"),
]

SYNONYMS = {  # Q&A / Copilot terms
    "Net Sales": ["revenue", "turnover", "net sales"], "Target": ["quota", "goal", "plan"],
    "Attainment": ["achievement", "quota attainment", "percent to target"], "Reps": ["salespeople", "sales reps", "account managers"],
    "Resellers": ["customers", "dealers", "bike shops"], "Territories": ["regions", "areas"],
}


def q(name):
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else f"'{name}'"


def tag(*parts):
    """Deterministic lineage tags, so a rebuild only diffs where the model really changed."""
    return str(uuid.uuid5(NS, "/".join(parts)))


def block(text, indent):
    return [f"{T * indent}{l}" for l in text.split("\n")]


def m_text(name):
    return (PQ / f"{name}.pq").read_text().strip()


# BPA rules a measure breaks on purpose -> annotation BestPracticeAnalyzer_IgnoreRules (reason per entry).
TEXT_MEASURES = {"Status", "Status Color", "Status Label", "Top Rep", "Top Rep in Region", "Pareto Text",
                 "Reseller ABC Class", "Period Counted", "Churn Status", "Retention Color", "Transition Color",
                 "Order Outlier", "Headline"}  # return text (labels, hex colours): a format string means nothing
BPA_IGNORE = {
    **{m: ["PROVIDE_FORMAT_STRING_FOR_MEASURES", "INTEGER_FORMATTING"] for m in TEXT_MEASURES},
    "Last Sale Date": ["INTEGER_FORMATTING"],          # a date, formatted as a date
    "Avg Reseller Lifetime": ["INTEGER_FORMATTING"],   # '0.0 "yrs"': the unit is part of the format
    "Order Amount Z": ["INTEGER_FORMATTING"],          # a z-score needs two decimals
}
# "dynamic" measures ($K/$M by size, MONEY_FMT) also ignore INTEGER_FORMATTING: the rule only reads formatString.


def measure_lines(table, folder, name, fmt, desc, dax):
    lines = [f"{T}/// {desc}"] if desc else []
    lines += [f"{T}measure {q(name)} =", *block(dax, 3)] if "\n" in dax else [f"{T}measure {q(name)} = {dax}"]
    if fmt and fmt != "dynamic":
        lines.append(f"{T*2}formatString: {fmt}")
    lines += [f"{T*2}displayFolder: {folder}", f"{T*2}lineageTag: {tag(table, 'm', name)}", ""]
    # A dynamic format string is a child object, so it goes after the measure's own properties.
    lines += [f"{T*2}formatStringDefinition =", *block(MONEY_FMT, 4), ""] if fmt == "dynamic" else []
    if ignore := BPA_IGNORE.get(name) or (["INTEGER_FORMATTING"] if fmt == "dynamic" else None):
        lines += [f"{T*2}annotation BestPracticeAnalyzer_IgnoreRules = "
                  + json.dumps({"RuleIDs": ignore}, separators=(",", ":")), ""]
    return lines


def table_tmdl(name, query, cols):
    lines = [f"table {q(name)}", f"{T}lineageTag: {tag(name)}"]
    if name == "Date":
        lines.append(f"{T}dataCategory: Time")
    lines.append("")
    if name == "Cleaning Log":
        for m in LOG_MEASURES:
            lines += measure_lines(name, *m)
    for col, dtype, o in cols:
        lines += [f"{T}column {q(col)}", f"{T*2}dataType: {dtype}"]
        lines += [f"{T*2}isKey"] * bool(o.get("key")) + [f"{T*2}isHidden"] * bool(o.get("hidden"))
        if fmt := o.get("fmt") or {"int64": "0", "dateTime": "dd mmm yyyy"}.get(dtype):
            lines.append(f"{T*2}formatString: {fmt}")
        if cat := o.get("category"):
            lines.append(f"{T*2}dataCategory: {cat}")
        lines += [f"{T*2}lineageTag: {tag(name, col)}", f"{T*2}summarizeBy: {'sum' if o.get('sum') else 'none'}",
                  f"{T*2}sourceColumn: {o.get('src', col)}"]
        if sort := o.get("sort"):
            lines.append(f"{T*2}sortByColumn: {q(sort)}")
        lines.append("")
    for col, dtype, dax, o in CALC_COLUMNS.get(name, []):
        lines += [f"{T}column {q(col)} =", *block(dax, 3), f"{T*2}dataType: {dtype}"]
        lines += [f"{T*2}isHidden"] * bool(o.get("hidden"))
        lines += [f"{T*2}formatString: {o.get('fmt')}"] * bool(o.get("fmt"))
        lines += [f"{T*2}dataCategory: {o.get('category')}"] * bool(o.get("category"))
        lines += [f"{T*2}lineageTag: {tag(name, col)}", f"{T*2}summarizeBy: none"]
        lines += [f"{T*2}sortByColumn: {q(o.get('sort') or '')}"] * bool(o.get("sort")) + [""]
    for h, levels in HIERARCHIES.get(name, []):
        lines += [f"{T}hierarchy {q(h)}", f"{T*2}lineageTag: {tag(name, 'h', h)}", ""]
        for lv in levels:
            lines += [f"{T*2}level {q(lv)}", f"{T*3}lineageTag: {tag(name, 'h', h, lv)}", f"{T*3}column: {q(lv)}", ""]
    if name == "Date":
        for cal, groups in CALENDARS:
            lines += [f"{T}calendar {q(cal)}", ""]
            for unit, primary, assoc in groups:
                lines += [f"{T*2}calendarColumnGroup = {unit}", f"{T*3}primaryColumn: {q(primary)}"]
                lines += ([f"{T*3}associatedColumn: {q(assoc)}"] if assoc else []) + [""]
    lines += [f"{T}partition {q(name)} = m", f"{T*2}mode: import", f"{T*2}source ="]
    lines += [f"{T*4}{l}" for l in m_text(query).split("\n") if l.strip()]
    lines += ["", f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def calculated_tmdl(name, dax, cols):
    lines = [f"table {q(name)}", f"{T}lineageTag: {tag(name)}", ""]
    for col, src, dtype, extra in cols:
        lines += [f"{T}column {q(col)}", f"{T*2}dataType: {dtype}", f"{T*2}lineageTag: {tag(name, col)}",
                  f"{T*2}summarizeBy: none", f"{T*2}isNameInferred", f"{T*2}sourceColumn: {src}"]
        lines += [f"{T*2}{e}" for e in extra] + [""]
    src = [f"{T*2}source =", *block(dax, 4)] if "\n" in dax else [f"{T*2}source = {dax}"]
    return "\n".join(lines + [f"{T}partition {q(name)} = calculated", f"{T*2}mode: import", *src, ""])


def calc_group_tmdl():
    lines = ["table 'Time Intelligence'", f"{T}lineageTag: {tag('Time Intelligence')}", "",
             f"{T}calculationGroup", f"{T*2}precedence: 10", ""]
    for i, (name, dax, fmt) in enumerate(TIME_ITEMS):
        lines += [f"{T*2}calculationItem {q(name)} =", *block(dax, 4)] if "\n" in dax \
            else [f"{T*2}calculationItem {q(name)} = {dax}"]
        lines.append(f"{T*3}ordinal: {i}")
        if fmt:
            lines += [f"{T*3}formatStringDefinition = {fmt}"]
        lines.append("")
    lines += [f"{T}column Period", f"{T*2}dataType: string", f"{T*2}lineageTag: {tag('Time Intelligence', 'Period')}",
              f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: Name", f"{T*2}sortByColumn: Ordinal", "",
              f"{T}column Ordinal", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
              f"{T*2}lineageTag: {tag('Time Intelligence', 'Ordinal')}", f"{T*2}summarizeBy: sum",
              f"{T*2}sourceColumn: Ordinal", ""]
    return "\n".join(lines)


def field_parameter_tmdl(t, items):
    rows = ",\n".join(f'    ( "{label}", NAMEOF ( {ref} ), {i} )' for i, (label, ref) in enumerate(items))
    return "\n".join([
        f"table {q(t)}", f"{T}lineageTag: {tag(t)}", "",
        f"{T}column {q(t)}", f"{T*2}dataType: string", f"{T*2}lineageTag: {tag(t, 'a')}",
        f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: [Value1]", f"{T*2}sortByColumn: '{t} Order'", "",
        f"{T*2}relatedColumnDetails", f"{T*3}groupByColumn: '{t} Fields'", "",
        f"{T}column '{t} Fields'", f"{T*2}dataType: string", f"{T*2}isHidden", f"{T*2}lineageTag: {tag(t, 'f')}",
        f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: [Value2]", f"{T*2}sortByColumn: '{t} Order'", "",
        f"{T*2}extendedProperty ParameterMetadata =", f"{T*4}{{", f'{T*4}  "version": 3,', f'{T*4}  "kind": 2',
        f"{T*4}}}", "",
        f"{T}column '{t} Order'", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
        f"{T*2}lineageTag: {tag(t, 'o')}", f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: [Value3]", "",
        f"{T}partition {q(t)} = calculated", f"{T*2}mode: import", f"{T*2}source =",
        *block("{\n" + rows + "\n}", 4), ""])


def measures_tmdl():
    lines = ["table _Measures", f"{T}lineageTag: {tag('_Measures')}", ""]
    for m in MEASURES:
        lines += measure_lines("_Measures", *m)
    lines += [f"{T}column Value", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
              f"{T*2}lineageTag: {tag('_Measures', 'Value')}", f"{T*2}summarizeBy: none", f"{T*2}isNameInferred",
              f"{T*2}sourceColumn: [Value]", "",
              f"{T}partition _Measures = calculated", f"{T*2}mode: import", f"{T*2}source = {{1}}", ""]
    return "\n".join(lines)


def functions_tmdl():
    lines = []
    for name, desc, body in FUNCTIONS:
        lines.append(f"/// {desc}")
        lines += [f"function {q(name)} =", *block(body, 2)] if "\n" in body else [f"function {q(name)} = {body}"]
        lines += [f"{T}lineageTag: {tag('fn', name)}", ""]
    return "\n".join(lines)


def role_tmdl(name, r):
    lines = [f"role {q(name)}", f"{T}modelPermission: read", ""]
    for table in sorted(set(r["filters"]) | set(r["hide_columns"])):
        dax = r["filters"].get(table)
        lines.append(f"{T}tablePermission {q(table)}" + (f" = {dax}" if dax else ""))
        lines += [""] + [f"{T*2}columnPermission {q(c)} = none" for c in r["hide_columns"].get(table, [])] + [""]
    for table in r["hide_tables"]:
        lines += [f"{T}tablePermission {q(table)}", f"{T*2}metadataPermission: none", ""]
    return "\n".join(lines)


def perspective_tmdl(name, tables):
    lines = [f"perspective {q(name)}", ""]
    for t in tables:
        lines += [f"{T}perspectiveTable {q(t)}", f"{T*2}includeAll", ""]
    return "\n".join(lines)


def culture_tmdl():
    entities = {}
    for obj, terms in SYNONYMS.items():
        binding = {"ConceptualEntity": obj} if obj in TABLES else {"ConceptualEntity": "_Measures", "ConceptualProperty": obj}
        key = re.sub(r"\W", "_", obj.lower())
        entities[key] = {"Definition": {"Binding": binding}, "State": "Authored",
                         "Terms": [{obj.lower(): {"State": "Authored"}}] + [{t: {"State": "Authored"}} for t in terms]}
    doc = {"Version": "1.0.0", "Language": "en-US", "DynamicImprovement": "HighConfidence", "Entities": entities}
    return "\n".join(["cultureInfo en-US", "", f"{T}linguisticMetadata = {json.dumps(doc, ensure_ascii=False)}",
                      f"{T*2}contentType: json", ""])


def check():
    """Fail fast on name collisions and DAX references that don't resolve."""
    columns = {t: {c[0] for c in cols} for t, (_, cols) in TABLES.items()}
    for t, cols in CALC_COLUMNS.items():
        columns[t] |= {c[0] for c in cols}
    columns |= {t: {c[0] for c in cols} for t, (_, cols) in CALCULATED.items()}
    columns |= {"Analyze By": {"Analyze By"}, "Metric": {"Metric"}, "Time Intelligence": {"Period"}}
    measures = [m[1] for m in MEASURES + LOG_MEASURES]
    taken = {n.lower() for n in set().union(*columns.values())} | {t.lower() for t in columns}
    clash = {m for m in measures if m.lower() in taken or measures.count(m) > 1}
    assert not clash, f"measure names collide with tables/columns: {clash}"
    tables = set(columns) | {"Gregorian", "Fiscal"}
    fns = {f[0] for f in FUNCTIONS}
    params = {p for f in FUNCTIONS for p in re.findall(r"(\w+) :", f[2])}
    dax_all = [(m[1], m[4]) for m in MEASURES + LOG_MEASURES] \
        + [(f"role {r}", d) for r, v in ROLES.items() for d in v["filters"].values()] \
        + [(f"item {n}", d) for n, d, _ in TIME_ITEMS] + [(f"function {n}", b) for n, _, b in FUNCTIONS] \
        + [(c[0], c[2]) for cols in CALC_COLUMNS.values() for c in cols] + [("Analyze By", r) for _, r in ANALYZE_BY] + [("Metric", r) for _, r in METRIC] \
        + [(f"table {t}", d) for t, (d, _) in CALCULATED.items()]
    for name, dax in dax_all:
        # [X] may also be a column the expression defines itself ("@x", "Cohort", …) or a LINESTX output column.
        local = set(re.findall(r'"([^"]*)"', dax)) | {"Value", "Slope1", "Intercept"}
        dax = re.sub(r'"[^"]*"', '""', dax)
        for a, b, col in re.findall(r"(?:'([^']+)'|\b([A-Za-z_][\w ]*?))\[([^\]]+)\]", dax):
            assert col in columns.get(a or b, ()), f"[{name}] references missing column {a or b}[{col}]"
        for ref in re.findall(r"(?<![\w'\]])\[([^\]]+)\]", dax):
            assert ref in measures or ref in local, f"[{name}] references missing measure [{ref}]"
        for ref in re.findall(r"'([^'\n]+)'", re.sub(r"'[^'\n]+'\[[^\]]*\]", "", dax)):
            assert ref in tables, f"[{name}] references missing table/calendar '{ref}'"
        for fn in re.findall(r"\b([A-Z][A-Za-z]*\.[A-Za-z]+)\s*\(", dax):
            assert fn in fns or fn.isupper(), f"[{name}] calls unknown function {fn}"
        assert dax.count("(") == dax.count(")"), f"[{name}] unbalanced parentheses"
    for ft, fc, tt, tc, _ in RELATIONSHIPS:
        assert fc in columns[ft] and tc in columns[tt], (ft, fc, tt, tc)
    for _, groups in CALENDARS:
        for _, p, a in groups:
            assert p in columns["Date"] and (a is None or a in columns["Date"]), (p, a)
    for t, hs in HIERARCHIES.items():
        assert all(lv in columns[t] for _, lvs in hs for lv in lvs), t
    del params
    print(f"  ✓ {len(measures)} measures, {len(FUNCTIONS)} functions: every column/measure/function reference resolves")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", default="C:\\SalesInbox")
    inbox = ap.parse_args().inbox.rstrip("/\\")

    check()
    shutil.rmtree(OUT / "definition", ignore_errors=True)
    d = OUT / "definition"
    for sub in ("tables", "roles", "perspectives", "cultures"):
        (d / sub).mkdir(parents=True)

    (OUT / "definition.pbism").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",\n'
        '  "version": "4.2",\n  "settings": {}\n}\n')
    (OUT / ".platform").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",\n'
        '  "metadata": {"type": "SemanticModel", "displayName": "SalesTarget"},\n'
        '  "config": {"version": "2.0", "logicalId": "3c7f1a2e-9d84-4b6e-a2f5-6e1b0d9c4a73"}\n}\n')
    # 1702: user-defined functions and calendars need the current compatibility level.
    (d / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1702\n")
    all_tables = [*TABLES, *CALCULATED, "Time Intelligence", "Analyze By", "Metric", "_Measures"]
    (d / "model.tmdl").write_text("\n".join([
        "model Model", f"{T}culture: en-US", f"{T}defaultPowerBIDataSourceVersion: powerBI_V3",
        f"{T}discourageImplicitMeasures", f"{T}sourceQueryCulture: en-US", f"{T}dataAccessOptions",
        f"{T*2}legacyRedirects", f"{T*2}returnErrorValuesAsNull", "",
        "annotation __PBI_TimeIntelligenceEnabled = 0", "",
        *[f"ref table {q(t)}" for t in all_tables], "",
        *[f"ref role {q(r)}" for r in ROLES], "",
        *[f"ref perspective {q(p)}" for p in PERSPECTIVES], "",
        "ref cultureInfo en-US", ""]))

    expr = []
    for name in EXPRESSIONS:
        text = m_text(name)
        if name == "InboxFolder":
            text = f'"{inbox}"' + text.split("\n")[-1].split('"', 2)[2]
            expr += [f"expression {name} = {text}"]
        else:
            expr += [f"expression {q(name)} =", *[f"{T*2}{l}" for l in text.split("\n") if l.strip()]]
        kind = {"InboxFolder": "Text", "FiscalYearStart": "Number"}.get(name) or ("Function" if name.startswith("fn") else "Table")
        expr += [f"{T}lineageTag: {tag('expr', name)}", "", f"{T}annotation PBI_ResultType = {kind}", ""]
        if name == "Sales Staging":
            expr += [f"{T}annotation PBI_NavigationStepName = Navigation", ""]
    (d / "expressions.tmdl").write_text("\n".join(expr))
    (d / "functions.tmdl").write_text(functions_tmdl())

    for name, (query, cols) in TABLES.items():
        (d / "tables" / f"{name}.tmdl").write_text(table_tmdl(name, query, cols))
    for name, (dax, cols) in CALCULATED.items():
        (d / "tables" / f"{name}.tmdl").write_text(calculated_tmdl(name, dax, cols))
    (d / "tables" / "Time Intelligence.tmdl").write_text(calc_group_tmdl())
    (d / "tables" / "Analyze By.tmdl").write_text(field_parameter_tmdl("Analyze By", ANALYZE_BY))
    (d / "tables" / "Metric.tmdl").write_text(field_parameter_tmdl("Metric", METRIC))
    (d / "tables" / "_Measures.tmdl").write_text(measures_tmdl())
    for name, r in ROLES.items():
        (d / "roles" / f"{name}.tmdl").write_text(role_tmdl(name, r))
    for name, tables in PERSPECTIVES.items():
        (d / "perspectives" / f"{name}.tmdl").write_text(perspective_tmdl(name, tables))
    (d / "cultures" / "en-US.tmdl").write_text(culture_tmdl())

    rel = []
    for ft, fc, tt, tc, o in RELATIONSHIPS:
        rel += [f"relationship {tag('rel', ft, fc)}", f"{T}isActive: {str(o.get('active', True)).lower()}",
                f"{T}crossFilteringBehavior: oneDirection"]
        rel += [f"{T}fromCardinality: many", f"{T}toCardinality: many"] if o.get("m2m") else []
        rel += [f"{T}fromColumn: {q(ft)}.{q(fc)}", f"{T}toColumn: {q(tt)}.{q(tc)}", ""]
    (d / "relationships.tmdl").write_text("\n".join(rel))
    # DAX query view tabs: one .dax per tab, tab order in .pbi/daxQueries.json (Power BI projects layout).
    from dax_queries import QUERIES, ASSERTS
    dq = OUT / "DAXQueries"
    shutil.rmtree(dq, ignore_errors=True)
    (dq / ".pbi").mkdir(parents=True)
    for tab, text in QUERIES.items():
        (dq / f"{tab}.dax").write_text(text)
    for tab, spec in ASSERTS.items():
        (dq / f"{tab}.assert.json").write_text(json.dumps(spec, indent=2) + "\n")
    (dq / ".pbi" / "daxQueries.json").write_text(json.dumps(
        {"version": "1.0.0", "tabOrder": list(QUERIES), "defaultTab": next(iter(QUERIES))}, indent=2) + "\n")
    print(f"  ✓ wrote {dq.relative_to(ROOT)} ({len(QUERIES)} saved DAX queries, {len(ASSERTS)} with asserts)")
    print(f"  ✓ wrote {OUT.relative_to(ROOT)} ({len(all_tables)} tables, {len(RELATIONSHIPS)} relationships, "
          f"{len(FUNCTIONS)} functions, {len(CALENDARS)} calendars, {len(ROLES)} roles, {len(PERSPECTIVES)} perspectives)")


if __name__ == "__main__":
    main()
