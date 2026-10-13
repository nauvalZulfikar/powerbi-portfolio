"""Write the SYNTHETIC FY2013 reforecast submissions in seeds/forecast/ (run once; the CSVs are committed).

    uv run python src/make_forecast_seed.py      # needs a built data/warehouse/finance.duckdb

AdventureWorks has no forecast, so each entity's "submission" is its FY2012 actual (USD) per P&L
detail line and month x (1 + a seeded adjustment). Australia has no FY2012 actual (consolidated
from 2013), so its file starts from FY2013 actual. The files imitate what entities really send:
two title rows, months as columns, a Total column, "n/a"/"-" for empty cells, the entity name only
on the first row of each block (US file holds all five divisions), and Germany in de-DE format
(semicolon delimiter, decimal comma, dd.mm.yyyy dates).
"""
import csv
import random
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "seeds" / "forecast"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
# file -> (culture, submitted, [(entity code, entity key)], base year)
FILES = {
    "forecast_FY2013_US_en-US.csv": ("en-US", "2013-01-09", [("US01", 3), ("US02", 4), ("US03", 5), ("US04", 6), ("US05", 7)], 2012),
    "forecast_FY2013_CA01_en-US.csv": ("en-US", "2013-01-08", [("CA01", 8)], 2012),
    "forecast_FY2013_FR01_en-US.csv": ("en-US", "2013-01-14", [("FR01", 11)], 2012),
    "forecast_FY2013_DE01_de-DE.csv": ("de-DE", "2013-01-10", [("DE01", 12)], 2012),
    "forecast_FY2013_AU01_en-US.csv": ("en-US", "2013-01-17", [("AU01", 13)], 2013),
}


def fmt(v, culture):
    s = f"{v:.2f}"
    return s.replace(".", ",") if culture == "de-DE" else s


def main():
    rng = random.Random(2013)
    con = duckdb.connect(str(ROOT / "data" / "warehouse" / "finance.duckdb"), read_only=True)
    lines = con.execute("select line_id, line from mart.dim_pnl_line where line_kind = 'detail' order by line_sort").fetchall()
    names = dict(con.execute("select entity_key, entity from mart.dim_entity").fetchall())
    OUT.mkdir(parents=True, exist_ok=True)
    for file, (culture, submitted, entities, base) in FILES.items():
        delim = ";" if culture == "de-DE" else ","
        date = ".".join(reversed(submitted.split("-"))) if culture == "de-DE" else submitted
        rows = [[f"AdventureWorks Group - FY2013 reforecast (USD) - SYNTHETIC"], [f"Prepared {date}"],
                ["Entity", "Line", *MONTHS, "Total", "Submitted"]]
        for code, key in entities:
            first = True
            for line_id, line in lines:
                actual = dict(con.execute(
                    "select month(period), actual from mart.variance_cube "
                    "where entity_key = ? and line_id = ? and year(period) = ?", [key, line_id, base]).fetchall())
                if not actual:
                    continue
                adj = 1 + rng.uniform(-0.06, 0.08)
                vals = [round(actual[m] * adj, 2) if actual.get(m) is not None else None for m in range(1, 13)]
                cells = ["n/a" if v is None else "-" if v == 0 else fmt(v, culture) for v in vals]
                total = round(sum(v or 0 for v in vals), 2)
                rows.append([f"{code} - {names[key]}" if first else "", f" {line} " if rng.random() < 0.1 else line,
                             *cells, fmt(total, culture), date])
                first = False
        with open(OUT / file, "w", newline="", encoding="utf-8") as f:
            csv.writer(f, delimiter=delim, lineterminator="\n").writerows(rows)
        print(f"  ✓ {file}: {len(rows) - 3} rows")


if __name__ == "__main__":
    main()
