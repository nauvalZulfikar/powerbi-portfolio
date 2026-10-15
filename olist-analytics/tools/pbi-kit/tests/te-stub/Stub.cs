// Minimal stand-in for TabularEditor.TOMWrapper members used by te/*.csx (TE2 names).
global using static TeStub.Globals;
namespace TeStub;
public class Table { public string Name = ""; public bool IsHidden; }
public class Measure
{
    public string Name = "", DisplayFolder = "", FormatString = "", FormatStringExpression = "", Description = "", Expression = "";
    public bool IsHidden; public Table Table = new();
}
public class Model { public List<Measure> AllMeasures = new(); }
public static class Globals
{
    public static Model Model = new() { AllMeasures = {
        new Measure { Name = "Win Rate", Table = new Table { Name = "_Measures" } },
        new Measure { Name = "Won Revenue", Table = new Table { Name = "_Measures" }, Description = "ok" },
        new Measure { Name = "Headline", Table = new Table { Name = "_Measures" }, DisplayFolder = "Text" },
    } };
    public static void Output(object o) => Console.WriteLine(o);
    public static void Info(string s) => Console.WriteLine(s);
}
