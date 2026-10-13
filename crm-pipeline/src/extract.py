"""Incremental CRM extractor: REST API -> DuckDB raw layer.

    uv run python src/extract.py --base-url http://127.0.0.1:8765 --token pat-local-demo

Each run:
  * reloads the small reference objects (owners, companies, products) in full;
  * pulls only deals modified since the last watermark (search on hs_lastmodifieddate GTE watermark,
    so nothing that changed on the watermark day is missed), and appends every new deal *version*
    to raw.deal_versions. The CRM only knows a deal's current state; keeping the versions is what
    lets the warehouse rebuild the pipeline as it stood at each sync;
  * logs the run (requests, retries, rows, watermark before/after) to raw.sync_runs.

Retries: 429 waits for Retry-After; 5xx and connection errors back off exponentially; 4xx fail fast.
"""
import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import duckdb

PAGE = 100
DDL = """
create schema if not exists raw;
create table if not exists raw.owners (id varchar, email varchar, first_name varchar, last_name varchar,
    team varchar, synced_at date);
create table if not exists raw.companies (id varchar, properties json, synced_at date);
create table if not exists raw.products (id varchar, properties json, synced_at date);
create table if not exists raw.deal_versions (id varchar, modified_at timestamp, properties json,
    synced_at date, primary key (id, modified_at));
create table if not exists raw.sync_runs (run_id varchar, as_of date, mode varchar, started_at timestamp,
    seconds double, requests int, retries int, rate_limited int, server_errors int,
    owners int, companies int, products int, deals_seen int, deal_versions_added int,
    watermark_before timestamp, watermark_after timestamp);
"""


class Client:
    def __init__(self, base_url, token, max_tries=6):
        self.base, self.token, self.max_tries = base_url.rstrip("/"), token, max_tries
        self.stats = dict(requests=0, retries=0, rate_limited=0, server_errors=0)

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(self.max_tries):
            req = urllib.request.Request(self.base + path, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            self.stats["requests"] += 1
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    self.stats["rate_limited"] += 1
                    wait = float(e.headers.get("Retry-After") or 1)
                elif e.code >= 500:
                    self.stats["server_errors"] += 1
                    wait = 0.25 * 2 ** attempt
                else:
                    raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}") from None
            except urllib.error.URLError:
                wait = 0.25 * 2 ** attempt
            self.stats["retries"] += 1
            time.sleep(wait)
        raise RuntimeError(f"{method} {path}: gave up after {self.max_tries} tries")

    def list_all(self, path):
        after = None
        while True:
            out = self.call("GET", f"{path}?limit={PAGE}" + (f"&after={after}" if after else ""))
            yield from out["results"]
            if not (after := out.get("paging", {}).get("next", {}).get("after")):
                return

    def search_deals(self, since_ms=None):
        body = {"limit": PAGE, "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}]}
        if since_ms is not None:
            body["filterGroups"] = [{"filters": [
                {"propertyName": "hs_lastmodifieddate", "operator": "GTE", "value": str(since_ms)}]}]
        while True:
            out = self.call("POST", "/crm/v3/objects/deals/search", body)
            yield from out["results"]
            if not (after := out.get("paging", {}).get("next", {}).get("after")):
                return
            body["after"] = after


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None) if s else None


def sync(con, client, as_of):
    """One extraction run. as_of is the run's business date (the day the CRM is read)."""
    con.execute(DDL)
    t0 = time.perf_counter()
    started = datetime.now(timezone.utc).replace(tzinfo=None)
    before = dict(client.stats)
    wm = con.execute("select max(modified_at) from raw.deal_versions").fetchone()[0]
    mode = "incremental" if wm else "full"

    owners = list(client.list_all("/crm/v3/owners"))
    con.execute("delete from raw.owners")
    con.executemany("insert into raw.owners values (?, ?, ?, ?, ?, ?)", [
        (o["id"], o["email"], o["firstName"], o["lastName"], next(t["name"] for t in o["teams"] if t["primary"]), as_of)
        for o in owners])
    refs = {}
    for obj in ("companies", "products"):
        rows = list(client.list_all(f"/crm/v3/objects/{obj}"))
        con.execute(f"delete from raw.{obj}")
        con.executemany(f"insert into raw.{obj} values (?, ?, ?)",
                        [(r["id"], json.dumps(r["properties"]), as_of) for r in rows])
        refs[obj] = len(rows)

    since = int(wm.replace(tzinfo=timezone.utc).timestamp() * 1000) if wm else None
    deals = list(client.search_deals(since))
    n0 = con.execute("select count(*) from raw.deal_versions").fetchone()[0]
    con.executemany("insert or ignore into raw.deal_versions values (?, ?, ?, ?)", [
        (d["id"], ts(d["properties"]["hs_lastmodifieddate"]), json.dumps(d["properties"]), as_of) for d in deals])
    added = con.execute("select count(*) from raw.deal_versions").fetchone()[0] - n0
    wm_after = con.execute("select max(modified_at) from raw.deal_versions").fetchone()[0]

    delta = {k: client.stats[k] - before[k] for k in client.stats}
    con.execute("insert into raw.sync_runs values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        f"sync-{as_of}", as_of, mode, started, round(time.perf_counter() - t0, 2), delta["requests"], delta["retries"],
        delta["rate_limited"], delta["server_errors"], len(owners), refs["companies"], refs["products"], len(deals),
        added, wm, wm_after])
    return mode, len(deals), added, delta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8765")
    ap.add_argument("--token", default="pat-local-demo")
    ap.add_argument("--db", default="data/warehouse/crm.duckdb")
    ap.add_argument("--as-of", default=datetime.now().date().isoformat())
    a = ap.parse_args()
    con = duckdb.connect(a.db)
    mode, seen, added, d = sync(con, Client(a.base_url, a.token), a.as_of)
    print(f"{mode}: {seen} deals seen, {added} new versions, {d['requests']} requests, {d['retries']} retries")


if __name__ == "__main__":
    main()
