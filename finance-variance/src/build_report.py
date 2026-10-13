"""Generate the Power BI report (PBIR) — 5 designed pages, a drillthrough page and a tooltip page
over the GroupFinance semantic model.

    uv run python src/build_report.py

Writes powerbi/GroupFinance.Report + GroupFinance.pbip, then runs `pbir validate`
(schema + field references against the model). Open GroupFinance.pbip in Power BI Desktop.

Colour rule: blue = actual, grey = comparison, amber = outlook. Teal/orange are reserved
for variance sign (favourable / unfavourable) and never used as series colours.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
PBI = ROOT / "powerbi"
REPORT = PBI / "GroupFinance.Report"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"

W, H = 1600, 900
SIDEBAR = 232
X0, X1, GAP = 256, 1576, 16
CW = X1 - X0

# ---------------------------------------------------------------- palette
INK, INK2, MUTED, HAIR, SURFACE, PLANE = "#0b0b0b", "#52514e", "#898781", "#E6E4DD", "#FCFCFB", "#F4F4F1"
SIDE, SIDE_TEXT, SIDE_LINE = "#12302c", "#a9cfc7", "#21504a"
ACTUAL, COMPARE, OUTLOOK = "#2a78d6", "#B9B8B0", "#e0a030"
GOOD, BAD, TOTAL = "#0F766E", "#C2410C", "#12302c"
CCY = {"EUR": "#2a78d6", "CAD": "#104281", "AUD": "#86b6ef"}


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
    """'Table.Column', '_Measures.Measure', or 'Table.Hierarchy>Level' for a hierarchy level."""
    table, name = ref.rstrip("*").split(".", 1)
    if ">" in name:
        hierarchy, level = name.split(">", 1)
        return {"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {"SourceRef": {"Entity": table}},
                                                                "Hierarchy": hierarchy}}, "Level": level}}
    kind = "Measure" if table == "_Measures" else "Column"
    return {kind: {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}}


def projection(ref):
    """A trailing * marks the hierarchy level the visual is drilled to (active); 'ref|Label' renames the field
    in this visual only (projection displayName)."""
    ref, _, label = ref.partition("|")
    clean = ref.rstrip("*")
    out = {"field": field(ref), "queryRef": clean.replace(">", "."), "nativeQueryRef": clean.split(".", 1)[1].replace(">", " ")}
    if ref.endswith("*"):
        out["active"] = True
    if label:
        out["displayName"] = label
    return out


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


def visual_filter(name, ref, values):
    return {"name": name, "field": field(ref), "type": "Categorical", "filter": in_filter(ref, values)}


OPERATING_DETAIL = [visual_filter("f_kind", "P&L Line.Line Kind", ["detail"]),
                    visual_filter("f_oper", "P&L Line.Is Operating", [True])]
# Measure filter: only the cells flagged by [Is Variance Outlier] (Advanced, = 1).
OUTLIER_FILTER = {"name": "f_outlier", "type": "Advanced",
                  "field": {"Measure": {"Expression": {"SourceRef": {"Entity": "_Measures"}}, "Property": "Is Variance Outlier"}},
                  "filter": {"Version": 2, "From": [{"Name": "m", "Entity": "_Measures", "Type": 0}],
                             "Where": [{"Condition": {"Comparison": {"ComparisonKind": 0,
                                 "Left": {"Measure": {"Expression": {"SourceRef": {"Source": "m"}}, "Property": "Is Variance Outlier"}},
                                 "Right": {"Literal": {"Value": "1L"}}}}}]}}
OPEX_LINES = [visual_filter("f_opex", "P&L Line.Line ID", [f"L{i:02d}" for i in range(5, 12)])]
FOREIGN = [visual_filter("f_ccy", "GL.Currency", list(CCY))]
COUNTRIES = ["United States", "Canada", "France", "Germany", "Australia"]


class Page:
    def __init__(self, pid, name, title, subtitle):
        self.id, self.name, self.title, self.subtitle = pid, name, title, subtitle
        self.visuals = []
        self.interactions = []  # (source visual, target visual, DataFilter | HighlightFilter | NoFilter)
        self.filters = []       # page-level filters
        self.mobile = {}        # visual name -> (x, y, w, h) on the phone layout

    def add(self, name, vtype, x, y, w, h, roles=None, title=None, subtitle=None,
            objects=None, container=None, sort=None, filters=None, hidden=False, alt=None, **visual_extra):
        vc = dict(container or {})
        # Alt text for screen readers: a measure (dynamic) or a fixed sentence; defaults to title + subtitle.
        alt = alt if alt is not None else ". ".join(t for t in (title, subtitle) if t)
        if alt and vtype != "textbox":
            vc.setdefault("general", [{"properties": {"altText": {"expr": field(alt)} if alt.startswith("_Measures.")
                                                      else lit(alt)}}])
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
        if hidden:
            container_json["isHidden"] = True
        self.visuals.append(container_json)

    def vcalc(self, role, name, expr, hidden=False):
        """Append a visual calculation (DAX evaluated on the visual's own grid) to the last visual added."""
        proj = {"field": {"NativeVisualCalculation": {"Language": "dax", "Expression": expr, "Name": name,
                                                      "DataType": "Double"}},
                "queryRef": name, "nativeQueryRef": name}
        if hidden:
            proj["hidden"] = True
        state = self.visuals[-1]["visual"]["query"]["queryState"]
        state.setdefault(role, {"projections": []})["projections"].append(proj)


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
    extra.setdefault("alt", f"Slicer: {ref.split('.', 1)[1]}")
    values = default if isinstance(default, list) else [default]
    general = {"properties": {"filter": {"filter": in_filter(ref, values)}}} if default is not None else None
    objects = {"data": props(mode=mode),
               "header": props(show=header, fontColor=color(MUTED), textSize=9.0),
               "selection": props(singleSelect=single, strictSingleSelect=single)}
    if general:
        objects["general"] = [general]
    objects.update(extra.pop("objects", {}))
    kw = {"syncGroup": {"groupName": sync, "fieldChanges": True, "filterChanges": True}} if sync else {}
    page.add(name, "slicer", x, y, w, h, roles={"Values": ref}, objects=objects, **kw, **extra)


# --------------------------------------------------------- page scaffolding
PAGES = [
    ("overview", "Overview", "Group performance",
     "Operating profit against budget or prior year, with a full-year outlook."),
    ("pnl", "P&L Statement", "Consolidated P&L",
     "Every line in USD, after intercompany eliminations. Variance + is favourable on every line."),
    ("drivers", "Variance Drivers", "Why profit missed budget",
     "Budget operating profit → currency effect → each line's operational variance → actual."),
    ("entities", "Entities & FX", "Entities, ownership & currency",
     "Who owns what, what minority shareholders take, and how exposed the result is to exchange rates."),
    ("costs", "Costs & Headcount", "Operating costs & headcount",
     "Where the money goes, and how cost per head moved as the team grew."),
    ("forecast", "Reforecast", "FY2013 reforecast vs actual",
     "Entity reforecast files (SYNTHETIC) combined in Power Query, against actual and budget."),
]

# Pages kept out of the sidebar: reached by right-click drillthrough or by hovering a bar.
# (id, display name, title, subtitle, page type)
HIDDEN_PAGES = [
    ("account", "Account detail", "Account detail",
     "Drillthrough target: right-click any P&L line (P&L matrix, cost variance, overruns table) → Drill through.",
     "Drillthrough"),
    ("tooltip", "Variance tooltip", "", "", "Tooltip"),
]
TOOLTIP_W, TOOLTIP_H = 320, 240
TOOLTIP = props(show=True, type="Canvas", section="tooltip")  # report-page tooltip wiring

# Overview bookmarks: (name, display name, visual shown, visual hidden)
BOOKMARKS = [("bm_scorecard", "Scorecard view", "table_entity", "bar_entity_compare"),
             ("bm_chart", "Chart view", "bar_entity_compare", "table_entity")]

ROW_KPI_Y, ROW_KPI_H = 104, 96
ROW2_Y, ROW2_H = 216, 332
ROW3_Y, ROW3_H = 564, 316


def cols(*widths):
    """x positions for panels of the given widths separated by GAP, starting at X0."""
    xs, x = [], X0
    for w in widths:
        xs.append(x)
        x += w + GAP
    assert abs(x - GAP - X1) <= 1, f"row widths {widths} sum to {x - GAP - X0}, expected {CW}"
    return xs


def scaffold(page):
    textbox(page, "brand", 24, 28, 190, 64,
            ("ADVENTURE WORKS", 8, SIDE_TEXT, True), ("Group Finance", 17, "#FFFFFF", True))
    for i, (pid, name, *_) in enumerate(PAGES):
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
                            "background": props(show=False), "border": props(show=False)},
                 alt=f"Go to page {name}")
    textbox(page, "footer", 24, 790, 190, 90,
            ("Source: Microsoft AdventureWorksDW finance ledger (MIT)", 8, SIDE_TEXT, False),
            ("Pipeline: Python · DuckDB SQL · Power BI (TMDL + PBIR)", 8, SIDE_TEXT, False))

    textbox(page, "title", X0, 22, 500, 70, (page.title, 20, INK, True), (page.subtitle, 10, INK2, False))
    sx = cols(500, 120, 150, 180, 150, 140)
    slicer(page, "slicer_year", sx[1], 16, 120, 76, "Date.Year", default=2013, sync="Year")
    slicer(page, "slicer_month", sx[2], 16, 150, 76, "Date.Month Name", single=False, sync="Month")
    slicer(page, "slicer_entity", sx[3], 16, 180, 76, "Entity.Entity", single=False, sync="Entity")
    slicer(page, "slicer_compare", sx[4], 16, 150, 76, "Compare To.Compare To", sync="Compare")
    slicer(page, "slicer_timeview", sx[5], 16, 140, 76, "Time View.Time View", sync="TimeView")


# Dynamic alt text (a measure) for the cards that have one; the rest read "KPI card: <measure>".
KPI_ALT = {"Operating Profit": "_Measures.Alt Text Operating Profit", "Operating Profit Variance": "_Measures.Alt Text Operating Profit",
           "Opex Spend": "_Measures.Alt Text Opex", "Operating Expenses Variance": "_Measures.Alt Text Opex"}


def kpis(page, measures):
    w = (CW - GAP * (len(measures) - 1)) // len(measures)
    for i, (m, units, precision) in enumerate(measures):
        page.add(f"kpi_{i}", "card", X0 + i * (w + GAP), ROW_KPI_Y, w, ROW_KPI_H, roles={"Values": f"_Measures.{m}"},
                 alt=KPI_ALT.get(m.partition("|")[0], f"KPI card: {m.partition('|')[2] or m}"),
                 objects={"labels": props(color=color(INK), fontSize=22.0, fontFamily="Segoe UI Semibold",
                                          labelDisplayUnits=units, labelPrecision=precision),
                          "categoryLabels": props(show=True, color=color(MUTED), fontSize=9.0)})


M, K, NONE = 1000000, 1000, 0
NO_UNITS = 1  # "None": let the measure's dynamic format string ($M / $K) do the scaling


def bar_labels(units=K):
    return props(show=True, color=color(INK2), fontSize=8.0, labelDisplayUnits=units, labelPrecision=0)


def axes(show_x=True, show_y=True, grid=True):
    return {"categoryAxis": props(show=show_x, showAxisTitle=False, fontSize=9.0, labelColor=color(MUTED)),
            "valueAxis": props(show=show_y, showAxisTitle=False, fontSize=9.0, labelColor=color(MUTED),
                               gridlineShow=grid, gridlineColor=color("#ECEBE6"), gridlineStyle="solid")}


def legend(position="Top"):
    return props(show=True, position=position, fontSize=9.0, labelColor=color(INK2), showTitle=False)


def variance_fill():
    return [{"properties": {"fill": expr_color("_Measures.Variance Color")}, "selector": WILDCARD}]


def table_fmt(color_measures=(), image=False):
    """image=True sizes the inline SVG bars (an ImageUrl measure) in the grid."""
    vals = [{"properties": {"fontColor": expr_color("_Measures.Variance Color")},
             "selector": {**WILDCARD, "metadata": m}} for m in color_measures]
    grid = props(rowPadding=6, **({"imageHeight": 16.0, "imageWidth": 120.0} if image else {}))
    return {"values": vals, "grid": grid} if vals else {"grid": grid}


def outlier_rows(objects, columns):
    """Background rule on every column: [Outlier Fill] (light red when the row holds a P95 outlier cell).
    Merged into an existing per-column fontColor entry so one selector carries both properties."""
    vals = {v["selector"]["metadata"]: v for v in objects.get("values", [])}
    for c in columns:
        vals.setdefault(c, {"properties": {}, "selector": {**WILDCARD, "metadata": c}})["properties"]["backColor"] = \
            expr_color("_Measures.Outlier Fill")
    return {**objects, "values": list(vals.values())}


# ------------------------------------------------------------------- pages
def overview(p):
    kpis(p, [("Net Sales", M, 1), ("Operating Profit", M, 2), ("Operating Profit Comparison", M, 2),
             ("Operating Profit Variance", NO_UNITS, 1), ("Operating Profit Variance %", NONE, 1), ("Operating Margin %", NONE, 1),
             ("Entities Behind Budget", NONE, 0)])
    a, b = cols(880, 424)
    # Metric field parameter: the first line is whichever metric is picked above it. pbir rejects a
    # field-parameter column in a Measure-only role, so the pick reaches the chart through [Metric Value] (SWITCH).
    slicer(p, "slicer_metric", a, ROW2_Y, 880, 44, "Metric.Metric", mode="HorizontalList", header=False,
           default="Actual",
           objects={"items": props(fontColor=color(INK2), textSize=9.0)},
           container={"background": props(show=False), "border": props(show=False)})
    p.add("line_monthly", "lineChart", a, ROW2_Y + 52, 880, ROW2_H - 52,
          roles={"Category": "Date.Month Start",
                 "Y": ["_Measures.Metric Value", "_Measures.Operating Profit Comparison"]},
          title="Monthly operating profit",
          subtitle="Line 1 = the metric picked above (Metric field parameter); line 2 = the comparison",
          alt="_Measures.Alt Text Operating Profit",
          objects={**axes(), "legend": legend(),
                   "lineStyles": props(strokeWidth=2, showMarker=True, markerSize=3),
                   "dataPoint": [measure_color("_Measures.Metric Value", ACTUAL),
                                 measure_color("_Measures.Operating Profit Comparison", COMPARE)]})
    p.add("card_headline", "card", b, ROW2_Y, 424, 176, roles={"Values": "_Measures.Headline Insight"},
          title="In one sentence", alt="_Measures.Headline Insight",
          objects={"labels": props(color=color(INK), fontSize=13.0, fontFamily="Segoe UI"),
                   "categoryLabels": props(show=False), "wordWrap": props(show=True)})
    y = ROW2_Y + 188
    slicer(p, "slicer_asof", b, y, 140, ROW2_H - 188, "As Of Month.As Of", default="Jun 2013", sync="AsOf")
    # Two full-year views side by side: budget-based outlook vs a LINESTX trend line on the last 12 months of opex.
    p.add("card_outlook", "multiRowCard", b + 156, y, 268, ROW2_H - 188,
          roles={"Values": ["_Measures.Operating Profit Outlook", "_Measures.Opex Outlook", "_Measures.Opex Trend Forecast"]},
          title="Full-year outlook", subtitle="Opex: budget after As Of vs 12-month trend (LINESTX)",
          objects={"dataLabels": props(color=color(INK), fontSize=12.0, fontFamily="Segoe UI Semibold"),
                   "categoryLabels": props(show=True, color=color(MUTED), fontSize=8.0),
                   "card": props(barShow=False, outlineStyle="None")})
    a, b = cols(640, 664)
    p.add("slicer_analyze", "slicer", a, ROW3_Y, 400, 44, roles={"Values": "Analyze By.Analyze By"},
          objects={"data": props(mode="HorizontalList"), "header": props(show=False),
                   "selection": props(singleSelect=True, strictSingleSelect=True),
                   "items": props(fontColor=color(INK2), textSize=9.0),
                   "general": [{"properties": {"filter": {"filter": in_filter("Analyze By.Analyze By", ["Entity"])}}}]},
          container={"background": props(show=False), "border": props(show=False)})
    p.add("bar_analyze", "clusteredBarChart", a, ROW3_Y + 52, 400, ROW3_H - 52,
          roles={"Category": "Analyze By.Analyze By", "Y": "_Measures.Variance"},
          sort=("_Measures.Variance", "Ascending"),
          title="Operating variance by the dimension picked above", subtitle="Detail operating lines only; teal = favourable",
          filters=OPERATING_DETAIL, container={"visualTooltip": TOOLTIP}, alt="_Measures.Alt Text Biggest Miss",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(), "dataPoint": variance_fill()})
    # Dynamic segmentation: entities counted per variance band (disconnected Variance Band table).
    p.add("col_band", "clusteredColumnChart", a + 416, ROW3_Y, 224, ROW3_H,
          roles={"Category": "Variance Band.Band", "Y": "_Measures.Entities in Band"},
          sort=("Variance Band.Band", "Ascending"),
          title="Entities by variance band", subtitle="Operating profit variance %: <−10% · to −2% · ±2% · >+2%",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(NONE), "dataPoint": props(fill=color(ACTUAL))})
    # Two views of the same entities in one slot; the bookmark buttons swap which one is visible.
    p.add("table_entity", "tableEx", b, ROW3_Y, 664, ROW3_H,
          roles={"Values": ["Entity.Entity", "_Measures.Entity Variance Rank", "_Measures.Variance Status",
                            "_Measures.Variance", "_Measures.Variance Arrow", "_Measures.Share of Region Variance",
                            "_Measures.Entity Variance Bar"]},
          title="Entity scorecard — operating profit",
          subtitle="Rank 1 = worst miss (RANK); ▲/▼ = favourable/unfavourable; share of the region's variance",
          filters=OPERATING_DETAIL, sort=("_Measures.Entity Variance Rank", "Ascending"), alt="_Measures.Alt Text Biggest Miss",
          objects=table_fmt(["_Measures.Variance", "_Measures.Variance Arrow"], image=True))
    p.add("bar_entity_compare", "clusteredBarChart", b, ROW3_Y, 664, ROW3_H,
          roles={"Category": "Entity.Entity", "Y": ["_Measures.Actual", "_Measures.Comparison"]},
          sort=("_Measures.Actual", "Descending"), hidden=True,
          title="Entity operating profit — actual vs comparison", subtitle="USD; hover a bar for the variance tooltip",
          filters=OPERATING_DETAIL, container={"visualTooltip": TOOLTIP}, alt="_Measures.Alt Text Biggest Miss",
          objects={**axes(show_y=False, grid=False), "legend": legend(), "labels": bar_labels(),
                   "dataPoint": [measure_color("_Measures.Actual", ACTUAL), measure_color("_Measures.Comparison", COMPARE)]})
    for i, (bm, label, *_) in enumerate(BOOKMARKS):
        p.add(f"btn_{bm}", "actionButton", b + 664 - 208 + i * 100, ROW3_Y + 10, 94, 26,
              objects={"text": props(show=True, text=label.replace(" view", ""), fontColor=color(INK2), fontSize=9.0,
                                     fontFamily="Segoe UI Semibold"),
                       "fill": props(show=True, fillColor=color(PLANE)), "outline": props(show=True, lineColor=color(HAIR)),
                       "icon": props(show=False), "shape": props(tileShape="rectangleRounded", rectangleRoundedCurve=6)},
              container={"visualLink": props(show=True, type="Bookmark", bookmark=bm),
                         "background": props(show=False), "border": props(show=False)}, alt=f"Show the {label.lower()}")
    # Edit interactions: picking a bar filters the scorecard but leaves the KPI cards and the headline on the page total.
    p.interactions += [("bar_analyze", f"kpi_{i}", "NoFilter") for i in range(7)]
    p.interactions += [("bar_analyze", "col_band", "NoFilter"), ("bar_analyze", "card_headline", "NoFilter"), ("bar_analyze", "table_entity", "DataFilter")]
    # Phone layout (portrait, 320 wide): 4 KPIs, the one-sentence headline, the variance bar.
    p.mobile = {"title": (0, 0, 320, 64), "kpi_0": (0, 72, 156, 84), "kpi_1": (164, 72, 156, 84),
                "kpi_3": (0, 164, 156, 84), "kpi_4": (164, 164, 156, 84), "card_headline": (0, 256, 320, 160),
                "bar_analyze": (0, 424, 320, 320)}


def pnl(p):
    a, b = cols(808, 496)
    vals = ["_Measures.Actual", "_Measures.Comparison", "_Measures.Variance", "_Measures.Variance %"]
    # Rows: the P&L line, then the Account level of the P&L Drill hierarchy (+ expands a line into its accounts).
    p.add("matrix_pnl", "pivotTable", a, ROW_KPI_Y, 808, H - 20 - ROW_KPI_Y,
          roles={"Rows": ["P&L Line.Line", "Account.P&L Drill>Account"], "Values": vals},
          title="Profit & loss",
          subtitle="Shaded rows are subtotals. + drills a line down to its accounts; right-click → Drill through → Account detail",
          objects={"values": [{"properties": {"backColor": expr_color("_Measures.Line Row Fill"),
                                              **({"fontColor": expr_color("_Measures.Variance Color")}
                                                 if m in vals[2:] else {})},
                               "selector": {**WILDCARD, "metadata": m}} for m in vals],
                   "subTotals": props(rowSubtotals=False, columnSubtotals=False),
                   "rowHeaders": props(showExpandCollapseButtons=True, stepped=True),
                   "grid": props(rowPadding=7)})
    hs, hc = 236, 244
    hb = H - 20 - ROW_KPI_Y - hs - hc - 2 * GAP
    p.add("table_pnl_summary", "tableEx", b, ROW_KPI_Y, 496, hs,
          roles={"Values": ["P&L Line.Line", "_Measures.Actual", "_Measures.Variance", "_Measures.Variance Arrow"]},
          title="Key lines", subtitle="▲ favourable / ▼ unfavourable vs the comparison",
          filters=[visual_filter("f_key", "P&L Line.Line ID", ["S1", "S2", "S3", "S4", "S6"])],
          sort=("P&L Line.Line", "Ascending"), objects=table_fmt(["_Measures.Variance", "_Measures.Variance Arrow"]))
    # Fiscal Calendar hierarchy (Year → Quarter → Month), opened at month level; the header arrows drill up/down.
    p.add("col_monthly_var", "lineClusteredColumnComboChart", b, ROW_KPI_Y + hs + GAP, 496, hc,
          roles={"Category": ["Date.Fiscal Calendar>Year", "Date.Fiscal Calendar>Year Quarter", "Date.Fiscal Calendar>Month Year*"],
                 "Y": "_Measures.Variance"},
          title="Monthly operating variance",
          subtitle="Fiscal Calendar hierarchy: drill up to quarter or year; line = cumulative (visual calculation RUNNINGSUM)",
          filters=OPERATING_DETAIL,
          objects={**axes(), "legend": legend(), "dataPoint": variance_fill(),
                   "lineStyles": props(strokeWidth=2, showMarker=True, markerSize=3)})
    p.vcalc("Y2", "Cumulative variance", "RUNNINGSUM ( [Variance] )")
    # The only P&L visual that reads an object the External Auditor role can't see (Budget Drivers, via [Driver Growth]),
    # so under that role only this visual fails and the rest of the page still works.
    p.add("table_budget", "tableEx", b, ROW_KPI_Y + hs + hc + 2 * GAP, 496, hb,
          roles={"Values": ["P&L Line.Line", "_Measures.Budget", "_Measures.Driver Growth"]},
          title="Budget assumptions — operating expenses",
          subtitle="Prior-year actual × (1 + growth), via TREATAS; hidden from the auditor role",
          filters=OPEX_LINES, sort=("P&L Line.Line", "Ascending"),
          objects=table_fmt())
    p.mobile = {"title": (0, 0, 320, 64), "slicer_year": (0, 72, 320, 64), "table_pnl_summary": (0, 144, 320, 240),
                "col_monthly_var": (0, 392, 320, 280)}


def drivers(p):
    kpis(p, [("Operating Profit Budget", M, 2), ("Operating Profit FX Effect", K, 0),
             ("Operating Profit Operational Variance|Operational variance (excl. FX)", K, 0), ("Operating Profit", M, 2),
             ("Operating Overrun Only|All misses, revenue + cost (no netting)", M, 2)])
    a, b = cols(880, 424)
    p.add("waterfall_bridge", "waterfallChart", a, ROW2_Y, 880, ROW2_H, alt="_Measures.Alt Text Operating Profit",
          roles={"Category": "Variance Step.Step", "Y": "_Measures.Bridge Amount"},
          sort=("Variance Step.Step", "Ascending"),
          title="Budget → actual operating profit bridge",
          subtitle="Always vs budget. The last bar (Total) is actual operating profit",
          objects={**axes(), "labels": props(show=True, color=color(INK2), fontSize=8.0, labelDisplayUnits=K,
                                             labelPrecision=0),
                   "sentimentColors": props(increaseFill=color(GOOD), decreaseFill=color(BAD), totalFill=color(TOTAL))})
    # Decomposition tree: the operating variance (detail operating lines sum to operating profit variance), split on demand.
    p.add("decomp_variance", "decompositionTreeVisual", b, ROW2_Y, 424, ROW2_H,
          roles={"Analyze": "_Measures.Operational Variance",
                 "ExplainBy": ["Entity.Entity", "P&L Line.Line", "Department.Department"]},
          title="Where the operational variance sits (excl. FX)",
          subtitle="At budget rates, so the root equals the card; + splits by Entity → P&L line → Department",
          filters=OPERATING_DETAIL, objects={"tree": props(barsPerLevel=6, density="dense")})
    a, b, c = cols(552, 456, 280)
    p.add("table_top_var", "tableEx", a, ROW3_Y, 552, ROW3_H,
          roles={"Values": ["Entity.Entity", "P&L Line.Line", "_Measures.Actual", "_Measures.Budget",
                            "_Measures.FX Effect|FX", "_Measures.Operational Variance|Operational var."]},
          title="Biggest operational overruns", subtitle="Entity × line, at budget rates; worst first",
          filters=OPERATING_DETAIL, sort=("_Measures.Operational Variance", "Ascending"),
          objects=table_fmt(["_Measures.Operational Variance"]))
    # Pareto: accounts by opex overrun (columns) + cumulative share (line); the title says how many make 80%.
    p.add("combo_pareto", "lineClusteredColumnComboChart", b, ROW3_Y, 456, ROW3_H,
          roles={"Category": "Account.Account", "Y": "_Measures.Opex Overrun", "Y2": "_Measures.Overrun Cumulative %"},
          sort=("_Measures.Opex Overrun", "Descending"),
          title="Pareto of the opex overrun",
          subtitle="Opex accounts only (the 'all misses' card adds revenue + cost of sales); line = cumulative %",
          container={"title": [{"properties": {"show": lit(True), "text": {"expr": field("_Measures.Pareto Title")}}}]},
          objects={**axes(show_x=False), "legend": props(show=False),
                   "dataPoint": [measure_color("_Measures.Opex Overrun", BAD), measure_color("_Measures.Overrun Cumulative %", INK2)],
                   "lineStyles": props(strokeWidth=2, showMarker=False)})
    p.add("bar_fx_ccy", "clusteredBarChart", c, ROW3_Y, 280, ROW3_H,
          roles={"Category": "GL.Currency", "Y": "_Measures.FX Effect"},
          title="FX effect by currency", subtitle="Operating lines translated at actual vs budget rate",
          filters=OPERATING_DETAIL + FOREIGN,
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(),
                   "dataPoint": [{"properties": {"fill": expr_color("_Measures.Variance Color")}, "selector": WILDCARD}]})
    # Edit interactions: a row in the overruns table highlights its bars in the bridge; nothing filters the FX chart
    # (it explains currency, which an entity × line pick would only blank out).
    p.interactions += [("table_top_var", "waterfall_bridge", "HighlightFilter"), ("table_top_var", "bar_fx_ccy", "NoFilter"),
                       ("waterfall_bridge", "bar_fx_ccy", "NoFilter"), ("decomp_variance", "bar_fx_ccy", "NoFilter"),
                       ("table_top_var", "combo_pareto", "NoFilter")]


def entities(p):
    kpis(p, [("Net Income", M, 2), ("Group Share", M, 2), ("Non-Controlling Interest", K, 0),
             ("Intercompany Eliminated", M, 2), ("Foreign Currency Actual", M, 2)])
    a, b = cols(760, 544)
    p.add("table_ownership", "tableEx", a, ROW2_Y, 760, ROW2_H,
          roles={"Values": ["Entity.Entity", "Entity.Currency Group", "Entity.Effective Ownership",
                            "Entity.Consolidated From", "Entity.Has Budget", "_Measures.Net Income", "_Measures.Group Share",
                            "_Measures.Non-Controlling Interest"]},
          title="Ownership & minority interest", subtitle="Net income per entity; group share = net income × effective ownership",
          objects=table_fmt())
    p.add("line_fx", "lineChart", b, ROW2_Y, 544, ROW2_H,
          roles={"Category": "Date.Month Start", "Y": [f"_Measures.{c} per USD" for c in CCY]},
          title="Exchange rates", subtitle="Local currency per USD, monthly average; dashed = trend line (Analytics pane)",
          objects={**axes(), "legend": legend(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "trend": props(show=True, combineSeries=False, style="dashed", width=1, transparency=30.0),
                   "dataPoint": [measure_color(f"_Measures.{c} per USD", h) for c, h in CCY.items()]})
    a, b, c, d = cols(280, 340, 324, 328)
    p.add("slicer_fx_shock", "slicer", a, ROW3_Y, 280, 120, roles={"Values": "FX Shock.FX Shock"},
          title="What-if: move every non-USD currency",
          objects={"data": props(mode="Basic"), "header": props(show=False),
                   "selection": props(singleSelect=True, strictSingleSelect=True)})
    textbox(p, "fx_shock_note", a + 8, ROW3_Y + 136, 264, 170,
            ("How to read it", 10, INK, True),
            ("+10% = euro, Canadian and Australian dollar all 10% stronger against USD. "
             "Translated profit moves by the same share of the foreign-currency result.", 9, INK2, False))
    p.add("card_shock_op", "multiRowCard", b, ROW3_Y, 340, ROW3_H,
          roles={"Values": ["_Measures.FX Shock Value", "_Measures.Operating Profit", "_Measures.Operating Profit at FX Shock",
                            "_Measures.FX Shock Impact"]},
          title="Operating profit under the shock",
          objects={"dataLabels": props(fontSize=16.0, color=color(INK)),
                   "categoryLabels": props(fontSize=9.0, color=color(MUTED))})
    p.add("bar_fx_move", "clusteredBarChart", c, ROW3_Y, 324, ROW3_H,
          roles={"Category": "FX Rates.Rate Currency", "Y": "_Measures.FX Move vs Budget %"},
          title="Currency move vs budget rate", subtitle="+ = local currency stronger than planned → translates to more USD",
          objects={**axes(show_y=False, grid=False), "labels": props(show=True, color=color(INK2), fontSize=8.0),
                   "dataPoint": props(fill=color(ACTUAL))})
    # Azure Maps filled map (country shapes); the tenant setting "Use Azure Maps visual" must be on.
    p.add("map_country", "azureMap", d, ROW3_Y, 328, ROW3_H,
          roles={"Category": "Entity.Country", "Tooltips": "_Measures.Variance"},
          title="Operating variance by country", subtitle="Azure Maps, filled by country; teal = ahead of the comparison",
          filters=OPERATING_DETAIL + [visual_filter("f_country", "Entity.Country", COUNTRIES)],
          objects={"filledMap": props(show=True, mapTransparency=10.0, borderShow=True, strokeColor=color("#FFFFFF")),
                   "bubbleLayer": props(show=False), "dataPoint": variance_fill()})


def costs(p):
    kpis(p, [("Opex Spend", M, 2), ("Operating Expenses Variance", K, 0), ("Avg Headcount", NONE, 0),
             ("Cost per Head", K, 1), ("Salary per Head", K, 1), ("Sales per Head", K, 0), ("Opex Rolling 12M", M, 2)])
    a, b, c = cols(420, 500, 368)
    p.add("bar_dept_opex", "clusteredBarChart", a, ROW2_Y, 420, ROW2_H,
          roles={"Category": "Department.Department", "Y": "_Measures.Opex Spend"},
          sort=("_Measures.Opex Spend", "Descending"),
          title="Operating expenses by department", subtitle="USD, actual", alt="_Measures.Alt Text Opex",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(), "dataPoint": props(fill=color(ACTUAL))})
    p.add("line_headcount", "lineChart", b, ROW2_Y, 500, ROW2_H,
          roles={"Category": "Date.Month Start", "Y": ["_Measures.Avg Headcount", "_Measures.Headcount PY"]},
          title="Headcount", subtitle="Month-end heads vs same month last year",
          objects={**axes(), "legend": legend(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color("_Measures.Avg Headcount", ACTUAL),
                                 measure_color("_Measures.Headcount PY", COMPARE)]})
    p.vcalc("Y", "3-month average", "MOVINGAVERAGE ( [Avg Headcount], 3 )")
    # Headcount Plan is per region: Entity[Region] reaches it through a many-to-many (limited) relationship.
    p.add("table_heads_plan", "tableEx", c, ROW2_Y, 368, ROW2_H,
          roles={"Values": ["Entity.Region", "_Measures.Heads Closing", "_Measures.Heads Plan", "_Measures.Heads vs Plan"]},
          title="Heads vs plan", subtitle="Closing heads vs year-end plan (SYNTHETIC), by region; total includes planned Asia Operations",
          sort=("Entity.Region", "Ascending"), objects=table_fmt())
    a, b, c = cols(360, 600, 328)
    p.add("bar_opex_var", "clusteredBarChart", a, ROW3_Y, 360, ROW3_H,
          roles={"Category": "P&L Line.Line", "Y": "_Measures.Variance"},
          sort=("_Measures.Variance", "Ascending"), filters=OPEX_LINES, container={"visualTooltip": TOOLTIP},
          title="Cost variance by line", subtitle="Orange = overspend; right-click a bar: drill through",
          alt="_Measures.Alt Text Opex",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(), "dataPoint": variance_fill()})
    p.add("table_dept", "tableEx", b, ROW3_Y, 600, ROW3_H,
          roles={"Values": ["Department.Department", "_Measures.Avg Headcount", "_Measures.Headcount PY",
                            "_Measures.Opex Spend", "_Measures.Share of Group Opex", "_Measures.Cost per Head|Cost/head",
                            "_Measures.Operating Expenses Variance|Opex var."]},
          title="Department scorecard", subtitle="Spend rank = visual calculation RANK; share = of all departments (REMOVEFILTERS)",
          sort=("_Measures.Opex Spend", "Descending"),
          objects=table_fmt(["_Measures.Operating Expenses Variance"]))
    p.vcalc("Values", "Spend rank", "RANK ( DENSE, ORDERBY ( [Opex Spend], DESC ) )")
    p.add("table_acct_abc", "tableEx", c, ROW3_Y, 328, ROW3_H,
          roles={"Values": ["Account.Account", "_Measures.Opex Spend", "_Measures.Spend Class"]},
          title="Accounts by spend class (ABC)", subtitle="A = top 70% of opex, B = next 20%, C = rest; follows slicers",
          sort=("_Measures.Opex Spend", "Descending"), objects=table_fmt())
    # Page filter (locked): only operating P&L lines reach this page's visuals.
    p.filters.append({**visual_filter("pf_operating", "P&L Line.Is Operating", [True]), "isLockedInViewMode": True})


def forecast(p):
    kpis(p, [("Operating Profit Forecast", M, 2), ("Operating Profit", M, 2), ("Operating Profit vs Forecast", K, 0),
             ("Entities Submitted", NONE, 0), ("Late Submissions", NONE, 0)])
    a, b = cols(880, 424)
    p.add("line_forecast", "lineChart", a, ROW2_Y, 880, ROW2_H,
          roles={"Category": "Date.Month Start",
                 "Y": ["_Measures.Operating Profit", "_Measures.Operating Profit Budget", "_Measures.Operating Profit Forecast"]},
          title="Monthly operating profit: actual, budget, reforecast", subtitle="Reforecast exists for FY2013 only",
          objects={**axes(), "legend": legend(), "lineStyles": props(strokeWidth=2, showMarker=True, markerSize=3),
                   "dataPoint": [measure_color("_Measures.Operating Profit", ACTUAL),
                                 measure_color("_Measures.Operating Profit Budget", COMPARE),
                                 measure_color("_Measures.Operating Profit Forecast", OUTLOOK)]})
    p.add("table_submissions", "tableEx", b, ROW2_Y, 424, ROW2_H,
          roles={"Values": ["Forecast.Entity Code|Entity", "Forecast.Submitted", "Forecast.Is Late Submission|Late",
                            "_Measures.Operating Profit Forecast|OP forecast"]},
          title="Submissions", subtitle="One row per entity block; deadline 10 Jan",
          sort=("Forecast.Entity Code", "Ascending"), objects={"grid": props(rowPadding=2)})
    a, b = cols(760, 544)
    p.add("bar_forecast_entity", "clusteredBarChart", a, ROW3_Y, 760, ROW3_H,
          roles={"Category": "Entity.Entity", "Y": "_Measures.Operating Profit vs Forecast"},
          sort=("_Measures.Operating Profit vs Forecast", "Ascending"),
          title="Actual vs reforecast by entity", subtitle="Operating profit; teal = beat the reforecast",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(),
                   "dataPoint": [{"properties": {"fill": expr_color("_Measures.Variance Color")}, "selector": WILDCARD}]})
    textbox(p, "forecast_intake", b + 8, ROW3_Y + 8, 528, ROW3_H - 16,
            ("How the files get in (Power Query)", 11, INK, True),
            ("Folder.Files over seeds/forecast → one parse function per file: skip 2 title rows, promote headers, "
             "fill down the entity, \"n/a\"/\"-\" → null, drop Total, unpivot Jan–Dec, type with the file's locale "
             "(Germany = de-DE, decimal comma) → append → split \"CA01 - Canadian Division\" → merge to Entity and P&L Line.",
             9, INK2, False),
            ("The files are SYNTHETIC: prior-year actual × a seeded adjustment (Australia: FY2013 actual, no prior year).",
             9, INK2, False))


def account(p):
    """Drillthrough page: the accounts under the P&L line the user drilled from."""
    p.add("btn_back", "actionButton", X0, ROW_KPI_Y + 30, 132, 36,
          objects={"text": props(show=True, text="← Back", fontColor=color(INK), fontSize=10.0,
                                 fontFamily="Segoe UI Semibold"),
                   "fill": props(show=True, fillColor=color(SURFACE)), "outline": props(show=True, lineColor=color(HAIR)),
                   "icon": props(show=False), "shape": props(tileShape="rectangleRounded", rectangleRoundedCurve=8)},
          container={"visualLink": props(show=True, type="Back"),
                     "background": props(show=False), "border": props(show=False)})
    x, w = X0 + 148, (CW - 148 - 5 * GAP) // 6
    for i, (m, units, precision) in enumerate([("Actual", K, 0), ("Comparison", K, 0), ("Variance", K, 0),
                                               ("Variance %", NONE, 1), ("Cut-off Drift", K, 0), ("Accounts Posted", NONE, 0)]):
        p.add(f"kpi_{i}", "card", x + i * (w + GAP), ROW_KPI_Y, w, ROW_KPI_H, roles={"Values": f"_Measures.{m}"},
              objects={"labels": props(color=color(INK), fontSize=22.0, fontFamily="Segoe UI Semibold",
                                       labelDisplayUnits=units, labelPrecision=precision),
                       "categoryLabels": props(show=True, color=color(MUTED), fontSize=9.0)})
    a3, b3, c3 = cols(520, 416, 352)
    a, b = cols(760, 544)
    p.add("table_accounts", "tableEx", a, ROW2_Y, 760, ROW2_H,
          roles={"Values": ["Account.Account Code", "Account.Account", "_Measures.Account Variance Rank",
                            "_Measures.Actual", "_Measures.Comparison", "_Measures.Variance",
                            "_Measures.Outlier Cells", "_Measures.Account Variance Bar"]},
          title="Accounts under the selected line",
          subtitle="Rank 1 = worst miss (RANKX); red row = holds a P95 outlier cell (conditional formatting); bar = SVG",
          sort=("_Measures.Account Variance Rank", "Ascending"),
          objects=outlier_rows(table_fmt(["_Measures.Variance"], image=True),
                               ["Account.Account Code", "Account.Account", "_Measures.Account Variance Rank",
                                "_Measures.Actual", "_Measures.Comparison", "_Measures.Variance", "_Measures.Outlier Cells"]))
    p.add("table_journal", "tableEx", a3, ROW3_Y, 520, ROW3_H,
          roles={"Values": ["GL Journal.Journal ID", "GL Journal.Posting Date", "Entity.Entity", "Account.Account",
                            "_Measures.Journal Amount"]},
          title="Journal postings", subtitle="GL Journal: one row per posting (DirectQuery in the Fabric model); largest debit first",
          sort=("_Measures.Journal Amount", "Ascending"), objects=table_fmt())
    p.add("col_account_month", "lineClusteredColumnComboChart", b, ROW2_Y, 544, ROW2_H,
          roles={"Category": "Date.Month Start", "Y": "_Measures.Variance", "Y2": "_Measures.Variance Running Total"},
          title="Variance by month", subtitle="Columns: month (teal = favourable); line: running total (WINDOW measure)",
          objects={**axes(), "legend": legend(), "dataPoint": variance_fill(),
                   "lineStyles": props(strokeWidth=2, showMarker=True, markerSize=3)})
    # Outliers: cells whose |variance %| is above the P95 of all account × entity × month cells (measure filter = 1).
    p.add("table_outliers", "tableEx", b3, ROW3_Y, 416, ROW3_H,
          roles={"Values": ["Account.Account", "Entity.Entity", "Date.Month Year", "_Measures.Variance", "_Measures.Variance %"]},
          title="Variance outliers (above P95)", subtitle="|Variance %| > 95th percentile of account × entity × month",
          sort=("_Measures.Variance", "Ascending"), filters=[OUTLIER_FILTER],
          objects=table_fmt(["_Measures.Variance", "_Measures.Variance %"]))
    p.add("bar_account_entity", "clusteredBarChart", c3, ROW3_Y, 352, ROW3_H,
          roles={"Category": "Entity.Entity", "Y": "_Measures.Variance"},
          sort=("_Measures.Variance", "Ascending"),
          title="Variance by entity", subtitle="Worst first",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(), "dataPoint": variance_fill()})


def tooltip(p):
    """Report-page tooltip: shown when hovering a variance bar."""
    textbox(p, "tt_title", 12, 8, 296, 28, ("Variance detail", 11, INK, True))
    for i, (m, units, precision) in enumerate([("Actual", K, 0), ("Comparison", K, 0), ("Variance", K, 0),
                                               ("Variance %", NONE, 1)]):
        p.add(f"tt_{i}", "card", 12 + (i % 2) * 152, 40 + (i // 2) * 98, 144, 90, roles={"Values": f"_Measures.{m}"},
              objects={"labels": props(color=color(INK), fontSize=16.0, fontFamily="Segoe UI Semibold",
                                       labelDisplayUnits=units, labelPrecision=precision),
                       "categoryLabels": props(show=True, color=color(MUTED), fontSize=8.0)})


BUILDERS = {"overview": overview, "pnl": pnl, "drivers": drivers, "entities": entities, "costs": costs, "forecast": forecast,
            "account": account, "tooltip": tooltip}


def page_json(pid, name, ptype, page=None):
    """page.json: designed pages get the canvas wallpaper; drillthrough/tooltip pages are hidden and bound."""
    tip = ptype == "Tooltip"
    out = {"$schema": f"{SCHEMA}/page/2.1.0/schema.json", "name": pid, "displayName": name,
           "displayOption": "FitToPage", "width": TOOLTIP_W if tip else W, "height": TOOLTIP_H if tip else H}
    if tip:
        out["objects"] = {"background": [{"properties": {"color": color("#FFFFFF"), "transparency": lit(0.0)}}]}
    else:
        out["objects"] = {
            "background": [{"properties": {
                "image": {"image": {"name": lit("canvas.png"),
                                    "url": {"expr": {"ResourcePackageItem": {"PackageName": "RegisteredResources",
                                                                             "PackageType": 1, "ItemName": "canvas.png"}}},
                                    "scaling": lit("Fit")}},
                "transparency": lit(0.0)}}],
            "outspace": [{"properties": {"color": color("#E9E8E3")}}]}
    if ptype == "Drillthrough":
        out["filterConfig"] = {"filters": [{"name": "dt_line", "field": field("P&L Line.Line"), "type": "Categorical",
                                            "howCreated": "Drillthrough"}]}
        out["pageBinding"] = {"name": "dt_account", "type": "Drillthrough",
                              "parameters": [{"name": "dt_line_param", "boundFilter": "dt_line",
                                              "fieldExpr": field("P&L Line.Line")}]}
    if ptype == "Tooltip":
        out["pageBinding"] = {"name": "tt_variance", "type": "Tooltip"}
    if ptype:
        out["type"], out["visibility"] = ptype, "HiddenInViewMode"
    if page and page.filters:
        out.setdefault("filterConfig", {"filters": []})["filters"] += page.filters
    if page and page.interactions:
        out["visualInteractions"] = [{"source": a, "target": b, "type": t} for a, b, t in page.interactions]
    return out


def year_from_filter(year):
    """Advanced (Comparison >=) filter on Date[Year], used as a locked report-level filter."""
    return {"name": "rf_year", "field": field("Date.Year"), "type": "Advanced", "isLockedInViewMode": True,
            "filter": {"Version": 2, "From": [{"Name": "d", "Entity": "Date", "Type": 0}],
                       "Where": [{"Condition": {"Comparison": {"ComparisonKind": 2,
                           "Left": {"Column": {"Expression": {"SourceRef": {"Source": "d"}}, "Property": "Year"}},
                           "Right": {"Literal": {"Value": lit_value(year)}}}}}]}}


def tab_order(page):
    """Keyboard order: page content top-to-bottom, left-to-right, then the side panel (brand, nav, footer)."""
    side = lambda v: v["name"] in ("brand", "footer") or v["name"].startswith("nav_")
    for i, v in enumerate(sorted(page.visuals, key=lambda v: (side(v), v["position"]["y"], v["position"]["x"]))):
        v["position"]["tabOrder"] = i * 100


def mobile_json(pos):
    x, y, w, h = pos
    return {"$schema": f"{SCHEMA}/visualContainerMobileState/2.4.0/schema.json",
            "position": {"x": x, "y": y, "z": 0, "width": w, "height": h, "tabOrder": y}}


# IBCS-style variance chart for the Deneb custom visual (Vega-Lite). Deneb is an AppSource visual, so this spec is
# shipped as a file to paste into Deneb's editor with the fields Line, Actual, Budget, Variance; not wired in the report.
DENEB_IBCS = {
    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
    "description": "IBCS variance: AC (solid) vs BU (outline) by P&L line, absolute variance on the right.",
    "data": {"name": "dataset"},
    "spacing": 8,
    "hconcat": [
        {"width": 220, "title": {"text": "AC vs BU", "anchor": "start", "fontSize": 11},
         "encoding": {"y": {"field": "Line", "type": "nominal", "sort": None, "title": None}},
         "layer": [
             {"mark": {"type": "bar", "fill": "#FFFFFF", "stroke": "#404040", "strokeWidth": 1, "yOffset": 4, "height": 12},
              "encoding": {"x": {"field": "Budget", "type": "quantitative", "title": None, "axis": {"format": "~s"}}}},
             {"mark": {"type": "bar", "fill": "#404040", "height": 12},
              "encoding": {"x": {"field": "Actual", "type": "quantitative"}}}]},
        {"width": 160, "title": {"text": "ΔBU", "anchor": "start", "fontSize": 11},
         "encoding": {"y": {"field": "Line", "type": "nominal", "sort": None, "axis": None}},
         "layer": [
             {"mark": {"type": "bar", "height": 12},
              "encoding": {"x": {"field": "Variance", "type": "quantitative", "title": None, "axis": {"format": "~s"}},
                           "color": {"condition": {"test": "datum.Variance >= 0", "value": "#0F766E"}, "value": "#C2410C"}}},
             {"mark": {"type": "text", "align": {"expr": "datum.Variance < 0 ? 'right' : 'left'"},
                       "dx": {"expr": "datum.Variance < 0 ? -3 : 3"}, "fontSize": 9, "color": "#525252"},
              "encoding": {"x": {"field": "Variance", "type": "quantitative"},
                           "text": {"field": "Variance", "type": "quantitative", "format": "+,.2~s"}}}]}],
    "config": {"view": {"stroke": None}, "axis": {"grid": False, "labelFontSize": 9}},
}


def bookmark_json(bm, label, show, hide):
    visual_types = {"table_entity": "tableEx", "bar_entity_compare": "clusteredBarChart"}
    return {
        "$schema": f"{SCHEMA}/bookmark/2.1.0/schema.json", "displayName": label, "name": bm,
        "options": {"applyOnlyToTargetVisuals": True, "targetVisualNames": [show, hide], "suppressData": True},
        "explorationState": {"version": "1.3", "activeSection": "overview", "sections": {"overview": {"visualContainers": {
            show: {"singleVisual": {"visualType": visual_types[show]}},
            hide: {"singleVisual": {"visualType": visual_types[hide], "display": {"mode": "hidden"}}}}}}}}


# ------------------------------------------------------------ theme & files
THEME = {
    "name": "Group Finance",
    "dataColors": [ACTUAL, COMPARE, OUTLOOK, "#104281", "#86b6ef", "#4a3aa7", "#e87ba4", "#008300"],
    "foreground": INK, "foregroundNeutralSecondary": INK2, "foregroundNeutralTertiary": MUTED,
    "background": SURFACE, "backgroundLight": PLANE, "backgroundNeutral": HAIR, "tableAccent": ACTUAL,
    "good": GOOD, "neutral": OUTLOOK, "bad": BAD, "null": "#F0EFEC",
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
    d.rectangle([SIDEBAR - 3, 0, SIDEBAR - 1, H], fill=GOOD)
    d.line([24, 112, SIDEBAR - 24, 112], fill=SIDE_LINE, width=1)
    d.line([24, 776, SIDEBAR - 24, 776], fill=SIDE_LINE, width=1)
    d.line([X0, 99, X1, 99], fill=HAIR, width=1)
    img.save(path, optimize=True)


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def main():
    shutil.rmtree(REPORT, ignore_errors=True)
    res = REPORT / "StaticResources" / "RegisteredResources"
    res.mkdir(parents=True)
    dump(res / "GroupFinance.json", THEME)
    background_png(res / "canvas.png")

    dump(PBI / "GroupFinance.pbip", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": "GroupFinance.Report"}}],
        "settings": {"enableAutoRecovery": True}})
    dump(REPORT / ".platform", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Report", "displayName": "GroupFinance"},
        "config": {"version": "2.0", "logicalId": "4e8c1a73-9b2d-4f56-a0e7-3d5b8c2f1a96"}})
    dump(REPORT / "definition.pbir", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": "../GroupFinance.SemanticModel"}}})
    d = REPORT / "definition"
    dump(d / "version.json", {"$schema": f"{SCHEMA}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"})
    dump(d / "report.json", {
        "$schema": f"{SCHEMA}/report/3.3.0/schema.json",
        "themeCollection": {"customTheme": {
            "name": "GroupFinance.json", "type": "RegisteredResources",
            "reportVersionAtImport": {"visual": "1.8.95", "report": "2.0.95", "page": "1.3.95"}}},
        "objects": {"outspacePane": [{"properties": {"expanded": lit(False)}}]},
        "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True,
                     "hideVisualContainerHeader": False, "useEnhancedTooltips": True,
                     "exportDataMode": "AllowSummarized"},
        "filterConfig": {"filters": [year_from_filter(2011)]},
        "resourcePackages": [{"name": "RegisteredResources", "type": "RegisteredResources", "items": [
            {"name": "GroupFinance.json", "path": "GroupFinance.json", "type": "CustomTheme"},
            {"name": "canvas.png", "path": "canvas.png", "type": "Image"}]}]})
    dump(d / "pages" / "pages.json", {"$schema": f"{SCHEMA}/pagesMetadata/1.1.0/schema.json",
                                      "pageOrder": [p[0] for p in PAGES + HIDDEN_PAGES],
                                      "activePageName": PAGES[0][0]})
    dump(d / "bookmarks" / "bookmarks.json", {"$schema": f"{SCHEMA}/bookmarksMetadata/1.0.0/schema.json",
                                              "items": [{"name": b[0]} for b in BOOKMARKS]})
    for b in BOOKMARKS:
        dump(d / "bookmarks" / f"{b[0]}.bookmark.json", bookmark_json(*b))

    total = 0
    for pid, name, title, subtitle, ptype in [(*p, None) for p in PAGES] + HIDDEN_PAGES:
        page = Page(pid, name, title, subtitle)
        if ptype != "Tooltip":
            scaffold(page)
        BUILDERS[pid](page)
        tab_order(page)
        dump(d / "pages" / pid / "page.json", page_json(pid, name, ptype, page))
        for v in page.visuals:
            dump(d / "pages" / pid / "visuals" / v["name"] / "visual.json", v)
        for vname, pos in page.mobile.items():
            assert any(v["name"] == vname for v in page.visuals), vname
            dump(d / "pages" / pid / "visuals" / vname / "mobile.json", mobile_json(pos))
        total += len(page.visuals)
        print(f"  ✓ page {name:<18} {len(page.visuals):>3} visuals")
    print(f"  ✓ wrote {REPORT.relative_to(ROOT)} ({len(PAGES) + len(HIDDEN_PAGES)} pages, {total} visuals, "
          f"{len(BOOKMARKS)} bookmarks)")

    dump(PBI / "deneb" / "ibcs_variance.json", DENEB_IBCS)
    print("  ✓ wrote powerbi/deneb/ibcs_variance.json (Deneb / Vega-Lite spec, not wired into the report)")

    result = subprocess.run(["pbir", "validate", str(REPORT), "--all"], capture_output=True, text=True,
                            env={**os.environ, "NO_COLOR": "1", "COLUMNS": "200"})
    print(result.stdout[-3000:], result.stderr[-2000:])


if __name__ == "__main__":
    main()
