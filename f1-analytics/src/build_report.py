"""Build the 7-page Power BI report (PBIR) on top of the TMDL model, via the pbir CLI.

    uv tool install pbir-cli        # once
    uv run python src/build_report.py

Regenerates powerbi/F1.Report from scratch every run, so the report is code:
layout, theme, bindings, filters and conditional formatting all live in this file.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PBI = ROOT / "powerbi"
R = "F1.Report"

# ------------------------------------------------------------------ design tokens (dark, validated)
PLANE, SURFACE, HAIR, GRID = "#111116", "#1C1C24", "#2C2C36", "#26262E"
INK, INK2, MUTED = "#FFFFFF", "#C3C2B7", "#898781"
F1_RED = "#E10600"                                            # brand chrome only, never a data series
PALETTE = ["#3987E5", "#D95926", "#199E70", "#C98500", "#D55181", "#008300", "#9085E9", "#E66767"]
SEQ_LO, SEQ_HI = "#1C1C24", "#2F6FC8"                          # sequential blue; white text on it = 4.97:1
GOOD, BAD, NEUTRAL = "#0CA30C", "#D03B3B", "#383835"
# Race outcomes: colour-blind-safe (no red/green pair) and every segment also carries a % data label.
OUTCOME_COLORS = {"Finished": "#3987E5", "Finished (lapped)": "#86B6EF", "Mechanical failure": "#D95926",
                  "Crash / driver": "#9085E9", "Retired (other)": "#898781", "Disqualified": "#D55181"}


def contrast(a, b):
    """WCAG 2.x contrast ratio between two hex colours."""
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# Accessibility gate: text ≥ 4.5:1 on the card surface (and white on the darkest heat cell), data marks ≥ 3:1.
assert all(contrast(c, SURFACE) >= 4.5 for c in (INK, INK2, MUTED)), "text contrast"
assert contrast(INK, SEQ_HI) >= 4.5, "white on heat-map maximum"
assert all(contrast(c, SURFACE) >= 3 for c in PALETTE + [OUTCOME_COLORS["Finished"], OUTCOME_COLORS["Mechanical failure"]]), "data contrast"

# ------------------------------------------------------------------ grid (1280 × 720)
X0, GAP, RIGHT = 16, 12, 1264
KPI_Y, KPI_H = 80, 96
ROW2_Y, ROW2_H = 188, 292
ROW3_Y, ROW3_H = 492, 216


def cols(n, x0=X0, x1=RIGHT, gap=GAP):
    w = (x1 - x0 - gap * (n - 1)) // n
    return [(x0 + i * (w + gap), w) for i in range(n)]


def split(first_w, x0=X0, x1=RIGHT):
    return (x0, first_w), (x0 + first_w + GAP, x1 - x0 - first_w - GAP)


def pbir(*args, check=True):
    env = {**os.environ, "NO_COLOR": "1", "PBIR_ALLOW_OVERLAP": "1", "COLUMNS": "200"}
    r = subprocess.run(["pbir", "-q", *args], cwd=PBI, env=env, capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"✗ pbir {' '.join(args)}\n{r.stdout}\n{r.stderr}")
    return r


def pp(page):
    return f"{R}/{page}.Page"


def visual(page, vtype, name, x, y, w, h, data=(), title=None, subtitle=None):
    args = ["add", "visual", vtype, pp(page), "-n", name, "-x", str(x), "-y", str(y), "-w", str(w), "-h", str(h), "--force"]
    for d in data:
        args += ["-d", d]
    pbir(*args)
    v = f"{pp(page)}/{name}.Visual"
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
    return "'" + s.replace("'", "''") + "'"


def patch(v, prop, value):
    """Write a formatting property straight into visual.json (for props pbir's validator rejects)."""
    _, page, name = v.split("/")
    page, name = page.removesuffix(".Page"), name.removesuffix(".Visual")
    folder = next(p.parent for p in (PBI / R / "definition" / "pages").glob("*/page.json")
                  if json.loads(p.read_text())["displayName"] == page)
    f = folder / "visuals" / name / "visual.json"
    doc = json.loads(f.read_text())
    obj, key = prop.split(".")
    expr = {"expr": {"Literal": {"Value": literal(value)}}}
    if str(value).startswith("#"):
        expr = {"solid": {"color": expr}}
    container = obj in ("background", "border", "dropShadow", "title", "subTitle", "padding", "visualHeader", "visualLink", "divider",
                        "spacing", "general", "visualTooltip", "visualHeaderTooltip")
    entries = doc["visual"].setdefault("visualContainerObjects" if container else "objects", {}).setdefault(obj, [])
    base = next((e for e in entries if "selector" not in e), None)
    if base is None:
        base = {"properties": {}}
        entries.insert(0, base)
    base.setdefault("properties", {})[key] = expr
    f.write_text(json.dumps(doc, indent=2, ensure_ascii=False))


def setp(v, prop, value):
    r = pbir("set", f"{v}.{prop}", "--value", str(value), check=False)
    if r.returncode != 0:
        if "Unknown component" not in r.stdout + r.stderr:
            sys.exit(f"✗ pbir set {v}.{prop}\n{r.stdout}\n{r.stderr}")
        patch(v, prop, value)


def add_series(v, field):
    """Bind a legend column. pbir's role catalog omits lineChart's Series role, so write the projection directly."""
    _, page, name = v.split("/")
    folder = next(p.parent for p in (PBI / R / "definition" / "pages").glob("*/page.json")
                  if json.loads(p.read_text())["displayName"] == page.removesuffix(".Page"))
    f = folder / "visuals" / name.removesuffix(".Visual") / "visual.json"
    doc = json.loads(f.read_text())
    table, col = field.split(".", 1)
    state = doc["visual"]["query"]["queryState"]
    state["Series"] = {"projections": [{"queryRef": field, "nativeQueryRef": col, "field": {
        "Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": col}}}]}
    doc["visual"]["query"]["queryState"] = {k: state[k] for k in ["Category", "Series", *[k for k in state if k not in ("Category", "Series")]]}
    f.write_text(json.dumps(doc, indent=2, ensure_ascii=False))


def vjson(v):
    _, page, name = v.split("/")
    folder = next(p.parent for p in (PBI / R / "definition" / "pages").glob("*/page.json")
                  if json.loads(p.read_text())["displayName"] == page.removesuffix(".Page"))
    return folder / "visuals" / name.removesuffix(".Visual") / "visual.json"


def rename(v, labels):
    """Column headers: give projections a display name (e.g. two point totals with different definitions)."""
    f = vjson(v)
    doc = json.loads(f.read_text())
    for role in doc["visual"]["query"]["queryState"].values():
        for pr in role["projections"]:
            if pr["queryRef"] in labels:
                pr["displayName"] = labels[pr["queryRef"]]
    f.write_text(json.dumps(doc, indent=2, ensure_ascii=False))


def vedit(v, fn):
    f = vjson(v)
    doc = json.loads(f.read_text())
    fn(doc)
    f.write_text(json.dumps(doc, indent=2, ensure_ascii=False))


def objects(v, obj, **props):
    """Write one formatting object straight into visual.json (analytics-pane trend/forecast, matrix subtotals)."""
    vedit(v, lambda doc: doc["visual"].setdefault("objects", {}).update({obj: [{"properties": {k: lit(x) for k, x in props.items()}}]}))


def page_dir(page):
    return next(p.parent for p in (PBI / R / "definition" / "pages").glob("*/page.json") if json.loads(p.read_text())["displayName"] == page)


def lit(value):
    return {"expr": {"Literal": {"Value": literal(value)}}}


def vcalc(v, role, name, expr):
    """Visual calculation: DAX over the visual's own rows (RUNNINGSUM, MOVINGAVERAGE, RANK), not stored in the model."""
    f = vjson(v)
    doc = json.loads(f.read_text())
    doc["visual"]["query"]["queryState"][role]["projections"].append(
        {"field": {"NativeVisualCalculation": {"Language": "dax", "Expression": expr, "Name": name, "DataType": "Double"}},
         "queryRef": name, "nativeQueryRef": name})
    f.write_text(json.dumps(doc, indent=2, ensure_ascii=False))


def bind(v, role, field):
    """Put a column on a well in place of the placeholder (field parameters and calc-group columns; pbir's role
    catalog only accepts measures on chart value wells, Desktop takes a field parameter there)."""
    f = vjson(v)
    doc = json.loads(f.read_text())
    table, col = field.split(".", 1)
    doc["visual"]["query"]["queryState"][role] = {"projections": [{"queryRef": field, "nativeQueryRef": col, "field": {
        "Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": col}}}]}
    f.write_text(json.dumps(doc, indent=2, ensure_ascii=False))


def no_filter(page, source, *targets):
    for t in targets:
        pbir("pages", "interactions", pp(page), "--source", source, "--target", t, "--type", "NoFilter")


def interact(page, source, kind, *targets):
    for t in targets:
        pbir("pages", "interactions", pp(page), "--source", source, "--target", t, "--type", kind)


def png(path, w, h, pixel):
    """Minimal RGBA PNG writer (stdlib only): pixel(x, y) -> (r, g, b, a)."""
    raw = b"".join(b"\x00" + bytes(c for x in range(w) for c in pixel(x, y)) for y in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)  # noqa: E731
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    return path


def start_grid(x, y):
    """Page background: the staggered start-grid boxes painted on a track (two columns, 10 rows)."""
    for col, x0 in enumerate((980, 1110)):
        for row in range(10):
            y0 = 60 + row * 66 + col * 33
            on_bar = y0 <= y <= y0 + 3 and x0 <= x <= x0 + 90
            on_tick = x0 <= x <= x0 + 3 and y0 <= y <= y0 + 26
            if on_bar or on_tick:
                return (255, 255, 255, 255)
    return (0, 0, 0, 0)


def chequered(x, y):
    return (255, 255, 255, 255) if ((x // 12) + (y // 12)) % 2 == 0 else (17, 17, 22, 255)


def button(page, name, x, y, w, h, kind, target=None, tooltip=None, icon=None, text=None):
    b = visual(page, "actionButton", name, x, y, w, h)
    if icon:
        setp(b, "icon.shapeType", icon)
        setp(b, "icon.lineColor", INK2)
    if text:
        for prop, value in [("text.show", "true"), ("text.text", text), ("text.fontColor", INK2), ("text.fontSize", "10")]:
            setp(b, prop, value)
    setp(b, "fill.show", "false")
    setp(b, "outline.show", "false")
    args = ["--type", kind] + (["--target", target or "_"] if kind == "Bookmark" else []) + (["--tooltip", tooltip] if tooltip else [])
    pbir("visuals", "action", b, *args)
    bare(b)
    return b


def link_bookmark(b, bookmark_id):
    """pbir writes the bookmark's display name; Desktop resolves a bookmark action by its id. No id = help-only button."""
    def fn(doc):
        props = doc["visual"]["visualContainerObjects"]["visualLink"][0]["properties"]
        if bookmark_id:
            props["bookmark"] = lit(bookmark_id)
        else:
            props.pop("bookmark", None)
    vedit(b, fn)


def bookmark(name, page, shown, hidden):
    """A bookmark that only toggles visibility (no data state) of the listed visuals on one page."""
    pbir("add", "bookmark", R, name)
    folder = PBI / R / "definition" / "bookmarks"
    f = next(f for f in folder.glob("*.bookmark.json") if json.loads(f.read_text())["displayName"] == name)
    doc = json.loads(f.read_text())
    pid = page_dir(page).name
    vtype = lambda v: json.loads(vjson(f"{pp(page)}/{v}.Visual").read_text())["visual"]["visualType"]  # noqa: E731
    doc["options"] = {"targetVisualNames": shown + hidden, "suppressData": True, "applyOnlyToTargetVisuals": True}
    doc["explorationState"] = {"version": "1.3", "activeSection": pid, "sections": {pid: {"visualContainers": {
        v: {"singleVisual": {"visualType": vtype(v), "objects": {}, **({"display": {"mode": "hidden"}} if v in hidden else {})}}
        for v in shown + hidden}}}}
    f.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
    return doc["name"]


def group(page, name, display, members):
    """Selection-pane group: a container visual, members point at it through parentGroupName."""
    pos = [json.loads(vjson(f"{pp(page)}/{m}.Visual").read_text())["position"] for m in members]
    x, y = min(p["x"] for p in pos), min(p["y"] for p in pos)
    w, h = max(p["x"] + p["width"] for p in pos) - x, max(p["y"] + p["height"] for p in pos) - y
    d = page_dir(page) / "visuals" / name
    d.mkdir()
    (d / "visual.json").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.9.0/schema.json",
        "name": name, "position": {"x": x, "y": y, "z": 0, "width": w, "height": h, "tabOrder": 0},
        "visualGroup": {"displayName": display, "groupMode": "ScaleMode"}}, indent=2))
    for m in members:
        vedit(f"{pp(page)}/{m}.Visual", lambda doc: doc.update(parentGroupName=name))


def hide(v):
    vedit(v, lambda doc: doc.update(isHidden=True))


def tooltip_page(v, page):
    """Report-page tooltip: hovering the visual shows the given tooltip page, filtered to the hovered point."""
    vedit(v, lambda doc: doc["visual"].setdefault("visualContainerObjects", {}).update(visualTooltip=[
        {"properties": {"show": lit("true"), "type": lit("Canvas"), "section": lit(page_dir(page).name)}}]))


def mobile(page, order):
    """Phone layout (mobile.json beside visual.json): a single 320-px column, stacked in the order given."""
    y = 0
    for i, (name, h) in enumerate(order):
        f = vjson(f"{pp(page)}/{name}.Visual").parent / "mobile.json"
        f.write_text(json.dumps({
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainerMobileState/2.4.0/schema.json",
            "position": {"x": 0, "y": y, "z": 0, "width": 320, "height": h, "tabOrder": i}},
            indent=2))
        y += h + 8


def outcome_colors(v):
    def fn(doc):
        doc["visual"].setdefault("objects", {})["dataPoint"] = [{"properties": {"fill": {"solid": {"color": lit(c)}}}, "selector": {"data": [
            {"scopeId": {"Comparison": {"ComparisonKind": 0, "Left": {"Column": {"Expression": {"SourceRef": {"Entity": "Results"}},
                                                                                 "Property": "Status Group"}},
                                        "Right": {"Literal": {"Value": literal(g)}}}}}]}} for g, c in OUTCOME_COLORS.items()]
    vedit(v, fn)


def deneb(v, spec):
    """Deneb custom visual (AppSource, Vega-Lite): pbir has no Deneb type, so add a table with the same fields and
    retype it; the fields move to Deneb's single 'dataset' well and the spec goes in objects.vega."""
    def fn(doc):
        doc["visual"]["visualType"] = DENEB
        state = doc["visual"]["query"]["queryState"]
        state["dataset"] = state.pop("Values")
    vedit(v, fn)
    spec_file = Path(tempfile.mkdtemp()) / "spec.json"
    spec_file.write_text(json.dumps(spec))
    pbir("visuals", "deneb", v, "--spec-file", str(spec_file))
    f = PBI / R / "definition" / "report.json"
    doc = json.loads(f.read_text())
    doc["publicCustomVisuals"] = sorted(set(doc.get("publicCustomVisuals", [])) | {DENEB})
    f.write_text(json.dumps(doc, indent=2, ensure_ascii=False))


DENEB = "deneb7E15AEF80B9E4D4F8E12924291ECE89A"


def bare(v):
    for prop in ("background.show", "border.show", "dropShadow.show"):
        setp(v, prop, "false")


def topn(v, table, field, n, by):
    pbir("add", "filter", table, field, "-v", v, "--type", "TopN", "--n", str(n), "--by-table", "_Measures", "--by-field", by)


def sort(v, field, direction="Descending"):
    pbir("visuals", "sort", v, "--field", field, "--direction", direction)


# ================================================================== chrome
def chrome(page, title, subtitle, help_text):
    pbir("pages", "background", pp(page), "--color", PLANE, "--transparency", "0")

    stripe = visual(page, "shape", "brand_stripe", X0, 16, 6, 48)
    setp(stripe, "fill.fillColor", F1_RED)
    setp(stripe, "outline.show", "false")
    bare(stripe)

    head = visual(page, "textbox", "header", X0 + 14, 8, 560, 64, title=title, subtitle=subtitle)
    pbir("visuals", "title", head, "--fontColor", INK, "--fontSize", "20", "--bold")
    pbir("visuals", "subtitle", head, "--fontColor", INK2, "--fontSize", "10")
    bare(head)
    # built-in help: an (i) button whose hover tooltip explains the page (the button has no bookmark to jump to)
    info = button(page, "btn_info", 600, 24, 28, 28, "Bookmark", tooltip=help_text, icon="information")
    link_bookmark(info, None)

    nav = visual(page, "pageNavigator", "page_nav", 640, 20, RIGHT - 640, 40)  # horizontal by default
    for prop, value in [("fill.fillColor", SURFACE), ("text.fontColor", INK2), ("text.fontSize", "10"), ("outline.show", "false"),
                        ("fill.selected.fillColor", F1_RED), ("text.selected.fontColor", INK), ("text.selected.bold", "true")]:
        setp(nav, prop, value)
    bare(nav)


def kpis(page, cards, x0=X0, x1=RIGHT):
    for (x, w), (name, measure) in zip(cols(len(cards), x0, x1), cards):
        visual(page, "cardVisual", name, x, KPI_Y, w, KPI_H, data=[f"Data:_Measures.{measure}"])


def slicer(page, name, field, label, x, w, y=KPI_Y, h=KPI_H):
    s = visual(page, "slicer", name, x, y, w, h, data=[f"Values:{field}"], title=label)
    setp(s, "data.mode", "Dropdown")
    setp(s, "header.show", "false")
    pbir("visuals", "title", s, "--fontSize", "9", "--fontColor", MUTED)
    return s


def theme():
    pbir("theme", "set-colors", R, "--data-colors", json.dumps(PALETTE),
         "--good", GOOD, "--bad", BAD, "--neutral", NEUTRAL, "--minimum", SEQ_LO, "--maximum", SEQ_HI,
         "--background", SURFACE, "--background-light", HAIR, "--background-neutral", HAIR,
         "--foreground", INK, "--foreground-neutral-secondary", INK2, "--foreground-neutral-tertiary", MUTED,
         "--table-accent", PALETTE[0], "--accent", F1_RED, "--shape-stroke", HAIR)
    for level, size, face, color in [("title", 12, "Segoe UI Semibold", INK), ("label", 10, "Segoe UI", INK2),
                                     ("callout", 24, "Segoe UI Semibold", INK), ("header", 11, "Segoe UI Semibold", INK)]:
        pbir("theme", "set-fonts", R, level, "--font-size", str(size), "--font-face", face, "--color", color)
    for prop, value in [
        ("*.*.background.show", "true"), ("*.*.background.color", SURFACE), ("*.*.background.transparency", "0"),
        ("*.*.border.show", "true"), ("*.*.border.color", HAIR), ("*.*.border.radius", "8"),
        ("*.*.dropShadow.show", "false"),
        ("*.*.title.fontColor", INK), ("*.*.title.fontSize", "12"),
        ("*.*.subTitle.fontColor", MUTED), ("*.*.subTitle.fontSize", "9"),
        ("*.*.padding.top", "10"), ("*.*.padding.left", "12"), ("*.*.padding.right", "12"), ("*.*.padding.bottom", "8"),
        ("*.*.valueAxis.gridlineColor", GRID), ("*.*.categoryAxis.labelColor", MUTED), ("*.*.valueAxis.labelColor", MUTED),
        ("*.*.legend.labelColor", INK2), ("*.*.labels.color", INK2),
        ("*.*.lineStyles.strokeWidth", "2"),
    ]:
        pbir("theme", "set-formatting", R, prop, "--value", value)


# ================================================================== pages
def page_grid(p):
    kpis(p, [("kpi_seasons", "Seasons"), ("kpi_races", "Races"), ("kpi_drivers", "Drivers"),
             ("kpi_teams", "Constructors"), ("kpi_circuits", "Circuits Used")])
    (a, aw), (b, bw) = split(740)
    m = visual(p, "azureMap", "map_circuits", a, ROW2_Y, aw, ROW2_H,
               ["Category:Circuit.Circuit Name", "X:_Measures.Map Longitude", "Y:_Measures.Map Latitude", "Size:_Measures.Circuit Races Hosted"],
               "Where F1 has raced", "77 circuits · bubble = Grands Prix hosted · Monza and Monaco lead with 74 and 70")
    v = visual(p, "columnChart", "col_races", b, ROW2_Y, bw, ROW2_H,
               ["Category:Season.Year", "Y:_Measures.Races"], "Grands Prix per season", "7 races in 1950 → 24 in 2024")
    (a, aw), (b, bw) = split(640)
    t = visual(p, "tableEx", "table_champions", a, ROW3_Y, aw, ROW3_H,
               ["Values:Season.Year", "Values:Season.Champion", "Values:Season.Constructor Champion",
                "Values:Season.Champion Wins", "Values:Season.Title Margin"],
               "Roll of champions", "Newest first · constructors' title awarded from 1958")
    sort(t, "Season.Year")
    pbir("visuals", "cf", t, "--data-bars", "--field", "Season.Champion Wins", "--positive-color", PALETTE[0])
    v = visual(p, "lineChart", "line_dominance", b, ROW3_Y, bw, ROW3_H,
               ["Category:Season.Year", "Y:_Measures.Avg Top Team Share"],
               "How dominant was the best team?", "Best constructor's share of the season's points (modern scale) · 3-season average = visual calculation")
    vcalc(v, "Y", "3-season average", "MOVINGAVERAGE ( [Avg Top Team Share], 3 )")
    tooltip_page(m, "Circuit Card")
    tooltip_page(v, "Season Card")
    pbir("pages", "background", pp(p), "--image", str(png(Path(tempfile.mkdtemp()) / "start-grid.png", 1280, 720, start_grid)),
         "--transparency", "85")
    mobile(p, [("header", 64), ("kpi_seasons", 72), ("kpi_races", 72), ("kpi_drivers", 72), ("kpi_teams", 72), ("kpi_circuits", 72),
               ("map_circuits", 240), ("table_champions", 280), ("col_races", 200), ("line_dominance", 200)])
    return m


def page_dynasties(p):
    s = slicer(p, "slicer_era", "Season.Era", "Era", RIGHT - 150, 150)
    m = slicer(p, "slicer_metric", "Dynasty Metric.Dynasty Metric", "Ribbon metric", RIGHT - 150 - GAP - 190, 190)
    pbir("add", "filter", "Dynasty Metric", "Dynasty Metric", "-v", m, "--values", "Points share")
    setp(m, "selection.singleSelect", "true")
    kpis(p, [("kpi_wins", "Wins"), ("kpi_teams", "Constructors"), ("kpi_share", "Avg Top Team Share"),
             ("kpi_winners", "Different Winners Count")], x1=RIGHT - 150 - GAP - 190 - GAP)
    v = visual(p, "ribbonChart", "ribbon_dynasties", X0, ROW2_Y, RIGHT - X0, ROW2_H,
               ["Category:Season.Decade", "Series:Constructor.Constructor Name", "Y:_Measures.Team Points Share"],
               "Dynasties by decade", "Metric picked above (default: share of all points) · top 8 by points · drill up to era, down to season · "
               "decade leaders by share: Ferrari → McLaren → Williams → Ferrari → Mercedes → Red Bull")
    bind(v, "Y", "Dynasty Metric.Dynasty Metric")
    drill(v, "Season", ["Era", "Decade", "Year"], active="Decade")
    topn(v, "Constructor", "Constructor Name", 8, "Points (Modern Scale)")
    (a, aw), (b, bw), (c, cw) = cols(3)
    v = visual(p, "barChart", "bar_team_wins", a, ROW3_Y, aw, ROW3_H,
               ["Category:Constructor.Constructor Name", "Y:_Measures.Wins"], "Most race wins", "Top 10 constructors")
    topn(v, "Constructor", "Constructor Name", 10, "Wins")
    sort(v, "_Measures.Wins")
    m = visual(p, "pivotTable", "matrix_decades", b, ROW3_Y, bw, ROW3_H,
               ["Rows:Constructor.Constructor Name", "Columns:Season.Decade", "Values:_Measures.Wins"],
               "Wins by decade", "Heat = wins · who owned which decade")
    topn(m, "Constructor", "Constructor Name", 8, "Wins")
    pbir("visuals", "cf", m, "--gradient", "--field", "_Measures.Wins", "--min-color", SEQ_LO, "--max-color", SEQ_HI, "--on", "values.backColor")
    t = visual(p, "tableEx", "table_titles", c, ROW3_Y, cw, ROW3_H,
               ["Values:Constructor.Constructor Name", "Values:Constructor.Constructor Titles", "Values:Constructor.Team Driver Titles",
                "Values:Constructor.Team Win Rate"],
               "Championship haul", "Constructors' and drivers' titles, all time")
    sort(t, "Constructor.Constructor Titles")
    pbir("visuals", "cf", t, "--data-bars", "--field", "Constructor.Constructor Titles", "--positive-color", PALETTE[0])
    return s


def page_legends(p):
    s = slicer(p, "slicer_tier", "Driver.Achievement Tier", "Achievement", RIGHT - 150, 150)
    by = slicer(p, "slicer_legends_by", "Legends By.Legends By", "Treemap by", RIGHT - 150 - GAP - 190, 190)
    pbir("add", "filter", "Legends By", "Legends By", "-v", by, "--values", "Nationality")
    setp(by, "selection.singleSelect", "true")
    kpis(p, [("kpi_drivers", "Drivers"), ("kpi_winners", "Different Winners Count"), ("kpi_titles", "Driver Titles Won"),
             ("kpi_debut", "Avg Debut Age")], x1=RIGHT - 150 - GAP - 190 - GAP)
    (a, aw), (b, bw) = split(730)
    t = visual(p, "tableEx", "table_leaderboard", a, ROW2_Y, aw, ROW2_H,
               ["Values:Driver.Driver Name", "Values:Driver.Driver Country", "Values:_Measures.Driver Titles Won",
                "Values:_Measures.Wins", "Values:_Measures.Poles", "Values:_Measures.Podiums",
                "Values:_Measures.Win Rate", "Values:_Measures.Car-Adjusted Skill"],
               "Hall of fame", "Sorted by wins · car-adjusted skill: + beats an average driver in the same car")
    sort(t, "_Measures.Wins")
    pbir("visuals", "cf", t, "--data-bars", "--field", "_Measures.Wins", "--positive-color", PALETTE[0])
    pbir("visuals", "cf", t, "--measure", "values.fontColor _Measures.Skill Color", "--field", "_Measures.Car-Adjusted Skill")
    v = visual(p, "scatterChart", "scatter_careers", b, ROW2_Y, bw, ROW2_H,
               ["Category:Driver.Driver Name", "X:_Measures.Starts", "Y:_Measures.Win Rate", "Size:_Measures.Driver Titles Won"],
               "Longevity vs strike rate", "Top 60 winners · colour = win-rate band · bubble = titles · dotted = trend · Fangio wins 1 in 2.4 starts")
    topn(v, "Driver", "Driver Name", 60, "Wins")
    add_series(v, "Driver.Win Band")  # grouping: calc column banding career win rate (sort by Win Band Order)
    objects(v, "trend", show="true", combineSeries="true", style="dotted", displayName="Trend")
    (a, aw), (b, bw) = split(620)
    v = visual(p, "barChart", "bar_skill", a, ROW3_Y, aw, ROW3_H,
               ["Category:Driver.Driver Name", "Y:_Measures.Car-Adjusted Skill"],
               "Best drivers once the car is removed", "Regression: finish percentile ~ driver + team-season · 40+ race careers")
    topn(v, "Driver", "Driver Name", 15, "Car-Adjusted Skill")
    pbir("add", "filter", "Driver Skill", "Is Ranked", "-v", v, "--values", "true")
    sort(v, "_Measures.Car-Adjusted Skill")
    pbir("visuals", "cf", v, "--measure", "dataPoint.fill _Measures.Skill Color")
    patch(v, "visualHeader.showTooltipButton", "true")
    patch(v, "visualHeaderTooltip.text", SKILL_HELP)
    v = visual(p, "treemap", "tree_countries", b, ROW3_Y, bw, ROW3_H,
               ["Group:Driver.Driver Country", "Values:_Measures.Wins"], "Wins, grouped by the field picked above",
               "Default nationality: Britain leads; Germany, Brazil, France follow")
    bind(v, "Group", "Legends By.Legends By")
    # edit interactions: the achievement slicer leaves the all-time KPI totals alone; the scatter only highlights the skill bars
    interact(p, "slicer_tier", "NoFilter", "kpi_drivers", "kpi_winners", "kpi_titles", "kpi_debut")
    interact(p, "scatter_careers", "HighlightFilter", "bar_skill")
    return s


def page_racecraft(p):
    s = slicer(p, "slicer_era", "Grid Conversion.Grid Era", "Era (grid chart)", RIGHT - 300, 300, h=56)
    button(p, "btn_reset", RIGHT - 300, KPI_Y + 64, 300, 32, "ClearAllSlicers", tooltip="Clear every slicer on this page", text="↺  Reset filters")
    kpis(p, [("kpi_pole", "Pole Conversion"), ("kpi_finish", "Finish Rate"), ("kpi_mech", "Mechanical DNF Rate"),
             ("kpi_crash", "Crash DNF Rate")], x1=RIGHT - 300 - GAP)
    (a, aw), (b, bw) = split(620)
    v = visual(p, "columnChart", "col_grid", a, ROW2_Y, aw, ROW2_H,
               ["Category:Grid Conversion.Starting Slot", "Y:_Measures.Grid Win Probability"],
               "Does qualifying decide the race?", "Chance of winning by starting slot · pole won 29% of turbo-era races, 52% of hybrid-era races")
    pbir("visuals", "labels", v, "--show", "--labelPrecision", "0")
    v = visual(p, "lineChart", "line_reliability", b, ROW2_Y, bw, ROW2_H,
               ["Category:Calendar.Year", "Y:_Measures.Mechanical DNF Rate", "Y:_Measures.Crash DNF Rate", "Y:_Measures.DNF Rate 5Y Avg"],
               "The reliability revolution", "Share of starts ending in a breakdown vs a crash · all retirements, 5-season average (DATESINPERIOD) · dotted = trend")
    rename(v, {"_Measures.DNF Rate 5Y Avg": "All DNFs, 5-season avg"})
    objects(v, "trend", show="true", combineSeries="false", style="dotted", displayName="Trend")
    (a, aw), (b, bw) = split(620)
    v = visual(p, "hundredPercentStackedColumnChart", "stack_outcomes", a, ROW3_Y, aw, ROW3_H,
               ["Category:Season.Decade", "Series:Results.Status Group", "Y:_Measures.Outcome Share"],
               "How races ended, by decade", "Share of starts · 44–52% finished in each decade up to the 1990s, 86% in the 2020s")
    outcome_colors(v)
    pbir("visuals", "labels", v, "--show", "--labelPrecision", "0")
    t = visual(p, "tableEx", "table_circuits", b, ROW3_Y, bw, ROW3_H,
               ["Values:Circuit.Circuit Name", "Values:Circuit.Circuit Country", "Values:Circuit.Races Hosted",
                "Values:_Measures.Circuit Pole Win Rate", "Values:Circuit.Lap Record", "Values:Circuit.Lap Record Holder"],
               "Circuit almanac", "Pole win rate · fastest race lap on record (2004+ timing)")
    sort(t, "Circuit.Races Hosted")
    pbir("visuals", "cf", t, "--gradient", "--field", "_Measures.Circuit Pole Win Rate", "--min-color", SEQ_LO, "--max-color", SEQ_HI, "--on", "values.backColor")
    # the era slicer is for the grid chart only: no filtering of the KPIs and the all-time visuals
    no_filter(p, "slicer_era", "kpi_pole", "kpi_finish", "kpi_mech", "kpi_crash", "line_reliability", "stack_outcomes", "table_circuits")
    return s


def page_titlefight(p):
    s = slicer(p, "slicer_season", "Season.Year", "Season", RIGHT - 160, 160)
    pbir("add", "filter", "Season", "Year", "-v", s, "--values", "2021")
    ps = slicer(p, "slicer_points_system", "Points Systems.System", "Re-score with points system", RIGHT - 160 - GAP - 200, 200)
    pbir("add", "filter", "Points Systems", "System", "-v", ps, "--values", "2010–")
    wp = slicer(p, "slicer_win_points", "Win Points.Win Points", "What-if: points for a win", RIGHT - 160 - GAP - 200 - GAP - 170, 170)
    pbir("add", "filter", "Win Points", "Win Points", "-v", wp, "--values", "25")
    setp(wp, "selection.singleSelect", "true")
    kpis(p, [("kpi_champion", "Champions (driver · team)"), ("kpi_rescored", "Re-scored Champion"), ("kpi_whatif", "What-if Champion")],
         x1=RIGHT - 160 - GAP - 200 - GAP - 170 - GAP)
    for name in ("kpi_champion", "kpi_rescored", "kpi_whatif"):  # text callouts: smaller font so names fit
        setp(f"{pp(p)}/{name}.Visual", "value.fontSize", "14")
    v = visual(p, "lineChart", "line_titlefight", X0, ROW2_Y, RIGHT - X0, ROW2_H,
               ["Category:Race.Round", "Y:_Measures.Championship Points"],
               "The title fight, round by round", "Official cumulative points of the season's top 5 · 2021: Verstappen and Hamilton level before the finale")
    add_series(v, "Driver.Driver Name")
    topn(v, "Driver", "Driver Name", 5, "Final Standings Points")
    # same chart on today's 25-18-15 scale; a bookmark pair swaps which one is visible
    m = visual(p, "lineChart", "line_titlefight_modern", X0, ROW2_Y, RIGHT - X0, ROW2_H,
               ["Category:Race.Round", "Y:_Measures.Running Points (Window)"],
               "The title fight, round by round", "Modern-scale cumulative points (25 for a win) of the season's top 5 · WINDOW running sum")
    add_series(m, "Driver.Driver Name")
    topn(m, "Driver", "Driver Name", 5, "Final Standings Points")
    hide(m)
    actual = button(p, "btn_actual", RIGHT - 236, ROW2_Y + 8, 108, 26, "Bookmark", tooltip="Official points of the season", text="Actual points")
    modern = button(p, "btn_modern", RIGHT - 120, ROW2_Y + 8, 108, 26, "Bookmark", tooltip="Today's 25-18-15 scale", text="Modern points")
    link_bookmark(actual, bookmark("Actual points", p, ["line_titlefight"], ["line_titlefight_modern"]))
    link_bookmark(modern, bookmark("Modern points", p, ["line_titlefight_modern"], ["line_titlefight"]))
    group(p, "points_toggle", "Points toggle", ["btn_actual", "btn_modern"])
    (a, aw), (b, bw) = split(620)
    t = visual(p, "tableEx", "table_standings", a, ROW3_Y, aw, ROW3_H,
               ["Values:Driver.Driver Name", "Values:_Measures.Points", "Values:_Measures.Sprint Points Scored",
                "Values:_Measures.Points incl. Sprints", "Values:_Measures.Wins",
                "Values:_Measures.Points (Chosen System)", "Values:_Measures.What-if Points"],
               "Final standings", "Official = GP + sprint · Re-scored = every finish under the chosen system · What-if = modern scale, win worth the slider value")
    rename(t, {"_Measures.Points": "GP Points", "_Measures.Sprint Points Scored": "Sprint Points",
               "_Measures.Points incl. Sprints": "Total Points", "_Measures.Points (Chosen System)": "Re-scored",
               "_Measures.What-if Points": "What-if"})
    sort(t, "_Measures.Points incl. Sprints")
    pbir("visuals", "cf", t, "--data-bars", "--field", "_Measures.Points incl. Sprints", "--positive-color", PALETTE[0])
    v = visual(p, "tableEx", "bump_positions", b, ROW3_Y, bw, ROW3_H,
               ["Values:Race.Round", "Values:Driver.Driver Name", "Values:_Measures.Championship Position"],
               "Championship position, round by round", "Deneb (Vega-Lite) bump chart · top 5 at season end · P1 at the top")
    topn(v, "Driver", "Driver Name", 5, "Final Standings Points")
    deneb(v, {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json", "data": {"name": "dataset"},
        "config": {"background": "transparent", "view": {"stroke": None},
                   "axis": {"labelColor": MUTED, "titleColor": MUTED, "gridColor": GRID, "domainColor": HAIR, "tickColor": HAIR},
                   "legend": {"labelColor": INK2, "titleColor": MUTED, "orient": "bottom"}},
        "encoding": {"x": {"field": "Round", "type": "ordinal", "axis": {"labelAngle": 0}},
                     "y": {"field": "Championship Position", "type": "quantitative", "scale": {"reverse": True, "zero": False},
                           "axis": {"tickMinStep": 1, "title": "Position"}},
                     "color": {"field": "Driver Name", "type": "nominal", "scale": {"range": PALETTE[:5]}, "title": None}},
        "layer": [{"mark": {"type": "line", "strokeWidth": 2}}, {"mark": {"type": "point", "filled": True, "size": 30}}]})
    mobile(p, [("header", 64), ("slicer_season", 56), ("slicer_points_system", 56), ("slicer_win_points", 56), ("kpi_champion", 72),
               ("kpi_rescored", 72), ("kpi_whatif", 72), ("line_titlefight", 240), ("bump_positions", 240), ("table_standings", 280)])
    return s


def page_pitwall(p):
    s = slicer(p, "slicer_race", "Race.Race Label", "Race (1996+ lap data)", RIGHT - 300, 300)
    pbir("add", "filter", "Race", "Has Lap Data", "-p", pp(p), "--values", "true")  # page filter: lap data exists 1996+
    pbir("add", "filter", "Race", "Race Label", "-v", s, "--values", "2021 · R22 · Abu Dhabi GP")
    kpis(p, [("kpi_winner", "Race Winner Label"), ("kpi_stops", "Pit Stops Made"), ("kpi_pitlane", "Median Pit Lane Time"),
             ("kpi_bestlap", "Best Lap Time")], x1=RIGHT - 300 - GAP)
    v = visual(p, "lineChart", "line_racestory", X0, ROW2_Y, RIGHT - X0, ROW2_H,
               ["Category:Lap Times.Lap", "Y:_Measures.Running Position"],
               "Race story, lap by lap", "Running position of the top 8 finishers · P1 at the top · dips are pit stops")
    add_series(v, "Driver.Driver Code")
    topn(v, "Driver", "Driver Code", 8, "Avg Finish Percentile")
    setp(v, "valueAxis.invertAxis", "true")
    (a, aw), (b, bw), (c, cw) = cols(3)
    v = visual(p, "barChart", "bar_laps_led", a, ROW3_Y, aw, ROW3_H,
               ["Category:Driver.Driver Name", "Y:_Measures.Laps Led"], "Laps led", "Who controlled the race")
    sort(v, "_Measures.Laps Led")
    v = visual(p, "barChart", "bar_pit_team", b, ROW3_Y, bw, ROW3_H,
               ["Category:Constructor.Constructor Name", "Y:_Measures.Median Pit Lane Time"],
               "Pit lane time by team", "Median entry-to-exit, routine stops · shorter is better")
    sort(v, "_Measures.Median Pit Lane Time", "Ascending")
    t = visual(p, "tableEx", "table_race_pace", c, ROW3_Y, cw, ROW3_H,
               ["Values:Driver.Driver Code", "Values:_Measures.Avg Median Race Pace", "Values:Race Pace.Laps Timed"],
               "Race pace", "Median racing lap (in/out laps dropped, Power Query Group By) · rank = visual calculation")
    vcalc(t, "Values", "Pace Rank", "RANK ( DENSE, ORDERBY ( [Avg Median Race Pace], ASC ) )")
    rename(t, {"_Measures.Avg Median Race Pace": "Median Lap", "Race Pace.Laps Timed": "Laps Timed"})
    sort(t, "_Measures.Avg Median Race Pace", "Ascending")
    return s


def page_seasonlens(p):
    d = slicer(p, "slicer_driver", "Driver.Driver Name", "Driver", RIGHT - 200, 200)
    pbir("add", "filter", "Driver", "Driver Name", "-v", d, "--values", "Lewis Hamilton")
    s = slicer(p, "slicer_season", "Season.Year", "Season (bottom row)", RIGHT - 200 - GAP - 160, 160)
    pbir("add", "filter", "Season", "Year", "-v", s, "--values", "2021")
    kpis(p, [("kpi_best", "Best Season Points"), ("kpi_per_race", "Avg Points per Race"), ("kpi_seasons_won", "Seasons With a Win"),
             ("kpi_home", "Home Wins")], x1=RIGHT - 200 - GAP - 160 - GAP)
    (a, aw), (b, bw) = split(760)
    t = visual(p, "tableEx", "table_seasons", a, ROW2_Y, aw, ROW2_H,
               ["Values:Season.Year", "Values:_Measures.Points (Modern Scale)", "Values:_Measures.Points PY", "Values:_Measures.Points YoY %",
                "Values:_Measures.Wins", "Values:_Measures.Share of Season Wins"],
               "Career, season by season", "Modern-scale points · PY = SAMEPERIODLASTYEAR · share = of all wins that season (ALLEXCEPT) · running wins = visual calc")
    vcalc(t, "Values", "Running Wins", "RUNNINGSUM ( [Wins] )")
    rename(t, {"_Measures.Points (Modern Scale)": "Points", "_Measures.Points YoY %": "YoY", "_Measures.Share of Season Wins": "Share of Wins"})
    sort(t, "Season.Year", "Ascending")
    m = visual(p, "pivotTable", "matrix_period", b, ROW2_Y, bw, ROW2_H,
               ["Rows:Season.Year", "Columns:Period.Period", "Values:_Measures.Win Rate"],
               "Win rate through the Period calculation group", "One measure, four lenses: current, previous season, YoY change, career to date")
    pbir("add", "filter", "Period", "Period", "-v", m, *[a for v in ("Current", "Prev Season", "YoY %", "Career-to-date") for a in ("--values", v)])
    (a, aw), (b, bw) = split(620)
    r = visual(p, "tableEx", "table_rounds", a, ROW3_Y, aw, ROW3_H,
               ["Values:Race.Round", "Values:_Measures.Points (Modern Scale)", "Values:_Measures.Running Points (Window)",
                "Values:_Measures.Points 3-Race Avg", "Values:_Measures.Championship Points", "Values:_Measures.Points Prev Round"],
               "Season picked above, round by round", "Running and 3-race average: WINDOW, modern scale · standings: official points, previous round via OFFSET")
    rename(r, {"_Measures.Points (Modern Scale)": "Points", "_Measures.Running Points (Window)": "Running",
               "_Measures.Points 3-Race Avg": "3-Race Avg", "_Measures.Championship Points": "Standings (official)",
               "_Measures.Points Prev Round": "Prev Round (official)"})
    sort(r, "Race.Round", "Ascending")
    k = visual(p, "tableEx", "table_ranking", b, ROW3_Y, bw, ROW3_H,
               ["Values:Driver.Driver Name", "Values:_Measures.Wins", "Values:_Measures.Season Rank by Wins", "Values:_Measures.% of Visible Wins",
                "Values:_Measures.Front Row Wins", "Values:_Measures.Wins From Outside Top 10"],
               "Who won the season picked above", "All drivers (ignores the driver slicer) · rank: RANK DENSE · front row: KEEPFILTERS")
    rename(k, {"_Measures.Season Rank by Wins": "Rank", "_Measures.% of Visible Wins": "Share", "_Measures.Front Row Wins": "From Front Row",
               "_Measures.Wins From Outside Top 10": "From P11+"})
    sort(k, "_Measures.Wins")
    no_filter(p, "slicer_season", "kpi_best", "kpi_per_race", "kpi_seasons_won", "kpi_home", "table_seasons", "matrix_period")
    no_filter(p, "slicer_driver", "table_ranking")
    return t


def xs(*widths, x0=X0):
    """Left edges for panels of the given widths, GAP apart."""
    out = []
    for w in widths:
        out.append((x0, w))
        x0 += w + GAP
    return out


def no_totals(v):
    objects(v, "subTotals", rowSubtotals="false", columnSubtotals="false")


# subtitle numbers for the default view (all eras / Red Bull 2024), same rules as the DAX, checked against the twin
PARETO = "36 of 861 drivers (4.2%) take 80% of 1,128 wins"
ABC = "11 A, 22 B, 101 C"
STAY = "Top 3 kept 69%"
WATERFALL = "Red Bull 2024: -246"
PIT_SLOPE = "+0.065"


def page_market(p):
    s = slicer(p, "slicer_era", "Season.Era", "Era", RIGHT - 150, 150)
    kpis(p, [("kpi_period", "Period Counted"), ("kpi_rookies", "Rookies"), ("kpi_returning", "Returning Drivers"),
             ("kpi_churn", "Churn Rate"), ("kpi_pareto", "Drivers for 80% of Wins")], x1=RIGHT - 150 - GAP)
    (a, aw), (b, bw), (c, cw) = xs(380, 460, 384)
    visual(p, "columnChart", "col_rookies", a, ROW2_Y, aw, ROW2_H,
           ["Category:Season.Year", "Y:_Measures.Rookies", "Y:_Measures.Returning Drivers"],
           "Rookies vs returning drivers", "Drivers per season · rookie = first season that year · cards above: the period counted")
    v = visual(p, "lineClusteredColumnComboChart", "combo_pareto", b, ROW2_Y, bw, ROW2_H,
               ["Category:Driver.Driver Name", "Y:_Measures.Wins", "Y2:_Measures.Cumulative Win Share"],
               "Pareto: who holds the wins",
               f"Top 40 winners · bars = wins · line = cumulative share · all eras: {PARETO}")
    topn(v, "Driver", "Driver Name", 40, "Wins")
    sort(v, "_Measures.Wins")
    t = visual(p, "tableEx", "table_abc", c, ROW2_Y, cw, ROW2_H,
               ["Values:Constructor.Constructor Name", "Values:_Measures.Points (Modern Scale)",
                "Values:_Measures.Cumulative Points Share", "Values:_Measures.ABC Class"],
               "ABC classes of constructors", f"Modern-scale points · A = first 70%, B = next 20%, C = last 10% · all eras: {ABC}")
    rename(t, {"_Measures.Points (Modern Scale)": "Points (Modern)", "_Measures.Cumulative Points Share": "Cumulative",
               "_Measures.ABC Class": "Class"})
    sort(t, "_Measures.Points (Modern Scale)")
    (a, aw), (b, bw), (c, cw), (d, dw) = xs(428, 250, 250, 284)
    m = visual(p, "pivotTable", "matrix_cohort", a, ROW3_Y, aw, ROW3_H,
               ["Rows:Driver.Debut Decade", "Columns:Seasons Since Debut.Seasons Since Debut", "Values:_Measures.% Still on Grid"],
               "Cohort retention: % still on the grid", "Row = debut decade · column = seasons after debut · ignores the era slicer")
    no_totals(m)
    pbir("visuals", "cf", m, "--gradient", "--field", "_Measures.% Still on Grid", "--min-color", SEQ_LO, "--max-color", SEQ_HI, "--on", "values.backColor")
    visual(p, "lineChart", "line_churn", b, ROW3_Y, bw, ROW3_H, ["Category:Season.Year", "Y:_Measures.Churn Rate"],
           "Churn per season", "Share of the season's drivers who never raced again · 2024 left out (leavers not known yet)")
    v = visual(p, "barChart", "bar_bands", c, ROW3_Y, cw, ROW3_H, ["Category:Win Bands.Win Band", "Y:_Measures.Drivers in Band"],
               "Drivers by win-rate band", "Disconnected 'Win Bands' table · follows the era slicer")
    sort(v, "Win Bands.Win Band", "Ascending")
    m = visual(p, "pivotTable", "matrix_transition", d, ROW3_Y, dw, ROW3_H,
               ["Rows:Team Season.Rank Band", "Columns:Team Season.Next Band", "Values:_Measures.Transition %"],
               "Where teams land next season", f"Constructors' rank band in season N (row) → N+1 (column) · % of the row · {STAY}")
    no_totals(m)
    pbir("visuals", "cf", m, "--gradient", "--field", "_Measures.Transition %", "--min-color", SEQ_LO, "--max-color", SEQ_HI, "--on", "values.backColor")
    no_filter(p, "slicer_era", "matrix_cohort")
    mobile(p, [("header", 64), ("slicer_era", 56), ("kpi_period", 72), ("kpi_rookies", 72), ("kpi_returning", 72), ("kpi_churn", 72),
               ("kpi_pareto", 72), ("combo_pareto", 240), ("col_rookies", 220), ("table_abc", 260), ("matrix_cohort", 220),
               ("line_churn", 200), ("bar_bands", 200), ("matrix_transition", 200)])
    return s


def page_why(p):
    t = slicer(p, "slicer_team", "Constructor.Constructor Name", "Constructor", RIGHT - 200, 200)
    pbir("add", "filter", "Constructor", "Constructor Name", "-v", t, "--values", "Red Bull")
    s = slicer(p, "slicer_season", "Season.Year", "Season", RIGHT - 200 - GAP - 150, 150)
    pbir("add", "filter", "Season", "Year", "-v", s, "--values", "2024")
    kpis(p, [("kpi_points", "Points (Modern Scale)"), ("kpi_points_py", "Points Change vs PY"),
             ("kpi_dnf_py", "DNF Rate Change vs PY"), ("kpi_grid_py", "Avg Grid Change vs PY")], x1=RIGHT - 200 - GAP - 150 - GAP)
    (a, aw), (b, bw), (c, cw) = xs(400, 464, 360)
    visual(p, "waterfallChart", "waterfall_drivers", a, ROW2_Y, aw, ROW2_H,
           ["Category:Driver.Driver Name", "Y:_Measures.Points Change vs PY"],
           "What moved the team's points", f"Change in modern-scale points vs last season, per driver · {WATERFALL}")
    visual(p, "keyDriversVisual", "ki_dnf", b, ROW2_Y, bw, ROW2_H,
           ["Target:Results.Is DNF", "ExplainBy:Race.Race Era", "ExplainBy:Constructor.Constructor Name", "ExplainBy:Results.Grid Band"],
           "What makes a DNF more likely", "Key influencers (AI visual) · all seasons, ignores the slicers · era, team, grid band")
    visual(p, "funnel", "funnel_race", c, ROW2_Y, cw, ROW2_H, ["Category:Funnel Stage.Stage", "Y:_Measures.Funnel Count"],
           "Race funnel", "Entries → wins for the team and season picked · each stage inside the one before")
    (a, aw), (b, bw), (c, cw) = xs(500, 368, 356)
    visual(p, "decompositionTreeVisual", "decomp_points", a, ROW3_Y, aw, ROW3_H,
           ["Analyze:_Measures.Points (Modern Scale)", "ExplainBy:Season.Era", "ExplainBy:Constructor.Constructor Name",
            "ExplainBy:Driver.Driver Name"],
           "Where the points came from", "Decomposition tree (AI visual): era → constructor → driver · all seasons, ignores the slicers")
    for name, x, w, measure, title, sub in [
            ("line_pit_forecast", b, bw, "Median Pit Lane Time", "Median pit lane time + forecast",
             f"Routine stops, 2011–2024 · forecast 3 seasons, 95% band · LINESTX trend {PIT_SLOPE} s per season (tooltip)"),
            ("line_lap_forecast", c, cw, "Avg Lap Time", "Average lap time + forecast",
             "1996–2024 · forecast 3 seasons, 95% band · calendar mix changes every year, read the level loosely")]:
        tip = ["Tooltips:_Measures.Pit Lane Time Trend"] if measure == "Median Pit Lane Time" else []
        v = visual(p, "lineChart", name, x, ROW3_Y, w, ROW3_H, ["Category:Season.Year", f"Y:_Measures.{measure}", *tip], title, sub)
        setp(v, "categoryAxis.axisType", "Scalar")
        # forecastLength is not in pbir's property catalog: written straight into visual.json (confidence band: default 95%)
        objects(v, "forecast", show="true", displayName="Forecast", forecastLength="3")
    no_filter(p, "slicer_team", "ki_dnf", "decomp_points", "line_pit_forecast", "line_lap_forecast")
    no_filter(p, "slicer_season", "ki_dnf", "decomp_points", "line_pit_forecast", "line_lap_forecast")
    mobile(p, [("header", 64), ("slicer_team", 56), ("slicer_season", 56), ("kpi_points", 72), ("kpi_points_py", 72),
               ("kpi_dnf_py", 72), ("kpi_grid_py", 72), ("waterfall_drivers", 220), ("funnel_race", 220), ("ki_dnf", 300),
               ("decomp_points", 300), ("line_pit_forecast", 200), ("line_lap_forecast", 200)])
    return s


def drill(v, table, levels, active):
    """Drill-down on the axis: several Category columns form the drill path; 'active' marks the level shown first."""
    def fn(doc):
        doc["visual"]["query"]["queryState"]["Category"] = {"projections": [
            {"queryRef": f"{table}.{c}", "nativeQueryRef": c, "field": {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": c}},
             **({"active": True} if c == active else {})} for c in levels]}
    vedit(v, fn)


# ------------------------------------------------------------------ tooltip and drillthrough pages
def card_page(p, cards, title):
    """A 320 × 240 report-page tooltip: a caption and a 2 × 2 grid of cards."""
    head = visual(p, "textbox", "caption", 8, 4, 304, 36, title=title)
    pbir("visuals", "title", head, "--fontColor", INK, "--fontSize", "12", "--bold")
    bare(head)
    for i, (name, measure) in enumerate(cards):
        visual(p, "cardVisual", name, 8 + (i % 2) * 156, 44 + (i // 2) * 96, 148, 88, data=[f"Data:_Measures.{measure}"])


def page_circuit_card(p):
    card_page(p, [("kpi_hosted", "Circuit Races Hosted"), ("kpi_pole", "Circuit Pole Win Rate"),
                  ("kpi_record", "Circuit Lap Record"), ("kpi_holder", "Circuit Lap Record Holder")], "Circuit card")


def page_season_card(p):
    card_page(p, [("kpi_champion", "Champions (driver · team)"), ("kpi_races", "Races"),
                  ("kpi_winners", "Different Winners Count"), ("kpi_share", "Avg Top Team Share")], "Season card")


def page_driver_profile(p):
    pbir("pages", "background", pp(p), "--color", PLANE, "--transparency", "0")
    button(p, "btn_back", X0, 20, 40, 40, "Back", tooltip="Back to the page you came from", icon="back")
    head = visual(p, "textbox", "header", X0 + 52, 8, 700, 64, title="DRIVER PROFILE",
                  subtitle="Drill-through target (right-click any driver) · career, team-mate duels, links")
    pbir("visuals", "title", head, "--fontColor", INK, "--fontSize", "20", "--bold")
    pbir("visuals", "subtitle", head, "--fontColor", INK2, "--fontSize", "10")
    bare(head)
    pbir("pages", "drillthrough", pp(p), "--table", "Driver", "--field", "Driver Name")
    (a, aw), (b, bw) = split(340)
    visual(p, "cardVisual", "kpi_driver", a, KPI_Y, aw, KPI_H, data=["Data:Driver.Driver Name"])
    kpis(p, [("kpi_titles", "Driver Titles Won"), ("kpi_wins", "Wins"), ("kpi_podiums", "Podiums"), ("kpi_skill", "Car-Adjusted Skill")], x0=b)
    (a, aw), (b, bw) = split(760)
    t = visual(p, "tableEx", "table_career", a, ROW2_Y, aw, ROW2_H,
               ["Values:Season.Year", "Values:Constructor.Constructor Name", "Values:_Measures.Points (Modern Scale)",
                "Values:_Measures.Wins", "Values:_Measures.Podiums", "Values:_Measures.Poles"],
               "Career, season by season", "Team driven for · modern-scale points")
    rename(t, {"_Measures.Points (Modern Scale)": "Points"})
    sort(t, "Season.Year", "Ascending")
    pbir("visuals", "cf", t, "--data-bars", "--field", "_Measures.Points (Modern Scale)", "--positive-color", PALETTE[0])
    h = visual(p, "tableEx", "table_h2h", b, ROW2_Y, bw, ROW2_H,
               ["Values:Teammate H2H.H2H Year", "Values:Constructor.Constructor Name", "Values:_Measures.Quali H2H Win %",
                "Values:_Measures.Race H2H Win %"],
               "Team-mate duels", "Share of races ahead of the team-mate · qualifying and race")
    rename(h, {"Teammate H2H.H2H Year": "Year", "_Measures.Quali H2H Win %": "Quali", "_Measures.Race H2H Win %": "Race"})
    sort(h, "Teammate H2H.H2H Year", "Ascending")
    (a, aw), (b, bw) = split(RIGHT - X0 - 240)
    w = visual(p, "tableEx", "table_links", a, ROW3_Y, aw, ROW3_H,
               ["Values:Driver.Driver Name", "Values:Driver.Driver Nationality", "Values:Driver.Driver First Season",
                "Values:Driver.Driver Last Season", "Values:Driver.Teams Driven For", "Values:Driver.Wikipedia URL"],
               "Read more", "Wikipedia link from the Ergast driver file (column categorised as Web URL)")
    rename(w, {"Driver.Driver First Season": "First", "Driver.Driver Last Season": "Last", "Driver.Teams Driven For": "Teams",
               "Driver.Wikipedia URL": "Wikipedia"})
    setp(w, "values.urlIcon", "true")
    pbir("add", "visual", "image", pp(p), "-n", "img_logo", "-x", str(b), "-y", str(ROW3_Y), "-w", str(bw), "-h", str(ROW3_H), "--force",
         "--image", str(png(Path(tempfile.mkdtemp()) / "chequered-logo.png", 96, 96, chequered)))
    return t


SKILL_HELP = ("Car-adjusted skill: a ridge-regularised regression of finish percentile on driver + team-season effects "
              "(src/driver_skill.py). The team-season effect absorbs car quality, so + means the driver beat an average driver "
              "in the same car. Ranked only with 40+ modelled races; cross-era gaps are indicative.")


def finish():
    """Accessibility pass over every page: alt text from each visual's title + subtitle (cards: their measure),
    and keyboard tab order top-to-bottom, left-to-right."""
    for page in (PBI / R / "definition" / "pages").glob("*/page.json"):
        docs = []
        for f in page.parent.glob("visuals/*/visual.json"):
            doc = json.loads(f.read_text())
            docs.append((doc["position"]["y"], doc["position"]["x"], f, doc))
        for i, (_, _, f, doc) in enumerate(sorted(docs, key=lambda d: (d[0], d[1]))):
            doc["position"]["tabOrder"] = i
            if "visual" in doc:
                vco = doc["visual"].get("visualContainerObjects", {})
                text = lambda k: next((e["properties"]["text"]["expr"]["Literal"]["Value"][1:-1].replace("''", "'")  # noqa: E731
                                       for e in vco.get(k, []) if "text" in e.get("properties", {})), "")
                vt = doc["visual"]["visualType"]
                fields = [p["queryRef"].split(".", 1)[-1] for r in doc["visual"].get("query", {}).get("queryState", {}).values()
                          for p in r.get("projections", [])]
                alt = ". ".join(t for t in (text("title"), text("subTitle")) if t) or {
                    "cardVisual": "KPI card: " + ", ".join(fields), "shape": "Decorative F1-red stripe",
                    "pageNavigator": "Page navigation", "image": "Project logo: chequered flag"}.get(vt, vt + ": " + ", ".join(fields))
                if vt == "actionButton":
                    alt = "Button: " + next((e["properties"]["tooltip"]["expr"]["Literal"]["Value"][1:-1]
                                             for e in vco.get("visualLink", []) if "tooltip" in e.get("properties", {})), "")
                vco["general"] = [{"properties": {"altText": lit(alt[:250])}}]
                doc["visual"]["visualContainerObjects"] = vco
            f.write_text(json.dumps(doc, indent=2, ensure_ascii=False))


def validate():
    """pbir validate, minus two known false positives: its role check rejects lineChart's Series (legend) role,
    although pbir's own capabilities.json lists it; and it types a field-parameter column on a measure well as a plain
    Column (Desktop puts field parameters on value wells; only the 'Dynasty Metric' parameter is exempted)."""
    out = json.loads(pbir("validate", R, "--all", "--json", check=False).stdout)
    errors = [e for e in out["errors"]
              if not (e["code"] == "VISUAL_ROLE_NOT_SUPPORTED" and e["details"] == {"role": "Series", "visual_type": "lineChart"})
              and not (e["code"] == "ROLE_TYPE_MISMATCH" and e["details"]["field_ref"] == "Dynasty Metric.Dynasty Metric")]
    for e in errors:
        print(f"  ✗ {e['code']}: {e['display_location']}\n    {e['message']}")
    codes = {}
    for w in out["warnings"]:
        codes[w["code"]] = codes.get(w["code"], 0) + 1
    print(f"  validate: {len(errors)} errors · warnings {codes}")
    if errors:
        sys.exit("✗ validation failed")


PAGES = [  # (name, subtitle, builder, help text behind the (i) button)
    ("Grid", "75 years of Formula 1 · 1950–2024 · every Grand Prix, driver, team and circuit", page_grid,
     "Dashboard: KPIs first (3 seconds), charts next (30 seconds), the champions table last (300 seconds). "
     "Hover a circuit or a season for its card."),
    ("Dynasties", "Which teams ruled which era, and how hard they dominated", page_dynasties,
     "Pick the ribbon metric and the era at the top right. Drill the ribbon up to era or down to season."),
    ("Legends", "The greatest drivers by record, and by skill once the car is taken out", page_legends,
     "The achievement slicer filters the charts, not the all-time KPIs. Click a driver in the scatter to highlight the skill bars. "
     "Right-click a driver to drill through to the profile."),
    ("Race Craft", "Grid position, reliability and circuits: what actually decides a Grand Prix", page_racecraft,
     "The era slicer only changes the grid chart. Reset filters clears it."),
    ("Title Fight", "Pick a season: how the championship swung round by round", page_titlefight,
     "Pick a season. Actual / Modern swaps the points scale of the line chart. Re-scored uses a historical points system; "
     "What-if uses today's scale with a win worth the slider value. The Re-scored / What-if Champion cards name the "
     "leader under each scenario and say whether the official champion changes."),
    ("Pit Wall", "Pick a race: positions lap by lap, pit stops and race control", page_pitwall,
     "Pick a race from 1996 on (lap timing starts there; a page filter keeps the rest out)."),
    ("Season Lens", "Pick a driver: a career season by season, and one season round by round", page_seasonlens,
     "Pick a driver for the career rows and a season for the bottom row."),
    ("Market", "The driver market: who holds the wins, who arrives, who stays, who leaves", page_market,
     "Era slicer at the top right. Rookie / returning / churn cards count the era picked; with every era selected they "
     "fall back to 2023 (the 'Period Counted' card says which). The cohort matrix ignores the slicer."),
    ("Why", "Why did a team's points fall? Change vs last season, then the drivers behind it", page_why,
     "Pick a constructor and a season: the cards and the waterfall compare it with the season before. "
     "Key influencers, the decomposition tree and the forecasts use all seasons. Forecasts are Power BI's built-in "
     "exponential smoothing: a projection, not a prediction."),
]
# hidden pages: (name, builder, kind) -- report-page tooltips (320 × 240) and the drillthrough target
EXTRA = [("Circuit Card", page_circuit_card, "tooltip"), ("Season Card", page_season_card, "tooltip"),
         ("Driver Profile", page_driver_profile, "drillthrough")]


def main():
    if not shutil.which("pbir"):
        sys.exit("pbir CLI not found — install with: uv tool install pbir-cli")
    shutil.rmtree(PBI / R, ignore_errors=True)
    (PBI / "F1.pbip").unlink(missing_ok=True)

    pbir("new", "report", R, "--thick", "--no-title")
    pbir("report", "rebind", R, "--local", "../F1.SemanticModel")
    first = next((PBI / R / "definition" / "pages").glob("*/page.json"))
    pbir("pages", "rename", f"{R}/{json.loads(first.read_text())['displayName']}.Page", "--to", PAGES[0][0], "-f")
    for name in [p[0] for p in PAGES[1:]] + [e[0] for e in EXTRA]:
        pbir("add", "page", pp(name), "-n", name, "--width", "1280", "--height", "720")
    for f in (PBI / R / "definition" / "pages").glob("*/visuals/*/visual.json"):  # drop template title boxes
        shutil.rmtree(f.parent)
    theme()
    for name, build, kind in EXTRA:  # built first: the Grid tooltips point at these pages
        print(f"  building {name}")
        if kind == "tooltip":
            pbir("pages", "set-tooltip", pp(name), "--width", "320", "--height", "240")
        build(name)
        pbir("pages", "hide", pp(name))
    for name, subtitle, build, help_text in PAGES:
        print(f"  building {name}")
        chrome(name, name.upper() if name != "Grid" else "FORMULA 1 · THE GRID", subtitle, help_text)
        build(name)
    pbir("pages", "active-page", R, PAGES[0][0])
    # report-level filter: completed seasons only (the data ends with 2024; a partial season would skew the per-season averages)
    pbir("add", "filter", "Season", "Year", "-r", R, "--type", "Advanced", "--operator", "LessThan", "--values", "2025")
    pbir("set", f"{R}.settings.exportDataMode", "--value", "AllowSummarized")  # export: summarized data only, never underlying rows
    finish()

    validate()


if __name__ == "__main__":
    main()
