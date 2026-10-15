let
    Source = Sql.Database(SqlEndpoint, LakehouseDb),
    Data = Source{[Schema = "dbo", Item = "fact_order_items"]}[Data],
    // folds -> SELECT only these columns
    #"Chose columns" = Table.SelectColumns(Data, {"order_id", "product_id", "seller_id", "customer_unique_id", "order_date", "is_valid", "is_late", "price", "freight"}),
    // folds -> CAST per column
    Typed = Table.TransformColumnTypes(#"Chose columns", {{"order_id", type text}, {"product_id", type text}, {"seller_id", type text}, {"customer_unique_id", type text}, {"order_date", type date}, {"is_valid", type logical}, {"is_late", type logical}, {"price", type number}, {"freight", type number}})
in
    Typed
