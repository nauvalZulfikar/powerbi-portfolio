let
    // Read the mart the pipeline wrote
    Data = Parquet.Document(File.Contents(DataFolder & "dim_priority.parquet")),
    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these
    Kept = Table.SelectColumns(Data, {"priority_key", "priority", "response_hours", "resolution_days"}),
    // Business names
    Renamed = Table.RenameColumns(Kept, {{"priority_key", "Priority Key"}, {"priority", "Priority"}, {"response_hours", "Response Target Hours"}, {"resolution_days", "Resolution Target Days"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Priority Key", Int64.Type}, {"Priority", type text}, {"Response Target Hours", Int64.Type}, {"Resolution Target Days", Int64.Type}})
in
    Typed
