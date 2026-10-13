"""Generate the Power BI semantic model (TMDL) from the DuckDB mart schema.

    uv run python src/build_model.py [--data-dir PATH]

Column types are read from DuckDB, so the model never drifts from the SQL.
--data-dir is the folder Power BI reads the Parquet marts from (default: this
repo's data/marts); change it later in Power BI via Transform data > Edit parameters > DataFolder.

Beyond the imported star schema the model carries:
  * incremental refresh policies (RangeStart/RangeEnd) on both fact tables, with change
    detection on Opportunities via max(modified_at)
  * a calculation group (Date Basis: Closed / Engaged / Created, via USERELATIONSHIP)
  * a field parameter (Analyze By: Agent / Manager / Region / Product / Sector)
  * what-if tables (Stale Threshold days, Win Probability)
  * row-level security (dynamic Sales Manager role + static West Region role) and object-level
    security (External Partner role cannot see the Sync Runs table or account financials)
  * DAX user-defined functions (functions.tmdl), a Gregorian calendar on Date for calendar-based
    time intelligence, and a Time Intelligence calculation group with a dynamic format string
  * window-function measures (OFFSET / WINDOW / INDEX), an ISINSCOPE share-of-parent measure, and a
    many-to-many path Steps -> Funnel (bridge) -> Opportunities via CROSSFILTER
  * hierarchies, perspectives and Q&A synonyms; best-practice checks run before anything is written
"""
import argparse
import json
import re
import shutil
import uuid
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "powerbi" / "SalesPipeline.SemanticModel"
FABRIC_OUT = ROOT / "powerbi" / "SalesPipeline.Fabric.SemanticModel"
T = "\t"

TYPES = {  # duckdb -> (TMDL dataType, Power Query type)
    "BIGINT": ("int64", "Int64.Type"), "INTEGER": ("int64", "Int64.Type"),
    "DOUBLE": ("double", "type number"), "DATE": ("dateTime", "type date"),
    "TIMESTAMP": ("dateTime", "type datetime"), "BOOLEAN": ("boolean", "type logical"),
    "VARCHAR": ("string", "type text"),
}
USD0 = "\\$#,0;(\\$#,0);\\$0"
DEFAULT_FMT = {"int64": "0", "double": "#,0.00", "dateTime": "dd mmm yyyy"}  # no numeric column without a format
PCT0, PCT1, INT = "0%", "#,0.0%;-#,0.0%;#,0.0%", "#,0"  # PCT1 = BPA PERCENTAGE_FORMATTING

# Account calculated columns: (name, dataType, format, DAX)
ACCOUNT_CALC = [
    ("Lifetime Won Revenue", "double", USD0,
     "CALCULATE ( [Won Revenue] )"),  # context transition: the row becomes a filter on Account
    ("Deal Count", "int64", INT, "COUNTROWS ( RELATEDTABLE ( Opportunities ) )"),
    # Fixed A/B/C on all-time revenue (for slicers and axes); the measure Account ABC follows the slicers instead.
    ("Static ABC", "string", None,
     "VAR _rev = Account[Lifetime Won Revenue]\n"
     "VAR _total = SUMX ( FILTER ( Account, Account[Account ID] <> \"0\" ), Account[Lifetime Won Revenue] )\n"
     "VAR _cum = SUMX ( FILTER ( Account, Account[Account ID] <> \"0\" && Account[Lifetime Won Revenue] >= _rev ), "
     "Account[Lifetime Won Revenue] )\n"
     "VAR _share = DIVIDE ( _cum, _total )\n"
     "RETURN\n    SWITCH ( TRUE (), Account[Account ID] = \"0\", \"n/a\", _rev = 0, \"C\", _share <= 0.8, \"A\", "
     "_share <= 0.95, \"B\", \"C\" )"),
]

# Power BI table -> (mart table, config)
TABLES = {
    "Date": ("dim_date", dict(
        key="date", date_table=True, sort={"quarter": "quarter_sort", "month": "month_sort"},
        hidden=["quarter_sort", "month_sort"], fmt={"month_start": "mmm yyyy", "week_start": "dd mmm yyyy"})),
    "Account": ("dim_account", dict(
        key="account_id", hidden=["account_id", "size_sort"], sort={"size_band": "size_sort"},
        fmt={"annual_revenue": USD0, "employees": INT}, category={"country": "Country"},
        names={"is_subsidiary": "Is Subsidiary"},
        # Calculated columns only where a column is required (axis, slicer, static grouping); everything that
        # should follow the slicers stays a measure. Import only: a Dual table cannot hold aggregating columns.
        calc_cols=ACCOUNT_CALC)),
    "Product": ("dim_product", dict(
        key="product_id", hidden=["product_id", "product_sort"], sort={"product": "product_sort"},
        fmt={"list_price": USD0})),
    "Agent": ("dim_agent", dict(
        key="owner_id", hidden=["owner_id"], names={"regional_office": "Region", "agent": "Sales Agent"})),
    "Stage": ("dim_stage", dict(
        key="stage_key", hidden=["stage_key", "stage_sort"], sort={"stage": "stage_sort"})),
    "Cycle Band": ("dim_cycle_band", dict(
        key="cycle_band", sort={"cycle_band": "cycle_sort"}, hidden=["cycle_sort", "low_days", "high_days"])),
    "Age Band": ("dim_age_band", dict(
        key="age_band", sort={"age_band": "age_sort"}, hidden=["age_sort", "low_days", "high_days"])),
    "Opportunities": ("fact_opportunity", dict(
        key="opportunity_id",
        hidden=["owner_id", "account_id", "product_id", "stage_key", "cycle_band", "age_band"],
        fmt={"close_value": USD0, "list_price": USD0, "open_value": USD0, "price_realisation": PCT0},
        # modified_at only feeds change detection, which pollingExpression reads from the source itself:
        # keeping it in the model would store a datetime column (391 distinct values) no visual uses.
        drop=["modified_at"],
        names={"price_realisation": "Realisation vs List"},
        # no implicit sums (BPA NUMERIC_COLUMN_SUMMARIZE_BY): measures do the aggregating
        # Partitions by created date. Old deals stay open for over a year here (oldest: ~14 months),
        # so the refresh window is 15 months; max(modified_at) per partition skips untouched months.
        refresh=dict(column="created_date", rolling=("year", 3), incremental=("month", 15),
                     polling="modified_at", hybrid=True))),
    "Pipeline Snapshots": ("fact_pipeline_snapshot", dict(
        hidden=["opportunity_id", "owner_id", "account_id", "product_id", "stage_key", "age_band"],
        fmt={"open_value": USD0, "close_value": USD0},
        names={"age_days": "Snapshot Age Days", "open_value": "Snapshot Open Value",
               "close_value": "Snapshot Close Value"},
        # Snapshots never change once taken: refresh only the latest month.
        refresh=dict(column="snapshot_date", rolling=("year", 3), incremental=("month", 1)))),
    "Funnel": ("fact_funnel", dict(
        hidden=["opportunity_id", "owner_id", "account_id", "product_id", "step_sort", "step"],
        sort={"step": "step_sort"}, names={"step": "Funnel Step"})),
    # Stage at the previous month-end sync -> stage at this one (transition matrix on Diagnosis).
    "Stage Transitions": ("fact_stage_transition", dict(
        hidden=["opportunity_id", "owner_id", "account_id", "product_id", "from_sort", "to_sort"],
        sort={"from_stage": "from_sort", "to_stage": "to_sort"},
        names={"snapshot_date": "Transition Date", "from_stage": "From Stage", "to_stage": "To Stage"})),
    "Sync Runs": ("sync_runs", dict(
        fmt={"started_at": "dd mmm yyyy hh:nn", "watermark_before": "dd mmm yyyy", "watermark_after": "dd mmm yyyy",
             "seconds": "0.0", "deals_seen": INT, "deal_versions_added": INT},
        names={"as_of": "Sync Date", "rate_limited": "Rate Limited 429", "server_errors": "Server Errors 5xx",
               "deal_versions_added": "Versions Added", "deals_seen": "Deals Seen", "seconds": "Run Seconds"})),
    "Manager Access": ("manager_access", dict(
        hidden_table=True, names={"regional_office": "Region"})),
}

RELATIONSHIPS = [  # (from table, from col, to table, to col, active)
    ("Opportunities", "closed_date", "Date", "date", True),
    ("Opportunities", "engaged_date", "Date", "date", False),   # Date Basis = Engaged
    ("Opportunities", "created_date", "Date", "date", False),   # Date Basis = Created
    ("Opportunities", "owner_id", "Agent", "owner_id", True),
    ("Opportunities", "account_id", "Account", "account_id", True),
    ("Opportunities", "product_id", "Product", "product_id", True),
    ("Opportunities", "stage_key", "Stage", "stage_key", True),
    ("Opportunities", "cycle_band", "Cycle Band", "cycle_band", True),
    ("Opportunities", "age_band", "Age Band", "age_band", True),
    ("Pipeline Snapshots", "snapshot_date", "Date", "date", True),
    ("Pipeline Snapshots", "owner_id", "Agent", "owner_id", True),
    ("Pipeline Snapshots", "account_id", "Account", "account_id", True),
    ("Pipeline Snapshots", "product_id", "Product", "product_id", True),
    ("Pipeline Snapshots", "stage_key", "Stage", "stage_key", True),
    ("Pipeline Snapshots", "age_band", "Age Band", "age_band", True),
    # Funnel reaches every dimension through its deal.
    ("Funnel", "opportunity_id", "Opportunities", "opportunity_id", True),
    ("Stage Transitions", "snapshot_date", "Date", "date", True),
    ("Stage Transitions", "owner_id", "Agent", "owner_id", True),
    ("Stage Transitions", "account_id", "Account", "account_id", True),
    ("Stage Transitions", "product_id", "Product", "product_id", True),
    # Many-to-many: a deal reaches several steps and a step holds many deals. Funnel is the bridge;
    # measures that need Steps to filter Opportunities switch the bridge to both directions with CROSSFILTER.
    ("Funnel", "step", "Steps", "Step", True),
]

HIERARCHIES = {
    "Date": [("Calendar", ["Year", "Quarter", "Month", "Date"])],
    "Agent": [("Sales Team", ["Region", "Manager", "Sales Agent"])],
}

# Calendar-based time intelligence: (calendar, [(time unit, primary column, associated column)])
CALENDARS = [
    ("Gregorian", [("year", "Year", None), ("quarter", "Quarter Sort", "Quarter"),
                   ("month", "Month Sort", "Month"), ("date", "Date", None)]),
]

# (name, description, signature => body)
FUNCTIONS = [
    ("Kpi.Rate", "Part ÷ whole; blank when the whole is empty.",
     "( part : NUMERIC, whole : NUMERIC ) => DIVIDE ( part, whole )"),
    ("Kpi.ShareOf", "A measure as a share of the same measure with the filters on one column or table removed.",
     "( value : SCALAR NUMERIC EXPR, scope : ANYREF ) => DIVIDE ( value, CALCULATE ( value, REMOVEFILTERS ( scope ) ) )"),
    ("Kpi.Lift", "Win rate inside a segment ÷ win rate of everything else in the visual (1.0 = no difference).",
     "( won : NUMERIC, closed : NUMERIC, allWon : NUMERIC, allClosed : NUMERIC ) =>\n"
     "\tDIVIDE ( DIVIDE ( won, closed ), DIVIDE ( allWon - won, allClosed - closed ) )"),
    ("Kpi.PriorMonth", "A measure one month back on the calendar passed in.",
     "( value : SCALAR NUMERIC EXPR, cal : CALENDARREF ) => CALCULATE ( value, DATEADD ( cal, -1, MONTH ) )"),
    ("Kpi.PriorQuarter", "A measure one quarter back on the calendar passed in.",
     "( value : SCALAR NUMERIC EXPR, cal : CALENDARREF ) => CALCULATE ( value, DATEADD ( cal, -1, QUARTER ) )"),
    ("Kpi.PriorYear", "A measure one year back on the calendar passed in.",
     "( value : SCALAR NUMERIC EXPR, cal : CALENDARREF ) => CALCULATE ( value, DATEADD ( cal, -1, YEAR ) )"),
    ("Fmt.Money", "An amount as text in millions, e.g. $3.5m.",
     '( amount : NUMERIC ) => FORMAT ( amount / 1e6, "$#,0.0" ) & "m"'),
]

# Time Intelligence calculation group on the Gregorian calendar: (item, DAX, format string expression or None)
TIME_ITEMS = [
    ("Current", "SELECTEDMEASURE ()", None),
    ("MTD", "CALCULATE ( SELECTEDMEASURE (), DATESMTD ( 'Gregorian' ) )", None),
    ("QTD", "CALCULATE ( SELECTEDMEASURE (), DATESQTD ( 'Gregorian' ) )", None),
    ("YTD", "CALCULATE ( SELECTEDMEASURE (), DATESYTD ( 'Gregorian' ) )", None),
    ("Prior Month", "Kpi.PriorMonth ( SELECTEDMEASURE (), 'Gregorian' )", None),
    ("MoM %", "VAR _pm = Kpi.PriorMonth ( SELECTEDMEASURE (), 'Gregorian' )\n"
              "RETURN DIVIDE ( SELECTEDMEASURE () - _pm, _pm )", '"+0.0%;-0.0%;0.0%"'),
    ("Prior Year", "Kpi.PriorYear ( SELECTEDMEASURE (), 'Gregorian' )", None),
    ("YoY %", "VAR _py = Kpi.PriorYear ( SELECTEDMEASURE (), 'Gregorian' )\n"
              "RETURN DIVIDE ( SELECTEDMEASURE () - _py, _py )", '"+0.0%;-0.0%;0.0%"'),
]

# DAX-only tables: name -> (DAX source, [(column, sourceColumn, dataType, extra lines)])
CALCULATED = {
    "Steps": (
        'DATATABLE ( "Step", STRING, "Step Order", INTEGER, '
        '{ { "Created", 1 }, { "Engaged", 2 }, { "Closed", 3 }, { "Won", 4 } } )',
        [("Step", "[Step]", "string", ["isKey", "sortByColumn: 'Step Order'"]),
         ("Step Order", "[Step Order]", "int64", ["isHidden", "formatString: 0"])]),
    "Stale Threshold": (
        'SELECTCOLUMNS ( GENERATESERIES ( 30, 180, 1 ), "Stale Threshold", [Value] )',
        [("Stale Threshold", "[Stale Threshold]", "int64", ["formatString: 0"])]),
    "Win Probability": (
        'SELECTCOLUMNS ( GENERATESERIES ( 0.4, 0.8, 0.01 ), "Win Probability", [Value] )',
        [("Win Probability", "[Win Probability]", "double", [f"formatString: {PCT0}"])]),
    # Links of the Sankey on Drivers. Static on purpose: the width of each link is the measure Flow Deals,
    # so the flow follows the slicers (a calculated table over Opportunities would be frozen at refresh).
    "Stage Flow": (
        'DATATABLE ( "From", STRING, "To", STRING, "Flow Order", INTEGER, '
        '{ { "Created", "Engaged", 1 }, { "Created", "Still prospecting", 2 }, { "Engaged", "Won", 3 }, '
        '{ "Engaged", "Lost", 4 }, { "Engaged", "Still engaging", 5 } } )',
        [("From", "[From]", "string", []), ("To", "[To]", "string", []),
         ("Flow Order", "[Flow Order]", "int64", ["isHidden", "formatString: 0"])]),
    # Disconnected revenue bands for dynamic segmentation: an account's band follows the slicers (Accounts in Segment).
    "Account Segment": (
        'DATATABLE ( "Segment", STRING, "Low", DOUBLE, "High", DOUBLE, "Segment Order", INTEGER, '
        '{ { "Under $50k", 0, 50000, 1 }, { "$50k–100k", 50000, 100000, 2 }, { "$100k–150k", 100000, 150000, 3 }, '
        '{ "$150k+", 150000, 1e12, 4 } } )',
        [("Segment", "[Segment]", "string", ["isKey", "sortByColumn: 'Segment Order'"]),
         ("Low", "[Low]", "double", ["isHidden", f"formatString: {USD0}"]),
         ("High", "[High]", "double", ["isHidden", f"formatString: {USD0}"]),
         ("Segment Order", "[Segment Order]", "int64", ["isHidden", "formatString: 0"])]),
}

ANALYZE_BY = [("Sales agent", "'Agent'[Sales Agent]"), ("Manager", "'Agent'[Manager]"),
              ("Region", "'Agent'[Region]"), ("Product", "'Product'[Product]"), ("Sector", "'Account'[Sector]")]
# Field parameter of measures: the Overview bar chart switches what it measures, not only what it groups by.
METRIC = [("Won revenue", "[Won Revenue]"), ("Win rate", "[Win Rate]"), ("Avg deal size", "[Avg Deal Size]"),
          ("Avg cycle days", "[Avg Cycle Days]"), ("Deals", "[Deals]")]

# Calculation group. Closed is the model's active relationship, so it leaves the context as-is.
DATE_BASIS = [
    ("Closed", "SELECTEDMEASURE ()", None),
    ("Engaged", "CALCULATE ( SELECTEDMEASURE (), USERELATIONSHIP ( Opportunities[Engaged Date], 'Date'[Date] ) )", None),
    ("Created", "CALCULATE ( SELECTEDMEASURE (), USERELATIONSHIP ( Opportunities[Created Date], 'Date'[Date] ) )", None),
]

ROLES = {
    # A manager sees their own team; a row with no manager grants the whole region.
    "Sales Manager": {"filters": {
        "Agent": "VAR _u = USERPRINCIPALNAME () RETURN NOT ISEMPTY ( FILTER ( 'Manager Access', "
                 "'Manager Access'[User Email] = _u && 'Manager Access'[Region] = Agent[Region] "
                 "&& ( 'Manager Access'[Manager] = \"\" || 'Manager Access'[Manager] = Agent[Manager] ) ) )",
        "Manager Access": "'Manager Access'[User Email] = USERPRINCIPALNAME ()",
    }, "hide_tables": [], "hide_columns": {}},
    "West Region": {"filters": {"Agent": "Agent[Region] = \"West\""}, "hide_tables": [], "hide_columns": {}},
    # Object-level security: a reseller sees every deal but not the API log or the accounts' financials.
    "External Partner": {"filters": {}, "hide_tables": ["Sync Runs", "Sync Run Events", "Agent Login Check"],
                         "hide_columns": {"Account": ["Annual Revenue", "Employees"]}},
}

PERSPECTIVES = {
    "Sales": ["Opportunities", "Funnel", "Steps", "Pipeline Snapshots", "Date", "Agent", "Account", "Product", "Stage",
              "Cycle Band", "Age Band", "Date Basis", "Time Intelligence", "Analyze By", "Metric", "Stage Flow", "Stale Threshold",
              "Stage Transitions", "Account Segment",
              "Win Probability", "_Measures"],
    "Data Ops": ["Sync Runs", "Sync Run Events", "Source Reconciliation", "Agent Login Check", "Data Dictionary", "Pipeline Snapshots",
                 "Opportunities", "Date", "Agent", "Manager Access", "_Measures"],
}

SYNONYMS = {  # Q&A / Copilot terms
    "Opportunities": ["deals", "opportunity", "pipeline deals"], "Won Revenue": ["bookings", "sales", "revenue"],
    "Win Rate": ["close rate", "conversion rate", "hit rate"], "Agent": ["sales reps", "reps", "sellers", "owners"],
    "Account": ["customers", "clients", "companies"], "Open Pipeline": ["pipeline", "open value"],
    "Stale Deals": ["old deals", "stuck deals", "zombie deals"], "Sync Runs": ["api syncs", "extraction runs", "loads"],
}

# ---------------------------------------------------------------- Power Query
# Staging queries over the raw Maven CSVs. Not loaded (TMDL `expression`, the way Desktop stores a query with
# "Enable load" off); the loaded tables below Reference them. Every step has a name so Applied Steps reads as a recipe.
CSV = 'Csv.Document(File.Contents(RawFolder & "{f}"), [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv])'
STAGING = {
    "raw_pipeline": [
        f"    Source = {CSV.format(f='sales_pipeline.csv')},",
        "    PromotedHeaders = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),",
        '    KeptColumns = Table.SelectColumns(PromotedHeaders, {"opportunity_id", "sales_agent", "product", "account",',
        '        "deal_stage", "engage_date", "close_date", "close_value"}),',
        '    DroppedBlankRows = Table.SelectRows(KeptColumns, each [deal_stage] <> null and [deal_stage] <> ""),',
        '    Typed = Table.TransformColumnTypes(DroppedBlankRows, {{"engage_date", type date}, {"close_date", type date},',
        '        {"close_value", type number}}, "en-US"),',
        '    ErrorsReplaced = Table.ReplaceErrorValues(Typed, {{"close_value", null}}),',
        '    RemovedErrorRows = Table.RemoveRowsWithErrors(ErrorsReplaced, {"engage_date", "close_date"}),',
        '    FixedTypos = Table.ReplaceValue(RemovedErrorRows, "GTXPro", "GTX Pro", Replacer.ReplaceValue, {"product"}),',
        '    FilledAccount = Table.ReplaceValue(FixedTypos, each [account], each if [account] = null or [account] = ""',
        '        then "(no account)" else [account], Replacer.ReplaceValue, {"account"})',
        "in", "    FilledAccount"],
    # Keep Errors: the rows a type change would break, for audit (empty on today's file).
    "raw_pipeline_errors": [
        f"    Source = {CSV.format(f='sales_pipeline.csv')},",
        "    PromotedHeaders = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),",
        '    Typed = Table.TransformColumnTypes(PromotedHeaders, {{"engage_date", type date}, {"close_date", type date},',
        '        {"close_value", type number}}, "en-US"),',
        '    KeptErrors = Table.SelectRowsWithErrors(Typed, {"engage_date", "close_date", "close_value"})',
        "in", "    KeptErrors"],
    "raw_accounts": [
        f"    Source = {CSV.format(f='accounts.csv')},",
        "    PromotedHeaders = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),",
        '    Typed = Table.TransformColumnTypes(PromotedHeaders, {{"year_established", Int64.Type}, {"revenue", type number},',
        '        {"employees", Int64.Type}}, "en-US"),',
        '    ErrorsReplaced = Table.ReplaceErrorValues(Typed, {{"revenue", null}, {"employees", null}}),',
        '    FixedSector = Table.ReplaceValue(ErrorsReplaced, "technolgy", "technology", Replacer.ReplaceValue, {"sector"}),',
        '    FixedCountry = Table.ReplaceValue(FixedSector, "Philipines", "Philippines", Replacer.ReplaceValue, {"office_location"})',
        "in", "    FixedCountry"],
    "raw_teams": [
        f"    Source = {CSV.format(f='sales_teams.csv')},",
        "    PromotedHeaders = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),",
        "    TrimmedHeaders = Table.TransformColumnNames(PromotedHeaders, Text.Trim),",
        '    Cleaned = Table.TransformColumns(TrimmedHeaders, {{"sales_agent", each Text.Clean(Text.Trim(_)), type text},',
        '        {"manager", each Text.Clean(Text.Trim(_)), type text}, {"regional_office", each Text.Clean(Text.Trim(_)), type text}}),',
        '    Renamed = Table.RenameColumns(Cleaned, {{"sales_agent", "Sales Agent"}, {"manager", "Manager"}, {"regional_office", "Region"}}),',
        '    Typed = Table.TransformColumnTypes(Renamed, {{"Sales Agent", type text}, {"Manager", type text}, {"Region", type text}}),',
        '    RemovedDuplicates = Table.Distinct(Typed, {"Sales Agent"})',
        "in", "    RemovedDuplicates"],
}
NZ = "(if {c} = null then 0 else {c})"
# Loaded M tables: name -> (columns [(name, dataType, extra lines)], M steps, needs the raw CSV folder)
M_TABLES = {
    # Question 4, "can the data be trusted?": the CSV the CRM was seeded from vs the mart rebuilt from API syncs.
    "Source Reconciliation": ([
        ("Source", "string", []), ("Stage", "string", ["sortByColumn: 'Stage Sort'"]),
        ("Stage Sort", "int64", ["isHidden", "formatString: 0"]), ("Deal Rows", "int64", [f"formatString: {INT}"]),
        ("Close Value", "double", [f"formatString: {USD0}"]), ("Raw Deal Rows", "int64", ["isHidden", f"formatString: {INT}"]),
        ("Raw Close Value", "double", ["isHidden", f"formatString: {USD0}"]),
        ("Deals Diff", "int64", ["formatString: +#,0;-#,0;0"]), ("Value Diff", "double", ["formatString: +#,0;-#,0;0"])], [
        "    // Raw side: the seed CSV, cleaned in raw_pipeline (Reference, not Duplicate)",
        "    RawSource = raw_pipeline,",
        '    RawGrouped = Table.Group(RawSource, {"deal_stage"}, {{"Deal Rows", each Table.RowCount(_), Int64.Type},',
        '        {"Close Value", each List.Sum([close_value]), type nullable number}}),',
        '    RawRenamed = Table.RenameColumns(RawGrouped, {{"deal_stage", "Stage"}}),',
        '    RawTotals = Table.AddColumn(RawRenamed, "Source", each "Raw CSV", type text),',
        "    // Mart side: the deals as the month-end API syncs rebuilt them",
        '    Stages = Parquet.Document(File.Contents(DataFolder & "dim_stage.parquet")),',
        '    Mart = Parquet.Document(File.Contents(DataFolder & "fact_opportunity.parquet")),',
        '    MartGrouped = Table.Group(Mart, {"stage_key"}, {{"Deal Rows", each Table.RowCount(_), Int64.Type},',
        '        {"Close Value", each List.Sum([close_value]), type nullable number}}),',
        '    MartLabelled = Table.NestedJoin(MartGrouped, {"stage_key"}, Stages, {"stage_key"}, "StageRow", JoinKind.LeftOuter),',
        '    MartExpanded = Table.ExpandTableColumn(MartLabelled, "StageRow", {"stage"}, {"Stage"}),',
        '    MartTotals = Table.AddColumn(Table.RemoveColumns(MartExpanded, {"stage_key"}), "Source", each "Mart (API syncs)", type text),',
        "    // Append both sides, then Merge the raw totals back on to show the difference per stage",
        "    Appended = Table.Combine({RawTotals, MartTotals}),",
        '    MergedRaw = Table.NestedJoin(Appended, {"Stage"}, RawTotals, {"Stage"}, "RawRow", JoinKind.LeftOuter),',
        '    ExpandedRaw = Table.ExpandTableColumn(MergedRaw, "RawRow", {"Deal Rows", "Close Value"}, {"Raw Deal Rows", "Raw Close Value"}),',
        '    AddedDealsDiff = Table.AddColumn(ExpandedRaw, "Deals Diff", each [Deal Rows] - [Raw Deal Rows], Int64.Type),',
        f'    AddedValueDiff = Table.AddColumn(AddedDealsDiff, "Value Diff", each {NZ.format(c="[Close Value]")} - '
        f'{NZ.format(c="[Raw Close Value]")}, type number),',
        "    // Merge to Stage for the funnel order",
        '    MergedStage = Table.NestedJoin(AddedValueDiff, {"Stage"}, Stages, {"stage"}, "StageSort", JoinKind.LeftOuter),',
        '    WithSort = Table.ExpandTableColumn(MergedStage, "StageSort", {"stage_sort"}, {"Stage Sort"}),',
        '    Typed = Table.TransformColumnTypes(WithSort, {{"Stage Sort", Int64.Type}, {"Close Value", type number},',
        '        {"Raw Close Value", type number}})',
        "in", "    Typed"], True),
    # Does every agent's login (first.last@) match the model's email column, and does every manager have an RLS row?
    "Agent Login Check": ([
        ("Sales Agent", "string", []), ("Manager", "string", []), ("Region", "string", []),
        ("First Name", "string", []), ("Last Name", "string", []), ("Login Email", "string", []),
        ("Model Email", "string", []), ("Email Matches", "boolean", []), ("Manager In RLS Table", "boolean", [])], [
        "    Source = raw_teams,",
        '    CopiedName = Table.DuplicateColumn(Source, "Sales Agent", "Agent Name Copy"),',
        '    SplitName = Table.SplitColumn(CopiedName, "Agent Name Copy", Splitter.SplitTextByEachDelimiter({" "}, QuoteStyle.None, false),',
        '        {"First Name", "Last Name"}),',
        '    AddedLogin = Table.AddColumn(SplitName, "Login Email", each Text.Lower([First Name] & "." & [Last Name]) & "@maven-crm.example", type text),',
        '    Agents = Parquet.Document(File.Contents(DataFolder & "dim_agent.parquet")),',
        '    MergedAgent = Table.NestedJoin(AddedLogin, {"Sales Agent"}, Agents, {"agent"}, "AgentRow", JoinKind.LeftOuter),',
        '    ExpandedAgent = Table.ExpandTableColumn(MergedAgent, "AgentRow", {"email"}, {"Model Email"}),',
        '    AddedMatch = Table.AddColumn(ExpandedAgent, "Email Matches", each [Login Email] = [Model Email], type logical),',
        '    Access = Parquet.Document(File.Contents(DataFolder & "manager_access.parquet")),',
        '    MergedAccess = Table.NestedJoin(AddedMatch, {"Manager"}, Access, {"manager"}, "AccessRows", JoinKind.LeftOuter),',
        '    AddedCovered = Table.AddColumn(MergedAccess, "Manager In RLS Table", each Table.RowCount([AccessRows]) > 0, type logical),',
        '    Result = Table.SelectColumns(AddedCovered, {"Sales Agent", "Manager", "Region", "First Name", "Last Name",',
        '        "Login Email", "Model Email", "Email Matches", "Manager In RLS Table"})',
        "in", "    Result"], True),
    # The About page's dictionary: the seed CSV's field descriptions, mapped to where each field lives in the model.
    "Data Dictionary": ([
        ("Source Table", "string", []), ("Source Field", "string", []), ("Description", "string", []),
        ("Model Table", "string", [])], [
        f"    Source = {CSV.format(f='data_dictionary.csv')},",
        "    PromotedHeaders = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),",
        '    Renamed = Table.RenameColumns(PromotedHeaders, {{"Table", "Source Table"}, {"Field", "Source Field"}}),',
        "    // The CSV has trailing spaces (e.g. 'Sales agent '): trim every text column",
        '    Trimmed = Table.TransformColumns(Renamed, {{"Source Table", Text.Trim, type text}, {"Source Field", Text.Trim, type text},',
        '        {"Description", Text.Trim, type text}}),',
        '    AddedModelTable = Table.AddColumn(Trimmed, "Model Table", each Record.FieldOrDefault([accounts = "Account",',
        '        products = "Product", sales_teams = "Agent", sales_pipeline = "Opportunities"], [Source Table], "?"), type text)',
        "in", "    AddedModelTable"], True),
    # One row per run and counter, so a stacked column can show what each run ran into.
    "Sync Run Events": ([
        ("Run ID", "string", ["isHidden"]), ("Sync Date", "dateTime", ["formatString: dd mmm yyyy"]),
        ("Metric", "string", []), ("Value", "int64", [f"formatString: {INT}"])], [
        '    Source = #"Sync Runs",',
        "    // Every retry follows a 429 or a 5xx, so first-try + 429 + 5xx stacks up to exactly the requests sent",
        '    AddedFirstTry = Table.AddColumn(Source, "first_try", each [requests] - [retries], Int64.Type),',
        '    KeptCounters = Table.SelectColumns(AddedFirstTry, {"run_id", "as_of", "first_try", "rate_limited", "server_errors"}),',
        '    Unpivoted = Table.UnpivotOtherColumns(KeptCounters, {"run_id", "as_of"}, "Metric", "Value"),',
        '    Labelled = Table.TransformColumns(Unpivoted, {{"Metric", each Record.FieldOrDefault([first_try = "Answered first time",',
        '        rate_limited = "Rate Limited 429", server_errors = "Server Errors 5xx"], _, _), type text}}),',
        '    Renamed = Table.RenameColumns(Labelled, {{"run_id", "Run ID"}, {"as_of", "Sync Date"}}),',
        '    Typed = Table.TransformColumnTypes(Renamed, {{"Sync Date", type date}, {"Value", Int64.Type}})',
        "in", "    Typed"], False),
}

# ---------------------------------------------------------------- Fabric variant (composite model)
# SalesPipeline.Fabric.SemanticModel: same tables and DAX, sources switched to the Lakehouse SQL endpoint.
#   Opportunities  import + hybrid refresh policy (the newest partition is DirectQuery: today's deals in real time)
#   Pipeline Snapshots  DirectQuery; Snapshot Agg (import) answers the totals via alternateOf
#   dimensions  dual, so they join Import, hybrid and DirectQuery facts with regular relationships
FABRIC_MODE = {"Pipeline Snapshots": "directQuery", **{t: "dual" for t in (
    "Date", "Account", "Product", "Agent", "Stage", "Cycle Band", "Age Band")}}
SNAPSHOT_AGG = [  # (column, mart column, dataType, summarization, base column in Pipeline Snapshots)
    ("Snapshot Date", "snapshot_date", "dateTime", "groupBy", "Snapshot Date"),
    ("Owner ID", "owner_id", "string", "groupBy", "Owner ID"),
    ("Stage Key", "stage_key", "string", "groupBy", "Stage Key"),
    ("Age Band", "age_band", "string", "groupBy", "Age Band"),
    ("Agg Open Value", "open_value", "double", "sum", "Snapshot Open Value"),
    ("Agg Close Value", "close_value", "double", "sum", "Snapshot Close Value"),
    ("Agg Rows", "rows", "int64", "count", None),
]
FABRIC_RELATIONSHIPS = [("Snapshot Agg", "Snapshot Date", "Date", "date", True),
                        ("Snapshot Agg", "Owner ID", "Agent", "owner_id", True),
                        ("Snapshot Agg", "Stage Key", "Stage", "stage_key", True),
                        ("Snapshot Agg", "Age Band", "Age Band", "age_band", True)]

OPEN = 'Opportunities[Stage Key] IN { "engaging", "prospecting" }'
SNAP_OPEN = "'Pipeline Snapshots'[Stage Key] IN { \"engaging\", \"prospecting\" }"
# A snapshot is a balance: on any axis, take the latest snapshot inside the current filter.
LAST_SNAP = "VAR _d = MAX ( 'Pipeline Snapshots'[Snapshot Date] )\nRETURN\n    CALCULATE (\n        {expr},\n        'Pipeline Snapshots'[Snapshot Date] = _d{extra}\n    )"


# (folder, name, format, description, DAX)
MONTHS = "ALLSELECTED ( 'Date'[Month Start] )"  # window-function relation: the months on the visual's axis
CROSS = "CROSSFILTER ( Funnel[Opportunity ID], Opportunities[Opportunity ID], BOTH )"
MEASURES = [
    # ------------------------------------------------------------ Deals (respect Date Basis)
    ("Deals", "Deals", INT, "Opportunities in the current filter.", "COUNTROWS ( Opportunities )"),
    ("Deals", "Won Deals", INT, "Deals closed as won.", 'CALCULATE ( [Deals], Opportunities[Stage Key] = "closedwon" )'),
    ("Deals", "Lost Deals", INT, "Deals closed as lost.", 'CALCULATE ( [Deals], Opportunities[Stage Key] = "closedlost" )'),
    ("Deals", "Closed Deals", INT, "Won plus lost deals.", "[Won Deals] + [Lost Deals]"),
    ("Deals", "Win Rate", PCT1, "Won ÷ closed (won + lost). Open deals are not counted.",
     "Kpi.Rate ( [Won Deals], [Closed Deals] )"),
    ("Deals", "Won Revenue", USD0, "Close value of won deals.",
     'CALCULATE ( SUM ( Opportunities[Close Value] ), Opportunities[Stage Key] = "closedwon" )'),
    ("Deals", "Won Revenue Compact", None,
     "Won revenue with a dynamic format string: $1.23M, $45K or $678 depending on size.", "[Won Revenue]",
     'SWITCH ( TRUE (), ABS ( SELECTEDMEASURE () ) >= 1e6, "$#,0.00,,""M""", '
     'ABS ( SELECTEDMEASURE () ) >= 1e3, "$#,0,""K""", "$#,0" )'),
    ("Deals", "Avg Deal Size", USD0, "Won revenue per won deal.", "Kpi.Rate ( [Won Revenue], [Won Deals] )"),
    ("Deals", "Avg Cycle Days", INT, "Average days from engage to close, closed deals only.",
     "AVERAGE ( Opportunities[Cycle Days] )"),
    ("Deals", "Price Realisation", PCT1, "Won revenue ÷ list price of the products sold. Above 100% = sold over list.",
     'Kpi.Rate ( [Won Revenue], CALCULATE ( SUM ( Opportunities[List Price] ), Opportunities[Stage Key] = "closedwon" ) )'),
    ("Deals", "Revenue Share", PCT1, "Share of won revenue across everything in the visual.",
     "DIVIDE ( [Won Revenue], CALCULATE ( [Won Revenue], ALLSELECTED () ) )"),
    ("Deals", "Selected Metric", None,
     "The measure picked in the Metric field parameter; its format string follows the pick.",
     "SWITCH ( SELECTEDVALUE ( Metric[Metric Order], 0 ),\n    0, [Won Revenue],\n    1, [Win Rate],\n"
     "    2, [Avg Deal Size],\n    3, [Avg Cycle Days],\n    4, [Deals]\n)",
     'SWITCH ( SELECTEDVALUE ( Metric[Metric Order], 0 ), 1, "0.0%", 3, "0", 4, "#,0", "\\$#,0" )'),
    # ------------------------------------------------------------ Iterators, RELATED, KEEPFILTERS, FILTER in CALCULATE
    ("Deals", "Discount Leakage", USD0, "List price minus close value, summed over won deals (SUMX, row by row).",
     'SUMX ( FILTER ( Opportunities, Opportunities[Stage Key] = "closedwon" ), '
     "Opportunities[List Price] - Opportunities[Close Value] )"),
    ("Deals", "Catalog Value Won", USD0, "Catalogue list price of every won deal, read from Product with RELATED.",
     'SUMX ( FILTER ( Opportunities, Opportunities[Stage Key] = "closedwon" ), RELATED ( Product[List Price] ) )'),
    ("Deals", "Above-Average Deal Revenue", USD0,
     "Won revenue from deals larger than the average won deal in the current filter (FILTER inside CALCULATE).",
     # The average goes into a variable first: written inline, [Avg Deal Size] would be re-evaluated per deal
     # (context transition) and every deal would be compared with itself.
     "VAR _avg = [Avg Deal Size]\nRETURN\n    CALCULATE ( [Won Revenue], FILTER ( Opportunities, Opportunities[Close Value] > _avg ) )"),
    ("Deals", "Won Revenue Large Accounts", USD0,
     "Won revenue from Large (5k+) accounts. KEEPFILTERS intersects with a Size Band slicer instead of overriding it.",
     'CALCULATE ( [Won Revenue], KEEPFILTERS ( Account[Size Band] = "Large (5k+)" ) )'),
    # ------------------------------------------------------------ Accounts (table functions, expanded tables)
    ("Accounts", "Active Accounts", INT, "Accounts with at least one won deal (DISTINCTCOUNT), excluding the no-account row.",
     'CALCULATE ( DISTINCTCOUNT ( Opportunities[Account ID] ), Opportunities[Stage Key] = "closedwon", '
     "Opportunities[Has Account] = TRUE () )"),
    ("Accounts", "Accounts Touched", INT,
     "Accounts with any deal in the filter: Opportunities as a CALCULATE filter carries its expanded table to Account.",
     "CALCULATE ( COUNTROWS ( Account ), Opportunities )"),
    ("Accounts", "Avg Revenue per Account", USD0, "Won revenue averaged over the accounts in the filter (AVERAGEX).",
     "AVERAGEX ( VALUES ( Account[Account] ), [Won Revenue] )"),
    ("Accounts", "Top 10 Accounts Revenue", USD0, "Won revenue of the ten largest accounts among those selected (TOPN).",
     "CALCULATE ( [Won Revenue], TOPN ( 10, ALLSELECTED ( Account[Account] ), [Won Revenue], DESC ) )"),
    ("Accounts", "Account Cumulative Share", PCT1,
     "Pareto: won revenue of this account and every larger one, as a share of all selected accounts (WINDOW).",
     "VAR _cum = CALCULATE ( [Won Revenue], WINDOW ( 1, ABS, 0, REL, ALLSELECTED ( Account[Account] ), "
     "ORDERBY ( [Won Revenue], DESC, Account[Account], ASC ) ) )\n"
     "RETURN\n    IF ( NOT ISBLANK ( [Won Revenue] ), DIVIDE ( _cum, CALCULATE ( [Won Revenue], ALLSELECTED ( Account[Account] ) ) ) )"),
    ("Accounts", "Account ABC", None,
     "A = accounts making the first 80% of won revenue in the current selection, B = the next 15%, C = the rest.",
     "VAR _rev = [Won Revenue]\n"
     "VAR _accounts = ADDCOLUMNS ( ALLSELECTED ( Account[Account] ), \"@rev\", [Won Revenue] )\n"
     "VAR _total = SUMX ( _accounts, [@rev] )\n"
     "VAR _cum = SUMX ( FILTER ( _accounts, [@rev] >= _rev ), [@rev] )\n"
     "VAR _share = DIVIDE ( _cum, _total )\n"
     "RETURN\n    IF ( HASONEVALUE ( Account[Account] ) && _rev > 0,\n"
     "        SWITCH ( TRUE (), _share <= 0.8, \"A\", _share <= 0.95, \"B\", \"C\" ) )"),
    ("Accounts", "Accounts to 80%", INT, "Pareto: how many accounts make the first 80% of won revenue in the selection.",
     "COUNTROWS ( FILTER ( ALLSELECTED ( Account[Account] ), VAR _s = [Account Cumulative Share] RETURN NOT ISBLANK ( _s ) && _s <= 0.8 ) )"),
    ("Accounts", "Share of Accounts to 80%", PCT0, "Accounts to 80% as a share of the accounts with won revenue in the selection.",
     "Kpi.Rate ( [Accounts to 80%], COUNTROWS ( FILTER ( ALLSELECTED ( Account[Account] ), [Won Revenue] > 0 ) ) )"),
    # Dynamic segmentation: the band is decided per account at query time, so it moves with the slicers.
    ("Accounts", "Accounts in Segment", INT, "Accounts whose won revenue in the current filter falls inside the Account Segment band.",
     "VAR _lo = MIN ( 'Account Segment'[Low] )\nVAR _hi = MAX ( 'Account Segment'[High] )\nRETURN\n"
     "    COUNTROWS ( FILTER ( VALUES ( Account[Account] ), VAR _r = [Won Revenue] RETURN _r > 0 && _r >= _lo && _r < _hi ) )"),
    ("Accounts", "Segment Revenue", USD0, "Won revenue of the accounts inside the Account Segment band.",
     "VAR _lo = MIN ( 'Account Segment'[Low] )\nVAR _hi = MAX ( 'Account Segment'[High] )\nRETURN\n"
     "    SUMX ( FILTER ( VALUES ( Account[Account] ), VAR _r = [Won Revenue] RETURN _r > 0 && _r >= _lo && _r < _hi ), [Won Revenue] )"),
    # ------------------------------------------------------------ Variance bridge (quarter on quarter, per product)
    # Q = won deals, P = avg deal size, mix = product share of won deals. Per product:
    #   volume = (Qc - Qp) * mix_p * Pp, mix = Qc * (mix_c - mix_p) * Pp, price = Qc_i * (Pc - Pp); they sum to Rc - Rp.
    ("Variance", "Won Revenue PQ", USD0, "Won revenue one quarter back on the Gregorian calendar.",
     "Kpi.PriorQuarter ( [Won Revenue], 'Gregorian' )"),
    ("Variance", "Revenue Change QoQ", USD0, "Won revenue minus won revenue one quarter back.",
     "[Won Revenue] - [Won Revenue PQ]"),
    ("Variance", "Revenue Change MoM", USD0, "Won revenue minus won revenue one month back (calendar-based, no axis needed).",
     "[Won Revenue] - [Revenue Last Month]"),
    ("Variance", "Volume Effect", USD0, "Part of the QoQ change from winning more or fewer deals at last quarter's mix and prices.",
     "VAR _qc = CALCULATE ( [Won Deals], ALLSELECTED ( Product ) )\n"
     "VAR _qp = Kpi.PriorQuarter ( CALCULATE ( [Won Deals], ALLSELECTED ( Product ) ), 'Gregorian' )\nRETURN\n"
     "    SUMX ( VALUES ( Product[Product] ),\n"
     "        VAR _np = Kpi.PriorQuarter ( [Won Deals], 'Gregorian' )\n"
     "        VAR _pp = Kpi.PriorQuarter ( [Avg Deal Size], 'Gregorian' )\n"
     "        RETURN ( _qc - _qp ) * DIVIDE ( _np, _qp ) * _pp )"),
    ("Variance", "Mix Effect", USD0, "Part of the QoQ change from selling a different product mix, at last quarter's prices.",
     "VAR _qc = CALCULATE ( [Won Deals], ALLSELECTED ( Product ) )\n"
     "VAR _qp = Kpi.PriorQuarter ( CALCULATE ( [Won Deals], ALLSELECTED ( Product ) ), 'Gregorian' )\nRETURN\n"
     "    SUMX ( VALUES ( Product[Product] ),\n"
     "        VAR _nc = [Won Deals]\n"
     "        VAR _np = Kpi.PriorQuarter ( [Won Deals], 'Gregorian' )\n"
     "        VAR _pp = Kpi.PriorQuarter ( [Avg Deal Size], 'Gregorian' )\n"
     "        RETURN _qc * ( DIVIDE ( _nc, _qc ) - DIVIDE ( _np, _qp ) ) * _pp )"),
    ("Variance", "Price Effect", USD0, "Part of the QoQ change from a different average deal size per product.",
     "SUMX ( VALUES ( Product[Product] ),\n"
     "    VAR _nc = [Won Deals]\n"
     "    VAR _pc = [Avg Deal Size]\n"
     "    VAR _pp = Kpi.PriorQuarter ( [Avg Deal Size], 'Gregorian' )\n"
     "    RETURN _nc * ( _pc - _pp ) )"),
    ("Variance", "Bridge Check", USD0, "Revenue Change QoQ minus volume, mix and price effects; zero when the bridge closes.",
     "[Revenue Change QoQ] - [Volume Effect] - [Mix Effect] - [Price Effect]"),
    # ------------------------------------------------------------ Percentiles, outliers, forecast
    ("Outliers", "P90 Cycle Days", INT, "90th percentile of engage-to-close days over the closed deals in the filter (PERCENTILEX.INC).",
     "PERCENTILEX.INC ( FILTER ( Opportunities, NOT ISBLANK ( Opportunities[Cycle Days] ) ), Opportunities[Cycle Days], 0.9 )"),
    ("Outliers", "Agent Stale Z-Score", "+0.00;-0.00;0.00",
     "How many standard deviations an agent's stale share sits from the average agent in the selection; above 2 = outlier.",
     # Agents with open pipeline but no stale deal count as 0% (Stale Share itself is blank there).
     "VAR _s = IF ( [Open Pipeline] > 0, COALESCE ( [Stale Share], 0 ) )\n"
     "VAR _agents = ADDCOLUMNS ( FILTER ( ALLSELECTED ( Agent[Sales Agent] ), [Open Pipeline] > 0 ), \"@s\", COALESCE ( [Stale Share], 0 ) )\n"
     "VAR _mu = AVERAGEX ( _agents, [@s] )\nVAR _sd = STDEVX.P ( _agents, [@s] )\nRETURN\n"
     "    IF ( HASONEVALUE ( Agent[Sales Agent] ) && NOT ISBLANK ( _s ), DIVIDE ( _s - _mu, _sd ) )"),
    # Least squares over the months that have won revenue (Mar–Dec 2017: ten points), extended to Mar 2018.
    ("Outliers", "Revenue Trend Forecast", USD0,
     "Linear trend of monthly won revenue (LINESTX); months after the last sync are a projection, not a forecast with seasonality.",
     "VAR _pts = CALCULATETABLE (\n"
     "    FILTER ( ADDCOLUMNS ( VALUES ( 'Date'[Month Start] ), \"@x\", YEAR ( 'Date'[Month Start] ) * 12 + MONTH ( 'Date'[Month Start] ), "
     "\"@y\", [Won Revenue] ), NOT ISBLANK ( [@y] ) ),\n    REMOVEFILTERS ( 'Date' ) )\n"
     "VAR _fit = LINESTX ( _pts, [@y], [@x] )\n"
     "VAR _m = MAX ( 'Date'[Month Start] )\nRETURN\n"
     "    IF ( HASONEVALUE ( 'Date'[Month Start] ) && COUNTROWS ( _pts ) >= 3 && _m >= MINX ( _pts, 'Date'[Month Start] ),\n"
     "        MAXX ( _fit, [Intercept] ) + MAXX ( _fit, [Slope1] ) * ( YEAR ( _m ) * 12 + MONTH ( _m ) ) )"),
    # ------------------------------------------------------------ Team (ALLEXCEPT)
    ("Team", "Region Win Rate", PCT1, "Win rate of the whole region of the agent or manager in the row (ALLEXCEPT).",
     "CALCULATE ( [Win Rate], ALLEXCEPT ( Agent, Agent[Region] ) )"),
    ("Team", "Win Rate vs Region", "+0.0%;-0.0%;0.0%", "Win rate minus the win rate of the region it belongs to.",
     "[Win Rate] - [Region Win Rate]"),
    # Conditional formatting by field value: the table colours Win Rate vs Region with this hex code.
    ("Team", "Win Rate vs Region Color", None, "Font colour for Win Rate vs Region: green above the region, red below.",
     'VAR _d = [Win Rate vs Region]\nRETURN\n    SWITCH ( TRUE (), ISBLANK ( _d ), BLANK (), _d >= 0, "#1E7B34", "#B42318" )'),
    # ------------------------------------------------------------ Flow (Sankey on Drivers)
    ("Flow", "Flow Deals", INT, "Deals on the Stage Flow link in the row: Created → Engaged / still prospecting, Engaged → Won / Lost / still engaging.",
     'SWITCH ( SELECTEDVALUE ( \'Stage Flow\'[To] ),\n'
     '    "Engaged", CALCULATE ( [Deals], NOT ISBLANK ( Opportunities[Engaged Date] ) ),\n'
     '    "Still prospecting", CALCULATE ( [Deals], Opportunities[Stage Key] = "prospecting" ),\n'
     '    "Won", [Won Deals],\n    "Lost", [Lost Deals],\n'
     '    "Still engaging", CALCULATE ( [Deals], Opportunities[Stage Key] = "engaging" )\n)'),
    ("Flow", "Transition Deals", INT, "Deals that went from the From Stage at the previous month-end sync to the To Stage at this one.",
     "COUNTROWS ( 'Stage Transitions' )"),
    # ------------------------------------------------------------ Open pipeline (as of the last sync)
    ("Pipeline", "Open Deals", INT, "Deals still open at the last sync. Ignores date filters.",
     f"CALCULATE ( [Deals], {OPEN}, REMOVEFILTERS ( 'Date' ) )"),
    ("Pipeline", "Open Pipeline", USD0, "List value of open deals (no amount exists until a deal closes).",
     "CALCULATE ( SUM ( Opportunities[Open Value] ), REMOVEFILTERS ( 'Date' ) )"),
    # The default is computed, not typed in: P90 of engage-to-close over every closed deal (104 days on this data,
    # checked by the SQL test "P90 of engage-to-close is 104 days").
    ("Pipeline", "Stale Days", INT, "Selected Stale Threshold; default = P90 Cycle Days over all closed deals (104).",
     "SELECTEDVALUE ( 'Stale Threshold'[Stale Threshold], ROUND ( CALCULATE ( [P90 Cycle Days], REMOVEFILTERS () ), 0 ) )"),
    ("Pipeline", "Stale Deals", INT, "Engaging deals older than the stale threshold.",
     "VAR _t = [Stale Days]\nRETURN\n    CALCULATE ( [Deals], Opportunities[Stage Key] = \"engaging\", "
     "Opportunities[Age Days] > _t, REMOVEFILTERS ( 'Date' ) )"),
    ("Pipeline", "Stale Pipeline", USD0, "List value of engaging deals older than the stale threshold.",
     "VAR _t = [Stale Days]\nRETURN\n    CALCULATE ( SUM ( Opportunities[Open Value] ), Opportunities[Stage Key] = \"engaging\", "
     "Opportunities[Age Days] > _t, REMOVEFILTERS ( 'Date' ) )"),
    ("Pipeline", "Stale Share", PCT0, "Share of open pipeline value that is stale.",
     "Kpi.Rate ( [Stale Pipeline], [Open Pipeline] )"),
    ("Pipeline", "Live Pipeline", USD0, "Open pipeline minus stale deals.", "[Open Pipeline] - [Stale Pipeline]"),
    # REMOVEFILTERS Stage too: Pipeline Health carries a page filter Stage[Is Open] = TRUE, which would
    # otherwise blank the closed deals the historical win rate and price realisation are computed from.
    ("Pipeline", "Forecast Win Rate", PCT0, "Selected Win Probability; defaults to the historical win rate.",
     "SELECTEDVALUE ( 'Win Probability'[Win Probability], CALCULATE ( [Win Rate], REMOVEFILTERS ( 'Date', Stage ) ) )"),
    ("Pipeline", "Weighted Forecast", USD0,
     "Live pipeline × forecast win rate × historical price realisation. Stale deals are assumed lost.",
     "[Live Pipeline] * [Forecast Win Rate] * CALCULATE ( [Price Realisation], REMOVEFILTERS ( 'Date', Stage ) )"),
    ("Pipeline", "Open Without Account", INT, "Open deals not linked to any account: a CRM hygiene gap.",
     f"CALCULATE ( [Deals], {OPEN}, Opportunities[Has Account] = FALSE (), REMOVEFILTERS ( 'Date' ) )"),
    # ------------------------------------------------------------ Snapshots (pipeline as each sync saw it)
    ("Snapshots", "Snapshot Open Pipeline", USD0, "Open list value at the latest snapshot in the filter.",
     LAST_SNAP.format(expr="SUM ( 'Pipeline Snapshots'[Snapshot Open Value] )", extra="")),
    ("Snapshots", "Snapshot Open Deals", INT, "Open deals at the latest snapshot in the filter.",
     LAST_SNAP.format(expr="COUNTROWS ( 'Pipeline Snapshots' )", extra=f",\n        {SNAP_OPEN}")),
    ("Snapshots", "Snapshot Stale Pipeline", USD0, "Engaging value older than the stale threshold at that snapshot.",
     "VAR _t = [Stale Days]\n" + LAST_SNAP.format(
         expr="SUM ( 'Pipeline Snapshots'[Snapshot Open Value] )",
         extra=",\n        'Pipeline Snapshots'[Stage Key] = \"engaging\",\n        'Pipeline Snapshots'[Snapshot Age Days] > _t")),
    ("Snapshots", "Snapshot Stale Share", PCT0, "Stale share of open value at the latest snapshot in the filter.",
     "Kpi.Rate ( [Snapshot Stale Pipeline], [Snapshot Open Pipeline] )"),
    # ------------------------------------------------------------ Funnel
    ("Funnel", "Funnel Deals", INT, "Deals that reached the step.", "COUNTROWS ( Funnel )"),
    ("Funnel", "Share of Created", PCT0, "Funnel deals ÷ deals created.",
     "Kpi.Rate ( [Funnel Deals], CALCULATE ( [Funnel Deals], Funnel[Step Sort] = 1 ) )"),
    ("Funnel", "Step Conversion", PCT1, "Deals at this step ÷ deals at the step before it (window function OFFSET).",
     "VAR _prev = CALCULATE ( [Funnel Deals], OFFSET ( -1, ALLSELECTED ( Steps[Step], Steps[Step Order] ), "
     "ORDERBY ( Steps[Step Order], ASC ) ) )\nRETURN\n    Kpi.Rate ( [Funnel Deals], _prev )"),
    ("Funnel", "Value Reaching Step", USD0,
     "List value of the deals that reached the step: Steps filters Opportunities through the Funnel bridge (many-to-many).",
     f"CALCULATE ( SUM ( Opportunities[List Price] ), {CROSS} )"),
    ("Funnel", "Avg Value Reaching Step", USD0, "Average list price of the deals that reached the step (many-to-many).",
     f"CALCULATE ( AVERAGE ( Opportunities[List Price] ), {CROSS} )"),
    # ------------------------------------------------------------ Trends (window functions, calendar TI, ISINSCOPE)
    ("Trends", "Revenue Last Month", USD0, "Won revenue one month back on the Gregorian calendar (calendar-based time intelligence).",
     "Kpi.PriorMonth ( [Won Revenue], 'Gregorian' )"),
    ("Trends", "Prev Month Revenue", USD0, "Won revenue of the previous month on the visual's axis (window function OFFSET).",
     f"CALCULATE ( [Won Revenue], OFFSET ( -1, {MONTHS}, ORDERBY ( 'Date'[Month Start], ASC ) ) )"),
    ("Trends", "MoM Change", "+0.0%;-0.0%;0.0%", "Won revenue change against the previous month on the axis.",
     "VAR _prev = [Prev Month Revenue]\nRETURN\n    Kpi.Rate ( [Won Revenue] - _prev, _prev )"),
    ("Trends", "Rolling 3M Revenue", USD0, "Won revenue of this month and the two before it (window function WINDOW).",
     f"CALCULATE ( [Won Revenue], WINDOW ( -2, REL, 0, REL, {MONTHS}, ORDERBY ( 'Date'[Month Start], ASC ) ) )"),
    ("Trends", "Best Month Revenue", USD0, "Won revenue of the best month among those selected (window function INDEX).",
     f"CALCULATE ( [Won Revenue], INDEX ( 1, {MONTHS}, ORDERBY ( [Won Revenue], DESC ) ) )"),
    ("Trends", "Best Month", None, "Name of the best month by won revenue (window function INDEX).",
     f"FORMAT ( MAXX ( INDEX ( 1, {MONTHS}, ORDERBY ( [Won Revenue], DESC ) ), 'Date'[Month Start] ), \"mmm yyyy\" )"),
    ("Trends", "Share of Parent", PCT1,
     "Won revenue as a share of the level above in the Sales Team hierarchy: agent of manager, manager of region, region of total.",
     "SWITCH (\n    TRUE (),\n    ISINSCOPE ( Agent[Sales Agent] ), Kpi.ShareOf ( [Won Revenue], Agent[Sales Agent] ),\n"
     "    ISINSCOPE ( Agent[Manager] ), Kpi.ShareOf ( [Won Revenue], Agent[Manager] ),\n"
     "    ISINSCOPE ( Agent[Region] ), Kpi.ShareOf ( [Won Revenue], Agent[Region] ),\n    1\n)"),
    # ------------------------------------------------------------ Drivers (DAX twin of the key influencers visual)
    ("Drivers", "Win Rate Lift", "0.00",
     "Win rate in this segment ÷ win rate of every other closed deal in the visual; 1.20 = 20% more likely to win.",
     "Kpi.Lift ( [Won Deals], [Closed Deals], CALCULATE ( [Won Deals], ALLSELECTED () ), "
     "CALCULATE ( [Closed Deals], ALLSELECTED () ) )"),
    # ------------------------------------------------------------ API sync log
    ("Sync", "Sync Count", INT, "Extraction runs in the filter.", "COUNTROWS ( 'Sync Runs' )"),
    ("Sync", "API Requests", INT, "HTTP requests sent to the CRM API.", "SUM ( 'Sync Runs'[Requests] )"),
    ("Sync", "API Retries", INT, "Requests that were retried after a 429 or 5xx.", "SUM ( 'Sync Runs'[Retries] )"),
    ("Sync", "Throttled Requests", INT, "429 responses (rate limit), retried after Retry-After.",
     "SUM ( 'Sync Runs'[Rate Limited 429] )"),
    ("Sync", "Server Error Responses", INT, "5xx responses, retried with backoff.",
     "SUM ( 'Sync Runs'[Server Errors 5xx] )"),
    ("Sync", "Deal Versions Loaded", INT, "Deal versions appended to the raw history.", "SUM ( 'Sync Runs'[Versions Added] )"),
    ("Sync", "Last Sync", "dd mmm yyyy", "Date of the latest extraction run.", "MAX ( 'Sync Runs'[Sync Date] )"),
    ("Sync", "Run Responses", INT, "API responses per run and outcome (Sync Run Events, unpivoted in Power Query).",
     "SUM ( 'Sync Run Events'[Value] )"),
    # ------------------------------------------------------------ Data trust (Power Query reconciliation)
    ("Data Trust", "Reconciliation Gaps", INT, "Stages where the mart's deal count or close value differs from the seed CSV.",
     "COUNTROWS ( FILTER ( 'Source Reconciliation', 'Source Reconciliation'[Deals Diff] <> 0 "
     "|| 'Source Reconciliation'[Value Diff] <> 0 ) )"),
    ("Data Trust", "Revenue Covered by RLS", USD0,
     "Won revenue of the managers listed in the RLS access table, applied to Agent with TREATAS (no relationship).",
     "CALCULATE ( [Won Revenue], KEEPFILTERS ( TREATAS ( VALUES ( 'Manager Access'[Manager] ), Agent[Manager] ) ) )"),
    ("Data Trust", "RLS Coverage", PCT1, "Share of won revenue whose manager has a row in the RLS access table.",
     "Kpi.Rate ( [Revenue Covered by RLS], [Won Revenue] )"),
    ("Data Trust", "Login Mismatches", INT,
     "Agents whose first.last login differs from the model email, or whose manager has no RLS row.",
     "COUNTROWS ( FILTER ( 'Agent Login Check', NOT 'Agent Login Check'[Email Matches] "
     "|| NOT 'Agent Login Check'[Manager In RLS Table] ) )"),
    # ------------------------------------------------------------ Narrative
    ("Narrative", "Headline", None, "One-sentence summary that follows the slicers.",
     "VAR _w = [Won Revenue]\nVAR _r = [Win Rate]\nVAR _s = [Stale Share]\nRETURN\n"
     "    \"Won \" & Fmt.Money ( _w ) & \" at a \" & FORMAT ( _r, \"0%\" ) & \" win rate. \"\n"
     "        & FORMAT ( [Stale Deals], \"#,0\" ) & \" of \" & FORMAT ( [Open Deals], \"#,0\" )\n"
     "        & \" open deals are older than \" & [Stale Days] & \" days, holding \" & FORMAT ( _s, \"0%\" ) & \" of the open pipeline.\""),
]
TEXT_MEASURES = {"Headline", "Best Month", "Account ABC", "Win Rate vs Region Color"}
# Deliberate BPA exceptions, written as the annotation Tabular Editor's "Ignore item" writes. measure -> (rules, why)
BPA_IGNORE = {
    **{m: (["PROVIDE_FORMAT_STRING_FOR_MEASURES", "INTEGER_FORMATTING"], "text measure: a format string means nothing")
       for m in TEXT_MEASURES},
    "Won Revenue Compact": (["INTEGER_FORMATTING", "MEASURES_SHOULD_NOT_BE_DIRECT_REFERENCES_OF_OTHER_MEASURES"],
                            "exists only to carry a dynamic format string over [Won Revenue]"),
    "Selected Metric": (["INTEGER_FORMATTING"], "dynamic format string follows the field-parameter pick"),
    "Agent Stale Z-Score": (["INTEGER_FORMATTING"], "z-score needs sign and 2 decimals"),
    "Win Rate Lift": (["INTEGER_FORMATTING"], "ratio (1.25x), 2 decimals on purpose"),
    "Last Sync": (["INTEGER_FORMATTING"], "returns a date, formatted as a date"),
}


# Asserted DAX tests (pbi-kit dax-tests). Every expected value is one the warehouse already proves in
# sql/90_tests.sql, so the model and the SQL marts must agree. Offline: syntax + model refs + assert file;
# online (CI, Test workspace): executeQueries. The model has RLS roles -> online run needs a user token.
DAX_TESTS = {
    "test-key-totals": ("""// sql/90_tests.sql: 8800 deals; won revenue $10,005,534; 4238 won, 2473 lost, 1589 + 500 open.
// Win Rate = 4238 / (4238 + 2473) = 0.63150 (open deals are not counted).
EVALUATE
ROW (
    "Deals", [Deals], "Won Deals", [Won Deals], "Lost Deals", [Lost Deals],
    "Open Deals", [Open Deals], "Won Revenue", [Won Revenue], "Win Rate", [Win Rate]
)
""", {"rows": 1, "checks": [
        {"column": "[Deals]", "equals": 8800}, {"column": "[Won Deals]", "equals": 4238},
        {"column": "[Lost Deals]", "equals": 2473}, {"column": "[Open Deals]", "equals": 2089},
        {"column": "[Won Revenue]", "equals": 10005534, "tolerance": 0.5},
        {"column": "[Win Rate]", "equals": 0.6315, "tolerance": 0.0001}]}),
    "test-stage-counts": ("""// sql/90_tests.sql: stage counts match the source.
EVALUATE
SUMMARIZECOLUMNS ( Stage[Stage Key], "Deals", [Deals] )
ORDER BY Stage[Stage Key]
""", {"rows": 4, "checks": [
        {"column": "[Deals]", "row": 0, "equals": 2473}, {"column": "[Deals]", "row": 1, "equals": 4238},
        {"column": "[Deals]", "row": 2, "equals": 1589}, {"column": "[Deals]", "row": 3, "equals": 500}]}),
    "test-stale-default": ("""// sql/90_tests.sql: P90 of engage-to-close is 104 days, the default of Stale Days.
EVALUATE
ROW ( "P90 Cycle Days", [P90 Cycle Days], "Stale Days", [Stale Days] )
""", {"rows": 1, "checks": [{"column": "[P90 Cycle Days]", "equals": 104, "tolerance": 0.5},
                            {"column": "[Stale Days]", "equals": 104, "tolerance": 0.5}]}),
}

# DAX query view files (<model>/DAXQueries). Expected results come from the DuckDB warehouse; none of these
# has been run in Power BI yet (no Desktop on the build machine).
DAX_QUERIES = {
    "accounts-touched.dax": """// Expanded table: Opportunities used as a CALCULATE filter carries its whole expanded table
// (Opportunities + Account + Agent + ...) so Account is filtered to the accounts that have a deal.
// Test: [Accounts Touched] must equal a plain DISTINCTCOUNT of the account key on every row.
// Expected (DuckDB): Cara Losch 65, Celia Rouche 69, Dustin Brinkmann 75, Melvin Marxen 76,
// Rocco Neubert 61, Summer Sewald 73; Equal = TRUE on all six rows.
EVALUATE
ADDCOLUMNS (
    VALUES ( Agent[Manager] ),
    "Accounts Touched", [Accounts Touched],
    "Distinct Account IDs", CALCULATE ( DISTINCTCOUNT ( Opportunities[Account ID] ) ),
    "Equal", [Accounts Touched] = CALCULATE ( DISTINCTCOUNT ( Opportunities[Account ID] ) )
)
ORDER BY Agent[Manager]
""",
    "keepfilters.dax": """// Replace vs add: a plain filter argument overwrites the Size Band coming from the row;
// KEEPFILTERS intersects with it.
// Expected (DuckDB):
//   Size Band     Won Revenue   Large, KEEPFILTERS   Large, plain filter
//   Large (5k+)     4,205,222            4,205,222             4,205,222
//   Mid (1k–5k)     4,206,231              (blank)             4,205,222
//   Small (<1k)     1,594,081              (blank)             4,205,222
EVALUATE
SUMMARIZECOLUMNS (
    Account[Size Band],
    "Won Revenue", [Won Revenue],
    "Large, KEEPFILTERS", [Won Revenue Large Accounts],
    "Large, plain filter", CALCULATE ( [Won Revenue], Account[Size Band] = "Large (5k+)" )
)
ORDER BY Account[Size Band]
""",
    "ols-test.dax": """// Object-level security test. In DAX query view pick "View as" > role External Partner, then run.
// Expected: query 1 fails with an error that Account[Annual Revenue] cannot be found;
// query 2 returns won revenue per sector (the role still sees every deal).
// Not run yet: Power BI Desktop is not available on the build machine.
EVALUATE
TOPN ( 5, SELECTCOLUMNS ( Account, "Account", Account[Account], "Annual Revenue", Account[Annual Revenue] ) )

EVALUATE
SUMMARIZECOLUMNS ( Account[Sector], "Won Revenue", [Won Revenue] )
""",
    "variance-bridge.dax": """// Q4 2017 vs Q3 2017 won revenue, split into volume, mix and price per product. Check must be 0 on every row.
// Expected (DuckDB, src/build_preview.py bridge()): total change -179,759 = volume -144,724 + mix -53,584 + price +18,549.
//   GTX Pro  PQ 1,096,599  Q4 958,491  volume -53,216  mix -72,941  price -11,951
//   GTX Plus Pro  PQ 775,606  Q4 795,530  volume -37,639  mix +37,639  price +19,924
// Not run yet: Power BI Desktop is not available on the build machine.
EVALUATE
CALCULATETABLE (
    ADDCOLUMNS (
        VALUES ( Product[Product] ),
        "Won PQ", [Won Revenue PQ], "Won", [Won Revenue], "Volume", [Volume Effect],
        "Mix", [Mix Effect], "Price", [Price Effect], "Check", [Bridge Check]
    ),
    'Date'[Quarter] = "Q4 2017"
)
ORDER BY [Volume]
""",
    "trend-forecast.dax": """// Revenue Trend Forecast (LINESTX over the ten months Mar-Dec 2017 that have won revenue).
// Expected (DuckDB least squares): slope ~ +$400 per month, Dec 2017 ~ 1,002,355, Mar 2018 ~ 1,003,555.
// The trend is flat: ten points of one year are no basis for seasonality, so Jan-Mar 2018 is a projection only.
// P90 Cycle Days should return 104, the default of Stale Days.
EVALUATE
SUMMARIZECOLUMNS ( 'Date'[Month Start], "Won", [Won Revenue], "Trend", [Revenue Trend Forecast] )
ORDER BY 'Date'[Month Start]

EVALUATE
ROW ( "P90 Cycle Days", [P90 Cycle Days], "Stale Days", [Stale Days] )
""",
}


def pretty(col):
    return " ".join(w.upper() if w in ("id",) else w.capitalize() for w in col.split("_"))


def q(name):
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else f"'{name}'"


_LINEAGE = iter(range(10**9))


def lineage():
    # Deterministic (n-th tag of the build), so a rebuild without changes leaves git diff empty: only real edits show.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"crm-pipeline/lineage/{next(_LINEAGE)}"))


def col_name(cfg, col):
    return cfg.get("names", {}).get(col) or pretty(col)


def schema(con, mart):
    return con.execute(f"describe mart.{mart}").fetchall()


def dax_block(dax, indent=3):
    return [f"{T*indent}{l}" for l in dax.split("\n")]


def source_steps(mart, fabric):
    if fabric:  # Lakehouse SQL endpoint: every later step folds into the SELECT sent to the endpoint
        return ["    Source = Sql.Database(SqlEndpoint, LakehouseDb),",
                f'    Data = Source{{[Schema = "dbo", Item = "{mart}"]}}[Data],']
    return [f'    Source = Parquet.Document(File.Contents(DataFolder & "{mart}.parquet")),',
            "    Data = Source,"]


def m_source(mart, m_types, refresh=None, fabric=False, drop=()):
    m = ["let", *source_steps(mart, fabric)]
    if drop:
        m.append(f"    Kept = Table.RemoveColumns(Data, {{{', '.join(f'{chr(34)}{c}{chr(34)}' for c in drop)}}}),")
    m.append(f"    Typed = Table.TransformColumnTypes({'Kept' if drop else 'Data'}, {{{', '.join(m_types)}}})")
    if not refresh:
        return m + ["in", "    Typed"]
    c = refresh["column"]
    m[-1] += ","
    return m + [f"    InRange = Table.SelectRows(Typed, each DateTime.From([{c}]) >= RangeStart "
                f"and DateTime.From([{c}]) < RangeEnd)", "in", "    InRange"]


def polling_m(mart, refresh, fabric=False):
    c = refresh["column"]
    return ["let", *source_steps(mart, fabric),
            f"    InRange = Table.SelectRows(Data, each DateTime.From([{c}]) >= RangeStart "
            f"and DateTime.From([{c}]) < RangeEnd),",
            f"    LastChange = List.Max(InRange[{refresh['polling']}])",
            "in", "    LastChange"]


def table_tmdl(con, name, mart, cfg, fabric=False):
    mode = FABRIC_MODE.get(name, "import") if fabric else "import"
    lines = [f"table {q(name)}", f"{T}lineageTag: {lineage()}"]
    if cfg.get("date_table"):
        lines.append(f"{T}dataCategory: Time")
    if cfg.get("hidden_table"):
        lines.append(f"{T}isHidden")
    lines.append("")
    m_types = []
    for col, dtype, *_ in schema(con, mart):
        if col in cfg.get("drop", ()):
            continue
        tm, mq = TYPES[dtype]
        m_types.append(f'{{"{col}", {mq}}}')
        lines += [f"{T}column {q(col_name(cfg, col))}", f"{T*2}dataType: {tm}"]
        if col == cfg.get("key"):
            lines.append(f"{T*2}isKey")
        if col in cfg.get("hidden", []) or cfg.get("hidden_table"):
            lines.append(f"{T*2}isHidden")
        fmt = cfg.get("fmt", {}).get(col) or DEFAULT_FMT.get(tm)
        if fmt:
            lines.append(f"{T*2}formatString: {fmt}")
        if cat := cfg.get("category", {}).get(col):
            lines.append(f"{T*2}dataCategory: {cat}")
        lines += [f"{T*2}lineageTag: {lineage()}",
                  f"{T*2}summarizeBy: {'sum' if col in cfg.get('sum', []) else 'none'}",
                  f"{T*2}sourceColumn: {col}"]
        if sort := cfg.get("sort", {}).get(col):
            lines.append(f"{T*2}sortByColumn: {q(col_name(cfg, sort))}")
        lines.append("")
    for cname, dtype, fmt, dax in (cfg.get("calc_cols", []) if mode == "import" else []):
        lines += [f"{T}column {q(cname)} =", *dax_block(dax)] if "\n" in dax else [f"{T}column {q(cname)} = {dax}"]
        lines += [f"{T*2}dataType: {dtype}"] + ([f"{T*2}formatString: {fmt}"] if fmt else [])
        lines += [f"{T*2}lineageTag: {lineage()}", f"{T*2}summarizeBy: none", ""]
    for h, levels in HIERARCHIES.get(name, []):
        lines += [f"{T}hierarchy {q(h)}", f"{T*2}lineageTag: {lineage()}", ""]
        for lv in levels:
            lines += [f"{T*2}level {q(lv)}", f"{T*3}lineageTag: {lineage()}", f"{T*3}column: {q(lv)}", ""]
    if cfg.get("date_table"):
        for cal, groups in CALENDARS:
            lines += [f"{T}calendar {q(cal)}", ""]
            for unit, primary, assoc in groups:
                lines += [f"{T*2}calendarColumnGroup = {unit}", f"{T*3}primaryColumn: {q(primary)}"]
                lines += ([f"{T*3}associatedColumn: {q(assoc)}"] if assoc else []) + [""]
    refresh = cfg.get("refresh") if mode == "import" else None
    m = m_source(mart, m_types, refresh, fabric, cfg.get("drop", ()))
    if refresh:
        (rg, rp), (ig, ip) = refresh["rolling"], refresh["incremental"]
        lines += [f"{T}refreshPolicy", f"{T*2}policyType: basic"]
        if fabric and refresh.get("hybrid"):  # "Get the latest data in real time": newest partition is DirectQuery
            lines.append(f"{T*2}mode: hybrid")
        lines += [
                  f"{T*2}rollingWindowGranularity: {rg}", f"{T*2}rollingWindowPeriods: {rp}",
                  f"{T*2}incrementalGranularity: {ig}", f"{T*2}incrementalPeriods: {ip}"]
        if refresh.get("polling"):
            lines += [f"{T*2}pollingExpression =", *[f"{T*4}{l}" for l in polling_m(mart, refresh, fabric)]]
        lines += [f"{T*2}sourceExpression =", *[f"{T*4}{l}" for l in m], ""]
    lines += [f"{T}partition {q(name)} = m", f"{T*2}mode: {mode}", f"{T*2}source ="]
    lines += [f"{T*4}{l}" for l in m]
    lines += ["", f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def calculated_tmdl(name, dax, cols):
    lines = [f"table {q(name)}", f"{T}lineageTag: {lineage()}", ""]
    for col, src, dtype, extra in cols:
        lines += [f"{T}column {q(col)}", f"{T*2}dataType: {dtype}", f"{T*2}lineageTag: {lineage()}",
                  f"{T*2}summarizeBy: none", f"{T*2}isNameInferred", f"{T*2}sourceColumn: {src}"]
        lines += [f"{T*2}{e}" for e in extra] + [""]
    lines += [f"{T}partition {q(name)} = calculated", f"{T*2}mode: import", f"{T*2}source = {dax}", ""]
    return "\n".join(lines)


def m_table_tmdl(name, cols, steps):
    lines = [f"table {q(name)}", f"{T}lineageTag: {lineage()}", ""]
    for col, dtype, extra in cols:
        fmt = [] if any(e.startswith("formatString") for e in extra) or dtype not in DEFAULT_FMT \
            else [f"formatString: {'#,0' if dtype == 'int64' else DEFAULT_FMT[dtype]}"]
        lines += [f"{T}column {q(col)}", f"{T*2}dataType: {dtype}", f"{T*2}lineageTag: {lineage()}",
                  f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: {col}"]
        lines += [f"{T*2}{e}" for e in extra + fmt] + [""]
    lines += [f"{T}partition {q(name)} = m", f"{T*2}mode: import", f"{T*2}source =",
              *[f"{T*4}{l}" for l in ["let", *steps]], "", f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def snapshot_agg_tmdl():
    """Import aggregation over the DirectQuery snapshots; the engine rewrites matching queries to it (alternateOf)."""
    lines = ["table 'Snapshot Agg'", f"{T}isHidden", f"{T}lineageTag: {lineage()}", ""]
    for col, src, dtype, summ, base in SNAPSHOT_AGG:
        lines += [f"{T}column {q(col)}", f"{T*2}dataType: {dtype}", f"{T*2}isHidden",
                  f"{T*2}lineageTag: {lineage()}", f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: {src}", "",
                  f"{T*2}alternateOf", f"{T*3}summarization: {summ}"]
        lines += [f"{T*3}baseColumn: 'Pipeline Snapshots'.{q(base)}"] if base else [f"{T*3}baseTable: 'Pipeline Snapshots'"]
        lines.append("")
    keys = [src for _, src, _, s, _ in SNAPSHOT_AGG if s == "groupBy"]
    m = ["let", *source_steps("fact_pipeline_snapshot", True),
         f"    Grouped = Table.Group(Data, {{{', '.join(chr(34) + k + chr(34) for k in keys)}}}, {{",
         '        {"open_value", each List.Sum([open_value]), type number},',
         '        {"close_value", each List.Sum([close_value]), type number},',
         '        {"rows", each Table.RowCount(_), Int64.Type}})', "in", "    Grouped"]
    lines += [f"{T}partition 'Snapshot Agg' = m", f"{T*2}mode: import", f"{T*2}source =", *[f"{T*4}{l}" for l in m],
              "", f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def relationship_kind(ft, tt, fabric):
    """Regular when both ends sit in one source group; dual counts as both. Hybrid = import + DirectQuery."""
    def groups(t):
        if not fabric:
            return {"import"}
        mode = FABRIC_MODE.get(t, "import")
        if t == "Opportunities":
            return {"import", "directQuery"}
        return {"import", "directQuery", "dual"} if mode == "dual" else {mode}
    many, one = groups(ft), groups(tt)
    return "regular" if "dual" in one or many == one else "limited"


def field_parameter_tmdl(name="Analyze By", items=ANALYZE_BY):
    rows = ",\n".join(f'    ( "{label}", NAMEOF ( {ref} ), {i} )' for i, (label, ref) in enumerate(items))
    return "\n".join([
        f"table {q(name)}", f"{T}lineageTag: {lineage()}", "",
        f"{T}column {q(name)}", f"{T*2}dataType: string", f"{T*2}lineageTag: {lineage()}",
        f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: [Value1]", f"{T*2}sortByColumn: '{name} Order'", "",
        f"{T*2}relatedColumnDetails", f"{T*3}groupByColumn: '{name} Fields'", "",
        f"{T}column '{name} Fields'", f"{T*2}dataType: string", f"{T*2}isHidden", f"{T*2}lineageTag: {lineage()}",
        f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: [Value2]", f"{T*2}sortByColumn: '{name} Order'", "",
        f"{T*2}extendedProperty ParameterMetadata =", f"{T*4}{{", f'{T*4}  "version": 3,', f'{T*4}  "kind": 2',
        f"{T*4}}}", "",
        f"{T}column '{name} Order'", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
        f"{T*2}lineageTag: {lineage()}", f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: [Value3]", "",
        f"{T}partition {q(name)} = calculated", f"{T*2}mode: import", f"{T*2}source =",
        *dax_block("{\n" + rows + "\n}", 4), ""])


def calc_group_tmdl(table, column, precedence, items):
    lines = [f"table {q(table)}", f"{T}lineageTag: {lineage()}", "", f"{T}calculationGroup", f"{T*2}precedence: {precedence}", ""]
    for i, (name, dax, fmt) in enumerate(items):
        lines += [f"{T*2}calculationItem {q(name)} =", *dax_block(dax, 4)] if "\n" in dax \
            else [f"{T*2}calculationItem {q(name)} = {dax}"]
        lines.append(f"{T*3}ordinal: {i}")
        if fmt:
            lines.append(f"{T*3}formatStringDefinition = {fmt}")
        lines.append("")
    lines += [f"{T}column {q(column)}", f"{T*2}dataType: string", f"{T*2}lineageTag: {lineage()}",
              f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: Name", f"{T*2}sortByColumn: Ordinal", "",
              f"{T}column Ordinal", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
              f"{T*2}lineageTag: {lineage()}", f"{T*2}summarizeBy: sum", f"{T*2}sourceColumn: Ordinal", ""]
    return "\n".join(lines)


def measures_tmdl():
    lines = ["table _Measures", f"{T}lineageTag: {lineage()}", ""]
    for folder, name, fmt, desc, dax, *fmt_dax in MEASURES:
        lines.append(f"{T}/// {desc}")
        if "\n" in dax:
            lines += [f"{T}measure {q(name)} =", *dax_block(dax)]
        else:
            lines.append(f"{T}measure {q(name)} = {dax}")
        if fmt:
            lines.append(f"{T*2}formatString: {fmt}")
        lines += [f"{T*2}displayFolder: {folder}", f"{T*2}lineageTag: {lineage()}"]
        if fmt_dax:
            lines.append(f"{T*2}formatStringDefinition = {fmt_dax[0]}")
        if name in BPA_IGNORE:  # reason lives in BPA_IGNORE; the annotation value must stay plain JSON
            lines += ["", f"{T*2}annotation BestPracticeAnalyzer_IgnoreRules = "
                      + json.dumps({"RuleIDs": BPA_IGNORE[name][0]}, separators=(",", ":"))]
        lines.append("")
    lines += [f"{T}column Value", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
              f"{T*2}lineageTag: {lineage()}", f"{T*2}summarizeBy: none", f"{T*2}isNameInferred",
              f"{T*2}sourceColumn: [Value]", "",
              f"{T}partition _Measures = calculated", f"{T*2}mode: import", f"{T*2}source = {{1}}", ""]
    return "\n".join(lines)


def functions_tmdl():
    lines = []
    for name, desc, body in FUNCTIONS:
        lines.append(f"/// {desc}")
        lines += [f"function {q(name)} =", *dax_block(body, 2)] if "\n" in body else [f"function {q(name)} = {body}"]
        lines += [f"{T}lineageTag: {lineage()}", ""]
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
    measure_names = {m[1] for m in MEASURES}
    for obj, terms in SYNONYMS.items():
        binding = {"ConceptualEntity": "_Measures", "ConceptualProperty": obj} if obj in measure_names \
            else {"ConceptualEntity": obj}
        entities[re.sub(r"\W", "_", obj.lower())] = {
            "Definition": {"Binding": binding}, "State": "Authored",
            "Terms": [{obj.lower(): {"State": "Authored"}}] + [{t: {"State": "Authored"}} for t in terms]}
    doc = {"Version": "1.0.0", "Language": "en-US", "DynamicImprovement": "HighConfidence", "Entities": entities}
    return "\n".join(["cultureInfo en-US", "", f"{T}linguisticMetadata = {json.dumps(doc, ensure_ascii=False)}",
                      f"{T*2}contentType: json", ""])


def disp(table, col):
    """Display name of a column given by its mart name (imported tables) or display name (DAX tables)."""
    return col_name(TABLES[table][1], col) if table in TABLES else col


def check(con):
    """Fail fast on name collisions, DAX references that don't resolve, and best-practice breaks."""
    for t, (mart, cfg) in TABLES.items():
        names = [col_name(cfg, c[0]).lower() for c in schema(con, mart)]
        assert len(names) == len(set(names)), f"duplicate column names in {t}: {names}"
    columns = {t: {col_name(cfg, c[0]) for c in schema(con, mart)} for t, (mart, cfg) in TABLES.items()}
    columns |= {t: {c[0] for c in cols} for t, (_, cols) in CALCULATED.items()}
    columns |= {t: {c[0] for c in cols} for t, (cols, _, _) in M_TABLES.items()}
    columns |= {"Analyze By": {"Analyze By"}, "Metric": {"Metric", "Metric Order"}, "Date Basis": {"Date Basis"}, "Time Intelligence": {"Period"}}
    for t, (_, cfg) in TABLES.items():
        columns[t] |= {c[0] for c in cfg.get("calc_cols", [])}
    measures = [m[1] for m in MEASURES]
    taken = {n.lower() for n in set().union(*columns.values(), columns)}
    clash = {m for m in measures if m.lower() in taken or measures.count(m) > 1}
    assert not clash, f"measure names collide with tables/columns: {clash}"
    tables, fns = set(columns) | {c for c, _ in CALENDARS}, {f[0] for f in FUNCTIONS}
    dax_all = [(m[1], m[4]) for m in MEASURES] \
        + [(f"role {r}", d) for r, v in ROLES.items() for d in v["filters"].values()] \
        + [(f"calc {n}", d) for n, d, _ in DATE_BASIS + TIME_ITEMS] + [(f"function {n}", b) for n, _, b in FUNCTIONS] \
        + [(f"table {n}", d) for n, (d, _) in CALCULATED.items()] + [("Analyze By", r) for _, r in ANALYZE_BY] + [("Metric", r) for _, r in METRIC] \
        + [(f"column {t}[{c[0]}]", c[3]) for t, (_, cfg) in TABLES.items() for c in cfg.get("calc_cols", [])]
    for name, dax in dax_all:
        dax = re.sub(r'"(?:[^"]|"")*"', '""', dax)
        for a, b, col in re.findall(r"(?:'([^']+)'|\b([A-Za-z_][\w ]*?))\[([^\]]+)\]", dax):
            assert col in columns.get(a or b, ()), f"[{name}] references missing column {a or b}[{col}]"
        for ref in re.findall(r"(?<![\w'\]])\[([^\]]+)\]", dax):
            assert ref in measures or ref in ("Value", "Slope1", "Intercept") or ref.startswith("@"), f"[{name}] references missing measure [{ref}]"
        for ref in re.findall(r"'([^'\n]+)'", re.sub(r"'[^'\n]+'\[[^\]]*\]", "", dax)):
            assert ref in tables, f"[{name}] references missing table/calendar '{ref}'"
        for fn in re.findall(r"\b([A-Z][a-z][A-Za-z]*\.[A-Za-z]+)\s*\(", dax):  # UDFs are Mixed.Case; PERCENTILEX.INC is built in
            assert fn in fns, f"[{name}] calls unknown function {fn}"
        assert dax.count("(") == dax.count(")"), f"[{name}] unbalanced parentheses"
    for t, (_, cfg) in TABLES.items():
        if r := cfg.get("refresh"):
            assert r["column"] in {c[0] for c in schema(con, TABLES[t][0])}, f"{t}: no column {r['column']}"
    for _, groups in CALENDARS:
        assert all(p in columns["Date"] and (a is None or a in columns["Date"]) for _, p, a in groups)
    for t, hs in HIERARCHIES.items():
        assert all(lv in columns[t] for _, lvs in hs for lv in lvs), t
    for r in ROLES.values():
        assert all(t in columns for t in [*r["filters"], *r["hide_tables"], *r["hide_columns"]])
        assert all(c in columns[t] for t, cs in r["hide_columns"].items() for c in cs)
    for p, ts in PERSPECTIVES.items():
        assert all(t in columns or t == "_Measures" for t in ts), p
    assert all(o in measures or o in columns for o in SYNONYMS)
    # best-practice rules (a subset of Tabular Editor's BPA that applies to a generated model)
    for folder, name, fmt, desc, _, *fmt_dax in MEASURES:
        assert desc and desc.endswith("."), f"[{name}] needs a one-sentence description"
        assert fmt or fmt_dax or name in TEXT_MEASURES, f"[{name}] numeric measure without a format string"
    for t, (mart, cfg) in TABLES.items():  # sort-by helpers are plumbing: never visible in the field list
        for sort_col in cfg.get("sort", {}).values():
            assert sort_col in cfg.get("hidden", []), f"{t}[{sort_col}] is a sort helper and should be hidden"
    calc_cols = {t: {c[0]: c[3] for c in cols} for t, (_, cols) in CALCULATED.items()}
    for ft, fc, tt, tc, _ in RELATIONSHIPS:
        key = TABLES[tt][1].get("key") == tc if tt in TABLES else "isKey" in calc_cols[tt][tc]
        assert key, f"relationship {ft}[{fc}] -> {tt}[{tc}] must point at a key column"
        assert fc in TABLES[ft][1].get("hidden", []) or fc.endswith("_date"), f"foreign key {ft}[{fc}] should be hidden"
    print(f"  ✓ {len(MEASURES)} measures, {len(FUNCTIONS)} functions: every reference resolves; best-practice rules pass")


def folder(path):
    sep = "\\" if "\\" in path else "/"
    return path.rstrip("/\\") + sep


def write_model(con, out, fabric, data_dir, raw_dir):
    """fabric=False: local Parquet import (Desktop on any machine, CI). fabric=True: Lakehouse SQL composite."""
    m_tables = {n: v for n, v in M_TABLES.items() if not (fabric and v[2])}  # raw CSVs live on the dev machine only
    shutil.rmtree(out / "definition", ignore_errors=True)
    d = out / "definition"
    (d / "tables").mkdir(parents=True)
    for sub in ("roles", "perspectives", "cultures"):
        (d / sub).mkdir()
    display = "SalesPipeline (Fabric)" if fabric else "SalesPipeline"
    logical = "9c2e7b14-5a3f-4d8e-b6c1-0f4a2d9e7c35" if fabric else "3f6a1c52-8e47-4b9d-a0c3-5d2e7f914b68"
    (out / "definition.pbism").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",\n'
        '  "version": "4.2",\n  "settings": {}\n}\n')
    (out / ".platform").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",\n'
        f'  "metadata": {{"type": "SemanticModel", "displayName": "{display}"}},\n'
        f'  "config": {{"version": "2.0", "logicalId": "{logical}"}}\n}}\n')
    # 1702: user-defined functions and calendars need the current compatibility level.
    (d / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1702\n")
    extra = ["Snapshot Agg"] if fabric else []
    all_tables = [*TABLES, *extra, *m_tables, *CALCULATED, "Analyze By", "Metric", "Date Basis", "Time Intelligence", "_Measures"]
    (d / "model.tmdl").write_text("\n".join([
        "model Model", f"{T}culture: en-US", f"{T}defaultPowerBIDataSourceVersion: powerBI_V3",
        f"{T}discourageImplicitMeasures", f"{T}sourceQueryCulture: en-US", f"{T}dataAccessOptions",
        f"{T*2}legacyRedirects", f"{T*2}returnErrorValuesAsNull", "",
        "annotation __PBI_TimeIntelligenceEnabled = 0", "",
        *[f"ref table {q(t)}" for t in all_tables], "",
        *[f"ref role {q(r)}" for r in ROLES], "",
        *[f"ref perspective {q(p)}" for p in PERSPECTIVES], "",
        "ref cultureInfo en-US", ""]))
    # RangeStart/RangeEnd are the reserved names Power BI's incremental refresh substitutes per partition.
    # In Desktop they bound the import to the whole history; the service rewrites them per partition.
    param = 'meta [IsParameterQuery=true, Type="{t}", IsParameterQueryRequired=true]'

    def expr(name, value, rtype, kind):
        return [f"expression {name} = {value} {param.format(t=kind)}", f"{T}lineageTag: {lineage()}", "",
                f"{T}annotation PBI_ResultType = {rtype}", ""]
    params = expr("SqlEndpoint", '"<workspace>.datawarehouse.fabric.microsoft.com"', "Text", "Text") \
        + expr("LakehouseDb", '"crm_lakehouse"', "Text", "Text") if fabric \
        else expr("DataFolder", f'"{data_dir}"', "Text", "Text") + expr("RawFolder", f'"{raw_dir}"', "Text", "Text")
    params += expr("RangeStart", "#datetime(2016, 10, 1, 0, 0, 0)", "DateTime", "DateTime")
    params += expr("RangeEnd", "#datetime(2018, 1, 1, 0, 0, 0)", "DateTime", "DateTime")
    if not fabric:  # staging queries: Enable load off, referenced by the loaded M tables
        for name, steps in STAGING.items():
            params += [f"expression {name} =", *[f"{T*2}{l}" for l in ["let", *steps]], f"{T}lineageTag: {lineage()}", "",
                       f"{T}annotation PBI_ResultType = Table", ""]
    (d / "expressions.tmdl").write_text("\n".join(params))

    for name, (mart, cfg) in TABLES.items():
        (d / "tables" / f"{name}.tmdl").write_text(table_tmdl(con, name, mart, cfg, fabric))
    if fabric:
        (d / "tables" / "Snapshot Agg.tmdl").write_text(snapshot_agg_tmdl())
    for name, (cols, steps, _) in m_tables.items():
        (d / "tables" / f"{name}.tmdl").write_text(m_table_tmdl(name, cols, steps))
    for name, (dax, cols) in CALCULATED.items():
        (d / "tables" / f"{name}.tmdl").write_text(calculated_tmdl(name, dax, cols))
    (d / "tables" / "Analyze By.tmdl").write_text(field_parameter_tmdl())
    (d / "tables" / "Metric.tmdl").write_text(field_parameter_tmdl("Metric", METRIC))
    # Both groups can be selected together: Time Intelligence (precedence 20) shifts the dates,
    # Date Basis (10) picks which date relationship those dates filter.
    (d / "tables" / "Date Basis.tmdl").write_text(calc_group_tmdl("Date Basis", "Date Basis", 10, DATE_BASIS))
    (d / "tables" / "Time Intelligence.tmdl").write_text(calc_group_tmdl("Time Intelligence", "Period", 20, TIME_ITEMS))
    (d / "functions.tmdl").write_text(functions_tmdl())
    (d / "tables" / "_Measures.tmdl").write_text(measures_tmdl())
    for name, r in ROLES.items():
        r = {**r, "hide_tables": [t for t in r["hide_tables"] if t in all_tables]}
        (d / "roles" / f"{name}.tmdl").write_text(role_tmdl(name, r))
    for name, tables in PERSPECTIVES.items():
        (d / "perspectives" / f"{name}.tmdl").write_text(perspective_tmdl(name, [t for t in tables if t in all_tables]))
    (d / "cultures" / "en-US.tmdl").write_text(culture_tmdl())
    if not fabric:
        (out / "DAXQueries").mkdir(exist_ok=True)
        for fname, text in DAX_QUERIES.items():
            (out / "DAXQueries" / fname).write_text(text)
        for name, (text, expect) in DAX_TESTS.items():  # pbi-kit daxtest.py: .dax + .assert.json
            (out / "DAXQueries" / f"{name}.dax").write_text(text)
            (out / "DAXQueries" / f"{name}.assert.json").write_text(json.dumps(expect, indent=1) + "\n")

    rel, kinds = [], []
    for ft, fc, tt, tc, active in RELATIONSHIPS + (FABRIC_RELATIONSHIPS if fabric else []):
        rel += [f"relationship {lineage()}"]
        if not active:
            rel.append(f"{T}isActive: false")
        rel += [f"{T}fromColumn: {q(ft)}.{q(disp(ft, fc))}", f"{T}toColumn: {q(tt)}.{q(disp(tt, tc))}", ""]
        kinds.append(f"{ft} -> {tt}: {relationship_kind(ft, tt, fabric)}")
    (d / "relationships.tmdl").write_text("\n".join(rel))
    print(f"  ✓ wrote {out.relative_to(ROOT)} ({len(all_tables)} tables, {len(kinds)} relationships, "
          f"{len(FUNCTIONS)} functions, {len(CALENDARS)} calendar, {len(ROLES)} roles, {len(PERSPECTIVES)} perspectives)")
    limited = sorted({k for k in kinds if k.endswith("limited")})
    print(f"    relationships: {len(kinds) - len([k for k in kinds if k.endswith('limited')])} regular, "
          f"{len([k for k in kinds if k.endswith('limited')])} limited" + (f" ({'; '.join(limited)})" if limited else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data" / "marts"))
    ap.add_argument("--raw-dir", default=str(ROOT / "data" / "raw"))
    args = ap.parse_args()
    con = duckdb.connect(str(ROOT / "data" / "warehouse" / "crm.duckdb"), read_only=True)
    check(con)
    write_model(con, OUT, False, folder(args.data_dir), folder(args.raw_dir))
    write_model(con, FABRIC_OUT, True, None, None)


if __name__ == "__main__":
    main()
