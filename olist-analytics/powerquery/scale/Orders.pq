let
    Source = Sql.Database(SqlEndpoint, LakehouseDb),
    Data = Source{[Schema = "dbo", Item = "fact_orders"]}[Data],
    // folds -> WHERE purchased_at >= @RangeStart AND purchased_at < @RangeEnd (filter first)
    #"Filtered purchase window" = Table.SelectRows(Data, each [purchased_at] >= RangeStart and [purchased_at] < RangeEnd),
    // folds -> SELECT only these columns
    #"Chose columns" = Table.SelectColumns(#"Filtered purchase window", {"order_id", "customer_unique_id", "order_date", "purchased_at", "is_valid", "is_delivered", "is_late", "review_score", "delivery_days"}),
    // folds -> CAST per column
    Typed = Table.TransformColumnTypes(#"Chose columns", {{"order_id", type text}, {"customer_unique_id", type text}, {"order_date", type date}, {"purchased_at", type datetime}, {"is_valid", type logical}, {"is_delivered", type logical}, {"is_late", type logical}, {"review_score", Int64.Type}, {"delivery_days", type number}})
in
    Typed
