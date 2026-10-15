#!/usr/bin/env bash
# Mac/Linux BPA: bpa.sh <X.SemanticModel/definition> [--rules project-rules.json] [--fail-on 3] [--json out.json]
# Builds the TOM-based runner on first use (or when its source/rules changed), then runs it.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
export DOTNET_ROOT=${DOTNET_ROOT:-$HOME/.dotnet}
export PATH="$DOTNET_ROOT:$PATH"
dll="$here/runner/bin/Release/net8.0/bpa-run.dll"
if [ ! -f "$dll" ] || [ -n "$(find "$here/runner/Program.cs" "$here/BPARules.json" -newer "$dll" 2>/dev/null)" ]; then
  dotnet build "$here/runner" -c Release -nologo -v q >/dev/null
fi
exec dotnet "$dll" "$@"
