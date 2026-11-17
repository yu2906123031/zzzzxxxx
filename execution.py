"""Persistent, order-level PAPER execution. This module cannot send real orders."""
import copy
import json
import math
import sqlite3
import time
import uuid
from market_identity import identity_conflict
from depth import size_pair, entry_cost, viable, levels, expected_profit

TERMINAL = {'FILLED','PARTIAL_CANCELLED','CANCELLED','REJECTED','TIMED_OUT'}
RETRY_HALTS = {'close retries: new entries halted', 'recovery attempts exhausted'}
DEFAULTS = dict(enabled=True, initial_balance_per_venue=1000, leverage=1,
    latency_seconds=1, order_timeout_seconds=30, max_unhedged_seconds=30,
    max_daily_loss_usd=20, slippage_bps=10, depth_participation=0.25,
    poll_seconds=2, max_watch_pairs=4, quantity_step=0.00000001, min_notional_usd=5,
    max_recovery_attempts=3, maintenance_fraction=0.1, max_valuation_gap_seconds=30,
    max_price_ratio=1.1, min_expected_profit_usd=0.02,
    route_window=8, route_min_trades=3, route_max_consecutive_losses=2,
    route_loss_limit_usd=0.5, route_pause_seconds=86400,
    break_even_after_seconds=60, break_even_buffer_usd=0.02)


def validate_execution(config):
    if not isinstance(config,dict): raise ValueError('execution must be an object')
    unknown=set(config)-set(DEFAULTS)
    if unknown: raise ValueError('unknown execution config: '+str(unknown))
    merged={**DEFAULTS,**config}
    if not isinstance(merged['enabled'],bool): raise ValueError('execution.enabled must be boolean')
    for key,value in merged.items():
        if key=='enabled': continue
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
            raise ValueError('invalid execution config: '+key)
    for key in ('route_window','route_min_trades','route_max_consecutive_losses'):
        if int(merged[key])!=merged[key]:raise ValueError('integer required: '+key)
    if merged['route_min_trades']>merged['route_window']:raise ValueError('route minimum exceeds window')
    if merged['max_price_ratio']<=1: raise ValueError('price ratio must exceed one')
    if merged['depth_participation']>1 or merged['maintenance_fraction']>=1 or merged['slippage_bps']>=10000:
        raise ValueError('invalid execution ratio')
    if merged['order_timeout_seconds']<=merged['latency_seconds']: raise ValueError('timeout must exceed latency')
    return merged


class PaperExecutor:
    def __init__(self, config, database, fault=None):
        self.c=config
        self.e=validate_execution(config.get('execution',{}))
        self.db=sqlite3.connect(database,timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS execution_state (id INTEGER PRIMARY KEY, value TEXT)')
        row=self.db.execute('SELECT value FROM execution_state WHERE id=1').fetchone()
        self.s=json.loads(row[0]) if row else dict(version=1,mode='PAPER_EXECUTION',orders=[],pairs=[],
            accounts={},ledger={},events=[],cooldowns={},halt_reason=None,day=None,day_equity=None,fault_used=False)
        for venue in config['venues']:
            self.s['accounts'].setdefault(venue,dict(initial=self.e['initial_balance_per_venue'],cash=self.e['initial_balance_per_venue'],realized=0,fees=0,funding_reserve=0))
        self.fault=fault
        self.books={}
        self.s.setdefault('route_pauses',{})
        self.update_route_pauses(time.time())
        self.save()

    def save(self):
        self.db.execute('INSERT OR REPLACE INTO execution_state VALUES (1,?)',(json.dumps(self.s,allow_nan=False),))
        self.db.commit()

    def event(self,kind,now,**fields):
        self.s['events'].append(dict(kind=kind,time=now,**fields))
        self.s['events']=self.s['events'][-500:]

    def ledger(self,venue,symbol):
        return self.s['ledger'].setdefault(venue+':'+symbol,dict(venue=venue,symbol=symbol,quantity=0.,average=0.))

    def fresh(self,book,now):
        return book and -2<=now-book['time']<=self.c['max_quote_age_seconds']

    def account_view(self,venue,books,now):
        account=self.s['accounts'][venue]
        unrealized=margin=0.;fresh=True
        for leg in self.s['ledger'].values():
            if leg['venue']!=venue or abs(leg['quantity'])<1e-12: continue
            book=books.get((venue,leg['symbol']))
            if not self.fresh(book,now): fresh=False
            mark=(book['bids'][0][0] if leg['quantity']>0 else book['asks'][0][0]) if book else leg['average']
            unrealized+=leg['quantity']*(mark-leg['average'])
            margin+=abs(leg['quantity'])*leg['average']/self.e['leverage']
        reserved=sum((o['quantity']-o['filled'])*o['limit']/self.e['leverage'] for o in self.s['orders'] if o['venue']==venue and not o['reduce_only'] and o['status'] not in TERMINAL)
        equity=account['cash']+unrealized
        return dict(account,venue=venue,equity=equity,unrealized=unrealized,margin=margin,reserved=reserved,
                    available=equity-margin-reserved,fresh=fresh)

    def order(self,pair,venue,side,quantity,book,now,reduce_only=False,purpose='OPEN'):
        price=book['asks'][0][0] if side=='BUY' else book['bids'][0][0]
        order=dict(id=uuid.uuid4().hex,pair_id=pair['id'],symbol=pair['symbol'],venue=venue,side=side,
            quantity=quantity,filled=0.,average=None,fee=0.,realized=0.,limit=price*(1+(1 if side=='BUY' else -1)*self.e['slippage_bps']/10000),
            created=now,ready_at=now+self.e['latency_seconds'],expires=now+self.e['order_timeout_seconds'],
            reduce_only=reduce_only,purpose=purpose,status='SUBMITTED',events=[dict(status='SUBMITTED',time=now)])
        self.s['orders'].append(order)
        self.event('ORDER_SUBMITTED',now,order_id=order['id'],pair_id=pair['id'],venue=venue,purpose=purpose)
        return order

    @staticmethod
    def route_key(p):return ':'.join((p['symbol'],p['buy'],p['sell']))

    def route_stats(self):
        grouped={}
        for p in self.s['pairs']:
            if p['state'] in ('CLOSED','ABORTED') and not p.get('drill') and not (p['state']=='ABORTED' and abs(p['net_pnl'])<1e-12):
                grouped.setdefault(self.route_key(p),[]).append(p)
        result=[]
        for key,trades in sorted(grouped.items()):
            trades=sorted(trades,key=lambda p:p.get('closed',p['created']))[-int(self.e['route_window']):]
            pnl=sum(p['net_pnl'] for p in trades);streak=0
            for p in reversed(trades):
                if p['net_pnl']>=0:break
                streak+=1
            pause=self.s['route_pauses'].get(key,{})
            result.append(dict(key=key,trades=len(trades),net_pnl=pnl,average_pnl=pnl/len(trades),
                wins=sum(p['net_pnl']>0 for p in trades),loss_streak=streak,
                last_trade=trades[-1]['id'],paused_until=pause.get('until'),pause_reason=pause.get('reason')))
        return result

    def update_route_pauses(self,now):
        for r in self.route_stats():
            previous=self.s['route_pauses'].get(r['key'],{})
            if previous.get('last_trade')==r['last_trade']:continue
            reason=None
            if r['loss_streak']>=self.e['route_max_consecutive_losses']:reason='consecutive losing trades'
            elif r['trades']>=self.e['route_min_trades'] and r['net_pnl']<=0:reason='non-positive recent net performance'
            elif r['net_pnl']<=-self.e['route_loss_limit_usd']:reason='route loss budget exceeded'
            if reason:
                self.s['route_pauses'][r['key']]=dict(until=now+self.e['route_pause_seconds'],reason=reason,last_trade=r['last_trade'])
                self.event('ROUTE_PAUSED',now,route=r['key'],reason=reason,net_pnl=r['net_pnl'])

    def submit(self,signal,books,now=None,drill=False):
        now=time.time() if now is None else now
        symbol,buy,sell=signal['symbol'],signal['buy'],signal['sell']
        def reject(reason):
            self.event('ENTRY_BLOCKED',now,symbol=symbol,reason=reason,drill=drill);self.save();return None
        if self.s['halt_reason']: return reject(self.s['halt_reason'])
        self.update_route_pauses(now)
        pause=self.s['route_pauses'].get(self.route_key(signal),{})
        if not drill and pause.get('until',0)>now:return reject('route paused: '+pause['reason'])
        active=[p for p in self.s['pairs'] if p['state'] not in ('CLOSED','ABORTED')]
        if any(p['symbol']==symbol for p in active): return reject('symbol already occupied')
        if len(active)>=self.c['max_positions']: return reject('position limit')
        if now-self.s['cooldowns'].get(symbol,-1e20)<self.c['cooldown_seconds']: return reject('cooldown')
        if buy==sell or buy not in self.c['venues'] or sell not in self.c['venues']:return reject('venue disabled')
        a,b=books.get((buy,symbol)),books.get((sell,symbol))
        conflict=identity_conflict(symbol,buy,sell,(a or {}).get('identity'),(b or {}).get('identity'))
        if conflict:return reject(conflict)
        if not self.fresh(a,now) or not self.fresh(b,now) or abs(a['time']-b['time'])>self.c['max_quote_skew_seconds']:return reject('stale or skewed preflight quote')
        if any(p.get('valuation_status') != 'fresh' or p['state'] != 'OPEN' for p in active):
            return reject('existing position requires risk recovery')
        if max(a['asks'][0][0],b['asks'][0][0])/min(a['asks'][0][0],b['asks'][0][0])>self.e['max_price_ratio']:
            return reject('cross-venue price discrepancy requires identity review')
        try: q,bp,sp=size_pair(a,b,self.c,signal['quantity'])
        except ValueError as exc: return reject(str(exc))
        cost=entry_cost(self.c,buy,sell)
        spread=(sp-bp)/bp*10000
        edge=spread-signal.get('baseline',0)-cost
        if not drill and not viable(spread,signal.get('baseline',0),cost,self.c):
            return reject('absolute spread or reversion edge cannot cover costs')
        profit=expected_profit(q,bp,sp,signal.get('baseline',0),cost)
        if not drill and profit<self.e['min_expected_profit_usd']:return reject('expected dollar profit below minimum after sizing')
        exposure=sum(p['quantity']*(p['buy_reference']+p['sell_reference']) for p in active)
        if exposure+q*(bp+sp)>self.c['max_total_notional_usd']:return reject('total exposure limit')
        for venue,price in [(buy,bp),(sell,sp)]:
            account=self.account_view(venue,books,now)
            needed=q*price*(1+self.e['slippage_bps']/10000)*(1/self.e['leverage']+self.c['fees_bps'][venue]/10000)
            if not account['fresh'] or account['available']<needed:return reject('virtual margin unavailable or stale')
        pair=dict(id=uuid.uuid4().hex,symbol=symbol,buy=buy,sell=sell,quantity=q,buy_reference=bp,sell_reference=sp,
            state='OPENING',created=now,opened=None,reason=None,drill=drill,recovery_attempts=0,funding_reserve=0.,funding_at=now,entry_baseline=signal.get('baseline',0),entry_cost_bps=cost,expected_profit_usd=profit)
        self.s['pairs'].append(pair)
        self.order(pair,buy,'BUY',q,a,now)
        self.order(pair,sell,'SELL',q,b,now)
        self.event('PAIR_PREPARED',now,pair_id=pair['id'],symbol=symbol,drill=drill,preflight_edge=edge,requested_quantity=signal['quantity'],sized_quantity=q,notional_buy=q*bp,notional_sell=q*sp)
        self.save();return pair['id']

    def apply_fill(self,order,quantity,price):
        ledger=self.ledger(order['venue'],order['symbol'])
        old=ledger['quantity'];delta=quantity*(1 if order['side']=='BUY' else -1)
        if order['reduce_only'] and (old*delta>=0 or quantity>abs(old)+1e-10):raise ValueError('reduce-only invariant')
        realized=0.
        if old*delta<0:
            closing=min(abs(old),quantity)
            realized=closing*(price-ledger['average'])*(1 if old>0 else -1)
            if quantity>abs(old):ledger['average']=price
        elif quantity:
            ledger['average']=(abs(old)*ledger['average']+quantity*price)/(abs(old)+quantity)
        ledger['quantity']=old+delta
        if abs(ledger['quantity'])<1e-12:ledger.update(quantity=0.,average=0.)
        fee=quantity*price*self.c['fees_bps'][order['venue']]/10000
        account=self.s['accounts'][order['venue']]
        account['cash']+=realized-fee;account['realized']+=realized;account['fees']+=fee
        order.update(filled=quantity,average=price,fee=fee,realized=realized)

    def process_order(self,o,books,now):
        if o['status'] in TERMINAL:return
        if o['status']=='UNKNOWN':
            if now>=o['reveal_at']:
                o['status']=o.pop('resolved_status');o['events'].append(dict(status=o['status'],time=now))
                self.event('ORDER_RECONCILED',now,order_id=o['id'])
            return
        if now<o['ready_at']:return
        b=books.get((o['venue'],o['symbol']))
        if now>=o['expires']:
            o['status']='TIMED_OUT';o['events'].append(dict(status=o['status'],time=now));return
        if not self.fresh(b,now) or b.get('received',b['time'])<=o['created']:return
        fault=None
        if self.fault and not self.s['fault_used'] and o['purpose']=='OPEN' and o['side']=='SELL':
            fault=self.fault;self.s['fault_used']=True
        if fault=='reject_short':
            o['status']='REJECTED';o['events'].append(dict(status='REJECTED',time=now));return
        wanted=o['quantity']
        if o['reduce_only']:
            pos=self.ledger(o['venue'],o['symbol'])['quantity']
            wanted=min(wanted,abs(pos)) if pos*(1 if o['side']=='BUY' else -1)<0 else 0.
        if fault=='partial_short':wanted*=.5
        filled=cost=0.
        for price,size in b['asks' if o['side']=='BUY' else 'bids']:
            if (o['side']=='BUY' and price>o['limit']) or (o['side']=='SELL' and price<o['limit']):break
            take=min(wanted-filled,size*self.e['depth_participation'])
            filled+=take;cost+=take*price
            if filled>=wanted-1e-12:break
        # Opening lots follow the explicit simulated quantity step; reductions close residual dust.
        if not o['reduce_only']:
            rounded=math.floor((filled+1e-14)/self.e['quantity_step'])*self.e['quantity_step']
            if filled:cost*=rounded/filled
            filled=rounded
        if filled>1e-12:self.apply_fill(o,filled,cost/filled)
        status='FILLED' if filled>=o['quantity']-1e-10 else 'PARTIAL_CANCELLED' if filled>1e-12 else 'CANCELLED'
        o['status']=status;o['events'].append(dict(status=status,time=now))
        if fault=='unknown_short':
            o.update(status='UNKNOWN',resolved_status=status,reveal_at=now+self.e['poll_seconds'])
            o['events'].append(dict(status='UNKNOWN',time=now))
        self.event('ORDER_RESULT',now,order_id=o['id'],status=o['status'],filled=filled)

    def pair_orders(self,pair):return [o for o in self.s['orders'] if o['pair_id']==pair['id']]

    def pair_pnl(self,p,books,now):
        orders=self.pair_orders(p);unrealized=0.;fresh=True
        for venue in (p['buy'],p['sell']):
            leg=self.ledger(venue,p['symbol']);b=books.get((venue,p['symbol']))
            if abs(leg['quantity'])>1e-12:
                if not self.fresh(b,now):fresh=False
                if not b: continue
                from arb import vwap
                try:mark=vwap(b['bids'] if leg['quantity']>0 else b['asks'],abs(leg['quantity']))
                except ValueError:
                    fresh=False
                    rows=b['bids'] if leg['quantity']>0 else b['asks']
                    if not rows: continue
                    mark=rows[-1][0]
                unrealized+=leg['quantity']*(mark-leg['average'])
        gross=sum(o['realized'] for o in orders)+unrealized
        fees=sum(o['fee'] for o in orders)
        exit_fees=sum(abs(self.ledger(v,p['symbol'])['quantity'])*(books[(v,p['symbol'])]['bids'][0][0] if self.ledger(v,p['symbol'])['quantity']>0 else books[(v,p['symbol'])]['asks'][0][0])*self.c['fees_bps'][v]/10000 for v in (p['buy'],p['sell']) if (v,p['symbol']) in books)
        exit_slippage=sum(abs(self.ledger(v,p['symbol'])['quantity'])*self.ledger(v,p['symbol'])['average'] for v in (p['buy'],p['sell']))*self.e['slippage_bps']/10000
        return dict(exit_slippage_reserve=exit_slippage,conservative_close_net=gross-fees-p['funding_reserve']-exit_fees-exit_slippage,break_even_gap_usd=max(0,-(gross-fees-p['funding_reserve']-exit_fees-exit_slippage)),gross_pnl=gross,fees=fees,reserve=p['funding_reserve'],net_pnl=gross-fees-p['funding_reserve'],estimated_close_net=gross-fees-p['funding_reserve']-exit_fees,valuation_status='fresh' if fresh else 'stale / awaiting depth')

    def request_close(self,pair_id,reason='manual_drill'):
        p=next(p for p in self.s['pairs'] if p['id']==pair_id)
        if p['state'] not in ('CLOSED','ABORTED'):p['state']='CLOSING';p['reason']=reason
        self.save()

    def reduce(self,p,venue,quantity,books,now,purpose):
        b=books.get((venue,p['symbol']))
        if quantity<=1e-12 or not self.fresh(b,now):return False
        leg=self.ledger(venue,p['symbol'])
        side='SELL' if leg['quantity']>0 else 'BUY'
        capacity=sum(size for _,size in levels(b,side,self.e['slippage_bps'],self.e['depth_participation']))
        quantity=min(quantity,capacity)
        if quantity<=1e-12:return False
        self.order(p,venue,side,quantity,b,now,True,purpose)
        return True

    def step(self,books,now=None):
        now=time.time() if now is None else now;self.books=books
        # Migrate only legacy retry latches. Real accounting/risk halts stay latched.
        if self.s['halt_reason'] in RETRY_HALTS and self.reconcile(now):
            previous=self.s['halt_reason']
            self.s['halt_reason']=None
            self.event('PAPER_RETRY_HALT_CLEARED',now,reason=previous)
        if self.s['day_equity'] is None:
            self.s.update(day=int(now//86400),day_equity=sum(self.account_view(v,books,now)['equity'] for v in self.s['accounts']))
        # First reconcile all outstanding intents; a timeout never creates an automatic duplicate.
        active=[p for p in self.s['pairs'] if p['state'] not in ('CLOSED','ABORTED')]
        for p in active:
            elapsed=max(0,now-p['funding_at']);p['funding_at']=now
            for v in (p['buy'],p['sell']):
                leg=self.ledger(v,p['symbol']);charge=abs(leg['quantity'])*leg['average']*self.c['funding_reserve_bps_per_hour']/2/10000*elapsed/3600
                p['funding_reserve']+=charge;self.s['accounts'][v]['cash']-=charge;self.s['accounts'][v]['funding_reserve']+=charge
        # Recheck both opening legs before any delayed fill. Unknown reports are reconciled unchanged.
        for p in active:
            orders=self.pair_orders(p)
            if p['state']=='OPENING' and not p.get('drill') and all(o['status']=='SUBMITTED' and o['filled']==0 for o in orders):
                a,b=books.get((p['buy'],p['symbol'])),books.get((p['sell'],p['symbol']))
                if self.fresh(a,now) and self.fresh(b,now) and min(a.get('received',a['time']),b.get('received',b['time']))>p['created']:
                    try:
                        q,bp,sp=size_pair(a,b,self.c,p['quantity'])
                        cost=entry_cost(self.c,p['buy'],p['sell'])
                        ok=(abs(q-p['quantity'])<1e-10 and abs(a['time']-b['time'])<=self.c['max_quote_skew_seconds']
                            and not identity_conflict(p['symbol'],p['buy'],p['sell'],a.get('identity'),b.get('identity'))
                            and viable((sp-bp)/bp*10000,p.get('entry_baseline',0),cost,self.c)
                            and expected_profit(q,bp,sp,p.get('entry_baseline',0),cost)>=self.e['min_expected_profit_usd'])
                    except ValueError:ok=False
                    if not ok:
                        for o in orders:
                            o['status']='CANCELLED';o['events'].append(dict(status='CANCELLED',time=now))
                        p['reason']='entry edge or depth vanished before fills'
                        self.event('ENTRY_CANCELLED',now,pair_id=p['id'],symbol=p['symbol'],reason=p['reason'])
        for o in self.s['orders']:self.process_order(o,books,now)
        accounts=[self.account_view(v,books,now) for v in self.s['accounts']]
        equity=sum(a['equity'] for a in accounts)
        day=int(now//86400)
        if self.s['day']!=day and all(a['fresh'] for a in accounts):self.s.update(day=day,day_equity=equity)
        if self.s['day_equity'] is not None and equity<=self.s['day_equity']-self.e['max_daily_loss_usd']:self.s['halt_reason']='daily loss limit'
        if any(a['margin'] and a['equity']<=a['margin']*self.e['maintenance_fraction'] for a in accounts):self.s['halt_reason']='simulated margin emergency'
        for p in active:
            long=self.ledger(p['buy'],p['symbol'])['quantity'];short=-self.ledger(p['sell'],p['symbol'])['quantity']
            if self.s['halt_reason'] or (now-p['created']>self.e['max_unhedged_seconds'] and abs(long-short)>1e-10):
                for o in self.pair_orders(p):
                    if o['status']=='SUBMITTED' and not o['reduce_only']:
                        o['status']='CANCELLED';o['events'].append(dict(status='CANCELLED',time=now))
                p['state']='CLOSING';p['reason']=p.get('reason') or self.s['halt_reason'] or 'unhedged timeout'
            pending=any(o['status'] not in TERMINAL for o in self.pair_orders(p))
            if pending:continue
            if abs(long)<1e-12 and abs(short)<1e-12:
                p.update(state='CLOSED' if p['opened'] else 'ABORTED',closed=now)
                self.s['cooldowns'][p['symbol']]=now;p.update(self.pair_pnl(p,books,now))
                self.event('PAIR_FINISHED',now,pair_id=p['id'],state=p['state'],net_pnl=p['net_pnl']);continue
            if long< -1e-10 or short< -1e-10:
                self.s['halt_reason']='reconciliation position direction mismatch';p['state']='RECOVERY_REQUIRED'
            if self.s['halt_reason']:p['state']='CLOSING';p['reason']=self.s['halt_reason']
            if p['state'] in ('OPENING','HEDGING'):
                if now-p['created']>self.e['max_unhedged_seconds'] and abs(long-short)>1e-10:
                    p['state']='CLOSING';p['reason']='unhedged timeout'
                elif abs(long-short)>1e-10:
                    p['state']='HEDGING';v=p['buy'] if long>short else p['sell']
                    if self.reduce(p,v,abs(long-short),books,now,'TRIM'):p['recovery_attempts']+=1
                    if p['recovery_attempts']>=self.e['max_recovery_attempts']:
                        p['state']='CLOSING';p['reason']='recovery attempts exhausted'
                        self.event('PAPER_RECOVERY_CLOSING',now,pair_id=p['id'],symbol=p['symbol'])
                    continue
                else:
                    p.update(state='OPEN',opened=now,quantity=long)
                    self.event('PAIR_HEDGED',now,pair_id=p['id'],quantity=long)
            if p['state']=='OPEN':
                pnl=self.pair_pnl(p,books,now);p.update(pnl)
                conflict=identity_conflict(p['symbol'],p['buy'],p['sell'],books.get((p['buy'],p['symbol']),{}).get('identity'),books.get((p['sell'],p['symbol']),{}).get('identity'))
                if pnl['valuation_status']=='fresh':p['valuation_gap_since']=None
                elif p.get('valuation_gap_since') is None:p['valuation_gap_since']=now
                if conflict or (p.get('valuation_gap_since') is not None and now-p['valuation_gap_since']>=self.e['max_valuation_gap_seconds']):
                    p['state']='CLOSING';p['reason']=conflict or 'valuation unavailable timeout'

                reason='take_profit' if pnl['valuation_status']=='fresh' and pnl['estimated_close_net']>=self.c['take_profit_usd'] else 'stop_loss' if pnl['valuation_status']=='fresh' and pnl['estimated_close_net']<=-self.c['stop_loss_usd'] else 'timeout' if now-p['opened']>=self.c['max_hold_seconds'] else None
                if not reason and not p.get('drill') and pnl['valuation_status']=='fresh' and now-p['opened']>=self.e['break_even_after_seconds'] and pnl['conservative_close_net']>=self.e['break_even_buffer_usd']:
                    reason='break_even_exit'
                if reason=='take_profit' and pnl['conservative_close_net']<self.c['take_profit_usd']:reason=None
                if reason and p['state']=='OPEN':p['state']='CLOSING';p['reason']=reason
            if p['state'] in ('CLOSING','RECOVERY_REQUIRED'):
                submitted=False
                quantities={v:abs(self.ledger(v,p['symbol'])['quantity']) for v in (p['buy'],p['sell'])}
                # Routine exits preserve the hedge while consuming the smaller closing depth.
                if p.get('reason') in ('take_profit','break_even_exit','timeout','manual_drill') and abs(long-short)<1e-10:
                    capacities=[]
                    for v,side in ((p['buy'],'SELL'),(p['sell'],'BUY')):
                        book=books.get((v,p['symbol']))
                        capacities.append(sum(size for _,size in levels(book,side,self.e['slippage_bps'],self.e['depth_participation'])) if self.fresh(book,now) else 0)
                    chunk=min(long,*capacities)
                    quantities={v:chunk for v in quantities}
                    if chunk<=1e-12 and now-p.get('close_wait_since',now)>=self.e['max_valuation_gap_seconds']:
                        p['reason']='close liquidity unavailable timeout'
                    elif chunk<=1e-12:p.setdefault('close_wait_since',now)
                    else:p.pop('close_wait_since',None)
                for v in (p['buy'],p['sell']):submitted=self.reduce(p,v,quantities[v],books,now,'CLOSE') or submitted
                if submitted:p['recovery_attempts']+=1
                if p['recovery_attempts']>=self.e['max_recovery_attempts']:
                    # Partial fills are progress, not a reason to stop the paper account.
                    # Keep fresh-book, reduce-only retries and the original limit rules.
                    if not p.get('retry_warning_recorded'):
                        self.event('PAPER_CLOSE_RETRYING',now,pair_id=p['id'],symbol=p['symbol'],attempts=p['recovery_attempts'])
                        p['retry_warning_recorded']=True
                    p['state']='CLOSING'
        self.reconcile(now)
        self.update_route_pauses(now)
        self.save()
        return self.snapshot(books,now)

    def reconcile(self,now):
        expected={}
        for o in self.s['orders']:
            key=o['venue']+':'+o['symbol'];expected[key]=expected.get(key,0)+o['filled']*(1 if o['side']=='BUY' else -1)
        for key,leg in self.s['ledger'].items():
            if abs(leg['quantity']-expected.get(key,0))>1e-9:
                self.s['halt_reason']='ledger reconciliation mismatch'
                self.event('RECONCILIATION_FAILED',now,key=key)
                return False
        return True

    def targets(self):
        return {(v,p['symbol']) for p in self.s['pairs'] if p['state'] not in ('CLOSED','ABORTED') for v in (p['buy'],p['sell'])}

    def snapshot(self,books=None,now=None):
        now=time.time() if now is None else now;books=self.books if books is None else books
        pairs=[]
        for p in self.s['pairs']:
            view=dict(p)
            if p['state'] not in ('CLOSED','ABORTED'):view.update(self.pair_pnl(p,books,now))
            view['long_quantity']=self.ledger(p['buy'],p['symbol'])['quantity'] if p['state'] not in ('CLOSED','ABORTED') else 0
            view['short_quantity']=-self.ledger(p['sell'],p['symbol'])['quantity'] if p['state'] not in ('CLOSED','ABORTED') else 0
            view['unhedged_quantity']=view['long_quantity']-view['short_quantity'];pairs.append(view)
        accounts=[self.account_view(v,books,now) for v in self.s['accounts']]
        return copy.deepcopy(dict(mode='PAPER_EXECUTION',time=now,halt_reason=self.s['halt_reason'],
            positions=[p for p in pairs if p['state'] not in ('CLOSED','ABORTED')],
            trades=[p for p in pairs if p['state'] in ('CLOSED','ABORTED')][-100:],orders=self.s['orders'][-100:],
            accounts=accounts,events=self.s['events'][-100:],route_stats=self.route_stats(),
            risk_config=dict(min_expected_profit_usd=self.e['min_expected_profit_usd'],break_even_buffer_usd=self.e['break_even_buffer_usd'],route_pause_seconds=self.e['route_pause_seconds']),
            cost_source='configured fee assumptions and funding reserve; not private account rates or settled funding',
            realized_pnl=sum(p['net_pnl'] for p in pairs if p['state'] in ('CLOSED','ABORTED')),
            cash_pnl=sum(a['cash']-a['initial'] for a in accounts),
            equity_pnl=sum(a['equity']-a['initial'] for a in accounts),
            assumptions='SIMULATED IOC fills, lot/minimum rules, margin and funding reserve; no exchange orders'))
