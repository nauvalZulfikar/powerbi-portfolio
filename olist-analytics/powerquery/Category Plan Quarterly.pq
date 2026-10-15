let
    Source = #"Plan Raw",
    // Q1 2018, the same text as Date[Year Quarter]
    #"Quarter label" = Table.AddColumn(Source, "Year Quarter", each "Q" & Text.From(Number.RoundUp([Month No] / 3)) & " " & Text.From([Year]), type text),
    // Group By: sum the months (Base + Promo lines) per group and quarter
    #"Grouped by quarter" = Table.Group(#"Quarter label", {"Category Group", "Year", "Year Quarter"}, {{"Plan Revenue", each List.Sum([Plan Revenue]), type nullable number}, {"Plan Months", each List.Count(List.Distinct([Month No])), Int64.Type}})
in
    #"Grouped by quarter"
