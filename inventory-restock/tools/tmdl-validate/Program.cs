// Parse a TMDL semantic model with Microsoft's own TOM serializer (the same one
// Power BI Desktop uses) and print a summary. Non-zero exit on any parse error.
//   dotnet run --project tools/tmdl-validate -- powerbi/StockRestock.SemanticModel/definition
using Microsoft.AnalysisServices.Tabular;

var path = args.Length > 0 ? args[0] : "powerbi/StockRestock.SemanticModel/definition";
try
{
    var db = TmdlSerializer.DeserializeDatabaseFromFolder(path);
    var m = db.Model;
    Console.WriteLine($"✓ TMDL parsed: compatibility {db.CompatibilityLevel}, {m.Tables.Count} tables, " +
                      $"{m.Relationships.Count} relationships, {m.Tables.Sum(t => t.Measures.Count)} measures, " +
                      $"{m.Expressions.Count} expressions");
    foreach (var t in m.Tables)
        Console.WriteLine($"  {t.Name,-18} cols {t.Columns.Count,3}  measures {t.Measures.Count,3}  partitions {t.Partitions.Count}" +
                          (t.DataCategory == "Time" ? "  [date table]" : ""));
    foreach (SingleColumnRelationship r in m.Relationships)
        Console.WriteLine($"  rel {r.FromTable.Name}[{r.FromColumn.Name}] ({r.FromCardinality}) → {r.ToTable.Name}[{r.ToColumn.Name}] ({r.ToCardinality})");
    foreach (var r in m.Roles)
        Console.WriteLine($"  role {r.Name}: " + string.Join(", ", r.TablePermissions.Select(tp => tp.Table.Name)));
    foreach (var t in m.Tables.Where(t => t.CalculationGroup != null))
        Console.WriteLine($"  calc group {t.Name}: " + string.Join(", ", t.CalculationGroup.CalculationItems.Select(i => i.Name)));
    foreach (var f in m.Functions)
        Console.WriteLine($"  function {f.Name}");
    foreach (var t in m.Tables)
        foreach (var c in t.Calendars)
            Console.WriteLine($"  calendar {t.Name}.{c.Name}: " + string.Join(", ", c.CalendarColumnGroups.OfType<TimeUnitColumnAssociation>().Select(g => g.TimeUnit)));
    foreach (var p in m.Perspectives)
        Console.WriteLine($"  perspective {p.Name}: {p.PerspectiveTables.Count} tables");
    foreach (var r in m.Roles)
        foreach (var tp in r.TablePermissions.Where(tp => tp.MetadataPermission == MetadataPermission.None || tp.ColumnPermissions.Count > 0))
            Console.WriteLine($"  OLS {r.Name}: {tp.Table.Name} " + (tp.MetadataPermission == MetadataPermission.None ? "hidden" : $"{tp.ColumnPermissions.Count} columns hidden"));
    var sorted = m.Tables.SelectMany(t => t.Columns).Where(c => c.SortByColumn != null).Select(c => $"{c.Name}→{c.SortByColumn.Name}");
    Console.WriteLine($"  sort-by: {string.Join(", ", sorted)}");
    foreach (var t in m.Tables.Where(t => t.RefreshPolicy is BasicRefreshPolicy))
    {
        var p = (BasicRefreshPolicy)t.RefreshPolicy;
        Console.WriteLine($"  refresh policy {t.Name}: keep {p.RollingWindowPeriods} {p.RollingWindowGranularity}, " +
                          $"refresh {p.IncrementalPeriods} {p.IncrementalGranularity}" +
                          (string.IsNullOrEmpty(p.PollingExpression) ? "" : ", detect changes"));
    }
    var errors = m.Validate().Errors;
    foreach (var e in errors) Console.Error.WriteLine($"✗ {e.Message}");
    if (errors.Count > 0) Environment.Exit(1);
    Console.WriteLine("✓ model.Validate(): no errors");
    // Round-trip: serialize back out and make sure it deserializes again.
    var tmp = Path.Combine(Path.GetTempPath(), "tmdl-roundtrip-" + Guid.NewGuid());
    TmdlSerializer.SerializeDatabaseToFolder(db, tmp);
    TmdlSerializer.DeserializeDatabaseFromFolder(tmp);
    Directory.Delete(tmp, true);
    Console.WriteLine("✓ round-trip serialize/deserialize OK");
}
catch (Exception e)
{
    Console.Error.WriteLine($"✗ {e.GetType().Name}: {e.Message}");
    if (e.InnerException != null) Console.Error.WriteLine($"  inner: {e.InnerException.Message}");
    Environment.Exit(1);
}
