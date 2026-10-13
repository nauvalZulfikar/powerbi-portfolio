"""Generate the Power BI report (PBIR) — 6 designed pages over the StockRestock semantic model.

    uv run python src/build_report.py

Writes powerbi/StockRestock.Report + StockRestock.pbip, then runs `pbir validate`
(schema + field references against the model). Open StockRestock.pbip in Power BI Desktop.

Colour rule: orange = order now, purple = excess stock, green = everything else.

Power BI features placed here (the model side is in build_model.py):
  * analytics pane: forecast with a 95% band and anomaly detection on daily January sales (Trends page)
  * visual calculations: RUNNINGSUM (Trends), MOVINGAVERAGE (Vendors), RANK (Stores)
  * the Time Intelligence calculation group as a period matrix (Trends)
  * the new card visual (cardVisual) and a button slicer (advancedSlicerVisual) on Trends
  * synced slicers: City / Vendor / Category / ABC sync on every page through syncGroup; the Trends
    button slicer joins the Category group
  * a phone layout for the Overview page (mobile.json per visual)
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from build_model import MEASURES, USD_AUTO

ROOT = Path(__file__).resolve().parents[1]
PBI = ROOT / "powerbi"
REPORT = PBI / "StockRestock.Report"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"

W, H = 1600, 900
SIDEBAR = 232
X0, X1, GAP = 256, 1576, 16
CW = X1 - X0

# ---------------------------------------------------------------- palette
# MUTED was #898781 (3.5:1 on SURFACE); darkened so every text colour clears WCAG AA 4.5:1 (contrast_check).
INK, INK2, MUTED, HAIR, SURFACE, PLANE = "#0b0b0b", "#52514e", "#6F6D68", "#E6E4DD", "#FCFCFB", "#F4F4F1"
SIDE, SIDE_TEXT, SIDE_LINE = "#17302a", "#a9c2b8", "#2b4a40"
DUE_C, SLOW_C, AMBER = "#C2410C", "#6d4fb3", "#B45309"
ACCENT = "#2f7d62"


# ------------------------------------------------------------ PBIR helpers
def lit_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v}L"
    if isinstance(v, float):
        return f"{v}D"
    return "'" + str(v).replace("'", "''") + "'"


def lit(v):
    return {"expr": {"Literal": {"Value": lit_value(v)}}}


def color(hex_):
    return {"solid": {"color": lit(hex_)}}


def field(ref):
    table, name = ref.split(".", 1)
    kind = "Measure" if table == "_Measures" else "Column"
    return {kind: {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}}


def projection(ref):
    if isinstance(ref, dict):
        return ref
    return {"field": field(ref), "queryRef": ref, "nativeQueryRef": ref.split(".", 1)[1]}


def field_hier(table, hierarchy, lv):
    return {"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {"SourceRef": {"Entity": table}},
                                                            "Hierarchy": hierarchy}}, "Level": lv}}


def level(table, hierarchy, lv):
    """A projection on one level of a model hierarchy (drill-down follows the hierarchy's order)."""
    ref = f"{table}.{hierarchy}.{lv}"
    return {"field": field_hier(table, hierarchy, lv), "queryRef": ref, "nativeQueryRef": lv}


def vcalc(name, expr, hidden=False):
    """A visual calculation: DAX evaluated on the visual's own matrix (rows / columns), not in the model."""
    p = {"field": {"NativeVisualCalculation": {"Language": "dax", "Expression": expr, "Name": name,
                                                "DataType": "Double"}},
         "queryRef": name, "nativeQueryRef": name}
    return {**p, "hidden": True} if hidden else p


def hidden(ref):
    """A field the visual queries (so visual calculations can use it) but doesn't draw."""
    return {**projection(ref), "hidden": True}


def props(**kw):
    """One formatting object: plain python values become literals; dicts pass through."""
    return [{"properties": {k: (v if isinstance(v, dict) else lit(v)) for k, v in kw.items()}}]


def measure_color(ref, hex_):
    return {"properties": {"fill": color(hex_)}, "selector": {"metadata": ref}}


def expr_color(measure_ref):
    return {"solid": {"color": {"expr": field(measure_ref)}}}


WILDCARD = {"data": [{"dataViewWildcard": {"matchingOption": 1}}]}


def in_condition(ref, values, alias="t"):
    table, col = ref.split(".", 1)
    return {"Condition": {"In": {
        "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": alias}}, "Property": col}}],
        "Values": [[{"Literal": {"Value": lit_value(v)}}] for v in values]}}}


def in_filter(ref, values):
    """Categorical 'is one of' filter, usable in a visual's filterConfig or a slicer's default selection."""
    table, col = ref.split(".", 1)
    return {"Version": 2, "From": [{"Name": "t", "Entity": table, "Type": 0}], "Where": [in_condition(ref, values)]}


def visual_filter(name, ref, values, exclude=False):
    f = in_filter(ref, values)
    if exclude:
        f["Where"] = [{"Condition": {"Not": {"Expression": f["Where"][0]["Condition"]}}}]
    return {"name": name, "field": field(ref), "type": "Categorical", "filter": f}


def topn_filter(name, ref, by_measure, n):
    """Top N filter (filters pane 'Top N'): keep the n values of ref with the highest by_measure."""
    table, col = ref.split(".", 1)
    mtable, mname = by_measure.split(".", 1)
    sub = {"Version": 2,
           "From": [{"Name": "t", "Entity": table, "Type": 0}, {"Name": "m", "Entity": mtable, "Type": 0}],
           "Select": [{"Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": col}, "Name": "field"}],
           "OrderBy": [{"Direction": 2, "Expression": {"Measure": {"Expression": {"SourceRef": {"Source": "m"}},
                                                                   "Property": mname}}}],
           "Top": n}
    return {"name": name, "field": field(ref), "type": "TopN", "filter": {
        "Version": 2,
        "From": [{"Name": "subquery", "Expression": {"Subquery": {"Query": sub}}, "Type": 2},
                 {"Name": "t", "Entity": table, "Type": 0}],
        "Where": [{"Condition": {"In": {
            "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "t"}}, "Property": col}}],
            "Table": {"SourceRef": {"Source": "subquery"}}}}}]}}


def measure_filter(name, ref, kind, value):
    """Visual-level filter on a measure (filters pane 'Advanced'): ComparisonKind 0 =, 1 >, 2 >=, 3 <, 4 <=."""
    table, prop = ref.split(".", 1)
    return {"name": name, "field": field(ref), "type": "Advanced", "filter": {
        "Version": 2, "From": [{"Name": "m", "Entity": table, "Type": 0}],
        "Where": [{"Condition": {"Comparison": {
            "ComparisonKind": kind, "Left": {"Measure": {"Expression": {"SourceRef": {"Source": "m"}}, "Property": prop}},
            "Right": {"Literal": {"Value": lit_value(float(value))}}}}}]}}


def conditional(input_ref, cases):
    """Rules-based value: cases = [(ComparisonKind, threshold, literal value)], first match wins."""
    return {"Conditional": {"Cases": [
        {"Condition": {"Comparison": {"ComparisonKind": k, "Left": field(input_ref),
                                      "Right": {"Literal": {"Value": lit_value(float(t))}}}},
         "Value": {"Literal": {"Value": lit_value(v)}}} for k, t, v in cases]}}


def cf_rules(target, input_ref, cases, prop="fontColor"):
    """Conditional formatting by rules on one table column (font or background colour)."""
    return {"properties": {prop: {"solid": {"color": {"expr": conditional(input_ref, cases)}}}},
            "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}], "metadata": target}}


def cf_icons(target, input_ref, cases):
    """Conditional formatting icons (shape + colour) in front of a column's values."""
    return {"properties": {"icon": {"kind": "Icon", "layout": lit("Before"), "verticalAlignment": lit("Middle"),
                                    "value": {"expr": conditional(input_ref, cases)}}},
            "selector": {"data": [{"dataViewWildcard": {"matchingOption": 0}}], "metadata": target}}


def cf_gradient(ref, low, high, prop="backColor"):
    """Two-colour gradient on a measure column (lowest value -> low, highest -> high)."""
    rule = {"linearGradient2": {"min": {"color": {"Literal": {"Value": lit_value(low)}}},
                                "max": {"color": {"Literal": {"Value": lit_value(high)}}},
                                "nullColoringStrategy": {"strategy": {"Literal": {"Value": "'asZero'"}}}}}
    return {"properties": {prop: {"solid": {"color": {"expr": {"FillRule": {"Input": field(ref), "FillRule": rule}}}}}},
            "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}], "metadata": ref}}


def data_bars(ref, hex_):
    return {"properties": {"dataBars": {"positiveColor": color(hex_), "negativeColor": color(DUE_C),
                                        "axisColor": color(HAIR), "reverseDirection": lit(False), "hideText": lit(False)}},
            "selector": {"metadata": ref}}


# Stock status: never colour alone. Every status keeps its text label and gets an icon shape as well.
STATUS_CODE = "_Measures.Status Code"
STATUS_FONT = [(0, 1, DUE_C), (0, 2, AMBER), (0, 4, SLOW_C), (0, 5, SLOW_C)]
STATUS_ICONS = [(0, 1, "SymbolLow"), (0, 2, "SymbolMedium"), (0, 3, "SymbolHigh"), (0, 4, "FlagMedium"), (0, 5, "FlagLow")]


def status_cf(extra=()):
    return {"values": [cf_rules("Status.Status", STATUS_CODE, STATUS_FONT), cf_icons("Status.Status", STATUS_CODE, STATUS_ICONS),
                       *extra]}


def drill_header():
    """Theme hides the header icons; drill and export visuals need them."""
    return {"visualHeader": props(show=True, transparency=0.0, showDrillRoleSelector=True, showDrillUpButton=True,
                                  showDrillToggleButton=True, showDrillDownLevelButton=True,
                                  showDrillDownExpandButton=True, showOptionsMenu=True)}


# Measures with a dynamic format string ($940K / $3.52M): visuals must not rescale them again.
DYNAMIC = {m[1] for m in MEASURES if m[2] == USD_AUTO}
AS_IS = 1  # labelDisplayUnits "None"


class Page:
    def __init__(self, pid, name, title, subtitle):
        self.id, self.name, self.title, self.subtitle = pid, name, title, subtitle
        self.visuals = []
        self.mobile = {}  # visual name -> phone-layout position
        self.slicers = []  # (visual name, field, default value or None) for the Reset bookmark
        self.interactions = []  # (source, target, type) -> page.json visualInteractions
        self.filters = []  # page-level filters
        self.page_extra = {}  # page.json keys for drillthrough / tooltip pages
        self.deferred = []  # bookmark builders that need every visual on the page to exist first

    def get(self, name):
        return next(v for v in self.visuals if v["name"] == name)

    def group(self, name, display, children):
        """A selection-pane group (visualGroup container); the children get parentGroupName."""
        kids = [self.get(c) for c in children]
        x0 = min(k["position"]["x"] for k in kids)
        y0 = min(k["position"]["y"] for k in kids)
        x1 = max(k["position"]["x"] + k["position"]["width"] for k in kids)
        y1 = max(k["position"]["y"] + k["position"]["height"] for k in kids)
        z = len(self.visuals) * 100
        self.visuals.append({"$schema": f"{SCHEMA}/visualContainer/2.9.0/schema.json", "name": name,
                             "position": {"x": x0, "y": y0, "z": z, "width": x1 - x0, "height": y1 - y0, "tabOrder": z},
                             "visualGroup": {"displayName": display, "groupMode": "ScaleMode"}})
        for k in kids:
            k["parentGroupName"] = name

    def add(self, name, vtype, x, y, w, h, roles=None, title=None, subtitle=None,
            objects=None, container=None, sort=None, filters=None, alt=None, hidden_=False, **visual_extra):
        vc = dict(container or {})
        # Alt text for screen readers: the business question the visual answers (ALT), else its title.
        alt = alt or ALT.get(name) or (f"{title}. {subtitle}" if title and subtitle else title)
        if alt and vtype not in ("textbox", "actionButton"):
            vc.setdefault("general", props(altText=alt))
        if title is not None:
            vc.setdefault("title", props(show=True, text=title))
            if subtitle:
                vc.setdefault("subTitle", props(show=True, text=subtitle))
        elif vtype not in ("textbox", "actionButton"):
            vc.setdefault("title", props(show=False))
        query = {"queryState": {role: {"projections": [projection(r) for r in ([refs] if isinstance(refs, str) else refs)]}
                                for role, refs in (roles or {}).items()}}
        if sort:
            ref, direction = sort
            query["sortDefinition"] = {"sort": [{"field": field(ref), "direction": direction}], "isDefaultSort": False}
        visual = {"visualType": vtype, "objects": objects or {}, "visualContainerObjects": vc,
                  "drillFilterOtherVisuals": True, **visual_extra}
        if roles:
            visual["query"] = query
        z = len(self.visuals) * 100
        container_json = {
            "$schema": f"{SCHEMA}/visualContainer/2.9.0/schema.json",
            "name": name,
            "position": {"x": x, "y": y, "z": z, "width": w, "height": h, "tabOrder": z},
            "visual": visual,
        }
        if filters:
            container_json["filterConfig"] = {"filters": filters}
        if hidden_:
            container_json["isHidden"] = True
        self.visuals.append(container_json)


def text_runs(*runs):
    """runs: (text, size_pt, color, bold) tuples -> one paragraph each."""
    return [{"textRuns": [{"value": t, "textStyle": {
        "fontFamily": "Segoe UI Semibold" if bold else "Segoe UI", "fontSize": f"{size}pt", "color": col}}]}
        for t, size, col, bold in runs]


def textbox(page, name, x, y, w, h, *runs):
    page.add(name, "textbox", x, y, w, h, objects={"general": [{"properties": {"paragraphs": text_runs(*runs)}}]},
             container={"background": props(show=False), "border": props(show=False),
                        "padding": props(top=0, bottom=0, left=0, right=0)})


def slicer(page, name, x, y, w, h, ref, mode="Dropdown", single=True, default=None, header=True, sync=None, **extra):
    general = {"properties": {"filter": {"filter": in_filter(ref, [default])}}} if default is not None else None
    objects = {"data": props(mode=mode),
               "header": props(show=header, fontColor=color(MUTED), textSize=9.0),
               "selection": props(singleSelect=single, strictSingleSelect=single)}
    if general:
        objects["general"] = [general]
    objects.update(extra.pop("objects", {}))
    page.slicers.append((name, ref, default))
    kw = {"syncGroup": {"groupName": sync, "fieldChanges": True, "filterChanges": True}} if sync else {}
    extra.setdefault("alt", f"Slicer: filter this page by {extra.get('title') or ref.split('.', 1)[1]}")
    page.add(name, "slicer", x, y, w, h, roles={"Values": ref}, objects=objects, **kw, **extra)



# --------------------------------------------------------- page scaffolding
PAGES = [
    ("overview", "Overview", "Stock & restock",
     "What is on the shelf at 31 Dec 2016, what has to be ordered now, and what is not moving."),
    ("restock", "Restock List", "Restock list",
     "Lines at or below their reorder point. Move the sliders to trade stock-outs against cash."),
    ("health", "Stock Health", "Stock health",
     "Days of cover, overstock and dead stock: where the cash sits."),
    ("demand", "Demand & Forecast", "Demand & forecast",
     "January 2016 sales by day and weekday, and how four forecast methods scored."),
    ("trends", "Trends & Periods", "Trends & periods",
     "Daily sales with Power BI's forecast and anomaly detection, and receipts across calendar periods."),
    ("vendors", "Vendors & Lead Time", "Vendors & lead time",
     "Spend, lead time from PO to receipt, freight and payment terms per vendor."),
    ("stores", "Stores", "Stores",
     "Sales, stock, stock-outs and the order due, store by store."),
    ("assortment", "Assortment", "Assortment",
     "Which items carry the sales, which lines are new, and which are going quiet."),
    ("movement", "Movement & Outliers", "Movement & outliers",
     "How the stock moved in 2016, how POs flow to payment, and the lines far outside normal cover."),
    ("diagnose", "Diagnose", "Diagnose",
     "Why lines run out of stock and where the order comes from: AI visuals and Q&A."),
]
# Drillthrough pages: hidden from navigation, reached by right-click > Drill through on an item or a vendor.
DETAIL_PAGES = [
    ("item_detail", "Item Detail", "Item detail",
     "One item across the stores: on hand against reorder point and order-up-to level.", "Item.Item"),
    ("vendor_detail", "Vendor Detail", "Vendor detail",
     "One vendor: lead-time spread, invoices that don't match a receipt, items shared with other vendors.",
     "Vendor.Vendor"),
]
# Report-page tooltips: (id, name) — 320 × 240 canvases shown when hovering a bar.
TOOLTIP_PAGES = [("vendor_tip", "Vendor Tip"), ("line_tip", "Line Tip")]
TIP_W, TIP_H = 320, 240

# The "?" button on every page opens this (help bookmark); the restock rule sits on the Restock page.
HELP = {
    "assortment": "Pareto: items ranked by 2016 sales (top 40 drawn); the line is the running share of all sales in the "
                  "selection. ABC Dynamic re-ranks the brands inside the slicers; ABC Static is the chain-wide class. "
                  "New in 2016 = in the closing count but not the opening count. Dormant = stock on hand, no January "
                  "sales, no receipt in the last 120 days of 2016.",
    "movement": "Stock value change = (closing − opening units) × 2016 unit cost. The matrix crosses the 1 Jan state "
                "with the 31 Dec status. Funnel steps keep the conditions of the steps before. Outlier = cover above "
                "the 95th percentile of its category. Band edges come from the Cover Band table (Min / Max Days).",
    "overview": "Status is set per store × item at the 31 Dec 2016 count. Order now = at or below the reorder point "
                "(demand over the lead time + safety stock). Excess = cost of units beyond 180 days of cover plus dead stock. "
                "Pick a dimension above the bottom-left chart; drill the right chart from category to pack size to item.",
    "restock": "Safety stock = z × √(lead days × daily spread² + daily demand² × lead-time spread²). Reorder point = "
               "demand over the lead time + safety stock. A due line is topped up to demand over lead time + review "
               "period + safety stock. Hover a vendor for its line totals; switch the table between Order now and Excess.",
    "health": "Days of cover = units on hand ÷ forecast daily demand. Overstock = more than 180 days; dead stock = "
              "sold nothing in 2016. The bridge walks opening stock to closing stock at 2016 unit cost. Toggle the "
              "middle panel between pack sizes and ABC classes.",
    "demand": "The forecast behind the restock list is the 2016 daily rate × a January season factor × a weekday "
              "index, chosen because it scored the lowest bias in the 25–31 Jan backtest.",
    "trends": "The forecast band and anomaly flags come from Power BI's analytics pane; the period matrix is the "
              "Time Intelligence calculation group on received value.",
    "vendors": "Lead time = PO date to receiving date per shipment. Click a vendor to filter the scorecard and highlight "
               "its months; hover it for lead-time P90, freight and payment days; right-click > Drill through for detail.",
    "stores": "Click a store to filter the out-of-stock and cover charts. Arrows in the scorecard: ▲ under 15% "
              "out of stock, ► 15–20%, ▼ above 20%.",
    "diagnose": "Key influencers ranks what raises the chance a line is out of stock; the decomposition tree splits "
                "the order by vendor, category and store; ask Q&A in plain words (supplier, branch, stockout rate).",
    "item_detail": "Bars = units on hand, grey = order-up-to level, orange tick = reorder point, per store. "
                   "Back returns to the page you came from.",
    "vendor_detail": "Lead-time histogram = shipments by days from PO to receipt. Unmatched invoices quote a PO "
                     "number that never appears on this vendor's receiving lines.",
}

ROW_KPI_Y, ROW_KPI_H = 104, 96
ROW2_Y, ROW2_H = 216, 332
ROW3_Y, ROW3_H = 564, 316


# Alt text: the business question each visual answers (screen readers read this, not the chart).
ALT = {
    "cards_trend": "January sales, units sold, average selling price and received value for the current selection.",
    "col_status": "How many store-item lines are out of stock, due for reorder, healthy, overstocked or dead?",
    "card_headline": "One-sentence summary of stock value, the order due now and excess stock.",
    "bar_order_by": "Where is the order due now concentrated, by the dimension picked in the slicer?",
    "bar_excess_by": "Which categories, pack sizes and items hold the excess stock? Drillable chart.",
    "bar_order_vendor": "How much has to be ordered from each vendor at the selected service level and review period?",
    "table_order": "Which store-item lines must be ordered now, how many units and at what cost?",
    "table_excess": "Which lines hold stock beyond 180 days of cover?",
    "col_cover": "How much stock value sits in each days-of-cover band?",
    "matrix_product": "How is stock and excess split by category, pack size and item?",
    "table_abc": "How do A, B and C items differ in stock, stock-outs and dead stock?",
    "waterfall_bridge": "How did stock value move from opening (1 Jan) to closing (31 Dec 2016): received in, sold out?",
    "table_slow": "Which overstock and dead-stock lines tie up the most cash?",
    "line_backtest": "How close did each forecast method get to actual chain units in the last January week?",
    "col_weekday": "Which weekdays sell more than the average day?",
    "table_scores": "Which forecast method has the lowest error and bias?",
    "col_metric": "The forecast scores again, one small panel per method.",
    "line_forecast": "What does the next week of daily units look like, with a 95% band?",
    "line_anomaly": "Which January days sold unusually high or low?",
    "matrix_periods": "How does received value compare across calendar periods?",
    "line_cumulative": "How did January sales accumulate day by day?",
    "bar_spend_vendor": "Which vendors take the most purchase spend?",
    "col_received": "How do goods received and cash paid compare month by month? Drillable from year to month.",
    "table_vendors": "Vendor scorecard with lead times, received value and the order due, safe for Store Staff.",
    "table_multi_sourced": "Which items are bought from more than one vendor?",
    "bar_store_sales": "How do stores rank on the metric picked above?",
    "bar_store_oos": "Which stores run out of stock most often?",
    "scatter_cover": "Which stores have thin stock and a big order due at the same time?",
    "table_stores": "Store scorecard: sales, stock, cover, stock-outs and the order due.",
    "key_influencers": "What raises the chance that a store-item line is out of stock?",
    "decomp_order": "Which vendor, category and store make up the order due?",
    "qna": "Ask a question about stock in plain words.",
    "deneb_bullet": "For this item, how does each store's stock compare with its reorder point and order-up-to level?",
    "table_item_vendor": "Which vendor supplies this item, its lead time and the order due?",
    "table_item_store": "Per store: status, units on hand, reorder point and units to order for this item.",
    "col_lead_days": "How are this vendor's lead times spread across shipments?",
    "table_vendor_multi": "Which of this vendor's items are also bought from other vendors?",
    "table_unmatched": "Which invoices quote a PO number with no receiving line?",
    "table_vendor_order": "Which of this vendor's items are due for ordering?",
    "card_vendor_tip": "Lead time P90, freight share, payment days and shipments for the hovered vendor.",
    "card_line_tip": "Safety stock, reorder point and units to order for the hovered vendor.",
}


def cols(*widths):
    """x positions for panels of the given widths separated by GAP, starting at X0."""
    xs, x = [], X0
    for w in widths:
        xs.append(x)
        x += w + GAP
    assert abs(x - GAP - X1) <= 1, f"row widths {widths} sum to {x - GAP - X0}, expected {CW}"
    return xs



def nav_pages():
    return PAGES


def scaffold(page):
    textbox(page, "brand", 24, 28, 190, 64,
            ("BIBITOR LLC", 8, SIDE_TEXT, True), ("Stock & Restock", 17, "#FFFFFF", True))
    for i, (pid, name, *_) in enumerate(nav_pages()):
        active = pid == page.id
        page.add(f"nav_{i}", "actionButton", 12, 128 + i * 46, 208, 40,
                 objects={
                     "text": props(show=True, text=name, fontColor=color("#FFFFFF" if active else SIDE_TEXT),
                                   fontSize=11.0, fontFamily="Segoe UI Semibold" if active else "Segoe UI",
                                   horizontalAlignment="left", leftMargin=16),
                     "fill": props(show=active, fillColor=color("#FFFFFF"), transparency=86.0),
                     "outline": props(show=False),
                     "icon": props(show=False),
                     "shape": props(tileShape="rectangleRounded", rectangleRoundedCurve=8),
                 },
                 container={"visualLink": props(show=True, type="PageNavigation", navigationSection=pid),
                            "background": props(show=False), "border": props(show=False)})
    textbox(page, "footer", 24, 790, 190, 90,
            ("Source: Bibitor LLC inventory case data, 2016 (Kaggle mirror, Apache 2.0)", 8, SIDE_TEXT, False),
            ("Pipeline: CSV · DuckDB SQL · Power BI (TMDL + PBIR)", 8, SIDE_TEXT, False))

    textbox(page, "title", X0, 22, 452, 70, (page.title, 20, INK, True), (page.subtitle, 10, INK2, False))
    sx = cols(452, 210, 280, 140, 126, 32)
    slicer(page, "slicer_city", sx[1], 16, 210, 76, "Store.City", single=False, sync="City")
    slicer(page, "slicer_vendor", sx[2], 16, 280, 76, "Vendor.Vendor", single=False, sync="Vendor")
    if page.id != "trends":  # Trends has a button slicer in the same sync group instead
        slicer(page, "slicer_category", sx[3], 16, 140, 76, "Item.Category", single=False, sync="Category")
    slicer(page, "slicer_abc", sx[4], 16, 126, 76, "ABC.ABC Class", single=False, sync="ABC")
    # Reset (data bookmark: every slicer back to its default) and help (display bookmark: shows the help panel).
    for name, y, glyph, bm, tip in (("btn_reset", 16, "↺", f"reset_{page.id}", "Reset all slicers on this page"),
                                    ("btn_help", 56, "?", f"help_{page.id}", "How this page works")):
        page.add(name, "actionButton", sx[5], y, 32, 36,
                 objects={"text": props(show=True, text=glyph, fontColor=color(INK2), fontSize=12.0,
                                        fontFamily="Segoe UI Semibold", horizontalAlignment="center"),
                          "fill": props(show=True, fillColor=color(SURFACE), transparency=0.0),
                          "outline": props(show=True, lineColor=color(HAIR), weight=1.0),
                          "icon": props(show=False),
                          "shape": props(tileShape="rectangleRounded", rectangleRoundedCurve=8)},
                 container={"visualLink": props(show=True, type="Bookmark", bookmark=bm, tooltip=tip),
                            "background": props(show=False), "border": props(show=False)})


def help_panel(page):
    """Hidden help text + close button, shown by the help_<page> bookmark and hidden again by nohelp_<page>."""
    x, w = X1 - 560, 560
    page.add("help_text", "textbox", x, 100, w, 150,
             objects={"general": [{"properties": {"paragraphs": text_runs(
                 ("How this page works", 11, INK, True), (HELP[page.id], 10, INK2, False))}}]},
             container={"background": props(show=True, color=color("#FFFFFF")), "border": props(show=True, color=color(INK2)),
                        "padding": props(top=14, bottom=14, left=16, right=44), "dropShadow": props(show=True)},
             hidden_=True)
    page.add("help_close", "actionButton", X1 - 40, 108, 28, 28,
             objects={"text": props(show=True, text="×", fontColor=color(INK2), fontSize=12.0, horizontalAlignment="center"),
                      "fill": props(show=False), "outline": props(show=False), "icon": props(show=False)},
             container={"visualLink": props(show=True, type="Bookmark", bookmark=f"nohelp_{page.id}", tooltip="Close help"),
                        "background": props(show=False), "border": props(show=False)},
             hidden_=True)
    page.group("help_group", "Help", ["help_text", "help_close"])
    page.get("help_group")["isHidden"] = True
    BOOKMARKS.append(display_bookmark(page, f"help_{page.id}", f"Help · {page.name}", show=["help_group"], hide=[]))
    BOOKMARKS.append(display_bookmark(page, f"nohelp_{page.id}", f"Close help · {page.name}", show=[], hide=["help_group"]))


# ------------------------------------------------------------- bookmarks
BOOKMARKS = []  # bookmark JSON objects, written to definition/bookmarks
BOOKMARK_GROUPS = {}  # group name -> (display name, [bookmark names])


def bookmark(name, display, page, containers, options, groups=None):
    section = {"visualContainers": containers}
    if groups:
        section["visualContainerGroups"] = groups
    return {"$schema": f"{SCHEMA}/bookmark/2.1.0/schema.json", "name": name, "displayName": display,
            "explorationState": {"version": "1.3", "activeSection": page.id, "sections": {page.id: section}},
            "options": options}


def display_bookmark(page, name, display, show, hide):
    """Display-only bookmark on a few visuals or groups: shows `show`, hides `hide`, leaves data and the rest alone."""
    groups = {g: {"isHidden": g in hide} for g in show + hide if g.endswith("_group")}
    singles = {v: {"singleVisual": {"visualType": page.get(v)["visual"]["visualType"],
                                    **({"display": {"mode": "hidden"}} if v in hide else {})}}
               for v in show + hide if not v.endswith("_group")}
    return bookmark(name, display, page, singles,
                    {"suppressData": True, "applyOnlyToTargetVisuals": True, "targetVisualNames": show + hide},
                    groups or None)


def reset_bookmark(page):
    """Data bookmark: every slicer on the page back to its default selection (none, or the what-if default)."""
    def state(ref, default):
        objs = {"merge": {"general": [{"properties": {"filter": {"filter": in_filter(ref, [default])}}}]}} \
            if default is not None else {}
        return {"singleVisual": {"visualType": "slicer", "objects": objs}}
    names = [n for n, _, _ in page.slicers]
    return bookmark(f"reset_{page.id}", f"Reset · {page.name}", page,
                    {n: state(r, d) for n, r, d in page.slicers},
                    {"suppressDisplay": True, "applyOnlyToTargetVisuals": True, "targetVisualNames": names})


def kpis(page, measures, x0=X0, width=CW):
    w = (width - GAP * (len(measures) - 1)) // len(measures)
    for i, (m, units, precision) in enumerate(measures):
        units = AS_IS if m in DYNAMIC else units
        page.add(f"kpi_{i}", "card", x0 + i * (w + GAP), ROW_KPI_Y, w, ROW_KPI_H, roles={"Values": f"_Measures.{m}"},
                 alt=f"KPI card: {m}, for the current slicer selection.",
                 objects={"labels": props(color=color(INK), fontSize=22.0, fontFamily="Segoe UI Semibold",
                                          labelDisplayUnits=units, labelPrecision=precision),
                          "categoryLabels": props(show=True, color=color(MUTED), fontSize=9.0)})


M, K, NONE = 1000000, 1000, 0


def bar_labels(units=K):
    return props(show=True, color=color(INK2), fontSize=8.0, labelDisplayUnits=units, labelPrecision=0)


def axes(show_x=True, show_y=True, grid=True):
    return {"categoryAxis": props(show=show_x, showAxisTitle=False, fontSize=9.0, labelColor=color(MUTED)),
            "valueAxis": props(show=show_y, showAxisTitle=False, fontSize=9.0, labelColor=color(MUTED),
                               gridlineShow=grid, gridlineColor=color("#ECEBE6"), gridlineStyle="solid")}


def legend(position="Top"):
    return props(show=True, position=position, fontSize=9.0, labelColor=color(INK2), showTitle=False)



def fill(hex_):
    return props(fill=color(hex_))


def table(page, name, x, y, w, h, values, title, subtitle=None, sort=None, **kw):
    page.add(name, "tableEx", x, y, w, h, roles={"Values": values}, title=title, subtitle=subtitle, sort=sort,
             objects={"grid": props(rowPadding=6), **kw.pop("objects", {})}, **kw)


def bar(page, name, vtype, x, y, w, h, category, measure, hex_, title, subtitle=None, units=K, sort=None,
        tooltips=None, **kw):
    units = AS_IS if measure.split(".", 1)[1] in DYNAMIC else units
    roles = {"Category": category, "Y": measure, **({"Tooltips": tooltips} if tooltips else {})}
    page.add(name, vtype, x, y, w, h, roles=roles, title=title, subtitle=subtitle,
             sort=sort, objects={**axes(show_y=vtype == "clusteredColumnChart", grid=False),
                                 "labels": bar_labels(units), "dataPoint": fill(hex_)}, **kw)


def analyze_slicer(page, x, y, w):
    page.slicers.append(("slicer_analyze", "Analyze By.Analyze By", "Vendor"))
    page.add("slicer_analyze", "slicer", x, y, w, 44, roles={"Values": "Analyze By.Analyze By"},
             alt="Slicer: pick the dimension the order-value chart is split by (field parameter)",
             objects={"data": props(mode="HorizontalList"), "header": props(show=False),
                      "selection": props(singleSelect=True, strictSingleSelect=True),
                      "items": props(fontColor=color(INK2), textSize=9.0),
                      "general": [{"properties": {"filter": {"filter": in_filter("Analyze By.Analyze By", ["Vendor"])}}}]},
             container={"background": props(show=False), "border": props(show=False)})


def navigator(page, name, x, y, w, h, group, display, items):
    """Bookmark navigator over a bookmark group; items = (bookmark, label, show, hide) display-only bookmarks."""
    page.add(name, "bookmarkNavigator", x, y, w, h,
             objects={"bookmarks": props(bookmarkGroup=group, allowDeselectionBookmark=False, selectedBookmark=items[0][0]),
                      "text": props(fontColor=color(INK2), fontSize=10.0),
                      "fill": props(fillColor=color(SURFACE))},
             container={"background": props(show=False), "border": props(show=False)})
    page.deferred.append(lambda: [BOOKMARKS.append(display_bookmark(page, bm, label, show, hide))
                                  for bm, label, show, hide in items])
    BOOKMARK_GROUPS[group] = (display, [bm for bm, *_ in items])


# ------------------------------------------------------------------- pages
DUE = ["Out of stock", "Reorder now"]
SLOW = ["Overstock", "Dead stock"]


def overview(p):
    kpis(p, [("Stock Value", M, 2), ("Stock Turns", NONE, 1), ("Out of Stock Rate", NONE, 1),
             ("Lines To Order", NONE, 0), ("Order Value", M, 2), ("Excess Stock Value", M, 2)])
    a, b = cols(880, 424)
    bar(p, "col_status", "clusteredColumnChart", a, ROW2_Y, 880, ROW2_H, "Status.Status", "_Measures.Lines", ACCENT,
        "Store × item lines by stock status", "At 31 Dec 2016; a line is one item in one store")
    p.add("card_headline", "card", b, ROW2_Y, 424, ROW2_H, roles={"Values": "_Measures.Headline"},
          title="In one sentence",
          objects={"labels": props(color=color(INK), fontSize=14.0, fontFamily="Segoe UI"),
                   "categoryLabels": props(show=False), "wordWrap": props(show=True)})
    a, b = cols(652, 652)
    analyze_slicer(p, a, ROW3_Y, 652)
    y, h = ROW3_Y + 52, ROW3_H - 52
    bar(p, "bar_order_by", "clusteredBarChart", a, y, 652, h, "Analyze By.Analyze By", "_Measures.Order Value", DUE_C,
        "Order value due now, by the dimension picked above", sort=("_Measures.Order Value", "Descending"))
    # Drill down / up on the Product hierarchy; opens on Pack Size (two categories alone say little).
    levels = [level("Item", "Product", lv) for lv in ("Category", "Pack Size", "Item")]
    levels[1]["active"] = True
    bar(p, "bar_excess_by", "clusteredBarChart", b, ROW3_Y, 652, ROW3_H, levels, "_Measures.Excess Stock Value",
        SLOW_C, "Excess stock by pack size", "Drill up to category or down to item (header arrows); cost beyond 180 days of cover plus dead stock",
        sort=("_Measures.Excess Stock Value", "Descending"), container=drill_header())
    # Edit interactions: the two bottom charts filter the status chart; the headline sentence stays chain-wide.
    for src in ("bar_order_by", "bar_excess_by"):
        p.interactions += [(src, "col_status", "DataFilter"), (src, "card_headline", "NoFilter")]
    phone_layout(p, [["title"], ["kpi_0", "kpi_4"], ["kpi_2", "kpi_3"], ["kpi_1", "kpi_5"], ["card_headline"],
                     ["col_status"], ["slicer_analyze"], ["bar_order_by"], ["bar_excess_by"]],
                 heights={"title": 64, "card_headline": 150, "col_status": 240, "slicer_analyze": 44,
                          "bar_order_by": 260, "bar_excess_by": 260})


PHONE_W, PHONE_GAP = 320, 8


def phone_layout(page, rows, heights, default_h=84):
    """Mobile layout (Power BI's phone view): one stacked column; rows of 1-2 visuals; anything not listed is
    left off the phone canvas (navigation, header slicers, footer)."""
    y = 0
    for row in rows:
        w = (PHONE_W - PHONE_GAP * (len(row) - 1)) // len(row)
        h = heights.get(row[0], default_h)
        for i, name in enumerate(row):
            z = len(page.mobile) * 100
            page.mobile[name] = {"x": i * (w + PHONE_GAP), "y": y, "z": z, "width": w, "height": h, "tabOrder": z}
        y += h + PHONE_GAP


def check_mobile(path):
    """pbir validate does not read mobile.json, so check it against the visualContainerMobileState 2.4.0 rules
    here: only $schema / position / objects / visualContainerObjects; position needs x, y, width, height;
    numbers only; and the visual must sit inside the phone column."""
    m = json.loads(path.read_text())
    assert m["$schema"].endswith("/visualContainerMobileState/2.4.0/schema.json"), path
    assert set(m) <= {"$schema", "position", "objects", "visualContainerObjects"}, path
    pos = m["position"]
    assert {"x", "y", "width", "height"} <= set(pos) <= {"x", "y", "z", "width", "height", "tabOrder", "angle"}, path
    assert all(isinstance(v, (int, float)) for v in pos.values()), path
    assert 0 <= pos["x"] and pos["x"] + pos["width"] <= PHONE_W and pos["y"] >= 0, path


def restock(p):
    kpis(p, [("Lines To Order", NONE, 0), ("Units To Order", NONE, 0), ("Order Value", M, 2),
             ("Order Shelf Value 2017", M, 2), ("Safety Stock Value", M, 2), ("Urgent Order Value", M, 2)])
    a, b = cols(880, 424)
    # Page filter (filters pane): dead and inactive lines never get an order, so the page leaves them out.
    p.filters.append(visual_filter("f_restock_status", "Status.Status", ["Dead stock", "Inactive"], exclude=True))
    bar(p, "bar_order_vendor", "clusteredBarChart", a, ROW2_Y, 880, ROW2_H, "Vendor.Vendor", "_Measures.Order Value",
        DUE_C, "Order value by vendor", "One purchase order per vendor; hover a bar for its safety stock, reorder point and units",
        sort=("_Measures.Order Value", "Descending"),
        container={"visualTooltip": props(show=True, type="Canvas", section="line_tip")})
    slicer(p, "slicer_service", b, ROW2_Y, 204, 80, "Service Level.Service Level", default=0.95,
           title="What-if: service level")
    slicer(p, "slicer_review", b + 220, ROW2_Y, 204, 80, "Review Period.Review Days", default=7,
           title="What-if: review every (days)")
    textbox(p, "whatif_note", b + 8, ROW2_Y + 96, 408, ROW2_H - 96 - 52,
            ("How the order is sized", 10, INK, True),
            ("Safety stock = z × √(lead days × daily spread² + daily demand² × lead-time spread²). "
             "Reorder point = demand over the lead time + safety stock. A line at or below it is topped up to "
             "demand over lead time + review period + safety stock. Higher service level = fewer stock-outs, "
             "more cash in stock.", 9, INK2, False))
    p.group("whatif_group", "What-if", ["slicer_service", "slicer_review", "whatif_note"])
    navigator(p, "nav_restock_list", b, ROW2_Y + ROW2_H - 44, 424, 44, "grp_restock", "Restock list",
              [("restock_order", "Order now", ["table_order"], ["table_excess"]),
               ("restock_excess", "Excess", ["table_excess"], ["table_order"])])
    table(p, "table_order", X0, ROW3_Y, CW, ROW3_H,
          ["Store.Store Name", "Item.Item", "Item.Size", "Vendor.Vendor", "Status.Status", "Stock.End Units",
           "Stock.Daily Forecast", "Stock.Lead Days", "_Measures.Units To Order", "_Measures.Order Value",
           "_Measures.% of Visible Order"],
          "Restock list", "Every line due at the selected settings, largest first; % = share of the order shown",
          sort=("_Measures.Order Value", "Descending"),
          filters=[measure_filter("f_due", "_Measures.Units To Order", 1, 0)],
          objects=status_cf([cf_gradient("_Measures.Order Value", SURFACE, "#F3B48F")]),
          container=drill_header())  # header on: Export data / Analyze in Excel from the "..." menu
    table(p, "table_excess", X0, ROW3_Y, CW, ROW3_H,
          ["Store.Store Name", "Item.Item", "Item.Size", "Vendor.Vendor", "Status.Status", "Stock.End Units",
           "Stock.Daily Forecast", "Stock.Cover Days", "_Measures.Excess Stock Value"],
          "Excess list", "Lines over 180 days of cover at this page's filter; dead stock is on the Stock Health page",
          sort=("_Measures.Excess Stock Value", "Descending"),
          filters=[measure_filter("f_excess", "_Measures.Excess Stock Value", 1, 0)],
          objects=status_cf(), hidden_=True)
    phone_layout(p, [["title"], ["kpi_0", "kpi_2"], ["kpi_1", "kpi_5"], ["bar_order_vendor"],
                     ["slicer_service", "slicer_review"], ["table_order"]],
                 heights={"title": 64, "bar_order_vendor": 260, "slicer_service": 80, "table_order": 360})


def health(p):
    kpis(p, [("Stock Value", M, 2), ("Days of Cover", NONE, 0), ("Overstock Value", M, 2),
             ("Dead Stock Value", M, 2), ("Excess Stock Value", M, 2), ("Out of Stock Lines", NONE, 0),
             ("Units On Hand (Closing)", NONE, 0)])
    a, b, c = cols(440, 424, 424)
    bar(p, "col_cover", "clusteredColumnChart", a, ROW2_Y, 440, ROW2_H, "Cover Band.Cover Band", "_Measures.Stock Value",
        ACCENT, "Stock value by days of cover", "Units on hand ÷ forecast daily demand", units=M)
    navigator(p, "nav_health_view", b, ROW2_Y, 424, 40, "grp_health", "Health view",
              [("health_pack", "By pack size", ["matrix_product"], ["table_abc"]),
               ("health_abc", "By ABC class", ["table_abc"], ["matrix_product"])])
    y2, h2 = ROW2_Y + 48, ROW2_H - 48
    # Product hierarchy (Category → Pack Size → Item); Pack Size is a DAX calculated column on Item.
    p.add("matrix_product", "pivotTable", b, y2, 424, h2,
          roles={"Rows": [level("Item", "Product", lv) for lv in ("Category", "Pack Size", "Item")],
                 "Values": ["_Measures.Stock Value", "_Measures.Excess Stock Value"]},
          title="Stock by category and pack size",
          subtitle="Product hierarchy: Category → Pack Size → Item (Pack Size is a calculated column)",
          objects={"grid": props(rowPadding=5), "values": props(fontSize=9.0),
                   "rowHeaders": props(fontSize=9.0, stepped=True)},
          # Open on the Pack Size level for both categories; Item is one more drill down.
          expansionStates=[{"roles": ["Rows"],
                            "levels": [{"queryRefs": ["Item.Product.Category"], "isPinned": True},
                                       {"queryRefs": ["Item.Product.Pack Size"], "isPinned": True}],
                            "root": {"children": [{"identityValues": [lit(c)["expr"]], "isToggled": True}
                                                  for c in ("Spirits", "Wine")]}}])
    table(p, "table_abc", b, y2, 424, h2,
          ["ABC.ABC Class", "_Measures.Lines", "_Measures.Stock Value", "_Measures.Out of Stock Rate",
           "_Measures.Dead Stock Value", "_Measures.Lines Over Target Cover"],
          "By ABC class", "Over target = cover above the (synthetic) category × class target", hidden_=True)
    # Waterfall: opening stock + received − sold = closing stock, at 2016 unit cost; Power BI adds the total bar.
    p.add("waterfall_bridge", "waterfallChart", c, ROW2_Y, 424, ROW2_H,
          roles={"Category": "Bridge Step.Step", "Y": "_Measures.Bridge Value"},
          title="Stock bridge, 1 Jan → 31 Dec 2016", subtitle="Opening + received − sold = closing, at unit cost",
          sort=("Bridge Step.Step", "Ascending"),
          objects={**axes(), "labels": bar_labels(M),
                   "sentimentColors": props(increaseFill=color(ACCENT), decreaseFill=color(DUE_C), totalFill=color(INK2))})
    table(p, "table_slow", X0, ROW3_Y, CW, ROW3_H,
          ["Store.Store Name", "Item.Item", "Item.Size", "Vendor.Vendor", "Status.Status", "Stock.End Units",
           "Stock.Sold Units", "Stock.Cover Days", "Stock.Stock Cost"],
          "Overstock and dead stock, largest first", "Candidates for transfers to other stores, markdowns or returns",
          sort=("Stock.Stock Cost", "Descending"), filters=[visual_filter("f_slow", "Status.Status", SLOW)],
          objects=status_cf([data_bars("Stock.Cover Days", "#CFC3EE")]))


def demand(p):
    kpis(p, [("January Units Sold", NONE, 0), ("January Sales", M, 2), ("Avg Selling Price", NONE, 2),
             ("Forecast Units per Day", NONE, 0)])
    a, b = cols(880, 424)
    series = ["_Measures.Actual Chain Units", "_Measures.Forecast Restock Method", "_Measures.Forecast Avg Weekday",
              "_Measures.Forecast Last Week"]
    p.add("line_backtest", "lineChart", a, ROW2_Y, 880, ROW2_H,
          roles={"Category": "Forecast Daily.Forecast Date", "Y": series},
          title="Backtest: chain units per day, January 2016",
          subtitle="Methods trained on 1–24 Jan, scored on 25–31 Jan",
          objects={**axes(), "legend": legend(), "lineStyles": props(strokeWidth=2, showMarker=True, markerSize=3),
                   "dataPoint": [measure_color(s, c) for s, c in zip(series, [INK, ACCENT, DUE_C, MUTED])]})
    bar(p, "col_weekday", "clusteredColumnChart", b, ROW2_Y, 424, ROW2_H, "Weekday Profile.Day", "_Measures.Day Index",
        ACCENT, "Weekday index", "Friday sells 2.5× a Monday; tooltip = January units that weekday", units=NONE,
        tooltips=["_Measures.January Units by Weekday"])
    a, b = cols(560, 744)
    table(p, "table_scores", a, ROW3_Y, 560, ROW3_H,
          ["Forecast Scores.Method", "_Measures.Line Week Error", "_Measures.Chain Day Error", "_Measures.Forecast Bias"],
          "Forecast error by method", "The annual method is the only one not biased low (−1%), so the restock list uses it",
          sort=("Forecast Scores.Method", "Ascending"))
    # Small multiples: one panel per method, built on the Power Query unpivot of the score table.
    p.add("col_metric", "clusteredBarChart", b, ROW3_Y, 744, ROW3_H,
          roles={"Category": "Forecast Metric.Metric", "Y": "_Measures.Metric Score",
                 "Rows": "Forecast Metric.Forecast Method"},
          title="The same scores, one panel per method",
          subtitle="Small multiples on the unpivoted score table; bias is forecast ÷ actual − 1",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(NONE),
                   "dataPoint": fill(ACCENT),
                   "smallMultiplesLayout": props(layoutType="custom", rowCount=2, columnCount=2)})


def transform(name, algorithm, x_ref, y_ref, params, outputs):
    """Analytics-pane QueryTransform (semanticQuery schema): input table = the chart's axis + value,
    output table = the columns Power BI adds (forecast line and band, or anomaly flags)."""
    def col(role, ref):
        return {"Role": role, "Expression": {**field(ref), "Name": ref}}
    return {"Name": name, "Algorithm": algorithm,
            "Input": {"Parameters": [{**lit(v)["expr"], "Name": k} for k, v in params.items()],
                      "Table": {"Name": f"{name}_in", "Columns": [col("X", x_ref), col("Y", y_ref)]}},
            "Output": {"Table": {"Name": f"{name}_out", "Columns": [
                {"Role": r, "Expression": {"TransformOutputRoleRef": {"Role": r}, "Name": f"{name}.{r}"}}
                for r in outputs]}}}


def trends(p):
    # Row 1: the new card visual (four tiles in one visual) and a button slicer synced with the Category dropdowns.
    a, b = cols(1000, 304)
    tiles = ["_Measures.January Sales", "_Measures.January Units Sold", "_Measures.Avg Selling Price",
             "_Measures.Received Value"]
    p.add("cards_trend", "cardVisual", a, ROW_KPI_Y, 1000, ROW_KPI_H, roles={"Data": tiles},
          objects={"layout": props(orientation=2, columnCount=4, maxTiles=4),
                   "value": props(fontSize=22.0, fontColor=color(INK), fontFamily="Segoe UI Semibold",
                                  labelDisplayUnits=AS_IS),
                   "label": props(show=True, position="belowValue", fontColor=color(MUTED), fontSize=9.0),
                   "accentBar": props(show=True, color=color(ACCENT), position="Left", width=4.0),
                   "divider": props(show=True, dividerColor=color(HAIR)),
                   "fillCustom": props(show=False)},
          container={"background": props(show=True, color=color(SURFACE))})
    p.add("buttons_category", "advancedSlicerVisual", b, ROW_KPI_Y, 304, ROW_KPI_H, roles={"Values": "Item.Category"},
          title="Category",
          objects={"layout": props(orientation=0, columnCount=2, maxTiles=4),
                   "value": props(fontColor=color(INK), fontSize=10.0),
                   "fillCustom": props(show=True, fillColor=color(PLANE), transparency=0.0),
                   "outline": props(show=True, lineColor=color(HAIR), weight=1.0),
                   "shapeCustomRectangle": props(tileShape="rectangleRounded", rectangleRoundedCurve=8),
                   "selection": props(singleSelect=False, selectAllCheckboxEnabled=False),
                   "accentBar": props(show=True, color=color(ACCENT), position="Left", width=3.0)},
          syncGroup={"groupName": "Category", "fieldChanges": True, "filterChanges": True})

    # Row 2: analytics pane on daily January sales.
    a, b = cols(652, 652)
    units = "_Measures.January Units Sold"
    p.add("line_forecast", "lineChart", a, ROW2_Y, 652, ROW2_H, roles={"Category": "Date.Date", "Y": units},
          title="Units per day, with a 7-day forecast",
          subtitle="Analytics pane forecast: weekly seasonality, 95% confidence band",
          objects={**axes(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color(units, ACCENT)],
                   "forecast": [{"properties": {
                       "show": lit(True), "displayName": lit("Forecast"), "lineColor": color(ACCENT),
                       "style": lit("dashed"), "width": lit(2.0),
                       "bandAreaShow": lit(True), "bandAreaColor": color(ACCENT), "bandAreaTransparency": lit(80.0),
                       "bandLineShow": lit(False),
                       "transform": transform("forecast", "Forecast", "Date.Date", units,
                                              {"ForecastLength": 7, "ConfidenceLevel": 0.95, "Seasonality": 7,
                                               "IgnoreLast": 0, "Unit": "Point"},
                                              ["forecast.ForecastValue", "forecast.ConfidenceHighBound",
                                               "forecast.ConfidenceLowBound"])},
                       "selector": {"metadata": units}}]})
    sales = "_Measures.January Sales"
    p.add("line_anomaly", "lineChart", b, ROW2_Y, 652, ROW2_H, roles={"Category": "Date.Date", "Y": sales},
          title="Sales per day, with anomalies flagged",
          subtitle="Analytics pane anomaly detection: sensitivity 70%",
          objects={**axes(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color(sales, INK2)],
                   "anomalyDetection": [{"properties": {
                       "show": lit(True), "displayName": lit("Anomaly"), "markerShow": lit(True),
                       "markerColor": color(DUE_C), "markerShape": lit("circle"), "markerShapeSize": lit(6.0),
                       "confidenceBandShow": lit(True), "confidenceBandColor": color(MUTED),
                       "transparency": lit(80.0),
                       "transform": transform("anomaly", "AnomalyDetection", "Date.Date", sales,
                                              {"Sensitivity": 0.7},
                                              ["anomaly.IsAnomaly", "anomaly.ExpectedValue",
                                               "anomaly.LowerBound", "anomaly.UpperBound"])},
                       "selector": {"metadata": sales}}]})

    # Row 3: the Time Intelligence calculation group as a matrix, and a running total (visual calculation).
    a, b = cols(760, 544)
    p.add("matrix_periods", "pivotTable", a, ROW3_Y, 760, ROW3_H,
          roles={"Rows": "Date.Month", "Columns": "Time Intelligence.Period", "Values": "_Measures.Received Value"},
          title="Received value across calendar periods",
          subtitle="Columns are the Time Intelligence calculation group; Prior Year and YoY % are blank (one year of data)",
          objects={"grid": props(rowPadding=5), "subTotals": props(rowSubtotals=False, columnSubtotals=False),
                   "values": props(fontSize=9.0)})
    p.add("line_cumulative", "lineChart", b, ROW3_Y, 544, ROW3_H,
          roles={"Category": "Date.Date",
                 "Y": [hidden(sales), vcalc("Cumulative sales", "RUNNINGSUM ( [January Sales] )")]},
          title="Cumulative January sales", subtitle="RUNNINGSUM visual calculation over the chart's own rows",
          objects={**axes(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "labels": props(show=False)})


# Store Staff cannot see Invoices or the invoice columns on Vendor (OLS); the scorecard only uses fields they can.
OLS_SAFE_VENDOR_COLUMNS = ["Vendor.Vendor", "_Measures.Listed Items", "Vendor.Avg Lead Days", "Vendor.Lead Days P90",
                           "_Measures.Received Value", "_Measures.Items Supplied", "_Measures.Stock of Supplied Items",
                           "_Measures.Order Value"]


def calendar_levels():
    levels = [level("Date", "Calendar", lv) for lv in ("Year", "Quarter", "Month Start")]
    levels[2]["active"] = True
    return levels


def vendors(p):
    kpis(p, [("Purchase Spend", M, 1), ("Purchase Orders", NONE, 0), ("Lead Time", NONE, 1),
             ("Freight Share", NONE, 2), ("Payment Days", NONE, 1), ("Unmatched POs", NONE, 0)])
    a, b = cols(640, 664)
    bar(p, "bar_spend_vendor", "clusteredBarChart", a, ROW2_Y, 640, ROW2_H, "Vendor.Vendor", "_Measures.Purchase Spend",
        ACCENT, "Purchase spend by vendor", "2016 invoices; hover a vendor for lead time, freight and payment days", units=M,
        sort=("_Measures.Purchase Spend", "Descending"),
        container={"visualTooltip": props(show=True, type="Canvas", section="vendor_tip")})
    # Edit interactions: a vendor click highlights its months, filters the scorecard, leaves the KPI cards alone.
    p.interactions += [("bar_spend_vendor", "col_received", "HighlightFilter"),
                       ("bar_spend_vendor", "table_vendors", "DataFilter")] + \
        [("bar_spend_vendor", f"kpi_{i}", "NoFilter") for i in range(6)]
    # Cash out vs goods in. Paid Dollars runs on the inactive Pay Date relationship (USERELATIONSHIP).
    # Two 3-month averages: a model measure with WINDOW, and a visual calculation (MOVINGAVERAGE).
    p.add("col_received", "lineClusteredColumnComboChart", b, ROW2_Y, 664, ROW2_H,
          roles={"Category": calendar_levels(), "Y": ["_Measures.Received Value", "_Measures.Paid Dollars"],
                 "Y2": ["_Measures.Received Value 3M (WINDOW)",
                        vcalc("Paid 3M avg (visual calc)", "MOVINGAVERAGE ( [Paid Dollars], 3 )")]},
          title="Received vs paid per month",
          subtitle="Paid = invoices by payment date; lines = 3-month averages; drill up to quarter / year",
          container=drill_header(),
          objects={**axes(), "legend": legend(),
                   "labels": props(show=False),
                   "lineStyles": props(strokeWidth=2, showMarker=True, markerSize=3),
                   "dataPoint": [measure_color("_Measures.Received Value", ACCENT),
                                 measure_color("_Measures.Paid Dollars", INK2)]})
    a, b = cols(860, 444)
    table(p, "table_vendors", a, ROW3_Y, 860, ROW3_H,
          OLS_SAFE_VENDOR_COLUMNS,
          "Vendor scorecard", "No invoice fields, so Store Staff (object-level security) sees it too; lead days = PO to receipt",
          sort=("_Measures.Received Value", "Descending"))
    # Many-to-many: Item and Vendor meet through the Item Supplier bridge, not through Stock.
    table(p, "table_multi_sourced", b, ROW3_Y, 444, ROW3_H,
          ["Item.Item", "Vendor.Vendor", "_Measures.Multi-Sourced Units", "_Measures.Multi-Sourced Spend"],
          "Items bought from more than one vendor", "2016 purchases, through the Item Supplier bridge table",
          sort=("_Measures.Multi-Sourced Spend", "Descending"))


def stores(p):
    kpis(p, [("January Sales", M, 2), ("Stock Value", M, 2), ("Out of Stock Rate", NONE, 1),
             ("Order Value", M, 2), ("Excess Stock Value", M, 2)])
    a, b, c = cols(440, 424, 424)
    # Measure field parameter: the chart shows whichever metric is picked in the slicer above it.
    p.slicers.append(("slicer_metric", "Metric.Metric", "January Sales"))
    p.add("slicer_metric", "slicer", a, ROW2_Y, 440, 48, roles={"Values": "Metric.Metric"},
          alt="Slicer: pick the measure the store chart shows (field parameter)",
          objects={"data": props(mode="Dropdown"), "header": props(show=False),
                   "selection": props(singleSelect=True, strictSingleSelect=True),
                   "items": props(fontColor=color(INK2), textSize=9.0),
                   "general": [{"properties": {"filter": {"filter": in_filter("Metric.Metric", ["January Sales"])}}}]},
          container={"background": props(show=False), "border": props(show=False)})
    y, h = ROW2_Y + 52, ROW2_H - 52
    bar(p, "bar_store_sales", "clusteredBarChart", a, y, 440, h, "Store.Store Name", "_Measures.January Sales",
        ACCENT, "Stores by the metric picked above", "Field parameter over five measures", units=AS_IS,
        sort=("_Measures.January Sales", "Descending"))
    # The Y well holds the parameter; Desktop stores the field it currently projects plus the parameter itself.
    p.visuals[-1]["visual"]["query"]["queryState"]["Y"]["fieldParameters"] = [
        {"parameterExpr": field("Metric.Metric"), "index": 0, "length": 1}]
    # Store Size is a list group (stores in thirds by stock value), used as a slicer.
    slicer(p, "slicer_size", b, ROW2_Y, 424, 44, "Store.Store Size", mode="HorizontalList", single=False, header=False,
           container={"background": props(show=False), "border": props(show=False)})
    bar(p, "bar_store_oos", "clusteredBarChart", b, y, 424, h, "Store.Store Name",
        "_Measures.Out of Stock Rate", DUE_C, "Out-of-stock rate by store",
        "Out-of-stock lines ÷ lines with demand; store size is a group on stock value",
        units=NONE, sort=("_Measures.Out of Stock Rate", "Descending"))
    p.add("scatter_cover", "scatterChart", c, ROW2_Y, 424, ROW2_H,
          roles={"Category": "Store.Store Name", "X": "_Measures.Days of Cover", "Y": "_Measures.Order Value"},
          title="Days of cover vs order due, per store", subtitle="Top left = thin stock and a large order due",
          objects={**axes(), "dataPoint": fill(ACCENT), "categoryLabels": props(show=False)})
    p.interactions += [("bar_store_sales", "bar_store_oos", "DataFilter"), ("bar_store_sales", "scatter_cover", "DataFilter")] + \
        [("bar_store_sales", f"kpi_{i}", "NoFilter") for i in range(5)]
    table(p, "table_stores", X0, ROW3_Y, CW, ROW3_H,
          [vcalc("Sales Rank", "RANK ( DENSE, ORDERBY ( [January Sales], DESC ) )"),
           "Store.Store Name", "Store.City", "_Measures.January Sales", "_Measures.Stock Value",
           "_Measures.Share of Stock Value", "_Measures.Days of Cover",
           "_Measures.Out of Stock Rate", "_Measures.Lines To Order", "_Measures.Order Value",
           "_Measures.Excess Stock Value"],
          "Store scorecard", "Sales Rank is a visual calculation; arrows: ▲ under 15% out of stock, ► 15–20%, ▼ over 20%",
          sort=("_Measures.January Sales", "Descending"),
          objects={"values": [cf_icons("_Measures.Out of Stock Rate", "_Measures.Out of Stock Rate",
                                       [(3, 0.15, "ColoredArrowUp"), (4, 0.2, "ColoredArrowRight"),
                                        (1, 0.2, "ColoredArrowDown")])]})


def assortment(p):
    kpis(p, [("Sales 2016", M, 2), ("Lines Going Dormant", NONE, 0), ("Dormant Stock Value", M, 2),
             ("New Line OOS Rate", NONE, 1), ("Carried Line OOS Rate", NONE, 1)])
    a, b = cols(880, 424)
    # Pareto: bars = sales per item, line = cumulative share (REMOVEFILTERS on Item[Item] ignores the Top N filter).
    p.add("pareto_items", "lineClusteredColumnComboChart", a, ROW2_Y, 880, ROW2_H,
          roles={"Category": "Item.Item", "Y": "_Measures.Sales 2016",
                 "Y2": ["_Measures.Cumulative Sales Share", "_Measures.Pareto 80% Line"]},
          title="Pareto: 2016 sales by item", subtitle="Top 40 items drawn; the line is the running share of all items in the selection",
          sort=("_Measures.Sales 2016", "Descending"),
          filters=[topn_filter("f_top40", "Item.Item", "_Measures.Sales 2016", 40)],
          objects={**axes(), "legend": legend(), "labels": props(show=False),
                   "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color("_Measures.Sales 2016", ACCENT),
                                 measure_color("_Measures.Cumulative Sales Share", INK),
                                 measure_color("_Measures.Pareto 80% Line", DUE_C)]})
    table(p, "table_abc_dynamic", b, ROW2_Y, 424, ROW2_H,
          ["Item.Brand No", "Item.Item", "_Measures.Sales 2016", "_Measures.ABC Static", "_Measures.ABC Dynamic"],
          "ABC: static vs re-ranked in the selection", "Pick a city or vendor: the dynamic class re-ranks inside it",
          sort=("_Measures.Sales 2016", "Descending"))
    a, b = cols(440, 864)
    table(p, "table_origin", a, ROW3_Y, 440, ROW3_H,
          ["Stock.Line Origin", "_Measures.Lines", "_Measures.Out of Stock Rate", "_Measures.Dead Stock Value",
           "_Measures.Stock Value"],
          "New lines vs carried over", "New in 2016 = closing count only; received, not counted = bought in 2016 "
          "but in neither count, so empty at year-end")
    table(p, "table_dormant", b, ROW3_Y, 864, ROW3_H,
          ["Store.Store Name", "Item.Item", "Item.Size", "Stock.End Units", "Stock.Last Receipt", "Stock.Stock Cost"],
          "Lines going dormant, largest first",
          "Stock on hand, no January sales, no receipt after 2 Sep 2016 (blank = none in 2016)",
          sort=("Stock.Stock Cost", "Descending"),
          filters=[measure_filter("f_dormant", "_Measures.Lines Going Dormant", 1, 0)])


def movement(p):
    kpis(p, [("Stock Value Change", M, 2), ("Cover P95 by Category", NONE, 0), ("Outlier Lines", NONE, 0),
             ("Outlier Stock Value", M, 2), ("Lines", NONE, 0)])
    a, b, c = cols(424, 440, 424)
    p.add("waterfall_category", "waterfallChart", a, ROW2_Y, 424, ROW2_H,
          roles={"Category": "Item.Category", "Y": "_Measures.Stock Value Change",
                 "Tooltips": "_Measures.Stock Value Change %"},
          title="Stock value change by category", subtitle="Closing − opening at unit cost; total bar = whole change",
          objects={**axes(), "labels": bar_labels(M),
                   "sentimentColors": props(increaseFill=color(SLOW_C), decreaseFill=color(ACCENT), totalFill=color(INK2))})
    p.add("matrix_transition", "pivotTable", b, ROW2_Y, 440, ROW2_H,
          roles={"Rows": "Status.Status", "Columns": "Stock.Opening State", "Values": "_Measures.Lines"},
          title="From 1 Jan to 31 Dec", subtitle="Lines by year-end status (rows) and opening state (columns)",
          objects={"grid": props(rowPadding=6), "values": props(fontSize=9.0), "columnHeaders": props(fontSize=9.0)})
    p.add("funnel_po", "funnel", c, ROW2_Y, 424, ROW2_H,
          roles={"Category": "Funnel Step.Funnel Step", "Y": "_Measures.Funnel POs"},
          title="Procurement funnel (purchase orders)", subtitle="Each step keeps the conditions of the steps before",
          sort=("Funnel Step.Funnel Step", "Ascending"),
          objects={"labels": props(show=True, color=color(INK), fontSize=9.0), "dataPoint": fill(ACCENT)})
    a, b = cols(500, 804)
    table(p, "table_band_dynamic", a, ROW3_Y, 500, ROW3_H,
          ["Cover Band.Cover Band", "Cover Band.Min Days", "Cover Band.Max Days", "_Measures.Lines in Band (Dynamic)",
           "_Measures.Lines"],
          "Cover bands from a table", "Dynamic = counted from the Min / Max Days edges; Lines = the stored band")
    table(p, "table_outliers", b, ROW3_Y, 804, ROW3_H,
          ["Store.Store Name", "Item.Item", "Item.Category", "Stock.Cover Days", "_Measures.Cover P95 by Category",
           "Stock.Stock Cost"],
          "Cover outliers, largest stock value first", "Lines with cover above their category's 95th percentile",
          sort=("Stock.Stock Cost", "Descending"),
          filters=[measure_filter("f_outlier", "_Measures.Outlier Lines", 1, 0)])


def diagnose(p):
    kpis(p, [("Out of Stock Rate", NONE, 1), ("Out of Stock Lines", NONE, 0), ("Selling Lines", NONE, 0),
             ("Order Value", M, 2), ("Urgent Order Value", M, 2)])
    a, b = cols(652, 652)
    # Key influencers: target = status (Out of stock sorts first), analysed per stock line (Inventory ID).
    p.add("key_influencers", "keyDriversVisual", a, ROW2_Y, 652, ROW2_H,
          roles={"Target": "Status.Status",
                 "ExplainBy": ["ABC.ABC Class", "Item.Category", "Item.Pack Size", "Store.City"],
                 "Details": "Stock.Inventory ID"},
          title="What makes a line run out of stock", subtitle="Key influencers: ABC class, category, pack size, city")
    p.add("decomp_order", "decompositionTreeVisual", b, ROW2_Y, 652, ROW2_H,
          roles={"Analyze": "_Measures.Order Value", "ExplainBy": ["Vendor.Vendor", "Item.Category", "Store.Store Name"]},
          title="Where the order comes from", subtitle="Decomposition tree: order value → vendor → category → store")
    p.add("qna", "qnaVisual", a, ROW3_Y, 652, ROW3_H, title="Ask a question",
          subtitle="Q&A uses the model synonyms: supplier, branch, stockout rate, reorder quantity, days of supply")
    # Narrative: KPI → decomposition → key influencers → drillthrough, as a DAX measure that follows the slicers.
    p.add("card_diagnosis", "card", b, ROW3_Y, 652, ROW3_H - 76, roles={"Values": "_Measures.Diagnosis"},
          title="Diagnosis", subtitle="Written by a DAX measure; follows the slicers",
          objects={"labels": props(color=color(INK), fontSize=13.0, fontFamily="Segoe UI"),
                   "categoryLabels": props(show=False), "wordWrap": props(show=True)})
    textbox(p, "diagnose_note", b + 16, ROW3_Y + ROW3_H - 64, 620, 60,
            ("Then split the order in the tree, check the traits on the left, and right-click an item or vendor "
             "> Drill through for its detail page.", 10, INK2, False))


def back_button(p):
    p.add("btn_back", "actionButton", X0, ROW_KPI_Y, 120, ROW_KPI_H,
          objects={"text": props(show=True, text="← Back", fontColor=color(INK), fontSize=11.0,
                                 fontFamily="Segoe UI Semibold", horizontalAlignment="center"),
                   "fill": props(show=True, fillColor=color(SURFACE), transparency=0.0),
                   "outline": props(show=True, lineColor=color(HAIR), weight=1.0),
                   "icon": props(show=False), "shape": props(tileShape="rectangleRounded", rectangleRoundedCurve=12)},
          container={"visualLink": props(show=True, type="Back", tooltip="Back to the page you came from"),
                     "background": props(show=False), "border": props(show=False)})


def drillthrough(p, ref):
    table, col = ref.split(".", 1)
    f = {"name": "f_drill", "field": field(ref), "type": "Categorical", "howCreated": "Drillthrough"}
    p.filters.append(f)
    p.page_extra.update(type="Drillthrough", visibility="HiddenInViewMode", pageBinding={
        "name": f"bind_{p.id}", "type": "Drillthrough", "parameters": [{
            "name": f"param_{p.id}", "boundFilter": "f_drill", "asAggregation": False,
            "fieldExpr": field(ref)}]})


DENEB = "deneb7E15AEF80B9E4D4F8E12924291ECE89A"


def item_detail(p):
    drillthrough(p, "Item.Item")
    back_button(p)
    kpis(p, [("Units On Hand", NONE, 0), ("Stock Value", K, 1), ("January Units Sold", NONE, 0),
             ("Lines To Order", NONE, 0), ("Order Value", K, 1)], x0=X0 + 136, width=CW - 136)
    a, b = cols(880, 424)
    spec = (PBI / "deneb" / "rop_bullet.json").read_text()
    p.add("deneb_bullet", DENEB, a, ROW2_Y, 880, ROW2_H,
          roles={"dataset": ["Store.Store Name", "_Measures.Units On Hand", "_Measures.Reorder Point Units",
                             "_Measures.Order Up To Units"]},
          title="On hand vs reorder point vs order-up-to, by store",
          subtitle="Deneb bullet (Vega-Lite): green = on hand, grey = order-up-to, orange tick = reorder point; top 15 stores",
          objects={"vega": props(jsonSpec=spec, provider="vegaLite", renderMode="svg")})
    table(p, "table_item_vendor", b, ROW2_Y, 424, ROW2_H,
          ["Vendor.Vendor", "Vendor.Avg Lead Days", "Vendor.Lead Days P90", "_Measures.Lines To Order",
           "_Measures.Order Value"],
          "Vendor", "Who supplies the item and how long it takes")
    table(p, "table_item_store", X0, ROW3_Y, CW, ROW3_H,
          ["Store.Store Name", "Store.City", "Status.Status", "Stock.End Units", "Stock.Daily Forecast",
           "_Measures.Reorder Point Units", "_Measures.Order Up To Units", "_Measures.Units To Order",
           "_Measures.Order Value"],
          "Every store stocking the item", "Reorder point and order-up-to follow the what-if service level on Restock",
          sort=("_Measures.Order Up To Units", "Descending"), objects=status_cf())


def vendor_detail(p):
    drillthrough(p, "Vendor.Vendor")
    back_button(p)
    kpis(p, [("Lead Time", NONE, 1), ("Vendor Shipments", NONE, 0), ("Purchase Spend", M, 2),
             ("Freight Share", NONE, 2), ("Payment Days", NONE, 1), ("Unmatched POs", NONE, 0)],
         x0=X0 + 136, width=CW - 136)
    a, b = cols(652, 652)
    bar(p, "col_lead_days", "clusteredColumnChart", a, ROW2_Y, 652, ROW2_H, "Shipments.Lead Day",
        "_Measures.Vendor Shipments", ACCENT, "Lead-time spread", "Shipments by days from PO to receipt", units=NONE)
    table(p, "table_vendor_multi", b, ROW2_Y, 652, ROW2_H,
          ["Item.Item", "_Measures.Vendors per Item", "_Measures.Multi-Sourced Units", "_Measures.Multi-Sourced Spend"],
          "Items also bought from other vendors", "2016 purchases through the Item Supplier bridge",
          sort=("_Measures.Multi-Sourced Spend", "Descending"))
    table(p, "table_unmatched", a, ROW3_Y, 652, ROW3_H,
          ["Invoices.PO Number", "Invoices.PO Date", "Invoices.Invoice Date", "Invoices.Invoice Dollars",
           "Invoices.Invoice Freight"],
          "Invoices without a matching receipt", "PO number never seen on this vendor's receiving lines",
          sort=("Invoices.Invoice Dollars", "Descending"),
          filters=[visual_filter("f_unmatched", "Invoices.PO Matches Receipts", [False])])
    table(p, "table_vendor_order", b, ROW3_Y, 652, ROW3_H,
          ["Item.Item", "Store.Store Name", "Status.Status", "_Measures.Units To Order", "_Measures.Order Value"],
          "Items to order from this vendor", "Lines due at the Restock page's what-if settings",
          sort=("_Measures.Order Value", "Descending"),
          filters=[measure_filter("f_due", "_Measures.Units To Order", 1, 0)], objects=status_cf())


def tip_card(p, name, values):
    p.page_extra.update(type="Tooltip", visibility="HiddenInViewMode", displayOption="ActualSize",
                        width=TIP_W, height=TIP_H)
    p.add(name, "multiRowCard", 0, 0, TIP_W, TIP_H, roles={"Values": values},
          objects={"dataLabels": props(color=color(INK), fontSize=14.0),
                   "categoryLabels": props(color=color(MUTED), fontSize=9.0),
                   "card": props(barShow=True, barColor=color(ACCENT), barWeight=3.0)},
          container={"border": props(show=False)})


def vendor_tip(p):
    tip_card(p, "card_vendor_tip", ["Vendor.Vendor", "Vendor.Lead Days P90", "Vendor.Freight Pct",
                                    "Vendor.Avg Days To Pay", "_Measures.Vendor Shipments"])


def line_tip(p):
    tip_card(p, "card_line_tip", ["Vendor.Vendor", "_Measures.Safety Stock Units", "_Measures.Reorder Point Units",
                                  "_Measures.Units To Order", "_Measures.Lines To Order"])


BUILDERS = {"overview": overview, "restock": restock, "health": health, "demand": demand, "trends": trends,
            "vendors": vendors, "stores": stores, "assortment": assortment,
            "movement": movement, "diagnose": diagnose, "item_detail": item_detail,
            "vendor_detail": vendor_detail, "vendor_tip": vendor_tip, "line_tip": line_tip}


# ------------------------------------------------------------ theme & files
THEME = {
    "name": "Stock Restock",
    "dataColors": [ACCENT, DUE_C, SLOW_C, "#0b0b0b", "#e0a030", "#2a78d6", "#86b6ef", "#B9B8B0", "#e87ba4", "#104281"],
    "foreground": INK, "foregroundNeutralSecondary": INK2, "foregroundNeutralTertiary": MUTED,
    "background": SURFACE, "backgroundLight": PLANE, "backgroundNeutral": HAIR, "tableAccent": ACCENT,
    "good": ACCENT, "neutral": "#e0a030", "bad": DUE_C, "null": "#F0EFEC",
    "textClasses": {
        "callout": {"fontSize": 22, "fontFace": "Segoe UI Semibold", "color": INK},
        "title": {"fontSize": 12, "fontFace": "Segoe UI Semibold", "color": INK},
        "header": {"fontSize": 11, "fontFace": "Segoe UI Semibold", "color": INK},
        "label": {"fontSize": 9, "fontFace": "Segoe UI", "color": INK2},
    },
    "visualStyles": {
        "*": {"*": {
            "background": [{"show": True, "color": {"solid": {"color": SURFACE}}, "transparency": 0}],
            "border": [{"show": True, "color": {"solid": {"color": HAIR}}, "radius": 12, "width": 1}],
            "dropShadow": [{"show": False}],
            "padding": [{"top": 12, "bottom": 10, "left": 14, "right": 14}],
            "title": [{"show": True, "fontColor": {"solid": {"color": INK}}, "fontSize": 12,
                       "fontFamily": "Segoe UI Semibold", "titleWrap": True}],
            "subTitle": [{"show": True, "fontColor": {"solid": {"color": MUTED}}, "fontSize": 9, "fontFamily": "Segoe UI"}],
            "spacing": [{"customizeSpacing": True, "spaceBelowTitle": 2, "spaceBelowSubTitle": 10}],
            "visualHeader": [{"show": True, "foreground": {"solid": {"color": MUTED}}, "transparency": 100}],
            "visualTooltip": [{"titleFontColor": {"solid": {"color": MUTED}}, "valueFontColor": {"solid": {"color": INK}},
                               "background": {"solid": {"color": "#FFFFFF"}}, "fontSize": 9}],
        }},
        "page": {"*": {"background": [{"color": {"solid": {"color": PLANE}}, "transparency": 0}],
                       "outspace": [{"color": {"solid": {"color": "#E9E8E3"}}, "transparency": 0}]}},
        "card": {"*": {"labels": [{"color": {"solid": {"color": INK}}, "fontSize": 22}],
                       "categoryLabels": [{"show": True, "color": {"solid": {"color": MUTED}}, "fontSize": 9}]}},
        "slicer": {"*": {"header": [{"show": True, "fontColor": {"solid": {"color": MUTED}}, "textSize": 9}],
                         "items": [{"fontColor": {"solid": {"color": INK}}, "textSize": 10}],
                         "border": [{"show": True, "color": {"solid": {"color": HAIR}}, "radius": 8}]}},
        "pivotTable": {"*": {
            "columnHeaders": [{"fontColor": {"solid": {"color": MUTED}}, "backColor": {"solid": {"color": SURFACE}},
                               "fontFamily": "Segoe UI Semibold", "fontSize": 9, "outline": "BottomOnly"}],
            "rowHeaders": [{"fontColor": {"solid": {"color": INK}}, "backColor": {"solid": {"color": SURFACE}},
                            "fontFamily": "Segoe UI", "fontSize": 10}],
            "values": [{"fontColorPrimary": {"solid": {"color": INK}}, "backColorPrimary": {"solid": {"color": SURFACE}},
                        "backColorSecondary": {"solid": {"color": SURFACE}}, "fontSize": 10}],
            "grid": [{"gridHorizontal": True, "gridHorizontalColor": {"solid": {"color": "#ECEBE6"}},
                      "gridVertical": False, "outlineColor": {"solid": {"color": HAIR}}}]}},
        "tableEx": {"*": {
            "columnHeaders": [{"fontColor": {"solid": {"color": MUTED}}, "backColor": {"solid": {"color": SURFACE}},
                               "fontFamily": "Segoe UI Semibold", "fontSize": 9, "outline": "BottomOnly"}],
            "values": [{"fontColorPrimary": {"solid": {"color": INK}}, "backColorPrimary": {"solid": {"color": SURFACE}},
                        "backColorSecondary": {"solid": {"color": SURFACE}}, "fontSize": 10}],
            "grid": [{"gridHorizontal": True, "gridHorizontalColor": {"solid": {"color": "#ECEBE6"}}, "gridVertical": False}]}},
    },
}


def background_png(path):
    """Canvas wallpaper: warm-white plane with a dark-green sidebar and a hairline accent."""
    img = Image.new("RGB", (W, H), PLANE)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, SIDEBAR - 1, H], fill=SIDE)
    d.rectangle([SIDEBAR - 3, 0, SIDEBAR - 1, H], fill=ACCENT)
    d.line([24, 112, SIDEBAR - 24, 112], fill=SIDE_LINE, width=1)
    d.line([24, 776, SIDEBAR - 24, 776], fill=SIDE_LINE, width=1)
    d.line([X0, 99, X1, 99], fill=HAIR, width=1)
    img.save(path, optimize=True)


def contrast(fg, bg):
    """WCAG 2.1 contrast ratio between two hex colours."""
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def contrast_check():
    """Every text colour in the theme and the report against the background it sits on: AA needs 4.5:1."""
    pairs = [("INK", INK, "SURFACE", SURFACE), ("INK2", INK2, "SURFACE", SURFACE), ("MUTED", MUTED, "SURFACE", SURFACE),
             ("MUTED", MUTED, "PLANE", PLANE), ("DUE_C (order now)", DUE_C, "SURFACE", SURFACE),
             ("AMBER (reorder)", AMBER, "SURFACE", SURFACE), ("SLOW_C (excess)", SLOW_C, "SURFACE", SURFACE),
             ("ACCENT", ACCENT, "SURFACE", SURFACE), ("SIDE_TEXT", SIDE_TEXT, "SIDE", SIDE),
             ("white (active nav)", "#FFFFFF", "SIDE", SIDE), ("INK on gradient top", INK, "#F3B48F", "#F3B48F"),
             ("INK on data bar", INK, "#CFC3EE", "#CFC3EE")]
    print("  contrast (WCAG AA ≥ 4.5:1):")
    for fn, f, bn, b in pairs:
        r = contrast(f, b)
        print(f"    {fn:<22} {f} on {bn:<8} {r:5.2f}:1")
        assert r >= 4.5, (fn, bn, r)


def ols_check(definition):
    """Which visuals would break for the Store Staff role (OLS hides Invoices and four Vendor columns)?
    Resolves measures transitively to the columns they read."""
    import re
    blocked_cols = {("Vendor", c) for c in ("Invoiced Spend", "Invoiced Freight", "Freight Pct", "Avg Days To Pay")}
    dax = {m[1]: m[4] for m in MEASURES}

    def touches(expr, seen=()):
        if re.search(r"(?<![\w'])'?Invoices'?\s*\[", expr):
            return True
        for t, c in re.findall(r"'?([\w ]+?)'?\[([^\]]+)\]", expr):
            if (t.strip(), c) in blocked_cols:
                return True
        return any(m in dax and m not in seen and touches(dax[m], (*seen, m))
                   for m in re.findall(r"(?<![\w'\]])\[([^\]]+)\]", expr))

    def bad(ref):
        table, name = ref.split(".", 1)
        if table == "_Measures":
            return touches(dax.get(name, ""))
        return table == "Invoices" or (table, name) in blocked_cols

    broken = {}
    for vf in sorted(definition.glob("pages/*/visuals/*/visual.json")):
        v = json.loads(vf.read_text())
        refs = re.findall(r'"queryRef": "([^"]+)"', json.dumps(v.get("visual", {}).get("query", {})))
        hit = sorted({r for r in refs if "." in r and bad(r)})
        if hit:
            broken.setdefault(vf.parts[-4], []).append((vf.parts[-2], hit))
    print("  OLS (Store Staff) — visuals that would show an error for that role:")
    for page, rows in broken.items():
        for vis, hit in rows:
            print(f"    {page}/{vis}: {', '.join(hit)}")
    assert "table_vendors" not in {vis for vis, _ in broken.get("vendors", [])}, "Vendor scorecard must be OLS-safe"
    print("  ✓ vendors/table_vendors uses no OLS-hidden field")


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


COMPOSITE = PBI / "Restock Komposit.Report"


def composite_report():
    """Restock Komposit: one test page over the composite variant model (DirectQuery + Dual + Import).
    Same theme, no canvas image: it exists so Desktop has something to open, and visuals to time in Performance Analyzer."""
    shutil.rmtree(COMPOSITE, ignore_errors=True)
    dump(PBI / "Restock Komposit.pbip", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": "Restock Komposit.Report"}}],
        "settings": {"enableAutoRecovery": True}})
    dump(COMPOSITE / ".platform", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Report", "displayName": "Restock Komposit"},
        "config": {"version": "2.0", "logicalId": "5d1c7e2a-8b40-4f69-a3e5-0c9b6d27f418"}})
    dump(COMPOSITE / "definition.pbir", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": "../Restock Komposit.SemanticModel"}}})
    d = COMPOSITE / "definition"
    dump(d / "version.json", {"$schema": f"{SCHEMA}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"})
    dump(COMPOSITE / "StaticResources" / "RegisteredResources" / "StockRestock.json", THEME)  # same theme as the main report
    dump(d / "report.json", {
        "$schema": f"{SCHEMA}/report/3.3.0/schema.json",
        "themeCollection": {"customTheme": {
            "name": "StockRestock.json", "type": "RegisteredResources",
            "reportVersionAtImport": {"visual": "1.8.95", "report": "2.0.95", "page": "1.3.95"}}},
        "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True},
        "resourcePackages": [{"name": "RegisteredResources", "type": "RegisteredResources", "items": [
            {"name": "StockRestock.json", "path": "StockRestock.json", "type": "CustomTheme"}]}]})
    pid = "composite"
    dump(d / "pages" / "pages.json", {"$schema": f"{SCHEMA}/pagesMetadata/1.1.0/schema.json",
                                      "pageOrder": [pid], "activePageName": pid})
    page = Page(pid, "Storage Modes", "", "")
    textbox(page, "title", 24, 16, 1552, 64,
            ("Storage modes: DirectQuery till rows, Dual Date & Store, Import Item & Stock", 18, INK, True),
            ("Daily Sales Detail is live SQL on PostgreSQL; the hidden Daily Sales aggregation answers day/store/brand "
             "queries from memory; Detail → Item is a limited relationship (DirectQuery → Import).", 10, INK2, False))
    for i, m in enumerate(["Detail Units", "Detail Dollars", "Detail Rows", "Units Without Item"]):
        page.add(f"kpi_{i}", "card", 24 + i * 392, 96, 376, 140, roles={"Values": f"_Measures.{m}"},
                 alt=f"KPI card: {m}.")
    page.add("city_slicer", "slicer", 24, 252, 376, 120, roles={"Values": "Store.City"}, title="City (Dual table)",
             objects={"data": props(mode="Dropdown")})
    bar(page, "units_by_day", "clusteredColumnChart", 416, 252, 1160, 300, "Date.Date", "_Measures.Detail Units",
        ACCENT, "Units per day", "Date is Dual: this query can be answered by the Import aggregation")
    table(page, "by_category", 24, 568, 760, 316,
          ["Item.Category", "_Measures.Detail Units", "_Measures.Detail Units Via TREATAS",
           "_Measures.Units At Item Cost"],
          "By item category (limited relationship)",
          "Item is Import, the till rows are DirectQuery: filters still flow, RELATED does not")
    table(page, "by_store", 800, 568, 776, 316,
          ["Store.Store Name", "_Measures.Detail Dollars", "_Measures.Stock End Units"],
          "By store (Dual dimension, DirectQuery + Import facts)")
    dump(d / "pages" / pid / "page.json", {"$schema": f"{SCHEMA}/page/2.1.0/schema.json", "name": pid,
                                            "displayName": page.name, "displayOption": "FitToPage", "width": W, "height": H})
    for v in page.visuals:
        dump(d / "pages" / pid / "visuals" / v["name"] / "visual.json", v)
    print(f"  ✓ wrote {COMPOSITE.relative_to(ROOT)} (1 page, {len(page.visuals)} visuals)")
    result = subprocess.run(["pbir", "validate", str(COMPOSITE), "--all"], capture_output=True, text=True,
                            env={**os.environ, "NO_COLOR": "1", "COLUMNS": "200"})
    print(result.stdout[-3000:], result.stderr[-2000:])


def main():
    shutil.rmtree(REPORT, ignore_errors=True)
    res = REPORT / "StaticResources" / "RegisteredResources"
    res.mkdir(parents=True)
    dump(res / "StockRestock.json", THEME)
    background_png(res / "canvas.png")

    dump(PBI / "StockRestock.pbip", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": "StockRestock.Report"}}],
        "settings": {"enableAutoRecovery": True}})
    dump(REPORT / ".platform", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Report", "displayName": "StockRestock"},
        "config": {"version": "2.0", "logicalId": "c4e7a913-2b6d-4f80-b5a1-7e3d9f04c26b"}})
    dump(REPORT / "definition.pbir", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": "../StockRestock.SemanticModel"}}})
    d = REPORT / "definition"
    dump(d / "version.json", {"$schema": f"{SCHEMA}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"})
    dump(d / "report.json", {
        "$schema": f"{SCHEMA}/report/3.3.0/schema.json",
        "themeCollection": {"customTheme": {
            "name": "StockRestock.json", "type": "RegisteredResources",
            "reportVersionAtImport": {"visual": "1.8.95", "report": "2.0.95", "page": "1.3.95"}}},
        "objects": {"outspacePane": [{"properties": {"expanded": lit(False)}}]},
        "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True,
                     "hideVisualContainerHeader": False, "useEnhancedTooltips": True,
                     "exportDataMode": "AllowSummarized"},
        # Report-level filter: store 46 closed during 2016, so it is left out of every page.
        "filterConfig": {"filters": [visual_filter("f_open_stores", "Store.Closed 2016", [False])]},
        "publicCustomVisuals": [DENEB],
        "resourcePackages": [{"name": "RegisteredResources", "type": "RegisteredResources", "items": [
            {"name": "StockRestock.json", "path": "StockRestock.json", "type": "CustomTheme"},
            {"name": "canvas.png", "path": "canvas.png", "type": "Image"}]}]})
    all_pages = [(pid, name, title, sub, "nav") for pid, name, title, sub in PAGES] + \
        [(pid, name, title, sub, "detail") for pid, name, title, sub, _ in DETAIL_PAGES] + \
        [(pid, name, name, "", "tip") for pid, name in TOOLTIP_PAGES]
    dump(d / "pages" / "pages.json", {"$schema": f"{SCHEMA}/pagesMetadata/1.1.0/schema.json",
                                      "pageOrder": [p[0] for p in all_pages], "activePageName": PAGES[0][0]})

    total = 0
    for pid, name, title, subtitle, kind in all_pages:
        page = Page(pid, name, title, subtitle)
        if kind != "tip":
            scaffold(page)
        BUILDERS[pid](page)
        if kind != "tip":
            help_panel(page)
            BOOKMARKS.append(reset_bookmark(page))
        for f in page.deferred:
            f()
        # Tab order = reading order (top to bottom, then left to right); z keeps the build order.
        for n, v in enumerate(sorted(page.visuals, key=lambda v: (v["position"]["y"], v["position"]["x"]))):
            v["position"]["tabOrder"] = n * 100
        page_json = {
            "$schema": f"{SCHEMA}/page/2.1.0/schema.json",
            "name": pid, "displayName": name, "displayOption": "FitToPage", "width": W, "height": H}
        if kind != "tip":
            page_json["objects"] = {
                "background": [{"properties": {
                    "image": {"image": {"name": lit("canvas.png"),
                                        "url": {"expr": {"ResourcePackageItem": {"PackageName": "RegisteredResources",
                                                                                 "PackageType": 1, "ItemName": "canvas.png"}}},
                                        "scaling": lit("Fit")}},
                    "transparency": lit(0.0)}}],
                "outspace": [{"properties": {"color": color("#E9E8E3")}}]}
        page_json.update(page.page_extra)
        if page.filters:
            page_json["filterConfig"] = {"filters": page.filters}
        if page.interactions:
            names = {v["name"] for v in page.visuals}
            assert all(s_ in names and t in names for s_, t, _ in page.interactions), page.interactions
            page_json["visualInteractions"] = [{"source": s_, "target": t, "type": k} for s_, t, k in page.interactions]
        dump(d / "pages" / pid / "page.json", page_json)
        for v in page.visuals:
            dump(d / "pages" / pid / "visuals" / v["name"] / "visual.json", v)
            if v["name"] in page.mobile:
                mobile = d / "pages" / pid / "visuals" / v["name"] / "mobile.json"
                dump(mobile, {"$schema": f"{SCHEMA}/visualContainerMobileState/2.4.0/schema.json",
                              "position": page.mobile[v["name"]]})
                check_mobile(mobile)
        total += len(page.visuals)
        assert set(page.mobile) <= {v["name"] for v in page.visuals}, set(page.mobile) - {v["name"] for v in page.visuals}
        print(f"  ✓ page {name:<18} {len(page.visuals):>3} visuals" +
              (f", phone layout {len(page.mobile)} visuals (mobile.json checked)" if page.mobile else ""))

    # Bookmarks: one file each + bookmarks.json (navigator groups first, then the loose help / reset bookmarks).
    grouped = {b for _, names in BOOKMARK_GROUPS.values() for b in names}
    for b in BOOKMARKS:
        dump(d / "bookmarks" / f"{b['name']}.bookmark.json", b)
    items = [{"name": g, "displayName": disp, "children": names} for g, (disp, names) in BOOKMARK_GROUPS.items()]
    items += [{"name": b["name"]} for b in BOOKMARKS if b["name"] not in grouped]
    dump(d / "bookmarks" / "bookmarks.json", {"$schema": f"{SCHEMA}/bookmarksMetadata/1.0.0/schema.json", "items": items})
    print(f"  ✓ wrote {REPORT.relative_to(ROOT)} ({len(all_pages)} pages, {total} visuals, {len(BOOKMARKS)} bookmarks)")
    contrast_check()
    ols_check(d)

    result = subprocess.run(["pbir", "validate", str(REPORT), "--all"], capture_output=True, text=True,
                            env={**os.environ, "NO_COLOR": "1", "COLUMNS": "200"})
    print(result.stdout[-3000:], result.stderr[-2000:])
    composite_report()


if __name__ == "__main__":
    main()
