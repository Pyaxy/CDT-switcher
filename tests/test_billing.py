"""账单集成离线回归：不访问阿里云或 Telegram。"""
from decimal import Decimal
from dataclasses import replace
from datetime import datetime
from html import unescape
from pathlib import Path
import sys
import threading
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import billing_query as b
import cdt_switcher as m
from tests.mock_e2e import make_world, ticks


class BillingTests(unittest.TestCase):
    def world(self):
        r, f, s, j, tg = make_world(instance_names={"A": "香港主节点", "B": "东京备用"})
        self.addCleanup(s.close)
        r.cfg.billing.enabled = True
        return r, f, s, j, tg

    def rows(self):
        return [
            b.BillRow("ECS", "PayAsYouGoBill", "USD", Decimal("1.234")),
            b.BillRow("ECS", "PayAsYouGoBill", "USD", Decimal("0.006")),
            b.BillRow("CDT", "PayAsYouGoBill", "USD", Decimal("2.00")),
            b.BillRow("ECS", "Refund", "USD", Decimal("0.10")),
            b.BillRow("ECS", "PayAsYouGoBill", "CNY", Decimal("7.00")),
        ]

    def command(self, r, text):
        r.handle_tg_command(text, 1)
        self.assertIsNotNone(r._bill_thread)
        r._bill_thread.join(timeout=2)
        self.assertFalse(r._bill_thread.is_alive())

    def test_all_accounts_totals_and_selected_products(self):
        r, f, s, j, tg = self.world()
        with patch.object(b, "query_overview", return_value=self.rows()) as query:
            self.command(r, "bill")
            report = tg.messages[-1]
            self.assertEqual([c.args[0].key for c in query.call_args_list], ["A", "B"])
            self.assertEqual(len({c.args[1] for c in query.call_args_list}), 1)
            self.assertIn("香港主节点 [A]", report)
            self.assertIn("东京备用 [B]", report)
            self.assertIn("USD 3.240", report)
            self.assertIn("退款 · 税前金额小计：USD 0.10", report)
            self.assertIn("CNY 7.00", report)
            self.assertNotIn("    ECS：", report)
            query.reset_mock()
            self.command(r, "bill B")
            report = tg.messages[-1]
            query.assert_not_called()
            self.assertIn("缓存数据", report)
            self.assertIn("    ECS：USD 1.240", report)
            self.assertIn("    CDT：USD 2.00", report)
            self.assertNotIn("香港主节点", report)

    def test_invalid_account_or_extra_arguments_do_not_query(self):
        r, f, s, j, tg = self.world()
        with patch.object(b, "query_overview") as query:
            for cmd in ("bill absent", "bill A B"):
                r.handle_tg_command(cmd, 1)
                self.assertIn("用法：/bill", tg.messages[-1])
                self.assertIn("香港主节点 [A]", tg.messages[-1])
            query.assert_not_called()
            self.assertIsNone(r._bill_thread)

    def test_daily_failure_keeps_status_and_other_account_cost(self):
        r, f, s, j, tg = self.world()
        ticks(r)
        before = j.load()
        for error in (b.BillingError("账单请求失败（Forbidden）"), RuntimeError("SECRET")):
            with patch.object(b, "query_overview", side_effect=[error, self.rows()]):
                r.daily_report()
            report = tg.messages[-1]
            self.assertIn("🕘 每日运行报告", report)
            self.assertIn("本月流量", report)
            self.assertIn("香港主节点 [A]\n  查询失败，当前金额未知", report)
            self.assertIn("东京备用 [B]", report)
            self.assertIn("USD 3.240", report)
            self.assertNotIn("SECRET", report)
            self.assertEqual(j.load(), before)

    def test_empty_bill_is_not_reported_as_zero(self):
        r, f, s, j, tg = self.world()
        with patch.object(b, "query_overview", return_value=[]):
            self.command(r, "bill A")
        self.assertIn("暂无账单条目", tg.messages[-1])
        self.assertNotIn("USD 0", tg.messages[-1])

    def test_blocked_bill_does_not_block_control_or_spawn_more_workers(self):
        r, f, s, j, tg = self.world()
        ticks(r)
        entered, release = threading.Event(), threading.Event()

        def blocked(*args):
            entered.set()
            if not release.wait(timeout=3):
                raise RuntimeError("test timeout")
            return []

        with patch.object(b, "query_overview", side_effect=blocked) as query:
            try:
                r.handle_tg_command("bill A", 1)
                self.assertTrue(entered.wait(timeout=1))
                worker = r._bill_thread
                r.handle_tg_command("bill A", 1)
                r.handle_tg_command("bill B", 1)
                self.assertIs(r._bill_thread, worker)
                self.assertIn("已有账单查询", tg.messages[-1])
                self.assertTrue(r._tick_lock.acquire(blocking=False))
                r._tick_lock.release()
                r.handle_tg_command("breaker confirm", 1)
                r.transition_tick()
                self.assertEqual(f["A"].status, "Stopped")
                self.assertEqual(query.call_count, 1)
            finally:
                release.set()
                r._bill_thread.join(timeout=2)
            self.assertEqual([call.args[0].key for call in query.call_args_list], ["A", "B"])
            self.assertFalse(r._bill_requests)
        with patch.object(b, "query_overview", return_value=[]):
            self.command(r, "bill B")

    def test_config_requires_explicit_boolean_opt_in(self):
        example = Path(__file__).resolve().parents[1] / "config.example.yaml"
        data = m.yaml.safe_load(example.read_text())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            for section, expected in ((None, False), ({}, False),
                                      ({"enabled": False}, False), ({"enabled": True}, True)):
                if section is None:
                    data.pop("billing", None)
                else:
                    data["billing"] = section
                path.write_text(m.yaml.safe_dump(data))
                self.assertEqual(m.load_config(str(path)).billing.enabled, expected)
            for invalid in ("false", "true", 1, None):
                data["billing"] = {"enabled": invalid}
                path.write_text(m.yaml.safe_dump(data))
                with self.assertRaisesRegex(ValueError, "billing.enabled"):
                    m.load_config(str(path))

    def test_disabled_has_no_bill_menu_help_query_or_daily_section(self):
        r, f, s, j, tg = self.world()
        r.cfg.billing.enabled = False
        with patch.object(b, "query_overview") as query:
            r.handle_tg_command("bill", 1)
            self.assertNotIn("/bill", tg.messages[-1])
            r.daily_report()
            self.assertNotIn("账单", tg.messages[-1])
            self.assertIn("每日运行报告", tg.messages[-1])
            self.assertIsNone(r._bill_thread)
            query.assert_not_called()
        client = m.TGClient(m.TGCfg(bot_token="fake", chat_ids=[1]))
        with patch.object(m.requests, "post") as post:
            for enabled in (True, False):
                client.register_commands(billing_enabled=enabled)
                commands = post.call_args.kwargs["json"]["commands"]
                self.assertEqual(any(c["command"] == "bill" for c in commands), enabled)

    def test_expired_manual_refresh_and_failed_refresh_marks_old_data(self):
        r, f, s, j, tg = self.world()
        with patch.object(b, "query_overview", return_value=self.rows()) as query:
            self.command(r, "bill A")
            first = r._bill_cache["A"]
            self.command(r, "bill A")
            self.assertEqual(query.call_count, 1)
            r._bill_cache["A"] = replace(first, attempted_mono=first.attempted_mono - 301)
            self.command(r, "bill A")
            self.assertEqual(query.call_count, 2)
        last_success = r._bill_cache["A"]
        r._bill_cache["A"] = replace(last_success, attempted_mono=last_success.attempted_mono - 301)
        with patch.object(b, "query_overview", side_effect=b.BillingError("权限不足")) as query:
            self.command(r, "bill A")
            report = tg.messages[-1]
            self.assertIn("查询失败，当前金额未知", report)
            self.assertIn("以下为旧数据", report)
            self.assertIn(last_success.queried_at, report)
            self.assertIn("USD 3.240", report)
            self.assertEqual(r._bill_cache["A"].queried_at, last_success.queried_at)
            self.command(r, "bill A")
            self.assertEqual(query.call_count, 1)
            self.assertIn("以下为旧数据", tg.messages[-1])
        with patch.object(b, "query_overview", return_value=[]):
            # 每日刷新绕过五分钟窗口，清除之前的失败标记。
            r.daily_report()
        self.assertNotIn("以下为旧数据", tg.messages[-1])
        self.assertNotIn("USD 3.240", tg.messages[-1])

    def test_month_change_drops_previous_month_and_failed_old_values(self):
        r, f, s, j, tg = self.world()
        with patch.object(m, "datetime") as clock, patch.object(b, "query_overview", return_value=self.rows()):
            clock.now.return_value = datetime(2026, 9, 30, 23, 58, tzinfo=b.BILL_TZ)
            self.command(r, "bill")
        self.assertEqual(set(r._bill_cache), {"A", "B"})
        with patch.object(m, "datetime") as clock, patch.object(b, "query_overview", side_effect=b.BillingError("超时")) as query:
            clock.now.return_value = datetime(2026, 10, 1, 0, 1, tzinfo=b.BILL_TZ)
            self.command(r, "bill A")
            self.assertEqual(query.call_args.args[1], "2026-10")
        self.assertEqual(set(r._bill_cache), {"A"})
        self.assertNotIn("USD 3.240", tg.messages[-1])
        self.assertNotIn("以下为旧数据", tg.messages[-1])
        self.assertIn("当前金额未知", tg.messages[-1])

    def test_unexpected_billing_failure_keeps_daily_status(self):
        r, f, s, j, tg = self.world()
        with patch.object(r, "_billing_report_text", side_effect=RuntimeError("SECRET")):
            r.daily_report()
        self.assertIn("每日运行报告", tg.messages[-1])
        self.assertIn("账单查询未完成", tg.messages[-1])
        self.assertNotIn("SECRET", tg.messages[-1])

    def test_daily_overlapping_manual_query_reuses_inflight_result(self):
        r, f, s, j, tg = self.world()
        entered, release, daily_requested = threading.Event(), threading.Event(), threading.Event()
        monotonic = m.time.monotonic
        reports = []

        def clock():
            value = monotonic()
            if threading.current_thread().name == "test-daily":
                daily_requested.set()
            return value

        def query(account, month):
            if account.key == "A":
                entered.set()
                if not release.wait(timeout=3):
                    raise RuntimeError("test timeout")
            return self.rows()

        daily = threading.Thread(
            target=lambda: reports.append(r._billing_report_text(force=True)), name="test-daily",
        )
        with patch.object(b, "query_overview", side_effect=query) as fetch, \
                patch.object(m.time, "monotonic", side_effect=clock):
            try:
                r.handle_tg_command("bill A", 1)
                self.assertTrue(entered.wait(timeout=1))
                daily.start()
                self.assertTrue(daily_requested.wait(timeout=1))
            finally:
                release.set()
                r._bill_thread.join(timeout=2)
                if daily.ident is not None:
                    daily.join(timeout=2)
            self.assertFalse(daily.is_alive())
            self.assertEqual([call.args[0].key for call in fetch.call_args_list], ["A", "B"])
            self.assertIn("USD 3.240", reports[0])

    def test_query_finishing_after_month_boundary_is_not_cached(self):
        r, f, s, j, tg = self.world()
        with patch.object(m, "datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 30, 23, 59, tzinfo=b.BILL_TZ)

            def finish_next_month(account, month):
                self.assertEqual(month, "2026-09")
                clock.now.return_value = datetime(2026, 10, 1, 0, 0, tzinfo=b.BILL_TZ)
                return self.rows()

            with patch.object(b, "query_overview", side_effect=finish_next_month):
                self.command(r, "bill A")
            self.assertFalse(r._bill_cache)
            with patch.object(b, "query_overview", return_value=[]) as query:
                self.command(r, "bill A")
                self.assertEqual(query.call_args.args[1], "2026-10")
            self.assertNotIn("USD 3.240", tg.messages[-1])

    def test_configured_name_wins_and_legacy_event_names_are_friendly(self):
        r, f, s, j, tg = self.world()
        r._instance_names["A"] = "云端旧名称"
        self.assertEqual(r._account_label("A"), "香港主节点 [A]")
        r._start_transition(to="B", from_="A")
        self.assertIn("原节点：香港主节点 [A]", tg.messages[-1])
        self.assertIn("目标节点：东京备用 [B]", tg.messages[-1])
        event = r._public_event_text("启动切换流水线 from=A to=B reason=manual")
        self.assertIn("香港主节点 [A]", event)
        self.assertIn("东京备用 [B]", event)

    def test_long_telegram_report_preserves_text_and_escapes_each_chunk(self):
        tg = m.TGClient(m.TGCfg(bot_token="fake", chat_ids=[1]))
        report = ("ECS <香港> & 💰" * 400) + "\n" + ("CDT 费用\n" * 300)
        with patch.object(tg, "_enqueue_send", return_value=True) as enqueue:
            tg.send(report)
        chunks = [unescape(call.args[1]) for call in enqueue.call_args_list]
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), report)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 4096 for chunk in chunks))
        self.assertTrue(all("<香港>" not in call.args[1] for call in enqueue.call_args_list))


if __name__ == "__main__":
    unittest.main()
