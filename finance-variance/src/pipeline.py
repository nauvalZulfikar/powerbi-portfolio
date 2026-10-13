"""End-to-end build: AdventureWorksDW finance extract -> DuckDB staging -> star schema -> tests -> Parquet marts.

    uv run python src/pipeline.py
"""
import re
import subprocess
import sys
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
SEEDS = ROOT / "seeds"
DB = ROOT / "data" / "warehouse" / "finance.duckdb"
MARTS = ROOT / "data" / "marts"
SQL = ROOT / "sql"
# Pinned commit of microsoft/sql-server-samples (MIT) so the numbers never drift.
SOURCE = ("https://raw.githubusercontent.com/microsoft/sql-server-samples/beaab06ef72831089ca80e5355d65e661fd19b26/"
          "samples/databases/adventure-works/data-warehouse-install-script/{}.csv")
FILES = ["DimAccount", "DimCurrency", "DimDepartmentGroup", "DimOrganization", "DimScenario",
         "FactCurrencyRate", "FactFinance"]

EXPORTS = [
    "dim_date", "dim_entity", "dim_department", "dim_account", "dim_pnl_line", "bridge_line_account",
    "fact_gl", "fact_headcount", "fx_rates", "budget_drivers", "entity_access", "dim_variance_step", "forecast",
    "gl_journal", "headcount_plan",
]


def download():
    RAW.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        if not (RAW / f"{name}.csv").exists():
            print(f"  ↓ {name}.csv")
            subprocess.run(["curl", "-fsSL", "-o", str(RAW / f"{name}.csv"), SOURCE.format(name)], check=True)


def run_sql(con, name):
    t = time.perf_counter()
    con.execute((SQL / name).read_text().replace("{raw}", RAW.as_posix()).replace("{seeds}", SEEDS.as_posix()))
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
    print("Downloading AdventureWorksDW finance extract")
    download()
    DB.parent.mkdir(parents=True, exist_ok=True)
    MARTS.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB))
    con.execute("set enable_progress_bar = false")

    print("Building warehouse")
    for name in ["10_staging.sql", "20_marts.sql", "30_analytics.sql"]:
        run_sql(con, name)

    print("Data-quality tests")
    if failed := run_tests(con):
        sys.exit(f"{failed} test(s) failed — marts not exported")

    print("Exporting marts for Power BI")
    for table in EXPORTS:
        out = MARTS / f"{table}.parquet"
        con.execute(f"copy mart.{table} to '{out.as_posix()}' (format parquet, compression zstd)")
        n = con.execute(f"select count(*) from mart.{table}").fetchone()[0]
        print(f"  → {table:<20} {n:>10,} rows")


if __name__ == "__main__":
    main()
