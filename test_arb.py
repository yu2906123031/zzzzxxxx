import copy
import json
import tempfile
import unittest
from pathlib import Path
from arb import Engine, healthy, liquidation, normalize, validate, vwap

CONFIG=json.loads(Path('config.json').read_text(encoding='utf-8-sig'))
CONFIG['symbols']=['BTC','ETH']
CONFIG.pop('execution',None)
CONFIG['venues']=['hyperliquid','lighter','edgex','robinhood_lighter','popdex','arcus']
def book(bid,ask,t=1000,size=100):
    return dict(bids=[(bid,size)],asks=[(ask,size)],time=t)
def books(t,other=100):
    return {('hyperliquid','BTC'):book(99.99,100,t),('lighter','BTC'):book(other,other+.01,t)}

class Tests(unittest.TestCase):
    def test_depth(self):
        self.assertAlmostEqual(vwap([(100,1),(110,2)],2),105)
        with self.assertRaises(ValueError): vwap([(100,1)],2)
    def test_invalid_price(self):
        with self.assertRaises(ValueError): normalize([dict(p='nan',q='1')],'p','q',True)
    def test_freshness(self):
        self.assertFalse(healthy(book(1,2,900),book(1,2,1000),1000,CONFIG))
        self.assertFalse(healthy(book(1,2,1000),book(1,2,994),1000,CONFIG))
    def test_four_fees_and_funding(self):
        p=dict(quantity=1,buy_price=100,sell_price=102,buy='hyperliquid',sell='lighter',open_fees=.0704,notional=100,opened=1000)
        result=liquidation(p,book(101,101.1),book(100.9,101),CONFIG,4600)
        self.assertAlmostEqual(result['gross_pnl'],2)
        self.assertAlmostEqual(result['fees'],.1411)
        self.assertAlmostEqual(result['reserve'],.04)
        self.assertAlmostEqual(result['net_pnl'],1.8189)
    def engine(self,path=':memory:'):
        c=copy.deepcopy(CONFIG)
        c.update(symbols=['BTC'],warmup_seconds=10,min_samples=2,confirm_seconds=5,poll_seconds=5)
        return Engine(c,path)
    def test_warmup_entry_exit_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'state.db')
            e=self.engine(path)
            self.assertEqual(e.tick(books(1000),{},1000)['rows'][0]['status'],'WARMUP')
            e.tick(books(1005),{},1005)
            self.assertEqual(e.tick(books(1010,101),{},1010)['rows'][0]['status'],'PENDING')
            e.tick(books(1015,101),{},1015)
            self.assertEqual(len(e.state['positions']),1)
            self.assertLessEqual(e.state['positions'][0]['quantity']*101,100.000001)
            e.db.close()
            e=self.engine(path)
            self.assertEqual(len(e.state['positions']),1)
            result=e.tick(books(1020,100),{},1020)
            self.assertEqual(len(result['positions']),0)
            self.assertEqual(result['trades'][0]['reason'],'take_profit')
            self.assertGreater(result['realized_pnl'],0)
            e.db.close()
    def test_gap_resets_confirmation(self):
        e=self.engine()
        for t in (1000,1005): e.tick(books(t),{},t)
        e.tick(books(1010,101),{},1010)
        e.tick({}, {'test':'offline'},1015)
        out=e.tick(books(1020,101),{},1020)
        self.assertEqual(out['rows'][0]['duration'],0)
        self.assertFalse(out['positions'])
        e.db.close()
    def test_insufficient_depth_no_sample(self):
        e=self.engine()
        out=e.tick({('hyperliquid','BTC'):book(99,100,size=.0001),('lighter','BTC'):book(101,102)}, {},1000)
        self.assertFalse(out['positions'])
        self.assertEqual(e.db.execute('select count(*) from samples').fetchone()[0],0)
        e.db.close()
    def test_config(self):
        c=copy.deepcopy(CONFIG);c['notional_usd']=-1
        with self.assertRaises(ValueError): validate(c)

if __name__=='__main__': unittest.main()

