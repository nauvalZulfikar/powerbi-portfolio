"""End-to-end build: Jira issues + change history -> DuckDB staging -> ticket facts, flow and SLA -> tests -> Parquet.

    uv run python src/extract.py     # once: pulls data/raw/*.jsonl.gz from the ASF Jira REST API
    uv run python src/pipeline.py

Everything is computed as of 30 Sep 2026 23:59 UTC by replaying each issue's change history, so
an issue that was closed later still counts as open on that date and the numbers don't drift.
"""
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
SEEDS = ROOT / "seeds"
DB = ROOT / "data" / "warehouse" / "team.duckdb"
MARTS = ROOT / "data" / "marts"
SQL = ROOT / "sql"

EXPORTS = ["dim_date", "dim_team", "dim_person", "dim_priority", "dim_type", "dim_stage", "fact_ticket",
           "fact_stage_time", "fact_backlog_daily", "status_map", "data_checks", "team_access",
           "dim_component", "bridge_ticket_component", "fact_stage_transition"]


def build(raw=RAW, db=DB):
    """Staging + marts in a fresh DuckDB file; returns the connection and the data-test results."""
    db.parent.mkdir(parents=True, exist_ok=True)
    db.unlink(missing_ok=True)
    con = duckdb.connect(str(db))
    for name in ("10_staging.sql", "20_marts.sql"):
        t = time.perf_counter()
        con.execute((SQL / name).read_text().replace("{raw}", raw.as_posix()).replace("{seeds}", SEEDS.as_posix()))
        print(f"  ✓ {name:<16} {time.perf_counter() - t:5.1f}s")
    return con, con.execute((SQL / "90_tests.sql").read_text()).fetchall()


def main():
    con, results = build()
    for test, ok, detail in results:
        print(f"  {'✓' if ok else '✗'} {test}{'' if ok else f'  ({detail})'}")
    if not all(ok for _, ok, _ in results):
        raise SystemExit("data tests failed: nothing exported")
    MARTS.mkdir(parents=True, exist_ok=True)
    for t in EXPORTS:
        con.execute(f"copy marts.{t} to '{(MARTS / t).as_posix()}.parquet' (format parquet)")
        print(f"  → {t:<20} {con.execute(f'select count(*) from marts.{t}').fetchone()[0]:>9,} rows")


if __name__ == "__main__":
    main()
