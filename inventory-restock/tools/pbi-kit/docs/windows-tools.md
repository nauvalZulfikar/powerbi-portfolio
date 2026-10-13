# Windows-only tools (Desktop side of the program)

| Tool | Install | Used for |
|---|---|---|
| Power BI Desktop (latest, PBIP + TMDL preview features on) | Microsoft Store | open .pbip, Performance Analyzer, DAX query view, View as role |
| DAX Studio | https://daxstudio.org → installer | Server Timings, query plan, VertiPaq .vpax (`perf-lab/dax-studio.md`) |
| Tabular Editor 2 (free) | https://github.com/TabularEditor/TabularEditor/releases → `TabularEditor.Installer.msi` or Portable zip | BPA UI, `te/format-check.csx`, CLI `-A` in CI |
| Tabular Editor 3 (paid, optional) | tabulareditor.com | same + DAX debugger |

Both DAX Studio and TE2 register themselves under Desktop's **External Tools** ribbon.

## BPA in Tabular Editor 2 UI
Tools → Best Practice Analyzer → Manage rules → **Add → Rule file** → `tools/pbi-kit/bpa/BPARules.json`
(+ `bpa/project-rules.json`). Right-click a violation → "Ignore item" writes the same
`BestPracticeAnalyzer_IgnoreRules` annotation the Mac runner honours.

## TE2 CLI (what `bpa/te2-bpa.ps1` runs)
```
TabularEditor.exe "<X.SemanticModel>\definition" -A tools\pbi-kit\bpa\BPARules.json -G   # GitHub annotations (-V for Azure DevOps)
TabularEditor.exe "<X.SemanticModel>\definition" -S tools\pbi-kit\te\format-check.csx -TMDL "<X.SemanticModel>\definition"
```
Exit code 1 when any severity-3 rule is violated. `-A` takes one file, so the ps1 merges kit + project rules first.
