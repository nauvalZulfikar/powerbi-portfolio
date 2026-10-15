"""Build the F1 warehouse: Kaggle -> DuckDB staging -> star schema -> model -> tests -> Parquet marts.

    uv run python src/pipeline.py            # uses data/raw if present
    uv run python src/pipeline.py --refresh  # re-download the latest Kaggle version
"""
import re
import shutil
import sys
import time
from pathlib import Path

import duckdb

import driver_skill

ROOT = Path(__file__).resolve().parents[1]
RAW, DB, MARTS, SQL = ROOT / "data/raw", ROOT / "data/warehouse/f1.duckdb", ROOT / "data/marts", ROOT / "sql"
DATASET = "rohanrao/formula-1-world-championship-1950-2020"

EXPORTS = [
    "dim_season", "dim_race", "dim_driver", "dim_constructor", "dim_circuit",
    "fact_results", "fact_qualifying", "fact_pit_stops", "fact_lap_times", "fact_standings",
    "grid_conversion", "team_season", "teammate_h2h", "driver_skill",
]


def ingest(refresh):
    if refresh or not any(RAW.glob("*.csv")):
        import kagglehub  # public dataset: no API key needed
        src = Path(kagglehub.dataset_download(DATASET, force_download=refresh))
        RAW.mkdir(parents=True, exist_ok=True)
        for f in src.glob("*.csv"):
            shutil.copy(f, RAW / f.name)
    print(f"  ✓ raw                  {len(list(RAW.glob('*.csv')))} CSVs from kaggle.com/datasets/{DATASET}")


def run_sql(con, name):
    t = time.perf_counter()
    con.execute((SQL / name).read_text().replace("{raw}", RAW.as_posix()))
    print(f"  ✓ {name:<20} {time.perf_counter() - t:5.1f}s")


def run_tests(con):
    failed = 0
    for name, query in re.findall(r"-- name: (.+?)\n(.+?;)\s*(?=--|$)", (SQL / "90_tests.sql").read_text(), re.S):
        bad = con.execute(query).fetchone()[0]
        failed += bad > 0
        print(f"  {'✓' if bad == 0 else '✗'} {name}{'' if bad == 0 else f'  ({bad})'}")
    return failed


def main():
    DB.parent.mkdir(parents=True, exist_ok=True)
    MARTS.mkdir(parents=True, exist_ok=True)
    print("Ingest")
    ingest("--refresh" in sys.argv)

    con = duckdb.connect(str(DB))
    print("Build warehouse")
    for f in ["10_staging.sql", "20_marts.sql", "30_analytics.sql"]:
        run_sql(con, f)
    driver_skill.fit(con)

    print("Data-quality tests")
    if failed := run_tests(con):
        sys.exit(f"{failed} test(s) failed — marts not exported")

    print("Export marts")
    for t in EXPORTS:
        con.execute(f"copy mart.{t} to '{(MARTS / t).as_posix()}.parquet' (format parquet)")
        print(f"  → {t:<18} {con.execute(f'select count(*) from mart.{t}').fetchone()[0]:>9,} rows")


if __name__ == "__main__":
    main()
