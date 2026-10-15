let
    Source = Sql.Database(SqlEndpoint, LakehouseDb),
    Data = Source{[Schema = "dbo", Item = "dim_date"]}[Data],
    // folds -> SELECT only these columns
    #"Chose columns" = Table.SelectColumns(Data, {"date", "year", "quarter", "year_quarter", "year_quarter_key", "month_no", "month_name", "year_month", "year_month_key", "month_start"}),
    // folds -> CAST per column
    Typed = Table.TransformColumnTypes(#"Chose columns", {{"date", type date}, {"year", Int64.Type}, {"quarter", Int64.Type}, {"year_quarter", type text}, {"year_quarter_key", Int64.Type}, {"month_no", Int64.Type}, {"month_name", type text}, {"year_month", type text}, {"year_month_key", Int64.Type}, {"month_start", type date}})
in
    Typed
