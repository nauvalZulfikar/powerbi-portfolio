let
    // Lap-by-lap timing (1996+), the same mart as Lap Times
    Source = Parquet.Document(File.Contents(DataFolder & "fact_lap_times.parquet")),
    // In- and out-laps distort pace: keep racing laps only
    RacingLaps = Table.SelectRows(Source, each [is_pit_lap] = false),
    // One row per driver per race
    Grouped = Table.Group(RacingLaps, {"race_id", "driver_id"}, {{"Median Lap Sec", each List.Median([lap_seconds]), type number}, {"Laps Timed", each Table.RowCount(_), Int64.Type}}),
    Renamed = Table.RenameColumns(Grouped, {{"race_id", "Race Id"}, {"driver_id", "Driver Id"}}),
    Typed = Table.TransformColumnTypes(Renamed, {{"Race Id", Int64.Type}, {"Driver Id", Int64.Type}})
in
    Typed
