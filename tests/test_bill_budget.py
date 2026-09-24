"""账单预算保护的离线回归：金额、持久化、控制状态机与查询隔离。"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import billing_query as b
import cdt_switcher as m
from tests.mock_e2e import assert_one_running, make_world, ticks


def rows(amount="1", kind="PayAsYouGoBill", currency="USD"):
    return [b.BillRow("ECS", kind, currency, Decimal(amount))]


class BillBudgetTests(unittest.TestCase):
    def world(self):
        r, f, s, j, tg = make_world(account_names=("A", "B", "C"))
        self.addCleanup(s.close)
        r.cfg.billing = m.BillingCfg(enabled=True, threshold_usd=Decimal("10"))
        r.cfg.scheduling.strategy = "priority"
        for a in f:
            r.cfg.accounts[a].priority = 100 if a == "C" else 10
            s.record_bill_budget(a, m.current_month(), Decimal("1"))
        ticks(r)
        assert_one_running(f, "A")
        tg.messages.clear()
        return r, f, s, j, tg

    def test_amount_includes_prepaid_and_positive_adjustments_not_refunds(self):
        data = (rows("5.123", "SubscriptionOrder") + rows("4.877")
                + rows("0.5", "Adjustment") + rows("-0.25", "Adjustment")
                + rows("100", "Refund"))
        self.assertEqual(b.budget_amount_usd(data), Decimal("10.500"))
        for invalid in ([], rows("1", currency="CNY"), rows("1") + rows("1", currency="JPY"),
                        rows("1", "Refund"), rows("1", "Unknown"), rows("NaN")):
            with self.subTest(invalid=invalid), self.assertRaises(b.BillingError):
                b.budget_amount_usd(invalid)
        self.assertEqual(b.budget_amount_usd(rows("0")), Decimal("0"))

    def test_background_refresh_updates_budget_but_only_control_stops_instances(self):
        r, f, s, j, tg = self.world()
        def query(account, month):
            return rows("6", "SubscriptionOrder") + rows("4") if account.key == "A" else rows()
        with patch.object(b, "query_overview", side_effect=query) as fetch:
            r.billing_job()
            self.assertEqual(fetch.call_count, 3)
            assert_one_running(f, "A")
            self.assertEqual(tg.messages, [])
            ticks(r)
            assert_one_running(f, "B")
            self.assertEqual(fetch.call_count, 3)  # 巡检只读取本地快照
            self.assertIn("USD 10 / USD 10", r._summary_text("状态"))
            self.assertTrue(any("本月账单达到保护阈值" in msg for msg in tg.messages))
            r._billing_report_text()
            self.assertEqual(fetch.call_count, 3)  # TG 查询复用周期任务结果

    def test_budget_excludes_manual_target_and_all_exceeded_converge_to_breaker(self):
        r, f, s, j, tg = self.world()
        s.record_bill_budget("B", m.current_month(), Decimal("10"))
        r.submit_intent("SWITCH", account="B")
        ticks(r)
        assert_one_running(f, "A")
        for a in f:
            s.record_bill_budget(a, m.current_month(), Decimal("10"))
        ticks(r)
        self.assertEqual(j.load()["global_state"], m.GS_BREAKER)
        self.assertTrue(all(c.status == "Stopped" and not c.duty for c in f.values()))
        r.submit_intent("RESUME")
        ticks(r)
        self.assertEqual(j.load()["global_state"], m.GS_BREAKER)

    def test_failure_and_stale_values_do_not_stop_healthy_duty_or_become_zero(self):
        r, f, s, j, tg = self.world()
        with patch.object(b, "query_overview", side_effect=b.BillingError("权限不足")):
            r.billing_job()
        self.assertEqual(r._bill_budget_status("A")[0], "unknown")
        self.assertIsNotNone(r._select_target(s.get_runtimes()))
        ticks(r)
        assert_one_running(f, "A")
        report = r._summary_text("状态")
        self.assertIn("金额未知", report)
        self.assertIn("上次 USD 1", report)
        self.assertNotIn("USD 0", report)
        s.record_bill_budget("B", m.current_month(), Decimal("1"))
        s._conn.execute("UPDATE bill_budget SET queried_at = '2000-01-01T00:00:00' WHERE account='B'")
        s._conn.commit()
        self.assertEqual(r._bill_budget_status("B")[0], "unknown")
        self.assertTrue(r._eligible("B", s.get_runtimes(), s.latest_traffic()))
        s.record_bill_budget("B", m.current_month(), Decimal("1"))
        self.assertEqual(r._select_target(s.get_runtimes()), "B")

    def test_empty_or_wrong_currency_preserves_unknown_budget_without_breaking_report(self):
        r, f, s, j, tg = self.world()
        for data in ([], rows("1", currency="CNY")):
            with self.subTest(data=data), patch.object(b, "query_overview", return_value=data):
                text = r._billing_report_text(force=True)
                self.assertEqual(r._bill_budget_status("A")[0], "unknown")
                self.assertIn("账单保护：金额未知", text)
                self.assertIn("查询时间", text)
        ticks(r)
        assert_one_running(f, "A")

    def test_highwater_survives_lower_amount_failure_and_database_reopen(self):
        r, f, s, j, tg = self.world()
        s.record_bill_budget("A", m.current_month(), Decimal("10"))
        s.record_bill_budget("A", m.current_month(), Decimal("2"))
        s.record_bill_budget("A", m.current_month(), None, "网络失败")
        database = s._conn.execute("PRAGMA database_list").fetchone()[2]
        reopened = m.StateStore(database)
        self.addCleanup(reopened.close)
        restarted = m.Rotator(r.cfg, reopened, j, tg)
        restarted.clients = f
        restarted._probe_service = lambda _: True
        self.assertTrue(restarted._bill_exceeded("A"))
        self.assertEqual(reopened.get_bill_budget("A").amount, Decimal("2"))
        ticks(restarted)
        assert_one_running(f, "B")

    def test_new_month_does_not_reuse_old_highwater_or_claim_zero(self):
        r, f, s, j, tg = self.world()
        s.record_bill_budget("A", m.current_month(), Decimal("10"))
        with patch.object(m, "current_month", return_value="2099-01"):
            self.assertFalse(r._bill_exceeded("A"))
            self.assertEqual(r._bill_budget_status("A")[0], "unknown")
            s.record_bill_budget("A", "2099-01", Decimal("0.3"))
            self.assertEqual(r._bill_budget_status("A")[0], "ok")

    def test_result_crossing_month_boundary_cannot_authorize_new_month(self):
        r, f, s, j, tg = self.world()
        with patch.object(m, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2099, 1, 31, 23, 59, tzinfo=b.BILL_TZ)
            def finish_next_month(account, month):
                self.assertEqual(month, "2099-01")
                clock.now.return_value = datetime(2099, 2, 1, 0, 0, tzinfo=b.BILL_TZ)
                return rows("0")
            with patch.object(b, "query_overview", side_effect=finish_next_month):
                r._billing_report_text("A", force=True)
            self.assertIsNone(s.get_bill_budget("A"))
            self.assertEqual(r._bill_budget_status("A")[0], "unknown")

    def test_inflight_unknown_budget_does_not_freeze_and_exceeded_reselects(self):
        r, f, s, j, tg = self.world()
        r.submit_intent("SWITCH", account="B")
        r.transition_tick()
        self.assertEqual(j.load()["transition"]["step"], m.STEP_T2)
        s.record_bill_budget("B", m.current_month(), None, "网络失败")
        r.transition_tick()
        self.assertEqual(f["B"].status, "Running")
        self.assertEqual(f["A"].status, "Running")
        r.transition_tick()  # 启动请求后下一轮回读 Running 才进入服务检查。
        self.assertEqual(j.load()["transition"]["step"], m.STEP_T3)
        s.record_bill_budget("B", m.current_month(), Decimal("10"))
        r.transition_tick()
        self.assertEqual(j.load()["transition"]["to"], "A")
        ticks(r)
        assert_one_running(f, "A")

    def test_budget_network_lock_does_not_block_patrol_or_manual_breaker(self):
        r, f, s, j, tg = self.world()
        entered, release = threading.Event(), threading.Event()
        def blocked(account, month):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test timeout")
            return rows()
        worker = threading.Thread(target=r.billing_job)
        with patch.object(b, "query_overview", side_effect=blocked):
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                r.tick()
                self.assertIn("账单保护", r._summary_text("状态"))
                r.submit_intent("BREAKER")
                r.transition_tick()
                self.assertEqual(f["A"].status, "Stopped")
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_disabled_guard_leaves_original_selection_unchanged(self):
        r, f, s, j, tg = self.world()
        for a in f:
            s.record_bill_budget(a, m.current_month(), Decimal("100"))
        r.cfg.billing.threshold_usd = None
        self.assertIsNotNone(r._select_target(s.get_runtimes()))
        with patch.object(b, "query_overview") as query:
            r.billing_job()
            query.assert_not_called()
        ticks(r)
        assert_one_running(f, "A")
        self.assertNotIn("账单保护", r._summary_text("状态"))

    def test_multi_cleanup_uses_budget_before_priority(self):
        r, f, s, j, tg = self.world()
        for c in f.values():
            c.status = "Running"
        s.record_bill_budget("A", m.current_month(), Decimal("10"))
        s.record_bill_budget("B", m.current_month(), Decimal("10"))
        ticks(r)
        assert_one_running(f, "C")

    def test_unknown_ecs_still_freezes_budget_triggered_cloud_operations(self):
        r, f, s, j, tg = self.world()
        s.record_bill_budget("A", m.current_month(), Decimal("10"))
        before = {a: (c.status, c.duty, c.stop_calls) for a, c in f.items()}
        with patch.object(f["C"], "get_instance_obs", side_effect=m.CloudAPIError("offline")):
            ticks(r, 3)
        self.assertEqual(before, {a: (c.status, c.duty, c.stop_calls) for a, c in f.items()})
        ticks(r)
        assert_one_running(f, "B")

    def test_budget_config_validation_and_defaults(self):
        data = m.yaml.safe_load((Path(__file__).resolve().parents[1] / "config.example.yaml").read_text())
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / "config.yaml"
            def load(section):
                data["billing"] = section
                path.write_text(m.yaml.safe_dump(data))
                return m.load_config(str(path)).billing
            self.assertIsNone(load({"enabled": True}).threshold_usd)
            self.assertEqual(load({"enabled": True, "threshold_usd": 10}).threshold_usd, Decimal("10"))
            self.assertEqual(load({}).refresh_interval_min, 60)
            for value in (True, 0, -1, "NaN", "Infinity", {}, []):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    load({"enabled": True, "threshold_usd": value})
            for section in ({"enabled": False, "threshold_usd": 10},
                            {"refresh_interval_min": 0}, {"refresh_interval_min": True},
                            {"stale_sec": 60}, {"stale_sec": -1}):
                with self.subTest(section=section), self.assertRaises(ValueError):
                    load(section)


if __name__ == "__main__":
    unittest.main()
