# Perf lab — runs on Windows Power BI Desktop + DAX Studio only

Nothing here runs on the Mac. The Mac side writes the plan (which visual, which measure, what to expect);
you run it on Desktop, fill `results-template.md`, and drop the artefacts in `<project>/evidence/`.

| File | Use |
|---|---|
| `performance-analyzer.md` | checklist: record, read the three timings, isolate one visual, hand the query to DAX query view |
| `dax-studio.md` | checklist: Server Timings cold/warm, 5× median, query plan, xmSQL, CallbackDataID, EVALUATEANDLOG, VertiPaq .vpax |
| `results-template.md` | one copy per session → `<project>/evidence/<method>-perf.md` |
| `complaint-case.md` | "report is slow" → evidence → fix → re-measure, written as a case study |

## Evidence naming (matches `_plan`)
`<project>/evidence/<method#>-s<step#>.<ext>` — e.g. `8-s1.png` (Performance Analyzer pane), `8-s9.txt`
(Server Timings export), `8-s12.vpax` (VertiPaq Analyzer), `13-s4.txt` (query plan). One file per step;
screenshots PNG, text exports TXT, model stats VPAX. Commit them (small) — `.vpax` of a large model can be
several MB: keep it only if < 5 MB, otherwise commit the `VertiPaq Analyzer` summary screenshot instead.

## Rules
- Always measure the **same** visual/query before and after, same filters, same machine, same Desktop version.
- Report **cold** (Clear Cache) and **warm** separately. Warm = median of 5 runs. Never quote a single run.
- Write the Desktop version and model size (from .vpax) into the results file.
