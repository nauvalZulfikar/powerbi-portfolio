let
    Source = #"Plan Raw",
    // Merge (like a VLOOKUP) to the product groups: left outer join on Category Group
    #"Merged group code" = Table.NestedJoin(Source, {"Category Group"}, #"Plan Groups", {"Category Group"}, "Group", JoinKind.LeftOuter),
    // Bring back the code
    #"Expanded group code" = Table.ExpandTableColumn(#"Merged group code", "Group", {"Group Code"}),
    // First day of the month: the key to Date[Month Start]
    #"Month start" = Table.AddColumn(#"Expanded group code", "Month Start", each #date([Year], [Month No], 1), type date),
    // Conditional column: November is Black Friday
    #"Is peak month" = Table.AddColumn(#"Month start", "Is Peak Month", each [Month No] = 11, type logical),
    // Index column, handy when checking against the sheet (last: an index stops folding)
    #"Plan row" = Table.AddIndexColumn(#"Is peak month", "Plan Row", 1, 1, Int64.Type),
    // Final types
    #"Changed type" = Table.TransformColumnTypes(#"Plan row", {{"Plan Revenue", type number}, {"Group Code", type text}})
in
    #"Changed type"
