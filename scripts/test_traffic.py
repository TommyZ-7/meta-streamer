#!/usr/bin/env python3
"""Unit tests for traffic.py (fixture-based, no vnstat needed)."""
import importlib.util
import sys
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "traffic", str(Path(__file__).with_name("traffic.py")))
tr = importlib.util.module_from_spec(SPEC)
sys.modules["traffic"] = tr
SPEC.loader.exec_module(tr)

FIXTURE = {
    "interfaces": [
        {"name": "lo", "traffic": {"total": {"rx": 999, "tx": 999}, "day": []}},
        {"name": "ens3", "traffic": {
            "total": {"rx": 10_000, "tx": 50_000},
            "day": [
                {"date": {"year": 2026, "month": 8, "day": 30},
                 "rx": 1_000_000_000, "tx": 2_000_000_000},
                {"date": {"year": 2026, "month": 9, "day": 1},
                 "rx": 3_000_000_000, "tx": 4_000_000_000},
                {"date": {"year": 2026, "month": 9, "day": 2},
                 "rx": 5_000_000_000, "tx": 6_000_000_000},
                {"date": {"year": 2026, "month": 9, "day": 3},  # broken entry
                 "rx": 1, "tx": 1},
                {"nonsense": True},
            ]}},
    ]
}


class TestTraffic(unittest.TestCase):
    def test_pick_iface_skips_lo(self):
        self.assertEqual(tr.pick_iface(FIXTURE)["name"], "ens3")

    def test_pick_iface_env(self):
        tr.IFACE_ENV = "lo"
        try:
            self.assertEqual(tr.pick_iface(FIXTURE)["name"], "lo")
        finally:
            tr.IFACE_ENV = ""
        self.assertIsNone(tr.pick_iface({"interfaces": []}))

    def test_vnstat_days(self):
        days = tr.vnstat_days(tr.pick_iface(FIXTURE))
        self.assertEqual(days["2026-08-30"], (1_000_000_000, 2_000_000_000))
        self.assertEqual(days["2026-09-02"], (5_000_000_000, 6_000_000_000))
        self.assertNotIn("nonsense", str(days))

    def test_merge_vnstat_wins(self):
        csv_rows = {"2026-09-01": (1, 1), "2026-07-01": (7, 8)}
        live = {"2026-09-01": (3_000_000_000, 4_000_000_000)}
        m = tr.merge(csv_rows, live)
        self.assertEqual(m["2026-09-01"], (3_000_000_000, 4_000_000_000))
        self.assertEqual(m["2026-07-01"], (7, 8))  # old csv history kept

    def test_build_months(self):
        merged = {"2026-09-02": (5, 6), "2026-09-01": (3, 4),
                  "2026-08-30": (1, 2)}
        months = tr.build_months(merged)
        self.assertEqual(months["2026-09"]["rx"], 8)
        self.assertEqual(months["2026-09"]["tx"], 10)
        self.assertEqual([d["day"] for d in months["2026-09"]["days"]],
                         ["2026-09-01", "2026-09-02"])

    def test_snapshot_upsert(self):
        import tempfile
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "traffic-daily.csv"
            tr.save_snapshot(p, "ens3", {today: (5, 6)})
            tr.save_snapshot(p, "ens3", {today: (50, 60)})  # overwrite same day
            rows = tr.load_csv(p, "ens3")
            self.assertEqual(rows, {today: (50, 60)})
            self.assertEqual(tr.load_csv(p, "eth0"), {})  # other iface ignored

    def test_render_html_embeds_months(self):
        payload = {"updated": "t", "iface": "ens3",
                   "months": {"2026-09": {"days": [], "rx": 1, "tx": 2}}}
        html = tr.render_html(payload)
        self.assertIn('<select id="month">', html)
        self.assertIn("2026-09", html)
        self.assertIn("traffic.json", html)

    def test_fmt_gb(self):
        self.assertEqual(tr.fmt_gb(2_500_000_000), "2.50")


if __name__ == "__main__":
    unittest.main()
