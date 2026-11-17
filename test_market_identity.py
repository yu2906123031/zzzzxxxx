import copy
import unittest
from unittest.mock import patch
from market_identity import MarketCatalog, identity_conflict
from discovery import discover, universe
from feeds import Feed
from arb import Engine
from execution import PaperExecutor
from execution_runtime import confirmation
from test_execution import config
from test_arb import book

class IdentityTests(unittest.TestCase):
    def test_qnt_collision_excluded_without_losing_catalog_for_existing_positions(self):
        catalogs={'arcus':{'QNT':'QNT-USD','BTC':'BTC-USD'},'edgex':{'QNT':'30000181','BTC':'30000001'}}
        self.assertEqual(universe(catalogs),{'BTC':['arcus','edgex']})
        f=Feed();f.catalogs=catalogs
        self.assertEqual(f.market('arcus','QNT'),'QNT-USD')
        self.assertEqual(f.market('edgex','QNT'),'30000181')

    def test_metadata_retained_and_other_asset_class_collision_excluded(self):
        m=dict(type='PERPETUAL',status='ONLINE',quoteAsset='USD',baseAsset='NEW',marketDisplayName='NEW-USD',category='EQUITIES',fullAssetName='New Stock')
        with patch('discovery.request',return_value={'markets':[m]}):c=discover('arcus')
        self.assertEqual(c.identities['NEW']['asset_class'],'EQUITIES')
        self.assertEqual(universe({'arcus':c,'edgex':MarketCatalog({'NEW':123},{'NEW':{'asset_class':'CRYPTO'}})}),{})
        f=Feed();f.catalogs={'arcus':c}
        with patch('feeds.request',return_value=dict(bids=[[99,2]],asks=[[100,2]],timestamp=1000000000)):
            self.assertEqual(f.book('arcus','NEW')['identity']['name'],'New Stock')

    def test_unknown_qnt_venue_is_not_assumed_equivalent(self):
        self.assertIsNotNone(identity_conflict('QNT','arcus','popdex'))
        self.assertIsNotNone(identity_conflict('QNT','edgex','popdex'))
        self.assertIsNone(identity_conflict('CASHCAT','arcus','robinhood_lighter',{'asset_class':'CRYPTO'}))

    def test_fixed_symbol_radar_never_samples_or_signals_qnt_collision(self):
        c=config();c['symbols']=['QNT']
        e=Engine(c,':memory:');self.addCleanup(e.db.close)
        b={('arcus','QNT'):book(49,50),('edgex','QNT'):book(229,230)}
        rows=e.tick(b,{},1000)['rows']
        self.assertTrue(all(r['error_code']=='ASSET_IDENTITY' for r in rows))
        self.assertEqual(e.db.execute('select count(*) from samples').fetchone()[0],0)
        self.assertFalse(e.state['positions'])

    def test_pending_signal_and_direct_drill_cannot_bypass_identity_check(self):
        c=config();c['symbols']=['QNT']
        s=dict(symbol='QNT',buy='arcus',sell='edgex',quantity=.1,baseline=0,cost_bps=0)
        b={('arcus','QNT'):book(49,50),('edgex','QNT'):book(229,230)}
        self.assertIsNone(confirmation(s,b,1000,c))
        e=PaperExecutor(c,':memory:');self.addCleanup(e.db.close)
        self.assertIsNone(e.submit(s,b,1000,drill=True))
        self.assertFalse(e.s['orders'])
        self.assertIn('underlying mismatch',e.s['events'][-1]['reason'])

    def test_existing_position_can_still_close_after_identity_discovered(self):
        c=config();c['symbols']=['NEW']
        s=dict(symbol='NEW',buy='arcus',sell='edgex',quantity=.1,baseline=0,cost_bps=0)
        def snapshots(t,classified=False):
            b={('arcus','NEW'):book(99,100,t),('edgex','NEW'):book(101,102,t)}
            if classified:
                b[('arcus','NEW')]['identity']={'asset_class':'EQUITIES'}
                b[('edgex','NEW')]['identity']={'asset_class':'CRYPTO'}
            return b
        e=PaperExecutor(c,':memory:');self.addCleanup(e.db.close)
        pid=e.submit(s,snapshots(1000),1000,drill=True)
        e.step(snapshots(1002),1002)
        e.request_close(pid)
        e.step(snapshots(1004,True),1004)
        result=e.step(snapshots(1006,True),1006)
        self.assertFalse(result['positions'])
        self.assertEqual(result['trades'][0]['state'],'CLOSED')
        self.assertTrue(e.reconcile(1006))

if __name__=='__main__':unittest.main()
