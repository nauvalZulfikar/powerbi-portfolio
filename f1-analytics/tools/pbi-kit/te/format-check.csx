// format-check.csx — Tabular Editor 2/3 C# script (pbi-kit)
//
// Checks every visible measure for displayFolder, formatString and description, and optionally fixes the
// first two in bulk. Run it:
//   * TE2/TE3 UI (External Tools ribbon in Power BI Desktop): C# Script tab → open file → Run → Ctrl+S
//   * TE2 CLI (Windows):  TabularEditor.exe "<X.SemanticModel>/definition" -S te/format-check.csx -TMDL "<X.SemanticModel>/definition"
//     (set PBIKIT_FIX=1 to write fixes; without it the script only reports and -TMDL rewrites nothing that changed)
// On the Mac there is no Tabular Editor: change the project generator (build_model.py) instead and let
// `bpa/bpa.sh` (rules KIT_MEASURE_DISPLAY_FOLDER / PROVIDE_FORMAT_STRING_FOR_MEASURES / KIT_MEASURE_DESCRIPTION) prove it.

var fix = Environment.GetEnvironmentVariable("PBIKIT_FIX") == "1";

// measures whose result is text/colour — never give these a number format
var textLike = new System.Text.RegularExpressions.Regex(@"(?i)(headline|label|title|text|colou?r|icon|abc|status|narrative|insight)");

Func<string, string> guessFormat = name =>
{
    var n = name.ToLowerInvariant();
    if (n.Contains("%") || n.Contains("rate") || n.Contains("share") || n.Contains("ratio") || n.Contains("pct") || n.Contains("margin"))
        return "0.0%;-0.0%;0.0%";
    if (n.Contains("revenue") || n.Contains("value") || n.Contains("amount") || n.Contains("cost") || n.Contains("price")
        || n.Contains("profit") || n.Contains("sales") || n.Contains("$"))
        return "\\$#,0;(\\$#,0);\\$#,0";
    if (n.Contains("days") || n.Contains("avg") || n.Contains("average") || n.Contains("median"))
        return "#,0.0";
    return "#,0";
};

int nFolder = 0, nFormat = 0, nDesc = 0;
var log = new System.Text.StringBuilder();
foreach (var m in Model.AllMeasures.Where(x => !x.IsHidden && !x.Table.IsHidden).OrderBy(x => x.Table.Name).ThenBy(x => x.Name))
{
    var id = "'" + m.Table.Name + "'[" + m.Name + "]";
    if (string.IsNullOrWhiteSpace(m.DisplayFolder))
    {
        nFolder++;
        var folder = m.Table.Name.TrimStart('_');
        log.AppendLine("folder  " + id + (fix ? "  -> " + folder : ""));
        if (fix) m.DisplayFolder = folder;
    }
    if (string.IsNullOrWhiteSpace(m.FormatString) && string.IsNullOrWhiteSpace(m.FormatStringExpression) && !textLike.IsMatch(m.Name))
    {
        nFormat++;
        var fmt = guessFormat(m.Name);
        log.AppendLine("format  " + id + (fix ? "  -> " + fmt : ""));
        if (fix) m.FormatString = fmt;
    }
    if (string.IsNullOrWhiteSpace(m.Description))
    {
        nDesc++;
        log.AppendLine("descr   " + id + "  (write it by hand — not guessed)");
    }
}

log.Insert(0, string.Format("format-check ({0}): {1} missing displayFolder, {2} missing formatString, {3} missing description\n",
    fix ? "FIX" : "CHECK", nFolder, nFormat, nDesc));
Output(log.ToString());
