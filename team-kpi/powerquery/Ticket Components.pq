let
    // Read the mart the pipeline wrote
    Data = Parquet.Document(File.Contents(DataFolder & "bridge_ticket_component.parquet")),
    // Keep only the columns the model uses, first: Parquet (and SQL) then reads just these
    Kept = Table.SelectColumns(Data, {"ticket_key", "component_key"}),
    // Business names
    Renamed = Table.RenameColumns(Kept, {{"ticket_key", "Ticket Key"}, {"component_key", "Component Key"}}),
    // Types last, once the shape is final
    Typed = Table.TransformColumnTypes(Renamed, {{"Ticket Key", type text}, {"Component Key", Int64.Type}})
in
    Typed
