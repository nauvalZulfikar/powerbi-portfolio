let
    Source = Parquet.Document(File.Contents(DataFolder & "order_seller.parquet")),
    Typed = Table.TransformColumnTypes(Source, {{"order_id", type text}, {"seller_id", type text}})
in
    Typed
