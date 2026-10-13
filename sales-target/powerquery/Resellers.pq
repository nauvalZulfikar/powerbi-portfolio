// Two resellers share a name, so the code is the key. City and country come from DimGeography (for the map).
let
    Source = fnReference("Resellers")
in
    Table.TransformColumnTypes(Source, {
        {"Reseller Key", Int64.Type}, {"Reseller Code", type text}, {"Reseller", type text}, {"Business Type", type text},
        {"City", type text}, {"Country", type text}})
