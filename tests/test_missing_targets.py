"""Automatic selection must not start an observed reclaimed instance."""
import unittest
from unittest.mock import patch

import cdt_switcher as m
from tests.mock_e2e import make_world, ticks


class MissingTargetsTests(unittest.TestCase):
    def world(self):
        r, f, store, journal, tg = make_world(account_names=('A', 'B', 'C'))
        self.addCleanup(store.close)
        return r, f, store, journal, tg

    def test_zero_and_quota_fallback_skip_absent(self):
        for quota in (False, True):
            with self.subTest(quota=quota):
                r, f, store, journal, _ = self.world()
                f['B'].exists = False
                if quota:
                    f['A'].status, f['A'].duty = 'Running', True
                    ticks(r)
                    store.add_traffic('A', r.cfg.th.traffic_threshold_gb)
                else:
                    f['A'].exists = False
                with patch.object(f['B'], 'start_instance', wraps=f['B'].start_instance) as start:
                    ticks(r)
                    start.assert_not_called()
                self.assertEqual(journal.load()['duty_account'], 'C')
                self.assertEqual(f['B'].status, 'Stopped')

    def test_month_recovery_skips_absent_priority_candidate(self):
        r, f, store, journal, _ = self.world()
        f['A'].exists = False
        r._start_transition(breaker=True, reason='no_eligible')
        with patch.object(m, 'month_recovery_ready', return_value=True):
            ticks(r)
            state = journal.load()
            state['breaker_month'] = '2000-01'
            journal.save(state)
            ticks(r)
        self.assertEqual(journal.load()['duty_account'], 'B')

    def test_nostock_does_not_rotate_to_absent_node(self):
        r, f, store, journal, _ = self.world()
        f['A'].nostock_times = 1
        f['B'].exists = False
        ticks(r)
        self.assertEqual(journal.load()['duty_account'], 'C')

    def test_disappears_in_t1_or_t2_and_manual_remains_explicit(self):
        for step in (m.STEP_T1, m.STEP_T2, m.STEP_T3, m.STEP_T4, m.STEP_T5):
            for explicit in (False, True):
                with self.subTest(step=step, explicit=explicit):
                    r, f, store, journal, _ = self.world()
                    r.tick()
                    r._start_transition(to='A', reason='manual' if explicit else 'traffic')
                    state = journal.load()
                    state['transition']['step'] = step
                    journal.save(state)
                    f['A'].exists = False
                    with patch.object(f['A'], 'start_instance', wraps=f['A'].start_instance) as start:
                        r.transition_tick()
                        start.assert_not_called()
                    tr = journal.load().get('transition')
                    if explicit:
                        self.assertIsNone(tr)
                    else:
                        self.assertEqual(tr['to'], 'B')

    def test_missing_late_target_does_not_retain_stopped_or_warned_source(self):
        for stopped, warned in ((True, False), (False, True)):
            with self.subTest(stopped=stopped, warned=warned):
                r, f, store, journal, _ = self.world()
                f['A'].status, f['A'].duty = 'Running', True
                ticks(r)
                r._start_transition(from_='A', to='B', reason='spot_warn')
                state = journal.load()
                state['transition']['step'] = m.STEP_T4
                state['transition']['stop_list'] = ['A']
                journal.save(state)
                f['A'].status = 'Stopped' if stopped else 'Running'
                f['A'].lock_recycling = warned
                f['B'].exists = False
                r.transition_tick()
                state = journal.load()
                self.assertEqual(state['transition']['to'], 'C')
                self.assertEqual(state['transition']['from'], None if stopped else 'A')
                self.assertIn('A', state['transition']['stop_list'])
                if stopped:
                    self.assertIsNone(state['duty_account'])

    def test_reappearing_instance_becomes_eligible(self):
        r, f, store, journal, _ = self.world()
        f['A'].exists = False
        r._observe_all()
        self.assertEqual(r._select_target(store.get_runtimes()), 'B')
        f['A'].exists = True
        r._observe_all()
        self.assertEqual(r._select_target(store.get_runtimes()), 'A')


if __name__ == '__main__':
    unittest.main()
