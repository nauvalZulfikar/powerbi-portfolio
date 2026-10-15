let
    Source = Table.NestedJoin(#"Plan Raw", {"Category Group"}, #"Plan Groups", {"Category Group"}, "Match", JoinKind.LeftAnti)
in
    Source
