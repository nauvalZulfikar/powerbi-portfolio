# pbi-kit — shared Power BI CI/CD toolkit

Shared by the 9 projects in the method-coverage program (`../../_plan/README.md`). This covers every `| KIT |`
row in `_plan/<project>.md`. Each project vendors a copy in phase E.

| Part | Path | Runs on |
|---|---|---|
| BPA rules: 71 Microsoft rules + 3 kit rules (measure displayFolder / description / name ≠ column) | `bpa/BPARules.json` | — |
| BPA runner on TOM, so no Tabular Editor needed | `bpa/bpa.sh` → `bpa/runner` (.NET 8, TOM 19.117, System.Linq.Dynamic.Core) | Mac / Linux / CI |
| BPA through Tabular Editor 2 itself | `bpa/te2-bpa.ps1` (downloads TE2 portable, merges rules, `-A … -G/-V`) | Windows |
| Per-project rule examples (Restock→Stock.*, rate→LC.MaturedOnly, variance +/-, _Ukuran description) | `bpa/project-rules.example.json` | — |
| Bulk displayFolder/formatString check + fix | `te/format-check.csx` | TE2/TE3 (Windows) |
| DAX tests: `.dax` + `.assert.json`; offline checks syntax, structure and model refs, online runs `executeQueries` | `dax-tests/daxtest.py` (stdlib only) | offline anywhere; online needs Service |
| Deploy with fabric-cicd, values per env from `parameter.yml` | `deploy/deploy.py`, `deploy/parameter.example.yml` | dry-run anywhere; publish needs Service |
| CI: data tests → BPA → TOM → DAX → deploy Test → online DAX → approval → Prod | `ci/github-pbi-ci.yml`, `ci/azure-pipelines.yml`, `ci/branch-protection.md` | GitHub / Azure DevOps |
| Perf lab (Performance Analyzer, DAX Studio, results + complaint case templates) | `perf-lab/` | Windows Desktop |
| Docs: 4 CI/CD options, Fabric Git, deployment pipeline, service principal, roles/flow, Windows tools | `docs/` | — |
| PBIP `.gitignore` | `templates/pbip.gitignore` | — |
| Offline test suite for the kit | `tests/run-offline.sh [X.SemanticModel]` | Mac |

## BPA
```bash
tools/pbi-kit/bpa/bpa.sh powerbi/X.SemanticModel/definition                       # kit rules
tools/pbi-kit/bpa/bpa.sh <def> --rules bpa/project-rules.json --json bpa.json     # + project rules; same ID overrides
tools/pbi-kit/bpa/bpa.sh <def> --fail-on 2                                        # warnings also fail
```
- Exit 1 on any severity ≥ 3 (same as TE2 `-A`). Exit 2 when the model doesn't load.
- Rules embedded in the model (`BestPracticeAnalyzer` annotation) are applied too.
- **To ignore a rule**, add an annotation to the object or to the model. Tabular Editor writes the same annotation when you click "Ignore item".
  ```
  annotation BestPracticeAnalyzer_IgnoreRules = {"RuleIDs":["PROVIDE_FORMAT_STRING_FOR_MEASURES"]}
  ```
  Text measures (Headline, colour) are the typical case.
- The runner translates TE's rule dialect to Dynamic LINQ. A rule that still fails to compile shows as `SKIP` and does not fail the build.

Known gaps compared with TE:
- TMDL has no VertiPaq statistics, so cardinality-based rules never fire.
- Dependencies are found by a lexical DAX scan, not TE's parser.
- KPI, Variation and Culture scopes are not modelled.
- `UNNECESSARY_COLUMNS` doesn't know what the report uses. TE has the same gap.

Use the `bpa-te2` CI job (Windows) when you need TE's own verdict.

## DAX tests
```bash
python3 tools/pbi-kit/dax-tests/daxtest.py powerbi/X.SemanticModel/DAXQueries --offline
```
Put `name.assert.json` next to `name.dax`. Example:
```json
{"rows":1,"checks":[{"column":"[Won Revenue]","equals":10005534,"tolerance":0.5}]}
```
An asserted query must have exactly one EVALUATE. Offline mode only validates the assert file. Online mode runs the query on the published model:
```bash
PBI_WORKSPACE_ID=… PBI_DATASET_NAME=… PBI_ACCESS_TOKEN=$(az account get-access-token --resource https://analysis.windows.net/powerbi/api --query accessToken -o tsv) \
  python3 tools/pbi-kit/dax-tests/daxtest.py powerbi/X.SemanticModel/DAXQueries
```
**Microsoft does not allow service principals to run executeQueries on models with RLS roles**, so RLS models (e.g. crm) need a user token. See `docs/service-principal.md`.

## Deploy
```bash
uv run --project tools/pbi-kit python tools/pbi-kit/deploy/deploy.py --repo powerbi --env Test --dry-run
FABRIC_WS_TEST=<guid> uv run --project tools/pbi-kit python tools/pbi-kit/deploy/deploy.py --repo powerbi --env Test
```
- `--dry-run` is offline. It lists the items, shows which file each `find_value` hits, and runs fabric-cicd's own `parameter.yml` validator.
- Credentials: `AZURE_TENANT_ID/CLIENT_ID/CLIENT_SECRET`, or else `DefaultAzureCredential` (az login, GitHub OIDC).
- The Python environment is `uv` (`pyproject.toml`: fabric-cicd 1.4.0 + pyyaml).

## Installing into a project (phase E — not done here)
1. Copy the kit, without build or venv folders:
   ```bash
   rsync -a --exclude .git --exclude .venv --exclude bin --exclude obj tools/pbi-kit/ <project>/tools/pbi-kit/
   ```
   The project keeps its own `tools/tmdl-validate`.
   - f1, lendingclub and olist still use `Microsoft.AnalysisServices.NetCore.retail.amd64` 19.84.1 and should move to `Microsoft.AnalysisServices` 19.117.
   - bearing-forecast has no validator yet. Copy crm's.
2. Set up the project files:
   - `ci/github-pbi-ci.yml` → `<project>/.github/workflows/ci.yml`. Set `MODEL`, `PBIP_DIR` and the data-test command, then merge it with the project's existing CI steps (generators, m-validate, preview).
   - finance-variance also gets `ci/azure-pipelines.yml`.
   - `deploy/parameter.example.yml` → `<project>/powerbi/parameter.yml`. Keep only the parameters that model actually has (SqlEndpoint, LakehouseDb, DataFolder, InboxFolder, RawFolder, SourceMode, TestUpn).
   - Project rules → `<project>/bpa/project-rules.json`. Start from `bpa/project-rules.example.json`.
   - `templates/pbip.gitignore` → merge into `<project>/.gitignore`.
3. Write the DAX tests: `DAXQueries/<name>.dax` + `.assert.json`, using the plan's numbers (crm $10,005,534 / 63.2% / 1,437 stale, …).
4. Run these locally. All of them must be green before pushing:
   - `bpa.sh`
   - `daxtest.py --offline`
   - `deploy.py --dry-run`
   - `tests/run-offline.sh <project>/powerbi/X.SemanticModel`
5. One-time setup on GitHub and in the Service: `ci/branch-protection.md` and `docs/service-principal.md`.

## What needs what
| Needs | Items |
|---|---|
| Mac only (done, tested) | BPA runner, DAX offline, deploy dry-run, YAML/py checks, csx compile against a TE API stub |
| Windows | `te2-bpa.ps1`, `format-check.csx` inside real TE, Performance Analyzer, DAX Studio, Desktop "View as" for RLS |
| Power BI Service / Fabric | `deploy.py` publish, refresh, online DAX tests, Fabric Git integration, deployment pipelines, SP + tenant settings |
| GitHub remote | branch protection, environments + approval, running the workflow |

Not yet tested anywhere:
- `te2-bpa.ps1`, since there's no pwsh on this Mac.
- The CI workflows, which have only been YAML-parsed. actionlint isn't installed.
- fabric-cicd publish, online executeQueries and the refresh step.
- The csx in real Tabular Editor. It was only compiled against a stub of TE's API.
