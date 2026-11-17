import copy,json,tempfile,unittest
from pathlib import Path
from execution import PaperExecutor,validate_execution
from execution_runtime import confirmation
from test_execution import config,books,signal
from replay import Recorder,replay

class ConservativeTests(unittest.TestCase):
    def make(self,**kw):
        e=PaperExecutor(config(**kw),':memory:');self.addCleanup(e.db.close);return e
    def historical(self,e,n=2,pnl=-.2):
        for i in range(n):e.s['pairs'].append(dict(id=str(i),symbol='BTC',buy='edgex',sell='arcus',state='CLOSED',drill=False,created=900+i,closed=900+i,net_pnl=pnl))
    def test_dollar_threshold_after_resize(self):
        e=self.make(min_expected_profit_usd=.1)
        self.assertIsNone(e.submit(signal(),books(1000),1000));self.assertFalse(e.s['orders'])
        self.assertIsNone(confirmation(signal(),books(1000),1000,e.c))
    def test_route_pauses_and_does_not_extend_without_new_loss(self):
        e=self.make();self.historical(e);e.update_route_pauses(1000)
        until=e.s['route_pauses']['BTC:edgex:arcus']['until'];e.update_route_pauses(1001)
        self.assertEqual(until,e.s['route_pauses']['BTC:edgex:arcus']['until'])
        self.assertIsNone(e.submit(signal(),books(1002),1002))
        self.assertTrue(e.submit(signal(),books(until+1),until+1))
    def test_drills_excluded_and_direction_specific(self):
        e=self.make();self.historical(e)
        for p in e.s['pairs']:p['drill']=True
        e.update_route_pauses(1000);self.assertFalse(e.s['route_pauses'])
        for p in e.s['pairs']:p['drill']=False;p['buy'],p['sell']=p['sell'],p['buy']
        e.update_route_pauses(1000);self.assertTrue(e.submit(signal(),books(1000),1000))
    def test_cancel_both_before_edge_disappears(self):
        e=self.make();self.assertTrue(e.submit(signal(),books(1000),1000))
        r=e.step(books(1002,b=(100,101)),1002)
        self.assertFalse(r['positions']);self.assertTrue(all(o['filled']==0 for o in e.s['orders']))
        self.assertEqual(r['trades'][0]['reason'],'entry edge or depth vanished before fills')
    def test_profit_exit_covers_extra_exit_slippage(self):
        e=self.make(break_even_after_seconds=1)
        e.submit(signal(),books(1000),1000);e.step(books(1002),1002)
        r=e.step(books(1004,a=(100.8,100.9),b=(101,101.1)),1004)
        self.assertEqual(r['positions'][0]['reason'],'break_even_exit')
        r=e.step(books(1006,a=(100.8,100.9),b=(101,101.1)),1006)
        self.assertGreater(r['trades'][0]['net_pnl'],0)
    def test_recorder_replay_matches_live_paper_orders(self):
        with tempfile.TemporaryDirectory() as d:
            e=self.make();path=str(Path(d)/'replay.sqlite');rec=Recorder(path,e)
            s=signal();s.update(queued_at=1000,key='BTC:edgex:arcus')
            w=dict(signal=s,check=dict(since=970,checked=999))
            rec.record(books(1000),[w],1000);e.step(books(1000),1000);e.submit(s,books(1000),1000)
            rec.record(books(1002),[],1002);e.step(books(1002),1002);rec.close()
            result=replay(path)
            self.assertEqual(result['frames'],2);self.assertEqual(len(result['remaining_positions']),1)
            self.assertAlmostEqual(result['remaining_positions'][0]['net_pnl'],e.snapshot(books(1002),1002)['positions'][0]['net_pnl'])
    def test_invalid_route_configuration(self):
        for c in [dict(route_window=1,route_min_trades=3),dict(route_window=2.5)]:
            with self.assertRaises(ValueError):validate_execution(c)

if __name__=='__main__':unittest.main()
