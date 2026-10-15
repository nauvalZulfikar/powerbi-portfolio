let
    Source = Sql.Database(SqlEndpoint, LakehouseDb),
    Data = Source{[Schema = "dbo", Item = "dim_product"]}[Data],
    // folds -> SELECT only these columns
    #"Chose columns" = Table.SelectColumns(Data, {"product_id", "category", "category_group"}),
    // folds -> CAST per column
    Typed = Table.TransformColumnTypes(#"Chose columns", {{"product_id", type text}, {"category", type text}, {"category_group", type text}})
in
    Typed
