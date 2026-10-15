let
    // Read the mart the pipeline wrote
    Data = Parquet.Document(File.Contents(DataFolder & "fact_backlog_daily.parquet")),
    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these
    Kept = Table.SelectColumns(Data, {"date", "team_key", "priority_key", "stage_key", "open_tickets"}),
    // Business names
    Renamed = Table.RenameColumns(Kept, {{"date", "Date"}, {"team_key", "Team Key"}, {"priority_key", "Priority Key"}, {"stage_key", "Stage Key"}, {"open_tickets", "Open Count"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Date", type date}, {"Team Key", type text}, {"Priority Key", Int64.Type}, {"Stage Key", Int64.Type}, {"Open Count", Int64.Type}}),
    // Nothing after the as-of date (parameter AsOfDate)
    UpToAsOf = Table.SelectRows(Typed, each [Date] <= Date.From(AsOfDate)),
    // Incremental refresh: the Service swaps RangeStart/RangeEnd per partition (>= start, < end)
    InRange = Table.SelectRows(UpToAsOf, each DateTime.From([Date]) >= RangeStart and DateTime.From([Date]) < RangeEnd)
in
    InRange
