"""End-to-end build: retailer CSV exports -> DuckDB staging -> star schema + restock engine -> tests -> Parquet marts.

    uv run python src/pipeline.py

Source: a 79-store wine & spirits chain's 2016 records (stock counts on 1 Jan and 31 Dec, every
purchase-order line received in 2016, vendor invoices, daily sales). Everything is computed as of
31 Dec 2016, the date of the last physical stock count.
"""
import hashlib
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
SEEDS = ROOT / "seeds"
DB = ROOT / "data" / "warehouse" / "inventory.duckdb"
MARTS = ROOT / "data" / "marts"
SQL = ROOT / "sql"
# Kaggle mirror of the "Inventory Analysis Case Study" (Apache 2.0). Not versioned, so the hash pins it.
SOURCE = "https://www.kaggle.com/api/v1/datasets/download/bhanupratapbiswas/inventory-analysis-case-study"
SHA256 = "05330aaad2d5e6ff39d6434df35b4a112c368ff2605bbebccaa81b7aa33ff578"
FILES = {"begin_inventory": "BegInvFINAL12312016.csv", "end_inventory": "EndInvFINAL12312016.csv",
         "purchases": "PurchasesFINAL12312016.csv", "invoices": "InvoicePurchases12312016.csv",
         "sales": "SalesFINAL12312016.csv", "price_list": "2017PurchasePricesDec.csv"}

EXPORTS = ["dim_date", "dim_store", "dim_vendor", "dim_item", "dim_status", "dim_cover_band", "dim_abc",
           "fact_stock", "fact_sales_daily", "fact_receipts", "fact_po", "forecast_daily", "forecast_scores",
           "weekday_profile", "store_access", "item_supplier", "category_target",
           "fact_shipment"]


def download():
    RAW.mkdir(parents=True, exist_ok=True)
    if all((RAW / f).exists() for f in FILES.values()):
        return
    z = RAW / "inventory.zip"
    print("  ↓ inventory-analysis-case-study.zip (62 MB)")
    subprocess.run(["curl", "-fsSL", "-o", str(z), SOURCE], check=True)
    if (h := hashlib.sha256(z.read_bytes()).hexdigest()) != SHA256:
        sys.exit(f"source changed upstream (sha256 {h}) — check it before trusting the numbers")
    zipfile.ZipFile(z).extractall(RAW)


def load_raw(con):
    con.execute("create schema if not exists raw")
    for table, f in FILES.items():
        t = time.perf_counter()
        con.execute(f"create or replace table raw.{table} as select * from read_csv('{(RAW / f).as_posix()}')")
        n = con.execute(f"select count(*) from raw.{table}").fetchone()[0]
        print(f"  ✓ raw.{table:<16} {n:>10,} rows  {time.perf_counter() - t:5.1f}s")


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
    print("Downloading source")
    download()
    DB.parent.mkdir(parents=True, exist_ok=True)
    MARTS.mkdir(parents=True, exist_ok=True)
    DB.unlink(missing_ok=True)
    con = duckdb.connect(str(DB))
    con.execute("set enable_progress_bar = false")

    print("Loading raw exports")
    load_raw(con)

    print("Building warehouse")
    for name in ["10_staging.sql", "20_marts.sql", "30_power_query.sql"]:
        run_sql(con, name)
    # sum() of bigint is HUGEINT in DuckDB; Power BI has no 128-bit integer.
    for t, c in con.execute("select table_name, column_name from information_schema.columns "
                            "where table_schema = 'mart' and data_type = 'HUGEINT'").fetchall():
        con.execute(f"alter table mart.{t} alter {c} type bigint")

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
