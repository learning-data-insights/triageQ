#!/usr/bin/env python3
"""Self-test for usage_report.py. Run from the repo root:
python3 scripts/test_usage_report.py
"""
import contextlib
import csv
import gzip
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usage_report  # noqa: E402

DAY = "2026-10-23"
BASE = 1792713600.0  # 2026-10-23T00:00:00Z
IPS = ["203.0.113.5", "198.51.100.7", "192.0.2.99"]
UA = "Mozilla/5.0 SecretAgent"


def entry(ts, ip, method, uri, duration=0.0, status=200):
    return json.dumps({
        "level": "info", "ts": ts, "msg": "handled request",
        "request": {"remote_ip": ip, "client_ip": ip, "method": method, "host": "x",
                    "uri": uri, "headers": {"User-Agent": [UA]}},
        "user_agent": UA, "duration": duration, "status": status})


class UsageReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = os.path.join(self.tmp.name, "logs")
        self.usage = os.path.join(self.tmp.name, "usage")
        os.makedirs(self.logs)
        os.makedirs(self.usage)
        current = [
            entry(BASE + 100, IPS[0], "GET", "/"),
            entry(BASE + 200, IPS[0], "GET", "/?x=1"),
            entry(BASE + 300, IPS[1], "GET", "/static/a.js"),
            "this is not json",
            "{}",
            # sessions: A 600-1200 (10m), B 900-1500 (10m), C 1300-1360 (1m)
            entry(BASE + 1200, IPS[0], "GET", "/_stcore/stream", 600),
            entry(BASE + 1500, IPS[1], "GET", "/_stcore/stream", 600),
        ]
        rolled = [
            entry(BASE + 1360, IPS[2], "GET", "/_stcore/stream", 60),
            entry(BASE - 10, IPS[2], "GET", "/"),  # previous day
            # started previous day, ended today: belongs to previous day
            entry(BASE + 30, IPS[2], "GET", "/_stcore/stream", 100),
        ]
        with open(os.path.join(self.logs, "access.log"), "w") as fh:
            fh.write("\n".join(current) + "\n")
        with gzip.open(os.path.join(self.logs, "access-2026-10-22.log.gz"), "wt") as fh:
            fh.write("\n".join(rolled) + "\n")
        with open(os.path.join(self.usage, DAY + ".json"), "w") as fh:
            json.dump({"total": 28, "runs_workspace": {"abc": 1, "def": 3},
                       "runs_ip": {IPS[0]: 1}}, fh)

    def row(self):
        days = usage_report.collect(self.logs)
        return usage_report.day_row(days, self.usage, DAY)

    def test_numbers(self):
        r = self.row()
        self.assertEqual(r["unique_visitors"], 3)
        self.assertEqual(r["page_loads"], 2)
        self.assertEqual(r["sessions"], 3)
        self.assertEqual(r["peak_concurrent"], 2)
        self.assertEqual(r["median_session_minutes"], "10.0")
        self.assertEqual(r["demo_runs"], 4)
        self.assertEqual(r["model_calls"], 28)

    def test_missing_usage_file(self):
        days = usage_report.collect(self.logs)
        r = usage_report.day_row(days, self.usage, "2026-10-22")
        self.assertEqual((r["demo_runs"], r["model_calls"]), (0, 0))
        self.assertEqual(r["sessions"], 1)  # the session that started the previous day

    def test_output_has_no_ip_or_ua(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            usage_report.main(["--log-dir", self.logs, "--usage-dir", self.usage,
                               "--date", DAY])
        out = buf.getvalue()
        self.assertIn(DAY, out)
        for ip in IPS:
            self.assertNotIn(ip, out)
        self.assertNotIn("SecretAgent", out)

    def run_main(self, args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = usage_report.main(args)
        return code, buf.getvalue()

    def append_args(self, path):
        return ["--log-dir", self.logs, "--usage-dir", self.usage,
                "--date", DAY, "--append", path]

    def read_rows(self, path):
        with open(path, newline="") as fh:
            return list(csv.DictReader(fh))

    def test_append_once(self):
        path = os.path.join(self.tmp.name, "out.csv")
        args = self.append_args(path)
        _, out1 = self.run_main(args)
        _, out2 = self.run_main(args)
        self.assertEqual(out1.strip(), "appended " + DAY)
        self.assertEqual(out2.strip(), "unchanged " + DAY)
        rows = self.read_rows(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(list(rows[0]), usage_report.CSV_COLUMNS)
        self.assertEqual(rows[0]["sessions"], "3")
        with open(path) as fh:
            text = fh.read()
        for ip in IPS:
            self.assertNotIn(ip, text)

    def test_late_session_close_updates_row(self):
        path = os.path.join(self.tmp.name, "out.csv")
        args = self.append_args(path)
        self.run_main(args)
        # Closes the next day, started on DAY.
        with open(os.path.join(self.logs, "access.log"), "a") as fh:
            fh.write(entry(BASE + 86400 + 1800, IPS[1], "GET", "/_stcore/stream", 7200) + "\n")
        _, out = self.run_main(args)
        self.assertIn("updated " + DAY, out)
        self.assertIn("sessions 3->4", out)
        rows = [r for r in self.read_rows(path) if r["date"] == DAY]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sessions"], "4")

    def test_window_carries_old_rows(self):
        path = os.path.join(self.tmp.name, "out.csv")
        old = "2026-09-01,5,6,7,2,3.5,1,9\n"
        with open(path, "w", newline="") as fh:
            fh.write(",".join(usage_report.CSV_COLUMNS) + "\r\n" + old.replace("\n", "\r\n"))
        self.run_main(self.append_args(path))
        with open(path, newline="") as fh:
            lines = fh.read().split("\r\n")
        rows = self.read_rows(path)
        self.assertEqual([r["date"] for r in rows], ["2026-09-01", DAY])
        self.assertEqual(lines[1], old.strip())

    def test_header_mismatch_refuses(self):
        path = os.path.join(self.tmp.name, "out.csv")
        content = "date,foo\n2026-09-01,bar\n"
        with open(path, "w", newline="") as fh:
            fh.write(content)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code, _ = self.run_main(self.append_args(path))
        self.assertNotEqual(code, 0)
        self.assertIn("header", buf.getvalue())
        with open(path, newline="") as fh:
            self.assertEqual(fh.read(), content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
