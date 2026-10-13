"""Generate the Power BI report (PBIR) — 11 designed pages + 2 hidden (drillthrough, tooltip) over the SalesPipeline semantic model.

    uv run python src/build_report.py

Writes powerbi/SalesPipeline.Report + SalesPipeline.pbip, then runs `pbir validate`
(schema + field references against the model). Open SalesPipeline.pbip in Power BI Desktop.

Colour rule: blue = won, grey = lost, amber = open, orange = stale. Nothing else uses those four.

Beyond the standard visuals: visual calculations (RUNNINGSUM / MOVINGAVERAGE / RANK), small multiples,
a decomposition tree, key influencers, a Time Intelligence matrix, and a phone layout (mobile.json)
for the Overview page.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
PBI = ROOT / "powerbi"
REPORT = PBI / "SalesPipeline.Report"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"

W, H = 1600, 900
SIDEBAR = 232
X0, X1, GAP = 256, 1576, 16
CW = X1 - X0

# ---------------------------------------------------------------- palette
# MUTED was #898781 (3.5:1 on the card surface, fails WCAG AA for text); #6B6963 is 5.3:1.
INK, INK2, MUTED, HAIR, SURFACE, PLANE = "#0b0b0b", "#52514e", "#6B6963", "#E6E4DD", "#FCFCFB", "#F4F4F1"
SIDE, SIDE_TEXT, SIDE_LINE = "#1b2440", "#aeb9d8", "#2c3a63"
WON, LOST, OPEN, STALE = "#2a78d6", "#B9B8B0", "#e0a030", "#C2410C"
ACCENT = "#4a6fd6"
GOOD, BAD = "#1E7B34", "#B42318"  # font colours of 'Win Rate vs Region Color' (set in the model)


def contrast(fg, bg):
    """WCAG 2.x contrast ratio of two #RRGGBB colours."""
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# WCAG AA: every text colour on its background >= 4.5:1. Bars/lines (graphics) need 3:1: WON passes (4.3),
# OPEN amber (2.2) and LOST grey (1.9) do not, so charts that use them carry data labels and a legend.
for _fg, _bg in [(INK, SURFACE), (INK2, SURFACE), (MUTED, SURFACE), (MUTED, PLANE), (INK2, PLANE),
                 (SIDE_TEXT, SIDE), ("#FFFFFF", SIDE), (GOOD, SURFACE), (BAD, SURFACE)]:
    assert contrast(_fg, _bg) >= 4.5, f"contrast {_fg} on {_bg} = {contrast(_fg, _bg):.2f} < 4.5"
assert contrast(WON, SURFACE) >= 3


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


def vcalc(name, expr, hidden=False):
    """A visual calculation: DAX evaluated on the visual's own matrix (rows / columns), not in the model."""
    p = {"field": {"NativeVisualCalculation": {"Language": "dax", "Expression": expr, "Name": name,
                                                "DataType": "Double"}},
         "queryRef": name, "nativeQueryRef": name}
    return {**p, "hidden": True} if hidden else p


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


def unlit(v):
    """Inverse of lit() for a string literal: the plain text."""
    raw = v["expr"]["Literal"]["Value"] if isinstance(v, dict) else str(v)
    return raw[1:-1].replace("''", "'") if raw.startswith("'") else raw


def describe(vtype, roles, objects):
    """Fallback alt text for a visual without a title: what it shows, in words."""
    if vtype == "textbox":
        return " ".join(r["value"] for para in objects["general"][0]["properties"]["paragraphs"] for r in para["textRuns"])
    if vtype == "actionButton":
        t = objects.get("text", [{}])[0].get("properties", {}).get("text")
        return f"Button: {unlit(t)}" if t else "Button"
    names = [r if isinstance(r, str) else r.get("queryRef", "") for refs in (roles or {}).values()
             for r in ([refs] if isinstance(refs, (str, dict)) else refs)]
    names = [n.split(".", 1)[-1] for n in names]
    kind = {"card": "KPI card", "slicer": "Slicer", "image": "Image"}.get(vtype, vtype)
    return f"{kind}: {', '.join(names)}" if names else kind


class Page:
    def __init__(self, pid, name, title, subtitle):
        self.id, self.name, self.title, self.subtitle = pid, name, title, subtitle
        self.visuals = []
        self.mobile = {}  # visual name -> (x, y, width, height) in % of the phone canvas
        self.nav = PAGES
        self.meta = {}       # extra page.json keys: type, visibility, pageBinding, filterConfig, visualInteractions
        self.bookmarks = []  # (name, displayName, {group name: hidden?}) — target visuals are the groups' children

    def add(self, name, vtype, x, y, w, h, roles=None, title=None, subtitle=None,
            objects=None, container=None, sort=None, filters=None, alt=None, group=None, hidden=False,
            **visual_extra):
        vc = dict(container or {})
        if title is not None:
            vc.setdefault("title", props(show=True, text=title))
            if subtitle:
                vc.setdefault("subTitle", props(show=True, text=subtitle))
        elif vtype not in ("textbox", "actionButton"):
            vc.setdefault("title", props(show=False))
        # Accessibility: every visual gets alt text (screen readers read it instead of the chart).
        alt = alt or (f"{title}. {subtitle}" if title and subtitle else title) or describe(vtype, roles, objects or {})
        vc.setdefault("general", props(altText=alt[:250]))
        query = {"queryState": {role: {"projections": [projection(r) for r in ([refs] if isinstance(refs, (str, dict)) else refs)]}
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

    def group(self, name, display, hidden=False):
        """Selection-pane group around the visuals already added with group=name; its box is their union."""
        kids = [v for v in self.visuals if v.get("parentGroupName") == name]
        x0 = min(v["position"]["x"] for v in kids)
        y0 = min(v["position"]["y"] for v in kids)
        x1 = max(v["position"]["x"] + v["position"]["width"] for v in kids)
        y1 = max(v["position"]["y"] + v["position"]["height"] for v in kids)
        z = len(self.visuals) * 100
        g = {"$schema": f"{SCHEMA}/visualContainer/2.9.0/schema.json", "name": name,
             "position": {"x": x0, "y": y0, "z": z, "width": x1 - x0, "height": y1 - y0, "tabOrder": z},
             "visualGroup": {"displayName": display, "groupMode": "ScaleMode"}}
        if hidden:
            g["isHidden"] = True
        self.visuals.append(g)

    def finalize(self):
        """Tab order for keyboard / screen-reader users: page title, then KPIs → charts → tables in reading order
        (top-to-bottom, left-to-right), then the header slicers, then the navigation."""
        def rank(v):
            n, p = v["name"], v["position"]
            if n == "title":
                return (0, 0, 0)
            if n.startswith("nav_") or n in ("brand", "footer"):
                return (3, p["y"], p["x"])
            if n.startswith("slicer_") and p["y"] < ROW_KPI_Y:
                return (2, p["y"], p["x"])
            return (1, p["y"], p["x"])
        for i, v in enumerate(sorted(self.visuals, key=rank)):
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



# --------------------------------------------------------- page scaffolding
PAGES = [
    ("overview", "Overview", "Sales pipeline",
     "Revenue won, win rate and what is still open. Date Basis picks which date the axis uses."),
    ("funnel", "Funnel & Cycle", "Funnel & sales cycle",
     "How many deals reach each step, and how the time to close relates to winning."),
    ("pipeline", "Pipeline Health", "Pipeline health",
     "What is open, how old it is, and what it is realistically worth."),
    ("team", "Sales Team", "Sales team",
     "Managers and agents: revenue, win rate, deal size and the open book."),
    ("products", "Products & Accounts", "Products & accounts",
     "What sells, at what price against list, and to whom."),
    ("account_value", "Account Value", "Account value",
     "Which accounts make the revenue: Pareto, ABC classes and revenue bands that all move with the slicers."),
    ("sync", "API Sync", "CRM API sync log",
     "Every extraction run: requests, throttling, server errors and rows loaded."),
    ("trends", "Trends & Windows", "Trends & window functions",
     "Month-on-month, rolling and best-month figures; one panel per manager; time intelligence by calendar."),
    ("drivers", "Drivers", "What drives revenue and wins",
     "Decomposition tree, key influencers, share of parent and the funnel as a many-to-many bridge."),
    ("diagnosis", "Diagnosis", "Why did revenue fall?",
     "Month-on-month change → volume / mix / price bridge → where the drop sits → the deals to call."),
    ("about", "About & Data", "About the data",
     "Where the numbers come from, how they flow, and what each field means."),
]
# Hidden pages: not in the navigation, reached by drillthrough / hover only.
HIDDEN_PAGES = [
    ("deal_detail", "Deal Detail", "Deal detail", "Open deals behind the agent or account you drilled through from."),
    ("agent_tooltip", "Agent Tooltip", "", ""),
]
NO_SCAFFOLD = {"agent_tooltip"}
SANKEY = "sankey02300D1BE6F5427989F3DE31CCA9E0F32020"  # Microsoft Sankey (AppSource) visual GUID

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
            ("MAVEN TECH", 8, SIDE_TEXT, True), ("Sales Pipeline", 17, "#FFFFFF", True))
    for i, (pid, name, *_) in enumerate(page.nav):
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
            ("Source: Maven Analytics CRM Sales Opportunities (public domain)", 8, SIDE_TEXT, False),
            ("Pipeline: CRM API · DuckDB SQL · Power BI (TMDL + PBIR)", 8, SIDE_TEXT, False))

    textbox(page, "title", X0, 22, 520, 70, (page.title, 20, INK, True), (page.subtitle, 10, INK2, False))
    sx = cols(520, 130, 190, 270, 146)
    slicer(page, "slicer_year", sx[1], 16, 130, 76, "Date.Year", single=False, sync="Year")
    slicer(page, "slicer_region", sx[2], 16, 190, 76, "Agent.Region", single=False, sync="Region")
    slicer(page, "slicer_product", sx[3], 16, 270, 76, "Product.Product", single=False, sync="Product")
    slicer(page, "slicer_basis", sx[4], 16, 146, 76, "Date Basis.Date Basis", default="Closed", sync="Basis")


def kpis(page, measures):
    w = (CW - GAP * (len(measures) - 1)) // len(measures)
    for i, (m, units, precision) in enumerate(measures):
        page.add(f"kpi_{i}", "card", X0 + i * (w + GAP), ROW_KPI_Y, w, ROW_KPI_H, roles={"Values": f"_Measures.{m}"},
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


def table(page, name, x, y, w, h, values, title, subtitle=None, sort=None, objects=None, **kw):
    page.add(name, "tableEx", x, y, w, h, roles={"Values": values}, title=title, subtitle=subtitle, sort=sort,
             objects={"grid": props(rowPadding=6), **(objects or {})}, **kw)


def data_bar(ref, hex_):
    """Conditional formatting: a data bar behind the numbers of one table column."""
    return {"properties": {"dataBars": {"positiveColor": color(hex_), "negativeColor": color(BAD),
                                        "axisColor": color(HAIR), "reverseDirection": lit(False),
                                        "hideText": lit(False)}},
            "selector": {"metadata": ref}}


def font_by_measure(ref, color_measure):
    """Conditional formatting: font colour of one column comes from a measure that returns a hex colour."""
    return {"properties": {"fontColor": expr_color(color_measure)}, "selector": {"metadata": ref}}


def icon_rules(ref, rules):
    """Conditional formatting: icon set by rules on a text measure; rules = [(value, icon name)]."""
    measure = field(ref)
    cases = [{"Condition": {"Comparison": {"ComparisonKind": 0, "Left": measure, "Right": {"Literal": {"Value": lit_value(v)}}}},
              "Value": {"Literal": {"Value": lit_value(icon)}}} for v, icon in rules]
    return {"properties": {"icon": {"layout": lit("Before"), "verticalAlignment": lit("Middle"),
                                    "value": {"expr": {"Conditional": {"Cases": cases}}}}},
            "selector": {"metadata": ref}}


def hierarchy(table_, hier, levels, active=True):
    """Projections for a model hierarchy; all levels active = expanded ('expand all down'), drill up/down works."""
    out = []
    for lv in levels:
        p = {"field": {"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {"SourceRef": {"Entity": table_}},
                                                                      "Hierarchy": hier}}, "Level": lv}},
             "queryRef": f"{table_}.{hier}.{lv}", "nativeQueryRef": f"{hier} {lv}"}
        if active:
            p["active"] = True
        out.append(p)
    return out


def button(page, name, x, y, w, h, text, link, alt=None, tooltip=None, icon=None, filled=False, **kw):
    """Action button: link = visualLink properties (PageNavigation / Bookmark / Back / Drillthrough)."""
    objects = {"text": props(show=bool(text), text=text or " ", fontColor=color("#FFFFFF" if filled else INK2),
                             fontSize=10.0, fontFamily="Segoe UI Semibold", horizontalAlignment="center"),
               "fill": props(show=True, fillColor=color(ACCENT if filled else "#FFFFFF"), transparency=0.0),
               "outline": props(show=True, lineColor=color(HAIR), weight=1.0),
               "shape": props(tileShape="rectangleRounded", rectangleRoundedCurve=8)}
    objects["icon"] = props(show=True, shapeType=icon, lineColor=color(INK2)) if icon else props(show=False)
    link = {"show": True, **link}
    if tooltip:
        link["tooltip"] = tooltip
    page.add(name, "actionButton", x, y, w, h, objects=objects, alt=alt or tooltip or text,
             container={"visualLink": props(**link), "background": props(show=False), "border": props(show=False)}, **kw)


# Report-page tooltip: hovering an agent / manager bar shows the hidden Agent Tooltip page instead of the default.
TOOLTIP_AGENT = {"visualTooltip": props(show=True, type="Canvas", section="agent_tooltip")}


def bool_filter(name, ref, value=True, how="User", locked=True, hidden=False):
    """Page / report level filter on a boolean column, locked (and optionally hidden) in reading view."""
    f = {"name": name, "field": field(ref), "type": "Categorical", "howCreated": how, "filter": in_filter(ref, [value]),
         "isLockedInViewMode": locked}
    if hidden:
        f["isHiddenInViewMode"] = True
    return f


def info_button(page, name, x, y, tip):
    """(i) button: hover shows the definition; click opens the About page with the data dictionary."""
    button(page, name, x, y, 28, 28, None, {"type": "PageNavigation", "navigationSection": "about"},
           tooltip=tip, icon="information")


def bar(page, name, vtype, x, y, w, h, category, measure, hex_, title, subtitle=None, units=K, sort=None, **kw):
    page.add(name, vtype, x, y, w, h, roles={"Category": category, "Y": measure}, title=title, subtitle=subtitle,
             sort=sort, objects={**axes(show_y=vtype == "clusteredColumnChart", grid=False),
                                 "labels": bar_labels(units), "dataPoint": fill(hex_)}, **kw)


def param_slicer(page, name, ref, default, x, y, w):
    """Horizontal single-select slicer over a field parameter."""
    page.add(name, "slicer", x, y, w, 44, roles={"Values": ref},
             objects={"data": props(mode="HorizontalList"), "header": props(show=False),
                      "selection": props(singleSelect=True, strictSingleSelect=True),
                      "items": props(fontColor=color(INK2), textSize=9.0),
                      "general": [{"properties": {"filter": {"filter": in_filter(ref, [default])}}}]},
             container={"background": props(show=False), "border": props(show=False)})


# ------------------------------------------------------------------- pages
def overview(p):
    kpis(p, [("Won Revenue", M, 2), ("Won Deals", NONE, 0), ("Win Rate", NONE, 1), ("Avg Deal Size", NONE, 0),
             ("Open Pipeline", M, 2), ("Stale Share", NONE, 0)])
    a, b = cols(880, 424)
    # Axis = the Date[Calendar] hierarchy, expanded to month; drill up to quarter / year from the visual header.
    # Data labels + legend: won/lost are not told apart by colour alone (grey is under 3:1 on the card).
    p.add("col_monthly", "clusteredColumnChart", a, ROW2_Y, 880, ROW2_H,
          roles={"Category": hierarchy("Date", "Calendar", ["Year", "Quarter", "Month"]),
                 "Y": ["_Measures.Won Deals", "_Measures.Lost Deals"]},
          title="Deals closed per month", subtitle="Won vs lost; drill up to quarter or year. Date Basis switches the date used",
          objects={**axes(), "legend": legend(), "labels": bar_labels(NONE),
                   "dataPoint": [measure_color("_Measures.Won Deals", WON), measure_color("_Measures.Lost Deals", LOST)]})
    p.add("card_headline", "card", b, ROW2_Y, 424, ROW2_H, roles={"Values": "_Measures.Headline"},
          title="In one sentence",
          objects={"labels": props(color=color(INK), fontSize=14.0, fontFamily="Segoe UI"),
                   "categoryLabels": props(show=False), "wordWrap": props(show=True)})
    a, b = cols(652, 652)
    # Two field parameters: Analyze By picks the grouping (both charts), Metric picks the measure (left chart).
    param_slicer(p, "slicer_analyze", "Analyze By.Analyze By", "Manager", a, ROW3_Y, 652)
    param_slicer(p, "slicer_metric", "Metric.Metric", "Won revenue", b, ROW3_Y, 652)
    y, h = ROW3_Y + 52, ROW3_H - 52
    # Y is [Selected Metric], keyed on the Metric field parameter's order column: pbir rejects a field-parameter
    # column in a Measure-only role, so the measure pick goes through SWITCH (format string follows the pick).
    bar(p, "bar_revenue_by", "clusteredBarChart", a, y, 652, h, "Analyze By.Analyze By", "_Measures.Selected Metric", WON,
        "The measure picked (right) by the dimension picked (left)", "Analyze By and Metric are field parameters",
        sort=("_Measures.Selected Metric", "Descending"), container=TOOLTIP_AGENT)
    bar(p, "bar_winrate_by", "clusteredBarChart", b, y, 652, h, "Analyze By.Analyze By", "_Measures.Win Rate", ACCENT,
        "Win rate by the same dimension", "Revenue differs a lot; win rate barely moves", units=NONE,
        sort=("_Measures.Win Rate", "Descending"))
    # Phone layout: title, KPIs two per row, then the headline and the two main charts stacked.
    p.mobile = {"title": (0, 0, 100, 7), **{f"kpi_{i}": (50 * (i % 2), 8 + 10 * (i // 2), 50, 9) for i in range(6)},
                "card_headline": (0, 38, 100, 14), "col_monthly": (0, 53, 100, 23), "bar_revenue_by": (0, 77, 100, 23)}


def funnel(p):
    kpis(p, [("Deals", NONE, 0), ("Closed Deals", NONE, 0), ("Win Rate", NONE, 1), ("Avg Cycle Days", NONE, 0)])
    a, b = cols(640, 664)
    p.add("funnel_steps", "funnel", a, ROW2_Y, 640, ROW2_H,
          roles={"Category": "Steps.Step", "Y": "_Measures.Funnel Deals"},
          title="Deal funnel", subtitle="Deals reaching each step; Date Basis = Created shows a cohort",
          objects={"dataPoint": fill(WON), "labels": props(show=True, color=color(INK), fontSize=10.0)})
    bar(p, "col_winrate_cycle", "clusteredColumnChart", b, ROW2_Y, 664, ROW2_H, "Cycle Band.Cycle Band",
        "_Measures.Win Rate", ACCENT, "Win rate by days from engage to close",
        "Low 0–14 bar is a data quirk: Apr, Jul and Oct closes all took ~1 week and won 49%", units=NONE)
    p.add("col_cycle_mix", "columnChart", a, ROW3_Y, 640, ROW3_H,
          roles={"Category": "Cycle Band.Cycle Band", "Y": ["_Measures.Won Deals", "_Measures.Lost Deals"]},
          title="Closed deals by cycle length", subtitle="Outside Apr, Jul and Oct, quick closes win 67%, like every other band",
          objects={**axes(), "legend": legend(), "labels": bar_labels(NONE),
                   "dataPoint": [measure_color("_Measures.Won Deals", WON), measure_color("_Measures.Lost Deals", LOST)]})
    p.add("line_cycle_month", "lineChart", b, ROW3_Y, 664, ROW3_H,
          roles={"Category": "Date.Month Start", "Y": ["_Measures.Avg Cycle Days"]},
          title="Average cycle by close month", subtitle="Days from engage to close; Apr, Jul and Oct dip to ~6 days in the source data",
          objects={**axes(), "lineStyles": props(strokeWidth=2, showMarker=True, markerSize=3),
                   "dataPoint": [measure_color("_Measures.Avg Cycle Days", ACCENT)],
                   # Analytics: constant line at the 104-day stale threshold, and an average line whose value is
                   # [Avg Cycle Days] evaluated at the visual total (the whole period, weighted by deals).
                   "y1AxisReferenceLine": [
                       {"properties": {"show": lit(True), "displayName": lit("Stale after 104 days"), "value": lit(104.0),
                                       "lineColor": color(STALE), "style": lit("dashed"), "dataLabelShow": lit(True),
                                       "dataLabelColor": color(STALE)}, "selector": {"id": "1"}},
                       {"properties": {"show": lit(True), "displayName": lit("Average"),
                                       "value": {"expr": field("_Measures.Avg Cycle Days")},
                                       "lineColor": color(INK2), "style": lit("dotted"), "dataLabelShow": lit(True),
                                       "dataLabelColor": color(INK2)}, "selector": {"id": "2"}}]})
    info_button(p, "info_cycle", X1 - 40, ROW3_Y + 10,
                "Cycle = days from engage to close. Closes in Apr, Jul and Oct took ~6 days in the source data "
                "(a quirk of the public dataset, not a process change). Click for the data dictionary.")


def pipeline(p):
    kpis(p, [("Open Deals", NONE, 0), ("Open Pipeline", M, 2), ("Stale Deals", NONE, 0), ("Stale Pipeline", M, 2),
             ("Weighted Forecast", M, 2), ("Open Without Account", NONE, 0)])
    a, b = cols(880, 424)
    p.add("line_snapshots", "lineChart", a, ROW2_Y, 880, ROW2_H,
          roles={"Category": "Date.Month Start",
                 "Y": ["_Measures.Snapshot Open Pipeline", "_Measures.Snapshot Stale Pipeline"]},
          title="Open pipeline at each month-end sync", subtitle="List value; the stale part grows every month",
          objects={**axes(), "legend": legend(), "lineStyles": props(strokeWidth=2, showMarker=True, markerSize=3),
                   "labels": bar_labels(M),
                   "dataPoint": [measure_color("_Measures.Snapshot Open Pipeline", OPEN),
                                 measure_color("_Measures.Snapshot Stale Pipeline", STALE)],
                   # Analytics pane: linear trend per series over the month-end snapshots.
                   "trend": props(show=True, displayName="Trend", lineColor=color(INK2), style="dashed",
                                  combineSeries=False)})
    slicer(p, "slicer_stale", b, ROW2_Y, 204, 80, "Stale Threshold.Stale Threshold", default=104,
           title="What-if: stale after (days)")
    slicer(p, "slicer_winprob", b + 220, ROW2_Y, 204, 80, "Win Probability.Win Probability",
           title="What-if: win probability")
    textbox(p, "whatif_note", b + 8, ROW2_Y + 96, 408, ROW2_H - 96,
            ("How to read it", 10, INK, True),
            ("Stale = an engaging deal older than the threshold. 104 days is how long 90% of closed deals took; "
             "past that, a deal rarely closes. Weighted forecast = live (non-stale) pipeline × win probability × "
             "historical price realisation. Leave win probability empty to use the historical win rate.",
             9, INK2, False))
    info_button(p, "info_stale", X1 - 40, ROW2_Y + 98,
                "Stale = an engaging deal older than 104 days since engage. 90% of closed deals closed within "
                "104 days (P90 of the cycle). Click for the data dictionary.")
    a, b = cols(640, 664)
    bar(p, "col_age", "clusteredColumnChart", a, ROW3_Y, 640, ROW3_H, "Age Band.Age Band", "_Measures.Open Pipeline",
        OPEN, "Open pipeline by deal age", "Click a band to filter the agent table; the KPI cards stay put")
    # Two tables share one slot; the bookmarks 'All open' / 'Stale only' swap which group is visible.
    ty, th = ROW3_Y + 40, ROW3_H - 40
    button(p, "btn_stale", b, ROW3_Y, 120, 32, "Stale only", {"type": "Bookmark", "bookmark": "bm_stale"},
           alt="Show the stale-deal table (bookmark)", filled=True)
    button(p, "btn_all", b + 128, ROW3_Y, 120, 32, "All open", {"type": "Bookmark", "bookmark": "bm_all"},
           alt="Show all open deals by agent (bookmark)")
    stale_bar = {"columnFormatting": [data_bar("_Measures.Stale Share", STALE)]}
    table(p, "table_stale_agents", b, ty, 664, th,
          ["Agent.Sales Agent", "Agent.Manager", "_Measures.Open Deals", "_Measures.Stale Deals",
           "_Measures.Stale Pipeline", "_Measures.Stale Share"],
          "Who holds the stale deals", "Right-click an agent → Drill through → Deal Detail for the call list",
          sort=("_Measures.Stale Pipeline", "Descending"), objects=stale_bar, group="grp_stale")
    table(p, "table_open_agents", b, ty, 664, th,
          ["Agent.Sales Agent", "Agent.Manager", "_Measures.Open Deals", "_Measures.Open Pipeline",
           "_Measures.Weighted Forecast", "_Measures.Stale Share"],
          "Open book by agent", "Everything still open; weighted forecast uses the what-if win probability",
          sort=("_Measures.Open Pipeline", "Descending"), objects=stale_bar, group="grp_open")
    p.group("grp_stale", "Stale table")
    p.group("grp_open", "All-open table", hidden=True)
    p.bookmarks = [("bm_stale", "Stale only", {"grp_stale": False, "grp_open": True}),
                   ("bm_all", "All open", {"grp_stale": True, "grp_open": False})]
    # Page filter: only open deals (Stage[Is Open]); locked so readers cannot clear it. The few measures that
    # need closed deals (win rate, price realisation in the forecast) remove the Stage filter themselves.
    p.meta["filterConfig"] = {"filters": [bool_filter("open_only", "Stage.Is Open")]}
    # Edit interactions: the age chart filters both agent tables but leaves the KPI cards alone.
    p.meta["visualInteractions"] = (
        [{"source": "col_age", "target": t, "type": "DataFilter"} for t in ("table_stale_agents", "table_open_agents")]
        + [{"source": "col_age", "target": f"kpi_{i}", "type": "NoFilter"} for i in range(6)])
    p.mobile = {"title": (0, 0, 100, 7), **{f"kpi_{i}": (50 * (i % 2), 8 + 10 * (i // 2), 50, 9) for i in range(6)},
                "line_snapshots": (0, 38, 100, 20), "col_age": (0, 59, 100, 18), "table_stale_agents": (0, 78, 100, 22)}


def team(p):
    kpis(p, [("Won Revenue", M, 2), ("Win Rate", NONE, 1), ("Avg Deal Size", NONE, 0), ("Avg Cycle Days", NONE, 0)])
    a, b = cols(560, 744)
    bar(p, "bar_manager", "clusteredBarChart", a, ROW2_Y, 560, ROW2_H, "Agent.Manager", "_Measures.Won Revenue", WON,
        "Won revenue by manager", sort=("_Measures.Won Revenue", "Descending"))
    table(p, "table_manager", b, ROW2_Y, 744, ROW2_H,
          ["Agent.Region", "Agent.Manager", "_Measures.Won Revenue", "_Measures.Won Deals", "_Measures.Win Rate",
           "_Measures.Win Rate vs Region", "_Measures.Avg Deal Size", "_Measures.Open Pipeline"],
          "Manager scorecard", "Win rates sit within two points of each other; team size and deal mix drive revenue. "
          "Rank is a visual calculation; vs region (green above, red below) uses ALLEXCEPT",
          sort=("_Measures.Won Revenue", "Descending"),
          objects={"values": [font_by_measure("_Measures.Win Rate vs Region", "_Measures.Win Rate vs Region Color")]})
    p.visuals[-1]["visual"]["query"]["queryState"]["Values"]["projections"].insert(
        0, vcalc("Rank", "RANK ( DENSE, ORDERBY ( [Won Revenue], DESC ) )"))
    bar(p, "col_agents", "clusteredColumnChart", X0, ROW3_Y, CW, ROW3_H, "Agent.Sales Agent", "_Measures.Won Revenue",
        WON, "Won revenue by sales agent", "Report filter Has Deals = true hides the five agents with no deals. "
        "Hover a bar for the agent tooltip", sort=("_Measures.Won Revenue", "Descending"), container=TOOLTIP_AGENT)


def products(p):
    kpis(p, [("Won Revenue", M, 2), ("Avg Deal Size", NONE, 0), ("Price Realisation", NONE, 1), ("Win Rate", NONE, 1)])
    a, b = cols(560, 744)
    bar(p, "bar_product", "clusteredBarChart", a, ROW2_Y, 560, ROW2_H, "Product.Product", "_Measures.Won Revenue", WON,
        "Won revenue by product", sort=("_Measures.Won Revenue", "Descending"))
    table(p, "table_product", b, ROW2_Y, 744, ROW2_H,
          ["Product.Product", "Product.Series", "Product.List Price", "_Measures.Won Deals", "_Measures.Win Rate",
           "_Measures.Price Realisation", "_Measures.Won Revenue"],
          "Product scorecard", "Price realisation = won revenue ÷ list price; deals close both above and below list",
          sort=("_Measures.Won Revenue", "Descending"))
    bar(p, "bar_sector", "clusteredBarChart", a, ROW3_Y, 560, ROW3_H, "Account.Sector", "_Measures.Won Revenue", WON,
        "Won revenue by sector", sort=("_Measures.Won Revenue", "Descending"))
    table(p, "table_accounts", b, ROW3_Y, 744, ROW3_H,
          ["Account.Account", "_Measures.Account ABC", "Account.Sector", "Account.Country", "Account.Parent Company",
           "_Measures.Won Revenue", "_Measures.Win Rate", "_Measures.Revenue Share"],
          "Accounts", "ABC icon: A = first 80% of won revenue, B = next 15%, C = rest. Parent company groups subsidiaries",
          sort=("_Measures.Won Revenue", "Descending"),
          objects={"values": [icon_rules("_Measures.Account ABC",
                                         [("A", "CircleHigh"), ("B", "CircleMedium"), ("C", "CircleLow")])]})


def sync(p):
    kpis(p, [("Sync Count", NONE, 0), ("API Requests", NONE, 0), ("Throttled Requests", NONE, 0),
             ("Server Error Responses", NONE, 0), ("Reconciliation Gaps", NONE, 0), ("Login Mismatches", NONE, 0)])
    a, c, b = cols(420, 448, 420)
    p.add("col_versions", "clusteredColumnChart", a, ROW2_Y, 420, ROW2_H,
          roles={"Category": "Sync Runs.Sync Date", "Y": ["_Measures.Deal Versions Loaded"]},
          title="Deal versions loaded per sync", subtitle="First run is a full load; the rest pull only what changed",
          objects={**axes(), "labels": bar_labels(NONE), "dataPoint": [measure_color("_Measures.Deal Versions Loaded", ACCENT)]})
    # Sync Run Events is Sync Runs unpivoted in Power Query: one row per run and response outcome.
    p.add("col_run_events", "columnChart", c, ROW2_Y, 448, ROW2_H,
          roles={"Category": "Sync Run Events.Sync Date", "Series": "Sync Run Events.Metric", "Y": "_Measures.Run Responses"},
          title="API responses per sync", subtitle="Stacked: answered first time, 429 and 5xx add up to the requests sent",
          objects={**axes(), "legend": legend()})
    textbox(p, "sync_note", b + 8, ROW2_Y + 8, 408, ROW2_H - 16,
            ("How the extract works", 11, INK, True),
            ("Reference objects (owners, companies, products) reload in full. Deals are pulled with a "
             "modified-since search from the last watermark, and every new version is kept, so the warehouse "
             "can rebuild the pipeline as it stood at each sync.", 9, INK2, False),
            ("429 responses wait for Retry-After; 5xx responses back off exponentially; other 4xx fail fast. "
             "Power BI then refreshes incrementally: only partitions whose latest modified date changed.", 9, INK2, False))
    a, b = cols(560, 744)
    # Question 4, can the data be trusted: Power Query appends the seed CSV totals to the mart totals and merges.
    table(p, "table_recon", a, ROW3_Y, 560, ROW3_H,
          ["Source Reconciliation.Source", "Source Reconciliation.Stage", "Source Reconciliation.Deal Rows",
           "Source Reconciliation.Close Value", "Source Reconciliation.Deals Diff", "Source Reconciliation.Value Diff"],
          "Seed CSV vs warehouse, per stage", "Power Query: Group By, Append, Merge. Zero difference = nothing lost in the API syncs",
          sort=("Source Reconciliation.Stage", "Ascending"))
    table(p, "table_runs", b, ROW3_Y, 744, ROW3_H,
          ["Sync Runs.Sync Date", "Sync Runs.Mode", "Sync Runs.Requests", "Sync Runs.Rate Limited 429",
           "Sync Runs.Server Errors 5xx", "Sync Runs.Deals Seen", "Sync Runs.Versions Added", "Sync Runs.Run Seconds"],
          "Run log", sort=("Sync Runs.Sync Date", "Ascending"))


def trends(p):
    kpis(p, [("Won Revenue Compact", NONE, 0), ("Best Month", NONE, 0), ("Best Month Revenue", K, 0),
             ("Avg Deal Size", NONE, 0)])
    a, b = cols(880, 424)
    series = ["Cumulative won", "Moving average, 3 months"]
    p.add("line_running", "lineChart", a, ROW2_Y, 880, ROW2_H,
          roles={"Category": "Date.Month Start", "Y": ["_Measures.Won Revenue", "_Measures.Revenue Trend Forecast"]},
          title="Won revenue per month, running total, 3-month average and linear trend",
          subtitle="Visual calculations: RUNNINGSUM and MOVINGAVERAGE. Trend = LINESTX on 10 months; Jan–Mar 2018 is a projection",
          objects={**axes(), "legend": legend(), "lineStyles": props(strokeWidth=2, showMarker=True, markerSize=3),
                   "dataPoint": [measure_color("_Measures.Won Revenue", WON), measure_color(series[0], ACCENT),
                                 measure_color(series[1], OPEN), measure_color("_Measures.Revenue Trend Forecast", MUTED)]})
    p.visuals[-1]["visual"]["query"]["queryState"]["Y"]["projections"] += [
        vcalc(series[0], "RUNNINGSUM ( [Won Revenue] )"), vcalc(series[1], "MOVINGAVERAGE ( [Won Revenue], 3 )")]
    table(p, "table_windows", b, ROW2_Y, 424, ROW2_H,
          ["Date.Month Start", "_Measures.Won Revenue", "_Measures.Prev Month Revenue", "_Measures.MoM Change",
           "_Measures.Rolling 3M Revenue"],
          "Window functions, month by month", "OFFSET = previous month, WINDOW = this + two before",
          sort=("Date.Month Start", "Ascending"))
    a, b = cols(820, 484)
    p.add("line_multiples", "lineChart", a, ROW3_Y, 820, ROW3_H,
          roles={"Category": "Date.Month Start", "Y": ["_Measures.Won Revenue"], "Rows": "Agent.Manager"},
          title="Won revenue per month, one panel per manager",
          subtitle="Small multiples share one axis: the teams rise and fall together",
          objects={**axes(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color("_Measures.Won Revenue", WON)],
                   "smallMultiplesLayout": props(layoutType="custom", rowCount=2, columnCount=3)})
    p.add("matrix_periods", "pivotTable", b, ROW3_Y, 484, ROW3_H,
          roles={"Rows": "Date.Quarter", "Columns": "Time Intelligence.Period", "Values": "_Measures.Won Revenue"},
          title="Won revenue by quarter, four ways",
          subtitle="Columns come from the Time Intelligence calculation group on the Gregorian calendar; "
                   "MoM % switches its own format string",
          objects={"grid": props(rowPadding=6)},
          filters=[visual_filter("periods", "Time Intelligence.Period", ["Current", "QTD", "YTD", "MoM %"])])


def drivers(p):
    kpis(p, [("Win Rate", NONE, 1), ("Closed Deals", NONE, 0), ("Won Revenue", M, 2), ("Avg Deal Size", NONE, 0)])
    a, b = cols(880, 424)
    p.add("decomp_revenue", "decompositionTreeVisual", a, ROW2_Y, 880, ROW2_H,
          roles={"Analyze": "_Measures.Won Revenue",
                 "ExplainBy": ["Agent.Region", "Agent.Manager", "Product.Product", "Account.Sector", "Account.Size Band"]},
          title="Where the won revenue comes from",
          subtitle="Decomposition tree: split by region, manager, product, sector or account size in any order",
          objects={"tree": props(barsPerLevel=6, density="default", connectorType="round", accentColor=color(WON)),
                   "dataBars": props(positiveBarColor=color(WON))})
    table(p, "table_m2m", b, ROW2_Y, 424, ROW2_H,
          ["Steps.Step", "_Measures.Funnel Deals", "_Measures.Step Conversion", "_Measures.Value Reaching Step",
           "_Measures.Avg Value Reaching Step"],
          "The funnel as a many-to-many bridge",
          "Steps filters deals through Funnel (CROSSFILTER both); conversion uses OFFSET",
          sort=("Steps.Step", "Ascending"))
    a, c, b = cols(470, 380, 438)
    p.add("key_influencers", "keyDriversVisual", a, ROW3_Y, 470, ROW3_H,
          roles={"Target": "Stage.Stage",
                 "ExplainBy": ["Product.Product", "Account.Sector", "Agent.Region", "Account.Size Band",
                               "Cycle Band.Cycle Band"]},
          title="What makes a deal more likely to be won", subtitle="Key influencers on closed deals (won vs lost)",
          objects={"keyDrivers": props(selectedAnalysis="keyInfluencers", targetValue="Won", selectedSort="impact",
                                       allowKeyDrivers=True, allowProfiles=True, countType="relative")},
          filters=[visual_filter("closed", "Stage.Stage", ["Won", "Lost"])])
    # AppSource custom visual (Microsoft Sankey, registered in report.json publicCustomVisuals).
    p.add("sankey_flow", SANKEY, c, ROW3_Y, 380, ROW3_H,
          roles={"Source": "Stage Flow.From", "Destination": "Stage Flow.To", "Weight": "_Measures.Flow Deals"},
          title="Where deals go", subtitle="Created → engaged → won, lost or still open (Sankey custom visual)")
    p.add("matrix_parent", "pivotTable", b, ROW3_Y, 438, ROW3_H,
          roles={"Rows": ["Agent.Region", "Agent.Manager", "Agent.Sales Agent"],
                 "Values": ["_Measures.Won Revenue", "_Measures.Share of Parent", "_Measures.Win Rate"]},
          title="Share of parent: region → manager → agent",
          subtitle="ISINSCOPE picks the level: each row is a share of the row above. Expand a region to drill",
          objects={"grid": props(rowPadding=5)})


def account_value(p):
    kpis(p, [("Won Revenue", M, 2), ("Active Accounts", NONE, 0), ("Accounts to 80%", NONE, 0),
             ("Share of Accounts to 80%", NONE, 0), ("Top 10 Accounts Revenue", M, 2)])
    a, b = cols(880, 424)
    # Pareto: columns = revenue per account (largest first), line = cumulative share (WINDOW) on the second axis.
    p.add("combo_pareto", "lineClusteredColumnComboChart", a, ROW2_Y, 880, ROW2_H,
          roles={"Category": "Account.Account", "Y": "_Measures.Won Revenue", "Y2": "_Measures.Account Cumulative Share"},
          title="Pareto: won revenue per account and cumulative share",
          subtitle="Largest account first; the line crosses 80% at the last A account. Follows the year / region / product slicers",
          sort=("_Measures.Won Revenue", "Descending"),
          objects={**axes(), "legend": legend(), "dataPoint": [measure_color("_Measures.Won Revenue", WON),
                                                             measure_color("_Measures.Account Cumulative Share", STALE)],
                   "lineStyles": props(strokeWidth=2, showMarker=False)})
    table(p, "table_abc", b, ROW2_Y, 424, ROW2_H,
          ["Account.Account", "_Measures.Won Revenue", "_Measures.Account Cumulative Share", "_Measures.Account ABC"],
          "ABC class per account", "A = first 80% of revenue, B = next 15%, C = rest; recomputed for the slicer selection",
          sort=("_Measures.Won Revenue", "Descending"),
          objects={"values": [icon_rules("_Measures.Account ABC",
                                         [("A", "CircleHigh"), ("B", "CircleMedium"), ("C", "CircleLow")])],
                   "columnFormatting": [data_bar("_Measures.Account Cumulative Share", WON)]})
    a, b = cols(640, 664)
    bar(p, "col_segment", "clusteredColumnChart", a, ROW3_Y, 640, ROW3_H, "Account Segment.Segment",
        "_Measures.Accounts in Segment", WON, "Accounts per revenue band",
        "Disconnected Account Segment table: an account's band is decided at query time, so it moves with the slicers",
        units=NONE, sort=("Account Segment.Segment", "Ascending"))
    table(p, "table_segment", b, ROW3_Y, 664, ROW3_H,
          ["Account Segment.Segment", "_Measures.Accounts in Segment", "_Measures.Segment Revenue"],
          "Revenue bands", "Dynamic segmentation: same bands, recounted for whatever is selected",
          sort=("Account Segment.Segment", "Ascending"))


Q4, OCT = ["Q4 2017"], ["Oct 2017"]


def diagnosis(p):
    """Walkthrough of one drop (Sep → Oct 2017, Q3 → Q4): what moved, why, where, and who to call."""
    w = (CW - GAP * 5) // 6
    for i, (m, units) in enumerate([("Won Revenue PQ", M), ("Won Revenue", M), ("Revenue Change QoQ", K),
                                    ("Volume Effect", K), ("Mix Effect", K), ("Price Effect", K)]):
        p.add(f"kpi_{i}", "card", X0 + i * (w + GAP), ROW_KPI_Y, w, ROW_KPI_H, roles={"Values": f"_Measures.{m}"},
              objects={"labels": props(color=color(INK), fontSize=22.0, fontFamily="Segoe UI Semibold",
                                       labelDisplayUnits=units, labelPrecision=2 if units == M else 0),
                       "categoryLabels": props(show=True, color=color(MUTED), fontSize=9.0)},
              alt=f"{m}, Q4 2017 against Q3 2017", filters=[visual_filter("q4", "Date.Quarter", Q4)])
    a, b, c = cols(520, 400, 368)
    # Step 1: the drop. Step 2: the bridge (total, then per product). Step 3: where. Step 4: who → drillthrough.
    p.add("combo_mom", "lineClusteredColumnComboChart", a, ROW2_Y, 520, ROW2_H,
          roles={"Category": "Date.Month Start", "Y": "_Measures.Won Revenue", "Y2": "_Measures.MoM Change"},
          title="1 · Won revenue and month-on-month change",
          subtitle="Oct 2017 falls 41% after September; Nov and Dec recover",
          objects={**axes(), "legend": legend(), "labels": bar_labels(K),
                   "dataPoint": [measure_color("_Measures.Won Revenue", WON), measure_color("_Measures.MoM Change", STALE)],
                   "lineStyles": props(strokeWidth=2, showMarker=True, markerSize=3)})
    p.add("waterfall_bridge", "waterfallChart", b, ROW2_Y, 400, ROW2_H,
          roles={"Category": "Product.Product", "Y": "_Measures.Revenue Change QoQ"},
          title="2 · Q4 vs Q3 change, by product", subtitle="Waterfall: which products took the quarter down",
          objects={**axes(), "labels": bar_labels(K),
                   "sentimentColors": props(increaseFill=color(WON), decreaseFill=color(STALE), totalFill=color(INK2))},
          filters=[visual_filter("q4", "Date.Quarter", Q4)])
    table(p, "table_bridge", c, ROW2_Y, 368, ROW2_H,
          ["Product.Product", "_Measures.Volume Effect", "_Measures.Mix Effect", "_Measures.Price Effect",
           "_Measures.Bridge Check"],
          "2 · Volume, mix, price per product", "Q4 vs Q3. Price effect near zero = prices held; Bridge Check = 0",
          filters=[visual_filter("q4", "Date.Quarter", Q4)])
    a, b, c = cols(504, 400, 384)
    p.add("decomp_oct", "decompositionTreeVisual", a, ROW3_Y, 504, ROW3_H,
          roles={"Analyze": "_Measures.Revenue Change MoM",
                 "ExplainBy": ["Agent.Region", "Agent.Manager", "Product.Product", "Account.Sector"]},
          title="3 · Where the October drop sits",
          subtitle="Decomposition tree on Revenue Change MoM, Oct 2017 vs Sep 2017",
          objects={"tree": props(barsPerLevel=6, density="default", connectorType="round", accentColor=color(STALE)),
                   "dataBars": props(positiveBarColor=color(WON), negativeBarColor=color(STALE))},
          filters=[visual_filter("oct", "Date.Month", OCT)])
    p.add("matrix_transition", "pivotTable", b, ROW3_Y, 400, ROW3_H,
          roles={"Rows": "Stage Transitions.From Stage", "Columns": "Stage Transitions.To Stage",
                 "Values": "_Measures.Transition Deals"},
          title="3 · Stage at end of Sep → end of Oct",
          subtitle="Transition matrix from the month-end snapshots: how many engaging deals closed, and how",
          objects={"grid": props(rowPadding=6)}, filters=[visual_filter("oct", "Date.Month", OCT)])
    table(p, "table_agent_z", c, ROW3_Y, 384, ROW3_H - 44,
          ["Agent.Sales Agent", "_Measures.Stale Share", "_Measures.Agent Stale Z-Score"],
          "4 · Agents whose open book is stale", "Z-score above 2 = outlier. Select a row, then Open deals",
          sort=("_Measures.Agent Stale Z-Score", "Descending"),
          objects={"columnFormatting": [data_bar("_Measures.Stale Share", STALE)]})
    # Drillthrough button: enabled once an agent row is selected; opens Deal Detail filtered to that agent.
    button(p, "btn_drill", c, ROW3_Y + ROW3_H - 36, 384, 36, "Open deals of the selected agent",
           {"type": "Drillthrough", "drillthroughSection": "deal_detail"},
           alt="Drill through to Deal Detail for the selected agent", filled=True)


def about(p):
    a, b = cols(640, 664)
    textbox(p, "about_text", a, ROW_KPI_Y, 640, 160,
            ("About this report", 12, INK, True),
            ("Data: Maven Analytics CRM Sales Opportunities (public domain), served through a mock CRM API. "
             "The sync history, rate limits and errors are synthetic: produced by the local API simulator.", 9, INK2, False),
            ("Stale = an engaging deal older than 104 days (P90 of engage-to-close). Weighted forecast = live pipeline × "
             "win probability × price realisation.", 9, INK2, False))
    p.add("img_flow", "image", a, ROW_KPI_Y + 176, 640, 330,
          objects={"general": [{"properties": {"imageUrl": {"expr": {"ResourcePackageItem": {
              "PackageName": "RegisteredResources", "PackageType": 1, "ItemName": "dataflow.png"}}}}}]},
          alt="Data flow: CRM API, incremental sync into DuckDB, SQL marts exported to Parquet, "
              "Power BI semantic model and report")
    table(p, "table_dictionary", b, ROW_KPI_Y, 664, H - ROW_KPI_Y - 20,
          ["Data Dictionary.Model Table", "Data Dictionary.Source Field", "Data Dictionary.Description"],
          "Data dictionary", "Loaded by Power Query from data_dictionary.csv (trimmed, mapped to model tables)",
          sort=("Data Dictionary.Model Table", "Ascending"))


def deal_detail(p):
    """Drillthrough target: right-click an agent or account → the open deals behind it."""
    p.meta.update({
        "type": "Drillthrough", "visibility": "HiddenInViewMode",
        "filterConfig": {"filters": [
            {"name": "dt_agent", "field": field("Agent.Sales Agent"), "type": "Categorical", "howCreated": "Drillthrough"},
            {"name": "dt_account", "field": field("Account.Account"), "type": "Categorical", "howCreated": "Drillthrough"}]},
        "pageBinding": {"name": "deal_detail_binding", "type": "Drillthrough", "parameters": [
            {"name": f"p_{k}", "boundFilter": f"dt_{k}", "asAggregation": False, "qnaSingleSelectRequired": False,
             "fieldExpr": field(ref)} for k, ref in (("agent", "Agent.Sales Agent"), ("account", "Account.Account"))]}})
    button(p, "btn_back", X0, ROW_KPI_Y, 96, 32, "Back", {"type": "Back"}, alt="Back to the page you drilled from",
           icon="back")
    kpis_x = X0 + 112
    w = (X1 - kpis_x - 2 * GAP) // 3
    for i, m in enumerate(["Open Deals", "Stale Deals", "Open Pipeline"]):
        p.add(f"kpi_{i}", "card", kpis_x + i * (w + GAP), ROW_KPI_Y, w, ROW_KPI_H, roles={"Values": f"_Measures.{m}"},
              objects={"labels": props(color=color(INK), fontSize=22.0, fontFamily="Segoe UI Semibold",
                                       labelDisplayUnits=NONE, labelPrecision=0),
                       "categoryLabels": props(show=True, color=color(MUTED), fontSize=9.0)})
    table(p, "table_deals", X0, ROW2_Y, CW, H - ROW2_Y - 20,
          ["Opportunities.Opportunity ID", "Agent.Sales Agent", "Account.Account", "Product.Product", "Stage.Stage",
           "Opportunities.Engaged Date", "Opportunities.Age Days", "Opportunities.Open Value"],
          "Open deals: the call list", "Oldest first; anything past 104 days is stale",
          sort=("Opportunities.Age Days", "Descending"),
          objects={"columnFormatting": [data_bar("Opportunities.Age Days", STALE)]},
          filters=[visual_filter("open", "Stage.Is Open", [True])])


def agent_tooltip(p):
    """Report-page tooltip (320 × 240), hidden from navigation; shown on hover over agent / manager bars."""
    p.meta.update({"type": "Tooltip", "visibility": "HiddenInViewMode", "width": 320, "height": 240,
                   "displayOption": "ActualSize"})
    textbox(p, "tt_title", 12, 8, 296, 28, ("Agent at a glance", 11, INK, True))
    for i, (m, units, prec) in enumerate([("Win Rate", NONE, 1), ("Stale Share", NONE, 0), ("Open Deals", NONE, 0)]):
        p.add(f"tt_{i}", "card", 12, 40 + i * 64, 296, 60, roles={"Values": f"_Measures.{m}"},
              objects={"labels": props(color=color(INK), fontSize=16.0, fontFamily="Segoe UI Semibold",
                                       labelDisplayUnits=units, labelPrecision=prec),
                       "categoryLabels": props(show=True, color=color(MUTED), fontSize=9.0)})


BUILDERS = {"overview": overview, "funnel": funnel, "pipeline": pipeline, "team": team, "products": products,
            "sync": sync, "trends": trends, "drivers": drivers, "about": about,
            "account_value": account_value, "diagnosis": diagnosis, "deal_detail": deal_detail,
            "agent_tooltip": agent_tooltip}


# ------------------------------------------------------------ theme & files
THEME = {
    "name": "Sales Pipeline",
    "dataColors": [WON, LOST, OPEN, STALE, ACCENT, "#104281", "#86b6ef", "#4a3aa7", "#e87ba4", "#008300"],
    "foreground": INK, "foregroundNeutralSecondary": INK2, "foregroundNeutralTertiary": MUTED,
    "background": SURFACE, "backgroundLight": PLANE, "backgroundNeutral": HAIR, "tableAccent": WON,
    "good": WON, "neutral": OPEN, "bad": STALE, "null": "#F0EFEC",
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


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


THIN = PBI / "PipelineReview.Report"
# Thin report owned by sales ops: no model of its own, it connects live to the published SalesPipeline model.
THIN_PAGES = ["overview", "pipeline", "team", "about"]
THIN_CONNECTION = ('Data Source="powerbi://api.powerbi.com/v1.0/myorg/Sales Pipeline [Prod]";'
                   'initial catalog=SalesPipeline;access mode=readonly;integrated security=ClaimsToken')


def dataflow_png(path):
    """About-page diagram: the four stages data passes through, drawn with PIL (no external image)."""
    img = Image.new("RGB", (1280, 560), SURFACE)
    d = ImageDraw.Draw(img)
    steps = [("CRM API", "mock CRM API\nincremental sync\nrate-limited"),
             ("DuckDB", "raw deal versions\nSQL marts\nsnapshots, recon"),
             ("Parquet", "one file per mart\nincremental\nrefresh"),
             ("Power BI", "TMDL model\nPBIR report\nRLS / OLS roles")]
    bw, gap, y0, bh = 250, 60, 150, 260
    x = (1280 - 4 * bw - 3 * gap) // 2
    for i, (head, body) in enumerate(steps):
        d.rounded_rectangle([x, y0, x + bw, y0 + bh], radius=24, fill="#FFFFFF", outline=SIDE, width=4)
        d.text((x + 24, y0 + 28), head, fill=INK, font_size=40)
        d.multiline_text((x + 24, y0 + 100), body, fill=INK2, font_size=26, spacing=10)
        if i < 3:
            ax = x + bw + 10
            d.line([ax, y0 + bh // 2, ax + gap - 20, y0 + bh // 2], fill=ACCENT, width=6)
            d.polygon([(ax + gap - 20, y0 + bh // 2 - 14), (ax + gap - 4, y0 + bh // 2), (ax + gap - 20, y0 + bh // 2 + 14)],
                      fill=ACCENT)
        x += bw + gap
    d.text((40, 40), "Data flow", fill=INK, font_size=44)
    d.text((40, 470), "Sync history is synthetic (local API simulator).", fill=INK2, font_size=26)
    d.text((40, 506), "Deal data: the public Maven CRM dataset.", fill=INK2, font_size=26)
    img.save(path, optimize=True)


# Object-level security guard (External Partner role): these columns / tables are invisible to that role, so a
# visual that uses them would break for partners. Static check of the generated JSON, not a 'View as' test.
OLS_COLUMNS = {("Account", "Annual Revenue"), ("Account", "Employees")}
OLS_TABLES = {"Sync Runs", "Sync Run Events", "Agent Login Check"}
OLS_CHECKED_PAGES = {"about", "overview", "funnel", "pipeline", "team", "products", "drivers", "trends", "deal_detail",
                     "agent_tooltip"}


def ols_refs(obj):
    """(entity, property) pairs referenced anywhere in a visual's JSON."""
    out = set()
    if isinstance(obj, dict):
        if "Property" in obj and "Expression" in obj and "SourceRef" in obj["Expression"]:
            out.add((obj["Expression"]["SourceRef"].get("Entity"), obj["Property"]))
        for v in obj.values():
            out |= ols_refs(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= ols_refs(v)
    return out


def bookmark_files(d, built):
    items = []
    for page in built:
        for name, display, hidden in page.bookmarks:
            kids = {grp: [v["name"] for v in page.visuals if v.get("parentGroupName") == grp] for grp in hidden}
            containers = {k: ({"singleVisual": {"display": {"mode": "hidden"}}} if hidden[g] else {})
                          for g, ks in kids.items() for k in ks}
            dump(d / "bookmarks" / f"{name}.bookmark.json", {
                "$schema": f"{SCHEMA}/bookmark/2.1.0/schema.json", "name": name, "displayName": display,
                # Display only: switch which table is visible; keep the reader's filters and selections.
                "options": {"applyOnlyToTargetVisuals": True, "targetVisualNames": list(hidden) + [k for ks in kids.values() for k in ks],
                            "suppressData": True, "suppressActiveSection": False},
                "explorationState": {"version": "1.3", "activeSection": page.id, "sections": {page.id: {
                    "visualContainers": containers,
                    "visualContainerGroups": {g: {"isHidden": h} for g, h in hidden.items()}}}}})
            items.append({"name": name})
    if items:
        dump(d / "bookmarks" / "bookmarks.json", {"$schema": f"{SCHEMA}/bookmarksMetadata/1.0.0/schema.json",
                                                  "items": items})


def write_report(folder, pages, dataset_ref, display, logical):
    shutil.rmtree(folder, ignore_errors=True)
    res = folder / "StaticResources" / "RegisteredResources"
    res.mkdir(parents=True)
    dump(res / "SalesPipeline.json", THEME)
    background_png(res / "canvas.png")
    dataflow_png(res / "dataflow.png")
    dump(folder / ".platform", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Report", "displayName": display},
        "config": {"version": "2.0", "logicalId": logical}})
    dump(folder / "definition.pbir", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": dataset_ref})
    d = folder / "definition"
    dump(d / "version.json", {"$schema": f"{SCHEMA}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"})
    dump(d / "report.json", {
        "$schema": f"{SCHEMA}/report/3.3.0/schema.json",
        "themeCollection": {"customTheme": {
            "name": "SalesPipeline.json", "type": "RegisteredResources",
            "reportVersionAtImport": {"visual": "1.8.95", "report": "2.0.95", "page": "1.3.95"}}},
        # Report-level filter: agents with no deals at all are noise on every page. Locked in reading view.
        "filterConfig": {"filters": [bool_filter("has_deals", "Agent.Has Deals")]},
        "objects": {"outspacePane": [{"properties": {"expanded": lit(False)}}]},
        "publicCustomVisuals": [SANKEY],
        # Export data: summarized only (no underlying rows) for everyone, which covers the External Partner
        # audience. This is report-wide; Power BI has no per-role export setting.
        "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True,
                     "hideVisualContainerHeader": False, "useEnhancedTooltips": True,
                     "exportDataMode": "AllowSummarized"},
        "resourcePackages": [{"name": "RegisteredResources", "type": "RegisteredResources", "items": [
            {"name": "SalesPipeline.json", "path": "SalesPipeline.json", "type": "CustomTheme"},
            {"name": "canvas.png", "path": "canvas.png", "type": "Image"},
            {"name": "dataflow.png", "path": "dataflow.png", "type": "Image"}]}]})
    every = list(pages) + HIDDEN_PAGES
    dump(d / "pages" / "pages.json", {"$schema": f"{SCHEMA}/pagesMetadata/1.1.0/schema.json",
                                      "pageOrder": [p[0] for p in every], "activePageName": pages[0][0]})
    total, built = 0, []
    for pid, name, title, subtitle in every:
        page = Page(pid, name, title, subtitle)
        page.nav = pages
        if pid not in NO_SCAFFOLD:
            scaffold(page)
        BUILDERS[pid](page)
        page.finalize()
        built.append(page)
        if pid in OLS_CHECKED_PAGES:
            for v in page.visuals:
                bad = {r for r in ols_refs(v) if r in OLS_COLUMNS or r[0] in OLS_TABLES}
                assert not bad, f"{pid}/{v['name']} uses OLS-restricted fields {bad}"
        plain = page.meta.get("type") == "Tooltip"
        background = ([{"properties": {"color": color(SURFACE), "transparency": lit(0.0)}}] if plain else
                      [{"properties": {
                          "image": {"image": {"name": lit("canvas.png"),
                                              "url": {"expr": {"ResourcePackageItem": {"PackageName": "RegisteredResources",
                                                                                       "PackageType": 1, "ItemName": "canvas.png"}}},
                                              "scaling": lit("Fit")}},
                          "transparency": lit(0.0)}}])
        dump(d / "pages" / pid / "page.json", {
            "$schema": f"{SCHEMA}/page/2.1.0/schema.json",
            "name": pid, "displayName": name, "displayOption": "FitToPage", "width": W, "height": H,
            **page.meta,
            "objects": {"background": background, "outspace": [{"properties": {"color": color("#E9E8E3")}}]}})
        for v in page.visuals:
            dump(d / "pages" / pid / "visuals" / v["name"] / "visual.json", v)
            if m := page.mobile.get(v["name"]):  # same convention as `pbir visuals mobile`: % of the phone canvas
                x, y, w, h = m
                dump(d / "pages" / pid / "visuals" / v["name"] / "mobile.json", {
                    "$schema": f"{SCHEMA}/visualContainerMobileState/2.4.0/schema.json",
                    "position": {"x": float(x), "y": float(y), "z": v["position"]["z"], "width": float(w),
                                 "height": float(h), "tabOrder": v["position"]["tabOrder"]}})
        total += len(page.visuals)
        print(f"  ✓ page {name:<18} {len(page.visuals):>3} visuals")
    bookmark_files(d, built)
    print(f"  ✓ wrote {folder.relative_to(ROOT)} ({len(every)} pages, {total} visuals)")


RDL = ROOT / "reports" / "paginated" / "Stale Deals.rdl"
RDL_DAX = """EVALUATE
SUMMARIZECOLUMNS (
    Agent[Manager],
    Agent[Sales Agent],
    "Stale Deals", [Stale Deals],
    "Stale Pipeline", [Stale Pipeline],
    "Open Deals", [Open Deals]
)
ORDER BY Agent[Manager], [Stale Pipeline] DESC"""


def write_rdl():
    """Paginated report (RDL 2016): stale deals per agent, grouped by manager, for print / the monthly meeting.
    Data source = the published SalesPipeline semantic model (XMLA, placeholder workspace). Checked as well-formed
    XML only; it has not been opened in Power BI Report Builder."""
    import xml.etree.ElementTree as ET
    from xml.sax.saxutils import escape
    cols = [("Manager", "Agent[Manager]", "2.2in", None), ("Sales Agent", "Agent[Sales Agent]", "2.2in", None),
            ("Open Deals", "[Open Deals]", "1in", "#,0"), ("Stale Deals", "[Stale Deals]", "1in", "#,0"),
            ("Stale Pipeline", "[Stale Pipeline]", "1.4in", "$#,0")]
    fname = lambda c: c.replace(" ", "_")

    def cell(name, value, bold=False, fmt=None):
        style = "<FontWeight>Bold</FontWeight>" if bold else ""
        style += f"<Format>{fmt}</Format>" if fmt else ""
        return (f"<TablixCell><CellContents><Textbox Name=\"{name}\"><CanGrow>true</CanGrow><KeepTogether>true</KeepTogether>"
                f"<Paragraphs><Paragraph><TextRuns><TextRun><Value>{escape(value)}</Value><Style>{style}</Style></TextRun>"
                f"</TextRuns><Style /></Paragraph></Paragraphs><Style><Border><Style>None</Style></Border>"
                f"<BottomBorder><Color>LightGrey</Color><Style>Solid</Style></BottomBorder><PaddingLeft>2pt</PaddingLeft>"
                f"<PaddingTop>2pt</PaddingTop><PaddingBottom>2pt</PaddingBottom></Style></Textbox></CellContents></TablixCell>")

    header = "".join(cell(f"h_{fname(c)}", c, bold=True) for c, *_ in cols)
    detail = "".join(cell(f"d_{fname(c)}", "" if c == "Manager" else f"=Fields!{fname(c)}.Value", fmt=f)
                     for c, _, _, f in cols)
    total = "".join(cell(f"t_{fname(c)}", "=Fields!Manager.Value" if c == "Manager" else
                         ("" if c == "Sales Agent" else f"=Sum(Fields!{fname(c)}.Value)"), bold=True, fmt=f)
                    for c, _, _, f in cols)
    fields = "".join(f"<Field Name=\"{fname(c)}\"><DataField>{escape(src)}</DataField></Field>" for c, src, _, _ in cols)
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<Report xmlns="http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition"
        xmlns:rd="http://schemas.microsoft.com/SQLServer/reporting/reportdesigner">
  <AutoRefresh>0</AutoRefresh>
  <DataSources>
    <DataSource Name="SalesPipeline">
      <ConnectionProperties>
        <DataProvider>PBIDATASET</DataProvider>
        <ConnectString>Data Source=powerbi://api.powerbi.com/v1.0/myorg/Sales Pipeline [Prod];Initial Catalog=SalesPipeline</ConnectString>
      </ConnectionProperties>
      <rd:SecurityType>None</rd:SecurityType>
    </DataSource>
  </DataSources>
  <DataSets>
    <DataSet Name="StaleByAgent">
      <Query>
        <DataSourceName>SalesPipeline</DataSourceName>
        <CommandText>{escape(RDL_DAX)}</CommandText>
      </Query>
      <Fields>{fields}</Fields>
    </DataSet>
  </DataSets>
  <ReportSections>
    <ReportSection>
      <Body>
        <ReportItems>
          <Textbox Name="Title"><CanGrow>true</CanGrow><KeepTogether>true</KeepTogether>
            <Paragraphs><Paragraph><TextRuns><TextRun><Value>Stale deals by manager</Value>
              <Style><FontSize>16pt</FontSize><FontWeight>Bold</FontWeight></Style></TextRun></TextRuns><Style /></Paragraph>
            <Paragraph><TextRuns><TextRun><Value>Engaging deals older than 104 days at the last sync. For the monthly pipeline review.</Value>
              <Style><FontSize>9pt</FontSize><Color>#52514e</Color></Style></TextRun></TextRuns><Style /></Paragraph></Paragraphs>
            <Top>0in</Top><Left>0in</Left><Height>0.7in</Height><Width>7.8in</Width><Style /></Textbox>
          <Tablix Name="StaleTable">
            <TablixBody>
              <TablixColumns>{"".join(f"<TablixColumn><Width>{w}</Width></TablixColumn>" for _, _, w, _ in cols)}</TablixColumns>
              <TablixRows>
                <TablixRow><Height>0.3in</Height><TablixCells>{header}</TablixCells></TablixRow>
                <TablixRow><Height>0.3in</Height><TablixCells>{total}</TablixCells></TablixRow>
                <TablixRow><Height>0.25in</Height><TablixCells>{detail}</TablixCells></TablixRow>
              </TablixRows>
            </TablixBody>
            <TablixColumnHierarchy><TablixMembers>{"<TablixMember />" * len(cols)}</TablixMembers></TablixColumnHierarchy>
            <TablixRowHierarchy>
              <TablixMembers>
                <TablixMember><KeepWithGroup>After</KeepWithGroup><RepeatOnNewPage>true</RepeatOnNewPage></TablixMember>
                <TablixMember>
                  <Group Name="Manager"><GroupExpressions><GroupExpression>=Fields!Manager.Value</GroupExpression></GroupExpressions>
                    <PageBreak><BreakLocation>Between</BreakLocation></PageBreak></Group>
                  <SortExpressions><SortExpression><Value>=Fields!Manager.Value</Value></SortExpression></SortExpressions>
                  <TablixMembers>
                    <TablixMember><KeepWithGroup>After</KeepWithGroup></TablixMember>
                    <TablixMember><Group Name="Details" />
                      <SortExpressions><SortExpression><Value>=Fields!Stale_Pipeline.Value</Value>
                        <Direction>Descending</Direction></SortExpression></SortExpressions></TablixMember>
                  </TablixMembers>
                </TablixMember>
              </TablixMembers>
            </TablixRowHierarchy>
            <DataSetName>StaleByAgent</DataSetName>
            <Filters><Filter><FilterExpression>=Fields!Stale_Deals.Value</FilterExpression><Operator>GreaterThan</Operator>
              <FilterValues><FilterValue DataType="Integer">0</FilterValue></FilterValues></Filter></Filters>
            <Top>0.8in</Top><Left>0in</Left><Height>0.85in</Height><Width>7.8in</Width><Style /></Tablix>
        </ReportItems>
        <Height>2in</Height><Style />
      </Body>
      <Width>7.8in</Width>
      <Page>
        <PageFooter><Height>0.3in</Height><PrintOnFirstPage>true</PrintOnFirstPage><PrintOnLastPage>true</PrintOnLastPage>
          <ReportItems><Textbox Name="PageNo"><CanGrow>true</CanGrow><KeepTogether>true</KeepTogether>
            <Paragraphs><Paragraph><TextRuns><TextRun><Value>="Page " &amp; Globals!PageNumber &amp; " of " &amp; Globals!TotalPages</Value>
              <Style><FontSize>8pt</FontSize></Style></TextRun></TextRuns><Style /></Paragraph></Paragraphs>
            <Top>0in</Top><Left>0in</Left><Height>0.25in</Height><Width>7.8in</Width><Style /></Textbox></ReportItems>
          <Style /></PageFooter>
        <PageHeight>11in</PageHeight><PageWidth>8.5in</PageWidth>
        <LeftMargin>0.35in</LeftMargin><RightMargin>0.35in</RightMargin><TopMargin>0.5in</TopMargin><BottomMargin>0.5in</BottomMargin>
        <Style />
      </Page>
    </ReportSection>
  </ReportSections>
  <ReportParametersLayout><GridLayoutDefinition><NumberOfColumns>4</NumberOfColumns><NumberOfRows>2</NumberOfRows>
  </GridLayoutDefinition></ReportParametersLayout>
  <rd:ReportUnitType>Inch</rd:ReportUnitType>
  <rd:ReportID>3f6c2a91-5b7e-4d08-9c1a-8e2d4b6f7a10</rd:ReportID>
</Report>
"""
    RDL.parent.mkdir(parents=True, exist_ok=True)
    RDL.write_text(xml)
    root = ET.parse(RDL).getroot()  # well-formedness check
    ns = {"r": "http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition"}
    n_cells = len(root.findall(".//r:TablixCell", ns))
    assert n_cells == 3 * len(cols), n_cells
    print(f"  ✓ wrote {RDL.relative_to(ROOT)} (well-formed XML, {n_cells} cells, grouped by manager)")


def validate(folder):
    result = subprocess.run(["pbir", "validate", str(folder), "--all"], capture_output=True, text=True,
                            env={**os.environ, "NO_COLOR": "1", "COLUMNS": "200"})
    print(result.stdout[-3000:], result.stderr[-2000:])
    return any(l.startswith("Valid") for l in result.stdout.splitlines())


def main():
    dump(PBI / "SalesPipeline.pbip", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": "SalesPipeline.Report"}}],
        "settings": {"enableAutoRecovery": True}})
    write_report(REPORT, PAGES, {"byPath": {"path": "../SalesPipeline.SemanticModel"}}, "SalesPipeline",
                 "8b1d4e62-7c3a-4f19-9e05-a2d6c7b3f481")
    ok = validate(REPORT)
    write_rdl()
    write_report(THIN, [p for p in PAGES if p[0] in THIN_PAGES], {"byConnection": {"connectionString": THIN_CONNECTION}},
                 "PipelineReview", "5d0c8a37-2f61-4b9e-8d14-7a3e6c2b9f50")
    dump(PBI / "PipelineReview.pbip", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": "PipelineReview.Report"}}], "settings": {}})
    # A live-connected report has no local model to resolve fields against: schema checks only.
    thin = subprocess.run(["pbir", "validate", str(THIN)], capture_output=True, text=True,
                          env={**os.environ, "NO_COLOR": "1", "COLUMNS": "200"})
    print("PipelineReview (thin):", thin.stdout.strip().splitlines()[-1:] if thin.stdout.strip() else thin.stderr[-500:])
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
