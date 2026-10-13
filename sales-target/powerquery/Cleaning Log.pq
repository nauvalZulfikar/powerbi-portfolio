// What happened to every source row, per file: loaded, or which rule set it aside.
let
    Tagged = Table.AddColumn(#"Sales Staging", "Outcome", each if [Issue] = null then "Loaded" else [Issue], type text),
    Grouped = Table.Group(Tagged, {"Branch", "File", "Outcome"}, {
        {"Rows", each Table.RowCount(_), Int64.Type},
        {"Amount", each List.Sum([Amount]), Currency.Type}})
in
    Table.Sort(Grouped, {{"Branch", Order.Ascending}, {"File", Order.Ascending}, {"Outcome", Order.Ascending}})
