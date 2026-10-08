from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import schedule  # noqa: E402


class BiweeklyScheduleTests(unittest.TestCase):
    anchor = dt.date(2026, 10, 12)

    def test_alternate_mondays_across_month_and_year_boundaries(self) -> None:
        for week in range(20):
            with self.subTest(week=week):
                day = self.anchor + dt.timedelta(weeks=week)
                self.assertEqual(schedule.scan_due("schedule", day, self.anchor),
                                 week % 2 == 0)

    def test_delayed_run_stays_in_its_scheduled_week(self) -> None:
        for offset in range(14):
            with self.subTest(offset=offset):
                day = self.anchor + dt.timedelta(days=offset)
                self.assertEqual(schedule.scan_due("schedule", day, self.anchor),
                                 offset < 7)

    def test_scheduled_runs_before_anchor_are_skipped(self) -> None:
        self.assertFalse(schedule.scan_due("schedule", dt.date(2026, 10, 5), self.anchor))

    def test_manual_runs_ignore_cadence(self) -> None:
        for day in (dt.date(2026, 10, 8), dt.date(2026, 10, 19)):
            self.assertTrue(schedule.scan_due("workflow_dispatch", day, self.anchor))

    def test_unexpected_events_do_not_scan(self) -> None:
        self.assertFalse(schedule.scan_due("push", self.anchor, self.anchor))

    def test_anchor_must_be_monday(self) -> None:
        with self.assertRaises(ValueError):
            schedule.scan_due("schedule", self.anchor, dt.date(2026, 10, 13))

    def test_cli_outputs_for_github_job_gate(self) -> None:
        for day, expected in (("2026-10-12", "true"), ("2026-10-19", "false")):
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "schedule.py"), "--event", "schedule",
                 "--anchor", self.anchor.isoformat(), "--date", day],
                check=True, capture_output=True, text=True,
            )
            self.assertEqual(result.stdout.splitlines()[0], f"run={expected}")
            self.assertTrue(result.stdout.splitlines()[1].startswith("reason="))


if __name__ == "__main__":
    unittest.main()
