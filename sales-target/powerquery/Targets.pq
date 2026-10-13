// Quarterly quota per rep (USD '000, wide) -> one row per rep per day, in dollars.
// Spreading a quarter evenly over its days lets targets sit on the same Date table as sales,
// so month-to-date and year-to-date targets need no special DAX.
let
    Book = Excel.Workbook(File.Contents(InboxFolder & "\targets\Sales quota 2011-2013.xlsx"), null, true),
    Sheet = Book{[Item = "Quota", Kind = "Sheet"]}[Data],
    Body = Table.PromoteHeaders(Table.Skip(Sheet, 2), [PromoteAllScalars = true]),
    Reps = Table.SelectRows(Body, each [Rep] <> null and [Rep] <> "Total"),
    QuarterColumns = List.Select(Table.ColumnNames(Reps), each Text.Length(_) = 7 and Text.Middle(_, 4, 2) = " Q"),
    Long = Table.UnpivotOtherColumns(Table.SelectColumns(Reps, {"Rep"} & QuarterColumns), {"Rep"}, "Quarter", "Quota '000"),
    // Some cells were typed as text with thousands separators ("1,234.000").
    Numbers = Table.AddColumn(Long, "Quota", each
        1000 * (if [#"Quota '000"] is text then Number.FromText([#"Quota '000"], "en-US") else [#"Quota '000"]), type number),
    Aliases = Table.Buffer(Table.TransformColumns(fnReference("Aliases"), {{"Alias", fnKey}})),
    WithRep = Table.ExpandTableColumn(
        Table.NestedJoin(Table.AddColumn(Numbers, "Rep Lookup", each fnKey([Rep])), "Rep Lookup", Aliases, "Alias", "A", JoinKind.LeftOuter),
        "A", {"Rep Key"}),
    Starts = Table.AddColumn(WithRep, "Quarter Start", each
        #date(Number.FromText(Text.Start([Quarter], 4)), 3 * Number.FromText(Text.End([Quarter], 1)) - 2, 1), type date),
    Days = Table.AddColumn(Starts, "Date", each
        let n = Duration.Days(Date.EndOfQuarter([Quarter Start]) - [Quarter Start]) + 1
        in List.Dates([Quarter Start], n, #duration(1, 0, 0, 0))),
    PerDay = Table.AddColumn(Days, "Day Count", each List.Count([Date]), Int64.Type),
    Expanded = Table.ExpandListColumn(PerDay, "Date"),
    Daily = Table.AddColumn(Expanded, "Target", each [Quota] / [Day Count], Currency.Type)
in
    Table.TransformColumnTypes(Table.SelectColumns(Daily, {"Rep Key", "Date", "Quarter", "Target"}),
        {{"Rep Key", Int64.Type}, {"Date", type date}, {"Quarter", type text}})
