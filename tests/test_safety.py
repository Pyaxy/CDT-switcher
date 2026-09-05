"""审计故障回归；所有云操作使用 FakeAliyun，进程锁使用临时路径。

运行：.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.mock_e2e import make_world, ticks
import cdt_switcher as m


class SafetyTests(unittest.TestCase):
    def world(self, names=("A", "B")):
        r, f, s, j, tg = make_world(account_names=names)
        self.addCleanup(s.close)
        ticks(r)
        tg.messages.clear()
        return r, f, s, j, tg

    def stage(self, j, step, **kwargs):
        tr = dict(version=2, step=step, started_at=time.time(), attempts={},
                  breaker=False, stop_list=["A"], meta={"reason": "manual", "stop_done": {}})
        tr.update({"from": "A", "to": "B"})
        tr.update(kwargs)
        state = j.load()
        state.update(global_state=m.GS_TRANSITION, transition=tr)
        j.save(state)
        return tr

    def test_ecs_requires_explicit_instance_list(self):
        r, f, s, j, tg = self.world()
        cli = m.AliyunAccountClient(r.cfg.accounts["A"])
        for payload in ({}, {"Instances": {}}, {"Instances": None},
                        {"Instances": {"Instance": "broken"}},
                        {"Instances": {"Instance": [{"InstanceId": "wrong", "Status": "Running"}]}}):
            with self.subTest(payload=payload), patch.object(cli, "_call", return_value=payload):
                with self.assertRaises(m.CloudAPIError):
                    cli.get_instance_obs()
        with patch.object(cli, "_call", return_value={"Instances": {"Instance": []}}):
            self.assertEqual(cli.get_instance_obs().obs, m.OBS_NOT_FOUND)
        with patch.object(cli, "_call", return_value={"Instances": {"Instance": [
            {"InstanceId": "i-a", "Status": "Running"}]}}):
            self.assertEqual(cli.get_instance_obs().obs, m.OBS_RUNNING)

    def test_bad_cdt_does_not_replace_good_snapshot(self):
        r, f, s, j, tg = self.world()
        s.add_traffic("A", 180)
        invalid = [None, {}, {"Traffic": "214748364800"}, {"TrafficDetails": None},
                   {"Items": [{"TrafficBytes": 123}]},
                   {"TrafficDetails": [None]},
                   {"TrafficDetails": [{"Traffic": 12}]}]
        for value in (None, "broken", True, -1, float("nan"), float("inf")):
            invalid.append({"TrafficDetails": [{"BusinessRegionId": "cn-hongkong", "Traffic": value}]})
        invalid.append({"TrafficDetails": [{"BusinessRegionId": "cn-hongkong"}]})
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    m._parse_traffic_gb(payload)
                f["A"].get_traffic_gb = lambda payload=payload: m._parse_traffic_gb(payload)
                values, errors = r._refresh_traffic()
                self.assertIn("A", errors)
                self.assertNotIn("A", values)
                self.assertEqual(s.latest_traffic()["A"], 180)
        self.assertEqual(m._parse_traffic_gb({"TrafficDetails": []}), 0)
        self.assertEqual(m._parse_traffic_gb({"TrafficDetails": [
            {"BusinessRegionId": "cn-hongkong", "Traffic": "1073741824"},
            {"BusinessRegionId": "cn-beijing", "Traffic": 999}]}), 1)

    def test_both_control_loops_freeze_unknown_at_every_stage(self):
        for loop in ("tick", "transition_tick"):
            for step in (None, m.STEP_T2, m.STEP_T3, m.STEP_T4, m.STEP_T5):
                with self.subTest(loop=loop, step=step):
                    r, f, s, j, tg = self.world()
                    if step:
                        self.stage(j, step)
                    else:
                        r.submit_intent("SWITCH", account="B")
                    before = {a: (c.status, c.duty) for a, c in f.items()}
                    f["A"].get_instance_obs = Mock(side_effect=m.CloudAPIError("offline"))
                    getattr(r, loop)()
                    self.assertEqual(before, {a: (c.status, c.duty) for a, c in f.items()})
                    self.assertEqual(j.load()["accounts"]["A"]["last_observed"], m.OBS_UNKNOWN)

    def test_t4_rechecks_target_before_stopping_source(self):
        for failure in ("released", "port_down", "unbound", "api_error"):
            with self.subTest(failure=failure):
                r, f, s, j, tg = self.world()
                f["B"].status = "Running"
                self.stage(j, m.STEP_T4)
                before = f["A"].stop_calls
                # 巡检快照之后、实际停机之前发生故障。
                original = f["B"].get_instance_obs
                reads = []
                def changed():
                    ob = original()
                    reads.append(1)
                    if len(reads) > 1:
                        if failure == "released":
                            return m.InstanceObs(exists=False, obs=m.OBS_NOT_FOUND)
                        if failure == "api_error":
                            raise m.CloudAPIError("offline")
                        if failure == "unbound":
                            ob.eip_bound = False
                    return ob
                f["B"].get_instance_obs = changed
                if failure == "port_down":
                    r._probe_service = lambda a: a != "B"
                r.transition_tick()
                self.assertEqual(f["A"].status, "Running")
                self.assertEqual(f["A"].stop_calls, before)
                self.assertIsNotNone(j.load().get("transition"))

    def blocked_tags(self):
        r, f, s, j, tg = self.world()
        f["A"].status = "Stopped"
        f["A"].duty = False
        f["B"].status = "Running"
        f["B"].set_duty = Mock(side_effect=m.CloudAPIError("Tag permission denied"))
        self.stage(j, m.STEP_T5)
        r.transition_tick()
        self.assertEqual(j.load()["transition"]["step"], m.STEP_T5)
        return r, f, s, j, tg

    def test_manual_breaker_preempts_t5_even_with_full_queue(self):
        r, f, s, j, tg = self.blocked_tags()
        for _ in range(r.MAX_INTENT_QUEUE_SIZE):
            r.submit_intent("SWITCH", account="B")
        self.assertTrue(r.submit_intent("BREAKER"))
        r.transition_tick()
        self.assertEqual(f["B"].status, "Stopped")
        self.assertTrue(j.load()["breaker_latched"])
        for _ in range(6):
            r.transition_tick()
        self.assertEqual(j.load()["global_state"], m.GS_BREAKER)

    def test_quota_protection_preempts_failed_tags(self):
        for quota in ("traffic", "runtime"):
            with self.subTest(quota=quota):
                r, f, s, j, tg = self.blocked_tags()
                if quota == "traffic":
                    s.add_traffic("B", 999)
                else:
                    s.set_runtime("B", 10 ** 9)
                for _ in range(16):
                    r.transition_tick()
                self.assertEqual(f["A"].status, "Running")
                self.assertEqual(f["B"].status, "Stopped")
                self.assertEqual(j.load()["duty_account"], "A")

    def test_breaker_retries_stop_even_when_tags_fail_and_OOS_restarts(self):
        r, f, s, j, tg = self.world()
        f["A"].set_duty = Mock(side_effect=m.CloudAPIError("Tag permission denied"))
        r.submit_intent("BREAKER")
        for _ in range(5):
            r.transition_tick()
        self.assertEqual(j.load()["transition"]["step"], m.STEP_T5)
        f["A"].status = "Running"  # OOS 按未清除的 on 标签再次拉起。
        for _ in range(3):
            r.transition_tick()
        self.assertEqual(f["A"].status, "Stopped")
        self.assertTrue(j.load()["breaker_latched"])
        self.assertIsNotNone(j.load()["transition"])

    def test_breaker_is_persisted_but_does_not_stop_unknown_instances(self):
        r, f, s, j, tg = self.world()
        original = f["A"].get_instance_obs
        f["A"].get_instance_obs = Mock(side_effect=m.CloudAPIError("offline"))
        r.submit_intent("BREAKER")
        r.transition_tick()
        self.assertTrue(j.load()["breaker_latched"])
        self.assertEqual(f["A"].status, "Running")
        f["A"].get_instance_obs = original
        r.transition_tick()
        self.assertEqual(f["A"].status, "Stopped")

    def test_t4_rechecks_target_between_each_source_stop(self):
        r, f, s, j, tg = self.world(names=("A", "B", "C"))
        f["B"].status = f["C"].status = "Running"
        self.stage(j, m.STEP_T4, stop_list=["A", "C"])
        original = f["A"].stop_instance_stopcharging
        def stop_then_target_fails():
            original()
            f["B"].status = "Stopped"
        f["A"].stop_instance_stopcharging = stop_then_target_fails
        r.transition_tick()
        self.assertEqual(f["A"].status, "Stopped")
        self.assertEqual(f["C"].status, "Running")
        self.assertEqual(j.load()["transition"]["step"], m.STEP_T3)

    def test_all_running_over_quota_converge_to_breaker(self):
        r, f, s, j, tg = self.world()
        for a, cli in f.items():
            cli.status = "Running"
            s.add_traffic(a, 999)
        ticks(r, 20)
        self.assertEqual(j.load()["global_state"], m.GS_BREAKER)
        self.assertTrue(all(cli.status == "Stopped" and not cli.duty for cli in f.values()))

    def test_manual_over_quota_target_is_rejected_not_silently_replaced(self):
        r, f, s, j, tg = self.world()
        s.add_traffic("B", 999)
        self.stage(j, m.STEP_T1)
        r.transition_tick()
        self.assertIsNone(j.load()["transition"])
        self.assertEqual(j.load()["duty_account"], "A")
        self.assertEqual(f["A"].status, "Running")
        self.assertEqual(f["B"].status, "Stopped")

    def test_multi_with_unknown_usage_is_not_treated_as_all_exceeded(self):
        r, f, s, j, tg = self.world()
        for cli in f.values():
            cli.status = "Running"
        with s._lock:
            s._conn.execute("DELETE FROM traffic_snapshots")
            s._conn.commit()
        ticks(r, 6)
        self.assertIsNone(j.load().get("transition"))
        self.assertTrue(all(cli.status == "Running" for cli in f.values()))
        self.assertFalse(j.load().get("breaker_latched"))

    def test_restart_preserves_breaker_with_one_or_many_running(self):
        for running_count in (1, 2):
            with self.subTest(running_count=running_count):
                r, f, s, j, tg = self.world()
                j.save({"global_state": m.GS_BREAKER, "transition": None, "duty_account": None})
                for a in list(f)[:running_count]:
                    f[a].status = "Running"
                r._first_done = False
                r.tick()
                self.assertEqual(j.load()["global_state"], m.GS_BREAKER)
                ticks(r, 5)
                self.assertTrue(all(cli.status == "Stopped" for cli in f.values()))
                self.assertTrue(j.load()["breaker_latched"])
                r.submit_intent("SWITCH", account="A")
                r.transition_tick()
                self.assertTrue(all(cli.status == "Stopped" for cli in f.values()))

    def test_stop_done_cannot_hide_restarted_source(self):
        r, f, s, j, tg = self.world()
        f["B"].status = "Running"
        self.stage(j, m.STEP_T4, meta={"reason": "manual", "stop_done": {"A": True}})
        r.transition_tick()
        self.assertEqual(f["A"].status, "Stopped")

    def test_t5_detects_OOS_restart_after_stop_confirmation(self):
        r, f, s, j, tg = self.world()
        f["A"].duty = False
        f["B"].status = "Running"
        f["B"].duty = True
        self.stage(j, m.STEP_T5, meta={"reason": "manual", "stop_done": {"A": True},
                                    "tag_done": {"A": True, "B": True}})
        r.transition_tick()
        self.assertIsNotNone(j.load().get("transition"))
        self.assertFalse(any("✅ 切换完成" in msg for msg in tg.messages))
        for _ in range(10):
            r.transition_tick()
        self.assertEqual(f["A"].status, "Stopped")
        self.assertEqual(j.load()["duty_account"], "B")

    def test_tag_done_is_rechecked_and_unknown_cannot_complete(self):
        r, f, s, j, tg = self.blocked_tags()
        tr = j.load()["transition"]
        tr["meta"]["tag_done"] = {"A": True, "B": True}
        state = j.load(); state["transition"] = tr; j.save(state)
        r.transition_tick()
        self.assertIsNotNone(j.load().get("transition"))
        f["B"].get_instance_obs = Mock(side_effect=m.CloudAPIError("offline"))
        r.transition_tick()
        self.assertIsNotNone(j.load().get("transition"))
        self.assertFalse(any("✅ 切换完成" in msg for msg in tg.messages))

    def test_t5_final_read_detects_target_disappearing_after_tag_write(self):
        r, f, s, j, tg = self.world()
        f["A"].status = "Stopped"; f["A"].duty = False
        f["B"].status = "Running"; f["B"].duty = True
        self.stage(j, m.STEP_T5)
        original = f["B"].get_instance_obs
        first = original()
        f["B"].get_instance_obs = Mock(side_effect=[first, m.InstanceObs(exists=False, obs=m.OBS_NOT_FOUND)])
        r.transition_tick()
        self.assertEqual(j.load()["global_state"], m.GS_ZERO)
        self.assertFalse(any("✅ 切换完成" in msg for msg in tg.messages))


class ProcessLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "state.json")

    def child(self, code):
        return subprocess.run([sys.executable, "-c", code, self.path],
                              capture_output=True, text=True, timeout=10)

    def test_other_process_is_rejected_then_allowed_after_release(self):
        code = "import sys; from cdt_switcher import ProcessLock;\nwith ProcessLock([sys.argv[1]]): print('acquired')"
        with m.ProcessLock([self.path]):
            result = self.child(code)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("已有 CDT-switcher", result.stderr)
        self.assertEqual(self.child(code).returncode, 0)

    def test_crashed_holder_releases_kernel_lock(self):
        code = "import sys; from cdt_switcher import ProcessLock;\nwith ProcessLock([sys.argv[1]]):\n print('ready',flush=True)\n input()"
        proc = subprocess.Popen([sys.executable, "-c", code, self.path],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            import select
            ready, _, _ = select.select([proc.stdout], [], [], 10)
            self.assertTrue(ready)
            self.assertEqual(proc.stdout.readline().strip(), "ready")
            with self.assertRaises(RuntimeError):
                with m.ProcessLock([self.path]):
                    pass
            proc.kill(); proc.wait(timeout=10)
            with m.ProcessLock([self.path]):
                pass
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)

    def test_alias_and_shared_database_cannot_bypass_lock(self):
        alias = str(Path(self.temp.name) / "alias")
        Path(self.path).touch()
        os.symlink(self.path, alias)
        with m.ProcessLock([self.path]):
            with self.assertRaises(RuntimeError):
                with m.ProcessLock([alias]):
                    pass
        shared = str(Path(self.temp.name) / "shared.db")
        with m.ProcessLock([self.path, shared]):
            with self.assertRaises(RuntimeError):
                with m.ProcessLock([str(Path(self.temp.name) / "other.json"), shared]):
                    pass

    def test_main_rejects_duplicate_before_cloud_initialization(self):
        cfg = Mock()
        cfg.th.db_path = self.path
        cfg.th.state_path = str(Path(self.temp.name) / "other.json")
        with m.ProcessLock([self.path]), patch.object(m, "load_config", return_value=cfg), \
             patch.object(m, "_run") as run, patch.object(sys, "argv", ["cdt_switcher.py", self.path]):
            with self.assertRaises(SystemExit):
                m.main()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
