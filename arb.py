"""Public perpetual radar and paper trading. Never signs or submits orders."""
import argparse
import concurrent.futures
import json
import math
from itertools import permutations
from feeds import Feed, VENUES, normalize
from discovery import discover, universe, SYMBOL
from market_identity import identity_conflict, catalog_conflicts
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
def vwap(levels, quantity):
    if quantity <= 0 or not math.isfinite(quantity):
        raise ValueError('invalid quantity')
    remaining, cost = quantity, 0.0
    for price, size in levels:
        take = min(remaining, size)
        cost += take * price
        remaining -= take
        if remaining <= quantity * 1e-10:
            return cost / quantity
    raise ValueError('insufficient depth')

def healthy(a,b,now,c):
    return all(-2<=now-book['time']<=c['max_quote_age_seconds'] for book in (a,b)) and abs(a['time']-b['time'])<=c['max_quote_skew_seconds']

def liquidation(p,a,b,c,now):
    q = p['quantity']
    long_exit, short_exit = vwap(a['bids'],q), vwap(b['asks'],q)
    gross = q*(long_exit-p['buy_price']+p['sell_price']-short_exit)
    fees = p['open_fees']+q*(long_exit*c['fees_bps'][p['buy']]+short_exit*c['fees_bps'][p['sell']])/10000
    hours = max(0,now-p['opened'])/3600
    reserve = p['notional']*(c['slippage_buffer_bps']+hours*c['funding_reserve_bps_per_hour'])/10000
    return dict(gross_pnl=gross,fees=fees,reserve=reserve,net_pnl=gross-fees-reserve,long_exit=long_exit,short_exit=short_exit)

class Engine:
    def __init__(self,c,database):
        self.c = c
        self.db = sqlite3.connect(database)
        self.db.execute('CREATE TABLE IF NOT EXISTS samples (key TEXT, ts REAL, spread REAL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS samples_key_ts ON samples(key,ts)')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, value TEXT)')
        row = self.db.execute('SELECT value FROM state WHERE id=1').fetchone()
        self.state = json.loads(row[0]) if row else {'positions':[],'trades':[],'cooldowns':{}}
        self.pending = {}
        self.execution = None

    def tick(self,books,errors,now=None,symbols=None):
        now = time.time() if now is None else now
        c = self.c
        self.db.execute('DELETE FROM samples WHERE ts < ?', (now-c['history_seconds'],))
        remaining = []
        for p in self.state['positions']:
            if symbols is not None and p['symbol'] not in symbols:
                remaining.append(p)
                continue
            a,b = books.get((p['buy'],p['symbol'])),books.get((p['sell'],p['symbol']))
            p['valuation_status'] = 'stale / awaiting depth'
            if a and b and healthy(a,b,now,c):
                try:
                    pnl = liquidation(p,a,b,c,now)
                    p.update(pnl,valuation_status='fresh')
                    reason = 'take_profit' if pnl['net_pnl']>=c['take_profit_usd'] else 'stop_loss' if pnl['net_pnl']<=-c['stop_loss_usd'] else 'timeout' if now-p['opened']>=c['max_hold_seconds'] else None
                    if reason:
                        self.state['trades'].append(dict(p,closed=now,reason=reason))
                        self.state['cooldowns'][p['symbol']] = now
                        continue
                except ValueError:
                    pass
            remaining.append(p)
        self.state['positions'] = remaining
        rows = []
        for symbol in (c['symbols'] if symbols is None else [s for s in symbols if s in c['symbols']]):
            for buy,sell in permutations(c.get('symbol_venues', {}).get(symbol, c.get('venues', ['edgex','robinhood_lighter','popdex','arcus'])), 2):
                key = f'{symbol}:{buy}:{sell}'
                row = dict(key=key,symbol=symbol,buy=buy,sell=sell,status='UNAVAILABLE',sample_time=now)
                rows.append(row)
                a,b = books.get((buy,symbol)),books.get((sell,symbol))
                conflict = identity_conflict(symbol, buy, sell, (a or {}).get('identity'), (b or {}).get('identity'))
                if conflict:
                    row.update(error=conflict, error_code='ASSET_IDENTITY')
                    self.pending.pop(key,None)
                    continue
                if a and b:
                    row['quote_time'] = min(a['time'],b['time'])
                if not a or not b or not healthy(a,b,now,c):
                    if a and b:
                        row['error'] = f"quote timing: buy_age={now-a['time']:.1f}s sell_age={now-b['time']:.1f}s skew={abs(a['time']-b['time']):.1f}s"
                        row['error_code'] = 'QUOTE_TIMING'
                    self.pending.pop(key,None)
                    continue
                try:
                    from depth import size_pair, entry_cost, viable, expected_profit
                    from execution import validate_execution
                    if max(a['asks'][0][0],b['asks'][0][0])/min(a['asks'][0][0],b['asks'][0][0])>validate_execution(c.get('execution',{}))['max_price_ratio']:
                        raise ValueError('cross-venue price discrepancy requires identity review')
                    q,bp,sp = size_pair(a,b,c)
                    spread = (sp-bp)/bp*10000
                    baseline,count,oldest = self.db.execute('SELECT AVG(spread),COUNT(*),MIN(ts) FROM samples WHERE key=?',(key,)).fetchone()
                    deviation = None if baseline is None else spread-baseline
                    cost = entry_cost(c,buy,sell)
                    edge = None if deviation is None else deviation-cost
                    ready = count>=c['min_samples'] and oldest is not None and now-oldest>=c['warmup_seconds']
                    profit = None if baseline is None else expected_profit(q,bp,sp,baseline,cost)
                    qualified = ready and viable(spread,baseline,cost,c) and profit>=validate_execution(c.get('execution',{}))['min_expected_profit_usd']
                    if qualified:
                        prev = self.pending.get(key)
                        since = prev[0] if prev and now-prev[1]<=c['poll_seconds']*3 else now
                        self.pending[key] = (since,now)
                    else:
                        self.pending.pop(key,None)
                    duration = now-self.pending[key][0] if key in self.pending else 0
                    status = 'WARMUP' if not ready else 'TRIGGERED' if qualified and duration>=c['confirm_seconds'] else 'PENDING' if qualified else 'IDLE'
                    row.update(expected_profit_usd=profit,notional_buy=q*bp,notional_sell=q*sp,status=status,spread=spread,baseline=baseline,deviation=deviation,estimated_edge=edge,cost_bps=cost,duration=duration,samples=count,history_hours=0 if oldest is None else (now-oldest)/3600,buy_price=bp,sell_price=sp,quantity=q)
                    self.db.execute('INSERT INTO samples VALUES (?,?,?)',(key,now,spread))
                    positions = self.state['positions']
                    occupied = any(p['symbol']==symbol for p in positions)
                    exposure = sum(p['quantity']*(p['buy_price']+p['sell_price']) for p in positions)
                    cooldown = now-self.state['cooldowns'].get(symbol,0)<c['cooldown_seconds']
                    if self.execution and status in ('PENDING','TRIGGERED'):
                        self.execution.enqueue(row)
                    if not self.c.get('execution',{}).get('enabled',False) and status=='TRIGGERED' and not occupied and not cooldown and len(positions)<c['max_positions'] and exposure+q*(bp+sp)<=c['max_total_notional_usd']:
                        positions.append(dict(symbol=symbol,buy=buy,sell=sell,quantity=q,buy_price=bp,sell_price=sp,notional=q*bp,open_fees=q*(bp*c['fees_bps'][buy]+sp*c['fees_bps'][sell])/10000,opened=now,valuation_status='awaiting next snapshot'))
                        self.pending.pop(key,None)
                except (ValueError,KeyError) as exc:
                    row['error'] = str(exc)
                    self.pending.pop(key,None)
        self.db.execute('INSERT OR REPLACE INTO state VALUES (1,?)',(json.dumps(self.state,allow_nan=False),))
        self.db.commit()
        return dict(time=now,mode='PAPER ONLY',rows=rows,errors=errors,positions=self.state['positions'],trades=self.state['trades'][-100:],realized_pnl=sum(t['net_pnl'] for t in self.state['trades']),config=c,venue_names=VENUES)

def validate(c):
    venues = c.setdefault('venues', ['edgex','robinhood_lighter','popdex','arcus'])
    if not isinstance(venues,list) or len(venues)<2 or any(not isinstance(v,str) or v not in VENUES for v in venues) or len(set(venues))!=len(venues):
        raise ValueError('invalid or duplicate venues (at least two required)')
    if c['symbols'] != 'ALL' and (not isinstance(c['symbols'],list) or not c['symbols'] or any(not isinstance(s,str) or not SYMBOL.fullmatch(s) for s in c['symbols']) or len(set(c['symbols']))!=len(c['symbols'])):
        raise ValueError('invalid or duplicate symbols')
    for k,v in c.items():
        if k in ('symbols','fees_bps','venues','execution'):
            continue
        if not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) or v<=0:
            raise ValueError('config must be positive: '+k)
    for venue in venues:
        value = c['fees_bps'].get(venue)
        if not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value) or value<0:
            raise ValueError('invalid or missing fee: '+venue)
    if 'execution' in c:
        from execution import validate_execution
        validate_execution(c['execution'])
    if c['warmup_seconds']>=c['history_seconds']:
        raise ValueError('warmup must be shorter than history')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--port',type=int,default=8787)
    parser.add_argument('--config',default=str(ROOT/'config.json'))
    parser.add_argument('--database',default=str(ROOT/'data/radar.sqlite'))
    parser.add_argument('--execution-database',default=None)
    args = parser.parse_args()
    c = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    validate(c)
    Path(args.database).parent.mkdir(parents=True,exist_ok=True)
    all_symbols = c['symbols'] == 'ALL'
    if all_symbols:
        c['symbols'] = []
    engine,feed = Engine(c,args.database),Feed()
    runtime = None
    if c.get('execution',{}).get('enabled',False):
        if engine.state['positions']:
            raise RuntimeError('legacy paper positions exist; migrate before enabling order execution')
        from execution_runtime import ExecutionRuntime
        execution_database = args.execution_database or str(Path(args.database).with_suffix('.execution.sqlite'))
        runtime = ExecutionRuntime(c,execution_database,feed)
        engine.execution = runtime
    discovery_errors = {}
    next_discovery = 0
    row_cache = {}
    last_cycle_seconds = None
    snapshot = dict(mode='PAPER ONLY',rows=[],positions=[],trades=[],errors={},time=0,realized_pnl=0)
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path=='/api/state':
                with lock:
                    response = dict(snapshot)
                    if runtime:
                        execution = runtime.snapshot()
                        response.update(execution=execution,positions=execution['positions'],trades=execution['trades'],realized_pnl=execution['realized_pnl'])
                        fast_rows={r['key']:r for r in execution.get('watch_rows',[])}
                        response['rows']=[fast_rows.get(r['key'],r) for r in response['rows']]
                    drill_path = ROOT/'output/execution-drill/report.json'
                    if drill_path.exists():
                        try:
                            response['execution_drill'] = json.loads(drill_path.read_text(encoding='utf-8'))
                        except (OSError,ValueError):
                            pass
                    body = json.dumps(response,allow_nan=False).encode()
                content = 'application/json; charset=utf-8'
            elif self.path in ('/','/index.html'):
                body = (ROOT/'web/index.html').read_bytes()
                content = 'text/html; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type',content)
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            self.wfile.write(body)
        def log_message(self,*_):
            pass
    server = None
    if not args.once:
        server = ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        print(f'PAPER ONLY: http://127.0.0.1:{args.port}',flush=True)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            while True:
                start = time.monotonic()
                if all_symbols and time.monotonic() >= next_discovery:
                    jobs = {pool.submit(discover,v):v for v in c['venues']}
                    catalogs, discovery_errors = {}, {}
                    for job in concurrent.futures.as_completed(jobs):
                        venue = jobs[job]
                        try:
                            catalogs[venue] = job.result()
                        except Exception as exc:
                            catalogs[venue] = {}
                            discovery_errors['discovery:'+venue] = str(exc)
                    feed.catalogs = catalogs
                    discovery_errors.update({'identity:'+s:reason for s,reason in catalog_conflicts(catalogs).items()})
                    c['symbol_venues'] = universe(catalogs)
                    c['symbols'] = list(c['symbol_venues'])
                    next_discovery = time.monotonic()+60
                targets = {(v,s) for s in c['symbols'] for v in c.get('symbol_venues',{}).get(s,c['venues'])}
                # Keep valuing existing paper positions when a venue is disabled.
                for p in engine.state['positions']:
                    for v in (p['buy'],p['sell']):
                        if v not in c['fees_bps']:
                            raise ValueError('keep fee for open position venue: '+v)
                        targets.add((v,p['symbol']))
                futures = {pool.submit(feed.book,v,s):(v,s) for v,s in sorted(targets,key=lambda x:(x[1],x[0]))}
                outstanding = {}
                for v,symbol in targets:
                    outstanding[symbol] = outstanding.get(symbol,0)+1
                allowed = {f'{s}:{a}:{b}' for s in c['symbols'] for a,b in permutations(c.get('symbol_venues',{}).get(s,c['venues']),2)}
                row_cache = {k:r for k,r in row_cache.items() if k in allowed}
                books,errors = {},dict(discovery_errors)
                for future in concurrent.futures.as_completed(futures):
                    key = futures[future]
                    try:
                        books[key] = future.result()
                    except Exception as exc:
                        errors[':'.join(key)] = f'{type(exc).__name__}: {exc}'
                    outstanding[key[1]] -= 1
                    if outstanding[key[1]] == 0:
                        result = engine.tick(books,errors,symbols=[key[1]])
                        row_cache.update({r['key']:r for r in result['rows'] if r['key'] in allowed})
                        result['rows'] = list(row_cache.values())
                        result['market_coverage'] = {v:len(ms) for v,ms in feed.catalogs.items()}
                        result['symbol_mode'] = 'ALL' if all_symbols else 'SELECTED'
                        result['last_cycle_seconds'] = last_cycle_seconds
                        with lock:
                            snapshot = json.loads(json.dumps(result))
                if not futures:
                    result = engine.tick({},errors)
                    with lock:
                        snapshot = json.loads(json.dumps(result))
                last_cycle_seconds = time.monotonic()-start
                if args.once:
                    print(json.dumps(snapshot,ensure_ascii=False,indent=2))
                    break
                time.sleep(max(0.1,c['poll_seconds']-(time.monotonic()-start)))
    except KeyboardInterrupt:
        pass
    finally:
        if runtime:
            runtime.close()
        engine.db.close()
        if server:
            server.shutdown()

if __name__=='__main__':
    main()

