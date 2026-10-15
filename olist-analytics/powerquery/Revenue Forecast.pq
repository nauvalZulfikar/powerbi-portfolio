let
    Source = Parquet.Document(File.Contents(DataFolder & "revenue_forecast.parquet")),
    Typed = Table.TransformColumnTypes(Source, {{"month_start", type date}, {"forecast_revenue", type number}, {"lower_80", type number}, {"upper_80", type number}, {"lower_95", type number}, {"upper_95", type number}})
in
    Typed
