(path as text) as table =>
    let
        Source = Csv.Document(File.Contents(path), [Delimiter = ",", Columns = 16, Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
        // Two title rows sit above the header
        #"Skip title rows" = Table.Skip(Source, 2),
        // The third row holds the column names
        #"Promoted headers" = Table.PromoteHeaders(#"Skip title rows", [PromoteAllScalars = true]),
        // Empty cells arrive as ""; Fill Down only fills nulls
        #"Blank group to null" = Table.ReplaceValue(#"Promoted headers", "", null, Replacer.ReplaceValue, {"Category Group"}),
        // The group is written once per block (merged cells in the sheet): fill it down
        #"Filled down group" = Table.FillDown(#"Blank group to null", {"Category Group"}),
        // The TOTAL row is a sum of the rows above; keep only the lines
        #"Removed TOTAL row" = Table.SelectRows(#"Filled down group", each [Category Group] <> "TOTAL"),
        // One owner typed the Portuguese 'Casa' for Home
        #"Replaced Casa typo" = Table.ReplaceValue(#"Removed TOTAL row", "Casa", "Home", Replacer.ReplaceText, {"Category Group"}),
        // Stray spaces and lower case: Text.Trim + Text.Proper
        #"Trimmed and proper case" = Table.TransformColumns(#"Replaced Casa typo", {{"Category Group", each Text.Proper(Text.Trim(_)), type text}}),
        // Drop the free-text Notes and Owner columns
        #"Kept plan columns" = Table.SelectColumns(#"Trimmed and proper case", List.RemoveItems(Table.ColumnNames(#"Trimmed and proper case"), {"Notes", "Owner"})),
        // 12 month columns -> one row per group, line and month
        #"Unpivoted months" = Table.UnpivotOtherColumns(#"Kept plan columns", {"Category Group", "Plan Line"}, "Month", "Plan Text"),
        // 2018-01 -> Year 2018 and Month No 01
        #"Split month" = Table.SplitColumn(#"Unpivoted months", "Month", Splitter.SplitTextByDelimiter("-", QuoteStyle.None), {"Year", "Month No"}),
        // 'n/a' and '-' are not numbers: try … otherwise null instead of an import error
        #"Amounts as numbers" = Table.AddColumn(#"Split month", "Plan Revenue", each try Number.From([Plan Text], "en-US") otherwise null, type nullable number),
        // The text version is no longer needed
        #"Removed text amount" = Table.RemoveColumns(#"Amounts as numbers", {"Plan Text"}),
        // Types once the shape is final
        #"Changed type" = Table.TransformColumnTypes(#"Removed text amount", {{"Plan Line", type text}, {"Year", Int64.Type}, {"Month No", Int64.Type}}),
        // Safety net: a malformed month header would fail the type above; drop such rows (0 today)
        #"Removed errors" = Table.RemoveRowsWithErrors(#"Changed type", {"Year", "Month No"})
    in
        #"Removed errors"
