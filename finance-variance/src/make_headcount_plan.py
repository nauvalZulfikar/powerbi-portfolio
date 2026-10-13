"""Write the SYNTHETIC headcount plan to seeds/headcount_plan.csv (run once; the CSV is committed).

    uv run python src/make_headcount_plan.py      # needs a built data/warehouse/finance.duckdb

AdventureWorks has no headcount plan. Each region x department gets a year-end target = the prior
December's actual heads x (1 + a seeded growth), rounded. "Asia Operations" is a planned region with
no entity yet (it shows how a many-to-many relationship treats a value the other side doesn't have).
"""
import csv
import random
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]


def main():
    rng = random.Random(2012)
    con = duckdb.connect(str(ROOT / "data" / "warehouse" / "finance.duckdb"), read_only=True)
    dec = con.execute("""
        select e.region, h.department_key, year(h.period) + 1 as fiscal_year, sum(h.headcount) heads
        from mart.fact_headcount h join mart.dim_entity e using (entity_key)
        where month(h.period) = 12 and year(h.period) in (2011, 2012)
        group by all order by all""").fetchall()
    rows = [(r, d, y, round(h * (1 + rng.uniform(-0.02, 0.10)))) for r, d, y, h in dec]
    rows += [("Asia Operations", d, 2013, n) for d, n in ((7, 4), (2, 1))]
    with open(ROOT / "seeds" / "headcount_plan.csv", "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["region", "department_key", "fiscal_year", "planned_heads"])
        w.writerows(rows)
    print(f"  ✓ seeds/headcount_plan.csv: {len(rows)} rows")


if __name__ == "__main__":
    main()
