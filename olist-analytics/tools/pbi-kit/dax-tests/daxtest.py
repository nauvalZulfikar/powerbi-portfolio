#!/usr/bin/env python3
"""DAX query tests for a PBIP semantic model. Python stdlib only.

    python daxtest.py <X.SemanticModel/DAXQueries | file.dax ...> [--model <X.SemanticModel/definition>] [--offline]

Every `name.dax` is checked offline: lexes cleanly (strings, 'tables', [names], comments closed), brackets
balance, has EVALUATE (DEFINE only before it), and every 'Table'[Column] / [Measure] it names exists in the
TMDL model (or is defined in the query). Unknown unqualified [names] are warnings: they may be ADDCOLUMNS
output columns.

A sibling `name.assert.json` turns the query into a value test:

    {"checks": [{"column": "[Won Revenue]", "row": 0, "equals": 10005534, "tolerance": 0.5}],
     "rows": 1}                      # optional: exact row count

Online mode runs asserted queries against a published model through the Power BI REST API
`executeQueries` (one EVALUATE per asserted file, one result table). It switches on when credentials exist:

    PBI_WORKSPACE_ID + (PBI_DATASET_ID | PBI_DATASET_NAME)
    and either PBI_ACCESS_TOKEN (e.g. `az account get-access-token --resource https://analysis.windows.net/powerbi/api`)
    or AZURE_TENANT_ID + AZURE_CLIENT_ID + AZURE_CLIENT_SECRET (service principal, client-credentials flow)

Limits from Microsoft: service principals cannot query models that have RLS roles — use a user token
(PBI_ACCESS_TOKEN) or PBI_IMPERSONATE_UPN won't help for SPs. Tenant settings "Dataset Execute Queries REST API"
and "Service principals can use Fabric APIs" must be on. Exit 1 on any error or failed assert.
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.powerbi.com/v1.0/myorg"


# ------------------------------------------------------------------------------------------- model
def _name(raw):
    raw = raw.strip()
    return raw[1:-1].replace("''", "'") if raw.startswith("'") and raw.endswith("'") else raw


def load_model(definition):
    """{'tables': {lower name: set(lower columns)}, 'measures': set(lower)} from TMDL text."""
    tables, measures = {}, set()
    for f in sorted(Path(definition, "tables").glob("*.tmdl")):
        current = None
        for line in f.read_text(encoding="utf-8-sig").splitlines():
            if m := re.match(r"^table\s+(.+?)\s*$", line):
                current = _name(m.group(1)).lower()
                tables.setdefault(current, set())
            elif current and (m := re.match(r"^\t(column|measure|calculationItem)\s+('(?:[^']|'')+'|[^=\s]+)", line)):
                kind, nm = m.group(1), _name(m.group(2)).lower()
                (measures.add(nm) if kind == "measure" else tables[current].add(nm))
    return {"tables": tables, "measures": measures}


def find_model(dax_file):
    for p in Path(dax_file).resolve().parents:
        if p.name.endswith(".SemanticModel") and (p / "definition").is_dir():
            return p / "definition"
    return None


# ------------------------------------------------------------------------------------------- lexer
TOKEN = re.compile(r"""
    (?P<ws>\s+) | (?P<lc>(?://|--)[^\n]*) | (?P<bc>/\*.*?\*/) |
    (?P<str>"(?:[^"]|"")*") | (?P<tbl>'(?:[^']|'')+') | (?P<col>\[(?:[^\]]|\]\])+\]) |
    (?P<num>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?) | (?P<id>[A-Za-z_][\w.]*) | (?P<op>[(){},=<>&|+\-*/^:;!]+|\.)
""", re.X | re.S)


def lex(text):
    pos, out = 0, []
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m:
            ch = text[pos]
            what = {'"': "string", "'": "quoted table name", "[": "[name]"}.get(ch, f"character {ch!r}")
            if text.startswith("/*", pos):
                what = "block comment"
            raise SyntaxError(f"unterminated {what} at line {text.count(chr(10), 0, pos) + 1}")
        kind = m.lastgroup
        if kind not in ("ws", "lc", "bc"):
            out.append((kind, m.group(), text.count("\n", 0, pos) + 1))
        pos = m.end()
    return out


def check_file(path, model):
    """returns (errors, warnings, n_evaluate, text)"""
    text = Path(path).read_text(encoding="utf-8-sig")
    errors, warnings = [], []
    try:
        toks = lex(text)
    except SyntaxError as e:
        return [str(e)], [], 0, text

    stack = []
    for kind, val, line in toks:
        if kind != "op":
            continue
        for ch in val:
            if ch in "({":
                stack.append((ch, line))
            elif ch in ")}":
                if not stack or {"(": ")", "{": "}"}[stack[-1][0]] != ch:
                    errors.append(f"line {line}: unmatched '{ch}'")
                    break
                stack.pop()
    errors += [f"line {line}: '{ch}' never closed" for ch, line in stack]

    words = [(v.upper(), line) for k, v, line in toks if k == "id"]
    evals = [line for w, line in words if w == "EVALUATE"]
    if not evals:
        errors.append("no EVALUATE statement")
    defines = [line for w, line in words if w == "DEFINE"]
    if defines and evals and defines[0] > evals[0]:
        errors.append(f"line {defines[0]}: DEFINE after EVALUATE")

    if model:
        defined_measures, defined_tables, literals = set(), set(), set()
        for i, (k, v, _) in enumerate(toks):
            if k == "id" and v.upper() in ("MEASURE", "COLUMN") and i + 1 < len(toks):
                name = toks[i + 2][1] if i + 2 < len(toks) and toks[i + 2][0] == "col" else None
                if name:
                    defined_measures.add(name[1:-1].replace("]]", "]").lower())
            if k == "id" and v.upper() in ("VAR", "TABLE") and i + 1 < len(toks) and toks[i + 1][0] in ("id", "tbl"):
                defined_tables.add(_name(toks[i + 1][1]).lower())
            if k == "str":
                literals.add(v[1:-1].replace('""', '"').lower())
        mt, mm = model["tables"], model["measures"]
        for i, (k, v, line) in enumerate(toks):
            if k != "col":
                continue
            name = v[1:-1].replace("]]", "]").lower()
            prev = toks[i - 1] if i else None
            if prev and prev[0] in ("tbl", "id") and (prev[0] == "tbl" or prev[1].upper() not in ("RETURN", "EVALUATE")):
                table = _name(prev[1]).lower()
                if table in mt:
                    if name not in mt[table] and name not in mm and name not in defined_measures:
                        errors.append(f"line {line}: {prev[1]}{v} — no such column/measure in model")
                elif table not in defined_tables and prev[0] == "tbl":
                    errors.append(f"line {line}: table {prev[1]} not in model")
            elif name not in mm and name not in defined_measures and name not in literals \
                    and not any(name in cols for cols in mt.values()):
                warnings.append(f"line {line}: {v} is not a model measure (fine if it is a query column)")
    return errors, warnings, len(evals), text


# ------------------------------------------------------------------------------------------- online
def _post(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8-sig"))


def token():
    if t := os.environ.get("PBI_ACCESS_TOKEN"):
        return t
    tenant, cid, secret = (os.environ.get(k) for k in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"))
    if not (tenant and cid and secret):
        return None
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid, "client_secret": secret,
                                   "scope": "https://analysis.windows.net/powerbi/api/.default"}).encode()
    return _post(f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", body,
                 {"Content-Type": "application/x-www-form-urlencoded"})["access_token"]


def dataset_id(tok, ws):
    if d := os.environ.get("PBI_DATASET_ID"):
        return d
    name = os.environ.get("PBI_DATASET_NAME")
    req = urllib.request.Request(f"{API}/groups/{ws}/datasets", headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        hits = [d["id"] for d in json.load(r)["value"] if d["name"] == name]
    if not hits:
        sys.exit(f"✗ dataset {name!r} not found in workspace {ws}")
    return hits[0]


def run_query(tok, ws, ds, query):
    body = {"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}}
    if upn := os.environ.get("PBI_IMPERSONATE_UPN"):
        body["impersonatedUserName"] = upn
    try:
        res = _post(f"{API}/groups/{ws}/datasets/{ds}/executeQueries", json.dumps(body).encode(),
                    {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:400]}")
    r0 = res["results"][0]
    err = res.get("error") or r0.get("error") or (r0.get("tables") or [{}])[0].get("error")
    if err:
        raise RuntimeError(f"{err.get('code')}: {err.get('message') or err}")
    return r0["tables"][0]["rows"]


def check_asserts(rows, spec):
    fails = []
    if "rows" in spec and len(rows) != spec["rows"]:
        fails.append(f"rows {len(rows)} != {spec['rows']}")
    for c in spec.get("checks", []):
        r = c.get("row", 0)
        if r >= len(rows):
            fails.append(f"row {r} missing")
            continue
        row = rows[r]
        col = c["column"]
        got = row.get(col, next((v for k, v in row.items() if k.endswith(col)), "<missing>"))
        want, tol = c["equals"], c.get("tolerance", 0)
        ok = (abs(got - want) <= tol) if isinstance(want, (int, float)) and isinstance(got, (int, float)) else got == want
        if not ok:
            fails.append(f"{col} row {r}: got {got!r}, want {want!r}" + (f" ±{tol}" if tol else ""))
    return fails


def validate_assert_spec(spec):
    errs = []
    if not isinstance(spec, dict) or not isinstance(spec.get("checks", []), list):
        return ["assert file must be an object with a 'checks' list"]
    for i, c in enumerate(spec.get("checks", [])):
        if not isinstance(c, dict) or "column" not in c or "equals" not in c:
            errs.append(f"check {i}: needs 'column' and 'equals'")
    if not spec.get("checks") and "rows" not in spec:
        errs.append("assert file has neither 'checks' nor 'rows'")
    return errs


# ------------------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--model", help="TMDL definition folder (default: the enclosing X.SemanticModel/definition)")
    ap.add_argument("--offline", action="store_true", help="never call the service")
    a = ap.parse_args()

    files = sorted({f for p in a.paths for f in ([Path(p)] if Path(p).is_file() else Path(p).rglob("*.dax"))})
    if not files:
        sys.exit("✗ no .dax files found")
    ws = os.environ.get("PBI_WORKSPACE_ID")
    tok = None if a.offline or not ws else token()
    online = bool(tok and (os.environ.get("PBI_DATASET_ID") or os.environ.get("PBI_DATASET_NAME")))
    ds = dataset_id(tok, ws) if online else None
    print(f"DAX tests: {len(files)} file(s), mode {'ONLINE (executeQueries)' if online else 'offline (syntax/structure/references)'}")

    models, failed, n_assert = {}, 0, 0
    for f in files:
        mdef = Path(a.model) if a.model else find_model(f)
        if mdef and mdef not in models:
            models[mdef] = load_model(mdef)
        errors, warnings, n_eval, text = check_file(f, models.get(mdef))
        spec_file = f.with_suffix(".assert.json")
        spec = None
        if spec_file.exists():
            n_assert += 1
            try:
                spec = json.loads(spec_file.read_text(encoding="utf-8"))
                errors += validate_assert_spec(spec)
            except json.JSONDecodeError as e:
                errors.append(f"{spec_file.name}: invalid JSON ({e})")
            if n_eval > 1:
                errors.append("asserted query must have exactly one EVALUATE (executeQueries returns one table)")
        if online and spec and not errors:
            try:
                errors += check_asserts(run_query(tok, ws, ds, text), spec)
            except RuntimeError as e:
                errors.append(str(e))
        status = "FAIL" if errors else "ok  "
        print(f"{status} {f.name}" + (" [assert]" if spec is not None else "") + ("" if mdef else "  (no model found: references not checked)"))
        for e in errors:
            print(f"       ✗ {e}")
        for w in warnings:
            print(f"       ! {w}")
        failed += bool(errors)
    print(f"{len(files) - failed}/{len(files)} passed, {n_assert} with assert file" +
          ("" if online else "; asserts NOT executed (offline)"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
