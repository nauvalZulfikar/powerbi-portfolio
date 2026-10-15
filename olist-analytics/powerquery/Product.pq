let
    Source = Parquet.Document(File.Contents(DataFolder & "dim_product.parquet")),
    Typed = Table.TransformColumnTypes(Source, {{"product_id", type text}, {"category", type text}, {"category_group", type text}, {"photos_qty", Int64.Type}, {"weight_g", Int64.Type}, {"volume_cm3", Int64.Type}, {"weight_band", type text}, {"weight_band_order", Int64.Type}})
in
    Typed
