// Best Practice Analyzer for TMDL on Mac/Linux — no Tabular Editor needed.
// Loads the model with Microsoft TOM (TmdlSerializer, same as tools/tmdl-validate), wraps every object in a
// Tabular-Editor-shaped facade (Name, IsHidden, DependsOn, ReferencedBy, UsedInRelationships, GetAnnotation, ...)
// and evaluates the BPARules.json expressions with Dynamic LINQ, so the same rule file runs here and in
// TabularEditor.exe -A on Windows CI.
//
//   bpa-run <SemanticModel>/definition [--rules extra.json]... [--no-default] [--fail-on 3] [--json out.json]
//
// Ignore a rule on one object (Tabular Editor convention, works in TE2 too):
//   annotation BestPracticeAnalyzer_IgnoreRules = {"RuleIDs":["RULE_ID"]}
// The same annotation on the model switches the rule off for the whole model.
// Rules stored in the model annotation "BestPracticeAnalyzer" are applied as well (TE does the same).
// Exit code: 1 if any violation has severity >= --fail-on (default 3 = TE "error"), 2 on load/usage errors.
using System.Linq.Dynamic.Core;
using System.Linq.Dynamic.Core.CustomTypeProviders;
using System.Linq.Expressions;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using T = Microsoft.AnalysisServices.Tabular;

string? path = null, jsonOut = null;
var ruleFiles = new List<string>();
bool useDefault = true;
int failOn = 3;
for (int i = 0; i < args.Length; i++)
{
    switch (args[i])
    {
        case "--rules": ruleFiles.Add(args[++i]); break;
        case "--no-default": useDefault = false; break;
        case "--fail-on": failOn = int.Parse(args[++i]); break;
        case "--json": jsonOut = args[++i]; break;
        default: path = args[i]; break;
    }
}
if (path == null) { Console.Error.WriteLine("usage: bpa-run <definition folder> [--rules f.json]... [--no-default] [--fail-on N] [--json out.json]"); return 2; }
if (useDefault) ruleFiles.Insert(0, Path.Combine(AppContext.BaseDirectory, "BPARules.json"));

T.Database db;
try { db = T.TmdlSerializer.DeserializeDatabaseFromFolder(path); }
catch (Exception e) { Console.Error.WriteLine($"✗ TMDL load failed: {e.Message}"); return 2; }

var model = Wrap.Build(db.Model);
var rules = new List<Rule>();
var jopt = new JsonSerializerOptions { PropertyNameCaseInsensitive = true, ReadCommentHandling = JsonCommentHandling.Skip, AllowTrailingCommas = true };
foreach (var f in ruleFiles) rules.AddRange(JsonSerializer.Deserialize<List<Rule>>(File.ReadAllText(f), jopt)!);
var embedded = model.GetAnnotation("BestPracticeAnalyzer");
if (!string.IsNullOrWhiteSpace(embedded)) rules.AddRange(JsonSerializer.Deserialize<List<Rule>>(embedded, jopt)!);
// later files override earlier ones with the same ID (project rules can retune a kit rule)
rules = rules.GroupBy(r => r.ID).Select(g => g.Last()).ToList();

var modelIgnored = model.IgnoredRules;
var results = new List<Violation>();
var skipped = new List<string>();
var cfg = new ParsingConfig { CustomTypeProvider = new KitTypes(), IsCaseSensitive = false };
foreach (var rule in rules)
{
    if (modelIgnored.Contains(rule.ID)) { skipped.Add($"{rule.ID}: ignored by model annotation"); continue; }
    var expr = Rule.Translate(rule.Expression);
    foreach (var scope in rule.Scope.Split(',', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries))
    {
        var objs = model.InScope(scope).ToList();
        if (objs.Count == 0) continue;
        Delegate fn;
        try
        {
            var lambda = DynamicExpressionParser.ParseLambda(cfg, objs[0].GetType(), typeof(bool), expr);
            fn = lambda.Compile();
        }
        catch (Exception e) { skipped.Add($"{rule.ID} [{scope}]: expression not supported here ({e.Message})"); continue; }
        foreach (var o in objs)
        {
            if (o.IgnoredRules.Contains(rule.ID)) continue;
            bool hit;
            try { hit = (bool)fn.DynamicInvoke(o)!; }
            catch (Exception e) { skipped.Add($"{rule.ID} on {o.DaxObjectFullName}: {(e.InnerException ?? e).Message}"); continue; }
            if (hit) results.Add(new Violation(rule.ID, rule.Name, rule.Severity, rule.Category ?? "", scope, o.DaxObjectFullName));
        }
    }
}

string Sev(int s) => s >= 3 ? "ERROR" : s == 2 ? "WARN " : "INFO ";
foreach (var v in results.OrderByDescending(v => v.Severity).ThenBy(v => v.RuleId).ThenBy(v => v.Object))
    Console.WriteLine($"{Sev(v.Severity)} {v.RuleId,-55} {v.Scope,-22} {v.Object}");
foreach (var s in skipped) Console.WriteLine($"SKIP  {s}");
var bySev = results.GroupBy(v => Math.Min(v.Severity, 3)).ToDictionary(g => g.Key, g => g.Count());
Console.WriteLine($"\nBPA {path}: {rules.Count} rules, {results.Count} violations — " +
                  $"error {bySev.GetValueOrDefault(3)}, warning {bySev.GetValueOrDefault(2)}, info {bySev.GetValueOrDefault(1) + bySev.GetValueOrDefault(0)}; " +
                  $"{skipped.Count} skipped");
if (jsonOut != null)
    File.WriteAllText(jsonOut, JsonSerializer.Serialize(new { model = path, rules = rules.Count, violations = results, skipped },
        new JsonSerializerOptions { WriteIndented = true }));
var failing = results.Count(v => v.Severity >= failOn);
if (failing > 0) { Console.Error.WriteLine($"✗ {failing} violation(s) with severity >= {failOn}"); return 1; }
Console.WriteLine($"✓ no violations with severity >= {failOn}");
return 0;

record Violation(string RuleId, string Rule, int Severity, string Category, string Scope, string Object);

class Rule
{
    public string ID { get; set; } = "";
    public string Name { get; set; } = "";
    public string? Category { get; set; }
    public int Severity { get; set; } = 1;
    public string Scope { get; set; } = "";
    public string Expression { get; set; } = "true";

    // Tabular Editor's Dynamic LINQ dialect → System.Linq.Dynamic.Core
    public static string Translate(string e)
    {
        // TE's (original System.Linq.Dynamic) string literals are raw: backslashes are literal, "" is a quote.
        // Dynamic.Core unescapes backslashes, so re-escape every literal first.
        e = Regex.Replace(e, "\"((?:[^\"]|\"\")*)\"", m => "\"" + m.Groups[1].Value.Replace("\\", "\\\\").Replace("\"\"", "\\\"") + "\"");
        e = e.Replace(".ToString()", ""); // the facade already exposes enums as strings
        e = Regex.Replace(e, @"\b(current|outerIt)\b", "root", RegexOptions.IgnoreCase);
        // enum literals → strings (the facade exposes enums as strings)
        e = Regex.Replace(e, @"(?<![\w.])(DataType|CrossFilteringBehavior|ObjectType|ColumnType)\.(\w+)", "\"$2\"");
        e = Regex.Replace(e, @"\bchar\((\d+)\)", "Convert.ToChar($1)");
        return e;
    }
}

[DynamicLinqType]
public static class RegEx
{
    public static bool IsMatch(string? input, string pattern) => input != null && Regex.IsMatch(input, pattern);
}

class KitTypes : DefaultDynamicLinqCustomTypeProvider
{
    public KitTypes() : base(ParsingConfig.Default) { }
    public override HashSet<Type> GetCustomTypes() =>
        new(typeof(Obj).Assembly.GetTypes().Where(t => t.IsPublic && (typeof(Obj).IsAssignableFrom(t) || t == typeof(RegEx) || t == typeof(Ref)
                                                                      || t == typeof(ReferencedBy) || t == typeof(PerspectiveFlags))));
}

// ---------------------------------------------------------------- TE-shaped facade over TOM
public class Ref { public bool FullyQualified { get; init; } }

public class ReferencedBy : List<Obj>
{
    public IEnumerable<MeasureW> AllMeasures => this.OfType<MeasureW>();
    public IEnumerable<ColumnW> AllColumns => this.OfType<ColumnW>();
}

public class PerspectiveFlags
{
    internal HashSet<string> In = new();
    public bool this[string perspective] => In.Contains(perspective);
}

public abstract class Obj
{
    internal T.MetadataObject? Tom;
    public string Name { get; set; } = "";
    public abstract string ObjectType { get; }
    internal virtual string ScopeName => ObjectType; // BPA scope name (DataColumn, CalculatedTable, ...)
    public virtual string ObjectTypeName => ObjectType;
    public ModelW Model { get; set; } = null!;
    public string Description => Prop<string>("Description") ?? "";
    public bool IsHidden => Prop<bool?>("IsHidden") ?? false;
    public virtual string DaxObjectName => $"[{Name}]";
    public virtual string DaxObjectFullName => DaxObjectName;
    public ReferencedBy ReferencedBy { get; } = new();
    public Dictionary<Obj, List<Ref>> DependsOn { get; } = new();
    public string? GetAnnotation(string name) => Annotations.GetValueOrDefault(name);
    public bool HasAnnotation(string name) => Annotations.ContainsKey(name);
    public Dictionary<string, string> Annotations =>
        ((Tom?.GetType().GetProperty("Annotations")?.GetValue(Tom) as System.Collections.IEnumerable)?.OfType<T.Annotation>() ?? Enumerable.Empty<T.Annotation>())
        .ToDictionary(a => a.Name, a => a.Value ?? "", StringComparer.OrdinalIgnoreCase);
    internal HashSet<string> IgnoredRules
    {
        get
        {
            var raw = GetAnnotation("BestPracticeAnalyzer_IgnoreRules");
            if (string.IsNullOrWhiteSpace(raw)) return new();
            try { return JsonDocument.Parse(raw).RootElement.GetProperty("RuleIDs").EnumerateArray().Select(x => x.GetString()!).ToHashSet(); }
            catch { return new(); }
        }
    }
    TV? Prop<TV>(string n) { var p = Tom?.GetType().GetProperty(n); return p == null ? default : (TV?)p.GetValue(Tom); }
    public override string ToString() => DaxObjectFullName;
}

public class ModelW : Obj
{
    public override string ObjectType => "Model";
    public override string DaxObjectFullName => "Model";
    public List<TableW> Tables { get; } = new();
    public List<RelationshipW> Relationships { get; } = new();
    public List<RoleW> Roles { get; } = new();
    public List<PerspectiveW> Perspectives { get; } = new();
    public List<NamedExpressionW> Expressions { get; } = new();
    public List<DataSourceW> DataSources { get; } = new();
    public IEnumerable<MeasureW> AllMeasures => Tables.SelectMany(t => t.Measures);
    public IEnumerable<ColumnW> AllColumns => Tables.SelectMany(t => t.Columns);
    public IEnumerable<PartitionW> AllPartitions => Tables.SelectMany(t => t.Partitions);
    public IEnumerable<HierarchyW> AllHierarchies => Tables.SelectMany(t => t.Hierarchies);
    public IEnumerable<LevelW> AllLevels => AllHierarchies.SelectMany(h => h.Levels);
    public IEnumerable<CalculationItemW> AllCalculationItems => Tables.SelectMany(t => t.CalculationItems);
    public string DefaultPowerBIDataSourceVersion { get; set; } = "";

    public IEnumerable<Obj> InScope(string scope) => scope switch
    {
        "Model" => new Obj[] { this },
        "Table" => Tables.Where(t => t.ScopeName == "Table"),
        "CalculatedTable" => Tables.Where(t => t.ScopeName == "CalculatedTable"),
        "CalculationGroup" => Tables.Where(t => t.ScopeName == "CalculationGroup"),
        "Measure" => AllMeasures,
        "DataColumn" or "CalculatedColumn" or "CalculatedTableColumn" => AllColumns.Where(c => c.ScopeName == scope),
        "Hierarchy" => AllHierarchies,
        "Level" => AllLevels,
        "Relationship" => Relationships,
        "Perspective" => Perspectives,
        "Partition" => AllPartitions,
        "ModelRole" => Roles,
        "TablePermission" => Roles.SelectMany(r => r.TablePermissions),
        "CalculationItem" => AllCalculationItems,
        "NamedExpression" => Expressions,
        "ProviderDataSource" or "StructuredDataSource" => DataSources.Where(d => d.ScopeName == scope),
        _ => Enumerable.Empty<Obj>(), // KPI, Variation, Culture: not modelled
    };
}

public class TableW : Obj
{
    internal string Kind = "Table";
    public override string ObjectType => "Table";
    internal override string ScopeName => Kind;
    public override string ObjectTypeName => Kind switch
    {
        "CalculatedTable" => "Calculated Table",
        "CalculationGroup" => "Calculation Group Table",
        _ => Partitions.Any(p => p.Mode == "DirectQuery") ? "Table (DirectQuery)" : "Table",
    };
    public override string DaxObjectName => $"'{Name.Replace("'", "''")}'";
    public override string DaxObjectFullName => DaxObjectName;
    public List<ColumnW> Columns { get; } = new();
    public List<MeasureW> Measures { get; } = new();
    public List<PartitionW> Partitions { get; } = new();
    public List<HierarchyW> Hierarchies { get; } = new();
    public List<CalculationItemW> CalculationItems { get; } = new();
    public string DataCategory { get; set; } = "";
    public string Expression { get; set; } = "";
    public string SourceExpression => Expression;
    public List<RelationshipW> UsedInRelationships { get; } = new();
    public List<string> RowLevelSecurity { get; } = new();
    public List<string> ObjectLevelSecurity { get; } = new();
    public PerspectiveFlags InPerspective { get; } = new();
}

public class ColumnW : Obj
{
    internal string Kind = "DataColumn";
    public override string ObjectType => "Column";
    internal override string ScopeName => Kind;
    public TableW Table { get; set; } = null!;
    public override string DaxObjectFullName => $"{Table.DaxObjectName}[{Name}]";
    public string DataType { get; set; } = "";
    public string Type { get; set; } = "";
    public string Expression { get; set; } = "";
    public string SourceColumn { get; set; } = "";
    public string FormatString { get; set; } = "";
    public string DisplayFolder { get; set; } = "";
    public string DataCategory { get; set; } = "";
    public string SummarizeBy { get; set; } = "";
    public bool IsKey { get; set; }
    public bool IsAvailableInMDX { get; set; }
    public ColumnW? SortByColumn { get; set; }
    public object? AlternateOf { get; set; }
    public List<ColumnW> UsedInSortBy { get; } = new();
    public List<HierarchyW> UsedInHierarchies { get; } = new();
    public List<object> UsedInVariations { get; } = new();
    public List<RelationshipW> UsedInRelationships { get; } = new();
    public List<string> ObjectLevelSecurity { get; } = new();
}

public class MeasureW : Obj
{
    public override string ObjectType => "Measure";
    public TableW Table { get; set; } = null!;
    public override string DaxObjectFullName => $"{Table.DaxObjectName}[{Name}]";
    public string Expression { get; set; } = "";
    public string FormatString { get; set; } = "";
    public string FormatStringExpression { get; set; } = "";
    public string DisplayFolder { get; set; } = "";
}

public class HierarchyW : Obj
{
    public override string ObjectType => "Hierarchy";
    public TableW Table { get; set; } = null!;
    public override string DaxObjectFullName => $"{Table.DaxObjectName}[{Name}]";
    public List<LevelW> Levels { get; } = new();
}

public class LevelW : Obj
{
    public override string ObjectType => "Level";
    public HierarchyW Hierarchy { get; set; } = null!;
    public ColumnW? Column { get; set; }
    public override string DaxObjectFullName => $"{Hierarchy.DaxObjectFullName}.[{Name}]";
}

public class RelationshipW : Obj
{
    public override string ObjectType => "Relationship";
    public TableW FromTable { get; set; } = null!;
    public TableW ToTable { get; set; } = null!;
    public ColumnW FromColumn { get; set; } = null!;
    public ColumnW ToColumn { get; set; } = null!;
    public string FromCardinality { get; set; } = "";
    public string ToCardinality { get; set; } = "";
    public string CrossFilteringBehavior { get; set; } = "";
    public bool IsActive { get; set; }
    public override string DaxObjectFullName => $"{FromColumn.DaxObjectFullName} → {ToColumn.DaxObjectFullName}";
}

public class PartitionW : Obj
{
    public override string ObjectType => "Partition";
    public TableW Table { get; set; } = null!;
    public string SourceType { get; set; } = "";
    public string Mode { get; set; } = "";
    public string Query { get; set; } = "";
    public string Expression => Query;
    public DataSourceW? DataSource { get; set; }
    public override string DaxObjectFullName => $"{Table.DaxObjectName}.Partitions[{Name}]";
}

public class DataSourceW : Obj
{
    internal string Kind = "ProviderDataSource";
    public override string ObjectType => "DataSource";
    internal override string ScopeName => Kind;
    public string Type { get; set; } = "";
    public List<PartitionW> UsedByPartitions { get; } = new();
}

public class RoleW : Obj
{
    public override string ObjectType => "ModelRole";
    public List<string> Members { get; } = new();
    public List<TablePermissionW> TablePermissions { get; } = new();
    public IEnumerable<string> RowLevelSecurity => TablePermissions.Select(tp => tp.Expression).Where(x => !string.IsNullOrWhiteSpace(x));
    public override string DaxObjectFullName => $"Role[{Name}]";
}

public class TablePermissionW : Obj
{
    public override string ObjectType => "TablePermission";
    public RoleW Role { get; set; } = null!;
    public TableW Table { get; set; } = null!;
    public string Expression { get; set; } = "";
    public override string DaxObjectFullName => $"Role[{Role.Name}].{Table.DaxObjectName}";
}

public class PerspectiveW : Obj { public override string ObjectType => "Perspective"; public override string DaxObjectFullName => $"Perspective[{Name}]"; }

public class NamedExpressionW : Obj
{
    public override string ObjectType => "NamedExpression";
    public string Expression { get; set; } = "";
    public override string DaxObjectFullName => $"Expression[{Name}]";
}

public class CalculationItemW : Obj
{
    public override string ObjectType => "CalculationItem";
    public TableW CalculationGroup { get; set; } = null!;
    public TableW Table => CalculationGroup;
    public string Expression { get; set; } = "";
    public string FormatStringExpression { get; set; } = "";
    public override string DaxObjectFullName => $"{CalculationGroup.DaxObjectName}.Items[{Name}]";
}

static class Wrap
{
    public static ModelW Build(T.Model m)
    {
        var model = new ModelW { Tom = m, Name = m.Name ?? "Model", DefaultPowerBIDataSourceVersion = m.DefaultPowerBIDataSourceVersion.ToString() };
        var cols = new Dictionary<T.Column, ColumnW>();
        var tables = new Dictionary<T.Table, TableW>();
        var sources = new Dictionary<T.DataSource, DataSourceW>();
        foreach (var ds in m.DataSources)
        {
            var w = new DataSourceW { Tom = ds, Name = ds.Name, Model = model, Kind = ds is T.StructuredDataSource ? "StructuredDataSource" : "ProviderDataSource", Type = ds.Type.ToString() };
            sources[ds] = w; model.DataSources.Add(w);
        }
        foreach (var t in m.Tables)
        {
            var tw = new TableW { Tom = t, Name = t.Name, Model = model, DataCategory = t.DataCategory ?? "" };
            tables[t] = tw; model.Tables.Add(tw);
            if (t.CalculationGroup != null) tw.Kind = "CalculationGroup";
            else if (t.Partitions.Any(p => p.SourceType == T.PartitionSourceType.Calculated)) tw.Kind = "CalculatedTable";
            foreach (var c in t.Columns.Where(c => c.Type != T.ColumnType.RowNumber))
            {
                var cw = new ColumnW
                {
                    Tom = c, Name = c.Name, Model = model, Table = tw,
                    Kind = c.Type switch { T.ColumnType.Calculated => "CalculatedColumn", T.ColumnType.CalculatedTableColumn => "CalculatedTableColumn", _ => "DataColumn" },
                    DataType = c.DataType.ToString(), Type = c.Type.ToString(), FormatString = c.FormatString ?? "", DisplayFolder = c.DisplayFolder ?? "",
                    DataCategory = c.DataCategory ?? "", SummarizeBy = c.SummarizeBy.ToString(), IsKey = c.IsKey, IsAvailableInMDX = c.IsAvailableInMDX,
                    AlternateOf = c.AlternateOf,
                    Expression = (c as T.CalculatedColumn)?.Expression ?? "",
                    SourceColumn = (c as T.DataColumn)?.SourceColumn ?? (c as T.CalculatedTableColumn)?.SourceColumn ?? "",
                };
                cols[c] = cw; tw.Columns.Add(cw);
            }
            foreach (var ms in t.Measures)
                tw.Measures.Add(new MeasureW
                {
                    Tom = ms, Name = ms.Name, Model = model, Table = tw, Expression = ms.Expression ?? "", FormatString = ms.FormatString ?? "",
                    FormatStringExpression = ms.FormatStringDefinition?.Expression ?? "", DisplayFolder = ms.DisplayFolder ?? "",
                });
            foreach (var p in t.Partitions)
            {
                var pw = new PartitionW { Tom = p, Name = p.Name, Model = model, Table = tw, SourceType = p.SourceType.ToString(), Mode = p.Mode.ToString() };
                switch (p.Source)
                {
                    case T.MPartitionSource s: pw.Query = s.Expression ?? ""; break;
                    case T.CalculatedPartitionSource s: pw.Query = s.Expression ?? ""; tw.Expression = pw.Query; break;
                    case T.QueryPartitionSource s:
                        pw.Query = s.Query ?? "";
                        if (s.DataSource != null && sources.TryGetValue(s.DataSource, out var dsw)) { pw.DataSource = dsw; dsw.UsedByPartitions.Add(pw); }
                        break;
                    case T.EntityPartitionSource s: pw.Query = s.EntityName ?? ""; break;
                }
                tw.Partitions.Add(pw);
            }
            if (t.CalculationGroup != null)
                foreach (var ci in t.CalculationGroup.CalculationItems)
                    tw.CalculationItems.Add(new CalculationItemW
                    {
                        Tom = ci, Name = ci.Name, Model = model, CalculationGroup = tw, Expression = ci.Expression ?? "",
                        FormatStringExpression = ci.FormatStringDefinition?.Expression ?? "",
                    });
        }
        foreach (var t in m.Tables)
        {
            var tw = tables[t];
            foreach (var c in t.Columns.Where(c => c.SortByColumn != null && cols.ContainsKey(c)))
            {
                cols[c].SortByColumn = cols[c.SortByColumn];
                cols[c.SortByColumn].UsedInSortBy.Add(cols[c]);
            }
            foreach (var h in t.Hierarchies)
            {
                var hw = new HierarchyW { Tom = h, Name = h.Name, Model = model, Table = tw };
                foreach (var l in h.Levels)
                {
                    var lw = new LevelW { Tom = l, Name = l.Name, Model = model, Hierarchy = hw, Column = cols.GetValueOrDefault(l.Column) };
                    hw.Levels.Add(lw);
                    if (lw.Column != null && !lw.Column.UsedInHierarchies.Contains(hw)) lw.Column.UsedInHierarchies.Add(hw);
                }
                tw.Hierarchies.Add(hw);
            }
        }
        foreach (var r in m.Relationships.OfType<T.SingleColumnRelationship>())
        {
            var rw = new RelationshipW
            {
                Tom = r, Model = model, FromTable = tables[r.FromTable], ToTable = tables[r.ToTable], FromColumn = cols[r.FromColumn], ToColumn = cols[r.ToColumn],
                FromCardinality = r.FromCardinality.ToString(), ToCardinality = r.ToCardinality.ToString(),
                CrossFilteringBehavior = r.CrossFilteringBehavior.ToString(), IsActive = r.IsActive,
            };
            rw.Name = rw.DaxObjectFullName;
            model.Relationships.Add(rw);
            foreach (var x in new Obj[] { rw.FromTable, rw.ToTable }.Distinct()) ((TableW)x).UsedInRelationships.Add(rw);
            rw.FromColumn.UsedInRelationships.Add(rw);
            if (rw.ToColumn != rw.FromColumn) rw.ToColumn.UsedInRelationships.Add(rw);
        }
        foreach (var role in m.Roles)
        {
            var rw = new RoleW { Tom = role, Name = role.Name, Model = model };
            rw.Members.AddRange(role.Members.Select(x => x.MemberName));
            foreach (var tp in role.TablePermissions)
            {
                var tpw = new TablePermissionW { Tom = tp, Name = $"{role.Name}.{tp.Table.Name}", Model = model, Role = rw, Table = tables[tp.Table], Expression = tp.FilterExpression ?? "" };
                rw.TablePermissions.Add(tpw);
                if (!string.IsNullOrWhiteSpace(tpw.Expression)) tpw.Table.RowLevelSecurity.Add(tpw.Expression);
                tpw.Table.ObjectLevelSecurity.Add(tp.MetadataPermission.ToString());
                foreach (var cp in tp.ColumnPermissions) cols[cp.Column].ObjectLevelSecurity.Add(cp.MetadataPermission.ToString());
            }
            model.Roles.Add(rw);
        }
        foreach (var p in m.Perspectives)
        {
            model.Perspectives.Add(new PerspectiveW { Tom = p, Name = p.Name, Model = model });
            foreach (var pt in p.PerspectiveTables) tables[pt.Table].InPerspective.In.Add(p.Name);
        }
        foreach (var e in m.Expressions)
            model.Expressions.Add(new NamedExpressionW { Tom = e, Name = e.Name, Model = model, Expression = e.Expression ?? "" });

        // DAX dependencies (DependsOn / ReferencedBy) from a lexical scan — enough for the BPA rules that use them
        var daxObjects = new List<(Obj o, string dax, TableW? home)>();
        foreach (var t in model.Tables)
        {
            if (t.ScopeName == "CalculatedTable") daxObjects.Add((t, t.Expression, null));
            daxObjects.AddRange(t.Measures.Select(x => ((Obj)x, x.Expression + "\n" + x.FormatStringExpression, (TableW?)t)));
            daxObjects.AddRange(t.Columns.Where(c => c.ScopeName == "CalculatedColumn").Select(c => ((Obj)c, c.Expression, (TableW?)t)));
            daxObjects.AddRange(t.CalculationItems.Select(c => ((Obj)c, c.Expression + "\n" + c.FormatStringExpression, (TableW?)t)));
        }
        daxObjects.AddRange(model.Roles.SelectMany(r => r.TablePermissions).Select(tp => ((Obj)tp, tp.Expression, (TableW?)tp.Table)));
        var tableByName = model.Tables.ToDictionary(t => t.Name, StringComparer.OrdinalIgnoreCase);
        var measureByName = model.AllMeasures.GroupBy(x => x.Name, StringComparer.OrdinalIgnoreCase).ToDictionary(g => g.Key, g => g.First(), StringComparer.OrdinalIgnoreCase);
        foreach (var (o, dax, home) in daxObjects)
            foreach (var (table, name) in Dax.Refs(dax))
            {
                Obj? target = null; bool fq = table != null;
                if (table != null && tableByName.TryGetValue(table, out var tw))
                    target = name == null ? tw
                        : (Obj?)tw.Columns.FirstOrDefault(c => c.Name.Equals(name, StringComparison.OrdinalIgnoreCase)) ?? measureByName.GetValueOrDefault(name);
                else if (table == null && name != null)
                    target = (Obj?)measureByName.GetValueOrDefault(name) ?? home?.Columns.FirstOrDefault(c => c.Name.Equals(name, StringComparison.OrdinalIgnoreCase));
                if (target == null || target == o) continue;
                if (!o.DependsOn.TryGetValue(target, out var list)) o.DependsOn[target] = list = new();
                list.Add(new Ref { FullyQualified = fq });
                if (!target.ReferencedBy.Contains(o)) target.ReferencedBy.Add(o);
            }
        return model;
    }
}

static class Dax
{
    // yields (table, null) for a bare table reference, (table, name) for 'T'[x] / T[x], (null, name) for [x]
    public static IEnumerable<(string? table, string? name)> Refs(string s)
    {
        string? pendingTable = null;
        int i = 0;
        while (i < s.Length)
        {
            char c = s[i];
            if (char.IsWhiteSpace(c)) { i++; continue; }
            if ((c == '/' && At(s, i + 1) == '/') || (c == '-' && At(s, i + 1) == '-')) { while (i < s.Length && s[i] != '\n') i++; continue; }
            if (c == '/' && At(s, i + 1) == '*') { var e = s.IndexOf("*/", i + 2, StringComparison.Ordinal); i = e < 0 ? s.Length : e + 2; continue; }
            if (pendingTable != null && c != '[') { yield return (pendingTable, null); pendingTable = null; }
            if (c == '"') { i = SkipQuoted(s, i, '"'); continue; }
            if (c == '\'')
            {
                int end = SkipQuoted(s, i, '\'');
                pendingTable = s.Substring(i + 1, Math.Max(0, end - i - 2)).Replace("''", "'");
                i = end; continue;
            }
            if (c == '[')
            {
                int end = s.IndexOf(']', i + 1);
                while (end >= 0 && At(s, end + 1) == ']') end = s.IndexOf(']', end + 2);
                if (end < 0) yield break;
                yield return (pendingTable, s.Substring(i + 1, end - i - 1).Replace("]]", "]"));
                pendingTable = null; i = end + 1; continue;
            }
            if (char.IsLetter(c) || c == '_')
            {
                int j = i;
                while (j < s.Length && (char.IsLetterOrDigit(s[j]) || s[j] == '_' || s[j] == '.')) j++;
                var ident = s[i..j];
                int k = j; while (k < s.Length && char.IsWhiteSpace(s[k])) k++;
                // an identifier directly followed by '(' is a function; otherwise it may be an unquoted table name
                if (At(s, k) != '(') pendingTable = ident;
                i = j; continue;
            }
            i++;
        }
        if (pendingTable != null) yield return (pendingTable, null);
    }
    static char At(string s, int i) => i < s.Length ? s[i] : '\0';
    static int SkipQuoted(string s, int i, char q)
    {
        int j = i + 1;
        while (j < s.Length) { if (s[j] == q) { if (At(s, j + 1) == q) { j += 2; continue; } return j + 1; } j++; }
        return s.Length;
    }
}
