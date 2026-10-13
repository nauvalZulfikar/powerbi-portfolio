// Every row of every branch file in one schema, with the reason it can't be loaded (Issue).
// Not loaded to the model: "Sales" keeps the clean rows, "Cleaning Log" counts the rest.
// Folding: no step folds. Every source is an Excel file (Folder.Files + Excel.Workbook), which has no query
// engine to push work to, so the order of steps is chosen for the mashup engine instead: cheap per-row
// conversions first, Table.Buffer on the small lookups (Aliases, Products) so each is read once, and one
// buffered, sorted copy for the duplicate check. Were the branches a SQL source, the type conversions and
// the joins would fold and the Table.Buffer calls would have to go, since a buffer stops folding.
let
    // One query per branch (NA Rows / EU Rows / Pacific Rows), appended with an explicit Table.Combine:
    // a branch that changes its layout breaks only its own query and the rest still refresh.
    Combined = Table.Combine({#"NA Rows", #"EU Rows", #"Pacific Rows"}),
    // Values typed by hand in Excel arrive as text ("3", " 12"); anything that won't convert becomes null.
    Typed = Table.TransformColumns(Combined, {
        {"Order Line", each try Int64.From(_) otherwise null, Int64.Type},
        {"Quantity", each try Number.From(_) otherwise null, type nullable number},
        {"Unit Price", each try Number.From(_) otherwise null, type nullable number},
        {"Discount", each try Number.From(_) otherwise null, type nullable number},
        {"Amount", each try Number.From(_) otherwise null, type nullable number}}),
    Blank = Table.AddColumn(Typed, "Is Blank", each [Order Number] = null or Text.Trim(Text.From([Order Number])) = "", type logical),
    // Reps are matched through the alias list, so nicknames, "LAST, First" and logins all resolve.
    Aliases = Table.Buffer(Table.TransformColumns(fnReference("Aliases"), {{"Alias", fnKey}})),
    RepJoined = Table.NestedJoin(Table.AddColumn(Blank, "Rep Lookup", each fnKey([Rep Name])), "Rep Lookup",
        Aliases, "Alias", "Alias Match", JoinKind.LeftOuter),
    WithRep = Table.ExpandTableColumn(RepJoined, "Alias Match", {"Rep Key"}),
    Products = Table.Buffer(Table.SelectColumns(fnReference("Products"), {"Product Code"})),
    ProductJoined = Table.NestedJoin(WithRep, "Product Code", Table.RenameColumns(Products, {{"Product Code", "Known Product"}}),
        "Known Product", "Product Match", JoinKind.LeftOuter),
    WithProduct = Table.AddColumn(ProductJoined, "Product Known", each not Table.IsEmpty([Product Match]), type logical),
    FirstIssue = Table.AddColumn(WithProduct, "Row Issue", each
        if [Sheet Issue] <> null then [Sheet Issue]
        else if [Is Blank] then "Blank row"
        else if [Order Date] = null or [Amount] = null or [Quantity] = null or [Order Line] = null then "Bad value"
        else if [Rep Key] = null then "Unknown rep"
        else if not [Product Known] then "Unknown product"
        else null, type nullable text),
    // A line sent twice (the Pacific resend overlaps July-August 2013): the first file by name wins.
    Ordered = Table.Buffer(Table.AddIndexColumn(
        Table.Sort(FirstIssue, {{"Branch", Order.Ascending}, {"File", Order.Ascending}, {"Sheet", Order.Ascending}, {"Row", Order.Ascending}}),
        "Seq", 1, 1, Int64.Type)),
    Firsts = Table.Group(Table.SelectRows(Ordered, each [Row Issue] = null), {"Order Number", "Order Line"},
        {{"First Seq", each List.Min([Seq]), Int64.Type}}),
    Marked = Table.ExpandTableColumn(
        Table.NestedJoin(Ordered, {"Order Number", "Order Line"}, Firsts, {"Order Number", "Order Line"}, "First", JoinKind.LeftOuter),
        "First", {"First Seq"}),
    WithIssue = Table.AddColumn(Marked, "Issue", each
        if [Row Issue] <> null then [Row Issue]
        else if [Seq] <> [First Seq] then "Duplicate line"
        else null, type nullable text),
    Result = Table.SelectColumns(WithIssue,
        {"Branch", "File", "Sheet", "Row", "Order Number", "Order Line", "Order Date", "Ship Date", "Reseller Code", "Rep Name", "Rep Key",
         "Territory", "Product Code", "Quantity", "Unit Price", "Discount", "Amount", "Issue"}),
    Final = Table.TransformColumnTypes(Result, {
        {"Branch", type text}, {"File", type text}, {"Sheet", type text}, {"Order Number", type text},
        {"Order Date", type date}, {"Ship Date", type nullable date}, {"Reseller Code", type text}, {"Rep Name", type text}, {"Rep Key", Int64.Type},
        {"Territory", type text}, {"Product Code", type text}, {"Issue", type nullable text}})
in
    Final
