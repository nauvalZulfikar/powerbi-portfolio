"""Service build: turn a generated local semantic model into one that reads its files from a Fabric Lakehouse.

Post-processes the TMDL a generator just wrote: every `File.Contents(<FolderParam> & "x")` becomes
`fnOneLake("<repo-relative folder>/x")`, so all downstream M steps (Parquet.Document, Excel.Workbook, ...) stay as
they are. Adds the OneLakeFolder parameter + fnOneLake function and writes upload.json (local file -> OneLake path).

Reusable: a project only supplies FOLDERS = {M folder parameter: repo-relative folder}.
"""
import json
import re
from pathlib import Path

DEFAULT_URL = "https://onelake.dfs.fabric.microsoft.com/WORKSPACE_ID/LAKEHOUSE_ID/Files/{project}/"
FN = """expression fnOneLake =
\t\t(relPath as text) as binary =>
\t\tlet
\t\t    // One file from the lakehouse Files area (ADLS Gen2 connector); relPath mirrors the repo layout
\t\t    Source = AzureStorage.DataLake(OneLakeFolder, [HierarchicalNavigation = false]),
\t\t    Match = Table.SelectRows(Source, each [Folder Path] & [Name] = OneLakeFolder & relPath),
\t\t    File = Match{{0}}[Content]
\t\tin
\t\t    File
\tlineageTag: {tag_fn}

\tannotation PBI_ResultType = Function

expression OneLakeFolder = "{url}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]
\tlineageTag: {tag_param}

\tannotation PBI_ResultType = Text
"""


def to_service(model_dir, root, folders, project, tag):
    """Rewrite file reads in model_dir/definition; return the upload list. tag(kind, name) -> lineage tag."""
    model_dir, root = Path(model_dir), Path(root)
    pat = re.compile(r'File\.Contents\((' + "|".join(map(re.escape, folders)) + r') & "([^"]+)"\)')
    reads, files = 0, {}

    def sub(m):
        nonlocal reads
        rel = f"{folders[m[1]].strip('/')}/{m[2]}"
        reads += 1
        files[rel] = root / rel
        return f'fnOneLake("{rel}")'

    for f in sorted((model_dir / "definition").rglob("*.tmdl")):
        text = f.read_text()
        new = pat.sub(sub, text)
        if new != text:
            f.write_text(new)
    left = [f.name for f in (model_dir / "definition").rglob("*.tmdl")
            if re.search(r"File\.Contents|Folder\.Files|Csv\.Document\(File", f.read_text())]
    assert not left, f"local file reads left in {left}: extend FOLDERS / to_service"
    exp = model_dir / "definition" / "expressions.tmdl"
    exp.write_text(exp.read_text().rstrip("\n") + "\n\n" + FN.format(
        url=DEFAULT_URL.format(project=project), tag_fn=tag("expr", "fnOneLake"),
        tag_param=tag("expr", "OneLakeFolder")))
    upload = [{"local": rel, "remote": rel, "bytes": p.stat().st_size} for rel, p in sorted(files.items())]
    (model_dir.parent / "upload.json").write_text(json.dumps(upload, indent=2) + "\n")
    print(f"  ✓ service model: {reads} file reads -> fnOneLake, {len(upload)} files "
          f"({sum(u['bytes'] for u in upload):,} bytes) in {model_dir.parent.name}/upload.json")
    return upload
