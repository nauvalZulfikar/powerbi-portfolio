#!/usr/bin/env bash
# Everything in pbi-kit that can be tested without Windows or the Power BI Service.
#   tests/run-offline.sh [path/to/X.SemanticModel]   (optional real model, read-only)
set -uo pipefail
K="$(cd "$(dirname "$0")/.." && pwd)"
REAL="${1:-}"; [ -n "$REAL" ] && REAL="$(cd "$REAL" && pwd)"
export DOTNET_ROOT="${DOTNET_ROOT:-$HOME/.dotnet}"; export PATH="$DOTNET_ROOT:$PATH"
fail=0; ok() { echo "PASS  $1"; }; bad() { echo "FAIL  $1"; fail=1; }

free=$(df -g / | tail -1 | awk '{print $4}')
[ "$free" -lt 5 ] && { echo "disk guard: ${free} GB free < 5 — stop"; exit 3; }

FX="$K/tests/fixtures"
# 1 BPA fixture: expected findings present, ignored ones absent
out=$("$K/bpa/bpa.sh" "$FX/bpa-model/definition" 2>&1); code=$?
for want in "ERROR DAX_COLUMNS_FULLY_QUALIFIED .*'Sales'\[Bad\]" "ERROR DAX_MEASURES_UNQUALIFIED .*'Sales'\[Bad Ref\]" \
            "WARN  KIT_MEASURE_DESCRIPTION .*'Sales'\[Bad\]" "WARN  KIT_MEASURE_DISPLAY_FOLDER .*'Sales'\[Bad\]" \
            "WARN  HIDE_FOREIGN_KEYS .*'Sales'\[Customer Key\]"; do
  grep -q "$want" <<<"$out" || { bad "bpa fixture missing: $want"; }
done
grep -q "PROVIDE_FORMAT_STRING_FOR_MEASURES.*Label" <<<"$out" && bad "bpa object-level ignore annotation not honoured"
grep -q "^SKIP  ENSURE_TABLES_HAVE_RELATIONSHIPS: ignored by model annotation" <<<"$out" || bad "bpa model-level ignore not honoured"
grep -q "16 violations .* 1 skipped" <<<"$out" || bad "bpa fixture: expected 16 violations, only the ignored rule skipped"
[ $code -eq 1 ] && ok "bpa fixture (findings, ignores, exit 1 on severity 3)" || bad "bpa fixture exit $code"
grep -q "0 skipped" <<<"$("$K/bpa/bpa.sh" "$FX/bpa-model/definition" --no-default --rules "$K/bpa/project-rules.example.json")" \
  && ok "bpa project-rules example compiles" || bad "bpa project-rules example"

# 2 BPA on a real model (read-only)
if [ -n "$REAL" ]; then
  real=$("$K/bpa/bpa.sh" "$REAL/definition" 2>&1)
  grep -q "; 0 skipped" <<<"$real" && ok "bpa real model: $(grep '^BPA ' <<<"$real" | sed 's/.*: //')" || bad "bpa real model"
fi

# 3 format-check.csx compiles + runs against a stub of the TE scripting API
dotnet run --project "$K/tests/te-stub" >/dev/null 2>&1 && ok "te/format-check.csx (stub compile + run)" || bad "te/format-check.csx stub"

# 4 DAX tests offline
python3 "$K/dax-tests/daxtest.py" "$FX/dax-good" --model "$FX/bpa-model/definition" --offline >/dev/null && ok "daxtest good fixtures" || bad "daxtest good fixtures"
n=$(python3 "$K/dax-tests/daxtest.py" "$FX/dax-bad" --model "$FX/bpa-model/definition" --offline | grep -c '^FAIL')
[ "$n" -eq "$(ls "$FX"/dax-bad/*.dax | wc -l)" ] && ok "daxtest bad fixtures all rejected ($n)" || bad "daxtest bad fixtures: $n rejected"
python3 -c "
import sys; sys.path.insert(0,'$K/dax-tests'); import daxtest as d
assert d.check_asserts([{'[A]':1.2}],{'rows':1,'checks':[{'column':'[A]','equals':1,'tolerance':0.5}]})==[]
assert len(d.check_asserts([{'[A]':2}],{'rows':2,'checks':[{'column':'[A]','equals':1}]}))==2" && ok "daxtest assert logic" || bad "daxtest assert logic"
if [ -n "$REAL" ] && [ -d "$REAL/DAXQueries" ]; then
  python3 "$K/dax-tests/daxtest.py" "$REAL/DAXQueries" --offline >/dev/null && ok "daxtest real DAXQueries" || bad "daxtest real DAXQueries"
fi

# 5 Python compiles, YAML parses
PY="$K/.venv/bin/python"; [ -x "$PY" ] || { (cd "$K" && uv sync -q); }
"$PY" -m py_compile "$K/deploy/deploy.py" "$K/dax-tests/daxtest.py" && ok "py_compile" || bad "py_compile"
"$PY" -c "import yaml,sys; [yaml.safe_load(open(f)) for f in sys.argv[1:]]" "$K"/ci/*.yml "$K/deploy/parameter.example.yml" \
  && ok "yaml parse (ci templates, parameter.example.yml)" || bad "yaml parse"

# 6 deploy dry-run (offline: items + fabric-cicd's own parameter.yml validation)
if [ -n "$REAL" ]; then
  (cd "$(mktemp -d)" && "$PY" "$K/deploy/deploy.py" --repo "$(dirname "$REAL")" --env Test --parameter "$K/deploy/parameter.example.yml" --dry-run >/dev/null 2>&1) \
    && ok "deploy.py --dry-run (Test)" || bad "deploy.py --dry-run"
  (cd "$(mktemp -d)" && "$PY" "$K/deploy/deploy.py" --repo "$(dirname "$REAL")" --env Prod --parameter "$FX/parameter.bad.yml" --dry-run >/dev/null 2>&1) \
    && bad "deploy.py accepted a bad parameter.yml" || ok "deploy.py rejects bad parameter.yml"
fi

exit $fail
