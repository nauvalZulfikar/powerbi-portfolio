let
    // Enter Data: one row per scoring system, P1..P10 across
    Source = Table.FromRows(Json.Document(Binary.Decompress(Binary.FromText("jdDNDsIgDAfwVzGcd2hhW+wD6AvojXEyRLlosqnZ49tq2cSPZAn/FBr4BfDebNM9robDpY/9YCqD1EB3s4COZ6QNLmtOy6k5jmM5xQiVN7s0FlQLc/kF4Jfw2o/5BqSAbKTlAClgAaw2pMAiYZOOp+vbMxhxE0fayFx+VfOHFXAfzwWH+sG6kCLHUSx8zuwnX09sCA8=", BinaryEncoding.Base64), Compression.Deflate)), let _t = ((type nullable text) meta [Serialized.Text = true]) in type table [Era = _t, System = _t, #"First Season" = _t, P1 = _t, P2 = _t, P3 = _t, P4 = _t, P5 = _t, P6 = _t, P7 = _t, P8 = _t, P9 = _t, P10 = _t]),
    // Enter Data stores an empty cell as "": make it null so Fill Down and Unpivot skip it
    Blanks = Table.ReplaceValue(Source, "", null, Replacer.ReplaceValue, Table.ColumnNames(Source)),
    // Era is typed once per family
    Filled = Table.FillDown(Blanks, {"Era"}),
    // Wide to long: one row per system and paying position (nulls drop out)
    Long = Table.UnpivotOtherColumns(Filled, {"Era", "System", "First Season"}, "Slot", "System Points"),
    // "P7" -> 7
    Positioned = Table.AddColumn(Long, "Position", each Number.From(Text.AfterDelimiter([Slot], "P")), Int64.Type),
    Kept = Table.SelectColumns(Positioned, {"Era", "System", "First Season", "Position", "System Points"}),
    Typed = Table.TransformColumnTypes(Kept, {{"First Season", Int64.Type}, {"System Points", type number}})
in
    Typed
