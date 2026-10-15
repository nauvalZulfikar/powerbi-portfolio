let
    Source = Parquet.Document(File.Contents(DataFolder & "cohort_retention.parquet")),
    Typed = Table.TransformColumnTypes(Source, {{"cohort_month", type date}, {"cohort_label", type text}, {"cohort_size", Int64.Type}, {"month_offset", Int64.Type}, {"month_offset_label", type text}, {"active_customers", Int64.Type}, {"retention_rate", type number}})
in
    Typed
