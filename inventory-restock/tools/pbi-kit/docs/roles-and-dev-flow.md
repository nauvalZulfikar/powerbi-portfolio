# Who does what, and the dev flow (template — copy into the project README in phase E)

| Role | Person (portfolio: all you) | Does | Never |
|---|---|---|---|
| Model developer | | edits generators / TMDL, writes DAX tests, opens PR | publishes to Prod by hand |
| Report developer | | edits PBIR, page layout | changes measures without a test |
| Reviewer | | reviews PR, reads BPA + DAX test output | merges a red `ci` |
| Release approver | | approves the Prod environment | approves own unreviewed change (solo: note it) |
| Data owner | | signs off numbers (assert values) | |
| Workspace admin | | workspace roles, SP, gateway | |

## Flow
1. `git switch -c feat/<thing>` → change generator / model.
2. Local: regenerate → `tools/pbi-kit/bpa/bpa.sh <model>/definition` → `dotnet run --project tools/tmdl-validate -- <model>/definition` → `python3 tools/pbi-kit/dax-tests/daxtest.py <model>/DAXQueries --offline`.
3. Windows Desktop (when the change is visual or perf): open .pbip, check, perf-lab if needed → evidence.
4. Push → PR → `ci` green → review → merge.
5. `main` → deploy Test (fabric-cicd) → refresh → online DAX tests → **approval** → deploy Prod.
6. Tag the release `vYYYY.MM.DD`; changelog line in README.

## Git hygiene for PBIP
- Copy `templates/pbip.gitignore` → `.gitignore` (cache.abf, localSettings.json, data/raw, .venv, bin/obj).
- One commit = one logical change; TMDL diffs are readable, review them.
- Never commit `.pbix` next to the `.pbip` of the same model.
