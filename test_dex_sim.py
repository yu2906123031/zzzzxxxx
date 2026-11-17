import contextlib, csv, io, tempfile, unittest
from pathlib import Path
from dex_sim import Pool, quote, run

class Tests(unittest.TestCase):
    def test_formula_and_invariant(self):
        p=Pool('A',1000,1000)
        self.assertAlmostEqual(p.output(100,True),1000*99.7/1099.7)
        p.swap(100,True)
        self.assertGreater(p.quote*p.base,1000000)
    def test_equal_pools_no_profit(self):
        self.assertLess(quote(Pool('A',300000,100),Pool('B',300000,100),200),200)
    def test_quote_execution(self):
        a,b=Pool('A',300000,100),Pool('B',310000,100)
        expected=quote(a,b,200)
        self.assertAlmostEqual(b.swap(a.swap(200,True),False),expected)
    def test_accounting(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            result=run(steps=200,output=d)
            with (Path(result['output'])/'trades.csv').open(encoding='utf-8-sig') as f:
                rows=list(csv.DictReader(f))
            self.assertAlmostEqual(sum(float(r['realized_net']) for r in rows),result['net_pnl'])
            self.assertGreater(result['filled'],0)
            self.assertGreater(result['reverted'],0)
            for row in rows:
                self.assertLessEqual(float(row['amount']),200)
                if row['status']=='REVERT': self.assertAlmostEqual(float(row['realized_net']),-.15)
            self.assertEqual(result['net_pnl'],run(steps=200,output=d)['net_pnl'])
    def test_invalid_config(self):
        for kwargs in [{'gas':-1},{'capital':0},{'slippage_bps':10000},{'max_trade':float('nan')}]:
            with self.assertRaises(ValueError): run(**kwargs)

if __name__=='__main__': unittest.main()
