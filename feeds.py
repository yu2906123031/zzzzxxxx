"""Verified public perpetual order books. No credentials or order submission."""
import json
import math
import re
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from functools import lru_cache
from email.utils import parsedate_to_datetime
from market_identity import market_identity

VENUES = {'hyperliquid': 'HyperLiquid', 'lighter': 'Lighter', 'edgex': 'edgeX',
          'robinhood_lighter': 'Robinhood Lighter', 'popdex': 'PopDEX', 'arcus': 'Arcus'}
HL = 'https://api.hyperliquid.xyz/info'
LIGHTER_APIS = {'lighter': 'https://mainnet.zklighter.elliot.ai/api/v1/',
               'robinhood_lighter': 'https://api.rh.lighter.xyz/api/v1/'}
EDGEX = 'https://edgex-prod-v2.edgex.exchange/api/v2/public/'
ARCUS = 'https://api.arcus.xyz/v1/'
POPDEX = 'https://api.popdex.xyz/api/v1/'

@lru_cache(maxsize=1)
def alternate_tls_context():
    # Some Windows CA stores select an expired chain. Try Node's bundled Mozilla
    # roots, keeping certificate and hostname verification enabled.
    roots = subprocess.run(['node', '-p', "require('node:tls').rootCertificates.join('\\n')"],
                           capture_output=True, text=True, check=True, timeout=5).stdout
    return ssl.create_default_context(cadata=roots)

def read_response(response, started):
    raw = json.load(response)
    if isinstance(raw, dict):
        try:
            server_time = parsedate_to_datetime(response.headers['Date']).timestamp()
            age = max(0, float(response.headers.get('Age', '0')))
            if math.isfinite(server_time) and math.isfinite(age):
                raw['_http_clock'] = dict(server_time=server_time, started=started, cache_age=age)
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    return raw

def local_snapshot_time(stamp, raw):
    clock = raw.get('_http_clock')
    if not clock:
        return stamp, 'exchange; HTTP clock unavailable'
    delta = stamp-clock['server_time']
    if delta > 2:
        raise ValueError('snapshot ahead of HTTP server clock')
    # HTTP Date has one-second resolution. Anchor to request start conservatively.
    return clock['started']+min(0,delta)-1-clock['cache_age'], 'exchange aligned to HTTP Date; request-start anchor'

def request(url, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'application/json', 'User-Agent': 'PerpRadar/1.0'})
    started = time.time()
    try:
        response = urllib.request.urlopen(req, timeout=8)
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise
        with urllib.request.urlopen(req, timeout=8, context=alternate_tls_context()) as response:
            return read_response(response, started)
    with response:
        return read_response(response, started)

def normalize(rows, price, size, reverse):
    result = [(float(r[price]), float(r[size])) for r in rows]
    if any(not math.isfinite(p) or not math.isfinite(q) or p <= 0 or q < 0 for p,q in result):
        raise ValueError('invalid book')
    result = sorted(((p,q) for p,q in result if q > 0), reverse=reverse)
    if not result:
        raise ValueError('empty book')
    return result

class Feed:
    def __init__(self):
        self.markets = {}
        self.market_lock = threading.Lock()
        self.catalogs = {}
        self.identities = {}

    @staticmethod
    def unique(matches, venue, symbol):
        if len(matches) != 1:
            raise ValueError(f'missing, inactive or ambiguous {venue} market: {symbol}')
        return matches[0]

    def market(self, venue, symbol):
        if venue in self.catalogs:
            catalog = self.catalogs[venue]
            if symbol not in catalog:
                raise ValueError(f'market not active in latest catalog: {venue}:{symbol}')
            self.identities[(venue, symbol)] = getattr(catalog, 'identities', {}).get(symbol, market_identity(venue, symbol))
            return catalog[symbol]
        key = (venue, symbol)
        with self.market_lock:
            cached = self.markets.get(key)
            if cached and time.monotonic() - cached[0] < 60:
                return cached[1]
        # Network I/O outside the global lock keeps venue failures independent.
        if venue in LIGHTER_APIS:
            raw = request(LIGHTER_APIS[venue] + 'orderBooks')
            if raw.get('code') != 200:
                raise ValueError(venue + ' metadata API error')
            matches = [b for b in raw['order_books'] if b['symbol'] == symbol and
                       b['market_type'] == 'perp' and b['status'] == 'active' and
                       not b.get('is_frozen', False) and float(b.get('multiplier', 1)) == 1]
            value = self.unique(matches, venue, symbol)['market_id']
        elif venue == 'edgex':
            raw = request(EDGEX + 'meta/getMetaData')
            if raw.get('code') != 'SUCCESS':
                raise ValueError('edgeX metadata API error')
            coins = {c['coinId']: c['coinName'] for c in raw['data']['coinList']}
            matches = [c for c in raw['data']['contractList'] if
                       coins.get(c['baseCoinId']) == symbol and coins.get(c['quoteCoinId']) == 'USDC'
                       and c['enableTrade'] and c['enableOpenPosition']]
            matched = self.unique(matches, venue, symbol)
            self.identities[key] = market_identity(venue, symbol, matched)
            value = matched['contractId']
        elif venue == 'arcus':
            raw = request(ARCUS + 'markets?market=' + symbol + '-USD')
            matches = [m for m in raw['markets'] if m['baseAsset'] == symbol and
                       m['quoteAsset'] == 'USD' and m['type'] == 'PERPETUAL' and m['status'] == 'ONLINE']
            matched = self.unique(matches, venue, symbol)
            self.identities[key] = market_identity(venue, symbol, matched)
            value = matched['marketDisplayName']
        elif venue == 'popdex':
            raw = request(POPDEX + 'config/symbol?category=Futures&symbol=' + symbol + 'USDT')
            if str(raw.get('code')) != '200':
                raise ValueError('PopDEX metadata API error')
            m = raw['data']
            matches = [m] if (m['baseToken'] == symbol and m['quoteToken'] == 'USDT' and
                             m['category'] == 'Futures' and m['status'] == 'Trading') else []
            value = self.unique(matches, venue, symbol)['symbol']
        else:
            raise ValueError('unknown venue: ' + venue)
        with self.market_lock:
            self.markets[key] = (time.monotonic(), value)
        return value

    def book(self, venue, symbol):
        if venue not in VENUES:
            raise ValueError('unknown venue: ' + venue)
        if not isinstance(symbol,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',symbol):
            raise ValueError('unsupported symbol: ' + symbol)
        if venue == 'hyperliquid':
            raw = request(HL, {'type': 'l2Book', 'coin': symbol})
            if raw.get('coin') != symbol:
                raise ValueError('Hyperliquid market mismatch')
            bids = normalize(raw['levels'][0], 'px', 'sz', True)
            asks = normalize(raw['levels'][1], 'px', 'sz', False)
            stamp, clock = float(raw['time']) / 1000, 'exchange'
        elif venue in LIGHTER_APIS:
            market = self.market(venue, symbol)
            started = time.time()
            raw = request(LIGHTER_APIS[venue] + f'orderBookOrders?market_id={market}&limit=250')
            if raw.get('code') != 200:
                raise ValueError(venue + ' depth API error')
            bids = normalize(raw['bids'], 'price', 'remaining_base_amount', True)
            asks = normalize(raw['asks'], 'price', 'remaining_base_amount', False)
            stamp, clock = started, 'request-start; exchange timestamp unavailable'
        elif venue == 'edgex':
            market = self.market(venue, symbol)
            started = time.time()
            raw = request(EDGEX + f'quote/getDepth?contractId={market}&level=200')
            if raw.get('code') != 'SUCCESS':
                raise ValueError('edgeX depth API error')
            depth = self.unique([d for d in raw['data'] if str(d['contractId']) == str(market)
                                 and d['depthType'] == 'SNAPSHOT'], venue, symbol)
            bids = normalize(depth['bids'], 'price', 'size', True)
            asks = normalize(depth['asks'], 'price', 'size', False)
            # responseTime is an HTTP response time, not an order book timestamp.
            stamp, clock = started, 'request-start; exchange timestamp unavailable'
        elif venue == 'arcus':
            market = self.market(venue, symbol)
            raw = request(ARCUS + f'l2OrderBook/{market}?nLevels=200')
            bids = normalize(raw['bids'], 0, 1, True)
            asks = normalize(raw['asks'], 0, 1, False)
            stamp, clock = local_snapshot_time(float(raw['timestamp']) / 1_000_000, raw)
        elif venue == 'popdex':
            market = self.market(venue, symbol)
            raw = request(POPDEX + f'public/market/orderbook?category=Futures&symbol={market}&levels=200')
            if str(raw.get('code')) != '200':
                raise ValueError('PopDEX depth API error')
            bids = normalize(raw['data']['bids'], 0, 1, True)
            asks = normalize(raw['data']['asks'], 0, 1, False)
            stamp, clock = local_snapshot_time(float(raw['updatedTime']) / 1000, raw)
        if not math.isfinite(stamp) or stamp <= 0:
            raise ValueError('invalid snapshot timestamp')
        if bids[0][0] >= asks[0][0]:
            raise ValueError('crossed book')
        return dict(bids=bids, asks=asks, time=stamp, received=time.time(), clock=clock,
                    identity=self.identities.get((venue, symbol), market_identity(venue, symbol)))
