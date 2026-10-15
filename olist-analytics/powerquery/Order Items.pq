let
    Source = Parquet.Document(File.Contents(DataFolder & "fact_order_items.parquet")),
    Typed = Table.TransformColumnTypes(Source, {{"order_id", type text}, {"order_item_id", Int64.Type}, {"product_id", type text}, {"seller_id", type text}, {"customer_unique_id", type text}, {"order_date", type date}, {"is_valid", type logical}, {"is_late", type logical}, {"review_score", Int64.Type}, {"price", type number}, {"freight", type number}, {"freight_share", type number}, {"distance_km", type number}, {"distance_band", type text}, {"distance_band_order", Int64.Type}})
in
    Typed
