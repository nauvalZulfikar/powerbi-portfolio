// SYNTHETIC: a category-level sales plan. The source only has quota per rep, so this spreads each fiscal
// quarter's total quota (up to the last sale date, as the Quota measure does) over product categories by
// the category mix of the previous fiscal year. The first fiscal year has no history and uses its own mix.
// One row per Category x Fiscal Quarter: it relates many-to-many to Products[Category] and Date[Fiscal Quarter].
let
    LastSale = List.Max(Sales[Order Date]),
    FyOf = (d as date) as number => Date.Year(d) + (if FiscalYearStart > 1 and Date.Month(d) >= FiscalYearStart then 1 else 0),
    FqOf = (d as date) as text =>
        let fm = Number.Mod(Date.Month(d) - FiscalYearStart, 12) + 1
        in "FY" & Text.From(FyOf(d)) & " Q" & Text.From(Number.IntegerDivide(fm - 1, 3) + 1),
    Quota = Table.Group(
        Table.AddColumn(Table.SelectRows(Targets, each [Date] <= LastSale), "Fiscal Quarter", each FqOf([Date]), type text),
        {"Fiscal Quarter"}, {{"Quota", each List.Sum([Target]), type number}}),
    Categories = Table.SelectColumns(Products, {"Product Code", "Category"}),
    SalesByCategory = Table.ExpandTableColumn(
        Table.NestedJoin(Table.SelectColumns(Sales, {"Order Date", "Product Code", "Amount"}), "Product Code",
            Categories, "Product Code", "P", JoinKind.LeftOuter), "P", {"Category"}),
    WithFy = Table.AddColumn(SalesByCategory, "FY", each FyOf([Order Date]), Int64.Type),
    ByFy = Table.Group(WithFy, {"FY", "Category"}, {{"Sales", each List.Sum([Amount]), type number}}),
    FyTotals = Table.Group(ByFy, {"FY"}, {{"Total", each List.Sum([Sales]), type number}}),
    Mix = Table.Buffer(Table.AddColumn(
        Table.ExpandTableColumn(Table.NestedJoin(ByFy, "FY", FyTotals, "FY", "T", JoinKind.Inner), "T", {"Total"}),
        "Share", each [Sales] / [Total], type number)),
    FirstFy = List.Min(Mix[FY]),
    Plan = Table.AddColumn(Quota, "Rows", each
        let
            fy = Number.FromText(Text.Middle([Fiscal Quarter], 2, 4)),
            basis = if List.Contains(Mix[FY], fy - 1) then fy - 1 else FirstFy,
            label = if basis = fy - 1 then "Prior fiscal year mix" else "Same fiscal year mix (no history)",
            q = [Quota]
        in
            Table.FromRecords(List.Transform(Table.ToRecords(Table.SelectRows(Mix, each [FY] = basis)), each
                [Category = [Category], #"Plan Amount" = q * [Share], #"Mix Basis" = label]))),
    Expanded = Table.ExpandTableColumn(Table.SelectColumns(Plan, {"Fiscal Quarter", "Rows"}), "Rows",
        {"Category", "Plan Amount", "Mix Basis"})
in
    Table.TransformColumnTypes(Expanded, {
        {"Fiscal Quarter", type text}, {"Category", type text}, {"Plan Amount", Currency.Type}, {"Mix Basis", type text}})
