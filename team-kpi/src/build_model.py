"""Generate the Power BI semantic model (TMDL) for the engineering team KPIs.

    uv run python src/build_model.py [--data-dir PATH]

--data-dir is the folder Power BI reads the Parquet marts from (default: this repo's data/marts);
change it later in Power BI via Transform data > Edit parameters.

Beyond the star schema the model uses the 2026 feature set:
  * DAX user-defined functions (functions.tmdl): rates, SLA checks, status and colour, percentiles,
    prior week / prior year on any calendar
  * calendar-based time intelligence: a Gregorian and an ISO-week calendar on Date
  * a Time Intelligence calculation group (previous week, last 4 weeks, MTD … YoY %) with a dynamic
    format string on YoY %
  * an inactive relationship on the resolved date, switched on with USERELATIONSHIP
  * field parameter (Analyze By) and a what-if SLA scale
  * dynamic row-level security by team (an access table keyed on the user's e-mail), plus
    object-level security that hides the people table from stakeholders
  * perspectives, descriptions on every measure, and synonyms for Q&A / Copilot
"""
import argparse
import json
import re
import shutil
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "powerbi" / "TeamKPI.SemanticModel"
T = "\t"
NS = uuid.UUID("7e4b2c19-3d5a-4f60-8b1e-9c0a2d4f6e81")

PCT0, PCT1, INT, DAYS, HOURS = "0%", "#,0.0%;-#,0.0%;#,0.0%", "#,0", "#,0.0", "#,0"  # PCT1: BPA PERCENTAGE_FORMATTING
M_TYPES = {"int64": "Int64.Type", "string": "type text", "double": "type number", "boolean": "type logical"}

LEAD_BANDS = [(1, "Up to 1 day"), (7, "2–7 days"), (30, "8–30 days"), (90, "31–90 days"), (None, "Over 90 days")]
LEAD_BAND_DAX = "VAR _d = Tickets[Lead Days]\nRETURN SWITCH ( TRUE (),\n    ISBLANK ( _d ), BLANK (),\n" + "".join(
    f'    _d <= {n}, "{b}",\n' for n, b in LEAD_BANDS if n) + f'    "{LEAD_BANDS[-1][1]}" )'
LEAD_BAND_ORDER_DAX = "VAR _d = Tickets[Lead Days]\nRETURN SWITCH ( TRUE (),\n    ISBLANK ( _d ), BLANK (),\n" + "".join(
    f"    _d <= {n}, {i},\n" for i, (n, _) in enumerate(LEAD_BANDS, 1) if n) + f"    {len(LEAD_BANDS)} )"

# Power BI table -> (mart, columns). Column: (name, dataType, options); options["src"] is the Parquet column,
# options["dax"] makes it a DAX calculated column, options["staging"] keeps it in Tickets Source only.
TABLES = {
    "Tickets": ("fact_ticket", [
        ("Ticket Key", "string", {"key": True, "src": "ticket_key", "sort": "Issue Number"}),
        ("Team Key", "string", {"hidden": True, "src": "team_key"}),
        ("Type Key", "int64", {"hidden": True, "src": "type_key"}),
        ("Priority Key", "int64", {"hidden": True, "src": "priority_key"}),
        ("Person Key", "int64", {"hidden": True, "src": "person_key"}),
        ("Stage", "string", {"src": "stage", "sort": "Stage Key"}),
        ("Stage Key", "int64", {"hidden": True, "src": "stage_key"}),
        ("Status", "string", {"src": "status"}), ("Resolution", "string", {"src": "resolution"}),
        ("Outcome", "string", {"src": "outcome"}),
        # read by the staging query, then moved to Ticket Text (long free text, only the drillthrough needs it)
        ("Summary", "string", {"src": "summary", "staging": True}),
        ("Created At", "dateTime", {"hidden": True, "src": "created_at", "fmt": "dd mmm yyyy hh:nn"}),
        ("Created Date", "dateTime", {"src": "created_date", "m": "type date"}),
        ("Resolved At", "dateTime", {"hidden": True, "src": "resolved_at", "fmt": "dd mmm yyyy hh:nn"}),
        ("Resolved Date", "dateTime", {"src": "resolved_date", "m": "type date"}),
        ("First Response At", "dateTime", {"hidden": True, "src": "first_response_at", "fmt": "dd mmm yyyy hh:nn"}),
        ("Started At", "dateTime", {"hidden": True, "src": "started_at", "fmt": "dd mmm yyyy hh:nn"}),
        ("Response Hours", "double", {"src": "response_hours", "fmt": "#,0.0"}),
        ("Wait Hours", "double", {"hidden": True, "src": "wait_hours", "fmt": "#,0.0"}),
        ("Lead Days", "double", {"src": "lead_days", "fmt": DAYS}),
        ("Cycle Days", "double", {"src": "cycle_days", "fmt": DAYS}),
        ("Age Days", "double", {"src": "age_days", "fmt": DAYS}),
        ("Reopened", "int64", {"src": "reopened", "fmt": INT}),
        ("Response SLA", "string", {"src": "response_sla"}),
        ("Resolution SLA", "string", {"src": "resolution_sla"}),
        # added in Power Query (TICKET_STEPS), not read from the mart
        ("Project", "string", {}), ("Issue Number", "int64", {"hidden": True}),
        ("Jira URL", "string", {"category": "WebUrl"}),
        ("Age Band", "string", {"sort": "Age Band Order"}), ("Age Band Order", "int64", {"hidden": True}),
        ("Days Past Target", "double", {"fmt": DAYS}), ("Stages Visited", "int64", {"fmt": INT}),
        # DAX calculated columns: fixed per ticket, so they're computed once at refresh and can sit on an axis
        ("Lead Time Band", "string", {"sort": "Lead Time Band Order", "dax": LEAD_BAND_DAX}),
        ("Lead Time Band Order", "int64", {"hidden": True, "dax": LEAD_BAND_ORDER_DAX}),
    ]),
    "Ticket Text": (None, [("Ticket Key", "string", {"key": True, "hidden": True}), ("Summary", "string", {})]),
    "Components": ("dim_component", [
        ("Component Key", "int64", {"key": True, "hidden": True, "src": "component_key"}),
        ("Project", "string", {"src": "team_key"}), ("Component", "string", {"src": "component"}),
    ]),
    "Ticket Components": ("bridge_ticket_component", [
        ("Ticket Key", "string", {"hidden": True, "src": "ticket_key"}),
        ("Component Key", "int64", {"hidden": True, "src": "component_key"}),
    ]),
    "Stage Time": ("fact_stage_time", [
        ("Ticket Key", "string", {"hidden": True, "src": "ticket_key"}),
        ("Stage Key", "int64", {"hidden": True, "src": "stage_key"}),
        ("Days", "double", {"hidden": True, "src": "days", "fmt": DAYS}),
    ]),
    "Backlog": ("fact_backlog_daily", [
        ("Date", "dateTime", {"hidden": True, "src": "date", "m": "type date"}),
        ("Team Key", "string", {"hidden": True, "src": "team_key"}),
        ("Priority Key", "int64", {"hidden": True, "src": "priority_key"}),
        ("Stage Key", "int64", {"hidden": True, "src": "stage_key"}),
        ("Open Count", "int64", {"hidden": True, "src": "open_tickets", "fmt": INT}),
    ]),
    "Teams": ("dim_team", [
        ("Team Key", "string", {"key": True, "hidden": True, "src": "team_key"}),
        ("Team", "string", {"src": "team"}), ("Product", "string", {"src": "product"}),
    ]),
    "People": ("dim_person", [
        ("Person Key", "int64", {"key": True, "hidden": True, "src": "person_key"}),
        ("Person", "string", {"src": "person"}), ("Home Team", "string", {"src": "home_team"}),
        ("First Delivered", "dateTime", {"src": "first_delivered", "m": "type date"}),
        ("First Quarter", "string", {"src": "first_quarter", "sort": "First Quarter Key"}),
        ("First Quarter Key", "int64", {"hidden": True, "src": "first_quarter_key"}),
    ]),
    "Stage Transitions": ("fact_stage_transition", [
        ("Ticket Key", "string", {"hidden": True, "src": "ticket_key"}),
        ("From Stage Key", "int64", {"hidden": True, "src": "from_stage_key"}),
        ("To Stage Key", "int64", {"hidden": True, "src": "to_stage_key"}),
        ("From Stage", "string", {"dax": "LOOKUPVALUE ( Stages[Stage], Stages[Stage Key], 'Stage Transitions'[From Stage Key] )",
                                  "sort": "From Stage Key"}),
        ("To Stage", "string", {"dax": "LOOKUPVALUE ( Stages[Stage], Stages[Stage Key], 'Stage Transitions'[To Stage Key] )",
                                "sort": "To Stage Key"}),
        ("Transition Date", "dateTime", {"src": "transition_date", "m": "type date"}),
    ]),
    "Priorities": ("dim_priority", [
        ("Priority Key", "int64", {"key": True, "hidden": True, "src": "priority_key"}),
        ("Priority", "string", {"src": "priority", "sort": "Priority Key"}),
        ("Response Target Hours", "int64", {"src": "response_hours", "fmt": INT}),
        ("Resolution Target Days", "int64", {"src": "resolution_days", "fmt": INT}),
    ]),
    "Types": ("dim_type", [
        ("Type Key", "int64", {"key": True, "hidden": True, "src": "type_key"}),
        ("Type", "string", {"src": "type", "sort": "Type Key"}),
    ]),
    "Stages": ("dim_stage", [
        ("Stage Key", "int64", {"key": True, "hidden": True, "src": "stage_key"}),
        ("Stage", "string", {"src": "stage", "sort": "Stage Key"}),
        ("Is Open", "boolean", {"hidden": True, "src": "is_open"}),
    ]),
    "Date": ("dim_date", [
        ("Date", "dateTime", {"key": True, "src": "date", "m": "type date"}),
        ("Year", "int64", {"src": "year", "fmt": "0"}),
        ("Year Quarter", "int64", {"hidden": True, "src": "year_quarter"}),
        ("Quarter", "string", {"src": "quarter", "sort": "Year Quarter"}),
        ("Year Month", "int64", {"hidden": True, "src": "year_month"}),
        ("Month", "string", {"src": "month", "sort": "Year Month"}),
        ("Month of Year", "int64", {"hidden": True, "src": "month_of_year"}),
        ("Month Name", "string", {"src": "month_name", "sort": "Month of Year"}),
        ("ISO Year", "int64", {"src": "iso_year", "fmt": "0"}),
        ("Year Week", "int64", {"hidden": True, "src": "year_week"}),
        ("Week", "string", {"src": "week", "sort": "Year Week"}),
        ("Week of Year", "int64", {"hidden": True, "src": "week_of_year"}),
        ("Week Start", "dateTime", {"src": "week_start", "m": "type date"}),
        ("Is Weekend", "boolean", {"hidden": True, "src": "is_weekend"}),
    ]),
    "Status Map": ("status_map", [
        ("Team Key", "string", {"hidden": True, "src": "team_key"}),
        ("Jira Status", "string", {"src": "status"}), ("Shared Stage", "string", {"src": "stage"}),
        ("Tickets Now", "int64", {"src": "tickets", "fmt": INT}),
    ]),
    "Data Checks": ("data_checks", [
        ("Team Key", "string", {"hidden": True, "src": "team_key"}),
        ("Step", "int64", {"hidden": True, "src": "step"}),
        ("Check", "string", {"src": "check", "sort": "Step"}),
        ("Tickets", "int64", {"src": "tickets", "fmt": INT}),
    ]),
    "Team Access": ("team_access", [
        ("Email", "string", {"src": "email"}), ("Team Key", "string", {"src": "team_key"}),
    ]),
    # Built from their own M below (no mart): an Excel seed and the rows that failed their type in Tickets Source.
    "Quarter Goals": (None, [
        ("Team Key", "string", {"hidden": True}), ("Year Quarter", "int64", {"hidden": True}),
        ("Delivery Goal", "int64", {"fmt": INT}), ("Goal Row", "int64", {"hidden": True}),
    ]),
    "Import Errors": (None, [("Ticket Key", "string", {}), ("Issue", "string", {})]),
}
HIDDEN_TABLES = {"Stage Time", "Backlog", "Team Access", "Ticket Components"}


def model_cols(cols):
    """Columns that end up in the model (staging-only columns are read by Tickets Source and dropped)."""
    return [c for c in cols if not c[2].get("staging")]

HIERARCHIES = {
    "Date": [("Calendar", ["Year", "Quarter", "Month"]), ("Weeks", ["ISO Year", "Week"])],
}

# Calendar-based time intelligence: (calendar, [(time unit, primary column, associated column)])
CALENDARS = [
    ("Gregorian", [("year", "Year", None), ("quarter", "Year Quarter", "Quarter"), ("month", "Year Month", "Month"),
                   ("monthOfYear", "Month of Year", "Month Name"), ("date", "Date", None)]),
    ("Weekly", [("year", "ISO Year", None), ("week", "Year Week", "Week"), ("weekOfYear", "Week of Year", None),
                ("date", "Date", None)]),
]

# (from table, from column, to table, to column[, options]). Options: active=False, both=True (filters both ways),
# one=True (1:1), m2m=True (many-to-many: a limited relationship).
RELATIONSHIPS = [
    ("Tickets", "Created Date", "Date", "Date"), ("Tickets", "Resolved Date", "Date", "Date", {"active": False}),
    ("Tickets", "Team Key", "Teams", "Team Key"), ("Tickets", "Type Key", "Types", "Type Key"),
    ("Tickets", "Priority Key", "Priorities", "Priority Key"), ("Tickets", "Person Key", "People", "Person Key"),
    ("Stage Time", "Ticket Key", "Tickets", "Ticket Key"), ("Stage Time", "Stage Key", "Stages", "Stage Key"),
    ("Stage Transitions", "Ticket Key", "Tickets", "Ticket Key"),
    ("Backlog", "Date", "Date", "Date"), ("Backlog", "Team Key", "Teams", "Team Key"),
    ("Backlog", "Priority Key", "Priorities", "Priority Key"), ("Backlog", "Stage Key", "Stages", "Stage Key"),
    ("Status Map", "Team Key", "Teams", "Team Key"), ("Data Checks", "Team Key", "Teams", "Team Key"),
    # 1:1 split: the long text sits in its own table (see README, performance)
    ("Ticket Text", "Ticket Key", "Tickets", "Ticket Key", {"one": True, "both": True}),
    # goals are set per team and quarter: many-to-many on Year Quarter (Date has ~91 rows per quarter)
    ("Quarter Goals", "Year Quarter", "Date", "Year Quarter", {"m2m": True}),
    ("Quarter Goals", "Team Key", "Teams", "Team Key"),
    # bridge: a ticket sits in several components and a component holds many tickets
    ("Ticket Components", "Ticket Key", "Tickets", "Ticket Key", {"both": True}),
    ("Ticket Components", "Component Key", "Components", "Component Key"),
]

# (name, description, signature => body)
FUNCTIONS = [
    ("Kpi.Rate", "Part ÷ whole; blank when the whole is empty.",
     "( part : NUMERIC, whole : NUMERIC ) => DIVIDE ( part, whole )"),
    ("Kpi.Status", "On track from 90%, at risk from 75%, off track below that.",
     '( rate : NUMERIC ) =>\n\tSWITCH ( TRUE (),\n\t\tISBLANK ( rate ), BLANK (),\n'
     '\t\trate >= 0.9, "On track",\n\t\trate >= 0.75, "At risk",\n\t\t"Off track" )'),
    ("Kpi.StatusColor", "Hex colour for a status, for conditional formatting.",
     '( status : STRING ) => SWITCH ( status, "On track", "#1B7F4B", "At risk", "#A35F00", "Off track", "#B42318", "#8C959F" )'),
    ("Kpi.ShareOf", "A measure as a share of the same measure with the filters on one table or column removed.",
     "( value : SCALAR NUMERIC EXPR, scope : ANYREF ) => DIVIDE ( value, CALCULATE ( value, REMOVEFILTERS ( scope ) ) )"),
    ("Kpi.PriorYear", "A measure one year back on the calendar passed in.",
     "( value : SCALAR NUMERIC EXPR, cal : CALENDARREF ) => CALCULATE ( value, DATEADD ( cal, -1, YEAR ) )"),
    ("Kpi.PriorWeek", "A measure one week back on the calendar passed in.",
     "( value : SCALAR NUMERIC EXPR, cal : CALENDARREF ) => CALCULATE ( value, DATEADD ( cal, -1, WEEK ) )"),
    ("Sla.Within", "True when the time taken is within the target scaled by the what-if SLA scale.",
     "( taken : NUMERIC, target : NUMERIC, scale : NUMERIC ) => taken <= target * scale"),
    ("Flow.Percentile", "The k-th percentile of a duration column in the current filter (0.85 = 85th).",
     "( days : ANYREF, k : NUMERIC ) => PERCENTILEX.INC ( FILTER ( VALUES ( days ), NOT ISBLANK ( days ) ), days, k )"),
]

# Time Intelligence calculation group: (item, DAX, format string expression or None)
TIME_ITEMS = [
    ("Current", "SELECTEDMEASURE ()", None),
    ("Previous Week", "CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Weekly', -1, WEEK ) )", None),
    ("Last 4 Weeks", "CALCULATE ( SELECTEDMEASURE (), DATESINPERIOD ( 'Date'[Date], MAX ( 'Date'[Date] ), -28, DAY ) )", None),
    ("MTD", "CALCULATE ( SELECTEDMEASURE (), DATESMTD ( 'Gregorian' ) )", None),
    ("QTD", "CALCULATE ( SELECTEDMEASURE (), DATESQTD ( 'Gregorian' ) )", None),
    ("YTD", "CALCULATE ( SELECTEDMEASURE (), DATESYTD ( 'Gregorian' ) )", None),
    ("Prior Year", "CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, YEAR ) )", None),
    ("YoY %", "VAR _py = CALCULATE ( SELECTEDMEASURE (), DATEADD ( 'Gregorian', -1, YEAR ) )\n"
              "RETURN DIVIDE ( SELECTEDMEASURE () - _py, _py )", '"+0.0%;-0.0%;0.0%"'),
]

CALCULATED = {  # what-if parameter
    "SLA Scale": (
        'SELECTCOLUMNS ( GENERATESERIES ( 0.5, 2, 0.25 ), "SLA Scale", [Value] )',
        [("SLA Scale", "[SLA Scale]", "double", [f"formatString: {PCT0}"])]),
    # Disconnected tables: rows to put on an axis; the measures read the selected row (no relationship).
    "ABC Class": (
        'DATATABLE ( "Class", STRING, "Lower", DOUBLE, "Upper", DOUBLE, '
        '{ { "A", 0, 0.8 }, { "B", 0.8, 0.95 }, { "C", 0.95, 1 } } )',
        [("Class", "[Class]", "string", []), ("Lower", "[Lower]", "double", ["isHidden", f"formatString: {PCT0}"]),
         ("Upper", "[Upper]", "double", ["isHidden", f"formatString: {PCT0}"])]),
    "Load Band": (
        'DATATABLE ( "Band", STRING, "Band Order", INTEGER, "Min Open", INTEGER, "Max Open", INTEGER, '
        '{ { "1–5", 1, 1, 5 }, { "6–20", 2, 6, 20 }, { "Over 20", 3, 21, 100000 } } )',
        [("Band", "[Band]", "string", ["sortByColumn: 'Band Order'"]),
         ("Band Order", "[Band Order]", "int64", ["isHidden"]), ("Min Open", "[Min Open]", "int64", ["isHidden"]),
         ("Max Open", "[Max Open]", "int64", ["isHidden"])]),
    "Funnel Step": (
        'DATATABLE ( "Step", STRING, "Step Order", INTEGER, '
        '{ { "Created", 1 }, { "Reached In Progress", 2 }, { "Reached In Review", 3 }, { "Delivered", 4 } } )',
        [("Step", "[Step]", "string", ["sortByColumn: 'Step Order'"]), ("Step Order", "[Step Order]", "int64", ["isHidden"])]),
    "Quarter Offset": (
        'SELECTCOLUMNS ( GENERATESERIES ( 1, 7, 1 ), "Quarter Offset", [Value] )',
        [("Quarter Offset", "[Quarter Offset]", "int64", ["formatString: 0"])]),
    "Contributor Segments": (
        'DATATABLE ( "Segment", STRING, "Segment Order", INTEGER, "Action", STRING, '
        '{ { "Core", 1, "Watch load; pair with a riser" }, '
        '{ "Rising", 2, "Mentor; hand over bigger tickets" }, '
        '{ "Fading", 3, "Check in; reassign open work" }, '
        '{ "Occasional", 4, "Tag good first issues" } } )',
        [("Segment", "[Segment]", "string", ["sortByColumn: 'Segment Order'"]),
         ("Segment Order", "[Segment Order]", "int64", ["isHidden"]), ("Action", "[Action]", "string", [])]),
}

# Field parameters: (label, NAMEOF target). Analyze By swaps the axis column, Metric Picker swaps the measure.
FIELD_PARAMETERS = {
    "Analyze By": [("Team", "Teams[Team]"), ("Priority", "Priorities[Priority]"), ("Type", "Types[Type]"),
                   ("Engineer", "People[Person]"), ("Stage", "Tickets[Stage]")],
    "Metric Picker": [("Delivered", "[Delivered]"), ("Median Lead Time", "[Median Lead Time]"),
                      ("Resolution SLA %", "[Resolution SLA %]"), ("Open Tickets", "[Open Tickets]")],
}
ANALYZE_BY = FIELD_PARAMETERS["Analyze By"]

# A team lead sees their own team(s): the access table maps a login e-mail to teams.
# Stakeholders see every team but not the people table (object-level security).
ROLES = {
    "Team Lead": {
        "filters": {"Teams": "Teams[Team Key] IN CALCULATETABLE ( VALUES ( 'Team Access'[Team Key] ), "
                             "'Team Access'[Email] = USERPRINCIPALNAME () )"},
        "hide_tables": [], "hide_columns": {},
    },
    "Stakeholder": {"filters": {}, "hide_tables": ["People"], "hide_columns": {"Ticket Text": ["Summary"]}},
    # Static RLS: the filter is a constant, so this role needs one copy per team (compare Team Lead above).
    "HBase Readers": {"filters": {"Teams": 'Teams[Team Key] = "HBASE"'}, "hide_tables": [], "hide_columns": {}},
}

PERSPECTIVES = {
    "Delivery": ["Tickets", "Stage Time", "Backlog", "Teams", "People", "Priorities", "Types", "Stages", "Date",
                 "Time Intelligence", "Analyze By", "Metric Picker", "SLA Scale", "Quarter Goals", "Ticket Text",
                 "Components", "Ticket Components", "Stage Transitions", "ABC Class", "Load Band", "Funnel Step",
                 "Quarter Offset", "Contributor Segments", "_Measures"],
    "Data Quality": ["Data Checks", "Status Map", "Import Errors", "Teams", "Tickets", "Date"],
}

# Measures. (folder, name, format, description, DAX)
RESOLVED = "USERELATIONSHIP ( Tickets[Resolved Date], 'Date'[Date] )"
OPEN_NOW = "ISBLANK ( Tickets[Resolved At] ), REMOVEFILTERS ( 'Date' )"
# New / returning only mean something against a period. When the dates cover the whole extract, every engineer's
# first delivery is inside it, so these measures fall back to the last quarter (Period Counted says which).
PERIOD_VARS = ("VAR _full = MIN ( 'Date'[Date] ) = CALCULATE ( MIN ( 'Date'[Date] ), REMOVEFILTERS ( 'Date' ) )\n"
               "    && MAX ( 'Date'[Date] ) = CALCULATE ( MAX ( 'Date'[Date] ), REMOVEFILTERS ( 'Date' ) )\n"
               "VAR _lastq = CALCULATE ( MAX ( 'Date'[Year Quarter] ), REMOVEFILTERS ( 'Date' ) )\n")


def in_period(expr):
    inner = "\n".join("        " + line for line in expr.split("\n"))
    return (PERIOD_VARS + "RETURN IF ( _full,\n    CALCULATE (\n" + inner +
            ",\n        REMOVEFILTERS ( 'Date' ), 'Date'[Year Quarter] = _lastq ),\n" + "\n".join(
                "    " + line for line in expr.split("\n")) + " )")


MEASURES = [
    # ---------------------------------------------------------------- Flow
    ("Flow", "Created", INT, "Tickets opened in the period (by created date).", "COUNTROWS ( Tickets )"),
    ("Flow", "Resolved", INT, "Tickets resolved in the period, any outcome (by resolved date, inactive relationship).",
     f"CALCULATE ( COUNTROWS ( Tickets ), {RESOLVED}, NOT ISBLANK ( Tickets[Resolved Date] ) )"),
    ("Flow", "Delivered", INT, "Tickets resolved as fixed or done in the period.",
     'CALCULATE ( [Resolved], Tickets[Outcome] = "Delivered" )'),
    ("Flow", "Net Flow", "+#,0;-#,0;0", "Created − resolved. Positive means the backlog grew.", "[Created] - [Resolved]"),
    ("Flow", "Open Backlog", INT, "Open tickets at the end of the last day in the period (daily snapshot).",
     "VAR _d = MAX ( 'Date'[Date] )\nRETURN CALCULATE ( SUM ( Backlog[Open Count] ), 'Date'[Date] = _d )"),
    ("Flow", "Weekly Throughput", "#,0.0", "Delivered tickets per ISO week in the period.",
     "DIVIDE ( [Delivered], DISTINCTCOUNT ( 'Date'[Year Week] ) )"),
    ("Flow", "Delivered Prev Week", INT, "Delivered a week earlier on the ISO-week calendar (UDF Kpi.PriorWeek).",
     "Kpi.PriorWeek ( [Delivered], 'Weekly' )"),
    ("Flow", "Created PY", INT, "Created a year earlier (UDF Kpi.PriorYear).", "Kpi.PriorYear ( [Created], 'Gregorian' )"),
    ("Flow", "Created YoY %", PCT1, "Change in tickets created against a year earlier.",
     "DIVIDE ( [Created] - [Created PY], [Created PY] )"),
    ("Flow", "Throughput Δ vs Prev Week", "+#,0;-#,0;0",
     "Delivered in the week minus the week before, via OFFSET over the selected weeks (a card shows the last week).",
     "VAR _weeks = ALLSELECTED ( 'Date'[Week Start] )\n"
     "VAR _wk = MAX ( 'Date'[Week Start] )\n"
     "RETURN\n    CALCULATE (\n"
     "        VAR _prev = OFFSET ( -1, _weeks, ORDERBY ( 'Date'[Week Start] ) )\n"
     "        RETURN IF ( NOT ISEMPTY ( _prev ),\n"
     "            [Delivered] - CALCULATE ( [Delivered], REMOVEFILTERS ( 'Date' ), _prev ) ),\n"
     "        'Date'[Week Start] = _wk )"),
    ("Flow", "Delivered 4W Avg", "#,0.0",
     "Delivered per week averaged over this week and the three before, via WINDOW (works in cards and tooltips).",
     "VAR _weeks = ALLSELECTED ( 'Date'[Week Start] )\n"
     "VAR _wk = MAX ( 'Date'[Week Start] )\n"
     "RETURN\n    CALCULATE (\n"
     "        AVERAGEX (\n"
     "            WINDOW ( -3, REL, 0, REL, _weeks, ORDERBY ( 'Date'[Week Start] ) ),\n"
     "            VAR _w = 'Date'[Week Start]\n"
     "            RETURN CALCULATE ( [Delivered], REMOVEFILTERS ( 'Date' ), 'Date'[Week Start] = _w ) + 0 ),\n"
     "        'Date'[Week Start] = _wk )"),
    ("Flow", "Reopen Rate", PCT1, "Share of tickets resolved in the period that had been reopened at least once.",
     f"Kpi.Rate ( CALCULATE ( COUNTROWS ( Tickets ), {RESOLVED}, NOT ISBLANK ( Tickets[Resolved Date] ), "
     "Tickets[Reopened] > 0 ), [Resolved] )"),
    # ---------------------------------------------------------------- Speed
    ("Speed", "Median Lead Time", DAYS, "Days from created to resolved, median, for tickets delivered in the period.",
     f'CALCULATE ( MEDIAN ( Tickets[Lead Days] ), {RESOLVED}, Tickets[Outcome] = "Delivered" )'),
    ("Speed", "Lead Time P85", DAYS, "85% of delivered tickets were resolved within this many days (UDF Flow.Percentile).",
     f'CALCULATE ( Flow.Percentile ( Tickets[Lead Days], 0.85 ), {RESOLVED}, Tickets[Outcome] = "Delivered" )'),
    ("Speed", "Median Cycle Time", DAYS, "Days from first marked in progress to resolved, median, where the start is known.",
     f'CALCULATE ( MEDIAN ( Tickets[Cycle Days] ), {RESOLVED}, Tickets[Outcome] = "Delivered", '
     "NOT ISBLANK ( Tickets[Started At] ) )"),
    ("Speed", "Cycle Time Coverage", PCT0, "Share of delivered tickets that were ever marked in progress or in review.",
     f'Kpi.Rate ( CALCULATE ( [Delivered], NOT ISBLANK ( Tickets[Started At] ) ), [Delivered] )'),
    ("Speed", "Lead Time vs Team Median", "+#,0.0;-#,0.0;0.0",
     "Median lead time here minus the median of the whole team in the same dates (ALLEXCEPT keeps only the team).",
     "VAR _dates = VALUES ( 'Date'[Date] )\n"
     "VAR _team = CALCULATE ( [Median Lead Time], ALLEXCEPT ( Tickets, Teams[Team] ), _dates )\n"
     "RETURN IF ( NOT ISBLANK ( [Median Lead Time] ), [Median Lead Time] - _team )"),
    ("Speed", "Days in Stage", DAYS, "Days tickets spent in a workflow stage (open stages only).", "SUM ( 'Stage Time'[Days] )"),
    ("Speed", "Flow Efficiency", PCT0, "Share of open time spent being worked on (in progress or in review) rather than waiting.",
     'Kpi.Rate ( CALCULATE ( [Days in Stage], Stages[Stage] IN { "In Progress", "In Review" } ), '
     "CALCULATE ( [Days in Stage], REMOVEFILTERS ( Stages ) ) )"),
    # ---------------------------------------------------------------- SLA
    ("SLA", "SLA Scale %", PCT0, "What-if multiplier on every SLA target; 100% is the policy.",
     "SELECTEDVALUE ( 'SLA Scale'[SLA Scale], 1 )"),
    ("SLA", "Median First Response", "#,0.0", "Hours from created to the first action by someone other than the reporter.",
     "MEDIAN ( Tickets[Response Hours] )"),
    ("SLA", "Response SLA %", PCT0,
     "Tickets created in the period that got a first response within the priority's target. "
     "Tickets still waiting count once their target has passed.",
     "VAR _s = [SLA Scale %]\n"
     "VAR _t = ADDCOLUMNS ( Tickets, \"@target\", RELATED ( Priorities[Response Target Hours] ) )\n"
     "VAR _met = COUNTROWS ( FILTER ( _t, NOT ISBLANK ( Tickets[Response Hours] ) && Sla.Within ( Tickets[Wait Hours], [@target], _s ) ) )\n"
     "VAR _due = COUNTROWS ( FILTER ( _t, NOT ISBLANK ( Tickets[Response Hours] ) || NOT Sla.Within ( Tickets[Wait Hours], [@target], _s ) ) )\n"
     "RETURN Kpi.Rate ( _met, _due )"),
    ("SLA", "Resolution SLA Target", PCT0, "The resolution SLA target: 90%, where on track starts (gauge target line).",
     "0.9"),
    ("SLA", "Resolution SLA %", PCT0, "Tickets resolved in the period within the priority's resolution target.",
     "VAR _s = [SLA Scale %]\n"
     f"RETURN CALCULATE (\n    Kpi.Rate (\n        COUNTROWS ( FILTER ( Tickets, Sla.Within ( Tickets[Lead Days], "
     "RELATED ( Priorities[Resolution Target Days] ), _s ) ) ),\n        COUNTROWS ( Tickets ) ),\n"
     f"    {RESOLVED}, NOT ISBLANK ( Tickets[Resolved Date] ) )"),
    ("SLA", "P1 Resolution SLA %", PCT0,
     "Resolution SLA for P1 tickets; KEEPFILTERS intersects with a Priority slicer instead of replacing it.",
     "CALCULATE ( [Resolution SLA %], KEEPFILTERS ( Priorities[Priority Key] = 1 ) )"),
    ("SLA", "Open Breached", INT, "Tickets open on the as-of date that are already past their resolution target.",
     "VAR _s = [SLA Scale %]\n"
     "RETURN\n    CALCULATE (\n        [Open Tickets],\n"
     "        // FILTER as a filter argument: RELATED works because a Tickets row carries its expanded table,\n"
     "        // every column on the one side of its relationships (Priorities included)\n"
     "        FILTER (\n            CALCULATETABLE ( Tickets, REMOVEFILTERS ( 'Date' ) ),\n"
     "            NOT Sla.Within ( Tickets[Age Days], RELATED ( Priorities[Resolution Target Days] ), _s ) ) )"),
    ("SLA", "SLA Status", None, "On track / at risk / off track on the resolution SLA (UDF Kpi.Status).",
     "Kpi.Status ( [Resolution SLA %] )"),
    ("SLA", "SLA Color", None, "Colour for the SLA status, used by conditional formatting.", "Kpi.StatusColor ( [SLA Status] )"),
    # ---------------------------------------------------------------- Workload (as-of snapshot)
    ("Workload", "Open Tickets", INT, "Tickets open on the as-of date (30 Sep 2026), whatever the date filter.",
     f"CALCULATE ( COUNTROWS ( Tickets ), {OPEN_NOW} )"),
    ("Workload", "Work in Progress", INT, "Open tickets in progress or in review on the as-of date.",
     'CALCULATE ( [Open Tickets], Tickets[Stage] IN { "In Progress", "In Review" } )'),
    ("Workload", "Unassigned Open", INT, "Open tickets nobody is assigned to.",
     "CALCULATE ( [Open Tickets], People[Person Key] = 0 )"),
    ("Workload", "Aged Open", INT, "Open tickets older than 180 days.", "CALCULATE ( [Open Tickets], Tickets[Age Days] > 180 )"),
    ("Workload", "Engineers With Open Work", INT, "Engineers with at least one open ticket assigned.",
     f"CALCULATE ( DISTINCTCOUNT ( Tickets[Person Key] ), {OPEN_NOW}, People[Person Key] <> 0 )"),
    ("Workload", "Open per Engineer", "#,0.0", "Assigned open tickets per engineer who has any.",
     "DIVIDE ( [Open Tickets] - [Unassigned Open], [Engineers With Open Work] )"),
    ("Workload", "Active Engineers", INT, "Engineers who resolved at least one ticket in the period.",
     f"CALCULATE ( DISTINCTCOUNT ( Tickets[Person Key] ), {RESOLVED}, NOT ISBLANK ( Tickets[Resolved Date] ), "
     "People[Person Key] <> 0 )"),
    ("Workload", "Delivered Share", PCT1, "Share of all delivered tickets in the current filter (UDF Kpi.ShareOf).",
     "Kpi.ShareOf ( [Delivered], People )"),
    ("Workload", "Visible Delivered Share", PCT1,
     "Share of what the engineers shown in the visual delivered (ALLSELECTED keeps the visual and page filters).",
     "DIVIDE ( [Delivered], CALCULATE ( [Delivered], ALLSELECTED ( People ) ) )"),
    ("Workload", "Engineer Rank in Team", INT, "Rank by delivered tickets among the engineers of the same home team.",
     "IF ( ISINSCOPE ( People[Person] ) && NOT ISBLANK ( [Delivered] ),\n"
     "    RANK ( DENSE, ALLSELECTED ( People[Person], People[Home Team] ), ORDERBY ( [Delivered], DESC ), LAST,\n"
     "        PARTITIONBY ( People[Home Team] ) ) )"),
    ("Workload", "Top Engineer", None, "The engineer who delivered the most in the current filter (INDEX 1).",
     'VAR _people = FILTER ( ALLSELECTED ( People[Person] ), People[Person] <> "Unassigned" )\n'
     "VAR _top = INDEX ( 1, _people, ORDERBY ( [Delivered], DESC ) )\n"
     "RETURN IF ( NOT ISBLANK ( [Delivered] ), MAXX ( _top, People[Person] ) )"),
    ("Workload", "Top Engineer Share", PCT0,
     "Share of the assigned delivered tickets that the busiest engineer closed: a bus-factor warning when high.",
     "VAR _top = MAXX ( FILTER ( VALUES ( People[Person Key] ), People[Person Key] <> 0 ), [Delivered] )\n"
     "RETURN DIVIDE ( _top, CALCULATE ( [Delivered], People[Person Key] <> 0 ) )"),
    ("Workload", "Overloaded Engineers", INT, "Engineers holding more than 20 open tickets on the as-of date.",
     "COUNTROWS ( FILTER ( VALUES ( People[Person Key] ), People[Person Key] <> 0 && [Open Tickets] > 20 ) ) + 0"),
    # ---------------------------------------------------------------- Goals (synthetic, seeds/quarter_goals.xlsx)
    ("Goals", "Delivery Target", INT, "Delivery goal for the quarters in the filter (synthetic goals, Quarter Goals).",
     "SUM ( 'Quarter Goals'[Delivery Goal] )"),
    ("Goals", "Goal Attainment %", PCT0, "Delivered ÷ delivery goal; goals are per whole quarter (synthetic).",
     "Kpi.Rate ( [Delivered], [Delivery Target] )"),
    # ---------------------------------------------------------------- Contributors (People, from Tickets)
    ("Contributors", "Delivered Cumulative %", PCT0,
     "Pareto: share of all delivered tickets closed by this engineer and everyone who delivered at least as many.",
     "VAR _me = [Delivered]\n"
     "VAR _t = ADDCOLUMNS ( FILTER ( ALLSELECTED ( People[Person] ), People[Person] <> \"Unassigned\" ), \"@d\", [Delivered] )\n"
     "RETURN IF ( NOT ISBLANK ( _me ), DIVIDE ( SUMX ( FILTER ( _t, [@d] >= _me ), [@d] ), SUMX ( _t, [@d] ) ) )"),
    ("Contributors", "Pareto Line", PCT0, "The 80% reference line of the Pareto chart.", "0.8"),
    ("Contributors", "Engineers in Class", INT,
     "Engineers whose cumulative share of deliveries falls in the ABC class on the row (A up to 80%, B to 95%, C the rest); "
     "follows the slicers.",
     "VAR _t = FILTER ( ADDCOLUMNS ( FILTER ( ALLSELECTED ( People[Person] ), People[Person] <> \"Unassigned\" ), "
     "\"@d\", [Delivered] ), [@d] > 0 )\n"
     "VAR _tot = SUMX ( _t, [@d] )\n"
     "VAR _cum = ADDCOLUMNS ( _t, \"@cum\", VAR _me = [@d] RETURN DIVIDE ( SUMX ( FILTER ( _t, [@d] >= _me ), [@d] ), _tot ) )\n"
     "VAR _lo = MIN ( 'ABC Class'[Lower] )\nVAR _hi = MAX ( 'ABC Class'[Upper] )\n"
     "RETURN COUNTROWS ( FILTER ( _cum, [@cum] > _lo && [@cum] <= _hi ) ) + 0"),
    ("Contributors", "Engineers for 80%", INT, "How many engineers close 80% of the delivered tickets (class A).",
     "CALCULATE ( [Engineers in Class], 'ABC Class'[Class] = \"A\" )"),
    ("Contributors", "Engineers in Band", INT,
     "Engineers whose open tickets on the as-of date fall in the load band on the row.",
     "VAR _lo = MIN ( 'Load Band'[Min Open] )\nVAR _hi = MAX ( 'Load Band'[Max Open] )\n"
     "VAR _t = ADDCOLUMNS ( FILTER ( VALUES ( People[Person Key] ), People[Person Key] <> 0 ), \"@o\", [Open Tickets] )\n"
     "RETURN COUNTROWS ( FILTER ( _t, [@o] >= _lo && [@o] <= _hi ) ) + 0"),
    ("Contributors", "Last Delivery", "d mmm yyyy", "Date of the engineer's most recent delivered ticket, whatever the date filter.",
     f"CALCULATE ( MAX ( Tickets[Resolved Date] ), Tickets[Outcome] = \"Delivered\", REMOVEFILTERS ( 'Date' ) )"),
    ("Contributors", "Recency Days", INT, "RFM recency: days from the last delivery to the as-of date.",
     "VAR _l = [Last Delivery]\nVAR _asof = CALCULATE ( MAX ( 'Date'[Date] ), REMOVEFILTERS ( 'Date' ) )\n"
     "RETURN IF ( NOT ISBLANK ( _l ), DATEDIFF ( _l, _asof, DAY ) )"),
    ("Contributors", "Active Quarters", INT, "RFM frequency: calendar quarters with at least one delivery.",
     "VAR _t = CALCULATETABLE ( Tickets, Tickets[Outcome] = \"Delivered\", REMOVEFILTERS ( 'Date' ) )\n"
     "RETURN IF ( NOT ISEMPTY ( _t ), COUNTROWS ( DISTINCT ( SELECTCOLUMNS ( _t, \"@q\", "
     "YEAR ( Tickets[Resolved Date] ) * 10 + QUARTER ( Tickets[Resolved Date] ) ) ) ) )"),
    ("Contributors", "Delivered All Time", INT, "RFM volume: delivered tickets over the whole extract.",
     "CALCULATE ( [Delivered], REMOVEFILTERS ( 'Date' ) )"),
    ("Contributors", "RFV Score", None, "Recency, frequency and volume scored 1–5 each, written as R-F-V.",
     "VAR _r = [Recency Days]\nVAR _f = [Active Quarters]\nVAR _v = [Delivered All Time]\n"
     "RETURN IF ( NOT ISBLANK ( _r ),\n"
     "    SWITCH ( TRUE (), _r <= 30, 5, _r <= 90, 4, _r <= 180, 3, _r <= 365, 2, 1 ) & \"-\"\n"
     "    & SWITCH ( TRUE (), _f >= 7, 5, _f >= 5, 4, _f >= 3, 3, _f >= 2, 2, 1 ) & \"-\"\n"
     "    & SWITCH ( TRUE (), _v >= 200, 5, _v >= 50, 4, _v >= 20, 3, _v >= 5, 2, 1 ) )"),
    ("Contributors", "Contributor Segment", None,
     "Core (R and F 4+), Rising (recent, F up to 3), Fading (R up to 3, F 3+), Occasional (the rest).",
     "VAR _r = [Recency Days]\nVAR _f = [Active Quarters]\n"
     "VAR _rs = SWITCH ( TRUE (), _r <= 30, 5, _r <= 90, 4, _r <= 180, 3, _r <= 365, 2, 1 )\n"
     "VAR _fs = SWITCH ( TRUE (), _f >= 7, 5, _f >= 5, 4, _f >= 3, 3, _f >= 2, 2, 1 )\n"
     "RETURN IF ( NOT ISBLANK ( _r ), SWITCH ( TRUE (),\n"
     "    _rs >= 4 && _fs >= 4, \"Core\", _rs >= 4, \"Rising\", _fs >= 3, \"Fading\", \"Occasional\" ) )"),
    ("Contributors", "Contributors in Segment", INT, "Engineers in the RFM segment on the row.",
     "VAR _seg = SELECTEDVALUE ( 'Contributor Segments'[Segment] )\n"
     "VAR _t = ADDCOLUMNS ( FILTER ( VALUES ( People[Person Key] ), People[Person Key] <> 0 ), \"@s\", [Contributor Segment] )\n"
     "RETURN COUNTROWS ( FILTER ( _t, [@s] = _seg ) ) + 0"),
    ("Contributors", "Period Counted", None,
     "The period the active / new / returning cards count: the selected dates, or the last quarter when nothing "
     "narrows the dates (over the whole extract everyone would be new): the quarter name, or \"Selected\".",
     PERIOD_VARS + "RETURN IF ( _full, CALCULATE ( MAX ( 'Date'[Quarter] ), REMOVEFILTERS ( 'Date' ), "
     "'Date'[Year Quarter] = _lastq ), \"Selected\" )"),
    ("Contributors", "Active Contributors", INT,
     "Engineers who delivered at least one ticket in the period (the last quarter when no dates are selected).",
     in_period("COUNTROWS ( FILTER ( VALUES ( People[Person Key] ), People[Person Key] <> 0 && [Delivered] > 0 ) ) + 0")),
    ("Contributors", "New Contributors", INT,
     "Active engineers whose first delivery (in the teams in the filter) falls inside the period "
     "(the last quarter when no dates are selected).",
     in_period("VAR _s = MIN ( 'Date'[Date] )\n"
               "VAR _t = ADDCOLUMNS ( FILTER ( VALUES ( People[Person Key] ), People[Person Key] <> 0 && [Delivered] > 0 ), "
               "\"@first\",\n"
               "    CALCULATE ( MIN ( Tickets[Resolved Date] ), Tickets[Outcome] = \"Delivered\", REMOVEFILTERS ( 'Date' ) ) )\n"
               "RETURN COUNTROWS ( FILTER ( _t, [@first] >= _s ) ) + 0")),
    ("Contributors", "Returning Contributors", INT, "Active engineers who had delivered before the period started.",
     "[Active Contributors] - [New Contributors]"),
    ("Contributors", "Cohort Retention %", PCT0,
     "Share of the engineers whose first delivery was in the row's quarter who delivered again N quarters later.",
     "VAR _off = SELECTEDVALUE ( 'Quarter Offset'[Quarter Offset] )\n"
     "VAR _fq = MIN ( People[First Quarter Key] )\n"
     "VAR _i = INT ( _fq / 10 ) * 4 + MOD ( _fq, 10 ) - 1 + _off\n"
     "VAR _target = INT ( _i / 4 ) * 10 + MOD ( _i, 4 ) + 1\n"
     "VAR _last = CALCULATE ( MAX ( 'Date'[Year Quarter] ), REMOVEFILTERS ( 'Date' ) )\n"
     "VAR _cohort = CALCULATE ( COUNTROWS ( People ), NOT ISBLANK ( People[First Quarter Key] ) )\n"
     "VAR _active = CALCULATE ( DISTINCTCOUNT ( Tickets[Person Key] ), Tickets[Outcome] = \"Delivered\", REMOVEFILTERS ( 'Date' ),\n"
     "    FILTER ( ALL ( Tickets[Resolved Date] ), YEAR ( Tickets[Resolved Date] ) * 10 + QUARTER ( Tickets[Resolved Date] ) = _target ) )\n"
     "RETURN IF ( NOT ISBLANK ( _fq ) && NOT ISBLANK ( _off ) && _target <= _last, DIVIDE ( _active, _cohort ) + 0 )"),
    ("Contributors", "Churned Contributors", INT, "Engineers with 5+ deliveries and none in the last 90 days.",
     "VAR _t = ADDCOLUMNS ( FILTER ( VALUES ( People[Person Key] ), People[Person Key] <> 0 ), \"@v\", [Delivered All Time], "
     "\"@r\", [Recency Days] )\nRETURN COUNTROWS ( FILTER ( _t, [@v] >= 5 && [@r] > 90 ) ) + 0"),
    ("Contributors", "At Risk Contributors", INT, "Engineers with 5+ deliveries whose last one was 46–90 days ago.",
     "VAR _t = ADDCOLUMNS ( FILTER ( VALUES ( People[Person Key] ), People[Person Key] <> 0 ), \"@v\", [Delivered All Time], "
     "\"@r\", [Recency Days] )\nRETURN COUNTROWS ( FILTER ( _t, [@v] >= 5 && [@r] > 45 && [@r] <= 90 ) ) + 0"),
    # ---------------------------------------------------------------- Stage flow
    ("Stage flow", "Transitions", INT, "Moves from one shared stage to another (tickets created in the period).",
     "COUNTROWS ( 'Stage Transitions' ) + 0"),
    ("Stage flow", "Review Bounce Rate", PCT1, "Share of moves out of In Review that went back to In Progress.",
     "DIVIDE ( CALCULATE ( [Transitions], 'Stage Transitions'[From Stage] = \"In Review\", "
     "'Stage Transitions'[To Stage] = \"In Progress\" ),\n"
     "    CALCULATE ( [Transitions], 'Stage Transitions'[From Stage] = \"In Review\" ) )"),
    ("Stage flow", "Funnel Tickets", INT, "Tickets created in the period that reached the funnel step on the row.",
     "SWITCH ( SELECTEDVALUE ( 'Funnel Step'[Step Order] ),\n"
     "    1, COUNTROWS ( Tickets ),\n"
     "    2, CALCULATE ( DISTINCTCOUNT ( 'Stage Time'[Ticket Key] ), Stages[Stage] = \"In Progress\" ),\n"
     "    3, CALCULATE ( DISTINCTCOUNT ( 'Stage Time'[Ticket Key] ), Stages[Stage] = \"In Review\" ),\n"
     "    4, CALCULATE ( COUNTROWS ( Tickets ), Tickets[Outcome] = \"Delivered\" ) )"),
    ("Stage flow", "Funnel %", PCT0, "Funnel step as a share of the tickets created.",
     "DIVIDE ( [Funnel Tickets], CALCULATE ( [Funnel Tickets], REMOVEFILTERS ( 'Funnel Step' ), 'Funnel Step'[Step Order] = 1 ) )"),
    ("Stage flow", "Net Flow Share", "+0%;-0%;0%",
     "This row's share of the backlog growth across the teams and priorities in the filter.",
     "DIVIDE ( [Net Flow], CALCULATE ( [Net Flow], ALLSELECTED ( Teams ), ALLSELECTED ( Priorities ) ) )"),
    ("Stage flow", "Backlog Trend /wk", "+#,0.0;-#,0.0;0.0",
     "Least-squares slope (LINESTX) of the week-end open backlog over the weeks in the filter, per week.",
     "VAR _t = ADDCOLUMNS ( VALUES ( 'Date'[Year Week] ), \"@x\", CONVERT ( CALCULATE ( MAX ( 'Date'[Date] ) ), DOUBLE ), "
     "\"@y\", [Open Backlog] )\n"
     "RETURN MAXX ( LINESTX ( _t, [@y], [@x] ), [Slope1] ) * 7"),
    # ---------------------------------------------------------------- Narrative
    ("Narrative", "Headline", None, "One-sentence summary that follows the slicers.",
     'VAR _net = [Net Flow]\nRETURN\n    FORMAT ( [Created], "#,0" ) & " tickets in, " & FORMAT ( [Resolved], "#,0" ) & " out: the backlog "\n'
     '        & IF ( _net > 0, "grew by " & FORMAT ( _net, "#,0" ), "shrank by " & FORMAT ( -_net, "#,0" ) ) & ". "\n'
     '        & FORMAT ( [Resolution SLA %], "0%" ) & " were resolved within SLA; median lead time "\n'
     '        & FORMAT ( [Median Lead Time], "0.0" ) & " days. " & FORMAT ( [Open Breached], "#,0" )\n'
     '        & " open tickets are past their target."'),
]
# Hosted in Data Checks so they sit with the table they summarise.
CHECK_MEASURES = [
    ("Data quality", "Tickets Pulled", INT, "Issues returned by the Jira API for the window.",
     "CALCULATE ( SUM ( 'Data Checks'[Tickets] ), 'Data Checks'[Step] = 1 )"),
    ("Data quality", "Tickets Counted", INT, "Issues kept as tickets (epics excluded).",
     "CALCULATE ( SUM ( 'Data Checks'[Tickets] ), 'Data Checks'[Step] = 3 )"),
    ("Data quality", "Tickets Excluded", INT, "Issues left out, with the reason in the Check column.",
     "[Tickets Pulled] - [Tickets Counted]"),
    ("Data quality", "Check Tickets", INT, "Tickets behind each data check.", "SUM ( 'Data Checks'[Tickets] )"),
    ("Data quality", "No Start Recorded", INT, "Resolved tickets never marked in progress: cycle time can't be measured.",
     "CALCULATE ( SUM ( 'Data Checks'[Tickets] ), 'Data Checks'[Step] = 5 )"),
    ("Data quality", "Import Error Rows", INT, "Ticket rows where a duration failed its type on import (query Import Errors).",
     "COUNTROWS ( 'Import Errors' ) + 0"),
    ("Data quality", "Tickets in Status", INT,
     "Tickets whose Jira status is the mapping row's status, live from Tickets (TREATAS: no relationship needed).",
     "CALCULATE ( COUNTROWS ( Tickets ), TREATAS ( VALUES ( 'Status Map'[Jira Status] ), Tickets[Status] ), "
     "REMOVEFILTERS ( 'Date' ) )"),
]

SYNONYMS = {  # Q&A / Copilot terms
    "Tickets": ["issues", "work items", "jira tickets", "bugs"], "Created": ["opened", "incoming", "arrivals", "new tickets"],
    "Resolved": ["closed", "done", "completed", "finished"], "Median Lead Time": ["turnaround", "time to resolve", "lead time"],
    "People": ["engineers", "assignees", "developers"], "Teams": ["squads", "projects"],
    "Open Backlog": ["backlog", "open work", "queue"],
}


def q(name):
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else f"'{name}'"


def tag(*parts):
    """Deterministic lineage tags, so a rebuild only diffs where the model really changed."""
    return str(uuid.uuid5(NS, "/".join(parts)))


def block(text, indent):
    return [f"{T * indent}{l}" for l in text.split("\n")]


TEXT_MEASURES = {"SLA Status", "SLA Color", "Headline", "Top Engineer", "RFV Score", "Contributor Segment",
                 "Period Counted"}
# Deliberate BPA exceptions, written as the annotation Tabular Editor's "Ignore item" writes. measure -> (rules, why)
BPA_IGNORE = {
    **{m: (["PROVIDE_FORMAT_STRING_FOR_MEASURES", "INTEGER_FORMATTING"], "returns text: a format string means nothing")
       for m in TEXT_MEASURES},
    **{m: (["INTEGER_FORMATTING"], "signed format (+/-) on purpose: the sign is the message")
       for m in ("Net Flow", "Throughput Δ vs Prev Week", "Lead Time vs Team Median", "Backlog Trend /wk")},
    "Last Delivery": (["INTEGER_FORMATTING"], "returns a date, formatted as a date"),
}


def measure_lines(table, folder, name, fmt, desc, dax):
    lines = [f"{T}/// {desc}"] if desc else []
    lines += [f"{T}measure {q(name)} =", *block(dax, 3)] if "\n" in dax else [f"{T}measure {q(name)} = {dax}"]
    if fmt:
        lines.append(f"{T*2}formatString: {fmt}")
    lines += [f"{T*2}displayFolder: {folder}", f"{T*2}lineageTag: {tag(table, 'm', name)}"]
    if name in BPA_IGNORE:  # the reason lives in BPA_IGNORE; the annotation value must stay plain JSON
        lines += ["", f"{T*2}annotation BestPracticeAnalyzer_IgnoreRules = "
                  + json.dumps({"RuleIDs": BPA_IGNORE[name][0]}, separators=(",", ":"))]
    return lines + [""]


# ------------------------------------------------------------------ Power Query (M)
# Every query reads a Parquet mart the same way: pick the columns first (Parquet only reads those), rename,
# then set types once the shape is final. Steps are named and commented so the Applied Steps pane reads like a recipe.
LAKEHOUSE_DB = "teamkpi_lh"


def m_source(mart, cols, lakehouse=False):
    src = [(c, d, o) for c, d, o in cols if "src" in o]
    types = ", ".join(f'{{"{c}", {o.get("m") or M_TYPES.get(d, "type datetime")}}}' for c, d, o in src)
    renames = ", ".join(f'{{"{o["src"]}", "{c}"}}' for c, _, o in src)
    keep = ", ".join(f'"{o["src"]}"' for _, _, o in src)
    read = ["    // Fabric variant: the lakehouse SQL endpoint; every step below folds into one SELECT",
            f'    Source = Sql.Database(LakehouseSqlEndpoint, "{LAKEHOUSE_DB}"),',
            f'    Data = Source{{[Schema = "dbo", Item = "{mart}"]}}[Data],'] if lakehouse else \
           ["    // Read the mart the pipeline wrote",
            f'    Data = Parquet.Document(File.Contents(DataFolder & "{mart}.parquet")),']
    return ["let", *read,
            "    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these",
            f"    Kept = Table.SelectColumns(Data, {{{keep}}}),",
            "    // Business names",
            f"    Renamed = Table.RenameColumns(Kept, {{{renames}}}),",
            "    // Types last, once the shape is final",
            f"    Typed = Table.TransformColumnTypes(Renamed, {{{types}}})",
            "in", "    Typed"]


def chain(source_lines, steps):
    """Append (step, comment, expression with {prev}) steps to a let … in query."""
    body, last = source_lines[:-2], source_lines[-1].strip()
    body[-1] += ","
    for k, (name, comment, expr) in enumerate(steps):
        body += [f"    // {comment}", f"    {name} = {expr.format(prev=last)}" + ("," if k < len(steps) - 1 else "")]
        last = name
    return body + ["in", f"    {last}"]


AGE_BANDS = [(30, "0–30 days"), (90, "31–90 days"), (180, "91–180 days"), (None, "Over 180 days")]
TICKET_STEPS = [
    ("NoErrors", "A duration that fails its type becomes null instead of failing the refresh (Import Errors keeps those rows)",
     'Table.ReplaceErrorValues({prev}, {{{{"Lead Days", null}}, {{"Cycle Days", null}}, {{"Response Hours", null}}}})'),
    ("HasKey", "Drop rows without a ticket key: they can't join to anything",
     "Table.SelectRows({prev}, each [Ticket Key] <> null)"),
    ("UpToAsOf", "Nothing created after the as-of date (parameter AsOfDate)",
     "Table.SelectRows({prev}, each [Created Date] <= Date.From(AsOfDate))"),
    ("OwnerOrUnassigned", "No assignee -> person 0, the Unassigned row in People",
     'Table.ReplaceValue({prev}, null, 0, Replacer.ReplaceValue, {{"Person Key"}})'),
    ("NoSummary", "The summary moves to Ticket Text (long free text; only the ticket list reads it)",
     'Table.RemoveColumns({prev}, {{"Summary"}})'),
    ("CleanText", "Title-case statuses (some teams type them in lower case)",
     'Table.TransformColumns({prev}, {{{{"Status", Text.Proper, type text}}}})'),
    ("KeyCopy", "Copy the key so it survives the split", 'Table.DuplicateColumn({prev}, "Ticket Key", "Key Parts")'),
    ("SplitKey", "HIVE-1234 -> project HIVE + issue number 1234 (sorts tickets numerically)",
     'Table.SplitColumn({prev}, "Key Parts", Splitter.SplitTextByDelimiter("-", QuoteStyle.None), '
     '{{"Project", "Issue Number"}})'),
    ("IssueNumber", "The issue number is a whole number", 'Table.TransformColumnTypes({prev}, {{{{"Issue Number", Int64.Type}}}})'),
    ("JiraUrl", "Link to the ticket on issues.apache.org",
     'Table.AddColumn({prev}, "Jira URL", each "https://issues.apache.org/jira/browse/" & [Ticket Key], type text)'),
    ("AgeBand", "Bucket the age of open tickets",
     'Table.AddColumn({prev}, "Age Band", each if [Age Days] = null then null '
     + " ".join(f'else if [Age Days] <= {n} then "{b}"' for n, b in AGE_BANDS if n) + f' else "{AGE_BANDS[-1][1]}", type text)'),
    ("AgeBandOrder", "Sort order for the bands",
     'Table.AddColumn({prev}, "Age Band Order", each if [Age Days] = null then null '
     + " ".join(f"else if [Age Days] <= {n} then {i}" for i, (n, _) in enumerate(AGE_BANDS, 1) if n)
     + f" else {len(AGE_BANDS)}, Int64.Type)"),
    ("PriorityTargets", "Read the priority targets (a second source, merged below)",
     'Table.RenameColumns(Table.SelectColumns(Parquet.Document(File.Contents(DataFolder & "dim_priority.parquet")), '
     '{{"priority_key", "resolution_days"}}), {{{{"priority_key", "Priority Key"}}, {{"resolution_days", "Target Days"}}}})'),
    ("WithTarget", "Merge (left outer join) on Priority Key, like a VLOOKUP",
     'Table.NestedJoin(AgeBandOrder, {{"Priority Key"}}, PriorityTargets, {{"Priority Key"}}, "Priority", JoinKind.LeftOuter)'),
    ("TargetDays", "Bring back just the resolution target",
     'Table.ExpandTableColumn(WithTarget, "Priority", {{"Target Days"}})'),
    ("PastTarget", "Open tickets: days past the resolution target (negative = time left)",
     'Table.AddColumn({prev}, "Days Past Target", each if [Age Days] = null then null else [Age Days] - [Target Days], '
     "type number)"),
    ("NoTargetDays", "The target itself lives in Priorities", 'Table.RemoveColumns({prev}, {{"Target Days"}})'),
    ("StagesPerTicket", "Group By: how many open stages each ticket passed through (Stage Time has one row per ticket and stage)",
     'Table.Group(Parquet.Document(File.Contents(DataFolder & "fact_stage_time.parquet")), {{"ticket_key"}}, '
     '{{{{"Stages Visited", each Table.RowCount(_), Int64.Type}}}})'),
    ("WithStages", "Merge the counts back onto the tickets",
     'Table.NestedJoin(NoTargetDays, {{"Ticket Key"}}, StagesPerTicket, {{"ticket_key"}}, "Stages", JoinKind.LeftOuter)'),
    ("Tickets", "Tickets that never sat in an open stage visited none",
     'Table.ReplaceValue(Table.ExpandTableColumn({prev}, "Stages", {{"Stages Visited"}}), null, 0, '
     'Replacer.ReplaceValue, {{"Stages Visited"}})'),
]
# The steps that read a second source don't chain on the previous step; {prev} is unused there.
TABLE_STEPS = {
    "Stage Time": [("NonZero", "Drop zero-length stage intervals (status changed and changed back in the same second)",
                    "Table.SelectRows({prev}, each [Days] > 0)")],
    "Backlog": [("UpToAsOf", "Nothing after the as-of date (parameter AsOfDate)",
                 "Table.SelectRows({prev}, each [Date] <= Date.From(AsOfDate))"),
                ("InRange", "Incremental refresh: the Service swaps RangeStart/RangeEnd per partition (>= start, < end)",
                 "Table.SelectRows({prev}, each DateTime.From([Date]) >= RangeStart and DateTime.From([Date]) < RangeEnd)")],
    "Team Access": [
        ("TestUser", "One extra login for testing the Team Lead role in the Service (parameter TestUpn)",
         '#table(type table [Email = text, #"Team Key" = text], {{{{TestUpn, "HBASE"}}}})'),
        ("Combined", "Append: the seed rows + the test login", "Table.Combine({{Typed, TestUser}})")],
}
STAGING = {"Tickets Source": "fact_ticket"}  # shared expressions: loaded by nothing, referenced by queries

CUSTOM_M = {
    "Tickets": ["let", "    // Reference (not duplicate) the shared staging query: one place reads the Parquet file",
                '    Source = #"Tickets Source"', "in", "    Source"],
    "Import Errors": ["let", "    // Same staging query as Tickets",
                      '    Source = #"Tickets Source"', "in", "    Source"],
    "Ticket Text": ["let", "    // Same staging query as Tickets: one read of the Parquet file",
                    '    Source = #"Tickets Source"', "in", "    Source"],
    "Quarter Goals": [
        "let", "    // An ops-team workbook (synthetic goals): a title row and a blank row sit above the header",
        '    Book = Excel.Workbook(File.Contents(SeedFolder & "quarter_goals.xlsx"), null, true),',
        "    // Navigate to the Goals sheet",
        '    Sheet = Book{[Item = "Goals", Kind = "Sheet"]}[Data]', "in", "    Sheet"],
}
CUSTOM_STEPS = {
    "Tickets": TICKET_STEPS,
    "Ticket Text": [
        ("HasKey", "Same row filters as Tickets, so the 1:1 relationship lines up",
         "Table.SelectRows({prev}, each [Ticket Key] <> null and [Created Date] <= Date.From(AsOfDate))"),
        ("Kept", "Just the key and the text", 'Table.SelectColumns({prev}, {{"Ticket Key", "Summary"}})'),
        ("Cleaned", "Trim and strip control characters",
         'Table.TransformColumns({prev}, {{{{"Summary", each Text.Clean(Text.Trim(_)), type text}}}})')],
    "Import Errors": [
        ("Errors", "Keep the rows where a duration failed its type (normally none)",
         'Table.SelectRowsWithErrors({prev}, {{"Lead Days", "Cycle Days", "Response Hours"}})'),
        ("Keys", "Just the key", 'Table.SelectColumns({prev}, {{"Ticket Key"}})'),
        ("Flagged", "What went wrong",
         'Table.AddColumn({prev}, "Issue", each "A lead, cycle or response value failed its type", type text)')],
    "Quarter Goals": [
        ("NoTitle", "Remove the title row and the blank row", "Table.Skip({prev}, 2)"),
        ("Headers", "The third row holds the column names", "Table.PromoteHeaders({prev}, [PromoteAllScalars = true])"),
        ("NoBlankRows", "Drop the blank rows between team blocks", 'Table.SelectRows({prev}, each [Work Type] <> null)'),
        ("TeamFilled", "The team is only written on the first row of its block: fill it down", 'Table.FillDown({prev}, {{"Team"}})'),
        ("Unpivoted", "One column per quarter -> one row per team, work type and quarter",
         'Table.UnpivotOtherColumns({prev}, {{"Team", "Work Type"}}, "Quarter", "Goal")'),
        ("QuarterKey", "Q3 2026 -> 20263, the Year Quarter key on Date",
         'Table.AddColumn({prev}, "Year Quarter", each Number.FromText(Text.End([Quarter], 4)) * 10 '
         "+ Number.FromText(Text.Middle([Quarter], 1, 1)), Int64.Type)"),
        ("PerTeamQuarter", "Group By: bug + other goals per team and quarter",
         'Table.Group({prev}, {{"Team", "Year Quarter"}}, {{{{"Delivery Goal", each List.Sum([Goal]), type number}}}})'),
        ("Indexed", "A row number, handy when checking against the sheet",
         'Table.AddIndexColumn({prev}, "Goal Row", 1, 1, Int64.Type)'),
        ("Renamed", "Model name for the team column", 'Table.RenameColumns({prev}, {{{{"Team", "Team Key"}}}})'),
        ("Typed", "Types last",
         'Table.TransformColumnTypes({prev}, {{{{"Team Key", type text}}, {{"Delivery Goal", Int64.Type}}}})')],
}


def table_m(name, mart, cols):
    if name in CUSTOM_M:
        return chain(CUSTOM_M[name], CUSTOM_STEPS[name])
    base = m_source(mart, cols)
    return chain(base, TABLE_STEPS[name]) if name in TABLE_STEPS else base


def staging_m(name):
    mart = STAGING[name]
    tname = next(t for t, (m, _) in TABLES.items() if m == mart)
    return m_source(mart, TABLES[tname][1])


# Incremental refresh: (rolling window unit, periods, incremental unit, periods). Backlog is a daily snapshot that
# only ever appends; Tickets stays a full refresh because the as-of replay can change an old ticket.
REFRESH = {"Backlog": ("month", 24, "day", 7)}


def table_tmdl(name, mart, cols):
    cols = model_cols(cols)
    lines = [f"table {q(name)}", f"{T}lineageTag: {tag(name)}"]
    if name == "Date":
        lines.append(f"{T}dataCategory: Time")
    if name in HIDDEN_TABLES:
        lines.append(f"{T}isHidden")
    lines.append("")
    if name == "Data Checks":
        for m in CHECK_MEASURES:
            lines += measure_lines(name, *m)
    for col, dtype, o in cols:
        lines += [f"{T}column {q(col)} =", *block(o["dax"], 3)] if "dax" in o else [f"{T}column {q(col)}"]
        lines += [f"{T*2}dataType: {dtype}"]
        lines += [f"{T*2}isKey"] * bool(o.get("key")) + [f"{T*2}isHidden"] * bool(o.get("hidden"))
        if fmt := o.get("fmt") or {"int64": "0", "dateTime": "dd mmm yyyy"}.get(dtype):
            lines.append(f"{T*2}formatString: {fmt}")
        if cat := o.get("category"):
            lines.append(f"{T*2}dataCategory: {cat}")
        lines += [f"{T*2}lineageTag: {tag(name, col)}", f"{T*2}summarizeBy: {'sum' if o.get('sum') else 'none'}"]
        lines += [] if "dax" in o else [f"{T*2}sourceColumn: {col}"]
        if sort := o.get("sort"):
            lines.append(f"{T*2}sortByColumn: {q(sort)}")
        lines.append("")
    for h, levels in HIERARCHIES.get(name, []):
        lines += [f"{T}hierarchy {q(h)}", f"{T*2}lineageTag: {tag(name, 'h', h)}", ""]
        for lv in levels:
            lines += [f"{T*2}level {q(lv)}", f"{T*3}lineageTag: {tag(name, 'h', h, lv)}", f"{T*3}column: {q(lv)}", ""]
    if name == "Date":
        for cal, groups in CALENDARS:
            lines += [f"{T}calendar {q(cal)}", ""]
            for unit, primary, assoc in groups:
                lines += [f"{T*2}calendarColumnGroup = {unit}", f"{T*3}primaryColumn: {q(primary)}"]
                lines += ([f"{T*3}associatedColumn: {q(assoc)}"] if assoc else []) + [""]
    m = table_m(name, mart, TABLES[name][1])
    if name in REFRESH:
        rg, rp, ig, ip = REFRESH[name]
        lines += [f"{T}refreshPolicy", f"{T*2}policyType: basic", f"{T*2}rollingWindowGranularity: {rg}",
                  f"{T*2}rollingWindowPeriods: {rp}", f"{T*2}incrementalGranularity: {ig}",
                  f"{T*2}incrementalPeriods: {ip}", f"{T*2}sourceExpression =", *[f"{T*4}{l}" for l in m], ""]
    lines += [f"{T}partition {q(name)} = m", f"{T*2}mode: import", f"{T*2}source ="]
    lines += [f"{T*4}{l}" for l in m]
    lines += ["", f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def calculated_tmdl(name, dax, cols):
    lines = [f"table {q(name)}", f"{T}lineageTag: {tag(name)}", ""]
    for col, src, dtype, extra in cols:
        lines += [f"{T}column {q(col)}", f"{T*2}dataType: {dtype}", f"{T*2}lineageTag: {tag(name, col)}",
                  f"{T*2}summarizeBy: none", f"{T*2}isNameInferred", f"{T*2}sourceColumn: {src}"]
        lines += [f"{T*2}{e}" for e in extra] + [""]
    return "\n".join(lines + [f"{T}partition {q(name)} = calculated", f"{T*2}mode: import", f"{T*2}source = {dax}", ""])


def calc_group_tmdl():
    lines = ["table 'Time Intelligence'", f"{T}lineageTag: {tag('Time Intelligence')}", "",
             f"{T}calculationGroup", f"{T*2}precedence: 10", ""]
    for i, (name, dax, fmt) in enumerate(TIME_ITEMS):
        lines += [f"{T*2}calculationItem {q(name)} =", *block(dax, 4)] if "\n" in dax \
            else [f"{T*2}calculationItem {q(name)} = {dax}"]
        lines.append(f"{T*3}ordinal: {i}")
        if fmt:
            lines += [f"{T*3}formatStringDefinition = {fmt}"]
        lines.append("")
    lines += [f"{T}column Period", f"{T*2}dataType: string", f"{T*2}lineageTag: {tag('Time Intelligence', 'Period')}",
              f"{T*2}summarizeBy: none", f"{T*2}sourceColumn: Name", f"{T*2}sortByColumn: Ordinal", "",
              f"{T}column Ordinal", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
              f"{T*2}lineageTag: {tag('Time Intelligence', 'Ordinal')}", f"{T*2}summarizeBy: sum",
              f"{T*2}sourceColumn: Ordinal", ""]
    return "\n".join(lines)


def field_parameter_tmdl(t, items):
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
        *block("{\n" + rows + "\n}", 4), ""])


def measures_tmdl():
    lines = ["table _Measures", f"{T}lineageTag: {tag('_Measures')}", ""]
    for m in MEASURES:
        lines += measure_lines("_Measures", *m)
    lines += [f"{T}column Value", f"{T*2}dataType: int64", f"{T*2}isHidden", f"{T*2}formatString: 0",
              f"{T*2}lineageTag: {tag('_Measures', 'Value')}", f"{T*2}summarizeBy: none", f"{T*2}isNameInferred",
              f"{T*2}sourceColumn: [Value]", "",
              f"{T}partition _Measures = calculated", f"{T*2}mode: import", f"{T*2}source = {{1}}", ""]
    return "\n".join(lines)


def functions_tmdl():
    lines = []
    for name, desc, body in FUNCTIONS:
        lines.append(f"/// {desc}")
        lines += [f"function {q(name)} =", *block(body, 2)] if "\n" in body else [f"function {q(name)} = {body}"]
        lines += [f"{T}lineageTag: {tag('fn', name)}", ""]
    return "\n".join(lines)


def role_tmdl(name, r):
    lines = [f"role {q(name)}", f"{T}modelPermission: read", ""]
    for table in sorted(set(r["filters"]) | set(r["hide_columns"])):
        dax = r["filters"].get(table)
        lines.append(f"{T}tablePermission {q(table)}" + (f" = {dax}" if dax else ""))
        lines += [""] + [f"{T*2}columnPermission {q(c)} = none" for c in r["hide_columns"].get(table, [])] + [""]
    for table in r["hide_tables"]:
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


def check():
    """Fail fast on name collisions and DAX references that don't resolve."""
    columns = {t: {c[0] for c in model_cols(cols)} for t, (_, cols) in TABLES.items()}
    columns |= {t: {c[0] for c in cols} for t, (_, cols) in CALCULATED.items()}
    columns |= {t: {t} for t in FIELD_PARAMETERS} | {"Time Intelligence": {"Period"}}
    measures = [m[1] for m in MEASURES + CHECK_MEASURES]
    taken = {n.lower() for n in set().union(*columns.values())} | {t.lower() for t in columns}
    clash = {m for m in measures if m.lower() in taken or measures.count(m) > 1}
    assert not clash, f"measure names collide with tables/columns: {clash}"
    tables = set(columns) | {c for c, _ in CALENDARS}
    fns = {f[0] for f in FUNCTIONS}
    dax_all = [(m[1], m[4]) for m in MEASURES + CHECK_MEASURES] \
        + [(f"role {r}", d) for r, v in ROLES.items() for d in v["filters"].values()] \
        + [(f"item {n}", d) for n, d, _ in TIME_ITEMS] + [(f"function {n}", b) for n, _, b in FUNCTIONS] \
        + [(p, r) for p, items in FIELD_PARAMETERS.items() for _, r in items] \
        + [(f"{t}[{c}]", o["dax"]) for t, (_, cols) in TABLES.items() for c, _, o in cols if "dax" in o]
    for name, dax in dax_all:
        dax = re.sub(r'"[^"]*"', '""', dax)
        for a, b, col in re.findall(r"(?:'([^']+)'|\b([A-Za-z_][\w ]*?))\[([^\]]+)\]", dax):
            assert col in columns.get(a or b, ()), f"[{name}] references missing column {a or b}[{col}]"
        for ref in re.findall(r"(?<![\w'\]])\[([^\]]+)\]", dax):
            assert ref in measures or ref.startswith("@") or ref in ("Value", "Slope1"), \
                f"[{name}] references missing measure [{ref}]"
        for ref in re.findall(r"'([^'\n]+)'", re.sub(r"'[^'\n]+'\[[^\]]*\]", "", dax)):
            assert ref in tables, f"[{name}] references missing table/calendar '{ref}'"
        for fn in re.findall(r"\b([A-Z][A-Za-z]*\.[A-Za-z]+)\s*\(", dax):
            assert fn in fns or fn in ("PERCENTILEX.INC",), f"[{name}] calls unknown function {fn}"
        assert dax.count("(") == dax.count(")"), f"[{name}] unbalanced parentheses"
    for ft, fc, tt, tc, *_ in RELATIONSHIPS:
        assert fc in columns[ft] and tc in columns[tt], (ft, fc, tt, tc)
        assert ft not in REFRESH or ft in TABLE_STEPS and any(s[0] == "InRange" for s in TABLE_STEPS[ft]), ft
    for _, groups in CALENDARS:
        for _, p, a in groups:
            assert p in columns["Date"] and (a is None or a in columns["Date"]), (p, a)
    for t, hs in HIERARCHIES.items():
        assert all(lv in columns[t] for _, lvs in hs for lv in lvs), t
    for r in ROLES.values():
        assert all(t in columns for t in [*r["filters"], *r["hide_tables"], *r["hide_columns"]])
    # best-practice rules (a subset of Tabular Editor's BPA that applies to a generated model)
    for folder, name, fmt, desc, _ in MEASURES + CHECK_MEASURES:
        assert desc and desc.endswith("."), f"[{name}] needs a one-sentence description"
        assert fmt or name in TEXT_MEASURES, f"[{name}] numeric measure without a format string"
    opts = {t: {c: o for c, _, o in model_cols(cols)} for t, (_, cols) in TABLES.items()}
    types = {t: {c: d for c, d, _ in model_cols(cols)} for t, (_, cols) in TABLES.items()}
    for ft, fc, tt, tc, *o in RELATIONSHIPS:
        assert opts[tt][tc].get("key") or (o and o[0].get("m2m")), f"relationship {ft}[{fc}] -> {tt}[{tc}] must point at a key column"
        assert opts[ft][fc].get("hidden") or ft in HIDDEN_TABLES or fc.endswith("Date"), f"foreign key {ft}[{fc}] should be hidden"
    for t, o in opts.items():
        for c, x in o.items():
            assert x.get("sort") in (None, *o), f"{t}[{c}] sorts by a missing column"
            # 7-s9: helper columns stay out of the field list, numbers carry a format
            if x.get("sort"):
                assert o[x["sort"]].get("hidden") or x["sort"] in (c, "Priority Key", "Stage Key", "Type Key"), \
                    f"{t}[{x['sort']}] is a sort-by helper and should be hidden"
            if x.get("key") and types[t][c] == "int64":
                assert x.get("hidden"), f"surrogate key {t}[{c}] should be hidden"
            if types[t][c] == "double":
                assert x.get("fmt"), f"{t}[{c}] is a decimal column without a format string"
    print(f"  ✓ {len(measures)} measures, {len(FUNCTIONS)} functions: every reference resolves; best-practice rules pass")


PARAMETERS = [  # (name, M value, type): Transform data > Edit parameters
    ("DataFolder", None, "Text"),
    ("SeedFolder", None, "Text"),
    ("AsOfDate", "#datetime(2026, 9, 30, 0, 0, 0)", "DateTime"),
    ("TestUpn", '"test.lead@example.org"', "Text"),
    ("LakehouseSqlEndpoint", '"<workspace>.datawarehouse.fabric.microsoft.com"', "Text"),
    # reserved names for incremental refresh; Desktop loads everything between them (the full two years)
    ("RangeStart", "#datetime(2024, 10, 1, 0, 0, 0)", "DateTime"),
    ("RangeEnd", "#datetime(2026, 10, 1, 0, 0, 0)", "DateTime"),
]
LAKEHOUSE_VARIANT = {"Tickets Source": "Tickets", "Backlog": "Backlog"}  # query -> table whose columns it reads


def expressions_tmdl(data_dir):
    values = {"DataFolder": f'"{data_dir}"', "SeedFolder": f'"{ROOT / "seeds"}/"'}
    lines = []
    for name, value, kind in PARAMETERS:
        lines += [f"expression {name} = {value or values[name]} meta [IsParameterQuery=true, Type=\"{kind}\", "
                  "IsParameterQueryRequired=true]", f"{T}lineageTag: {tag('expr', name)}", "",
                  f"{T}annotation PBI_ResultType = {kind}", ""]
    for name in STAGING:  # enable load off: a shared expression, not a table
        lines += [f"expression {q(name)} =", *[f"{T*2}{l}" for l in staging_m(name)],
                  f"{T}lineageTag: {tag('expr', name)}", "", f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data" / "marts"))
    ap.add_argument("--service", action="store_true", help="write powerbi-service/: files read from OneLake")
    args = ap.parse_args()
    data_dir = args.data_dir.rstrip("/\\") + "/"
    global OUT
    if args.service:
        OUT = ROOT / "powerbi-service" / OUT.name

    check()
    shutil.rmtree(OUT / "definition", ignore_errors=True)
    d = OUT / "definition"
    for sub in ("tables", "roles", "perspectives", "cultures"):
        (d / sub).mkdir(parents=True)

    (OUT / "definition.pbism").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",\n'
        '  "version": "4.2",\n  "settings": {}\n}\n')
    (OUT / ".platform").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",\n'
        '  "metadata": {"type": "SemanticModel", "displayName": "TeamKPI"},\n'
        '  "config": {"version": "2.0", "logicalId": "b2d84f1e-6a3c-4e97-9f05-3c71e8a2d640"}\n}\n')
    # 1702: user-defined functions and calendars need the current compatibility level.
    (d / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1702\n")
    all_tables = [*TABLES, *CALCULATED, "Time Intelligence", *FIELD_PARAMETERS, "_Measures"]
    (d / "model.tmdl").write_text("\n".join([
        "model Model", f"{T}culture: en-US", f"{T}defaultPowerBIDataSourceVersion: powerBI_V3",
        f"{T}discourageImplicitMeasures", f"{T}sourceQueryCulture: en-US", f"{T}dataAccessOptions",
        f"{T*2}legacyRedirects", f"{T*2}returnErrorValuesAsNull", "",
        "annotation __PBI_TimeIntelligenceEnabled = 0", "",
        *[f"ref table {q(t)}" for t in all_tables], "",
        *[f"ref role {q(r)}" for r in ROLES], "",
        *[f"ref perspective {q(p)}" for p in PERSPECTIVES], "",
        "ref cultureInfo en-US", ""]))
    (d / "expressions.tmdl").write_text(expressions_tmdl(data_dir))
    (d / "functions.tmdl").write_text(functions_tmdl())

    pq = ROOT / "powerquery"  # the same M as the partitions, one file per query, for review and the parser check
    shutil.rmtree(pq, ignore_errors=True)
    pq.mkdir()
    for name, (mart, cols) in TABLES.items():
        (d / "tables" / f"{name}.tmdl").write_text(table_tmdl(name, mart, cols))
        (pq / f"{name}.pq").write_text("\n".join(table_m(name, mart, cols)) + "\n")
    for name in STAGING:
        (pq / f"{name}.pq").write_text("\n".join(staging_m(name)) + "\n")
    for name, table in LAKEHOUSE_VARIANT.items():  # Fabric source, same steps: paste into the Advanced Editor
        mart, cols = TABLES[table]
        (pq / f"Lakehouse {name}.pq").write_text("\n".join(m_source(mart, cols, lakehouse=True)) + "\n")
    for name, (dax, cols) in CALCULATED.items():
        (d / "tables" / f"{name}.tmdl").write_text(calculated_tmdl(name, dax, cols))
    (d / "tables" / "Time Intelligence.tmdl").write_text(calc_group_tmdl())
    for t, items in FIELD_PARAMETERS.items():
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
        rel += [f"relationship {tag('rel', ft, fc)}"] + [f"{T}isActive: false"] * (o.get("active") is False)
        rel += [f"{T}crossFilteringBehavior: bothDirections"] * bool(o.get("both"))
        rel += [f"{T}fromCardinality: one"] * bool(o.get("one"))
        rel += [f"{T}fromCardinality: many", f"{T}toCardinality: many"] * bool(o.get("m2m"))
        rel += [f"{T}fromColumn: {q(ft)}.{q(fc)}", f"{T}toColumn: {q(tt)}.{q(tc)}", ""]
    (d / "relationships.tmdl").write_text("\n".join(rel))
    # DAX query view tabs: one .dax per tab, tab order in .pbi/daxQueries.json (Power BI projects layout).
    from dax_queries import QUERIES, ASSERTS
    dq = OUT / "DAXQueries"
    shutil.rmtree(dq, ignore_errors=True)
    (dq / ".pbi").mkdir(parents=True)
    for tab, text in QUERIES.items():
        (dq / f"{tab}.dax").write_text(text)
    for tab, spec in ASSERTS.items():
        (dq / f"{tab}.assert.json").write_text(json.dumps(spec, indent=2) + "\n")
    (dq / ".pbi" / "daxQueries.json").write_text(json.dumps(
        {"version": "1.0.0", "tabOrder": list(QUERIES), "defaultTab": next(iter(QUERIES))}, indent=2) + "\n")
    print(f"  ✓ wrote {dq.relative_to(ROOT)} ({len(QUERIES)} saved DAX queries, {len(ASSERTS)} with asserts)")
    print(f"  ✓ wrote {OUT.relative_to(ROOT)} ({len(all_tables)} tables, {len(RELATIONSHIPS)} relationships, "
          f"{len(FUNCTIONS)} functions, {len(CALENDARS)} calendars, {len(ROLES)} roles, {len(PERSPECTIVES)} perspectives)")
    if args.service:  # same model, files from the lakehouse (powerquery/ above stays the local M)
        from service_build import to_service
        to_service(OUT, ROOT, {"DataFolder": "data/marts", "SeedFolder": "seeds"}, "team-kpi", tag)


if __name__ == "__main__":
    main()
