"""Generate the Power BI report (PBIR) — 6 designed pages (plus drillthrough, tooltip and DAX notes) over the SalesTarget semantic model.

    uv run python src/build_report.py

Writes powerbi/SalesTarget.Report + SalesTarget.pbip, then runs `pbir validate`
(schema + field references against the model). Open SalesTarget.pbip in Power BI Desktop.

Visual calculations (RUNNINGSUM, RANK) live in the visuals, not in the model:
they only make sense on the visual's own axis, so they don't belong in the measure list.

Colour rule: green = on target, amber = at risk (90-99%), red = behind, blue = everything else.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
PBI = ROOT / "powerbi"
REPORT = PBI / "SalesTarget.Report"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"

W, H = 1600, 900
SIDEBAR = 232
X0, X1, GAP = 256, 1576, 16
CW = X1 - X0

# ---------------------------------------------------------------- palette
INK, INK2, MUTED, HAIR, SURFACE, PLANE = "#0b0b0b", "#52514e", "#6E6C66", "#E6E4DD", "#FCFCFB", "#F4F4F1"
SIDE, SIDE_TEXT, SIDE_LINE = "#14263a", "#a7b9cc", "#27405a"
# Text colours clear 4.5:1 on SURFACE (WCAG AA); TARGET_C is a line, not text, so it needs 3:1 (tests/test_report.py).
ON_C, RISK_C, BEHIND_C, NO_STATUS = "#1B7F4B", "#A35F00", "#B42318", "#6B7280"
ACCENT, TARGET_C = "#2a5d8f", "#7D8A97"

# Measures hosted outside _Measures (object-level security hides them with their table).
LOG_MEASURES = {"Source Rows", "Rows Loaded", "Rows Set Aside", "Amount Set Aside", "Load Rate"}


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
    kind = "Measure" if table == "_Measures" or name in LOG_MEASURES else "Column"
    return {kind: {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}}


def projection(ref):
    if isinstance(ref, dict):
        return ref
    return {"field": field(ref), "queryRef": ref, "nativeQueryRef": ref.split(".", 1)[1]}


def named(ref, label):
    """A projection renamed for this visual only (short column header)."""
    return {**projection(ref), "displayName": label}


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


def measure_filter(name, measure, kind=None, value=None):
    """Visual-level filter on a measure: 'is not blank' by default, else ComparisonKind (1 = greater than) value."""
    table, prop = measure.split(".", 1)
    left = {"Measure": {"Expression": {"SourceRef": {"Source": "m"}}, "Property": prop}}
    cond = ({"Not": {"Expression": {"Comparison": {"ComparisonKind": 0, "Left": left, "Right": {"Literal": {"Value": "null"}}}}}}
            if kind is None else {"Comparison": {"ComparisonKind": kind, "Left": left, "Right": {"Literal": {"Value": lit_value(value)}}}})
    return {"name": name, "field": field(measure), "type": "Advanced",
            "filter": {"Version": 2, "From": [{"Name": "m", "Entity": table, "Type": 0}], "Where": [{"Condition": cond}]}}


def heat(measure, color_measure):
    """Matrix / table cells filled with the hex code a colour measure returns."""
    return [{"properties": {"backColor": expr_color(color_measure)}, "selector": {**WILDCARD, "metadata": measure}}]


BULLET = "BulletChart1443347686880"  # Microsoft "Bullet Chart" custom visual on AppSource, v2.6.1.0
KIND = {"card": "KPI card", "clusteredBarChart": "Bar chart", "clusteredColumnChart": "Column chart",
        "lineChart": "Line chart", "pivotTable": "Matrix", "tableEx": "Table", "slicer": "Slicer",
        "waterfallChart": "Waterfall chart", "decompositionTreeVisual": "Decomposition tree",
        "keyDriversVisual": "Key influencers", "azureMap": "Map", BULLET: "Bullet chart"}


class Page:
    def __init__(self, pid, name, title, subtitle):
        self.id, self.name, self.title, self.subtitle = pid, name, title, subtitle
        self.visuals, self.interactions, self.mobile, self.filters = [], [], {}, []

    def group(self, name, display, x, y, w, h, hidden=False):
        """A visual group (selection pane folder); children pass group=name to add()."""
        g = {"$schema": f"{SCHEMA}/visualContainer/2.9.0/schema.json", "name": name,
             "position": {"x": x, "y": y, "z": len(self.visuals) * 100, "width": w, "height": h},
             "visualGroup": {"displayName": display, "groupMode": "ScaleMode"}}
        self.visuals.append({**g, "isHidden": True} if hidden else g)

    def add(self, name, vtype, x, y, w, h, roles=None, title=None, subtitle=None,
            objects=None, container=None, sort=None, filters=None, alt=None, hidden=False, group=None,
            **visual_extra):
        vc = dict(container or {})
        # Alt text for screen readers on every data visual: what it is + what it shows.
        if alt:
            vc.setdefault("general", props(altText=alt))
        elif roles:
            what = title or ", ".join(r.split(".", 1)[1] for refs in roles.values()
                                      for r in ([refs] if isinstance(refs, str) else refs) if isinstance(r, str))
            vc.setdefault("general", props(altText=f"{KIND.get(vtype, vtype)}: {what}"))
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
        if group:
            container_json["parentGroupName"] = group
        if hidden:
            container_json["isHidden"] = True
        self.visuals.append(container_json)

    def tab_order(self):
        """Keyboard order = reading order: sidebar first, then content top-to-bottom, left-to-right."""
        pos = lambda v: (v["position"]["x"] >= X0, v["position"]["y"], v["position"]["x"])
        for i, v in enumerate(sorted((v for v in self.visuals if "visual" in v), key=pos)):
            v["position"]["tabOrder"] = i * 100


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
    kw = {"syncGroup": {"groupName": sync, "fieldChanges": True, "filterChanges": True}} if sync else {}
    page.add(name, "slicer", x, y, w, h, roles={"Values": ref}, objects=objects, **kw, **extra)





def vcalc(name, expr, hidden=False):
    """A visual calculation: DAX evaluated on the visual's own matrix (rows / columns), not in the model."""
    p = {"field": {"NativeVisualCalculation": {"Language": "dax", "Expression": expr, "Name": name,
                                                "DataType": "Double"}},
         "queryRef": name, "nativeQueryRef": name}
    return {**p, "hidden": True} if hidden else p


# --------------------------------------------------------- page scaffolding
PAGES = [
    ("overview", "Overview", "Sales vs target",
     "Reseller sales against the quarterly quota, Dec 2010 – Nov 2013. Stretch the target with the slider."),
    ("reps", "Reps", "Reps",
     "Who is on target, who is at risk, and how each manager's team adds up."),
    ("time", "Time & Fiscal", "Time & fiscal year",
     "Month by month, year to date on the July–June fiscal year, and against the year before."),
    ("products", "Products & Resellers", "Products & resellers",
     "What sells and to whom. Targets are set per rep, so there is no target at this level."),
    ("resellers", "Resellers", "Resellers",
     "Which resellers carry the sales, which are slipping away, and what each rep should do about them."),
    ("quality", "Data Quality", "Data quality",
     "What Power Query read from the branch files, and which rows it set aside and why."),
    ("why", "Why", "Why is it behind?",
     "Split the gap to target until it points at someone, then ask which resellers are buying less."),
]

ROW_KPI_Y, ROW_KPI_H = 104, 96
ROW2_Y, ROW2_H = 216, 332
ROW3_Y, ROW3_H = 564, 316
ROW4_Y, ROW4_H = 896, 316
ROW5_Y, ROW5_H = 1228, 316
TALL = ROW4_Y + ROW4_H + 20  # pages with a fourth row scroll: 1600 × 1232
TALLER = ROW5_Y + ROW5_H + 20  # and a fifth: 1600 × 1564
PAGE_H = {"overview": TALL, "reps": TALL, "time": TALL, "products": TALLER, "resellers": TALL, "quality": TALL,
          "why": TALL, "rep_detail": TALL}
WALLPAPER = {H: "canvas.png", TALL: "canvas-tall.png", TALLER: "canvas-taller.png"}

# Bookmarks: (name, display name, page, options, visual containers state, group state)
BOOKMARKS = []


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
            ("ADVENTURE WORKS", 8, SIDE_TEXT, True), ("Sales vs Target", 17, "#FFFFFF", True))
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
                            "background": props(show=False), "border": props(show=False)})
    page.add("button_reset", "actionButton", 12, 720, 208, 36,
             objects={"text": props(show=True, text="↺  Reset filters", fontColor=color(SIDE_TEXT), fontSize=10.0,
                                    horizontalAlignment="left", leftMargin=16),
                      "fill": props(show=False), "outline": props(show=True, lineColor=color(SIDE_LINE)),
                      "icon": props(show=False), "shape": props(tileShape="rectangleRounded", rectangleRoundedCurve=8)},
             container={"visualLink": props(show=True, type="Bookmark", bookmark="bm_reset_filters"),
                        "background": props(show=False), "border": props(show=False)},
             alt="Button: clear the fiscal year, region, category and business type slicers on every page")
    textbox(page, "footer", 24, 790, 190, 90,
            ("Source: AdventureWorks DW reseller sales & quotas (Microsoft sample, MIT)", 8, SIDE_TEXT, False),
            ("Pipeline: messy Excel · Power Query (M) · Power BI (TMDL + PBIR)", 8, SIDE_TEXT, False))

    textbox(page, "title", X0, 22, 500, 70, (page.title, 20, INK, True), (page.subtitle, 10, INK2, False))
    sx = cols(500, 176, 200, 200, 180)
    slicer(page, "slicer_fy", sx[1], 16, 176, 76, "Date.Fiscal Year", single=False, sync="FY")
    slicer(page, "slicer_region", sx[2], 16, 200, 76, "Territories.Region", single=False, sync="Region")
    slicer(page, "slicer_category", sx[3], 16, 200, 76, "Products.Category", single=False, sync="Category")
    slicer(page, "slicer_btype", sx[4], 16, 180, 76, "Resellers.Business Type", single=False, sync="BType")


def hierarchy(table, name, levels, active=None):
    """Category projections for a drillable hierarchy; `active` levels are the ones on show."""
    return [{"field": {"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {"SourceRef": {"Entity": table}},
                                                                        "Hierarchy": name}}, "Level": lv}},
             "queryRef": f"{table}.{name}.{lv}", "nativeQueryRef": lv,
             **({"active": True} if lv in (active or levels[:1]) else {})} for lv in levels]


def kpis(page, measures):
    w = (CW - GAP * (len(measures) - 1)) // len(measures)
    for i, (m, units, precision, *size) in enumerate(measures):
        page.add(f"kpi_{i}", "card", X0 + i * (w + GAP), ROW_KPI_Y, w, ROW_KPI_H, roles={"Values": m if "." in m else f"_Measures.{m}"},
                 objects={"labels": props(color=color(INK), fontSize=float(size[0] if size else 22), fontFamily="Segoe UI Semibold",
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
    # No word wrap: a header or a negative number never breaks onto a second line.
    page.add(name, "tableEx", x, y, w, h, roles={"Values": values}, title=title, subtitle=subtitle, sort=sort,
             objects={"grid": props(rowPadding=6), "columnHeaders": props(wordWrap=False),
                      "values": props(wordWrap=False)}, **kw)


def bar(page, name, vtype, x, y, w, h, category, measure, hex_, title, subtitle=None, units=K, sort=None, **kw):
    page.add(name, vtype, x, y, w, h, roles={"Category": category, "Y": measure}, title=title, subtitle=subtitle,
             sort=sort, objects={**axes(show_y=vtype == "clusteredColumnChart", grid=False),
                                 "labels": bar_labels(units), "dataPoint": fill(hex_)}, **kw)


def analyze_slicer(page, x, y, w):
    page.add("slicer_analyze", "slicer", x, y, w, 44, roles={"Values": "Analyze By.Analyze By"},
             objects={"data": props(mode="HorizontalList"), "header": props(show=False),
                      "selection": props(singleSelect=True, strictSingleSelect=True),
                      "items": props(fontColor=color(INK2), textSize=9.0),
                      "general": [{"properties": {"filter": {"filter": in_filter("Analyze By.Analyze By", ["Region"])}}}]},
             container={"background": props(show=False), "border": props(show=False)})




def button(page, name, x, y, w, h, text, bookmark, alt, group=None, primary=False):
    """A small rounded button that applies a bookmark."""
    page.add(name, "actionButton", x, y, w, h, group=group, alt=alt,
             objects={"text": props(show=True, text=text, fontColor=color("#FFFFFF" if primary else INK2), fontSize=9.0,
                                    fontFamily="Segoe UI Semibold"),
                      "fill": props(show=True, fillColor=color(ACCENT if primary else SURFACE), transparency=0.0),
                      "outline": props(show=True, lineColor=color(ACCENT if primary else HAIR)),
                      "icon": props(show=False), "shape": props(tileShape="rectangleRounded", rectangleRoundedCurve=6)},
             container={"visualLink": props(show=True, type="Bookmark", bookmark=bookmark),
                        "background": props(show=False), "border": props(show=False)})


def bookmark(name, display, page, visuals=None, groups=None, targets=None, data=False):
    """Display-only bookmark (suppressData) that shows / hides the listed visuals or groups on one page."""
    BOOKMARKS.append({"name": name, "displayName": display,
                      "options": {"applyOnlyToTargetVisuals": True, "targetVisualNames": targets,
                                  "suppressData": not data, "suppressActiveSection": True},
                      "explorationState": {"version": "1.3", "activeSection": page, "sections": {page: {
                          "visualContainers": {v: {"singleVisual": {"visualType": t, "objects": {},
                                                                    **({"display": {"mode": "hidden"}} if hide else {})}}
                                               for v, (t, hide) in (visuals or {}).items()},
                          **({"visualContainerGroups": {g: {"isHidden": hide} for g, hide in groups.items()}}
                             if groups else {})}}}})


# ------------------------------------------------------------------- pages
def ref_line(i, value, name, hex_, measure=None):
    """One analytics-pane constant line; value is a number or a measure (fx)."""
    v = {"expr": field(measure)} if measure else lit(value)
    return {"properties": {"show": lit(True), "displayName": lit(name), "value": v, "lineColor": color(hex_),
                           "style": lit("dashed"), "width": lit(1.0), "dataLabelShow": lit(True),
                           "dataLabelText": lit("Name"), "dataLabelColor": color(hex_)},
            "selector": {"id": i}}


def status_fill(measure="_Measures.Status Color"):
    """Conditional colour: every data point takes the hex code the Status Color measure returns."""
    return [{"properties": {"fill": expr_color(measure)}, "selector": WILDCARD}]


def overview(p):
    kpis(p, [("Net Sales", M, 1), ("Target", M, 1), ("Attainment", NONE, 1), ("Gap to Target", M, 1),
             ("Attainment FYTD", NONE, 1), ("Reps On Target", NONE, 0)])
    a, b = cols(880, 424)
    series = ["Cumulative Sales", "Cumulative Target"]
    p.add("line_cumulative", "lineChart", a, ROW2_Y, 880, ROW2_H,
          roles={"Category": "Date.Year Month",
                 "Y": ["_Measures.Net Sales", "_Measures.Target"]},
          title="Cumulative sales vs cumulative target",
          subtitle="Visual calculations: RUNNINGSUM over the months on the axis",
          objects={**axes(), "legend": legend(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color(s, c) for s, c in zip(series, [ACCENT, TARGET_C])]})
    p.visuals[-1]["visual"]["query"]["queryState"]["Y"]["projections"] = [
        {**projection("_Measures.Net Sales"), "hidden": True}, {**projection("_Measures.Target"), "hidden": True},
        vcalc("Cumulative Sales", "RUNNINGSUM ( [Net Sales] )"),
        vcalc("Cumulative Target", "RUNNINGSUM ( [Target] )")]
    # Chart ↔ table: the same months as a table, hidden until the Table bookmark shows it.
    p.add("table_monthly", "tableEx", a, ROW2_Y, 880, ROW2_H, hidden=True,
          roles={"Values": ["Date.Month", "_Measures.Net Sales", "_Measures.Target", "_Measures.Attainment",
                            "_Measures.Status Label"]},
          title="Sales vs target by month", subtitle="Table view of the chart (Chart / Table buttons)",
          sort=("Date.Month", "Ascending"), objects={"grid": props(rowPadding=5)})
    p.group("grp_view_toggle", "Chart / Table buttons", a + 880 - 14 - 144 - 6, ROW2_Y + 10, 150, 26)
    button(p, "button_chart", a + 880 - 14 - 144 - 6, ROW2_Y + 10, 72, 26, "Chart", "bm_view_chart",
           "Button: show the cumulative chart", group="grp_view_toggle", primary=True)
    button(p, "button_table", a + 880 - 14 - 72, ROW2_Y + 10, 72, 26, "Table", "bm_view_table",
           "Button: show the same numbers as a table", group="grp_view_toggle")
    views = {"line_cumulative": "lineChart", "table_monthly": "tableEx"}
    bookmark("bm_view_chart", "View: chart", "overview", {v: (t, v != "line_cumulative") for v, t in views.items()},
             targets=list(views))
    bookmark("bm_view_table", "View: table", "overview", {v: (t, v != "table_monthly") for v, t in views.items()},
             targets=list(views))
    p.add("card_headline", "card", b, ROW2_Y, 424, 220, roles={"Values": "_Measures.Headline"},
          title="In one sentence",
          objects={"labels": props(color=color(INK), fontSize=14.0, fontFamily="Segoe UI"),
                   "categoryLabels": props(show=False), "wordWrap": props(show=True)})
    slicer(p, "slicer_stretch", b, ROW2_Y + 236, 424, 96, "Target Stretch.Target Stretch", default=0.0,
           title="What-if: stretch the target", subtitle="Quota × (1 + stretch)")
    # Info button → a help panel (visual group, hidden by default) explaining the status colours and symbols.
    button(p, "button_help", b + 424 - 14 - 110, ROW2_Y + 10, 110, 26, "ⓘ  Read status", "bm_help_show",
           "Button: explain the status colours and symbols")
    p.group("grp_status_help", "Status help panel", b, ROW2_Y, 424, ROW2_H, hidden=True)
    textbox(p, "help_text", b, ROW2_Y, 424, ROW2_H,
            ("How to read the status", 12, INK, True),
            ("▲ On target — attainment 100% or more (green).", 10, ON_C, True),
            ("● At risk — 90% to 99% (amber).", 10, RISK_C, True),
            ("▼ Behind — below 90% (red).", 10, BEHIND_C, True),
            ("Attainment = net sales ÷ target. The target is the rep's quota up to the last sale date, "
             "times 1 + the stretch you pick. Tables spell the status out, so colour is never the only cue.",
             9, INK2, False))
    p.visuals[-1]["parentGroupName"] = "grp_status_help"
    p.visuals[-1]["visual"]["visualContainerObjects"].update(
        background=props(show=True, color=color("#FFFFFF")), border=props(show=True, color=color(ACCENT), radius=12.0),
        padding=props(top=16, bottom=12, left=18, right=18))
    button(p, "button_help_close", b + 424 - 14 - 72, ROW2_Y + ROW2_H - 40, 72, 26, "Close", "bm_help_hide",
           "Button: close the status help", group="grp_status_help", primary=True)
    for name, hide in (("bm_help_show", False), ("bm_help_hide", True)):
        bookmark(name, f"Status help: {'hide' if hide else 'show'}", "overview",
                 groups={"grp_status_help": hide}, targets=["grp_status_help"])
    a, b = cols(652, 652)
    analyze_slicer(p, a, ROW3_Y, 652)
    p.add("slicer_metric", "slicer", b, ROW3_Y, 652, 44, roles={"Values": "Metric.Metric"},
          objects={"data": props(mode="HorizontalList"), "header": props(show=False),
                   "selection": props(singleSelect=True, strictSingleSelect=True),
                   "items": props(fontColor=color(INK2), textSize=9.0),
                   "general": [{"properties": {"filter": {"filter": in_filter("Metric.Metric", ["Net Sales"])}}}]},
          container={"background": props(show=False), "border": props(show=False)})
    y, h = ROW3_Y + 52, ROW3_H - 52
    p.add("bar_sales_by", "clusteredBarChart", a, y, 652, h,
          roles={"Category": "Analyze By.Analyze By", "Y": "_Measures.Net Sales",
                 "Tooltips": "_Measures.Share of Visible Total"},
          title="The metric picked above, by the dimension picked above",
          subtitle="Two field parameters: Metric switches the measure, Analyze By the axis",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(M), "dataPoint": fill(ACCENT)})
    # A field parameter of measures is saved the way Desktop saves it: the projection holds the measure the
    # parameter currently resolves to (Net Sales, the slicer default) and fieldParameters points at the parameter.
    p.visuals[-1]["visual"]["query"]["queryState"]["Y"]["fieldParameters"] = [
        {"parameterExpr": field("Metric.Metric"), "index": 0, "length": 1}]
    p.add("bar_attain_by", "clusteredBarChart", b, y, 652, h,
          roles={"Category": "Analyze By.Analyze By", "Y": "_Measures.Attainment"},
          title="Attainment by the same dimension", subtitle="Blank where no target applies (product, reseller)",
          sort=("_Measures.Attainment", "Descending"),
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(NONE), "dataPoint": status_fill()})
    p.add("waterfall_gap", "waterfallChart", X0, ROW4_Y, CW, ROW4_H,
          roles={"Category": "Territories.Region", "Y": "_Measures.Gap to Target"},
          title="Where the gap to target comes from, region by region",
          subtitle="Each bar adds one region's sales − target; the last bar is the total gap. Targets follow the rep's home region; managers have none, so their quota shows as (Blank)",
          sort=("_Measures.Gap to Target", "Descending"),
          objects={**axes(grid=True), "labels": bar_labels(M),
                   "sentimentColors": props(increaseFill=color(ON_C), decreaseFill=color(BEHIND_C),
                                            totalFill=color(ACCENT))})
    # Edit interactions: picking a bar in "Attainment by" highlights the metric bars instead of filtering them
    # (line charts can't highlight, so the cumulative line filters).
    p.interactions += [{"source": "bar_attain_by", "target": "bar_sales_by", "type": "HighlightFilter"},
                       {"source": "bar_attain_by", "target": "line_cumulative", "type": "DataFilter"}]
    p.mobile = phone(["slicer_fy"], ["kpi_2", "kpi_3", "kpi_5"], ["line_cumulative", "bar_attain_by"], [])


def phone(slicers, cards, charts, tables):
    """Phone layout, read top to bottom: Fiscal year slicer, KPI cards two a row, then charts, then tables."""
    out, y = {}, 0
    for v in slicers:
        out[v], y = (0, y, 360, 72), y + 80
    for i, v in enumerate(cards):
        out[v] = ((i % 2) * 184, y + (i // 2) * 104, 176, 96)
    y += -(-len(cards) // 2) * 104
    for v in charts:
        out[v], y = (0, y, 360, 300), y + 308
    for v in tables:
        out[v], y = (0, y, 360, 420), y + 428
    return out


def reps(p):
    kpis(p, [("Reps With Target", NONE, 0), ("Reps On Target", NONE, 0), ("Attainment", NONE, 1),
             ("Gap to Target", M, 1), ("Sales Share", NONE, 1), ("Sales from At-Risk Reps", M, 2)])
    a, b = cols(640, 664)
    p.add("bar_attain_rep", "clusteredBarChart", a, ROW2_Y, 640, ROW2_H,
          roles={"Category": "Reps.Rep", "Y": "_Measures.Attainment"},
          title="Attainment by rep", subtitle="Green ≥ 100% · amber 90–99% · red below 90%",
          sort=("_Measures.Attainment", "Descending"),
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(NONE), "dataPoint": status_fill(),
                   # Analytics pane: constant lines at the two status thresholds (value axis = x on a bar chart).
                   "xAxisReferenceLine": [ref_line("1", 1.0, "Target 100%", ON_C), ref_line("2", 0.9, "At risk 90%", RISK_C)]},
          container={"visualTooltip": props(show=True, type="Canvas", section=TIP)})
    p.add("matrix_org", "pivotTable", b, ROW2_Y, 664, ROW2_H,
          roles={"Rows": ["Reps.Org Level 1", "Reps.Org Level 2"],
                 "Values": ["_Measures.Net Sales", "_Measures.Target", "_Measures.Attainment"]},
          title="By manager", subtitle="Org levels come from PATH() over the manager key; RLS uses the same path",
          objects={"grid": props(rowPadding=5)})
    p.add("table_scorecard", "tableEx", X0, ROW3_Y, CW, ROW3_H,
          roles={"Values": ["Reps.Rep", "Reps.Title", "Territories.Region", "_Measures.Net Sales", "_Measures.Target",
                            "_Measures.Gap to Target", "_Measures.Attainment", "_Measures.Status Label",
                            "_Measures.Attainment Prev FQ", "_Measures.Rep Share of Team", "_Measures.Top Rep in Region"]},
          title="Rep scorecard",
          subtitle="Rank is a visual calculation; previous fiscal quarter via OFFSET, team share via ALLEXCEPT",
          sort=("_Measures.Attainment", "Descending"),
          objects={"grid": props(rowPadding=6),
                   "values": [{"properties": {"fontColor": expr_color("_Measures.Status Color")},
                               "selector": {**WILDCARD, "metadata": "_Measures.Status Label"}}]})
    p.visuals[-1]["visual"]["query"]["queryState"]["Values"]["projections"].insert(
        0, vcalc("Rank", "RANK ( DENSE, ORDERBY ( [Attainment], DESC ) )"))
    # Custom visual from AppSource (publicCustomVisuals in report.json): actual vs target with 90% / 100% bands.
    p.add("bullet_reps", BULLET, X0, ROW4_Y, CW, ROW4_H,
          roles={"Category": "Reps.Rep", "Value": "_Measures.Net Sales", "TargetValue": "_Measures.Target",
                 "Satisfactory": "_Measures.Target at 90%", "Good": "_Measures.Target"},
          title="Sales against quota, rep by rep",
          subtitle="Bullet Chart custom visual (Microsoft, AppSource) · bar = net sales, marker = target, bands at 90% and 100%",
          sort=("_Measures.Net Sales", "Descending"))
    # Picking a manager in the matrix filters the rep bars, but not the scorecard, so its ranking stays whole.
    p.interactions += [{"source": "matrix_org", "target": "bar_attain_rep", "type": "DataFilter"},
                       {"source": "matrix_org", "target": "table_scorecard", "type": "NoFilter"}]
    p.mobile = phone(["slicer_fy"], ["kpi_2", "kpi_3", "kpi_1"], ["bar_attain_rep"], ["table_scorecard"])


def time(p):
    kpis(p, [("Sales FYTD", M, 1), ("Target FYTD", M, 1), ("Attainment FYTD", NONE, 1), ("Sales FYTD PY", M, 1),
             ("Sales YoY %", NONE, 1), ("Net Sales by Ship Date", M, 1), ("Avg Days to Ship", NONE, 1),
             ("Active Reps (End of Period)", NONE, 0)])
    a, b = cols(880, 424)
    p.add("line_monthly", "lineChart", a, ROW2_Y, 880, ROW2_H,
          roles={"Category": "Date.Month Start",
                 "Y": ["_Measures.Net Sales", "_Measures.Sales 3M Avg", "_Measures.Sales Linear Trend"],
                 "Y2": "_Measures.Attainment R12M"},
          title="Net sales per month, 3-month average and rolling 12-month attainment",
          subtitle="3M average: WINDOW · R12M: DATESINPERIOD, right axis · dashed: monthly average · "
                   "Sales Linear Trend: LINESTX, runs on to the last quota month",
          objects={**axes(), "legend": legend(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "categoryAxis": props(show=True, showAxisTitle=False, fontSize=9.0, labelColor=color(MUTED),
                                         axisType="Scalar"),
                   "y1AxisReferenceLine": [ref_line("1", None, "Monthly average", INK2, "_Measures.Avg Monthly Sales")],
                   "trend": [{"properties": {"show": lit(True), "combineSeries": lit(False), "style": lit("dotted"),
                                             "displayName": lit("Trend")}}],
                   "dataPoint": [measure_color("_Measures.Net Sales", TARGET_C),
                                 measure_color("_Measures.Sales 3M Avg", ACCENT),
                                 measure_color("_Measures.Sales Linear Trend", INK2),
                                 measure_color("_Measures.Attainment R12M", RISK_C)]})
    p.add("col_fy_attain", "clusteredColumnChart", b, ROW2_Y, 424, ROW2_H,
          roles={"Category": hierarchy("Date", "Fiscal", ["Fiscal Year", "Fiscal Quarter", "Month"]),
                 "Y": "_Measures.Attainment"},
          title="Attainment by fiscal year", subtitle="Fiscal year runs July to June · drill down to quarter and month",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(NONE), "dataPoint": status_fill()})
    p.add("matrix_periods", "pivotTable", X0, ROW3_Y, CW, ROW3_H,
          roles={"Rows": ["Date.Fiscal Year", "Date.Fiscal Quarter"], "Columns": "Time Intelligence.Period",
                 "Values": "_Measures.Net Sales"},
          title="One measure, eleven views",
          subtitle="Columns come from the Time Intelligence calculation group; YoY % switches its own format string",
          objects={"grid": props(rowPadding=5)})
    # Built-in forecast needs one measure on a continuous date axis, so it gets its own chart.
    a, b = cols(880, 424)
    p.add("line_forecast", "lineChart", a, ROW4_Y, 880, ROW4_H,
          roles={"Category": "Date.Month Start", "Y": "_Measures.Net Sales"},
          title="Net sales per month with the built-in forecast",
          subtitle="Analytics pane: forecast (exponential smoothing, computed by Power BI when the visual renders) + trend line",
          objects={**axes(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "categoryAxis": props(show=True, showAxisTitle=False, fontSize=9.0, labelColor=color(MUTED),
                                         axisType="Scalar"),
                   "dataPoint": [measure_color("_Measures.Net Sales", ACCENT)],
                   "forecast": [{"properties": {"show": lit(True), "displayName": lit("Forecast"),
                                                "lineColor": color(RISK_C), "bandAreaShow": lit(True)}}],
                   "trend": [{"properties": {"show": lit(True), "style": lit("dotted"), "displayName": lit("Trend")}}]})
    p.add("col_new_returning", "columnChart", b, ROW4_Y, 424, ROW4_H,
          roles={"Category": "Date.Fiscal Year", "Y": ["_Measures.New Resellers", "_Measures.Returning Resellers"]},
          title="New vs returning resellers", subtitle="New = first order ever in that fiscal year (ALLEXCEPT on the reseller)",
          objects={**axes(show_y=False, grid=False), "legend": legend(), "labels": bar_labels(NONE),
                   "dataPoint": [measure_color("_Measures.New Resellers", ON_C),
                                 measure_color("_Measures.Returning Resellers", ACCENT)]})


def products(p):
    kpis(p, [("Net Sales", M, 1), ("Units", K, 1), ("Orders", NONE, 0), ("Avg Order Value", K, 1),
             ("Sales YoY %", NONE, 1), ("Active Resellers", NONE, 0), ("Gross Margin %", NONE, 1)])
    a, b = cols(652, 652)
    bar(p, "bar_category", "clusteredBarChart", a, ROW2_Y, 652, ROW2_H,
        hierarchy("Products", "Product Hierarchy", ["Category", "Subcategory", "Product"], active=["Subcategory"]),
        "_Measures.Net Sales", ACCENT, "Net sales by subcategory", "Drill up to category or down to product",
        units=M, sort=("_Measures.Net Sales", "Descending"))
    bar(p, "bar_btype", "clusteredBarChart", b, ROW2_Y, 652, ROW2_H, "Resellers.Business Type", "_Measures.Net Sales",
        ACCENT, "Net sales by reseller type", units=M, sort=("_Measures.Net Sales", "Descending"))
    a, b = cols(880, 424)
    table(p, "table_products", a, ROW3_Y, 880, ROW3_H,
          ["Products.Product", "Products.Subcategory", "Products.Category", "_Measures.Net Sales", "_Measures.Units",
           "_Measures.Sales PY", "_Measures.Sales YoY %", "_Measures.Gross Margin", "_Measures.Gross Margin %"],
          "Products, largest first", "Prior year: DAX UDF · gross margin: SUMX with RELATED standard cost",
          sort=("_Measures.Net Sales", "Descending"))
    table(p, "table_category_plan", b, ROW3_Y, 424, ROW3_H,
          ["Products.Category", "_Measures.Net Sales", "_Measures.Category Target", "_Measures.Category Attainment"],
          "Category plan (synthetic)", "Quota split by last year's category mix; many-to-many to Products",
          sort=("_Measures.Net Sales", "Descending"))
    p.add("map_resellers", "azureMap", X0, ROW4_Y, CW, ROW4_H,
          roles={"Category": "Resellers.Location", "Size": "_Measures.Net Sales", "Series": "Territories.Region",
                 "Tooltips": ["_Measures.Orders", "_Measures.Active Resellers"]},
          title="Where the sales come from",
          subtitle="Azure Maps · one bubble per reseller city (\"City, Country\", data category Place), size = net sales, "
                   "colour = sales region",
          objects={"bubbleLayer": props(show=True), "legend": props(show=True, position="Right")})
    a, b = cols(880, 424)
    p.add("combo_pareto", "lineClusteredColumnComboChart", a, ROW5_Y, 880, ROW5_H,
          roles={"Category": "Products.Product", "Y": "_Measures.Net Sales", "Y2": "_Measures.Product Cum Share"},
          sort=("_Measures.Net Sales", "Descending"), alt="Pareto chart: net sales by product with the cumulative share",
          container={"title": [{"properties": {"show": lit(True), "text": {"expr": field("_Measures.Pareto Text")}}}],
                     "subTitle": props(show=True, text="Bars: net sales per product, largest first · "
                                                        "line: cumulative share (right axis), read off where it crosses 80%")},
          objects={**axes(show_x=False), "labels": props(show=False), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color("_Measures.Net Sales", ACCENT),
                                 measure_color("_Measures.Product Cum Share", RISK_C)]})
    p.add("col_size_bands", "clusteredColumnChart", b, ROW5_Y, 424, ROW5_H,
          roles={"Category": "Reseller Size Bands.Band", "Y": "_Measures.Resellers in Band",
                 "Tooltips": "_Measures.Sales in Band"},
          title="Resellers by size", subtitle="Disconnected band table · pick a fiscal year to band that year's sales",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(NONE), "dataPoint": fill(ACCENT)})


def resellers(p):
    # New / returning count the date selection, else the latest fiscal quarter; the text card says which.
    kpis(p, [("Period Counted", NONE, 0, 12), ("Resellers in Period", NONE, 0), ("New Resellers", NONE, 0),
             ("Returning Resellers", NONE, 0), ("At-Risk Resellers", NONE, 0), ("Churned Resellers", NONE, 0),
             ("Annual Churn Rate", NONE, 1), ("Reseller CLV on Sales", K, 0)])
    a, b = cols(880, 424)
    table(p, "table_abc", a, ROW2_Y, 880, ROW2_H,
          ["Resellers.Reseller", "Resellers.Business Type", "_Measures.Sales in Class", "_Measures.Reseller Cum Share",
           "_Measures.Reseller ABC Class", "Resellers.RFM Segment", "_Measures.Churn Status"],
          "Resellers by ABC class, largest first",
          "Class follows the slicers: A = first 70% of net sales, B = next 20%, C = last 10% · RFM segment as of the last sale",
          sort=("_Measures.Sales in Class", "Descending"),
          filters=[measure_filter("abc_selected", "_Measures.Sales in Class")])
    slicer(p, "slicer_abc", b, ROW2_Y, 424, 64, "ABC Class.Class", mode="HorizontalList", single=False)
    p.add("col_abc", "clusteredColumnChart", b, ROW2_Y + 72, 424, ROW2_H - 72,
          roles={"Category": "ABC Class.Class", "Y": "_Measures.Sales in Class", "Tooltips": "_Measures.Resellers in Class"},
          title="Net sales by ABC class", subtitle="Few A resellers, most of the sales",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(M), "dataPoint": fill(ACCENT)})
    p.add("matrix_cohort", "pivotTable", a, ROW3_Y, 880, ROW3_H,
          roles={"Rows": "Reseller Cohort.Cohort", "Columns": "Reseller Cohort.Quarter N",
                 "Values": "_Measures.Cohort Retention %"},
          title="Cohort retention: share of each first-order quarter still ordering N quarters later",
          subtitle="Calculated table Reseller Cohort (fiscal quarters, static) · darker = more of the cohort ordered",
          objects={"grid": props(rowPadding=4), "values": heat("_Measures.Cohort Retention %", "_Measures.Retention Color"),
                   "subTotals": props(rowSubtotals=False, columnSubtotals=False)})
    table(p, "table_clv", b, ROW3_Y, 424, ROW3_H,
          [named("Resellers.Business Type", "Type"), named("_Measures.Annual Churn Rate", "Churn"),
           named("_Measures.Avg Reseller Lifetime", "Life (yr)"), named("_Measures.Sales per Reseller FY2013", "Sales/res."),
           named("_Measures.Reseller CLV on Sales", "CLV sales"), named("_Measures.Reseller CLV", "CLV GM")],
          "Lifetime value by reseller type",
          "Sales/res. = FY2013 net sales per reseller · Life = 1 ÷ churn FY2012→FY2013 · CLV GM is negative: "
          "standard cost exceeds the reseller price in this sample")
    c, d = cols(652, 652)
    p.add("matrix_transition", "pivotTable", c, ROW4_Y, 652, ROW4_H,
          roles={"Rows": "RFM Transition.From Segment", "Columns": "RFM Transition.To Segment",
                 "Values": "_Measures.Transition Share"},
          title="Where FY2012's segments ended FY2013",
          subtitle="Rows: RFM segment on 30 Jun 2012 · columns: on 30 Jun 2013 · share of the row",
          objects={"grid": props(rowPadding=4), "values": heat("_Measures.Transition Share", "_Measures.Transition Color"),
                   "subTotals": props(rowSubtotals=False, columnSubtotals=False)})
    table(p, "table_basket", d, ROW4_Y, 652, ROW4_H,
          ["Subcategory Pairs.Subcategory A", "Subcategory Pairs.Subcategory B", "Subcategory Pairs.Orders Together",
           "Subcategory Pairs.Support", "Subcategory Pairs.Confidence A to B", "Subcategory Pairs.Confidence B to A",
           "Subcategory Pairs.Lift"],
          "Bought together: subcategory pairs in one order",
          "Lift above 1 = bought together more often than chance · pairs in at least 30 orders",
          sort=("Subcategory Pairs.Lift", "Descending"))


def quality(p):
    kpis(p, [("Cleaning Log.Source Rows", NONE, 0), ("Cleaning Log.Rows Loaded", NONE, 0),
             ("Cleaning Log.Rows Set Aside", NONE, 0), ("Cleaning Log.Amount Set Aside", K, 1),
             ("Cleaning Log.Load Rate", NONE, 2), ("Order P95", K, 0), ("Order P99", K, 0)])
    a, b = cols(760, 544)
    p.add("col_branch", "clusteredColumnChart", a, ROW2_Y, 760, ROW2_H,
          roles={"Category": "Cleaning Log.Outcome", "Y": "Cleaning Log.Source Rows"},
          title="Rows by outcome", subtitle="Every row read is either loaded or set aside with a reason",
          sort=("Cleaning Log.Source Rows", "Descending"),
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(NONE), "dataPoint": fill(ACCENT)})
    textbox(p, "cleaning_note", b + 8, ROW2_Y + 8, 528, ROW2_H - 16,
            ("What Power Query does with the three branch formats", 11, INK, True),
            ("North America sends a workbook per year with a sheet per month, a title block, a total row and the "
             "odd pasted duplicate sheet. Europe sends a workbook per quarter in German number and date formats, "
             "with reps as \"LASTNAME, First\". Pacific sends a system export with order and line in one cell, "
             "Excel serial dates and Windows logins.", 9, INK2, False),
            ("One reader function per format maps all three to one schema. Text numbers are converted, reps resolve "
             "through an alias list, lines sent twice are kept once, and every row that fails a rule is counted "
             "here with its reason instead of being dropped silently.", 9, INK2, False),
            ("Sales managers cannot see this page's data: object-level security hides the Cleaning Log.",
             9, INK2, False))
    # Page-level filter the reader can change (filters pane), next to the locked report-level one.
    p.filters = [{"name": "page_outcome", "displayName": "Outcome", "ordinal": 0, "field": field("Cleaning Log.Outcome"),
                  "type": "Categorical", "howCreated": "User"}]
    table(p, "table_log", X0, ROW3_Y, CW, ROW3_H,
          ["Cleaning Log.Branch", "Cleaning Log.File", "Cleaning Log.Outcome", "Cleaning Log.Source Rows",
           "Cleaning Log.Amount Set Aside"],
          "Cleaning log by file", sort=("Cleaning Log.Source Rows", "Descending"))
    table(p, "table_outliers", X0, ROW4_Y, CW, ROW4_H,
          ["Sales.Order Number", "Sales.Order Date", "Resellers.Reseller", "Sales.Branch", "_Measures.Net Sales",
           "_Measures.Order Amount Z", "_Measures.Order Outlier"],
          "Unusual orders: above the 95th percentile of order value, P99 flagged",
          "Order Amount Z = (order − mean) ÷ standard deviation · P95 / P99 with PERCENTILEX.INC over every order "
          "in the selection · check these against the branch file before trusting them",
          sort=("_Measures.Order Amount Z", "Descending"),
          filters=[measure_filter("outliers_only", "_Measures.Order Outlier")])


def why(p):
    kpis(p, [("Gap to Target", M, 1), ("Attainment", NONE, 1), ("Reps On Target", NONE, 0),
             ("Sales from At-Risk Reps", M, 2)])
    p.add("decomp_gap", "decompositionTreeVisual", X0, ROW2_Y, CW, ROW2_H,
          roles={"Analyze": "_Measures.Gap to Target",
                 "ExplainBy": ["Territories.Region", "Reps.Rep", "Products.Category"]},
          title="Decomposition tree: gap to target",
          subtitle="Split by region, then rep. Targets are per rep, so a split by category below a rep compares "
                   "category sales with the rep's whole target: use it for sales mix, not for the gap",
          objects={"tree": props(barsPerLevel=10)})
    a, b = cols(880, 424)
    p.add("ki_trend", "keyDriversVisual", a, ROW3_Y, 880, ROW3_H,
          roles={"Target": "Resellers.Sales Trend", "ExplainBy": ["Resellers.Business Type", "Resellers.Country"]},
          title="Key influencers: which resellers buy less than last year",
          subtitle="Target: Resellers[Sales Trend] (FY2013 vs FY2012) = Declining · explained by business type and country")
    textbox(p, "why_steps", b + 8, ROW3_Y + 8, 408, ROW3_H - 16,
            ("How to diagnose a miss", 11, INK, True),
            ("1. Decomposition tree: split the gap by region, then by rep, and follow the biggest red bar.", 9, INK2, False),
            ("2. Key influencers: are declining resellers concentrated in one business type or country?", 9, INK2, False),
            ("3. Time & Fiscal: is it a dip, a trend, or a quarter that never had sales?", 9, INK2, False),
            ("4. Right-click the rep → Drill through → Rep detail for the months and the resellers.", 9, INK2, False))
    a, b = cols(880, 424)
    p.add("line_anomaly", "lineChart", a, ROW4_Y, 880, ROW4_H,
          roles={"Category": "Date.Month Start", "Y": "_Measures.Net Sales"},
          title="United Kingdom: net sales per month with anomaly detection",
          subtitle="Analytics pane: anomalies (expected range from Power BI's model, computed when the visual renders)",
          filters=[visual_filter("uk_only", "Territories.Region", ["United Kingdom"])],
          objects={**axes(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "categoryAxis": props(show=True, showAxisTitle=False, fontSize=9.0, labelColor=color(MUTED),
                                         axisType="Scalar"),
                   "dataPoint": [measure_color("_Measures.Net Sales", ACCENT)],
                   "anomalyDetection": [{"properties": {"show": lit(True), "confidenceBandShow": lit(True)},
                                         "selector": {"metadata": "_Measures.Net Sales"}}]})
    textbox(p, "why_finding", b + 8, ROW4_Y + 8, 408, ROW4_H - 16,
            ("Finding: why the United Kingdom is at 43%", 11, INK, True),
            ("Tree: the UK has the largest negative gap (−$5.8M). Split by rep: Jae Pak −$10.1M (his whole UK "
             "quota, no UK sales), José Saraiva +$3.8M, Amy Alberts +$0.4M.", 9, INK2, False),
            ("Rep detail: Jae Pak's home territory is the UK, but all 6,738 of his order lines are booked to Canada "
             "($8.5M). José Saraiva, based in Canada, books $3.8M in the UK.", 9, INK2, False),
            ("So the UK quota follows Jae Pak while his sales land in Canada: a territory-assignment artefact in the "
             "source data, not UK resellers buying less. Fix the assignment before coaching anyone.", 9, INK2, False))


BUILDERS = {"overview": overview, "reps": reps, "time": time, "products": products, "resellers": resellers,
            "quality": quality, "why": why}

# ------------------------------------------- drillthrough & report-page tooltip
# Both pages are hidden in view mode and carry a page binding on Reps[Rep]: the drillthrough page shows up
# under right-click → Drill through on any visual with Rep in it; the tooltip page replaces the default
# tooltip on "Attainment by rep" (reps page) via its visualTooltip object.
DRILL, TIP = "rep_detail", "rep_tooltip"
TIP_W, TIP_H = 320, 240


def rep_binding(kind):
    """Page filter + page binding that ties a Drillthrough/Tooltip page to Reps[Rep]."""
    flt = f"{kind.lower()}_rep"
    return {"filterConfig": {"filters": [{"name": flt, "field": field("Reps.Rep"), "type": "Categorical",
                                          "howCreated": "Drillthrough"}]},
            "pageBinding": {"name": f"{kind.lower()}_binding", "type": kind,
                            "parameters": [{"name": f"{kind.lower()}_rep_param", "boundFilter": flt,
                                            "fieldExpr": field("Reps.Rep")}]},
            "visibility": "HiddenInViewMode"}


def rep_detail(p):
    p.add("button_back", "actionButton", X0, ROW_KPI_Y, 160, ROW_KPI_H,
          objects={"text": props(show=True, text="← Back", fontColor=color(INK), fontSize=12.0,
                                 fontFamily="Segoe UI Semibold"),
                   "icon": props(show=False), "outline": props(show=True, lineColor=color(HAIR)),
                   "fill": props(show=True, fillColor=color(SURFACE)),
                   "shape": props(tileShape="rectangleRounded", rectangleRoundedCurve=12)},
          container={"visualLink": props(show=True, type="Back"), "background": props(show=False),
                     "border": props(show=False)})
    measures = [("Net Sales", M, 2), ("Target", M, 2), ("Attainment", NONE, 0), ("Gap to Target", M, 2), ("Orders", NONE, 0)]
    w = (CW - 176 - GAP * (len(measures) - 1)) // len(measures)
    for i, (m, units, precision) in enumerate(measures):
        p.add(f"kpi_{i}", "card", X0 + 176 + i * (w + GAP), ROW_KPI_Y, w, ROW_KPI_H, roles={"Values": f"_Measures.{m}"},
              objects={"labels": props(color=color(INK), fontSize=22.0, fontFamily="Segoe UI Semibold",
                                       labelDisplayUnits=units, labelPrecision=precision),
                       "categoryLabels": props(show=True, color=color(MUTED), fontSize=9.0)})
    a, b = cols(880, 424)
    p.add("line_rep_quarter", "lineChart", a, ROW2_Y, 880, ROW2_H,
          roles={"Category": "Date.Year Month",
                 "Y": ["_Measures.Net Sales", "_Measures.Target", "_Measures.Sales 3M Avg"],
                 "Y2": "_Measures.Sales Running FY"},
          title="Net sales vs target per month, 3-month average and fiscal-year running total",
          subtitle="Only the rep you drilled through from · WINDOW measures shared with Time & Fiscal",
          objects={**axes(), "legend": legend(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color("_Measures.Net Sales", ACCENT), measure_color("_Measures.Target", TARGET_C),
                                 measure_color("_Measures.Sales 3M Avg", INK2),
                                 measure_color("_Measures.Sales Running FY", RISK_C)]})
    bar(p, "bar_rep_category", "clusteredBarChart", b, ROW2_Y, 424, ROW2_H, "Products.Category", "_Measures.Net Sales",
        ACCENT, "Net sales by category", units=M, sort=("_Measures.Net Sales", "Descending"))
    table(p, "table_rep_resellers", X0, ROW3_Y, CW, ROW3_H,
          ["Resellers.Reseller", "Resellers.Business Type", "Territories.Region", "_Measures.Net Sales",
           "_Measures.Orders", "_Measures.Units", "_Measures.Avg Order Value"],
          "The rep's resellers, largest first", sort=("_Measures.Net Sales", "Descending"))
    a, b = cols(880, 424)
    table(p, "table_account_plan", a, ROW4_Y, 880, ROW4_H,
          ["Resellers.Reseller", "Resellers.RFM Segment", "Resellers.RFM Action", "Resellers.Recency Days",
           "Resellers.Frequency", "_Measures.Net Sales"],
          "Account plan", "RFM segment as of the last sale (quintile scores on recency and order count) and what to do next",
          sort=("_Measures.Net Sales", "Descending"))
    table(p, "table_lapsing", b, ROW4_Y, 424, ROW4_H,
          ["Resellers.Reseller", "_Measures.Days Since Last Order", "_Measures.Churn Status"],
          "Lapsing resellers", "No order for over 90 days: at risk to 180, churned after",
          sort=("_Measures.Days Since Last Order", "Ascending"),
          filters=[measure_filter("lapsing_only", "_Measures.Days Since Last Order", 1, 90)])


def rep_tooltip(p):
    for i, (m, units, precision) in enumerate([("Net Sales", M, 2), ("Target", M, 2), ("Attainment", NONE, 0),
                                               ("Gap to Target", M, 2)]):
        p.add(f"kpi_{i}", "card", 8 + (i % 2) * 156, 8 + (i // 2) * 72, 148, 64, roles={"Values": f"_Measures.{m}"},
              objects={"labels": props(color=color(INK), fontSize=15.0, fontFamily="Segoe UI Semibold",
                                       labelDisplayUnits=units, labelPrecision=precision),
                       "categoryLabels": props(show=True, color=color(MUTED), fontSize=8.0)})
    p.add("col_rep_fy", "clusteredColumnChart", 8, 152, 304, 80,
          roles={"Category": "Date.Fiscal Year", "Y": "_Measures.Attainment"},
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(NONE), "dataPoint": status_fill()})


HIDDEN_PAGES = [  # id, display name, builder, page.json extras, scaffold?
    (DRILL, "Rep detail", rep_detail, rep_binding("Drillthrough"), True),
    (TIP, "Rep tooltip", rep_tooltip, {**rep_binding("Tooltip"), "type": "Tooltip", "width": TIP_W, "height": TIP_H,
                                       "displayOption": "ActualSize"}, False),
]


# ------------------------------------------------------------ theme & files
THEME = {
    "name": "Sales Target",
    # Series colours (e.g. 10 regions on the map) all clear 3:1 on SURFACE; no second amber that reads as "at risk".
    "dataColors": [ACCENT, ON_C, RISK_C, BEHIND_C, TARGET_C, INK2, "#7A4FA3", "#2A78D6", "#C2185B", "#0E7C86",
                   "#8A6D3B", "#104281"],
    "foreground": INK, "foregroundNeutralSecondary": INK2, "foregroundNeutralTertiary": MUTED,
    "background": SURFACE, "backgroundLight": PLANE, "backgroundNeutral": HAIR, "tableAccent": ACCENT,
    "good": ON_C, "neutral": RISK_C, "bad": BEHIND_C, "null": "#F0EFEC",
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


def background_png(path, h=H):
    """Canvas wallpaper: warm-white plane with a navy sidebar and a hairline accent."""
    img = Image.new("RGB", (W, h), PLANE)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, SIDEBAR - 1, h], fill=SIDE)
    d.rectangle([SIDEBAR - 3, 0, SIDEBAR - 1, h], fill=ACCENT)
    d.line([24, 112, SIDEBAR - 24, 112], fill=SIDE_LINE, width=1)
    d.line([24, 776, SIDEBAR - 24, 776], fill=SIDE_LINE, width=1)
    d.line([X0, 99, X1, 99], fill=HAIR, width=1)
    img.save(path, optimize=True)


# ------------------------------------------------------- paginated report (RDL)
RDL_DAX = """DEFINE
    VAR _quarters =
        SELECTCOLUMNS (
            SUMMARIZECOLUMNS ( Reps[Rep], 'Date'[Fiscal Quarter Number], 'Date'[Fiscal Quarter],
                "S", [Net Sales], "T", [Target] ),
            "Rep", Reps[Rep], "Section", "1 Quarter", "Sort", 'Date'[Fiscal Quarter Number],
            "Label", 'Date'[Fiscal Quarter], "Net Sales", [S], "Target", [T] )
    VAR _resellers =
        SELECTCOLUMNS (
            GENERATE ( VALUES ( Reps[Rep] ),
                TOPN ( 5, ADDCOLUMNS ( VALUES ( Resellers[Reseller] ), "S", [Net Sales] ), [S], DESC ) ),
            "Rep", Reps[Rep], "Section", "2 Top reseller", "Sort", - [S],
            "Label", Resellers[Reseller], "Net Sales", [S], "Target", BLANK () )
EVALUATE
    FILTER ( UNION ( _quarters, _resellers ), NOT ISBLANK ( [Net Sales] ) || NOT ISBLANK ( [Target] ) )
ORDER BY [Rep], [Section], [Sort]"""


def write_rdl(path):
    """RepStatement.rdl: one printed page per rep (page break on the Rep group) with net sales vs target per fiscal
    quarter and the rep's top five resellers, from the published SalesTarget semantic model. Not rendered here:
    Report Builder / the Service are Windows/cloud only; the dataset id is filled in at publish (phase G)."""
    from xml.sax.saxutils import escape

    def tb(name, value, fmt=None, bold=False, size=9, align="Left", color_=INK):
        style = f"<FontFamily>Segoe UI</FontFamily><FontSize>{size}pt</FontSize><Color>{color_}</Color>"
        style += "<FontWeight>Bold</FontWeight>" * bold + (f"<Format>{fmt}</Format>" if fmt else "")
        return (f'<Textbox Name="{name}"><CanGrow>true</CanGrow><KeepTogether>true</KeepTogether><Paragraphs><Paragraph>'
                f"<TextRuns><TextRun><Value>{escape(value)}</Value><Style>{style}</Style></TextRun></TextRuns>"
                f"<Style><TextAlign>{align}</TextAlign></Style></Paragraph></Paragraphs>"
                f"<Style><Border><Style>None</Style></Border><BottomBorder><Color>{HAIR}</Color><Style>Solid</Style></BottomBorder>"
                f"<PaddingLeft>4pt</PaddingLeft><PaddingRight>4pt</PaddingRight><PaddingTop>3pt</PaddingTop>"
                f"<PaddingBottom>3pt</PaddingBottom></Style></Textbox>")

    cell = lambda body, span=1: (f"<TablixCell><CellContents>{body}" + (f"<ColSpan>{span}</ColSpan>" if span > 1 else "")
                                 + "</CellContents></TablixCell>" + "<TablixCell />" * (span - 1))
    row = lambda h, cells: f"<TablixRow><Height>{h}in</Height><TablixCells>{''.join(cells)}</TablixCells></TablixRow>"
    att = "=IIF(IsNothing(Fields!Target.Value) Or Fields!Target.Value = 0, Nothing, Fields!Net_Sales.Value / Fields!Target.Value)"
    rows = [
        row(0.4, [cell(tb("RepName", '="Sales statement · " & Fields!Rep.Value', bold=True, size=14), 4)]),
        row(0.25, [cell(tb("SectionHead", '=IIF(Fields!Section.Value = "1 Quarter", "Fiscal quarter", "Top reseller")',
                           bold=True, color_=MUTED)),
                   cell(tb("HeadSales", "Net sales", bold=True, align="Right", color_=MUTED)),
                   cell(tb("HeadTarget", '=IIF(Fields!Section.Value = "1 Quarter", "Target", "")', bold=True,
                           align="Right", color_=MUTED)),
                   cell(tb("HeadAtt", '=IIF(Fields!Section.Value = "1 Quarter", "Attainment", "")', bold=True,
                           align="Right", color_=MUTED))]),
        row(0.22, [cell(tb("Label", "=Fields!Label.Value")),
                   cell(tb("NetSales", "=Fields!Net_Sales.Value", "$#,0", align="Right")),
                   cell(tb("Target", "=Fields!Target.Value", "$#,0", align="Right")),
                   cell(tb("Attainment", att, "0%", align="Right"))]),
    ]
    fields = "".join(f'<Field Name="{n.replace(" ", "_")}"><DataField>[{n}]</DataField></Field>'
                     for n in ("Rep", "Section", "Sort", "Label", "Net Sales", "Target"))
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<Report xmlns="http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition" xmlns:rd="http://schemas.microsoft.com/SQLServer/reporting/reportdesigner">
  <AutoRefresh>0</AutoRefresh>
  <DataSources>
    <DataSource Name="SalesTarget">
      <ConnectionProperties>
        <DataProvider>PBIDATASET</DataProvider>
        <ConnectString>Data Source=pbiazure://api.powerbi.com/;Identity Provider="https://login.microsoftonline.com/common, https://analysis.windows.net/powerbi/api, 929d0ec0-7a41-4b1e-bc7c-b754a28bddcc";Initial Catalog=sobe_wowvirtualserver-DATASET_ID_SET_AT_PUBLISH;Integrated Security=ClaimsToken</ConnectString>
      </ConnectionProperties>
      <rd:SecurityType>None</rd:SecurityType>
    </DataSource>
  </DataSources>
  <DataSets>
    <DataSet Name="Statement">
      <Query>
        <DataSourceName>SalesTarget</DataSourceName>
        <CommandText>{escape(RDL_DAX)}</CommandText>
      </Query>
      <Fields>{fields}</Fields>
    </DataSet>
  </DataSets>
  <ReportSections>
    <ReportSection>
      <Body>
        <ReportItems>
          <Tablix Name="Statement">
            <TablixBody>
              <TablixColumns>{"".join(f"<TablixColumn><Width>{w}in</Width></TablixColumn>" for w in (3.2, 1.3, 1.3, 1.1))}</TablixColumns>
              <TablixRows>{"".join(rows)}</TablixRows>
            </TablixBody>
            <TablixColumnHierarchy><TablixMembers>{"<TablixMember />" * 4}</TablixMembers></TablixColumnHierarchy>
            <TablixRowHierarchy>
              <TablixMembers>
                <TablixMember>
                  <Group Name="Rep"><GroupExpressions><GroupExpression>=Fields!Rep.Value</GroupExpression></GroupExpressions>
                    <PageBreak><BreakLocation>Between</BreakLocation></PageBreak></Group>
                  <SortExpressions><SortExpression><Value>=Fields!Rep.Value</Value></SortExpression></SortExpressions>
                  <TablixMembers>
                    <TablixMember><KeepWithGroup>After</KeepWithGroup><RepeatOnNewPage>true</RepeatOnNewPage></TablixMember>
                    <TablixMember>
                      <Group Name="Section"><GroupExpressions><GroupExpression>=Fields!Section.Value</GroupExpression></GroupExpressions></Group>
                      <SortExpressions><SortExpression><Value>=Fields!Section.Value</Value></SortExpression></SortExpressions>
                      <TablixMembers>
                        <TablixMember><KeepWithGroup>After</KeepWithGroup></TablixMember>
                        <TablixMember>
                          <Group Name="Details" />
                          <SortExpressions><SortExpression><Value>=Fields!Sort.Value</Value></SortExpression></SortExpressions>
                        </TablixMember>
                      </TablixMembers>
                    </TablixMember>
                  </TablixMembers>
                </TablixMember>
              </TablixMembers>
            </TablixRowHierarchy>
            <DataSetName>Statement</DataSetName>
            <Top>0in</Top><Left>0in</Left><Height>0.87in</Height><Width>6.9in</Width>
            <Style><Border><Style>None</Style></Border></Style>
          </Tablix>
        </ReportItems>
        <Height>0.87in</Height>
        <Style />
      </Body>
      <Width>6.9in</Width>
      <Page>
        <PageFooter>
          <Height>0.3in</Height><PrintOnFirstPage>true</PrintOnFirstPage><PrintOnLastPage>true</PrintOnLastPage>
          <ReportItems>{tb("Footer", '="Adventure Works · Sales vs Target · printed " & Format(Globals!ExecutionTime, "dd MMM yyyy") & " · page " & Globals!PageNumber & " of " & Globals!TotalPages', size=8, color_=MUTED).replace("<CanGrow>", "<Top>0.05in</Top><Height>0.22in</Height><Width>6.9in</Width><CanGrow>", 1)}</ReportItems>
          <Style />
        </PageFooter>
        <PageHeight>11.69in</PageHeight><PageWidth>8.27in</PageWidth>
        <LeftMargin>0.6in</LeftMargin><RightMargin>0.6in</RightMargin><TopMargin>0.6in</TopMargin><BottomMargin>0.6in</BottomMargin>
        <Style />
      </Page>
    </ReportSection>
  </ReportSections>
  <rd:ReportUnitType>Inch</rd:ReportUnitType>
  <rd:ReportID>5b7e1d2c-3f4a-4c8e-9a61-2d0f7b3e8c41</rd:ReportID>
</Report>
"""
    path.write_text(xml)


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def main():
    shutil.rmtree(REPORT, ignore_errors=True)
    res = REPORT / "StaticResources" / "RegisteredResources"
    res.mkdir(parents=True)
    dump(res / "SalesTarget.json", THEME)
    background_png(res / "canvas.png")
    background_png(res / "canvas-tall.png", TALL)
    background_png(res / "canvas-taller.png", TALLER)

    dump(PBI / "SalesTarget.pbip", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": "SalesTarget.Report"}}],
        "settings": {"enableAutoRecovery": True}})
    dump(REPORT / ".platform", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Report", "displayName": "SalesTarget"},
        "config": {"version": "2.0", "logicalId": "8d2f61a0-4c3b-4e7a-9f15-b06e2d7c3a94"}})
    dump(REPORT / "definition.pbir", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": "../SalesTarget.SemanticModel"}}})
    d = REPORT / "definition"
    dump(d / "version.json", {"$schema": f"{SCHEMA}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"})
    dump(d / "report.json", {
        "$schema": f"{SCHEMA}/report/3.3.0/schema.json",
        "themeCollection": {"customTheme": {
            "name": "SalesTarget.json", "type": "RegisteredResources",
            "reportVersionAtImport": {"visual": "1.8.95", "report": "2.0.95", "page": "1.3.95"}}},
        "objects": {"outspacePane": [{"properties": {"expanded": lit(False)}}]},
        # Report-level filter: hidden and locked, so no one can bring in rows without a rep title.
        "filterConfig": {"filters": [{
            "name": "report_rep_title", "displayName": "Reps with a title", "ordinal": 0, "field": field("Reps.Title"),
            "type": "Advanced", "howCreated": "User", "isHiddenInViewMode": True, "isLockedInViewMode": True,
            "filter": {"Version": 2, "From": [{"Name": "r", "Entity": "Reps", "Type": 0}],
                       "Where": [{"Condition": {"Not": {"Expression": {"Comparison": {
                           "ComparisonKind": 0,
                           "Left": {"Column": {"Expression": {"SourceRef": {"Source": "r"}}, "Property": "Title"}},
                           "Right": {"Literal": {"Value": "null"}}}}}}}]}}],
            "filterSortOrder": "Custom"},
        "publicCustomVisuals": [BULLET],
        "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True,
                     "hideVisualContainerHeader": False, "useEnhancedTooltips": True},
        "resourcePackages": [{"name": "RegisteredResources", "type": "RegisteredResources", "items": [
            {"name": "SalesTarget.json", "path": "SalesTarget.json", "type": "CustomTheme"},
            {"name": "canvas.png", "path": "canvas.png", "type": "Image"},
            {"name": "canvas-tall.png", "path": "canvas-tall.png", "type": "Image"},
            {"name": "canvas-taller.png", "path": "canvas-taller.png", "type": "Image"}]}]})
    dump(d / "pages" / "pages.json", {"$schema": f"{SCHEMA}/pagesMetadata/1.1.0/schema.json",
                                      "pageOrder": [p[0] for p in PAGES] + [h[0] for h in HIDDEN_PAGES],
                                      "activePageName": PAGES[0][0]})

    total = 0
    pages = [(pid, name, title, subtitle, BUILDERS[pid], {}, True) for pid, name, title, subtitle in PAGES]
    pages += [(pid, name, name, "Drillthrough: one rep, filtered by the page binding", build, extra, sc)
              for pid, name, build, extra, sc in HIDDEN_PAGES]
    for pid, name, title, subtitle, build, extra, with_scaffold in pages:
        page = Page(pid, name, title, subtitle)
        if with_scaffold:
            scaffold(page)
        build(page)
        page.tab_order()
        h = PAGE_H.get(pid, H)
        img = WALLPAPER[h]
        more = {"visualInteractions": page.interactions} if page.interactions else {}
        if page.filters:
            more["filterConfig"] = {"filters": page.filters, "filterSortOrder": "Custom"}
        dump(d / "pages" / pid / "page.json", {
            "$schema": f"{SCHEMA}/page/2.1.0/schema.json",
            "name": pid, "displayName": name, "displayOption": "FitToWidth" if h > H else "FitToPage",
            "width": W, "height": h, **extra, **more,
            "objects": {} if not with_scaffold else {
                "background": [{"properties": {
                    "image": {"image": {"name": lit(img),
                                        "url": {"expr": {"ResourcePackageItem": {"PackageName": "RegisteredResources",
                                                                                 "PackageType": 1, "ItemName": img}}},
                                        "scaling": lit("Fit")}},
                    "transparency": lit(0.0)}}],
                "outspace": [{"properties": {"color": color("#E9E8E3")}}]}})
        for v in page.visuals:
            dump(d / "pages" / pid / "visuals" / v["name"] / "visual.json", v)
        for i, (v, (x, y, w, hh)) in enumerate(page.mobile.items()):
            dump(d / "pages" / pid / "visuals" / v / "mobile.json", {
                "$schema": f"{SCHEMA}/visualContainerMobileState/2.4.0/schema.json",
                "position": {"x": x, "y": y, "z": i * 100, "width": w, "height": hh, "tabOrder": i * 100}})
        total += len(page.visuals)
        print(f"  ✓ page {name:<18} {len(page.visuals):>3} visuals")
    # Reset filters: every synced slicer back to All on every page (data only, stays on the current page).
    slicers = {"slicer_fy": "slicer", "slicer_region": "slicer", "slicer_category": "slicer", "slicer_btype": "slicer"}
    BOOKMARKS.append({"name": "bm_reset_filters", "displayName": "Reset filters",
                      "options": {"suppressDisplay": True, "suppressActiveSection": True},
                      "explorationState": {"version": "1.3", "activeSection": PAGES[0][0], "sections": {
                          pid: {"visualContainers": {v: {"singleVisual": {"visualType": t, "objects": {}}}
                                                     for v, t in slicers.items()}} for pid, *_ in PAGES}}})
    dump(d / "bookmarks" / "bookmarks.json", {"$schema": f"{SCHEMA}/bookmarksMetadata/1.0.0/schema.json",
                                              "items": [{"name": b["name"]} for b in BOOKMARKS]})
    for b in BOOKMARKS:
        dump(d / "bookmarks" / f"{b['name']}.bookmark.json", {"$schema": f"{SCHEMA}/bookmark/2.1.0/schema.json", **b})
    write_rdl(ROOT / "reports" / "RepStatement.rdl")
    print(f"  ✓ wrote {REPORT.relative_to(ROOT)} ({len(pages)} pages, {total} visuals, {len(BOOKMARKS)} bookmarks)")

    result = subprocess.run(["pbir", "validate", str(REPORT), "--all"], capture_output=True, text=True,
                            env={**os.environ, "NO_COLOR": "1", "COLUMNS": "200"})
    print(result.stdout[-3000:], result.stderr[-2000:])


if __name__ == "__main__":
    main()
