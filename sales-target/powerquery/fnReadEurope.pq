// Europe: one workbook per quarter, German number and date formats kept as text,
// rep as "LASTNAME, First", item as "code name", blank spacer rows and a Total row.
// The ship date sits in the last column ("Shipped"). The header "Net " carries a trailing space; one quarter has an extra Comment column (ignored).
(content as binary) as table =>
    let
        Sheet = Excel.Workbook(content, true, true){[Item = "Sales", Kind = "Sheet"]}[Data],
        Numbered = Table.AddIndexColumn(Sheet, "Row", 2, 1, Int64.Type),
        Picked = Table.SelectColumns(Numbered,
            {"Order", "Pos", "Date", "Customer", "Sales rep", "Country", "Item", "Qty", "Price", "Disc", "Net ", "Shipped", "Row"}),
        Renamed = Table.RenameColumns(Picked, {
            {"Order", "Order Number"}, {"Pos", "Order Line"}, {"Date", "Order Date"}, {"Customer", "Reseller Code"},
            {"Sales rep", "Rep Name"}, {"Country", "Territory"}, {"Item", "Product Code"}, {"Qty", "Quantity"},
            {"Price", "Unit Price"}, {"Disc", "Discount"}, {"Net ", "Amount"}, {"Shipped", "Ship Date"}}),
        Converted = Table.TransformColumns(Renamed, {
            {"Order Date", each try Date.FromText(_, [Format = "dd.MM.yyyy"]) otherwise null},
            {"Ship Date", each try Date.FromText(_, [Format = "dd.MM.yyyy"]) otherwise null},
            {"Unit Price", each try Number.FromText(_, "de-DE") otherwise null},
            {"Amount", each try Number.FromText(_, "de-DE") otherwise null},
            {"Product Code", each if _ = null then null else Text.BeforeDelimiter(_, " ")}}),
        Tagged = Table.AddColumn(Converted, "Sheet Issue", each
            if [Order Number] = "Total" then "Subtotal row" else null, type nullable text)
    in
        Table.AddColumn(Tagged, "Sheet", each "Sales", type text)
