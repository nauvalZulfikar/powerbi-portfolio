"""Build the Power BI reports (PBIR) on top of the TMDL model.

    uv tool install pbir-cli        # once
    uv run python src/build_report.py

Regenerates from scratch every run, so the reports are code:
  powerbi/Olist.Report       6 pages + drillthrough page `Seller Detail` + tooltip page `TT Category`
  powerbi/Olist.Ops.Report   thin one-page report on the same model (byPath -> byConnection once published)
  reports/Seller Scorecard.rdl  paginated report, one page per seller (Report Builder / Service only)

Most visuals go through the pbir CLI (it validates bindings as it goes). What the CLI has no command for
(hierarchies, field parameters, page bindings, bookmarks, mobile layout, interactions, alt text, the
background image, report filters) is written straight into the PBIR JSON afterwards, then `pbir validate`
checks the whole report against the schema and the model.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
PBI = ROOT / "powerbi"
R = "Olist.Report"
OPS = "Olist.Ops.Report"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"

# ------------------------------------------------------------------ design tokens
NAVY, INK, INK2, MUTED = "#0D366B", "#0B0B0B", "#52514E", "#898781"
PLANE, SURFACE, HAIR = "#F4F3EF", "#FFFFFF", "#E6E4DD"
PALETTE = ["#2A78D6", "#EB6834", "#1BAF7A", "#EDA100", "#E87BA4", "#008300", "#4A3AA7", "#E34948"]
GOOD, BAD, NEUTRAL = "#0CA30C", "#D03B3B", "#C3C2B7"
BLUE_LO, BLUE_HI = "#CDE2FB", "#0D366B"
NAV_TEXT, NAV_SOFT, NAV_ACTIVE = "#B7D3F6", "#9EC5F4", "#1C5CAB"

# ------------------------------------------------------------------ grid
# Layout is designed on a 1280 × 720 grid and scaled ×1.5 onto a 1920 × 1080 canvas,
# so boxes grow while font sizes stay put — text gets room instead of being clipped.
SCALE, PAGE_W, PAGE_H = 1.5, 1920, 1080
X0, GAP, RIGHT = 216, 12, 1264
KPI_Y, KPI_H = 84, 104
ROW2_Y, ROW2_H = 200, 250
ROW3_Y, ROW3_H = 462, 246
ROW4_Y, ROW4_H = 720, 246
ROW5_Y, ROW5_H = 978, 246
# pages with more than three rows scroll (fit to width): rows -> design height (4 rows: 1920 × 1470, 5 rows: 1920 × 1857)
TALL_PAGES = {"Sales & Products": 5, "Delivery & Satisfaction": 5, "Sellers & Logistics": 5, "Customers & Retention": 4}


def page_height(name):
    rows = TALL_PAGES.get(name)
    return PAGE_H if not rows else round(({4: ROW4_Y + ROW4_H, 5: ROW5_Y + ROW5_H}[rows] + 14) * SCALE)
SANKEY = "sankey02300D1BE6F5427989F3DE31CCA9E0F32020"  # Microsoft Sankey (AppSource) custom visual
TT_W, TT_H = 360, 260  # tooltip page, actual size

BOOKMARKS, INTERACTIONS, PAGE_FILTERS, MOBILE = [], {}, {}, {}


def cols(n, x0=X0, x1=RIGHT, gap=GAP):
    """Split [x0, x1] into n equal columns -> list of (x, width)."""
    w = (x1 - x0 - gap * (n - 1)) // n
    return [(x0 + i * (w + gap), w) for i in range(n)]


def pbir(*args, check=True):
    env = {**os.environ, "NO_COLOR": "1", "PBIR_ALLOW_OVERLAP": "1", "COLUMNS": "200"}
    r = subprocess.run(["pbir", "-q", *args], cwd=PBI, env=env, capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"✗ pbir {' '.join(args)}\n{r.stdout}\n{r.stderr}")
    return r


def page_path(page, report=R):
    return f"{report}/{page}.Page"


def visual(page, vtype, name, x, y, w, h, data=(), title=None, subtitle=None):
    x, y, w, h = (round(n * SCALE) for n in (x, y, w, h))
    args = ["add", "visual", vtype, page_path(page), "-n", name, "-x", str(x), "-y", str(y), "-w", str(w), "-h", str(h), "--force"]
    for d in data:
        args += ["-d", d]
    pbir(*args)
    v = f"{page_path(page)}/{name}.Visual"
    pbir("visuals", "z-order", v, "--value", "1000")
    if title:
        pbir("visuals", "title", v, "--show", "--text", title)
    if subtitle:
        pbir("visuals", "subtitle", v, "--show", "--text", subtitle)
    return v


def literal(value):
    s = str(value)
    if s in ("true", "false"):
        return s
    if s.lstrip("-").replace(".", "", 1).isdigit():
        return f"{s}D"
    return f"'{s}'"


def patch(v, prop, value):
    """Write a formatting property straight into visual.json (for props pbir's validator rejects)."""
    page, name = v.split("/")[1].removesuffix(".Page"), v.split("/")[2].removesuffix(".Visual")
    f = vfile(page, name)
    doc = json.loads(f.read_text())
    obj, key = prop.split(".")
    expr = {"expr": {"Literal": {"Value": literal(value)}}}
    if str(value).startswith("#"):
        expr = {"solid": {"color": expr}}
    entries = doc["visual"].setdefault("objects", {}).setdefault(obj, [{"properties": {}}])
    entries[0].setdefault("properties", {})[key] = expr
    f.write_text(json.dumps(doc, indent=2, ensure_ascii=False))


def setp(v, prop, value):
    r = pbir("set", f"{v}.{prop}", "--value", str(value), check=False)
    if r.returncode != 0:
        if "Unknown component" not in r.stdout + r.stderr:
            sys.exit(f"✗ pbir set {v}.{prop}\n{r.stdout}\n{r.stderr}")
        patch(v, prop, value)


# ------------------------------------------------------------------ direct PBIR JSON
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


def hierarchy(table, name, levels, active=None):
    """Category projections for a drillable hierarchy; `active` levels are the ones on show (all = expanded)."""
    return [{"field": {"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {"SourceRef": {"Entity": table}},
                                                                        "Hierarchy": name}}, "Level": lv}},
             "queryRef": f"{table}.{name}.{lv}", "nativeQueryRef": lv,
             **({"active": True} if lv in (active or levels[:1]) else {})} for lv in levels]


def props(**kw):
    """One formatting object: plain python values become literals; dicts pass through."""
    return [{"properties": {k: (v if isinstance(v, dict) else lit(v)) for k, v in kw.items()}}]


def expr_color(measure_ref):
    return {"solid": {"color": {"expr": field(measure_ref)}}}


WILDCARD = {"data": [{"dataViewWildcard": {"matchingOption": 1}}]}


def in_filter(ref, values, alias="t"):
    table, col = ref.split(".", 1)
    return {"Version": 2, "From": [{"Name": alias, "Entity": table, "Type": 0}],
            "Where": [{"Condition": {"In": {
                "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": alias}}, "Property": col}}],
                "Values": [[{"Literal": {"Value": lit_value(v)}}] for v in values]}}}]}


def cat_filter(name, ref, values, **extra):
    return {"name": name, "field": field(ref), "type": "Categorical", "filter": in_filter(ref, values), **extra}


def measure_filter(name, measure, kind, value):
    """Visual-level filter on a measure: ComparisonKind 0 =, 1 >, 2 >=, 3 <, 4 <=."""
    table, prop = measure.split(".", 1)
    left = {"Measure": {"Expression": {"SourceRef": {"Source": "m"}}, "Property": prop}}
    cond = {"Comparison": {"ComparisonKind": kind, "Left": left, "Right": {"Literal": {"Value": lit_value(value)}}}}
    return {"name": name, "field": field(measure), "type": "Advanced",
            "filter": {"Version": 2, "From": [{"Name": "m", "Entity": table, "Type": 0}], "Where": [{"Condition": cond}]}}


def text_runs(*runs):
    """runs: (text, size_pt, color, bold) tuples -> one paragraph each."""
    return [{"textRuns": [{"value": t, "textStyle": {
        "fontFamily": "Segoe UI Semibold" if bold else "Segoe UI", "fontSize": f"{size}pt", "color": col}}]}
        for t, size, col, bold in runs]


def load(path):
    return json.loads(path.read_text())


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def pdir(page, report=R):
    """Folder of a page, found by display name (pbir names page folders with random ids)."""
    return next(p.parent for p in (PBI / report / "definition" / "pages").glob("*/page.json")
                if load(p)["displayName"] == page)


def pid(page, report=R):
    return load(pdir(page, report) / "page.json")["name"]


def vfile(page, name, report=R):
    return pdir(page, report) / "visuals" / name / "visual.json"


def edit(page, name, fn, report=R):
    f = vfile(page, name, report)
    doc = load(f)
    fn(doc)
    dump(f, doc)


def add_role(page, name, role, refs):
    """Append projections to a role of a pbir-built visual (e.g. Tooltips)."""
    def fn(doc):
        q = doc["visual"]["query"]["queryState"].setdefault(role, {"projections": []})
        q["projections"] += [projection(r) for r in refs]
    edit(page, name, fn)


def raw(page, name, vtype, x, y, w, h, roles=None, title=None, subtitle=None, objects=None, container=None,
        sort=None, filters=None, hidden=False, group=None, alt=None, report=R, scale=SCALE, z=1000):
    """Write one visual.json directly: for what the CLI can't express (hierarchies, field parameters, groups ...)."""
    vc = dict(container or {})
    if alt:
        vc["general"] = props(altText=alt)
    if title is not None:
        vc.setdefault("title", props(show=True, text=title))
        if subtitle:
            vc.setdefault("subTitle", props(show=True, text=subtitle))
    query = {"queryState": {role: {"projections": [projection(r) for r in (refs if isinstance(refs, list) else [refs])]}
                            for role, refs in (roles or {}).items()}}
    if sort:
        ref, direction = sort
        query["sortDefinition"] = {"sort": [{"field": field(ref), "direction": direction}], "isDefaultSort": False}
    v = {"visualType": vtype, "objects": objects or {}, "visualContainerObjects": vc, "drillFilterOtherVisuals": True}
    if roles:
        v["query"] = query
    x, y, w, h = (round(n * scale) for n in (x, y, w, h))
    doc = {"$schema": f"{SCHEMA}/visualContainer/2.9.0/schema.json", "name": name,
           "position": {"x": x, "y": y, "z": z, "width": w, "height": h, "tabOrder": 0}, "visual": v}
    if filters:
        doc["filterConfig"] = {"filters": filters}
    if group:
        doc["parentGroupName"] = group
    if hidden:
        doc["isHidden"] = True
    dump(pdir(page, report) / "visuals" / name / "visual.json", doc)
    return doc


def group_box(page, name, display, x, y, w, h, hidden=False, z=5000):
    x, y, w, h = (round(n * SCALE) for n in (x, y, w, h))
    doc = {"$schema": f"{SCHEMA}/visualContainer/2.9.0/schema.json", "name": name,
           "position": {"x": x, "y": y, "z": z, "width": w, "height": h},
           "visualGroup": {"displayName": display, "groupMode": "ScaleMode"}}
    if hidden:
        doc["isHidden"] = True
    dump(pdir(page) / "visuals" / name / "visual.json", doc)


def textbox(page, name, x, y, w, h, *runs, group=None, boxed=False, z=1000, scale=SCALE, report=R):
    box = ({"background": props(show=True, color=color(SURFACE)), "border": props(show=True, color=color(NAVY), radius=12.0),
            "padding": props(top=16, bottom=12, left=18, right=18), "dropShadow": props(show=True)} if boxed else
           {"background": props(show=False), "border": props(show=False)})
    raw(page, name, "textbox", x, y, w, h, objects={"general": [{"properties": {"paragraphs": text_runs(*runs)}}]},
        container=box, group=group, z=z, scale=scale, report=report)


def button(page, name, x, y, w, h, text, link, alt, tooltip=None, group=None, style="light", icon=None, z=1000):
    """actionButton; `link` = visualLink props (type Bookmark / PageNavigation / Back ...)."""
    fg, bg, line = {"light": (INK2, SURFACE, HAIR), "primary": ("#FFFFFF", PALETTE[0], PALETTE[0]),
                    "nav": (NAV_TEXT, NAVY, "#2B5A94")}[style]
    objects = {"text": props(show=bool(text), text=text or "", fontColor=color(fg), fontSize=10.0,
                             horizontalAlignment="center" if style != "nav" else "left", leftMargin=12),
               "fill": props(show=True, fillColor=color(bg), transparency=0.0),
               "outline": props(show=True, lineColor=color(line)),
               "shape": props(tileShape="rectangleRounded", rectangleRoundedCurve=8)}
    objects["icon"] = props(show=True, shapeType=icon, lineColor=color(fg), iconSize=18) if icon else props(show=False)
    link = {"show": True, **link}
    if tooltip:
        link["tooltip"] = tooltip
    raw(page, name, "actionButton", x, y, w, h, objects=objects, alt=alt, group=group, z=z,
        container={"visualLink": props(**link), "background": props(show=False), "border": props(show=False)})


def param_slicer(page, name, x, y, w, h, ref, default, alt):
    """Single-select chip slicer for a field parameter (Metric / Breakdown)."""
    raw(page, name, "slicer", x, y, w, h, roles={"Values": ref}, alt=alt,
        objects={"data": props(mode="HorizontalList"), "header": props(show=False),
                 "selection": props(singleSelect=True, strictSingleSelect=True),
                 "items": props(fontColor=color(INK2), textSize=9.0),
                 "general": [{"properties": {"filter": {"filter": in_filter(ref, [default])}}}]},
        container={"background": props(show=False), "border": props(show=False)})


def bookmark(name, display, page, visuals=None, groups=None, targets=None):
    """Display-only bookmark (suppressData) that shows / hides the listed visuals or groups on one page."""
    sec = pid(page)
    BOOKMARKS.append({"name": name, "displayName": display,
                      "options": {"applyOnlyToTargetVisuals": True, "targetVisualNames": targets,
                                  "suppressData": True, "suppressActiveSection": True},
                      "explorationState": {"version": "1.3", "activeSection": sec, "sections": {sec: {
                          "visualContainers": {v: {"singleVisual": {"visualType": vtype(page, v), "objects": {},
                                                                    **({"display": {"mode": "hidden"}} if hide else {})}}
                                               for v, hide in (visuals or {}).items()},
                          **({"visualContainerGroups": {g: {"isHidden": hide} for g, hide in groups.items()}}
                             if groups else {})}}}})


def vtype(page, name):
    return load(vfile(page, name))["visual"]["visualType"]


def phone(slicers, cards, charts):
    """Phone layout, top to bottom: slicer, KPI cards two a row, then charts (360 px wide canvas)."""
    out, y = {}, 0
    for v in slicers:
        out[v], y = (0, y, 360, 72), y + 80
    for i, v in enumerate(cards):
        out[v] = ((i % 2) * 184, y + (i // 2) * 104, 176, 96)
    y += -(-len(cards) // 2) * 104
    for v in charts:
        out[v], y = (0, y, 360, 300), y + 308
    return out


# ------------------------------------------------------------------ accessibility
def luminance(hex_):
    c = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


SIDEBAR_TEXT = [("#FFFFFF", NAVY), (NAV_TEXT, NAVY), (NAV_SOFT, NAVY), ("#FFFFFF", NAV_ACTIVE)]

# Definitions behind the info buttons, the overlay on Overview and the About page (and the twin).
DEFS = [("Revenue", "Item price of valid orders (not cancelled / unavailable). Freight is separate; GMV = revenue + freight."),
        ("Late", "Delivered after the estimated date the customer was promised. Late rate = late ÷ delivered orders."),
        ("Late status", "▲ High ≥ 12% · ● Watch ≥ 8% · ✓ OK — the icon and the word carry the status, colour only repeats it."),
        ("Customer", "A person (customer_unique_id), not an order-level customer_id: one person can hold many ids."),
        ("RFM", "Recency, frequency, monetary quintile scores per customer, mapped to segments (Champions, At risk ...)."),
        ("Seller code", "Short public code (S0001 ...). Seller ids, customer ids and names never appear in a visual.")]


# ------------------------------------------------------------------ chrome
def chrome(page, title, subtitle, third=("slicer_group", "Product.Category Group", "Category group"), info=True):
    """Page navigator (sidebar drawn by the background image), page header with info button, three slicers."""
    brand = visual(page, "textbox", "brand", 16, 18, 168, 70, title="OLIST", subtitle="Marketplace Analytics")
    pbir("visuals", "title", brand, "--fontColor", "#FFFFFF", "--fontSize", "20", "--bold")
    pbir("visuals", "subtitle", brand, "--fontColor", NAV_SOFT, "--fontSize", "11")
    setp(brand, "background.show", "false")
    setp(brand, "border.show", "false")

    for i, (target, _) in enumerate(PAGES):
        current = target == page
        raw(page, f"nav_{i}", "actionButton", 12, 112 + i * 46, 176, 40,
            alt=f"Go to page {target}" + (" (current page)" if current else ""),
            objects={"text": props(show=True, text=target, fontColor=color("#FFFFFF" if current else NAV_TEXT), fontSize=11.0,
                                   bold=current, horizontalAlignment="left", leftMargin=12),
                     "fill": props(show=True, fillColor=color(NAV_ACTIVE if current else NAVY), transparency=0.0),
                     "outline": props(show=False), "icon": props(show=False),
                     "shape": props(tileShape="rectangleRounded", rectangleRoundedCurve=8)},
            container={"visualLink": props(show=not current, type="PageNavigation", navigationSection=pid(target)),
                       "background": props(show=False), "border": props(show=False)})
    about = PAGES[-1][0]
    NAV_END = 112 + len(PAGES) * 46 + 10
    if page != about:
        button(page, "button_help", 12, NAV_END, 84, 34, "? Help", {"type": "PageNavigation", "navigationSection": pid(about)},
               "Button: open About & Definitions", tooltip="Definitions, synonyms and the Q&A visual", style="nav")
    button(page, "button_reset", 104, NAV_END, 84, 34, "↺ Reset", {"type": "Bookmark", "bookmark": "bm_reset_filters"},
           "Button: clear the year, region and category / state slicers on every page", tooltip="Reset filters", style="nav")

    foot = visual(page, "textbox", "footer", 16, 640, 168, 64, title="Source", subtitle="Kaggle · Olist 2016–2018")
    pbir("visuals", "title", foot, "--fontColor", NAV_SOFT, "--fontSize", "9")
    pbir("visuals", "subtitle", foot, "--fontColor", "#FFFFFF", "--fontSize", "9")
    setp(foot, "background.show", "false")
    setp(foot, "border.show", "false")

    head = visual(page, "textbox", "header", X0, 12, 600, 62, title=title, subtitle=subtitle)
    pbir("visuals", "title", head, "--fontColor", INK, "--fontSize", "20", "--bold")
    pbir("visuals", "subtitle", head, "--fontColor", INK2, "--fontSize", "11")
    setp(head, "background.show", "false")
    setp(head, "border.show", "false")
    if info:
        # 18-s9: info icon; hover shows the key definitions, click opens them in full
        link = ({"type": "Bookmark", "bookmark": "bm_defs_show"} if page == PAGES[0][0]
                else {"type": "PageNavigation", "navigationSection": pid(about)})
        button(page, "button_info", X0 + 606, 18, 30, 30, None, link, "Info: definitions of revenue, late and RFM",
               tooltip=" | ".join(f"{k}: {v}" for k, v in DEFS[:2]) + " | RFM: recency, frequency, monetary",
               icon="information")

    for (x, w), (name, fld, label) in zip(cols(3, 888), [
        ("slicer_year", "Date.Year", "Year"),
        ("slicer_region", "Customer.Region", "Customer region"),
        third,
    ]):
        s = visual(page, "slicer", name, x, 12, w, 64, data=[f"Values:{fld}"], title=label)
        setp(s, "data.mode", "Dropdown")
        setp(s, "header.show", "false")
        pbir("visuals", "title", s, "--fontSize", "9", "--fontColor", MUTED)


def kpis(page, cards, x0=X0):
    for (x, w), (name, measure, ref) in zip(cols(len(cards), x0), cards):
        v = visual(page, "cardVisual", name, x, KPI_Y, w, KPI_H, data=[f"Data:_Measures.{measure}"])
        if ref:
            actual, target, detail, col = ref
            pbir("visuals", "card-reference", v, "--actual", f"_Measures.{actual}", "--target", f"_Measures.{target}",
                 "--detail", f"_Measures.{detail}", "--detail-color", f"_Measures.{col}", "--title", "vs prior year")


def theme(report=R):
    pbir("theme", "set-colors", report, "--data-colors", str(PALETTE).replace("'", '"'),
         "--good", GOOD, "--bad", BAD, "--neutral", NEUTRAL,
         "--minimum", BLUE_LO, "--maximum", BLUE_HI, "--background", SURFACE, "--foreground", INK,
         "--table-accent", PALETTE[0], "--accent", NAVY)
    for level, size, face, col in [("title", 12, "Segoe UI Semibold", INK), ("label", 10, "Segoe UI", INK2),
                                   ("callout", 22, "Segoe UI Semibold", INK), ("header", 11, "Segoe UI Semibold", INK)]:
        pbir("theme", "set-fonts", report, level, "--font-size", str(size), "--font-face", face, "--color", col)
    for prop, value in [
        ("*.*.background.show", "true"), ("*.*.background.color", SURFACE), ("*.*.background.transparency", "0"),
        ("*.*.border.show", "true"), ("*.*.border.color", HAIR), ("*.*.border.radius", "10"),
        ("*.*.dropShadow.show", "false"),
        ("*.*.title.fontColor", INK), ("*.*.title.fontSize", "12"),
        ("*.*.subTitle.fontColor", MUTED), ("*.*.subTitle.fontSize", "10"),
        ("*.*.padding.top", "10"), ("*.*.padding.left", "12"), ("*.*.padding.right", "12"), ("*.*.padding.bottom", "8"),
        ("*.*.valueAxis.gridlineColor", "#EFEEEA"), ("*.*.categoryAxis.labelColor", MUTED), ("*.*.valueAxis.labelColor", MUTED),
    ]:
        pbir("theme", "set-formatting", report, prop, "--value", value)


# ================================================================== pages
def page_overview(p):
    chrome(p, "Executive Overview", "Revenue grew fast through 2017 and plateaued in 2018 — where it comes from, and how customers pay.")
    kpis(p, [
        ("kpi_revenue", "Revenue", ("Revenue", "Revenue PY", "Revenue YoY %", "Revenue YoY Color")),
        ("kpi_orders", "Order Count", ("Order Count", "Orders PY", "Orders YoY %", "Orders YoY Color")),
        ("kpi_aov", "AOV", None),
        ("kpi_review", "Item Avg Review", None),
        ("kpi_late", "Seller Late Rate %", None),
    ])
    # 17-s11: the revenue card reads its alt text from a measure, so a screen reader hears the current number
    edit(p, "kpi_revenue", lambda d: d["visual"].setdefault("visualContainerObjects", {}).update(
        general=[{"properties": {"altText": {"expr": field("_Measures.Alt Text Revenue")}}}]))
    (x1, w1), (x2, w2) = [(X0, 630), (X0 + 630 + GAP, RIGHT - X0 - 630 - GAP)]
    # 7-s1 + 17-s8: Metric field parameter on Y, Date[Calendar] hierarchy (expanded to month) on the axis -> drill up/down
    param_slicer(p, "slicer_metric", x1, ROW2_Y, w1, 34, "Metric.Metric", "Revenue", "Slicer: pick the metric of the trend")
    raw(p, "trend_revenue", "lineChart", x1, ROW2_Y + 40, w1, ROW2_H - 40,
        roles={"Category": hierarchy("Date", "Calendar", ["Year", "Quarter", "Month Name"],
                                     active=["Year", "Quarter", "Month Name"]),
               "Y": "_Measures.Revenue"},
        title="Monthly trend of the metric picked above",
        subtitle="Year → quarter → month (drill up for years) · Black Friday 2017 is the revenue peak",
        objects={"lineStyles": props(strokeWidth=2.0),
                 # 10-s15: analytics pane forecast (Desktop draws it only on a continuous date axis: drill to months)
                 "forecast": props(show=True, displayName="Forecast", style="dotted")})
    edit(p, "trend_revenue", lambda d: d["visual"]["query"]["queryState"]["Y"].update(
        fieldParameters=[{"parameterExpr": field("Metric.Metric"), "index": 0, "length": 1}]))
    # 7-s2: Breakdown field parameter on the axis of the bar chart
    param_slicer(p, "slicer_breakdown", x2, ROW2_Y, w2, 34, "Breakdown.Breakdown", "Category Group",
                 "Slicer: pick what to break revenue down by")
    raw(p, "bar_group", "barChart", x2, ROW2_Y + 40, w2, ROW2_H - 40,
        roles={"Category": "Product.Category Group", "Y": "_Measures.Revenue"},
        title="Revenue by the breakdown picked above", subtitle="Category group · customer region · seller tier · distance band",
        sort=("_Measures.Revenue", "Descending"))
    edit(p, "bar_group", lambda d: d["visual"]["query"]["queryState"]["Category"].update(
        fieldParameters=[{"parameterExpr": field("Breakdown.Breakdown"), "index": 0, "length": 1}]))
    (a, aw), (b, bw), (c, cw) = cols(3)
    # 4-s10: drill down / up Region -> State -> City through the Customer[Geo] hierarchy
    raw(p, "tree_region", "treemap", a, ROW3_Y, aw, ROW3_H,
        roles={"Group": hierarchy("Customer", "Geo", ["Region", "State Name", "City"]), "Values": "_Measures.Revenue"},
        title="Where customers are", subtitle="Revenue by region · drill down to state and city · Southeast dominates")
    visual(p, "donutChart", "donut_payment", b, ROW3_Y, bw, ROW3_H,
           ["Category:Payments.Payment Type", "Y:_Measures.Payment Value"], "How customers pay", "Share of payment value")
    visual(p, "columnChart", "col_weekday", c, ROW3_Y, cw, ROW3_H,
           ["Category:Date.Weekday Name", "Y:_Measures.Order Count"], "Orders by weekday", "Shoppers buy on Mon–Tue, not weekends")
    # 17-s7: donut highlights (not filters) the trend; weekday clicks leave the KPI cards alone
    INTERACTIONS[p] = [{"source": "donut_payment", "target": "trend_revenue", "type": "HighlightFilter"}] + [
        {"source": "col_weekday", "target": k, "type": "NoFilter"}
        for k in ("kpi_revenue", "kpi_orders", "kpi_aov", "kpi_review", "kpi_late")]
    # 4-s13: definitions overlay (visual group, hidden until the info button applies bm_defs_show)
    gx, gy, gw, gh = X0 + 180, ROW2_Y - 20, 680, 300
    group_box(p, "grp_defs", "Definitions overlay", gx, gy, gw, gh, hidden=True)
    textbox(p, "defs_text", gx, gy, gw, gh, ("Definitions", 13, INK, True),
            *[(f"{k} — {v}", 10, INK2, False) for k, v in DEFS], group="grp_defs", boxed=True, z=5001)
    button(p, "button_defs_close", gx + gw - 96, gy + gh - 44, 80, 30, "Close", {"type": "Bookmark", "bookmark": "bm_defs_hide"},
           "Button: close the definitions", group="grp_defs", style="primary", z=5002)
    for name, hide in (("bm_defs_show", False), ("bm_defs_hide", True)):
        bookmark(name, f"Definitions: {'hide' if hide else 'show'}", p, groups={"grp_defs": hide}, targets=["grp_defs"])
    # 17-s12: phone layout
    MOBILE[p] = phone(["slicer_year"], ["kpi_revenue", "kpi_orders", "kpi_aov", "kpi_review"], ["trend_revenue"])


def page_sales(p):
    chrome(p, "Sales & Products", "Which categories earn the money, how they rate, and when people shop.")
    kpis(p, [
        ("kpi_gmv", "GMV", None), ("kpi_items", "Items Sold", None), ("kpi_price", "Avg Item Price", None),
        ("kpi_freight", "Freight Share %", None), ("kpi_ytd", "Revenue YTD", None),
    ])
    # 4-s10 / 4-s11 / 4-s8: Category Tree hierarchy (on Category, drill up to group), report-page tooltip, Top N 10
    raw(p, "bar_category", "barChart", X0, ROW2_Y, 400, ROW2_H,
        roles={"Category": hierarchy("Product", "Category Tree", ["Category Group", "Category"], active=["Category"]),
               "Y": "_Measures.Revenue",
               "Tooltips": ["_Measures.Category Share in Group %", "_Measures.Customers Buying Category"]},
        title="Top 10 categories by revenue", subtitle="Top N filter · hover for the category card · drill up to the group",
        sort=("_Measures.Revenue", "Descending"),
        container={"visualTooltip": props(show=True, type="Canvas", section=pid("TT Category"))})
    pbir("add", "filter", "Product", "Category", "-v", f"{page_path(p)}/bar_category.Visual", "--type", "TopN",
         "--n", "10", "--by-table", "_Measures", "--by-field", "Revenue")
    visual(p, "scatterChart", "scatter_category", X0 + 400 + GAP, ROW2_Y, RIGHT - X0 - 400 - GAP, ROW2_H,
           ["Category:Product.Category", "X:_Measures.Avg Item Price", "Y:_Measures.Item Avg Review", "Size:_Measures.Items Sold"],
           "Price vs satisfaction by category", "Bubble size = items sold · expensive categories don't rate better")
    # 17-s9: extra fields on the tooltip
    add_role(p, "scatter_category", "Tooltips",
             ["_Measures.Seller Late Rate %", "_Measures.Freight Share %", "_Measures.Category Share in Group %"])
    m = visual(p, "pivotTable", "matrix_hour", X0, ROW3_Y, 640, ROW3_H,
               ["Rows:Date.Weekday Name", "Columns:Orders.Order Hour", "Values:_Measures.Orders Placed"],
               "When orders are placed", "Orders by weekday × hour of purchase")
    pbir("visuals", "cf", m, "--gradient", "--field", "_Measures.Orders Placed", "--min-color", BLUE_LO, "--max-color", BLUE_HI, "--on", "values.backColor")
    visual(p, "stackedAreaChart", "area_group", X0 + 640 + GAP, ROW3_Y, RIGHT - X0 - 640 - GAP, ROW3_H,
           ["Category:Date.Month Start", "Series:Product.Category Group", "Y:_Measures.Revenue"],
           "Category mix over time", "Monthly revenue stacked by category group")
    # 7-s5: the Time Calc calculation group as matrix columns (rows = 2018 months, like the twin)
    raw(p, "matrix_timecalc", "pivotTable", X0, ROW4_Y, RIGHT - X0, ROW4_H,
        roles={"Rows": "Date.Year Month", "Columns": "Time Calc.Time Calc", "Values": "_Measures.Revenue"},
        title="Revenue through the Time Calc group", subtitle="One measure, six calculation items (Current, PY, YoY %, YTD, MoM %, 3M Avg) · 2018",
        objects={"grid": props(rowPadding=4), "columnHeaders": props(wordWrap=False), "values": props(wordWrap=False)},
        filters=[cat_filter("year_2018", "Date.Year", [2018])])
    # 10-s13: who made 2018 grow: revenue change vs Jan–Aug 2017 per category group
    (a, aw), (c, cw) = cols(2)
    raw(p, "waterfall_yoy", "waterfallChart", a, ROW5_Y, aw, ROW5_H,
        roles={"Category": "Product.Category Group", "Y": "_Measures.Revenue YoY Δ"},
        title="Who drove 2018 growth", subtitle="Revenue change vs same months of 2017, by category group · total = growth",
        sort=("_Measures.Revenue YoY Δ", "Descending"),
        objects={"sentimentColors": props(increaseFill=color(GOOD), decreaseFill=color(BAD), totalFill=color(NAVY)),
                 "labels": props(show=True)},
        filters=[cat_filter("year_2018_wf", "Date.Year", [2018])])
    # 10-s15: ETS forecast (statsmodels) with its 80% interval, plus the built-in forecast on the actual line
    raw(p, "trend_forecast", "lineChart", c, ROW5_Y, cw, ROW5_H,
        roles={"Category": "Date.Month Start",
               "Y": ["_Measures.Revenue Actual (to Aug 2018)", "_Measures.Forecast Revenue",
                     "_Measures.Forecast Lower 80", "_Measures.Forecast Upper 80"]},
        title="Revenue forecast Sep–Dec 2018", subtitle="ETS (additive damped trend) · band = 80% prediction interval",
        objects={"forecast": props(show=True, displayName="Built-in forecast", style="dotted"),
                 "lineStyles": props(strokeWidth=2.0)},
        filters=[cat_filter("years_17_18", "Date.Year", [2017, 2018])])


def page_customers(p):
    chrome(p, "Customers & Retention", "97% of customers buy once. Who is worth keeping, and how fast cohorts fade.",
           ("slicer_state", "Customer.State Name", "Customer state"))
    kpis(p, [
        ("kpi_customers", "Customers", None), ("kpi_new", "New Customers", None), ("kpi_returning", "Returning Customers", None),
        ("kpi_period", "Period Counted", None), ("kpi_repeat", "Repeat Order %", None),
        ("kpi_rpc", "Revenue per Customer", None), ("kpi_repeat_rate", "Repeat Customer Rate", None),
    ])
    (a, aw), (b, bw), (c, cw) = cols(3)
    visual(p, "barChart", "bar_rfm_count", a, ROW2_Y, aw, ROW2_H,
           ["Category:Customer.RFM Segment", "Y:_Measures.Customer Count"], "Customers by RFM segment", "Recency · frequency · monetary")
    visual(p, "barChart", "bar_rfm_value", b, ROW2_Y, bw, ROW2_H,
           ["Category:Customer.RFM Segment", "Y:_Measures.Customer Lifetime Value"], "Lifetime value by RFM segment", "New high-value + at-risk big spenders hold most revenue")
    visual(p, "columnChart", "col_repeat", c, ROW2_Y, cw, ROW2_H,
           ["Category:Date.Month Start", "Y:_Measures.Repeat Order %"], "Repeat order share", "Orders from customers who bought before")
    m = visual(p, "pivotTable", "matrix_cohort", X0, ROW3_Y, 700, ROW3_H,
               ["Rows:Cohort Retention.Cohort Label", "Columns:Cohort Retention.Month Offset Label", "Values:_Measures.Avg Retention %"],
               "Cohort retention", "Share of each first-purchase cohort ordering again N months later")
    pbir("visuals", "cf", m, "--gradient", "--field", "_Measures.Avg Retention %", "--min-color", "#FFFFFF", "--max-color", PALETTE[0], "--on", "values.backColor")
    visual(p, "barChart", "bar_state_customers", X0 + 700 + GAP, ROW3_Y, RIGHT - X0 - 700 - GAP, ROW3_H,
           ["Category:Customer.State Name", "Y:_Measures.Customers"], "Customers by state", "São Paulo alone is ~40% of the base")
    pbir("visuals", "sort", f"{page_path(p)}/bar_state_customers.Visual", "--field", "_Measures.Customers", "--direction", "Descending")
    # 10-s3 dynamic bands · 10-s8 churn · 10-s9 predicted CLV
    (a, aw), (b, bw), (c, cw) = cols(3)
    raw(p, "col_spend_band", "columnChart", a, ROW4_Y, aw, ROW4_H,
        roles={"Category": "Spend Bands.Spend Band", "Y": "_Measures.Customers in Band", "Tooltips": ["_Measures.Revenue in Band"]},
        title="Customers by spend band", subtitle="Band = revenue per customer in the current filters (dynamic)")
    visual(p, "cardVisual", "card_churn", b, ROW4_Y, bw, ROW4_H,
           ["Data:_Measures.Churn Rate %", "Data:_Measures.Churned Customers", "Data:_Measures.At-Risk Customers (90–180d)"],
           "Churn: no purchase for 180+ days", "At risk = 90–179 days since the last purchase")
    visual(p, "barChart", "bar_clv_rfm", c, ROW4_Y, cw, ROW4_H,
           ["Category:Customer.RFM Segment", "Y:_Measures.Avg Predicted CLV"],
           "Predicted 12-month value per customer", "BG/NBD × Gamma-Gamma (src/predict.py) · by RFM segment")
    # 4-s9: an RFM click must not cut the cohort grid; it does filter the state bars
    INTERACTIONS[p] = [{"source": "bar_rfm_count", "target": "matrix_cohort", "type": "NoFilter"},
                       {"source": "bar_rfm_count", "target": "bar_state_customers", "type": "DataFilter"}]


def page_delivery(p):
    chrome(p, "Delivery & Satisfaction", "Late orders are the #1 driver of bad reviews — where and when the delivery promise breaks.",
           ("slicer_state", "Customer.State Name", "Customer state"))
    kpis(p, [
        ("kpi_ontime", "On-Time Rate %", None), ("kpi_days", "Avg Delivery Days", None),
        ("kpi_promised", "Avg Promised Days", None), ("kpi_avgreview", "Avg Review", None), ("kpi_bad", "Bad Review %", None),
    ])
    v = visual(p, "columnChart", "col_bucket_review", X0, ROW2_Y, 500, ROW2_H,
               ["Category:Orders.Delivery Bucket", "Y:_Measures.Avg Review"], "Average review by delivery outcome", "Late > 7 days averages 1.7★ vs 4.3★ when early")
    pbir("visuals", "cf", v, "--gradient", "--field", "_Measures.Avg Review", "--min-color", BAD, "--max-color", PALETTE[0])
    pbir("visuals", "labels", v, "--show", "--labelPrecision", "2")
    visual(p, "lineChart", "trend_late", X0 + 500 + GAP, ROW2_Y, RIGHT - X0 - 500 - GAP, ROW2_H,
           ["Category:Date.Month Start", "Y:_Measures.Late Rate %"], "Late-delivery rate by month", "Spikes follow demand peaks: Nov 2017 and Feb–Mar 2018")
    edit(p, "trend_late", lambda d: d["visual"].setdefault("visualContainerObjects", {}).update(
        general=[{"properties": {"altText": {"expr": field("_Measures.Alt Text Late Rate")}}}]))
    # 17-s17: analytics pane: trend line + constant line at the 8% target
    edit(p, "trend_late", lambda d: d["visual"].setdefault("objects", {}).update(
        trend=props(show=True, displayName="Trend", lineColor=color(MUTED), style="dashed", transparency=20.0),
        y1AxisReferenceLine=[{"properties": props(show=True, value=0.08, displayName="Target 8%", lineColor=color(BAD),
                                                  style="dashed", dataLabelShow=True, dataLabelText="ValueAndName")[0]["properties"],
                              "selector": {"id": "1"}}]))
    b = visual(p, "barChart", "bar_state_late", X0, ROW3_Y, 400, ROW3_H,
               ["Category:Customer.State", "Y:_Measures.Late Rate %", "Tooltips:_Measures.Late Status"],
               "Late rate by customer state", "▲ High ≥ 12% (red) · ● Watch ≥ 8% (orange) · hover for the status")
    pbir("visuals", "sort", b, "--field", "_Measures.Late Rate %", "--direction", "Descending")
    pbir("visuals", "cf", b, "--measure", "dataPoint.fill _Measures.Late Rate Color")
    # 18-s7 / 17-s10: the same numbers as a filled map, hidden until the "Map" button applies its bookmark
    raw(p, "map_state_late", "azureMap", X0, ROW3_Y, 400, ROW3_H, hidden=True,
        roles={"Category": "Customer.State Geo", "Tooltips": ["_Measures.Late Rate %", "_Measures.Late Status"]},
        title="Late rate by customer state (map)", subtitle="Filled map · colour = Late Rate Color (red ≥ 12%, orange ≥ 8%)",
        objects={"filledMap": props(show=True), "bubbleLayer": props(show=False),
                 "dataPoint": [{"properties": {"fill": expr_color("_Measures.Late Rate Color")}, "selector": WILDCARD}]})
    bx = X0 + 400 - 14 - 116
    group_box(p, "grp_view", "Bars / Map buttons", bx, ROW3_Y + 10, 116, 24)
    button(p, "button_bars", bx, ROW3_Y + 10, 56, 24, "Bars", {"type": "Bookmark", "bookmark": "bm_view_bars"},
           "Button: show late rate by state as bars", group="grp_view", style="primary", z=5001)
    button(p, "button_map", bx + 60, ROW3_Y + 10, 56, 24, "Map", {"type": "Bookmark", "bookmark": "bm_view_map"},
           "Button: show late rate by state on a map", group="grp_view", z=5001)
    views = ("bar_state_late", "map_state_late")
    bookmark("bm_view_bars", "View: bars", p, {v: v != "bar_state_late" for v in views}, targets=list(views))
    bookmark("bm_view_map", "View: map", p, {v: v != "map_state_late" for v in views}, targets=list(views))
    t = visual(p, "tableEx", "table_lanes", X0 + 400 + GAP, ROW3_Y, RIGHT - X0 - 400 - GAP, ROW3_H,
               ["Values:Freight Lanes.Lane", "Values:_Measures.Lane Items", "Values:_Measures.Lane Avg Freight",
                "Values:_Measures.Lane Avg Delivery Days", "Values:_Measures.Lane Late Rate %", "Values:_Measures.Lane Late Status"],
               "Busiest freight lanes (seller → customer state)", "Visual filter: lanes with ≥ 30 items · status = ▲ / ● / ✓")
    pbir("visuals", "sort", t, "--field", "_Measures.Lane Items", "--direction", "Descending")
    pbir("visuals", "cf", t, "--data-bars", "--field", "_Measures.Lane Items")
    pbir("visuals", "cf", t, "--gradient", "--field", "_Measures.Lane Late Rate %", "--min-color", "#FFFFFF", "--max-color", "#F19A99", "--on", "values.backColor")
    # 18-s13: small lanes are hidden by a visual-level filter, not just sorted away
    edit(p, "table_lanes", lambda d: d.setdefault("filterConfig", {"filters": []})["filters"].append(
        measure_filter("lanes_min_30", "_Measures.Lane Items", 2, 30)))
    # 10-s14: P90 of the customer state next to each lane, ▲ when the lane is slower than that
    add_role(p, "table_lanes", "Values", ["_Measures.Lane State P90", "_Measures.Lane Outlier"])
    # 2-s9: same orders on two dates (USERELATIONSHIP on the inactive Estimated Date relationship) + backlog snapshot
    (a, aw), (c, cw) = cols(2)
    visual(p, "lineChart", "trend_late_promise", a, ROW4_Y, aw, ROW4_H,
           ["Category:Date.Month Start", "Y:_Measures.Late Rate %", "Y:_Measures.Late Rate % (by Promise Month)"],
           "Late rate: month bought vs month promised", "Same orders, two dates (inactive relationship via USERELATIONSHIP)")
    visual(p, "areaChart", "area_backlog", c, ROW4_Y, cw, ROW4_H,
           ["Category:Date.Date", "Y:_Measures.Orders In Transit (EoP)"],
           "Orders in transit (end of day)", "Bought, not yet delivered · a snapshot: last day of the period, never a sum")
    # 4-s8: page filter (delivered orders only)
    PAGE_FILTERS[p] = [cat_filter("page_delivered", "Orders.Is Delivered", [True], displayName="Delivered orders only",
                                  howCreated="User")]
    # 10-s12 funnel · 10-s16 what-if · 17-s17 grouping (Delay Days bins)
    (a, aw), (b, bw), (c, cw) = cols(3)
    raw(p, "funnel_lifecycle", "funnel", a, ROW5_Y, aw, ROW5_H,
        roles={"Category": "Funnel Stage.Stage", "Y": "_Measures.Funnel Orders", "Tooltips": ["_Measures.Funnel Conversion %"]},
        title="Order lifecycle funnel", subtitle="Purchased → approved → carrier → delivered → reviewed · all orders",
        objects={"labels": props(show=True), "percentBarLabel": props(show=True)})
    raw(p, "slicer_late_reduction", "slicer", b, ROW5_Y, bw, 70, roles={"Values": "Late Reduction (pp).Late Reduction (pp)"},
        title="What if: cut the late rate by … percentage points", alt="What-if slider: late-rate reduction in percentage points",
        objects={"data": props(mode="Single"), "header": props(show=False)})
    for i, (name, m) in enumerate([("kpi_proj_late", "Projected Late Rate %"), ("kpi_proj_bad", "Projected Bad Review %"),
                                   ("kpi_bad_delta", "Bad Review Δ (what-if)")]):
        ww = (bw - 2 * 8) // 3
        visual(p, "cardVisual", name, b + i * (ww + 8), ROW5_Y + 78, ww, ROW5_H - 78, data=[f"Data:_Measures.{m}"])
    raw(p, "col_delay_bins", "columnChart", c, ROW5_Y, cw, ROW5_H,
        roles={"Category": "Orders.Delay Days (bins)", "Y": "_Measures.Bad Review %"},
        title="Bad reviews by days vs promise (3-day bins)", subtitle="Grouping: Delay Days (bins) · < 0 = early, > 0 = late",
        sort=("Orders.Delay Days (bins)", "Ascending"))
    # 18-s6: phone layout for people in the field
    MOBILE[p] = phone(["slicer_year"], ["kpi_ontime", "kpi_bad"], ["trend_late", "bar_state_late"])


def page_sellers(p):
    chrome(p, "Sellers & Logistics", "A few sellers make half the revenue; distance decides what customers pay for freight.")
    kpis(p, [
        ("kpi_sellers", "Active Sellers", None), ("kpi_rps", "Revenue per Seller", None),
        ("kpi_toptier", "Top-Tier Revenue Share %", None), ("kpi_seller_late", "Seller Late Rate %", None),
        ("kpi_risk", "Revenue at Risk (Low-Rated Sellers)", None), ("kpi_distance", "Avg Distance km", None),
    ])
    visual(p, "scatterChart", "scatter_sellers", X0, ROW2_Y, 620, ROW2_H,
           ["Category:Seller.Seller Code", "X:_Measures.Revenue", "Y:_Measures.Item Avg Review", "Size:_Measures.Order Count", "Series:Seller.Pareto Tier"],
           "Every seller: revenue vs rating", "Colour = Pareto tier · size = orders · right-click → Drill through → Seller Detail")
    add_role(p, "scatter_sellers", "Tooltips", ["_Measures.Seller Late Rate %", "_Measures.Freight Share %"])
    # 17-s17: P90 of seller revenue as an X reference line (value bound to a PERCENTILEX.INC measure, follows the slicers)
    edit(p, "scatter_sellers", lambda d: d["visual"].setdefault("objects", {}).update(
        xAxisReferenceLine=[{"properties": {**props(show=True, displayName="P90 revenue", lineColor=color(BAD), style="dashed",
                                                    dataLabelShow=True, dataLabelText="ValueAndName")[0]["properties"],
                                            "value": {"expr": field("_Measures.Seller Revenue P90")}},
                             "selector": {"id": "1"}}]))
    visual(p, "donutChart", "donut_tier", X0 + 620 + GAP, ROW2_Y, RIGHT - X0 - 620 - GAP, ROW2_H,
           ["Category:Seller.Pareto Tier", "Y:_Measures.Revenue"], "Revenue by seller tier", "Tier A = ~4% of sellers")
    t = visual(p, "tableEx", "table_sellers", X0, ROW3_Y, 620, ROW3_H,
               ["Values:Seller.Seller Code", "Values:Seller.State", "Values:Seller.Pareto Tier", "Values:_Measures.Revenue",
                "Values:_Measures.Order Count", "Values:_Measures.Item Avg Review", "Values:_Measures.Seller Late Rate %"],
               "Seller leaderboard", "Sorted by revenue · right-click a seller → Drill through → Seller Detail")
    pbir("visuals", "sort", t, "--field", "_Measures.Revenue", "--direction", "Descending")
    pbir("visuals", "cf", t, "--data-bars", "--field", "_Measures.Revenue")
    c = visual(p, "columnChart", "col_distance", X0 + 620 + GAP, ROW3_Y, RIGHT - X0 - 620 - GAP, ROW3_H,
               ["Category:Order Items.Distance Band", "Y:_Measures.Freight Share %"], "Freight share by distance", "Freight ÷ (price + freight) · 10% under 100 km → 19% past 2,000 km")
    pbir("visuals", "labels", c, "--show")
    # 18-s7: Azure Map bubbles at each seller's coordinates; 18-s8: Sankey custom visual for the lane flows
    raw(p, "map_sellers", "azureMap", X0, ROW4_Y, 620, ROW4_H,
        roles={"Category": "Seller.Seller Code", "Y": "_Measures.Seller Lat", "X": "_Measures.Seller Lng", "Size": "_Measures.Revenue",
               "Series": "Seller.Pareto Tier", "Tooltips": ["_Measures.Item Avg Review", "_Measures.Seller Late Rate %"]},
        title="Where sellers are", subtitle="Azure Maps · bubble per seller at its zip-prefix centroid · size = revenue · colour = tier",
        objects={"bubbleLayer": props(show=True), "legend": props(show=True, position="Top")})
    raw(p, "sankey_lanes", SANKEY, X0 + 620 + GAP, ROW4_Y, RIGHT - X0 - 620 - GAP, ROW4_H,
        roles={"Source": "Freight Lanes.Seller Region", "Destination": "Freight Lanes.Customer Region",
               "Weight": "_Measures.Lane Items"},
        title="Where goods flow", subtitle="Sankey custom visual (AppSource) · seller region → customer region · items")
    # 10-s10 tier transition 2017 → 2018 · 10-s2 ABC that follows the slicers
    m = visual(p, "pivotTable", "matrix_transition", X0, ROW5_Y, 620, ROW5_H,
               ["Rows:Seller Tier Transition.Tier 2017", "Columns:Seller Tier Transition.Tier 2018",
                "Values:_Measures.Transition Sellers", "Values:_Measures.Transition Share %"],
               "Seller tier 2017 → 2018 (Jan–Aug)", "Rows = 2017 tier (New = no 2017 sale) · columns = 2018 tier")
    pbir("visuals", "cf", m, "--gradient", "--field", "_Measures.Transition Share %", "--min-color", "#FFFFFF", "--max-color", PALETTE[0], "--on", "values.backColor")
    t = visual(p, "tableEx", "table_abc", X0 + 620 + GAP, ROW5_Y, RIGHT - X0 - 620 - GAP, ROW5_H,
               ["Values:Seller.Seller Code", "Values:Seller.Pareto Tier", "Values:_Measures.Seller ABC (dynamic)",
                "Values:_Measures.Seller Rank (selection)", "Values:_Measures.Revenue"],
               "Lifetime tier vs tier in this selection", "Pick a year or category: Seller ABC (dynamic) re-ranks, Pareto Tier doesn't")
    pbir("visuals", "sort", t, "--field", "_Measures.Revenue", "--direction", "Descending")


def page_why(p):
    """10-s17: diagnosis path for the early-2018 drop: KPI -> late rate by promise month -> decomposition tree -> key influencers."""
    chrome(p, "Why Reviews Dropped", "1 the KPI → 2 late deliveries by promise month → 3 where (decomposition tree) → 4 what drives it.",
           ("slicer_state", "Customer.State Name", "Customer state"))
    kpis(p, [("kpi_w_review", "Avg Review", None), ("kpi_w_bad", "Bad Review %", None),
             ("kpi_w_late", "Late Rate %", None), ("kpi_w_late_promise", "Late Rate % (by Promise Month)", None)])
    visual(p, "lineChart", "trend_why", X0, ROW2_Y, RIGHT - X0, ROW2_H,
           ["Category:Date.Month Start", "Y:_Measures.Bad Review %", "Y:_Measures.Late Rate % (by Promise Month)"],
           "Step 2 · bad reviews move with late deliveries",
           "Late rate on the month the delivery was promised · the Feb–Mar 2018 spike drags reviews down")
    (a, aw), (b, bw) = cols(2)
    raw(p, "decomp_bad", "decompositionTreeVisual", a, ROW3_Y, aw, ROW3_H,
        roles={"Analyze": "_Measures.Bad Review %",
               "ExplainBy": ["Orders.Delivery Bucket", "Customer.Region", "Customer.State Name", "Seller.Pareto Tier",
                             "Orders.Main Payment Type"]},
        title="Step 3 · where the bad reviews sit", subtitle="Decomposition tree · click + to split by bucket, region, state, tier")
    raw(p, "ki_bad", "keyDriversVisual", b, ROW3_Y, bw, ROW3_H,
        roles={"Target": "Orders.Review Outcome",
               "ExplainBy": ["Orders.Delivery Bucket", "Orders.Main Payment Type", "Customer.Region", "Seller.Pareto Tier"]},
        title="Step 4 · what makes a review bad", subtitle="Key influencers on Review Outcome · logit check: late × 8.97 odds (insights.html)")
    PAGE_FILTERS[p] = [cat_filter("page_why_delivered", "Orders.Is Delivered", [True], displayName="Delivered orders only",
                                  howCreated="User")]


def page_about(p):
    chrome(p, "About & Definitions", "What the numbers mean, the words Q&A understands, and how to ask your own question.", info=False)
    raw(p, "qna", "qnaVisual", X0, KPI_Y, 520, 624, title="Ask a question",
        subtitle="Q&A visual · try “sales by state”, “late rate by month”, “rating by category”",
        alt="Q&A visual: type a question about the data in plain English")
    from build_model import SYNONYMS
    x, w = X0 + 520 + GAP, RIGHT - X0 - 520 - GAP
    textbox(p, "defs_text", x, KPI_Y, w, 380, ("Definitions", 13, INK, True),
            *[(f"{k} — {v}", 10, INK2, False) for k, v in DEFS],
            ("Rules: revenue and orders count valid orders only (report filter, hidden + locked); the Delivery page "
             "adds a page filter for delivered orders.", 10, INK2, False), boxed=True)
    textbox(p, "synonyms_text", x, KPI_Y + 392, w, 232, ("Words Q&A understands (cultures/en-US.tmdl)", 13, INK, True),
            *[(f"{name}: {', '.join(words)}", 10, INK2, False) for (_, name), words in SYNONYMS.items()], boxed=True)


def page_seller_detail(p):
    """Drillthrough target (Seller[Seller Code]): one seller in full."""
    chrome(p, "Seller Detail", "Drillthrough: one seller, filtered by the page binding on Seller Code.", info=False)
    button(p, "button_back", X0 + 612, 20, 120, 36, "← Back", {"type": "Back"}, "Button: back to the page you drilled from",
           style="primary")
    kpis(p, [("kpi_d_revenue", "Revenue", None), ("kpi_d_orders", "Order Count", None), ("kpi_d_review", "Item Avg Review", None),
             ("kpi_d_late", "Seller Late Rate %", None), ("kpi_d_freight", "Freight Share %", None)])
    raw(p, "line_seller_month", "lineChart", X0, ROW2_Y, 630, ROW2_H,
        roles={"Category": "Date.Month Start", "Y": ["_Measures.Seller Monthly Revenue", "_Measures.Seller Monthly Orders"]},
        title="Revenue and orders per month", subtitle="From the Seller Monthly snapshot table (one row per seller-month)",
        objects={"lineStyles": props(strokeWidth=2.0)})
    raw(p, "bar_seller_category", "barChart", X0 + 630 + GAP, ROW2_Y, RIGHT - X0 - 630 - GAP, ROW2_H,
        roles={"Category": "Product.Category", "Y": "_Measures.Revenue"}, title="What the seller sells",
        subtitle="Revenue by category", sort=("_Measures.Revenue", "Descending"))
    raw(p, "line_seller_quality", "lineChart", X0, ROW3_Y, RIGHT - X0, ROW3_H,
        roles={"Category": "Date.Month Start", "Y": "_Measures.Seller Monthly Late Rate",
               "Y2": "_Measures.Seller Monthly Avg Review"},
        title="Late rate and review per month", subtitle="Late rate (left axis) against the average review (right axis)",
        objects={"lineStyles": props(strokeWidth=2.0)})


def page_tt_category(p):
    """Report-page tooltip for bar_category (actual size 360 × 260, bound to Product[Category])."""
    cards = [("tt_revenue", "Revenue"), ("tt_review", "Item Avg Review"), ("tt_late", "Seller Late Rate %"),
             ("tt_share", "Category Share in Group %"), ("tt_buyers", "Customers Buying Category")]
    for i, (name, m) in enumerate(cards):
        x, y, w = (8 + (i % 3) * 116, 8, 112) if i < 3 else (8 + (i - 3) * 174, 76, 170)
        raw(p, name, "cardVisual", x, y, w, 62, roles={"Data": f"_Measures.{m}"}, scale=1,
            objects={"value": props(fontSize=16.0), "label": props(fontSize=8.0)})  # actual-size page: small callout
    raw(p, "tt_spark", "lineChart", 8, 144, 344, 108, roles={"Category": "Date.Month Start", "Y": "_Measures.Revenue (Last 12 Months)"},
        title="Last 12 months", objects={"lineStyles": props(strokeWidth=2.0), "valueAxis": props(show=False)}, scale=1)


PAGES = [("Executive Overview", page_overview), ("Sales & Products", page_sales), ("Customers & Retention", page_customers),
         ("Delivery & Satisfaction", page_delivery), ("Sellers & Logistics", page_sellers), ("Why Reviews Dropped", page_why),
         ("About & Definitions", page_about)]
HIDDEN = [("TT Category", page_tt_category, "Tooltip", "Product.Category"),
          ("Seller Detail", page_seller_detail, "Drillthrough", "Seller.Seller Code")]
THIRD = {"Customers & Retention": "slicer_state", "Delivery & Satisfaction": "slicer_state", "Why Reviews Dropped": "slicer_state"}

KIND = {"cardVisual": "KPI card", "barChart": "Bar chart", "columnChart": "Column chart", "lineChart": "Line chart",
        "pivotTable": "Matrix", "tableEx": "Table", "slicer": "Slicer", "treemap": "Treemap", "donutChart": "Donut chart",
        "scatterChart": "Scatter chart", "stackedAreaChart": "Stacked area chart", "areaChart": "Area chart",
        "azureMap": "Map", SANKEY: "Sankey diagram", "qnaVisual": "Q&A", "funnel": "Funnel", "waterfallChart": "Waterfall chart",
        "decompositionTreeVisual": "Decomposition tree", "keyDriversVisual": "Key influencers"}


# ================================================================== finishing (JSON post-processing)
def svg(path, h):
    """Canvas wallpaper: plane with the navy sidebar (17-s13; replaces one shape visual per page)."""
    sw = 200 * SCALE
    path.write_text(f"""<svg xmlns="http://www.w3.org/2000/svg" width="{PAGE_W}" height="{h}" viewBox="0 0 {PAGE_W} {h}">
<rect width="{PAGE_W}" height="{h}" fill="{PLANE}"/>
<rect width="{sw:.0f}" height="{h}" fill="{NAVY}"/>
<rect x="{sw - 4:.0f}" width="4" height="{h}" fill="{NAV_ACTIVE}"/>
<line x1="24" y1="{100 * SCALE:.0f}" x2="{sw - 24:.0f}" y2="{100 * SCALE:.0f}" stroke="#184F95" stroke-width="1.5"/>
<line x1="24" y1="{630 * SCALE:.0f}" x2="{sw - 24:.0f}" y2="{630 * SCALE:.0f}" stroke="#184F95" stroke-width="1.5"/>
<line x1="{X0 * SCALE:.0f}" y1="{78 * SCALE:.0f}" x2="{RIGHT * SCALE:.0f}" y2="{78 * SCALE:.0f}" stroke="{HAIR}" stroke-width="1.5"/>
</svg>
""")


def alt_and_tabs(folder):
    """17-s11 alt text on every data visual (kind + title) and 18-s5 tab order = reading order."""
    docs = []
    for f in folder.glob("visuals/*/visual.json"):
        d = load(f)
        docs.append((f, d))
        v = d.get("visual")
        if not v or "query" not in v:
            continue
        vc = v.setdefault("visualContainerObjects", {})
        if "general" not in vc:
            t = vc.get("title", [{}])[0].get("properties", {}).get("text", {}).get("expr", {}).get("Literal", {}).get("Value", "")
            what = t.strip("'") or ", ".join(p["queryRef"].split(".", 1)[-1] for r in v["query"]["queryState"].values()
                                             for p in r["projections"])
            vc["general"] = props(altText=f"{KIND.get(v['visualType'], v['visualType'])}: {what}")
    ordered = sorted((x for x in docs if "visual" in x[1]),
                     key=lambda x: (x[1]["position"]["x"] >= X0 * SCALE - 1, x[1]["position"]["y"], x[1]["position"]["x"]))
    for i, (f, d) in enumerate(ordered):
        d["position"]["tabOrder"] = i * 100
    for f, d in docs:
        dump(f, d)


def finish_page(name, kind=None, bind=None):
    folder = pdir(name)
    page = load(folder / "page.json")
    if kind:
        flt = f"{kind.lower()}_filter"
        page.update({"filterConfig": {"filters": [{"name": flt, "field": field(bind), "type": "Categorical",
                                                   "howCreated": "Drillthrough"}]},
                     "pageBinding": {"name": f"{kind.lower()}_binding", "type": kind,
                                     "parameters": [{"name": f"{kind.lower()}_param", "boundFilter": flt, "fieldExpr": field(bind)}]},
                     "visibility": "HiddenInViewMode"})
    if kind == "Tooltip":
        page.update({"type": "Tooltip", "width": TT_W, "height": TT_H, "displayOption": "ActualSize"})
    else:
        tall = name in TALL_PAGES
        img = f"bg-{page_height(name)}.svg" if tall else "bg.svg"
        page.update({"height": page_height(name), "displayOption": "FitToWidth" if tall else "FitToPage"})
        page["objects"] = {"background": [{"properties": {
            "image": {"image": {"name": lit(img),
                                "url": {"expr": {"ResourcePackageItem": {"PackageName": "RegisteredResources", "PackageType": 1,
                                                                         "ItemName": img}}},
                                "scaling": lit("Fit")}},
            "transparency": lit(0.0)}}],
            "outspace": [{"properties": {"color": color("#E9E8E3")}}]}
    if name in INTERACTIONS:
        page["visualInteractions"] = INTERACTIONS[name]
    if name in PAGE_FILTERS:
        page["filterConfig"] = {"filters": PAGE_FILTERS[name], "filterSortOrder": "Custom"}
    dump(folder / "page.json", page)
    for i, (v, (x, y, w, h)) in enumerate(MOBILE.get(name, {}).items()):
        dump(folder / "visuals" / v / "mobile.json", {"$schema": f"{SCHEMA}/visualContainerMobileState/2.4.0/schema.json",
                                                      "position": {"x": x, "y": y, "z": i * 100, "width": w, "height": h,
                                                                   "tabOrder": i * 100}})
    alt_and_tabs(folder)


def finish_report():
    d = PBI / R / "definition"
    res = PBI / R / "StaticResources" / "RegisteredResources"
    images = {"bg.svg": PAGE_H} | {f"bg-{page_height(n)}.svg": page_height(n) for n in TALL_PAGES}
    for n, h in images.items():
        svg(res / n, h)
    rep = load(d / "report.json")
    rep["resourcePackages"][0]["items"] += [{"name": n, "path": n, "type": "Image"} for n in images]
    rep["publicCustomVisuals"] = [SANKEY]
    # 17-s14: only summarized data can be exported (no underlying rows)
    rep["settings"] = {**rep.get("settings", {}), "exportDataMode": "AllowSummarized"}
    # 4-s8: report filter, hidden and locked: cancelled / unavailable orders never reach a visual
    rep["filterConfig"] = {"filters": [cat_filter("report_valid", "Orders.Is Valid", [True], displayName="Valid orders only",
                                                  howCreated="User", isHiddenInViewMode=True, isLockedInViewMode=True)],
                           "filterSortOrder": "Custom"}
    dump(d / "report.json", rep)
    # 4-s13: reset = every page's slicers back to All (data only, stays on the current page)
    BOOKMARKS.append({"name": "bm_reset_filters", "displayName": "Reset filters",
                      "options": {"suppressDisplay": True, "suppressActiveSection": True},
                      "explorationState": {"version": "1.3", "activeSection": pid(PAGES[0][0]), "sections": {
                          pid(name): {"visualContainers": {v: {"singleVisual": {"visualType": "slicer", "objects": {}}}
                                                           for v in ("slicer_year", "slicer_region", THIRD.get(name, "slicer_group"))}}
                          for name, _ in PAGES}}})
    dump(d / "bookmarks" / "bookmarks.json", {"$schema": f"{SCHEMA}/bookmarksMetadata/1.0.0/schema.json",
                                              "items": [{"name": b["name"]} for b in BOOKMARKS]})
    for b in BOOKMARKS:
        dump(d / "bookmarks" / f"{b['name']}.bookmark.json", {"$schema": f"{SCHEMA}/bookmark/2.1.0/schema.json", **b})


# ================================================================== thin report (15-s5, 18-s10)
def build_ops():
    """Olist.Ops.Report: one page, 8 visuals, no model of its own — the source of the 'Olist Ops' dashboard tiles."""
    for old in [PBI / OPS, PBI / "Olist.Ops.pbip"]:
        shutil.rmtree(old, ignore_errors=True) if old.is_dir() else old.unlink(missing_ok=True)
    pbir("new", "report", OPS, "--thick", "--no-title")
    pbir("report", "rebind", OPS, "--local", "../Olist.SemanticModel")
    first = next((PBI / OPS / "definition" / "pages").glob("*/page.json"))
    pbir("pages", "rename", f"{OPS}/{load(first)['displayName']}.Page", "--to", "Olist Ops", "-f")
    pbir("rm", f"{OPS}/**/*.Visual", "-f", check=False)
    theme(OPS)
    p = "Olist Ops"
    textbox(p, "title", 24, 16, 800, 56, ("Olist Ops", 20, INK, True),
            ("Pinned to the 'Olist Ops' dashboard in the Service — one tile per visual", 10, INK2, False), scale=1, report=OPS)
    for (x, w), (name, m) in zip(cols(4, 24, 1256, 16), [("tile_revenue", "Revenue"), ("tile_yoy", "Revenue YoY %"),
                                                         ("tile_late", "Late Rate %"), ("tile_review", "Avg Review")]):
        raw(p, name, "cardVisual", x, 84, w, 120, roles={"Data": f"_Measures.{m}"}, report=OPS, scale=1)
    raw(p, "tile_trend", "lineChart", 24, 220, 760, 480, roles={"Category": "Date.Month Start", "Y": "_Measures.Revenue"},
        title="Revenue per month", report=OPS, scale=1)
    raw(p, "tile_late_state", "barChart", 800, 220, 456, 480, roles={"Category": "Customer.State", "Y": "_Measures.Late Rate %"},
        title="Late rate by customer state", sort=("_Measures.Late Rate %", "Descending"), report=OPS, scale=1,
        objects={"dataPoint": [{"properties": {"fill": expr_color("_Measures.Late Rate Color")}, "selector": WILDCARD}]})
    page = load(pdir(p, OPS) / "page.json")
    page.update({"width": 1280, "height": 720, "displayOption": "FitToPage"})
    dump(pdir(p, OPS) / "page.json", page)
    alt_and_tabs(pdir(p, OPS))


# ================================================================== paginated (17-s14)
RDL_DAX = """EVALUATE
SUMMARIZECOLUMNS (
    Seller[Seller Code], Seller[State], Seller[Pareto Tier], 'Date'[Year Month Key], 'Date'[Year Month],
    "Revenue", [Revenue], "Orders", [Order Count], "Avg Review", [Item Avg Review], "Late Rate", [Seller Late Rate %]
)
ORDER BY Seller[Seller Code], 'Date'[Year Month Key]"""
RDL_SELLERS = """EVALUATE
SELECTCOLUMNS ( FILTER ( ADDCOLUMNS ( VALUES ( Seller[Seller Code] ), "R", [Revenue] ), NOT ISBLANK ( [R] ) ),
    "Seller Code", Seller[Seller Code], "Revenue", [R] )
ORDER BY [Revenue] DESC"""
RDL_TOP = """EVALUATE
SELECTCOLUMNS ( TOPN ( 10, ADDCOLUMNS ( VALUES ( Seller[Seller Code] ), "R", [Revenue] ), [R], DESC ),
    "Seller Code", Seller[Seller Code] )"""


def write_rdl(path):
    """Seller Scorecard.rdl: multi-value parameter Seller Code (default = top 10 by revenue), one printed page per seller
    (page break on the Seller group) with monthly revenue, orders, review and late rate from the published model.
    Not rendered here: Report Builder / the Service are Windows/cloud only; the dataset id is filled in at publish."""
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
    head = lambda n, t: cell(tb(n, t, bold=True, align="Right" if n != "HMonth" else "Left", color_=MUTED))
    rows = [
        row(0.45, [cell(tb("SellerName", '="Seller scorecard · " & Fields!Seller_Code.Value & " · " & Fields!State.Value'
                                         ' & " · tier " & Fields!Pareto_Tier.Value', bold=True, size=14), 5)]),
        row(0.25, [head("HMonth", "Month"), head("HRevenue", "Revenue"), head("HOrders", "Orders"),
                   head("HReview", "Avg review"), head("HLate", "Late rate")]),
        row(0.22, [cell(tb("Month", "=Fields!Year_Month.Value")),
                   cell(tb("Revenue", "=Fields!Revenue.Value", "R$ #,0", align="Right")),
                   cell(tb("Orders", "=Fields!Orders.Value", "#,0", align="Right")),
                   cell(tb("Review", "=Fields!Avg_Review.Value", "0.00", align="Right")),
                   cell(tb("Late", "=Fields!Late_Rate.Value", "0.0%", align="Right"))]),
        row(0.25, [cell(tb("TotalLabel", "Total", bold=True)),
                   cell(tb("TotalRevenue", "=Sum(Fields!Revenue.Value)", "R$ #,0", bold=True, align="Right")),
                   cell(tb("TotalOrders", "=Sum(Fields!Orders.Value)", "#,0", bold=True, align="Right")),
                   cell(tb("TotalBlank1", "")), cell(tb("TotalBlank2", ""))]),
    ]
    fields = lambda names: "".join(f'<Field Name="{n.replace(" ", "_")}"><DataField>[{n}]</DataField></Field>' for n in names)
    ds = lambda name, dax, names, extra="": (f'<DataSet Name="{name}"><Query><DataSourceName>Olist</DataSourceName>'
                                             f"<CommandText>{escape(dax)}</CommandText></Query><Fields>{fields(names)}</Fields>{extra}</DataSet>")
    flt = ("<Filters><Filter><FilterExpression>=Fields!Seller_Code.Value</FilterExpression><Operator>In</Operator>"
           "<FilterValues><FilterValue>=Parameters!SellerCode.Value</FilterValue></FilterValues></Filter></Filters>")
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<Report xmlns="http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition" xmlns:rd="http://schemas.microsoft.com/SQLServer/reporting/reportdesigner">
  <AutoRefresh>0</AutoRefresh>
  <DataSources>
    <DataSource Name="Olist">
      <ConnectionProperties>
        <DataProvider>PBIDATASET</DataProvider>
        <ConnectString>Data Source=pbiazure://api.powerbi.com/;Identity Provider="https://login.microsoftonline.com/common, https://analysis.windows.net/powerbi/api, 929d0ec0-7a41-4b1e-bc7c-b754a28bddcc";Initial Catalog=sobe_wowvirtualserver-DATASET_ID_SET_AT_PUBLISH;Integrated Security=ClaimsToken</ConnectString>
      </ConnectionProperties>
      <rd:SecurityType>None</rd:SecurityType>
    </DataSource>
  </DataSources>
  <DataSets>
    {ds("Scorecard", RDL_DAX, ["Seller Code", "State", "Pareto Tier", "Year Month Key", "Year Month", "Revenue", "Orders", "Avg Review", "Late Rate"], flt)}
    {ds("SellerList", RDL_SELLERS, ["Seller Code", "Revenue"])}
    {ds("TopSellers", RDL_TOP, ["Seller Code"])}
  </DataSets>
  <ReportParameters>
    <ReportParameter Name="SellerCode">
      <DataType>String</DataType>
      <Prompt>Seller code</Prompt>
      <MultiValue>true</MultiValue>
      <DefaultValue><DataSetReference><DataSetName>TopSellers</DataSetName><ValueField>Seller_Code</ValueField></DataSetReference></DefaultValue>
      <ValidValues><DataSetReference><DataSetName>SellerList</DataSetName><ValueField>Seller_Code</ValueField><LabelField>Seller_Code</LabelField></DataSetReference></ValidValues>
    </ReportParameter>
  </ReportParameters>
  <ReportSections>
    <ReportSection>
      <Body>
        <ReportItems>
          <Tablix Name="Scorecard">
            <TablixBody>
              <TablixColumns>{"".join(f"<TablixColumn><Width>{w}in</Width></TablixColumn>" for w in (2.2, 1.4, 1.0, 1.1, 1.2))}</TablixColumns>
              <TablixRows>{"".join(rows)}</TablixRows>
            </TablixBody>
            <TablixColumnHierarchy><TablixMembers>{"<TablixMember />" * 5}</TablixMembers></TablixColumnHierarchy>
            <TablixRowHierarchy>
              <TablixMembers>
                <TablixMember>
                  <Group Name="Seller"><GroupExpressions><GroupExpression>=Fields!Seller_Code.Value</GroupExpression></GroupExpressions>
                    <PageBreak><BreakLocation>Between</BreakLocation></PageBreak></Group>
                  <SortExpressions><SortExpression><Value>=Fields!Seller_Code.Value</Value></SortExpression></SortExpressions>
                  <TablixMembers>
                    <TablixMember><KeepWithGroup>After</KeepWithGroup><RepeatOnNewPage>true</RepeatOnNewPage></TablixMember>
                    <TablixMember><KeepWithGroup>After</KeepWithGroup><RepeatOnNewPage>true</RepeatOnNewPage></TablixMember>
                    <TablixMember>
                      <Group Name="Details" />
                      <SortExpressions><SortExpression><Value>=Fields!Year_Month_Key.Value</Value></SortExpression></SortExpressions>
                    </TablixMember>
                    <TablixMember><KeepWithGroup>Before</KeepWithGroup></TablixMember>
                  </TablixMembers>
                </TablixMember>
              </TablixMembers>
            </TablixRowHierarchy>
            <DataSetName>Scorecard</DataSetName>
            <Top>0in</Top><Left>0in</Left><Height>1.17in</Height><Width>6.9in</Width>
            <Style><Border><Style>None</Style></Border></Style>
          </Tablix>
        </ReportItems>
        <Height>1.17in</Height>
        <Style />
      </Body>
      <Width>6.9in</Width>
      <Page>
        <PageFooter>
          <Height>0.3in</Height><PrintOnFirstPage>true</PrintOnFirstPage><PrintOnLastPage>true</PrintOnLastPage>
          <ReportItems>{tb("Footer", '="Olist · Seller scorecard · printed " & Format(Globals!ExecutionTime, "dd MMM yyyy") & " · page " & Globals!PageNumber & " of " & Globals!TotalPages', size=8, color_=MUTED).replace("<CanGrow>", "<Top>0.05in</Top><Height>0.22in</Height><Width>6.9in</Width><CanGrow>", 1)}</ReportItems>
          <Style />
        </PageFooter>
        <PageHeight>11.69in</PageHeight><PageWidth>8.27in</PageWidth>
        <LeftMargin>0.6in</LeftMargin><RightMargin>0.6in</RightMargin><TopMargin>0.6in</TopMargin><BottomMargin>0.6in</BottomMargin>
        <Style />
      </Page>
    </ReportSection>
  </ReportSections>
  <rd:ReportUnitType>Inch</rd:ReportUnitType>
  <rd:ReportID>3c9f0a52-7d1e-4b6a-8e2f-5a4d1c7b9e03</rd:ReportID>
</Report>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml)
    import xml.etree.ElementTree as ET
    ET.parse(path)  # well-formed, or this raises


# ================================================================== main
def main():
    if not shutil.which("pbir"):
        sys.exit("pbir CLI not found — install with: uv tool install pbir-cli")
    bad = [(fg, bg, round(contrast(fg, bg), 2)) for fg, bg in SIDEBAR_TEXT if contrast(fg, bg) < 4.5]
    assert not bad, f"sidebar text under 4.5:1 contrast: {bad}"
    for old in [PBI / R, PBI / "Olist.pbip"]:
        shutil.rmtree(old, ignore_errors=True) if old.is_dir() else old.unlink(missing_ok=True)

    pbir("new", "report", R, "--thick", "--no-title")
    pbir("report", "rebind", R, "--local", "../Olist.SemanticModel")
    first = next((PBI / R / "definition" / "pages").glob("*/page.json"))
    pbir("pages", "rename", f"{R}/{load(first)['displayName']}.Page", "--to", PAGES[0][0], "-f")
    pbir("pages", "resize", f"{R}/{PAGES[0][0]}.Page", "--width", str(PAGE_W), "--height", str(PAGE_H))
    for name, _ in PAGES[1:]:
        h = page_height(name)
        pbir("add", "page", f"{R}/{name}.Page", "-n", name, "--width", str(PAGE_W), "--height", str(h))
    for name, _, kind, _ in HIDDEN:
        w, h = (TT_W, TT_H) if kind == "Tooltip" else (PAGE_W, PAGE_H)
        pbir("add", "page", f"{R}/{name}.Page", "-n", name, "--width", str(w), "--height", str(h))
    # `add page` seeds a default title textbox; start every page from an empty canvas
    pbir("rm", f"{R}/**/*.Visual", "-f", check=False)
    theme()
    for name, build in PAGES:
        print(f"  building {name}")
        build(name)
    for name, build, _, _ in HIDDEN:
        print(f"  building {name} (hidden)")
        build(name)
    for name, _ in PAGES:
        finish_page(name)
    for name, _, kind, bind in HIDDEN:
        finish_page(name, kind, bind)
    finish_report()
    pbir("pages", "active-page", R, PAGES[0][0])
    build_ops()
    write_rdl(ROOT / "reports" / "Seller Scorecard.rdl")
    print(f"  ✓ {len(BOOKMARKS)} bookmarks · sidebar contrast ≥ "
          f"{min(contrast(a, b) for a, b in SIDEBAR_TEXT):.1f}:1 · wrote reports/Seller Scorecard.rdl")

    for rep in (R, OPS):
        r = pbir("validate", rep, "--all", check=False)
        print(r.stdout[-2500:])
        if r.returncode != 0:
            sys.exit(f"✗ validation failed: {rep}")


if __name__ == "__main__":
    main()
