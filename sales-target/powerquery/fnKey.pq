// Normalises free text before a lookup: trim, collapse repeated spaces, upper-case.
// "  Linda  Mitchell " and "LINDA MITCHELL" both become "LINDA MITCHELL".
(value as nullable text) as nullable text =>
    if value = null then null
    else Text.Upper(Text.Combine(List.Select(Text.Split(Text.Trim(value), " "), each _ <> ""), " "))
