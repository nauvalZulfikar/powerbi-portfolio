"""Write seeds/quarter_goals.xlsx: a SYNTHETIC delivery-goal sheet laid out the way an ops team keeps one.

    uv run python src/seed_goals.py

The goals are made up (the real quarterly delivered count per team and work type, scaled by a fixed
random factor and rounded to 5). The layout is deliberately messy so the Power Query steps have work to do:
a title row and a blank row above the header, the team written only on the first row of its block,
a blank row between blocks, and one column per quarter.
"""
import random
from pathlib import Path

import duckdb
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
MARTS = ROOT / "data" / "marts"
QUARTERS = ["Q4 2024", "Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025", "Q1 2026", "Q2 2026", "Q3 2026"]


def main():
    rows = duckdb.sql(f"""
        select f.team_key, case when ty.type = 'Bug' then 'Bug' else 'Other' end as work_type, d.quarter, count(*) n
        from '{MARTS / 'fact_ticket.parquet'}' f join '{MARTS / 'dim_type.parquet'}' ty using (type_key)
        join '{MARTS / 'dim_date.parquet'}' d on d.date = f.resolved_date
        where f.outcome = 'Delivered' group by all""").fetchall()
    actual = {(t, w, q): n for t, w, q, n in rows}
    rnd = random.Random(20260930)
    wb = Workbook()
    ws = wb.active
    ws.title = "Goals"
    ws.append(["Quarterly delivery goals by team (SYNTHETIC: made up for this portfolio project)"])
    ws.append([])
    ws.append(["Team", "Work Type", *QUARTERS])
    for team in ("HIVE", "CASSANDRA", "HBASE", "ARTEMIS"):
        for i, work in enumerate(("Bug", "Other")):
            goals = [max(5, round(actual.get((team, work, q), 0) * rnd.uniform(0.9, 1.3) / 5) * 5) for q in QUARTERS]
            ws.append([team if i == 0 else None, work, *goals])
        ws.append([])
    out = ROOT / "seeds" / "quarter_goals.xlsx"
    from datetime import datetime
    wb.properties.created = wb.properties.modified = datetime(2026, 9, 30)
    wb.save(out)
    print(f"  ✓ wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
