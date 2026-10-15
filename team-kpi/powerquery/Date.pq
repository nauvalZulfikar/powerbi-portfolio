let
    // Read the mart the pipeline wrote
    Data = Parquet.Document(File.Contents(DataFolder & "dim_date.parquet")),
    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these
    Kept = Table.SelectColumns(Data, {"date", "year", "year_quarter", "quarter", "year_month", "month", "month_of_year", "month_name", "iso_year", "year_week", "week", "week_of_year", "week_start", "is_weekend"}),
    // Business names
    Renamed = Table.RenameColumns(Kept, {{"date", "Date"}, {"year", "Year"}, {"year_quarter", "Year Quarter"}, {"quarter", "Quarter"}, {"year_month", "Year Month"}, {"month", "Month"}, {"month_of_year", "Month of Year"}, {"month_name", "Month Name"}, {"iso_year", "ISO Year"}, {"year_week", "Year Week"}, {"week", "Week"}, {"week_of_year", "Week of Year"}, {"week_start", "Week Start"}, {"is_weekend", "Is Weekend"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Date", type date}, {"Year", Int64.Type}, {"Year Quarter", Int64.Type}, {"Quarter", type text}, {"Year Month", Int64.Type}, {"Month", type text}, {"Month of Year", Int64.Type}, {"Month Name", type text}, {"ISO Year", Int64.Type}, {"Year Week", Int64.Type}, {"Week", type text}, {"Week of Year", Int64.Type}, {"Week Start", type date}, {"Is Weekend", type logical}})
in
    Typed
