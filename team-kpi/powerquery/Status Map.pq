let
    // Read the mart the pipeline wrote
    Data = Parquet.Document(File.Contents(DataFolder & "status_map.parquet")),
    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these
    Kept = Table.SelectColumns(Data, {"team_key", "status", "stage", "tickets"}),
    // Business names
    Renamed = Table.RenameColumns(Kept, {{"team_key", "Team Key"}, {"status", "Jira Status"}, {"stage", "Shared Stage"}, {"tickets", "Tickets Now"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Team Key", type text}, {"Jira Status", type text}, {"Shared Stage", type text}, {"Tickets Now", Int64.Type}})
in
    Typed
