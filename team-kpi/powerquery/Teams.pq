let
    // Read the mart the pipeline wrote
    Data = Parquet.Document(File.Contents(DataFolder & "dim_team.parquet")),
    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these
    Kept = Table.SelectColumns(Data, {"team_key", "team", "product"}),
    // Business names
    Renamed = Table.RenameColumns(Kept, {{"team_key", "Team Key"}, {"team", "Team"}, {"product", "Product"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Team Key", type text}, {"Team", type text}, {"Product", type text}})
in
    Typed
