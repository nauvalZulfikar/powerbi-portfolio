let
    // Fabric variant: the lakehouse SQL endpoint; every step below folds into one SELECT
    Source = Sql.Database(LakehouseSqlEndpoint, "teamkpi_lh"),
    Data = Source{[Schema = "dbo", Item = "fact_backlog_daily"]}[Data],
    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these
    Kept = Table.SelectColumns(Data, {"date", "team_key", "priority_key", "stage_key", "open_tickets"}),
    // Business names
    Renamed = Table.RenameColumns(Kept, {{"date", "Date"}, {"team_key", "Team Key"}, {"priority_key", "Priority Key"}, {"stage_key", "Stage Key"}, {"open_tickets", "Open Count"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Date", type date}, {"Team Key", type text}, {"Priority Key", Int64.Type}, {"Stage Key", Int64.Type}, {"Open Count", Int64.Type}})
in
    Typed
