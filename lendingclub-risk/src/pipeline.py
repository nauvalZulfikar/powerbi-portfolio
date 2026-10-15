"""End-to-end build: Kaggle download -> DuckDB staging -> PD model -> star schema -> tests -> Parquet marts.

    uv run python src/pipeline.py
"""
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import duckdb

import glossary
import pd_model

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
DB = ROOT / "data" / "warehouse" / "lendingclub.duckdb"
MARTS = ROOT / "data" / "marts"
SQL = ROOT / "sql"
KAGGLE = "https://www.kaggle.com/api/v1/datasets/download/adarshsng/lending-club-loan-data-csv"

EXPORTS = [
    "dim_date", "dim_grade", "dim_state", "dim_purpose", "dim_cash_bridge", "fact_loans",
    "vintage_curve", "grade_lgd", "book_balance",
    "model_calibration", "model_roc", "model_importance", "model_metrics",
]


def download():
    if (RAW / "loan.csv").exists():
        return
    RAW.mkdir(parents=True, exist_ok=True)
    zipped = RAW / "lending-club.zip"
    print("Downloading LendingClub loan tape from Kaggle (~355 MB)")
    subprocess.run(["curl", "-fL", "-o", str(zipped), KAGGLE], check=True)
    zipfile.ZipFile(zipped).extractall(RAW)
    zipped.unlink()


def run_sql(con, name):
    t = time.perf_counter()
    con.execute((SQL / name).read_text().replace("{raw}", RAW.as_posix()))
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
    download()
    DB.parent.mkdir(parents=True, exist_ok=True)
    MARTS.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB))
    con.execute("set enable_progress_bar = false")

    print("Building warehouse")
    run_sql(con, "10_staging.sql")
    pd_model.run(con)
    run_sql(con, "20_marts.sql")
    run_sql(con, "30_analytics.sql")

    print("Data-quality tests")
    if failed := run_tests(con) + glossary_tests(con):
        sys.exit(f"{failed} test(s) failed — marts not exported")

    print("Exporting marts for Power BI")
    for table in EXPORTS:
        out = MARTS / f"{table}.parquet"
        con.execute(f"copy mart.{table} to '{out.as_posix()}' (format parquet, compression zstd)")
        n = con.execute(f"select count(*) from mart.{table}").fetchone()[0]
        print(f"  → {table:<20} {n:>10,} rows")
    # Power Query reads the official LendingClub data dictionary next to the marts (Field Glossary query).
    shutil.copy(RAW / "LCDataDictionary.xlsx", MARTS / "LCDataDictionary.xlsx")
    print("  → LCDataDictionary.xlsx (Power Query: Dictionary Raw → Field Glossary)")


def glossary_tests(con):
    """Python mirror of the Power Query glossary: the merge must give every PD feature an official description."""
    rows = glossary.field_glossary(RAW / "LCDataDictionary.xlsx")
    codes = [r["Field Code"] for r in rows]
    features = [f for (f,) in con.execute("select feature from mart.model_importance").fetchall()]
    missing = [f for f, d in glossary.feature_descriptions(RAW / "LCDataDictionary.xlsx", features).items() if not d]
    checks = {"glossary has rows from both sheets": {r["Sheet"] for r in rows} != set(glossary.SHEETS),
              "glossary field codes unique": len(codes) != len(set(codes)),
              "glossary has no footnote / header rows": any(c.startswith("*") or r["Description"] == "Description"
                                                             for c, r in zip(codes, rows)),
              f"every PD feature has an LC description ({len(features)})": bool(missing)}
    for name, bad in checks.items():
        print(f"  {'✗' if bad else '✓'} {name}" + (f"  ({missing})" if bad and missing else ""))
    return sum(checks.values())


if __name__ == "__main__":
    main()
