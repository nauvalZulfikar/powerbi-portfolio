// Sales people with their manager, for the org hierarchy and dynamic row-level security.
let
    Source = fnReference("Reps"),
    // PATH() fails on a parent that isn't in the table (the VP reports to the CEO), so those become the top.
    Keys = List.Buffer(Source[Rep Key]),
    Rooted = Table.ReplaceValue(Source, each [Manager Key], each if List.Contains(Keys, [Manager Key]) then [Manager Key] else null,
        Replacer.ReplaceValue, {"Manager Key"}),
    WithEmail = Table.AddColumn(Rooted, "Email", each Text.AfterDelimiter([Login], "\") & "@adventure-works.com", type text)
in
    Table.TransformColumnTypes(WithEmail, {
        {"Rep Key", Int64.Type}, {"Rep", type text}, {"Login", type text}, {"Title", type text},
        {"Territory Key", Int64.Type}, {"Manager Key", Int64.Type}})
