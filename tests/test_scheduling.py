"""两种候选选择策略的离线回归；不调用真实云服务。"""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cdt_switcher as m
from tests.mock_e2e import assert_one_running, make_world, ticks


class SchedulingTests(unittest.TestCase):
    def world(self, strategy="priority", names=("A", "B", "C")):
        r, f, s, j, tg = make_world(account_names=names)
        self.addCleanup(s.close)
        r.cfg.scheduling.strategy = strategy
        for a in names:
            r.cfg.accounts[a].priority = 100 if a == "C" else 10
        for a, seconds in {"A": 2000, "B": 1000, "C": 0}.items():
            s.set_runtime(a, seconds)
        return r, f, s, j, tg

    def test_default_balanced_ignores_priority(self):
        r, f, s, j, tg = self.world(strategy=m.SchedulingCfg().strategy)
        self.assertEqual(r._select_target(s.get_runtimes()), "C")
        ticks(r)
        assert_one_running(f, "C")

    def test_priority_groups_keep_balancing_within_group(self):
        r, f, s, j, tg = self.world()
        self.assertEqual(r._select_target(s.get_runtimes()), "B")
        ticks(r)
        assert_one_running(f, "B")
        # 两台主力分别耗尽流量和时长，才选备用 C。
        s.add_traffic("A", r.cfg.th.traffic_threshold_gb)
        s.set_runtime("B", int(r.cfg.th.tmax_hours * 3600))
        ticks(r)
        assert_one_running(f, "C")

    def test_ties_keep_config_order_not_alphabetical_order(self):
        for strategy in ("balanced", "priority"):
            with self.subTest(strategy=strategy):
                r, f, s, j, tg = self.world(strategy, names=("B", "A", "C"))
                for a in f:
                    s.set_runtime(a, 0)
                self.assertEqual(r._select_target(s.get_runtimes()), "B")

    def test_priority_never_admits_ineligible_candidates(self):
        for strategy in ("balanced", "priority"):
            for failure in ("traffic", "runtime", "arrears", "missing", "stale"):
                with self.subTest(strategy=strategy, failure=failure):
                    r, f, s, j, tg = self.world(strategy)
                    for a in ("A", "B"):
                        s.set_runtime(a, 0)
                        if failure == "traffic":
                            s.add_traffic(a, r.cfg.th.traffic_threshold_gb)
                        elif failure == "runtime":
                            s.set_runtime(a, int(r.cfg.th.tmax_hours * 3600))
                        elif failure == "arrears":
                            r._arrears.add(a)
                        elif failure == "missing":
                            s._conn.execute("DELETE FROM traffic_snapshots WHERE account = ?", (a,))
                        else:
                            s._conn.execute(
                                "UPDATE traffic_snapshots SET ts = ? WHERE account = ?",
                                ("2000-01-01T00:00:00", a))
                    s._conn.commit()
                    self.assertEqual(r._select_target(s.get_runtimes()), "C")
                    s.add_traffic("C", r.cfg.th.traffic_threshold_gb)
                    self.assertIsNone(r._select_target(s.get_runtimes()))

    def test_running_backup_stays_after_month_reset_until_manual_switch(self):
        r, f, s, j, tg = self.world()
        f["C"].status = "Running"
        f["C"].duty = True
        ticks(r)
        assert_one_running(f, "C")
        r.month_reset()
        ticks(r)
        assert_one_running(f, "C")
        self.assertIsNone(j.load().get("transition"))
        r.submit_intent("SWITCH")
        ticks(r)
        assert_one_running(f, "A")

    def test_explicit_manual_target_bypasses_priority_but_not_eligibility(self):
        r, f, s, j, tg = self.world()
        ticks(r)
        assert_one_running(f, "B")
        r.submit_intent("SWITCH", account="C")
        ticks(r)
        assert_one_running(f, "C")
        s.add_traffic("A", r.cfg.th.traffic_threshold_gb)
        r.submit_intent("SWITCH", account="A")
        ticks(r)
        assert_one_running(f, "C")

    def test_multi_cleanup_ranks_only_eligible_running_nodes(self):
        r, f, s, j, tg = self.world()
        r.cfg.accounts["A"].priority = 0  # A 停机，不得扩张纠偏候选范围。
        for a in ("B", "C"):
            f[a].status = "Running"
            f[a].duty = True
        ticks(r)
        assert_one_running(f, "B")

    def test_inflight_quota_reselection_preserves_candidate_filter(self):
        for missing_primary in (False, True):
            with self.subTest(missing_primary=missing_primary):
                r, f, s, j, tg = self.world()
                ticks(r)
                r._start_transition(to="B")
                state = j.load()
                state["transition"]["step"] = m.STEP_T3
                j.save(state)
                s.add_traffic("B", r.cfg.th.traffic_threshold_gb)
                f["A"].exists = not missing_primary
                r.transition_tick()
                expected = "C" if missing_primary else "A"
                self.assertEqual(j.load()["transition"]["to"], expected)
                ticks(r)
                self.assertEqual(j.load()["duty_account"], expected)


class SchedulingConfigTests(unittest.TestCase):
    def setUp(self):
        config = Path(__file__).resolve().parents[1] / "config.example.yaml"
        self.data = m.yaml.safe_load(config.read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def load(self, data):
        path = Path(self.temp.name) / "config.yaml"
        path.write_text(m.yaml.safe_dump(data, sort_keys=False))
        with patch.dict(os.environ, {}, clear=True):
            return m.load_config(str(path))

    def test_legacy_and_priority_configs(self):
        self.data.pop("scheduling")
        for ac in self.data["accounts"].values():
            ac.pop("priority")
        cfg = self.load(self.data)
        self.assertEqual(cfg.scheduling.strategy, "balanced")
        self.assertEqual(cfg.accounts["A"].priority, 100)
        self.data["scheduling"] = {"strategy": "priority"}
        self.data["accounts"]["A"]["priority"] = 0
        cfg = self.load(self.data)
        self.assertEqual(cfg.scheduling.strategy, "priority")
        self.assertEqual(cfg.accounts["A"].priority, 0)
        self.assertEqual(cfg.accounts["B"].priority, 100)

    def test_invalid_strategy_and_priority_are_rejected(self):
        for value in (None, [], "priority", {"strategy": "unknown"},
                      {"strategy": None}, {"strategy": []}):
            with self.subTest(scheduling=value):
                data = copy.deepcopy(self.data)
                data["scheduling"] = value
                with self.assertRaisesRegex(ValueError, "scheduling"):
                    self.load(data)
        for value in (-1, True, 1.5, "10", None, []):
            with self.subTest(priority=value):
                data = copy.deepcopy(self.data)
                data["accounts"]["A"]["priority"] = value
                with self.assertRaisesRegex(ValueError, "priority"):
                    self.load(data)


if __name__ == "__main__":
    unittest.main()
