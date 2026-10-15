let
    Source = Parquet.Document(File.Contents(DataFolder & "seller_monthly.parquet")),
    Typed = Table.TransformColumnTypes(Source, {{"seller_id", type text}, {"month_start", type date}, {"orders", Int64.Type}, {"revenue", type number}, {"avg_review", type number}, {"late_rate", type number}})
in
    Typed
