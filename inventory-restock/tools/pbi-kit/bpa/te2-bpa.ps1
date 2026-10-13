# Windows CI / Windows laptop: same BPARules.json through the real Tabular Editor 2 CLI.
#   pwsh tools/pbi-kit/bpa/te2-bpa.ps1 -Model powerbi/X.SemanticModel/definition [-ProjectRules bpa/rules.project.json] [-Ci github|vsts]
# TE2 -A takes one rule file, so kit + project rules are merged first (project wins on duplicate ID).
# Exit code comes from TabularEditor.exe: 1 if any severity >= 3 rule is violated.
param(
  [Parameter(Mandatory)] [string] $Model,
  [string] $ProjectRules = "",
  [ValidateSet("github", "vsts", "none")] [string] $Ci = "github",
  [string] $TeVersion = "2.29.0"
)
$ErrorActionPreference = "Stop"
$kit = Split-Path -Parent $PSCommandPath
$te = Join-Path $env:RUNNER_TEMP "te2"
if (-not $env:RUNNER_TEMP) { $te = Join-Path $env:TEMP "te2" }
if (-not (Test-Path "$te/TabularEditor.exe")) {
  New-Item -ItemType Directory -Force $te | Out-Null
  Invoke-WebRequest "https://github.com/TabularEditor/TabularEditor/releases/download/$TeVersion/TabularEditor.Portable.zip" -OutFile "$te/te.zip"
  Expand-Archive "$te/te.zip" -DestinationPath $te -Force
}
$rules = @{}
foreach ($f in @("$kit/BPARules.json", $ProjectRules) | Where-Object { $_ -and (Test-Path $_) }) {
  foreach ($r in (Get-Content $f -Raw | ConvertFrom-Json)) { $rules[$r.ID] = $r }
}
$merged = Join-Path $te "BPARules.merged.json"
ConvertTo-Json -InputObject @($rules.Values) -Depth 10 | Set-Content $merged -Encoding utf8
$flag = @{ github = "-G"; vsts = "-V"; none = "" }[$Ci]
$argList = "`"$((Resolve-Path $Model).Path)`" -A `"$merged`" $flag"
$p = Start-Process -FilePath "$te/TabularEditor.exe" -ArgumentList $argList -Wait -NoNewWindow -PassThru
exit $p.ExitCode
