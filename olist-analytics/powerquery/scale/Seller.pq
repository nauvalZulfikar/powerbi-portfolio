let
    Source = Sql.Database(SqlEndpoint, LakehouseDb),
    Data = Source{[Schema = "dbo", Item = "dim_seller"]}[Data],
    // folds -> SELECT only these columns
    #"Chose columns" = Table.SelectColumns(Data, {"seller_id", "seller_code", "state", "region", "pareto_tier"}),
    // folds -> CAST per column
    Typed = Table.TransformColumnTypes(#"Chose columns", {{"seller_id", type text}, {"seller_code", type text}, {"state", type text}, {"region", type text}, {"pareto_tier", type text}})
in
    Typed
