let
    // Read the mart the pipeline wrote
    Data = Parquet.Document(File.Contents(DataFolder & "fact_stage_transition.parquet")),
    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these
    Kept = Table.SelectColumns(Data, {"ticket_key", "from_stage_key", "to_stage_key", "transition_date"}),
    // Business names
    Renamed = Table.RenameColumns(Kept, {{"ticket_key", "Ticket Key"}, {"from_stage_key", "From Stage Key"}, {"to_stage_key", "To Stage Key"}, {"transition_date", "Transition Date"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Ticket Key", type text}, {"From Stage Key", Int64.Type}, {"To Stage Key", Int64.Type}, {"Transition Date", type date}})
in
    Typed
