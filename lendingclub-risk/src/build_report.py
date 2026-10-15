"""Generate the Power BI report (PBIR) — 6 designed pages over the LendingClub semantic model.

    uv run python src/build_report.py

Writes powerbi/LendingClub.Report + LendingClub.pbip, then runs `pbir validate`
(schema + field references against the model). Open LendingClub.pbip in Power BI Desktop.

Colours follow a validated palette (see README): one blue ordinal ramp for risk
tier / vintage, the fixed status palette for loan outcomes, and a single
sequential blue for default-rate heatmaps.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
PBI = ROOT / "powerbi"
REPORT = PBI / "LendingClub.Report"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"

W, H = 1600, 900
SIDEBAR = 232
X0, X1, GAP = 256, 1576, 16
CW = X1 - X0

# ---------------------------------------------------------------- palette
INK, INK2, MUTED, HAIR, SURFACE, PLANE = "#0b0b0b", "#52514e", "#898781", "#E6E4DD", "#FCFCFB", "#F4F4F1"
NAVY, NAVY_TEXT = "#0d366b", "#b7d3f6"
BLUE, ORANGE, GRAY = "#2a78d6", "#eb6834", "#C3C2B7"
TIER = {"Prime": "#86b6ef", "Near-prime": "#2a78d6", "Sub-prime": "#104281"}
COHORT_COLORS = ("#9a9890", "#e8a33a", "#1a9e8f", "#2a78d6", "#7b4fc9", "#104281")  # 2010..2015, distinct hues
VINTAGE = {"2008–09 crisis": "#d03b3b", "2010–12": "#9a9890", "2013–14": "#2a78d6", "2015–16": "#7b4fc9", "2017": "#1a9e8f"}  # crisis red, rest distinct hues
OUTCOME = {"Fully Paid": "#0ca30c", "Current": "#2a78d6", "Delinquent": "#fab219", "Defaulted": "#d03b3b"}
HEAT = ("#EEF4FC", "#86b6ef", "#1c5cab")  # sequential blue: low -> high default rate


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
    if isinstance(ref, dict):  # hierarchy level / visual calculation / hidden helper, already built
        return ref
    return {"field": field(ref), "queryRef": ref, "nativeQueryRef": ref.split(".", 1)[1]}


def props(**kw):
    """One formatting object: plain python values become literals; dicts pass through."""
    return [{"properties": {k: (v if isinstance(v, dict) else lit(v)) for k, v in kw.items()}}]


def series_color(ref, value, hex_):
    table, col = ref.split(".", 1)
    return {"properties": {"fill": color(hex_)},
            "selector": {"data": [{"scopeId": {"Comparison": {
                "ComparisonKind": 0,
                "Left": {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": col}},
                "Right": {"Literal": {"Value": f"'{value}'"}}}}}]}}


def measure_color(ref, hex_):
    return {"properties": {"fill": color(hex_)}, "selector": {"metadata": ref}}


def gradient(measure_ref, colors=HEAT):
    lo, mid, hi = colors
    return {"solid": {"color": {"expr": {"FillRule": {
        "Input": field(measure_ref),
        "FillRule": {"linearGradient3": {
            "min": {"color": {"Literal": {"Value": f"'{lo}'"}}},
            "mid": {"color": {"Literal": {"Value": f"'{mid}'"}}},
            "max": {"color": {"Literal": {"Value": f"'{hi}'"}}},
            "nullColoringStrategy": {"strategy": {"Literal": {"Value": "'asZero'"}}}}}}}}}}


WILDCARD = {"data": [{"dataViewWildcard": {"matchingOption": 1}}]}


def in_condition(ref, values, alias="t"):
    table, col = ref.split(".", 1)
    return {"Condition": {"In": {
        "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": alias}}, "Property": col}}],
        "Values": [[{"Literal": {"Value": lit_value(v)}}] for v in values]}}}


def in_filter(ref, values):
    """Categorical 'is one of' filter, usable in a filterConfig or as a slicer's default selection."""
    table, col = ref.split(".", 1)
    return {"Version": 2, "From": [{"Name": "t", "Entity": table, "Type": 0}], "Where": [in_condition(ref, values)]}


def visual_filter(name, ref, values, locked=False):
    f = {"name": name, "field": field(ref), "type": "Categorical", "filter": in_filter(ref, values)}
    return {**f, "isLockedInViewMode": True} if locked else f


def topn_filter(name, ref, by_measure, n):
    """Filters pane 'Top N': keep the n values of ref with the highest by_measure."""
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


def hierarchy(table, name, levels, active=None):
    """Category projections for a drillable model hierarchy; `active` levels are the ones on show."""
    return [{"field": {"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {"SourceRef": {"Entity": table}},
                                                                        "Hierarchy": name}}, "Level": lv}},
             "queryRef": f"{table}.{name}.{lv}", "nativeQueryRef": lv,
             **({"active": True} if lv in (active or levels[:1]) else {})} for lv in levels]


def vcalc(name, expr):
    """Visual calculation: DAX over the visual's own rows, not stored in the model (and not exported)."""
    return {"field": {"NativeVisualCalculation": {"Language": "dax", "Expression": expr, "Name": name}},
            "queryRef": name, "nativeQueryRef": name}


def hidden(ref):
    """A field kept in the visual's query (a visual calculation needs it) but not drawn."""
    return {**projection(ref), "hidden": True}


def ref_line(measure, name, hex_=INK2, style="dashed"):
    """Analytics-pane constant line whose value is a measure (fx): the average / percentile lines."""
    return {"properties": {"show": lit(True), "displayName": lit(name), "value": {"expr": field(measure)},
                           "lineColor": color(hex_), "style": lit(style), "transparency": lit(0.0),
                           "dataLabelShow": lit(True), "dataLabelColor": color(hex_),
                           "dataLabelText": lit("Name"), "dataLabelHorizontalPosition": lit("right")},
            "selector": {"id": name}}


VTYPE_WORDS = {"card": "Card", "slicer": "Slicer", "lineChart": "Line chart", "areaChart": "Area chart",
               "clusteredBarChart": "Bar chart", "clusteredColumnChart": "Column chart", "tableEx": "Table",
               "pivotTable": "Matrix", "waterfallChart": "Waterfall chart", "donutChart": "Donut chart",
               "lineClusteredColumnComboChart": "Column and line chart", "scatterChart": "Scatter chart",
               "filledMap": "Filled map", "cardVisual": "Cards"}


def describe(vtype, roles, objects):
    """Fallback alt text for a visual without a title: what it shows, in words."""
    if vtype == "textbox":
        return " ".join(r["value"] for para in objects["general"][0]["properties"]["paragraphs"] for r in para["textRuns"])
    if vtype == "actionButton":
        t = objects.get("text", [{}])[0].get("properties", {}).get("text", {})
        return "Button: " + t.get("expr", {}).get("Literal", {}).get("Value", "").strip("'") if t else "Button"
    names = [(r if isinstance(r, str) else r["queryRef"]).split(".")[-1] for refs in (roles or {}).values()
             for r in ([refs] if isinstance(refs, (str, dict)) else refs)]
    return f"{VTYPE_WORDS.get(vtype, vtype)}: {', '.join(names)}"


class Page:
    def __init__(self, pid, name, title, subtitle):
        self.id, self.name, self.title, self.subtitle = pid, name, title, subtitle
        self.visuals = []
        self.meta = {}          # extra page.json keys: type, visibility, pageBinding, filterConfig
        self.interactions = []  # (source, target, type) -> page.json visualInteractions
        self.mobile = {}        # visual name -> (x, y, width, height) in % of the phone canvas
        self.bookmarks = []     # (name, displayName, {group: hidden?})

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
        # Accessibility: every visual carries alt text, read by screen readers instead of the chart.
        alt = alt or (f"{title}. {subtitle}" if title and subtitle else title) or describe(vtype, roles, objects or {})
        vc.setdefault("general", props(altText=alt[:250]))
        query = {"queryState": {role: {"projections": [projection(r) for r in
                                                       ([refs] if isinstance(refs, (str, dict)) else refs)]}
                                for role, refs in (roles or {}).items()}}
        if sort:
            ref, direction = sort
            query["sortDefinition"] = {"sort": [{"field": field(ref), "direction": direction}], "isDefaultSort": False}
        visual = {"visualType": vtype, "objects": objects or {}, "visualContainerObjects": vc,
                  "drillFilterOtherVisuals": True, **visual_extra}
        if roles:
            visual["query"] = query
        z = len(self.visuals) * 100
        c = {"$schema": f"{SCHEMA}/visualContainer/2.9.0/schema.json", "name": name,
             "position": {"x": x, "y": y, "z": z, "width": w, "height": h, "tabOrder": z}, "visual": visual}
        if filters:
            c["filterConfig"] = {"filters": filters}
        if group:
            c["parentGroupName"] = group
        if hidden:
            c["isHidden"] = True
        self.visuals.append(c)

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
        """Tab order for keyboard and screen-reader users: title, then the content in reading order (top to
        bottom, left to right), then the header slicers, then navigation / help / reset / brand / footer."""
        def rank(v):
            n, p = v["name"], v["position"]
            if n == "title":
                return (0, 0, 0)
            if n.startswith(("nav_", "help_", "btn_reset")) or n in ("brand", "footer"):
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


def textbox(page, name, x, y, w, h, *runs, panel=False, **kw):
    """panel=True: a white card with a border (help overlay, glossary); otherwise text straight on the canvas."""
    container = {} if panel else {"background": props(show=False), "border": props(show=False),
                                  "padding": props(top=0, bottom=0, left=0, right=0)}
    page.add(name, "textbox", x, y, w, h, objects={"general": [{"properties": {"paragraphs": text_runs(*runs)}}]},
             container=container, **kw)


def button(page, name, x, y, w, h, text, link, alt=None, icon=None, filled=False, dark=False, **kw):
    """Action button; link = visualLink properties (Bookmark / Back). dark = on the navy sidebar."""
    fg = "#FFFFFF" if filled else (NAVY_TEXT if dark else INK2)
    objects = {"text": props(show=bool(text), text=text or " ", fontColor=color(fg), fontSize=10.0,
                             fontFamily="Segoe UI Semibold", horizontalAlignment="left" if dark else "center",
                             leftMargin=16 if dark else 0),
               "fill": props(show=not dark, fillColor=color(BLUE if filled else "#FFFFFF"), transparency=0.0),
               "outline": props(show=True, lineColor=color("#184f95" if dark else HAIR), weight=1.0),
               "shape": props(tileShape="rectangleRounded", rectangleRoundedCurve=8)}
    objects["icon"] = props(show=True, shapeType=icon, lineColor=color(fg)) if icon else props(show=False)
    page.add(name, "actionButton", x, y, w, h, objects=objects, alt=alt or text,
             container={"visualLink": props(show=True, **link), "background": props(show=False),
                        "border": props(show=False)}, **kw)


def slicer(page, name, x, y, w, h, ref, mode="Dropdown", single=False, default=None, header=True, sync=None,
           bare=False, **extra):
    objects = {"data": props(mode=mode), "header": props(show=header, fontColor=color(MUTED), textSize=9.0),
               "selection": props(singleSelect=single, strictSingleSelect=single)}
    if mode == "HorizontalList":
        objects["items"] = props(fontColor=color(INK2), textSize=9.0)
    if default is not None:
        objects["general"] = [{"properties": {"filter": {"filter": in_filter(ref, [default])}}}]
    if sync:
        extra["syncGroup"] = {"groupName": sync, "fieldChanges": True, "filterChanges": True}
    if bare:
        extra["container"] = {"background": props(show=False), "border": props(show=False)}
    page.add(name, "slicer", x, y, w, h, roles={"Values": ref}, objects=objects, **extra)


DENEB = "deneb7E15AEF80B9E4D4F8E12924291ECE89A"  # Deneb (AppSource) custom visual GUID
GRADE_TIP = {"visualTooltip": props(show=True, type="Canvas", section="grade_tip")}
STATE_TIP = {"visualTooltip": props(show=True, type="Canvas", section="state_tip")}


# --------------------------------------------------------- page scaffolding
PAGES = [
    ("overview", "Overview", "Portfolio overview",
     "2.26M LendingClub consumer loans, 2007–2018 — what was lent, what came back, and what was lost."),
    ("risk-return", "Risk & Return", "Risk & return by grade",
     "Did higher interest rates pay for the extra defaults? Rates use matured loans (full term ended), so outcomes are final."),
    ("vintage", "Vintage & Loss", "Vintage curves & loss severity",
     "How fast each cohort of 36-month loans defaulted, and how much was lost when they did."),
    ("borrowers", "Borrower Profile", "Who defaults?",
     "Default rate across borrower attributes captured at origination. Use the tree to drill into any segment."),
    ("geography", "Geography", "Credit risk across the United States",
     "Each square is a state, shaded by lifetime default rate on matured loans. Hover for volume and losses."),
    ("model", "PD Model", "Probability-of-default model",
     "Trained on matured 2010–14 loans, tested out-of-time on 2015 loans, then used to price the live book."),
    ("glossary", "Glossary", "Glossary",
     "What the report's terms mean, and LendingClub's own definition of every field in the source data."),
]
# Not in the navigation: reached by drillthrough (right-click a sub-grade / state / purpose) or by hovering.
HIDDEN_PAGES = [
    ("loan_detail", "Loan Detail", "Live loans behind the selection",
     "Current and delinquent loans only (locked page filter), riskiest expected loss first. Income shown as a band."),
    ("grade_tip", "Grade Tip", "", ""),
    ("state_tip", "State Tip", "", ""),
]
NO_SCAFFOLD = {"grade_tip", "state_tip"}

# "How to read this page": a hidden overlay per page, shown / hidden by two display-only bookmarks.
MATURED = ("Default, loss and ROI figures use matured loans only (the full term has ended), so outcomes are final; "
           "loans issued from 2016 are mostly still running. ")
HELP = {
    "overview": "Top row: the headline numbers for the slicers above. 12M rolling ends at the latest month in the "
                "filter. " + MATURED + "Click a donut slice to highlight the charts below it. The Time Calc buttons "
                "switch the matrix columns (Current, PY, YoY %, YTD, 12M Rolling). In the column chart use the drill "
                "arrows: year → quarter → month. The originations chart adds Power BI's own forecast (shaded band).",
    "risk-return": MATURED + "Scatter: one bubble per sub-grade; the dashed line is the average default rate of the "
                   "sub-grades shown. Hover a bubble for the grade tooltip; right-click → Drill through → Loan Detail. "
                   "ROI columns drill from tier to grade to sub-grade; the dashed line is the 25th percentile. Bottom row: "
                   "prepayment by grade and term, and loans priced outside their sub-grade's usual band.",
    "vintage": "Curves: the share of each cohort of 36-month loans that had defaulted by each month on book. The "
               "Count / Dollar buttons switch between share of loans and share of dollars. Pick a metric above the "
               "table at the bottom to change what it shows. Bottom right: the same cohort curve rebuilt in DAX per issue "
               "year, and the loan-lifecycle funnel. " + MATURED,
    "borrowers": "Pick an attribute above the left chart to regroup it (income, DTI, employment, utilisation, home). "
                 + MATURED + "The tree splits default rate step by step: click + on any bar. Bottom row: DTI segments, ABC "
                 "classes of purpose, and the default-rate change split into grade mix and rate within grade.",
    "geography": MATURED + "Tiles and the map shade each state by default rate; hover a tile for the state tooltip. "
                 "The ranking shows the 15 worst states (Top N filter). Right-click a state → Drill through → Loan "
                 "Detail. Bottom right: Pareto of net credit loss. The Application Type filter in the filters pane applies to "
                 "every page.",
    "model": "The model was trained on matured 2010–14 loans and tested on 2015 loans it never saw. ROC, calibration "
             "and gains read the test set; expected loss reads the live book (current and delinquent loans). Move the "
             "two sliders at the bottom to stress PD and LGD.",
    "glossary": "Report terms on the left. The right table is LendingClub's own data dictionary, loaded with Power "
                "Query; pick a sheet to narrow it.",
}

ROW4_Y, ROW4_H = 896, 316
TALL = 1232
PAGE_H = {"overview": TALL, "risk-return": TALL, "vintage": TALL, "borrowers": TALL, "geography": TALL, "model": TALL}
WALLPAPER = {H: "canvas.png", TALL: "canvas-tall.png"}

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
            ("LENDINGCLUB", 8, NAVY_TEXT, True), ("Credit Risk Lab", 17, "#FFFFFF", True))
    for i, (pid, name, *_) in enumerate(PAGES):
        active = pid == page.id
        page.add(f"nav_{i}", "actionButton", 12, 128 + i * 46, 208, 40,
                 objects={
                     "text": props(show=True, text=name, fontColor=color("#FFFFFF" if active else NAVY_TEXT),
                                   fontSize=11.0, fontFamily="Segoe UI Semibold" if active else "Segoe UI",
                                   horizontalAlignment="left", leftMargin=16),
                     "fill": props(show=active, fillColor=color("#FFFFFF"), transparency=86.0),
                     "outline": props(show=False),
                     "icon": props(show=False),
                     "shape": props(tileShape="rectangleRounded", rectangleRoundedCurve=8),
                 },
                 container={"visualLink": props(show=True, type="PageNavigation", navigationSection=pid),
                            "background": props(show=False), "border": props(show=False)})
    hy = 128 + len(PAGES) * 46 + 16
    if page.id in HELP:
        button(page, "help_open", 12, hy, 208, 36, "How to read this page",
               {"type": "Bookmark", "bookmark": f"bm_help_{page.id}_on"}, icon="information", dark=True,
               alt="Show how to read this page")
        g = f"help_{page.id}"
        textbox(page, "help_text", X1 - 520, ROW_KPI_Y, 520, 260, ("How to read this page", 12, INK, True),
                (HELP[page.id], 10, INK2, False), panel=True, group=g)
        button(page, "help_close", X1 - 96 - 12, ROW_KPI_Y + 260 - 44, 96, 32, "Close",
               {"type": "Bookmark", "bookmark": f"bm_help_{page.id}_off"}, filled=True, group=g,
               alt="Close the help panel")
        page.group(g, "How to read", hidden=True)
        page.bookmarks += [(f"bm_help_{page.id}_on", f"{page.name}: help on", {g: False}),
                           (f"bm_help_{page.id}_off", f"{page.name}: help off", {g: True})]
    button(page, "btn_reset", 12, hy + 44, 208, 36, "↺  Reset filters", {"type": "Bookmark", "bookmark": "bm_reset_filters"},
           dark=True, alt="Button: clear the year, risk tier and term slicers on every page")
    textbox(page, "footer", 24, 790, 190, 90,
            ("Source: Kaggle — LendingClub loan data 2007–2018Q3", 8, NAVY_TEXT, False),
            ("Pipeline: Python · DuckDB SQL · scikit-learn · Power BI", 8, NAVY_TEXT, False))

    textbox(page, "title", X0, 22, 744, 70, (page.title, 20, INK, True), (page.subtitle, 10, INK2, False))
    sx = cols(744, 272, 136, 120)
    slicer(page, "slicer_year", sx[1], 16, 272, 76, "Date.Year", mode="Between", sync="Year")
    slicer(page, "slicer_tier", sx[2], 16, 136, 76, "Grade.Risk Tier", sync="Tier")
    slicer(page, "slicer_term", sx[3], 16, 120, 76, "Loans.Term", sync="Term")


def kpis(page, measures, slots=None, first=0, x=X0, w_total=CW):
    """KPI cards; slots/first leave room at the left of the row (first slots taken by another visual)."""
    n = slots or len(measures)
    w = (w_total - GAP * (n - 1)) // n
    for i, (m, precision) in enumerate(measures, start=first):
        page.add(f"kpi_{i}", "card", x + i * (w + GAP), ROW_KPI_Y, w, ROW_KPI_H, roles={"Values": f"_Measures.{m}"},
                 objects={"labels": props(color=color(INK), fontSize=22.0, fontFamily="Segoe UI Semibold",
                                          labelDisplayUnits=0, labelPrecision=precision),
                          "categoryLabels": props(show=True, color=color(MUTED), fontSize=9.0)})


def bar_labels(fmt_color=INK2):
    return props(show=True, color=color(fmt_color), fontSize=8.0)


def axes(show_x=True, show_y=True, grid=True):
    return {"categoryAxis": props(show=show_x, showAxisTitle=False, fontSize=9.0, labelColor=color(MUTED)),
            "valueAxis": props(show=show_y, showAxisTitle=False, fontSize=9.0, labelColor=color(MUTED),
                               gridlineShow=grid, gridlineColor=color("#ECEBE6"), gridlineStyle="solid")}


def legend(position="Top"):
    return props(show=True, position=position, fontSize=9.0, labelColor=color(INK2), showTitle=False)


# ------------------------------------------------------------------- pages
def overview(p):
    # 3-30-300: headline numbers → the two big stories (originations, cash bridge) → breakdowns → detail tables
    kpis(p, [("Loans Issued", 0), ("Funded Amount", 1), ("Funded 12M Rolling", 1),
             ("Default Rate", 1), ("Net Credit Loss", 1), ("Realised ROI", 1)])
    a, b = cols(880, 424)
    p.add("area_funded", "lineChart", a, ROW2_Y, 880, ROW2_H,
          roles={"Category": "Date.Month Start", "Y": "_Measures.Funded Amount",
                 "Tooltips": ["_Measures.Funded YoY %", "_Measures.Funded YTD", "_Measures.Funded 12M Rolling",
                              "_Measures.Funded QTD", "_Measures.Funded MTD", "_Measures.Funded 3M Avg",
                              "_Measures.Funded Trend"]},
          title="Monthly originations, trend and forecast",
          subtitle="Dotted = trend line; shaded = built-in forecast with its 95% band (analytics pane). Hover for YoY %, "
                   "YTD and the LINESTX trend",
          objects={**axes(),
                   "categoryAxis": props(show=True, showAxisTitle=False, fontSize=9.0, labelColor=color(MUTED), axisType="Scalar"),
                   "dataPoint": [measure_color("_Measures.Funded Amount", BLUE)],
                   "lineStyles": props(strokeWidth=2, showMarker=False),
                   "trend": [{"properties": {"show": lit(True), "style": lit("dotted"), "displayName": lit("Trend"),
                                             "lineColor": color(INK2)}}],
                   # Forecast needs one measure on a continuous date axis: this chart qualifies. Power BI computes it
                   # (exponential smoothing) when the visual renders; length and confidence are set in the pane.
                   "forecast": [{"properties": {"show": lit(True), "displayName": lit("Forecast"),
                                                "lineColor": color(ORANGE), "bandAreaShow": lit(True)}}]})
    p.add("waterfall_cash", "waterfallChart", b, ROW2_Y, 424, ROW2_H,
          roles={"Category": "Cash Bridge.Step", "Y": "_Measures.Cash Bridge Amount"},
          sort=("Cash Bridge.Step", "Ascending"),
          title="Where the money went", subtitle="Matured loans, whole book (the term slicer does not filter it)",
          objects={**axes(), "labels": props(show=True, color=color(INK2), fontSize=8.0, labelDisplayUnits=1000000000),
                   "sentimentColors": props(increaseFill=color("#0ca30c"), decreaseFill=color("#d03b3b"),
                                            totalFill=color(NAVY))})
    a, b, c = cols(430, 440, 418)
    p.add("donut_outcome", "donutChart", a, ROW3_Y, 430, ROW3_H,
          roles={"Category": "Loans.Outcome", "Y": "_Measures.Loans Issued"},
          title="Where every loan ended up", subtitle="Share of loans by status. Click a slice to highlight the charts",
          objects={"legend": legend("Bottom"),
                   "labels": props(show=True, labelStyle="Category, percent of total", fontSize=9.0, color=color(INK2)),
                   "slices": props(innerRadiusRatio=64),
                   "dataPoint": [series_color("Loans.Outcome", k, v) for k, v in OUTCOME.items()]})
    p.add("bar_purpose", "clusteredBarChart", b, ROW3_Y, 440, ROW3_H,
          roles={"Category": "Purpose.Purpose",
                 "Y": ["_Measures.Default Rate", hidden("_Measures.Matured Defaults"),
                       vcalc("Share of defaults", "DIVIDE([Matured Defaults], COLLAPSEALL([Matured Defaults], ROWS))")],
                 "Tooltips": ["_Measures.Purpose ABC", "_Measures.Funded Amount"]},
          sort=("_Measures.Default Rate", "Descending"),
          title="Default rate and share of all defaults, by purpose",
          subtitle="Share = visual calculation (percent of grand total). Hover for the purpose's ABC class by funding",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(), "legend": legend(),
                   "dataPoint": [measure_color("_Measures.Default Rate", BLUE), measure_color("Share of defaults", GRAY)]})
    p.add("col_term_tier", "clusteredColumnChart", c, ROW3_Y, 418, ROW3_H,
          roles={"Category": "Loans.Term", "Y": "_Measures.Default Rate", "Series": "Grade.Risk Tier"},
          title="Default rate by term and tier", subtitle="60-month loans default far more often",
          objects={**axes(show_y=False, grid=False), "legend": legend(), "labels": bar_labels(),
                   "dataPoint": [series_color("Grade.Risk Tier", k, v) for k, v in TIER.items()]})
    a, b = cols(560, 744)
    slicer(p, "slicer_time_calc", a, ROW4_Y, 560, 44, "Time Calc.Time Calc", mode="HorizontalList", header=False,
           bare=True)
    p.add("matrix_time_calc", "pivotTable", a, ROW4_Y + 52, 560, ROW4_H - 52,
          roles={"Rows": "Date.Year", "Columns": "Time Calc.Time Calc", "Values": "_Measures.Funded Amount"},
          title="Funded amount through the Time Calc calculation group",
          subtitle="Pick columns above. 2019 has only Jan–Feb dates and no loans, so YoY shows −100%",
          objects={"subTotals": props(rowSubtotals=False, columnSubtotals=False), "grid": props(rowPadding=3)})
    p.add("col_funded_drill", "lineClusteredColumnComboChart", b, ROW4_Y, 744, ROW4_H,
          roles={"Category": hierarchy("Date", "Calendar", ["Year", "Year Quarter", "Month Year"]),
                 "Y": "_Measures.Funded Amount", "Y2": vcalc("Cumulative funded", "RUNNINGSUM([Funded Amount])")},
          title="Funded per period and running total",
          subtitle="Drill year → quarter → month from the header. Line = visual calculation RUNNINGSUM",
          objects={**axes(), "legend": legend(),
                   "dataPoint": [measure_color("_Measures.Funded Amount", BLUE), measure_color("Cumulative funded", ORANGE)],
                   "lineStyles": props(strokeWidth=2, showMarker=False)})
    others = [f"kpi_{i}" for i in range(6)] + ["area_funded", "waterfall_cash", "donut_outcome", "bar_purpose",
                                               "col_term_tier", "col_funded_drill"]
    # The calculation-group slicer would rewrite every measure on the page; it only drives the matrix.
    p.interactions = ([("slicer_time_calc", v, "NoFilter") for v in others]
                      + [("slicer_term", "waterfall_cash", "NoFilter")]
                      + [("donut_outcome", v, "HighlightFilter") for v in ("bar_purpose", "col_term_tier", "col_funded_drill")])
    p.mobile = {"title": (0, 0, 100, 7), "slicer_year": (0, 8, 50, 8), "slicer_tier": (50, 8, 25, 8),
                "slicer_term": (75, 8, 25, 8),
                **{f"kpi_{i}": (50 * (i % 2), 17 + 9 * (i // 2), 50, 8) for i in range(6)},
                "area_funded": (0, 45, 100, 26), "waterfall_cash": (0, 72, 100, 26)}


def risk_return(p):
    kpis(p, [("Avg Interest Rate", 2), ("Default Rate", 1), ("LGD", 1),
             ("Loss Rate", 2), ("Net Profit", 1), ("Realised ROI", 1)])
    a, b, c = cols(600, 344, 344)
    p.add("scatter_subgrade", "scatterChart", a, ROW2_Y, 600, ROW2_H,
          roles={"Category": "Grade.Sub Grade", "X": "_Measures.Avg Interest Rate", "Y": "_Measures.Default Rate",
                 "Size": "_Measures.Funded Amount", "Series": "Grade.Risk Tier"},
          title="Interest rate vs default rate, 35 sub-grades",
          subtitle="Bubble = funded amount. Dashed = average of the sub-grades shown. Right-click → Loan Detail",
          objects={"categoryAxis": props(show=True, showAxisTitle=True, titleText="Average interest rate",
                                         fontSize=9.0, labelColor=color(MUTED), gridlineShow=False),
                   "valueAxis": props(show=True, showAxisTitle=True, titleText="Default rate", fontSize=9.0,
                                      labelColor=color(MUTED), gridlineColor=color("#ECEBE6")),
                   "legend": legend(), "categoryLabels": props(show=True, color=color(INK2), fontSize=8.0),
                   "bubbles": props(bubbleSize=-10),
                   "y1AxisReferenceLine": [ref_line("_Measures.Sub Grade Avg Default Rate", "Average")],
                   "dataPoint": [series_color("Grade.Risk Tier", k, v) for k, v in TIER.items()]},
          container=GRADE_TIP)
    p.add("col_rate_band", "clusteredColumnChart", b, ROW2_Y, 344, ROW2_H,
          roles={"Category": "Loans.Interest Rate Band", "Y": "_Measures.Default Rate"},
          sort=("Loans.Interest Rate Band", "Ascending"),
          title="Default rate by interest-rate band", subtitle="5-point bands (calculated column on Loans)",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(), "dataPoint": props(fill=color(BLUE))})
    p.add("col_roi", "clusteredColumnChart", c, ROW2_Y, 344, ROW2_H,
          roles={"Category": hierarchy("Grade", "Credit Grade", ["Risk Tier", "Grade", "Sub Grade"], active=["Grade"]),
                 "Y": ["_Measures.Realised ROI", vcalc("Versus previous", "VAR prev = PREVIOUS([Realised ROI]) RETURN IF(ISBLANK(prev), BLANK(), [Realised ROI] - prev)")]},
          title="Realised ROI by grade, and change vs the grade before",
          subtitle="Drill tier ↔ grade ↔ sub-grade. Dashed = 25th percentile of the grades shown",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(), "legend": legend(),
                   "y1AxisReferenceLine": [ref_line("_Measures.Grade ROI P25", "P25")],
                   "dataPoint": [{"properties": {"fill": {"solid": {"color": {"expr": field("_Measures.ROI Color")}}}},
                                  "selector": {**WILDCARD, "metadata": "_Measures.Realised ROI"}},
                                 measure_color("Versus previous", GRAY)]})
    a, b = cols(468, 836)
    p.add("matrix_grade_term", "pivotTable", a, ROW3_Y, 468, ROW3_H,
          roles={"Rows": "Grade.Grade", "Columns": "Loans.Term",
                 "Values": ["_Measures.Default Rate", "_Measures.Default Rate 60M"]},
          title="Default-rate heatmap", subtitle="Grade × term. 60M column uses KEEPFILTERS: blank on 36-month loans",
          objects={"values": [{"properties": {"backColor": gradient("_Measures.Default Rate"),
                                              "fontColor": color(INK)}, "selector": {**WILDCARD, "metadata": "_Measures.Default Rate"}}],
                   "subTotals": props(rowSubtotals=False, columnSubtotals=False),
                   "grid": props(rowPadding=8)},
          container=GRADE_TIP)
    p.add("table_scorecard", "tableEx", b, ROW3_Y, 836, ROW3_H,
          roles={"Values": ["Grade.Grade", "_Measures.Loans Issued", "_Measures.Funded Amount",
                            "_Measures.Avg Interest Rate", "_Measures.Default Rate", "_Measures.Worst Sub Grade",
                            "_Measures.Loss Rate", "_Measures.Net Profit", "_Measures.Realised ROI"]},
          title="Grade scorecard", subtitle="Lifetime outcomes of matured loans; red ROI would mean the grade lost money",
          objects={"values": [{"properties": {"fontColor": {"solid": {"color": {"expr": field("_Measures.ROI Color")}}}},
                               "selector": {**WILDCARD, "metadata": "_Measures.Realised ROI"}}],
                   "grid": props(rowPadding=8)})
    a, b, c = cols(520, 384, 384)
    p.add("deneb_rate_box", DENEB, a, ROW4_Y, 520, ROW4_H,
          roles={"dataset": ["Grade.Grade"] + [f"_Measures.Rate P{q:02d}" for q in (5, 25, 50, 75, 95)]},
          title="Interest-rate spread inside each grade (box plot)",
          subtitle="Deneb (Vega-Lite): whisker P5–P95, box P25–P75, white tick = median. Spec in powerbi/deneb",
          objects={"vega": props(provider="vegaLite", renderMode="svg", jsonSpec=json.dumps(BOXPLOT_SPEC),
                                 jsonConfig="{}", isNewDialogOpen=False)})
    p.add("col_prepay", "clusteredColumnChart", b, ROW4_Y, 384, ROW4_H,
          roles={"Category": "Grade.Grade", "Y": "_Measures.Prepayment Rate", "Series": "Loans.Term"},
          title="Prepayment rate by grade and term",
          subtitle="Matured loans repaid in full before the term ended: the interest the lender never earned",
          objects={**axes(show_y=False, grid=False), "legend": legend(),
                   "dataPoint": [series_color("Loans.Term", "36 months", BLUE), series_color("Loans.Term", "60 months", NAVY)]})
    p.add("card_outliers", "card", c, ROW4_Y, 384, 88, roles={"Values": "_Measures.Pricing Outliers"},
          objects={"labels": props(color=color(INK), fontSize=20.0, fontFamily="Segoe UI Semibold",
                                   labelDisplayUnits=0, labelPrecision=0),
                   "categoryLabels": props(show=True, color=color(MUTED), fontSize=9.0)})
    p.add("table_outliers", "tableEx", c, ROW4_Y + 96, 384, ROW4_H - 96,
          roles={"Values": ["Grade.Sub Grade", "_Measures.Sub Grade Rate P5", "_Measures.Sub Grade Rate P95",
                            "_Measures.Pricing Outliers"]},
          sort=("_Measures.Pricing Outliers", "Descending"),
          title="Loans priced outside their sub-grade's P5–P95 band",
          subtitle="Mostly rate-card changes over the years",
          objects={"grid": props(rowPadding=2)})
    # A matrix can't highlight, so the scatter filters it; the rate-band columns highlight instead.
    p.interactions = [("scatter_subgrade", "matrix_grade_term", "DataFilter"),
                      ("scatter_subgrade", "col_rate_band", "HighlightFilter")]
    p.mobile = {"title": (0, 0, 100, 7), "slicer_year": (0, 8, 50, 8), "slicer_tier": (50, 8, 25, 8),
                "slicer_term": (75, 8, 25, 8),
                **{f"kpi_{i}": (50 * (i % 2), 17 + 9 * (i // 2), 50, 8) for i in range(6)},
                "scatter_subgrade": (0, 45, 100, 28), "col_roi": (0, 74, 100, 25)}


BOXPLOT_SPEC = {
    "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
    "data": {"name": "dataset"},
    "height": "container", "width": "container",
    "encoding": {"x": {"field": "Grade", "type": "nominal", "sort": "ascending",
                       "axis": {"labelAngle": 0, "title": None, "labelColor": MUTED, "domainColor": HAIR}}},
    "layer": [
        {"mark": {"type": "rule", "color": INK2},
         "encoding": {"y": {"field": "Rate P05", "type": "quantitative",
                            "axis": {"format": ".0%", "title": "Interest rate", "labelColor": MUTED,
                                     "gridColor": "#ECEBE6", "titleColor": MUTED}},
                      "y2": {"field": "Rate P95"}}},
        {"mark": {"type": "bar", "size": 36, "color": BLUE, "cornerRadius": 2},
         "encoding": {"y": {"field": "Rate P25", "type": "quantitative"}, "y2": {"field": "Rate P75"}}},
        {"mark": {"type": "tick", "size": 36, "thickness": 2, "color": "#FFFFFF"},
         "encoding": {"y": {"field": "Rate P50", "type": "quantitative"}}},
    ],
}


# Count vs Dollar on the Vintage page: two display-only bookmarks flip which curve chart is visible.
VINTAGE_VIEWS = {"line_vintage": "lineChart", "line_vintage_dollar": "lineChart"}


def view_bookmark(name, display, pid, views, shown):
    return {"name": name, "displayName": display,
            "options": {"targetVisualNames": list(views), "applyOnlyToTargetVisuals": True, "suppressData": True},
            "explorationState": {"version": "1.3", "activeSection": pid, "sections": {pid: {"visualContainers": {
                v: {"singleVisual": {"visualType": t, "objects": {}, **({} if v == shown else {"display": {"mode": "hidden"}})}}
                for v, t in views.items()}}}}}


def vintage(p):
    kpis(p, [("Defaults", 0), ("Exposure at Default", 1), ("Net Recoveries", 1),
             ("Recovery Rate", 1), ("Avg Months to Default", 1), ("Dollar Default Rate", 1),
             ("Peak Cohort Cum Default", 1)])
    a, b = cols(880, 424)
    slicer(p, "slicer_vintage_grade", a, ROW2_Y, 664, 36, "Vintage Curve.Vintage Grade", mode="HorizontalList",
           header=False, bare=True)
    button(p, "btn_count", a + 680, ROW2_Y + 2, 92, 32, "Count", {"type": "Bookmark", "bookmark": "bm_vintage_count"},
           alt="Show the curves as share of loans defaulted")
    button(p, "btn_dollar", a + 780, ROW2_Y + 2, 100, 32, "Dollar", {"type": "Bookmark", "bookmark": "bm_vintage_dollar"},
           alt="Show the curves as share of dollars defaulted")
    for name, prefix, what in (("line_vintage", "Cum Default", "loans"), ("line_vintage_dollar", "Cum Dollar Default", "funded dollars")):
        p.add(name, "lineChart", a, ROW2_Y + 44, 880, ROW2_H - 44,
              roles={"Category": "Vintage Curve.Months on Book", "Y": [f"_Measures.{prefix} {v}" for v in VINTAGE]},
              title=f"Cumulative default rate by months on book — share of {what}",
              subtitle="36-month loans, grouped by issue period. Count / Dollar buttons switch the view",
              objects={**axes(), "legend": legend(),
                       "categoryAxis": props(show=True, showAxisTitle=True, titleText="Months on book", fontSize=9.0,
                                             labelColor=color(MUTED), axisType="Scalar"),
                       "lineStyles": props(strokeWidth=2, showMarker=False),
                       "dataPoint": [measure_color(f"_Measures.{prefix} {k}", v) for k, v in VINTAGE.items()]},
              group="vintage_curves", hidden=name == "line_vintage_dollar")
    p.group("vintage_curves", "Vintage curves")
    p.add("col_lgd", "clusteredColumnChart", b, ROW2_Y, 424, ROW2_H,
          roles={"Category": "Grade.Grade", "Y": "_Measures.LGD"},
          title="Loss given default by grade", subtitle="≈90% of the unpaid balance is lost, whatever the grade",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(), "dataPoint": props(fill=color(BLUE))})
    a, b, c = cols(520, 456, 312)
    p.add("matrix_year_grade", "pivotTable", a, ROW3_Y, 520, ROW3_H,
          roles={"Rows": "Date.Year", "Columns": "Grade.Grade", "Values": "_Measures.Default Rate"},
          title="Default rate by issue year and grade",
          subtitle="Matured loans only — loans issued from 2016 have not reached the end of their term",
          objects={"values": [{"properties": {"backColor": gradient("_Measures.Default Rate"), "fontColor": color(INK)},
                               "selector": {**WILDCARD, "metadata": "_Measures.Default Rate"}}],
                   "subTotals": props(rowSubtotals=False, columnSubtotals=False), "grid": props(rowPadding=4)})
    p.add("line_issued_closed", "lineChart", b, ROW3_Y, 456, ROW3_H,
          roles={"Category": "Date.Month Start", "Y": ["_Measures.Loans Issued", "_Measures.Loans Closed"]},
          title="Loans issued vs closed per month",
          subtitle="Closed = resolved, by month of last payment (inactive relationship)",
          objects={**axes(), "legend": legend(),
                   "categoryAxis": props(show=True, showAxisTitle=False, fontSize=9.0, labelColor=color(MUTED), axisType="Scalar"),
                   "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color("_Measures.Loans Issued", BLUE), measure_color("_Measures.Loans Closed", ORANGE)]})
    p.add("line_book", "areaChart", c, ROW3_Y, 312, ROW3_H,
          roles={"Category": "Date.Month Start", "Y": "_Measures.Book Outstanding"},
          title="Estimated book outstanding", subtitle="Month-end balance, amortisation estimate. Not split by term",
          objects={**axes(), "categoryAxis": props(show=True, showAxisTitle=False, fontSize=9.0, labelColor=color(MUTED),
                                                    axisType="Scalar"),
                   "dataPoint": props(fill=color(BLUE)), "lineStyles": props(strokeWidth=2, showMarker=False)})
    # Field parameter in a table: the PBIR catalog only takes measures in a matrix's Values well and reads the
    # parameter as a column, so the grade × metric view is a table (Desktop would accept the matrix too).
    slicer(p, "slicer_risk_metric", X0, ROW4_Y, 424, 44, "Risk Metric.Risk Metric", mode="HorizontalList", single=True,
           default="Default Rate", header=False, bare=True)
    p.add("table_risk_metric", "tableEx", X0, ROW4_Y + 52, 424, ROW4_H - 52,
          roles={"Values": ["Grade.Grade", "Risk Metric.Risk Metric"]}, sort=("Grade.Grade", "Ascending"),
          title="The metric picked above, by grade",
          subtitle="Risk Metric field parameter: default rate, loss rate, ROI or interest rate",
          objects={"grid": props(rowPadding=4)})
    _, b, c = cols(424, 432, 432)
    years = list(range(2010, 2016))
    p.add("line_cohort_mob", "lineChart", b, ROW4_Y, 432, ROW4_H,
          roles={"Category": "MOB.Month On Book", "Y": [f"_Measures.Cohort {y}" for y in years]},
          title="Cohort curves computed in DAX, by issue year",
          subtitle="Matured 36-month loans defaulted by month on book (MOB table). Pooled 2013–14 = the curve above",
          filters=[visual_filter("mob36", "MOB.Month On Book", list(range(0, 37)))],
          objects={**axes(), "legend": legend(),
                   "categoryAxis": props(show=True, showAxisTitle=False, fontSize=9.0, labelColor=color(MUTED), axisType="Scalar"),
                   "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color(f"_Measures.Cohort {y}", c_) for y, c_ in
                                 zip(years, COHORT_COLORS)]})
    p.add("funnel_lifecycle", "funnel", c, ROW4_Y, 432, ROW4_H,
          roles={"Category": "Funnel Stage.Stage", "Y": "_Measures.Lifecycle Loans"},
          sort=("Funnel Stage.Stage", "Ascending"),
          title="Loan lifecycle funnel",
          subtitle="Issued → term ended → resolved → defaulted → anything recovered. Bar label = % of issued",
          objects={"dataPoint": props(fill=color(BLUE)), "labels": props(show=True, color=color(INK2), fontSize=9.0),
                   "percentBarLabel": props(show=True)})


def borrowers(p):
    kpis(p, [("Avg Annual Income", 0), ("Avg DTI", 1), ("Avg Loan Size", 0),
             ("Active Loans", 0), ("Outstanding Principal", 1), ("Delinquency Rate", 2)])
    a, b = cols(880, 424)
    slicer(p, "slicer_borrower_attribute", a, ROW2_Y, 880, 44, "Borrower Attribute.Borrower Attribute",
           mode="HorizontalList", single=True, default="Income", header=False, bare=True)
    p.add("col_attribute", "clusteredColumnChart", a, ROW2_Y + 52, 880, ROW2_H - 52,
          roles={"Category": "Borrower Attribute.Borrower Attribute", "Y": "_Measures.Default Rate"},
          title="Default rate by the borrower attribute picked above",
          subtitle="Field parameter swaps the axis: income, debt-to-income, employment, card utilisation, home",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(), "dataPoint": props(fill=color(BLUE))})
    p.add("bar_purpose_group", "clusteredBarChart", b, ROW2_Y, 424, ROW2_H,
          roles={"Category": "Purpose.Purpose Group", "Y": "_Measures.Default Rate"},
          sort=("_Measures.Default Rate", "Descending"),
          title="By purpose group", subtitle="Purpose codes grouped into five families",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(), "dataPoint": props(fill=color(BLUE))})
    a, b = cols(880, 424)
    p.add("tree_default", "decompositionTreeVisual", a, ROW3_Y, 880, ROW3_H,
          roles={"Analyze": "_Measures.Default Rate",
                 "ExplainBy": ["Grade.Risk Tier", "Loans.Term", "Loans.Home Ownership", "Loans.Verification Status",
                               "Purpose.Purpose Group", "Loans.Income Band"]},
          title="Decomposition tree — default rate", subtitle="Click + to split by any attribute, or let AI pick the highest value")
    p.add("bar_outcome_home", "hundredPercentStackedBarChart", b, ROW3_Y, 424, ROW3_H,
          roles={"Category": "Loans.Home Ownership", "Y": "_Measures.Loans Issued", "Series": "Loans.Outcome"},
          title="Outcome mix by home ownership", subtitle="Share of loans",
          objects={**axes(show_y=False, grid=False), "legend": legend(),
                   "dataPoint": [series_color("Loans.Outcome", k, v) for k, v in OUTCOME.items()]})
    a, b, c = cols(424, 456, 408)
    p.add("col_dti_segment", "clusteredColumnChart", a, ROW4_Y, 424, ROW4_H,
          roles={"Category": "DTI Segments.DTI Segment", "Y": "_Measures.Default Rate by DTI Segment",
                 "Tooltips": "_Measures.Loans by DTI Segment"},
          sort=("DTI Segments.DTI Segment", "Ascending"),
          title="Default rate by DTI segment",
          subtitle="Bands come from the disconnected DTI Segments table: edit its rows to re-band, no SQL change",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(), "dataPoint": props(fill=color(BLUE))})
    p.add("table_purpose_abc", "tableEx", b, ROW4_Y, 456, ROW4_H,
          roles={"Values": ["Purpose.Purpose", "_Measures.Funded Amount", "_Measures.Cum Funded Share",
                            "_Measures.Purpose ABC"]},
          sort=("_Measures.Funded Amount", "Descending"),
          title="ABC classes of loan purpose, by funded amount",
          subtitle="A = first 80% of dollars, B = to 95%, C = tail. Follows the year / tier / term slicers",
          objects={"grid": props(rowPadding=2)})
    p.add("waterfall_mix_rate", "waterfallChart", c, ROW4_Y, 408, ROW4_H,
          roles={"Category": "Variance Step.Step", "Y": "_Measures.Variance Step Value"},
          sort=("Variance Step.Step", "Ascending"),
          subtitle="Latest matured issue year in the filter vs the year before. Total = change in default rate",
          container={"title": props(show=True, text={"expr": field("_Measures.Variance Title")})},
          alt="Waterfall: change in default rate between two vintage years, split into grade mix and rate within grade",
          objects={**axes(show_y=False, grid=False), "labels": props(show=True, color=color(INK2), fontSize=9.0),
                   "sentimentColors": props(increaseFill=color("#d03b3b"), decreaseFill=color("#0ca30c"),
                                            totalFill=color(NAVY))})


def geography(p):
    kpis(p, [("Loans Issued", 0), ("Funded Amount", 1), ("Default Rate", 1),
             ("Net Credit Loss", 1), ("States Lent In", 0), ("Worst State", 0)])
    # Worst State skips states under 1,000 matured loans (IA, NE, ...): say so on the card
    p.visuals[-1]["visual"]["query"]["queryState"]["Values"]["projections"][0]["displayName"] = "Worst State (1,000+ loans)"
    a, b = cols(652, 652)
    h = ROW3_Y + ROW3_H - ROW2_Y
    p.add("tilemap_state", "scatterChart", a, ROW2_Y, 652, h,
          roles={"Category": "State.State Code", "X": "_Measures.Tile X", "Y": "_Measures.Tile Y"},
          title="Default rate by state — tile grid", subtitle="Darker = more defaults. Tip shows matured loans: under 1,000 the rate is unreliable and Worst State skips it",
          objects={"categoryAxis": props(show=False, gridlineShow=False, start=-0.7, end=11.7),
                   "valueAxis": props(show=False, gridlineShow=False, start=-7.7, end=0.7),
                   "bubbles": props(markerShape="square", bubbleSize=22),
                   "categoryLabels": props(show=True, color=color(INK), fontSize=9.0, bold=True),
                   "dataPoint": [{"properties": {"fill": gradient("_Measures.Default Rate")}, "selector": WILDCARD}],
                   "legend": props(show=False)},
          container=STATE_TIP)
    p.add("filled_map", "filledMap", b, ROW2_Y, 652, h,
          roles={"Category": "State.State Name", "Tooltips": ["_Measures.Default Rate", "_Measures.Funded Amount"]},
          title="Default rate by state — filled map",
          subtitle="State Name carries the StateOrProvince data category; same shading as the tiles",
          objects={"dataPoint": [{"properties": {"fill": gradient("_Measures.Default Rate")}, "selector": WILDCARD}],
                   "legend": props(show=False)})
    a, b = cols(652, 652)
    p.add("bar_state", "clusteredBarChart", a, ROW4_Y, 652, ROW4_H,
          roles={"Category": "State.State Name", "Y": "_Measures.Default Rate",
                 "Tooltips": ["_Measures.State Default Rank", "_Measures.Share of All Defaults", "_Measures.Matured Loans"]},
          sort=("_Measures.Default Rate", "Descending"),
          title="The 15 states with the highest default rate",
          subtitle="Top N filter (15 by default rate). Hover shows matured loans: under 1,000 the rate rests on a small sample",
          filters=[topn_filter("top15", "State.State Name", "_Measures.Default Rate", 15)],
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(),
                   "dataPoint": [{"properties": {"fill": gradient("_Measures.Default Rate")}, "selector": WILDCARD}]})
    # Pareto: both measures are shares, so the line shares the left axis (secondary axis off) and the 80% constant
    # line reads against both.
    p.add("pareto_loss", "lineClusteredColumnComboChart", b, ROW4_Y, 652, ROW4_H,
          roles={"Category": "State.State Code", "Y": "_Measures.Loss Share", "Y2": "_Measures.Cum Loss Share"},
          sort=("_Measures.Loss Share", "Descending"),
          title="Pareto of net credit loss by state",
          subtitle="Bars = state's share of loss, line = cumulative share; dashed = 80%",
          objects={**axes(), "valueAxis": props(show=True, showAxisTitle=False, fontSize=9.0, labelColor=color(MUTED),
                                                gridlineShow=True, gridlineColor=color("#ECEBE6"), secShow=False),
                   "legend": legend(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "y1AxisReferenceLine": [{"properties": {"show": lit(True), "displayName": lit("80%"), "value": lit(0.8),
                                                           "lineColor": color(INK2), "style": lit("dashed"),
                                                           "dataLabelShow": lit(True), "dataLabelColor": color(INK2),
                                                           "dataLabelText": lit("Name")}, "selector": {"id": "p80"}}],
                   "dataPoint": [measure_color("_Measures.Loss Share", BLUE), measure_color("_Measures.Cum Loss Share", ORANGE)]})


GLOSSARY_TERMS = [
    ("Matured loan", "The full term (36 or 60 months) has ended, so the outcome is final. Default, loss and ROI use these only."),
    ("Default", "Charged off or in default. Default rate = defaulted ÷ matured loans."),
    ("LGD", "Loss given default: share of the unpaid balance lost after recoveries."),
    ("Loss rate", "Net credit loss ÷ funded amount, matured loans."),
    ("Realised ROI", "Net profit (interest + fees + recoveries − losses) ÷ funded amount, matured loans."),
    ("Vintage / MOB", "Loans grouped by issue period; months on book counts months since issue."),
    ("PD", "Probability of default from the model (gradient boosting, tested out-of-time on 2015 loans)."),
    ("Expected loss", "Outstanding principal × PD × grade LGD, live loans only — a conservative upper bound."),
    ("Sub-grade", "LendingClub's 35 risk buckets A1–G5; the letter is the grade."),
]


def glossary(p):
    runs = [("Report terms", 12, INK, True)]
    for term, text in GLOSSARY_TERMS:
        runs += [(term, 10, INK, True), (text, 9, INK2, False)]
    textbox(p, "text_terms", X0, ROW_KPI_Y, 424, H - ROW_KPI_Y - 20, *runs, panel=True,
            alt="Report terms: " + "; ".join(f"{t}: {x}" for t, x in GLOSSARY_TERMS))
    b = X0 + 424 + GAP
    slicer(p, "slicer_sheet", b, ROW_KPI_Y, 880, 44, "Field Glossary.Sheet", mode="HorizontalList", header=False,
           bare=True)
    p.add("table_glossary", "tableEx", b, ROW_KPI_Y + 52, 880, H - ROW_KPI_Y - 72,
          roles={"Values": ["Field Glossary.Field Label", "Field Glossary.Field Code", "Field Glossary.Sheet",
                            "Field Glossary.Description"]},
          sort=("Field Glossary.Field Label", "Ascending"),
          title="LendingClub data dictionary", subtitle="LCDataDictionary.xlsx, two sheets appended and cleaned in Power Query",
          objects={"grid": props(rowPadding=3)})


def drillthrough(p, params):
    """Make p a drillthrough target bound to the given (key, column) pairs, with a Back button."""
    p.meta.update({
        "type": "Drillthrough", "visibility": "HiddenInViewMode",
        "filterConfig": {"filters": [{"name": f"dt_{k}", "field": field(ref), "type": "Categorical",
                                      "howCreated": "Drillthrough"} for k, ref in params]},
        "pageBinding": {"name": f"{p.id}_binding", "type": "Drillthrough", "parameters": [
            {"name": f"p_{k}", "boundFilter": f"dt_{k}", "asAggregation": False, "qnaSingleSelectRequired": False,
             "fieldExpr": field(ref)} for k, ref in params]}})
    button(p, "btn_back", X0, ROW_KPI_Y, 96, ROW_KPI_H, "Back", {"type": "Back"}, icon="back",
           alt="Back to the page you drilled from")


def loan_detail(p):
    drillthrough(p, [("subgrade", "Grade.Sub Grade"), ("state", "State.State Name"), ("purpose", "Purpose.Purpose")])
    # Page-level filter: live loans only, locked so a reader can't widen the list to closed loans.
    p.meta["filterConfig"]["filters"].append(visual_filter("live_only", "Loans.Outcome", ["Current", "Delinquent"],
                                                           locked=True))
    kpis(p, [("Active Loans", 0), ("Outstanding Principal", 1), ("Avg PD", 1), ("Expected Loss", 1),
             ("Expected Loss Rate", 1)], x=X0 + 112, w_total=CW - 112)
    # No raw Annual Income here (18-s13): the band is enough to read the loan, and export is summarized-only.
    p.add("table_loans", "tableEx", X0, ROW2_Y, CW, H - ROW2_Y - 20,
          roles={"Values": ["Loans.Loan ID", "Grade.Sub Grade", "Purpose.Purpose", "State.State Code", "Loans.Term",
                            "Loans.Outcome", "Loans.Income Band", "Loans.Interest Rate", "Loans.PD",
                            "Loans.Outstanding Principal Amt", "Loans.Expected Loss Amt"]},
          sort=("Loans.Expected Loss Amt", "Descending"),
          title="Live loans, highest expected loss first",
          subtitle="Filtered by the drillthrough (sub-grade / state / purpose) and the locked live-loan page filter",
          objects={"grid": props(rowPadding=3)})


def tip_page(p, measures):
    """Report-page tooltip (320 × 240), hidden from navigation."""
    p.meta.update({"type": "Tooltip", "visibility": "HiddenInViewMode", "width": 320, "height": 240,
                   "displayOption": "ActualSize"})
    textbox(p, "tt_title", 12, 8, 296, 24, ("At a glance", 11, INK, True))
    for i, (m, prec) in enumerate(measures):
        p.add(f"tt_{i}", "card", 12 + 152 * (i % 2), 36 + 100 * (i // 2), 144, 92, roles={"Values": f"_Measures.{m}"},
              objects={"labels": props(color=color(INK), fontSize=16.0, fontFamily="Segoe UI Semibold",
                                       labelDisplayUnits=0, labelPrecision=prec),
                       "categoryLabels": props(show=True, color=color(MUTED), fontSize=8.0)})


def grade_tip(p):
    tip_page(p, [("Default Rate", 1), ("LGD", 1), ("Realised ROI", 1), ("Avg Interest Rate", 1)])


def state_tip(p):
    tip_page(p, [("Default Rate", 1), ("Matured Loans", 0), ("Net Credit Loss", 1), ("State Default Rank", 0)])


def model(p):
    # AUC / Gini / KS: one card visual, small multiples over the unpivoted Model Metrics Long table
    w3 = (CW - GAP * 5) // 6 * 3 + 2 * GAP
    p.add("cards_metrics", "cardVisual", X0, ROW_KPI_Y, w3, ROW_KPI_H,
          roles={"Data": "_Measures.Metric Value", "Rows": "Model Metrics Long.Metric"},
          sort=("Model Metrics Long.Metric", "Ascending"),
          objects={"smallMultiplesLayout": props(style="Cards", columnCount=3, rowCount=1, autoGrid=False),
                   "value": props(fontSize=22.0, fontColor=color(INK), fontFamily="Segoe UI Semibold", labelPrecision=3),
                   "label": props(show=False)},
          container={"background": props(show=False), "border": props(show=False)})
    kpis(p, [("Top Decile Capture", 1), ("Top Decile Lift", 2), ("Expected Loss", 1)], slots=6, first=3)
    a, b, c = cols(424, 456, 408)
    p.add("line_roc", "lineChart", a, ROW2_Y, 424, ROW2_H,
          roles={"Category": "Model ROC.False Positive Rate",
                 "Y": ["_Measures.ROC True Positive Rate", "_Measures.ROC Random Baseline"]},
          title="ROC curve", subtitle="2015 out-of-time test set, 194k matured loans",
          objects={**axes(), "legend": legend(),
                   "categoryAxis": props(show=True, axisType="Scalar", fontSize=9.0, labelColor=color(MUTED)),
                   "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color("_Measures.ROC True Positive Rate", BLUE),
                                 measure_color("_Measures.ROC Random Baseline", GRAY)]})
    p.add("col_calibration", "clusteredColumnChart", b, ROW2_Y, 456, ROW2_H,
          roles={"Category": "Model Calibration.Risk Decile",
                 "Y": ["_Measures.Decile Predicted PD", "_Measures.Decile Observed Default Rate"]},
          sort=("Model Calibration.Risk Decile", "Ascending"),
          title="Calibration by risk decile", subtitle="Predicted vs actual (D01 safest) — slightly under-predicts",
          objects={**axes(), "legend": legend(),
                   "dataPoint": [measure_color("_Measures.Decile Predicted PD", BLUE),
                                 measure_color("_Measures.Decile Observed Default Rate", ORANGE)]})
    p.add("line_gains", "lineChart", c, ROW2_Y, 408, ROW2_H,
          roles={"Category": "Model Calibration.Cum Population", "Y": "_Measures.Cumulative Default Capture"},
          title="Cumulative gains", subtitle="Reject the riskiest X% of loans → catch Y% of defaults",
          objects={**axes(), "categoryAxis": props(show=True, axisType="Scalar", fontSize=9.0, labelColor=color(MUTED)),
                   "lineStyles": props(strokeWidth=2, showMarker=True, markerSize=4),
                   "dataPoint": props(fill=color(BLUE))})
    a, b = cols(560, 744)
    p.add("bar_importance", "clusteredBarChart", a, ROW3_Y, 560, ROW3_H,
          roles={"Category": "Model Importance.Feature", "Y": "_Measures.Feature Importance",
                 "Tooltips": "Model Importance.Feature Description"},
          sort=("_Measures.Feature Importance", "Descending"),
          title="What drives the prediction",
          subtitle="Permutation importance: AUC lost when the feature is shuffled. Hover for LendingClub's field definition",
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(), "dataPoint": props(fill=color(BLUE))})
    p.add("matrix_el", "pivotTable", b, ROW3_Y, 744, ROW3_H,
          roles={"Rows": "Loans.PD Band",
                 "Values": ["_Measures.Active Loans", "_Measures.Outstanding Principal", "_Measures.Avg PD",
                            "_Measures.Expected Loss", "_Measures.Expected Loss Rate"]},
          title="Expected loss on the live book", subtitle="Outstanding principal × lifetime PD × grade LGD — a conservative upper bound",
          objects={"values": [{"properties": {"backColor": gradient("_Measures.Expected Loss Rate"), "fontColor": color(INK)},
                               "selector": {**WILDCARD, "metadata": "_Measures.Expected Loss Rate"}}],
                   "grid": props(rowPadding=8)})
    # What-if stress (10-s16): two single-value sliders over the GENERATESERIES parameter tables.
    a, b = cols(424, 880)
    for i, (name, ref) in enumerate((("slicer_pd_mult", "PD Multiplier.PD Multiplier"), ("slicer_lgd_shock", "LGD Shock.LGD Shock"))):
        slicer(p, name, a, ROW4_Y + i * 84, 424, 76, ref, mode="Single", single=True)
        p.visuals[-1]["visual"]["objects"]["slider"] = props(show=True, color=color(BLUE))
    for i, m in enumerate(("Stressed Expected Loss", "Stress Uplift")):
        p.add(f"kpi_stress_{i}", "card", a + i * 212, ROW4_Y + 168, 212, ROW4_H - 168, roles={"Values": f"_Measures.{m}"},
              objects={"labels": props(color=color(INK), fontSize=20.0, fontFamily="Segoe UI Semibold",
                                       labelDisplayUnits=0, labelPrecision=1),
                       "categoryLabels": props(show=True, color=color(MUTED), fontSize=9.0)})
    p.add("col_stress", "clusteredColumnChart", b, ROW4_Y, 880, ROW4_H,
          roles={"Category": "Grade.Grade", "Y": ["_Measures.Expected Loss (DAX)", "_Measures.Stressed Expected Loss"]},
          title="Expected loss under stress, by grade",
          subtitle="Live book. Sliders: PD × multiplier (capped at 100%), LGD + shock (kept within 0–100%)",
          objects={**axes(), "legend": legend(),
                   "dataPoint": [measure_color("_Measures.Expected Loss (DAX)", GRAY),
                                 measure_color("_Measures.Stressed Expected Loss", ORANGE)]})


BUILDERS = {"overview": overview, "risk-return": risk_return, "vintage": vintage,
            "borrowers": borrowers, "geography": geography, "model": model, "glossary": glossary,
            "loan_detail": loan_detail, "grade_tip": grade_tip, "state_tip": state_tip}


# ------------------------------------------------------------ theme & files
THEME = {
    "name": "Credit Risk Lab",
    "dataColors": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
    "foreground": INK, "foregroundNeutralSecondary": INK2, "foregroundNeutralTertiary": MUTED,
    "background": SURFACE, "backgroundLight": PLANE, "backgroundNeutral": HAIR, "tableAccent": BLUE,
    "good": "#0ca30c", "neutral": "#fab219", "bad": "#d03b3b",
    "maximum": HEAT[2], "center": HEAT[1], "minimum": HEAT[0], "null": "#F0EFEC",
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
                            "fontFamily": "Segoe UI Semibold", "fontSize": 10}],
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
    d.rectangle([0, 0, SIDEBAR - 1, h], fill=NAVY)
    d.rectangle([SIDEBAR - 3, 0, SIDEBAR - 1, h], fill="#1c5cab")
    d.line([24, 112, SIDEBAR - 24, 112], fill="#184f95", width=1)
    d.line([24, 776, SIDEBAR - 24, 776], fill="#184f95", width=1)
    d.line([X0, 99, X1, 99], fill=HAIR, width=1)
    img.save(path, optimize=True)


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def unused_loans_columns():
    """16-s12: Loans columns that no visual, DAX expression, relationship or sort-by uses. Every one of them is
    memory spent on 2.26M rows for nothing, so build_model.py drops them in Power Query (Kept Report Columns)."""
    d = PBI / "LendingClub.SemanticModel" / "definition"
    loans = (d / "tables" / "Loans.tmdl").read_text()
    columns = set(re.findall(r"^\tcolumn '?([^'=\n]+?)'?(?: =.*)?$", loans, re.M))
    used = set()
    for f in [*REPORT.rglob("visual.json"), REPORT / "definition" / "report.json"]:
        used |= set(re.findall(r'"Entity": "Loans"\s*}\s*},\s*"Property": "([^"]+)"', f.read_text()))
    tmdl = "\n".join(f.read_text() for f in d.rglob("*.tmdl"))
    used |= {a or b for a, b in re.findall(r"Loans\[([^\]]+)\]|Loans\.'?([^'\n]+?)'?$", tmdl, re.M)}
    used |= set(re.findall(r"sortByColumn: '?([^'\n]+)'?", loans))
    return sorted(columns - used)


def main():
    shutil.rmtree(REPORT, ignore_errors=True)
    res = REPORT / "StaticResources" / "RegisteredResources"
    res.mkdir(parents=True)
    dump(res / "CreditRiskLab.json", THEME)
    for h, img in WALLPAPER.items():
        background_png(res / img, h)
    (PBI / "deneb").mkdir(exist_ok=True)
    dump(PBI / "deneb" / "rate-boxplot.json", BOXPLOT_SPEC)  # the Deneb spec, readable outside the report

    dump(PBI / "LendingClub.pbip", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": "LendingClub.Report"}}],
        "settings": {"enableAutoRecovery": True}})
    dump(REPORT / ".platform", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Report", "displayName": "LendingClub"},
        "config": {"version": "2.0", "logicalId": "8b2e4d61-3a9f-4c7e-b1d5-6f0a2c9e7d84"}})
    dump(REPORT / "definition.pbir", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": "../LendingClub.SemanticModel"}}})
    d = REPORT / "definition"
    dump(d / "version.json", {"$schema": f"{SCHEMA}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"})
    dump(d / "report.json", {
        "$schema": f"{SCHEMA}/report/3.3.0/schema.json",
        "themeCollection": {"customTheme": {
            "name": "CreditRiskLab.json", "type": "RegisteredResources",
            "reportVersionAtImport": {"visual": "1.8.95", "report": "2.0.95", "page": "1.3.95"}}},
        # Report-level filter: an open Application Type card in the filters pane, on every page.
        "filterConfig": {"filters": [{"name": "app_type", "field": field("Loans.Application Type"), "type": "Categorical"}]},
        "objects": {"outspacePane": [{"properties": {"expanded": lit(False)}}]},
        "publicCustomVisuals": [DENEB],
        # Export: summarized data only. Loan-level rows (income band, PD) never leave the report as underlying data.
        "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True,
                     "hideVisualContainerHeader": False, "useEnhancedTooltips": True,
                     "exportDataMode": "AllowSummarized"},
        "resourcePackages": [{"name": "RegisteredResources", "type": "RegisteredResources", "items": [
            {"name": "CreditRiskLab.json", "path": "CreditRiskLab.json", "type": "CustomTheme"},
            *[{"name": img, "path": img, "type": "Image"} for img in WALLPAPER.values()]]}]})
    every = PAGES + HIDDEN_PAGES
    dump(d / "pages" / "pages.json", {"$schema": f"{SCHEMA}/pagesMetadata/1.1.0/schema.json",
                                      "pageOrder": [p[0] for p in every], "activePageName": PAGES[0][0]})
    bookmarks = [view_bookmark("bm_vintage_count", "Vintage: count", "vintage", VINTAGE_VIEWS, "line_vintage"),
                 view_bookmark("bm_vintage_dollar", "Vintage: dollar", "vintage", VINTAGE_VIEWS, "line_vintage_dollar")]
    # Reset filters: the three synced header slicers back to All on every page (data only, stays on the page).
    header = {"slicer_year": "slicer", "slicer_tier": "slicer", "slicer_term": "slicer"}
    bookmarks.append({"name": "bm_reset_filters", "displayName": "Reset filters",
                      "options": {"suppressDisplay": True, "suppressActiveSection": True},
                      "explorationState": {"version": "1.3", "activeSection": PAGES[0][0], "sections": {
                          pid: {"visualContainers": {v: {"singleVisual": {"visualType": t, "objects": {}}}
                                                     for v, t in header.items()}} for pid, *_ in PAGES}}})

    total = 0
    for pid, name, title, subtitle in every:
        page = Page(pid, name, title, subtitle)
        if pid not in NO_SCAFFOLD:
            scaffold(page)
        BUILDERS[pid](page)
        page.finalize()
        h = PAGE_H.get(pid, H)
        plain = page.meta.get("type") == "Tooltip"
        background = ([{"properties": {"color": color(SURFACE), "transparency": lit(0.0)}}] if plain else
                      [{"properties": {
                          "image": {"image": {"name": lit(WALLPAPER[h]),
                                              "url": {"expr": {"ResourcePackageItem": {"PackageName": "RegisteredResources",
                                                                                       "PackageType": 1, "ItemName": WALLPAPER[h]}}},
                                              "scaling": lit("Fit")}},
                          "transparency": lit(0.0)}}])
        more = {"visualInteractions": [{"source": a, "target": b, "type": t} for a, b, t in page.interactions]} \
            if page.interactions else {}
        dump(d / "pages" / pid / "page.json", {
            "$schema": f"{SCHEMA}/page/2.1.0/schema.json",
            "name": pid, "displayName": name, "displayOption": "FitToPage", "width": W, "height": h,
            **page.meta, **more,
            "objects": {"background": background, "outspace": [{"properties": {"color": color("#E9E8E3")}}]}})
        for v in page.visuals:
            dump(d / "pages" / pid / "visuals" / v["name"] / "visual.json", v)
            if m := page.mobile.get(v["name"]):  # same convention as `pbir visuals mobile`: % of the phone canvas
                x, y, w, hh = m
                dump(d / "pages" / pid / "visuals" / v["name"] / "mobile.json", {
                    "$schema": f"{SCHEMA}/visualContainerMobileState/2.4.0/schema.json",
                    "position": {"x": float(x), "y": float(y), "z": v["position"]["z"], "width": float(w),
                                 "height": float(hh), "tabOrder": v["position"]["tabOrder"]}})
        for bm, display, hide in page.bookmarks:  # display-only: show / hide a group, keep filters
            kids = [v["name"] for v in page.visuals if v.get("parentGroupName") in hide]
            bookmarks.append({
                "name": bm, "displayName": display,
                "options": {"applyOnlyToTargetVisuals": True, "targetVisualNames": list(hide) + kids,
                            "suppressData": True, "suppressActiveSection": False},
                "explorationState": {"version": "1.3", "activeSection": pid, "sections": {pid: {
                    "visualContainers": {k: {} for k in kids},
                    "visualContainerGroups": {g: {"isHidden": x} for g, x in hide.items()}}}}})
        total += len(page.visuals)
        print(f"  ✓ page {name:<18} {len(page.visuals):>3} visuals")
    for b in bookmarks:
        dump(d / "bookmarks" / f"{b['name']}.bookmark.json", {"$schema": f"{SCHEMA}/bookmark/2.1.0/schema.json", **b})
    dump(d / "bookmarks" / "bookmarks.json", {"$schema": f"{SCHEMA}/bookmarksMetadata/1.0.0/schema.json",
                                              "items": [{"name": b["name"]} for b in bookmarks]})
    print(f"  ✓ wrote {REPORT.relative_to(ROOT)} ({len(every)} pages, {total} visuals, {len(bookmarks)} bookmarks)")

    result = subprocess.run(["pbir", "validate", str(REPORT), "--all"], capture_output=True, text=True,
                            env={**__import__("os").environ, "NO_COLOR": "1", "COLUMNS": "200"})
    print(result.stdout[-3000:], result.stderr[-2000:])
    unused = unused_loans_columns()
    print(f"  {'✓' if not unused else '✗'} unused Loans columns: {', '.join(unused) or 'none'}")


if __name__ == "__main__":
    main()
