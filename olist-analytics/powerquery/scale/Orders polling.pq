let
    Source = Sql.Database(SqlEndpoint, LakehouseDb),
    Data = Source{[Schema = "dbo", Item = "fact_orders"]}[Data],
    // detect data changes: refresh a partition only when its latest purchase moved
    InRange = Table.SelectRows(Data, each [purchased_at] >= RangeStart and [purchased_at] < RangeEnd),
    Latest = List.Max(InRange[purchased_at])
in
    Latest
