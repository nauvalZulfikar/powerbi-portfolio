# Branch protection for `main` (needs a GitHub remote — set once per repo)

Goal: nothing reaches `main` (and therefore Test/Prod) without a PR whose `ci` job is green.

## Settings
- Require a pull request before merging, with 1 approval (solo repo: 0 approvals, but the PR is still required so CI runs).
- Require status checks to pass: **`ci`** (the job name in `github-pbi-ci.yml`). Tick "require branches to be up to date".
- Require linear history; block force pushes and deletion.
- Environments (Settings → Environments):
  - `Test`: variables `FABRIC_WS_TEST`, `PBI_DATASET_NAME`; secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` (and `PBI_ACCESS_TOKEN` only for RLS models).
  - `Prod`: variable `FABRIC_WS_PROD`; **Required reviewers** = you. This is the approval gate before Prod.
  - Deployment branches for both: `main` only.

## One command (gh CLI, repo admin)
```bash
gh api -X PUT repos/{owner}/{repo}/branches/main/protection --input - <<'JSON'
{
  "required_status_checks": {"strict": true, "contexts": ["ci"]},
  "enforce_admins": false,
  "required_pull_request_reviews": {"required_approving_review_count": 0},
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
gh api -X PUT repos/{owner}/{repo}/environments/Prod --input - <<JSON
{"reviewers":[{"type":"User","id":$(gh api user --jq .id)}],
 "deployment_branch_policy":{"protected_branches":true,"custom_branch_policies":false}}
JSON
```
Branch protection on private repos needs GitHub Pro/Team; on a free plan make the repo public or rely on the Prod environment reviewer alone.

## Azure DevOps equivalent
Repos → Branches → main → Branch policies: require a minimum number of reviewers, Build validation = this pipeline (required).
Pipelines → Environments → Prod → Approvals and checks → Approvals.
