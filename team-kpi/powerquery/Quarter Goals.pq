let
    // An ops-team workbook (synthetic goals): a title row and a blank row sit above the header
    Book = Excel.Workbook(File.Contents(SeedFolder & "quarter_goals.xlsx"), null, true),
    // Navigate to the Goals sheet
    Sheet = Book{[Item = "Goals", Kind = "Sheet"]}[Data],
    // Remove the title row and the blank row
    NoTitle = Table.Skip(Sheet, 2),
    // The third row holds the column names
    Headers = Table.PromoteHeaders(NoTitle, [PromoteAllScalars = true]),
    // Drop the blank rows between team blocks
    NoBlankRows = Table.SelectRows(Headers, each [Work Type] <> null),
    // The team is only written on the first row of its block: fill it down
    TeamFilled = Table.FillDown(NoBlankRows, {"Team"}),
    // One column per quarter -> one row per team, work type and quarter
    Unpivoted = Table.UnpivotOtherColumns(TeamFilled, {"Team", "Work Type"}, "Quarter", "Goal"),
    // Q3 2026 -> 20263, the Year Quarter key on Date
    QuarterKey = Table.AddColumn(Unpivoted, "Year Quarter", each Number.FromText(Text.End([Quarter], 4)) * 10 + Number.FromText(Text.Middle([Quarter], 1, 1)), Int64.Type),
    // Group By: bug + other goals per team and quarter
    PerTeamQuarter = Table.Group(QuarterKey, {"Team", "Year Quarter"}, {{"Delivery Goal", each List.Sum([Goal]), type number}}),
    // A row number, handy when checking against the sheet
    Indexed = Table.AddIndexColumn(PerTeamQuarter, "Goal Row", 1, 1, Int64.Type),
    // Model name for the team column
    Renamed = Table.RenameColumns(Indexed, {{"Team", "Team Key"}}),
    // Types last
    Typed = Table.TransformColumnTypes(Renamed, {{"Team Key", type text}, {"Delivery Goal", Int64.Type}})
in
    Typed
