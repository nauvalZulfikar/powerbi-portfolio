# Four ways to ship a Power BI / Fabric project — and which one the kit uses

| # | Option | How it moves content | Good for | Limits |
|---|---|---|---|---|
| 1 | **Fabric Git integration** | Workspace ↔ Git branch sync (Commit / Update in the Service) | one dev workspace mirrored to a repo | it syncs, it doesn't test or promote; needs Fabric/PPU capacity |
| 2 | **Deployment pipelines** (Service UI) | Dev → Test → Prod stages, deployment rules swap parameters/data sources | business teams, no YAML | no code-review gate; rules are per-item clicks; Premium/PPU/Fabric |
| 3 | **fabric-cicd** (Python, Microsoft) | CI publishes PBIP folders from the repo to each workspace; `parameter.yml` swaps per-env values | repo is the source of truth, tests run before publish | needs a service principal (or user) with workspace rights |
| 4 | **REST / PowerShell / Fabric CLI** scripts | Import PBIX / Items API calls you write yourself | one-off or unusual flows | you maintain everything |

**Kit choice: 3 (Git + fabric-cicd) driven by GitHub Actions**, because the repo already is the source of
truth (generators write the TMDL/PBIR), and CI can block a bad model (BPA, TOM, DAX tests) before it
reaches any workspace. Options 1 and 2 are documented below because phase-E projects demo them too:
1 for "how a Desktop developer commits from the Service", 2 for "how a business owner promotes".

## Fabric Git integration (option 1) — steps
1. Workspace settings → Git integration → connect GitHub/Azure DevOps repo, branch `dev`, folder `powerbi/`.
2. Edit in the Service → **Source control → Commit** (writes PBIP/TMDL to the branch).
3. Change in Git (PR merged) → **Source control → Update all** (pulls into the workspace).
4. Conflicts: resolve in Git, then Update. Never edit the same item in Desktop and Service at once.

## Deployment pipeline (option 2) — checklist
- [ ] Create pipeline with stages Development / Test / Production, assign one workspace per stage.
- [ ] Deployment rules on Test and Prod: parameter rules for each `expression` parameter (SqlEndpoint, LakehouseDb, DataFolder…), data source rules if any.
- [ ] Deploy Dev → Test, refresh, check; Test → Prod with "deploy selected" + note.
- [ ] Compare view shows "different" items before each deploy — screenshot for evidence.
- [ ] Access: pipeline admin vs workspace roles (contributors can't deploy to Prod).
