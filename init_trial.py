"""INIT Hyperliquid/Aster trial: public quotes and explicit paper execution only.

No credentials, signatures or real order endpoints are used.
"""
import argparse
import concurrent.futures
import json
import math
import time
from pathlib import Path

from arb import vwap
from depth import entry_cost, expected_profit, size_pair
from execution import PaperExecutor
from feeds import Feed, normalize, request

ROOT = Path(__file__).resolve().parent
ASTER = 'https://fapi.asterdex.com/fapi/v1/'
TARGETS = [('hyperliquid', 'INIT'), ('aster', 'INIT')]


class TrialFeed:
    def __init__(self):
        self.feed = Feed()
        self.checked = False

    def verify(self):
        hl = request('https://api.hyperliquid.xyz/info', {'type': 'meta'})
        matches = [m for m in hl['universe'] if m['name'] == 'INIT' and not m.get('isDelisted', False)]
        if len(matches) != 1:
            raise ValueError('INIT is missing, delisted or ambiguous on Hyperliquid')
        raw = request(ASTER + 'exchangeInfo')
        matches = [m for m in raw['symbols'] if m['symbol'] == 'INITUSDT'
                   and m['baseAsset'] == 'INIT' and m['quoteAsset'] == 'USDT'
                   and m['status'] == 'TRADING' and m['contractType'] == 'PERPETUAL']
        if len(matches) != 1:
            raise ValueError('INITUSDT perpetual is unavailable on Aster')
        self.checked = True

    def book(self, venue, symbol):
        if not self.checked or (venue, symbol) not in TARGETS:
            raise ValueError('unverified trial market')
        if venue == 'hyperliquid':
            return self.feed.book(venue, symbol)
        raw = request(ASTER + 'depth?symbol=INITUSDT&limit=1000')
        bids = normalize(raw['bids'], 0, 1, True)
        asks = normalize(raw['asks'], 0, 1, False)
        stamp = float(raw['T']) / 1000
        if not math.isfinite(stamp) or stamp <= 0 or bids[0][0] >= asks[0][0]:
            raise ValueError('invalid Aster snapshot')
        return dict(bids=bids, asks=asks, time=stamp, received=time.time(), clock='exchange')

    def books(self, pool):
        jobs = {pool.submit(self.book, *key): key for key in TARGETS}
        return {jobs[f]: f.result() for f in concurrent.futures.as_completed(jobs)}


def evaluate(books, config, now=None):
    now = time.time() if now is None else now
    a, b = (books[key] for key in TARGETS)
    if any(not -2 <= now - x['time'] <= config['max_quote_age_seconds'] for x in (a, b)):
        raise ValueError('stale quote or local clock mismatch')
    if abs(a['time'] - b['time']) > config['max_quote_skew_seconds']:
        raise ValueError('quote timestamps are too far apart')
    if max(a['asks'][0][0], b['asks'][0][0]) / min(a['asks'][0][0], b['asks'][0][0]) > 1.1:
        raise ValueError('price discrepancy requires market identity review')
    q, buy, sell = size_pair(a, b, config)
    cost = entry_cost(config, 'hyperliquid', 'aster')
    return dict(quantity=q, long_notional=q*buy, short_notional=q*sell,
                buy_vwap=buy, sell_vwap=sell, entry_spread_bps=(sell-buy)/buy*10000,
                convergence_gross_usd=q*(sell-buy),
                convergence_net_estimate_usd=expected_profit(q,buy,sell,0,cost),
                immediate_roundtrip_gross_usd=q*(vwap(a['bids'],q)-buy+sell-vwap(b['asks'],q)),
                cost_reserve_bps=cost,
                assumption='Exit spread converges to zero; fees are CLI assumptions; USD/USDT valued 1:1')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--paper', action='store_true', help='Allow one simulated pair; never real orders')
    parser.add_argument('--hl-fee-bps', type=float, required=True)
    parser.add_argument('--aster-fee-bps', type=float, required=True)
    parser.add_argument('--seconds', type=float, default=120)
    args = parser.parse_args()
    if any(not math.isfinite(x) or x < 0 for x in (args.hl_fee_bps,args.aster_fee_bps)) or not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error('fees must be finite/nonnegative; seconds must be finite/positive')
    config = json.loads((ROOT/'config.json').read_text(encoding='utf-8-sig'))
    config.update(symbols=['INIT'],venues=['hyperliquid','aster'],notional_usd=100,
                  max_positions=1,max_total_notional_usd=200,max_hold_seconds=60,
                  take_profit_usd=0.1,stop_loss_usd=1)
    config['fees_bps'] = {'hyperliquid': args.hl_fee_bps,'aster': args.aster_fee_bps}
    # Both legs stay <= $100; four-sided protected depth may reduce the size.
    config['execution'] = {**config.get('execution',{}),'slippage_bps':10,'depth_participation':0.25}
    folder = ROOT/'output'/'init-trial'/str(time.time_ns())
    folder.mkdir(parents=True)
    report = dict(mode='PAPER_EXECUTION' if args.paper else 'PUBLIC_PREFLIGHT',
                  real_orders_sent=False,config=config,status='STARTING')
    executor = PaperExecutor(config,str(folder/'paper.sqlite')) if args.paper else None
    feed = TrialFeed()
    pair_id = None
    since = None
    deadline = time.monotonic()+args.seconds
    try:
        feed.verify()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            while time.monotonic() < deadline:
                try:
                    books = feed.books(pool)
                    now = time.time()
                    if executor:
                        executor.step(books,now)
                        report['execution'] = executor.snapshot(books,now)
                    quote = evaluate(books,config,now)
                    report.update(quote=quote,status='QUOTED',last_error=None)
                    print(json.dumps(quote),flush=True)
                    if not executor:
                        break
                    eligible = quote['convergence_net_estimate_usd'] >= config['execution']['min_expected_profit_usd']
                    since = (since if since is not None else time.monotonic()) if eligible else None
                    if pair_id is None and since is not None and time.monotonic()-since >= config['confirm_seconds']:
                        pair_id = executor.submit(dict(symbol='INIT',buy='hyperliquid',sell='aster',quantity=quote['quantity'],baseline=0),books,now)
                    if pair_id:
                        pair = next(p for p in executor.s['pairs'] if p['id']==pair_id)
                        if pair['state'] in ('CLOSED','ABORTED'):
                            report['status'] = pair['state']
                            break
                        if time.monotonic() >= deadline-20:
                            executor.request_close(pair_id,'trial_deadline')
                except Exception as exc:
                    since = None
                    report.update(status='BLOCKED',last_error=str(exc))
                    print(json.dumps(dict(status='BLOCKED',error=str(exc))),flush=True)
                    if not executor:
                        break
                (folder/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
                time.sleep(2)
        if executor:
            report['execution'] = executor.snapshot()
            active = [p for p in executor.s['pairs'] if p['state'] not in ('CLOSED','ABORTED')]
            report['status'] = 'INCOMPLETE_PAPER_POSITION' if active else ('NO_ENTRY' if pair_id is None else report['status'])
    except Exception as exc:
        report.update(status='BLOCKED',last_error=str(exc))
    finally:
        (folder/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        if executor:
            executor.db.close()
        print(json.dumps(dict(report=str(folder/'report.json'),status=report['status'],error=report.get('last_error'))),flush=True)
    return 0 if report['status'] in ('QUOTED','CLOSED','ABORTED','NO_ENTRY') else 1


if __name__ == '__main__':
    raise SystemExit(main())
