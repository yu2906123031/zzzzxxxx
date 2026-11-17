import unittest
from depth import size_pair
from test_execution import config,books,signal
from execution import PaperExecutor
from execution_runtime import confirmation
from market_identity import identity_conflict

class DepthRiskTests(unittest.TestCase):
    def make(self):
        e=PaperExecutor(config(),':memory:');self.addCleanup(e.db.close);return e
    def test_smaller_side_sets_equal_orders(self):
        e=self.make();b=books(1000);b[('arcus','BTC')]['bids']=[(101,.3)]
        e.submit(signal(),b,1000,drill=True)
        self.assertAlmostEqual(e.s['orders'][0]['quantity'],.075)
        self.assertEqual(e.s['orders'][0]['quantity'],e.s['orders'][1]['quantity'])
        b[('arcus','BTC')]['time']=1002;b[('arcus','BTC')]['received']=1002
        b[('edgex','BTC')]['time']=1002;b[('edgex','BTC')]['received']=1002
        e.step(b,1002);self.assertEqual(e.s['pairs'][0]['state'],'OPEN')
        self.assertEqual(len(e.s['orders']),2)
    def test_exit_depth_limits_entry(self):
        b=books(1000);b[('edgex','BTC')]['bids']=[(99,.28)]
        self.assertAlmostEqual(size_pair(b[('edgex','BTC')],b[('arcus','BTC')],config(),.1)[0],.07)
    def test_outside_limit_depth_ignored(self):
        b=books(1000);b[('edgex','BTC')]['asks']=[(100,.28),(110,100)]
        self.assertAlmostEqual(size_pair(b[('edgex','BTC')],b[('arcus','BTC')],config(),.1)[0],.07)
    def test_below_minimum_rejected(self):
        e=self.make();self.assertIsNone(e.submit(signal(),books(1000,size=.01),1000,drill=True))
        self.assertFalse(e.s['orders'])
    def test_negative_absolute_spread_blocked_despite_deviation(self):
        e=self.make();s=signal();s['baseline']=-500
        self.assertIsNone(e.submit(s,books(1000,b=(99,100)),1000))
        self.assertIsNone(confirmation(s,books(1000,b=(99,100)),1000,config()))
    def test_large_unknown_price_discrepancy_blocked(self):
        e=self.make();self.assertIsNone(e.submit(signal(),books(1000,b=(200,201)),1000,drill=True))
    def test_missing_valuation_requests_exit_without_fabricated_fill(self):
        e=self.make();e.submit(signal(),books(1000),1000,drill=True);e.step(books(1002),1002)
        e.step({},1003);r=e.step({},1034)
        self.assertEqual(r['positions'][0]['reason'],'valuation unavailable timeout')
        self.assertEqual(len(e.s['orders']),2)
        e.step(books(1036),1036);r=e.step(books(1038),1038)
        self.assertFalse(r['positions'])
        self.assertEqual(r['trades'][0]['reason'],'valuation unavailable timeout')
    def test_equity_same_class_still_requires_underlying(self):
        self.assertIsNotNone(identity_conflict('ABC','arcus','edgex',{'asset_class':'EQUITIES'},{'asset_class':'EQUITIES'}))
    def test_closed_profit_separate_from_open_cash_costs(self):
        e=self.make();e.submit(signal(),books(1000),1000,drill=True);r=e.step(books(1002),1002)
        self.assertEqual(r['realized_pnl'],0);self.assertLess(r['cash_pnl'],0)

if __name__=='__main__':unittest.main()
