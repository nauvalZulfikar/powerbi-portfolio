"""Generate the Power BI semantic model (TMDL) from the DuckDB mart schema.

    uv run python src/build_model.py [--data-dir PATH]

Column types are read from DuckDB, so the model never drifts from the SQL.
--data-dir is the folder Power BI reads the Parquet marts from (default: this
repo's data/marts); change it later in Power BI via Transform data > Edit parameters > DataFolder.

Beyond the imported star schema the model carries:
  * what-if tables (Service Level, Review Days) that re-run the restock maths per line in DAX
  * a field parameter (Analyze By: Store / City / Vendor / Category / ABC class)
  * row-level security (dynamic Store Manager role + static Hornsey District role) and
    object-level security (Store Staff can't see invoices or supplier payment terms)
  * DAX user-defined functions (functions.tmdl): the safety-stock formula, rates, shares, prior month
  * a Gregorian calendar on Date and a Time Intelligence calculation group on top of it
    (prior month, MoM %, MTD, QTD, YTD, prior year, YoY %), with dynamic format strings on the % items
  * dynamic format strings on money measures ($940K / $3.52M)
  * a many-to-many item ↔ vendor bridge (Item Supplier), crossed with CROSSFILTER in DAX
  * a calculated column (Pack Size) and a Product hierarchy: Category → Pack Size → Item
  * perspectives, a description on every measure, synonyms for Q&A / Copilot, best-practice checks
"""
import argparse
import json
import re
import shutil
import uuid
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "powerbi" / "StockRestock.SemanticModel"
T = "\t"
NS = uuid.UUID("3c8f1a52-9d47-4e0b-a6f3-71e2b8d05c94")

TYPES = {  # duckdb -> (TMDL dataType, Power Query type)
    "BIGINT": ("int64", "Int64.Type"), "INTEGER": ("int64", "Int64.Type"),
    "DOUBLE": ("double", "type number"), "DATE": ("dateTime", "type date"),
    "BOOLEAN": ("boolean", "type logical"), "VARCHAR": ("string", "type text"),
    "DECIMAL": ("decimal", "Currency.Type"),  # PostgreSQL numeric(12,2) in the composite variant
}
USD0 = "\\$#,0;(\\$#,0);\\$0"
USD2 = "\\$#,0.00"
PCT0, PCT1, INT, DEC1 = "0%", "#,0.0%;-#,0.0%;#,0.0%", "#,0", "#,0.0"  # PCT1 = the BPA percentage format
SERVICE_DEFAULT, REVIEW_DEFAULT, OVERSTOCK_DAYS = 0.95, 7, 180  # same defaults as sql/20_marts.sql

# ---------------------------------------------------------------- Power Query (M) on the raw CSV exports
# Next to the Parquet marts, four queries read the raw CSVs through the RawFolder parameter. They are written the
# way the Advanced Editor shows them: one named step per change, with a comment on why. sql/30_power_query.sql
# repeats the same steps in DuckDB (Power Query can't run on a Mac): the row counts in the tests and the column
# types below come from there.
CSV = '[Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]'

# Staging query (a shared expression, so it is never loaded): referenced by Price List 2017 and Vendor Catalog.
PRICE_LIST_RAW = f"""let
    // Dec 2017 vendor price list, straight from the CSV export
    Source = Csv.Document(File.Contents(RawFolder & "2017PurchasePricesDec.csv"), {CSV}),
    // the first row holds the column names
    #"Promoted headers" = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    // a line without a brand number is not an item (guards against blank lines at the end of an export)
    #"Removed blank brands" = Table.SelectRows(#"Promoted headers", each [Brand] <> null and Text.Trim([Brand]) <> "")
in
    #"Removed blank brands\""""

# Staging query, also not loaded: merged into Item (List Price 2017, Pack Units).
PRICE_LIST_2017 = """let
    Source = #"Price List Raw",
    // whole-number keys: a brand or vendor that is not a number can't join to anything
    #"Changed key types" = Table.TransformColumnTypes(Source, {{"Brand", Int64.Type}, {"VendorNumber", Int64.Type}}, "en-US"),
    #"Removed key errors" = Table.RemoveRowsWithErrors(#"Changed key types", {"Brand", "VendorNumber"}),
    // prices: a malformed cell becomes null instead of failing the refresh
    #"Parsed prices" = Table.TransformColumns(#"Removed key errors", {
        {"Price", each try Number.FromText(_, "en-US") otherwise null, type nullable number},
        {"PurchasePrice", each try Number.FromText(_, "en-US") otherwise null, type nullable number}}),
    // one item (brand 4202) has no size, description or price: drop it
    #"Removed blank sizes" = Table.SelectRows(#"Parsed prices", each [Size] <> null and [Size] <> ""),
    // tidy the size labels before splitting: "1500ml" -> "1500mL", "750mL  3" -> "750mL 3"
    #"Fixed ml" = Table.ReplaceValue(#"Removed blank sizes", "ml", "mL", Replacer.ReplaceText, {"Size"}),
    #"Fixed double spaces" = Table.ReplaceValue(#"Fixed ml", "  ", " ", Replacer.ReplaceText, {"Size"}),
    // "750mL 4 Pk" -> "750mL" | "4"; anything after the pack count is dropped
    #"Split size" = Table.SplitColumn(#"Fixed double spaces", "Size",
        Splitter.SplitTextByDelimiter(" ", QuoteStyle.None), {"Size Text", "Pack Text"}),
    // four items have size "Unknown"
    #"Unknown size to null" = Table.ReplaceValue(#"Split size", "Unknown", null, Replacer.ReplaceValue, {"Size Text"}),
    #"Added Size mL" = Table.AddColumn(#"Unknown size to null", "Size mL",
        each try Number.FromText(Text.BeforeDelimiter([Size Text], "mL"), "en-US") otherwise null, type nullable number),
    // bottles per sellable unit: 1 unless the size names a pack ("+ 3/" gift packs count as 1)
    #"Added Pack Units" = Table.AddColumn(#"Added Size mL", "Pack Units",
        each if [Pack Text] = null then 1 else try Int64.From(Number.FromText([Pack Text])) otherwise 1, Int64.Type),
    // vendor names are padded and upper case in the export
    #"Cleaned text" = Table.TransformColumns(#"Added Pack Units", {
        {"VendorName", each Text.Proper(Text.Trim(_)), type text}, {"Description", Text.Trim, type text}}),
    // one row per brand, so the merge into Item can never duplicate an item
    #"Removed duplicate brands" = Table.Distinct(#"Cleaned text", {"Brand"}),
    #"Removed helper columns" = Table.RemoveColumns(#"Removed duplicate brands", {"Size Text", "Pack Text", "Volume", "Classification"}),
    #"Renamed columns" = Table.RenameColumns(#"Removed helper columns", {
        {"Brand", "Brand No"}, {"Description", "List Description"}, {"Price", "List Price"},
        {"PurchasePrice", "Purchase Price 2017"}, {"VendorNumber", "List Vendor No"}, {"VendorName", "List Vendor"}}),
    #"Reordered columns" = Table.ReorderColumns(#"Renamed columns", {
        "Brand No", "List Description", "Size mL", "Pack Units", "List Price", "Purchase Price 2017", "List Vendor No", "List Vendor"})
in
    #"Reordered columns\""""

STOCK_COUNTS = f"""let
    // both count files share one layout; only the name of the date column differs
    ReadCount = (file as text, dateColumn as text) as table =>
        let
            Source = Csv.Document(File.Contents(RawFolder & file), {CSV}),
            #"Promoted headers" = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
            #"Renamed date" = Table.RenameColumns(#"Promoted headers", {{{{dateColumn, "Count Date"}}}})
        in
            #"Renamed date",
    #"Opening count" = ReadCount("BegInvFINAL12312016.csv", "startDate"),
    #"Closing count" = ReadCount("EndInvFINAL12312016.csv", "endDate"),
    // Append: the 1 Jan and 31 Dec counts stacked in one table (the base of the semi-additive measures)
    #"Appended counts" = Table.Combine({{#"Opening count", #"Closing count"}}),
    // keep only what the model uses
    #"Selected columns" = Table.SelectColumns(#"Appended counts", {{"InventoryId", "Store", "City", "Brand", "onHand", "Price", "Count Date"}}),
    #"Changed types" = Table.TransformColumnTypes(#"Selected columns", {{
        {{"Store", Int64.Type}}, {{"Brand", Int64.Type}}, {{"onHand", Int64.Type}}, {{"Price", type number}}, {{"Count Date", type date}}}}, "en-US"),
    // a line without a count is not a count
    #"Kept counted lines" = Table.SelectRows(#"Changed types", each [onHand] <> null),
    // store 46 closed in 2016: its 1,284 rows of 31 Dec have an empty city, its 1 Jan rows don't
    #"Blank city to null" = Table.ReplaceValue(#"Kept counted lines", "", null, Replacer.ReplaceValue, {{"City"}}),
    #"Sorted store, date" = Table.Sort(#"Blank city to null", {{{{"Store", Order.Ascending}}, {{"Count Date", Order.Ascending}}}}),
    #"Filled city" = Table.FillDown(#"Sorted store, date", {{"City"}}),
    #"Proper city" = Table.TransformColumns(#"Filled city", {{{{"City", each Text.Proper(Text.Trim(_)), type text}}}}),
    // the same line counted twice on one day would double the stock
    #"Removed duplicates" = Table.Distinct(#"Proper city"),
    #"Renamed columns" = Table.RenameColumns(#"Removed duplicates", {{
        {{"InventoryId", "Count Inventory ID"}}, {{"Store", "Count Store"}}, {{"City", "Count City"}}, {{"Brand", "Count Brand"}},
        {{"onHand", "Counted Units"}}, {{"Price", "Count Price"}}}})
in
    #"Renamed columns\""""

VENDOR_CATALOG = """let
    Source = #"Price List Raw",
    #"Changed types" = Table.TransformColumnTypes(Source, {{"VendorNumber", Int64.Type}, {"Price", type number}}, "en-US"),
    #"Removed type errors" = Table.RemoveRowsWithErrors(#"Changed types", {"VendorNumber", "Price"}),
    // Group By: one row per vendor, how many items it lists and at what average shelf price
    #"Grouped by vendor" = Table.Group(#"Removed type errors", {"VendorNumber"}, {
        {"Catalog Items", each Table.RowCount(_), Int64.Type},
        {"Catalog Avg Price", each List.Average([Price]), type number}}),
    #"Renamed columns" = Table.RenameColumns(#"Grouped by vendor", {{"VendorNumber", "Catalog Vendor No"}})
in
    #"Renamed columns\""""

FORECAST_METRIC = """let
    Source = Parquet.Document(File.Contents(DataFolder & "forecast_scores.parquet")),
    #"Selected columns" = Table.SelectColumns(Source, {"method_label", "method_sort", "wape_line_day", "wape_line_week", "wape_chain_day", "bias"}),
    #"Renamed columns" = Table.RenameColumns(#"Selected columns", {
        {"method_label", "Forecast Method"}, {"method_sort", "Method Order"}, {"wape_line_day", "WAPE Line Day"},
        {"wape_line_week", "WAPE Line Week"}, {"wape_chain_day", "WAPE Chain Day"}, {"bias", "Bias"}}),
    // Unpivot: four metric columns become rows, so one chart can split by metric or by method
    #"Unpivoted metrics" = Table.UnpivotOtherColumns(#"Renamed columns", {"Forecast Method", "Method Order"}, "Metric", "Metric Value"),
    #"Changed types" = Table.TransformColumnTypes(#"Unpivoted metrics", {{"Method Order", Int64.Type}, {"Metric Value", type number}})
in
    #"Changed types\""""

# Merge queries: Item (Parquet) left-outer-joined to Price List 2017 on the brand number.
ITEM_MERGE = [
    '// Merge queries: shelf price and pack units from the Dec 2017 price list (left outer join on the brand)',
    '#"Merged price list" = Table.NestedJoin(Typed, {"brand"}, #"Price List 2017", {"Brand No"}, "Price List", JoinKind.LeftOuter),',
    '#"Expanded price list" = Table.ExpandTableColumn(#"Merged price list", "Price List", {"List Price", "Pack Units"}, '
    '{"list_price_2017", "pack_units"})',
]
SHARED_QUERIES = {"Price List Raw": PRICE_LIST_RAW, "Price List 2017": PRICE_LIST_2017}

# Power BI table -> (mart table, config)
TABLES = {
    "Date": ("dim_date", dict(
        key="date", date_table=True,
        sort={"month": "month_sort", "weekday": "weekday_sort", "quarter": "year_quarter", "month_name": "month_of_year"},
        hidden=["month_sort", "weekday_sort", "year_quarter", "month_of_year"],
        names={"month_of_year": "Month of Year"},
        fmt={"month_start": "mmm yyyy", "year": "0"},
        # Calendar ends on Month Start (a date) so the WINDOW / OFFSET measures over Month Start work at the bottom level.
        hierarchies=[("Calendar", ["Year", "Quarter", "Month Start"])])),
    "Store": ("dim_store", dict(
        key="store", names={"store": "Store No", "store_label": "Store Name", "opened_2016": "Opened 2016", "closed_2016": "Closed 2016"},
        sort={"store_label": "store"}, category={"city": "City"})),
    "Vendor": ("dim_vendor", dict(
        key="vendor_no", names={"vendor_no": "Vendor No", "lead_days": "Avg Lead Days", "lead_sd": "Lead Days SD",
                                "lead_p90": "Lead Days P90", "lead_estimated": "Lead Time Estimated",
                                "purchase_orders": "Invoiced POs", "spend": "Invoiced Spend",
                                "freight": "Invoiced Freight", "freight_pct": "Freight Pct",
                                "days_to_pay": "Avg Days To Pay"},
        fmt={"lead_days": DEC1, "lead_sd": DEC1, "lead_p90": DEC1, "spend": USD0, "freight": USD0,
             "freight_pct": "0.00%", "days_to_pay": DEC1, "shipments": INT, "purchase_orders": INT})),
    "Item": ("dim_item", dict(
        key="brand", hidden=["vendor_no", "abc"],
        names={"brand": "Brand No", "description": "Item", "volume_ml": "Volume ml",
               "sales_value_2016": "Item Sales 2016", "unit_cost": "Item Unit Cost", "abc": "Item ABC"},
        fmt={"retail_price": USD2, "unit_cost": USD2, "sales_value_2016": USD0, "list_price_2017": USD2},
        sort={"Pack Size": "Pack Size Order"},
        hierarchies=[("Product", ["Category", "Pack Size", "Item"])],
        # columns the Power Query merge adds on top of the Parquet file
        merge=ITEM_MERGE, extra=[("list_price_2017", "DOUBLE"), ("pack_units", "BIGINT")])),
    "Status": ("dim_status", dict(
        key="status", sort={"status": "status_sort"}, hidden=["status_sort"],
        names={"status_rule": "Status Rule"})),
    "Cover Band": ("dim_cover_band", dict(
        key="cover_band", sort={"cover_band": "cover_sort"}, hidden=["cover_sort"],
        names={"min_days": "Min Days", "max_days": "Max Days"}, fmt={"min_days": "0", "max_days": "0"})),
    "ABC": ("dim_abc", dict(
        key="abc", sort={"abc_label": "abc_sort"}, hidden=["abc_sort"],
        names={"abc": "ABC Key", "abc_label": "ABC Class"})),
    "Stock": ("fact_stock", dict(
        key="inventory_id",
        hidden=["store", "brand", "vendor_no", "category", "abc", "status", "cover_band"],
        names={"inventory_id": "Inventory ID", "store": "Stock Store", "brand": "Stock Brand",
               "vendor_no": "Stock Vendor", "category": "Stock Category", "abc": "Stock ABC",
               "status": "Stock Status", "cover_band": "Stock Cover Band", "daily_sd": "Daily SD",
               "lead_sd": "Lead SD", "new_line_2016": "New Line 2016", "line_origin": "Line Origin",
               "last_receipt": "Last Receipt", "opening_state": "Opening State"},
        fmt={"last_receipt": "d mmm yyyy", "unit_cost": USD2, "unit_price": USD2, "stock_cost": USD0, "stock_retail": USD0,
             "sales_value": USD0, "order_cost": USD0, "jan_factor": "0.000", "daily_rate": "0.000",
             "daily_forecast": "0.000", "daily_sd": "0.000", "lead_days": DEC1, "lead_sd": DEC1,
             "safety_units": DEC1, "reorder_point": DEC1, "order_up_to": DEC1, "cover_days": "#,0",
             "order_units": INT, "begin_units": INT, "received_units": INT, "end_units": INT,
             "sold_units": INT, "jan_units": INT})),
    "Daily Sales": ("fact_sales_daily", dict(
        hidden=["store", "brand"],
        names={"date": "Sales Date", "store": "Sales Store", "brand": "Sales Brand", "units": "Units Sold",
               "dollars": "Sales Dollars"},
        fmt={"dollars": USD0, "units": INT})),
    "Receipts": ("fact_receipts", dict(
        hidden=["store", "vendor_no", "month_start"],
        names={"month_start": "Receipt Month", "store": "Receipt Store", "vendor_no": "Receipt Vendor",
               "category": "Receipt Category", "units": "Received Units", "dollars": "Received Dollars",
               "lines": "Receipt Lines"},
        fmt={"dollars": USD0, "units": INT, "lines": INT})),
    "Invoices": ("fact_po", dict(
        key="po_number", hidden=["vendor_no"],
        names={"po_number": "PO Number", "vendor_no": "Invoice Vendor", "po_date": "PO Date", "invoice_date": "Invoice Date", "pay_date": "Pay Date",
               "units": "Invoice Units", "dollars": "Invoice Dollars", "freight": "Invoice Freight",
               "freight_pct": "Invoice Freight Pct", "po_matches_receipts": "PO Matches Receipts"},
        fmt={"dollars": USD0, "freight": USD2, "freight_pct": "0.00%", "units": INT},
        )),
    "Forecast Daily": ("forecast_daily", dict(
        sort={"series": "series_sort"}, hidden=["series_sort"],
        names={"date": "Forecast Date", "units": "Series Units"}, fmt={"units": INT})),
    "Forecast Scores": ("forecast_scores", dict(
        sort={"method_label": "method_sort"}, hidden=["method", "method_sort"],
        names={"method": "Method Key", "method_label": "Method", "wape_line_day": "WAPE Line Day", "wape_line_week": "WAPE Line Week",
               "wape_chain_day": "WAPE Chain Day", "bias": "Bias"},
        fmt={"wape_line_day": PCT0, "wape_line_week": PCT0, "wape_chain_day": PCT0, "bias": PCT1})),
    "Weekday Profile": ("weekday_profile", dict(
        sort={"weekday": "weekday_sort"}, hidden=["weekday_sort"],
        names={"weekday": "Day", "jan_units": "January Units"}, fmt={"weekday_index": "0.00", "jan_units": INT})),
    # Bridge: item × vendor pairs bought in 2016 (many-to-many between Item and Vendor).
    "Item Supplier": ("item_supplier", dict(
        hidden=["brand", "vendor_no"],
        names={"brand": "Supplier Brand", "vendor_no": "Supplier Vendor", "lines": "Supplier Lines",
               "units": "Supplier Units", "dollars": "Supplier Dollars", "vendors_for_item": "Vendors For Item",
               "multi_sourced": "Multi Sourced"},
        fmt={"dollars": USD0, "units": INT, "lines": INT})),
    # Shipments per vendor × store × lead-time day (Vendor Detail drillthrough: the lead-time distribution).
    "Shipments": ("fact_shipment", dict(
        hidden=["vendor_no", "store"],
        names={"vendor_no": "Shipment Vendor", "store": "Shipment Store", "lead_days": "Lead Day",
               "shipments": "Shipment Count"},
        fmt={"shipments": INT, "lead_days": "0"})),
    "Store Access": ("store_access", dict(
        hidden_table=True, names={"email": "User Email", "store": "Store No"})),
    # SYNTHETIC planning targets (seeds/category_target.csv): many-to-many to Item[Category], a limited relationship.
    "Category Target": ("category_target", dict(
        names={"category": "Category", "abc": "Target ABC", "target_cover_days": "Target Cover Days"},
        fmt={"target_cover_days": "0"})),
    # Power Query tables on the raw CSVs (column names come out of the M already in model form)
    "Stock Counts": ("pq_stock_counts", dict(
        m=STOCK_COUNTS, hidden=["Count Inventory ID", "Count Store", "Count Brand"],
        fmt={"Counted Units": INT, "Count Price": USD2})),
    "Vendor Catalog": ("pq_vendor_catalog", dict(
        m=VENDOR_CATALOG, hidden=["Catalog Vendor No"], fmt={"Catalog Items": INT, "Catalog Avg Price": USD2})),
    "Forecast Metric": ("pq_forecast_metric", dict(
        m=FORECAST_METRIC, sort={"Forecast Method": "Method Order"}, hidden=["Method Order"],
        fmt={"Metric Value": PCT1})),
}

INACTIVE = {"active": False}
RELATIONSHIPS = [  # (from table, from col, to table, to col[, options])
    ("Stock", "store", "Store", "store"),
    ("Stock", "brand", "Item", "brand"),
    ("Stock", "vendor_no", "Vendor", "vendor_no"),
    ("Stock", "status", "Status", "status"),
    ("Stock", "cover_band", "Cover Band", "cover_band"),
    ("Stock", "abc", "ABC", "abc"),
    ("Daily Sales", "date", "Date", "date"),
    ("Daily Sales", "store", "Store", "store"),
    ("Daily Sales", "brand", "Item", "brand"),
    ("Receipts", "month_start", "Date", "date"),
    ("Receipts", "store", "Store", "store"),
    ("Receipts", "vendor_no", "Vendor", "vendor_no"),
    ("Invoices", "po_date", "Date", "date"),
    # Role-playing Date: invoice and payment dates are inactive, switched on with USERELATIONSHIP.
    ("Invoices", "invoice_date", "Date", "date", INACTIVE),
    ("Invoices", "pay_date", "Date", "date", INACTIVE),
    ("Invoices", "vendor_no", "Vendor", "vendor_no"),
    ("Item Supplier", "brand", "Item", "brand"),
    ("Item Supplier", "vendor_no", "Vendor", "vendor_no"),
    ("Stock Counts", "Count Store", "Store", "store"),
    ("Stock Counts", "Count Brand", "Item", "brand"),
    ("Stock Counts", "Count Date", "Date", "date"),
    ("Vendor Catalog", "Catalog Vendor No", "Vendor", "vendor_no"),
    ("Shipments", "vendor_no", "Vendor", "vendor_no"),
    ("Shipments", "store", "Store", "store"),
    # Many-to-many on Category (6 targets, 2 categories; neither side is unique): a limited relationship.
    ("Category Target", "category", "Item", "category", {"many": True, "both": True}),
]

# Why each relationship is the way it is (README table).
REL_NOTES = {
    "Invoices.invoice_date": "role-playing Date; USERELATIONSHIP in Invoiced by Invoice Date",
    "Invoices.pay_date": "role-playing Date; USERELATIONSHIP in Paid Dollars (cash out by payment month)",
    "Category Target.category": "targets are per category × ABC class, so Category repeats on both sides; both directions so the Category slicer filters the targets",
}

# Calculated columns: table -> [(column, dataType, DAX, extra lines)]. Pack size bands from the bottle volume.
PACK = [(200, "Miniature"), (500, "Half bottle"), (750, "750 mL"), (1000, "Litre"), (1750, "1.5–1.75 L")]
PACK_LAST, PACK_UNKNOWN = "Large format", "Unknown"
_ml = "VAR _ml = IFERROR ( VALUE ( Item[Volume ml] ), BLANK () )\nRETURN\n    SWITCH ( TRUE (),\n"
def grouping(table, column, bin_size=None):
    """extendedProperty GroupingMetadata, as Desktop writes it for a bin (bin_size) or a list group."""
    meta = {"version": 0, "groupedColumns": [{"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": column}}]}
    if bin_size:
        meta["binningMetadata"] = {"binSize": {"value": float(bin_size), "unit": 0}}
    return [f"{T}extendedProperty GroupingMetadata =", *[f"{T*3}{l}" for l in json.dumps(meta, indent=2).split("\n")]]


CALC_COLUMNS = {"Item": [
    ("Pack Size", "string", _ml + f'        ISBLANK ( _ml ), "{PACK_UNKNOWN}",\n'
     + "".join(f'        _ml <= {v}, "{n}",\n' for v, n in PACK) + f'        "{PACK_LAST}" )', []),
    ("Pack Size Order", "int64", _ml + f"        ISBLANK ( _ml ), {len(PACK) + 1},\n"
     + "".join(f"        _ml <= {v}, {i},\n" for i, (v, _) in enumerate(PACK)) + f"        {len(PACK)} )",
     ["isHidden", "formatString: 0"]),
    # Bins: $10 retail price tiers (what Desktop's "New group > Bin" writes).
    ("Retail Price Tier", "double", "IF ( ISBLANK ( Item[Retail Price] ), BLANK (), INT ( Item[Retail Price] / 10 ) * 10 )",
     [f"formatString: {USD0}"], grouping("Item", "Retail Price", 10)),
], "Vendor": [
    # RELATEDTABLE: the Stock rows on the many side of Vendor, counted per vendor row.
    ("Lines Stocked", "int64", "COUNTROWS ( RELATEDTABLE ( Stock ) )", [f"formatString: {INT}"]),
]}
# Store[Store Size] is a list group (Desktop "New group"): stores in thirds by stock value, written in main()
# because the store lists come from the data.

# Calendar-based time intelligence: (calendar, [(time unit, primary column, associated column)])
CALENDARS = [
    ("Gregorian", [("year", "Year", None), ("quarter", "Year Quarter", "Quarter"), ("month", "Month Sort", "Month"),
                   ("monthOfYear", "Month of Year", "Month Name"), ("date", "Date", None)]),
]

# DAX user-defined functions: (name, description, signature => body)
FUNCTIONS = [
    ("Stock.SafetyStock", "Safety stock for one line: z × √(lead days × daily SD² + daily demand² × lead-time SD²).",
     "( z : NUMERIC, demand : NUMERIC, lead : NUMERIC, demandSd : NUMERIC, leadSd : NUMERIC ) =>\n"
     "\tz * SQRT ( lead * demandSd ^ 2 + demand ^ 2 * leadSd ^ 2 )"),
    ("Stock.ReorderPoint", "Reorder point: demand over the lead time plus safety stock. "
     "Example: Stock.ReorderPoint ( 2, 7, 5 ) = 19.",
     "( demand : NUMERIC, lead : NUMERIC, safety : NUMERIC ) => demand * lead + safety"),
    ("Stock.OrderQty", "Units that top a line up to demand over lead time + review period + safety stock, rounded up. "
     "Example: Stock.OrderQty ( 2, 7, 7, 5, 10 ) = 23.",
     "( demand : NUMERIC, lead : NUMERIC, review : NUMERIC, safety : NUMERIC, onHand : NUMERIC ) =>\n"
     "\tROUNDUP ( demand * ( lead + review ) + safety - onHand, 0 )"),
    ("Stock.CoverDays", "Days the units last at the daily demand; blank when there is no demand. "
     "Example: Stock.CoverDays ( 300, 5 ) = 60.",
     "( units : NUMERIC, perDay : NUMERIC ) => DIVIDE ( units, perDay )"),
    ("Kpi.Rate", "Part ÷ whole; blank when the whole is empty.",
     "( part : NUMERIC, whole : NUMERIC ) => DIVIDE ( part, whole )"),
    ("Kpi.Change", "Relative change from a base value; blank when the base is empty.",
     "( current : NUMERIC, base : NUMERIC ) => DIVIDE ( current - base, base )"),
    ("Kpi.ShareOf", "A measure as a share of the same measure with the filters on one table or column removed.",
     "( value : SCALAR NUMERIC EXPR, scope : ANYREF ) => DIVIDE ( value, CALCULATE ( value, REMOVEFILTERS ( scope ) ) )"),
    ("Kpi.PriorMonth", "A measure one month back on the calendar passed in.",
     "( value : SCALAR NUMERIC EXPR, cal : CALENDARREF ) => CALCULATE ( value, DATEADD ( cal, -1, MONTH ) )"),
]

# Time Intelligence calculation group: (item, DAX, format string expression or None)
PCT_SIGNED = '"+0.0%;-0.0%;0.0%"'
TIME_ITEMS = [
    ("Current", "SELECTEDMEASURE ()", None),
    ("Prior Month", "CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, MONTH ) )", None),
    ("MoM %", "VAR _pm = CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, MONTH ) )\n"
              "RETURN DIVIDE ( SELECTEDMEASURE () - _pm, _pm )", PCT_SIGNED),
    ("MTD", "CALCULATE ( SELECTEDMEASURE (), DATESMTD ( 'Gregorian' ) )", None),
    ("QTD", "CALCULATE ( SELECTEDMEASURE (), DATESQTD ( 'Gregorian' ) )", None),
    ("YTD", "CALCULATE ( SELECTEDMEASURE (), DATESYTD ( 'Gregorian' ) )", None),
    ("Prior Year", "CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, YEAR ) )", None),
    ("YoY %", "VAR _py = CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, YEAR ) )\n"
              "RETURN DIVIDE ( SELECTEDMEASURE () - _py, _py )", PCT_SIGNED),
]

# Dynamic format string for money: $940, $94K, $3.52M depending on the size of the value.
USD_AUTO = ('VAR _v = ABS ( SELECTEDMEASURE () )\nRETURN\n    SWITCH ( TRUE (),\n'
            '        _v >= 1e6, "$#,0.00,,""M"";($#,0.00,,""M"")",\n'
            '        _v >= 1e4, "$#,0,""K"";($#,0,""K"")",\n        "$#,0;($#,0)" )')

# DAX-only tables: name -> (DAX source, [(column, sourceColumn, dataType, extra lines)])
CALCULATED = {
    "Service Level": (
        'SELECTCOLUMNS ( GENERATESERIES ( 0.80, 0.99, 0.01 ), "Service Level", [Value] )',
        [("Service Level", "[Service Level]", "double", [f"formatString: {PCT0}"])]),
    # Disconnected steps for the stock-bridge waterfall (Bridge Value switches on the step).
    "Bridge Step": (
        'DATATABLE ( "Step", STRING, "Step Order", INTEGER, { { "Opening stock", 1 }, { "Received", 2 }, { "Sold", 3 } } )',
        [("Step", "[Step]", "string", ["sortByColumn: 'Step Order'"]),
         ("Step Order", "[Step Order]", "int64", ["isHidden", "formatString: 0"])]),
    # Procurement funnel steps (Funnel POs switches on the step).
    "Funnel Step": (
        'DATATABLE ( "Funnel Step", STRING, "Funnel Order", INTEGER, { { "PO raised", 1 }, { "Invoiced", 2 }, '
        '{ "Matches receipts", 3 }, { "Approved", 4 }, { "Paid", 5 } } )',
        [("Funnel Step", "[Funnel Step]", "string", ["sortByColumn: 'Funnel Order'"]),
         ("Funnel Order", "[Funnel Order]", "int64", ["isHidden", "formatString: 0"])]),
    "Review Period": (
        'SELECTCOLUMNS ( GENERATESERIES ( 1, 28, 1 ), "Review Days", [Value] )',
        [("Review Days", "[Review Days]", "int64", ["formatString: 0"])]),
}

ANALYZE_BY = [("Store", "'Store'[Store Name]"), ("City", "'Store'[City]"), ("Vendor", "'Vendor'[Vendor]"),
              ("Category", "'Item'[Category]"), ("ABC class", "'ABC'[ABC Class]")]
# Measure field parameter: one chart, the metric picked in a slicer.
METRIC = [("January Sales", "[January Sales]"), ("Order Value", "[Order Value]"),
          ("Excess Stock Value", "[Excess Stock Value]"), ("Stock Value", "[Stock Value]"),
          ("Out of Stock Lines", "[Out of Stock Lines]")]
FIELD_PARAMS = {"Analyze By": ANALYZE_BY, "Metric": METRIC}

ROLES = {
    # A store manager sees their store; a row with no store grants the whole city (district manager).
    "Store Manager": {"filters": {
        "Store": "VAR _u = USERPRINCIPALNAME () RETURN NOT ISEMPTY ( FILTER ( 'Store Access', "
                 "'Store Access'[User Email] = _u && 'Store Access'[City] = Store[City] "
                 "&& ( 'Store Access'[Store No] = \"\" || 'Store Access'[Store No] = FORMAT ( Store[Store No], \"0\" ) ) ) )",
        "Store Access": "'Store Access'[User Email] = USERPRINCIPALNAME ()"}},
    "Hornsey District": {"filters": {"Store": "Store[City] = \"Hornsey\""}},
    # Object-level security: shop-floor staff see stock and orders, not invoices or supplier payment terms.
    "Store Staff": {"filters": {}, "hide_tables": ["Invoices"],
                    "hide_columns": {"Vendor": ["Invoiced Spend", "Invoiced Freight", "Freight Pct", "Avg Days To Pay"]}},
}

PERSPECTIVES = {
    "Restock": ["Stock", "Item", "Store", "Vendor", "Status", "Cover Band", "ABC", "Service Level", "Review Period",
                "Analyze By", "Metric", "Category Target", "_Measures"],
    "Purchasing": ["Invoices", "Receipts", "Vendor", "Item Supplier", "Item", "Store", "Date", "Time Intelligence",
                   "_Measures"],
    "Demand": ["Daily Sales", "Forecast Daily", "Forecast Scores", "Weekday Profile", "Date", "Item", "Store",
               "Time Intelligence", "_Measures"],
}

SYNONYMS = {  # Q&A / Copilot terms
    "Vendor": ["supplier", "distributor", "wholesaler"], "Store": ["shop", "branch", "outlet"],
    "Item": ["product", "sku", "bottle"], "Stock Value": ["inventory value", "stock on hand value"],
    "Units To Order": ["reorder quantity", "order quantity", "order qty"],
    "Out of Stock Rate": ["stockout rate", "oos rate"], "Days of Cover": ["days of supply", "cover"],
    "Excess Stock Value": ["overstock", "excess inventory", "tied-up cash"],
}

# The restock rule, line by line, with the what-if service level and review period.
# Same maths as sql/20_marts.sql: SS = z·√(L·σ² + d²·σL²), ROP = d·L + SS, order up to d·(L+R) + SS.
LINE = ("VAR _z = [Service Z]\nVAR _r = [Review Days Selected]\nRETURN\n    {agg} (\n        Stock,\n"
        "        VAR _d = Stock[Daily Forecast]\n        VAR _l = Stock[Lead Days]\n"
        "        VAR _ss = Stock.SafetyStock ( _z, _d, _l, Stock[Daily SD], Stock[Lead SD] )\n"
        "        VAR _due = _d > 0 && Stock[End Units] <= Stock.ReorderPoint ( _d, _l, _ss )\n"
        "        RETURN {expr}\n    )")
ORDER_QTY = "Stock.OrderQty ( _d, _l, _r, _ss, Stock[End Units] )"


DAX_QUERIES = {
    "01 KEEPFILTERS vs overwrite": (
        "// Out-of-stock lines in class A per ABC class: KEEPFILTERS intersects, a plain filter overwrites.\n"
        "EVALUATE\nSUMMARIZECOLUMNS (\n    ABC[ABC Class],\n    \"Out of stock\", [Out of Stock Lines],\n"
        "    \"A, KEEPFILTERS\", [Out of Stock Lines A],\n"
        "    \"A, overwrite\", CALCULATE ( [Out of Stock Lines], ABC[ABC Key] = \"A\" )\n)\n"),
    "02 Vendors to call": (
        "// Vendors with more than $10K to order, biggest first.\n"
        "EVALUATE\nFILTER (\n    SUMMARIZECOLUMNS ( Vendor[Vendor], \"@Order\", [Order Value], \"@Lines\", [Lines To Order] ),\n"
        "    [@Order] > 10000\n)\nORDER BY [@Order] DESC\n"),
    "03 OFFSET WINDOW DATESINPERIOD": (
        "// Three ways to look back a month and average three: window functions vs time intelligence.\n"
        "EVALUATE\nSUMMARIZECOLUMNS (\n    'Date'[Month Start],\n    \"Received\", [Received Value],\n"
        "    \"Prev (OFFSET)\", [Received Value Prev (OFFSET)],\n    \"Prior month (DATEADD)\", [Received Value Prior Month],\n"
        "    \"3M (WINDOW)\", [Received Value 3M (WINDOW)],\n    \"3M avg (DATESINPERIOD)\", [Received Value 3M Avg]\n)\n"
        "ORDER BY 'Date'[Month Start]\n"),
    "04 Expanded table": (
        "// 'Daily Sales' as a CALCULATE filter is its expanded table: Store and Item travel to Stock.\n"
        "EVALUATE\nSUMMARIZECOLUMNS (\n    Item[Category],\n    \"Stock value\", [Stock Value],\n"
        "    \"of January sellers\", [Stock Value of Jan Sellers]\n)\n"),
}


def money(expr):
    """DAX text for an amount in $m, e.g. $3.5m."""
    return f'FORMAT ( {expr} / 1e6, "$#,0.0" ) & "m"'


# (folder, name, format, description, DAX)
MEASURES = [
    # ------------------------------------------------------------ Stock (31 Dec 2016)
    ("Stock", "Lines", INT, "Store × item lines in the current filter.", "COUNTROWS ( Stock )"),
    ("Stock", "Units On Hand", INT, "Units on the shelf at 31 Dec 2016.", "SUM ( Stock[End Units] )"),
    ("Stock", "Stock Value", USD_AUTO, "Units on hand × latest 2016 unit cost.", "SUM ( Stock[Stock Cost] )"),
    ("Stock", "Stock Retail Value", USD0, "Units on hand × shelf price.", "SUM ( Stock[Stock Retail] )"),
    ("Stock", "Units Sold 2016", INT, "Implied from the stock identity: begin + received − end. Includes shrinkage.",
     "SUM ( Stock[Sold Units] )"),
    ("Stock", "COGS 2016", USD0, "Units sold 2016 × unit cost.", "SUMX ( Stock, Stock[Sold Units] * Stock[Unit Cost] )"),
    ("Stock", "Stock Turns", "0.0", "COGS 2016 ÷ year-end stock value.", "Kpi.Rate ( [COGS 2016], [Stock Value] )"),
    ("Stock", "Days of Cover", "#,0", "Units on hand ÷ forecast daily demand.",
     "Stock.CoverDays ( [Units On Hand], SUM ( Stock[Daily Forecast] ) )"),
    ("Stock", "Out of Stock Lines", INT, "Lines with nothing on the shelf at the year-end count.", 'CALCULATE ( [Lines], Stock[Stock Status] = "Out of stock" )'),
    ("Stock", "Selling Lines", INT, "Lines with forecast demand (not dead or inactive).",
     "CALCULATE ( [Lines], Stock[Daily Forecast] > 0 )"),
    ("Stock", "Out of Stock Rate", PCT1, "Out-of-stock lines ÷ lines with demand.",
     "Kpi.Rate ( [Out of Stock Lines], [Selling Lines] )"),
    ("Stock", "Overstock Value", USD0, f"Stock value of lines with more than {OVERSTOCK_DAYS} days of cover.",
     'CALCULATE ( [Stock Value], Stock[Stock Status] = "Overstock" )'),
    ("Stock", "Dead Stock Value", USD0, "Stock value of lines that sold nothing in 2016.",
     'CALCULATE ( [Stock Value], Stock[Stock Status] = "Dead stock" )'),
    ("Stock", "Excess Stock Value", USD_AUTO,
     f"Cost of units beyond {OVERSTOCK_DAYS} days of cover, plus all dead stock: cash that could be freed.",
     f"SUMX ( Stock, IF ( Stock[Daily Forecast] > 0, MAX ( 0, Stock[End Units] - {OVERSTOCK_DAYS} * Stock[Daily Forecast] ), "
     "Stock[End Units] ) * Stock[Unit Cost] )"),
    # ------------------------------------------------------------ Restock (follows the what-if sliders)
    ("Restock", "Service Level Selected", PCT0, "Chance of not running out during one lead time. Default 95%.",
     f"SELECTEDVALUE ( 'Service Level'[Service Level], {SERVICE_DEFAULT} )"),
    ("Restock", "Service Z", "0.000", "Standard normal z for the selected service level.",
     "NORM.S.INV ( [Service Level Selected] )"),
    ("Restock", "Review Days Selected", "0", "Days between order runs. Default 7.",
     f"SELECTEDVALUE ( 'Review Period'[Review Days], {REVIEW_DEFAULT} )"),
    ("Restock", "Lines To Order", INT, "Lines at or below their reorder point.",
     LINE.format(agg="SUMX", expr="IF ( _due, 1 )")),
    ("Restock", "Units To Order", INT, "Units that bring every due line back up to its order-up-to level.",
     LINE.format(agg="SUMX", expr=f"IF ( _due, {ORDER_QTY} )")),
    ("Restock", "Order Value", USD_AUTO, "Units to order × unit cost.",
     LINE.format(agg="SUMX", expr=f"IF ( _due, {ORDER_QTY} * Stock[Unit Cost] )")),
    ("Restock", "Safety Stock Units", INT, "Buffer for demand and lead-time variation at the selected service level.",
     LINE.format(agg="SUMX", expr="_ss")),
    ("Restock", "Safety Stock Value", USD0, "Safety stock units × unit cost.", LINE.format(agg="SUMX", expr="_ss * Stock[Unit Cost]")),
    ("Restock", "Forecast Units per Day", INT, "Expected daily demand: 2016 rate × January season factor.",
     "SUM ( Stock[Daily Forecast] )"),
    # ------------------------------------------------------------ Demand (January 2016 till receipts)
    ("Demand", "January Units Sold", INT, "Units sold in January 2016 (the only complete month in the sales file).",
     "SUM ( 'Daily Sales'[Units Sold] )"),
    ("Demand", "January Sales", USD_AUTO, "Till receipts in January 2016.", "SUM ( 'Daily Sales'[Sales Dollars] )"),
    ("Demand", "Avg Selling Price", USD2, "January sales ÷ January units.", "Kpi.Rate ( [January Sales], [January Units Sold] )"),
    ("Demand", "Day Index", "0.00", "Average day's units ÷ average day. 1.00 = a normal day.",
     "AVERAGE ( 'Weekday Profile'[Weekday Index] )"),
    ("Demand", "Chain Units", INT, "Actual or forecast units for the whole chain (backtest week: 25–31 Jan).",
     "SUM ( 'Forecast Daily'[Series Units] )"),
    *[("Demand", name, INT, desc, f"CALCULATE ( [Chain Units], 'Forecast Daily'[Series] = \"{series}\" )")
      for name, series, desc in [
          ("Actual Chain Units", "Actual", "Units the chain sold each day of January 2016."),
          ("Forecast Restock Method", "Annual rate × season × weekday", "The method the restock list uses."),
          ("Forecast Avg Weekday", "Average × weekday", "Jan 1–24 average × weekday index."),
          ("Forecast Last Week", "Same day last week", "The units sold on the same weekday one week earlier.")]],
    ("Demand", "Line Week Error", PCT0, "WAPE per store × item × week. Lower is better.",
     "MIN ( 'Forecast Scores'[WAPE Line Week] )"),
    ("Demand", "Chain Day Error", PCT0, "WAPE of the chain's daily total. Lower is better.",
     "MIN ( 'Forecast Scores'[WAPE Chain Day] )"),
    ("Demand", "Forecast Bias", PCT1, "Forecast ÷ actual − 1. Negative = under-forecast, which means stock-outs.",
     "MIN ( 'Forecast Scores'[Bias] )"),
    # ------------------------------------------------------------ Purchasing
    ("Purchasing", "Purchase Orders", INT, "Invoiced purchase orders.", "COUNTROWS ( Invoices )"),
    ("Purchasing", "Purchase Spend", USD_AUTO, "Invoiced dollars, freight excluded.", "SUM ( Invoices[Invoice Dollars] )"),
    ("Purchasing", "Freight Cost", USD0, "Freight charged on the invoices.", "SUM ( Invoices[Invoice Freight] )"),
    ("Purchasing", "Freight Share", "0.00%", "Freight ÷ purchase spend.", "Kpi.Rate ( [Freight Cost], [Purchase Spend] )"),
    ("Purchasing", "Payment Days", DEC1, "Average days from invoice to payment.", "AVERAGE ( Invoices[Days To Pay] )"),
    ("Purchasing", "Unmatched POs", INT, "Invoiced POs whose number never appears on a receiving line.",
     "CALCULATE ( [Purchase Orders], Invoices[PO Matches Receipts] = FALSE () )"),
    ("Purchasing", "Received Value", USD_AUTO, "Receiving lines at cost.", "SUM ( Receipts[Received Dollars] )"),
    ("Purchasing", "Received Qty", INT, "Units on the receiving lines.", "SUM ( Receipts[Received Units] )"),
    ("Purchasing", "Lead Time", DEC1, "Average PO-to-receipt days over vendors with measured shipments.",
     "CALCULATE ( AVERAGE ( Vendor[Avg Lead Days] ), Vendor[Lead Time Estimated] = FALSE () )"),
    ("Purchasing", "Received Value Prior Month", USD0, "Received value one month back on the Gregorian calendar.",
     "Kpi.PriorMonth ( [Received Value], 'Gregorian' )"),
    ("Purchasing", "Received Value MoM", "+0.0%;-0.0%;0.0%", "Change in received value against the prior month.",
     "Kpi.Change ( [Received Value], [Received Value Prior Month] )"),
    # ------------------------------------------------------------ Item ↔ vendor bridge (many-to-many)
    ("Sourcing", "Vendors per Item", INT, "Vendors the item was bought from in 2016 (through the Item Supplier bridge).",
     "DISTINCTCOUNT ( 'Item Supplier'[Supplier Vendor] )"),
    ("Sourcing", "Items Supplied", INT, "Items a vendor delivered at least once in 2016.",
     "DISTINCTCOUNT ( 'Item Supplier'[Supplier Brand] )"),
    ("Sourcing", "Multi-Sourced Items", INT, "Items bought from more than one vendor in 2016.",
     "CALCULATE ( DISTINCTCOUNT ( 'Item Supplier'[Supplier Brand] ), 'Item Supplier'[Multi Sourced] = TRUE () )"),
    ("Sourcing", "Multi-Sourced Spend", USD0, "2016 purchase dollars on items that more than one vendor supplied.",
     "CALCULATE ( SUM ( 'Item Supplier'[Supplier Dollars] ), 'Item Supplier'[Multi Sourced] = TRUE () )"),
    ("Sourcing", "Multi-Sourced Units", INT, "2016 purchase units on items that more than one vendor supplied.",
     "CALCULATE ( SUM ( 'Item Supplier'[Supplier Units] ), 'Item Supplier'[Multi Sourced] = TRUE () )"),
    ("Sourcing", "Stock of Supplied Items", USD_AUTO,
     "Year-end stock value of every item the vendor supplied in 2016, even when the latest order went to another vendor.",
     "CALCULATE (\n    [Stock Value],\n    CROSSFILTER ( 'Item Supplier'[Supplier Brand], Item[Brand No], Both ),\n"
     "    CROSSFILTER ( Stock[Stock Vendor], Vendor[Vendor No], None )\n)"),
    ("Stock", "Share of Stock Value", PCT1, "This row's share of the stock value across all stores.",
     "Kpi.ShareOf ( [Stock Value], Store )"),
    # ------------------------------------------------------------ Power Query tables
    ("Restock", "Order Shelf Value 2017", USD_AUTO,
     "The order at the Dec 2017 shelf price merged in from the price list (the 2016 price where an item is not listed): "
     "what the restocked units sell for. The 2017 purchase price equals the 2016 unit cost on every line, so only the shelf price moves.",
     LINE.format(agg="SUMX", expr=f"IF ( _due, {ORDER_QTY} * COALESCE ( RELATED ( Item[List Price 2017] ), Stock[Unit Price] ) )")),
    ("Sourcing", "Listed Items", INT, "Items the vendor lists on the Dec 2017 price list (Vendor Catalog, grouped in Power Query).",
     "SUM ( 'Vendor Catalog'[Catalog Items] )"),
    ("Demand", "Metric Score", PCT1, "One forecast metric for one method, from the unpivoted Forecast Metric table.",
     "SUM ( 'Forecast Metric'[Metric Value] )"),
    # ------------------------------------------------------------ Model & DAX patterns
    ("Stock", "Units On Hand (Closing)", INT,
     "Semi-additive: counted units on the last count date in the filter (31 Dec 2016 unless a date is picked), "
     "from Stock Counts. Follows Store, City, Category and date; not Vendor or ABC class, which only filter Stock.",
     "CALCULATE (\n    SUM ( 'Stock Counts'[Counted Units] ),\n"
     "    LASTNONBLANK ( 'Date'[Date], CALCULATE ( SUM ( 'Stock Counts'[Counted Units] ) ) )\n)"),
    ("Stock", "Opening Units", INT, "Semi-additive: counted units on the first count date in the filter (1 Jan 2016).",
     "CALCULATE (\n    SUM ( 'Stock Counts'[Counted Units] ),\n"
     "    FIRSTNONBLANK ( 'Date'[Date], CALCULATE ( SUM ( 'Stock Counts'[Counted Units] ) ) )\n)"),
    ("Stock", "Lines Below Safety", INT,
     "Lines with fewer units than their safety stock; comparing two columns of the same row needs FILTER.",
     "CALCULATE ( [Lines], FILTER ( Stock, Stock[End Units] < Stock[Safety Units] ) )"),
    ("Stock", "Out of Stock Lines A", INT,
     "Out-of-stock lines in class A. KEEPFILTERS keeps the ABC slicer: with B picked the result is blank, not the A count.",
     'CALCULATE ( [Out of Stock Lines], KEEPFILTERS ( ABC[ABC Key] = "A" ) )'),
    ("Stock", "Category Stock Value", USD_AUTO,
     "Stock value of the whole category the row belongs to: ALLEXCEPT drops every Item filter except Category.",
     "CALCULATE ( [Stock Value], ALLEXCEPT ( Item, Item[Category] ) )"),
    ("Stock", "Stores Above 25% OOS", INT,
     "Stores whose out-of-stock rate is above 25%: the measure runs once per store through context transition.",
     "COUNTROWS ( FILTER ( VALUES ( Store[Store No] ), [Out of Stock Rate] > 0.25 ) )"),
    ("Stock", "Stock Value of Jan Sellers", USD_AUTO,
     "Stock value of the store × item lines that sold in January. 'Daily Sales' as a filter is its expanded table, "
     "so its Store and Item columns travel to Stock through the relationships.",
     "CALCULATE ( [Stock Value], 'Daily Sales' )"),
    ("Stock", "Lines Over Target Cover", INT,
     "Selling lines with more days of cover than the (synthetic) target for their category and ABC class. "
     "Category Target meets Item through a limited many-to-many relationship, so RELATED can't reach it: each target "
     "row filters Item by context transition instead.",
     "SUMX (\n    'Category Target',\n    VAR _abc = 'Category Target'[Target ABC]\n"
     "    VAR _days = 'Category Target'[Target Cover Days]\n    RETURN\n        CALCULATE (\n"
     "            COUNTROWS ( FILTER ( Stock, Stock[Daily Forecast] > 0 && Stock[Cover Days] > _days ) ),\n"
     "            KEEPFILTERS ( Stock[Stock ABC] = _abc )\n        )\n)"),
    ("Sourcing", "Lines Under P90 Lead", INT,
     "Selling lines whose cover is shorter than their vendor's 90th-percentile lead time (RELATED from Stock to Vendor).",
     "SUMX ( Stock, IF ( Stock[Daily Forecast] > 0 && Stock[Cover Days] < RELATED ( Vendor[Lead Days P90] ), 1 ) )"),
    ("Restock", "Urgent Order Value", USD_AUTO,
     "Order value for lines already out of stock. KEEPFILTERS adds to the Status filter instead of replacing it.",
     'CALCULATE ( [Order Value], KEEPFILTERS ( Status[Status] IN { "Out of stock" } ) )'),
    ("Restock", "Share of Selected Order", PCT1,
     "This vendor's share of the order across the vendors in the current selection (ALLSELECTED on Vendor).",
     "DIVIDE ( [Order Value], CALCULATE ( [Order Value], ALLSELECTED ( Vendor ) ) )"),
    ("Restock", "% of Visible Order", PCT1,
     "This row's share of the order total the visual shows: ALLSELECTED () keeps the slicers, drops the row.",
     "DIVIDE ( [Order Value], CALCULATE ( [Order Value], ALLSELECTED () ) )"),
    ("Restock", "Vendors To Call", INT, "Vendors with more than $10K to order: a temporary table of vendor × order value.",
     'COUNTROWS ( FILTER ( ADDCOLUMNS ( VALUES ( Vendor[Vendor] ), "@ord", [Order Value] ), [@ord] > 10000 ) )'),
    ("Restock", "Top Order Store in City", None,
     "The store with the biggest order in the city on the row: INDEX 1 within a PARTITIONBY City window.",
     "IF (\n    HASONEVALUE ( Store[City] ),\n    MAXX (\n"
     "        INDEX ( 1, SUMMARIZE ( ALLSELECTED ( Store ), Store[City], Store[Store Name] ),\n"
     "                ORDERBY ( [Order Value], DESC ), LAST, PARTITIONBY ( Store[City] ) ),\n"
     "        Store[Store Name]\n    )\n)"),
    ("Purchasing", "% of Visible Spend", PCT1,
     "This row's share of the purchase spend the visual shows: ALLSELECTED () keeps the slicers, drops the row.",
     "DIVIDE ( [Purchase Spend], CALCULATE ( [Purchase Spend], ALLSELECTED () ) )"),
    ("Purchasing", "Paid Dollars", USD_AUTO, "Invoice dollars by payment date (USERELATIONSHIP on the inactive Pay Date).",
     "CALCULATE ( SUM ( Invoices[Invoice Dollars] ), USERELATIONSHIP ( Invoices[Pay Date], 'Date'[Date] ) )"),
    ("Purchasing", "Invoiced by Invoice Date", USD_AUTO,
     "Invoice dollars by invoice date (USERELATIONSHIP on the inactive Invoice Date).",
     "CALCULATE ( SUM ( Invoices[Invoice Dollars] ), USERELATIONSHIP ( Invoices[Invoice Date], 'Date'[Date] ) )"),
    ("Purchasing", "Received Value 3M Avg", USD_AUTO,
     "Average monthly received value over the last three months with receipts (DATESINPERIOD).",
     "VAR _win = DATESINPERIOD ( 'Date'[Date], MAX ( 'Date'[Date] ), -3, MONTH )\nRETURN\n"
     "    DIVIDE ( CALCULATE ( [Received Value], _win ), CALCULATE ( DISTINCTCOUNT ( Receipts[Receipt Month] ), _win ) )"),
    ("Purchasing", "Received Value Prev (OFFSET)", USD_AUTO,
     "Received value one month back with OFFSET over the months in the visual, no time-intelligence function.",
     "CALCULATE (\n    [Received Value],\n"
     "    OFFSET ( -1, ALLSELECTED ( 'Date'[Month Start] ), ORDERBY ( 'Date'[Month Start] ) )\n)"),
    ("Purchasing", "Received Value 3M (WINDOW)", USD_AUTO,
     "Three-month moving average with WINDOW: this month and the two before, blank where nothing was received "
     "and above the month level (the Calendar hierarchy drills Year → Quarter → Month Start).",
     "IF (\n    ISINSCOPE ( 'Date'[Month Start] ) && NOT ISBLANK ( [Received Value] ),\n    AVERAGEX (\n"
     "        WINDOW ( -2, REL, 0, REL, ALLSELECTED ( 'Date'[Month Start] ), ORDERBY ( 'Date'[Month Start] ) ),\n"
     "        [Received Value]\n    )\n)"),
    ("Purchasing", "Vendor Rank in Category", INT,
     "Rank of the vendor by received value within its receipt category (RANK with PARTITIONBY).",
     "IF (\n    HASONEVALUE ( Vendor[Vendor] ) && HASONEVALUE ( Receipts[Receipt Category] ),\n"
     "    RANK ( DENSE, SUMMARIZE ( ALLSELECTED ( Receipts ), Vendor[Vendor], Receipts[Receipt Category] ),\n"
     "           ORDERBY ( [Received Value], DESC ), LAST, PARTITIONBY ( Receipts[Receipt Category] ) )\n)"),
    ("Demand", "January Units 7D Avg", INT, "Average daily units over the seven days ending on the row's date.",
     "AVERAGEX ( DATESINPERIOD ( 'Date'[Date], MAX ( 'Date'[Date] ), -7, DAY ), [January Units Sold] )"),
    ("Demand", "January Units by Weekday", INT,
     "January units per weekday of the disconnected Weekday Profile table, joined at query time with TREATAS.",
     "CALCULATE ( [January Units Sold], TREATAS ( VALUES ( 'Weekday Profile'[Day] ), 'Date'[Weekday] ) )"),
    # ------------------------------------------------------------ Report & UX (phase C)
    ("Stock", "Bridge Value", USD_AUTO,
     "Stock bridge at 2016 unit cost: opening stock + received − sold = closing stock value (waterfall on Bridge Step). "
     "Sold is implied by the stock identity, so the bridge closes exactly.",
     "VAR _cost = SWITCH (\n    SELECTEDVALUE ( 'Bridge Step'[Step] ),\n"
     "    \"Opening stock\", SUMX ( Stock, Stock[Begin Units] * Stock[Unit Cost] ),\n"
     "    \"Received\", SUMX ( Stock, Stock[Received Units] * Stock[Unit Cost] ),\n"
     "    \"Sold\", - SUMX ( Stock, Stock[Sold Units] * Stock[Unit Cost] )\n)\nRETURN _cost"),
    ("Stock", "Status Code", "0", "Sort number of the row's stock status: 1 out of stock, 2 reorder now, 3 healthy, "
     "4 overstock, 5 dead stock, 6 inactive. Drives the Status colours and icons in the tables.",
     "MIN ( Status[Status Sort] )"),
    ("Restock", "Reorder Point Units", INT, "Demand over the lead time + safety stock, at the selected service level.",
     LINE.format(agg="SUMX", expr="Stock.ReorderPoint ( _d, _l, _ss )")),
    ("Restock", "Order Up To Units", INT, "Demand over lead time + review period + safety stock: what a due line is topped up to.",
     LINE.format(agg="SUMX", expr="_d * ( _l + _r ) + _ss")),
    ("Purchasing", "Vendor Shipments", INT, "Shipments received (PO × store × receiving date).",
     "SUM ( Shipments[Shipment Count] )"),
    # ------------------------------------------------------------ Analytics (phase D)
    ("Assortment", "Sales 2016", USD_AUTO, "2016 sales value of the lines in the filter: implied units sold × shelf price.",
     "SUM ( Stock[Sales Value] )"),
    ("Assortment", "Cumulative Sales Share", PCT1,
     "Pareto: share of 2016 sales taken by this item and every item that sold more, within the current slicers. "
     "REMOVEFILTERS on Item[Item] drops the row and a Top N filter on the axis but keeps every other slicer.",
     "VAR _s = [Sales 2016]\nVAR _items =\n    CALCULATETABLE (\n"
     "        ADDCOLUMNS ( VALUES ( Item[Item] ), \"@s\", [Sales 2016] ),\n        REMOVEFILTERS ( Item[Item] )\n    )\n"
     "RETURN\n    IF (\n        HASONEVALUE ( Item[Item] ) && NOT ISBLANK ( _s ),\n"
     "        DIVIDE ( SUMX ( FILTER ( _items, [@s] >= _s ), [@s] ), SUMX ( _items, [@s] ) )\n    )"),
    ("Assortment", "Pareto 80% Line", PCT0, "Constant 80% reference line for the Pareto chart.", "0.8"),
    ("Assortment", "ABC Dynamic", None,
     "ABC class recomputed on the brands inside the current slicers (cumulative 2016 sales ≤ 80% A, ≤ 95% B, else C), "
     "against the static class in the data, which is ranked chain-wide.",
     "VAR _s = [Sales 2016]\nVAR _brands =\n    CALCULATETABLE (\n"
     "        ADDCOLUMNS ( VALUES ( Item[Brand No] ), \"@s\", [Sales 2016] ),\n"
     "        REMOVEFILTERS ( Item[Brand No], Item[Item], Item[Size] )\n    )\n"
     "VAR _cum = DIVIDE ( SUMX ( FILTER ( _brands, [@s] >= _s ), [@s] ), SUMX ( _brands, [@s] ) )\n"
     "RETURN\n    IF (\n        HASONEVALUE ( Item[Brand No] ) && NOT ISBLANK ( _s ),\n"
     "        SWITCH ( TRUE (), _cum <= 0.80, \"A\", _cum <= 0.95, \"B\", \"C\" )\n    )"),
    ("Assortment", "ABC Static", None, "The item's chain-wide ABC class as stored in the data.",
     "SELECTEDVALUE ( Item[Item ABC] )"),
    ("Assortment", "Lines in Band (Dynamic)", INT,
     "Lines whose days of cover fall inside the band's Min Days / Max Days edges (a table, not hard-coded text). "
     "No demand = no cover at all. Matches the Stock Cover Band column (a pipeline test checks the edges).",
     "VAR _lo = SELECTEDVALUE ( 'Cover Band'[Min Days] )\nVAR _hi = SELECTEDVALUE ( 'Cover Band'[Max Days] )\n"
     "RETURN\n    IF (\n        HASONEVALUE ( 'Cover Band'[Cover Band] ),\n        CALCULATE (\n            [Lines],\n"
     "            REMOVEFILTERS ( 'Cover Band' ),\n            FILTER (\n                VALUES ( Stock[Cover Days] ),\n"
     "                IF (\n                    ISBLANK ( _hi ),\n                    ISBLANK ( Stock[Cover Days] ),\n"
     "                    NOT ISBLANK ( Stock[Cover Days] ) && Stock[Cover Days] > _lo && Stock[Cover Days] <= _hi\n"
     "                )\n            )\n        )\n    )"),
    ("Assortment", "New Line OOS Rate", PCT1, "Out-of-stock rate of lines first listed in 2016 (in the closing count, not the opening one).",
     "CALCULATE ( [Out of Stock Rate], Stock[New Line 2016] = TRUE () )"),
    ("Assortment", "Carried Line OOS Rate", PCT1, "Out-of-stock rate of lines that were already in the opening count.",
     'CALCULATE ( [Out of Stock Rate], Stock[Line Origin] = "Carried over" )'),
    ("Assortment", "Lines Going Dormant", INT,
     "Lines with stock on the shelf, no January 2016 sales and no receipt in the last 120 days of 2016 (or none at all).",
     "COUNTROWS (\n    FILTER (\n        Stock,\n        Stock[End Units] > 0 && Stock[Jan Units] = 0\n"
     "            && ( ISBLANK ( Stock[Last Receipt] ) || Stock[Last Receipt] < DATE ( 2016, 12, 31 ) - 120 )\n    )\n)"),
    ("Assortment", "Dormant Stock Value", USD_AUTO, "Stock value of the lines going dormant.",
     "SUMX (\n    FILTER (\n        Stock,\n        Stock[End Units] > 0 && Stock[Jan Units] = 0\n"
     "            && ( ISBLANK ( Stock[Last Receipt] ) || Stock[Last Receipt] < DATE ( 2016, 12, 31 ) - 120 )\n    ),\n"
     "    Stock[Stock Cost]\n)"),
    ("Movement", "Stock Value Change", USD_AUTO, "Closing − opening units, at 2016 unit cost.",
     "SUMX ( Stock, ( Stock[End Units] - Stock[Begin Units] ) * Stock[Unit Cost] )"),
    ("Movement", "Stock Value Change %", PCT1, "This row's share of the stock value change across the visible rows.",
     "DIVIDE ( [Stock Value Change], CALCULATE ( [Stock Value Change], ALLSELECTED ( Item[Category] ) ) )"),
    ("Movement", "Funnel POs", INT,
     "Purchase orders still in the funnel at each step: raised → invoiced → invoice units and vendor match the "
     "receipts → approved → paid. Each step keeps the conditions of the steps before it. Every PO on a receiving "
     "line has an invoice in this data (a pipeline test checks it), so PO raised = Invoiced.",
     "VAR _inv = COUNTROWS ( Invoices )\n"
     "VAR _match = CALCULATE ( COUNTROWS ( Invoices ), Invoices[PO Matches Receipts] = TRUE () )\n"
     "VAR _appr = CALCULATE ( COUNTROWS ( Invoices ), Invoices[PO Matches Receipts] = TRUE (), Invoices[Approved] = TRUE () )\n"
     "VAR _paid = CALCULATE ( COUNTROWS ( Invoices ), Invoices[PO Matches Receipts] = TRUE (), Invoices[Approved] = TRUE (),\n"
     "    NOT ISBLANK ( Invoices[Pay Date] ) )\n"
     "RETURN\n    SWITCH (\n        SELECTEDVALUE ( 'Funnel Step'[Funnel Step] ),\n"
     "        \"PO raised\", _inv,\n        \"Invoiced\", _inv,\n        \"Matches receipts\", _match,\n"
     "        \"Approved\", _appr,\n        \"Paid\", _paid\n    )"),
    ("Movement", "Cover P95 by Category", "#,0",
     "95th percentile of days of cover among the selling lines (PERCENTILEX.INC); per category on a category row.",
     "PERCENTILEX.INC ( FILTER ( Stock, NOT ISBLANK ( Stock[Cover Days] ) ), Stock[Cover Days], 0.95 )"),
    ("Movement", "Outlier Lines", INT,
     "Lines with more days of cover than the P95 of their category (P95 taken over the selected stores, all items in "
     "the category).",
     "SUMX (\n    VALUES ( Item[Category] ),\n    VAR _cat = Item[Category]\n"
     "    VAR _p95 = CALCULATE ( [Cover P95 by Category], ALLSELECTED ( Store ), ALLSELECTED ( Item ), Item[Category] = _cat )\n"
     "    RETURN CALCULATE ( COUNTROWS ( FILTER ( Stock, Stock[Cover Days] > _p95 ) ) )\n)"),
    ("Movement", "Outlier Stock Value", USD_AUTO, "Stock value of the cover outliers (cover above the category P95).",
     "SUMX (\n    VALUES ( Item[Category] ),\n    VAR _cat = Item[Category]\n"
     "    VAR _p95 = CALCULATE ( [Cover P95 by Category], ALLSELECTED ( Store ), ALLSELECTED ( Item ), Item[Category] = _cat )\n"
     "    RETURN CALCULATE ( SUMX ( FILTER ( Stock, Stock[Cover Days] > _p95 ), Stock[Stock Cost] ) )\n)"),
    # ------------------------------------------------------------ Narrative
    ("Narrative", "Headline", None, "One-sentence summary that follows the slicers.",
     "VAR _v = [Order Value]\nVAR _x = [Excess Stock Value]\nRETURN\n"
     "    FORMAT ( [Lines To Order], \"#,0\" ) & \" lines need an order now: \" & FORMAT ( [Units To Order], \"#,0\" )\n"
     f"        & \" units, \" & {money('_v')} & \" at cost, for a \" & FORMAT ( [Service Level Selected], \"0%\" )\n"
     f"        & \" service level. Meanwhile \" & {money('_x')} & \" sits in stock beyond {OVERSTOCK_DAYS} days of cover or never sells.\""),
    ("Narrative", "Diagnosis", None,
     "The Diagnose story in one paragraph: out-of-stock rate, then the gap between class A and class C lines against "
     "the gap between short and long vendor lead times (split at the median lead time of the selling lines).",
     "VAR _all = [Out of Stock Rate]\n"
     "VAR _a = CALCULATE ( [Out of Stock Rate], ABC[ABC Key] = \"A\" )\n"
     "VAR _c = CALCULATE ( [Out of Stock Rate], ABC[ABC Key] = \"C\" )\n"
     "VAR _med = PERCENTILEX.INC ( FILTER ( Stock, Stock[Daily Forecast] > 0 ), Stock[Lead Days], 0.5 )\n"
     "VAR _short = CALCULATE ( [Out of Stock Rate], Stock[Lead Days] <= _med )\n"
     "VAR _long = CALCULATE ( [Out of Stock Rate], Stock[Lead Days] > _med )\n"
     "VAR _abcGap = _c - _a\nVAR _leadGap = _long - _short\n"
     "RETURN\n    FORMAT ( _all, \"0%\" ) & \" of the lines with demand were empty at the year-end count. Class C lines \"\n"
     "        & \"ran out \" & FORMAT ( _c, \"0%\" ) & \" of the time against \" & FORMAT ( _a, \"0%\" ) & \" for class A (\"\n"
     "        & FORMAT ( _abcGap * 100, \"0\" ) & \" points), while vendors slower than the median lead time of \"\n"
     "        & FORMAT ( _med, \"0.0\" ) & \" days add \" & FORMAT ( _leadGap * 100, \"0\" ) & \" points (\"\n"
     "        & FORMAT ( _long, \"0%\" ) & \" vs \" & FORMAT ( _short, \"0%\" ) & \"). \"\n"
     "        & IF ( ABS ( _abcGap ) > 2 * ABS ( _leadGap ), \"The stock-outs follow the item class, not the lead time: \"\n"
     "            & \"look at the C items in the tree, then drill through to one of them.\",\n"
     "            \"Item class and lead time both matter here: check the vendors in the tree.\" )"),
]


def pretty(col):
    return " ".join(w.upper() if w in ("id", "abc", "po") else w.capitalize() for w in col.split("_"))


def q(name):
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else f"'{name}'"


def tag(*parts):
    """Deterministic lineage tags, so a rebuild only diffs where the model really changed."""
    return str(uuid.uuid5(NS, "/".join(parts)))


def col_name(cfg, col):
    return cfg.get("names", {}).get(col) or (col if "m" in cfg else pretty(col))


def schema(con, mart):
    """(column, duckdb type) of a mart, plus the columns a Power Query merge adds to it."""
    cfg = next((c for m, c in TABLES.values() if m == mart), {})
    return [r[:2] for r in con.execute(f"describe mart.{mart}").fetchall()] + cfg.get("extra", [])


def dax_block(dax, indent=3):
    return [f"{T*indent}{l}" for l in dax.split("\n")]


def expr_lines(head, dax, indent):
    """`head = dax` on one line, or `head =` followed by the indented DAX when it spans lines."""
    return [f"{head} =", *dax_block(dax, indent)] if "\n" in dax else [f"{head} = {dax}"]


def m_source(mart, m_types, merge=None):
    if merge:
        return ["let",
                f'    Source = Parquet.Document(File.Contents(DataFolder & "{mart}.parquet")),',
                f"    Typed = Table.TransformColumnTypes(Source, {{{', '.join(m_types)}}}),",
                *[f"    {l}" for l in merge], "in", "    " + merge[-1].split(" = ")[0]]
    return ["let",
            f'    Source = Parquet.Document(File.Contents(DataFolder & "{mart}.parquet")),',
            f"    Typed = Table.TransformColumnTypes(Source, {{{', '.join(m_types)}}})",
            "in", "    Typed"]


def table_tmdl(con, name, mart, cfg, mode="import", source=None, cols=None, plain=False):
    """One table. The composite variant passes its storage mode, a PostgreSQL source builder (gets the M type
    list), an explicit column list, and plain=True (no calculated columns / calendars / hierarchies on missing columns)."""
    lines = [f"table {q(name)}", f"{T}lineageTag: {tag(name)}"]
    if cfg.get("date_table"):
        lines.append(f"{T}dataCategory: Time")
    if cfg.get("hidden_table"):
        lines.append(f"{T}isHidden")
    lines.append("")
    m_types = []
    extra = {c for c, _ in cfg.get("extra", [])}
    for col, dtype in cols or schema(con, mart):
        tm, mq = TYPES[dtype]
        if col not in extra:
            m_types.append(f'{{"{col}", {mq}}}')
        cname = col_name(cfg, col)
        lines += [f"{T}column {q(cname)}", f"{T*2}dataType: {tm}"]
        if col == cfg.get("key"):
            lines.append(f"{T*2}isKey")
        if col in cfg.get("hidden", []) or cfg.get("hidden_table"):
            lines.append(f"{T*2}isHidden")
        fmt = cfg.get("fmt", {}).get(col) or {"int64": "0", "dateTime": "dd mmm yyyy"}.get(tm)
        if fmt:
            lines.append(f"{T*2}formatString: {fmt}")
        if cat := cfg.get("category", {}).get(col):
            lines.append(f"{T*2}dataCategory: {cat}")
        lines += [f"{T*2}lineageTag: {tag(name, cname)}", f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: {col}"]
        if sort := cfg.get("sort", {}).get(col):
            lines.append(f"{T*2}sortByColumn: {q(col_name(cfg, sort))}")
        lines += [f"{T*2}{l}" for l in cfg.get("alternate", {}).get(col, [])]  # aggregation mapping
        lines.append("")
    for cname, dtype, dax, extra, *children in ([] if plain else CALC_COLUMNS.get(name, [])):
        lines += expr_lines(f"{T}column {q(cname)}", dax, 3)
        lines += [f"{T*2}dataType: {dtype}", *[f"{T*2}{e}" for e in extra],
                  f"{T*2}lineageTag: {tag(name, cname)}", f"{T*2}summarizeBy: none"]
        if sort := cfg.get("sort", {}).get(cname):
            lines.append(f"{T*2}sortByColumn: {q(sort)}")
        lines.append("")
        for child in children:
            lines += [f"{T}{l}" for l in child] + [""]
    names = {col_name(cfg, c) for c, _ in cols or schema(con, mart)}
    for h, levels in [h for h in cfg.get("hierarchies", []) if not plain or set(h[1]) <= names]:
        lines += [f"{T}hierarchy {q(h)}", f"{T*2}lineageTag: {tag(name, 'h', h)}", ""]
        for lv in levels:
            lines += [f"{T*2}level {q(lv)}", f"{T*3}lineageTag: {tag(name, 'h', h, lv)}", f"{T*3}column: {q(lv)}", ""]
    if cfg.get("date_table") and not plain:
        for cal, groups in CALENDARS:
            lines += [f"{T}calendar {q(cal)}", f"{T*2}lineageTag: {tag(name, 'cal', cal)}", ""]
            for unit, primary, assoc in groups:
                lines += [f"{T*2}calendarColumnGroup = {unit}", f"{T*3}primaryColumn: {q(primary)}"]
                lines += ([f"{T*3}associatedColumn: {q(assoc)}"] if assoc else []) + [""]
    lines += [f"{T}partition {q(name)} = m", f"{T*2}mode: {mode}", f"{T*2}source ="]
    m = source(m_types) if source else cfg["m"].split("\n") if "m" in cfg else m_source(mart, m_types, cfg.get("merge"))
    lines += [f"{T*4}{l}" for l in m]
    lines += ["", f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def expressions_tmdl(data_dir, raw_dir):
    """Two folder parameters, and the staging queries that other queries reference but nothing loads."""
    lines = []
    for name, value, desc in [("DataFolder", data_dir, "Folder with the Parquet marts (pipeline output)."),
                              ("RawFolder", raw_dir, "Folder with the raw CSV exports (Power Query tables).")]:
        lines += [f"/// {desc}",
                  f'expression {name} = "{value}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
                  f"{T}lineageTag: {tag('expr', name)}", "", f"{T}annotation PBI_ResultType = Text", ""]
    for name, m in SHARED_QUERIES.items():
        lines += [f"expression {q(name)} =", *[f"{T*2}{l}" for l in m.split("\n")],
                  f"{T}lineageTag: {tag('expr', name)}", "", f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def calculated_tmdl(name, dax, cols):
    lines = [f"table {q(name)}", f"{T}lineageTag: {tag(name)}", ""]
    for col, src, dtype, extra in cols:
        lines += [f"{T}column {q(col)}", f"{T*2}dataType: {dtype}", f"{T*2}lineageTag: {tag(name, col)}",
                  f"{T*2}summarizeBy: none", f"{T*2}isNameInferred", f"{T*2}sourceColumn: {src}"]
        lines += [f"{T*2}{e}" for e in extra] + [""]
    lines += [f"{T}partition {q(name)} = calculated", f"{T*2}mode: import", f"{T*2}source = {dax}", ""]
    return "\n".join(lines)


def calc_group_tmdl():
    t = "Time Intelligence"
    lines = [f"table {q(t)}", f"{T}lineageTag: {tag(t)}", "", f"{T}calculationGroup", f"{T*2}precedence: 10", ""]
    for i, (name, dax, fmt) in enumerate(TIME_ITEMS):
        lines += expr_lines(f"{T*2}calculationItem {q(name)}", dax, 4)
        lines.append(f"{T*3}ordinal: {i}")
        if fmt:
            lines.append(f"{T*3}formatStringDefinition = {fmt}")
        lines.append("")
    lines += [f"{T}column Period", f"{T*2}dataType: string", f"{T*2}lineageTag: {tag(t, 'Period')}",
              f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: Name", f"{T*2}sortByColumn: Ordinal", "",
              f"{T}column Ordinal", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
              f"{T*2}lineageTag: {tag(t, 'Ordinal')}", f"{T*2}summarizeBy: sum", f"{T*2}sourceColumn: Ordinal", ""]
    return "\n".join(lines)


def field_parameter_tmdl(t, items):
    """A field parameter table (columns or measures), as Desktop writes it."""
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
        *dax_block("{\n" + rows + "\n}", 4), ""])


TEXT_MEASURES = {"Headline", "Top Order Store in City", "ABC Dynamic", "ABC Static", "Diagnosis"}


def measures_tmdl(measures=None):
    lines = ["table _Measures", f"{T}lineageTag: {tag('_Measures')}", ""]
    for folder, name, fmt, desc, dax in measures or MEASURES:
        lines += [f"{T}/// {desc}"] + expr_lines(f"{T}measure {q(name)}", dax, 3)
        if fmt and fmt != USD_AUTO:
            lines.append(f"{T*2}formatString: {fmt}")
        lines += [f"{T*2}displayFolder: {folder}", f"{T*2}lineageTag: {tag('_Measures', 'm', name)}"]
        if name in TEXT_MEASURES:  # returns text: a number format would be wrong, so BPA is told on purpose
            lines += ["", f'{T*2}annotation BestPracticeAnalyzer_IgnoreRules = '
                      '{"RuleIDs":["PROVIDE_FORMAT_STRING_FOR_MEASURES","INTEGER_FORMATTING"]}']
        if fmt == USD_AUTO:  # dynamic format string (a child object, so after the properties)
            lines += ["", *expr_lines(f"{T*2}formatStringDefinition", fmt, 4)]
        lines.append("")
    lines += [f"{T}column Value", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
              f"{T*2}lineageTag: {tag('_Measures', 'Value')}", f"{T*2}summarizeBy: none", f"{T*2}isNameInferred",
              f"{T*2}sourceColumn: [Value]", "",
              f"{T}partition _Measures = calculated", f"{T*2}mode: import", f"{T*2}source = {{1}}", ""]
    return "\n".join(lines)


def functions_tmdl():
    lines = []
    for name, desc, body in FUNCTIONS:
        lines += [f"/// {desc}", *expr_lines(f"function {q(name)}", body, 2), f"{T}lineageTag: {tag('fn', name)}", ""]
    return "\n".join(lines)


def role_tmdl(name, r):
    lines = [f"role {q(name)}", f"{T}modelPermission: read", ""]
    hide_cols = r.get("hide_columns", {})
    for table in sorted(set(r["filters"]) | set(hide_cols)):
        dax = r["filters"].get(table)
        lines.append(f"{T}tablePermission {q(table)}" + (f" = {dax}" if dax else ""))
        lines += [""] + [f"{T*2}columnPermission {q(c)} = none" for c in hide_cols.get(table, [])] + [""]
    for table in r.get("hide_tables", []):
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
        key = re.sub(r"\W", "_", obj.lower())
        entities[key] = {"Definition": {"Binding": binding}, "State": "Authored",
                         "Terms": [{obj.lower(): {"State": "Authored"}}] + [{t: {"State": "Authored"}} for t in terms]}
    doc = {"Version": "1.0.0", "Language": "en-US", "DynamicImprovement": "HighConfidence", "Entities": entities}
    return "\n".join(["cultureInfo en-US", "", f"{T}linguisticMetadata = {json.dumps(doc, ensure_ascii=False)}",
                      f"{T*2}contentType: json", ""])


def store_size_column(con):
    """Store[Store Size]: a list group (what Desktop's "New group" writes) of stores in thirds by stock value."""
    rows = con.execute("select store, ntile(3) over (order by sum(stock_cost) desc) from mart.fact_stock "
                       "group by store order by store").fetchall()
    sizes = {1: "Large", 2: "Medium", 3: "Small"}
    arms = "".join(f"\n    Store[Store No] IN {{ {', '.join(str(s) for s, t in rows if t == k)} }}, \"{v}\","
                   for k, v in sizes.items())
    dax = f"SWITCH (\n    TRUE (),{arms}\n    \"Other\"\n)"
    return ("Store Size", "string", dax, [], grouping("Store", "Store No"))


def check(con):
    """Fail fast on name collisions, DAX references that don't resolve, and best-practice rule breaks."""
    for t, (mart, cfg) in TABLES.items():
        names = [col_name(cfg, c[0]).lower() for c in schema(con, mart)]
        assert len(names) == len(set(names)), f"duplicate column names in {t}: {names}"
    columns = {t: {col_name(cfg, c[0]) for c in schema(con, mart)} for t, (mart, cfg) in TABLES.items()}
    for t, ccs in CALC_COLUMNS.items():
        columns[t] |= {c[0] for c in ccs}
    columns |= {t: {c[0] for c in cols} for t, (_, cols) in CALCULATED.items()}
    columns |= {t: {t} for t in FIELD_PARAMS} | {"Time Intelligence": {"Period"}}
    measures = [m[1] for m in MEASURES]
    taken = {n.lower() for n in set().union(*columns.values(), columns)}
    clash = {m for m in measures if m.lower() in taken or measures.count(m) > 1}
    assert not clash, f"measure names collide with tables/columns: {clash}"
    tables = set(columns) | {c for c, _ in CALENDARS}
    fns = {f[0] for f in FUNCTIONS}
    dax_all = [(m[1], m[4]) for m in MEASURES] \
        + [(f"role {r}", d) for r, v in ROLES.items() for d in v["filters"].values()] \
        + [(f"table {n}", d) for n, (d, _) in CALCULATED.items()] + [(t, r) for t, it in FIELD_PARAMS.items() for _, r in it] \
        + [(f"item {n}", d) for n, d, _ in TIME_ITEMS] + [(f"function {n}", b) for n, _, b in FUNCTIONS] \
        + [(f"column {t}[{c[0]}]", c[2]) for t, ccs in CALC_COLUMNS.items() for c in ccs] + [("format", USD_AUTO)] \
        + [(f"query {n}", re.sub(r"(?m)^//.*$|\"[^\"]*\", ", "", d)) for n, d in DAX_QUERIES.items()]
    for name, dax in dax_all:
        dax = re.sub(r'"(?:[^"]|"")*"', '""', dax)
        for a, b, col in re.findall(r"(?:'([^']+)'|\b([A-Za-z_]+))\[([^\]]+)\]", dax):
            assert col in columns.get(a or b, ()), f"[{name}] references missing column {a or b}[{col}]"
        for ref in re.findall(r"(?<![\w'\]])\[([^\]]+)\]", dax):
            assert ref in measures or ref == "Value" or ref.startswith("@"), f"[{name}] references missing measure [{ref}]"
        for ref in re.findall(r"'([^'\n]+)'", re.sub(r"'[^'\n]+'\[[^\]]*\]", "", dax)):
            assert ref in tables, f"[{name}] references missing table/calendar '{ref}'"
        for fn in re.findall(r"(?<![\w.])([A-Z][A-Za-z]*\.[A-Za-z]+)\s*\(", dax):
            assert fn in fns or fn in ("NORM.S", "PERCENTILEX.INC"), f"[{name}] calls unknown function {fn}"
        assert dax.count("(") == dax.count(")"), f"[{name}] unbalanced parentheses"
    raw = {t: {c[0] for c in schema(con, mart)} for t, (mart, _) in TABLES.items()}
    for ft, fc, tt, tc, *_ in RELATIONSHIPS:
        assert fc in raw[ft] and tc in raw[tt], (ft, fc, tt, tc)
    for _, groups in CALENDARS:
        for _, p, a in groups:
            assert p in columns["Date"] and (a is None or a in columns["Date"]), (p, a)
    for t, (_, cfg) in TABLES.items():
        assert all(lv in columns[t] for _, lvs in cfg.get("hierarchies", []) for lv in lvs), t
    for r in ROLES.values():
        assert all(t in columns for t in [*r["filters"], *r.get("hide_tables", []), *r.get("hide_columns", {})])
        assert all(c in columns[t] for t, cs in r.get("hide_columns", {}).items() for c in cs)
    for p, ts in PERSPECTIVES.items():
        assert all(t in columns or t == "_Measures" for t in ts), p
    assert all(o in columns or o in measures for o in SYNONYMS), "synonym on an unknown object"
    # Fast pre-flight only; the full Tabular Editor BPA rule set runs in CI via tools/pbi-kit/bpa (bpa/project-rules.json)
    for folder, name, fmt, desc, _ in MEASURES:
        assert desc and desc.endswith("."), f"[{name}] needs a one-sentence description"
        assert fmt or name in TEXT_MEASURES, f"[{name}] numeric measure without a format string"
    for ft, fc, tt, tc, *o in RELATIONSHIPS:
        if o and o[0].get("many"):  # many-to-many: neither side is a key
            continue
        fcfg, tcfg = TABLES[ft][1], TABLES[tt][1]
        assert tcfg.get("key") == tc, f"relationship {ft}[{fc}] -> {tt}[{tc}] must point at a key column"
        assert fc in fcfg.get("hidden", []) or fcfg.get("date_table") or "date" in fc.lower() or "month" in fc, \
            f"foreign key {ft}[{fc}] should be hidden"
    for t, (mart, cfg) in TABLES.items():
        own = columns[t]
        for c, srt in cfg.get("sort", {}).items():
            assert col_name(cfg, srt) in own or srt in own, f"{t}[{c}] sorts by a missing column"
    print(f"  ✓ {len(measures)} measures, {len(FUNCTIONS)} functions, {len(TIME_ITEMS)} calc items: every reference "
          f"resolves; best-practice rules pass")


# ------------------------------------------------ composite variant (16-s2, 15-s6, 11-s1, 11-s2): Import + DQ + Dual
# A second semantic model on the real PostgreSQL copy of the till data (db `portfolio`, schema `inventory`, on the
# Mac Mini; Desktop reaches it over Tailscale and asks for the read-only pbi_reader login at connect time -- no
# password lives in this repo). The main StockRestock model stays all-Import so nothing built on it changes.
COMPOSITE_OUT = ROOT / "powerbi" / "Restock Komposit.SemanticModel"
PG_PARAMS = [("PgServer", "<db-server>", "PostgreSQL host: the Mac Mini's Tailscale address."),
             ("PgDatabase", "portfolio", "PostgreSQL database that holds the inventory schema.")]
DETAIL = "Daily Sales Detail"
# inventory.daily_sales_detail as created in PostgreSQL (raw till rows, Jan + part of Feb 2016). inventory_id and
# vendor_no exist there too but nothing in the variant uses them, so they are not selected (narrower SQL).
DETAIL_COLS = [("store", "INTEGER"), ("brand", "BIGINT"), ("sales_date", "DATE"), ("units", "INTEGER"),
               ("dollars", "DECIMAL"), ("price", "DECIMAL"), ("excise_tax", "DECIMAL")]
DETAIL_CFG = dict(
    hidden=["store", "brand", "sales_date", "units", "dollars"],  # facts are read through the measures
    names={"store": "Detail Store", "brand": "Detail Brand", "sales_date": "Detail Date",
           "units": "Units", "dollars": "Dollars", "price": "Price", "excise_tax": "Excise Tax"},
    fmt={"units": INT, "dollars": USD0, "price": USD2, "excise_tax": USD2})


def alt(summ, base):
    return ["alternateOf", f"{T}summarization: {summ}", f"{T}{'baseTable' if summ == 'count' else 'baseColumn'}: {base}"]


# Hidden Import aggregation at day × store × brand, grouped in PostgreSQL (Table.Group folds to GROUP BY).
AGG_COLS = [("sales_date", "DATE"), ("store", "INTEGER"), ("brand", "BIGINT"), ("units", "BIGINT"),
            ("dollars", "DECIMAL"), ("sales_rows", "BIGINT")]
AGG_CFG = dict(
    hidden_table=True,
    names={"sales_date": "Sales Date", "store": "Sales Store", "brand": "Sales Brand", "units": "Units Sold",
           "dollars": "Sales Dollars", "sales_rows": "Sales Rows"},
    fmt={"units": INT, "dollars": USD0, "sales_rows": INT},
    alternate={"sales_date": alt("groupBy", f"'{DETAIL}'.'Detail Date'"),
               "store": alt("groupBy", f"'{DETAIL}'.'Detail Store'"),
               "brand": alt("groupBy", f"'{DETAIL}'.'Detail Brand'"),
               "units": alt("sum", f"'{DETAIL}'.Units"), "dollars": alt("sum", f"'{DETAIL}'.Dollars"),
               "sales_rows": alt("count", f"'{DETAIL}'")})
AGG_M = """let
    Source = PostgreSQL.Database(PgServer, PgDatabase),
    Data = Source{[Schema = "inventory", Item = "daily_sales_detail"]}[Data],
    // select first, then group: both steps fold into one SELECT ... GROUP BY on the server
    Selected = Table.SelectColumns(Data, {"sales_date", "store", "brand", "units", "dollars"}),
    Grouped = Table.Group(Selected, {"sales_date", "store", "brand"}, {
        {"units", each List.Sum([units]), Int64.Type},
        {"dollars", each List.Sum([dollars]), Currency.Type},
        {"sales_rows", each Table.RowCount(_), Int64.Type}}),
    Typed = Table.TransformColumnTypes(Grouped, {{"sales_date", type date}, {"store", Int64.Type}, {"brand", Int64.Type}})
in
    Typed"""
# (from table, from column, to table, to column) by model name. Detail → Item crosses source groups
# (DirectQuery → Import), so the engine treats it as a LIMITED relationship (11-s2); every other one is regular.
COMPOSITE_RELS = [
    (DETAIL, "Detail Date", "Date", "Date"), (DETAIL, "Detail Store", "Store", "Store No"),
    (DETAIL, "Detail Brand", "Item", "Brand No"),
    ("Daily Sales", "Sales Date", "Date", "Date"), ("Daily Sales", "Sales Store", "Store", "Store No"),
    ("Daily Sales", "Sales Brand", "Item", "Brand No"),
    ("Stock", "Stock Store", "Store", "Store No"), ("Stock", "Stock Brand", "Item", "Brand No"),
]
COMPOSITE_MEASURES = [
    ("Composite", "Detail Units", INT, "Units sold on the till rows; answered from the hidden Daily Sales aggregation when the query fits day × store × brand, otherwise sent to PostgreSQL.",
     "SUM ( 'Daily Sales Detail'[Units] )"),
    ("Composite", "Detail Dollars", USD0, "Sales dollars on the till rows (aggregation hit or DirectQuery).",
     "SUM ( 'Daily Sales Detail'[Dollars] )"),
    ("Composite", "Detail Rows", INT, "Number of till rows (aggregation hit through the count mapping, or DirectQuery).",
     "COUNTROWS ( 'Daily Sales Detail' )"),
    ("Composite", "Stock End Units", INT, "Units on hand at the 31 Dec 2016 count, from the Import Stock table next to the DirectQuery facts.",
     "SUM ( Stock[End Units] )"),
    ("Limited Relationship", "Units At Item Cost", USD0,
     "Till units valued at the item's unit cost. RELATED ( Item[Item Unit Cost] ) inside SUMX over the till rows fails because the DirectQuery-to-Import relationship is limited, so this iterates Item and lets context transition filter the till rows.",
     "SUMX ( Item, Item[Item Unit Cost] * [Detail Units] )"),
    ("Limited Relationship", "Detail Units Via TREATAS", INT,
     "Detail Units with the Item filter moved by TREATAS (a virtual relationship) instead of the limited physical one; same numbers, the usual workaround when a relationship can't be used.",
     "CALCULATE (\n    [Detail Units],\n    REMOVEFILTERS ( Item ),\n    TREATAS ( VALUES ( Item[Brand No] ), 'Daily Sales Detail'[Detail Brand] )\n)"),
    ("Limited Relationship", "Units Without Item", INT,
     "Till units whose brand has no Item row. A limited relationship adds no blank Item row, so these units would drop out of every visual grouped by Item; 0 here because the database has no orphan brands.",
     "CALCULATE ( [Detail Units], REMOVEFILTERS ( Item ) )\n    - CALCULATE ( SUMX ( VALUES ( Item[Brand No] ), [Detail Units] ), REMOVEFILTERS ( Item ) )"),
]


def pg_m(item, m_types):
    """PostgreSQL source: navigate, select the columns (folds), set types."""
    cols = ", ".join(f'"{c}"' for c in re.findall(r'\{"([^"]+)",', ", ".join(m_types)))
    return ["let", "    Source = PostgreSQL.Database(PgServer, PgDatabase),",
            f'    Data = Source{{[Schema = "inventory", Item = "{item}"]}}[Data],',
            f"    Selected = Table.SelectColumns(Data, {{{cols}}}),",
            f"    Typed = Table.TransformColumnTypes(Selected, {{{', '.join(m_types)}}})", "in", "    Typed"]


def write_composite(con, data_dir):
    plain = lambda t: {k: v for k, v in TABLES[t][1].items() if k not in ("merge", "extra")}
    no_extra = lambda mart: [r[:2] for r in con.execute(f"describe mart.{mart}").fetchall()]
    tables = {
        DETAIL: table_tmdl(con, DETAIL, None, DETAIL_CFG, "directQuery", lambda t: pg_m("daily_sales_detail", t),
                           DETAIL_COLS, plain=True),
        "Daily Sales": table_tmdl(con, "Daily Sales", None, AGG_CFG, "import", lambda t: AGG_M.split("\n"),
                                  AGG_COLS, plain=True),
        **{t: table_tmdl(con, t, mart, plain(t), "dual", lambda ty, m=mart: pg_m(m, ty), no_extra(mart), plain=True)
           for t, mart in (("Date", "dim_date"), ("Store", "dim_store"))},
        # Item stays Import on purpose: that is what makes Detail → Item a limited relationship (Dual would make it regular)
        "Item": table_tmdl(con, "Item", "dim_item", plain("Item"), "import", lambda ty: pg_m("dim_item", ty),
                           no_extra("dim_item"), plain=True),
        "Stock": table_tmdl(con, "Stock", "fact_stock", TABLES["Stock"][1], plain=True),
        "_Measures": measures_tmdl(COMPOSITE_MEASURES),
    }
    # every column / measure reference in the variant's DAX and relationships must resolve
    cols = {t: set(re.findall(r"^\tcolumn ('[^']+'|\S+)", txt, re.M)) for t, txt in tables.items()}
    cols = {t: {c.strip("'") for c in cs} for t, cs in cols.items()}
    names = {m[1] for m in COMPOSITE_MEASURES}
    for _, n, _, _, dax in COMPOSITE_MEASURES:
        for a, b, c in re.findall(r"(?:'([^']+)'|\b([A-Za-z_]+))\[([^\]]+)\]", dax):
            assert c in cols[a or b], f"[{n}] references missing column {a or b}[{c}]"
        assert all(r in names for r in re.findall(r"(?<![\w'\]])\[([^\]]+)\]", dax)), n
    assert all(fc in cols[ft] and tc in cols[tt] for ft, fc, tt, tc in COMPOSITE_RELS)
    assert all(re.search(r"^\t\tmode: \w+", txt, re.M) for txt in tables.values())

    shutil.rmtree(COMPOSITE_OUT / "definition", ignore_errors=True)
    d = COMPOSITE_OUT / "definition"
    (d / "tables").mkdir(parents=True)
    (COMPOSITE_OUT / "definition.pbism").write_text((OUT / "definition.pbism").read_text())
    (COMPOSITE_OUT / ".platform").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",\n'
        '  "metadata": {"type": "SemanticModel", "displayName": "Restock Komposit"},\n'
        f'  "config": {{"version": "2.0", "logicalId": "{tag("composite", "model")}"}}\n}}\n')
    (d / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1702\n")
    (d / "model.tmdl").write_text("\n".join([
        "model Model", f"{T}culture: en-US", f"{T}defaultPowerBIDataSourceVersion: powerBI_V3",
        f"{T}discourageImplicitMeasures", f"{T}sourceQueryCulture: en-US", f"{T}dataAccessOptions",
        f"{T*2}legacyRedirects", f"{T*2}returnErrorValuesAsNull", "",
        "annotation __PBI_TimeIntelligenceEnabled = 0", "", *[f"ref table {q(t)}" for t in tables], ""]))
    expr = []
    for name, value, desc in [*PG_PARAMS, ("DataFolder", data_dir, "Folder with the Parquet marts (Stock).")]:
        expr += [f"/// {desc}",
                 f'expression {name} = "{value}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
                 f"{T}lineageTag: {tag('expr', name)}", "", f"{T}annotation PBI_ResultType = Text", ""]
    (d / "expressions.tmdl").write_text("\n".join(expr))
    for name, text in tables.items():
        (d / "tables" / f"{name}.tmdl").write_text(text)
    rel = []
    for ft, fc, tt, tc in COMPOSITE_RELS:
        rel += [f"relationship {tag('crel', ft, fc)}", f"{T}fromColumn: {q(ft)}.{q(fc)}", f"{T}toColumn: {q(tt)}.{q(tc)}", ""]
    (d / "relationships.tmdl").write_text("\n".join(rel))
    print(f"  ✓ wrote {COMPOSITE_OUT.relative_to(ROOT)} ({len(tables)} tables, {len(COMPOSITE_RELS)} relationships, "
          f"{len(COMPOSITE_MEASURES)} measures; modes: directQuery / dual / import)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data" / "marts"))
    ap.add_argument("--raw-dir", default=str(ROOT / "data" / "raw"))
    args = ap.parse_args()
    folder = lambda p: p.rstrip("/\\") + ("\\" if "\\" in p else "/")
    data_dir, raw_dir = folder(args.data_dir), folder(args.raw_dir)

    con = duckdb.connect(str(ROOT / "data" / "warehouse" / "inventory.duckdb"), read_only=True)
    CALC_COLUMNS.setdefault("Store", []).append(store_size_column(con))
    check(con)
    shutil.rmtree(OUT / "definition", ignore_errors=True)
    d = OUT / "definition"
    for sub in ("tables", "roles", "perspectives", "cultures"):
        (d / sub).mkdir(parents=True)

    (OUT / "definition.pbism").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",\n'
        '  "version": "4.2",\n  "settings": {}\n}\n')
    (OUT / ".platform").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",\n'
        '  "metadata": {"type": "SemanticModel", "displayName": "StockRestock"},\n'
        '  "config": {"version": "2.0", "logicalId": "8b2e4d71-5c90-4f3a-9e16-2a7d0c5b83f4"}\n}\n')
    # 1702: user-defined functions and calendars need the current compatibility level.
    (d / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1702\n")
    all_tables = [*TABLES, *CALCULATED, "Time Intelligence", *FIELD_PARAMS, "_Measures"]
    (d / "model.tmdl").write_text("\n".join([
        "model Model", f"{T}culture: en-US", f"{T}defaultPowerBIDataSourceVersion: powerBI_V3",
        f"{T}discourageImplicitMeasures", f"{T}sourceQueryCulture: en-US", f"{T}dataAccessOptions",
        f"{T*2}legacyRedirects", f"{T*2}returnErrorValuesAsNull", "",
        "annotation __PBI_TimeIntelligenceEnabled = 0", "",
        *[f"ref table {q(t)}" for t in all_tables], "",
        *[f"ref role {q(r)}" for r in ROLES], "",
        *[f"ref perspective {q(p)}" for p in PERSPECTIVES], "",
        "ref cultureInfo en-US", ""]))
    (d / "expressions.tmdl").write_text(expressions_tmdl(data_dir, raw_dir))
    (d / "functions.tmdl").write_text(functions_tmdl())

    for name, (mart, cfg) in TABLES.items():
        (d / "tables" / f"{name}.tmdl").write_text(table_tmdl(con, name, mart, cfg))
    for name, (dax, cols) in CALCULATED.items():
        (d / "tables" / f"{name}.tmdl").write_text(calculated_tmdl(name, dax, cols))
    (d / "tables" / "Time Intelligence.tmdl").write_text(calc_group_tmdl())
    for t, items in FIELD_PARAMS.items():
        (d / "tables" / f"{t}.tmdl").write_text(field_parameter_tmdl(t, items))
    (d / "tables" / "_Measures.tmdl").write_text(measures_tmdl())
    for name, r in ROLES.items():
        (d / "roles" / f"{name}.tmdl").write_text(role_tmdl(name, r))
    for name, tables in PERSPECTIVES.items():
        (d / "perspectives" / f"{name}.tmdl").write_text(perspective_tmdl(name, tables))
    (d / "cultures" / "en-US.tmdl").write_text(culture_tmdl())

    rel = []
    for ft, fc, tt, tc, *o in RELATIONSHIPS:
        o = o[0] if o else {}
        rel += [f"relationship {tag('rel', ft, fc)}"]
        rel += [f"{T}isActive: false"] if o.get("active") is False else []
        rel += [f"{T}crossFilteringBehavior: bothDirections"] if o.get("both") else []
        rel += [f"{T}fromCardinality: many", f"{T}toCardinality: many"] if o.get("many") else []
        rel += [f"{T}fromColumn: {q(ft)}.{q(col_name(TABLES[ft][1], fc))}",
                f"{T}toColumn: {q(tt)}.{q(col_name(TABLES[tt][1], tc))}", ""]
    (d / "relationships.tmdl").write_text("\n".join(rel))
    dq = OUT / "DAXQueries"
    shutil.rmtree(dq, ignore_errors=True)
    dq.mkdir()
    for fname, text in DAX_QUERIES.items():
        (dq / f"{fname}.dax").write_text(text)
    for f in sorted((ROOT / "dax").glob("*")):  # asserted CI tests (tools/pbi-kit/dax-tests)
        shutil.copy(f, dq / f.name)
    print(f"  ✓ wrote {OUT.relative_to(ROOT)} ({len(all_tables)} tables, {len(RELATIONSHIPS)} relationships, "
          f"{len(FUNCTIONS)} functions, {len(CALENDARS)} calendar, {len(ROLES)} roles, {len(PERSPECTIVES)} perspectives)")
    write_composite(con, data_dir)


if __name__ == "__main__":
    main()
