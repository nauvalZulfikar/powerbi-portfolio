let
    Source = Parquet.Document(File.Contents(DataFolder & "fact_backlog_daily.parquet")),
    Typed = Table.TransformColumnTypes(Source, {{"date", type date}, {"in_transit", Int64.Type}, {"purchased_today", Int64.Type}})
in
    Typed
