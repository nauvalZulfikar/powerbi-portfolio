"""Generate the Power BI semantic model (TMDL) from the DuckDB mart schema.

    uv run python src/build_model.py [--data-dir PATH]

Column types are read from DuckDB, so the model never drifts from the SQL.
--data-dir is the folder Power BI reads the Parquet marts from (default: this
repo's data/marts). On Windows, pass e.g. --data-dir "C:\\finance-variance\\data\\marts"
or change it later in Power BI: Transform data > Edit parameters > DataFolder.

Beyond the imported star schema the model carries:
  * a calculation group (Time View: Period / QTD / YTD / Full year)
  * a field parameter (Analyze By: Entity / Department / P&L line)
  * disconnected tables (Compare To, As Of Month, FX Shock what-if)
  * row-level security (dynamic Entity Manager role + static Europe Finance role)
  * object-level security (External Auditor role hides planning tables and a column)
  * DAX user-defined functions (functions.tmdl) used by the measures
  * calendar-based time intelligence (a Gregorian calendar on Date) used by Time View
  * dynamic format strings, a KPI, RANKX and SVG image measures
  * perspectives, en-US synonyms and an id-ID translation
  * Power Query: parameters (DataFolder, RawFolder, ForecastFolder, SourceMode, SqlEndpoint, LakehouseDb),
    a Local/Lakehouse source switch on every partition, FX Rates shaped from the raw daily CSV, and the
    Forecast table combined from a folder of entity files (--raw-dir / --forecast-dir set the folders)
  * role-playing Date (inactive GL[Posting Date] relationship + USERELATIONSHIP), hierarchies, a calculated
    column, window functions (OFFSET / WINDOW / INDEX / RANK), a Metric field parameter, a many-to-many
    (limited) relationship to a SYNTHETIC headcount plan, and an incremental refresh policy on GL
  * two variant models of the same tables and DAX, sourced from the Fabric Lakehouse:
      GroupFinance.Fabric.SemanticModel   composite: GL Journal DirectQuery + GL Journal Agg (alternateOf),
                                          Date/Entity/Account/Department dual, the rest import
      GroupFinance.DirectLake.SemanticModel  every table Direct Lake on the Lakehouse SQL endpoint
"""
import argparse
import json
import re
import shutil
import uuid
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "powerbi" / "GroupFinance.SemanticModel"
FABRIC_OUT = ROOT / "powerbi" / "GroupFinance.Fabric.SemanticModel"
DL_OUT = ROOT / "powerbi" / "GroupFinance.DirectLake.SemanticModel"
DAX_DIR = ROOT / "dax"
T = "\t"

TYPES = {  # duckdb -> (TMDL dataType, Power Query type)
    "BIGINT": ("int64", "Int64.Type"), "INTEGER": ("int64", "Int64.Type"),
    "DOUBLE": ("double", "type number"), "DATE": ("dateTime", "type date"),
    "BOOLEAN": ("boolean", "type logical"), "VARCHAR": ("string", "type text"),
}
USD0 = "\\$#,0;(\\$#,0);\\$0"
PCT1, PCT_SIGNED, RATE = "0.0%", "+0.0%;-0.0%;0.0%", "0.0000"

# Power BI table -> (mart table, config)
def GROUPING(table, column):
    """extendedProperty Desktop writes on a group column: which column the groups were built from."""
    meta = {"version": 0, "groupedColumns": [{"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": column}}]}
    return ["extendedProperty GroupingMetadata =", *("\t\t" + ln for ln in json.dumps(meta, indent=2).split("\n"))]


TABLES = {
    "Date": ("dim_date", dict(
        key="date", date_table=True,
        sort={"month_name": "month_no", "month_year": "month_year_sort", "year_quarter": "year_quarter_sort"},
        hidden=["month_year_sort", "year_quarter_sort"], fmt={"date": "dd mmm yyyy", "month_start": "mmm yyyy"},
        names={"month_no": "Month Number"})),
    "Entity": ("dim_entity", dict(
        key="entity_key", sort={"entity": "entity_sort"}, hidden=["entity_sort", "entity_path"],
        fmt={"effective_ownership": PCT1, "first_period": "mmm yyyy"},
        # The one calculated column: a static per-entity flag for slicing, computed once at refresh.
        calc_cols=[("Has Budget", "boolean", None,
                    'NOT ISEMPTY ( FILTER ( RELATEDTABLE ( GL ), GL[Scenario] = "Budget" ) )'),
                   # A Desktop "group" (Entity → New group): a SWITCH column tagged with GroupingMetadata.
                   ("Currency Group", "string", None,
                    'SWITCH ( TRUE (), ISBLANK ( \'Entity\'[Entity] ), "(Blank)", '
                    '\'Entity\'[Entity] IN { "Australia", "Canadian Division", "France", "Germany" }, "Non-USD entities", '
                    '"USD entities" )', GROUPING("Entity", "Entity"))],
        category={"country": "Country"},
        names={"currency_code": "Functional Currency", "first_period": "Consolidated From"},
        # Headcount Plan is budgeted per region (coarser than Entity): the M:M on Region is the design, and it
        # filters one way Entity -> plan, so the RLS filter on Entity still narrows the plan rows.
        bpa_ignore=["AVOID_USING_MANY-TO-MANY_RELATIONSHIPS_ON_TABLES_USED_FOR_DYNAMIC_ROW_LEVEL_SECURITY"])),
    "Department": ("dim_department", dict(key="department_key", hidden=["department_key"])),
    "Account": ("dim_account", dict(
        key="account_key", sort={"detail_line": "detail_line_sort"},
        hidden=["account_key", "line_id", "detail_line_sort", "pnl_sign"],
        names={"pnl_sign": "PnL Sign", "line_id": "Detail Line Id"})),
    "P&L Line": ("dim_pnl_line", dict(
        key="line_id", sort={"line": "line_sort"}, hidden=["line_sort"], names={"line": "Line"})),
    "Line Account": ("bridge_line_account", dict(hidden_table=True, hidden=["line_id", "account_key"])),
    "GL": ("fact_gl", dict(
        filter="each [period] >= #date(2011, 1, 1) and [period] <= #date(2013, 12, 1)",
        # amount_local stays in the mart (tests, HTML twin) but no measure or visual reads it: not loaded.
        drop=["amount_local"],
        hidden=["entity_key", "department_key", "account_key", "period", "posting_date", "fx_rate", "budget_rate"],
        fmt={"fx_rate": RATE, "budget_rate": RATE},
        names={"amount_usd": "Amount USD", "amount_usd_budget_rate": "Amount USD at Budget Rate",
               "fx_rate": "Translation Rate", "budget_rate": "Plan Rate", "currency_code": "Currency"},
        sum=["amount_usd", "amount_usd_budget_rate"],
        # Incremental refresh: keep 5 years, reload the last 2 months (books close monthly); a partition
        # is only reloaded when its newest posting date moved.
        refresh=dict(column="period", polling="posting_date", rolling=("year", 5), incremental=("month", 2)))),
    "GL Journal": ("gl_journal", dict(
        drop=["amount_local"], hidden=["entity_key", "department_key", "account_key", "period"],
        names={"journal_id": "Journal ID", "amount_usd": "Amount USD"}, sum=["amount_usd"],
        fmt={"journal_id": "0"})),
    "Headcount": ("fact_headcount", dict(
        hidden=["period", "entity_key", "department_key"], names={"headcount": "Heads"}, sum=["headcount"],
        fmt={"headcount": "#,0"})),
    "FX Rates": ("fx_rates", dict(
        m="FX Rates", hidden=["period"], fmt={"avg_rate": RATE, "budget_rate": RATE},
        names={"currency_code": "Rate Currency", "avg_rate": "Average Rate", "budget_rate": "Budget Rate"})),
    "Budget Drivers": ("budget_drivers", dict(
        hidden=["line_id", "line_sort"], sort={"line": "line_sort"}, fmt={"growth": PCT_SIGNED, "fiscal_year": "0"},
        names={"line": "Driver Line", "growth": "Growth"})),
    "Entity Access": ("entity_access", dict(hidden_table=True)),
    "Variance Step": ("dim_variance_step", dict(sort={"step": "step_sort"}, hidden=["step_sort", "step_id"])),
    "Forecast": ("forecast", dict(
        m="Forecast", hidden=["entity_key", "line_id", "period", "row_index"], sum=["amount_usd"],
        fmt={"fiscal_year": "0", "row_index": "0", "submitted": "dd mmm"})),
    "Headcount Plan": ("headcount_plan", dict(
        hidden=["region", "department_key"], names={"planned_heads": "Planned Heads"}, sum=["planned_heads"],
        fmt={"planned_heads": "#,0", "fiscal_year": "0"})),
}

# Hierarchies: table -> [(hierarchy, [level columns, top first])]
HIERARCHIES = {
    "Date": [("Fiscal Calendar", ["Year", "Year Quarter", "Month Year"])],
    "Entity": [("Entity Tree", ["Region", "Country", "Entity"])],
    "Account": [("P&L Drill", ["Detail Line", "Account"])],
}

RELATIONSHIPS = [  # (from table, from col, to table, to col, bidirectional[, "inactive" | "m2m"])
    ("GL", "period", "Date", "date", False),
    # Role-playing Date: the posting date relationship is inactive; USERELATIONSHIP switches to it.
    ("GL", "posting_date", "Date", "date", False, "inactive"),
    ("GL Journal", "period", "Date", "date", False),
    ("GL Journal", "entity_key", "Entity", "entity_key", False),
    ("GL Journal", "department_key", "Department", "department_key", False),
    ("GL Journal", "account_key", "Account", "account_key", False),
    # The plan is per region, Entity is per entity: many-to-many on Region (a limited relationship),
    # filtering one way, Entity -> plan.
    ("Headcount Plan", "region", "Entity", "region", False, "m2m"),
    ("Headcount Plan", "department_key", "Department", "department_key", False),
    ("GL", "entity_key", "Entity", "entity_key", False),
    ("GL", "department_key", "Department", "department_key", False),
    ("GL", "account_key", "Account", "account_key", False),
    ("Headcount", "period", "Date", "date", False),
    ("Headcount", "entity_key", "Entity", "entity_key", False),
    ("Headcount", "department_key", "Department", "department_key", False),
    ("FX Rates", "period", "Date", "date", False),
    ("Forecast", "period", "Date", "date", False),
    ("Forecast", "entity_key", "Entity", "entity_key", False),
    # P&L Line -> Line Account -> Account -> GL: a subtotal line reaches every account under it.
    ("Line Account", "account_key", "Account", "account_key", True),
    ("Line Account", "line_id", "P&L Line", "line_id", False),
]

# ----------------------------------------------------------------- Power Query
# Parameters (expressions.tmdl). SourceMode switches every Parquet partition to the Fabric Lakehouse SQL endpoint.
def parameters(data_dir, raw_dir, forecast_dir):
    return [("DataFolder", "Text", f'"{data_dir}"'), ("RawFolder", "Text", f'"{raw_dir}"'),
            ("ForecastFolder", "Text", f'"{forecast_dir}"'), ("SourceMode", "Text", '"Local"'),
            ("SqlEndpoint", "Text", '"<workspace>.datawarehouse.fabric.microsoft.com"'),
            ("LakehouseDb", "Text", '"finance_lh"')]


MONTHS = '{"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"}'
# Not-loaded queries: (name, result type, M). Step names are what the Applied Steps pane shows.
STAGING = [
    # Raw daily rates: headerless pipe-delimited export, typed with an explicit locale.
    ("FX Daily Raw", "Table", """let
    Source = Csv.Document(File.Contents(RawFolder & "FactCurrencyRate.csv"), [Delimiter = "|", Columns = 5, Encoding = 65001, QuoteStyle = QuoteStyle.None]),
    #"Name columns" = Table.RenameColumns(Source, {{"Column1", "CurrencyKey"}, {"Column2", "DateKey"}, {"Column3", "AverageRate"}, {"Column4", "EndOfDayRate"}, {"Column5", "RateDate"}}),
    #"Choose columns" = Table.SelectColumns(#"Name columns", {"CurrencyKey", "RateDate", "AverageRate"}),
    #"Split date from time" = Table.SplitColumn(#"Choose columns", "RateDate", Splitter.SplitTextByDelimiter(" ", QuoteStyle.None), {"Rate Day", "Rate Time"}),
    #"Remove time" = Table.RemoveColumns(#"Split date from time", {"Rate Time"}),
    #"Type with en-US locale" = Table.TransformColumnTypes(#"Remove time", {{"CurrencyKey", Int64.Type}, {"Rate Day", type date}, {"AverageRate", type number}}, "en-US"),
    Currencies = Csv.Document(File.Contents(RawFolder & "DimCurrency.csv"), [Delimiter = "|", Columns = 3, Encoding = 65001, QuoteStyle = QuoteStyle.None]),
    #"Name currency columns" = Table.RenameColumns(Currencies, {{"Column1", "CurrencyKey"}, {"Column2", "Rate Currency"}, {"Column3", "Currency Name"}}),
    #"Type currency key" = Table.TransformColumnTypes(#"Name currency columns", {{"CurrencyKey", Int64.Type}}),
    #"Merge currency code" = Table.NestedJoin(#"Type with en-US locale", {"CurrencyKey"}, #"Type currency key", {"CurrencyKey"}, "Currency", JoinKind.Inner),
    #"Expand currency code" = Table.ExpandTableColumn(#"Merge currency code", "Currency", {"Rate Currency"}),
    #"Keep group currencies" = Table.SelectRows(#"Expand currency code", each List.Contains({"EUR", "CAD", "AUD"}, [Rate Currency]))
in
    #"Keep group currencies\""""),
    # Rows whose rate failed to parse, with the reason, so rejected rows stay visible (Reference of FX Daily Raw).
    ("FX Rate Errors", "Table", """let
    Source = #"FX Daily Raw",
    #"Keep rows with errors" = Table.SelectRowsWithErrors(Source, {"AverageRate", "Rate Day"}),
    #"Add reason" = Table.AddColumn(#"Keep rows with errors", "Reason", each let rate = try [AverageRate], day = try [Rate Day] in if rate[HasError] then rate[Error][Message] else day[Error][Message], type text)
in
    #"Add reason\""""),
    # Parses one forecast submission; the file name carries the fiscal year and the file's culture.
    ("Parse Forecast File", "Function", """(content as binary, fileName as text) as table =>
let
    Culture = if Text.Contains(fileName, "de-DE") then "de-DE" else "en-US",
    FiscalYear = Number.From(Text.Middle(fileName, Text.PositionOf(fileName, "FY") + 2, 4)),
    Source = Csv.Document(content, [Delimiter = if Culture = "de-DE" then ";" else ",", Columns = 16, Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
    #"Skip title rows" = Table.Skip(Source, 2),
    #"Promote headers" = Table.PromoteHeaders(#"Skip title rows", [PromoteAllScalars = true]),
    #"Blank entity to null" = Table.ReplaceValue(#"Promote headers", "", null, Replacer.ReplaceValue, {"Entity"}),
    #"Fill down entity" = Table.FillDown(#"Blank entity to null", {"Entity"}),
    #"Replace n/a" = Table.ReplaceValue(#"Fill down entity", "n/a", null, Replacer.ReplaceValue, MONTHS),
    #"Replace dash" = Table.ReplaceValue(#"Replace n/a", "-", null, Replacer.ReplaceValue, MONTHS),
    #"Remove total" = Table.RemoveColumns(#"Replace dash", {"Total"}),
    #"Add row index" = Table.AddIndexColumn(#"Remove total", "row_index", 1, 1, Int64.Type),
    #"Unpivot months" = Table.UnpivotOtherColumns(#"Add row index", {"Entity", "Line", "Submitted", "row_index"}, "Month", "Amount"),
    #"Type with file locale" = Table.TransformColumnTypes(#"Unpivot months", {{"Amount", type number}, {"Submitted", type date}}, Culture),
    #"Add fiscal year" = Table.AddColumn(#"Type with file locale", "fiscal_year", each FiscalYear, Int64.Type),
    #"Add source file" = Table.AddColumn(#"Add fiscal year", "source_file", each fileName, type text)
in
    #"Add source file\"""".replace("MONTHS", MONTHS)),
]

# Loaded tables whose partition is Power Query shaping instead of a Parquet mart.
# The mart of the same name stays as the SQL twin the tests reconcile against.
PQ_PARTITIONS = {
    # Monthly average and budget rate (prior-December average), built from the daily rates.
    "FX Rates": """let
    Source = #"FX Daily Raw",
    #"Remove rate errors" = Table.RemoveRowsWithErrors(Source, {"AverageRate", "Rate Day"}),
    #"Add month" = Table.AddColumn(#"Remove rate errors", "period", each Date.StartOfMonth([Rate Day]), type date),
    #"Group by month" = Table.Group(#"Add month", {"Rate Currency", "period"}, {{"avg_rate", each List.Average([AverageRate]), type number}}),
    #"Add fiscal year" = Table.AddColumn(#"Group by month", "Fiscal Year", each Date.Year([period]), Int64.Type),
    Decembers = Table.SelectRows(#"Add fiscal year", each Date.Month([period]) = 12),
    #"December sets next year's rate" = Table.TransformColumns(Decembers, {{"Fiscal Year", each _ + 1, Int64.Type}}),
    #"Budget rates" = Table.RenameColumns(Table.SelectColumns(#"December sets next year's rate", {"Rate Currency", "Fiscal Year", "avg_rate"}), {{"avg_rate", "budget_rate"}}),
    #"Merge budget rate" = Table.NestedJoin(#"Add fiscal year", {"Rate Currency", "Fiscal Year"}, #"Budget rates", {"Rate Currency", "Fiscal Year"}, "Budget", JoinKind.Inner),
    #"Expand budget rate" = Table.ExpandTableColumn(#"Merge budget rate", "Budget", {"budget_rate"}),
    #"Keep reported periods" = Table.SelectRows(#"Expand budget rate", each [period] >= #date(2011, 1, 1) and [period] <= #date(2013, 12, 1)),
    #"Rename currency" = Table.RenameColumns(#"Keep reported periods", {{"Rate Currency", "currency_code"}}),
    #"Choose columns" = Table.SelectColumns(#"Rename currency", {"currency_code", "period", "avg_rate", "budget_rate"}),
    Typed = Table.TransformColumnTypes(#"Choose columns", {{"currency_code", type text}, {"avg_rate", type number}, {"budget_rate", type number}})
in
    Typed""",
    # Every entity's reforecast file: folder combine (append), clean, split, merge to Entity and P&L Line.
    "Forecast": """let
    Source = Folder.Files(ForecastFolder),
    #"Keep csv files" = Table.SelectRows(Source, each Text.Lower([Extension]) = ".csv" and Text.StartsWith([Name], "forecast_")),
    #"Parse each file" = Table.AddColumn(#"Keep csv files", "Rows", each #"Parse Forecast File"([Content], [Name])),
    #"Append files" = Table.Combine(#"Parse each file"[Rows]),
    #"Trim and clean names" = Table.TransformColumns(#"Append files", {{"Entity", each Text.Clean(Text.Trim(_)), type text}, {"Line", each Text.Clean(Text.Trim(_)), type text}}),
    #"Split entity code" = Table.SplitColumn(#"Trim and clean names", "Entity", Splitter.SplitTextByDelimiter(" - ", QuoteStyle.None), {"entity_code", "Entity Name"}),
    #"Add period" = Table.AddColumn(#"Split entity code", "period", each #date([fiscal_year], List.PositionOf(MONTHS, [Month]) + 1, 1), type date),
    #"Add late flag" = Table.AddColumn(#"Add period", "is_late_submission", each if [Submitted] > #date([fiscal_year], 1, 10) then true else false, type logical),
    #"Merge entity" = Table.NestedJoin(#"Add late flag", {"Entity Name"}, Entity, {"entity"}, "EntityRow", JoinKind.Inner),
    #"Expand entity key" = Table.ExpandTableColumn(#"Merge entity", "EntityRow", {"entity_key"}),
    #"Merge P&L line" = Table.NestedJoin(#"Expand entity key", {"Line"}, #"P&L Line", {"line"}, "LineRow", JoinKind.Inner),
    #"Expand line id" = Table.ExpandTableColumn(#"Merge P&L line", "LineRow", {"line_id"}),
    #"Rename columns" = Table.RenameColumns(#"Expand line id", {{"Amount", "amount_usd"}, {"Submitted", "submitted"}}),
    #"Choose columns" = Table.SelectColumns(#"Rename columns", {"entity_key", "entity_code", "line_id", "period", "fiscal_year", "amount_usd", "submitted", "is_late_submission", "source_file", "row_index"}),
    Typed = Table.TransformColumnTypes(#"Choose columns", {{"entity_key", Int64.Type}, {"entity_code", type text}, {"line_id", type text}, {"amount_usd", type number}})
in
    Typed""".replace("MONTHS", MONTHS),
}

# DAX-only tables: name -> (DAX source, [(column, sourceColumn, dataType, extra lines)])
CALCULATED = {
    "Compare To": (
        'DATATABLE ( "Compare To", STRING, "Compare Order", INTEGER, { { "Budget", 1 }, { "Prior year", 2 }, { "Forecast", 3 } } )',
        [("Compare To", "[Compare To]", "string", ["sortByColumn: 'Compare Order'"]),
         ("Compare Order", "[Compare Order]", "int64", ["isHidden", "formatString: 0"])]),
    "As Of Month": (
        "SELECTCOLUMNS (\n"
        "    SUMMARIZE ( 'Date', 'Date'[Month Start], 'Date'[Month Year], 'Date'[Month Year Sort] ),\n"
        "    \"As Of Month\", 'Date'[Month Start],\n"
        "    \"As Of\", 'Date'[Month Year],\n"
        "    \"As Of Sort\", 'Date'[Month Year Sort]\n"
        ")",
        [("As Of Month", "[As Of Month]", "dateTime", ["isHidden", "formatString: mmm yyyy"]),
         ("As Of", "[As Of]", "string", ["sortByColumn: 'As Of Sort'"]),
         ("As Of Sort", "[As Of Sort]", "int64", ["isHidden", "formatString: 0"])]),
    # Variance bands for dynamic segmentation (disconnected; Entities in Band reads the limits).
    "Variance Band": (
        'DATATABLE ( "Band", STRING, "Band Order", INTEGER, "Lower", DOUBLE, "Upper", DOUBLE, '
        '{ { "Big miss", 1, -1E9, -0.10 }, { "Miss", 2, -0.10, -0.02 }, { "On plan", 3, -0.02, 0.02 }, { "Beat", 4, 0.02, 1E9 } } )',
        [("Band", "[Band]", "string", ["sortByColumn: 'Band Order'"]),
         ("Band Order", "[Band Order]", "int64", ["isHidden", "formatString: 0"]),
         ("Lower", "[Lower]", "double", ["isHidden", f"formatString: {PCT_SIGNED}"]),
         ("Upper", "[Upper]", "double", ["isHidden", f"formatString: {PCT_SIGNED}"])]),
    "FX Shock": (
        'SELECTCOLUMNS ( GENERATESERIES ( -0.2, 0.2, 0.05 ), "FX Shock", [Value] )',
        [("FX Shock", "[FX Shock]", "double", [f"formatString: {PCT_SIGNED}"])]),
}

# Direct Lake: a calculated table can't read a Direct Lake table, so the As Of list is built from CALENDAR.
AS_OF_DL = ("SELECTCOLUMNS (\n"
            "    FILTER ( CALENDAR ( DATE ( 2011, 1, 1 ), DATE ( 2013, 12, 1 ) ), DAY ( [Date] ) = 1 ),\n"
            "    \"As Of Month\", [Date],\n"
            "    \"As Of\", FORMAT ( [Date], \"mmm yyyy\" ),\n"
            "    \"As Of Sort\", YEAR ( [Date] ) * 100 + MONTH ( [Date] )\n"
            ")")

ANALYZE_BY = [("Entity", "'Entity'[Entity]"), ("Department", "'Department'[Department]"),
              ("P&L line", "'P&L Line'[Line]")]

# Calendar-based time intelligence on Date: (calendar, [(time unit, primary column, associated columns)])
CALENDARS = [
    ("Gregorian", [("year", "Year", []), ("quarter", "Year Quarter Sort", ["Year Quarter"]),
                   ("quarterOfYear", "Quarter", []),
                   ("month", "Month Year Sort", ["Month Year", "Month Start"]),
                   ("monthOfYear", "Month Number", ["Month Name"]), ("date", "Date", [])]),
]

# Calculation group: (item, DAX, format string expression or None). 'Period' leaves the slicer context as-is.
# Every item but Period runs on the Gregorian calendar, not on 'Date'[Date].
PY = "DATEADD ( 'Gregorian', -1, YEAR )"
TIME_VIEW = [
    ("Period", "SELECTEDMEASURE ()", None),
    ("QTD", "CALCULATE ( SELECTEDMEASURE (), DATESQTD ( 'Gregorian' ) )", None),
    ("YTD", "CALCULATE ( SELECTEDMEASURE (), DATESYTD ( 'Gregorian' ) )", None),
    ("Full year", "CALCULATE ( SELECTEDMEASURE (), PARALLELPERIOD ( 'Gregorian', 0, YEAR ) )", None),
    ("Prior year", f"CALCULATE ( SELECTEDMEASURE (), {PY} )", None),
    ("YoY %", f"VAR _py = CALCULATE ( SELECTEDMEASURE (), {PY} )\nRETURN DIVIDE ( SELECTEDMEASURE () - _py, ABS ( _py ) )",
     '"+0.0%;-0.0%;0.0%"'),
    ("Prior quarter", "CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, QUARTER ) )", None),
    ("Prior month", "CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, MONTH ) )", None),
    ("MoM %", "VAR _pm = CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, MONTH ) )\nRETURN DIVIDE ( SELECTEDMEASURE () - _pm, ABS ( _pm ) )",
     '"+0.0%;-0.0%;0.0%"'),
]

# Metric field parameter: which operating-profit series the monthly line chart plots.
METRIC = [("Actual", "[Operating Profit]"), ("Comparison", "[Operating Profit Comparison]"),
          ("Outlook", "[Operating Profit Outlook]"), ("Variance", "[Operating Profit Variance]"),
          ("FX Effect", "[Operating Profit FX Effect]"), ("Operational Variance", "[Operating Profit Operational Variance]"),
          ("Prior month", "[Operating Profit PM]")]

# Roles: row filters (RLS), hidden tables and hidden columns (OLS).
ROLES = {
    # Users see the entities at or below the node(s) they are granted in Entity Access.
    "Entity Manager": dict(filters={
        "Entity": "VAR _u = USERPRINCIPALNAME () RETURN NOT ISEMPTY ( FILTER ( 'Entity Access', "
                  "'Entity Access'[User Email] = _u && PATHCONTAINS ( Entity[Entity Path], 'Entity Access'[Entity Key] & \"\" ) ) )",
        "Entity Access": "'Entity Access'[User Email] = USERPRINCIPALNAME ()"}, hide_tables=[], hide_columns={}),
    "Europe Finance": dict(filters={"Entity": "Entity[Region] = \"European Operations\""}, hide_tables=[], hide_columns={}),
    # Object-level security: an external auditor sees the ledger but not the planning assumptions,
    # the access list, or where each budget row came from.
    "External Auditor": dict(filters={}, hide_tables=["Budget Drivers", "Entity Access"],
                             hide_columns={"GL": ["Budget Source"]}),
}

PERSPECTIVES = {
    "Variance Analysis": ["GL", "Forecast", "Date", "Entity", "Department", "Account", "P&L Line", "Line Account", "Compare To",
                          "As Of Month", "Variance Band", "Analyze By", "Time View", "Variance Step", "_Measures"],
    "Treasury & FX": ["GL", "FX Rates", "FX Shock", "Date", "Entity", "_Measures"],
    "People Cost": ["Headcount", "GL", "Date", "Entity", "Department", "Account", "P&L Line", "Line Account", "_Measures"],
}

SYNONYMS = {  # Q&A / Copilot terms (en-US linguistic schema)
    "GL": ["general ledger", "ledger", "journal"], "Entity": ["company", "subsidiary", "business unit"],
    "Actual": ["actuals", "real", "spend"], "Budget": ["plan", "target", "forecast"],
    "Variance": ["difference", "gap", "deviation"], "Operating Profit": ["EBIT", "operating income"],
    "Net Sales": ["revenue", "turnover", "sales"], "Avg Headcount": ["FTE", "heads", "staff"],
}

# id-ID translations: object -> (caption, display folder or None)
ID_TABLES = {"GL": "Buku Besar", "Date": "Tanggal", "Entity": "Entitas", "Department": "Departemen",
             "Account": "Akun", "P&L Line": "Baris Laba Rugi", "Headcount": "Jumlah Karyawan", "FX Rates": "Kurs",
             "Budget Drivers": "Asumsi Anggaran", "Forecast": "Prakiraan", "Compare To": "Bandingkan Dengan", "Time View": "Tampilan Waktu",
             "_Measures": "Ukuran"}
ID_FOLDERS = {"Ledger": "Buku Besar", "Variance": "Selisih", "Key Lines": "Baris Utama", "Consolidation": "Konsolidasi",
              "FX": "Kurs", "Headcount": "Karyawan", "Ranking": "Peringkat", "Analysis": "Analisis", "Report UI": "Tampilan Laporan"}
ID_MEASURES = {"Actual": "Realisasi", "Budget": "Anggaran", "Prior Year": "Tahun Lalu", "Comparison": "Pembanding",
               "Variance": "Selisih", "Variance %": "Selisih %", "Net Sales": "Penjualan Bersih",
               "Operating Expenses": "Beban Operasional", "Operating Profit": "Laba Operasional",
               "Operating Profit Variance": "Selisih Laba Operasional", "Gross Margin %": "Margin Kotor %",
               "Net Income": "Laba Bersih", "FX Effect": "Efek Kurs", "Avg Headcount": "Rata-rata Karyawan",
               "Entity Variance Rank": "Peringkat Selisih Entitas", "Account Variance Rank": "Peringkat Selisih Akun"}

# DAX user-defined functions: (name, description, signature => body)
FUNCTIONS = [
    ("Fin.Variance", "Actual minus comparison; blank only when both sides are blank.",
     "( actual : NUMERIC, comparison : NUMERIC ) => IF ( NOT ISBLANK ( actual ) || NOT ISBLANK ( comparison ), actual - comparison )"),
    ("Fin.VariancePct", "Variance as a share of the absolute comparison, so a cost line reads the same way as income.",
     "( variance : NUMERIC, comparison : NUMERIC ) => DIVIDE ( variance, ABS ( comparison ) )"),
    ("Fin.VarianceColor", "Teal when favourable, orange when not, for conditional formatting.",
     '( variance : NUMERIC ) => IF ( variance >= 0, "#0F766E", "#C2410C" )'),
    ("Fin.OnLine", "Evaluates a measure on one P&L line, kept inside the lines already filtered (subtotals reach every account under them).",
     # KEEPFILTERS: intersect with the line(s) already in context instead of replacing them, so in a
     # matrix by P&L line every other row is blank rather than repeating this line's number.
     "( value : SCALAR NUMERIC EXPR, lineId : STRING ) => CALCULATE ( value, KEEPFILTERS ( 'P&L Line'[Line ID] = lineId ) )"),
    ("Fin.PriorYear", "A measure one year back on the calendar passed in.",
     "( value : SCALAR NUMERIC EXPR, cal : CALENDARREF ) => CALCULATE ( value, DATEADD ( cal, -1, YEAR ) )"),
    ("Fin.Millions", "An amount as short text in millions, e.g. $13.8m.",
     '( amount : NUMERIC ) => FORMAT ( amount / 1e6, "$#,0.0" ) & "m"'),
    ("Svg.VarianceBar", "An inline SVG bar centred on zero: right and teal when favourable, left and orange when not.",
     "( variance : NUMERIC, maxAbs : NUMERIC ) =>\n"
     "\tVAR _w = ROUND ( DIVIDE ( ABS ( variance ), maxAbs ) * 58, 1 )\n"
     "\tVAR _x = IF ( variance >= 0, 60, 60 - _w )\n"
     "\tVAR _c = IF ( variance >= 0, \"%230F766E\", \"%23C2410C\" )\n"
     "\tRETURN\n"
     "\t\tIF ( NOT ISBLANK ( variance ),\n"
     "\t\t\t\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='120' height='16'>\"\n"
     "\t\t\t\t& \"<rect x='\" & _x & \"' y='3' width='\" & _w & \"' height='10' rx='2' fill='\" & _c & \"'/>\"\n"
     "\t\t\t\t& \"<line x1='60' y1='0' x2='60' y2='16' stroke='%238C959F'/></svg>\" )"),
]


def line(id_):
    return f"'P&L Line'[Line ID] = \"{id_}\""


def on(measure, id_):
    """A measure on one P&L line, through the Fin.OnLine function."""
    return f"Fin.OnLine ( [{measure}], \"{id_}\" )"


def money(expr):
    """DAX text for an amount in $m, e.g. $13.8m."""
    return f"Fin.Millions ( {expr} )"


def rank(col):
    return f"IF ( HASONEVALUE ( {col} ), RANKX ( ALLSELECTED ( {col} ), [Variance], , ASC, DENSE ) )"


def rank_window(col):
    """The same rank with the RANK window function: the relation is the visible rows, ordered by variance."""
    return f"IF ( HASONEVALUE ( {col} ), RANK ( DENSE, ALLSELECTED ( {col} ), ORDERBY ( [Variance], ASC ) ) )"


# Months on the visual's axis, in calendar order: the relation the OFFSET / WINDOW measures walk.
MONTH_AXIS = "ALLSELECTED ( 'Date'[Month Start] ), ORDERBY ( 'Date'[Month Start] )"
IN_MONTH = "ISINSCOPE ( 'Date'[Month Start] )"


def svg_bar(col):
    return f"Svg.VarianceBar ( [Variance], MAXX ( ALLSELECTED ( {col} ), ABS ( [Variance] ) ) )"


# (folder, name, format, description, DAX)
MEASURES = [
    # --------------------------------------------------------------- Ledger
    ("Ledger", "Actual", USD0, "Actual P&L in USD at the monthly average rate. Signed: income +, cost −.",
     "CALCULATE ( SUM ( GL[Amount USD] ), GL[Scenario] = \"Actual\" )"),
    ("Ledger", "Budget", USD0, "Budget in USD at the budget rate (prior-December average).",
     "CALCULATE ( SUM ( GL[Amount USD] ), GL[Scenario] = \"Budget\" )"),
    ("Ledger", "Prior Year", USD0, "Actuals one year back on the Gregorian calendar.",
     "Fin.PriorYear ( [Actual], 'Gregorian' )"),
    ("Ledger", "Forecast Amount", USD0,
     "FY2013 reforecast submitted by the entities (SYNTHETIC). Forecast has no relationship to P&L Line; TREATAS maps the detail lines in context.",
     "CALCULATE ( SUM ( Forecast[Amount USD] ), TREATAS ( VALUES ( Account[Detail Line Id] ), Forecast[Line ID] ) )"),
    ("Ledger", "Actual at Budget Rate", USD0, "Actuals translated at the budget rate — strips currency movement out.",
     "CALCULATE ( SUM ( GL[Amount USD at Budget Rate] ), GL[Scenario] = \"Actual\" )"),
    ("Ledger", "Outlook", USD0,
     "Full-year outlook: actuals up to the As Of month, budget for the months after it.",
     "VAR _asof = SELECTEDVALUE ( 'As Of Month'[As Of Month], MAX ( 'As Of Month'[As Of Month] ) )\n"
     "RETURN\n"
     "    SUMX (\n"
     "        VALUES ( 'Date'[Month Start] ),\n"
     "        IF ( 'Date'[Month Start] <= _asof, [Actual], [Budget] )\n"
     "    )"),
    # ------------------------------------------------------------- Variance
    ("Variance", "Comparison", USD0, "Budget, prior year or forecast, per the Compare To slicer.",
     "SWITCH ( SELECTEDVALUE ( 'Compare To'[Compare To], \"Budget\" ),\n"
     "    \"Prior year\", [Prior Year],\n"
     "    \"Forecast\", [Forecast Amount],\n"
     "    [Budget]\n"
     ")"),
    ("Variance", "Variance", USD0, "Actual − comparison. Positive is always favourable (more income or less cost).",
     "Fin.Variance ( [Actual], [Comparison] )"),
    ("Variance", "Variance %", PCT_SIGNED, "Variance as a share of the absolute comparison.",
     "Fin.VariancePct ( [Variance], [Comparison] )"),
    ("Variance", "Budget Variance", USD0, "Actual − budget, whatever the Compare To slicer says.",
     "Fin.Variance ( [Actual], [Budget] )"),
    ("Variance", "FX Effect", USD0, "Part of the budget variance caused by exchange rates: actual at actual rate − actual at budget rate.",
     "[Actual] - [Actual at Budget Rate]"),
    ("Variance", "Operational Variance", USD0, "Budget variance at constant (budget) rates — what management controls.",
     "[Actual at Budget Rate] - [Budget]"),
    ("Variance", "Outlook vs Budget", USD0, "Full-year outlook − full-year budget.", "[Outlook] - [Budget]"),
    ("Variance", "Variance Color", None, "Green when favourable, orange when not.", "Fin.VarianceColor ( [Variance] )"),
    ("Variance", "Bridge Amount", USD0,
     "Waterfall: budget operating profit, then the FX effect, then each line's operational variance.",
     "VAR _s = SELECTEDVALUE ( 'Variance Step'[Step ID] )\n"
     "RETURN\n"
     "    SWITCH ( _s,\n"
     f"        \"BUD\", {on('Budget', 'S4')},\n"
     f"        \"FX\", {on('FX Effect', 'S4')},\n"
     "        CALCULATE ( [Operational Variance], 'P&L Line'[Line ID] = _s )\n"
     "    )"),
    # ------------------------------------------------------------ Key lines
    ("Key Lines", "Net Sales", USD0, "Actual net sales (P&L line S1).", on("Actual", "S1")),
    ("Key Lines", "Net Sales Variance", USD0, "Net sales variance against the selected comparison.", on("Variance", "S1")),
    ("Key Lines", "Gross Margin %", PCT1, "Gross margin as a share of net sales.",
     f"DIVIDE ( {on('Actual', 'S2')}, [Net Sales] )"),
    ("Key Lines", "Operating Expenses", USD0, "Actual operating expenses, signed negative (P&L line S3).", on("Actual", "S3")),
    ("Key Lines", "Operating Expenses Variance", USD0, "Operating expense variance; positive means under spend.",
     on("Variance", "S3")),
    ("Key Lines", "Operating Profit", USD0, "Actual operating profit (P&L line S4); carries a KPI against the comparison.",
     on("Actual", "S4")),
    ("Key Lines", "Operating Profit Comparison", USD0, "Operating profit for the selected comparison.", on("Comparison", "S4")),
    ("Key Lines", "Operating Profit Variance", USD0,
     "Operating profit variance; its format string switches between $M and $K with the size of the number.",
     on("Variance", "S4")),
    ("Key Lines", "Operating Profit Variance %", PCT_SIGNED, "Operating profit variance as a share of the comparison.",
     on("Variance %", "S4")),
    ("Key Lines", "Operating Profit Budget", USD0, "Budgeted operating profit.", on("Budget", "S4")),
    ("Key Lines", "Operating Profit Outlook", USD0, "Outlook operating profit (actuals to date, budget after).",
     on("Outlook", "S4")),
    ("Key Lines", "Operating Profit FX Effect", USD0, "Part of the operating profit variance caused by exchange rates.",
     on("FX Effect", "S4")),
    ("Key Lines", "Operating Profit Operational Variance", USD0, "Operating profit variance at constant budget rates.",
     on("Operational Variance", "S4")),
    ("Key Lines", "Operating Profit Forecast", USD0, "Operating profit in the entities' FY2013 reforecast (SYNTHETIC).",
     on("Forecast Amount", "S4")),
    ("Key Lines", "Operating Profit vs Forecast", USD0, "Actual operating profit − reforecast; positive = beat the forecast.",
     "Fin.Variance ( [Operating Profit], [Operating Profit Forecast] )"),
    ("Key Lines", "Entities Submitted", "#,0", "Entities with a reforecast file in the forecast folder.",
     "DISTINCTCOUNT ( Forecast[Entity Key] ) + 0"),
    ("Key Lines", "Late Submissions", "#,0", "Entities whose reforecast arrived after the 10 January deadline.",
     "CALCULATE ( DISTINCTCOUNT ( Forecast[Entity Key] ), Forecast[Is Late Submission] = TRUE () ) + 0"),
    ("Key Lines", "Opex Spend", USD0, "Operating expenses as a positive amount.", "- [Operating Expenses]"),
    ("Key Lines", "Operating Margin %", PCT1, "Operating profit as a share of net sales.",
     "DIVIDE ( [Operating Profit], [Net Sales] )"),
    ("Key Lines", "Net Income", USD0, "Actual net income (P&L line S6).", on("Actual", "S6")),
    # ------------------------------------------------------------- Ranking
    ("Ranking", "Entity Variance Rank", "#,0", "Rank of each entity by variance; 1 is the worst miss (RANK window function, dense).",
     rank_window("Entity[Entity]")),
    ("Ranking", "Account Variance Rank", "#,0", "Rank of each account by variance; 1 is the worst miss (RANKX, dense).",
     rank("Account[Account]")),
    ("Ranking", "Entity Variance Bar", None, "Inline SVG variance bar per entity, scaled to the largest entity.",
     svg_bar("Entity[Entity]")),
    ("Ranking", "Account Variance Bar", None, "Inline SVG variance bar per account, scaled to the largest account.",
     svg_bar("Account[Account]")),
    # ------------------------------------------------------ Consolidation
    ("Consolidation", "Group Share", USD0, "Actual × each entity's effective ownership (Australia = 75% × 50%).",
     "SUMX ( FILTER ( GL, GL[Scenario] = \"Actual\" ), GL[Amount USD] * RELATED ( Entity[Effective Ownership] ) )"),
    ("Consolidation", "Non-Controlling Interest", USD0, "Share of results that belongs to minority shareholders.",
     "[Actual] - [Group Share]"),
    ("Consolidation", "Intercompany Eliminated", USD0, "Intercompany sales removed on consolidation (offset in cost of sales).",
     f"- CALCULATE ( [Actual], GL[Is Elimination] = TRUE (), {line('L02')} )"),
    # ------------------------------------------------------------------ FX
    ("FX", "Avg FX Rate", RATE, "Local currency per USD, monthly average.", "AVERAGE ( 'FX Rates'[Average Rate] )"),
    ("FX", "Budget FX Rate", RATE, "Local currency per USD used in the budget.", "AVERAGE ( 'FX Rates'[Budget Rate] )"),
    ("FX", "FX Move vs Budget %", PCT_SIGNED, "Change of the local currency against USD vs the budget rate (+ = local currency stronger).",
     "DIVIDE ( [Budget FX Rate], [Avg FX Rate] ) - 1"),
    *[("FX", f"{c} per USD", RATE, f"Average {c} per USD.", f"CALCULATE ( [Avg FX Rate], 'FX Rates'[Rate Currency] = \"{c}\" )")
      for c in ("EUR", "CAD", "AUD")],
    ("FX", "FX Shock Value", PCT_SIGNED, "What-if: move of every non-USD currency against USD.",
     "SELECTEDVALUE ( 'FX Shock'[FX Shock], 0 )"),
    ("FX", "Foreign Currency Actual", USD0, "Actuals booked by entities whose currency is not USD.",
     "CALCULATE ( [Actual], GL[Currency] <> \"USD\" )"),
    ("FX", "Actual at FX Shock", USD0, "Actuals if every foreign currency moved by the FX Shock value.",
     "[Actual] + [FX Shock Value] * [Foreign Currency Actual]"),
    ("FX", "FX Shock Impact", USD0, "Change in actuals caused by the FX Shock what-if.", "[Actual at FX Shock] - [Actual]"),
    ("FX", "Operating Profit at FX Shock", USD0, "Operating profit under the FX Shock what-if.",
     on("Actual at FX Shock", "S4")),
    # ----------------------------------------------------------- Headcount
    ("Headcount", "Avg Headcount", "#,0", "Average month-end heads over the selected months.",
     "AVERAGEX ( VALUES ( 'Date'[Month Start] ), CALCULATE ( SUM ( Headcount[Heads] ) ) )"),
    ("Headcount", "Cost per Head", USD0, "Operating expenses per average head.",
     "DIVIDE ( - [Operating Expenses], [Avg Headcount] )"),
    ("Headcount", "Salary per Head", USD0, "Salaries and benefits per average head.",
     f"DIVIDE ( - {on('Actual', 'L05')}, [Avg Headcount] )"),
    ("Headcount", "Sales per Head", USD0, "Net sales per average head.", "DIVIDE ( [Net Sales], [Avg Headcount] )"),
    ("Headcount", "Headcount PY", "#,0", "Average headcount one year back on the Gregorian calendar.",
     "Fin.PriorYear ( [Avg Headcount], 'Gregorian' )"),
    ("Headcount", "Heads Closing", "#,0", "Month-end heads in the last month of the selection that has a count (semi-additive).",
     "CALCULATE ( SUM ( Headcount[Heads] ), LASTNONBLANK ( 'Date'[Month Start], CALCULATE ( SUM ( Headcount[Heads] ) ) ) )"),
    ("Headcount", "Heads Plan", "#,0",
     "Year-end planned heads (SYNTHETIC plan) for the latest year selected; the plan is per region, reached through a many-to-many relationship.",
     "VAR _y = MAX ( 'Date'[Year] )\nRETURN CALCULATE ( SUM ( 'Headcount Plan'[Planned Heads] ), 'Headcount Plan'[Fiscal Year] = _y )"),
    ("Headcount", "Heads vs Plan", "+#,0;-#,0;0", "Closing heads − planned heads; positive = more staff than planned.",
     "IF ( NOT ISBLANK ( [Heads Plan] ), [Heads Closing] - [Heads Plan] )"),
    # -------------------------------------------------------- Context & windows
    ("Variance", "Entities Behind Budget", "#,0", "Entities whose operating profit is below budget in the selection.",
     f"COUNTROWS ( FILTER ( VALUES ( Entity[Entity] ), {on('Budget Variance', 'S4')} < 0 ) ) + 0"),
    ("Variance", "Entities with Budget", "#,0", "Entities with a budgeted operating profit in the selection.",
     "COUNTROWS ( FILTER ( VALUES ( Entity[Entity] ), NOT ISBLANK ( [Operating Profit Budget] ) ) ) + 0"),
    ("Ledger", "Accounts Posted", "#,0", "Distinct GL accounts with an actual posting in the selection.",
     "CALCULATE ( DISTINCTCOUNT ( GL[Account Key] ), GL[Scenario] = \"Actual\" ) + 0"),
    ("Variance", "Variance Status", None, "On track / Watch (behind by under 5%) / Off track (behind by 5% or more).",
     "VAR _p = [Operating Profit Variance %]\nRETURN\n    SWITCH ( TRUE (),\n        ISBLANK ( _p ), BLANK (),\n"
     "        _p < -0.05, \"Off track\",\n        _p < 0, \"Watch\",\n        \"On track\"\n    )"),
    ("Key Lines", "Share of Group Opex", PCT1, "The department's share of group operating expenses (other filters kept).",
     "DIVIDE ( [Opex Spend], CALCULATE ( [Opex Spend], REMOVEFILTERS ( Department ) ) )"),
    ("Variance", "Share of Region Variance", PCT1,
     "The entity's share of its region's operating profit variance (ALLEXCEPT keeps only the region filter on Entity).",
     "DIVIDE ( [Operating Profit Variance], CALCULATE ( [Operating Profit Variance], ALLEXCEPT ( Entity, Entity[Region] ) ) )"),
    ("Variance", "Overrun Only", USD0, "Sum of the variance of accounts that missed; accounts ahead do not offset it.",
     "CALCULATE ( [Variance], FILTER ( VALUES ( Account[Account] ), [Variance] < 0 ) )"),
    ("Variance", "Operating Overrun Only", USD0,
     "Overrun Only on the operating profit line: the operating accounts that missed, with no offset from those ahead.",
     on("Overrun Only", "S4")),
    ("Ledger", "Actual by Posting Date", USD0,
     "Actuals by the date the journal was posted (inactive GL[Posting Date] relationship) instead of the period it belongs to.",
     "CALCULATE ( [Actual], USERELATIONSHIP ( GL[Posting Date], 'Date'[Date] ) )"),
    ("Ledger", "Cut-off Drift", USD0,
     "Actual by period − actual by posting date: amounts booked into a month but posted in the next or previous one.",
     "[Actual] - [Actual by Posting Date]"),
    ("Ledger", "Journal Amount", USD0, "Posted amount in USD from the journal lines (GL Journal).",
     "SUM ( 'GL Journal'[Amount USD] )"),
    ("Ledger", "Journal Lines", "#,0", "Number of journal postings.", "COUNTROWS ( 'GL Journal' )"),
    ("Key Lines", "Opex Rolling 12M", USD0, "Operating expenses over the 12 months ending at the last date in context.",
     "CALCULATE ( [Opex Spend], DATESINPERIOD ( 'Date'[Date], MAX ( 'Date'[Date] ), -12, MONTH ) )"),
    ("Variance", "Driver Growth", PCT_SIGNED,
     "Budget growth assumption for the P&L line and year in context; Budget Drivers has no relationships, TREATAS maps line and year.",
     "CALCULATE (\n    AVERAGE ( 'Budget Drivers'[Growth] ),\n    TREATAS ( VALUES ( 'P&L Line'[Line ID] ), 'Budget Drivers'[Line ID] ),\n"
     "    TREATAS ( VALUES ( 'Date'[Year] ), 'Budget Drivers'[Fiscal Year] )\n)"),
    ("Key Lines", "Operating Profit PM", USD0, "Operating profit of the previous month on the axis (OFFSET, no time intelligence).",
     f"IF ( {IN_MONTH}, CALCULATE ( [Operating Profit], OFFSET ( -1, {MONTH_AXIS} ) ) )"),
    ("Variance", "Variance Running Total", USD0, "Variance accumulated from the first month on the axis (WINDOW).",
     f"IF ( {IN_MONTH}, CALCULATE ( [Variance], WINDOW ( 1, ABS, 0, REL, {MONTH_AXIS} ) ) )"),
    ("Ranking", "Biggest Miss Entity", None, "The entity with the largest operating profit shortfall in the selection (INDEX).",
     "VAR _row = INDEX ( 1, FILTER ( ALLSELECTED ( Entity[Entity] ), [Operating Profit Variance] < 0 ), "
     "ORDERBY ( [Operating Profit Variance], ASC ) )\nRETURN MAXX ( _row, Entity[Entity] )"),
    ("Key Lines", "Metric Value", USD0,
     "The operating profit series picked in the Metric field parameter (by its order column).",
     "SWITCH ( SELECTEDVALUE ( Metric[Metric Order], 0 ),\n"
     + ",\n".join(f"    {i}, {ref}" for i, (_, ref) in enumerate(METRIC)) + "\n)"),
    # ------------------------------------------------------------- Analysis
    # Pareto of the operating expense overrun: accounts ranked by how far they overran, then cumulated.
    ("Analysis", "Opex Overrun", USD0,
     "How far the operating expense accounts overran the comparison, as a positive amount; accounts under it add nothing.",
     "Fin.OnLine ( SUMX ( VALUES ( Account[Account] ), VAR _v = [Variance] RETURN IF ( _v < 0, - _v ) ), \"S3\" )"),
    ("Analysis", "Overrun Cumulative %", PCT1,
     "Pareto: this account's overrun plus every larger one, as a share of the total opex overrun (accounts visible in the visual).",
     "VAR _cur = [Opex Overrun]\n"
     "VAR _tbl = ADDCOLUMNS ( ALLSELECTED ( Account[Account] ), \"@o\", [Opex Overrun] )\n"
     "VAR _all = SUMX ( _tbl, [@o] )\n"
     "RETURN IF ( _cur > 0, DIVIDE ( SUMX ( FILTER ( _tbl, [@o] >= _cur ), [@o] ), _all ) )"),
    ("Analysis", "Accounts to 80% Overrun", "#,0",
     "How many of the largest overrunning accounts it takes to reach 80% of the opex overrun.",
     "VAR _tbl = FILTER ( ADDCOLUMNS ( ALLSELECTED ( Account[Account] ), \"@o\", [Opex Overrun] ), [@o] > 0 )\n"
     "VAR _all = SUMX ( _tbl, [@o] )\n"
     "VAR _cum = ADDCOLUMNS ( _tbl, \"@before\", VAR _x = [@o] RETURN SUMX ( FILTER ( _tbl, [@o] > _x ), [@o] ) )\n"
     "RETURN COUNTROWS ( FILTER ( _cum, DIVIDE ( [@before], _all ) < 0.8 ) ) + 0"),
    ("Analysis", "Overrunning Accounts", "#,0", "Operating expense accounts that overran the comparison.",
     "COUNTROWS ( FILTER ( ALLSELECTED ( Account[Account] ), [Opex Overrun] > 0 ) ) + 0"),
    ("Analysis", "Pareto Title", None, "Dynamic title for the Pareto chart.",
     f"[Accounts to 80% Overrun] & \" of \" & [Overrunning Accounts] & \" accounts = 80% of the \" & {money('[Opex Overrun]')} & \" opex overrun\""),
    # ABC: A = the largest accounts that make up the first 70% of opex, B = the next 20%, C = the rest. Follows slicers.
    ("Analysis", "Spend Class", None,
     "ABC class of the account by opex spend within the current slicers: A = top 70% of spend, B = next 20%, C = the rest.",
     "VAR _cur = [Opex Spend]\n"
     "VAR _tbl = ADDCOLUMNS ( ALLSELECTED ( Account[Account] ), \"@s\", [Opex Spend] )\n"
     "VAR _all = SUMX ( FILTER ( _tbl, [@s] > 0 ), [@s] )\n"
     "VAR _before = SUMX ( FILTER ( _tbl, [@s] > _cur ), [@s] )\n"
     "VAR _p = DIVIDE ( _before, _all )\n"
     "RETURN IF ( _cur > 0, SWITCH ( TRUE (), _p < 0.7, \"A\", _p < 0.9, \"B\", \"C\" ) )"),
    # Dynamic segmentation: the disconnected Variance Band table holds the band limits.
    ("Analysis", "Entities in Band", "#,0",
     "Entities whose operating profit variance % falls inside the band on the row (Variance Band table, disconnected).",
     "VAR _lo = MIN ( 'Variance Band'[Lower] )\n"
     "VAR _hi = MAX ( 'Variance Band'[Upper] )\n"
     "RETURN\n"
     "    COUNTROWS ( FILTER ( VALUES ( Entity[Entity] ),\n"
     "        VAR _p = [Operating Profit Variance %] RETURN NOT ISBLANK ( _p ) && _p >= _lo && _p < _hi ) ) + 0"),
    # Outliers: P95 of |variance %| over every account × entity × month in the current slicers.
    ("Analysis", "Variance P95", "0.0%",
     "95th percentile (PERCENTILEX.INC) of |variance %| over all account × entity × month cells in the slicers / page filter.",
     "VAR _grid = CALCULATETABLE (\n"
     "    ADDCOLUMNS ( SUMMARIZE ( GL, Account[Account], Entity[Entity], 'Date'[Month Start] ), \"@p\", ABS ( [Variance %] ) ),\n"
     "    ALLSELECTED ()\n"
     ")\n"
     "RETURN PERCENTILEX.INC ( FILTER ( _grid, NOT ISBLANK ( [@p] ) ), [@p], 0.95 )"),
    ("Analysis", "Is Variance Outlier", "#,0",
     "1 when |variance %| of the cell is above [Variance P95] (account × entity × month grain).",
     "VAR _p = ABS ( [Variance %] )\n"
     "RETURN IF ( NOT ISBLANK ( _p ), IF ( _p > [Variance P95], 1, 0 ) )"),
    ("Analysis", "Outlier Cells", "#,0",
     "How many account × entity × month cells under the current row are above [Variance P95]; works at any grain.",
     "VAR _p95 = [Variance P95]\n"
     "RETURN COUNTROWS (\n"
     "    FILTER (\n"
     "        ADDCOLUMNS ( SUMMARIZE ( GL, Account[Account], Entity[Entity], 'Date'[Month Start] ), \"@p\", ABS ( [Variance %] ) ),\n"
     "        NOT ISBLANK ( [@p] ) && [@p] > _p95\n"
     "    )\n"
     ")"),
    # Forecast: straight-line trend of the last 12 months of opex up to the As Of month, projected over the rest.
    ("Analysis", "Opex Outlook", USD0, "Operating expenses in the outlook (actuals to the As Of month, budget after), positive.",
     f"- {on('Outlook', 'S3')}"),
    ("Analysis", "Opex Trend Forecast", USD0,
     "Opex actuals to the As Of month + a LINESTX straight line fitted on the 12 months up to it, for the months after.",
     "VAR _asof = SELECTEDVALUE ( 'As Of Month'[As Of Month], MAX ( 'As Of Month'[As Of Month] ) )\n"
     "VAR _hist = CALCULATETABLE (\n"
     "    ADDCOLUMNS ( VALUES ( 'Date'[Month Start] ),\n"
     "        \"@x\", YEAR ( 'Date'[Month Start] ) * 12 + MONTH ( 'Date'[Month Start] ), \"@y\", [Opex Spend] ),\n"
     "    REMOVEFILTERS ( 'Date' ), DATESINPERIOD ( 'Date'[Date], EOMONTH ( _asof, 0 ), -12, MONTH )\n"
     ")\n"
     "VAR _fit = LINESTX ( FILTER ( _hist, NOT ISBLANK ( [@y] ) ), [@y], [@x] )\n"
     "VAR _a = MAXX ( _fit, [Intercept] )\n"
     "VAR _b = MAXX ( _fit, [Slope1] )\n"
     "VAR _after = FILTER ( VALUES ( 'Date'[Month Start] ), 'Date'[Month Start] > _asof )\n"
     "VAR _actual = CALCULATE ( [Opex Spend], KEEPFILTERS ( 'Date'[Month Start] <= _asof ) )\n"
     "RETURN _actual + SUMX ( _after, _a + _b * ( YEAR ( 'Date'[Month Start] ) * 12 + MONTH ( 'Date'[Month Start] ) ) )"),
    # ----------------------------------------------------------- Report UI
    ("Report UI", "Variance Arrow", None,
     "Variance % with ▲ (favourable) or ▼ (unfavourable), so the sign never depends on colour alone.",
     "VAR _v = [Variance]\nVAR _p = [Variance %]\nRETURN\n    IF ( NOT ISBLANK ( _v ),\n"
     "        IF ( _v >= 0, \"▲ \", \"▼ \" ) & IF ( ISBLANK ( _p ), FORMAT ( _v, \"$#,0\" ), FORMAT ( _p, \"+0.0%;-0.0%;0.0%\" ) ) )"),
    ("Report UI", "Alt Text Operating Profit", None, "Alt text (screen readers) for operating profit visuals; follows the slicers.",
     "VAR _v = [Operating Profit Variance]\n"
     "VAR _cmp = LOWER ( SELECTEDVALUE ( 'Compare To'[Compare To], \"Budget\" ) )\n"
     f"RETURN \"Operating profit \" & {money('[Operating Profit]')} & \", \" & {money('ABS ( _v )')}\n"
     "    & IF ( _v >= 0, \" ahead of \", \" behind \" ) & _cmp & \".\""),
    ("Report UI", "Alt Text Biggest Miss", None, "Alt text for the entity variance visuals.",
     "\"Operating variance by entity: \" & [Entities Behind Budget] & \" of \" & [Entities with Budget]\n"
     "    & \" budgeted entities behind budget\"\n"
     "    & IF ( NOT ISBLANK ( [Biggest Miss Entity] ), \", biggest miss \" & [Biggest Miss Entity] ) & \".\""),
    ("Report UI", "Alt Text Opex", None, "Alt text for the operating expense visuals.",
     "VAR _v = [Operating Expenses Variance]\n"
     f"RETURN \"Operating expenses \" & {money('[Opex Spend]')}\n"
     f"    & IF ( _v >= 0, \", under budget by \", \", over budget by \" ) & {money('ABS ( _v )')} & \".\""),
    ("Report UI", "Outlier Fill", None,
     "Conditional-formatting background: light red when the row holds at least one P95 outlier cell ([Outlier Cells] > 0).",
     "IF ( [Outlier Cells] > 0, \"#FBE3DF\" )"),
    ("Report UI", "Line Row Fill", None, "Shades subtotal rows in the P&L matrix.",
     "IF ( SELECTEDVALUE ( 'P&L Line'[Line Kind] ) IN { \"subtotal\", \"total\" }, \"#EAF0EE\" )"),
    ("Report UI", "Period Label", None, "Header text: the selected months and the comparison.",
     "FORMAT ( MIN ( 'Date'[Date] ), \"mmm yyyy\" ) & \" – \" & FORMAT ( MAX ( 'Date'[Date] ), \"mmm yyyy\" )"
     " & \"  ·  vs \" & LOWER ( SELECTEDVALUE ( 'Compare To'[Compare To], \"Budget\" ) )"),
    ("Report UI", "As Of Label", None, "Explains the outlook cut-off month.",
     "\"Outlook = actuals to \" & FORMAT ( SELECTEDVALUE ( 'As Of Month'[As Of Month], MAX ( 'As Of Month'[As Of Month] ) ), \"mmm yyyy\" )"
     " & \", budget after\""),
    ("Report UI", "Headline Insight", None, "One-sentence dynamic insight for the overview page.",
     "VAR _v = [Operating Profit Variance]\n"
     "VAR _s = [Net Sales Variance]\n"
     "VAR _o = [Operating Expenses Variance]\n"
     "VAR _cmp = LOWER ( SELECTEDVALUE ( 'Compare To'[Compare To], \"Budget\" ) )\n"
     "RETURN\n"
     f"    \"Operating profit \" & {money('[Operating Profit]')}\n"
     f"        & \", \" & {money('ABS ( _v )')} & IF ( _v >= 0, \" ahead of \", \" behind \" ) & _cmp & \". \"\n"
     f"        & \"Net sales \" & IF ( _s >= 0, \"beat it by \", \"missed it by \" ) & {money('ABS ( _s )')}\n"
     f"        & \", operating expenses \" & IF ( _o >= 0, \"came in \" & {money('_o')} & \" under\", \"overran by \" & {money('- _o')} ) & \".\"\n"
     "        & IF ( NOT ISBLANK ( [Biggest Miss Entity] ), \" Biggest miss: \" & [Biggest Miss Entity] & \".\" )"),
]
TEXT_MEASURES = {m[1] for m in MEASURES if m[2] is None}

# Extra TMDL lines per measure: dynamic format strings, data categories, a KPI.
MEASURE_EXTRA = {
    "Operating Profit Variance": [
        "formatStringDefinition = IF ( ABS ( SELECTEDMEASURE () ) >= 1e6, \"\\$#,0.0,,\\M;(\\$#,0.0,,\\M);\\$0\", \"\\$#,0.0,\\K;(\\$#,0.0,\\K);\\$0\" )"],
    "Entity Variance Bar": ["dataCategory: ImageUrl"],
    "Account Variance Bar": ["dataCategory: ImageUrl"],
    "Operating Profit": [
        "kpi",
        "\tstatusGraphic: Three Circles Colored",
        "\ttargetExpression = [Operating Profit Comparison]",
        "\tstatusExpression =",
        "\t\t\tVAR _a = [Operating Profit]",
        "\t\t\tVAR _t = [Operating Profit Comparison]",
        "\t\t\tRETURN IF ( NOT ISBLANK ( _t ), IF ( _a >= _t, 1, IF ( _a >= _t - ABS ( _t ) * 0.05, 0, -1 ) ) )"],
}


def pretty(col):
    return " ".join(w.upper() if w in ("id", "usd", "fx") else w.capitalize() for w in col.split("_"))


def q(name):
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else f"'{name}'"


def lineage():
    return str(uuid.uuid4())


def col_name(cfg, col):
    return cfg.get("names", {}).get(col) or pretty(col)


def schema(con, mart):
    return con.execute(f"describe mart.{mart}").fetchall()


def dax_block(dax, indent=3):
    return [f"{T*indent}{l}" for l in dax.split("\n")]


def columns_of(con, mart, cfg):
    """Mart columns that are loaded (cfg drop = kept in the mart, left out of the model)."""
    return [c for c in schema(con, mart) if c[0] not in cfg.get("drop", ())]


def mdx_needed(name, cfg):
    """Columns that must stay available to MDX (Excel / Analyze in Excel): visible ones, keys,
    sort-by targets, hierarchy levels and calendar columns. Everything else gets isAvailableInMdx: false."""
    keep = {cfg.get("key")} | set(cfg.get("sort", {}).values()) | set(cfg.get("sort", {}))
    levels = {lv for _, lvs in HIERARCHIES.get(name, []) for lv in lvs}
    cal = {c for _, gs in CALENDARS for _, p, assoc in gs for c in (p, *assoc)} if cfg.get("date_table") else set()
    return keep, levels | cal


# Storage mode per variant. local = Parquet import everywhere (Desktop on any machine, CI).
#   fabric      composite on the Lakehouse SQL endpoint: GL Journal (daily postings) DirectQuery, its import
#               aggregation answers the totals, the dimensions Dual so they join both sides regularly.
#   directlake  every Lakehouse table Direct Lake; DAX-only tables stay calculated.
FABRIC_MODE = {"GL Journal": "directQuery", **{t: "dual" for t in ("Date", "Entity", "Account", "Department")}}
JOURNAL_AGG = [  # (column, mart column, dataType, summarization, base column in GL Journal)
    ("Agg Period", "period", "dateTime", "groupBy", "Period"),
    ("Agg Entity Key", "entity_key", "int64", "groupBy", "Entity Key"),
    ("Agg Department Key", "department_key", "int64", "groupBy", "Department Key"),
    ("Agg Account Key", "account_key", "int64", "groupBy", "Account Key"),
    ("Agg Amount USD", "amount_usd", "double", "sum", "Amount USD"),
    ("Agg Lines", "lines", "int64", "count", None),
]
AGG_RELATIONSHIPS = [("GL Journal Agg", "Agg Period", "Date", "date", False),
                     ("GL Journal Agg", "Agg Entity Key", "Entity", "entity_key", False),
                     ("GL Journal Agg", "Agg Department Key", "Department", "department_key", False),
                     ("GL Journal Agg", "Agg Account Key", "Account", "account_key", False)]


def mode_of(name, variant):
    if variant == "fabric":
        return FABRIC_MODE.get(name, "import")
    return "directLake" if variant == "directlake" else "import"


def table_tmdl(con, name, mart, cfg, variant="local"):
    mode = mode_of(name, variant)
    lines = [f"table {q(name)}", f"{T}lineageTag: {lineage()}"]
    if cfg.get("date_table"):
        lines.append(f"{T}dataCategory: Time")
    if cfg.get("hidden_table"):
        lines.append(f"{T}isHidden")
    lines.append("")
    keep, levels = mdx_needed(name, cfg)
    m_types = []
    for col, dtype, *_ in columns_of(con, mart, cfg):
        tm, mq = TYPES[dtype]
        m_types.append(f'{{"{col}", {mq}}}')
        cname = col_name(cfg, col)
        lines += [f"{T}column {q(cname)}", f"{T*2}dataType: {tm}"]
        if col == cfg.get("key"):
            lines.append(f"{T*2}isKey")
        # Summed fact columns are measure-backed: hidden, so users drag the measure, not an implicit SUM.
        hidden = col in cfg.get("hidden", []) or col in cfg.get("sum", []) or cfg.get("hidden_table")
        if hidden:
            lines.append(f"{T*2}isHidden")
        if hidden and col not in keep and cname not in levels:
            lines.append(f"{T*2}isAvailableInMdx: false")
        if fmt := col_fmt(cfg, col, tm):
            lines.append(f"{T*2}formatString: {fmt}")
        if cat := cfg.get("category", {}).get(col):
            lines.append(f"{T*2}dataCategory: {cat}")
        lines += [f"{T*2}lineageTag: {lineage()}",
                  f"{T*2}summarizeBy: {'sum' if col in cfg.get('sum', []) else 'none'}",
                  f"{T*2}sourceColumn: {col}"]
        if sort := cfg.get("sort", {}).get(col):
            lines.append(f"{T*2}sortByColumn: {q(col_name(cfg, sort))}")
        lines.append("")
    # Calculated columns only in import tables (Direct Lake and DirectQuery tables can't hold them).
    for cname, dtype, fmt, dax, *extra in (cfg.get("calc_cols", []) if mode == "import" else []):
        lines += [f"{T}column {q(cname)} = {dax}", f"{T*2}dataType: {dtype}"]
        lines += [f"{T*2}formatString: {fmt}"] * bool(fmt)
        lines += [f"{T*2}lineageTag: {lineage()}", f"{T*2}summarizeBy: none", ""]
        lines += [f"{T*2}{ln}" for ln in (extra[0] if extra else [])] + [""] * bool(extra)
    for h, lvs in HIERARCHIES.get(name, []):
        lines += [f"{T}hierarchy {q(h)}", f"{T*2}lineageTag: {lineage()}", ""]
        for lv in lvs:
            lines += [f"{T*2}level {q(lv)}", f"{T*3}lineageTag: {lineage()}", f"{T*3}column: {q(lv)}", ""]
    if cfg.get("date_table"):
        for cal, groups in CALENDARS:
            lines += [f"{T}calendar {q(cal)}", ""]
            for unit, primary, assoc in groups:
                lines += [f"{T*2}calendarColumnGroup = {unit}", f"{T*3}primaryColumn: {q(primary)}"]
                lines += [f"{T*3}associatedColumn: {q(a)}" for a in assoc] + [""]
    ignore = [bpa_ignore(cfg["bpa_ignore"])] if cfg.get("bpa_ignore") else []
    if mode == "directLake":
        lines += [f"{T}partition {q(name)} = entity", f"{T*2}mode: directLake", f"{T*2}source",
                  f"{T*3}entityName: {mart}", f"{T*3}schemaName: dbo", f"{T*3}expressionSource: DatabaseQuery", ""]
        return "\n".join(lines + ignore)
    refresh = cfg.get("refresh") if mode == "import" else None
    if cfg.get("m") and variant == "local":
        m = PQ_PARTITIONS[cfg["m"]].split("\n")
    else:
        m = source_m(con, mart, cfg, m_types, variant, refresh)
    if refresh:
        (rg, rp), (ig, ip) = refresh["rolling"], refresh["incremental"]
        lines += [f"{T}refreshPolicy", f"{T*2}policyType: basic",
                  f"{T*2}rollingWindowGranularity: {rg}", f"{T*2}rollingWindowPeriods: {rp}",
                  f"{T*2}incrementalGranularity: {ig}", f"{T*2}incrementalPeriods: {ip}",
                  f"{T*2}pollingExpression =", *[f"{T*4}{l}" for l in polling_m(mart, cfg, refresh, variant)],
                  f"{T*2}sourceExpression =", *[f"{T*4}{l}" for l in m], ""]
    lines += [f"{T}partition {q(name)} = m", f"{T*2}mode: {mode}", f"{T*2}source ="]
    lines += [f"{T*4}{l}" for l in m]
    lines += ["", f"{T}annotation PBI_ResultType = Table", ""] + ignore
    return "\n".join(lines)


def bpa_ignore(rules, indent=1):
    """Tabular Editor's 'Ignore item' annotation; the pbi-kit BPA runner honours it too."""
    return f"{T*indent}annotation BestPracticeAnalyzer_IgnoreRules = " + json.dumps({"RuleIDs": rules}, separators=(",", ":")) + "\n"


def col_fmt(cfg, col, tm):
    fmt = cfg.get("fmt", {}).get(col) or {"int64": "0", "dateTime": "dd mmm yyyy"}.get(tm)
    if col in cfg.get("sum", []) and tm == "double" and col not in cfg.get("fmt", {}):
        fmt = USD0
    return fmt


def source_steps(mart, variant):
    """local: Parquet, or the Lakehouse SQL endpoint when SourceMode = "Lakehouse". fabric: the endpoint only."""
    if variant == "fabric":
        return ["    Source = Sql.Database(SqlEndpoint, LakehouseDb),",
                f'    Data = Source{{[Schema = "dbo", Item = "{mart}"]}}[Data],']
    return ['    Data = if SourceMode = "Lakehouse"',
            f'        then Sql.Database(SqlEndpoint, LakehouseDb){{[Schema = "dbo", Item = "{mart}"]}}[Data]',
            f'        else Parquet.Document(File.Contents(DataFolder & "{mart}.parquet")),']


def in_range(prev, c):
    """The incremental refresh filter: Power BI substitutes RangeStart/RangeEnd per partition.
    >= start and < end, so a row never lands in two partitions. Folds to a WHERE on the endpoint."""
    return f"    InRange = Table.SelectRows({prev}, each DateTime.From([{c}]) >= RangeStart and DateTime.From([{c}]) < RangeEnd),"


def source_m(con, mart, cfg, m_types, variant="local", refresh=None):
    """Row filter, column choice and types come right after the source so they fold to SQL on the endpoint."""
    names = ", ".join(f'"{c[0]}"' for c in columns_of(con, mart, cfg))
    m = ["let", *source_steps(mart, variant)]
    prev = "Data"
    if flt := cfg.get("filter"):
        m.append(f'    #"Keep reported periods" = Table.SelectRows(Data, {flt}),')
        prev = '#"Keep reported periods"'
    if refresh:
        m.append(in_range(prev, refresh["column"]))
        prev = "InRange"
    return m + [f'    #"Choose columns" = Table.SelectColumns({prev}, {{{names}}}),',
                f"    Typed = Table.TransformColumnTypes(#\"Choose columns\", {{{', '.join(m_types)}}})",
                "in", "    Typed"]


def polling_m(mart, cfg, refresh, variant):
    """Detect data changes: a partition is reloaded only when its newest posting date moved."""
    return ["let", *source_steps(mart, variant), in_range("Data", refresh["column"]),
            f"    LastChange = List.Max(InRange[{refresh['polling']}])", "in", "    LastChange"]


def journal_agg_tmdl():
    """Import aggregation over the DirectQuery journal; the engine answers matching queries from it (alternateOf)."""
    lines = ["table 'GL Journal Agg'", f"{T}isHidden", f"{T}lineageTag: {lineage()}", ""]
    for col, src, dtype, summ, base in JOURNAL_AGG:
        lines += [f"{T}column {q(col)}", f"{T*2}dataType: {dtype}", f"{T*2}isHidden", f"{T*2}isAvailableInMdx: false",
                  f"{T*2}formatString: {'dd mmm yyyy' if dtype == 'dateTime' else USD0 if dtype == 'double' else '0'}",
                  f"{T*2}lineageTag: {lineage()}", f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: {src}", "",
                  f"{T*2}alternateOf", f"{T*3}summarization: {summ}"]
        lines += [f"{T*3}baseColumn: 'GL Journal'.{q(base)}"] if base else [f"{T*3}baseTable: 'GL Journal'"]
        lines.append("")
    keys = [src for _, src, _, s, _ in JOURNAL_AGG if s == "groupBy"]
    m = ["let", *source_steps("gl_journal", "fabric"),
         f"    Grouped = Table.Group(Data, {{{', '.join(chr(34) + k + chr(34) for k in keys)}}}, {{",
         '        {"amount_usd", each List.Sum([amount_usd]), type number},',
         '        {"lines", each Table.RowCount(_), Int64.Type}}),',
         '    Typed = Table.TransformColumnTypes(Grouped, {{"period", type date}, {"entity_key", Int64.Type}, '
         '{"department_key", Int64.Type}, {"account_key", Int64.Type}})', "in", "    Typed"]
    lines += [f"{T}partition 'GL Journal Agg' = m", f"{T*2}mode: import", f"{T*2}source =", *[f"{T*4}{l}" for l in m],
              "", f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def relationship_kind(ft, tt, variant, kind):
    """Regular when both ends sit in one source group (Dual counts as both); many-to-many is always limited."""
    if kind == "m2m":
        return "limited"
    if variant != "fabric":
        return "regular"
    def groups(t):
        mode = mode_of(t, variant)
        return {"import", "directQuery", "dual"} if mode == "dual" else {mode}
    return "regular" if "dual" in groups(tt) or groups(ft) == groups(tt) else "limited"


def expressions_tmdl(params, staging=True):
    lines = []
    for name, typ, value in params:
        lines += [f'expression {q(name)} = {value} meta [IsParameterQuery=true, Type="{typ}", IsParameterQueryRequired=true]',
                  f"{T}lineageTag: {lineage()}", "", f"{T}annotation PBI_ResultType = {typ}", ""]
    for name, result, m in STAGING * staging:
        lines += [f"/// Not loaded (staging): referenced by other queries.", f"expression {q(name)} =",
                  *[f"{T*2}{l}" for l in m.split("\n")],
                  f"{T}lineageTag: {lineage()}", "", f"{T}annotation PBI_ResultType = {result}", ""]
    return "\n".join(lines)


def calculated_tmdl(name, dax, cols):
    lines = [f"table {q(name)}", f"{T}lineageTag: {lineage()}", ""]
    for col, src, dtype, extra in cols:
        lines += [f"{T}column {q(col)}", f"{T*2}dataType: {dtype}", f"{T*2}lineageTag: {lineage()}",
                  f"{T*2}summarizeBy: none", f"{T*2}isNameInferred", f"{T*2}sourceColumn: {src}"]
        lines += [f"{T*2}{e}" for e in extra] + [""]
    lines += [f"{T}partition {q(name)} = calculated", f"{T*2}mode: import"]
    lines += [f"{T*2}source = {dax}"] if "\n" not in dax else [f"{T*2}source =", *dax_block(dax, 4)]
    return "\n".join(lines + [""])


def field_parameter_tmdl(name="Analyze By", items=ANALYZE_BY):
    rows = ",\n".join(f'    ( "{label}", NAMEOF ( {ref} ), {i} )' for i, (label, ref) in enumerate(items))
    fields, order = q(f"{name} Fields"), q(f"{name} Order")
    return "\n".join([
        f"table {q(name)}", f"{T}lineageTag: {lineage()}", "",
        f"{T}column {q(name)}", f"{T*2}dataType: string", f"{T*2}lineageTag: {lineage()}",
        f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: [Value1]", f"{T*2}sortByColumn: {order}", "",
        f"{T*2}relatedColumnDetails", f"{T*3}groupByColumn: {fields}", "",
        f"{T}column {fields}", f"{T*2}dataType: string", f"{T*2}isHidden", f"{T*2}lineageTag: {lineage()}",
        f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: [Value2]", f"{T*2}sortByColumn: {order}", "",
        f"{T*2}extendedProperty ParameterMetadata =", f"{T*4}{{", f'{T*4}  "version": 3,', f'{T*4}  "kind": 2',
        f"{T*4}}}", "",
        f"{T}column {order}", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
        f"{T*2}lineageTag: {lineage()}", f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: [Value3]", "",
        f"{T}partition {q(name)} = calculated", f"{T*2}mode: import", f"{T*2}source =",
        *dax_block("{\n" + rows + "\n}", 4), ""])


def calc_group_tmdl():
    lines = ["table 'Time View'", f"{T}lineageTag: {lineage()}", "", f"{T}calculationGroup", f"{T*2}precedence: 10", ""]
    for i, (name, dax, fmt) in enumerate(TIME_VIEW):
        if "\n" in dax:
            lines += [f"{T*2}calculationItem {q(name)} =", *dax_block(dax, 4)]
        else:
            lines.append(f"{T*2}calculationItem {q(name)} = {dax}")
        lines.append(f"{T*3}ordinal: {i}")
        if fmt:
            lines.append(f"{T*3}formatStringDefinition = {fmt}")
        lines.append("")
    lines += [f"{T}column 'Time View'", f"{T*2}dataType: string", f"{T*2}lineageTag: {lineage()}",
              f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: Name", f"{T*2}sortByColumn: Ordinal", "",
              f"{T}column Ordinal", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
              f"{T*2}lineageTag: {lineage()}", f"{T*2}summarizeBy: sum", f"{T*2}sourceColumn: Ordinal", ""]
    return "\n".join(lines)


def measures_tmdl():
    lines = ["table _Measures", f"{T}lineageTag: {lineage()}", ""]
    for folder, name, fmt, desc, dax in MEASURES:
        lines.append(f"{T}/// {desc}")
        if "\n" in dax:
            lines += [f"{T}measure {q(name)} =", *dax_block(dax)]
        else:
            lines.append(f"{T}measure {q(name)} = {dax}")
        if fmt:
            lines.append(f"{T*2}formatString: {fmt}")
        extra = MEASURE_EXTRA.get(name, [])
        child = bool(extra) and not extra[0].startswith("dataCategory")  # child objects (kpi, formatStringDefinition) go last
        lines += [f"{T*2}{l}" for l in extra] * (not child)
        lines += [f"{T*2}displayFolder: {folder}", f"{T*2}lineageTag: {lineage()}"]
        lines += [f"{T*2}{l}" for l in extra] * child + [""]
        # Text measures (labels, colours, SVG, alt text) return strings: a number format would be meaningless.
        lines += [bpa_ignore(["PROVIDE_FORMAT_STRING_FOR_MEASURES", "INTEGER_FORMATTING"], 2)] * (name in TEXT_MEASURES)
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


def culture_en_tmdl():
    """en-US linguistic schema: synonyms for Q&A and Copilot."""
    entities = {}
    measure_names = {m[1] for m in MEASURES}
    for obj, terms in SYNONYMS.items():
        binding = {"ConceptualEntity": "_Measures", "ConceptualProperty": obj} if obj in measure_names \
            else {"ConceptualEntity": obj}
        key = re.sub(r"\W", "_", obj.lower())
        entities[key] = {"Definition": {"Binding": binding}, "State": "Authored",
                         "Terms": [{obj.lower(): {"State": "Authored"}}] + [{t: {"State": "Authored"}} for t in terms]}
    doc = {"Version": "1.0.0", "Language": "en-US", "DynamicImprovement": "HighConfidence", "Entities": entities}
    return "\n".join(["cultureInfo en-US", "", f"{T}linguisticMetadata = {json.dumps(doc, ensure_ascii=False)}",
                      f"{T*2}contentType: json", ""])


def culture_id_tmdl():
    """id-ID: translated table captions, measure captions and display folders."""
    lines = ["cultureInfo id-ID", "", f"{T}translations", "", f"{T*2}model Model", ""]
    by_table = {t: [] for t in ID_TABLES}
    for folder, name, *_ in MEASURES:
        by_table["_Measures"].append((name, ID_MEASURES.get(name), ID_FOLDERS[folder]))
    for t, caption in ID_TABLES.items():
        lines += [f"{T*3}table {q(t)}", f"{T*4}caption: {caption}", ""]
        for name, cap, folder in by_table[t]:
            lines.append(f"{T*4}measure {q(name)}")
            lines += [f"{T*5}caption: {cap}"] * bool(cap) + [f"{T*5}displayFolder: {folder}", ""]
    return "\n".join(lines)


def check(con):
    """Fail fast on name collisions, DAX references that don't resolve, and best-practice rule breaks."""
    for t, (mart, cfg) in TABLES.items():
        names = [col_name(cfg, c[0]).lower() for c in columns_of(con, mart, cfg)] + [c[0].lower() for c in cfg.get("calc_cols", [])]
        assert len(names) == len(set(names)), f"duplicate column names in {t}: {names}"
        assert set(cfg.get("drop", ())) <= {c[0] for c in schema(con, mart)}, f"{t} drops a column the mart lacks"
    columns = {t: {col_name(cfg, c[0]) for c in columns_of(con, mart, cfg)} | {c[0] for c in cfg.get("calc_cols", [])}
               for t, (mart, cfg) in TABLES.items()}
    columns |= {t: {c[0] for c in cols} for t, (_, cols) in CALCULATED.items()}
    columns |= {fp: {fp, f"{fp} Fields", f"{fp} Order"} for fp in ("Analyze By", "Metric")} | {"Time View": {"Time View"}}
    measures = [m[1] for m in MEASURES]
    taken = {n.lower() for n in set().union(*columns.values(), columns)}
    clash = {m for m in measures if m.lower() in taken or measures.count(m) > 1}
    assert not clash, f"measure names collide with tables/columns: {clash}"
    tables = set(columns) | {c for c, _ in CALENDARS}
    fns = {f[0] for f in FUNCTIONS}
    dax_all = [(m[1], m[4]) for m in MEASURES] \
        + [(f"role {r}", d) for r, v in ROLES.items() for d in v["filters"].values()] \
        + [(f"calc {n}", d) for n, d, _ in TIME_VIEW] + [(f"table {n}", d) for n, (d, _) in CALCULATED.items()] \
        + [(f"function {n}", b) for n, _, b in FUNCTIONS] + [("Analyze By", r) for _, r in ANALYZE_BY] + [("Metric", r) for _, r in METRIC] \
        + [(f"column {t}[{c[0]}]", c[3]) for t, (_, cfg) in TABLES.items() for c in cfg.get("calc_cols", [])] \
        + [(f"extra {n}", "\n".join(x)) for n, x in MEASURE_EXTRA.items()]
    for name, dax in dax_all:
        dax = re.sub(r'"[^"]*"', '""', dax)
        for a, b, col in re.findall(r"(?:'([^']+)'|\b([A-Za-z_][\w]*))\[([^\]]+)\]", dax):
            assert col in columns.get(a or b, ()), f"[{name}] references missing column {a or b}[{col}]"
        for ref in re.findall(r"(?<![\w'\]])\[([^\]]+)\]", dax):
            assert ref in measures or ref in ("Value", "Intercept", "Slope1") or ref.startswith("@"), f"[{name}] references missing measure [{ref}]"
        for ref in re.findall(r"'([^'\n]+)'", re.sub(r"'[^'\n]+'\[[^\]]*\]", "", dax)):
            assert ref in tables, f"[{name}] references missing table/calendar '{ref}'"
        for fn in re.findall(r"\b([A-Z][A-Za-z]*\.[A-Za-z]+)\s*\(", dax):
            assert fn in fns or fn.isupper(), f"[{name}] calls unknown function {fn}"
        assert dax.count("(") == dax.count(")"), f"[{name}] unbalanced parentheses"
    for _, groups in CALENDARS:
        for _, p, assoc in groups:
            assert {p, *assoc} <= columns["Date"], f"calendar column missing: {p} {assoc}"
    for r in ROLES.values():
        assert set(r["hide_tables"]) <= set(columns), r
        for t, cs in r["hide_columns"].items():
            assert set(cs) <= columns[t], (t, cs)
    for p, ts in PERSPECTIVES.items():
        assert set(ts) <= set(columns) | {"_Measures"}, f"perspective {p}: {set(ts) - set(columns)}"
    assert set(ID_MEASURES) <= set(measures) and set(ID_TABLES) <= set(columns) | {"_Measures"}
    # best-practice rules (a subset of Tabular Editor's BPA that applies to a generated model)
    for folder, name, fmt, desc, _ in MEASURES:
        assert desc and desc.endswith("."), f"[{name}] needs a one-sentence description"
        assert fmt or name in TEXT_MEASURES, f"[{name}] numeric measure without a format string"
    for ft, fc, tt, tc, _, *kind in RELATIONSHIPS:
        fcfg, tcfg = TABLES[ft][1], TABLES[tt][1]
        assert kind == ["m2m"] or tcfg.get("key") == tc, f"relationship {ft}[{fc}] -> {tt}[{tc}] must point at a key column"
        assert fc in fcfg.get("hidden", []) or fcfg.get("hidden_table"), f"foreign key {ft}[{fc}] should be hidden"
        assert kind in ([], ["m2m"], ["inactive"]), kind
    for t, (mart, cfg) in TABLES.items():
        for col, dtype, *_ in columns_of(con, mart, cfg):  # every numeric column carries a format string
            assert dtype in ("VARCHAR", "BOOLEAN") or col_fmt(cfg, col, TYPES[dtype][0]), f"{t}[{col}] has no format string"
        for h, lvs in HIERARCHIES.get(t, []):
            assert set(lvs) <= columns[t], f"hierarchy {t}.{h}: missing {set(lvs) - columns[t]}"
        if r := cfg.get("refresh"):
            assert {r["column"], r["polling"]} <= {c[0] for c in columns_of(con, mart, cfg)}, f"{t} refresh columns"
    for t, (_, cfg) in TABLES.items():
        for c, s in cfg.get("sort", {}).items():
            assert col_name(cfg, s) in columns[t], f"{t}[{c}] sorts by a missing column"
    for t, (mart, cfg) in TABLES.items():
        if cfg.get("m"):
            missing = [c[0] for c in schema(con, mart) if f'"{c[0]}"' not in PQ_PARTITIONS[cfg["m"]]]
            assert not missing, f"Power Query for {t} never produces {missing}"
    print(f"  ✓ {len(MEASURES)} measures, {len(FUNCTIONS)} functions: every reference resolves; best-practice rules pass")


VARIANTS = {  # out folder, display name, logicalId
    "local": (OUT, "GroupFinance", "7d2b9e14-3a6c-4f80-b5e1-0c9a4d2f6b37"),
    "fabric": (FABRIC_OUT, "GroupFinance (Fabric)", "4e8c1a73-9b2d-4f65-a0e7-3c5d8b1f2a96"),
    "directlake": (DL_OUT, "GroupFinance DL", "b61f3d28-7c4e-4a19-8e52-9d0a6c3e7f14"),
}


def write_model(con, variant, data_dir=None, raw_dir=None, forecast_dir=None):
    out, display, logical = VARIANTS[variant]
    shutil.rmtree(out / "definition", ignore_errors=True)
    d = out / "definition"
    (d / "tables").mkdir(parents=True)
    for sub in ("roles", "perspectives", "cultures"):
        (d / sub).mkdir()
    (out / "definition.pbism").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",\n'
        '  "version": "4.2",\n  "settings": {}\n}\n')
    (out / ".platform").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",\n'
        f'  "metadata": {{"type": "SemanticModel", "displayName": "{display}"}},\n'
        f'  "config": {{"version": "2.0", "logicalId": "{logical}"}}\n}}\n')
    (d / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1702\n")
    agg = ["GL Journal Agg"] if variant == "fabric" else []
    all_tables = [*TABLES, *agg, *CALCULATED, "Analyze By", "Metric", "Time View", "_Measures"]
    (d / "model.tmdl").write_text("\n".join([
        "model Model", f"{T}culture: en-US", f"{T}defaultPowerBIDataSourceVersion: powerBI_V3",
        f"{T}discourageImplicitMeasures", f"{T}sourceQueryCulture: en-US", f"{T}dataAccessOptions",
        f"{T*2}legacyRedirects", f"{T*2}returnErrorValuesAsNull", "",
        "annotation __PBI_TimeIntelligenceEnabled = 0", "",
        *[f"ref table {q(t)}" for t in all_tables], "",
        *[f"ref role {q(r)}" for r in ROLES], "",
        *[f"ref perspective {q(p)}" for p in PERSPECTIVES], "",
        "ref cultureInfo en-US", "ref cultureInfo id-ID", ""]))
    (d / "functions.tmdl").write_text(functions_tmdl())
    for name, tables in PERSPECTIVES.items():
        (d / "perspectives" / f"{name}.tmdl").write_text(perspective_tmdl(name, tables))
    (d / "cultures" / "en-US.tmdl").write_text(culture_en_tmdl())
    (d / "cultures" / "id-ID.tmdl").write_text(culture_id_tmdl())
    if variant == "local":
        params = parameters(data_dir, raw_dir, forecast_dir) + RANGE
        (d / "expressions.tmdl").write_text(expressions_tmdl(params))
    elif variant == "fabric":
        (d / "expressions.tmdl").write_text(expressions_tmdl(parameters("", "", "")[4:] + RANGE, staging=False))
    else:  # Direct Lake: one shared expression the entity partitions point at
        (d / "expressions.tmdl").write_text("\n".join([
            "expression DatabaseQuery =", f"{T*2}let",
            f'{T*3}database = Sql.Database("<workspace>.datawarehouse.fabric.microsoft.com", "finance_lh")',
            f"{T*2}in", f"{T*3}database", f"{T}lineageTag: {lineage()}", "", f"{T}annotation PBI_IncludeFutureArtifacts = False", ""]))

    for name, (mart, cfg) in TABLES.items():
        (d / "tables" / f"{name}.tmdl").write_text(table_tmdl(con, name, mart, cfg, variant))
    if agg:
        (d / "tables" / "GL Journal Agg.tmdl").write_text(journal_agg_tmdl())
    for name, (dax, cols) in CALCULATED.items():
        if variant == "directlake" and name == "As Of Month":
            dax = AS_OF_DL
        (d / "tables" / f"{name}.tmdl").write_text(calculated_tmdl(name, dax, cols))
    (d / "tables" / "Analyze By.tmdl").write_text(field_parameter_tmdl())
    (d / "tables" / "Metric.tmdl").write_text(field_parameter_tmdl("Metric", METRIC))
    (d / "tables" / "Time View.tmdl").write_text(calc_group_tmdl())
    (d / "tables" / "_Measures.tmdl").write_text(measures_tmdl())
    for name, r in ROLES.items():
        (d / "roles" / f"{name}.tmdl").write_text(role_tmdl(name, r))
    if variant == "local":
        (out / "DAXQueries").mkdir(exist_ok=True)
        for f in sorted([*DAX_DIR.glob("*.dax"), *DAX_DIR.glob("*.assert.json")]):
            shutil.copy(f, out / "DAXQueries" / f.name)

    rel, kinds = [], []
    for ft, fc, tt, tc, both, *kind in RELATIONSHIPS + (AGG_RELATIONSHIPS if agg else []):
        kind = kind[0] if kind else None
        rel += [f"relationship {uuid.uuid5(uuid.NAMESPACE_URL, f'rel:{ft}.{fc}->{tt}.{tc}')}"]
        if kind == "inactive":
            rel.append(f"{T}isActive: false")
        if kind == "m2m":
            rel += [f"{T}fromCardinality: many", f"{T}toCardinality: many"]
        if both:
            rel.append(f"{T}crossFilteringBehavior: bothDirections")
        fname = fc if ft == "GL Journal Agg" else col_name(TABLES[ft][1], fc)
        rel += [f"{T}fromColumn: {q(ft)}.{q(fname)}", f"{T}toColumn: {q(tt)}.{q(col_name(TABLES[tt][1], tc))}", ""]
        kinds.append((f"{ft} -> {tt}", relationship_kind(ft, tt, variant, kind)))
    (d / "relationships.tmdl").write_text("\n".join(rel))
    stable_lineage(d)
    modes = {}
    for t in [*TABLES, *agg]:
        modes.setdefault("import" if t in agg else mode_of(t, variant), []).append(t)
    limited = [k for k, v in kinds if v == "limited"]
    print(f"  ✓ wrote {out.relative_to(ROOT)} ({len(all_tables)} tables, {len(kinds)} relationships: "
          f"{len(kinds) - len(limited)} regular, {len(limited)} limited [{'; '.join(limited)}])")
    print("    storage: " + "; ".join(f"{m} {len(ts)}" + (f" ({', '.join(ts)})" if len(ts) < 6 else "") for m, ts in modes.items()))


def stable_lineage(d):
    """Replace the random lineageTags with uuid5(file + owning object), so a rebuild only diffs what changed."""
    for f in d.rglob("*.tmdl"):
        stack, out = [], []
        for ln in f.read_text().split("\n"):
            body = ln.lstrip("\t")
            ind = len(ln) - len(body)
            if body.startswith("lineageTag:"):
                owner = "|".join(h for i, h in stack if i < ind)
                ln = f"{ln[:ind]}lineageTag: {uuid.uuid5(uuid.NAMESPACE_URL, f'{f.relative_to(d)}|{owner}')}"
            elif body and not body.startswith("///"):
                stack = [(i, h) for i, h in stack if i < ind] + [(ind, re.split(r" =( |$)", body)[0])]
            out.append(ln)
        f.write_text("\n".join(out))


RANGE = [("RangeStart", "DateTime", "#datetime(2011, 1, 1, 0, 0, 0)"), ("RangeEnd", "DateTime", "#datetime(2014, 1, 1, 0, 0, 0)")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data" / "marts"))
    ap.add_argument("--raw-dir", default=str(ROOT / "data" / "raw"))
    ap.add_argument("--forecast-dir", default=str(ROOT / "seeds" / "forecast"))
    args = ap.parse_args()

    def folder(p):
        sep = "\\" if "\\" in p else "/"
        return p.rstrip("/\\") + sep

    con = duckdb.connect(str(ROOT / "data" / "warehouse" / "finance.duckdb"), read_only=True)
    check(con)
    write_model(con, "local", folder(args.data_dir), folder(args.raw_dir), folder(args.forecast_dir))
    write_model(con, "fabric")
    write_model(con, "directlake")
    print(f"    {len(FUNCTIONS)} functions, {len(CALENDARS)} calendar, {len(ROLES)} roles, {len(PERSPECTIVES)} perspectives, "
          f"2 cultures, {sum(len(v) for v in HIERARCHIES.values())} hierarchies")


if __name__ == "__main__":
    main()
