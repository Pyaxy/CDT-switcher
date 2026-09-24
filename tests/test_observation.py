"""实例观测告警阈值及三节点/月初行为回归，全部使用离线云客户端。"""
from pathlib import Path
import os
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cdt_switcher as m
from tests.mock_e2e import make_world, ticks, assert_one_running


class ObservationTests(unittest.TestCase):
    def world(self, names=("A", "B", "C")):
        r, f, s, j, tg = make_world(account_names=names)
        self.addCleanup(s.close)
        ticks(r)
        tg.messages.clear()
        return r, f, s, j, tg

    def test_threshold_freezes_immediately_and_counts_actual_queries(self):
        for loop in ("tick", "transition_tick"):
            with self.subTest(loop=loop):
                r, f, s, j, tg = self.world()
                r.submit_intent("SWITCH", account="B")
                before = {a: (c.status, c.duty, c.stop_calls) for a, c in f.items()}
                f["C"].get_instance_obs = Mock(side_effect=m.CloudAPIError("offline"))
                for count in range(1, 4):
                    getattr(r, loop)()
                    self.assertEqual(r._observation_fails["C"], count)
                    self.assertEqual(len(tg.messages), int(count == 3))
                    self.assertEqual(before, {
                        a: (c.status, c.duty, c.stop_calls) for a, c in f.items()})
                # 安全守卫重复检查同一快照不增加失败次数。
                snapshot = {a: m.InstanceObs(obs=m.OBS_UNKNOWN) for a in f}
                for _ in range(4):
                    r._freeze_unknown(snapshot)
                self.assertEqual(r._observation_fails["C"], 3)
                getattr(r, loop)()
                self.assertEqual(len(tg.messages), 1)  # 重复告警受时间窗口限制
                self.assertIn("连续 3 次", tg.messages[0])

    def test_success_resets_and_accounts_do_not_share_failure_streak(self):
        r, f, s, j, tg = self.world()
        original = f["A"].get_instance_obs
        f["A"].get_instance_obs = Mock(return_value=m.InstanceObs(obs=m.OBS_UNKNOWN))
        ticks(r, 2)
        f["A"].get_instance_obs = original
        f["B"].get_instance_obs = Mock(side_effect=m.CloudAPIError("offline"))
        ticks(r, 2)
        self.assertEqual(r._observation_fails["A"], 0)
        self.assertEqual(tg.messages, [])
        f["A"].get_instance_obs = Mock(side_effect=m.CloudAPIError("offline"))
        ticks(r, 1)
        self.assertEqual(len(tg.messages), 1)
        self.assertIn("B", tg.messages[0])
        ticks(r, 2)
        self.assertEqual(len(tg.messages), 2)  # B 的节流不会压掉 A 的告警

    def test_threshold_one_and_repeat_interval(self):
        r, f, s, j, tg = self.world()
        r.cfg.th.observation_failure_threshold = 1
        f["C"].get_instance_obs = Mock(side_effect=m.CloudAPIError("offline"))
        ticks(r, 2)
        self.assertEqual(len(tg.messages), 1)
        r._notification_last["observation-unknown:C"] -= r.cfg.th.alert_repeat_interval_sec
        ticks(r, 1)
        self.assertEqual(len(tg.messages), 2)

    def test_switch_recheck_failures_use_same_threshold(self):
        for step in (m.STEP_T4, m.STEP_T5):
            with self.subTest(step=step):
                r, f, s, j, tg = self.world()
                f["B"].status = "Running"
                if step == m.STEP_T5:
                    f["A"].status = "Stopped"
                    f["A"].duty = False
                state = j.load()
                state.update(global_state=m.GS_TRANSITION, transition={
                    "version": m.TRANSITION_VERSION, "step": step,
                    "from": "A", "to": "B", "started_at": time.time(),
                    "attempts": {}, "breaker": False, "stop_list": ["A"],
                    "meta": {"reason": "manual", "stop_done": {}},
                })
                j.save(state)
                first = f["B"].get_instance_obs()
                f["B"].get_instance_obs = Mock(side_effect=[
                    first, m.CloudAPIError("recheck failed"),
                    m.CloudAPIError("offline"), m.CloudAPIError("offline"),
                ])
                stop_calls = f["A"].stop_calls
                r.transition_tick()
                self.assertEqual(r._observation_fails["B"], 1)
                self.assertEqual(tg.messages, [])
                self.assertEqual(f["A"].stop_calls, stop_calls)
                self.assertIsNotNone(j.load()["transition"])
                r.transition_tick()
                self.assertEqual(tg.messages, [])
                r.transition_tick()
                self.assertEqual(len(tg.messages), 1)
                self.assertIn("连续 3 次", tg.messages[0])

    def test_config_yaml_environment_and_validation(self):
        config_path = str(Path(__file__).resolve().parents[1] / "config.example.yaml")
        with patch.dict(os.environ, {}, clear=True):
            cfg = m.load_config(config_path)
            self.assertEqual(cfg.th.observation_failure_threshold, 3)
        with patch.dict(os.environ, {"OBSERVATION_FAILURE_THRESHOLD": "5"}, clear=True):
            self.assertEqual(m.load_config(config_path).th.observation_failure_threshold, 5)
        for value in (0, -1):
            cfg.th.observation_failure_threshold = value
            with self.assertRaisesRegex(ValueError, "observation_failure_threshold"):
                m._validate_config(cfg.accounts, cfg.th)

    def test_new_account_gets_mean_runtime_and_actual_traffic(self):
        r, f, s, j, tg = make_world(seed_traffic=False, account_names=("A", "B", "C"))
        self.addCleanup(s.close)
        s.set_runtime("A", 1000)
        s.set_runtime("B", 3000)
        f["C"].traffic_gb = 12.3
        r.traffic_job()
        r.tick()
        self.assertEqual(s.get_runtimes(), {"A": 1000, "B": 3000, "C": 2000})
        self.assertEqual(s.latest_traffic()["C"], 12.3)
        self.assertIn("C", j.load()["accounts"])

    def test_three_running_converge_and_switch_each_target(self):
        r, f, s, j, tg = self.world()
        for cli in f.values():
            cli.status = "Running"
            cli.duty = True
        ticks(r)
        assert_one_running(f, j.load()["duty_account"])
        for target in ("A", "B", "C"):
            r.submit_intent("SWITCH", account=target)
            ticks(r)
            self.assertEqual(j.load()["global_state"], m.GS_STEADY)
            self.assertEqual(j.load()["duty_account"], target)
            assert_one_running(f, target)

    def test_month_reset_keeps_actual_traffic_and_requires_eligible_usage(self):
        for returned_usage in (0.3, 199):
            with self.subTest(returned_usage=returned_usage):
                r, f, s, j, tg = self.world()
                r.submit_intent("BREAKER")
                ticks(r)
                for account, cli in f.items():
                    s.set_runtime(account, 10000)
                    cli.traffic_gb = returned_usage
                r._perform_month_reset()
                self.assertEqual(s.get_runtimes(), {a: 0 for a in f})
                self.assertEqual(s.latest_traffic(), {a: returned_usage for a in f})
                ticks(r)
                if returned_usage == 0.3:
                    self.assertEqual(j.load()["global_state"], m.GS_STEADY)
                    assert_one_running(f, j.load()["duty_account"])
                else:
                    self.assertEqual(j.load()["global_state"], m.GS_BREAKER)
                    self.assertTrue(all(c.status == "Stopped" for c in f.values()))


if __name__ == "__main__":
    unittest.main()
