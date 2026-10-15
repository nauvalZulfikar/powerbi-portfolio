let
    // Read the mart the pipeline wrote
    Data = Parquet.Document(File.Contents(DataFolder & "fact_stage_time.parquet")),
    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these
    Kept = Table.SelectColumns(Data, {"ticket_key", "stage_key", "days"}),
    // Business names
    Renamed = Table.RenameColumns(Kept, {{"ticket_key", "Ticket Key"}, {"stage_key", "Stage Key"}, {"days", "Days"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Ticket Key", type text}, {"Stage Key", Int64.Type}, {"Days", type number}}),
    // Drop zero-length stage intervals (status changed and changed back in the same second)
    NonZero = Table.SelectRows(Typed, each [Days] > 0)
in
    NonZero
