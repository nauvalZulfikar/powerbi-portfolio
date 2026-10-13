let
    Source = fnReference("Products")
in
    Table.TransformColumnTypes(Source, {{"Product Code", type text}, {"Product", type text}, {"Subcategory", type text}, {"Category", type text},
        {"Standard Cost", type nullable number}})
