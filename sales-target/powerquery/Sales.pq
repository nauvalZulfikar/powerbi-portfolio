// Clean order lines only: every row without an Issue. The source file name stays in Sales Staging and the
// Cleaning Log; the fact table keeps only what a report filters or adds up.
let
    Clean = Table.SelectRows(#"Sales Staging", each [Issue] = null),
    Territories = Table.Buffer(Table.SelectColumns(fnReference("Territories"), {"Territory Key", "Region"})),
    WithTerritory = Table.ExpandTableColumn(
        Table.NestedJoin(Clean, "Territory", Territories, "Region", "T", JoinKind.LeftOuter), "T", {"Territory Key"}),
    Kept = Table.SelectColumns(WithTerritory,
        {"Order Number", "Order Line", "Order Date", "Ship Date", "Reseller Code", "Rep Key", "Territory Key", "Product Code",
         "Quantity", "Unit Price", "Discount", "Amount", "Branch"})
in
    Table.TransformColumnTypes(Kept, {
        {"Territory Key", Int64.Type}, {"Quantity", Int64.Type}, {"Unit Price", Currency.Type},
        {"Discount", Percentage.Type}, {"Amount", Currency.Type}})
