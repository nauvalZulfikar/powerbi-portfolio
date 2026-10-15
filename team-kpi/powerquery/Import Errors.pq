let
    // Same staging query as Tickets
    Source = #"Tickets Source",
    // Keep the rows where a duration failed its type (normally none)
    Errors = Table.SelectRowsWithErrors(Source, {"Lead Days", "Cycle Days", "Response Hours"}),
    // Just the key
    Keys = Table.SelectColumns(Errors, {"Ticket Key"}),
    // What went wrong
    Flagged = Table.AddColumn(Keys, "Issue", each "A lead, cycle or response value failed its type", type text)
in
    Flagged
