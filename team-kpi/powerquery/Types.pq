let
    // Read the mart the pipeline wrote
    Data = Parquet.Document(File.Contents(DataFolder & "dim_type.parquet")),
    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these
    Kept = Table.SelectColumns(Data, {"type_key", "type"}),
    // Business names
    Renamed = Table.RenameColumns(Kept, {{"type_key", "Type Key"}, {"type", "Type"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Type Key", Int64.Type}, {"Type", type text}})
in
    Typed
