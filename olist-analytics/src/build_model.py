"""Generate the Power BI semantic model (TMDL) from the DuckDB mart schema.

    uv run python src/build_model.py [--data-dir PATH]

Column types come straight from DuckDB, so the model never drifts from the SQL.
--data-dir is the folder Power BI will read the Parquet marts from (defaults to
this repo's data/marts). Change it later in Power BI via Transform data > Edit parameters.
"""
import argparse
import json
import re
import shutil
import uuid
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "powerbi" / "Olist.SemanticModel"
T = "\t"
NS = uuid.UUID("3c6e0f4a-9b21-4d7e-8a53-1f2b6c7d8e90")

TYPES = {  # duckdb -> (tmdl dataType, Power Query type)
    "BIGINT": ("int64", "Int64.Type"), "INTEGER": ("int64", "Int64.Type"), "HUGEINT": ("int64", "Int64.Type"),
    "SMALLINT": ("int64", "Int64.Type"), "TINYINT": ("int64", "Int64.Type"), "UBIGINT": ("int64", "Int64.Type"),
    "DOUBLE": ("double", "type number"), "FLOAT": ("double", "type number"),
    "DATE": ("dateTime", "type date"), "TIMESTAMP": ("dateTime", "type datetime"),
    "BOOLEAN": ("boolean", "type logical"), "VARCHAR": ("string", "type text"),
}

BIG5 = ["Home & Living", "Leisure, Kids & Culture", "Electronics & Appliances", "Health & Beauty", "Fashion & Accessories"]

# table name -> (mart table, config)
TABLES = {
    "Date": ("dim_date", dict(
        key="date", date_table=True,
        sort={"month_name": "month_no", "year_month": "year_month_key", "weekday_name": "weekday_no",
              "year_quarter": "year_quarter_key"},
        hidden=["month_no", "year_month_key", "weekday_no", "year_quarter_key"],
        fmt={"date": "dd mmm yyyy", "month_start": "mmm yyyy"},
        hier=[("Calendar", ["Year", "Quarter", "Month Name"])],
        # calendar-based time intelligence (compat 1702): (time unit, primary column, associated columns)
        calendars=[("Gregorian", [("year", "Year", []), ("quarter", "Year Quarter Key", ["Year Quarter"]),
                                  ("month", "Year Month Key", ["Year Month", "Month Start"]),
                                  ("monthOfYear", "Month No", ["Month Name"]), ("date", "Date", [])])])),
    "Customer": ("dim_customer", dict(
        key="customer_unique_id",
        sort={"rfm_segment": "rfm_segment_order"},
        hidden=["customer_unique_id", "rfm_segment_order", "lat", "lng", "r_score", "f_score", "m_score"],
        category={"state_geo": "StateOrProvince", "city": "City", "lat": "Latitude", "lng": "Longitude"},
        fmt={"lifetime_value": "\\R$ #,0.00", "avg_review": "0.00", "predicted_clv_12m": "\\R$ #,0.00"},
        names={"avg_review": "Lifetime Avg Review", "predicted_clv_12m": "Predicted CLV 12M"},
        hier=[("Geo", ["Region", "State Name", "City"])],
        # 3-s11: a calculated column is fine here: a static yes/no attribute for a slicer, two values, compresses to nothing
        calc=[("Is Repeat", "Customer[Valid Orders] > 1", "boolean", {})])),
    "Seller": ("dim_seller", dict(
        key="seller_id", hidden=["seller_id", "lat", "lng"],
        category={"city": "City", "lat": "Latitude", "lng": "Longitude"},
        fmt={"lifetime_revenue": "\\R$ #,0", "lifetime_late_rate": "0.0%", "lifetime_avg_review": "0.00"},
        # 3-s10: RELATEDTABLE walks from the seller row to its order items
        calc=[("Distinct Products", "COUNTROWS ( DISTINCT ( SELECTCOLUMNS ( RELATEDTABLE ( 'Order Items' ), \"p\", "
               "'Order Items'[Product Id] ) ) )", "int64", {})])),
    "Product": ("dim_product", dict(
        key="product_id", sort={"weight_band": "weight_band_order"},
        hidden=["product_id", "weight_band_order"],
        hier=[("Category Tree", ["Category Group", "Category"])],
        # 16-s4: a Desktop "group" (5 largest groups by revenue, the rest Other), written the way Desktop stores it
        calc=[("Category Group (Big 5)", "SWITCH ( TRUE (),\n    Product[Category Group] IN { " + ", ".join(f'\"{g}\"' for g in BIG5)
               + " }, Product[Category Group],\n    \"Other\" )", "string",
               {"meta": {"version": 0, "groupedColumns": [{"Column": {"Expression": {"SourceRef": {"Entity": "Product"}},
                                                                     "Property": "Category Group"}}]}})])),
    "Orders": ("fact_orders", dict(
        sort={"delivery_bucket": "delivery_bucket_order"},
        key="order_id", hidden=["customer_unique_id", "delivery_bucket_order", "order_date", "delivered_date", "estimated_date"],
        # 16-s12: purchased_at is a datetime with ~99k distinct values (the costliest column to store); Order Date +
        # Order Hour already carry it, so it stays in SQL and out of the model
        drop=["purchased_at"],
        # 16-s4: a Desktop bin (size 3 days) on Delay Days
        calc=[("Delay Days (bins)", "IF ( ISBLANK ( Orders[Delay Days] ), BLANK (), INT ( Orders[Delay Days] / 3 ) * 3 )", "int64",
               {"meta": {"version": 0, "groupedColumns": [{"Column": {"Expression": {"SourceRef": {"Entity": "Orders"}},
                                                                     "Property": "Delay Days"}}],
                         "binningMetadata": {"binSize": {"value": 3, "unit": 0}}}}),
              # 10-s17: two-class target for the key influencers visual
              ("Review Outcome", "IF ( ISBLANK ( Orders[Review Score] ), BLANK (), IF ( Orders[Review Score] <= 2, \"Bad (1–2 ★)\", \"OK (3–5 ★)\" ) )",
               "string", {})],
        fmt={"items_value": "\\R$ #,0.00", "freight_value": "\\R$ #,0.00", "paid_value": "\\R$ #,0.00"})),
    "Order Items": ("fact_order_items", dict(
        sort={"distance_band": "distance_band_order"},
        hidden=["product_id", "seller_id", "customer_unique_id", "order_date", "distance_band_order"],
        fmt={"price": "\\R$ #,0.00", "freight": "\\R$ #,0.00", "freight_share": "0.0%"})),
    "Payments": ("fact_payments", dict(
        sort={"installment_band": "installment_band_order"},
        hidden=["order_date", "installment_band_order"], fmt={"payment_value": "\\R$ #,0.00"}, names={"payment_value": "Amount"})),
    "Cohort Retention": ("cohort_retention", dict(fmt={"retention_rate": "0.00%", "cohort_month": "mmm yyyy"})),
    "Seller Monthly": ("seller_monthly", dict(
        hidden=["seller_id", "month_start"], fmt={"revenue": "\\R$ #,0", "late_rate": "0.0%"},
        names={"orders": "Monthly Orders", "revenue": "Monthly Revenue", "avg_review": "Monthly Avg Review", "late_rate": "Monthly Late Rate"})),
    "Freight Lanes": ("freight_lanes", dict(fmt={"avg_freight": "\\R$ #,0.00", "avg_freight_share": "0.0%", "late_rate": "0.0%"},
        names={"avg_delivery_days": "Lane Delivery Days"},
        # 18-s8: region of each end of the lane, for the Sankey (seller region -> customer region). LOOKUPVALUE is safe:
        # every row of a state carries the same region, so the lookup never meets two different answers.
        # The 'Seller · ' / 'Customer · ' prefix keeps the two ends apart, so the Sankey has no self-loops (SE -> SE).
        calc=[("Seller Region", "\"Seller · \" & LOOKUPVALUE ( Seller[Region], Seller[State], 'Freight Lanes'[Seller State] )", "string", {}),
              ("Customer Region", "\"Customer · \" & LOOKUPVALUE ( Customer[Region], Customer[State], 'Freight Lanes'[Customer State] )", "string", {})])),
    # 11-s4: bridge between Seller and Orders (an order can hold several sellers)
    "Order Seller": ("order_seller", dict(table_hidden=True, hidden=["order_id", "seller_id"])),
    # 6-s14: a daily snapshot, semi-additive over dates
    "Backlog Daily": ("fact_backlog_daily", dict(hidden=["date"], names={"in_transit": "In Transit Snapshot"})),
    # 10-s10: seller tier 2017 -> 2018 (A/B/C/New -> A/B/C/Churned), pre-counted in SQL (sql/30_analytics.sql)
    "Seller Tier Transition": ("seller_tier_transition", dict(
        sort={"tier_2017": "tier_2017_order", "tier_2018": "tier_2018_order"}, hidden=["tier_2017_order", "tier_2018_order"])),
    # 10-s15: ETS forecast Sep-Dec 2018 with 80/95% intervals (src/predict.py, statsmodels)
    "Revenue Forecast": ("revenue_forecast", dict(
        hidden=["month_start"], fmt={c: "\\R$ #,0" for c in ("forecast_revenue", "lower_80", "upper_80", "lower_95", "upper_95")},
        names={"lower_80": "Lower 80", "upper_80": "Upper 80", "lower_95": "Lower 95", "upper_95": "Upper 95"})),
}

# (from table, from col, to table, to col[, options]). Every relationship is written with its cardinality and filter
# direction spelled out (2-s5). Options: active=False, both=True (filters both ways), m2m=True (many-to-many).
RELATIONSHIPS = [
    ("Orders", "order_date", "Date", "date"),
    ("Orders", "customer_unique_id", "Customer", "customer_unique_id"),
    ("Order Items", "order_date", "Date", "date"),
    ("Order Items", "customer_unique_id", "Customer", "customer_unique_id"),
    ("Order Items", "product_id", "Product", "product_id"),
    ("Order Items", "seller_id", "Seller", "seller_id"),
    ("Payments", "order_id", "Orders", "order_id"),  # Date/Customer filter payments through Orders
    ("Seller Monthly", "seller_id", "Seller", "seller_id"),
    ("Seller Monthly", "month_start", "Date", "date"),
    # role-playing Date (2-s8/2-s9): purchase date is active, delivered and promised dates wait for USERELATIONSHIP
    ("Orders", "delivered_date", "Date", "date", {"active": False}),
    ("Orders", "estimated_date", "Date", "date", {"active": False}),
    # 11-s3: the plan is set per category group and month; neither side is unique, so many-to-many (a limited relationship)
    ("Category Plan", "Category Group", "Product", "category_group", {"m2m": True}),
    ("Category Plan", "Month Start", "Date", "month_start", {"m2m": True}),
    # 11-s4: bridge. Seller filters the bridge, the bridge filters Orders both ways so a seller reaches its orders
    ("Order Seller", "seller_id", "Seller", "seller_id"),
    ("Order Seller", "order_id", "Orders", "order_id", {"both": True}),
    ("Backlog Daily", "date", "Date", "date"),
    ("Revenue Forecast", "month_start", "Date", "date"),
]
WHY = {  # README relationship table: why each relationship is the way it is
    ("Orders", "order_date"): "Purchase date drives every trend",
    ("Orders", "delivered_date"): "Inactive: USERELATIONSHIP for 'by delivery month'",
    ("Orders", "estimated_date"): "Inactive: USERELATIONSHIP for 'by promise month'",
    ("Payments", "order_id"): "Date and customer filters reach payments through the order",
    ("Category Plan", "Category Group"): "Plan grain is the group, not the product: many-to-many",
    ("Category Plan", "Month Start"): "Plan grain is the month, not the day: many-to-many",
    ("Order Seller", "order_id"): "Both directions only on the bridge, so a seller filter reaches order-level facts",
}

BRL0, BRL2, PCT1, NUM0, NUM1, NUM2 = "\\R$ #,0", "\\R$ #,0.00", "0.0%", "#,0", "#,0.0", "0.00"

# New / returning only mean something against a period. Over the whole extract every customer's first order is
# inside it, so those measures fall back to the last quarter with data (Period Counted says which).
PERIOD_VARS = ("VAR _full = MIN ( 'Date'[Date] ) = CALCULATE ( MIN ( 'Date'[Date] ), REMOVEFILTERS ( 'Date' ) )\n"
               "    && MAX ( 'Date'[Date] ) = CALCULATE ( MAX ( 'Date'[Date] ), REMOVEFILTERS ( 'Date' ) )\n"
               "VAR _lastData = CALCULATE ( MAX ( 'Order Items'[Order Date] ), REMOVEFILTERS (), 'Order Items'[Is Valid] = TRUE () )\n"
               "VAR _lastq = LOOKUPVALUE ( 'Date'[Year Quarter], 'Date'[Date], _lastData )\n")


def fallback(expr):
    """expr over the selected dates, or over the last quarter with data when no date filter narrows the extract."""
    return (PERIOD_VARS + "RETURN\n    IF ( _full,\n        CALCULATE ( " + expr + ", REMOVEFILTERS ( 'Date' ), "
            "'Date'[Year Quarter] = _lastq ),\n        " + expr + " )")


# (folder, name, format, description, DAX)
MEASURES = [
    # ---------------------------------------------------------------- Sales
    ("Sales", "Revenue", BRL0, "Item price of valid (not canceled/unavailable) orders, excluding freight.",
     "CALCULATE ( SUM ( 'Order Items'[Price] ), 'Order Items'[Is Valid] = TRUE () )"),
    ("Sales", "Freight Total", BRL0, "Freight charged on valid order items.",
     "CALCULATE ( SUM ( 'Order Items'[Freight] ), 'Order Items'[Is Valid] = TRUE () )"),
    ("Sales", "GMV", BRL0, "Gross merchandise value = revenue + freight.", "[Revenue] + [Freight Total]"),
    ("Sales", "Order Count", NUM0, "Distinct valid orders.",
     "CALCULATE ( DISTINCTCOUNT ( 'Order Items'[Order Id] ), 'Order Items'[Is Valid] = TRUE () )"),
    ("Sales", "Items Sold", NUM0, "Valid order items.",
     "CALCULATE ( COUNTROWS ( 'Order Items' ), 'Order Items'[Is Valid] = TRUE () )"),
    ("Sales", "AOV", BRL2, "Average order value (revenue per order).", "DIVIDE ( [Revenue], [Order Count] )"),
    ("Sales", "Avg Item Price", BRL2, None, "DIVIDE ( [Revenue], [Items Sold] )"),
    ("Sales", "Items per Order", NUM2, None, "DIVIDE ( [Items Sold], [Order Count] )"),
    ("Sales", "Freight Share %", PCT1, "Freight as a share of GMV.", "DIVIDE ( [Freight Total], [GMV] )"),
    ("Sales", "Revenue Share %", PCT1, "Share of revenue within the current visual selection.",
     "DIVIDE ( [Revenue], CALCULATE ( [Revenue], ALLSELECTED () ) )"),
    ("Sales", "Revenue 3M Avg", BRL0, "Rolling 3-month average of monthly revenue.",
     "VAR _end = MAX ( 'Date'[Date] )\n"
     "RETURN\n"
     "    DIVIDE (\n"
     "        CALCULATE ( [Revenue], DATESINPERIOD ( 'Date'[Date], _end, -3, MONTH ) ),\n"
     "        CALCULATE ( DISTINCTCOUNT ( 'Date'[Month Start] ), DATESINPERIOD ( 'Date'[Date], _end, -3, MONTH ) )\n"
     "    )"),
    # ---------------------------------------------------- Time intelligence
    ("Time Intelligence", "Revenue PY", BRL0,
     "Revenue in the same period last year, only when one year is in context, and only up to the last date with data (so a partial year is compared like-for-like).",
     "VAR _lastData = CALCULATE ( MAX ( 'Order Items'[Order Date] ), REMOVEFILTERS (), 'Order Items'[Is Valid] = TRUE () )\n"
     "VAR _dates = FILTER ( VALUES ( 'Date'[Date] ), 'Date'[Date] <= _lastData )\n"
     "RETURN IF ( HASONEVALUE ( 'Date'[Year] ), CALCULATE ( [Revenue], SAMEPERIODLASTYEAR ( _dates ) ) )"),
    ("Time Intelligence", "Revenue YoY %", PCT1, None,
     "VAR _py = [Revenue PY]\nRETURN IF ( NOT ISBLANK ( _py ), DIVIDE ( [Revenue] - _py, _py ) )"),
    ("Time Intelligence", "Revenue YTD", BRL0, None, "TOTALYTD ( [Revenue], 'Date'[Date] )"),
    ("Time Intelligence", "Revenue PM", BRL0, None, "CALCULATE ( [Revenue], DATEADD ( 'Date'[Date], -1, MONTH ) )"),
    ("Time Intelligence", "Revenue MoM %", PCT1, None,
     "VAR _pm = [Revenue PM]\nRETURN IF ( NOT ISBLANK ( _pm ), DIVIDE ( [Revenue] - _pm, _pm ) )"),
    ("Time Intelligence", "Orders PY", NUM0, "Same rules as Revenue PY.",
     "VAR _lastData = CALCULATE ( MAX ( 'Order Items'[Order Date] ), REMOVEFILTERS (), 'Order Items'[Is Valid] = TRUE () )\n"
     "VAR _dates = FILTER ( VALUES ( 'Date'[Date] ), 'Date'[Date] <= _lastData )\n"
     "RETURN IF ( HASONEVALUE ( 'Date'[Year] ), CALCULATE ( [Order Count], SAMEPERIODLASTYEAR ( _dates ) ) )"),
    ("Time Intelligence", "Orders YoY %", PCT1, None,
     "VAR _py = [Orders PY]\nRETURN IF ( NOT ISBLANK ( _py ), DIVIDE ( [Order Count] - _py, _py ) )"),
    ("Time Intelligence", "Revenue YoY Label", None, "Card subtitle: ▲/▼ change vs same period last year.",
     "VAR _g = [Revenue YoY %]\n"
     "RETURN\n"
     "    IF ( ISBLANK ( _g ), \"no prior-year data\",\n"
     "        IF ( _g >= 0, \"▲ \", \"▼ \" ) & FORMAT ( ABS ( _g ), \"0.0%\" ) & \" vs PY\" )"),
    ("Time Intelligence", "Orders YoY Label", None, None,
     "VAR _g = [Orders YoY %]\n"
     "RETURN\n"
     "    IF ( ISBLANK ( _g ), \"no prior-year data\",\n"
     "        IF ( _g >= 0, \"▲ \", \"▼ \" ) & FORMAT ( ABS ( _g ), \"0.0%\" ) & \" vs PY\" )"),
    ("Time Intelligence", "Revenue YoY Color", None, "Green when revenue grows vs PY, red when it shrinks.",
     "IF ( [Revenue YoY %] < 0, \"#D03B3B\", \"#0CA30C\" )"),
    ("Time Intelligence", "Orders YoY Color", None, None,
     "IF ( [Orders YoY %] < 0, \"#D03B3B\", \"#0CA30C\" )"),
    ("Sales", "Orders Placed", NUM0, "Valid orders counted on the Orders table (use with order-level attributes like hour or status).",
     "CALCULATE ( COUNTROWS ( Orders ), Orders[Is Valid] = TRUE () )"),
    # ------------------------------------------------------------ Customers
    ("Customers", "Customers", NUM0, "Distinct customers (customer_unique_id) with a valid order.",
     "CALCULATE ( DISTINCTCOUNT ( 'Order Items'[Customer Unique Id] ), 'Order Items'[Is Valid] = TRUE () )"),
    ("Customers", "Revenue per Customer", BRL2, None, "DIVIDE ( [Revenue], [Customers] )"),
    ("Customers", "New Customers", NUM0,
     "Customers placing their first-ever order in the period. With no date filter the period is the whole extract, where "
     "everyone is new, so it falls back to the last quarter with data (Period Counted says which).",
     fallback("CALCULATE ( COUNTROWS ( Orders ), Orders[Customer Order Seq] = 1, Orders[Is Valid] = TRUE () )")),
    ("Customers", "Returning Customers", NUM0,
     "Customers in the period who had ordered before it (same last-quarter fallback as New Customers).",
     fallback("CALCULATE ( DISTINCTCOUNT ( Orders[Customer Unique Id] ), Orders[Customer Order Seq] > 1, Orders[Is Valid] = TRUE () )")),
    ("Report UI", "Period Counted", None,
     "The period New / Returning Customers count: \"Selected dates\", or the last quarter with data when no date filter "
     "narrows the extract.",
     PERIOD_VARS + "RETURN IF ( _full, _lastq & \" (last quarter)\", \"Selected dates\" )"),
    ("Customers", "Repeat Orders", NUM0, "Valid orders from customers who had ordered before.",
     "CALCULATE ( COUNTROWS ( Orders ), Orders[Customer Order Seq] > 1, Orders[Is Valid] = TRUE () )"),
    ("Customers", "Repeat Order %", PCT1, None,
     "DIVIDE ( [Repeat Orders], CALCULATE ( COUNTROWS ( Orders ), Orders[Is Valid] = TRUE () ) )"),
    ("Customers", "Repeat Customer Rate", PCT1, "Share of customers (lifetime) with 2+ valid orders.",
     "DIVIDE (\n"
     "    CALCULATE ( COUNTROWS ( Customer ), Customer[Valid Orders] > 1 ),\n"
     "    CALCULATE ( COUNTROWS ( Customer ), Customer[Valid Orders] > 0 )\n"
     ")"),
    ("Customers", "Customer Count", NUM0, "Customers in the Customer dimension (lifetime, ignores date).",
     "CALCULATE ( COUNTROWS ( Customer ), Customer[Valid Orders] > 0 )"),
    ("Customers", "Customer Lifetime Value", BRL0, None, "SUM ( Customer[Lifetime Value] )"),
    ("Customers", "Avg Retention %", "0.00%", "Average cohort retention for the selected month offsets.",
     "AVERAGE ( 'Cohort Retention'[Retention Rate] )"),
    ("Customers", "Cohort Customers", NUM0, None, "SUM ( 'Cohort Retention'[Active Customers] )"),
    # ------------------------------------------------ Delivery & satisfaction
    ("Delivery", "Delivered Orders", NUM0, None,
     "CALCULATE ( COUNTROWS ( Orders ), Orders[Is Delivered] = TRUE () )"),
    ("Delivery", "Late Orders", NUM0, "Delivered after the estimated date shown at checkout.",
     "CALCULATE ( COUNTROWS ( Orders ), Orders[Is Late] = TRUE () )"),
    ("Delivery", "Late Rate %", PCT1, None, "DIVIDE ( [Late Orders], [Delivered Orders] )"),
    ("Delivery", "On-Time Rate %", PCT1, None, "IF ( NOT ISBLANK ( [Delivered Orders] ), 1 - [Late Rate %] )"),
    ("Delivery", "Avg Delivery Days", NUM1, "Purchase to customer delivery.",
     "CALCULATE ( AVERAGE ( Orders[Delivery Days] ), Orders[Is Delivered] = TRUE () )"),
    ("Delivery", "Avg Promised Days", NUM1, "Purchase to the estimated delivery date.",
     "CALCULATE ( AVERAGE ( Orders[Promised Days] ), Orders[Is Delivered] = TRUE () )"),
    ("Delivery", "Avg Buffer Days", NUM1, "How much earlier than promised orders arrive on average.",
     "[Avg Promised Days] - [Avg Delivery Days]"),
    ("Delivery", "Avg Days to Carrier", NUM1, "Purchase to hand-over to the carrier.",
     "CALCULATE ( AVERAGE ( Orders[Days To Carrier] ), Orders[Is Delivered] = TRUE () )"),
    ("Delivery", "Late Rate Color", None, "Status colour for conditional formatting of late rate.",
     "VAR _r = [Late Rate %]\n"
     "RETURN\n"
     "    SWITCH ( TRUE (),\n"
     "        ISBLANK ( _r ), \"#C3C2B7\",\n"
     "        _r >= 0.12, \"#D03B3B\",\n"
     "        _r >= 0.08, \"#EC835A\",\n"
     "        \"#2A78D6\"\n"
     "    )"),
    ("Satisfaction", "Reviews", NUM0, None, "COUNT ( Orders[Review Score] )"),
    ("Satisfaction", "Avg Review", "0.00", "Average 1–5 star review score.", "AVERAGE ( Orders[Review Score] )"),
    ("Satisfaction", "Bad Review %", PCT1, "Share of reviews that are 1 or 2 stars.",
     "DIVIDE ( CALCULATE ( [Reviews], Orders[Review Score] <= 2 ), [Reviews] )"),
    ("Satisfaction", "5-Star Review %", PCT1, None,
     "DIVIDE ( CALCULATE ( [Reviews], Orders[Review Score] = 5 ), [Reviews] )"),
    ("Satisfaction", "Review Share %", PCT1, "Share of reviews within each row of the visual (for 100% bars).",
     "DIVIDE ( [Reviews], CALCULATE ( [Reviews], ALL ( Orders[Review Label], Orders[Review Score] ) ) )"),
    ("Satisfaction", "Item Avg Review", "0.00", "Average review of orders containing the item (product/seller lens).",
     "CALCULATE ( AVERAGE ( 'Order Items'[Review Score] ), 'Order Items'[Is Valid] = TRUE () )"),
    ("Satisfaction", "Avg Review Label", None, None, "FORMAT ( [Avg Review], \"0.00\" ) & \" ★\""),
    # ------------------------------------------------------------- Payments
    ("Payments", "Payment Value", BRL0, None,
     "CALCULATE ( SUM ( Payments[Amount] ), Payments[Is Valid] = TRUE () )"),
    ("Payments", "Payments Count", NUM0, None,
     "CALCULATE ( COUNTROWS ( Payments ), Payments[Is Valid] = TRUE () )"),
    ("Payments", "Avg Installments", NUM1, None,
     "CALCULATE ( AVERAGE ( Payments[Installments] ), Payments[Is Valid] = TRUE (), Payments[Payment Type] = \"Credit Card\" )"),
    ("Payments", "Payment Share %", PCT1, None,
     "DIVIDE ( [Payment Value], CALCULATE ( [Payment Value], ALLSELECTED ( Payments ) ) )"),
    # -------------------------------------------------------------- Sellers
    ("Sellers", "Active Sellers", NUM0, "Sellers with at least one valid order item.",
     "CALCULATE ( DISTINCTCOUNT ( 'Order Items'[Seller Id] ), 'Order Items'[Is Valid] = TRUE () )"),
    ("Sellers", "Revenue per Seller", BRL0, None, "DIVIDE ( [Revenue], [Active Sellers] )"),
    ("Sellers", "Top-Tier Revenue Share %", PCT1, "Revenue share of tier-A sellers (the few who make 50% of lifetime revenue).",
     "DIVIDE ( CALCULATE ( [Revenue], Seller[Pareto Tier] = \"A · Top 50% revenue\" ), [Revenue] )"),
    ("Sellers", "Seller Late Rate %", PCT1, "Late share of delivered items (item lens).",
     "DIVIDE (\n"
     "    CALCULATE ( COUNTROWS ( 'Order Items' ), 'Order Items'[Is Late] = TRUE () ),\n"
     "    CALCULATE ( COUNTROWS ( 'Order Items' ), NOT ISBLANK ( 'Order Items'[Is Late] ) )\n"
     ")"),
    ("Sellers", "Avg Distance km", NUM0, "Average seller → customer distance.",
     "CALCULATE ( AVERAGE ( 'Order Items'[Distance Km] ), 'Order Items'[Is Valid] = TRUE () )"),
    # --------------------------------------------------------- Freight lanes
    ("Logistics", "Lane Items", NUM0, None, "SUM ( 'Freight Lanes'[Items] )"),
    ("Logistics", "Lane Late Rate %", PCT1, "Item-weighted late rate across lanes.",
     "DIVIDE ( SUMX ( 'Freight Lanes', 'Freight Lanes'[Items] * 'Freight Lanes'[Late Rate] ), [Lane Items] )"),
    ("Logistics", "Lane Avg Freight", BRL2, None,
     "DIVIDE ( SUMX ( 'Freight Lanes', 'Freight Lanes'[Items] * 'Freight Lanes'[Avg Freight] ), [Lane Items] )"),
    ("Logistics", "Lane Avg Delivery Days", NUM1, None,
     "DIVIDE ( SUMX ( 'Freight Lanes', 'Freight Lanes'[Items] * 'Freight Lanes'[Lane Delivery Days] ), [Lane Items] )"),
    # ------------------------------------------------------------ Report UI
    ("Report UI", "Period Label", None, "Selected date range, for page subtitles.",
     "FORMAT ( MIN ( 'Date'[Date] ), \"mmm yyyy\" ) & \" – \" & FORMAT ( MAX ( 'Date'[Date] ), \"mmm yyyy\" )"),
]

# ------------------------------------------------------------------ phase B: model & DAX methods
USE_DELIVERED = "USERELATIONSHIP ( Orders[Delivered Date], 'Date'[Date] )"
USE_PROMISED = "USERELATIONSHIP ( Orders[Estimated Date], 'Date'[Date] )"
MONTHS = "ALLSELECTED ( 'Date'[Year Month], 'Date'[Year Month Key] ), ORDERBY ( 'Date'[Year Month Key] )"
TOP_TIER = '"A · Top 50% revenue"'

MEASURES += [
    # RELATED / iterators (3-s10, 16-s9)
    ("Logistics", "Freight per kg", BRL2, "Freight charged per kilogram shipped: RELATED fetches each item's product weight.",
     "Olist.SafePct ( [Freight Total],\n"
     "    CALCULATE ( SUMX ( 'Order Items', RELATED ( Product[Weight G] ) ), 'Order Items'[Is Valid] = TRUE () ) / 1000 )"),
    ("Logistics", "Avg Freight per Km", "\\R$ #,0.000", "Iterator: freight ÷ distance per item, then the average of those ratios.",
     "AVERAGEX (\n    FILTER ( 'Order Items', 'Order Items'[Is Valid] && 'Order Items'[Distance Km] > 0 ),\n"
     "    DIVIDE ( 'Order Items'[Freight], 'Order Items'[Distance Km] )\n)"),
    ("Delivery", "Slowest State Days", NUM1, "Iterator over states: the highest state average delivery time in the selection.",
     "MAXX ( VALUES ( Customer[State] ), [Avg Delivery Days] )"),
    # CALCULATE filters (6-s3, 6-s5, 6-s6, 6-s7, 9-s2)
    ("Sellers", "Revenue Top Tier", BRL0,
     "Tier-A revenue, KEEPFILTERS: on a row for tier B it is blank, because the tier filter is added to the row's, not swapped in.",
     f"CALCULATE ( [Revenue], KEEPFILTERS ( Seller[Pareto Tier] = {TOP_TIER} ) )"),
    ("Sellers", "Revenue Top Tier (overwrite)", BRL0,
     "Tier-A revenue, plain filter: replaces the row's tier, so every tier row shows the tier-A total.",
     f"CALCULATE ( [Revenue], Seller[Pareto Tier] = {TOP_TIER} )"),
    ("Sales", "Category Share in Group %", PCT1, "A category's share of its category group (ALLEXCEPT keeps only the group filter on Product).",
     "Olist.SafePct ( [Revenue], CALCULATE ( [Revenue], ALLEXCEPT ( Product, Product[Category Group] ) ) )"),
    ("Sellers", "Revenue at Risk (Low-Rated Sellers)", BRL0,
     "Revenue from sellers whose items average under 3 stars: FILTER over sellers evaluates the review measure per seller.",
     "CALCULATE ( [Revenue], FILTER ( VALUES ( Seller[Seller Id] ), [Item Avg Review] < 3 ) )"),
    ("Customers", "Repeat Customers", NUM0,
     "Customers with more than one order in the selection. [Order Count] inside FILTER runs once per customer: the "
     "row becomes a filter (context transition), so each customer gets their own count.",
     "COUNTROWS ( FILTER ( VALUES ( Customer[Customer Unique Id] ), [Order Count] > 1 ) )"),
    ("Sellers", "Revenue SP Sellers", BRL0, "São Paulo sellers' revenue, overwrite: replaces any seller-state filter on the row.",
     'CALCULATE ( [Revenue], Seller[State] = "SP" )'),
    ("Sellers", "Revenue SP Sellers (KEEPFILTERS)", BRL0, "São Paulo sellers' revenue, KEEPFILTERS: blank on rows for other states.",
     'CALCULATE ( [Revenue], KEEPFILTERS ( Seller[State] = "SP" ) )'),
    # role-playing dates (6-s8)
    ("Delivery", "Delivered Orders (by Delivery Date)", NUM0, "Delivered orders counted in the month they arrived (inactive Delivered Date relationship).",
     f"CALCULATE ( [Delivered Orders], {USE_DELIVERED} )"),
    ("Delivery", "Late Rate % (by Promise Month)", PCT1, "Late rate by the month delivery was promised for, not the month of purchase.",
     f"CALCULATE ( [Late Rate %], {USE_PROMISED} )"),
    # time intelligence (6-s10, 6-s14, 6-s16, 9-s10)
    ("Time Intelligence", "Revenue QTD", BRL0, "Quarter to date.", "TOTALQTD ( [Revenue], 'Date'[Date] )"),
    ("Time Intelligence", "Revenue MTD", BRL0, "Month to date.", "TOTALMTD ( [Revenue], 'Date'[Date] )"),
    ("Delivery", "Orders In Transit (EoP)", NUM0,
     "Orders bought but not yet delivered at the end of the period: a snapshot, so take the last day, never the sum of days.",
     "CALCULATE ( SUM ( 'Backlog Daily'[In Transit Snapshot] ), LASTDATE ( 'Date'[Date] ) )"),
    # Seller Monthly snapshot (a pre-aggregated seller x month table) for the Seller Detail drillthrough page
    ("Sellers", "Seller Monthly Revenue", BRL0, "Revenue from the Seller Monthly snapshot table.", "SUM ( 'Seller Monthly'[Monthly Revenue] )"),
    ("Sellers", "Seller Monthly Orders", NUM0, "Orders from the Seller Monthly snapshot table.", "SUM ( 'Seller Monthly'[Monthly Orders] )"),
    ("Sellers", "Seller Monthly Late Rate", PCT1, "Late rate of the seller-month rows (plain average of the monthly rates).",
     "AVERAGE ( 'Seller Monthly'[Monthly Late Rate] )"),
    ("Sellers", "Seller Monthly Avg Review", NUM2, "Average review of the seller-month rows (plain average of the monthly averages).",
     "AVERAGE ( 'Seller Monthly'[Monthly Avg Review] )"),
    # 18-s7: the map's latitude / longitude wells take measures
    ("Sellers", "Seller Lat", "0.0000", "Latitude of the seller's zip-prefix centroid (for the map).", "AVERAGE ( Seller[Lat] )"),
    ("Sellers", "Seller Lng", "0.0000", "Longitude of the seller's zip-prefix centroid (for the map).", "AVERAGE ( Seller[Lng] )"),
    ("Time Intelligence", "Revenue PY (cal)", BRL0, "Prior year on the Gregorian calendar (calendar-based time intelligence).",
     "CALCULATE ( [Revenue], DATEADD ( 'Gregorian', -1, YEAR ) )"),
    ("Time Intelligence", "Revenue YTD (cal)", BRL0, "Year to date on the Gregorian calendar.",
     "CALCULATE ( [Revenue], DATESYTD ( 'Gregorian' ) )"),
    ("Time Intelligence", "Revenue PQ (cal)", BRL0, "Prior quarter on the Gregorian calendar; a Weekday Name filter is kept, not cleared.",
     "CALCULATE ( [Revenue], DATEADD ( 'Gregorian', -1, QUARTER ) )"),
    # expanded table, TREATAS, table functions (9-s1, 9-s4, 9-s5)
    ("Customers", "Customers Buying Category", NUM0,
     "Distinct customers with an order item under the current filters: 'Order Items' as a filter carries its expanded "
     "table, so Product filters reach Customer.",
     "CALCULATE ( DISTINCTCOUNT ( Customer[Customer Unique Id] ), 'Order Items' )"),
    ("Satisfaction", "Seller Order Review", "0.00",
     "Order-level review for the orders in context: TREATAS moves the item order ids onto Orders without a physical relationship.",
     "CALCULATE ( [Avg Review], TREATAS ( VALUES ( 'Order Items'[Order Id] ), Orders[Order Id] ) )"),
    ("Sales", "Top 3 Category Revenue", BRL0, "Revenue of the three best categories in the selection (TOPN over a temporary table).",
     "SUMX ( TOPN ( 3, ADDCOLUMNS ( VALUES ( Product[Category] ), \"@r\", [Revenue] ), [@r] ), [@r] )"),
    # window functions (9-s7, 9-s8, 9-s9)
    ("Time Intelligence", "Revenue PM (OFFSET)", BRL0, "Previous month with OFFSET over the months in the selection (no date functions).",
     f"CALCULATE ( [Revenue], OFFSET ( -1, {MONTHS} ) )"),
    ("Time Intelligence", "Revenue Running Total", BRL0, "Running total from the first selected month (WINDOW 1 ABS to 0 REL).",
     f"CALCULATE ( [Revenue], WINDOW ( 1, ABS, 0, REL, {MONTHS} ) )"),
    ("Time Intelligence", "Revenue 3M MA (WINDOW)", BRL0, "Average of this and the two previous months' revenue (WINDOW -2 REL to 0 REL).",
     f"AVERAGEX ( WINDOW ( -2, REL, 0, REL, {MONTHS} ), [Revenue] )"),
    ("Sales", "Category Rank", NUM0, "Dense revenue rank of a category within the selection.",
     "IF ( ISINSCOPE ( Product[Category] ), RANK ( DENSE, ALLSELECTED ( Product[Category] ), ORDERBY ( [Revenue], DESC ) ) )"),
    ("Sellers", "Top Seller in State", None, "Code of the highest-revenue seller in the state on the row (INDEX 1, PARTITIONBY state).",
     "VAR _top =\n    INDEX ( 1,\n        ADDCOLUMNS ( ALLSELECTED ( Seller[State], Seller[Seller Code] ), \"@rev\", [Revenue] ),\n"
     "        ORDERBY ( [@rev], DESC ), PARTITIONBY ( Seller[State] ) )\n"
     "RETURN IF ( HASONEVALUE ( Seller[State] ), MAXX ( _top, Seller[Seller Code] ) )"),
    # functions (9-s11 .. 9-s13)
    ("Time Intelligence", "Revenue YoY % (fn)", PCT1, "Olist.YoY on Revenue: one function for every YoY.", "Olist.YoY ( [Revenue] )"),
    ("Time Intelligence", "Orders YoY % (fn)", PCT1, "Olist.YoY on Order Count.", "Olist.YoY ( [Order Count] )"),
    ("Time Intelligence", "Customers YoY % (fn)", PCT1, "Olist.YoY on Customers.", "Olist.YoY ( [Customers] )"),
    # many-to-many plan (11-s3) and the bridge (11-s4)
    ("Plan", "Plan Revenue", BRL0, "SYNTHETIC plan (see README): filtered by Product and Date through many-to-many relationships.",
     "SUM ( 'Category Plan'[Plan Revenue] )"),
    ("Plan", "Revenue vs Plan %", PCT1, "Revenue above (+) or below (−) the synthetic plan.",
     "IF ( NOT ISBLANK ( [Plan Revenue] ), Olist.SafePct ( [Revenue] - [Plan Revenue], [Plan Revenue] ) )"),
    ("Satisfaction", "Seller Avg Review (order-level)", "0.00",
     "Order review per seller through the Order Seller bridge: each order counts once per seller, not once per item.",
     "CALCULATE ( [Avg Review], 'Order Seller' )"),
    ("Delivery", "Multi-seller Late Rate %", PCT1, "Late rate of orders that ship from more than one seller.",
     "CALCULATE ( [Late Rate %], Orders[Seller Count] > 1 )"),
    ("Delivery", "Single-seller Late Rate %", PCT1, "Late rate of orders from one seller, for comparison.",
     "CALCULATE ( [Late Rate %], Orders[Seller Count] = 1 )"),
    # calculated table + field parameter helpers (16-s8, 7-s6)
    ("Customers", "Customers in Spend Band", NUM0, "Customers whose lifetime value falls in the Spend Bands row.",
     "VAR _lo = MIN ( 'Spend Bands'[Min Value] )\nVAR _hi = MAX ( 'Spend Bands'[Max Value] )\n"
     "RETURN\n    COUNTROWS ( FILTER ( Customer, Customer[Valid Orders] > 0\n"
     "        && Customer[Lifetime Value] >= _lo && Customer[Lifetime Value] < _hi ) )"),
    ("Report UI", "Metric Value", None,
     "The measure picked in the Metric field parameter, with a format string that follows it (R$, count, %, stars).",
     "SWITCH ( SELECTEDVALUE ( Metric[Metric Order], 0 ),\n    0, [Revenue],\n    1, [Order Count],\n    2, [AOV],\n"
     "    3, [Late Rate %],\n    4, [Avg Review] )",
     'SWITCH ( SELECTEDVALUE ( Metric[Metric Order], 0 ), 0, "\\R$ #,0", 1, "#,0", 2, "\\R$ #,0.00", 3, "0.0%", 4, "0.00"" ★""" )'),
    # report text (17-s11 alt text, 18-s5 status in words, 4-s11 sparkline)
    ("Report UI", "Alt Text Revenue", None,
     "Screen-reader text for the revenue card: the number and its year-over-year change, recomputed for every filter.",
     'VAR _r = [Revenue]\nVAR _y = [Revenue YoY %]\nRETURN\n    "Revenue R$ " & FORMAT ( _r, "#,0" )\n'
     '        & IF ( ISBLANK ( _y ), ", no prior year in the selection", ", YoY " & FORMAT ( _y, "+0.0%;-0.0%" ) )'),
    ("Report UI", "Late Status", None, "Late rate as a symbol and a word, so the status never depends on colour alone.",
     'VAR _r = [Late Rate %]\nRETURN\n    SWITCH ( TRUE (),\n        ISBLANK ( _r ), BLANK (),\n        _r >= 0.12, "▲ High",\n'
     '        _r >= 0.08, "● Watch",\n        "✓ OK" )'),
    ("Report UI", "Lane Late Status", None, "Lane late rate as a symbol and a word (same thresholds as Late Status).",
     'VAR _r = [Lane Late Rate %]\nRETURN\n    SWITCH ( TRUE (),\n        ISBLANK ( _r ), BLANK (),\n        _r >= 0.12, "▲ High",\n'
     '        _r >= 0.08, "● Watch",\n        "✓ OK" )'),
    ("Report UI", "Alt Text Late Rate", None, "Screen-reader text for the late-rate card.",
     '"Late rate " & FORMAT ( [Late Rate %], "0.0%" ) & ", " & [Late Status]'),
    ("Sales", "Revenue (Last 12 Months)", BRL0,
     "Revenue for the 12 months up to the last month with data, blank before: the sparkline on the category tooltip.",
     "VAR _last = CALCULATE ( MAX ( 'Order Items'[Order Date] ), REMOVEFILTERS (), 'Order Items'[Is Valid] = TRUE () )\n"
     "VAR _m = MAX ( 'Date'[Month Start] )\nRETURN IF ( _m > EDATE ( _last, -12 ) && _m <= _last, [Revenue] )"),
    # ---------------------------------------------------- D: business analysis (10-s*, 17-s17, 19-s2)
    # 10-s3: dynamic segmentation: customers fall into a band by the revenue they bring in the current filters
    ("Customers", "Customers in Band", NUM0,
     "Customers whose revenue in the current filters (year, region, category ...) falls in the Spend Bands row.",
     "VAR _lo = MIN ( 'Spend Bands'[Min Value] )\nVAR _hi = MAX ( 'Spend Bands'[Max Value] )\nRETURN\n"
     "    COUNTROWS ( FILTER ( VALUES ( Customer[Customer Unique Id] ), VAR _r = [Revenue per Customer] RETURN NOT ISBLANK ( _r ) && _r >= _lo && _r < _hi ) )"),
    ("Customers", "Revenue in Band", BRL0, "Revenue of the customers in the Spend Bands row (same rule as Customers in Band).",
     "VAR _lo = MIN ( 'Spend Bands'[Min Value] )\nVAR _hi = MAX ( 'Spend Bands'[Max Value] )\nRETURN\n"
     "    SUMX ( FILTER ( VALUES ( Customer[Customer Unique Id] ), VAR _r = [Revenue per Customer] RETURN NOT ISBLANK ( _r ) && _r >= _lo && _r < _hi ), [Revenue] )"),
    # 10-s8: churn = no purchase for 180+ days at the end of the extract (Recency Days is counted to that date)
    ("Customers", "Churned Customers", NUM0, "Customers with a valid order whose last purchase is 180+ days before the end of the data.",
     "COUNTROWS ( FILTER ( Customer, Customer[Valid Orders] > 0 && Customer[Recency Days] >= 180 ) )"),
    ("Customers", "At-Risk Customers (90–180d)", NUM0, "Last purchase 90–179 days before the end of the data: not churned yet.",
     "COUNTROWS ( FILTER ( Customer, Customer[Valid Orders] > 0 && Customer[Recency Days] >= 90 && Customer[Recency Days] < 180 ) )"),
    ("Customers", "Churn Rate %", PCT1, "Churned customers ÷ customers with a valid order.",
     "DIVIDE ( [Churned Customers], COUNTROWS ( FILTER ( Customer, Customer[Valid Orders] > 0 ) ) )"),
    # 10-s9: BG/NBD x Gamma-Gamma (src/predict.py)
    ("Customers", "Predicted CLV", BRL0, "Expected item revenue in the next 12 months (BG/NBD × Gamma-Gamma), summed over customers.",
     "SUM ( Customer[Predicted CLV 12M] )"),
    ("Customers", "Avg Predicted CLV", BRL2, "Predicted CLV per customer with a valid order.",
     "AVERAGEX ( FILTER ( Customer, Customer[Valid Orders] > 0 ), Customer[Predicted CLV 12M] )"),
    # 10-s2: ABC that follows the slicers: cumulative revenue share over the sellers in the selection
    ("Sellers", "Seller ABC (dynamic)", None,
     "A/B/C tier recomputed on the current selection (A = top 50% of revenue, B = next 30%, C = rest). Pareto Tier is the lifetime version.",
     "VAR _rev = [Revenue]\n"
     "VAR _tbl = ADDCOLUMNS ( ALLSELECTED ( Seller[Seller Code] ), \"@rev\", [Revenue] )\n"
     "VAR _total = SUMX ( _tbl, [@rev] )\n"
     "VAR _cum =\n    SUMX (\n        WINDOW ( 1, ABS, 0, REL, _tbl, ORDERBY ( [@rev], DESC, Seller[Seller Code], ASC ), DEFAULT, , MATCHBY ( Seller[Seller Code] ) ),\n"
     "        [@rev] )\n"
     "VAR _share = DIVIDE ( _cum, _total )\n"
     "RETURN\n    IF ( HASONEVALUE ( Seller[Seller Code] ) && NOT ISBLANK ( _rev ),\n"
     "        SWITCH ( TRUE (), _share <= 0.5, \"A\", _share <= 0.8, \"B\", \"C\" ) )"),
    ("Sellers", "Seller Rank (selection)", NUM0, "Revenue rank of the seller among the sellers in the selection.",
     "IF ( HASONEVALUE ( Seller[Seller Code] ) && NOT ISBLANK ( [Revenue] ),\n"
     "    RANK ( SKIP, ADDCOLUMNS ( ALLSELECTED ( Seller[Seller Code] ), \"@rev\", [Revenue] ), ORDERBY ( [@rev], DESC ), DEFAULT, , MATCHBY ( Seller[Seller Code] ) ) )"),
    ("Sellers", "Seller Revenue P90", BRL0, "90th percentile of seller revenue in the selection: the reference line on the seller scatter.",
     "PERCENTILEX.INC ( FILTER ( ALLSELECTED ( Seller[Seller Code] ), NOT ISBLANK ( [Revenue] ) ), [Revenue], 0.9 )"),
    # 10-s10
    ("Sellers", "Transition Sellers", NUM0, "Sellers moving from the 2017 tier (row) to the 2018 tier (column), Jan–Aug both years.",
     "SUM ( 'Seller Tier Transition'[Sellers] )"),
    ("Sellers", "Transition Share %", PCT1, "Share of the 2017 tier (row) that ended in each 2018 tier.",
     "DIVIDE ( [Transition Sellers], CALCULATE ( [Transition Sellers], REMOVEFILTERS ( 'Seller Tier Transition'[Tier 2018] ) ) )"),
    # 10-s12: order lifecycle funnel. Each stage keeps the conditions of the stages before it, so the bars only shrink
    ("Delivery", "Funnel Orders", NUM0, "Orders that reached the Funnel Stage row: all orders, so it lifts the valid-only report filter and the delivered-only page filter.",
     "VAR _s = SELECTEDVALUE ( 'Funnel Stage'[Stage Order] )\n"
     "VAR _purchased = CALCULATE ( COUNTROWS ( Orders ), REMOVEFILTERS ( Orders[Is Valid], Orders[Is Delivered] ) )\n"
     "RETURN\n    SWITCH ( _s,\n"
     "        1, _purchased,\n"
     "        2, CALCULATE ( COUNTROWS ( Orders ), REMOVEFILTERS ( Orders[Is Valid], Orders[Is Delivered] ), NOT ISBLANK ( Orders[Approval Hours] ) ),\n"
     "        3, CALCULATE ( COUNTROWS ( Orders ), REMOVEFILTERS ( Orders[Is Valid], Orders[Is Delivered] ), NOT ISBLANK ( Orders[Approval Hours] ), NOT ISBLANK ( Orders[Days To Carrier] ) ),\n"
     "        4, CALCULATE ( COUNTROWS ( Orders ), REMOVEFILTERS ( Orders[Is Valid], Orders[Is Delivered] ), NOT ISBLANK ( Orders[Approval Hours] ), NOT ISBLANK ( Orders[Days To Carrier] ), Orders[Is Delivered] = TRUE () ),\n"
     "        5, CALCULATE ( COUNTROWS ( Orders ), REMOVEFILTERS ( Orders[Is Valid], Orders[Is Delivered] ), NOT ISBLANK ( Orders[Approval Hours] ), NOT ISBLANK ( Orders[Days To Carrier] ), Orders[Is Delivered] = TRUE (), NOT ISBLANK ( Orders[Review Score] ) ) )"),
    ("Delivery", "Funnel Conversion %", PCT1, "Orders at this stage ÷ orders purchased.",
     "DIVIDE ( [Funnel Orders], CALCULATE ( [Funnel Orders], 'Funnel Stage'[Stage Order] = 1 ) )"),
    # 10-s14: percentile and outliers
    ("Delivery", "Delivery Days P90", NUM1, "9 in 10 delivered orders arrive within this many days (PERCENTILEX.INC).",
     "PERCENTILEX.INC ( FILTER ( Orders, Orders[Is Delivered] = TRUE () && NOT ISBLANK ( Orders[Delivery Days] ) ), Orders[Delivery Days], 0.9 )"),
    ("Logistics", "Lane State P90", NUM1, "Delivery Days P90 of the lane's customer state (all lanes into that state).",
     "CALCULATE ( [Delivery Days P90], TREATAS ( VALUES ( 'Freight Lanes'[Customer State] ), Customer[State] ), REMOVEFILTERS ( 'Freight Lanes' ) )"),
    ("Logistics", "Lane Outlier", None, "▲ when the lane's average delivery time is above its customer state's P90 (an outlier lane).",
     "VAR _d = [Lane Avg Delivery Days]\nVAR _p = [Lane State P90]\n"
     "RETURN IF ( HASONEVALUE ( 'Freight Lanes'[Lane] ), IF ( _d > _p, \"▲ Above state P90\", \"–\" ) )"),
    ("Logistics", "Lane Outlier Color", None, "Red for outlier lanes (conditional formatting).",
     "IF ( [Lane Avg Delivery Days] > [Lane State P90], \"#D03B3B\", \"#52514E\" )"),
    # 10-s13: variance by contributor
    ("Time Intelligence", "Revenue YoY Δ", BRL0, "Revenue minus the same period last year (like-for-like months, see Revenue PY).",
     "VAR _py = [Revenue PY]\nRETURN IF ( NOT ISBLANK ( _py ), [Revenue] - _py )"),
    # 10-s15: ETS forecast table
    ("Sales", "Forecast Revenue", BRL0, "ETS forecast of monthly revenue, Sep–Dec 2018 (src/predict.py).", "SUM ( 'Revenue Forecast'[Forecast Revenue] )"),
    ("Sales", "Forecast Lower 80", BRL0, "80% prediction interval, lower bound.", "SUM ( 'Revenue Forecast'[Lower 80] )"),
    ("Sales", "Forecast Upper 80", BRL0, "80% prediction interval, upper bound.", "SUM ( 'Revenue Forecast'[Upper 80] )"),
    ("Sales", "Revenue Actual (to Aug 2018)", BRL0, "Revenue up to Aug 2018, blank after: the actual line next to the forecast.",
     "IF ( MAX ( 'Date'[Month Start] ) <= DATE ( 2018, 8, 1 ) && MIN ( 'Date'[Month Start] ) >= DATE ( 2017, 1, 1 ), [Revenue] )"),
    # 19-s2 / 10-s16: what-if
    ("Delivery", "Late Reduction (pp) Value", "0", "The what-if slider: percentage points taken off the late rate.",
     "SELECTEDVALUE ( 'Late Reduction (pp)'[Late Reduction (pp)], 0 )"),
    ("Delivery", "Projected Late Rate %", PCT1, "Late rate after the what-if reduction (never below 0).",
     "MAX ( 0, [Late Rate %] - [Late Reduction (pp) Value] / 100 )"),
    ("Satisfaction", "Projected Bad Review %", PCT1,
     "Bad review share if the late rate fell by the slider: on-time bad-review rate, late odds × 8.97 (logit in src/analysis.py).",
     "VAR _or = 8.97  -- odds ratio of late delivery in the bad-review logit (src/analysis.py)\n"
     "VAR _pOn = CALCULATE ( [Bad Review %], Orders[Is Late] = FALSE () )\n"
     "VAR _oddsLate = DIVIDE ( _pOn, 1 - _pOn ) * _or\n"
     "VAR _pLate = DIVIDE ( _oddsLate, 1 + _oddsLate )\n"
     "VAR _moved = [Late Rate %] - [Projected Late Rate %]\n"
     "RETURN IF ( NOT ISBLANK ( [Bad Review %] ), [Bad Review %] - _moved * ( _pLate - _pOn ) )"),
    ("Satisfaction", "Bad Review Δ (what-if)", "+0.0%;-0.0%;0.0%", "Projected minus actual bad review share.",
     "[Projected Bad Review %] - [Bad Review %]"),
]

# 17-s18: Q&A synonyms (cultures/en-US.tmdl linguistic metadata): model object -> extra words people type
SYNONYMS = {
    ("_Measures", "Revenue"): ["sales", "turnover", "income"],
    ("_Measures", "Late Rate %"): ["late", "late rate", "lateness", "delay rate"],
    ("_Measures", "Avg Review"): ["rating", "stars", "review score"],
    ("_Measures", "Order Count"): ["orders", "number of orders"],
    ("_Measures", "Freight Total"): ["shipping", "freight"],
    ("Customer", "State"): ["state", "uf", "customer state"],
    ("Seller", "Seller Code"): ["seller", "vendor"],
    ("Product", "Category"): ["category", "product category"],
}

# 9-s11: every percentage goes through one division function
for _i, (_f, _n, _fmt, _d, _x, *_rest) in enumerate(MEASURES):
    if _fmt in (PCT1, "0.00%") and "DIVIDE (" in _x:
        MEASURES[_i] = (_f, _n, _fmt, _d, _x.replace("DIVIDE (", "Olist.SafePct ("), *_rest)

# (name, description, signature => body). VAL arguments are evaluated once by the caller; EXPR arguments are
# re-evaluated inside the function, where its CALCULATE can change their filter context (README).
FUNCTIONS = [
    ("Olist.SafePct", "Part ÷ whole, blank when the whole is blank or zero. Every % measure uses it.",
     "( num : SCALAR NUMERIC VAL, den : SCALAR NUMERIC VAL ) => DIVIDE ( num, den )"),
    ("Olist.Growth", "Change from prev to cur as a share of prev; blank without a prev. Both values arrive already computed (VAL).",
     "( cur : SCALAR NUMERIC VAL, prev : SCALAR NUMERIC VAL ) => IF ( NOT ISBLANK ( prev ), DIVIDE ( cur - prev, prev ) )"),
    ("Olist.AtPY", "An expression one year back on the Gregorian calendar. EXPR: the expression is evaluated inside the shifted dates.",
     "( m : SCALAR NUMERIC EXPR ) => CALCULATE ( m, DATEADD ( 'Gregorian', -1, YEAR ) )"),
    ("Olist.AtPYVal", "Teaching copy of AtPY with a VAL argument: the value is fixed before CALCULATE runs, so it returns "
     "the current year (see DAXQueries/VAL vs EXPR.dax).",
     "( m : SCALAR NUMERIC VAL ) => CALCULATE ( m, DATEADD ( 'Gregorian', -1, YEAR ) )"),
    ("Olist.YoY", "Year-over-year growth of any measure: a typed measure reference, so one function serves Revenue, Orders, Customers.",
     "( m : MEASUREREF ) => Olist.Growth ( m, Olist.AtPY ( m ) )"),
]
FUNCTION_VERSION = "1.0.0"

# Time Calc calculation group (7-s3/7-s4/7-s6): (item, DAX, dynamic format string or None)
TIME_ITEMS = [
    ("Current", "SELECTEDMEASURE ()", None),
    ("PY", "CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, YEAR ) )", None),
    ("YoY %", "VAR _py = CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, YEAR ) )\n"
              "RETURN IF ( NOT ISBLANK ( _py ), DIVIDE ( SELECTEDMEASURE () - _py, _py ) )", '"+0.0%;-0.0%;0.0%"'),
    ("YTD", "CALCULATE ( SELECTEDMEASURE (), DATESYTD ( 'Gregorian' ) )", None),
    ("MoM %", "VAR _pm = CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, MONTH ) )\n"
              "RETURN IF ( NOT ISBLANK ( _pm ), DIVIDE ( SELECTEDMEASURE () - _pm, _pm ) )", '"+0.0%;-0.0%;0.0%"'),
    ("3M Avg", "VAR _months = CALCULATETABLE ( VALUES ( 'Date'[Year Month Key] ), DATESINPERIOD ( 'Date'[Date], MAX ( 'Date'[Date] ), -3, MONTH ) )\n"
               "RETURN\n    AVERAGEX ( _months,\n        VAR _k = 'Date'[Year Month Key]\n"
               "        RETURN CALCULATE ( SELECTEDMEASURE (), REMOVEFILTERS ( 'Date' ), 'Date'[Year Month Key] = _k ) )", None),
]

# Calculated tables (DAX): name -> (expression, [(column, source, dataType, extra lines)], hidden)
CALCULATED = {
    "Spend Bands": ('DATATABLE ( "Spend Band", STRING, "Band Order", INTEGER, "Min Value", DOUBLE, "Max Value", DOUBLE, '
                    '{ { "Under R$100", 1, 0, 100 }, { "R$100–250", 2, 100, 250 }, { "R$250–500", 3, 250, 500 }, '
                    '{ "R$500–1,000", 4, 500, 1000 }, { "R$1,000+", 5, 1000, 1000000 } } )',
                    [("Spend Band", "[Spend Band]", "string", ["sortByColumn: 'Band Order'"]),
                     ("Band Order", "[Band Order]", "int64", ["isHidden", "formatString: 0"]),
                     ("Min Value", "[Min Value]", "double", ["isHidden", "formatString: \\R$ #,0"]),
                     ("Max Value", "[Max Value]", "double", ["isHidden", "formatString: \\R$ #,0"])], False),
    # 19-s2 / 10-s16: what-if parameter (Desktop: Modeling > New parameter > Numeric range), 0-10 percentage points
    "Late Reduction (pp)": ('SELECTCOLUMNS ( GENERATESERIES ( 0, 10, 1 ), "Late Reduction (pp)", [Value] )',
                            [("Late Reduction (pp)", "[Late Reduction (pp)]", "int64",
                              ["formatString: 0", "", "extendedProperty ParameterMetadata =", "\t\t{\"version\":0}"])], False),
    # 10-s12: the stages of the order funnel, one row each (Funnel Orders switches on the stage)
    "Funnel Stage": ('DATATABLE ( "Stage", STRING, "Stage Order", INTEGER, { { "Purchased", 1 }, { "Approved", 2 }, '
                     '{ "Handed to carrier", 3 }, { "Delivered", 4 }, { "Reviewed", 5 } } )',
                     [("Stage", "[Stage]", "string", ["sortByColumn: 'Stage Order'"]),
                      ("Stage Order", "[Stage Order]", "int64", ["isHidden", "formatString: 0"])], False),
    # RLS mapping for Region Manager. The e-mails are placeholders: replace with real logins before publishing.
    "Region Access": ('DATATABLE ( "Email", STRING, "Region", STRING, '
                      '{ { "manager.northeast@example.com", "Northeast" }, { "manager.south@example.com", "South" }, '
                      '{ "manager.south@example.com", "Southeast" } } )',
                      [("Email", "[Email]", "string", []), ("Region", "[Region]", "string", [])], True),
}

# Field parameters (7-s1/7-s2): (label, NAMEOF target)
FIELD_PARAMETERS = {
    "Metric": [("Revenue", "[Revenue]"), ("Order Count", "[Order Count]"), ("AOV", "[AOV]"),
               ("Late Rate %", "[Late Rate %]"), ("Avg Review", "[Avg Review]")],
    # Payment Type is left out on purpose: Payments does not filter Order Items, so revenue by payment type is flat
    "Breakdown": [("Category Group", "Product[Category Group]"), ("Customer Region", "Customer[Region]"),
                  ("Seller Tier", "Seller[Pareto Tier]"), ("Distance Band", "'Order Items'[Distance Band]")],
}

ROLES = {
    # 5-s12: static RLS, a constant filter
    "Northeast Ops": {"filters": {"Customer": 'Customer[Region] = "Northeast"'}, "hide_tables": [], "hide_columns": {}},
    # 16-s13: dynamic RLS through a mapping table
    "Region Manager": {"filters": {"Customer": "Customer[Region] IN CALCULATETABLE ( VALUES ( 'Region Access'[Region] ), "
                                               "'Region Access'[Email] = USERPRINCIPALNAME () )"},
                       "hide_tables": [], "hide_columns": {}},
    # 11-s13/11-s14: a courier partner sees delivery data, never money
    "Logistics Partner": {"filters": {}, "hide_tables": ["Payments", "Seller Monthly"],
                          "hide_columns": {"Customer": ["Lifetime Value"], "Seller": ["Lifetime Revenue"]}},
}


# ------------------------------------------------------------------ Power Query: the plan files
# The marts arrive clean (DuckDB did the work), so Power Query only reads them. The plan is different: a finance
# sheet per year (SYNTHETIC, written by src/pipeline.py) that needs real shaping. One function reads a file, the two
# year queries call it (Reference, not Duplicate: the steps live in one place), an append stacks them, and two tables
# load from that: Category Plan (monthly) and Category Plan Quarterly (Group By). Everything except the two tables is
# a shared expression with load disabled. Step names are what the Applied Steps pane shows.
# (name, M type, value or None). Edit in Power BI: Transform data > Edit parameters.
PARAMETERS = [("DataFolder", "Text", None), ("PlanFolder", "Text", None)]


def steps(first, chain):
    """let … in from a first step and (name, comment, expression with {prev}) steps."""
    name, expr = first
    out = ["let", f"    {name} = {expr}"]
    last = name
    for step, comment, e in chain:
        out[-1] += ","
        out += [f"    // {comment}", f"    {step} = {e.format(prev=last)}"]
        last = step
    return out + ["in", f"    {last}"]


def qn(step):
    """M identifier: #"quoted" when it has spaces."""
    return step if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", step) else f'#"{step}"'


def chain(*items):
    return [(qn(n), c, e) for n, c, e in items]


READ_PLAN = steps(("Source", 'Csv.Document(File.Contents(path), [Delimiter = ",", Columns = 16, Encoding = 65001, '
                             'QuoteStyle = QuoteStyle.Csv])'), chain(
    ("Skip title rows", "Two title rows sit above the header", "Table.Skip({prev}, 2)"),
    ("Promoted headers", "The third row holds the column names", "Table.PromoteHeaders({prev}, [PromoteAllScalars = true])"),
    ("Blank group to null", "Empty cells arrive as \"\"; Fill Down only fills nulls",
     'Table.ReplaceValue({prev}, "", null, Replacer.ReplaceValue, {{"Category Group"}})'),
    ("Filled down group", "The group is written once per block (merged cells in the sheet): fill it down",
     'Table.FillDown({prev}, {{"Category Group"}})'),
    ("Removed TOTAL row", "The TOTAL row is a sum of the rows above; keep only the lines",
     'Table.SelectRows({prev}, each [Category Group] <> "TOTAL")'),
    ("Replaced Casa typo", "One owner typed the Portuguese 'Casa' for Home",
     'Table.ReplaceValue({prev}, "Casa", "Home", Replacer.ReplaceText, {{"Category Group"}})'),
    ("Trimmed and proper case", "Stray spaces and lower case: Text.Trim + Text.Proper",
     'Table.TransformColumns({prev}, {{{{"Category Group", each Text.Proper(Text.Trim(_)), type text}}}})'),
    ("Kept plan columns", "Drop the free-text Notes and Owner columns",
     'Table.SelectColumns({prev}, List.RemoveItems(Table.ColumnNames({prev}), {{"Notes", "Owner"}}))'),
    ("Unpivoted months", "12 month columns -> one row per group, line and month",
     'Table.UnpivotOtherColumns({prev}, {{"Category Group", "Plan Line"}}, "Month", "Plan Text")'),
    ("Split month", "2018-01 -> Year 2018 and Month No 01",
     'Table.SplitColumn({prev}, "Month", Splitter.SplitTextByDelimiter("-", QuoteStyle.None), {{"Year", "Month No"}})'),
    ("Amounts as numbers", "'n/a' and '-' are not numbers: try … otherwise null instead of an import error",
     'Table.AddColumn({prev}, "Plan Revenue", each try Number.From([Plan Text], "en-US") otherwise null, type nullable number)'),
    ("Removed text amount", "The text version is no longer needed", 'Table.RemoveColumns({prev}, {{"Plan Text"}})'),
    ("Changed type", "Types once the shape is final",
     'Table.TransformColumnTypes({prev}, {{{{"Plan Line", type text}}, {{"Year", Int64.Type}}, {{"Month No", Int64.Type}}}})'),
    ("Removed errors", "Safety net: a malformed month header would fail the type above; drop such rows (0 today)",
     'Table.RemoveRowsWithErrors({prev}, {{"Year", "Month No"}})'),
))

# shared expressions, load disabled. name -> (description, PBI_ResultType, M lines)
PQ_STAGING = {
    "fxReadPlan": ("Reads one yearly plan sheet and returns it long and clean (one row per group, line and month).",
                   "Function", ["(path as text) as table =>", *("    " + l for l in READ_PLAN)]),
    "Plan 2017": ("The 2017 plan sheet, through fxReadPlan.", "Table",
                  ["let", '    Source = fxReadPlan(PlanFolder & "category_plan_2017.csv")', "in", "    Source"]),
    "Plan 2018": ("The 2018 plan sheet, through fxReadPlan.", "Table",
                  ["let", '    Source = fxReadPlan(PlanFolder & "category_plan_2018.csv")', "in", "    Source"]),
    "Plan Raw": ("Both plan years stacked (Append). Staging only: the plan tables reference it.", "Table",
                 ["let", "    // Append: same columns in both years", '    Source = Table.Combine({#"Plan 2017", #"Plan 2018"})',
                  "in", "    Source"]),
    "Plan Groups": ("Distinct category groups from the product mart, with a short code: the lookup the plan is merged to.",
                    "Table", steps(("Source", 'Parquet.Document(File.Contents(DataFolder & "dim_product.parquet"))'), chain(
                        ("Kept group", "Only the group column (Parquet reads just this)",
                         'Table.SelectColumns({prev}, {{"category_group"}})'),
                        ("Distinct groups", "One row per group", "Table.Distinct({prev})"),
                        ("Renamed", "Model name", 'Table.RenameColumns({prev}, {{{{"category_group", "Category Group"}}}})'),
                        ("Group code", "ELEC, FASH, … for compact labels",
                         'Table.AddColumn({prev}, "Group Code", each Text.Upper(Text.Start(Text.Remove([Category Group], '
                         '{{" ", ",", "&"}}), 4)), type text)')))),
    "Plan Unknown Groups": ("Plan rows whose group is not in the product mart (left anti join). Must stay empty; "
                            "the pipeline runs the same check.", "Table",
                            ["let", '    Source = Table.NestedJoin(#"Plan Raw", {"Category Group"}, #"Plan Groups", '
                             '{"Category Group"}, "Match", JoinKind.LeftAnti)', "in", "    Source"]),
}

# loaded tables built in Power Query: name -> (columns [(name, dataType, options)], M lines)
PQ_TABLES = {
    "Category Plan": ([
        ("Category Group", "string", {}), ("Plan Line", "string", {}), ("Year", "int64", {"hidden": True}),
        ("Month No", "int64", {"hidden": True}), ("Plan Revenue", "double", {"fmt": "\\R$ #,0"}),
        ("Group Code", "string", {}), ("Month Start", "dateTime", {"fmt": "mmm yyyy"}),
        ("Is Peak Month", "boolean", {}), ("Plan Row", "int64", {"hidden": True})],
        steps(("Source", '#"Plan Raw"'), chain(
            ("Merged group code", "Merge (like a VLOOKUP) to the product groups: left outer join on Category Group",
             'Table.NestedJoin({prev}, {{"Category Group"}}, #"Plan Groups", {{"Category Group"}}, "Group", JoinKind.LeftOuter)'),
            ("Expanded group code", "Bring back the code", 'Table.ExpandTableColumn({prev}, "Group", {{"Group Code"}})'),
            ("Month start", "First day of the month: the key to Date[Month Start]",
             'Table.AddColumn({prev}, "Month Start", each #date([Year], [Month No], 1), type date)'),
            ("Is peak month", "Conditional column: November is Black Friday",
             'Table.AddColumn({prev}, "Is Peak Month", each [Month No] = 11, type logical)'),
            ("Plan row", "Index column, handy when checking against the sheet (last: an index stops folding)",
             'Table.AddIndexColumn({prev}, "Plan Row", 1, 1, Int64.Type)'),
            ("Changed type", "Final types", 'Table.TransformColumnTypes({prev}, {{{{"Plan Revenue", type number}}, '
                                            '{{"Group Code", type text}}}})')))),
    "Category Plan Quarterly": ([
        ("Category Group", "string", {}), ("Year", "int64", {"hidden": True}), ("Year Quarter", "string", {}),
        ("Plan Revenue", "double", {"fmt": "\\R$ #,0"}), ("Plan Months", "int64", {})],
        steps(("Source", '#"Plan Raw"'), chain(
            ("Quarter label", "Q1 2018, the same text as Date[Year Quarter]",
             'Table.AddColumn({prev}, "Year Quarter", each "Q" & Text.From(Number.RoundUp([Month No] / 3)) & " " '
             '& Text.From([Year]), type text)'),
            ("Grouped by quarter", "Group By: sum the months (Base + Promo lines) per group and quarter",
             'Table.Group({prev}, {{"Category Group", "Year", "Year Quarter"}}, {{{{"Plan Revenue", each List.Sum([Plan Revenue]), '
             'type nullable number}}, {{"Plan Months", each List.Count(List.Distinct([Month No])), Int64.Type}}}})')))),
}


def pq_table_tmdl(name):
    cols, m = PQ_TABLES[name]
    lines = [f"table {q(name)}", f"{T}lineageTag: {lineage(name)}", ""]
    for col, dtype, o in cols:
        lines += [f"{T}column {q(col)}", f"{T*2}dataType: {dtype}"] + [f"{T*2}isHidden"] * bool(o.get("hidden"))
        if fmt := o.get("fmt") or {"int64": "0", "dateTime": "dd mmm yyyy"}.get(dtype):
            lines.append(f"{T*2}formatString: {fmt}")
        lines += [f"{T*2}lineageTag: {lineage(name, col)}", f"{T*2}summarizeBy: none",  # totals via measures (BPA)
                  f"{T*2}sourceColumn: {col}", ""]
    lines += [f"{T}partition {q(name)} = m", f"{T*2}mode: import", f"{T*2}source =", *[f"{T*4}{l}" for l in m],
              "", f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def expressions_tmdl(values):
    lines = []
    for name, kind, _ in PARAMETERS:
        lines += [f'expression {name} = "{values[name]}" meta [IsParameterQuery=true, Type="{kind}", '
                  "IsParameterQueryRequired=true]", f"{T}lineageTag: {lineage('expr', name)}", "",
                  f"{T}annotation PBI_ResultType = {kind}", ""]
    for name, (desc, kind, m) in PQ_STAGING.items():
        lines += [f"/// {desc}", f"expression {q(name)} =", *[f"{T*2}{l}" for l in m],
                  f"{T}lineageTag: {lineage('expr', name)}", "", f"{T}annotation PBI_ResultType = {kind}", ""]
    return "\n".join(lines)



def pretty(col: str) -> str:
    special = {"aov": "AOV", "id": "Id", "km": "Km", "geo": "Geo", "rfm": "RFM", "iso": "ISO"}
    return " ".join(special.get(w, w.capitalize()) for w in col.split("_"))


def q(name: str) -> str:
    """Quote a TMDL/DAX object name when needed."""
    return f"'{name}'" if not name.replace("_", "").isalnum() or name[0].isdigit() else name


_seen = {}


def lineage(*parts) -> str:
    """Deterministic lineage tags (uuid5 of the object path), so a rebuild only diffs where the model changed."""
    key = "/".join(parts) or "anon"
    _seen[key] = _seen.get(key, 0) + 1
    return str(uuid.uuid5(NS, f"{key}#{_seen[key]}"))


def col_name(cfg, col):
    return cfg.get("names", {}).get(col) or pretty(col)


MART_M = {}  # table -> M lines of its partition (also written to powerquery/)


def table_tmdl(con, name, mart, cfg, data_dir_param="DataFolder"):
    cols = con.execute(f"describe mart.{mart}").fetchall()
    lines = [f"table {q(name)}", f"{T}lineageTag: {lineage(name)}"]
    if cfg.get("date_table"):
        lines.append(f"{T}dataCategory: Time")
    if cfg.get("table_hidden"):
        lines.append(f"{T}isHidden")
    lines.append("")
    m_types, kept = [], []
    for col, dtype, *_ in cols:
        if col in cfg.get("drop", []):
            continue
        kept.append(f'"{col}"')
        base = dtype.split("(")[0]
        tm, mq = TYPES.get(base, ("double", "type number")) if not base.startswith("DECIMAL") else ("double", "type number")
        m_types.append(f'{{"{col}", {mq}}}')
        lines.append(f"{T}column {q(col_name(cfg, col))}")
        lines.append(f"{T}{T}dataType: {tm}")
        if col == cfg.get("key"):
            lines.append(f"{T}{T}isKey")
        if col in cfg.get("hidden", []):
            lines.append(f"{T}{T}isHidden")
        if fmt := cfg.get("fmt", {}).get(col):
            lines.append(f"{T}{T}formatString: {fmt}")
        elif tm == "int64":
            lines.append(f"{T}{T}formatString: 0")
        elif tm == "dateTime":
            lines.append(f"{T}{T}formatString: {'General Date' if base == 'TIMESTAMP' else 'dd mmm yyyy'}")
        if cat := cfg.get("category", {}).get(col):
            lines.append(f"{T}{T}dataCategory: {cat}")
        lines.append(f"{T}{T}lineageTag: {lineage(name, col)}")
        # no implicit SUM: every total goes through a measure (BPA NUMERIC_COLUMN_SUMMARIZE_BY)
        lines.append(f"{T}{T}summarizeBy: none")
        lines.append(f"{T}{T}sourceColumn: {col}")
        if sort := cfg.get("sort", {}).get(col):
            lines.append(f"{T}{T}sortByColumn: {q(col_name(cfg, sort))}")
        lines.append("")
    for cname, dax, dtype, o in cfg.get("calc", []):
        lines += [f"{T}column {q(cname)} =", *[f"{T*3}{l}" for l in dax.split("\n")], f"{T*2}dataType: {dtype}"]
        lines += [f"{T*2}isHidden"] * bool(o.get("hidden"))
        if fmt := o.get("fmt") or {"int64": "0"}.get(dtype):
            lines.append(f"{T*2}formatString: {fmt}")
        lines += [f"{T*2}lineageTag: {lineage(name, cname)}", f"{T*2}summarizeBy: none"]
        if meta := o.get("meta"):
            lines += ["", f"{T*2}extendedProperty GroupingMetadata =", f"{T*4}{json.dumps(meta)}"]
        lines.append("")
    for h, levels in cfg.get("hier", []):
        lines += [f"{T}hierarchy {q(h)}", f"{T*2}lineageTag: {lineage(name, 'h', h)}", ""]
        for lv in levels:
            lines += [f"{T*2}level {q(lv)}", f"{T*3}lineageTag: {lineage(name, 'h', h, lv)}", f"{T*3}column: {q(lv)}", ""]
    for cal, groups in cfg.get("calendars", []):
        lines += [f"{T}calendar {q(cal)}", f"{T*2}lineageTag: {lineage(name, 'cal', cal)}", ""]
        for unit, primary, assoc in groups:
            lines += [f"{T*2}calendarColumnGroup = {unit}", f"{T*3}primaryColumn: {q(primary)}"]
            lines += [f"{T*3}associatedColumn: {q(a)}" for a in assoc] + [""]
    src = f'    Source = Parquet.Document(File.Contents({data_dir_param} & "{mart}.parquet")),'
    if cfg.get("drop"):  # choose columns first: Parquet then reads only these
        m = ["let", src, f"    // Leave out {', '.join(cfg['drop'])} (see README, performance)",
             f'    #"Kept columns" = Table.SelectColumns(Source, {{{", ".join(kept)}}}),',
             f"    Typed = Table.TransformColumnTypes(#\"Kept columns\", {{{', '.join(m_types)}}})", "in", "    Typed"]
    else:
        m = ["let", src, f"    Typed = Table.TransformColumnTypes(Source, {{{', '.join(m_types)}}})", "in", "    Typed"]
    MART_M[name] = m
    lines += [f"{T}partition {q(name)} = m", f"{T}{T}mode: import", f"{T}{T}source ="]
    lines += [f"{T*4}{l}" for l in m]
    lines += ["", f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def measures_tmdl():
    lines = ["table _Measures", f"{T}lineageTag: {lineage('_Measures')}", ""]
    for folder, name, fmt, desc, dax, *fsd in MEASURES:
        if desc:
            lines.append(f"{T}/// {desc}")
        if "\n" in dax:
            lines.append(f"{T}measure {q(name)} =")
            lines += [f"{T*3}{l}" for l in dax.split("\n")]
        else:
            lines.append(f"{T}measure {q(name)} = {dax}")
        if fmt:
            lines.append(f"{T}{T}formatString: {fmt}")
        lines.append(f"{T}{T}displayFolder: {folder}")
        lines.append(f"{T}{T}lineageTag: {lineage('_Measures', name)}")
        if fsd:  # dynamic format string (7-s6); a child object, so it goes after the plain properties
            lines += ["", f"{T}{T}formatStringDefinition = {fsd[0]}"]
        elif not fmt:  # returns text (label, colour, alt text): a number format means nothing, so BPA is told on purpose
            lines += ["", f'{T}{T}annotation BestPracticeAnalyzer_IgnoreRules = '
                      '{"RuleIDs":["PROVIDE_FORMAT_STRING_FOR_MEASURES","INTEGER_FORMATTING"]}']
        lines.append("")
    lines += [
        f"{T}column Value",
        f"{T}{T}dataType: int64",
        f"{T}{T}isHidden",
        f"{T}{T}formatString: 0",
        f"{T}{T}lineageTag: {lineage('_Measures', 'Value')}",
        f"{T}{T}summarizeBy: none",
        f"{T}{T}isNameInferred",
        f"{T}{T}sourceColumn: [Value]",
        "",
        f"{T}partition _Measures = calculated",
        f"{T}{T}mode: import",
        f"{T}{T}source = {{1}}",
        "",
    ]
    return "\n".join(lines)


def block(text, indent):
    return [f"{T * indent}{l}" for l in text.split("\n")]


def functions_tmdl():
    lines = []
    for name, desc, body in FUNCTIONS:
        lines.append(f"/// {desc}")
        lines += [f"function {q(name)} =", *block(body, 2)] if "\n" in body else [f"function {q(name)} = {body}"]
        lines += [f"{T}lineageTag: {lineage('fn', name)}", "", f"{T}annotation Version = {FUNCTION_VERSION}", ""]
    return "\n".join(lines)


def calc_group_tmdl():
    t = "Time Calc"
    lines = [f"table {q(t)}", f"{T}lineageTag: {lineage(t)}", "", f"{T}calculationGroup", f"{T*2}precedence: 10", ""]
    for i, (name, dax, fmt) in enumerate(TIME_ITEMS):
        lines += [f"{T*2}calculationItem {q(name)} =", *block(dax, 4)] if "\n" in dax else [f"{T*2}calculationItem {q(name)} = {dax}"]
        lines.append(f"{T*3}ordinal: {i}")
        lines += [f"{T*3}formatStringDefinition = {fmt}"] if fmt else []
        lines.append("")
    lines += [f"{T}column {q(t)}", f"{T*2}dataType: string", f"{T*2}lineageTag: {lineage(t, 'Name')}",
              f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: Name", f"{T*2}sortByColumn: Ordinal", "",
              f"{T}column Ordinal", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
              f"{T*2}lineageTag: {lineage(t, 'Ordinal')}", f"{T*2}summarizeBy: sum", f"{T*2}sourceColumn: Ordinal", ""]
    return "\n".join(lines)


def field_parameter_tmdl(t, items):
    rows = ",\n".join(f'    ( "{label}", NAMEOF ( {ref} ), {i} )' for i, (label, ref) in enumerate(items))
    return "\n".join([
        f"table {q(t)}", f"{T}lineageTag: {lineage(t)}", "",
        f"{T}column {q(t)}", f"{T*2}dataType: string", f"{T*2}lineageTag: {lineage(t, 'a')}",
        f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: [Value1]", f"{T*2}sortByColumn: '{t} Order'", "",
        f"{T*2}relatedColumnDetails", f"{T*3}groupByColumn: '{t} Fields'", "",
        f"{T}column '{t} Fields'", f"{T*2}dataType: string", f"{T*2}isHidden", f"{T*2}lineageTag: {lineage(t, 'f')}",
        f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: [Value2]", f"{T*2}sortByColumn: '{t} Order'", "",
        f"{T*2}extendedProperty ParameterMetadata =", f"{T*4}{{", f'{T*4}  "version": 3,', f'{T*4}  "kind": 2', f"{T*4}}}", "",
        f"{T}column '{t} Order'", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
        f"{T*2}lineageTag: {lineage(t, 'o')}", f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: [Value3]", "",
        f"{T}partition {q(t)} = calculated", f"{T*2}mode: import", f"{T*2}source =", *block("{\n" + rows + "\n}", 4), ""])


def calculated_tmdl(name, dax, cols, hidden):
    lines = [f"table {q(name)}", f"{T}lineageTag: {lineage(name)}"] + [f"{T}isHidden"] * hidden + [""]
    for col, src, dtype, extra in cols:
        lines += [f"{T}column {q(col)}", f"{T*2}dataType: {dtype}", f"{T*2}lineageTag: {lineage(name, col)}",
                  f"{T*2}summarizeBy: none", f"{T*2}isNameInferred", f"{T*2}sourceColumn: {src}"]
        lines += [f"{T*2}{e}" for e in extra] + [""]
    return "\n".join(lines + [f"{T}partition {q(name)} = calculated", f"{T*2}mode: import", f"{T*2}source = {dax}", ""])


def role_tmdl(name, r):
    lines = [f"role {q(name)}", f"{T}modelPermission: read", ""]
    for table in sorted(set(r["filters"]) | set(r["hide_columns"])):
        dax = r["filters"].get(table)
        lines.append(f"{T}tablePermission {q(table)}" + (f" = {dax}" if dax else ""))
        lines += [""] + [f"{T*2}columnPermission {q(c)} = none" for c in r["hide_columns"].get(table, [])] + [""]
    for table in r["hide_tables"]:
        lines += [f"{T}tablePermission {q(table)}", f"{T*2}metadataPermission: none", ""]
    return "\n".join(lines)


def relationships_tmdl(rels, colname):
    out = []
    for ft, fc, tt, tc, *o in rels:
        o = o[0] if o else {}
        out += [f"relationship {lineage('rel', ft, fc, tt)}"] + [f"{T}isActive: false"] * (o.get("active") is False)
        out += [f"{T}crossFilteringBehavior: {'bothDirections' if o.get('both') else 'oneDirection'}",
                f"{T}fromCardinality: many", f"{T}toCardinality: {'many' if o.get('m2m') else 'one'}",
                f"{T}fromColumn: {q(ft)}.{q(colname(ft, fc))}", f"{T}toColumn: {q(tt)}.{q(colname(tt, tc))}", ""]
    return "\n".join(out)


def model_columns(con):
    """table -> model column names, for every table in the model."""
    cols = {t: {col_name(cfg, c[0]) for c in con.execute(f"describe mart.{mart}").fetchall() if c[0] not in cfg.get("drop", [])}
            | {c[0] for c in cfg.get("calc", [])} for t, (mart, cfg) in TABLES.items()}
    cols |= {t: {c[0] for c in PQ_TABLES[t][0]} for t in PQ_TABLES}
    cols |= {t: {c[0] for c in v[1]} for t, v in CALCULATED.items()}
    cols |= {t: {t, f"{t} Fields", f"{t} Order"} for t in FIELD_PARAMETERS} | {"Time Calc": {"Time Calc", "Ordinal"}}
    return cols


def culture_tmdl():
    entities = {}
    for (table, prop), terms in SYNONYMS.items():
        key = re.sub(r"\W+", "_", f"{table}.{prop}".lower()).strip("_")
        entities[key] = {"Definition": {"Binding": {"ConceptualEntity": table, "ConceptualProperty": prop}}, "State": "Authored",
                         "Terms": [{prop.lower(): {"State": "Generated"}}] + [{t: {"State": "Authored"}} for t in terms]}
    doc = {"Version": "1.0.0", "Language": "en-US", "DynamicImprovement": "HighConfidence", "Entities": entities}
    return "\n".join(["cultureInfo en-US", "", f"{T}linguisticMetadata = {json.dumps(doc, ensure_ascii=False)}",
                      f"{T*2}contentType: json", ""])


def check(con):
    """Fail fast on name collisions and DAX references (columns, measures, tables, functions) that don't resolve."""
    columns = model_columns(con)
    measures = [m[1] for m in MEASURES]
    # measures live in _Measures: TOM forbids duplicates and clashes with that table's own columns, not other tables' columns
    taken = {t.lower() for t in columns} | {"value"}
    clash = {m for m in measures if m.lower() in taken or measures.count(m) > 1}
    assert not clash, f"measure names collide with tables/columns: {clash}"
    tables = set(columns) | {c for _, (_, cfg) in TABLES.items() for c, _ in cfg.get("calendars", [])}
    fns = {f[0] for f in FUNCTIONS}
    dax_all = [(m[1], d) for m in MEASURES for d in (m[4], *m[5:])] \
        + [(f"{t}[{c}]", d) for t, (_, cfg) in TABLES.items() for c, d, *_ in cfg.get("calc", [])] \
        + [(f"role {r}", d) for r, v in ROLES.items() for d in v["filters"].values()] \
        + [(f"item {n}", d) for n, d, _ in TIME_ITEMS] + [(f"function {n}", b) for n, _, b in FUNCTIONS] \
        + [(p, r) for p, items in FIELD_PARAMETERS.items() for _, r in items]
    for name, dax in dax_all:
        dax = re.sub(r'"(?:[^"]|"")*"', '""', dax)
        for a, b, col in re.findall(r"(?:'([^']+)'|\b([A-Za-z_]\w*))\[([^\]]+)\]", dax):
            assert col in columns.get(a or b, ()), f"[{name}] references missing column {a or b}[{col}]"
        for ref in re.findall(r"(?<![\w'\]])\[([^\]]+)\]", dax):
            assert ref in measures or ref.startswith("@"), f"[{name}] references missing measure [{ref}]"
        for ref in re.findall(r"'([^'\n]+)'", re.sub(r"'[^'\n]+'\[[^\]]*\]", "", dax)):
            assert ref in tables, f"[{name}] references missing table/calendar '{ref}'"
        for fn in re.findall(r"\b([A-Z][A-Za-z]*\.[A-Za-z]+)\s*\(", dax):
            assert fn in fns or fn.isupper(), f"[{name}] calls unknown function {fn}"  # PERCENTILEX.INC etc. are DAX built-ins
        assert dax.count("(") == dax.count(")"), f"[{name}] unbalanced parentheses"
    for ft, fc, tt, tc, *_ in RELATIONSHIPS:
        assert rel_col(ft, fc) in columns[ft] and rel_col(tt, tc) in columns[tt], (ft, fc, tt, tc)
    for t, (_, cfg) in TABLES.items():
        for _, levels in cfg.get("hier", []):
            assert all(lv in columns[t] for lv in levels), t
        for _, groups in cfg.get("calendars", []):
            assert all(c in columns[t] for _, p, a in groups for c in [p, *a]), t
    for r in ROLES.values():
        assert all(t in columns for t in [*r["filters"], *r["hide_tables"], *r["hide_columns"]])
        assert all(c in columns[t] for t, cs in r["hide_columns"].items() for c in cs)
    for t, c in SYNONYMS:
        assert (c in measures) if t == "_Measures" else (c in columns[t]), f"synonym target {t}[{c}] missing"
    print(f"  ✓ {len(MEASURES)} measures, {len(FUNCTIONS)} functions, {len(TIME_ITEMS)} calc items, {len(ROLES)} roles: "
          "no name collisions, every reference resolves")


def rel_col(table, col):
    return col if table not in TABLES else col_name(TABLES[table][1], col)


# ------------------------------------------------------------------ saved DAX queries (DAX query view)
DAX_QUERIES = {
    "KEEPFILTERS vs overwrite": """// 6-s3 / 9-s2: the same filter written two ways.
// Overwrite replaces the row's filter (every tier row shows tier A revenue); KEEPFILTERS intersects (blank off tier A).
EVALUATE
SUMMARIZECOLUMNS (
    Seller[Pareto Tier],
    "Revenue", [Revenue],
    "Top Tier (KEEPFILTERS)", [Revenue Top Tier],
    "Top Tier (overwrite)", [Revenue Top Tier (overwrite)]
)

// Seller state rows: only the SP row keeps a value with KEEPFILTERS
EVALUATE
TOPN (
    6,
    SUMMARIZECOLUMNS (
        Seller[State],
        "Revenue", [Revenue],
        "SP (overwrite)", [Revenue SP Sellers],
        "SP (KEEPFILTERS)", [Revenue SP Sellers (KEEPFILTERS)]
    ),
    [Revenue], DESC
)
""",
    "Table functions": """// 9-s5: temporary tables. SUMMARIZECOLUMNS builds the grid, TOPN + ADDCOLUMNS rank inside it.
EVALUATE
SUMMARIZECOLUMNS (
    Product[Category Group],
    "Revenue", [Revenue],
    "Top 3 categories", [Top 3 Category Revenue],
    "Top 3 share", DIVIDE ( [Top 3 Category Revenue], [Revenue] )
)
ORDER BY [Revenue] DESC

EVALUATE
TOPN ( 10, ADDCOLUMNS ( VALUES ( Product[Category] ), "Revenue", [Revenue], "Rank", [Category Rank] ), [Revenue], DESC )
""",
    "Window functions": """// 9-s6: window functions over a temporary table: ORDERBY the month key, PARTITIONBY customer region.
DEFINE
    VAR _m =
        SUMMARIZECOLUMNS (
            Customer[Region], 'Date'[Year Month], 'Date'[Year Month Key],
            "Rev", [Revenue]
        )
EVALUATE
ADDCOLUMNS (
    _m,
    "Prev month (same region)", MAXX ( OFFSET ( -1, _m, ORDERBY ( 'Date'[Year Month Key] ), PARTITIONBY ( Customer[Region] ) ), [Rev] ),
    "Running total (region)", SUMX ( WINDOW ( 1, ABS, 0, REL, _m, ORDERBY ( 'Date'[Year Month Key] ), PARTITIONBY ( Customer[Region] ) ), [Rev] ),
    "Rank in month", RANK ( DENSE, _m, ORDERBY ( [Rev], DESC ), PARTITIONBY ( 'Date'[Year Month Key] ) )
)
ORDER BY Customer[Region], 'Date'[Year Month Key]
""",
    "Functions": """// 9-s14: one check per function. Every Pass column should read TRUE.
EVALUATE
{
    ( "Olist.SafePct 1 / 4 = 0.25", Olist.SafePct ( 1, 4 ) = 0.25 ),
    ( "Olist.SafePct x / 0 is blank", ISBLANK ( Olist.SafePct ( 1, 0 ) ) ),
    ( "Olist.Growth 110 vs 100 = 10%", Olist.Growth ( 110, 100 ) = 0.1 ),
    ( "Olist.Growth without prev is blank", ISBLANK ( Olist.Growth ( 110, BLANK () ) ) )
}

EVALUATE
CALCULATETABLE (
    ROW (
        "Olist.AtPY = Revenue PY (cal)", Olist.AtPY ( [Revenue] ) = [Revenue PY (cal)],
        "Olist.YoY = Time Calc YoY %", Olist.YoY ( [Revenue] ) = CALCULATE ( [Revenue], 'Time Calc'[Time Calc] = "YoY %" )
    ),
    'Date'[Year] = 2018
)
""",
    "VAL vs EXPR": """// 9-s12: the same body, two parameter modes. In 2018:
// AtPY (EXPR) re-evaluates [Revenue] inside DATEADD -> 2017 revenue.
// AtPYVal (VAL) receives a number already computed for 2018 -> CALCULATE cannot change it -> 2018 revenue.
EVALUATE
CALCULATETABLE (
    ROW (
        "Revenue 2018", [Revenue],
        "AtPY (EXPR)", Olist.AtPY ( [Revenue] ),
        "AtPYVal (VAL)", Olist.AtPYVal ( [Revenue] ),
        "Growth (VAL, VAL)", Olist.Growth ( [Revenue], Olist.AtPY ( [Revenue] ) )
    ),
    'Date'[Year] = 2018
)
""",
    "Expanded table and bridge": """// 9-s1: 'Order Items' as a CALCULATE filter carries Product, Seller, Customer and Date with it.
EVALUATE
SUMMARIZECOLUMNS ( Product[Category Group], "Customers buying", [Customers Buying Category], "Revenue", [Revenue] )
ORDER BY [Revenue] DESC

// 11-s4: item-level vs order-level review per seller tier, and late rate for multi-seller orders
EVALUATE
SUMMARIZECOLUMNS (
    Seller[Pareto Tier],
    "Item Avg Review", [Item Avg Review],
    "Order-level (bridge)", [Seller Avg Review (order-level)],
    "Order-level (TREATAS)", [Seller Order Review]
)
EVALUATE
ROW ( "Single-seller late %", [Single-seller Late Rate %], "Multi-seller late %", [Multi-seller Late Rate %] )

// 11-s3: synthetic plan vs revenue through the many-to-many relationships
EVALUATE
SUMMARIZECOLUMNS ( 'Date'[Year], Product[Category Group], "Revenue", [Revenue], "Plan", [Plan Revenue], "vs Plan", [Revenue vs Plan %] )
""",
}


def write_dax_queries():
    dq = OUT / "DAXQueries"
    shutil.rmtree(dq, ignore_errors=True)
    (dq / ".pbi").mkdir(parents=True)
    for tab, text in DAX_QUERIES.items():
        (dq / f"{tab}.dax").write_text(text)
    tests = sorted((ROOT / "dax").glob("*"))  # asserted CI tests (tools/pbi-kit/dax-tests)
    for f in tests:
        shutil.copy(f, dq / f.name)
    tabs = list(DAX_QUERIES) + [f.stem for f in tests if f.suffix == ".dax"]
    (dq / ".pbi" / "daxQueries.json").write_text(json.dumps(
        {"version": "1.0.0", "tabOrder": tabs, "defaultTab": tabs[0]}, indent=2) + "\n")


# ------------------------------------------------------------------ Olist.Scale: the large-data design (not run)
# Same star, sized for a lakehouse that had grown past memory: Order Items DirectQuery, dimensions Dual, an Import
# aggregation that answers most visuals, Orders Import with incremental refresh. Every Power Query step folds.
LAKEHOUSE_DB = "olist_lh"
SCALE = {  # table -> (mart, mode, columns kept)
    "Order Items": ("fact_order_items", "directQuery",
                    ["order_id", "product_id", "seller_id", "customer_unique_id", "order_date", "is_valid", "is_late", "price", "freight"]),
    "Orders": ("fact_orders", "import",
               ["order_id", "customer_unique_id", "order_date", "purchased_at", "is_valid", "is_delivered", "is_late", "review_score",
                "delivery_days"]),
    "Date": ("dim_date", "dual", ["date", "year", "quarter", "year_quarter", "year_quarter_key", "month_no", "month_name",
                                  "year_month", "year_month_key", "month_start"]),
    "Product": ("dim_product", "dual", ["product_id", "category", "category_group"]),
    "Seller": ("dim_seller", "dual", ["seller_id", "seller_code", "state", "region", "pareto_tier"]),
    "Customer": ("dim_customer", "dual", ["customer_unique_id", "state", "region", "city"]),
}
SCALE_REL = [("Order Items", "order_date", "Date", "date"), ("Order Items", "product_id", "Product", "product_id"),
             ("Order Items", "seller_id", "Seller", "seller_id"), ("Order Items", "customer_unique_id", "Customer", "customer_unique_id"),
             ("Orders", "order_date", "Date", "date"), ("Orders", "customer_unique_id", "Customer", "customer_unique_id")]
# Order Items Agg: (agg column, source column, dataType, summarization, base table, base column)
SCALE_AGG = [
    ("Agg Order Date", "order_date", "dateTime", "groupBy", "Order Items", "Order Date"),
    ("Agg Category", "category", "string", "groupBy", "Product", "Category"),
    ("Agg Seller Id", "seller_id", "string", "groupBy", "Order Items", "Seller Id"),
    ("Agg Customer State", "customer_state", "string", "groupBy", "Customer", "State"),
    ("Agg Is Valid", "is_valid", "boolean", "groupBy", "Order Items", "Is Valid"),
    ("Agg Price", "price", "double", "sum", "Order Items", "Price"),
    ("Agg Freight", "freight", "double", "sum", "Order Items", "Freight"),
    ("Agg Items", "items", "int64", "count", "Order Items", None),
]
SCALE_MEASURES = [m for m in MEASURES if m[1] in ("Revenue", "Freight Total", "GMV", "Items Sold", "Order Count", "AOV")]
SCALE_M = {}  # query name -> M lines, also written to powerquery/scale/ for the parser check
SCALE_REFRESH = ("Orders", "year", 3, "month", 1)  # keep 3 years, refresh the last month


def scale_source(mart, keep, types, window=False):
    """Lakehouse SQL endpoint read; each step is a SQL clause after folding (noted per step)."""
    m = ["let", f"    Source = Sql.Database(SqlEndpoint, LakehouseDb),",
         f'    Data = Source{{[Schema = "dbo", Item = "{mart}"]}}[Data],']
    prev = "Data"
    if window:
        m += ["    // folds -> WHERE purchased_at >= @RangeStart AND purchased_at < @RangeEnd (filter first)",
              f"    #\"Filtered purchase window\" = Table.SelectRows({prev}, each [purchased_at] >= RangeStart and [purchased_at] < RangeEnd),"]
        prev = '#"Filtered purchase window"'
    m += ["    // folds -> SELECT only these columns", f'    #"Chose columns" = Table.SelectColumns({prev}, {{{", ".join(chr(34) + c + chr(34) for c in keep)}}}),',
          "    // folds -> CAST per column", f'    Typed = Table.TransformColumnTypes(#"Chose columns", {{{", ".join(types)}}})', "in", "    Typed"]
    return m


def scale_table(con, name, mart, mode, keep, prefix=None):
    cfg = TABLES[name][1]
    desc = {c[0]: c[1] for c in con.execute(f"describe mart.{mart}").fetchall()}
    lines = [f"table {q(name)}", f"{T}lineageTag: {lineage('scale', name)}"] + [f"{T}dataCategory: Time"] * (name == "Date") + [""]
    types = []
    for col in keep:
        base = desc[col].split("(")[0]
        tm, mq = ("double", "type number") if base.startswith("DECIMAL") else TYPES[base]
        types.append(f'{{"{col}", {mq}}}')
        lines += [f"{T}column {q(col_name(cfg, col))}", f"{T*2}dataType: {tm}"] + [f"{T*2}isKey"] * (col == cfg.get("key"))
        lines += [f"{T*2}isHidden"] * (col in cfg.get("hidden", []) or col.endswith("_key") and name == "Date")
        lines += [f"{T*2}formatString: {cfg.get('fmt', {}).get(col) or {'int64': '0', 'dateTime': 'dd mmm yyyy'}.get(tm, 'General')}"] \
            if tm != "string" and tm != "boolean" else []
        lines += [f"{T*2}lineageTag: {lineage('scale', name, col)}", f"{T*2}summarizeBy: none",  # agg hits come from alternateOf, not the column default
                  f"{T*2}sourceColumn: {col}"]
        if sort := cfg.get("sort", {}).get(col):
            if sort in keep:
                lines.append(f"{T*2}sortByColumn: {q(col_name(cfg, sort))}")
        lines.append("")
    window = name == SCALE_REFRESH[0]
    m = SCALE_M[name] = scale_source(mart, keep, types, window)
    if window:
        _, rg, rp, ig, ip = SCALE_REFRESH
        poll = ["let", "    Source = Sql.Database(SqlEndpoint, LakehouseDb),", f'    Data = Source{{[Schema = "dbo", Item = "{mart}"]}}[Data],',
                "    // detect data changes: refresh a partition only when its latest purchase moved",
                "    InRange = Table.SelectRows(Data, each [purchased_at] >= RangeStart and [purchased_at] < RangeEnd),",
                "    Latest = List.Max(InRange[purchased_at])", "in", "    Latest"]
        SCALE_M[f"{name} polling"] = poll
        lines += [f"{T}refreshPolicy", f"{T*2}policyType: basic", f"{T*2}rollingWindowGranularity: {rg}",
                  f"{T*2}rollingWindowPeriods: {rp}", f"{T*2}incrementalGranularity: {ig}", f"{T*2}incrementalPeriods: {ip}",
                  f"{T*2}pollingExpression =", *[f"{T*4}{l}" for l in poll], f"{T*2}sourceExpression =", *[f"{T*4}{l}" for l in m], ""]
    lines += [f"{T}partition {q(name)} = m", f"{T*2}mode: {mode}", f"{T*2}source =", *[f"{T*4}{l}" for l in m], "",
              f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def scale_agg_table():
    t = "Order Items Agg"
    lines = [f"table {q(t)}", f"{T}isHidden", f"{T}lineageTag: {lineage('scale', t)}", ""]
    for col, src, dtype, summ, bt, bc in SCALE_AGG:
        lines += [f"{T}column {q(col)}", f"{T*2}dataType: {dtype}", f"{T*2}isHidden", f"{T*2}isAvailableInMdx: false"]
        lines += [f"{T*2}formatString: {'dd mmm yyyy' if dtype == 'dateTime' else '0' if dtype == 'int64' else '#,0.00'}"] \
            if dtype in ("dateTime", "int64", "double") else []
        lines += [f"{T*2}lineageTag: {lineage('scale', t, col)}", f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: {src}", "",
                  f"{T*2}alternateOf", f"{T*3}summarization: {summ}"]
        lines += [f"{T*3}baseTable: {q(bt)}"] if bc is None else [f"{T*3}baseColumn: {q(bt)}.{q(bc)}"]
        lines.append("")
    m = ["let", "    Source = Sql.Database(SqlEndpoint, LakehouseDb),",
         '    Items = Source{[Schema = "dbo", Item = "fact_order_items"]}[Data],',
         '    Products = Table.SelectColumns(Source{[Schema = "dbo", Item = "dim_product"]}[Data], {"product_id", "category"}),',
         '    Customers = Table.SelectColumns(Source{[Schema = "dbo", Item = "dim_customer"]}[Data], {"customer_unique_id", "state"}),',
         "    // folds -> LEFT JOIN dim_product", '    #"Joined product" = Table.ExpandTableColumn(Table.NestedJoin(Items, {"product_id"}, Products, '
         '{"product_id"}, "p", JoinKind.LeftOuter), "p", {"category"}),',
         "    // folds -> LEFT JOIN dim_customer", '    #"Joined customer" = Table.ExpandTableColumn(Table.NestedJoin(#"Joined product", '
         '{"customer_unique_id"}, Customers, {"customer_unique_id"}, "c", JoinKind.LeftOuter), "c", {"state"}, {"customer_state"}),',
         "    // folds -> GROUP BY day, category, seller, customer state, validity",
         '    Grouped = Table.Group(#"Joined customer", {"order_date", "category", "seller_id", "customer_state", "is_valid"}, '
         '{{"price", each List.Sum([price]), type number}, {"freight", each List.Sum([freight]), type number}, '
         '{"items", each Table.RowCount(_), Int64.Type}}),',
         '    Typed = Table.TransformColumnTypes(Grouped, {{"order_date", type date}, {"is_valid", type logical}})', "in", "    Typed"]
    SCALE_M[t] = m
    lines += [f"{T}partition {q(t)} = m", f"{T*2}mode: import", f"{T*2}source =", *[f"{T*4}{l}" for l in m], "",
              f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def write_model_shell(path, display, logical):
    shutil.rmtree(path, ignore_errors=True)
    (path / "definition" / "tables").mkdir(parents=True)
    (path / "definition.pbism").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",\n'
        '  "version": "4.2",\n  "settings": {}\n}\n')
    (path / ".platform").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",\n'
        f'  "metadata": {{"type": "SemanticModel", "displayName": "{display}"}},\n'
        f'  "config": {{"version": "2.0", "logicalId": "{logical}"}}\n}}\n')
    (path / "definition" / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1702\n")
    return path / "definition"


def small_measures_tmdl(tag, measures):
    lines = ["table _Measures", f"{T}lineageTag: {lineage(tag, '_Measures')}", ""]
    for folder, name, fmt, desc, dax, *_ in measures:
        lines += [f"{T}/// {desc}"] * bool(desc) + [f"{T}measure {q(name)} = {dax}"]
        lines += [f"{T*2}formatString: {fmt}"] * bool(fmt) + [f"{T*2}displayFolder: {folder}", f"{T*2}lineageTag: {lineage(tag, name)}", ""]
    lines += [f"{T}column Value", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
              f"{T*2}lineageTag: {lineage(tag, '_Measures', 'Value')}", f"{T*2}summarizeBy: none", f"{T*2}isNameInferred",
              f"{T*2}sourceColumn: [Value]", "", f"{T}partition _Measures = calculated", f"{T*2}mode: import", f"{T*2}source = {{1}}", ""]
    return "\n".join(lines)


def write_scale_model(con):
    d = write_model_shell(ROOT / "powerbi" / "Olist.Scale.SemanticModel", "Olist Scale", "8a2b4c6d-1e3f-4a5b-9c7d-2e4f6a8b0c1d")
    tables = [*SCALE, "Order Items Agg", "_Measures"]
    (d / "model.tmdl").write_text("\n".join(["model Model", f"{T}culture: en-US", f"{T}defaultPowerBIDataSourceVersion: powerBI_V3",
                                             f"{T}sourceQueryCulture: en-US", "", *[f"ref table {q(t)}" for t in tables], ""]))
    params = [("SqlEndpoint", '"<workspace>.datawarehouse.fabric.microsoft.com"', "Text"), ("LakehouseDb", f'"{LAKEHOUSE_DB}"', "Text"),
              # reserved names for incremental refresh; Desktop loads what falls between them
              ("RangeStart", "#datetime(2016, 9, 1, 0, 0, 0)", "DateTime"), ("RangeEnd", "#datetime(2018, 11, 1, 0, 0, 0)", "DateTime")]
    (d / "expressions.tmdl").write_text("\n".join(
        l for n, v, k in params for l in [f'expression {n} = {v} meta [IsParameterQuery=true, Type="{k}", IsParameterQueryRequired=true]',
                                          f"{T}lineageTag: {lineage('scale', 'expr', n)}", "", f"{T}annotation PBI_ResultType = {k}", ""]))
    for name, (mart, mode, keep) in SCALE.items():
        (d / "tables" / f"{name}.tmdl").write_text(scale_table(con, name, mart, mode, keep))
    (d / "tables" / "Order Items Agg.tmdl").write_text(scale_agg_table())
    (d / "tables" / "_Measures.tmdl").write_text(small_measures_tmdl("scale", SCALE_MEASURES))
    (d / "relationships.tmdl").write_text(relationships_tmdl(SCALE_REL, rel_col))
    pq = ROOT / "powerquery" / "scale"
    pq.mkdir(parents=True, exist_ok=True)
    for n, m in SCALE_M.items():
        (pq / f"{n}.pq").write_text("\n".join(m) + "\n")


# ------------------------------------------------------------------ Olist.DirectLake: the Fabric-native variant (not run)
DL = {"Order Items": ("fact_order_items", ["order_id", "product_id", "order_date", "is_valid", "price", "freight"]),
      "Date": ("dim_date", ["date", "year", "year_month", "year_month_key"]),
      "Product": ("dim_product", ["product_id", "category", "category_group"])}


def write_directlake_model(con):
    d = write_model_shell(ROOT / "powerbi" / "Olist.DirectLake.SemanticModel", "Olist DirectLake", "5c3e7a9b-2d4f-4e6a-8b1c-3d5e7f9a1b2c")
    (d / "model.tmdl").write_text("\n".join(["model Model", f"{T}culture: en-US", f"{T}defaultPowerBIDataSourceVersion: powerBI_V3",
                                             f"{T}sourceQueryCulture: en-US", "", *[f"ref table {q(t)}" for t in [*DL, "_Measures"]], ""]))
    (d / "expressions.tmdl").write_text("\n".join([
        "/// The lakehouse SQL endpoint Direct Lake reads Delta tables through (replace both placeholders).", "expression DatabaseQuery =",
        f"{T*2}let", f'{T*2}    database = Sql.Database("<workspace>.datawarehouse.fabric.microsoft.com", "<lakehouse-id>")',
        f"{T*2}in", f"{T*2}    database", f"{T}lineageTag: {lineage('dl', 'expr')}", ""]))
    for name, (mart, keep) in DL.items():
        cfg = TABLES[name][1]
        desc = {c[0]: c[1] for c in con.execute(f"describe mart.{mart}").fetchall()}
        lines = [f"table {q(name)}", f"{T}lineageTag: {lineage('dl', name)}"] + [f"{T}dataCategory: Time"] * (name == "Date") + [""]
        for col in keep:
            base = desc[col].split("(")[0]
            tm = "double" if base.startswith("DECIMAL") else TYPES[base][0]
            lines += [f"{T}column {q(col_name(cfg, col))}", f"{T*2}dataType: {tm}"] + [f"{T*2}isKey"] * (col == cfg.get("key"))
            lines += [f"{T*2}lineageTag: {lineage('dl', name, col)}", f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: {col}", ""]
        lines += [f"{T}partition {q(name)} = entity", f"{T*2}mode: directLake", f"{T*2}source", f"{T*3}entityName: {mart}",
                  f"{T*3}schemaName: dbo", f"{T*3}expressionSource: DatabaseQuery", ""]
        (d / "tables" / f"{name}.tmdl").write_text("\n".join(lines))
    (d / "tables" / "_Measures.tmdl").write_text(small_measures_tmdl("dl", [m for m in MEASURES if m[1] in ("Revenue", "Freight Total")]))
    (d / "relationships.tmdl").write_text(relationships_tmdl([("Order Items", "order_date", "Date", "date"),
                                                             ("Order Items", "product_id", "Product", "product_id")], rel_col))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data" / "marts"))
    ap.add_argument("--plan-dir", default=str(ROOT / "data" / "plan"))
    args = ap.parse_args()
    data_dir = args.data_dir.rstrip("/\\") + ("\\" if "\\" in args.data_dir else "/")
    plan_dir = args.plan_dir.rstrip("/\\") + ("\\" if "\\" in args.plan_dir else "/")

    con = duckdb.connect(str(ROOT / "data" / "warehouse" / "olist.duckdb"), read_only=True)
    check(con)
    if OUT.exists():
        shutil.rmtree(OUT / "definition", ignore_errors=True)
    (OUT / "definition" / "tables").mkdir(parents=True, exist_ok=True)

    (OUT / "definition.pbism").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",\n'
        '  "version": "4.2",\n  "settings": {}\n}\n')
    (OUT / ".platform").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",\n'
        '  "metadata": {"type": "SemanticModel", "displayName": "Olist"},\n'
        '  "config": {"version": "2.0", "logicalId": "6f1d2c9e-4b7a-4f0e-9c3d-0a1b2c3d4e5f"}\n}\n')

    d = OUT / "definition"
    # 1702: user-defined functions and calendars need the current compatibility level
    (d / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1702\n")
    (d / "roles").mkdir()
    table_names = [*TABLES, *PQ_TABLES, *CALCULATED, "Time Calc", *FIELD_PARAMETERS, "_Measures"]
    (d / "model.tmdl").write_text("\n".join([
        "model Model",
        f"{T}culture: en-US",
        f"{T}defaultPowerBIDataSourceVersion: powerBI_V3",
        f"{T}sourceQueryCulture: en-US",
        f"{T}dataAccessOptions",
        f"{T}{T}legacyRedirects",
        f"{T}{T}returnErrorValuesAsNull",
        "",
        "annotation __PBI_TimeIntelligenceEnabled = 0",
        "",
        *[f"ref table {q(t)}" for t in table_names],
        "",
        *[f"ref role {q(r)}" for r in ROLES],
        "",
        "ref cultureInfo en-US",
        "",
    ]))
    (d / "cultures").mkdir()
    (d / "cultures" / "en-US.tmdl").write_text(culture_tmdl())
    (d / "functions.tmdl").write_text(functions_tmdl())
    for name, (dax, cols, hidden) in CALCULATED.items():
        (d / "tables" / f"{name}.tmdl").write_text(calculated_tmdl(name, dax, cols, hidden))
    (d / "tables" / "Time Calc.tmdl").write_text(calc_group_tmdl())
    for t, items in FIELD_PARAMETERS.items():
        (d / "tables" / f"{t}.tmdl").write_text(field_parameter_tmdl(t, items))
    for name, r in ROLES.items():
        (d / "roles" / f"{name}.tmdl").write_text(role_tmdl(name, r))
    (d / "expressions.tmdl").write_text(expressions_tmdl({"DataFolder": data_dir, "PlanFolder": plan_dir}))

    for name, (mart, cfg) in TABLES.items():
        (d / "tables" / f"{name}.tmdl").write_text(table_tmdl(con, name, mart, cfg))
    pq = ROOT / "powerquery"  # every query as the Advanced Editor shows it, one .pq file each (review + diff)
    shutil.rmtree(pq, ignore_errors=True)
    pq.mkdir()
    for name, m in MART_M.items():
        (pq / f"{name}.pq").write_text("\n".join(m) + "\n")
    for name in PQ_TABLES:
        (d / "tables" / f"{name}.tmdl").write_text(pq_table_tmdl(name))
        (pq / f"{name}.pq").write_text("\n".join(PQ_TABLES[name][1]) + "\n")
    for name, (_, _, m) in PQ_STAGING.items():
        (pq / f"{name}.pq").write_text("\n".join(m) + "\n")
    (d / "tables" / "_Measures.tmdl").write_text(measures_tmdl())

    (d / "relationships.tmdl").write_text(relationships_tmdl(RELATIONSHIPS, rel_col))
    write_dax_queries()
    write_scale_model(con)
    write_directlake_model(con)

    print(f"→ {OUT.relative_to(ROOT)}  ({len(table_names)} tables, {len(RELATIONSHIPS)} relationships, {len(MEASURES)} measures, "
          f"{len(FUNCTIONS)} functions, {len(ROLES)} roles)")
    print(f"  DataFolder = {data_dir}")


if __name__ == "__main__":
    main()
