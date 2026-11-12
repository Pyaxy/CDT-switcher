import unittest

import cdt_switcher as m
from tests.mock_e2e import make_world, ticks, steady_duty


class SpotWarningTests(unittest.TestCase):
    def world(self, names=('A', 'B', 'C')):
        r, f, store, sj, tg = make_world(account_names=names)
        self.addCleanup(store.close)
        ticks(r)
        return r, f, sj

    def test_stale_stopped_standby_warning_ignored(self):
        r, f, sj = self.world()
        duty = steady_duty(sj)
        standby = next(a for a in f if a != duty)
        r.submit_intent('SPOT_WARN', account=standby)
        ticks(r, 3)
        self.assertEqual(steady_duty(sj), duty)
        self.assertIsNone(sj.load()['transition'])

    def test_source_warning_prefers_other_even_when_source_lowest_runtime(self):
        r, f, sj = self.world()
        duty = steady_duty(sj)
        f[duty].lock_recycling = True
        ticks(r)
        self.assertNotEqual(steady_duty(sj), duty)

    def test_no_alternative_keeps_source_running(self):
        r, f, sj = self.world(('A',))
        duty = steady_duty(sj)
        f[duty].lock_recycling = True
        ticks(r, 4)
        self.assertEqual(steady_duty(sj), duty)
        self.assertEqual(f[duty].status, 'Running')
        self.assertEqual(f[duty].stop_calls, 0)

    def test_warned_inflight_target_stops_before_reselection(self):
        r, f, sj = self.world()
        source = steady_duty(sj)
        target, alternate = [a for a in f if a != source]
        r.submit_intent('SWITCH', account=target)
        for _ in range(8):
            r.tick()
            if f[target].status == 'Running':
                break
        self.assertEqual(f[target].status, 'Running')
        r.submit_intent('SPOT_WARN', account=target)
        r.tick()
        self.assertEqual(f[target].status, 'Stopped')
        self.assertEqual(sj.load()['transition']['meta']['reason'], 'rollback')
        ticks(r, 30)
        self.assertEqual(steady_duty(sj), alternate)
        self.assertEqual(f[source].status, 'Stopped')
        self.assertNotIn(target, r._target_backoff)

    def test_restart_during_cleanup_preserves_reselection(self):
        r, f, sj = self.world()
        source = steady_duty(sj)
        target, alternate = [a for a in f if a != source]
        r.submit_intent('SWITCH', account=target)
        for _ in range(8):
            r.tick()
            if f[target].status == 'Running':
                break
        r.submit_intent('SPOT_WARN', account=target)
        r.tick()
        self.assertEqual(sj.load()['spot_reselect']['target'], target)
        restarted = m.Rotator(r.cfg, r.store, sj, r.tg)
        restarted.clients = f
        restarted._probe_service = lambda a: True
        ticks(restarted, 40)
        self.assertEqual(steady_duty(sj), alternate)
        self.assertNotIn('spot_reselect', sj.load())

    def test_manual_breaker_supersedes_persisted_reselection(self):
        r, f, sj = self.world()
        source = steady_duty(sj)
        target = next(a for a in f if a != source)
        state = sj.load()
        state['spot_reselect'] = {'source': source, 'target': target}
        sj.save(state)
        r.submit_intent('BREAKER')
        ticks(r, 15)
        self.assertEqual(sj.load()['global_state'], m.GS_BREAKER)
        self.assertNotIn('spot_reselect', sj.load())

    def test_changed_duty_discards_stale_reselection(self):
        r, f, sj = self.world()
        source = steady_duty(sj)
        old_source, target = [a for a in f if a != source]
        state = sj.load()
        state['spot_reselect'] = {'source': old_source, 'target': target}
        sj.save(state)
        ticks(r, 2)
        self.assertEqual(steady_duty(sj), source)
        self.assertNotIn('spot_reselect', sj.load())
        self.assertIsNone(sj.load()['transition'])

    def test_warned_stopped_target_is_stale_and_never_stopped(self):
        r, f, sj = self.world()
        source = steady_duty(sj)
        target = next(a for a in f if a != source)
        f[target].lock_recycling = True
        r.submit_intent('SWITCH', account=target)
        r.tick()
        self.assertEqual(sj.load()['transition']['step'], m.STEP_T1)
        r.submit_intent('SPOT_WARN', account=target)
        r.tick()
        self.assertEqual(f[target].stop_calls, 0)
        self.assertNotEqual(sj.load()['transition']['meta']['reason'], 'rollback')

    def test_nostock_tries_other_candidate_not_warned_source(self):
        r, f, sj = self.world()
        source = steady_duty(sj)
        target, alternate = [a for a in f if a != source]
        f[target].nostock_times = 1
        f[source].lock_recycling = True
        ticks(r, 35)
        self.assertEqual(steady_duty(sj), alternate)
        self.assertEqual(f[source].status, 'Stopped')


if __name__ == '__main__':
    unittest.main()
