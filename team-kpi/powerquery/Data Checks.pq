let
    // Read the mart the pipeline wrote
    Data = Parquet.Document(File.Contents(DataFolder & "data_checks.parquet")),
    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these
    Kept = Table.SelectColumns(Data, {"team_key", "step", "check", "tickets"}),
    // Business names
    Renamed = Table.RenameColumns(Kept, {{"team_key", "Team Key"}, {"step", "Step"}, {"check", "Check"}, {"tickets", "Tickets"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Team Key", type text}, {"Step", Int64.Type}, {"Check", type text}, {"Tickets", Int64.Type}})
in
    Typed
