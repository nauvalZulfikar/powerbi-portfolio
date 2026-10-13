let
    Source = fnReference("Territories")
in
    Table.TransformColumnTypes(Source, {
        {"Territory Key", Int64.Type}, {"Region", type text}, {"Country", type text}, {"Group", type text}, {"Branch", type text}})
