#!/usr/bin/env python3
"""Publish a project's PBIP items to a Fabric/Power BI workspace with fabric-cicd.

    uv run --project tools/pbi-kit python tools/pbi-kit/deploy/deploy.py --repo powerbi --env Test [--dry-run]

--repo     folder holding the X.SemanticModel / X.Report items (+ parameter.yml)
--env      Test | Prod — selects the replace_value column in parameter.yml and the workspace
--items    item types in scope (default SemanticModel Report)
--parameter  parameter.yml path (default <repo>/parameter.yml)
--unpublish-orphans  delete workspace items of the in-scope types that no longer exist in the repo
--dry-run  no network, no credentials: list items, validate parameter.yml with fabric-cicd's own
           validator and show which find_value matches which file

Workspace id: FABRIC_WS_<ENV> (e.g. FABRIC_WS_TEST) or --workspace-id.
Credentials: AZURE_TENANT_ID + AZURE_CLIENT_ID + AZURE_CLIENT_SECRET (service principal), otherwise
azure-identity's DefaultAzureCredential (az login, OIDC federated token in GitHub Actions, managed identity).
"""
import argparse
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

import yaml


def items(repo, scope):
    out = []
    for plat in sorted(Path(repo).glob("*/.platform")):
        meta = json.loads(plat.read_text(encoding="utf-8-sig"))["metadata"]
        if meta["type"] in scope:
            out.append((meta["type"], meta["displayName"], plat.parent))
    return out


def dry_run(repo, env, scope, param_file):
    found = items(repo, scope)
    print(f"items in scope ({', '.join(scope)}): {len(found)}")
    for t, n, p in found:
        print(f"  {t:<14} {n!r:<36} {p.name}")
    if not found:
        print("✗ no items with a .platform file in scope")
        return 1
    if not param_file.exists():
        print(f"no {param_file.name}: items publish with committed values")
        return 0

    spec = yaml.safe_load(param_file.read_text(encoding="utf-8")) or {}
    problems = 0
    for i, r in enumerate(spec.get("find_replace") or []):
        if env not in (r.get("replace_value") or {}):
            print(f"✗ find_replace[{i}]: no value for env {env!r}")
            problems += 1
            continue
        rx = re.compile(r["find_value"] if str(r.get("is_regex", "")).lower() == "true" else re.escape(r["find_value"]))
        hits = []
        for t, n, p in found:
            if r.get("item_type") and r["item_type"] != t:
                continue
            for f in p.rglob("*"):
                rel = f.relative_to(repo).as_posix()
                if f.is_file() and (not r.get("file_path") or fnmatch.fnmatch(rel, r["file_path"]) or fnmatch.fnmatch(rel, "*/" + r["file_path"].lstrip("*/"))):
                    try:
                        if rx.search(f.read_text(encoding="utf-8-sig")):
                            hits.append(rel)
                    except UnicodeDecodeError:
                        pass
        print(f"  find_replace[{i}] {r['find_value'][:48]!r} → {r['replace_value'][env]!r}: "
              + (", ".join(hits) if hits else "NO MATCH (nothing would change)"))
    # fabric-cicd's own schema validation — offline, same code the real publish runs
    from fabric_cicd._parameter._utils import validate_parameter_file
    ok = validate_parameter_file(repository_directory=str(Path(repo).resolve()), item_type_in_scope=scope,
                                 environment=env, parameter_file_path=str(param_file.resolve()))
    print(f"fabric-cicd parameter validation: {'OK' if ok else 'FAILED'}")
    return 0 if ok and not problems else 1


def credential():
    from azure.identity import ClientSecretCredential, DefaultAzureCredential
    keys = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
    if all(os.environ.get(k) for k in keys):
        return ClientSecretCredential(*(os.environ[k] for k in keys))
    return DefaultAzureCredential()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--env", required=True, choices=["Test", "Prod"])
    ap.add_argument("--items", nargs="+", default=["SemanticModel", "Report"])
    ap.add_argument("--parameter")
    ap.add_argument("--workspace-id")
    ap.add_argument("--unpublish-orphans", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    repo = Path(a.repo)
    param = Path(a.parameter) if a.parameter else repo / "parameter.yml"
    print(f"deploy {repo} → {a.env}{' (DRY RUN)' if a.dry_run else ''}")
    if a.dry_run:
        sys.exit(dry_run(repo, a.env, a.items, param))

    ws = a.workspace_id or os.environ.get(f"FABRIC_WS_{a.env.upper()}")
    if not ws:
        sys.exit(f"✗ no workspace id: set FABRIC_WS_{a.env.upper()} or --workspace-id")
    from fabric_cicd import FabricWorkspace, publish_all_items, unpublish_all_orphan_items
    kw = {"parameter_file_path": str(param.resolve())} if a.parameter else {}
    target = FabricWorkspace(workspace_id=ws, environment=a.env, repository_directory=str(repo.resolve()),
                             item_type_in_scope=a.items, token_credential=credential(), **kw)
    publish_all_items(target)
    if a.unpublish_orphans:
        unpublish_all_orphan_items(target)
    print(f"✓ published {len(items(repo, a.items))} item(s) to workspace {ws} ({a.env})")


if __name__ == "__main__":
    main()
