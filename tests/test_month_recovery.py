"""Month rollover is idempotent and preserves current-month protection."""
import unittest
from unittest.mock import patch

import cdt_switcher as m
from tests.mock_e2e import make_world, ticks


class MonthRecoveryTests(unittest.TestCase):
    def setUp(self):
        ready = patch.object(m, 'month_recovery_ready', return_value=True)
        ready.start()
        self.addCleanup(ready.stop)

    def world(self):
        r, f, store, journal, tg = make_world()
        self.addCleanup(store.close)
        return r, f, store, journal, tg

    def old_breaker(self, r, journal):
        r._start_transition(breaker=True, reason='no_eligible')
        ticks(r)
        state = journal.load()
        state['breaker_month'] = '2000-01'
        state['month_processed'] = '2000-01'
        journal.save(state)

    def test_catchup_preserves_runtime_and_is_idempotent(self):
        r, f, store, journal, _ = self.world()
        self.old_breaker(r, journal)
        store.set_runtime('A', 1234)
        r._reconcile_month()
        self.assertEqual(store.get_runtimes()['A'], 1234)
        self.assertEqual(journal.load()['month_processed'], m.current_month())
        tr = journal.load()['transition']
        self.assertEqual(tr['meta']['reason'], 'month_reset')
        r._reconcile_month()
        self.assertEqual(journal.load()['transition'], tr)
        self.assertEqual(store.get_runtimes()['A'], 1234)

    def test_delayed_usage_is_retried_while_latched(self):
        r, f, store, journal, _ = self.world()
        self.old_breaker(r, journal)
        for cli in f.values():
            cli.traffic_gb = r.cfg.th.traffic_threshold_gb
        r._reconcile_month()
        self.assertTrue(journal.load()['breaker_latched'])
        self.assertIsNone(journal.load()['transition'])
        for cli in f.values():
            cli.traffic_gb = 0.3
        r._cooldown['_month_refresh'] = 0
        r._reconcile_month()
        self.assertEqual(journal.load()['transition']['meta']['reason'], 'month_reset')
        self.assertEqual(store.latest_traffic()['A'], 0.3)

    def test_current_manual_breaker_and_legacy_unknown_remain_latched(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                r, f, store, journal, _ = self.world()
                r._start_transition(breaker=True, reason='manual_breaker')
                ticks(r)
                state = journal.load()
                if legacy:
                    state.pop('breaker_month', None)
                    state.pop('month_processed', None)
                else:
                    state['month_processed'] = '2000-01'
                journal.save(state)
                r._reconcile_month()
                state = journal.load()
                self.assertTrue(state['breaker_latched'])
                self.assertIsNone(state['transition'])

    def test_prior_month_inflight_breaker_finishes_before_recovery(self):
        r, f, store, journal, _ = self.world()
        r._start_transition(breaker=True, reason='no_eligible')
        state = journal.load()
        state['breaker_month'] = '2000-01'
        state['month_processed'] = '2000-01'
        journal.save(state)
        r._reconcile_month()
        self.assertTrue(journal.load()['transition']['breaker'])
        ticks(r)
        self.assertEqual(journal.load()['global_state'], m.GS_STEADY)

    def test_before_0005_and_month_intent_do_not_release_current_breaker(self):
        r, f, store, journal, _ = self.world()
        self.old_breaker(r, journal)
        with patch.object(m, 'month_recovery_ready', return_value=False):
            r.month_reset()
            r.transition_tick()
            self.assertTrue(journal.load()['breaker_latched'])
            self.assertIsNone(journal.load()['transition'])
        r._reconcile_month()
        self.assertEqual(journal.load()['transition']['meta']['reason'], 'month_reset')

    def test_restart_retries_prior_breaker(self):
        r, f, store, journal, tg = self.world()
        self.old_breaker(r, journal)
        r2 = m.Rotator(r.cfg, store, journal, tg)
        r2.clients = f
        r2._probe_service = lambda a: True
        ticks(r2)
        self.assertEqual(journal.load()['global_state'], m.GS_STEADY)


if __name__ == '__main__':
    unittest.main()
