let
    Source = Parquet.Document(File.Contents(DataFolder & "dim_customer.parquet")),
    Typed = Table.TransformColumnTypes(Source, {{"customer_unique_id", type text}, {"city", type text}, {"state", type text}, {"state_name", type text}, {"region", type text}, {"state_geo", type text}, {"lat", type number}, {"lng", type number}, {"first_order_date", type date}, {"cohort_month", type date}, {"last_order_date", type date}, {"valid_orders", Int64.Type}, {"lifetime_value", type number}, {"avg_review", type number}, {"recency_days", Int64.Type}, {"r_score", Int64.Type}, {"f_score", Int64.Type}, {"m_score", Int64.Type}, {"rfm_cell", type text}, {"rfm_segment", type text}, {"rfm_segment_order", Int64.Type}, {"predicted_clv_12m", type number}})
in
    Typed
