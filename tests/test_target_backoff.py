"""Regression tests for target-specific failure backoff and cleanup."""
import unittest
from unittest.mock import patch

import cdt_switcher as m
from tests.mock_e2e import make_world, ticks


class TargetBackoffTests(unittest.TestCase):
    def world(self):
        r, f, store, journal, tg = make_world(account_names=("A", "B", "C"))
        self.addCleanup(store.close)
        return r, f, store, journal, tg

    def test_nostock_rotates_and_success_clears_only_target(self):
        r, f, store, journal, _ = self.world()
        f['A'].nostock_times = 1
        ticks(r)
        self.assertEqual(journal.load()['duty_account'], 'B')
        self.assertIn('A', r._target_backoff)
        self.assertNotIn('B', r._target_backoff)

    def test_backoff_progression_and_all_cooling_does_not_break(self):
        r, f, store, journal, _ = self.world()
        with patch.object(m.time, 'time', return_value=10000):
            for duration in (900, 1800, 3600, 3600):
                r._backoff_target('A')
                self.assertEqual(r._target_backoff['A'][1], 10000 + duration)
            for a in ('B', 'C'):
                r._backoff_target(a)
            self.assertIsNone(r._select_target(store.get_runtimes()))
            r._start_transition(reason='tmax')
            self.assertIsNone(journal.load().get('transition'))
            self.assertFalse(journal.load().get('breaker_latched'))
            self.assertEqual(r._select_target(store.get_runtimes(), include_cooling=True), 'A')

    def test_manual_explicit_target_bypasses_cooldown(self):
        r, f, store, journal, _ = self.world()
        r._backoff_target('A')
        r._start_transition(to='A', reason='manual')
        ticks(r)
        self.assertEqual(journal.load()['duty_account'], 'A')
        self.assertNotIn('A', r._target_backoff)

    def test_timeout_after_ambiguous_start_cleans_up_before_retry(self):
        r, f, store, journal, _ = self.world()
        r._start_transition(to='A', reason='degraded_zero')
        state = journal.load()
        tr = state['transition']
        tr['step'] = m.STEP_T2
        tr['started_at'] = 0
        tr['meta']['t2_submitted'] = True
        journal.save(state)
        r.transition_tick()
        tr = journal.load()['transition']
        self.assertEqual(tr['step'], m.STEP_T4)
        self.assertEqual(tr['stop_list'], ['A'])
        self.assertIn('A', r._target_backoff)
        ticks(r)
        self.assertEqual(journal.load()['duty_account'], 'B')

    def test_probe_failure_cools_target_and_cleans_it(self):
        r, f, store, journal, _ = self.world()
        r._start_transition(to='A', reason='degraded_zero')
        state = journal.load()
        tr = state['transition']
        tr['step'] = m.STEP_T3
        tr['meta']['t3_started_at'] = 0
        f['A'].status = 'Running'
        journal.save(state)
        r.transition_tick()
        self.assertIn('A', r._target_backoff)
        self.assertEqual(journal.load()['transition']['stop_list'], ['A'])
        ticks(r)
        self.assertEqual(journal.load()['duty_account'], 'B')

    def test_over_quota_source_nostock_immediately_rotates(self):
        r, f, store, journal, _ = self.world()
        f['A'].status, f['A'].duty = 'Running', True
        ticks(r)
        store.add_traffic('A', r.cfg.th.traffic_threshold_gb)
        r.cfg.th.stop_retry_interval_sec = 300
        f['B'].nostock_times = 1
        r._start_transition(from_='A', reason='traffic')
        for _ in range(3):
            r.transition_tick()
        tr = journal.load()['transition']
        self.assertEqual(tr['to'], 'C')
        self.assertEqual(tr['meta']['reason'], 'traffic')
        self.assertEqual(f['A'].status, 'Running')

    def test_rollback_not_preempted_when_source_over_quota(self):
        r, f, store, journal, _ = self.world()
        f['A'].status, f['A'].duty = 'Running', True
        ticks(r)
        store.add_traffic('A', r.cfg.th.traffic_threshold_gb)
        r._start_transition(from_='A', to='B', reason='traffic')
        state = journal.load()
        state['transition']['step'] = m.STEP_T3
        state['transition']['meta']['t3_started_at'] = 0
        f['B'].status = 'Running'
        journal.save(state)
        r.transition_tick()
        self.assertEqual(journal.load()['transition']['meta']['reason'], 'rollback')
        r.transition_tick()
        tr = journal.load()['transition']
        self.assertEqual(tr['meta']['reason'], 'rollback')
        self.assertNotEqual(f['C'].status, 'Running')

    def test_legacy_started_and_starting_timeout_cleanup(self):
        for old_marker in (True, False):
            with self.subTest(old_marker=old_marker):
                r, f, store, journal, _ = self.world()
                r._start_transition(to='A', reason='degraded_zero')
                state = journal.load()
                tr = state['transition']
                tr['step'], tr['started_at'] = m.STEP_T2, 0
                if old_marker:
                    tr['meta']['t2_started'] = True
                else:
                    f['A'].status = 'Starting'
                journal.save(state)
                r.transition_tick()
                self.assertEqual(journal.load()['transition']['meta']['reason'], 'rollback')

    def test_running_self_switch_probe_failure_does_not_stop_source(self):
        r, f, store, journal, _ = self.world()
        f['A'].status, f['A'].duty = 'Running', True
        ticks(r)
        r._start_transition(from_='A', to='A', reason='manual')
        r.transition_tick()
        r.transition_tick()
        state = journal.load()
        state['transition']['meta']['t3_started_at'] = 0
        journal.save(state)
        r.transition_tick()
        self.assertEqual(f['A'].status, 'Running')
        self.assertEqual(f['A'].stop_calls, 0)


if __name__ == '__main__':
    unittest.main()
