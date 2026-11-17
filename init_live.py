"""One INIT mainnet pair: short Aster, long Hyperliquid, <= $100 per leg.

Default is authenticated read-only preflight. --live sends real IOC orders.
Restarting an interrupted --live run reconciles and reduces; never opens again.
"""
import argparse
import concurrent.futures
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from live_venues import Aster, Hyperliquid, VenueError, D, number, wire, floor_step, common_step

ROOT = Path(__file__).resolve().parent
STATE = ROOT/'data'/'init-live.sqlite'
CAP = D('100')
SLIP = D('0.001')
PARTICIPATION = D('0.25')


@contextmanager
def process_lock(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    handle = path.open('a+b')
    handle.seek(0)
    if os.name=='nt':
        import msvcrt
        if path.stat().st_size==0:
            handle.write(b'0');handle.flush();handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            handle.close()
            raise VenueError('another INIT live process holds the lock') from None
    else:
        import fcntl
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise VenueError('another INIT live process holds the lock') from None
    try:
        yield
    finally:
        handle.close()


class Journal:
    def __init__(self,path):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS nonce(id INTEGER PRIMARY KEY,value INTEGER NOT NULL)')
        row = self.db.execute('SELECT value FROM state WHERE id=1').fetchone()
        self.s = json.loads(row[0]) if row else dict(status='NEW',orders=[],created=time.time())

    def save(self):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES(1,?)',(json.dumps(self.s,allow_nan=False),))

    def nonce(self):
        with self.db:
            row = self.db.execute('SELECT value FROM nonce WHERE id=1').fetchone()
            value = max(time.time_ns()//1000,(row[0]+1) if row else 0)
            self.db.execute('INSERT OR REPLACE INTO nonce VALUES(1,?)',(value,))
        return value

    def event(self,status,**fields):
        self.s.update(status=status,**fields)
        self.save()
        print(json.dumps(dict(status=status,**fields)),flush=True)


def sweep(rows,qty):
    left = number(qty);total = D(0)
    for price,size in rows:
        take = min(left,number(size))
        total += take*number(price);left -= take
        if left==0:
            return total/number(qty)
    raise VenueError('insufficient visible depth')


def fresh(books):
    now = time.time()
    if len(books)!=2 or any(not -2 <= now-b['time'] <= 5 for b in books.values()):
        raise VenueError('stale book or incorrect local clock')
    if abs(books['hl']['time']-books['aster']['time'])>3:
        raise VenueError('cross-venue quote skew')


def limit(venue,book,buy):
    best = number(book['asks' if buy else 'bids'][0][0])
    return venue.price(best*(1+SLIP if buy else 1-SLIP),buy)


def plan(venues,books,fees,requested=None):
    fresh(books)
    h,a = books['hl'],books['aster']
    if max(number(h['asks'][0][0]),number(a['asks'][0][0]))/min(number(h['asks'][0][0]),number(a['asks'][0][0]))>D('1.1'):
        raise VenueError('market identity/price discrepancy requires review')
    sides = {}
    for name in venues:
        for buy in (True,False):
            px = limit(venues[name],books[name],buy)
            rows = [(number(p),number(q)*PARTICIPATION) for p,q in books[name]['asks' if buy else 'bids']
                    if (number(p)<=px if buy else number(p)>=px)]
            if not rows:
                raise VenueError('empty protected depth')
            sides[name,buy] = rows
    step = common_step(venues['hl'].step,venues['aster'].step)
    ref = max(number(h['asks'][0][0]),number(a['asks'][0][0]))*(1+SLIP)
    qty = floor_step(min(CAP/ref,*[sum((q for _,q in rows),D(0)) for rows in sides.values()],
                         *[v.max_qty for v in venues.values()]),step)
    if requested is not None:
        qty = floor_step(min(qty,number(requested)),step)
    if qty<=0:
        raise VenueError('no common executable quantity')
    buy,sell = sweep(sides['hl',True],qty),sweep(sides['aster',False],qty)
    for name,price in [('hl',buy),('aster',sell)]:
        if qty<venues[name].min_qty or qty*price<venues[name].minimum:
            raise VenueError('protected size below exchange minimum')
    gross = qty*(sell-buy)
    # Full convergence scenario; both entry/exit fees and 40 BPS execution reserve.
    costs = 2*qty*(buy*fees['hl']+sell*fees['aster']) + qty*(buy+sell)*D('0.002') + CAP*D('0.0002')
    return dict(qty=wire(qty),buy_vwap=wire(buy),sell_vwap=wire(sell),
                long_notional=wire(qty*buy),short_notional=wire(qty*sell),
                convergence_gross_usd=wire(gross),convergence_net_estimate_usd=wire(gross-costs),
                assumption='Exit spread zero, USD/USDT 1:1; funding reserve, not guaranteed profit')


class Runner:
    def __init__(self,journal,venues,fees):
        self.j = journal;self.venues = venues;self.fees = fees

    def books(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(v.book):name for name,v in self.venues.items()}
            books = {futures[f]:f.result() for f in concurrent.futures.as_completed(futures)}
        fresh(books)
        return books

    def positions(self):
        return {name:v.position() for name,v in self.venues.items()}

    def reconcile_order(self,order,seconds=8):
        if 'result' in order:
            return order['result']
        deadline = time.monotonic()+seconds
        while True:
            try:
                result = self.venues[order['venue']].resolve(order)
                if result is not None:
                    if not 0<=number(result['qty'])<=number(order['qty']):
                        raise VenueError('order fill exceeds submitted quantity')
                    order['result'] = result;self.j.save()
                    return result
            except Exception:
                pass # Query failures do not prove order rejection.
            if time.monotonic()>=deadline:
                break
            time.sleep(.5)
        order['unknown'] = True;self.j.save()
        raise VenueError('order result UNKNOWN; no duplicate submission allowed')

    def send(self,name,buy,qty,price,reduce=False):
        qty,price = number(qty),number(price)
        venue = self.venues[name]
        if qty<=0 or floor_step(qty,venue.step)!=qty or price<=0:
            raise VenueError('invalid order precision')
        if not reduce and (qty*price>CAP or qty*price<venue.minimum or qty<venue.min_qty or qty>venue.max_qty):
            raise VenueError('opening order outside notional/quantity bounds')
        if any(o['venue']==name and 'result' not in o for o in self.j.s['orders']):
            raise VenueError('unresolved order blocks new orders on this venue')
        order = dict(id='0x'+uuid.uuid4().hex if name=='hl' else 'init_'+uuid.uuid4().hex,
                     venue=name,buy=buy,qty=wire(qty),price=wire(price),reduce=reduce,created=time.time())
        self.j.s['orders'].append(order)
        self.j.save()  # Persist intent before any network call, including cancellation/timeout cases.
        try:
            result = venue.send(order)
            if result is not None:
                order['result'] = result;self.j.save()
        except Exception:
            # A failed POST may already have executed. Only the client ID is queried.
            order['unknown'] = True;self.j.save()
        return self.reconcile_order(order)

    def expected_positions(self):
        expected = {name:D(0) for name in self.venues}
        for order in self.j.s['orders']:
            if 'result' not in order:
                raise VenueError('unresolved order')
            expected[order['venue']] += number(order['result']['qty'])*(1 if order['buy'] else -1)
        return expected

    def check_positions(self):
        actual = self.positions()
        if actual!=self.expected_positions():
            raise VenueError('account position differs from journal; external trading or delayed state')
        return actual

    def flatten(self):
        self.j.event('RECOVERING')
        for attempt in range(10):
            clean = True
            for name,venue in self.venues.items():
                try:
                    for order in self.j.s['orders']:
                        if order['venue']==name and 'result' not in order:
                            try:
                                venue.cancel(order)
                            except Exception:
                                pass
                            self.reconcile_order(order,seconds=3)
                    qty = venue.position()
                    expected = self.expected_positions_for(name)
                    if qty!=expected:
                        clean = False
                        continue  # Never reduce somebody else's position.
                    if qty:
                        clean = False
                        book = venue.book()
                        if not -2 <= time.time()-book['time'] <= 5:
                            continue
                        self.send(name,qty<0,abs(qty),limit(venue,book,qty<0),reduce=True)
                except Exception:
                    clean = False
            if clean:
                self.j.event('CLOSED',cash_pnl_excluding_funding=wire(self.cash_pnl()))
                return True
            time.sleep(1)
        self.j.event('RECOVERY_REQUIRED',detail='Exposure or unresolved order remains. Re-run --live to reconcile and reduce only.')
        return False

    def expected_positions_for(self,name):
        orders = [o for o in self.j.s['orders'] if o['venue']==name]
        if any('result' not in o for o in orders):
            raise VenueError('unknown order on venue')
        return sum((number(o['result']['qty'])*(1 if o['buy'] else -1) for o in orders),D(0))

    def cash_pnl(self):
        return sum((number(o['result']['notional'])*(-1 if o['buy'] else 1)-number(o['result']['fee'])
                    for o in self.j.s['orders'] if 'result' in o),D(0))

    def open_pair(self,p):
        self.j.event('OPENING',started=time.time())
        # Trade thinner Aster first. Hedge only its confirmed executed quantity.
        books = self.books()
        quote = plan(self.venues,books,self.fees,requested=p['qty'])
        if number(quote['convergence_net_estimate_usd'])<D('0.10'):
            raise VenueError('edge disappeared before first leg')
        qty = number(quote['qty'])
        short = self.send('aster',False,qty,limit(self.venues['aster'],books['aster'],False))
        filled = number(short['qty'])
        if not filled:
            raise VenueError('Aster IOC did not fill')
        hedge = floor_step(filled,self.venues['hl'].step)
        # If a partial fill is too small to hedge safely, reduce the Aster leg.
        books = self.books()
        price = limit(self.venues['hl'],books['hl'],True)
        if hedge*price<self.venues['hl'].minimum or hedge*price>CAP:
            raise VenueError('partial fill cannot be hedged within bounds')
        # Re-price using actual short proceeds. Do not chase a vanished spread.
        if number(short['notional'])/filled<=price*(1+2*(self.fees['hl']+self.fees['aster'])+D('0.004')):
            raise VenueError('hedge spread disappeared; reducing first leg')
        self.send('hl',True,hedge,price)
        actual = self.check_positions()
        if actual['hl']<=0 or actual['hl']!=-actual['aster']:
            raise VenueError('partial or mismatched hedge; flatten both legs')
        self.j.event('OPEN',opened=time.time(),quantity=wire(actual['hl']))

    def close_quote(self,positions,books):
        fresh(books)
        close_cash = D(0)
        for name,q in positions.items():
            if not q:
                continue
            buy = q<0
            px_limit = limit(self.venues[name],books[name],buy)
            rows = [(number(p),number(s)*PARTICIPATION) for p,s in books[name]['asks' if buy else 'bids']
                    if (number(p)<=px_limit if buy else number(p)>=px_limit)]
            px = sweep(rows,abs(q))
            # Account for current close VWAP, taker fees and execution/funding reserves.
            close_cash += q*px-abs(q)*px*(self.fees[name]+SLIP)
        return self.cash_pnl()+close_cash-CAP*D('0.0002')

    def monitor(self):
        deadline = time.monotonic()+60
        while time.monotonic()<deadline:
            positions = self.check_positions()
            if positions['hl']<=0 or positions['hl']!=-positions['aster']:
                raise VenueError('hedge imbalance detected')
            books = self.books()
            estimate = self.close_quote(positions,books)
            self.j.event('OPEN',close_pnl_estimate=wire(estimate))
            if estimate>=D('0.10') or estimate<=D('-1'):
                break
            time.sleep(2)


def credentials():
    load_dotenv(ROOT/'.env.init-live',override=False)
    required = ['HL_ACCOUNT_ADDRESS','HL_API_PRIVATE_KEY','ASTER_USER_ADDRESS','ASTER_API_PRIVATE_KEY']
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise VenueError('Missing local configuration: '+', '.join(missing))
    return {name:os.environ[name] for name in required}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Send real orders for one trial; recover only if interrupted')
    parser.add_argument('--wait-seconds',type=int,default=120)
    parser.add_argument('--public-check',action='store_true',help='Public V3 connectivity check; no credentials required')
    parser.add_argument('--balances',action='store_true',help='Read both account balances without requiring trial collateral')
    args = parser.parse_args()
    if args.balances and args.live:
        parser.error('--balances cannot be combined with --live')
    if not 30<=args.wait_seconds<=600:
        parser.error('--wait-seconds must be between 30 and 600')
    if args.public_check:
        from live_venues import http, ASTER_URL
        info = http('GET',ASTER_URL+'/fapi/v3/exchangeInfo')
        print(json.dumps(dict(status='PUBLIC_OK',init_available=any(m['symbol']=='INITUSDT' for m in info['symbols']))))
        return 0
    creds = credentials()
    with process_lock(STATE.with_suffix('.lock')):
        j = Journal(STATE)
        runner = None
        try:
            venues = {'hl':Hyperliquid(creds['HL_ACCOUNT_ADDRESS'],creds['HL_API_PRIVATE_KEY']),
                      'aster':Aster(creds['ASTER_USER_ADDRESS'],creds['ASTER_API_PRIVATE_KEY'],j)}
            owners = {'hl':venues['hl'].user.lower(),'aster':venues['aster'].user.lower()}
            if j.s.get('owners',owners)!=owners and j.s['orders']:
                raise VenueError('account differs from persisted journal')
            j.s['owners'] = owners;j.save()
            if args.balances:
                results = {}
                for name,v in venues.items():
                    try:
                        if name=='hl':
                            results[name] = v.balance()
                        else:
                            account = v.signed('GET','account')
                            results[name] = dict(currency='USDT',wallet_balance=wire(account['totalWalletBalance']),
                                                 account_equity=wire(account['totalMarginBalance']),
                                                 available_collateral=wire(account['availableBalance']))
                        results[name]['meets_102_collateral_requirement'] = number(results[name]['available_collateral'])>=CAP+D('2')
                    except Exception as exc:
                        results[name] = dict(error=str(exc) if isinstance(exc,VenueError) else type(exc).__name__)
                print(json.dumps(dict(mode='READ_ONLY_BALANCES',accounts=results)),flush=True)
                return 1 if any('error' in r for r in results.values()) else 0
            runner = Runner(j,venues,{})
            if j.s['orders']:
                if not args.live:
                    print(json.dumps(dict(status=j.s['status'],detail='Existing trial. --live reconciles/reduces only.')))
                    return 0
                return 0 if runner.flatten() else 2
            balances = {};fees = {}
            for name,v in venues.items():
                balances[name],fees[name] = v.preflight()
                if not D(0)<=fees[name]<=D('0.01'):
                    raise VenueError('invalid account fee rate')
                if balances[name]<CAP+D('2'):
                    raise VenueError(f'{name} needs >= $102 available collateral for 1x trial')
            runner.fees = fees
            quote = plan(venues,runner.books(),fees)
            j.event('PREFLIGHT_OK',quote=quote,fees={n:wire(f) for n,f in fees.items()})
            if not args.live:
                return 0
            for v in venues.values():
                v.leverage_one()
            until = time.monotonic()+args.wait_seconds
            since = None
            while time.monotonic()<until:
                try:
                    quote = plan(venues,runner.books(),fees)
                    if number(quote['convergence_net_estimate_usd'])<D('0.10'):
                        since = None
                    else:
                        since = time.monotonic() if since is None else since
                    j.event('WAITING',quote=quote)
                    if since is not None and time.monotonic()-since>=30:
                        for v in venues.values():
                            v.preflight()  # Reject positions/orders added during confirmation.
                        runner.open_pair(quote)
                        runner.monitor()
                        return 0 if runner.flatten() else 2
                except VenueError:
                    if j.s['orders']:
                        raise
                    since = None
                time.sleep(2)
            j.event('NO_ENTRY')
            return 0
        except (Exception,KeyboardInterrupt) as exc:
            # SDK exceptions may contain signed payloads; never print raw exception text.
            reason = str(exc) if isinstance(exc,VenueError) else type(exc).__name__
            j.event('BLOCKED',reason=reason)
            if runner and args.live and j.s['orders']:
                return 2 if not runner.flatten() else 1
            return 1
        finally:
            j.db.close()


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except VenueError as exc:
        print(json.dumps(dict(status='BLOCKED',reason=str(exc))),flush=True)
        raise SystemExit(1)
