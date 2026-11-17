import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from init_live import Journal, Runner, plan, process_lock
from live_venues import D, VenueError, common_step, Hyperliquid, Aster


class FakeVenue:
    step=D('1');minimum=D('10');min_qty=D('1');max_qty=D('100000')

    def __init__(self, name, price):
        self.name=name;self.mid=D(price);self.pos=D(0);self.orders={};self.sends=0
        self.partial=None;self.timeout=False;self.unknown=False

    def price(self,value,buy):
        return D(str(value)).quantize(D('0.000001'))

    def book(self):
        return dict(time=time.time(),bids=[(self.mid-D('.0001'),100000)],asks=[(self.mid+D('.0001'),100000)])

    def position(self):return self.pos

    def cancel(self,order):pass

    def send(self,order):
        self.sends+=1
        qty=D(order['qty'])
        if self.partial is not None and not order['reduce']:qty*=self.partial
        self.pos+=qty*(1 if order['buy'] else -1)
        self.orders[order['id']]=dict(status='FILLED',qty=str(qty),notional=str(qty*D(order['price'])),fee='0')
        if self.timeout:raise TimeoutError('simulated POST timeout after execution')

    def resolve(self,order):
        return None if self.unknown else self.orders.get(order['id'])


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.j=Journal(Path(self.temp.name)/'state.sqlite')
        self.venues={'hl':FakeVenue('hl','.094'),'aster':FakeVenue('aster','.097')}
        self.r=Runner(self.j,self.venues,{'hl':D('.0005'),'aster':D('.0005')})

    def tearDown(self):
        self.j.db.close();self.temp.cleanup()

    def test_common_step(self):
        self.assertEqual(common_step('.03','.02'),D('.06'))

    def test_plan_caps_size_and_quantizes(self):
        p=plan(self.venues,self.r.books(),self.r.fees)
        self.assertLessEqual(D(p['long_notional']),100)
        self.assertLessEqual(D(p['short_notional']),100)
        self.assertEqual(D(p['qty'])%1,0)
        self.assertGreater(D(p['convergence_net_estimate_usd']),0)

    def test_smaller_requested_size_recalculates_dollar_profit(self):
        books=self.r.books()
        full=plan(self.venues,books,self.r.fees)
        small=plan(self.venues,books,self.r.fees,requested='200')
        self.assertEqual(small['qty'],'200')
        self.assertLess(D(small['convergence_net_estimate_usd']),D(full['convergence_net_estimate_usd']))

    def open_small(self):
        self.r.send('aster',False,200,D('.0968'))
        self.r.send('hl',True,200,D('.0942'))
        for order in self.j.s['orders']:order['result']['fee']='0.01'

    def test_positive_opening_spread_is_not_immediate_profit(self):
        self.open_small()
        self.assertLess(self.r.close_quote(self.r.positions(),self.r.books()),0)

    def test_convergence_covers_all_fees_and_reserves(self):
        self.open_small()
        self.venues['hl'].mid=D('.0955');self.venues['aster'].mid=D('.0955')
        net=self.r.close_quote(self.r.positions(),self.r.books())
        # Cash flows: +.52 opening, -.04 exit spread, -.02 open fees,
        # -.0191 close fees, -.0382 execution reserve, -.02 funding reserve.
        self.assertEqual(net,D('.3827'))

    def test_close_depth_outside_limit_cannot_trigger_take_profit(self):
        self.open_small()
        self.venues['hl'].mid=D('.0955');self.venues['aster'].mid=D('.0955')
        books=self.r.books()
        books['hl']['bids']=[(D('.0954'),4),(D('.0950'),100000)]
        with self.assertRaisesRegex(VenueError,'depth'):
            self.r.close_quote(self.r.positions(),books)

    def test_monitor_exits_on_net_profit_and_stop_loss(self):
        for estimate in (D('.10'),D('-1')):
            with patch.object(self.r,'check_positions',return_value={'hl':D(200),'aster':D(-200)}), \
                 patch.object(self.r,'close_quote',return_value=estimate) as quote,patch('init_live.time.sleep') as sleep:
                self.r.monitor()
                self.assertEqual(quote.call_count,1)
                sleep.assert_not_called()

    def test_monitor_maximum_hold_does_not_wait_forever(self):
        with patch('init_live.time.monotonic',side_effect=[0,61]),patch.object(self.r,'books') as books:
            self.r.monitor()
            books.assert_not_called()

    def test_stale_books_block(self):
        books=self.r.books();books['hl']['time']-=20
        with self.assertRaisesRegex(VenueError,'stale'):plan(self.venues,books,self.r.fees)

    def test_intent_persisted_before_post(self):
        original=self.venues['aster'].send
        def send(order):
            persisted=self.j.db.execute('SELECT value FROM state WHERE id=1').fetchone()[0]
            self.assertIn(order['id'],persisted)
            return original(order)
        self.venues['aster'].send=send
        self.r.send('aster',False,200,D('.0968'))

    def test_timeout_queries_without_resend(self):
        self.venues['aster'].timeout=True
        result=self.r.send('aster',False,200,D('.0968'))
        self.assertEqual(result['qty'],'200')
        self.assertEqual(self.venues['aster'].sends,1)

    def test_unknown_blocks_duplicate(self):
        self.venues['aster'].unknown=True
        with patch('init_live.time.monotonic',side_effect=[0,100]):
            with self.assertRaisesRegex(VenueError,'UNKNOWN'):
                self.r.send('aster',False,200,D('.0968'))
        with self.assertRaisesRegex(VenueError,'unresolved'):
            self.r.send('aster',False,200,D('.0968'))
        self.assertEqual(self.venues['aster'].sends,1)

    def test_partial_short_hedges_actual_qty(self):
        self.venues['aster'].partial=D('.5')
        self.r.open_pair(plan(self.venues,self.r.books(),self.r.fees))
        self.assertGreater(self.venues['hl'].pos,0)
        self.assertEqual(self.venues['hl'].pos,-self.venues['aster'].pos)
        with patch('init_live.time.sleep'):
            self.assertTrue(self.r.flatten())
        self.assertEqual(self.venues['hl'].pos,0)
        self.assertEqual(self.venues['aster'].pos,0)

    def test_partial_hedge_recovers_reduce_only(self):
        self.venues['hl'].partial=D('.5')
        with self.assertRaisesRegex(VenueError,'mismatched'):
            self.r.open_pair(plan(self.venues,self.r.books(),self.r.fees))
        with patch('init_live.time.sleep'):
            self.assertTrue(self.r.flatten())
        self.assertTrue(all(o['reduce'] for o in self.j.s['orders'][2:]))

    def test_external_position_is_never_closed(self):
        self.venues['hl'].pos=D(100)
        with patch('init_live.time.sleep'):
            self.assertFalse(self.r.flatten())
        self.assertEqual(self.venues['hl'].sends,0)

    def test_restart_keeps_unresolved_order_and_nonce(self):
        self.j.s['orders']=[dict(id='test',venue='aster')];self.j.save()
        before=self.j.nonce()
        self.j.db.close();self.j=Journal(Path(self.temp.name)/'state.sqlite')
        self.assertEqual(self.j.s['orders'][0]['id'],'test')
        self.assertGreater(self.j.nonce(),before)

    def test_unknown_venue_does_not_block_other_leg_reduction(self):
        self.r.send('hl',True,200,D('.0942'))
        self.j.s['orders'].append(dict(id='unknown',venue='aster',buy=False,qty='200',price='.0968',reduce=False,created=time.time()))
        self.j.save()
        self.venues['aster'].unknown=True
        original=self.r.reconcile_order
        def reconcile(order,seconds=8):
            if order['venue']=='aster':raise VenueError('unknown')
            return original(order,seconds)
        with patch.object(self.r,'reconcile_order',side_effect=reconcile),patch('init_live.time.sleep'):
            self.assertFalse(self.r.flatten())
        self.assertEqual(self.venues['hl'].pos,0)
        self.assertEqual(self.venues['aster'].sends,0)

    def test_restart_reconciles_and_flattens_same_account(self):
        self.r.open_pair(plan(self.venues,self.r.books(),self.r.fees))
        self.j.db.close();self.j=Journal(Path(self.temp.name)/'state.sqlite')
        runner=Runner(self.j,self.venues,self.r.fees)
        sends=sum(v.sends for v in self.venues.values())
        with patch('init_live.time.sleep'):
            self.assertTrue(runner.flatten())
        self.assertEqual(sum(v.sends for v in self.venues.values())-sends,2)
        self.assertTrue(all(o['reduce'] for o in self.j.s['orders'][2:]))

    def test_signed_transport_never_exposes_request_url(self):
        import requests
        from live_venues import http
        with patch('live_venues.requests.request',side_effect=requests.ConnectionError('https://host?signature=SECRET')):
            with self.assertRaises(VenueError) as context:
                http('GET','https://host?signature=SECRET')
        self.assertNotIn('SECRET',str(context.exception))

    def test_process_lock_blocks_second_process(self):
        path=Path(self.temp.name)/'trial.lock'
        with process_lock(path):
            with self.assertRaises(VenueError):
                with process_lock(path):pass

    def test_hl_price_rounds_inside_protection(self):
        venue=object.__new__(Hyperliquid);venue.decimals=0
        self.assertLessEqual(venue.price(D('.0976893'),True),D('.0976893'))
        self.assertGreaterEqual(venue.price(D('.0976893'),False),D('.0976893'))

    def test_unified_balance_reads_spot_and_respects_holds(self):
        venue=object.__new__(Hyperliquid);venue.user='test';venue.info=Mock()
        venue.info.query_user_abstraction_state.return_value='unifiedAccount'
        venue.info.spot_user_state.return_value=dict(balances=[dict(coin='USDC',token=0,total='200.595751',hold='20')],
                                                     tokenToAvailableAfterMaintenance=[[0,'170']])
        result=venue.balance()
        self.assertEqual(result['wallet_balance'],'200.595751')
        self.assertEqual(result['available_collateral'],'170')
        venue.info.user_state.assert_not_called()

    def test_unified_missing_maintenance_availability_blocks(self):
        venue=object.__new__(Hyperliquid);venue.user='test';venue.info=Mock()
        venue.info.query_user_abstraction_state.return_value='unifiedAccount'
        venue.info.spot_user_state.return_value=dict(balances=[dict(coin='USDC',token=0,total='200',hold='0')])
        with self.assertRaisesRegex(VenueError,'maintenance'):
            venue.balance()

    def test_default_balance_uses_perp_state(self):
        venue=object.__new__(Hyperliquid);venue.user='test';venue.info=Mock()
        venue.info.query_user_abstraction_state.return_value='default'
        venue.info.user_state.return_value=dict(marginSummary=dict(accountValue='150'),withdrawable='130')
        result=venue.balance()
        self.assertEqual(result['available_collateral'],'130')
        venue.info.spot_user_state.assert_not_called()

    def test_unauthorized_hl_signer_blocked_before_opening(self):
        venue=object.__new__(Hyperliquid);venue.user='owner';venue.info=Mock();venue.exchange=Mock()
        venue.exchange.wallet.address='unauthorized'
        venue.info.post.return_value=[]
        with self.assertRaisesRegex(VenueError,'not authorized'):
            venue.preflight()
        venue.info.open_orders.assert_not_called()

    def test_expired_hl_signer_is_blocked(self):
        venue=object.__new__(Hyperliquid);venue.user='owner';venue.info=Mock();venue.exchange=Mock()
        venue.exchange.wallet.address='agent'
        venue.info.post.return_value=[dict(address='agent',validUntil=1)]
        with self.assertRaisesRegex(VenueError,'expired'):
            venue.preflight()

    def test_hl_fill_reconciliation_deduplicates_and_waits_for_missing_fill(self):
        venue=object.__new__(Hyperliquid);venue.user='owner';venue.info=Mock()
        order=dict(id='0x'+'0'*32,qty='200',created=time.time())
        venue.info.query_order_by_cloid.return_value=dict(status='order',order=dict(status='filled',
            order=dict(coin='INIT',oid=42,origSz='200',sz='0',cloid=order['id'])))
        fill=dict(tid=1,oid=42,sz='200',px='.0942',fee='.01',feeToken='USDC')
        venue.info.user_fills_by_time.return_value=[fill,fill]
        result=venue.resolve(order)
        self.assertEqual(result['qty'],'200')
        self.assertEqual(result['fee'],'0.01')
        venue.info.user_fills_by_time.return_value=[]
        self.assertIsNone(venue.resolve(order))

    def test_aster_signed_string_matches_wire_and_recovers_signer(self):
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        from urllib.parse import parse_qs
        # Disposable test-only key; never used for an account or real requests.
        venue=object.__new__(Aster);venue.wallet=Account.create();venue.user=Account.create().address;venue.journal=self.j
        with patch('live_venues.http',return_value={}) as transport:
            venue.signed('POST','order',symbol='INITUSDT',quantity='100')
        body=transport.call_args.args[2]
        msg,sig=body.rsplit('&signature=',1)
        typed=encode_typed_data(domain_data=dict(name='AsterSignTransaction',version='1',chainId=1666,
            verifyingContract='0x0000000000000000000000000000000000000000'),
            message_types={'Message':[{'name':'msg','type':'string'}]},message_data={'msg':msg})
        self.assertEqual(Account.recover_message(typed,signature=sig),venue.wallet.address)
        self.assertEqual(parse_qs(msg)['signer'],[venue.wallet.address])


if __name__=='__main__':unittest.main()
