let
    // Read the mart the pipeline wrote
    Data = Parquet.Document(File.Contents(DataFolder & "dim_person.parquet")),
    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these
    Kept = Table.SelectColumns(Data, {"person_key", "person", "home_team", "first_delivered", "first_quarter", "first_quarter_key"}),
    // Business names
    Renamed = Table.RenameColumns(Kept, {{"person_key", "Person Key"}, {"person", "Person"}, {"home_team", "Home Team"}, {"first_delivered", "First Delivered"}, {"first_quarter", "First Quarter"}, {"first_quarter_key", "First Quarter Key"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Person Key", Int64.Type}, {"Person", type text}, {"Home Team", type text}, {"First Delivered", type date}, {"First Quarter", type text}, {"First Quarter Key", Int64.Type}})
in
    Typed
