"""交接期限与云端观测、重启及候选重试的交互。"""
import unittest
from unittest.mock import Mock, patch

import cdt_switcher as m
from tests.mock_e2e import make_world, ticks


class QuotaDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.r, self.f, self.store, self.journal, self.tg = make_world(
            account_names=("A", "B", "C"))
        self.addCleanup(self.store.close)
        self.f["A"].status = "Running"
        self.f["A"].duty = True
        self.r.tick()
        self.r.cfg.th.stop_retry_interval_sec = 60
        self.store.add_traffic("A", 200)

    def test_quota_source_hands_over_promptly_via_third_node(self):
        self.f["B"].nostock_times = 100
        with patch.object(m.time, "time", return_value=10000):
            ticks(self.r, 40)
        self.assertEqual(self.journal.load()["duty_account"], "C")
        self.assertEqual(self.f["A"].status, "Stopped")
        self.assertEqual(self.f["C"].status, "Running")
        self.assertFalse(self.journal.load().get("quota_deadlines"))

    def test_deadline_survives_restart_unknown_peer_and_stale_traffic(self):
        with patch.object(m.time, "time", return_value=10000):
            self.r.tick()
        self.assertEqual(self.journal.load()["quota_deadlines"]["A"]["deadline"], 11800)
        restarted = m.Rotator(self.r.cfg, self.store, m.StateJson(self.journal._path), self.tg)
        restarted.clients = self.f
        restarted._probe_service = lambda account: True
        original = self.f["B"].get_instance_obs
        self.f["B"].get_instance_obs = Mock(side_effect=m.CloudAPIError("offline"))
        with patch.object(self.store, "latest_traffic", return_value={}):
            with patch.object(m.time, "time", return_value=11799):
                restarted.transition_tick()
                self.assertEqual(self.f["A"].stop_calls, 0)
            with patch.object(m.time, "time", return_value=11800):
                restarted.transition_tick()
                self.assertEqual(self.f["A"].stop_calls, 1)
                self.assertEqual(self.f["C"].stop_calls, 0)
                self.assertIsNone(self.journal.load()["transition"]["from"])
        self.f["B"].get_instance_obs = original
        with patch.object(m.time, "time", return_value=11801):
            ticks(restarted)
        self.assertEqual(self.journal.load()["duty_account"], "B")

    def test_unconfirmed_source_waits_then_retries_failed_stop(self):
        with patch.object(m.time, "time", return_value=10000):
            self.r.tick()
        original = self.f["A"].get_instance_obs
        self.f["A"].get_instance_obs = Mock(side_effect=m.CloudAPIError("offline"))
        with patch.object(m.time, "time", return_value=11800):
            self.r.transition_tick()
            self.assertEqual(self.f["A"].stop_calls, 0)
            self.f["A"].get_instance_obs = original
            # B remains unknown so that only the targeted quota stop can run.
            self.f["B"].get_instance_obs = Mock(side_effect=m.CloudAPIError("offline"))
            with patch.object(self.f["A"], "stop_instance_stopcharging",
                              side_effect=m.CloudAPIError("retry")) as stop:
                self.r.transition_tick()
                self.r.transition_tick()
                stop.assert_called_once()
        with patch.object(m.time, "time", return_value=11860):
            self.r.transition_tick()
        self.assertEqual(self.f["A"].stopped_mode, "StopCharging")
        self.assertEqual(self.f["A"].status, "Stopped")

    def test_previous_month_deadline_cannot_stop_new_month_instance(self):
        with patch.object(m.time, "time", return_value=10000):
            self.r.tick()
        with (patch.object(m.time, "time", return_value=12000),
              patch.object(m, "current_month", return_value="2099-01")):
            self.r.transition_tick()
        self.assertFalse(self.journal.load().get("quota_deadlines"))
        self.assertEqual(self.f["A"].stop_calls, 0)

    def test_all_candidates_over_quota_triggers_immediate_breaker(self):
        for account in ("B", "C"):
            self.store.add_traffic(account, 200)
        with patch.object(m.time, "time", return_value=10000):
            ticks(self.r, 12)
        self.assertTrue(self.journal.load()["breaker_latched"])
        self.assertEqual(self.f["A"].status, "Stopped")

    def test_temporary_stock_failure_recovers_after_forced_stop(self):
        for account in ("B", "C"):
            self.f[account].nostock_times = 10
        with patch.object(m.time, "time", return_value=10000):
            ticks(self.r, 15)
        with patch.object(m.time, "time", return_value=10900):
            ticks(self.r, 15)
        with patch.object(m.time, "time", return_value=11800):
            ticks(self.r, 4)
        self.assertTrue(all(client.status == "Stopped" for client in self.f.values()))
        self.assertFalse(self.journal.load().get("breaker_latched"))
        self.f["B"].nostock_times = 0
        with patch.object(m.time, "time", return_value=12700):
            ticks(self.r, 16)
        self.assertEqual(self.journal.load()["duty_account"], "B")
