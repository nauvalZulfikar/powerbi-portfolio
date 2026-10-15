let
    // Read the mart the pipeline wrote
    Data = Parquet.Document(File.Contents(DataFolder & "dim_component.parquet")),
    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these
    Kept = Table.SelectColumns(Data, {"component_key", "team_key", "component"}),
    // Business names
    Renamed = Table.RenameColumns(Kept, {{"component_key", "Component Key"}, {"team_key", "Project"}, {"component", "Component"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Component Key", Int64.Type}, {"Project", type text}, {"Component", type text}})
in
    Typed
