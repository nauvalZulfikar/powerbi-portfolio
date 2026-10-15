"""Pull the tickets of four engineering teams from the Apache Software Foundation's public Jira.

    uv run python src/extract.py        -> data/raw/<PROJECT>.jsonl.gz (one issue per line)

Window: issues created 1 Oct 2024 - 30 Sep 2026, with their full change history. Each page is
cached, so a rerun only fetches what is missing. Only the fields the model needs are kept;
avatars, descriptions and bot noise (worklogs, links, attachments) are dropped.
"""
import gzip
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
API = "https://issues.apache.org/jira/rest/api/2/search"
PROJECTS = ["HIVE", "CASSANDRA", "HBASE", "ARTEMIS"]
WINDOW = 'created >= "2024-10-01" AND created < "2026-10-01"'
FIELDS = "summary,issuetype,priority,status,resolution,created,resolutiondate,assignee,reporter,components,labels"
KEEP = {"status", "assignee", "resolution", "priority", "issuetype", "Component"}
PAGE = 100


def get(params):
    url = f"{API}?{urllib.parse.urlencode(params)}"
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                return json.load(r)
        except Exception as e:  # the ASF Jira times out now and then; back off and retry
            print(f"    retry {attempt + 1}: {e}")
            time.sleep(30 * (attempt + 1))
    raise RuntimeError(url)


def user(u):
    return u and {"key": u.get("key"), "name": u["name"], "display": u["displayName"]}


def slim(issue):
    f = issue["fields"]
    name = lambda x: x and x["name"]
    return {"key": issue["key"], "summary": f["summary"], "type": name(f["issuetype"]), "priority": name(f["priority"]),
            "status": name(f["status"]), "resolution": name(f["resolution"]), "created": f["created"],
            "resolved": f["resolutiondate"], "assignee": user(f["assignee"]), "reporter": user(f["reporter"]),
            "components": [c["name"] for c in f["components"]], "labels": f["labels"],
            "history": [{"at": h["created"], "by": user(h.get("author")),
                         "items": [{k: i[k] for k in ("field", "from", "fromString", "to", "toString")}
                                   for i in h["items"] if i["field"] in KEEP]}
                        for h in issue["changelog"]["histories"] if any(i["field"] in KEEP for i in h["items"])]}


def project(key):
    out = RAW / f"{key}.jsonl.gz"
    if out.exists():
        print(f"  {key}: cached")
        return
    pages = RAW / "pages" / key
    pages.mkdir(parents=True, exist_ok=True)
    jql = f"project = {key} AND {WINDOW} ORDER BY key ASC"
    total = get({"jql": jql, "maxResults": 0})["total"]
    for start in range(0, total, PAGE):
        page = pages / f"{start:05d}.json.gz"
        if page.exists():
            continue
        t = time.time()
        data = get({"jql": jql, "startAt": start, "maxResults": PAGE, "fields": FIELDS, "expand": "changelog"})
        page.write_bytes(gzip.compress(json.dumps([slim(i) for i in data["issues"]]).encode()))
        print(f"  {key} {start + len(data['issues']):>5}/{total} ({time.time() - t:.0f}s)", flush=True)
        time.sleep(2)
    issues = [i for p in sorted(pages.glob("*.json.gz")) for i in json.loads(gzip.decompress(p.read_bytes()))]
    assert len({i["key"] for i in issues}) == len(issues) == total, (key, len(issues), total)
    with gzip.open(out, "wt") as f:
        f.writelines(json.dumps(i) + "\n" for i in issues)
    print(f"  {key}: {len(issues):,} issues")


if __name__ == "__main__":
    for p in PROJECTS:
        project(p)
