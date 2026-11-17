import copy,tempfile,unittest
from pathlib import Path
from execution import PaperExecutor,validate_execution
from test_arb import CONFIG

def config(**overrides):
    c=copy.deepcopy(CONFIG);c.update(venues=['edgex','arcus'],symbols=['BTC'],take_profit_usd=100,stop_loss_usd=100,max_hold_seconds=1000)
    c['execution']={'enabled':True,**overrides};return c

def books(t,a=(99,100),b=(101,102),size=10):
    return {('edgex','BTC'):dict(bids=[(a[0],size)],asks=[(a[1],size)],time=t,received=t),('arcus','BTC'):dict(bids=[(b[0],size)],asks=[(b[1],size)],time=t,received=t)}

def signal():return dict(symbol='BTC',buy='edgex',sell='arcus',quantity=.1,baseline=0,cost_bps=22)

class ExecutionTests(unittest.TestCase):
    def make(self,fault=None,**kw):
        e=PaperExecutor(config(**kw),':memory:',fault);self.addCleanup(e.db.close);return e
    def test_complete_cycle_and_cash_reconciliation(self):
        e=self.make();pid=e.submit(signal(),books(1000),1000,drill=True)
        e.step(books(1002),1002);self.assertEqual(e.s['pairs'][0]['state'],'OPEN')
        e.request_close(pid);e.step(books(1003),1003);result=e.step(books(1005),1005)
        self.assertFalse(result['positions']);self.assertEqual(result['trades'][0]['state'],'CLOSED')
        self.assertEqual(len(result['orders']),4)
        self.assertTrue(all(abs(l['quantity'])<1e-12 for l in e.s['ledger'].values()))
        self.assertAlmostEqual(result['realized_pnl'],sum(o['realized']-o['fee'] for o in e.s['orders'])-sum(a['funding_reserve'] for a in e.s['accounts'].values()))
        self.assertLess(result['realized_pnl'],0)
    def test_single_leg_rejection_unwinds_filled_leg(self):
        e=self.make('reject_short');e.submit(signal(),books(1000),1000,drill=True)
        e.step(books(1002),1002);self.assertEqual(e.s['pairs'][0]['state'],'HEDGING')
        result=e.step(books(1004),1004)
        self.assertFalse(result['positions']);self.assertEqual(result['trades'][0]['state'],'ABORTED')
        self.assertTrue(any(o['purpose']=='TRIM' and o['reduce_only'] for o in e.s['orders']))
    def test_partial_fill_trims_excess(self):
        e=self.make('partial_short');e.submit(signal(),books(1000),1000,drill=True)
        e.step(books(1002),1002);result=e.step(books(1004),1004)
        p=result['positions'][0];self.assertEqual(p['state'],'OPEN');self.assertAlmostEqual(p['quantity'],.05)
        self.assertAlmostEqual(p['unhedged_quantity'],0)
    def test_unknown_result_survives_restart_without_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            path=str(Path(d)/'state.db');e=PaperExecutor(config(),path,'unknown_short')
            e.submit(signal(),books(1000),1000,drill=True);e.step(books(1002),1002)
            self.assertTrue(any(o['status']=='UNKNOWN' for o in e.s['orders']));e.db.close()
            e=PaperExecutor(config(),path);result=e.step(books(1005),1005)
            self.assertEqual(len(e.s['orders']),2);self.assertEqual(result['positions'][0]['state'],'OPEN');e.db.close()
    def test_no_fill_on_submission_snapshot(self):
        e=self.make();e.submit(signal(),books(1000),1000,drill=True);e.step(books(1000),1002)
        self.assertTrue(all(o['filled']==0 for o in e.s['orders']))
    def test_stale_quotes_do_not_fill_then_timeout(self):
        e=self.make();e.submit(signal(),books(1000),1000,drill=True);e.step(books(980),1002)
        result=e.step({},1031);self.assertFalse(result['positions']);self.assertTrue(all(o['status']=='TIMED_OUT' for o in e.s['orders']))
    def test_virtual_margin_blocks_entry(self):
        e=self.make(initial_balance_per_venue=1);self.assertIsNone(e.submit(signal(),books(1000),1000,drill=True));self.assertFalse(e.s['orders'])
    def test_requote_edge_disappears(self):
        e=self.make();self.assertIsNone(e.submit(signal(),books(1000,b=(99,100)),1000));self.assertFalse(e.s['orders'])
    def test_no_duplicate_pair(self):
        e=self.make();self.assertTrue(e.submit(signal(),books(1000),1000,drill=True));self.assertIsNone(e.submit(signal(),books(1000),1000,drill=True));self.assertEqual(len(e.s['orders']),2)
    def test_limit_prevents_adverse_fill(self):
        e=self.make();e.submit(signal(),books(1000),1000,drill=True);result=e.step(books(1002,a=(110,111),b=(90,91)),1002)
        self.assertFalse(result['positions']);self.assertTrue(all(o['filled']==0 for o in e.s['orders']))
    def test_daily_loss_halts_and_closes(self):
        e=self.make(max_daily_loss_usd=.01);e.submit(signal(),books(1000),1000,drill=True);result=e.step(books(1002),1002)
        self.assertEqual(result['halt_reason'],'daily loss limit');self.assertEqual(result['positions'][0]['state'],'CLOSING')
    def test_tampered_ledger_halts(self):
        e=self.make();e.ledger('edgex','BTC')['quantity']=1;self.assertFalse(e.reconcile(1000));self.assertEqual(e.s['halt_reason'],'ledger reconciliation mismatch')
    def test_closed_trade_not_valued_against_new_pair(self):
        e=self.make();pid=e.submit(signal(),books(1000),1000,drill=True);e.step(books(1002),1002);e.request_close(pid);e.step(books(1003),1003)
        first=e.step(books(1005),1005)['trades'][0]['net_pnl']
        e.submit(signal(),books(1100),1100,drill=True);e.step(books(1102),1102)
        self.assertEqual(e.snapshot(books(1103,a=(90,91)),1103)['trades'][0]['net_pnl'],first)
    def test_many_partial_closes_continue_and_allow_next_trade(self):
        e=self.make();pid=e.submit(signal(),books(1000),1000,drill=True)
        e.step(books(1002),1002);e.request_close(pid);e.step(books(1003),1003)
        for t in range(1005,1030,2):
            result=e.step(books(t,size=.04),t)
            self.assertIsNone(result['halt_reason'])
            if not result['positions']:break
        self.assertFalse(result['positions'])
        self.assertGreater(e.s['pairs'][0]['recovery_attempts'],3)
        self.assertTrue(e.reconcile(t))
        self.assertEqual(sum(x['kind']=='PAPER_CLOSE_RETRYING' for x in e.s['events']),1)
        self.assertTrue(e.submit(signal(),books(1200),1200,drill=True))

    def test_zero_fill_retries_do_not_halt_or_fake_close(self):
        e=self.make();pid=e.submit(signal(),books(1000),1000,drill=True)
        e.step(books(1002),1002);e.request_close(pid);e.step(books(1003),1003)
        for t in range(1005,1017,2):
            # Both close sides move beyond their previous limits each cycle.
            k=(t-1003)//2
            result=e.step(books(t,a=(99-k,100-k),b=(101+k,102+k)),t)
            self.assertIsNone(result['halt_reason'])
            self.assertTrue(result['positions'])
        self.assertTrue(all(o['filled']==0 for o in e.s['orders'] if o['purpose']=='CLOSE'))
        result=e.step(books(1020,a=(93,94),b=(107,108)),1020)
        self.assertFalse(result['positions'])
        self.assertTrue(e.reconcile(1020))

    def test_legacy_retry_halt_clears_after_restart_without_resetting_account(self):
        with tempfile.TemporaryDirectory() as d:
            path=str(Path(d)/'state.db');e=PaperExecutor(config(),path)
            pid=e.submit(signal(),books(1000),1000,drill=True);e.step(books(1002),1002)
            e.request_close(pid);e.step(books(1003),1003)
            e.s['halt_reason']='close retries: new entries halted';e.save()
            cash=copy.deepcopy(e.s['accounts']);e.db.close()
            e=PaperExecutor(config(),path)
            try:
                self.assertEqual(e.s['accounts'],cash)
                result=e.step(books(1005),1005)
                self.assertIsNone(result['halt_reason']);self.assertFalse(result['positions'])
                self.assertEqual(len(e.s['orders']),4)
                self.assertTrue(any(x['kind']=='PAPER_RETRY_HALT_CLEARED' for x in e.s['events']))
            finally:e.db.close()

    def test_retry_migration_never_clears_ledger_or_other_risk_halts(self):
        e=self.make();e.s['halt_reason']='close retries: new entries halted'
        e.ledger('edgex','BTC')['quantity']=1
        self.assertEqual(e.step({},1000)['halt_reason'],'ledger reconciliation mismatch')
        for reason in ('daily loss limit','simulated margin emergency','execution worker failed'):
            e=self.make();e.s['halt_reason']=reason
            self.assertEqual(e.step({},1000)['halt_reason'],reason)

    def test_invalid_execution_config(self):
        for c in [{'depth_participation':2},{'enabled':'yes'},{'leverage':float('nan')},{'order_timeout_seconds':.5},{'live':True}]:
            with self.assertRaises(ValueError):validate_execution(c)

if __name__=='__main__':unittest.main()
