"""The as-of rules on a handful of hand-written Jira issues: no network, runs in CI.

    uv run python -m unittest discover tests -v
"""
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pipeline import build  # noqa: E402

REP, DEV, LEAD = ({"key": k, "name": k, "display": k.title()} for k in ("rep", "dev", "lead"))


def change(at, by, field, frm, to, frm_id=None, to_id=None):
    return {"at": at, "by": by, "items": [{"field": field, "from": frm_id, "fromString": frm, "to": to_id, "toString": to}]}


def issue(key, created, history, *, type_="Bug", priority="Major", status="Open", resolution=None, assignee=None):
    return {"key": key, "summary": f"{key} summary", "type": type_, "priority": priority, "status": status,
            "resolution": resolution, "created": created, "resolved": None, "assignee": assignee, "reporter": REP,
            "components": [], "labels": [], "history": history}


ISSUES = {
    "HIVE": [
        # Worked and fixed inside the window: picked up by dev on day 1, resolved on day 4.
        issue("HIVE-1", "2025-01-01T00:00:00", [
            change("2025-01-02T00:00:00", DEV, "assignee", None, "Dev", None, "dev"),
            change("2025-01-02T00:00:00", DEV, "status", "Open", "In Progress"),
            change("2025-01-05T00:00:00", DEV, "status", "In Progress", "Resolved"),
            change("2025-01-05T00:00:00", DEV, "resolution", None, "Fixed"),
        ], status="Resolved", resolution="Fixed", assignee=DEV),
        # Resolved on 3 Oct 2026, after the as-of date: still open on 30 Sep, in the backlog, no response yet.
        issue("HIVE-2", "2026-09-01T00:00:00", [
            change("2026-10-03T00:00:00", LEAD, "status", "Open", "Resolved"),
            change("2026-10-03T00:00:00", LEAD, "resolution", None, "Fixed"),
        ], status="Resolved", resolution="Fixed"),
        # Epics are containers, not work.
        issue("HIVE-3", "2025-02-01T00:00:00", [], type_="Epic"),
    ],
    "CASSANDRA": [
        # Raised to Blocker after the as-of date: counts as P3 on 30 Sep. Closed as a duplicate, then reopened.
        issue("CASSANDRA-1", "2025-03-01T00:00:00", [
            change("2025-03-02T00:00:00", REP, "status", "Triage Needed", "Open"),
            change("2025-03-10T00:00:00", LEAD, "status", "Open", "Resolved"),
            change("2025-03-10T00:00:00", LEAD, "resolution", None, "Duplicate"),
            change("2025-04-01T00:00:00", LEAD, "status", "Resolved", "Open"),
            change("2025-04-01T00:00:00", LEAD, "resolution", "Duplicate", None),
            change("2026-10-02T00:00:00", LEAD, "priority", "Major", "Blocker"),
        ], priority="Blocker"),
    ],
}


class AsOf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        raw = Path(cls.tmp.name) / "raw"
        raw.mkdir()
        for team, issues in ISSUES.items():
            with gzip.open(raw / f"{team}.jsonl.gz", "wt") as f:
                f.write("\n".join(json.dumps(i) for i in issues) + "\n")
        cls.con, cls.results = build(raw, Path(cls.tmp.name) / "t.duckdb")

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.tmp.cleanup()

    def ticket(self, key):
        cur = self.con.execute("select * from marts.fact_ticket where ticket_key = ?", [key])
        row = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], row)) if row else None

    def test_data_tests_pass(self):
        self.assertEqual([t for t, ok, d in self.results if not ok], [])

    def test_resolved_ticket_times(self):
        t = self.ticket("HIVE-1")
        self.assertEqual((t["outcome"], t["stage"]), ("Delivered", "Done"))
        self.assertEqual((t["lead_days"], t["cycle_days"], t["response_hours"]), (4, 3, 24))
        self.assertEqual(t["resolution_sla"], "Met")  # P3: 90 days

    def test_later_resolution_does_not_leak(self):
        t = self.ticket("HIVE-2")
        self.assertIsNone(t["resolved_at"])
        self.assertEqual((t["outcome"], t["stage"], t["status"]), ("Open", "Backlog", "Open"))
        self.assertIsNone(t["first_response_at"])  # the only action came after the as-of date

    def test_epic_excluded(self):
        self.assertIsNone(self.ticket("HIVE-3"))

    def test_priority_and_reopen_as_of(self):
        t = self.ticket("CASSANDRA-1")
        self.assertEqual(self.con.execute("select priority from marts.dim_priority where priority_key = ?",
                                          [t["priority_key"]]).fetchone()[0], "P3 Normal")
        self.assertEqual((t["reopened"], t["outcome"], t["stage"]), (1, "Open", "Backlog"))
        # the reporter's own triage change is not a response; the lead's resolution 9 days in is
        self.assertEqual(t["response_hours"], 9 * 24)

    def test_backlog_counts_open_days(self):
        n = lambda d: self.con.execute("select coalesce(sum(open_tickets), 0) from marts.fact_backlog_daily "
                                       "where date = ?", [d]).fetchone()[0]
        self.assertEqual(n("2025-01-03"), 1)  # HIVE-1 in progress
        self.assertEqual(n("2025-03-15"), 0)  # CASSANDRA-1 closed as duplicate
        self.assertEqual(n("2025-04-02"), 1)  # ... and reopened
        self.assertEqual(n("2026-09-30"), 2)  # CASSANDRA-1 + HIVE-2

    def test_stage_transitions(self):
        moves = lambda k: self.con.execute(
            "select a.stage, b.stage from marts.fact_stage_transition t join marts.dim_stage a on a.stage_key = t.from_stage_key "
            "join marts.dim_stage b on b.stage_key = t.to_stage_key where ticket_key = ? order by transition_date", [k]).fetchall()
        self.assertEqual(moves("HIVE-1"), [("Backlog", "In Progress"), ("In Progress", "Done")])
        self.assertEqual(moves("CASSANDRA-1"), [("Backlog", "Done"), ("Done", "Backlog")])  # closed, then reopened

    def test_people_pseudonymised(self):
        names = [r[0] for r in self.con.execute("select person from marts.dim_person").fetchall()]
        self.assertEqual(sorted(names), ["Engineer 001", "Unassigned"])


if __name__ == "__main__":
    unittest.main()
