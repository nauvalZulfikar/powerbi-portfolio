"""Generate the Power BI report (PBIR) — 7 designed pages + 4 hidden ones (3 drillthrough, 1 tooltip) over the
TeamKPI semantic model, plus a paginated report (reports/paginated/Breached Tickets.rdl).

    uv run python src/build_report.py

Writes powerbi/TeamKPI.Report + TeamKPI.pbip, then runs `pbir validate`
(schema + field references against the model). Open TeamKPI.pbip in Power BI Desktop.

Visual calculations (RUNNINGSUM, MOVINGAVERAGE, RANK) live in the visuals, not in the model:
they only make sense on the visual's own axis, so they don't belong in the measure list.

Colour rule: green = on track (SLA ≥ 90%), amber = at risk (75–89%), red = off track, blue = everything else.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
PBI = ROOT / "powerbi"
REPORT = PBI / "TeamKPI.Report"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"

W, H = 1600, 900
SIDEBAR = 232
X0, X1, GAP = 256, 1576, 16
CW = X1 - X0

# ---------------------------------------------------------------- palette
INK, INK2, MUTED, HAIR, SURFACE, PLANE = "#0b0b0b", "#52514e", "#898781", "#E6E4DD", "#FCFCFB", "#F4F4F1"
SIDE, SIDE_TEXT, SIDE_LINE = "#14263a", "#a7b9cc", "#27405a"
# Status colours carry white text in the SLA matrix, so each must reach WCAG AA (4.5 : 1) against white;
# contrast() asserts it at build time. Amber was #C77700 (3.5 : 1) and was darkened for that reason.
ON_C, RISK_C, BEHIND_C = "#1B7F4B", "#A35F00", "#B42318"
ACCENT, TARGET_C = "#2a5d8f", "#9aa7b4"

# Measures hosted outside _Measures (they sit with the table they summarise).
HOSTED_MEASURES = {"Tickets Pulled", "Tickets Counted", "Tickets Excluded", "No Start Recorded", "Check Tickets",
                   "Import Error Rows", "Tickets in Status"}


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
    kind = "Measure" if table == "_Measures" or name in HOSTED_MEASURES else "Column"
    return {kind: {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}}


def projection(ref):
    if isinstance(ref, dict):
        return ref
    return {"field": field(ref), "queryRef": ref, "nativeQueryRef": ref.split(".", 1)[1]}


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
    if exclude:  # "is not one of"
        f["Where"][0] = {"Condition": {"Not": {"Expression": f["Where"][0]["Condition"]}}}
    return {"name": name, "field": field(ref), "type": "Categorical", "filter": f}


def date_filter(name, ref, op, iso):
    """Advanced filter `column <op> date`, op one of <, <=, >, >= (report / page level), locked in reading view."""
    table, col = ref.split(".", 1)
    kind = {"<": 3, "<=": 4, ">": 1, ">=": 2}[op]
    return {"name": name, "field": field(ref), "type": "Advanced", "isLockedInViewMode": True,
            "filter": {"Version": 2, "From": [{"Name": "d", "Entity": table, "Type": 0}], "Where": [{"Condition": {
                "Comparison": {"ComparisonKind": kind,
                               "Left": {"Column": {"Expression": {"SourceRef": {"Source": "d"}}, "Property": col}},
                               "Right": {"Literal": {"Value": f"datetime'{iso}T00:00:00'"}}}}}]}}


def bool_filter(name, ref, value=True):
    """Page-level filter on a boolean column, locked in reading view."""
    return {"name": name, "field": field(ref), "type": "Categorical", "howCreated": "User",
            "filter": in_filter(ref, [value]), "isLockedInViewMode": True}


VTYPE_WORDS = {"card": "Card", "slicer": "Slicer", "lineChart": "Line chart", "barChart": "Stacked bar chart",
               "clusteredBarChart": "Bar chart", "clusteredColumnChart": "Column chart", "tableEx": "Table",
               "pivotTable": "Matrix", "gauge": "Gauge", "waterfallChart": "Waterfall chart",
               "lineClusteredColumnComboChart": "Column and line chart", "funnel": "Funnel"}


def describe(vtype, roles, objects):
    """Fallback alt text for a visual without a title: what it shows, in words."""
    if vtype == "textbox":
        return " ".join(r["value"] for para in objects["general"][0]["properties"]["paragraphs"] for r in para["textRuns"])
    if vtype == "actionButton":
        t = objects.get("text", [{}])[0].get("properties", {}).get("text", {})
        return "Button: " + t.get("expr", {}).get("Literal", {}).get("Value", "").strip("'") if t else "Button"
    names = [(r if isinstance(r, str) else r["queryRef"]).split(".")[-1] for refs in (roles or {}).values()
             for r in ([refs] if isinstance(refs, str) else refs)]
    return f"{VTYPE_WORDS.get(vtype, vtype)}: {', '.join(names)}"


class Page:
    def __init__(self, pid, name, title, subtitle):
        self.id, self.name, self.title, self.subtitle = pid, name, title, subtitle
        self.visuals = []
        self.meta = {}       # extra page.json keys: type, visibility, pageBinding, filterConfig, visualInteractions
        self.mobile = {}     # visual name -> (x, y, width, height) in % of the phone canvas
        self.bookmarks = []  # (name, displayName, {group: hidden?})

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
        """Tab order for keyboard and screen-reader users: title, then KPIs → charts → tables in reading order
        (top to bottom, left to right), then the header slicers, then the navigation."""
        def rank(v):
            n, p = v["name"], v["position"]
            if n == "title":
                return (0, 0, 0)
            if n.startswith(("nav_", "help_")) or n in ("brand", "footer"):
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
    """panel=True: a white card with a border (help overlay); otherwise text straight on the canvas."""
    container = {} if panel else {"background": props(show=False), "border": props(show=False),
                                  "padding": props(top=0, bottom=0, left=0, right=0)}
    page.add(name, "textbox", x, y, w, h, objects={"general": [{"properties": {"paragraphs": text_runs(*runs)}}]},
             container=container, **kw)


def button(page, name, x, y, w, h, text, link, alt=None, icon=None, filled=False, **kw):
    """Action button: link = visualLink properties (PageNavigation / Bookmark / Back / Drillthrough)."""
    objects = {"text": props(show=bool(text), text=text or " ", fontColor=color("#FFFFFF" if filled else INK2),
                             fontSize=10.0, fontFamily="Segoe UI Semibold", horizontalAlignment="center"),
               "fill": props(show=True, fillColor=color(ACCENT if filled else "#FFFFFF"), transparency=0.0),
               "outline": props(show=True, lineColor=color(HAIR), weight=1.0),
               "shape": props(tileShape="rectangleRounded", rectangleRoundedCurve=8)}
    objects["icon"] = props(show=True, shapeType=icon, lineColor=color(INK2)) if icon else props(show=False)
    page.add(name, "actionButton", x, y, w, h, objects=objects, alt=alt or text,
             container={"visualLink": props(show=True, **link), "background": props(show=False),
                        "border": props(show=False)}, **kw)


def hierarchy(table_, hier, levels):
    """Projections for a model hierarchy, every level active (expanded); drill up / down from the visual header."""
    return [{"field": {"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {"SourceRef": {"Entity": table_}},
                                                                      "Hierarchy": hier}}, "Level": lv}},
             "queryRef": f"{table_}.{hier}.{lv}", "nativeQueryRef": f"{hier} {lv}", "active": True} for lv in levels]


# Report-page tooltip: hovering a bar shows the hidden Team Tip page instead of the default tooltip.
TEAM_TIP = {"visualTooltip": props(show=True, type="Canvas", section="team_tip")}
DENEB = "deneb7E15AEF80B9E4D4F8E12924291ECE89A"  # Deneb (AppSource) custom visual GUID


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
    ("overview", "Overview", "Engineering delivery",
     "Four Apache teams' Jira tickets, Oct 2024 – Sep 2026. Everything is as of 30 Sep 2026."),
    ("flow", "Flow & Speed", "Flow & speed",
     "How much gets done each week, how long it takes, and where tickets wait."),
    ("sla", "SLA", "Service levels",
     "First response and resolution against a target per priority. Loosen or tighten it with the slider."),
    ("workload", "Workload", "Workload",
     "What is open right now, who holds it, and how the backlog moved."),
    ("teams", "Team Trends", "Team trends",
     "Each team's weekly delivery side by side, and open work you can flip between a chart and a table."),
    ("quality", "Data Quality", "Data quality",
     "What was pulled from Jira, what was left out and why, and how each team's statuses were mapped."),
    ("diagnosis", "Diagnosis", "Why is the SLA slipping?",
     "From the trend to the breakdown to the tickets: follow the steps left to right, then drill through."),
    ("contributors", "Contributors", "Contributors",
     "Who carries the delivery: Pareto and ABC concentration, RFM-style segments, load bands and first-quarter cohorts."),
    ("flow_analysis", "Stage Flow", "Stage flow",
     "How tickets move between the shared stages, where they drop out, and which way the backlog is trending."),
]
# Not in the navigation: reached by drillthrough (right-click a team / engineer / priority) or by hovering.
HIDDEN_PAGES = [
    ("team_detail", "Team Detail", "Team detail", "One team: delivery, speed against its own median, its engineers."),
    ("engineer_detail", "Engineer Detail", "Engineer detail",
     "One engineer: what they delivered, how fast against the team median, what they hold."),
    ("ticket_list", "Ticket List", "Ticket list", "The open tickets behind the team, engineer or priority you drilled from."),
    ("team_tip", "Team Tip", "", ""),
]
NO_SCAFFOLD = {"team_tip"}

# "How to read this page": a hidden overlay per page, shown and hidden by two bookmarks.
HELP = {
    "overview": "Cards: the period's totals. The line: tickets in vs out, added up; the gap is backlog growth. "
                "Pick a dimension above the bars to regroup them. Hover a bar for the team tip; right-click a team to "
                "drill through. Click the line's drill arrows to go year → quarter → month.",
    "flow": "Throughput per week with its 4-week average; drill up from week to ISO year. The scatter has one dot per "
            "delivered ticket: dots above the P85 line took longer than 85% of tickets. The matrix shows one number "
            "through eight time views.",
    "sla": "Green = 90% or more within target, amber 75–89%, red below; the words On track / At risk / Off track are "
           "in the matrix tooltip and the Status column. The gauge needle is the SLA, the line the 90% target. Drill the "
           "bars from team to priority. The slider loosens or tightens every target.",
    "workload": "Everything here is as of 30 Sep 2026. Engineers are ranked by open work; right-click one to see their "
                "detail. The backlog line is replayed from the change history.",
    "teams": "One small chart per team on a shared axis. Use the two buttons to switch the open-work view between a "
             "chart and a table. The concentration table shows how much rests on one person.",
    "quality": "What the pipeline pulled and why some issues were left out. The Team slicer does not change the "
               "Tickets Pulled card: that is the whole extract.",
    "diagnosis": "1: when the SLA dropped. 2: where the backlog grew. 3: which team, priority, stage and type hold the "
                 "breached tickets (click + to split). 4: what makes a ticket miss. Then select a team and press the "
                 "button for its open tickets.",
    "contributors": "The first four cards count the period on the first card: \"Selected\" (the dates you picked) or "
                    "the last quarter, used when no dates are selected (over the whole extract everyone would be new). Pareto: bars are deliveries per engineer, the line their running share; everyone left of where it "
                    "crosses 80% is class A. Segments score recency (days since the last delivery), frequency (quarters "
                    "with a delivery) and volume 1–5. The cohort matrix: of the engineers who first delivered in a "
                    "quarter, the share still delivering N quarters later. Ignores the Team slicer.",
    "flow_analysis": "The matrix counts moves between stages: a high In Review → In Progress count means reviews send "
                     "work back. The funnel is the tickets created in the period that reached each stage. The trend "
                     "card is the least-squares slope of the weekly backlog: positive means it is growing.",
}

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
            ("APACHE JIRA", 8, SIDE_TEXT, True), ("Team Delivery", 17, "#FFFFFF", True))
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
    if page.id in HELP:
        hy = 128 + len(PAGES) * 46 + 16
        button(page, "help_open", 12, hy, 208, 36, "How to read this page",
               {"type": "Bookmark", "bookmark": f"bm_help_{page.id}_on"}, icon="information",
               alt="Show how to read this page")
        g = f"help_{page.id}"
        textbox(page, "help_text", X1 - 520, ROW_KPI_Y, 520, 260, ("How to read this page", 12, INK, True),
                (HELP[page.id], 10, INK2, False), panel=True, group=g)
        button(page, "help_close", X1 - 520 + 520 - 96 - 12, ROW_KPI_Y + 260 - 44, 96, 32, "Close",
               {"type": "Bookmark", "bookmark": f"bm_help_{page.id}_off"}, filled=True, group=g,
               alt="Close the help panel")
        page.group(g, "How to read", hidden=True)
        page.bookmarks += [(f"bm_help_{page.id}_on", f"{page.name}: help on", {g: False}),
                           (f"bm_help_{page.id}_off", f"{page.name}: help off", {g: True})]
    textbox(page, "footer", 24, 790, 190, 90,
            ("Source: issues.apache.org Jira (Hive, Cassandra, HBase, Artemis), public", 8, SIDE_TEXT, False),
            ("Pipeline: Jira REST · DuckDB · Parquet · Power BI (TMDL + PBIR)", 8, SIDE_TEXT, False))

    textbox(page, "title", X0, 22, 500, 70, (page.title, 20, INK, True), (page.subtitle, 10, INK2, False))
    sx = cols(500, 176, 200, 200, 180)
    slicer(page, "slicer_quarter", sx[1], 16, 176, 76, "Date.Quarter", single=False, sync="Quarter")
    slicer(page, "slicer_team", sx[2], 16, 200, 76, "Teams.Team", single=False, sync="Team")
    slicer(page, "slicer_priority", sx[3], 16, 200, 76, "Priorities.Priority", single=False, sync="Priority")
    slicer(page, "slicer_type", sx[4], 16, 180, 76, "Types.Type", single=False, sync="Type")


def kpis(page, measures):
    w = (CW - GAP * (len(measures) - 1)) // len(measures)
    for i, (m, units, precision) in enumerate(measures):
        page.add(f"kpi_{i}", "card", X0 + i * (w + GAP), ROW_KPI_Y, w, ROW_KPI_H, roles={"Values": m if "." in m else f"_Measures.{m}"},
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
             objects={"grid": props(rowPadding=6)}, **kw)


def bar(page, name, vtype, x, y, w, h, category, measure, hex_, title, subtitle=None, units=K, sort=None, **kw):
    page.add(name, vtype, x, y, w, h, roles={"Category": category, "Y": measure}, title=title, subtitle=subtitle,
             sort=sort, objects={**axes(show_y=vtype == "clusteredColumnChart", grid=False),
                                 "labels": bar_labels(units), "dataPoint": fill(hex_)}, **kw)


def analyze_slicer(page, x, y, w, param="Analyze By", default="Team"):
    """A field parameter as a single-select button row (Analyze By swaps the axis, Metric Picker the measure)."""
    ref = f"{param}.{param}"
    page.add(f"slicer_{param.lower().replace(' ', '_')}", "slicer", x, y, w, 44, roles={"Values": ref},
             objects={"data": props(mode="HorizontalList"), "header": props(show=False),
                      "selection": props(singleSelect=True, strictSingleSelect=True),
                      "items": props(fontColor=color(INK2), textSize=9.0),
                      "general": [{"properties": {"filter": {"filter": in_filter(ref, [default])}}}]},
             container={"background": props(show=False), "border": props(show=False)})


# ------------------------------------------------------------------- pages
def status_fill(measure="_Measures.SLA Color"):
    """Conditional colour: every data point takes the hex code the SLA Color measure returns."""
    return [{"properties": {"fill": expr_color(measure)}, "selector": WILDCARD}]


def overview(p):
    kpis(p, [("Created", NONE, 0), ("Resolved", NONE, 0), ("Net Flow", NONE, 0), ("Median Lead Time", NONE, 1),
             ("Resolution SLA %", NONE, 0), ("Open Tickets", NONE, 0)])
    a, b = cols(880, 424)
    series = ["Cumulative created", "Cumulative resolved"]
    p.add("line_cumulative", "lineChart", a, ROW2_Y, 880, ROW2_H,
          roles={"Category": hierarchy("Date", "Calendar", ["Year", "Quarter", "Month"]),
                 "Y": ["_Measures.Created", "_Measures.Resolved"]},
          title="Tickets in vs tickets out, cumulative",
          subtitle="RUNNINGSUM visual calculations over the Calendar hierarchy; drill up to quarter or year. "
                   "The gap is backlog growth",
          objects={**axes(), "legend": legend(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color(s, c) for s, c in zip(series, [TARGET_C, ACCENT])]})
    p.visuals[-1]["visual"]["query"]["queryState"]["Y"]["projections"] = [
        {**projection("_Measures.Created"), "hidden": True}, {**projection("_Measures.Resolved"), "hidden": True},
        vcalc(series[0], "RUNNINGSUM ( [Created] )"), vcalc(series[1], "RUNNINGSUM ( [Resolved] )")]
    p.add("card_headline", "card", b, ROW2_Y, 424, 220, roles={"Values": "_Measures.Headline"},
          title="In one sentence",
          objects={"labels": props(color=color(INK), fontSize=13.0, fontFamily="Segoe UI"),
                   "categoryLabels": props(show=False), "wordWrap": props(show=True)})
    slicer(p, "slicer_scale", b, ROW2_Y + 236, 424, 96, "SLA Scale.SLA Scale", default=1.0,
           title="What-if: scale every SLA target", subtitle="100% = the policy; 150% = half as strict again")
    a, b = cols(652, 652)
    analyze_slicer(p, a, ROW3_Y, CW)
    y, h = ROW3_Y + 52, ROW3_H - 52
    bar(p, "bar_delivered_by", "clusteredBarChart", a, y, 652, h, "Analyze By.Analyze By", "_Measures.Delivered",
        ACCENT, "Tickets delivered, by the dimension picked above", "Hover a bar for the team tip", units=NONE,
        sort=("_Measures.Delivered", "Descending"), container=TEAM_TIP)
    p.add("bar_sla_by", "clusteredBarChart", b, y, 652, h,
          roles={"Category": "Analyze By.Analyze By", "Y": "_Measures.Resolution SLA %"},
          title="Resolved within SLA, by the same dimension", subtitle="Green ≥ 90% · amber 75–89% · red below 75%",
          sort=("_Measures.Resolution SLA %", "Descending"), container=TEAM_TIP,
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(NONE), "dataPoint": status_fill()})
    # Edit interactions: picking a bar filters the line, highlights the other bar chart, and leaves the cards alone.
    p.meta["visualInteractions"] = (
        [{"source": "bar_delivered_by", "target": "line_cumulative", "type": "DataFilter"},
         {"source": "bar_delivered_by", "target": "bar_sla_by", "type": "HighlightFilter"}]
        + [{"source": "bar_delivered_by", "target": f"kpi_{i}", "type": "NoFilter"} for i in range(6)])
    p.mobile = {"title": (0, 0, 100, 7), **{f"kpi_{i}": (50 * (i % 2), 8 + 10 * (i // 2), 50, 9) for i in range(4)},
                "line_cumulative": (0, 29, 100, 24), "bar_sla_by": (0, 54, 100, 22), "card_headline": (0, 77, 100, 14)}


def flow(p):
    kpis(p, [("Delivered", NONE, 0), ("Weekly Throughput", NONE, 1), ("Median Lead Time", NONE, 1),
             ("Lead Time P85", NONE, 1), ("Median Cycle Time", NONE, 1), ("Flow Efficiency", NONE, 0)])
    a, c, b = cols(432, 432, 424)
    p.add("line_weekly", "lineChart", a, ROW2_Y, 432, ROW2_H,
          roles={"Category": hierarchy("Date", "Weeks", ["ISO Year", "Week"]), "Y": ["_Measures.Delivered"]},
          title="Delivered per week and the 4-week moving average",
          subtitle="MOVINGAVERAGE visual calculation over the Weeks hierarchy; drill up to ISO year",
          objects={**axes(), "legend": legend(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color("_Measures.Delivered", TARGET_C), measure_color("4-week average", ACCENT)]})
    p.visuals[-1]["visual"]["query"]["queryState"]["Y"]["projections"].append(
        vcalc("4-week average", "MOVINGAVERAGE ( [Delivered], 4 )"))
    # Deneb custom visual (Vega-Lite): the standard Kanban cycle-time scatter, one dot per delivered ticket.
    p.add("deneb_scatter", DENEB, c, ROW2_Y, 432, ROW2_H,
          roles={"dataset": ["Tickets.Ticket Key", "Tickets.Resolved Date", "Tickets.Lead Days", "Teams.Team"]},
          title="Lead time scatter: one dot per delivered ticket",
          subtitle="Deneb (Vega-Lite) custom visual; dashed lines = P50 and P85. Spec: powerbi/deneb/lead-time-scatter.json",
          objects={"vega": props(provider="vegaLite", renderMode="svg", jsonSpec=DENEB_SPEC, jsonConfig="{}",
                                 isNewDialogOpen=False)},
          filters=[visual_filter("delivered", "Tickets.Outcome", ["Delivered"])])
    p.mobile = {}
    bar(p, "col_stage", "clusteredColumnChart", b, ROW2_Y, 424, ROW2_H, "Stages.Stage", "_Measures.Days in Stage",
        ACCENT, "Where the time goes", "Ticket-days spent in each open stage. Flow efficiency = in progress + in review ÷ all",
        units=K)
    a, b = cols(880, 424)
    bar(p, "col_lead_band", "clusteredColumnChart", b, ROW3_Y, 424, ROW3_H, "Tickets.Lead Time Band",
        "_Measures.Delivered", ACCENT, "Delivered tickets by lead time",
        "Lead Time Band is a DAX calculated column: fixed per ticket, so it can sit on an axis", units=NONE,
        sort=("Tickets.Lead Time Band", "Ascending"))
    p.add("matrix_periods", "pivotTable", a, ROW3_Y, 880, ROW3_H,
          roles={"Rows": "Teams.Team", "Columns": "Time Intelligence.Period", "Values": "_Measures.Delivered"},
          title="Delivered, eight ways: week of 21 Sep 2026 (the last full ISO week)",
          subtitle="Columns come from the Time Intelligence calculation group; previous week runs on the ISO-week "
                   "calendar, YoY % switches its own format string",
          objects={"grid": props(rowPadding=6)},
          filters=[visual_filter("last_week", "Date.Week", ["W39 2026"])])


def sla(p):
    kpis(p, [("Response SLA %", NONE, 0), ("Resolution SLA %", NONE, 0), ("Median First Response", NONE, 1),
             ("P1 Resolution SLA %", NONE, 0), ("Open Breached", NONE, 0), ("SLA Scale %", NONE, 0)])
    a, g, b = cols(400, 224, 664)
    # Drill down: team first; the drill arrows (or right-click → Drill down) split a team into priorities.
    p.add("bar_sla_priority", "clusteredBarChart", a, ROW2_Y, 400, ROW2_H,
          roles={"Category": [{**projection("Teams.Team"), "active": True}, projection("Priorities.Priority")],
                 "Y": "_Measures.Resolution SLA %"},
          title="Resolved within SLA, by team; drill down to priority",
          subtitle="Targets: P1 7 days · P2 30 · P3 90 · P4 180 (an illustrative policy, see README)",
          sort=("_Measures.Resolution SLA %", "Descending"), container=TEAM_TIP,
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(NONE), "dataPoint": status_fill()})
    p.add("gauge_sla", "gauge", g, ROW2_Y, 224, ROW2_H,
          roles={"Y": "_Measures.Resolution SLA %", "TargetValue": "_Measures.Resolution SLA Target"},
          title="Resolution SLA vs target", subtitle="Needle: the SLA; line: the 90% target",
          objects={"axis": props(min=0.0, max=1.0), "dataPoint": props(fill=color(ACCENT), target=color(INK)),
                   "labels": props(show=True, color=color(MUTED), fontSize=8.0),
                   "calloutValue": props(show=True, color=color(INK), fontSize=20.0, labelDisplayUnits=NONE,
                                         labelPrecision=0)})
    p.add("matrix_team_priority", "pivotTable", b, ROW2_Y, 664, ROW2_H - 112,
          roles={"Rows": "Teams.Team", "Columns": "Priorities.Priority",
                 "Values": ["_Measures.Resolution SLA %", "_Measures.SLA Status"]},
          title="Team × priority", subtitle="Cell colour follows the on track / at risk / off track rule; the status "
          "is also written out, so colour is never the only cue",
          objects={"grid": props(rowPadding=6),
                   "values": [{"properties": {"backColor": expr_color("_Measures.SLA Color"),
                                              "fontColor": color("#FFFFFF")},
                               "selector": {**WILDCARD, "metadata": "_Measures.Resolution SLA %"}}]})
    slicer(p, "slicer_scale", b, ROW2_Y + ROW2_H - 96, 664, 96, "SLA Scale.SLA Scale", default=1.0,
           title="What-if: scale every SLA target", subtitle="Every SLA number on every page recomputes")
    table(p, "table_breached", X0, ROW3_Y, CW, ROW3_H,
          ["Tickets.Ticket Key", "Teams.Team", "Priorities.Priority", "Tickets.Status", "Tickets.Age Days",
           "Tickets.Days Past Target", "Tickets.Jira URL", "Ticket Text.Summary"],
          "Open tickets already past their resolution target", "Oldest first. Stakeholders don't see the summary "
          "column (object-level security on Ticket Text)", sort=("Tickets.Age Days", "Descending"),
          filters=[visual_filter("breached", "Tickets.Resolution SLA", ["Open, breached"])])
    # Phone layout: status first (the cards, the gauge), then where it breaks, then the list to act on.
    p.mobile = {"title": (0, 0, 100, 7), "kpi_1": (0, 8, 50, 9), "kpi_4": (50, 8, 50, 9), "kpi_3": (0, 18, 50, 9),
                "kpi_0": (50, 18, 50, 9), "gauge_sla": (0, 28, 100, 16), "bar_sla_priority": (0, 45, 100, 20),
                "table_breached": (0, 66, 100, 34)}


def workload(p):
    kpis(p, [("Open Tickets", NONE, 0), ("Work in Progress", NONE, 0), ("Unassigned Open", NONE, 0),
             ("Open per Engineer", NONE, 1), ("Aged Open", NONE, 0), ("Active Engineers", NONE, 0)])
    a, b = cols(640, 664)
    p.add("bar_open_team", "barChart", a, ROW2_Y, 640, ROW2_H,
          roles={"Category": "Teams.Team", "Series": "Tickets.Stage", "Y": "_Measures.Open Tickets"},
          title="Open tickets by team and stage", subtitle="As of 30 Sep 2026, whatever the date slicer says",
          sort=("_Measures.Open Tickets", "Descending"),
          objects={**axes(show_y=False, grid=False), "legend": legend(), "labels": bar_labels(NONE)})
    p.add("table_engineers", "tableEx", b, ROW2_Y, 664, ROW2_H,
          roles={"Values": ["People.Person", "People.Home Team", "_Measures.Open Tickets", "_Measures.Delivered",
                            "_Measures.Delivered Share", "_Measures.Visible Delivered Share",
                            "_Measures.Engineer Rank in Team"]},
          title="Engineers by open work", subtitle="Rank: visual calculation. Share: of all tickets (REMOVEFILTERS); "
          "visible share: of the engineers listed (ALLSELECTED); rank in team: RANK with PARTITIONBY home team",
          sort=("_Measures.Open Tickets", "Descending"), objects={"grid": props(rowPadding=5)},
          filters=[visual_filter("assigned", "People.Person", ["Unassigned"], exclude=True)])
    p.visuals[-1]["visual"]["query"]["queryState"]["Values"]["projections"].insert(
        0, vcalc("Rank", "RANK ( DENSE, ORDERBY ( [Open Tickets], DESC ) )"))
    a, c, b = cols(480, 384, 424)
    bar(p, "bar_age_band", "clusteredColumnChart", b, ROW3_Y, 424, ROW3_H, "Tickets.Age Band", "_Measures.Open Tickets",
        ACCENT, "Open tickets by age", "Age Band is added in Power Query (Tickets.pq) from Age Days", units=NONE,
        sort=("Tickets.Age Band", "Ascending"))
    bar(p, "bar_component", "barChart", c, ROW3_Y, 384, ROW3_H, "Components.Component", "_Measures.Open Tickets",
        ACCENT, "Open tickets by component", "Through the Ticket Components bridge; a ticket in two components "
        "counts in both. Current component values", units=NONE, sort=("_Measures.Open Tickets", "Descending"))
    p.add("line_backlog", "lineChart", a, ROW3_Y, 480, ROW3_H,
          roles={"Category": "Date.Week", "Y": ["_Measures.Open Backlog"]},
          title="Open backlog at the end of each week",
          subtitle="Replayed day by day from the change history, so a ticket closed later still counts as open back then",
          objects={**axes(), "legend": legend(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color("_Measures.Open Backlog", ACCENT)]})


def quality(p):
    kpis(p, [("Data Checks.Tickets Pulled", NONE, 0), ("Data Checks.Tickets Counted", NONE, 0),
             ("Data Checks.Tickets Excluded", NONE, 0), ("Data Checks.No Start Recorded", NONE, 0),
             ("Data Checks.Import Error Rows", NONE, 0), ("Cycle Time Coverage", NONE, 0)])
    a, b = cols(760, 544)
    p.add("bar_checks", "clusteredBarChart", a, ROW2_Y, 760, ROW2_H,
          roles={"Category": "Data Checks.Check", "Y": "Data Checks.Check Tickets"},
          title="What the pipeline counted", subtitle="Every pulled issue is a ticket or excluded with a reason",
          sort=("Data Checks.Check", "Ascending"),
          objects={**axes(show_y=False, grid=False), "labels": bar_labels(NONE), "dataPoint": fill(ACCENT)})
    textbox(p, "method_note", b + 8, ROW2_Y + 8, 528, ROW2_H - 16,
            ("How four different Jira workflows become one", 11, INK, True),
            ("Each team names its statuses differently: Cassandra moves tickets through triage, review and "
             "\"ready to commit\"; Hive mostly goes straight from open to resolved. A mapping table puts every "
             "status into one of four shared stages (backlog, in progress, in review, done), and every priority "
             "into P1–P4.", 9, INK2, False),
            ("Every number is as of 30 Sep 2026. The full change history is replayed, so a ticket closed in "
             "October still counts as open on that date and the figures don't drift between refreshes.", 9, INK2, False),
            ("First response is the first change by anyone other than the reporter; comments aren't pulled.",
             9, INK2, False))
    table(p, "table_status_map", X0, ROW3_Y, CW, ROW3_H,
          ["Teams.Team", "Status Map.Jira Status", "Status Map.Shared Stage", "Data Checks.Tickets in Status"],
          "Status mapping, per team", "The seed file behind the shared stages. Tickets per status is live: TREATAS "
          "applies the mapping's status to Tickets[Status] without a relationship", sort=("Teams.Team", "Ascending"))
    # The Team slicer filters everything except the Tickets Pulled card: that card is the whole extract.
    p.meta["visualInteractions"] = [{"source": "slicer_team", "target": "kpi_0", "type": "NoFilter"}]


# ------------------------------------------------- small multiples & bookmarks
# Two bookmarks on the Team Trends page flip "open work" between a chart and a table. They only touch the
# two target visuals' display state (suppressData), so slicer selections survive the switch.
OPEN_VIEWS = {"bar_open_priority": "barChart", "matrix_open_priority": "pivotTable"}
BOOKMARKS = [("bm_open_chart", "Open work: chart", "bar_open_priority"),
             ("bm_open_table", "Open work: table", "matrix_open_priority")]


def bookmark(name, display, shown):
    return {"$schema": f"{SCHEMA}/bookmark/2.1.0/schema.json", "name": name, "displayName": display,
            "options": {"targetVisualNames": list(OPEN_VIEWS), "applyOnlyToTargetVisuals": True, "suppressData": True},
            "explorationState": {"version": "1.3", "activeSection": "teams", "sections": {"teams": {"visualContainers": {
                v: {"singleVisual": {"visualType": t, "objects": {}, **({} if v == shown else {"display": {"mode": "hidden"}})}}
                for v, t in OPEN_VIEWS.items()}}}}}


def teams(p):
    kpis(p, [("Delivered", NONE, 0), ("Delivered 4W Avg", NONE, 1), ("Throughput Δ vs Prev Week", NONE, 0),
             ("Goal Attainment %", NONE, 0), ("Open Tickets", NONE, 0), ("Open Breached", NONE, 0)])
    a, b = cols(880, 424)
    p.add("line_team_multiples", "lineChart", a, ROW2_Y, 880, ROW2_H,
          roles={"Category": "Date.Week", "Y": "_Measures.Delivered", "Rows": "Teams.Team"},
          title="Delivered per week, one small multiple per team",
          subtitle="Small multiples split one line chart by Teams[Team] (the Rows well) on a shared y-axis",
          objects={**axes(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color("_Measures.Delivered", ACCENT)],
                   "smallMultiplesLayout": props(layoutType="custom", rowCount=2, columnCount=2, gridLineShow=False)})
    # Metric Picker sits in a table: the PBIR checker's visual catalog reads a field parameter as a plain column and
    # only takes measures in chart value wells (Desktop itself accepts it there).
    analyze_slicer(p, b, ROW2_Y, 424, param="Metric Picker", default="Delivered")
    table(p, "table_metric", b, ROW2_Y + 52, 424, ROW2_H - 52, ["Teams.Team", "Metric Picker.Metric Picker"],
          "The metric picked above, by team", "Metric Picker is a field parameter over four measures",
          sort=("Teams.Team", "Ascending"))
    table(p, "table_concentration", X0 + 880 + GAP, ROW3_Y, 424, ROW3_H,
          ["Teams.Team", "_Measures.Top Engineer", "_Measures.Top Engineer Share", "_Measures.Overloaded Engineers",
           "_Measures.Goal Attainment %"],
          "Who carries each team", "Top engineer: INDEX 1 by delivered. Overloaded: more than 20 open tickets. "
          "Goals are synthetic", sort=("_Measures.Top Engineer Share", "Descending"))
    title = "Open tickets by team and priority"
    sub = "As of 30 Sep 2026. The buttons are bookmarks: same data, chart or table"
    p.add("bar_open_priority", "barChart", X0, ROW3_Y, 880, ROW3_H,
          roles={"Category": "Teams.Team", "Series": "Priorities.Priority", "Y": "_Measures.Open Tickets"},
          title=title, subtitle=sub, sort=("_Measures.Open Tickets", "Descending"),
          objects={**axes(show_y=False, grid=False), "legend": legend(), "labels": bar_labels(NONE)})
    p.add("matrix_open_priority", "pivotTable", X0, ROW3_Y, 880, ROW3_H,
          roles={"Rows": "Teams.Team", "Columns": "Priorities.Priority", "Values": "_Measures.Open Tickets"},
          title=title, subtitle=sub, objects={"grid": props(rowPadding=6)})
    p.visuals[-1]["isHidden"] = True  # the chart bookmark is the default view
    for i, (bm, label, _) in enumerate(BOOKMARKS):
        p.add(f"button_{bm}", "actionButton", X0 + 880 - 2 * 128 - 8 + i * 136, ROW3_Y + 10, 128, 32,
              objects={"text": props(show=True, text=label.split(": ")[1].title(), fontColor=color(INK), fontSize=10.0,
                                     fontFamily="Segoe UI Semibold"),
                       "icon": props(show=False), "outline": props(show=True, lineColor=color(HAIR)),
                       "fill": props(show=True, fillColor=color(SURFACE)),
                       "shape": props(tileShape="rectangleRounded", rectangleRoundedCurve=8)},
              container={"visualLink": props(show=True, type="Bookmark", bookmark=bm),
                         "background": props(show=False), "border": props(show=False)})


def kpi_row(p, measures, x=X0, y=ROW_KPI_Y, w_total=CW):
    w = (w_total - GAP * (len(measures) - 1)) // len(measures)
    for i, (m, prec) in enumerate(measures):
        p.add(f"kpi_{i}", "card", x + i * (w + GAP), y, w, ROW_KPI_H, roles={"Values": f"_Measures.{m}"},
              objects={"labels": props(color=color(INK), fontSize=22.0, fontFamily="Segoe UI Semibold",
                                       labelDisplayUnits=NONE, labelPrecision=prec),
                       "categoryLabels": props(show=True, color=color(MUTED), fontSize=9.0)})


def diagnosis(p):
    kpis(p, [("Resolution SLA %", NONE, 0), ("Open Breached", NONE, 0), ("Net Flow", NONE, 0),
             ("Open Tickets", NONE, 0), ("Reopen Rate", NONE, 1), ("Median Lead Time", NONE, 1)])
    a, b, c = cols(432, 432, 424)
    p.add("line_sla_month", "lineChart", a, ROW2_Y, 432, ROW2_H,
          roles={"Category": "Date.Month", "Y": ["_Measures.Resolution SLA %", "_Measures.Resolution SLA Target"]},
          title="1 · When: resolution SLA per month", subtitle="Against the 90% target; months below it are the problem",
          objects={**axes(), "legend": legend(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color("_Measures.Resolution SLA %", ACCENT),
                                 measure_color("_Measures.Resolution SLA Target", TARGET_C)]})
    p.add("waterfall_net_flow", "waterfallChart", b, ROW2_Y, 432, ROW2_H,
          roles={"Category": "Date.Quarter", "Breakdown": "Teams.Team", "Y": "_Measures.Net Flow"},
          title="2 · Where: backlog growth per quarter, by team",
          subtitle="Waterfall of Net Flow (created − resolved): each step is a quarter, split by team; the total is "
                   "the growth since Oct 2024",
          objects={**axes(), "labels": bar_labels(NONE),
                   "sentimentColors": props(increaseFill=color(BEHIND_C), decreaseFill=color(ON_C),
                                            totalFill=color(INK2))})
    p.add("qna", "qnaVisual", c, ROW2_Y, 424, ROW2_H, title="Ask the data",
          subtitle="Q&A over the model's synonyms, e.g. \"open breached by team\"")
    a, b, c = cols(560, 504, 224)
    p.add("decomp_breached", "decompositionTreeVisual", a, ROW3_Y, 560, ROW3_H,
          roles={"Analyze": "_Measures.Open Breached",
                 "ExplainBy": ["Teams.Team", "Priorities.Priority", "Tickets.Stage", "Types.Type"]},
          title="3 · What: the breached tickets, split",
          subtitle="Decomposition tree: team → priority → stage → type in any order (+ to split)",
          objects={"tree": props(barsPerLevel=6, density="default", connectorType="round", accentColor=color(BEHIND_C)),
                   "dataBars": props(positiveBarColor=color(BEHIND_C))})
    p.add("key_influencers", "keyDriversVisual", b, ROW3_Y, 504, ROW3_H,
          roles={"Target": "Tickets.Resolution SLA",
                 "ExplainBy": ["Types.Type", "Priorities.Priority", "Teams.Team", "Tickets.Reopened",
                               "Tickets.Stages Visited"]},
          title="4 · Why: what makes a resolved ticket miss its target",
          subtitle="Key influencers on resolved tickets (met vs breached)",
          objects={"keyDrivers": props(selectedAnalysis="keyInfluencers", targetValue="Breached", selectedSort="impact",
                                       allowKeyDrivers=True, allowProfiles=True, countType="relative")},
          filters=[visual_filter("resolved", "Tickets.Resolution SLA", ["Met", "Breached"])])
    textbox(p, "steps", c + 12, ROW3_Y + 12, 200, ROW3_H - 76,
            ("Then act", 11, INK, True),
            ("Select a team in the tree, then open its breached and open tickets.", 9, INK2, False),
            ("Right-clicking a team anywhere in the report also offers Drill through → Team Detail or Ticket List.",
             9, INK2, False))
    button(p, "btn_drill", c, ROW3_Y + ROW3_H - 48, 224, 40, "Open tickets of the team",
           {"type": "Drillthrough", "drillthroughSection": "ticket_list"}, filled=True,
           alt="Drill through to the Ticket List for the selected team")


def contributors(p):
    kpis(p, [("Period Counted", NONE, 0), ("Active Contributors", NONE, 0), ("New Contributors", NONE, 0),
             ("Returning Contributors", NONE, 0), ("Engineers for 80%", NONE, 0), ("At Risk Contributors", NONE, 0),
             ("Churned Contributors", NONE, 0)])
    a, b = cols(760, 544)
    p.add("combo_pareto", "lineClusteredColumnComboChart", a, ROW2_Y, 760, ROW2_H,
          roles={"Category": "People.Person", "Y": "_Measures.Delivered",
                 "Y2": ["_Measures.Delivered Cumulative %", "_Measures.Pareto Line"]},
          title="Pareto: deliveries per engineer and their running share",
          subtitle="All engineers, sorted high to low (scroll right); the line crosses 80% at the last class-A "
                   "engineer (the 59th with no filters). Unassigned left out",
          sort=("_Measures.Delivered", "Descending"),
          objects={**axes(), "legend": legend(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color("_Measures.Delivered", ACCENT),
                                 measure_color("_Measures.Delivered Cumulative %", INK2),
                                 measure_color("_Measures.Pareto Line", BEHIND_C)]},
          filters=[visual_filter("assigned", "People.Person", ["Unassigned"], exclude=True)])
    table(p, "table_rfm", b, ROW2_Y, 544, ROW2_H,
          ["People.Person", "People.Home Team", "_Measures.Recency Days", "_Measures.Active Quarters",
           "_Measures.Delivered All Time", "_Measures.RFV Score", "_Measures.Contributor Segment"],
          "Engineers by recency, frequency and volume",
          "RFM adapted: R = days since the last delivery, F = quarters with one, V = all-time deliveries; each scored 1–5",
          sort=("_Measures.Delivered All Time", "Descending"),
          filters=[visual_filter("assigned", "People.Person", ["Unassigned"], exclude=True)])
    a, b, c, d = cols(200, 384, 256, 432)
    table(p, "table_abc", a, ROW3_Y, 200, ROW3_H, ["ABC Class.Class", "_Measures.Engineers in Class"],
          "ABC classes", "A: first 80% of deliveries, B: next 15%, C: the rest", sort=("ABC Class.Class", "Ascending"))
    table(p, "table_segments", b, ROW3_Y, 384, ROW3_H,
          ["Contributor Segments.Segment", "_Measures.Contributors in Segment", "Contributor Segments.Action"],
          "Segments and what to do", "Disconnected table: each row counts the engineers whose segment matches",
          sort=("Contributor Segments.Segment", "Ascending"))
    bar(p, "bar_load_band", "clusteredColumnChart", c, ROW3_Y, 256, ROW3_H, "Load Band.Band",
        "_Measures.Engineers in Band", ACCENT, "Engineers by open load", "Open tickets each engineer holds on 30 Sep 2026",
        units=NONE, sort=("Load Band.Band", "Ascending"))
    p.add("matrix_cohort", "pivotTable", d, ROW3_Y, 432, ROW3_H,
          roles={"Rows": "People.First Quarter", "Columns": "Quarter Offset.Quarter Offset",
                 "Values": "_Measures.Cohort Retention %"},
          title="Cohorts: still delivering N quarters later",
          subtitle="Rows: quarter of the first delivery (Q+0 is 100% by definition, so left out); blank = not reached yet",
          objects={"grid": props(rowPadding=4),
                   "values": [{"properties": {"backColor": {"solid": {"color": {"expr": {"FillRule": {
                       "Input": field("_Measures.Cohort Retention %"),
                       "FillRule": {"linearGradient2": {"min": {"color": {"Literal": {"Value": "'#FFFFFF'"}}},
                                                        "max": {"color": {"Literal": {"Value": f"'{ACCENT}'"}}},
                                                        "nullColoringStrategy": {"strategy": {
                                                            "Literal": {"Value": "'noColor'"}}}}}}}}}}},
                              "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}],
                                           "metadata": "_Measures.Cohort Retention %"}}]})


def flow_analysis(p):
    kpis(p, [("Backlog Trend /wk", NONE, 1), ("Transitions", NONE, 0), ("Review Bounce Rate", NONE, 1),
             ("Net Flow", NONE, 0), ("Created", NONE, 0), ("Delivered", NONE, 0)])
    a, b, c = cols(480, 352, 456)
    p.add("matrix_transitions", "pivotTable", a, ROW2_Y, 480, ROW2_H,
          roles={"Rows": "Stage Transitions.From Stage", "Columns": "Stage Transitions.To Stage",
                 "Values": "_Measures.Transitions"},
          title="Stage transitions: from (rows) → to (columns)",
          subtitle="Each move between shared stages in the replayed history; back-moves are rework",
          objects={"grid": props(rowPadding=6)})
    p.add("funnel_stage", "funnel", b, ROW2_Y, 352, ROW2_H,
          roles={"Category": "Funnel Step.Step", "Y": "_Measures.Funnel Tickets"},
          title="Funnel: tickets created that reached each step",
          subtitle="In Progress / In Review: any time in the stage. Many go Backlog → Done directly",
          objects={"labels": props(show=True, color=color(INK2), fontSize=9.0, labelDisplayUnits=NONE),
                   "dataPoint": fill(ACCENT)})
    p.add("matrix_funnel_team", "pivotTable", c, ROW2_Y, 456, ROW2_H,
          roles={"Rows": "Teams.Team", "Columns": "Funnel Step.Step", "Values": "_Measures.Funnel %"},
          title="Funnel % by team", subtitle="Share of the team's created tickets that reached In Progress, In Review, "
          "Done (Created, always 100%, left out)", filters=[visual_filter("no_created", "Funnel Step.Step", ["Created"],
                                                                           exclude=True)],
          objects={"grid": props(rowPadding=6)})
    a, b, c = cols(512, 400, 376)
    # Built-in forecast needs one measure on a continuous (Scalar) date axis.
    p.add("line_backlog_trend", "lineChart", a, ROW3_Y, 512, ROW3_H,
          roles={"Category": "Date.Week Start", "Y": "_Measures.Open Backlog"},
          title="Weekly open backlog with the built-in forecast",
          subtitle="Analytics pane forecast (computed by Power BI when the visual renders); card = LINESTX slope",
          objects={**axes(), "legend": legend(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "categoryAxis": props(show=True, showAxisTitle=False, fontSize=9.0, labelColor=color(MUTED),
                                         axisType="Scalar"),
                   "dataPoint": [measure_color("_Measures.Open Backlog", ACCENT)],
                   "forecast": [{"properties": {"show": lit(True), "displayName": lit("Forecast"),
                                                "lineColor": color(RISK_C), "bandAreaShow": lit(True)}}]})
    p.add("matrix_net_share", "pivotTable", b, ROW3_Y, 400, ROW3_H,
          roles={"Rows": "Teams.Team", "Columns": "Priorities.Priority", "Values": "_Measures.Net Flow Share"},
          title="Who adds to the backlog", subtitle="Each cell's share of the total net flow (created − resolved)",
          objects={"grid": props(rowPadding=6)})
    p.add("col_new_returning", "clusteredColumnChart", c, ROW3_Y, 376, ROW3_H,
          roles={"Category": "Date.Quarter", "Y": ["_Measures.New Contributors", "_Measures.Returning Contributors"]},
          title="New vs returning engineers per quarter", subtitle="New: first delivery (in the selected teams) that quarter",
          objects={**axes(), "legend": legend(), "labels": bar_labels(NONE),
                   "dataPoint": [measure_color("_Measures.New Contributors", ON_C),
                                 measure_color("_Measures.Returning Contributors", ACCENT)]})


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


def team_detail(p):
    drillthrough(p, [("team", "Teams.Team")])
    kpi_row(p, [("Delivered", 0), ("Median Lead Time", 1), ("Resolution SLA %", 0), ("Open Tickets", 0),
                ("Open Breached", 0)], x=X0 + 112, w_total=CW - 112)
    a, b = cols(880, 424)
    p.add("line_team_weekly", "lineChart", a, ROW2_Y, 880, ROW2_H,
          roles={"Category": hierarchy("Date", "Weeks", ["ISO Year", "Week"]), "Y": ["_Measures.Delivered"]},
          title="Delivered per week", subtitle="Drill up to ISO year from the visual header",
          objects={**axes(), "lineStyles": props(strokeWidth=2, showMarker=False),
                   "dataPoint": [measure_color("_Measures.Delivered", ACCENT)]})
    table(p, "table_team_priority", b, ROW2_Y, 424, ROW2_H,
          ["Priorities.Priority", "_Measures.Delivered", "_Measures.Median Lead Time",
           "_Measures.Lead Time vs Team Median", "_Measures.Resolution SLA %"],
          "Speed by priority, against the team", "Lead Time vs Team Median: ALLEXCEPT keeps only the team (and dates)",
          sort=("Priorities.Priority", "Ascending"))
    table(p, "table_team_engineers", X0, ROW3_Y, CW, ROW3_H,
          ["People.Person", "_Measures.Delivered", "_Measures.Median Lead Time", "_Measures.Lead Time vs Team Median",
           "_Measures.Open Tickets", "_Measures.Open Breached"],
          "The team's engineers", "Right-click an engineer → Drill through → Engineer Detail or Ticket List",
          sort=("_Measures.Delivered", "Descending"),
          filters=[visual_filter("assigned", "People.Person", ["Unassigned"], exclude=True)])


def engineer_detail(p):
    drillthrough(p, [("person", "People.Person")])
    kpi_row(p, [("Delivered", 0), ("Delivered Share", 1), ("Median Lead Time", 1), ("Lead Time vs Team Median", 1),
                ("Open Tickets", 0)], x=X0 + 112, w_total=CW - 112)
    a, b = cols(640, 664)
    table(p, "table_engineer_type", a, ROW2_Y, 640, ROW2_H,
          ["Types.Type", "_Measures.Delivered", "_Measures.Median Lead Time", "_Measures.Lead Time vs Team Median"],
          "What they delivered, by type", "Against the median of the team(s) in the filter; negative = faster",
          sort=("_Measures.Delivered", "Descending"))
    bar(p, "col_engineer_band", "clusteredColumnChart", b, ROW2_Y, 664, ROW2_H, "Tickets.Lead Time Band",
        "_Measures.Delivered", ACCENT, "Their delivered tickets by lead time", units=NONE,
        sort=("Tickets.Lead Time Band", "Ascending"))
    table(p, "table_engineer_open", X0, ROW3_Y, CW, ROW3_H,
          ["Tickets.Ticket Key", "Teams.Team", "Priorities.Priority", "Tickets.Status", "Tickets.Age Days",
           "Tickets.Days Past Target", "Tickets.Jira URL"],
          "What they hold now", "Open tickets as of 30 Sep 2026, oldest first", sort=("Tickets.Age Days", "Descending"),
          filters=[visual_filter("open", "Tickets.Outcome", ["Open"])])


def ticket_list(p):
    drillthrough(p, [("team", "Teams.Team"), ("person", "People.Person"), ("priority", "Priorities.Priority")])
    # Page-level filter: only tickets in an open stage. Locked, so a reader can't widen the list by accident.
    p.meta["filterConfig"]["filters"].append(bool_filter("open_only", "Stages.Is Open"))
    kpi_row(p, [("Open Tickets", 0), ("Open Breached", 0), ("Aged Open", 0), ("Unassigned Open", 0)],
            x=X0 + 112, w_total=CW - 112)
    table(p, "table_ticket_list", X0, ROW2_Y, CW, H - ROW2_Y - 20,
          ["Tickets.Ticket Key", "Teams.Team", "People.Person", "Priorities.Priority", "Tickets.Status",
           "Tickets.Age Days", "Tickets.Days Past Target", "Tickets.Jira URL", "Ticket Text.Summary"],
          "Open tickets, oldest first", "Filtered by the drillthrough (team / engineer / priority) and a locked page "
          "filter on open stages. Export data is on: … → Export data", sort=("Tickets.Age Days", "Descending"))


def team_tip(p):
    """Report-page tooltip (320 × 240), hidden from navigation; shown on hover over team / dimension bars."""
    p.meta.update({"type": "Tooltip", "visibility": "HiddenInViewMode", "width": 320, "height": 240,
                   "displayOption": "ActualSize"})
    textbox(p, "tt_title", 12, 8, 296, 24, ("At a glance", 11, INK, True))
    for i, (m, prec) in enumerate([("Median Lead Time", 1), ("Resolution SLA %", 0), ("Open Tickets", 0),
                                   ("Top Engineer Share", 0)]):
        p.add(f"tt_{i}", "card", 12 + 152 * (i % 2), 36 + 100 * (i // 2), 144, 92, roles={"Values": f"_Measures.{m}"},
              objects={"labels": props(color=color(INK), fontSize=16.0, fontFamily="Segoe UI Semibold",
                                       labelDisplayUnits=NONE, labelPrecision=prec),
                       "categoryLabels": props(show=True, color=color(MUTED), fontSize=8.0)})


BUILDERS = {"overview": overview, "flow": flow, "sla": sla, "workload": workload, "teams": teams, "quality": quality,
            "diagnosis": diagnosis, "contributors": contributors, "flow_analysis": flow_analysis,
            "team_detail": team_detail, "engineer_detail": engineer_detail,
            "ticket_list": ticket_list, "team_tip": team_tip}


# ------------------------------------------------------------ theme & files
THEME = {
    "name": "Team KPI",
    "dataColors": [ACCENT, ON_C, RISK_C, BEHIND_C, TARGET_C, "#0b0b0b", "#e0a030", "#2a78d6", "#86b6ef", "#B9B8B0", "#e87ba4", "#104281"],
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


def background_png(path):
    """Canvas wallpaper: warm-white plane with a navy sidebar and a hairline accent."""
    img = Image.new("RGB", (W, H), PLANE)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, SIDEBAR - 1, H], fill=SIDE)
    d.rectangle([SIDEBAR - 3, 0, SIDEBAR - 1, H], fill=ACCENT)
    d.line([24, 112, SIDEBAR - 24, 112], fill=SIDE_LINE, width=1)
    d.line([24, 776, SIDEBAR - 24, 776], fill=SIDE_LINE, width=1)
    d.line([X0, 99, X1, 99], fill=HAIR, width=1)
    img.save(path, optimize=True)


def contrast(fg, bg="#FFFFFF"):
    """WCAG 2 contrast ratio between two hex colours."""
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted([lum(fg), lum(bg)], reverse=True)
    return (hi + 0.05) / (lo + 0.05)


DENEB_SPEC_FILE = PBI / "deneb" / "lead-time-scatter.json"
DENEB_SPEC = DENEB_SPEC_FILE.read_text()

RDL = ROOT / "reports" / "paginated" / "Breached Tickets.rdl"
RDL_DAX = """EVALUATE
CALCULATETABLE (
    SELECTCOLUMNS (
        FILTER ( Tickets, Tickets[Resolution SLA] = "Open, breached" ),
        "Ticket", Tickets[Ticket Key],
        "Team", RELATED ( Teams[Team] ),
        "Priority", RELATED ( Priorities[Priority] ),
        "Status", Tickets[Status],
        "Age Days", Tickets[Age Days],
        "Days Past Target", Tickets[Days Past Target]
    ),
    TREATAS ( { @Team }, Teams[Team] )
)
ORDER BY [Days Past Target] DESC"""


def write_rdl():
    """Paginated report (RDL 2016): breached open tickets for one team (parameter), for the Monday e-mail
    subscription. Data source = the published TeamKPI model over XMLA (placeholder workspace). Checked as
    well-formed XML only; it has not been opened in Power BI Report Builder."""
    import xml.etree.ElementTree as ET
    from xml.sax.saxutils import escape
    cols = [("Ticket", "1.2in", None), ("Team", "1in", None), ("Priority", "1in", None), ("Status", "1.5in", None),
            ("Age Days", "1in", "#,0"), ("Days Past Target", "1.3in", "#,0")]
    fname = lambda c: c.replace(" ", "_")

    def cell(name, value, bold=False, fmt=None):
        style = ("<FontWeight>Bold</FontWeight>" if bold else "") + (f"<Format>{fmt}</Format>" if fmt else "")
        return (f"<TablixCell><CellContents><Textbox Name=\"{name}\"><CanGrow>true</CanGrow><KeepTogether>true</KeepTogether>"
                f"<Paragraphs><Paragraph><TextRuns><TextRun><Value>{escape(value)}</Value><Style>{style}</Style></TextRun>"
                f"</TextRuns><Style /></Paragraph></Paragraphs><Style><BottomBorder><Color>LightGrey</Color>"
                f"<Style>Solid</Style></BottomBorder><PaddingLeft>2pt</PaddingLeft><PaddingTop>2pt</PaddingTop>"
                f"<PaddingBottom>2pt</PaddingBottom></Style></Textbox></CellContents></TablixCell>")

    header = "".join(cell(f"h_{fname(c)}", c, bold=True) for c, *_ in cols)
    detail = "".join(cell(f"d_{fname(c)}", f"=Fields!{fname(c)}.Value", fmt=f) for c, _, f in cols)
    fields = "".join(f"<Field Name=\"{fname(c)}\"><DataField>[{c}]</DataField></Field>" for c, *_ in cols)
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<Report xmlns="http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition"
        xmlns:rd="http://schemas.microsoft.com/SQLServer/reporting/reportdesigner">
  <AutoRefresh>0</AutoRefresh>
  <DataSources>
    <DataSource Name="TeamKPI">
      <ConnectionProperties>
        <DataProvider>PBIDATASET</DataProvider>
        <ConnectString>Data Source=powerbi://api.powerbi.com/v1.0/myorg/Team KPI [Prod];Initial Catalog=TeamKPI</ConnectString>
      </ConnectionProperties>
      <rd:SecurityType>None</rd:SecurityType>
    </DataSource>
  </DataSources>
  <DataSets>
    <DataSet Name="Teams">
      <Query><DataSourceName>TeamKPI</DataSourceName>
        <CommandText>EVALUATE VALUES ( Teams[Team] ) ORDER BY Teams[Team]</CommandText></Query>
      <Fields><Field Name="Team"><DataField>Teams[Team]</DataField></Field></Fields>
    </DataSet>
    <DataSet Name="Breached">
      <Query>
        <DataSourceName>TeamKPI</DataSourceName>
        <QueryParameters><QueryParameter Name="Team"><Value>=Parameters!Team.Value</Value></QueryParameter></QueryParameters>
        <CommandText>{escape(RDL_DAX)}</CommandText>
      </Query>
      <Fields>{fields}</Fields>
    </DataSet>
  </DataSets>
  <ReportParameters>
    <ReportParameter Name="Team">
      <DataType>String</DataType>
      <Prompt>Team</Prompt>
      <ValidValues><DataSetReference><DataSetName>Teams</DataSetName><ValueField>Team</ValueField>
        <LabelField>Team</LabelField></DataSetReference></ValidValues>
    </ReportParameter>
  </ReportParameters>
  <ReportSections>
    <ReportSection>
      <Body>
        <ReportItems>
          <Textbox Name="Title"><CanGrow>true</CanGrow><KeepTogether>true</KeepTogether>
            <Paragraphs><Paragraph><TextRuns><TextRun><Value>="Breached open tickets: " &amp; Parameters!Team.Value</Value>
              <Style><FontSize>16pt</FontSize><FontWeight>Bold</FontWeight></Style></TextRun></TextRuns><Style /></Paragraph>
            <Paragraph><TextRuns><TextRun><Value>Open tickets past their resolution target, most overdue first. Sent every Monday.</Value>
              <Style><FontSize>9pt</FontSize><Color>#52514e</Color></Style></TextRun></TextRuns><Style /></Paragraph></Paragraphs>
            <Top>0in</Top><Left>0in</Left><Height>0.7in</Height><Width>7in</Width><Style /></Textbox>
          <Tablix Name="BreachedTable">
            <TablixBody>
              <TablixColumns>{"".join(f"<TablixColumn><Width>{w}</Width></TablixColumn>" for _, w, _ in cols)}</TablixColumns>
              <TablixRows>
                <TablixRow><Height>0.3in</Height><TablixCells>{header}</TablixCells></TablixRow>
                <TablixRow><Height>0.25in</Height><TablixCells>{detail}</TablixCells></TablixRow>
              </TablixRows>
            </TablixBody>
            <TablixColumnHierarchy><TablixMembers>{"<TablixMember />" * len(cols)}</TablixMembers></TablixColumnHierarchy>
            <TablixRowHierarchy><TablixMembers>
              <TablixMember><KeepWithGroup>After</KeepWithGroup><RepeatOnNewPage>true</RepeatOnNewPage></TablixMember>
              <TablixMember><Group Name="Details" /></TablixMember>
            </TablixMembers></TablixRowHierarchy>
            <DataSetName>Breached</DataSetName>
            <Top>0.8in</Top><Left>0in</Left><Height>0.55in</Height><Width>7in</Width><Style /></Tablix>
        </ReportItems>
        <Height>1.5in</Height><Style />
      </Body>
      <Width>7in</Width>
      <Page>
        <PageHeight>11in</PageHeight><PageWidth>8.5in</PageWidth>
        <LeftMargin>0.75in</LeftMargin><RightMargin>0.75in</RightMargin><TopMargin>0.5in</TopMargin><BottomMargin>0.5in</BottomMargin>
        <Style />
      </Page>
    </ReportSection>
  </ReportSections>
  <ReportParametersLayout><GridLayoutDefinition><NumberOfColumns>1</NumberOfColumns><NumberOfRows>1</NumberOfRows>
    <CellDefinitions><CellDefinition><ColumnIndex>0</ColumnIndex><RowIndex>0</RowIndex><ParameterName>Team</ParameterName>
    </CellDefinition></CellDefinitions></GridLayoutDefinition></ReportParametersLayout>
  <rd:ReportUnitType>Inch</rd:ReportUnitType>
  <rd:ReportID>6d2e8b14-3a7c-4f51-9e08-1c4b7a9d2e63</rd:ReportID>
</Report>
"""
    RDL.parent.mkdir(parents=True, exist_ok=True)
    RDL.write_text(xml)
    root = ET.parse(RDL).getroot()  # well-formedness check
    ns = {"r": "http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition"}
    n_cells = len(root.findall(".//r:TablixCell", ns))
    assert n_cells == 2 * len(cols), n_cells
    print(f"  ✓ wrote {RDL.relative_to(ROOT)} (well-formed XML, {n_cells} cells, parameter Team)")


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def main():
    global PBI, REPORT
    if "--service" in sys.argv:  # report copy bound (byPath) to powerbi-service/TeamKPI.SemanticModel
        PBI = ROOT / "powerbi-service"
        REPORT = PBI / REPORT.name
    for name, c in (("on track", ON_C), ("at risk", RISK_C), ("off track", BEHIND_C), ("accent", ACCENT)):
        r = contrast(c)
        assert r >= 4.5, f"{name} {c}: contrast {r:.2f} with white text, below WCAG AA 4.5"
    print("  ✓ status colours reach WCAG AA with white text: " +
          ", ".join(f"{c} {contrast(c):.1f}" for c in (ON_C, RISK_C, BEHIND_C, ACCENT)))
    shutil.rmtree(REPORT, ignore_errors=True)
    res = REPORT / "StaticResources" / "RegisteredResources"
    res.mkdir(parents=True)
    dump(res / "TeamKPI.json", THEME)
    background_png(res / "canvas.png")

    dump(PBI / "TeamKPI.pbip", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": "TeamKPI.Report"}}],
        "settings": {"enableAutoRecovery": True}})
    dump(REPORT / ".platform", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Report", "displayName": "TeamKPI"},
        "config": {"version": "2.0", "logicalId": "c41e7b25-9a08-4d6f-b3e2-5f19a7c80d36"}})
    dump(REPORT / "definition.pbir", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": "../TeamKPI.SemanticModel"}}})
    d = REPORT / "definition"
    dump(d / "version.json", {"$schema": f"{SCHEMA}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"})
    dump(d / "report.json", {
        "$schema": f"{SCHEMA}/report/3.3.0/schema.json",
        "themeCollection": {"customTheme": {
            "name": "TeamKPI.json", "type": "RegisteredResources",
            "reportVersionAtImport": {"visual": "1.8.95", "report": "2.0.95", "page": "1.3.95"}}},
        # Report-level filter: nothing after the as-of date, on every page. Locked in reading view.
        "filterConfig": {"filters": [date_filter("as_of", "Date.Date", "<=", "2026-09-30")]},
        "objects": {"outspacePane": [{"properties": {"expanded": lit(False)}}]},
        "publicCustomVisuals": [DENEB],
        # Export data: summarized and underlying rows (the ticket list is meant to be exported). Report-wide;
        # object-level security still hides Ticket Text[Summary] from stakeholders in an export.
        "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True,
                     "hideVisualContainerHeader": False, "useEnhancedTooltips": True,
                     "exportDataMode": "AllowSummarizedAndUnderlying"},
        "resourcePackages": [{"name": "RegisteredResources", "type": "RegisteredResources", "items": [
            {"name": "TeamKPI.json", "path": "TeamKPI.json", "type": "CustomTheme"},
            {"name": "canvas.png", "path": "canvas.png", "type": "Image"}]}]})
    every = PAGES + HIDDEN_PAGES
    dump(d / "pages" / "pages.json", {"$schema": f"{SCHEMA}/pagesMetadata/1.1.0/schema.json",
                                      "pageOrder": [p[0] for p in every], "activePageName": PAGES[0][0]})

    items = []
    for bm, display, shown in BOOKMARKS:
        dump(d / "bookmarks" / f"{bm}.bookmark.json", bookmark(bm, display, shown))
        items.append({"name": bm})

    total = 0
    for pid, name, title, subtitle in every:
        page = Page(pid, name, title, subtitle)
        if pid not in NO_SCAFFOLD:
            scaffold(page)
        BUILDERS[pid](page)
        page.finalize()
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
        for bm, display, hidden in page.bookmarks:  # display-only: show / hide a group, keep filters
            kids = [v["name"] for v in page.visuals if v.get("parentGroupName") in hidden]
            dump(d / "bookmarks" / f"{bm}.bookmark.json", {
                "$schema": f"{SCHEMA}/bookmark/2.1.0/schema.json", "name": bm, "displayName": display,
                "options": {"applyOnlyToTargetVisuals": True, "targetVisualNames": list(hidden) + kids,
                            "suppressData": True, "suppressActiveSection": False},
                "explorationState": {"version": "1.3", "activeSection": pid, "sections": {pid: {
                    "visualContainers": {k: {} for k in kids},
                    "visualContainerGroups": {g: {"isHidden": h} for g, h in hidden.items()}}}}})
            items.append({"name": bm})
        total += len(page.visuals)
        print(f"  ✓ page {name:<18} {len(page.visuals):>3} visuals")
    dump(d / "bookmarks" / "bookmarks.json", {"$schema": f"{SCHEMA}/bookmarksMetadata/1.0.0/schema.json",
                                              "items": items})
    print(f"  ✓ wrote {REPORT.relative_to(ROOT)} ({len(every)} pages, {total} visuals)")
    if PBI.name == "powerbi":
        write_rdl()

    result = subprocess.run(["pbir", "validate", str(REPORT), "--all"], capture_output=True, text=True,
                            env={**os.environ, "NO_COLOR": "1", "COLUMNS": "200"})
    print(result.stdout[-3000:], result.stderr[-2000:])


if __name__ == "__main__":
    main()
