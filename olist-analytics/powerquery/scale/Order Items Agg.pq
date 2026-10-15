let
    Source = Sql.Database(SqlEndpoint, LakehouseDb),
    Items = Source{[Schema = "dbo", Item = "fact_order_items"]}[Data],
    Products = Table.SelectColumns(Source{[Schema = "dbo", Item = "dim_product"]}[Data], {"product_id", "category"}),
    Customers = Table.SelectColumns(Source{[Schema = "dbo", Item = "dim_customer"]}[Data], {"customer_unique_id", "state"}),
    // folds -> LEFT JOIN dim_product
    #"Joined product" = Table.ExpandTableColumn(Table.NestedJoin(Items, {"product_id"}, Products, {"product_id"}, "p", JoinKind.LeftOuter), "p", {"category"}),
    // folds -> LEFT JOIN dim_customer
    #"Joined customer" = Table.ExpandTableColumn(Table.NestedJoin(#"Joined product", {"customer_unique_id"}, Customers, {"customer_unique_id"}, "c", JoinKind.LeftOuter), "c", {"state"}, {"customer_state"}),
    // folds -> GROUP BY day, category, seller, customer state, validity
    Grouped = Table.Group(#"Joined customer", {"order_date", "category", "seller_id", "customer_state", "is_valid"}, {{"price", each List.Sum([price]), type number}, {"freight", each List.Sum([freight]), type number}, {"items", each Table.RowCount(_), Int64.Type}}),
    Typed = Table.TransformColumnTypes(Grouped, {{"order_date", type date}, {"is_valid", type logical}})
in
    Typed
