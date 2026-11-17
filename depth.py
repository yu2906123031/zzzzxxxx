"""Shared sizing using protected L2 depth and the execution participation cap."""
import math

def levels(book, side, slippage_bps, participation):
    rows = book['asks' if side == 'BUY' else 'bids']
    if not rows: raise ValueError('empty order book')
    limit = rows[0][0]*(1+(1 if side=='BUY' else -1)*slippage_bps/10000)
    return [(p,s*participation) for p,s in rows if (p<=limit if side=='BUY' else p>=limit)]

def size_pair(a,b,config,requested=None):
    from execution import validate_execution
    from arb import vwap
    e=validate_execution(config.get('execution',{}))
    sides=[levels(book,side,e['slippage_bps'],e['depth_participation']) for book,side in [(a,'BUY'),(b,'SELL'),(a,'SELL'),(b,'BUY')]]
    q=min(sum(s for _,s in rows) for rows in sides)
    if q<=0: raise ValueError('insufficient executable depth')
    q=min(q,config['notional_usd']/max(p for rows in sides for p,_ in rows))
    if requested is not None: q=min(q,requested)
    q=math.floor((q+1e-14)/e['quantity_step'])*e['quantity_step']
    if q<=0: raise ValueError('insufficient executable depth')
    bp,sp=vwap(sides[0],q),vwap(sides[1],q)
    if q*min(bp,sp)<e['min_notional_usd']: raise ValueError('depth below minimum notional')
    return q,bp,sp

def entry_cost(config,buy,sell):
    from execution import validate_execution
    e=validate_execution(config.get('execution',{}))
    return 2*(config['fees_bps'][buy]+config['fees_bps'][sell])+max(config['slippage_buffer_bps'],4*e['slippage_bps'])+config['funding_reserve_bps_per_hour']*config['max_hold_seconds']/3600

def viable(spread,baseline,cost,config):
    return spread-cost>=config['min_net_edge_bps'] and spread-baseline>=config['entry_deviation_bps'] and spread-baseline-cost>=config['min_net_edge_bps']


def expected_profit(q,bp,sp,baseline,cost):
    spread=(sp-bp)/bp*10000
    return q*bp*min(spread-cost,spread-baseline-cost)/10000
