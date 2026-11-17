"""Discover active perpetual markets from public venue metadata."""
import re
import time
from collections import Counter, defaultdict
from feeds import request, EDGEX, ARCUS, POPDEX, LIGHTER_APIS
from market_identity import MarketCatalog, market_identity, catalog_conflicts

SYMBOL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')

def discover(venue):
    pairs = []
    identities = {}
    if venue == 'edgex':
        raw = request(EDGEX + 'meta/getMetaData')
        if raw.get('code') != 'SUCCESS': raise ValueError('edgeX metadata error')
        coins = {c['coinId']:c['coinName'] for c in raw['data']['coinList']}
        for m in raw['data']['contractList']:
            if coins.get(m['quoteCoinId']) == 'USDC' and m['enableTrade'] and m['enableOpenPosition']:
                pairs.append((coins.get(m['baseCoinId']),m['contractId']))
                identities[coins.get(m['baseCoinId'])] = market_identity(venue, coins.get(m['baseCoinId']), m)
    elif venue == 'robinhood_lighter':
        raw = request(LIGHTER_APIS[venue] + 'orderBooks')
        if raw.get('code') != 200: raise ValueError('Robinhood Lighter metadata error')
        for m in raw['order_books']:
            if m['market_type']=='perp' and m['status']=='active' and not m.get('is_frozen',False) and float(m.get('multiplier',1))==1:
                pairs.append((m['symbol'],m['market_id']))
    elif venue == 'popdex':
        raw = request(POPDEX + 'config/symbols?category=Futures')
        if str(raw.get('code'))!='200': raise ValueError('PopDEX metadata error')
        for m in raw['data']:
            if m['category']=='Futures' and m['status']=='Trading' and m['quoteToken']=='USDT':
                pairs.append((m['baseToken'],m['symbol']))
    elif venue == 'arcus':
        raw = request(ARCUS + 'markets')
        for m in raw['markets']:
            if m['type']=='PERPETUAL' and m['status']=='ONLINE' and m['quoteAsset']=='USD':
                pairs.append((m['baseAsset'],m['marketDisplayName']))
                identities[m['baseAsset']] = market_identity(venue, m['baseAsset'], m)
    else:
        raise ValueError('Discovery unsupported: '+venue)
    counts = Counter(s for s,_ in pairs)
    return MarketCatalog({s:mid for s,mid in pairs if isinstance(s,str) and SYMBOL.fullmatch(s) and counts[s]==1}, identities)

def universe(catalogs):
    conflicts = catalog_conflicts(catalogs)
    venues = defaultdict(list)
    for venue, markets in catalogs.items():
        for symbol in markets:
            venues[symbol].append(venue)
    return {s:vs for s,vs in sorted(venues.items()) if len(vs)>=2 and s not in conflicts}
