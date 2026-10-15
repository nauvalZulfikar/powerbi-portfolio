let
    Source = Parquet.Document(File.Contents(DataFolder & "dim_product.parquet")),
    // Only the group column (Parquet reads just this)
    #"Kept group" = Table.SelectColumns(Source, {"category_group"}),
    // One row per group
    #"Distinct groups" = Table.Distinct(#"Kept group"),
    // Model name
    Renamed = Table.RenameColumns(#"Distinct groups", {{"category_group", "Category Group"}}),
    // ELEC, FASH, … for compact labels
    #"Group code" = Table.AddColumn(Renamed, "Group Code", each Text.Upper(Text.Start(Text.Remove([Category Group], {" ", ",", "&"}), 4)), type text)
in
    #"Group code"
