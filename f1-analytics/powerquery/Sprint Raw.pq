let
    // The raw Ergast file: sprint results are not in the Parquet marts, Power Query shapes them
    Source = Csv.Document(File.Contents(RawFolder & "sprint_results.csv"), [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
    // First row holds the column names
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    // Ergast writes a missing value as \N: make it a real null in every column
    NullMarkers = Table.ReplaceValue(Promoted, "\N", null, Replacer.ReplaceValue, Table.ColumnNames(Promoted))
in
    NullMarkers
