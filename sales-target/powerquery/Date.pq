// One row per day from the first order month to the end of the last order or target month.
// The fiscal year starts in month FiscalYearStart (parameter, 7 = July: FY2013 = Jul 2012 - Jun 2013) and
// is named after the calendar year it ends in.
let
    Dates = List.Buffer(Sales[Order Date] & Targets[Date]),
    First = Date.StartOfMonth(List.Min(Dates)),
    Last = Date.EndOfMonth(List.Max(Dates)),
    Days = List.Dates(First, Duration.Days(Last - First) + 1, #duration(1, 0, 0, 0)),
    Base = Table.FromList(Days, Splitter.SplitByNothing(), type table [Date = date]),
    Columns = Table.AddColumn(Base, "Parts", each
        let
            d = [Date],
            fy = Date.Year(d) + (if FiscalYearStart > 1 and Date.Month(d) >= FiscalYearStart then 1 else 0),
            fm = Number.Mod(Date.Month(d) - FiscalYearStart, 12) + 1
        in
            [
                Year = Date.Year(d),
                #"Year Quarter" = Date.Year(d) * 10 + Date.QuarterOfYear(d),
                Quarter = Text.From(Date.Year(d)) & " Q" & Text.From(Date.QuarterOfYear(d)),
                #"Year Month" = Date.Year(d) * 100 + Date.Month(d),
                Month = Date.ToText(d, [Format = "MMM yyyy", Culture = "en-US"]),
                #"Month Start" = Date.StartOfMonth(d),
                #"Month of Year" = Date.Month(d),
                #"Month Name" = Date.ToText(d, [Format = "MMM", Culture = "en-US"]),
                #"Fiscal Year Number" = fy,
                #"Fiscal Year" = "FY" & Text.From(fy),
                #"Fiscal Quarter Number" = fy * 10 + Number.IntegerDivide(fm - 1, 3) + 1,
                #"Fiscal Quarter" = "FY" & Text.From(fy) & " Q" & Text.From(Number.IntegerDivide(fm - 1, 3) + 1),
                #"Fiscal Month Number" = fy * 100 + fm
            ]),
    Expanded = Table.ExpandRecordColumn(Columns, "Parts",
        {"Year", "Year Quarter", "Quarter", "Year Month", "Month", "Month Start", "Month of Year", "Month Name",
         "Fiscal Year Number", "Fiscal Year", "Fiscal Quarter Number", "Fiscal Quarter", "Fiscal Month Number"})
in
    Table.TransformColumnTypes(Expanded, {
        {"Year", Int64.Type}, {"Year Quarter", Int64.Type}, {"Quarter", type text}, {"Year Month", Int64.Type},
        {"Month", type text}, {"Month Start", type date}, {"Month of Year", Int64.Type}, {"Month Name", type text},
        {"Fiscal Year Number", Int64.Type}, {"Fiscal Year", type text}, {"Fiscal Quarter Number", Int64.Type},
        {"Fiscal Quarter", type text}, {"Fiscal Month Number", Int64.Type}})
