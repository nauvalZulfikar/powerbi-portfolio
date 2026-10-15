let
    // Append: same columns in both years
    Source = Table.Combine({#"Plan 2017", #"Plan 2018"})
in
    Source
