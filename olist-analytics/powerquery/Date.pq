let
    Source = Parquet.Document(File.Contents(DataFolder & "dim_date.parquet")),
    Typed = Table.TransformColumnTypes(Source, {{"date", type date}, {"year", Int64.Type}, {"quarter", Int64.Type}, {"year_quarter", type text}, {"year_quarter_key", Int64.Type}, {"month_no", Int64.Type}, {"month_name", type text}, {"year_month", type text}, {"year_month_key", Int64.Type}, {"month_start", type date}, {"weekday_no", Int64.Type}, {"weekday_name", type text}, {"is_weekend", type logical}, {"iso_week", Int64.Type}})
in
    Typed
