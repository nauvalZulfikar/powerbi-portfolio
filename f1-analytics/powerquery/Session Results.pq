let
    // Grand Prix results: the same mart as the Results table
    RaceSource = Parquet.Document(File.Contents(DataFolder & "fact_results.parquet")),
    RaceKept = Table.SelectColumns(RaceSource, {"race_id", "driver_id", "constructor_id", "position_order", "points", "status"}),
    RaceRenamed = Table.RenameColumns(RaceKept, {{"race_id", "Race Id"}, {"driver_id", "Driver Id"}, {"constructor_id", "Constructor Id"}, {"position_order", "Session Position"}, {"points", "Session Points"}, {"status", "Session Status"}}),
    RaceRows = Table.AddColumn(RaceRenamed, "Session", each "Race", type text),
    // Sprint results (from 2021), renamed to the same columns
    SprintRows = Table.AddColumn(Table.RenameColumns(Table.SelectColumns(#"Sprint Results", {"Race Id", "Driver Id", "Constructor Id", "Sprint Position", "Sprint Points", "Sprint Status"}), {{"Sprint Position", "Session Position"}, {"Sprint Points", "Session Points"}, {"Sprint Status", "Session Status"}}), "Session", each "Sprint", type text),
    // Append: one row per driver per session, so sprint points count in the championship
    Combined = Table.Combine({RaceRows, SprintRows}),
    Typed = Table.TransformColumnTypes(Combined, {{"Race Id", Int64.Type}, {"Driver Id", Int64.Type}, {"Constructor Id", Int64.Type}, {"Session Position", Int64.Type}, {"Session Points", type number}, {"Session Status", type text}, {"Session", type text}})
in
    Typed
