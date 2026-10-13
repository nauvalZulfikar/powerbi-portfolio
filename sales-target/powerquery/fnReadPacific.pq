// Pacific: a system export. Order and line share one cell ("SO51721/3"), dates are Excel
// serial numbers, reps are Windows logins, and the last row is a TOTAL.
(content as binary) as table =>
    let
        Sheet = Excel.Workbook(content, true, true){[Item = "Export", Kind = "Sheet"]}[Data],
        Numbered = Table.AddIndexColumn(Sheet, "Row", 2, 1, Int64.Type),
        Split = Table.SplitColumn(Numbered, "Order/Line",
            Splitter.SplitTextByDelimiter("/", QuoteStyle.None), {"Order Number", "Order Line"}),
        Picked = Table.SelectColumns(Split,
            {"Order Number", "Order Line", "Date", "Ship Date", "Customer", "Territory", "Rep", "SKU", "Qty", "Unit Price", "Discount", "Amount", "Row"}),
        Renamed = Table.RenameColumns(Picked, {
            {"Date", "Order Date"}, {"Customer", "Reseller Code"}, {"Rep", "Rep Name"}, {"SKU", "Product Code"}, {"Qty", "Quantity"}}),
        Dated = Table.TransformColumns(Renamed, {{"Order Date", each try Date.From(_) otherwise null},
            {"Ship Date", each try Date.From(_) otherwise null}}),
        Tagged = Table.AddColumn(Dated, "Sheet Issue", each
            if [Order Number] = "TOTAL" then "Subtotal row" else null, type nullable text)
    in
        Table.AddColumn(Tagged, "Sheet", each "Export", type text)
