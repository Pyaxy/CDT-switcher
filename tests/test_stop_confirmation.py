import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cdt_switcher as m


class StopConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.r = m.Rotator.__new__(m.Rotator)
        self.r.cfg = SimpleNamespace(th=SimpleNamespace(stop_timeout_sec=60,
            stop_retry_interval_sec=10, stop_alarm_interval_sec=30), accounts={'A': {}, 'B': {}})
        self.r.clients = {'A': Mock(), 'B': Mock()}
        self.r._freeze_unknown = Mock(return_value=False)
        self.r._notify = Mock()
        self.r._account_label = lambda a: a
        self.obs = {a: m.InstanceObs(exists=True, obs=m.OBS_RUNNING) for a in ('A', 'B')}
        self.tr = {'step': m.STEP_T4, 'started_at': 0, 'to': None,
                   'stop_list': ['A', 'B'], 'meta': {}}
        self.state = {}

    def gate(self, now):
        with patch.object(m.time, 'time', return_value=now):
            self.r._gate_t4(self.tr, self.obs, self.state)

    def test_first_alarm_retry_and_repeated_alarm(self):
        self.tr['meta']['t4_started_at'] = 100
        for now in (100, 159):
            self.gate(now)
        self.r._notify.assert_not_called()
        self.gate(160)
        self.r._notify.assert_called_once()
        self.gate(189)
        self.r._notify.assert_called_once()
        self.gate(190)
        self.assertEqual(self.r._notify.call_count, 2)
        self.assertGreaterEqual(self.r.clients['A'].stop_instance_stopcharging.call_count, 3)

    def test_legacy_cursor_starts_fresh_and_reentry_resets(self):
        self.tr['meta']['last_alarm'] = 1
        self.gate(100)
        self.assertEqual(self.tr['meta']['t4_started_at'], 100)
        self.gate(159)
        self.r._notify.assert_not_called()
        self.gate(160)
        with patch.object(m.time, 'time', return_value=200):
            self.assertTrue(self.r._return_to_stop_confirmation(self.tr, self.obs))
        self.assertEqual(self.tr['meta']['t4_started_at'], 200)
        self.gate(259)
        self.r._notify.assert_called_once()
        self.gate(260)
        self.assertEqual(self.r._notify.call_count, 2)

    def test_typed_tag_failure_does_not_block_other_accounts(self):
        self.r.cfg.th.probe_interval_sec = 10
        self.r.cfg.th.alert_repeat_interval_sec = 30
        self.r._notify_throttled = Mock()
        self.r._observe_one = Mock(return_value=m.InstanceObs(
            exists=True, obs=m.OBS_RUNNING, duty_on=False))
        self.r.clients['A'].set_duty.side_effect = m.ArrearsError('error')
        for observed in self.obs.values():
            observed.obs = m.OBS_STOPPED_SC
            observed.duty_on = True
        self.tr['step'] = m.STEP_T5
        with patch.object(m.time, 'time', return_value=100):
            self.r._gate_t5(self.tr, self.obs, self.state)
        self.r.clients['B'].set_duty.assert_called_once_with(False)
        self.assertEqual(self.tr['meta']['tag_retry_at']['A'], 110)

    def test_typed_stop_failure_does_not_block_other_accounts(self):
        for error in (m.ArrearsError, m.NoStockError, m.CloudAPIError):
            with self.subTest(error=error):
                self.tr['meta'] = {}
                self.r.clients['A'].stop_instance_stopcharging.side_effect = error('error')
                self.r.clients['B'].stop_instance_stopcharging.reset_mock()
                self.gate(100)
                self.r.clients['B'].stop_instance_stopcharging.assert_called_once()
                self.assertEqual(self.tr['meta']['stop_retry_at']['A'], 110)


if __name__ == '__main__':
    unittest.main()
