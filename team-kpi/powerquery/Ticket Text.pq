let
    // Same staging query as Tickets: one read of the Parquet file
    Source = #"Tickets Source",
    // Same row filters as Tickets, so the 1:1 relationship lines up
    HasKey = Table.SelectRows(Source, each [Ticket Key] <> null and [Created Date] <= Date.From(AsOfDate)),
    // Just the key and the text
    Kept = Table.SelectColumns(HasKey, {"Ticket Key", "Summary"}),
    // Trim and strip control characters
    Cleaned = Table.TransformColumns(Kept, {{"Summary", each Text.Clean(Text.Trim(_)), type text}})
in
    Cleaned
