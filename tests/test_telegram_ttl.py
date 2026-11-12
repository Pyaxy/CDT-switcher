import queue
import unittest
from unittest.mock import Mock, patch

import cdt_switcher as cdt


class TelegramControlTTLTests(unittest.TestCase):
    def setUp(self):
        self.client = cdt.TGClient.__new__(cdt.TGClient)
        self.client._chat_ids = {1}
        self.client.send = Mock()
        self.callback = Mock()
        self.clock = patch.object(cdt.time, 'time', return_value=1000)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def update(self, text, date=1000):
        self.client._handle_update({'message': {'chat': {'id': 1}, 'text': text, 'date': date}}, self.callback)

    def test_controls_require_valid_recent_dates(self):
        for date in (None, '1000', True, 879, 1001):
            for command in ('/switch A', '/resume', '/breaker confirm'):
                self.update(command, date)
        self.callback.assert_not_called()
        self.assertEqual(self.client.send.call_count, 15)
        self.update('/switch A', 880)
        self.callback.assert_called_once_with('switch A', 1, 880)

    def test_read_only_commands_do_not_require_date(self):
        self.update('/check', None)
        self.callback.assert_called_once_with('check', 1)

    def test_pending_semantic_dedup_and_expiry(self):
        rot = cdt.Rotator.__new__(cdt.Rotator)
        rot.intents = queue.Queue()
        rot._pending = []
        rot.intents.put({'type': 'SWITCH', 'account': 'A', 'tg_date': 900})
        rot.intents.put({'type': 'SWITCH', 'account': 'A', 'tg_date': 990})
        rot._drain_intents()
        self.assertEqual(rot._pending, [{'type': 'SWITCH', 'account': 'A', 'tg_date': 990}])
        rot._freeze_unknown = Mock(return_value=False)
        rot.sj = Mock()
        rot._start_transition = Mock()
        rot.tg = Mock()
        with patch.object(cdt.time, 'time', return_value=1111):
            rot._consume_intents({})
        rot._start_transition.assert_not_called()
        rot.tg.send.assert_called_once()
        self.assertEqual(rot._pending, [])

    def test_fresh_switch_executes_and_expired_resume_does_not(self):
        rot = cdt.Rotator.__new__(cdt.Rotator)
        rot._freeze_unknown = Mock(return_value=False)
        rot.sj = Mock()
        rot.sj.load.return_value = {'global_state': cdt.GS_STEADY}
        rot._start_transition = Mock()
        rot.tg = Mock()
        rot._pending = [{'type': 'SWITCH', 'account': 'A', 'tg_date': 900},
                        {'type': 'RESUME', 'tg_date': 879}]
        rot._consume_intents({})
        rot._start_transition.assert_called_once_with(to='A', reason='manual')
        rot._start_transition.reset_mock()
        rot._consume_intents({})
        rot._start_transition.assert_not_called()
        rot.tg.send.assert_called_once()

    def test_internal_duplicate_does_not_expire(self):
        rot = cdt.Rotator.__new__(cdt.Rotator)
        rot.intents = queue.Queue()
        rot._pending = []
        rot.intents.put({'type': 'RESUME', 'tg_date': 900})
        rot.intents.put({'type': 'RESUME'})
        rot.intents.put({'type': 'RESUME', 'tg_date': 990})
        rot._drain_intents()
        self.assertEqual(rot._pending, [{'type': 'RESUME'}])


if __name__ == '__main__':
    unittest.main()
