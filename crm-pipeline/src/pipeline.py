"""End-to-end build: CRM API -> incremental raw layer -> DuckDB staging -> star schema -> tests -> Parquet marts.

    uv run python src/pipeline.py

Replays a year of month-end syncs against the local CRM API (src/mock_crm.py): a full load on
31 Dec 2016, then 12 incremental runs, so the warehouse holds the pipeline as it was at each month end.
"""
import hashlib
import re
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path

import duckdb

import mock_crm
from extract import Client, sync

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
SEEDS = ROOT / "seeds"
DB = ROOT / "data" / "warehouse" / "crm.duckdb"
MARTS = ROOT / "data" / "marts"
SQL = ROOT / "sql"
# Maven Analytics "CRM Sales Opportunities" (public domain). The URL is not versioned, so the hash pins it.
SOURCE = "https://maven-datasets.s3.amazonaws.com/CRM+Sales+Opportunities/CRM+Sales+Opportunities.zip"
SHA256 = "f7d1a04f2651dd8c0a2d01d2e9c161438983dde01db47c3d8ba6e774012d01a1"

EXPORTS = ["dim_date", "dim_account", "dim_product", "dim_agent", "dim_stage", "dim_cycle_band", "dim_age_band",
           "fact_opportunity", "fact_pipeline_snapshot", "fact_funnel", "fact_stage_transition", "sync_runs", "manager_access"]


def month_ends():
    d = date(2016, 12, 31)
    while d <= mock_crm.EXPORT_DATE:
        yield d
        first = d + timedelta(days=1)
        d = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def download():
    RAW.mkdir(parents=True, exist_ok=True)
    if (RAW / "sales_pipeline.csv").exists():
        return
    z = RAW / "crm.zip"
    print("  ↓ CRM Sales Opportunities.zip")
    subprocess.run(["curl", "-fsSL", "-o", str(z), SOURCE], check=True)
    if (h := hashlib.sha256(z.read_bytes()).hexdigest()) != SHA256:
        sys.exit(f"source changed upstream (sha256 {h}) — check it before trusting the numbers")
    zipfile.ZipFile(z).extractall(RAW)


def clock(port, d):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/_mock/clock", data=f'{{"date": "{d}"}}'.encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req).read()


def extract(con):
    server = mock_crm.serve(port=0)
    port = server.server_port
    client = Client(f"http://127.0.0.1:{port}", mock_crm.TOKEN)
    try:
        for d in month_ends():
            clock(port, d)
            mode, seen, added, s = sync(con, client, d)
            print(f"  ✓ {d}  {mode:<11} {seen:>5} deals seen  {added:>5} new versions  "
                  f"{s['requests']:>3} requests  {s['rate_limited']} × 429  {s['server_errors']} × 5xx")
    finally:
        server.shutdown()


def run_sql(con, name):
    t = time.perf_counter()
    con.execute((SQL / name).read_text().replace("{seeds}", SEEDS.as_posix()))
    print(f"  ✓ {name:<20} {time.perf_counter() - t:5.1f}s")


def run_tests(con):
    tests = re.findall(r"-- name: ([^\n]+)\n(.+?;)", (SQL / "90_tests.sql").read_text(), re.S)
    failed = 0
    for name, query in tests:
        bad = con.execute(query).fetchone()[0]
        failed += bad > 0
        print(f"  {'✓' if bad == 0 else '✗'} {name}{'' if bad == 0 else f'  ({bad} rows)'}")
    return failed


def main():
    print("Downloading Maven CRM dataset")
    download()
    DB.parent.mkdir(parents=True, exist_ok=True)
    MARTS.mkdir(parents=True, exist_ok=True)
    DB.unlink(missing_ok=True)  # every build replays the syncs from scratch
    con = duckdb.connect(str(DB))
    con.execute("set enable_progress_bar = false")

    print("Extracting from the CRM API (month-end syncs)")
    extract(con)

    print("Building warehouse")
    for name in ["10_staging.sql", "20_marts.sql"]:
        run_sql(con, name)

    print("Data-quality tests")
    if failed := run_tests(con):
        sys.exit(f"{failed} test(s) failed — marts not exported")

    print("Exporting marts for Power BI")
    for table in EXPORTS:
        out = MARTS / f"{table}.parquet"
        con.execute(f"copy mart.{table} to '{out.as_posix()}' (format parquet, compression zstd)")
        n = con.execute(f"select count(*) from mart.{table}").fetchone()[0]
        print(f"  → {table:<24} {n:>8,} rows")


if __name__ == "__main__":
    main()
