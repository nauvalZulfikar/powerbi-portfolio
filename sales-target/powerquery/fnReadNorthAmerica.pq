// North America: one workbook per year, one sheet per month.
// Title block on rows 1-3, header on row 4, a "Month total" row at the bottom, a Notes sheet,
// and sometimes a pasted copy of a month ("Mar 2012 (2)"). The order date is typed only on the
// first line of each order, so it is filled down within the sheet before anything else.
(content as binary) as table =>
    let
        Book = Excel.Workbook(content, null, true),
        Months = Table.SelectRows(Book, each [Kind] = "Sheet" and [Name] <> "Notes"),
        Read = Table.AddColumn(Months, "Rows", each
            let
                Body = Table.PromoteHeaders(Table.Skip([Data], 3), [PromoteAllScalars = true]),
                Filled = Table.FillDown(Body, {"Order Date"}),
                Numbered = Table.AddIndexColumn(Filled, "Row", 5, 1, Int64.Type)
            in
                Numbered),
        Expanded = Table.ExpandTableColumn(Table.SelectColumns(Read, {"Name", "Rows"}), "Rows",
            {"Order #", "Line", "Order Date", "Ship Date", "Customer No", "Sales Rep", "Territory", "Product Code", "Qty", "Unit Price", "Discount", "Line Total", "Row"},
            {"Order Number", "Order Line", "Order Date", "Ship Date", "Reseller Code", "Rep Name", "Territory", "Product Code", "Quantity", "Unit Price", "Discount", "Amount", "Row"}),
        Dated = Table.TransformColumns(Expanded, {{"Order Date", each try Date.From(_) otherwise null},
            {"Ship Date", each try Date.From(_) otherwise null}}),
        Tagged = Table.AddColumn(Dated, "Sheet Issue", each
            if Text.EndsWith([Name], ")") then "Duplicate sheet"
            else if [Order Number] = "Month total" then "Subtotal row"
            else null, type nullable text)
    in
        Table.RenameColumns(Tagged, {{"Name", "Sheet"}})
