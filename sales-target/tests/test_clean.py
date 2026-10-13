"""Data tests: the Python mirror of the Power Query cleaning must reproduce the source exactly.

    uv run python -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import clean  # noqa: E402

SRC = ROOT / "data" / "source"
BRANCH = "case when terr between 1 and 6 then 'North America' when terr = 9 then 'Pacific' else 'Europe' end"


class CleanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clean.main()
        cls.c = c = duckdb.connect()
        c.execute(f"""create view truth as select column05::int rep, column08::int terr, column09 so, column10::int ln,
                      column12::int qty, column19::double amt, column24::timestamp::date d, column26::timestamp::date ship, {BRANCH.replace('terr', 'column08::int')} branch
                      from read_csv('{SRC}/FactResellerSales.csv', delim='|', header=false, quote='', all_varchar=true)""")
        for t in ("sales", "staging", "cleaning_log", "targets", "reps", "category_plan"):
            c.execute(f"create view {t} as select * from '{clean.MARTS}/{t}.parquet'")

    def q(self, sql):
        return self.c.execute(sql).fetchall()

    def test_every_source_line_loaded_once(self):
        self.assertEqual(self.q("select count(*) from sales"), self.q("select count(*) from truth"))
        self.assertEqual(self.q("select count(*) from (select order_number, order_line from sales group by all having count(*) > 1)"), [(0,)])
        self.assertEqual(self.q("""select count(*) from truth t anti join sales s
                                   on s.order_number = t.so and s.order_line = t.ln"""), [(0,)])

    def test_line_values_match_source(self):
        bad = self.q("""select count(*) from truth t join sales s on s.order_number = t.so and s.order_line = t.ln
                        where s.rep_key <> t.rep or s.territory_key <> t.terr or s.order_date <> t.d
                           or s.ship_date is distinct from t.ship
                           or s.quantity <> t.qty or abs(s.amount - t.amt) > 0.00501""")
        self.assertEqual(bad, [(0,)])

    def test_branch_totals(self):
        got = dict(self.q("select branch, sum(amount) from sales group by all"))
        want = dict(self.q("select branch, sum(amt) from truth group by all"))
        rows = dict(self.q("select branch, count(*) from sales group by all"))
        for b in ("North America", "Pacific"):
            self.assertAlmostEqual(got[b], want[b], delta=0.01, msg=b)
        # Europe sends amounts rounded to the cent, so the total may drift by half a cent per line at most.
        self.assertLess(abs(got["Europe"] - want["Europe"]), rows["Europe"] * 0.005)

    def test_cleaning_log_accounts_for_every_row(self):
        (staged,), = self.q("select count(*) from staging")
        (logged,), = self.q("select sum(rows) from cleaning_log")
        self.assertEqual(staged, logged)
        out = dict(self.q("select outcome, sum(rows) from cleaning_log group by all"))
        self.assertEqual(out["Loaded"], self.q("select count(*) from truth")[0][0])
        for o in ("Unknown rep", "Unknown product", "Bad value"):
            self.assertNotIn(o, out, o)
        (mar,), = self.q("select count(*) from truth where d between '2012-03-01' and '2012-03-31' and branch = 'North America'")
        self.assertEqual(out["Duplicate sheet"], mar + 1)  # the copy includes its Month total row
        (overlap,), = self.q("select count(*) from truth where branch = 'Pacific' and d between '2013-07-01' and '2013-08-31'")
        self.assertEqual(out["Duplicate line"], overlap)

    def test_targets(self):
        (quota,), = self.q(f"select sum(column5::double) from read_csv('{SRC}/FactSalesQuota.csv', delim='|', header=false, quote='', all_varchar=true)")
        (total,), = self.q("select sum(daily_quota) from targets")
        self.assertAlmostEqual(total, quota, delta=0.01)
        self.assertEqual(self.q("select count(*) from targets where rep_key is null or rep_key not in (select rep_key::int from reps)"), [(0,)])
        self.assertEqual(self.q("select min(date)::varchar, max(date)::varchar from targets"), [("2011-01-01", "2013-12-31")])

    def test_category_plan_adds_up_to_quota(self):
        """The synthetic category plan only re-splits quota: per fiscal quarter it sums to the quota up to the last sale."""
        (plan,), = self.q("select sum(plan_amount) from category_plan")
        (quota,), = self.q("select sum(daily_quota) from targets where date <= (select max(order_date) from sales)")
        self.assertAlmostEqual(plan, quota, delta=0.01)
        # Each quarter is split, not resized: one row per category, shares summing to that quarter's quota.
        (quarters,), = self.q("select count(distinct quarter) from targets where date <= (select max(order_date) from sales)")
        self.assertEqual(self.q("select count(distinct fiscal_quarter) from category_plan"), [(quarters,)])


if __name__ == "__main__":
    unittest.main()
