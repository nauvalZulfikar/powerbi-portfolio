let
    Source = Parquet.Document(File.Contents(DataFolder & "freight_lanes.parquet")),
    Typed = Table.TransformColumnTypes(Source, {{"seller_state", type text}, {"customer_state", type text}, {"lane", type text}, {"items", Int64.Type}, {"avg_distance_km", type number}, {"avg_freight", type number}, {"avg_freight_share", type number}, {"avg_delivery_days", type number}, {"late_rate", type number}})
in
    Typed
