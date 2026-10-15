let
    // Staging query (load disabled): the raw CSV with \N already turned into null
    Source = #"Sprint Raw",
    // Only what the model uses, first: the CSV doesn't fold, so drop columns before any work
    Kept = Table.SelectColumns(Source, {"raceId", "driverId", "constructorId", "grid", "positionOrder", "points", "milliseconds", "fastestLapTime", "statusId"}),
    // Stray spaces around the lap time would break the split below
    Trimmed = Table.TransformColumns(Kept, {{"fastestLapTime", Text.Trim, type text}}),
    // Text to number; a value that won't parse becomes null instead of an error
    Numbers = Table.TransformColumns(Trimmed, {{"raceId", each try Number.From(_, "en-US") otherwise null, type nullable number}, {"driverId", each try Number.From(_, "en-US") otherwise null, type nullable number}, {"constructorId", each try Number.From(_, "en-US") otherwise null, type nullable number}, {"grid", each try Number.From(_, "en-US") otherwise null, type nullable number}, {"positionOrder", each try Number.From(_, "en-US") otherwise null, type nullable number}, {"points", each try Number.From(_, "en-US") otherwise null, type nullable number}, {"milliseconds", each try Number.From(_, "en-US") otherwise null, type nullable number}, {"statusId", each try Number.From(_, "en-US") otherwise null, type nullable number}}),
    // Belt and braces: any error left in a numeric column is replaced by null
    Cleaned = Table.ReplaceErrorValues(Numbers, {{"raceId", null}, {"driverId", null}, {"constructorId", null}, {"grid", null}, {"positionOrder", null}, {"points", null}, {"milliseconds", null}, {"statusId", null}}),
    // A sprint row without points is a data error, not a zero
    Scored = Table.SelectRows(Cleaned, each [points] <> null and [points] >= 0),
    // "1:30.013" -> minutes and seconds
    Split = Table.SplitColumn(Scored, "fastestLapTime", Splitter.SplitTextByDelimiter(":", QuoteStyle.None), {"Lap Min", "Lap Sec"}),
    // Back to one number: 1 × 60 + 30.013 = 90.013 s
    LapSeconds = Table.AddColumn(Split, "Sprint Fastest Lap Sec", each try Number.From([Lap Min]) * 60 + Number.From([Lap Sec], "en-US") otherwise null, type nullable number),
    // Derived: race time in seconds (finishers on the lead lap only; null otherwise)
    SprintSeconds = Table.AddColumn(LapSeconds, "Sprint Seconds", each if [milliseconds] = null then null else [milliseconds] / 1000, type nullable number),
    // Look up the status text in status.csv (a second staging query)
    Merged = Table.NestedJoin(SprintSeconds, {"statusId"}, #"Status Lookup", {"statusId"}, "StatusRow", JoinKind.LeftOuter),
    Expanded = Table.ExpandTableColumn(Merged, "StatusRow", {"status"}, {"Sprint Status"}),
    // Helpers no longer needed
    Dropped = Table.RemoveColumns(Expanded, {"Lap Min", "Lap Sec", "milliseconds", "statusId"}),
    // Title Case business names, matching the rest of the model
    Renamed = Table.RenameColumns(Dropped, {{"raceId", "Race Id"}, {"driverId", "Driver Id"}, {"constructorId", "Constructor Id"}, {"grid", "Sprint Grid"}, {"positionOrder", "Sprint Position"}, {"points", "Sprint Points"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Race Id", Int64.Type}, {"Driver Id", Int64.Type}, {"Constructor Id", Int64.Type}, {"Sprint Grid", Int64.Type}, {"Sprint Position", Int64.Type}, {"Sprint Points", type number}, {"Sprint Fastest Lap Sec", type number}, {"Sprint Seconds", type number}, {"Sprint Status", type text}}),
    Sorted = Table.Sort(Typed, {{"Race Id", Order.Ascending}, {"Sprint Position", Order.Ascending}})
in
    Sorted
