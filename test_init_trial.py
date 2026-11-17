import copy
import json
import unittest
from pathlib import Path

from init_trial import evaluate


class TrialChecks(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(Path('config.json').read_text(encoding='utf-8-sig'))
        self.config['fees_bps'] = {'hyperliquid':5,'aster':5}
        self.books = {
            ('hyperliquid','INIT'):dict(time=100,bids=[(.0938,100000)],asks=[(.0939,100000)]),
            ('aster','INIT'):dict(time=100,bids=[(.0963,100000)],asks=[(.0964,100000)])}

    def test_equal_quantity_and_conditional_profit(self):
        quote = evaluate(self.books,self.config,100)
        self.assertAlmostEqual(quote['long_notional']/quote['buy_vwap'],quote['short_notional']/quote['sell_vwap'])
        self.assertLessEqual(max(quote['long_notional'],quote['short_notional']),100)
        self.assertGreater(quote['convergence_net_estimate_usd'],0)
        self.assertLess(quote['immediate_roundtrip_gross_usd'],0)

    def test_reject_stale_and_skew(self):
        with self.assertRaisesRegex(ValueError,'stale'):
            evaluate(self.books,self.config,120)
        books = copy.deepcopy(self.books)
        books[('aster','INIT')]['time'] = 94
        with self.assertRaisesRegex(ValueError,'timestamps'):
            evaluate(books,self.config,100)

    def test_exit_depth_caps_entry(self):
        self.books[('aster','INIT')]['asks'] = [(.0964,400)]
        quote = evaluate(self.books,self.config,100)
        self.assertLessEqual(quote['quantity'],100)


if __name__ == '__main__':
    unittest.main()
