let
    Source = Parquet.Document(File.Contents(DataFolder & "dim_seller.parquet")),
    Typed = Table.TransformColumnTypes(Source, {{"seller_id", type text}, {"seller_code", type text}, {"city", type text}, {"state", type text}, {"state_name", type text}, {"region", type text}, {"lat", type number}, {"lng", type number}, {"first_sale_date", type date}, {"lifetime_revenue", type number}, {"lifetime_orders", Int64.Type}, {"lifetime_avg_review", type number}, {"lifetime_late_rate", type number}, {"pareto_tier", type text}})
in
    Typed
