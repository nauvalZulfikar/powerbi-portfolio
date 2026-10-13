// First month of the fiscal year (7 = July, as in AdventureWorks). Date reads it for every fiscal column,
// so a client on an April year changes one parameter instead of editing the Date query.
7 meta [IsParameterQuery = true, Type = "Number", IsParameterQueryRequired = true]
