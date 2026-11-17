import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from arb import Engine, validate, healthy
from feeds import Feed, VENUES, LIGHTER_APIS
from test_arb import CONFIG, book

class MultiVenueTests(unittest.TestCase):
    def test_all_directions_and_failed_venue_isolation(self):
        c=copy.deepcopy(CONFIG); c['symbols']=['BTC']; validate(c)
        engine=Engine(c, ':memory:')
        books={(v,'BTC'):book(99,100) for v in c['venues'] if v!='popdex'}
        result=engine.tick(books, {'popdex:BTC':'offline'}, 1000)
        self.assertEqual(len(result['rows']),30)
        self.assertEqual(len({r['key'] for r in result['rows']}),30)
        self.assertEqual(sum(r['status']=='WARMUP' for r in result['rows']),20)
        self.assertEqual(sum(r['status']=='UNAVAILABLE' for r in result['rows']),10)
        self.assertTrue(all(r['buy']!=r['sell'] for r in result['rows']))
        engine.db.close()

    def test_active_four_venues(self):
        c=json.loads(Path('config.json').read_text(encoding='utf-8-sig'))
        validate(c)
        self.assertEqual(c['venues'],['edgex','robinhood_lighter','popdex','arcus'])
        self.assertEqual(c['symbols'],'ALL')
        c['symbols']=['BTC','ETH']
        e=Engine(c, ':memory:')
        try:
            result=e.tick({(v,s):book(99,100) for v in c['venues'] for s in c['symbols']}, {},1000)
            self.assertEqual(len(result['rows']),24)
            self.assertFalse(any(r['buy'] in ('hyperliquid','lighter') or r['sell'] in ('hyperliquid','lighter') for r in result['rows']))
        finally:
            e.db.close()

    def test_venue_and_fee_validation(self):
        for venues in [['edgex'],['edgex','edgex'],['edgex','typo']]:
            c=copy.deepcopy(CONFIG); c['venues']=venues
            with self.assertRaises(ValueError):validate(c)
        for value in [None,True,-1,float('nan')]:
            c=copy.deepcopy(CONFIG);c['fees_bps']['arcus']=value
            with self.assertRaises(ValueError):validate(c)
        c=copy.deepcopy(CONFIG);del c['venues'];validate(c)
        self.assertEqual(c['venues'],['edgex','robinhood_lighter','popdex','arcus'])

    def test_lighter_domains_have_separate_market_ids(self):
        def response(url, payload=None):
            rh=url.startswith(LIGHTER_APIS['robinhood_lighter'])
            mid=1 if rh else 42
            if url.endswith('orderBooks'):
                return dict(code=200,order_books=[dict(symbol='BTC',market_type='perp',status='active',market_id=mid,multiplier='1')])
            self.assertIn('market_id='+str(mid)+'&',url)
            return dict(code=200,bids=[dict(price='99',remaining_base_amount='2')],asks=[dict(price='100',remaining_base_amount='2')])
        f=Feed()
        with patch('feeds.request',side_effect=response):
            for venue in ['lighter','robinhood_lighter','lighter']:
                self.assertEqual(f.book(venue,'BTC')['bids'],[(99,2)])
        self.assertEqual(f.markets[('lighter','BTC')][1],42)
        self.assertEqual(f.markets[('robinhood_lighter','BTC')][1],1)

    def test_arcus_microseconds_and_stale_rejection(self):
        f=Feed()
        meta={'markets':[dict(baseAsset='BTC',quoteAsset='USD',type='PERPETUAL',status='ONLINE',marketId=1,marketDisplayName='BTC-USD')]}
        depth=dict(bids=[['99','1']],asks=[['100','1']],timestamp=1000000000)
        with patch('feeds.request',side_effect=[meta,depth]) as req:
            b=f.book('arcus','BTC')
        self.assertIn('/l2OrderBook/BTC-USD?',req.call_args.args[0])
        self.assertEqual(b['time'],1000)
        self.assertTrue(healthy(b,book(99,100),1000,CONFIG))
        self.assertFalse(healthy(b,book(99,100,1100),1100,CONFIG))

    def test_popdex_error_and_milliseconds(self):
        meta=dict(code='200',data=dict(baseToken='BTC',quoteToken='USDT',category='Futures',status='Trading',symbol='BTCUSDT'))
        depth=dict(code='200',updatedTime='1000000',data=dict(bids=[['99','2']],asks=[['100','3']]))
        with patch('feeds.request',side_effect=[meta,depth]):
            self.assertEqual(Feed().book('popdex','BTC')['time'],1000)
        with patch('feeds.request',side_effect=[meta,dict(depth,code='500')]):
            with self.assertRaises(ValueError):Feed().book('popdex','BTC')
        meta['data']['category']='Spot'
        with patch('feeds.request',return_value=meta):
            with self.assertRaises(ValueError):Feed().book('popdex','BTC')

    def test_edgex_dynamic_id_and_snapshot_only(self):
        meta=dict(code='SUCCESS',data=dict(coinList=[dict(coinId='1',coinName='BTC'),dict(coinId='2',coinName='USDC')],
            contractList=[dict(baseCoinId='1',quoteCoinId='2',enableTrade=True,enableOpenPosition=True,contractId='30000001')]))
        d=dict(contractId='30000001',depthType='SNAPSHOT',bids=[dict(price='99',size='2')],asks=[dict(price='100',size='2')])
        with patch('feeds.request',side_effect=[meta,dict(code='SUCCESS',data=[d],responseTime='1')]) as req, patch('feeds.time.time',return_value=1000):
            b=Feed().book('edgex','BTC')
            self.assertEqual(b['time'],1000)
            self.assertIn('contractId=30000001&',req.call_args.args[0])
        d['depthType']='CHANGED'
        with patch('feeds.request',side_effect=[meta,dict(code='SUCCESS',data=[d])]):
            with self.assertRaises(ValueError):Feed().book('edgex','BTC')

    def test_unknown_venue_never_falls_back(self):
        with patch('feeds.request') as req:
            with self.assertRaises(ValueError):Feed().book('unknown','BTC')
            req.assert_not_called()

if __name__=='__main__':unittest.main()
