import unittest
from unittest.mock import Mock

from tests.mock_e2e import make_world, ticks


class AdoptRunningTests(unittest.TestCase):
    def world(self):
        rot, clients, store, journal, tg = make_world(account_names=("A", "B", "C"))
        self.addCleanup(store.close)
        rot.cfg.scheduling.strategy = "priority"
        rot.cfg.accounts["A"].priority = 0
        for client in clients.values():
            client.start_instance = Mock(wraps=client.start_instance)
        return rot, clients, store, journal

    def test_adopt_only_running_instance_and_confirm_tags(self):
        for initial in ("zero", "stale_duty"):
            with self.subTest(initial=initial):
                rot, clients, _, journal = self.world()
                if initial == "stale_duty":
                    clients["A"].status = "Running"
                    clients["A"].duty = True
                rot.tick()
                clients["A"].status = "Stopped"
                clients["C"].status = "Running"
                rot._probe_service = Mock(return_value=True)
                ticks(rot)
                self.assertEqual(journal.load()["duty_account"], "C")
                self.assertTrue(clients["C"].duty)
                self.assertFalse(clients["A"].duty)
                rot._probe_service.assert_called_with("C")
                for client in clients.values():
                    client.start_instance.assert_not_called()
                    self.assertEqual(client.stop_calls, 0)

    def test_over_quota_online_instance_hands_over(self):
        rot, clients, store, journal = self.world()
        rot.tick()
        clients["C"].status = "Running"
        store.add_traffic("C", rot.cfg.th.traffic_threshold_gb)
        ticks(rot)
        self.assertEqual(journal.load()["duty_account"], "A")
        self.assertEqual(clients["C"].status, "Stopped")
        clients["C"].start_instance.assert_not_called()

    def test_service_failure_hands_over_even_if_source_is_first_priority(self):
        rot, clients, _, journal = self.world()
        rot.cfg.th.auto_switch_on_service_down = True
        clients["A"].status = "Running"
        rot._probe_service = lambda account: account != "A"
        ticks(rot)
        self.assertEqual(journal.load()["duty_account"], "B")
        self.assertEqual(clients["A"].status, "Stopped")
