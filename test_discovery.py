import copy
import unittest
from unittest.mock import patch
from discovery import discover, universe
from feeds import Feed
from arb import Engine,validate
from test_arb import CONFIG,book

class DiscoveryTests(unittest.TestCase):
    def test_shared_symbols_no_alias_guessing(self):
        self.assertEqual(universe({'a':{'BTC':1,'1000PEPE':2},'b':{'BTC':3,'PEPE':4}}),{'BTC':['a','b']})
    def test_popdex_inactive_wrong_quote_and_duplicates(self):
        good=dict(category='Futures',status='Trading',quoteToken='USDT',baseToken='NEW',symbol='NEWUSDT')
        rows=[good,dict(good,baseToken='BAD',status='Stopped'),dict(good,baseToken='OTHER',quoteToken='BTC'),dict(good,baseToken='DUP'),dict(good,baseToken='DUP')]
        with patch('discovery.request',return_value=dict(code='200',data=rows)):
            self.assertEqual(discover('popdex'),{'NEW':'NEWUSDT'})
    def test_arcus_active_perpetual_only(self):
        m=dict(type='PERPETUAL',status='ONLINE',quoteAsset='USD',baseAsset='NEW',marketDisplayName='NEW-USD')
        with patch('discovery.request',return_value={'markets':[m,dict(m,type='SPOT',baseAsset='BAD')]}):
            self.assertEqual(discover('arcus'),{'NEW':'NEW-USD'})
    def test_catalog_missing_market_fails_closed(self):
        f=Feed();f.catalogs={'popdex':{}}
        with patch('feeds.request') as request:
            with self.assertRaises(ValueError):f.market('popdex','BTC')
            request.assert_not_called()
    def test_partial_tick_does_not_resample_other_symbols(self):
        c=copy.deepcopy(CONFIG);c['symbols']=['BTC','ETH'];c['venues']=['edgex','arcus'];c['symbol_venues']={'BTC':['edgex','arcus'],'ETH':['edgex','arcus']}
        e=Engine(c,':memory:')
        try:
            books={(v,s):book(99,100) for v in c['venues'] for s in c['symbols']}
            e.tick(books,{},1000,symbols=['BTC'])
            e.tick(books,{},1000,symbols=['ETH'])
            self.assertEqual(e.db.execute('select count(*) from samples').fetchone()[0],4)
        finally:e.db.close()
    def test_removed_symbol_not_resampled(self):
        c=copy.deepcopy(CONFIG);c['symbols']=['ETH']
        e=Engine(c,':memory:')
        try:
            result=e.tick({}, {},1000,symbols=['BTC'])
            self.assertEqual(result['rows'],[])
        finally:e.db.close()
    def test_all_and_new_symbols_validate(self):
        for symbols in ['ALL',['NEWCOIN']]:
            c=copy.deepcopy(CONFIG);c['symbols']=symbols;validate(c)
        c=copy.deepcopy(CONFIG);c['symbols']=['../../bad']
        with self.assertRaises(ValueError):validate(c)

if __name__=='__main__':unittest.main()
