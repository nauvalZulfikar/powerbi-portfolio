// Parse a TMDL semantic model with Microsoft's own TOM serializer (the same one
// Power BI Desktop uses) and print a summary. Non-zero exit on any parse error.
//   dotnet run --project tools/tmdl-validate -- powerbi/GroupFinance.SemanticModel/definition
using Microsoft.AnalysisServices.Tabular;

var path = args.Length > 0 ? args[0] : "powerbi/GroupFinance.SemanticModel/definition";
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
    var allMeasures = m.Tables.SelectMany(t => t.Measures).ToList();
    foreach (var ms in allMeasures.Where(x => x.KPI != null))
        Console.WriteLine($"  KPI {ms.Name}: target = {ms.KPI.TargetExpression}, graphic = {ms.KPI.StatusGraphic}");
    foreach (var ms in allMeasures.Where(x => x.FormatStringDefinition != null))
        Console.WriteLine($"  dynamic format {ms.Name}");
    foreach (var ms in allMeasures.Where(x => x.DataCategory == "ImageUrl"))
        Console.WriteLine($"  image measure {ms.Name}");
    foreach (var c in m.Cultures)
        Console.WriteLine($"  culture {c.Name}: {c.ObjectTranslations.Count} translations" +
                          (c.LinguisticMetadata != null ? ", linguistic metadata" : ""));
    foreach (var t in m.Tables.Where(t => t.RefreshPolicy is BasicRefreshPolicy))
    {
        var p = (BasicRefreshPolicy)t.RefreshPolicy;
        Console.WriteLine($"  refresh policy {t.Name}: keep {p.RollingWindowPeriods} {p.RollingWindowGranularity}, " +
                          $"refresh {p.IncrementalPeriods} {p.IncrementalGranularity}" +
                          (string.IsNullOrEmpty(p.PollingExpression) ? "" : ", detect changes"));
    }
    foreach (var t in m.Tables)
        foreach (var h in t.Hierarchies)
            Console.WriteLine($"  hierarchy {t.Name}.{h.Name}: " + string.Join(" → ", h.Levels.OrderBy(l => l.Ordinal).Select(l => l.Column.Name)));
    Console.WriteLine("  storage: " + string.Join(", ", m.Tables.GroupBy(t => t.Partitions[0].Mode).Select(g => $"{g.Key} {g.Count()}")));
    foreach (var r in m.Relationships.OfType<SingleColumnRelationship>().Where(r => !r.IsActive || r.ToCardinality == RelationshipEndCardinality.Many))
        Console.WriteLine($"  {(r.IsActive ? "many-to-many" : "inactive")} {r.FromTable.Name}[{r.FromColumn.Name}] → {r.ToTable.Name}[{r.ToColumn.Name}]");
    foreach (var c in m.Tables.SelectMany(t => t.Columns).Where(c => c.AlternateOf != null))
        Console.WriteLine($"  alternateOf {c.Table.Name}[{c.Name}]: {c.AlternateOf.Summarization} of " +
                          (c.AlternateOf.BaseColumn != null ? $"{c.AlternateOf.BaseColumn.Table.Name}[{c.AlternateOf.BaseColumn.Name}]" : c.AlternateOf.BaseTable.Name));
    foreach (var c in m.Tables.SelectMany(t => t.Columns).OfType<CalculatedColumn>())
        Console.WriteLine($"  calculated column {c.Table.Name}[{c.Name}]");
    Console.WriteLine($"  isAvailableInMdx false: {m.Tables.SelectMany(t => t.Columns).Count(c => !c.IsAvailableInMDX)} columns");
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
