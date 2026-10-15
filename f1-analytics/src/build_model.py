"""Generate the Power BI semantic model (TMDL) from the DuckDB mart schema.

    uv run python src/build_model.py [--data-dir PATH]

Column types come straight from DuckDB, so the model never drifts from the SQL.
--data-dir is where Power BI reads the Parquet marts (default: this repo's data/marts).
On Windows, change it in Power BI: Transform data > Edit parameters > DataFolder.
"""
import argparse
import base64
import json
import re
import shutil
import uuid
import zlib
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "powerbi" / "F1.SemanticModel"
T = "\t"
NS = uuid.UUID("5f1a7c3e-2b9d-4e61-8a0f-1950c0de2024")


def tag(*parts):
    """Deterministic lineage tags, so a rebuild only diffs where the model really changed."""
    return str(uuid.uuid5(NS, "/".join(parts)))

TYPES = {  # duckdb -> (tmdl dataType, Power Query type)
    "BIGINT": ("int64", "Int64.Type"), "INTEGER": ("int64", "Int64.Type"),
    "DOUBLE": ("double", "type number"), "DATE": ("dateTime", "type date"),
    "BOOLEAN": ("boolean", "type logical"), "VARCHAR": ("string", "type text"),
}

PCT1, PCT0, NUM0, NUM1, NUM2, SEC = "#,0.0%;-#,0.0%;#,0.0%", "0%", "#,0", "#,0.0", "0.00", "0.00\" s\""

# table name -> (mart table, config)
TABLES = {
    "Season": ("dim_season", dict(
        key="year", hidden=["champion_driver_id"],
        names={"races": "Races in Season", "different_winners": "Different Winners", "champion_wins": "Champion Wins"},
        fmt={"champion_win_rate": PCT0, "title_margin_pct": PCT0, "top_team_points_share": PCT0})),
    "Race": ("dim_race", dict(
        key="race_id", hidden=["race_id", "circuit_id", "race_label_order"],
        sort={"race_label": "race_label_order"}, names={"year": "Race Year", "round": "Round", "era": "Race Era"},
        fmt={"race_date": "dd mmm yyyy"})),
    "Driver": ("dim_driver", dict(
        key="driver_id", hidden=["driver_id", "achievement_tier_order"],
        sort={"achievement_tier": "achievement_tier_order"},
        category={"country": "Country", "wikipedia_url": "WebUrl"},
        names={"wikipedia_url": "Wikipedia URL", "starts": "Career Starts", "wins": "Career Wins", "podiums": "Career Podiums", "poles": "Career Poles",
               "fastest_laps": "Career Fastest Laps", "titles": "Driver Titles", "win_rate": "Career Win Rate",
               "podium_rate": "Career Podium Rate", "dnfs": "Career DNFs", "country": "Driver Country",
               "nationality": "Driver Nationality", "is_active": "Active Driver", "first_season": "Driver First Season",
               "last_season": "Driver Last Season", "world_region": "Driver World Region"},
        fmt={"win_rate": PCT1, "podium_rate": PCT1, "dob": "dd mmm yyyy"},
        # cohort key and a grouping of drivers by career win rate (same limits as the 'Win Bands' table)
        calc=[("Debut Decade", "string", "FORMAT ( QUOTIENT ( Driver[Driver First Season], 10 ) * 10, \"0\" ) & \"s\"", None),
              ("Win Band Order", "int64", "VAR _r = Driver[Career Win Rate] RETURN SWITCH ( TRUE (), _r = 0, 1, _r < 0.05, 2, _r < 0.2, 3, 4 )",
               "0", ["isHidden"]),
              ("Win Band", "string", "SWITCH ( Driver[Win Band Order], 1, \"No wins\", 2, \"Under 5%\", 3, \"5–19%\", \"20%+\" )", None,
               ["sortByColumn: 'Win Band Order'"])])),
    "Constructor": ("dim_constructor", dict(
        key="constructor_id", hidden=["constructor_id"],
        category={"country": "Country"},
        names={"wins": "Team Wins", "podiums": "Team Podiums", "poles": "Team Poles", "win_rate": "Team Win Rate",
               "country": "Team Country", "nationality": "Team Nationality", "is_active": "Active Team",
               "first_season": "Team First Season", "last_season": "Team Last Season", "driver_titles": "Team Driver Titles",
               "mechanical_dnf_rate": "Team Mechanical DNF Rate"},
        fmt={"win_rate": PCT1, "mechanical_dnf_rate": PCT1})),
    "Circuit": ("dim_circuit", dict(
        key="circuit_id", hidden=["circuit_id", "lap_record_ms"],
        category={"lat": "Latitude", "lng": "Longitude", "country": "Country", "circuit_geo": "Place", "location": "City"},
        # calculated column: a per-row fact the slicer/axis needs, evaluated once at refresh (RELATEDTABLE walks 1→*)
        calc=[("Races in Data", "int64", "COUNTROWS ( RELATEDTABLE ( Race ) )", "0")],
        names={"country": "Circuit Country", "lat": "Latitude", "lng": "Longitude", "alt": "Altitude m"},
        fmt={"pole_win_rate": PCT0, "avg_position_change": NUM1})),
    "Results": ("fact_results", dict(
        hidden=["result_id", "race_id", "driver_id", "constructor_id", "circuit_id", "grid_band_order"],
        sort={"grid_band": "grid_band_order"},
        names={"year": "Result Year", "round": "Result Round", "points": "Points Scored", "points_modern": "Modern Points",
               "grid": "Grid Position", "laps": "Laps Completed", "positions_gained": "Positions Gained Value",
               "finish_percentile": "Finish Percentile Value", "fastest_lap_ms": "Fastest Lap Ms", "fastest_lap_kph": "Fastest Lap Kph"},
        fmt={"finish_percentile": PCT0, "fastest_lap_kph": NUM1},
        # calculated column, not a measure: it is a property of one result row (slicer + filter), cheap (boolean),
        # and RELATED needs a row context. Chain Results → Race → Circuit and Results → Driver.
        calc=[("Is Home Race", "boolean", "RELATED ( Driver[Driver Country] ) = RELATED ( Circuit[Circuit Country] )", None)])),
    "Qualifying": ("fact_qualifying", dict(
        hidden=["race_id", "driver_id", "constructor_id"], names={"year": "Quali Year"}, fmt={"gap_to_pole_pct": "0.00%"})),
    "Pit Stops": ("fact_pit_stops", dict(
        hidden=["race_id", "driver_id", "constructor_id"], names={"year": "Pit Year", "lap": "Pit Lap", "stop_no": "Stop No"},
        fmt={"pit_seconds": SEC})),
    "Lap Times": ("fact_lap_times", dict(
        hidden=["race_id", "driver_id", "race_date"], names={"position": "Lap Position", "lap": "Lap", "race_date": "Lap Race Date"},
        fmt={"lap_seconds": "0.000"})),
    "Standings": ("fact_standings", dict(
        hidden=["race_id", "driver_id"],
        names={"year": "Standings Year", "round": "Standings Round", "points": "Standing Points", "position": "Standing Position",
               "wins": "Standing Wins", "gap_to_leader": "Standing Gap"})),
    "Grid Conversion": ("grid_conversion", dict(
        names={"era": "Grid Era", "grid": "Starting Slot", "starts": "Slot Starts", "wins": "Slot Wins", "podiums": "Slot Podiums", "avg_positions_gained": "Slot Avg Positions Gained"},
        fmt={"win_probability": PCT1, "podium_probability": PCT1, "avg_positions_gained": NUM1})),
    "Team Season": ("team_season", dict(
        hidden=["constructor_id"],
        names={"year": "Team Year", "points_modern": "Team Modern Points", "wins": "Team Season Wins", "poles": "Team Season Poles"},
        fmt={"season_points_share": PCT1},
        # transition matrix: the team's rank band this season and the band it lands in the next (blank in the last data season)
        calc=[("Rank Band Order", "int64", "SWITCH ( TRUE (), 'Team Season'[Season Rank] <= 3, 1, 'Team Season'[Season Rank] <= 7, 2, 3 )",
               "0", ["isHidden"]),
              ("Rank Band", "string", "SWITCH ( 'Team Season'[Rank Band Order], 1, \"Top 3\", 2, \"Midfield 4–7\", \"Back 8+\" )", None,
               ["sortByColumn: 'Rank Band Order'"]),
              ("Next Band Order", "int64",
               "VAR _y = 'Team Season'[Team Year] + 1 "
               "VAR _r = LOOKUPVALUE ( 'Team Season'[Rank Band Order], 'Team Season'[Team Year], _y, 'Team Season'[Constructor Id], 'Team Season'[Constructor Id] ) "
               "RETURN IF ( _y > MAX ( 'Team Season'[Team Year] ), BLANK (), COALESCE ( _r, 4 ) )", "0", ["isHidden"]),
              ("Next Band", "string",
               "SWITCH ( 'Team Season'[Next Band Order], 1, \"Top 3\", 2, \"Midfield 4–7\", 3, \"Back 8+\", 4, \"Left F1\" )", None,
               ["sortByColumn: 'Next Band Order'"])])),
    "Teammate H2H": ("teammate_h2h", dict(
        hidden=["driver_id", "teammate_id", "constructor_id"], names={"year": "H2H Year"},
        fmt={"quali_h2h_rate": PCT0, "race_h2h_rate": PCT0})),
    "Driver Skill": ("driver_skill", dict(
        hidden=["driver_id"], names={"skill": "Skill Value", "skill_rank": "Skill Rank Value"},
        fmt={"skill": "+0.000;-0.000;0.000", "skill_se": "0.000"})),
}

RELATIONSHIPS = [  # (many table, column, one table, column) -- snowflake: Season <- Race <- facts
    ("Race", "circuit_id", "Circuit", "circuit_id"),
    # Season is reached through the date table: Race[Race Date] → Calendar[Date] → Calendar[Year] → Season[Year],
    # so season slicers and the time-intelligence measures filter the same path.
    ("Race", "race_date", "Calendar", "Date"),
    ("Calendar", "Year", "Season", "year"),
    ("Results", "race_id", "Race", "race_id"),
    ("Results", "driver_id", "Driver", "driver_id"),
    ("Results", "constructor_id", "Constructor", "constructor_id"),
    ("Qualifying", "race_id", "Race", "race_id"),
    ("Qualifying", "driver_id", "Driver", "driver_id"),
    ("Qualifying", "constructor_id", "Constructor", "constructor_id"),
    ("Pit Stops", "race_id", "Race", "race_id"),
    ("Pit Stops", "driver_id", "Driver", "driver_id"),
    ("Pit Stops", "constructor_id", "Constructor", "constructor_id"),
    ("Lap Times", "race_id", "Race", "race_id"),
    ("Lap Times", "driver_id", "Driver", "driver_id"),
    ("Standings", "race_id", "Race", "race_id"),
    ("Standings", "driver_id", "Driver", "driver_id"),
    ("Team Season", "year", "Season", "year"),
    ("Team Season", "constructor_id", "Constructor", "constructor_id"),
    ("Teammate H2H", "year", "Season", "year"),
    ("Teammate H2H", "driver_id", "Driver", "driver_id"),
    ("Teammate H2H", "constructor_id", "Constructor", "constructor_id"),
    # role-playing Driver: the team-mate side, switched on only inside 'Teammate Points Delta' (USERELATIONSHIP)
    ("Teammate H2H", "teammate_id", "Driver", "driver_id", {"active": False}),
    # many-to-many on era (7 values, both sides repeat it): a limited relationship, Race filters Grid Conversion
    ("Grid Conversion", "era", "Race", "era", {"m2m": True}),
    ("Driver Skill", "driver_id", "Driver", "driver_id"),
    # tables shaped in Power Query (below): model column names
    ("Sprint Results", "Race Id", "Race", "race_id"),
    ("Sprint Results", "Driver Id", "Driver", "driver_id"),
    ("Sprint Results", "Constructor Id", "Constructor", "constructor_id"),
    ("Session Results", "Race Id", "Race", "race_id"),
    ("Session Results", "Driver Id", "Driver", "driver_id"),
    ("Session Results", "Constructor Id", "Constructor", "constructor_id"),
    ("Race Pace", "Race Id", "Race", "race_id"),
    ("Race Pace", "Driver Id", "Driver", "driver_id"),
]

# ------------------------------------------------------------------ Power Query (M)
# Most tables read a Parquet mart the pipeline already shaped in SQL. The four below are shaped in Power Query
# itself, so the M is the logic: sprint results come straight from the raw Ergast CSV, the points systems are
# typed in (Enter Data). Every step has a name that reads like a recipe in the Applied Steps pane.
# Folding: neither CSV nor Parquet folds, so column selection and row filters sit as early as possible.


def m_list(names):
    return "{" + ", ".join(f'"{n}"' for n in names) + "}"


def m_pairs(pairs):
    return "{" + ", ".join(f'{{"{a}", {b}}}' for a, b in pairs) + "}"


def m_query(steps):
    """steps: (name, comment, expression) -> let … in lines."""
    lines = ["let"]
    for k, (name, comment, expr) in enumerate(steps):
        if comment:
            lines.append(f"    // {comment}")
        ident = name if re.fullmatch(r"[A-Za-z_]\w*", name) else f'#"{name}"'
        lines.append(f"    {ident} = {expr}" + ("," if k < len(steps) - 1 else ""))
    return lines + ["in", f"    {steps[-1][0]}"]


def enter_data(header, rows):
    """The exact M that Power BI's Enter Data writes: rows as deflated, base64'd JSON, every column text."""
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    blob = base64.b64encode(co.compress(json.dumps(rows, separators=(",", ":")).encode()) + co.flush()).decode()
    cols = ", ".join(f'{h if re.fullmatch(r"[A-Za-z_]\w*", h) else "#" + json.dumps(h)} = _t' for h in header)
    return (f'Table.FromRows(Json.Document(Binary.Decompress(Binary.FromText("{blob}", BinaryEncoding.Base64), '
            f'Compression.Deflate)), let _t = ((type nullable text) meta [Serialized.Text = true]) in type table [{cols}])')


CSV = '[Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]'
SPRINT_KEEP = ["raceId", "driverId", "constructorId", "grid", "positionOrder", "points", "milliseconds",
               "fastestLapTime", "statusId"]
SPRINT_NUMBERS = ["raceId", "driverId", "constructorId", "grid", "positionOrder", "points", "milliseconds", "statusId"]

# Shared expressions with load disabled: queried by other queries, never a table of their own.
STAGING = {
    "Sprint Raw": m_query([
        ("Source", "The raw Ergast file: sprint results are not in the Parquet marts, Power Query shapes them",
         f'Csv.Document(File.Contents(RawFolder & "sprint_results.csv"), {CSV})'),
        ("Promoted", "First row holds the column names", "Table.PromoteHeaders(Source, [PromoteAllScalars = true])"),
        ("NullMarkers", "Ergast writes a missing value as \\N: make it a real null in every column",
         'Table.ReplaceValue(Promoted, "\\N", null, Replacer.ReplaceValue, Table.ColumnNames(Promoted))'),
    ]),
    "Status Lookup": m_query([
        ("Source", "statusId -> text (\"Finished\", \"+1 Lap\", \"Engine\", …)",
         f'Csv.Document(File.Contents(RawFolder & "status.csv"), {CSV})'),
        ("Promoted", None, "Table.PromoteHeaders(Source, [PromoteAllScalars = true])"),
        ("Typed", None, 'Table.TransformColumnTypes(Promoted, {{"statusId", Int64.Type}, {"status", type text}})'),
    ]),
}

POINTS_SYSTEMS = [  # Era is typed only on the first row of each family; Fill Down completes it
    ["Five scorers", "1950–1959", "1950", "8", "6", "4", "3", "2", "", "", "", "", ""],
    ["Six scorers", "1960", "1960", "8", "6", "4", "3", "2", "1", "", "", "", ""],
    ["", "1961–1990", "1961", "9", "6", "4", "3", "2", "1", "", "", "", ""],
    ["", "1991–2002", "1991", "10", "6", "4", "3", "2", "1", "", "", "", ""],
    ["Eight scorers", "2003–2009", "2003", "10", "8", "6", "5", "4", "3", "2", "1", "", ""],
    ["Ten scorers", "2010–", "2010", "25", "18", "15", "12", "10", "8", "6", "4", "2", "1"],
]
SLOTS = [f"P{i}" for i in range(1, 11)]

# name -> (M lines, [(column, tmdl type, options)])
PQ_TABLES = {
    "Sprint Results": (m_query([
        ("Source", "Staging query (load disabled): the raw CSV with \\N already turned into null", '#"Sprint Raw"'),
        ("Kept", "Only what the model uses, first: the CSV doesn't fold, so drop columns before any work",
         f"Table.SelectColumns(Source, {m_list(SPRINT_KEEP)})"),
        ("Trimmed", "Stray spaces around the lap time would break the split below",
         'Table.TransformColumns(Kept, {{"fastestLapTime", Text.Trim, type text}})'),
        ("Numbers", "Text to number; a value that won't parse becomes null instead of an error",
         "Table.TransformColumns(Trimmed, {" + ", ".join(
             f'{{"{c}", each try Number.From(_, "en-US") otherwise null, type nullable number}}' for c in SPRINT_NUMBERS) + "})"),
        ("Cleaned", "Belt and braces: any error left in a numeric column is replaced by null",
         "Table.ReplaceErrorValues(Numbers, {" + ", ".join(f'{{"{c}", null}}' for c in SPRINT_NUMBERS) + "})"),
        ("Scored", "A sprint row without points is a data error, not a zero", "Table.SelectRows(Cleaned, each [points] <> null and [points] >= 0)"),
        ("Split", "\"1:30.013\" -> minutes and seconds",
         'Table.SplitColumn(Scored, "fastestLapTime", Splitter.SplitTextByDelimiter(":", QuoteStyle.None), {"Lap Min", "Lap Sec"})'),
        ("LapSeconds", "Back to one number: 1 × 60 + 30.013 = 90.013 s",
         'Table.AddColumn(Split, "Sprint Fastest Lap Sec", each try Number.From([Lap Min]) * 60 + Number.From([Lap Sec], "en-US") otherwise null, type nullable number)'),
        ("SprintSeconds", "Derived: race time in seconds (finishers on the lead lap only; null otherwise)",
         'Table.AddColumn(LapSeconds, "Sprint Seconds", each if [milliseconds] = null then null else [milliseconds] / 1000, type nullable number)'),
        ("Merged", "Look up the status text in status.csv (a second staging query)",
         'Table.NestedJoin(SprintSeconds, {"statusId"}, #"Status Lookup", {"statusId"}, "StatusRow", JoinKind.LeftOuter)'),
        ("Expanded", None, 'Table.ExpandTableColumn(Merged, "StatusRow", {"status"}, {"Sprint Status"})'),
        ("Dropped", "Helpers no longer needed", 'Table.RemoveColumns(Expanded, {"Lap Min", "Lap Sec", "milliseconds", "statusId"})'),
        ("Renamed", "Title Case business names, matching the rest of the model", "Table.RenameColumns(Dropped, " + m_pairs([
            ("raceId", '"Race Id"'), ("driverId", '"Driver Id"'), ("constructorId", '"Constructor Id"'), ("grid", '"Sprint Grid"'),
            ("positionOrder", '"Sprint Position"'), ("points", '"Sprint Points"')]) + ")"),
        ("Typed", "Types last, once the shape is final", "Table.TransformColumnTypes(Renamed, " + m_pairs([
            ("Race Id", "Int64.Type"), ("Driver Id", "Int64.Type"), ("Constructor Id", "Int64.Type"), ("Sprint Grid", "Int64.Type"),
            ("Sprint Position", "Int64.Type"), ("Sprint Points", "type number"), ("Sprint Fastest Lap Sec", "type number"),
            ("Sprint Seconds", "type number"), ("Sprint Status", "type text")]) + ")"),
        ("Sorted", None, 'Table.Sort(Typed, {{"Race Id", Order.Ascending}, {"Sprint Position", Order.Ascending}})'),
    ]), [("Race Id", "int64", {"hidden": True}), ("Driver Id", "int64", {"hidden": True}),
         ("Constructor Id", "int64", {"hidden": True}), ("Sprint Grid", "int64", {}), ("Sprint Position", "int64", {}),
         ("Sprint Points", "double", {"fmt": "0.#"}), ("Sprint Seconds", "double", {"fmt": "#,0.000"}),
         ("Sprint Fastest Lap Sec", "double", {"fmt": "0.000"}), ("Sprint Status", "string", {})]),

    "Session Results": (m_query([
        ("RaceSource", "Grand Prix results: the same mart as the Results table",
         'Parquet.Document(File.Contents(DataFolder & "fact_results.parquet"))'),
        ("RaceKept", None, 'Table.SelectColumns(RaceSource, {"race_id", "driver_id", "constructor_id", "position_order", "points", "status"})'),
        ("RaceRenamed", None, "Table.RenameColumns(RaceKept, " + m_pairs([
            ("race_id", '"Race Id"'), ("driver_id", '"Driver Id"'), ("constructor_id", '"Constructor Id"'),
            ("position_order", '"Session Position"'), ("points", '"Session Points"'), ("status", '"Session Status"')]) + ")"),
        ("RaceRows", None, 'Table.AddColumn(RaceRenamed, "Session", each "Race", type text)'),
        ("SprintRows", "Sprint results (from 2021), renamed to the same columns",
         'Table.AddColumn(Table.RenameColumns(Table.SelectColumns(#"Sprint Results", {"Race Id", "Driver Id", "Constructor Id", '
         '"Sprint Position", "Sprint Points", "Sprint Status"}), {{"Sprint Position", "Session Position"}, '
         '{"Sprint Points", "Session Points"}, {"Sprint Status", "Session Status"}}), "Session", each "Sprint", type text)'),
        ("Combined", "Append: one row per driver per session, so sprint points count in the championship",
         "Table.Combine({RaceRows, SprintRows})"),
        ("Typed", None, "Table.TransformColumnTypes(Combined, " + m_pairs([
            ("Race Id", "Int64.Type"), ("Driver Id", "Int64.Type"), ("Constructor Id", "Int64.Type"),
            ("Session Position", "Int64.Type"), ("Session Points", "type number"), ("Session Status", "type text"),
            ("Session", "type text")]) + ")"),
    ]), [("Race Id", "int64", {"hidden": True}), ("Driver Id", "int64", {"hidden": True}),
         ("Constructor Id", "int64", {"hidden": True}), ("Session", "string", {}), ("Session Position", "int64", {}),
         ("Session Points", "double", {"fmt": "0.#"}), ("Session Status", "string", {})]),

    "Race Pace": (m_query([
        ("Source", "Lap-by-lap timing (1996+), the same mart as Lap Times",
         'Parquet.Document(File.Contents(DataFolder & "fact_lap_times.parquet"))'),
        ("RacingLaps", "In- and out-laps distort pace: keep racing laps only", "Table.SelectRows(Source, each [is_pit_lap] = false)"),
        ("Grouped", "One row per driver per race",
         'Table.Group(RacingLaps, {"race_id", "driver_id"}, {{"Median Lap Sec", each List.Median([lap_seconds]), type number}, '
         '{"Laps Timed", each Table.RowCount(_), Int64.Type}})'),
        ("Renamed", None, 'Table.RenameColumns(Grouped, {{"race_id", "Race Id"}, {"driver_id", "Driver Id"}})'),
        ("Typed", None, 'Table.TransformColumnTypes(Renamed, {{"Race Id", Int64.Type}, {"Driver Id", Int64.Type}})'),
    ]), [("Race Id", "int64", {"hidden": True}), ("Driver Id", "int64", {"hidden": True}),
         ("Median Lap Sec", "double", {"fmt": "0.000"}), ("Laps Timed", "int64", {})]),

    "Points Systems": (m_query([
        ("Source", "Enter Data: one row per scoring system, P1..P10 across",
         enter_data(["Era", "System", "First Season", *SLOTS], POINTS_SYSTEMS)),
        ("Blanks", "Enter Data stores an empty cell as \"\": make it null so Fill Down and Unpivot skip it",
         'Table.ReplaceValue(Source, "", null, Replacer.ReplaceValue, Table.ColumnNames(Source))'),
        ("Filled", "Era is typed once per family", 'Table.FillDown(Blanks, {"Era"})'),
        ("Long", "Wide to long: one row per system and paying position (nulls drop out)",
         'Table.UnpivotOtherColumns(Filled, {"Era", "System", "First Season"}, "Slot", "System Points")'),
        ("Positioned", "\"P7\" -> 7", 'Table.AddColumn(Long, "Position", each Number.From(Text.AfterDelimiter([Slot], "P")), Int64.Type)'),
        ("Kept", None, 'Table.SelectColumns(Positioned, {"Era", "System", "First Season", "Position", "System Points"})'),
        ("Typed", None, 'Table.TransformColumnTypes(Kept, {{"First Season", Int64.Type}, {"System Points", type number}})'),
    ]), [("Era", "string", {}), ("System", "string", {"sort": "First Season"}), ("First Season", "int64", {"hidden": True}),
         ("Position", "int64", {}), ("System Points", "double", {"fmt": "0"})]),
}

LAP_FSD = 'VAR _s = SELECTEDMEASURE () VAR _m = INT ( _s / 60 ) RETURN IF ( ISBLANK ( _s ), "0.000", """" & _m & ":" & FORMAT ( _s - 60 * _m, "00.000" ) & """" )'  # dynamic format string: a quoted literal built from the value

F = lambda cond: f"CALCULATE ( COUNTROWS ( Results ), {cond} )"  # noqa: E731

# Rookies / returning / churn only mean something against a period. Over all 75 seasons every driver would be a rookie,
# so when nothing narrows the seasons the market measures count 2023: the last season whose leavers are known.
PER = ("VAR _full = MIN ( Season[Year] ) = CALCULATE ( MIN ( Season[Year] ), REMOVEFILTERS ( Season ) )\n"
       "    && MAX ( Season[Year] ) = CALCULATE ( MAX ( Season[Year] ), REMOVEFILTERS ( Season ) )\n"
       "VAR _ref = CALCULATE ( MAX ( Season[Year] ), REMOVEFILTERS ( Season ) ) - 1\n")


def in_period(expr):
    return PER + f"RETURN IF ( _full,\n    CALCULATE ( {expr}, REMOVEFILTERS ( Season ), Season[Year] = _ref ),\n    {expr} )"


ROOKIES = ("VAR _s = MIN ( Season[Year] ) RETURN COUNTROWS ( FILTER ( SUMMARIZE ( Results, Driver[Driver Id], Driver[Driver First Season] ), "
           "Driver[Driver First Season] >= _s ) ) + 0")
LEAVERS = ("VAR _b = MAX ( Season[Year] ) VAR _last = CALCULATE ( MAX ( Season[Year] ), REMOVEFILTERS ( Season ) ) RETURN "
           "IF ( MIN ( Season[Year] ) < _last, COUNTROWS ( FILTER ( SUMMARIZE ( Results, Driver[Driver Id], Driver[Driver Last Season] ), "
           "Driver[Driver Last Season] <= _b && Driver[Driver Last Season] < _last ) ) + 0 )")

# (folder, name, format, description, DAX)
MEASURES = [
    # ------------------------------------------------------------- Racing
    ("Racing", "Races", NUM0, "Grands Prix held in the current filter context.", "DISTINCTCOUNT ( Results[Race Id] )"),
    ("Racing", "Seasons", NUM0, "Distinct seasons in the current filter context.", "DISTINCTCOUNT ( Race[Race Year] )"),
    ("Racing", "Starts", NUM0, "Race entries that took the start (grid slot or classified).",
     "CALCULATE ( COUNTROWS ( Results ), Results[Status Group] <> \"Did not start\" )"),
    ("Racing", "Wins", NUM0, "Race wins.", F("Results[Is Win] = TRUE ()")),
    ("Racing", "Podiums", NUM0, "Top-three finishes.", F("Results[Is Podium] = TRUE ()")),
    ("Racing", "Poles", NUM0, "Started from grid slot 1.", F("Results[Is Pole] = TRUE ()")),
    ("Racing", "Fastest Laps", NUM0, "Races in which the driver set the fastest lap.", F("Results[Is Fastest Lap] = TRUE ()")),
    ("Racing", "Points", NUM1, "Championship points as awarded at the time.", "SUM ( Results[Points Scored] )"),
    ("Racing", "Points (Modern Scale)", NUM0, "Points re-scored on today's 25-18-15-12-10-8-6-4-2-1 system, comparable across eras.",
     "SUM ( Results[Modern Points] )"),
    ("Racing", "Win Rate", PCT1, "Wins per start (UDF F1.Rate).", "F1.Rate ( [Wins], [Starts] )"),
    ("Racing", "Podium Rate", PCT1, "Podiums per start (UDF F1.Rate).", "F1.Rate ( [Podiums], [Starts] )"),
    ("Racing", "Pole Conversion", PCT1, "Share of pole positions turned into a win.",
     "DIVIDE ( " + F("Results[Is Pole] = TRUE (), Results[Is Win] = TRUE ()") + ", [Poles] )"),
    ("Racing", "Avg Positions Gained", "+0.0;-0.0;0.0", "Grid slot minus finishing position (classified finishers).",
     "AVERAGE ( Results[Positions Gained Value] )"),
    ("Racing", "Avg Finish Percentile", PCT0, "1 = won, 0 = last classified. Comparable across field sizes.",
     "AVERAGE ( Results[Finish Percentile Value] )"),
    ("Racing", "Drivers", NUM0, "Distinct drivers with at least one race entry.", "DISTINCTCOUNT ( Results[Driver Id] )"),
    ("Racing", "Constructors", NUM0, "Distinct constructors with at least one race entry.", "DISTINCTCOUNT ( Results[Constructor Id] )"),
    ("Racing", "Different Winners Count", NUM0, "Distinct drivers who won at least one race.",
     "CALCULATE ( DISTINCTCOUNT ( Results[Driver Id] ), Results[Is Win] = TRUE () )"),
    ("Racing", "Circuits Used", NUM0, "Distinct circuits that hosted a race in the current filter context.", "CALCULATE ( DISTINCTCOUNT ( Race[Circuit Id] ), Results )"),
    ("Racing", "Map Latitude", "0.0000", "For map bubbles.", "AVERAGE ( Circuit[Latitude] )"),
    ("Racing", "Map Longitude", "0.0000", "For map bubbles.", "AVERAGE ( Circuit[Longitude] )"),
    ("Racing", "Circuit Races Hosted", NUM0, "Grands Prix held at the circuit (all time, ignores season filters).", "SUM ( Circuit[Races Hosted] )"),
    ("Racing", "Circuit Pole Win Rate", PCT0, "Share of races at the circuit won from pole.", "AVERAGE ( Circuit[Pole Win Rate] )"),
    ("Racing", "Top Speed kph", NUM1, "Highest fastest-lap average speed (2004+ data).", "MAX ( Results[Fastest Lap Kph] )"),
    # -------------------------------------------------------- Reliability
    ("Reliability", "DNF Rate", PCT1, "Share of starts that ended in retirement (UDF F1.Rate).",
     "F1.Rate ( " + F("Results[Is DNF] = TRUE ()") + ", [Starts] )"),
    ("Reliability", "Mechanical DNF Rate", PCT1, "Share of starts that ended in a mechanical retirement.",
     "DIVIDE ( " + F("Results[Is Mechanical DNF] = TRUE ()") + ", [Starts] )"),
    ("Reliability", "Crash DNF Rate", PCT1, "Share of starts that ended in a crash or driver-caused retirement.",
     "DIVIDE ( " + F("Results[Status Group] = \"Crash / driver\"") + ", [Starts] )"),
    ("Reliability", "Finish Rate", PCT1, "Share of starts that were classified at the finish.",
     "DIVIDE ( " + F("Results[Is Finished] = TRUE ()") + ", [Starts] )"),
    ("Reliability", "Outcome Share", PCT1, "Share of starts by outcome group (for 100% stacked charts).",
     "DIVIDE ( [Starts], CALCULATE ( [Starts], ALL ( Results[Status Group] ) ) )"),
    # -------------------------------------------------------- Championship
    ("Championship", "Driver Titles Won", NUM0, "World Drivers' Championships won by the drivers in context.",
     "CALCULATE ( COUNTROWS ( Season ), TREATAS ( VALUES ( Driver[Driver Id] ), Season[Champion Driver Id] ), REMOVEFILTERS ( Race ) )"),
    ("Championship", "Championship Points", NUM1, "Cumulative points in the standings after the round in context.",
     "MAX ( Standings[Standing Points] )"),
    ("Championship", "Gap to Leader", "+#,0;-#,0;0", "Championship points behind the standings leader (smallest gap in context).", "MIN ( Standings[Standing Gap] )"),
    ("Championship", "Final Standings Points", NUM1, "Points after the last round of the season in context.",
     "VAR _last = CALCULATE ( MAX ( Standings[Standings Round] ), REMOVEFILTERS ( Driver ) )\n"
     "RETURN CALCULATE ( MAX ( Standings[Standing Points] ), Standings[Standings Round] = _last )"),
    ("Championship", "Sprint Points Scored", NUM1, "Points from sprint races (2021+), shaped in Power Query from the raw CSV.",
     "SUM ( 'Sprint Results'[Sprint Points] )"),
    ("Championship", "Points incl. Sprints", NUM1, "Grand Prix + sprint points: the appended 'Session Results' query. Equals the standings.",
     "SUM ( 'Session Results'[Session Points] )"),
    ("Championship", "Points (Chosen System)", NUM0,
     "Classified finishes re-scored with the points system picked in the 'Points Systems' slicer (default: today's 25-18-15…).",
     "VAR _sys = SELECTEDVALUE ( 'Points Systems'[System], \"2010–\" )\n"
     "RETURN SUMX ( FILTER ( ALL ( 'Points Systems' ), 'Points Systems'[System] = _sys ),\n"
     "    VAR _p = 'Points Systems'[Position] RETURN 'Points Systems'[System Points] * CALCULATE ( COUNTROWS ( Results ), Results[Finish Position] = _p ) )"),
    ("Championship", "Team Points Share", PCT1, "Constructor share of all modern-scale points in the season(s) in context.",
     "DIVIDE ( SUM ( 'Team Season'[Team Modern Points] ), CALCULATE ( SUM ( 'Team Season'[Team Modern Points] ), REMOVEFILTERS ( Constructor ) ) )"),
    ("Championship", "Avg Top Team Share", PCT1, "How dominant the best team was: its share of the season's points.",
     "AVERAGE ( Season[Top Team Points Share] )"),
    ("Championship", "Avg Champion Win Rate", PCT1, "Average of each season's champion win rate (wins per start of the drivers' champion).", "AVERAGE ( Season[Champion Win Rate] )"),
    ("Championship", "Avg Title Margin %", PCT1, "Champion's points lead over the runner-up, as a share of the champion's points.",
     "AVERAGE ( Season[Title Margin Pct] )"),
    # -------------------------------------------------------- Qualifying
    ("Qualifying", "Avg Gap to Pole", "0.00%", "Best qualifying lap vs the pole lap (1994+ data).",
     "AVERAGE ( Qualifying[Gap To Pole Pct] )"),
    ("Qualifying", "Avg Grid", NUM1, "Average starting grid slot.", "AVERAGE ( Results[Grid Position] )"),
    ("Qualifying", "Grid Win Probability", PCT1, "Historic chance of winning from this starting slot.",
     "DIVIDE ( SUM ( 'Grid Conversion'[Slot Wins] ), SUM ( 'Grid Conversion'[Slot Starts] ) )"),
    ("Qualifying", "Grid Podium Probability", PCT1, "Historical chance of a podium from the selected grid slot.",
     "DIVIDE ( SUM ( 'Grid Conversion'[Slot Podiums] ), SUM ( 'Grid Conversion'[Slot Starts] ) )"),
    # -------------------------------------------------------- Pit stops & laps
    ("Pit Stops & Laps", "Pit Stops Made", NUM0, "Pit stops recorded (pit-stop data, 2011 onward).", "COUNTROWS ( 'Pit Stops' )"),
    ("Pit Stops & Laps", "Median Pit Lane Time", SEC, "Pit-lane entry to exit, routine stops only (≤ 60 s). 2011+ data.",
     "CALCULATE ( MEDIAN ( 'Pit Stops'[Pit Seconds] ), 'Pit Stops'[Is Routine Stop] = TRUE () )"),
    ("Pit Stops & Laps", "Fastest Pit Lane Time", SEC, "Quickest routine pit-lane time in seconds (excludes repairs and penalties).",
     "CALCULATE ( MIN ( 'Pit Stops'[Pit Seconds] ), 'Pit Stops'[Is Routine Stop] = TRUE () )"),
    ("Pit Stops & Laps", "Stops per Race", NUM2, "Average stops per driver per race.",
     "DIVIDE ( [Pit Stops Made], COUNTROWS ( SUMMARIZE ( 'Pit Stops', 'Pit Stops'[Race Id], 'Pit Stops'[Driver Id] ) ) )"),
    ("Pit Stops & Laps", "Running Position", "0", "Position at the end of the lap (race-story chart, axis reversed).",
     "MIN ( 'Lap Times'[Lap Position] )"),
    ("Pit Stops & Laps", "Avg Lap Time", "0.000", "Seconds; the dynamic format string shows it as m:ss.000 (90.013 → 1:30.013).",
     "CALCULATE ( AVERAGE ( 'Lap Times'[Lap Seconds] ), 'Lap Times'[Is Pit Lap] = FALSE () )", LAP_FSD),
    ("Pit Stops & Laps", "Avg Median Race Pace", "0.000", "Each driver's median racing lap per race (Power Query Group By), averaged; shown as m:ss.000.",
     "AVERAGE ( 'Race Pace'[Median Lap Sec] )", LAP_FSD),
    ("Pit Stops & Laps", "Best Lap Time", "0.000", "Seconds; the dynamic format string shows it as m:ss.000.",
     "MIN ( 'Lap Times'[Lap Seconds] )", LAP_FSD),
    ("Pit Stops & Laps", "Laps Led", NUM0, "Laps completed in first place (lap-time data, 1996 onward).",
     "CALCULATE ( COUNTROWS ( 'Lap Times' ), 'Lap Times'[Lap Position] = 1 )"),
    # -------------------------------------------------------- Drivers
    ("Drivers", "Car-Adjusted Skill", "+0.000;-0.000;0.000",
     "Car-adjusted skill: finish-percentile points above an average driver in the same car (0 = average).",
     "AVERAGE ( 'Driver Skill'[Skill Value] )"),
    ("Drivers", "Skill Rank", "#0", "Rank of the driver on car-adjusted skill (1 = best).", "MIN ( 'Driver Skill'[Skill Rank Value] )"),
    ("Drivers", "Quali H2H Win %", PCT0, "Share of races out-qualifying the team-mate.",
     "DIVIDE ( SUM ( 'Teammate H2H'[Quali Wins] ), SUM ( 'Teammate H2H'[Races Together] ) )"),
    ("Drivers", "Race H2H Win %", PCT0, "Share of races finishing ahead of the team-mate (mechanical DNFs excluded).",
     "DIVIDE ( SUM ( 'Teammate H2H'[Race Wins] ), SUM ( 'Teammate H2H'[Comparable Races] ) )"),
    ("Drivers", "Avg Debut Age", NUM1, "Average age of drivers at their first Grand Prix.", "AVERAGE ( Driver[Debut Age] )"),
    # -------------------------------------------------------- Iterators & RELATED
    ("Iterators", "Avg Points per Race", NUM1, "Modern-scale points per Grand Prix entered: AVERAGEX over the races in context.",
     "AVERAGEX ( VALUES ( Results[Race Id] ), [Points (Modern Scale)] )"),
    ("Iterators", "Home Wins", NUM0, "Wins at a circuit in the driver's own country (RELATED walks Results → Race → Circuit).",
     "COUNTROWS ( FILTER ( Results, Results[Is Win] && RELATED ( Driver[Driver Country] ) = RELATED ( Circuit[Circuit Country] ) ) )"),
    ("Iterators", "Wins at Home Circuits", NUM0,
     "Wins at circuits in any country of the drivers in context: the Circuit filter reaches Results through its expanded table (Results → Race → Circuit).",
     "CALCULATE ( [Wins], TREATAS ( VALUES ( Driver[Driver Country] ), Circuit[Circuit Country] ) )"),
    ("Iterators", "Best Season Points", NUM0, "Best single season on the modern scale: MAXX turns each season row into a filter (context transition).",
     "MAXX ( VALUES ( Season[Year] ), [Points (Modern Scale)] )"),
    ("Iterators", "Seasons With a Win", NUM0, "Seasons in which the drivers in context won at least once.",
     "COUNTROWS ( FILTER ( VALUES ( Season[Year] ), [Wins] > 0 ) )"),
    ("Iterators", "Top Team of Selection", None, "Constructor with the most modern-scale points in the current selection (TOPN over ADDCOLUMNS).",
     "VAR _t = TOPN ( 1, ADDCOLUMNS ( VALUES ( Constructor[Constructor Name] ), \"@p\", [Points (Modern Scale)] ), [@p], DESC )\n"
     "RETURN MAXX ( _t, Constructor[Constructor Name] )"),
    # -------------------------------------------------------- Filter context
    ("Filter Context", "Front Row Wins", NUM0, "Wins from grid slot 1 or 2. KEEPFILTERS: a grid filter already on the visual is intersected, not replaced.",
     "CALCULATE ( [Wins], KEEPFILTERS ( Results[Grid Position] IN { 1, 2 } ) )"),
    ("Filter Context", "Wins From Outside Top 10", NUM0, "Wins from grid slot 11 or lower (FILTER over the Results rows in context).",
     "CALCULATE ( [Wins], FILTER ( Results, Results[Grid Position] > 10 ) )"),
    ("Filter Context", "Share of Season Wins", PCT1, "Share of all wins in the season(s) in context: ALLEXCEPT keeps only the Season filters.",
     "F1.Share ( [Wins], CALCULATE ( [Wins], ALLEXCEPT ( Results, Season ) ) )"),
    ("Filter Context", "% of Visible Wins", PCT1, "Share of the wins of the drivers visible in the visual (ALLSELECTED Driver).",
     "F1.Share ( [Wins], CALCULATE ( [Wins], ALLSELECTED ( Driver ) ) )"),
    ("Filter Context", "Podiums From Pole", NUM0, "Podiums from pole; the pole filter REPLACES any filter on Results[Is Pole].",
     "CALCULATE ( [Podiums], Results[Is Pole] = TRUE () )"),
    ("Filter Context", "Podiums From Pole (KF)", NUM0, "Podiums from pole; KEEPFILTERS intersects with a filter already on Results[Is Pole].",
     "CALCULATE ( [Podiums], KEEPFILTERS ( Results[Is Pole] = TRUE () ) )"),
    ("Filter Context", "Teammate Points Delta", "+#,0;-#,0;0",
     "Team-mates' points minus the driver's, read from the team-mate side: USERELATIONSHIP switches on the inactive Teammate Id → Driver relationship.",
     "CALCULATE ( SUM ( 'Teammate H2H'[Points Delta] ), USERELATIONSHIP ( 'Teammate H2H'[Teammate Id], Driver[Driver Id] ) )"),
    # -------------------------------------------------------- Time intelligence (Calendar; an F1 season = a calendar year)
    ("Time Intelligence", "Points STD", NUM0, "Season-to-date modern-scale points (DATESYTD on the marked date table).",
     "CALCULATE ( [Points (Modern Scale)], DATESYTD ( 'Calendar'[Date] ) )"),
    ("Time Intelligence", "Points QTD", NUM0, "Quarter-to-date modern-scale points.",
     "CALCULATE ( [Points (Modern Scale)], DATESQTD ( 'Calendar'[Date] ) )"),
    ("Time Intelligence", "Points MTD", NUM0, "Month-to-date modern-scale points.",
     "CALCULATE ( [Points (Modern Scale)], DATESMTD ( 'Calendar'[Date] ) )"),
    ("Time Intelligence", "Points PY", NUM0, "Modern-scale points in the same period one season earlier (Season filters cleared, so a season slicer can shift).",
     "CALCULATE ( [Points (Modern Scale)], SAMEPERIODLASTYEAR ( 'Calendar'[Date] ), REMOVEFILTERS ( Season ) )"),
    ("Time Intelligence", "Points YoY %", "+0%;-0%;0%", "Modern-scale points vs the previous season.",
     "VAR _py = [Points PY]\nRETURN DIVIDE ( [Points (Modern Scale)] - _py, _py )"),
    ("Time Intelligence", "Win Rate Prev Season", PCT1, "Win rate one season back (DATEADD -1 YEAR).",
     "CALCULATE ( [Win Rate], DATEADD ( 'Calendar'[Date], -1, YEAR ), REMOVEFILTERS ( Season ) )"),
    ("Time Intelligence", "Win Rate Prev Season (PREVIOUSYEAR)", PCT1, "Same as Win Rate Prev Season with PREVIOUSYEAR: always the whole previous year.",
     "CALCULATE ( [Win Rate], PREVIOUSYEAR ( 'Calendar'[Date] ), REMOVEFILTERS ( Season ) )"),
    ("Time Intelligence", "Wins Prev Season", NUM0, "Wins one season back (UDF F1.PrevSeason, EXPR parameter).",
     "F1.PrevSeason ( [Wins] )"),
    ("Time Intelligence", "DNF Rate 5Y Avg", PCT1, "Average of the yearly DNF rates over the five seasons ending in context (DATESINPERIOD -5 YEAR).",
     "AVERAGEX (\n    CALCULATETABLE ( VALUES ( 'Calendar'[Year] ), DATESINPERIOD ( 'Calendar'[Date], MAX ( 'Calendar'[Date] ), -5, YEAR ), REMOVEFILTERS ( Season ) ),\n"
     "    CALCULATE ( [DNF Rate], REMOVEFILTERS ( Season ) ) )"),
    ("Time Intelligence", "Points at Season End", NUM1, "Semi-additive: the championship points on the last date with standings (LASTNONBLANKVALUE).",
     "LASTNONBLANKVALUE ( 'Calendar'[Date], [Championship Points] )"),
    ("Time Intelligence", "Points STD (Cal)", NUM0, "Season-to-date on the calendar 'F1 Calendar' (calendar-based time intelligence).",
     "TOTALYTD ( [Points (Modern Scale)], 'F1 Calendar' )"),
    ("Time Intelligence", "Wins STD", NUM0, "Season-to-date wins (UDF F1.SeasonToDate).", "F1.SeasonToDate ( [Wins], 'F1 Calendar' )"),
    ("Time Intelligence", "Podiums STD", NUM0, "Season-to-date podiums (UDF F1.SeasonToDate).", "F1.SeasonToDate ( [Podiums], 'F1 Calendar' )"),
    # -------------------------------------------------------- Window functions (partition = the season, order = round)
    ("Window Functions", "Points Prev Round", NUM1, "Championship points after the previous round (OFFSET -1 over the selected rounds).",
     "CALCULATE ( [Championship Points], OFFSET ( -1, ALLSELECTED ( Race[Round] ), ORDERBY ( Race[Round] ) ) )"),
    ("Window Functions", "Running Points (Window)", NUM0, "Modern-scale points from round 1 to the current round (WINDOW 1 ABS … 0 REL).",
     "SUMX ( WINDOW ( 1, ABS, 0, REL, ALLSELECTED ( Race[Round] ), ORDERBY ( Race[Round] ) ), [Points (Modern Scale)] )"),
    ("Window Functions", "Points 3-Race Avg", NUM1, "Average modern-scale points over the current and two previous rounds.",
     "AVERAGEX ( WINDOW ( -2, REL, 0, REL, ALLSELECTED ( Race[Round] ), ORDERBY ( Race[Round] ) ), [Points (Modern Scale)] )"),
    ("Window Functions", "Season Rank by Wins", "#0", "Dense rank of the driver by wins among the drivers visible in the visual.",
     "IF ( [Wins] > 0, RANK ( DENSE, ALLSELECTED ( Driver[Driver Name] ), ORDERBY ( [Wins], DESC ) ) )"),
    ("Window Functions", "Top Driver of Decade", None, "Driver with the most wins in the decade(s) in context (INDEX 1 over all drivers).",
     "MAXX ( INDEX ( 1, ALL ( Driver[Driver Name] ), ORDERBY ( [Wins], DESC, Driver[Driver Name], ASC ) ), Driver[Driver Name] )"),
    # -------------------------------------------------------- Report UI
    ("Report UI", "Season Range Label", None, "Selected seasons, for page subtitles.",
     "VAR _a = MIN ( Race[Race Year] ) VAR _b = MAX ( Race[Race Year] )\n"
     "RETURN IF ( _a = _b, FORMAT ( _a, \"0\" ), FORMAT ( _a, \"0\" ) & \"–\" & FORMAT ( _b, \"0\" ) )"),
    ("Report UI", "Champions (driver · team)", None, "Drivers' and constructors' champion of the one selected season (text).",
     "IF ( HASONEVALUE ( Season[Year] ), SELECTEDVALUE ( Season[Champion] ) & \" · \" & COALESCE ( SELECTEDVALUE ( Season[Constructor Champion] ), \"no constructors' title\" ), \"Select one season\" )"),
    ("Report UI", "Race Winner Label", None, "Winner and pole sitter of the one selected race (text).",
     "IF ( HASONEVALUE ( Race[Race Label] ), \"Winner: \" & SELECTEDVALUE ( Race[Winner] ) & \"  ·  Pole: \" & SELECTEDVALUE ( Race[Pole Sitter] ), \"Select one race\" )"),
    # -------------------------------------------------------- What-if (Title Fight) and tooltip pages
    ("What-if", "Win Points Value", NUM0, "The value picked on the 'Win Points' what-if slider (25 = today's system).",
     "SELECTEDVALUE ( 'Win Points'[Win Points], 25 )"),
    ("What-if", "What-if Points", NUM0,
     "Modern-scale points with a win worth the slider value instead of 25. Not the same as 'Points (Chosen System)', "
     "which re-scores every finishing position with a historical system.",
     "[Points (Modern Scale)] + ( [Win Points Value] - 25 ) * [Wins]"),
    ("What-if", "Championship Position", "#0", "Driver's position in the official standings after the round in context.",
     "MIN ( Standings[Standing Position] )"),
    ("Report UI", "Circuit Lap Record", None, "Fastest race lap on record at the circuit (2004+ timing).",
     "SELECTEDVALUE ( Circuit[Lap Record], \"—\" )"),
    ("Report UI", "Circuit Lap Record Holder", None, "Driver who set the circuit's race lap record.",
     "SELECTEDVALUE ( Circuit[Lap Record Holder], \"—\" )"),
    # -------------------------------------------------------- Market (driver market page): Pareto, ABC, bands, rookies, cohort, churn
    ("Market", "Driver Win Rank", "#0", "Dense rank of the driver by wins among all drivers (RANKX), in the seasons in context.",
     "IF ( [Wins] > 0, RANKX ( ALL ( Driver[Driver Name] ), [Wins], , DESC, DENSE ) )"),
    ("Market", "Cumulative Win Share", PCT0,
     "Pareto: share of all wins held by this driver and every driver ranked at or above (RANKX + SUMX, all drivers, seasons in context).",
     "VAR _r = [Driver Win Rank]\n"
     "VAR _t = FILTER ( ADDCOLUMNS ( ALL ( Driver[Driver Name] ), \"@w\", [Wins] ), [@w] > 0 )\n"
     "VAR _ranked = ADDCOLUMNS ( _t, \"@r\", RANKX ( _t, [@w], , DESC, DENSE ) )\n"
     "RETURN IF ( NOT ISBLANK ( _r ), DIVIDE ( SUMX ( FILTER ( _ranked, [@r] <= _r ), [@w] ), SUMX ( _t, [@w] ) ) )"),
    ("Market", "Drivers for 80% of Wins", NUM0,
     "How many drivers, best first, it takes to reach 80% of the wins in context (drivers tied on wins count together).",
     "VAR _t = FILTER ( ADDCOLUMNS ( VALUES ( Driver[Driver Name] ), \"@w\", [Wins] ), [@w] > 0 )\n"
     "VAR _tot = SUMX ( _t, [@w] )\n"
     "VAR _c = ADDCOLUMNS ( _t, \"@above\", VAR _x = [@w] RETURN SUMX ( FILTER ( _t, [@w] > _x ), [@w] ) )\n"
     "RETURN COUNTROWS ( FILTER ( _c, [@above] < 0.8 * _tot ) )"),
    ("Market", "Share of Drivers for 80% of Wins", PCT1, "'Drivers for 80% of Wins' as a share of every driver who raced in context.",
     "DIVIDE ( [Drivers for 80% of Wins], [Drivers] )"),
    ("Market", "Cumulative Points Share", PCT0,
     "Constructor's modern-scale points plus every team above it, as a share of all points in the seasons in context.",
     "VAR _p = [Points (Modern Scale)]\n"
     "VAR _t = FILTER ( ADDCOLUMNS ( ALL ( Constructor[Constructor Name] ), \"@p\", [Points (Modern Scale)] ), [@p] > 0 )\n"
     "RETURN IF ( _p > 0, DIVIDE ( SUMX ( FILTER ( _t, [@p] >= _p ), [@p] ), SUMX ( _t, [@p] ) ) )"),
    ("Market", "ABC Class", None,
     "Dynamic ABC on modern-scale points, follows the era slicer: A = the teams that make up the first 70% of points, "
     "B = the next 20%, C = the last 10% (class set by where the team's slice starts).",
     "VAR _cum = [Cumulative Points Share]\n"
     "VAR _before = _cum - DIVIDE ( [Points (Modern Scale)], CALCULATE ( [Points (Modern Scale)], REMOVEFILTERS ( Constructor ) ) )\n"
     "RETURN IF ( NOT ISBLANK ( _cum ), SWITCH ( TRUE (), _before < 0.7, \"A\", _before < 0.9, \"B\", \"C\" ) )"),
    ("Market", "Drivers in Band", NUM0,
     "Drivers who raced in context whose win rate in context falls in the selected 'Win Bands' row (disconnected table).",
     "VAR _lo = MIN ( 'Win Bands'[Min Win Rate] )\nVAR _hi = MAX ( 'Win Bands'[Max Win Rate] )\n"
     "VAR _t = FILTER ( ADDCOLUMNS ( VALUES ( Driver[Driver Name] ), \"@s\", [Starts], \"@r\", COALESCE ( [Win Rate], 0 ) ), [@s] > 0 )\n"
     "RETURN COUNTROWS ( FILTER ( _t, ( [@r] >= _lo && [@r] < _hi ) || ( _lo = 0 && [@r] = 0 ) ) ) + 0"),
    ("Market", "Period Counted", None,
     "The seasons the rookie / returning / churn cards count: the selected era or season, or 2023 when nothing narrows the "
     "seasons (over all 75 seasons every driver would be a rookie; 2023 is the last season whose leavers are known).",
     PER + "VAR _a = MIN ( Season[Year] ) VAR _b = MAX ( Season[Year] )\n"
     "RETURN IF ( _full, FORMAT ( _ref, \"0\" ) & \" (default)\", IF ( _a = _b, FORMAT ( _a, \"0\" ), FORMAT ( _a, \"0\" ) & \"–\" & FORMAT ( _b, \"0\" ) ) )"),
    ("Market", "Drivers in Period", NUM0, "Drivers who raced in the period counted (see 'Period Counted').",
     in_period("[Drivers] + 0")),
    ("Market", "Rookies", NUM0, "Drivers in the period counted whose first season falls inside it.", in_period(ROOKIES)),
    ("Market", "Returning Drivers", NUM0, "Drivers in the period counted who had raced before it started.",
     "[Drivers in Period] - [Rookies]"),
    ("Market", "Rookie Share", PCT0, "Rookies as a share of all drivers in the selected period.", "DIVIDE ( [Rookies], [Drivers in Period] )"),
    ("Market", "Drivers Leaving Grid", NUM0,
     "Drivers in the period counted whose last season falls inside it (the last data season is excluded: its leavers are not known yet).",
     in_period(LEAVERS)),
    ("Market", "Churn Rate", PCT1, "Drivers leaving the grid ÷ drivers in the period counted.",
     "DIVIDE ( [Drivers Leaving Grid], [Drivers in Period] )"),
    ("Market", "% Still on Grid", PCT0,
     "Cohort retention: of the drivers in the row's debut cohort, the share who raced N seasons after their debut season "
     "(cohorts too recent to observe season N are left out). Ignores the era slicer.",
     "VAR _n = SELECTEDVALUE ( 'Seasons Since Debut'[Seasons Since Debut] )\n"
     "VAR _last = CALCULATE ( MAX ( Season[Year] ), REMOVEFILTERS ( Season ) )\n"
     "VAR _base = FILTER ( SUMMARIZE ( Driver, Driver[Driver Id], Driver[Driver First Season] ), Driver[Driver First Season] + _n <= _last )\n"
     "VAR _kept = FILTER ( _base, VAR _y = Driver[Driver First Season] + _n RETURN CALCULATE ( NOT ISEMPTY ( Results ), REMOVEFILTERS ( Season ), Results[Result Year] = _y ) )\n"
     "RETURN IF ( NOT ISBLANK ( _n ), DIVIDE ( COUNTROWS ( _kept ), COUNTROWS ( _base ) ) )"),
    ("Market", "Team Moves", NUM0, "Team-seasons with a known next season (row band → column band).",
     "COUNTROWS ( FILTER ( 'Team Season', NOT ISBLANK ( 'Team Season'[Next Band] ) ) )"),
    ("Market", "Transition %", PCT0, "Share of the row band's team-seasons that landed in the column band the next season.",
     "DIVIDE ( [Team Moves], CALCULATE ( [Team Moves], REMOVEFILTERS ( 'Team Season'[Next Band], 'Team Season'[Next Band Order] ) ) )"),
    # -------------------------------------------------------- Diagnosis (Why page): funnel, change vs PY, trend, re-scored champions
    ("Diagnosis", "Funnel Count", NUM0,
     "Race entries reaching each 'Funnel Stage': entered → started → finished → scored points → podium → win; each stage nested in the one before.",
     "SWITCH ( SELECTEDVALUE ( 'Funnel Stage'[Stage Order] ),\n"
     "    1, COUNTROWS ( Results ),\n    2, [Starts],\n"
     "    3, " + F("Results[Is Finished] = TRUE ()") + ",\n"
     "    4, " + F("Results[Is Finished] = TRUE (), Results[Is Points Finish] = TRUE ()") + ",\n"
     "    5, " + F("Results[Is Finished] = TRUE (), Results[Is Points Finish] = TRUE (), Results[Is Podium] = TRUE ()") + ",\n"
     "    6, " + F("Results[Is Finished] = TRUE (), Results[Is Points Finish] = TRUE (), Results[Is Podium] = TRUE (), Results[Is Win] = TRUE ()") + " )"),
    ("Diagnosis", "% of Entries", PCT1, "Funnel stage as a share of all entries.",
     "DIVIDE ( [Funnel Count], CALCULATE ( [Funnel Count], REMOVEFILTERS ( 'Funnel Stage' ), 'Funnel Stage'[Stage Order] = 1 ) )"),
    ("Diagnosis", "Points Change vs PY", "+#,0;-#,0;0", "Modern-scale points minus the same filter one season earlier ('Points PY').",
     "[Points (Modern Scale)] - [Points PY]"),
    ("Diagnosis", "DNF Rate Change vs PY", "+0.0%;-0.0%;0.0%", "DNF rate minus last season's, in percentage points (UDF F1.PrevSeason).",
     "[DNF Rate] - F1.PrevSeason ( [DNF Rate] )"),
    ("Diagnosis", "Avg Grid Change vs PY", "+0.0;-0.0;0.0", "Average grid slot minus last season's: + = starting further back.",
     "[Avg Grid] - F1.PrevSeason ( [Avg Grid] )"),
    ("Diagnosis", "Pit Lane Time Trend", SEC,
     "Least-squares line through the per-season median pit-lane time (LINESTX over the seasons in the visual).",
     "VAR _t = FILTER ( ADDCOLUMNS ( ALLSELECTED ( Season[Year] ), \"@v\", [Median Pit Lane Time] ), NOT ISBLANK ( [@v] ) )\n"
     "VAR _fit = LINESTX ( _t, [@v], Season[Year] )\n"
     "RETURN IF ( NOT ISBLANK ( [Median Pit Lane Time] ), MAXX ( _fit, [Intercept] ) + MAXX ( _fit, [Slope1] ) * MAX ( Season[Year] ) )"),
    ("Diagnosis", "Pit Lane Time Slope", "+0.000\" s\";-0.000\" s\"", "Seconds per season of the LINESTX trend.",
     "VAR _t = FILTER ( ADDCOLUMNS ( ALLSELECTED ( Season[Year] ), \"@v\", [Median Pit Lane Time] ), NOT ISBLANK ( [@v] ) )\n"
     "RETURN MAXX ( LINESTX ( _t, [@v], Season[Year] ), [Slope1] )"),
    ("Diagnosis", "Re-scored Champion", None,
     "Season leader when every finish is re-scored with the points system picked in 'Points Systems'; 'changed' when it is not the official champion.",
     "IF ( HASONEVALUE ( Season[Year] ),\n"
     "    VAR _t = ADDCOLUMNS ( SUMMARIZE ( Results, Driver[Driver Name], Driver[Surname] ), \"@p\", [Points (Chosen System)] )\n"
     "    VAR _top = TOPN ( 1, _t, [@p], DESC )\n"
     "    RETURN CONCATENATEX ( _top, Driver[Surname], \" / \" ) & IF ( COUNTROWS ( FILTER ( _top, Driver[Driver Name] = SELECTEDVALUE ( Season[Champion] ) ) ) > 0, \" · unchanged\", \" · changed\" ),\n"
     "    \"Select one season\" )"),
    ("Diagnosis", "What-if Champion", None,
     "Season leader on 'What-if Points' (modern scale, a win worth the slider value); 'changed' when it is not the official champion.",
     "IF ( HASONEVALUE ( Season[Year] ),\n"
     "    VAR _t = ADDCOLUMNS ( SUMMARIZE ( Results, Driver[Driver Name], Driver[Surname] ), \"@p\", [What-if Points] )\n"
     "    VAR _top = TOPN ( 1, _t, [@p], DESC )\n"
     "    RETURN CONCATENATEX ( _top, Driver[Surname], \" / \" ) & IF ( COUNTROWS ( FILTER ( _top, Driver[Driver Name] = SELECTEDVALUE ( Season[Champion] ) ) ) > 0, \" · unchanged\", \" · changed\" ),\n"
     "    \"Select one season\" )"),
    ("Report UI", "Skill Color", None, "Diverging colour for car-adjusted skill.",
     "VAR _s = [Car-Adjusted Skill]\nRETURN SWITCH ( TRUE (), ISBLANK ( _s ), \"#383835\", _s >= 0.08, \"#3987E5\", _s >= 0, \"#86B6EF\", _s > -0.08, \"#F19A99\", \"#E66767\" )"),
]


# ------------------------------------------------------------------ calculated tables, calendar, hierarchies
# (table, DAX, [(column, source, dataType, [extra TMDL lines])], [table-level TMDL lines])
CALCULATED = {
    # Own date table: continuous 1 Jan 1950 – 31 Dec 2024, unique Date, marked as the date table (dataCategory Time +
    # Date isKey). An F1 season is a calendar year, so DATESYTD = season-to-date.
    "Calendar": (
        "ADDCOLUMNS (\n    CALENDAR ( DATE ( 1950, 1, 1 ), DATE ( 2024, 12, 31 ) ),\n    \"Year\", YEAR ( [Date] ),\n"
        "    \"Decade\", FORMAT ( INT ( YEAR ( [Date] ) / 10 ) * 10, \"0\" ) & \"s\",\n    \"Month Number\", MONTH ( [Date] ),\n"
        "    \"Month\", FORMAT ( [Date], \"mmm\" ),\n    \"Year Month\", YEAR ( [Date] ) * 100 + MONTH ( [Date] )\n)",
        [("Date", "[Date]", "dateTime", ["isKey", "formatString: dd mmm yyyy"]),
         ("Year", "[Year]", "int64", ["formatString: 0"]),
         ("Decade", "[Decade]", "string", []),
         ("Month Number", "[Month Number]", "int64", ["isHidden", "formatString: 0"]),
         ("Month", "[Month]", "string", ["sortByColumn: 'Month Number'"]),
         ("Year Month", "[Year Month]", "int64", ["isHidden", "formatString: 0"])],
        ["dataCategory: Time"]),
    # Disconnected segmentation table: the measures read the band's limits (no relationship).
    "Win Bands": (
        'DATATABLE ( "Win Band", STRING, "Band Order", INTEGER, "Min Win Rate", DOUBLE, "Max Win Rate", DOUBLE, '
        '{ { "No wins", 1, 0, 0 }, { "Under 5%", 2, 0.000001, 0.05 }, { "5–19%", 3, 0.05, 0.2 }, { "20%+", 4, 0.2, 1.01 } } )',
        [("Win Band", "[Win Band]", "string", ["sortByColumn: 'Band Order'"]),
         ("Band Order", "[Band Order]", "int64", ["isHidden", "formatString: 0"]),
         ("Min Win Rate", "[Min Win Rate]", "double", ["isHidden", f"formatString: {PCT0}"]),
         ("Max Win Rate", "[Max Win Rate]", "double", ["isHidden", f"formatString: {PCT0}"])], []),
    # What-if parameter: a disconnected 5..50 series; the slider value feeds 'What-if Points'.
    "Win Points": (
        "GENERATESERIES ( 5, 50, 5 )",
        [("Win Points", "[Value]", "int64", ["formatString: 0", 'extendedProperty ParameterMetadata =\n\t\t\t{\n\t\t\t  "version": 0\n\t\t\t}'])], []),
    # Cohort axis: seasons after the debut season (0 would be 100% by definition, so it starts at 1).
    "Seasons Since Debut": (
        "GENERATESERIES ( 1, 8, 1 )",
        [("Seasons Since Debut", "[Value]", "int64", ["formatString: 0"])], []),
    # Race funnel stages; 'Funnel Count' switches on the stage order. Each stage is nested in the one before.
    "Funnel Stage": (
        'DATATABLE ( "Stage", STRING, "Stage Order", INTEGER, { { "Entered", 1 }, { "Started", 2 }, { "Finished", 3 }, '
        '{ "Scored points", 4 }, { "Podium", 5 }, { "Win", 6 } } )',
        [("Stage", "[Stage]", "string", ["sortByColumn: 'Stage Order'"]),
         ("Stage Order", "[Stage Order]", "int64", ["isHidden", "formatString: 0"])], []),
    # Dynamic RLS lookup: a login e-mail → the constructor it may see. SYNTHETIC demo rows (example.org), see README.
    "Team Access": (
        'DATATABLE ( "Email", STRING, "Constructor Name", STRING, '
        '{ { "ferrari.analyst@example.org", "Ferrari" }, { "mercedes.analyst@example.org", "Mercedes" }, '
        '{ "redbull.analyst@example.org", "Red Bull" } } )',
        [("Email", "[Email]", "string", []), ("Constructor Name", "[Constructor Name]", "string", [])], ["isHidden"]),
}

HIERARCHIES = {
    "Season": [("Season Path", ["Era", "Decade", "Year"])],
    "Calendar": [("Date Path", ["Year", "Month", "Date"])],
}

# Calendar-based time intelligence: (calendar, [(time unit, primary column, associated column)])
CALENDARS = [("F1 Calendar", [("year", "Year", None), ("month", "Year Month", "Month"), ("date", "Date", None)])]

# Field parameters: (label, NAMEOF target). Dynasty Metric swaps the measure, Legends By swaps the axis column.
FIELD_PARAMETERS = {
    "Dynasty Metric": [("Points share", "[Team Points Share]"), ("Points", "[Points (Modern Scale)]"), ("Wins", "[Wins]"),
                       ("Podiums", "[Podiums]"), ("Poles", "[Poles]")],
    "Legends By": [("Nationality", "Driver[Driver Nationality]"), ("Driver", "Driver[Driver Name]"), ("Last team", "Driver[Last Team]")],
}

# Calculation group 'Period': (item, DAX, format string expression or None). Prev Season goes through the UDF.
PERIOD_ITEMS = [
    ("Current", "SELECTEDMEASURE ()", None),
    ("Prev Season", "F1.PrevSeason ( SELECTEDMEASURE () )", None),
    ("YoY %", "VAR _py = F1.PrevSeason ( SELECTEDMEASURE () )\nRETURN DIVIDE ( SELECTEDMEASURE () - _py, _py )", '"+0%;-0%;0%"'),
    ("Season-to-date", "CALCULATE ( SELECTEDMEASURE (), DATESYTD ( 'Calendar'[Date] ) )", None),
    ("Career-to-date", "CALCULATE ( SELECTEDMEASURE (), DATESBETWEEN ( 'Calendar'[Date], BLANK (), MAX ( 'Calendar'[Date] ) ), REMOVEFILTERS ( Season ) )", None),
]

# User-defined functions, namespace F1. (name, description, signature => body)
# VAL vs EXPR: a VAL argument is evaluated once, where the function is called; an EXPR argument is re-evaluated
# inside the function's own filter context. F1.PrevSeason must take EXPR: with VAL, [Wins] would arrive already
# computed for the current season and the DATEADD inside would have nothing left to shift. F1.Share takes VAL:
# both numbers are final before the division, re-evaluating them would change nothing.
FUNCTIONS = [
    ("F1.Rate", "Part ÷ whole, blank when the whole is empty. Used by Win Rate, Podium Rate and DNF Rate.",
     "( part : NUMERIC, whole : NUMERIC ) => DIVIDE ( part, whole )"),
    ("F1.Share", "A value as a share of a total. VAL parameters: both are computed by the caller, then divided.",
     "( value : SCALAR NUMERIC VAL, total : SCALAR NUMERIC VAL ) => DIVIDE ( value, total )"),
    ("F1.PrevSeason", "A measure one season back. EXPR parameter: re-evaluated after DATEADD shifts the dates.",
     "( value : SCALAR NUMERIC EXPR ) => CALCULATE ( value, DATEADD ( 'Calendar'[Date], -1, YEAR ), REMOVEFILTERS ( Season ) )"),
    ("F1.SeasonToDate", "Any measure from the season's first race to the date in context, on the calendar passed in (typed CALENDARREF).",
     "( value : SCALAR NUMERIC EXPR, cal : CALENDARREF ) => CALCULATE ( value, DATESYTD ( cal ) )"),
]

# RLS and OLS. Team Analyst is dynamic (login → constructor via 'Team Access'); Ferrari Analyst is the static
# version (one role per team); Public hides the experimental skill model (OLS) and dates of birth (CLS).
ROLES = {
    "Team Analyst": {"filters": {"Constructor": "Constructor[Constructor Name] IN CALCULATETABLE ( VALUES ( 'Team Access'[Constructor Name] ), "
                                                "'Team Access'[Email] = USERPRINCIPALNAME () )"},
                     "hide_tables": [], "hide_columns": {}},
    "Ferrari Analyst": {"filters": {"Constructor": 'Constructor[Constructor Name] = "Ferrari"'}, "hide_tables": [], "hide_columns": {}},
    "Public": {"filters": {}, "hide_tables": ["Driver Skill"], "hide_columns": {"Driver": ["Dob"]}},
}

# Incremental refresh on Lap Times: keep 30 seasons, refresh the latest one. (rolling unit, periods, incremental unit, periods)
REFRESH = {"Lap Times": ("year", 30, "year", 1)}
RANGE_FILTER = {  # the RangeStart/RangeEnd filter step (>= start, < end), on a date the partition carries
    "Lap Times": "each DateTime.From([race_date]) >= RangeStart and DateTime.From([race_date]) < RangeEnd",
    "Results": "each #datetime([year], 1, 1, 0, 0, 0) >= RangeStart and #datetime([year], 1, 1, 0, 0, 0) < RangeEnd",
}


def pretty(col: str) -> str:
    special = {"id": "Id", "h2h": "H2H", "dnf": "DNF", "dnfs": "DNFs", "pct": "Pct", "kph": "Kph", "ms": "Ms", "se": "SE"}
    return " ".join(special.get(w, w.capitalize()) for w in col.split("_"))


def q(name: str) -> str:
    """Quote a TMDL/DAX object name when needed."""
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else "'" + name.replace("'", "''") + "'"


def col_name(cfg, col):
    return cfg.get("names", {}).get(col) or pretty(col)


def table_tmdl(con, name, mart, cfg):
    cols = con.execute(f"describe mart.{mart}").fetchall()
    lines = [f"table {q(name)}", f"{T}lineageTag: {tag('table', name)}", ""]
    m_types = []
    for col, dtype, *_ in cols:
        tm, mq = TYPES[dtype]
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
            lines.append(f"{T}{T}formatString: dd mmm yyyy")
        if cat := cfg.get("category", {}).get(col):
            lines.append(f"{T}{T}dataCategory: {cat}")
        lines.append(f"{T}{T}lineageTag: {tag('column', name, col)}")
        lines.append(f"{T}{T}summarizeBy: none")  # BPA NUMERIC_COLUMN_SUMMARIZE_BY: aggregate through measures, not implicit sums
        lines.append(f"{T}{T}sourceColumn: {col}")
        if sort := cfg.get("sort", {}).get(col):
            lines.append(f"{T}{T}sortByColumn: {q(col_name(cfg, sort))}")
        lines.append("")
    for col, dtype, dax, fmt, *more in cfg.get("calc", []):  # calculated columns (+ optional extra TMDL lines)
        lines += [f"{T}column {q(col)} = {dax}", f"{T}{T}dataType: {dtype}"]
        lines += [f"{T}{T}formatString: {fmt}"] * bool(fmt)
        lines += [f"{T}{T}{e}" for e in (more[0] if more else [])]
        lines += [f"{T}{T}lineageTag: {tag('column', name, col)}", f"{T}{T}summarizeBy: none", ""]
    lines += hierarchy_lines(name)
    last = "Typed"
    m = ["let",
         f'    Source = Parquet.Document(File.Contents(DataFolder & "{mart}.parquet")),',
         f"    Typed = Table.TransformColumnTypes(Source, {{{', '.join(m_types)}}})"]
    if name in RANGE_FILTER:  # incremental refresh: the Service swaps RangeStart/RangeEnd per partition
        m[-1] += ","
        m.append(f"    InRange = Table.SelectRows(Typed, {RANGE_FILTER[name]})")
        last = "InRange"
    m += ["in", f"    {last}"]
    if name in REFRESH:
        rg, rp, ig, ip = REFRESH[name]
        lines += [f"{T}refreshPolicy", f"{T}{T}policyType: basic", f"{T}{T}rollingWindowGranularity: {rg}",
                  f"{T}{T}rollingWindowPeriods: {rp}", f"{T}{T}incrementalGranularity: {ig}",
                  f"{T}{T}incrementalPeriods: {ip}", f"{T}{T}sourceExpression =", *[f"{T * 4}{l}" for l in m], ""]
    lines += [f"{T}partition {q(name)} = m", f"{T}{T}mode: import", f"{T}{T}source ="]
    lines += [f"{T * 4}{l}" for l in m]
    lines += ["", f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def hierarchy_lines(name):
    lines = []
    for h, levels in HIERARCHIES.get(name, []):
        lines += [f"{T}hierarchy {q(h)}", f"{T}{T}lineageTag: {tag('hierarchy', name, h)}", ""]
        for lv in levels:
            lines += [f"{T}{T}level {q(lv)}", f"{T * 3}lineageTag: {tag('level', name, h, lv)}", f"{T * 3}column: {q(lv)}", ""]
    return lines


def calculated_tmdl(name, dax, cols, extra):
    lines = [f"table {q(name)}", f"{T}lineageTag: {tag('table', name)}", *[f"{T}{e}" for e in extra], ""]
    for col, src, dtype, more in cols:
        lines += [f"{T}column {q(col)}", f"{T}{T}dataType: {dtype}", f"{T}{T}lineageTag: {tag('column', name, col)}",
                  f"{T}{T}summarizeBy: none", f"{T}{T}isNameInferred", f"{T}{T}sourceColumn: {src}"]
        lines += [f"{T}{T}{e}" for e in more] + [""]
    lines += hierarchy_lines(name)
    if name == "Calendar":
        for cal, groups in CALENDARS:
            lines += [f"{T}calendar {q(cal)}", f"{T}{T}lineageTag: {tag('calendar', cal)}", ""]
            for unit, primary, assoc in groups:
                lines += [f"{T}{T}calendarColumnGroup = {unit}", f"{T * 3}primaryColumn: {q(primary)}"]
                lines += ([f"{T * 3}associatedColumn: {q(assoc)}"] if assoc else []) + [""]
    lines += [f"{T}partition {q(name)} = calculated", f"{T}{T}mode: import", f"{T}{T}source ="]
    lines += [f"{T * 4}{l}" for l in dax.split("\n")] + [""]
    return "\n".join(lines)


def calc_group_tmdl():
    lines = ["table Period", f"{T}lineageTag: {tag('table', 'Period')}", "", f"{T}calculationGroup", f"{T}{T}precedence: 10", ""]
    for i, (name, dax, fmt) in enumerate(PERIOD_ITEMS):
        if "\n" in dax:
            lines += [f"{T}{T}calculationItem {q(name)} =", *[f"{T * 4}{l}" for l in dax.split("\n")]]
        else:
            lines.append(f"{T}{T}calculationItem {q(name)} = {dax}")
        lines.append(f"{T * 3}ordinal: {i}")
        if fmt:
            lines.append(f"{T * 3}formatStringDefinition = {fmt}")
        lines.append("")
    lines += [f"{T}column Period", f"{T}{T}dataType: string", f"{T}{T}lineageTag: {tag('column', 'Period', 'Period')}",
              f"{T}{T}summarizeBy: none", f"{T}{T}sourceColumn: Name", f"{T}{T}sortByColumn: Ordinal", "",
              f"{T}column Ordinal", f"{T}{T}dataType: int64", f"{T}{T}isHidden", f"{T}{T}formatString: 0",
              f"{T}{T}lineageTag: {tag('column', 'Period', 'Ordinal')}", f"{T}{T}summarizeBy: sum", f"{T}{T}sourceColumn: Ordinal", ""]
    return "\n".join(lines)


def field_parameter_tmdl(t, items):
    rows = ",\n".join(f'    ( "{label}", NAMEOF ( {ref} ), {i} )' for i, (label, ref) in enumerate(items))
    return "\n".join([
        f"table {q(t)}", f"{T}lineageTag: {tag('table', t)}", "",
        f"{T}column {q(t)}", f"{T}{T}dataType: string", f"{T}{T}lineageTag: {tag('column', t, 'a')}",
        f"{T}{T}summarizeBy: none", f"{T}{T}sourceColumn: [Value1]", f"{T}{T}sortByColumn: '{t} Order'", "",
        f"{T}{T}relatedColumnDetails", f"{T * 3}groupByColumn: '{t} Fields'", "",
        f"{T}column '{t} Fields'", f"{T}{T}dataType: string", f"{T}{T}isHidden", f"{T}{T}lineageTag: {tag('column', t, 'f')}",
        f"{T}{T}summarizeBy: none", f"{T}{T}sourceColumn: [Value2]", f"{T}{T}sortByColumn: '{t} Order'", "",
        f"{T}{T}extendedProperty ParameterMetadata =", f"{T * 4}{{", f'{T * 4}  "version": 3,', f'{T * 4}  "kind": 2', f"{T * 4}}}", "",
        f"{T}column '{t} Order'", f"{T}{T}dataType: int64", f"{T}{T}isHidden", f"{T}{T}formatString: 0",
        f"{T}{T}lineageTag: {tag('column', t, 'o')}", f"{T}{T}summarizeBy: none", f"{T}{T}sourceColumn: [Value3]", "",
        f"{T}partition {q(t)} = calculated", f"{T}{T}mode: import", f"{T}{T}source =",
        *[f"{T * 4}{l}" for l in ("{\n" + rows + "\n}").split("\n")], ""])


def functions_tmdl():
    lines = []
    for name, desc, body in FUNCTIONS:
        lines += [f"/// {desc}", f"function {q(name)} = {body}", f"{T}lineageTag: {tag('function', name)}", ""]
    return "\n".join(lines)


def role_tmdl(name, r):
    lines = [f"role {q(name)}", f"{T}modelPermission: read", ""]
    for table in sorted(set(r["filters"]) | set(r["hide_columns"])):
        dax = r["filters"].get(table)
        lines.append(f"{T}tablePermission {q(table)}" + (f" = {dax}" if dax else ""))
        lines += [""] + [f"{T}{T}columnPermission {q(c)} = none" for c in r["hide_columns"].get(table, [])] + [""]
    for table in r["hide_tables"]:
        lines += [f"{T}tablePermission {q(table)}", f"{T}{T}metadataPermission: none", ""]
    return "\n".join(lines)


def pq_table_tmdl(name, m, cols):
    """A table whose shape lives in its own M (Power Query), not in a mart."""
    lines = [f"table {q(name)}", f"{T}lineageTag: {tag('table', name)}", ""]
    for col, tm, o in cols:
        lines.append(f"{T}column {q(col)}")
        lines.append(f"{T}{T}dataType: {tm}")
        if o.get("hidden"):
            lines.append(f"{T}{T}isHidden")
        if fmt := o.get("fmt", "0" if tm == "int64" else None):
            lines.append(f"{T}{T}formatString: {fmt}")
        lines.append(f"{T}{T}lineageTag: {tag('column', name, col)}")
        lines.append(f"{T}{T}summarizeBy: none")  # BPA NUMERIC_COLUMN_SUMMARIZE_BY, as in table_tmdl
        lines.append(f"{T}{T}sourceColumn: {col}")
        if o.get("sort"):
            lines.append(f"{T}{T}sortByColumn: {q(o['sort'])}")
        lines.append("")
    lines += [f"{T}partition {q(name)} = m", f"{T}{T}mode: import", f"{T}{T}source ="]
    lines += [f"{T * 4}{l}" for l in m]
    lines += ["", f"{T}annotation PBI_ResultType = Table", ""]
    return "\n".join(lines)


def model_columns(con):
    cols = {t: {col_name(cfg, c[0]) for c in con.execute(f"describe mart.{mart}").fetchall()} | {c[0] for c in cfg.get("calc", [])}
            for t, (mart, cfg) in TABLES.items()}
    cols.update({t: {c[0] for c in spec} for t, (_, spec) in PQ_TABLES.items()})
    cols.update({t: {c[0] for c in spec} for t, (_, spec, _) in CALCULATED.items()})
    cols.update({t: {t, f"{t} Fields", f"{t} Order"} for t in FIELD_PARAMETERS} | {"Period": {"Period", "Ordinal"}})
    return cols


def colref(table, col):
    return col_name(TABLES[table][1], col) if table in TABLES else col


# Measures that return text or a hex colour; BPA's format-string rules are ignored on them (annotation, reason above).
TEXT_MEASURES = {"ABC Class", "Champions (driver · team)", "Circuit Lap Record Holder", "Circuit Lap Record", "Period Counted",
                 "Race Winner Label", "Re-scored Champion", "Season Range Label", "Skill Color", "Top Driver of Decade",
                 "Top Team of Selection", "What-if Champion"}


def measures_tmdl():
    lines = ["table _Measures", f"{T}lineageTag: {tag('table', '_Measures')}", ""]
    for folder, name, fmt, desc, dax, *fsd in MEASURES:
        if desc:
            lines.append(f"{T}/// {desc}")
        if "\n" in dax:
            lines.append(f"{T}measure {q(name)} =")
            lines += [f"{T * 3}{l}" for l in dax.split("\n")]
        else:
            lines.append(f"{T}measure {q(name)} = {dax}")
        if fmt and not fsd:
            lines.append(f"{T}{T}formatString: {fmt}")
        lines.append(f"{T}{T}displayFolder: {folder}")
        lines.append(f"{T}{T}lineageTag: {tag('measure', name)}")
        if fsd:  # expression property last, like crm-pipeline (a following property reads as indentation error)
            lines.append(f"{T}{T}formatStringDefinition = {fsd[0]}")
        if name in TEXT_MEASURES:  # text / hex-colour result: a number format string has no meaning
            lines += ["", f'{T}{T}annotation BestPracticeAnalyzer_IgnoreRules = {{"RuleIDs":["PROVIDE_FORMAT_STRING_FOR_MEASURES","INTEGER_FORMATTING"]}}']
        lines.append("")
    lines += [f"{T}column Value", f"{T}{T}dataType: int64", f"{T}{T}isHidden", f"{T}{T}formatString: 0",
              f"{T}{T}lineageTag: {tag('column', '_Measures', 'Value')}", f"{T}{T}summarizeBy: none", f"{T}{T}isNameInferred",
              f"{T}{T}sourceColumn: [Value]", "",
              f"{T}partition _Measures = calculated", f"{T}{T}mode: import", f"{T}{T}source = {{1}}", ""]
    return "\n".join(lines)


def check(con):
    """Fail fast on name collisions, DAX references that don't resolve, and model best-practice slips."""
    columns = model_columns(con)
    measures = [m[1] for m in MEASURES]
    all_cols = {c.lower() for cs in columns.values() for c in cs} | {t.lower() for t in columns}
    clash = {m for m in measures if m.lower() in all_cols or measures.count(m) > 1}
    assert not clash, f"measure names collide with tables/columns: {clash}"
    fns = {f[0] for f in FUNCTIONS}
    tables = set(columns) | {c for c, _ in CALENDARS}
    dax_all = [(m[1], m[4]) for m in MEASURES] + [(f"item {n}", d) for n, d, _ in PERIOD_ITEMS] \
        + [(f"function {n}", b) for n, _, b in FUNCTIONS] + [(f"role {r}", d) for r, v in ROLES.items() for d in v["filters"].values()] \
        + [(p, ref) for p, items in FIELD_PARAMETERS.items() for _, ref in items] \
        + [(f"{t}[{c}]", d) for t, (_, cfg) in TABLES.items() for c, _, d, *_ in cfg.get("calc", [])]
    for name, dax in dax_all:
        dax = re.sub(r'"[^"]*"', '""', dax)
        for a, b, col in re.findall(r"(?:'([^']+)'|\b([A-Za-z_][A-Za-z0-9_]*))\[([^\]]+)\]", dax):
            table = a or b
            assert table in columns and col in columns[table], f"[{name}] references missing column {table}[{col}]"
        for ref in re.findall(r"(?<![\w'\]])\[([^\]]+)\]", dax):
            # @x = ADDCOLUMNS column; Intercept / Slope1 = LINESTX output columns
            assert ref in measures or ref.startswith("@") or ref in ("Intercept", "Slope1"), f"[{name}] references missing measure [{ref}]"
        for ref in re.findall(r"'([^'\n]+)'", re.sub(r"'[^'\n]+'\[[^\]]*\]", "", dax)):
            assert ref in tables, f"[{name}] references missing table/calendar '{ref}'"
        for fn in re.findall(r"\b([A-Z][A-Za-z0-9]*\.[A-Za-z]+)\s*\(", dax):
            assert fn in fns, f"[{name}] calls unknown function {fn}"
        assert dax.count("(") == dax.count(")"), f"[{name}] unbalanced parentheses"
    for ft, fc, tt, tc, *o in RELATIONSHIPS:
        assert colref(ft, fc) in columns[ft] and colref(tt, tc) in columns[tt], (ft, fc, tt, tc)
        # 2-s5: every relationship is single-direction; only the declared m2m is many-to-many
        assert not (o and o[0].get("both")), f"{ft} → {tt} must filter one way"
    assert sum(1 for r in RELATIONSHIPS if len(r) > 4 and r[4].get("active") is False) == 1, "exactly one inactive relationship"
    for _, groups in CALENDARS:
        assert all(p in columns["Calendar"] and (a is None or a in columns["Calendar"]) for _, p, a in groups)
    for t, hs in HIERARCHIES.items():
        assert all(lv in columns[t] for _, lvs in hs for lv in lvs), t
    for r in ROLES.values():
        assert all(t in columns for t in [*r["filters"], *r["hide_tables"], *r["hide_columns"]])
        assert all(c in columns[t] for t, cs in r["hide_columns"].items() for c in cs)
    # 7-s9: helper columns stay out of the field list — every *Id key and every sort-by helper is hidden
    for t, (mart, cfg) in TABLES.items():
        raw = [c[0] for c in con.execute(f"describe mart.{mart}").fetchall()]
        for c in raw:
            if c.endswith("_id"):
                assert c in cfg.get("hidden", []) or t in ("Season",), f"{t}[{c}] is a key and should be hidden"
        for helper in cfg.get("sort", {}).values():
            assert helper in cfg.get("hidden", []), f"{t}[{helper}] is a sort-by helper and should be hidden"
    for folder, name, fmt, desc, dax, *fsd in MEASURES:
        assert fmt or fsd or folder == "Report UI" or name in ("Top Team of Selection", "Top Driver of Decade", "ABC Class", "Period Counted",
                                                                               "Re-scored Champion", "What-if Champion"), f"[{name}] needs a format string"
    print(f"  ✓ {len(MEASURES)} measures, {len(FUNCTIONS)} functions, {len(PERIOD_ITEMS)} calculation items: every reference resolves; best-practice rules pass")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(ROOT / "data" / "marts"))
    raw = ap.parse_args().data_dir
    data_dir = raw.rstrip("/\\") + ("\\" if "\\" in raw else "/")

    con = duckdb.connect(str(ROOT / "data" / "warehouse" / "f1.duckdb"), read_only=True)
    check(con)
    shutil.rmtree(OUT, ignore_errors=True)
    d = OUT / "definition"
    (d / "tables").mkdir(parents=True)

    (OUT / "definition.pbism").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",\n'
        '  "version": "4.2",\n  "settings": {}\n}\n')
    (OUT / ".platform").write_text(
        '{\n  "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",\n'
        '  "metadata": {"type": "SemanticModel", "displayName": "F1"},\n'
        '  "config": {"version": "2.0", "logicalId": "3b0c9a55-7f1e-4d2a-9e61-f1a1c0de2024"}\n}\n')

    # 1702: user-defined functions and calendars need the current compatibility level.
    (d / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1702\n")
    all_tables = [*TABLES, *PQ_TABLES, *CALCULATED, "Period", *FIELD_PARAMETERS, "_Measures"]
    (d / "model.tmdl").write_text("\n".join([
        "model Model", f"{T}culture: en-US", f"{T}defaultPowerBIDataSourceVersion: powerBI_V3", f"{T}discourageImplicitMeasures",
        f"{T}sourceQueryCulture: en-US", f"{T}dataAccessOptions", f"{T}{T}legacyRedirects", f"{T}{T}returnErrorValuesAsNull",
        "", "annotation __PBI_TimeIntelligenceEnabled = 0", "",
        *[f"ref table {q(t)}" for t in all_tables], "", *[f"ref role {q(r)}" for r in ROLES], ""]))
    (d / "functions.tmdl").write_text(functions_tmdl())
    (d / "roles").mkdir()
    for name, r in ROLES.items():
        (d / "roles" / f"{name}.tmdl").write_text(role_tmdl(name, r))
    raw_dir = str(ROOT / "data" / "raw") + "/"
    expr = [f'expression DataFolder = "{data_dir}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
            f"{T}lineageTag: {tag('expression', 'DataFolder')}", "", f"{T}annotation PBI_ResultType = Text", "",
            f'expression RawFolder = "{raw_dir}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
            f"{T}lineageTag: {tag('expression', 'RawFolder')}", "", f"{T}annotation PBI_ResultType = Text", ""]
    # reserved names for incremental refresh; Desktop loads everything between them (all 75 seasons)
    for name, value in [("RangeStart", "#datetime(1950, 1, 1, 0, 0, 0)"), ("RangeEnd", "#datetime(2025, 1, 1, 0, 0, 0)")]:
        expr += [f'expression {name} = {value} meta [IsParameterQuery=true, Type="DateTime", IsParameterQueryRequired=true]',
                 f"{T}lineageTag: {tag('expression', name)}", "", f"{T}annotation PBI_ResultType = DateTime", ""]
    for name, m in STAGING.items():  # shared expressions are never loaded: staging queries with load disabled
        expr += [f"expression {q(name)} =", *[f"{T * 3}{l}" for l in m], f"{T}lineageTag: {tag('expression', name)}", "",
                 f"{T}annotation PBI_ResultType = Table", ""]
    (d / "expressions.tmdl").write_text("\n".join(expr))
    pqdir = ROOT / "powerquery"  # the same M as loose files, so a parser can check it (tools/m-check)
    shutil.rmtree(pqdir, ignore_errors=True)
    pqdir.mkdir()
    for name, m in [*STAGING.items(), *[(t, v[0]) for t, v in PQ_TABLES.items()]]:
        (pqdir / f"{name}.pq").write_text("\n".join(m) + "\n")

    for name, (mart, cfg) in TABLES.items():
        (d / "tables" / f"{name}.tmdl").write_text(table_tmdl(con, name, mart, cfg))
    for name, (m, cols) in PQ_TABLES.items():
        (d / "tables" / f"{name}.tmdl").write_text(pq_table_tmdl(name, m, cols))
    for name, (dax, cols, extra) in CALCULATED.items():
        (d / "tables" / f"{name}.tmdl").write_text(calculated_tmdl(name, dax, cols, extra))
    (d / "tables" / "Period.tmdl").write_text(calc_group_tmdl())
    for t, items in FIELD_PARAMETERS.items():
        (d / "tables" / f"{t}.tmdl").write_text(field_parameter_tmdl(t, items))
    (d / "tables" / "_Measures.tmdl").write_text(measures_tmdl())

    rel = []
    for ft, fc, tt, tc, *o in RELATIONSHIPS:
        o = o[0] if o else {}
        rel += [f"relationship {tag('rel', ft, fc, tt)}"] + [f"{T}isActive: false"] * (o.get("active") is False)
        rel += [f"{T}fromCardinality: many", f"{T}toCardinality: many"] * bool(o.get("m2m"))
        rel += [f"{T}fromColumn: {q(ft)}.{q(colref(ft, fc))}", f"{T}toColumn: {q(tt)}.{q(colref(tt, tc))}", ""]
    (d / "relationships.tmdl").write_text("\n".join(rel))

    # DAX query view tabs: one .dax per tab, tab order in .pbi/daxQueries.json (Power BI projects layout)
    from dax_queries import ASSERTS, QUERIES
    dq = OUT / "DAXQueries"
    (dq / ".pbi").mkdir(parents=True)
    for tab, text in QUERIES.items():
        (dq / f"{tab}.dax").write_text(text)
    for tab, expected in ASSERTS.items():  # pbi-kit daxtest.py reads <tab>.assert.json next to <tab>.dax
        (dq / f"{tab}.assert.json").write_text(json.dumps(expected, indent=2) + "\n")
    (dq / ".pbi" / "daxQueries.json").write_text(json.dumps(
        {"version": "1.0.0", "tabOrder": list(QUERIES), "defaultTab": next(iter(QUERIES))}, indent=2) + "\n")

    print(f"→ {OUT.relative_to(ROOT)}  ({len(all_tables)} tables, {len(RELATIONSHIPS)} relationships, {len(MEASURES)} measures, "
          f"{len(FUNCTIONS)} functions, {len(ROLES)} roles, {len(QUERIES)} DAX queries)")
    print(f"  DataFolder = {data_dir}")


if __name__ == "__main__":
    main()
