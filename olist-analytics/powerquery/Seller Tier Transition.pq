let
    Source = Parquet.Document(File.Contents(DataFolder & "seller_tier_transition.parquet")),
    Typed = Table.TransformColumnTypes(Source, {{"tier_2017", type text}, {"tier_2018", type text}, {"tier_2017_order", Int64.Type}, {"tier_2018_order", Int64.Type}, {"sellers", Int64.Type}})
in
    Typed
