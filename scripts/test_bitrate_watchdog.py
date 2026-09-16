#!/usr/bin/env python3
"""Unit tests for bitrate-watchdog.py (stdlib only, no server needed)."""
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "watchdog", str(Path(__file__).with_name("bitrate-watchdog.py")))
wd = importlib.util.module_from_spec(SPEC)
sys.modules["watchdog"] = wd
SPEC.loader.exec_module(wd)


class TestBitrate(unittest.TestCase):
    def test_compute_basic(self):
        # 375000 bytes over 5s = 600kbps... use 2000kbps case:
        # 2000kbps * 5s / 8 = 1_250_000 bytes
        self.assertAlmostEqual(wd.compute_kbps(0, 1_250_000, 5.0), 2000.0)

    def test_compute_negative_delta_is_zero(self):
        self.assertEqual(wd.compute_kbps(100, 50, 5.0), 0.0)

    def test_compute_zero_dt(self):
        self.assertEqual(wd.compute_kbps(0, 100, 0.0), 0.0)

    def test_kick_endpoint_rtmp(self):
        self.assertEqual(
            wd.kick_endpoint("rtmpConn", "abc"),
            "/v3/rtmp/conns/kick/abc")

    def test_kick_endpoint_rtsp(self):
        self.assertEqual(
            wd.kick_endpoint("rtspSession", "abc"),
            "/v3/rtsp/sessions/kick/abc")

    def test_kick_endpoint_unknown(self):
        self.assertIsNone(wd.kick_endpoint("rtspSource", "abc"))
        self.assertIsNone(wd.kick_endpoint("rtmpConn", ""))

    def test_should_watch(self):
        self.assertTrue(wd.should_watch("live/key1", {"id": "x", "type": "rtmpConn"}))
        self.assertFalse(wd.should_watch("other/key", {"id": "x"}))
        self.assertFalse(wd.should_watch("live/key1", None))
        self.assertFalse(wd.should_watch("live/key1", {}))

    def test_sustained_violation_triggers_kick(self):
        # MAX=3000kbps, LIMIT=3: feed 4000kbps samples, expect kick on 3rd
        wd.MAX_TOTAL_KBPS = 3000
        wd.VIOLATION_LIMIT = 3
        wd.POLL_INTERVAL = 5
        state = {}
        # bytes for 4000kbps over 5s = 2_500_000
        step = 2_500_000
        samples = [
            {"name": "live/k", "inboundBytes": 0,
             "source": {"id": "c1", "type": "rtmpConn"}},
            {"name": "live/k", "inboundBytes": step,
             "source": {"id": "c1", "type": "rtmpConn"}},
            {"name": "live/k", "inboundBytes": step * 2,
             "source": {"id": "c1", "type": "rtmpConn"}},
            {"name": "live/k", "inboundBytes": step * 3,
             "source": {"id": "c1", "type": "rtmpConn"}},
        ]
        times = [1000.0, 1005.0, 1010.0, 1015.0]
        kicks = []
        idx = {"i": 0}

        def fake_get(_path, timeout=10.0):
            return {"items": [samples[idx["i"]]]}

        with patch.object(wd, "api_get", side_effect=fake_get), \
             patch.object(wd, "api_kick",
                          side_effect=lambda t, i, timeout=10.0: kicks.append((t, i)) or True), \
             patch.object(wd.time, "time", side_effect=lambda: times[idx["i"]]):
            for i in range(4):
                idx["i"] = i
                wd.poll_once(state)
        self.assertEqual(kicks, [("rtmpConn", "c1")])

    def test_under_limit_never_kicks(self):
        wd.MAX_TOTAL_KBPS = 3000
        wd.VIOLATION_LIMIT = 3
        state = {}
        step = 1_250_000  # 2000kbps over 5s
        samples = [{"name": "live/ok", "inboundBytes": step * i,
                    "source": {"id": "c9", "type": "rtmpConn"}} for i in range(5)]
        times = [2000.0 + 5 * i for i in range(5)]
        kicks = []
        idx = {"i": 0}

        def fake_get(_path, timeout=10.0):
            return {"items": [samples[idx["i"]]]}

        with patch.object(wd, "api_get", side_effect=fake_get), \
             patch.object(wd, "api_kick",
                          side_effect=lambda t, i, timeout=10.0: kicks.append((t, i)) or True), \
             patch.object(wd.time, "time", side_effect=lambda: times[idx["i"]]):
            for i in range(5):
                idx["i"] = i
                wd.poll_once(state)
        self.assertEqual(kicks, [])
        self.assertEqual(state["live/ok"]["violations"], 0)

    def test_non_sustained_spike_resets(self):
        # over, over, under, over, over -> never reaches 3 consecutive
        wd.MAX_TOTAL_KBPS = 3000
        wd.VIOLATION_LIMIT = 3
        state = {}
        over = 2_500_000   # 4000kbps/5s
        under = 625_000    # 1000kbps/5s
        totals, acc = [], 0
        for d in (0, over, over, under, over, over):
            acc += d
            totals.append(acc)
        kicks = []
        idx = {"i": 0}
        times = [3000.0 + 5 * i for i in range(len(totals))]

        def fake_get(_path, timeout=10.0):
            i = idx["i"]
            return {"items": [{"name": "live/s", "inboundBytes": totals[i],
                               "source": {"id": "c2", "type": "rtmpConn"}}]}

        with patch.object(wd, "api_get", side_effect=fake_get), \
             patch.object(wd, "api_kick",
                          side_effect=lambda t, i, timeout=10.0: kicks.append((t, i)) or True), \
             patch.object(wd.time, "time", side_effect=lambda: times[idx["i"]]):
            for i in range(len(totals)):
                idx["i"] = i
                wd.poll_once(state)
        self.assertEqual(kicks, [])


    def test_poll_returns_ok_and_kicks(self):
        wd.MAX_TOTAL_KBPS = 3000
        wd.VIOLATION_LIMIT = 1  # kick on first over-limit sample
        state = {"live/q": {"prev_bytes": 0, "prev_time": 1000.0, "violations": 0}}
        item = {"name": "live/q", "inboundBytes": 2_500_000,
                "source": {"id": "c3", "type": "rtmpConn"}}
        with patch.object(wd, "api_get", return_value={"items": [item]}), \
             patch.object(wd, "api_kick", return_value=True), \
             patch.object(wd.time, "time", return_value=1005.0):
            ok, kicks = wd.poll_once(state)
        self.assertTrue(ok)
        self.assertEqual(kicks, 1)

    def test_poll_schema_invalid_is_failure(self):
        for bad in ({"nope": []}, [], None, "items"):
            with patch.object(wd, "api_get", return_value=bad):
                ok, kicks = wd.poll_once({})
            self.assertFalse(ok, bad)
            self.assertEqual(kicks, 0)

    def test_poll_api_error_is_failure(self):
        with patch.object(wd, "api_get", side_effect=ConnectionError("down")):
            ok, kicks = wd.poll_once({})
        self.assertFalse(ok)
        self.assertEqual(kicks, 0)

    def test_validate_config_clamps(self):
        old = (wd.MAX_TOTAL_KBPS, wd.POLL_INTERVAL, wd.VIOLATION_LIMIT,
               wd.MAX_CONSECUTIVE_FAILURES, wd.HEARTBEAT_MAX_AGE)
        try:
            wd.MAX_TOTAL_KBPS = -1
            wd.POLL_INTERVAL = 0
            wd.VIOLATION_LIMIT = 0
            wd.MAX_CONSECUTIVE_FAILURES = 0
            wd.HEARTBEAT_MAX_AGE = -5
            warns = wd.validate_config()
            self.assertEqual(len(warns), 5)
            self.assertEqual(
                (wd.MAX_TOTAL_KBPS, wd.POLL_INTERVAL, wd.VIOLATION_LIMIT,
                 wd.MAX_CONSECUTIVE_FAILURES, wd.HEARTBEAT_MAX_AGE),
                (2500.0, 5.0, 3, 12, 90.0))
        finally:
            (wd.MAX_TOTAL_KBPS, wd.POLL_INTERVAL, wd.VIOLATION_LIMIT,
             wd.MAX_CONSECUTIVE_FAILURES, wd.HEARTBEAT_MAX_AGE) = old

    def test_heartbeat_fresh_and_check(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            hb = str(Path(td) / "hb")
            old_file, old_age = wd.HEARTBEAT_FILE, wd.HEARTBEAT_MAX_AGE
            try:
                wd.HEARTBEAT_FILE = hb
                wd.HEARTBEAT_MAX_AGE = 90.0
                self.assertFalse(wd.heartbeat_fresh())  # missing
                self.assertEqual(wd.healthcheck(), 1)
                wd.write_heartbeat({"polls": 1, "kicks": 0, "failures": 0})
                self.assertTrue(wd.heartbeat_fresh())
                self.assertEqual(wd.healthcheck(), 0)
                # stale mtime
                os.utime(hb, (0, 0))
                self.assertFalse(wd.heartbeat_fresh(now=10_000_000.0))
            finally:
                wd.HEARTBEAT_FILE, wd.HEARTBEAT_MAX_AGE = old_file, old_age


if __name__ == "__main__":
    unittest.main()
