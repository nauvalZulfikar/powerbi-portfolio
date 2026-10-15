let
    Source = Parquet.Document(File.Contents(DataFolder & "fact_payments.parquet")),
    Typed = Table.TransformColumnTypes(Source, {{"order_id", type text}, {"payment_sequential", Int64.Type}, {"order_date", type date}, {"is_valid", type logical}, {"payment_type", type text}, {"installments", Int64.Type}, {"installment_band", type text}, {"installment_band_order", Int64.Type}, {"payment_value", type number}})
in
    Typed
