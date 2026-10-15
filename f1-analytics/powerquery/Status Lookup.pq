let
    // statusId -> text ("Finished", "+1 Lap", "Engine", …)
    Source = Csv.Document(File.Contents(RawFolder & "status.csv"), [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Typed = Table.TransformColumnTypes(Promoted, {{"statusId", Int64.Type}, {"status", type text}})
in
    Typed
