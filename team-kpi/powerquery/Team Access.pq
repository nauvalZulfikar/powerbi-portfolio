let
    // Read the mart the pipeline wrote
    Data = Parquet.Document(File.Contents(DataFolder & "team_access.parquet")),
    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these
    Kept = Table.SelectColumns(Data, {"email", "team_key"}),
    // Business names
    Renamed = Table.RenameColumns(Kept, {{"email", "Email"}, {"team_key", "Team Key"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Email", type text}, {"Team Key", type text}}),
    // One extra login for testing the Team Lead role in the Service (parameter TestUpn)
    TestUser = #table(type table [Email = text, #"Team Key" = text], {{TestUpn, "HBASE"}}),
    // Append: the seed rows + the test login
    Combined = Table.Combine({Typed, TestUser})
in
    Combined
