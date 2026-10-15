let
    // Reference (not duplicate) the shared staging query: one place reads the Parquet file
    Source = #"Tickets Source",
    // A duration that fails its type becomes null instead of failing the refresh (Import Errors keeps those rows)
    NoErrors = Table.ReplaceErrorValues(Source, {{"Lead Days", null}, {"Cycle Days", null}, {"Response Hours", null}}),
    // Drop rows without a ticket key: they can't join to anything
    HasKey = Table.SelectRows(NoErrors, each [Ticket Key] <> null),
    // Nothing created after the as-of date (parameter AsOfDate)
    UpToAsOf = Table.SelectRows(HasKey, each [Created Date] <= Date.From(AsOfDate)),
    // No assignee -> person 0, the Unassigned row in People
    OwnerOrUnassigned = Table.ReplaceValue(UpToAsOf, null, 0, Replacer.ReplaceValue, {"Person Key"}),
    // The summary moves to Ticket Text (long free text; only the ticket list reads it)
    NoSummary = Table.RemoveColumns(OwnerOrUnassigned, {"Summary"}),
    // Title-case statuses (some teams type them in lower case)
    CleanText = Table.TransformColumns(NoSummary, {{"Status", Text.Proper, type text}}),
    // Copy the key so it survives the split
    KeyCopy = Table.DuplicateColumn(CleanText, "Ticket Key", "Key Parts"),
    // HIVE-1234 -> project HIVE + issue number 1234 (sorts tickets numerically)
    SplitKey = Table.SplitColumn(KeyCopy, "Key Parts", Splitter.SplitTextByDelimiter("-", QuoteStyle.None), {"Project", "Issue Number"}),
    // The issue number is a whole number
    IssueNumber = Table.TransformColumnTypes(SplitKey, {{"Issue Number", Int64.Type}}),
    // Link to the ticket on issues.apache.org
    JiraUrl = Table.AddColumn(IssueNumber, "Jira URL", each "https://issues.apache.org/jira/browse/" & [Ticket Key], type text),
    // Bucket the age of open tickets
    AgeBand = Table.AddColumn(JiraUrl, "Age Band", each if [Age Days] = null then null else if [Age Days] <= 30 then "0–30 days" else if [Age Days] <= 90 then "31–90 days" else if [Age Days] <= 180 then "91–180 days" else "Over 180 days", type text),
    // Sort order for the bands
    AgeBandOrder = Table.AddColumn(AgeBand, "Age Band Order", each if [Age Days] = null then null else if [Age Days] <= 30 then 1 else if [Age Days] <= 90 then 2 else if [Age Days] <= 180 then 3 else 4, Int64.Type),
    // Read the priority targets (a second source, merged below)
    PriorityTargets = Table.RenameColumns(Table.SelectColumns(Parquet.Document(File.Contents(DataFolder & "dim_priority.parquet")), {"priority_key", "resolution_days"}), {{"priority_key", "Priority Key"}, {"resolution_days", "Target Days"}}),
    // Merge (left outer join) on Priority Key, like a VLOOKUP
    WithTarget = Table.NestedJoin(AgeBandOrder, {"Priority Key"}, PriorityTargets, {"Priority Key"}, "Priority", JoinKind.LeftOuter),
    // Bring back just the resolution target
    TargetDays = Table.ExpandTableColumn(WithTarget, "Priority", {"Target Days"}),
    // Open tickets: days past the resolution target (negative = time left)
    PastTarget = Table.AddColumn(TargetDays, "Days Past Target", each if [Age Days] = null then null else [Age Days] - [Target Days], type number),
    // The target itself lives in Priorities
    NoTargetDays = Table.RemoveColumns(PastTarget, {"Target Days"}),
    // Group By: how many open stages each ticket passed through (Stage Time has one row per ticket and stage)
    StagesPerTicket = Table.Group(Parquet.Document(File.Contents(DataFolder & "fact_stage_time.parquet")), {"ticket_key"}, {{"Stages Visited", each Table.RowCount(_), Int64.Type}}),
    // Merge the counts back onto the tickets
    WithStages = Table.NestedJoin(NoTargetDays, {"Ticket Key"}, StagesPerTicket, {"ticket_key"}, "Stages", JoinKind.LeftOuter),
    // Tickets that never sat in an open stage visited none
    Tickets = Table.ReplaceValue(Table.ExpandTableColumn(WithStages, "Stages", {"Stages Visited"}), null, 0, Replacer.ReplaceValue, {"Stages Visited"})
in
    Tickets
