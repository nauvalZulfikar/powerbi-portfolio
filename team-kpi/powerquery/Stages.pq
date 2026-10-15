let
    // Read the mart the pipeline wrote
    Data = Parquet.Document(File.Contents(DataFolder & "dim_stage.parquet")),
    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these
    Kept = Table.SelectColumns(Data, {"stage_key", "stage", "is_open"}),
    // Business names
    Renamed = Table.RenameColumns(Kept, {{"stage_key", "Stage Key"}, {"stage", "Stage"}, {"is_open", "Is Open"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Stage Key", Int64.Type}, {"Stage", type text}, {"Is Open", type logical}})
in
    Typed
