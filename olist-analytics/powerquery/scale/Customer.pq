let
    Source = Sql.Database(SqlEndpoint, LakehouseDb),
    Data = Source{[Schema = "dbo", Item = "dim_customer"]}[Data],
    // folds -> SELECT only these columns
    #"Chose columns" = Table.SelectColumns(Data, {"customer_unique_id", "state", "region", "city"}),
    // folds -> CAST per column
    Typed = Table.TransformColumnTypes(#"Chose columns", {{"customer_unique_id", type text}, {"state", type text}, {"region", type text}, {"city", type text}})
in
    Typed
