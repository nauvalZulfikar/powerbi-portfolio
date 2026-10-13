// North America branch: every workbook in sales\North America\ read with fnReadNorthAmerica, tagged with branch and file name.
// Folding: none. Folder.Files and Excel.Workbook are not foldable sources, so every step here runs in the
// mashup engine; filtering the folder listing first keeps the engine from opening files it will throw away.
let
    Files = Folder.Files(InboxFolder & "\sales\North America"),
    Workbooks = Table.SelectRows(Files, each [Extension] = ".xlsx" and not Text.StartsWith([Name], "~$")),
    Read = Table.AddColumn(Workbooks, "Rows", each fnReadNorthAmerica([Content])),
    Expanded = Table.ExpandTableColumn(Table.SelectColumns(Read, {"Name", "Rows"}), "Rows",
        {"Sheet", "Row", "Order Number", "Order Line", "Order Date", "Ship Date", "Reseller Code", "Rep Name", "Territory",
         "Product Code", "Quantity", "Unit Price", "Discount", "Amount", "Sheet Issue"}),
    Tagged = Table.AddColumn(Expanded, "Branch", each "North America", type text)
in
    Table.RenameColumns(Tagged, {{"Name", "File"}})
