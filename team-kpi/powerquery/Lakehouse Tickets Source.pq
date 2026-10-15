let
    // Fabric variant: the lakehouse SQL endpoint; every step below folds into one SELECT
    Source = Sql.Database(LakehouseSqlEndpoint, "teamkpi_lh"),
    Data = Source{[Schema = "dbo", Item = "fact_ticket"]}[Data],
    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these
    Kept = Table.SelectColumns(Data, {"ticket_key", "team_key", "type_key", "priority_key", "person_key", "stage", "stage_key", "status", "resolution", "outcome", "summary", "created_at", "created_date", "resolved_at", "resolved_date", "first_response_at", "started_at", "response_hours", "wait_hours", "lead_days", "cycle_days", "age_days", "reopened", "response_sla", "resolution_sla"}),
    // Business names
    Renamed = Table.RenameColumns(Kept, {{"ticket_key", "Ticket Key"}, {"team_key", "Team Key"}, {"type_key", "Type Key"}, {"priority_key", "Priority Key"}, {"person_key", "Person Key"}, {"stage", "Stage"}, {"stage_key", "Stage Key"}, {"status", "Status"}, {"resolution", "Resolution"}, {"outcome", "Outcome"}, {"summary", "Summary"}, {"created_at", "Created At"}, {"created_date", "Created Date"}, {"resolved_at", "Resolved At"}, {"resolved_date", "Resolved Date"}, {"first_response_at", "First Response At"}, {"started_at", "Started At"}, {"response_hours", "Response Hours"}, {"wait_hours", "Wait Hours"}, {"lead_days", "Lead Days"}, {"cycle_days", "Cycle Days"}, {"age_days", "Age Days"}, {"reopened", "Reopened"}, {"response_sla", "Response SLA"}, {"resolution_sla", "Resolution SLA"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Ticket Key", type text}, {"Team Key", type text}, {"Type Key", Int64.Type}, {"Priority Key", Int64.Type}, {"Person Key", Int64.Type}, {"Stage", type text}, {"Stage Key", Int64.Type}, {"Status", type text}, {"Resolution", type text}, {"Outcome", type text}, {"Summary", type text}, {"Created At", type datetime}, {"Created Date", type date}, {"Resolved At", type datetime}, {"Resolved Date", type date}, {"First Response At", type datetime}, {"Started At", type datetime}, {"Response Hours", type number}, {"Wait Hours", type number}, {"Lead Days", type number}, {"Cycle Days", type number}, {"Age Days", type number}, {"Reopened", Int64.Type}, {"Response SLA", type text}, {"Resolution SLA", type text}})
in
    Typed
