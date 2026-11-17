"""Asset-class checks and reviewed identities; a ticker is not an asset ID."""

# Verified 2026-09-29: Arcus /v1/markets; edgeX metadata and Quant news.
REVIEWED = {
    ('arcus', 'QNT'): dict(asset_class='EQUITIES', underlying='equity:quantinuum', name='Quantinuum'),
    ('edgex', 'QNT'): dict(asset_class='CRYPTO', underlying='crypto:quant', name='Quant'),
}

class MarketCatalog(dict):
    def __init__(self, markets, identities):
        super().__init__(markets)
        self.identities = identities

def market_identity(venue, symbol, metadata=None):
    m = metadata or {}
    identity = dict(REVIEWED.get((venue, symbol), {}))
    if venue == 'arcus' and m.get('category'):
        identity['asset_class'] = m['category'].upper()
    elif venue == 'edgex':
        if m.get('isStock') is True:
            identity['asset_class'] = 'EQUITIES'
        elif m.get('isFx') is True:
            identity['asset_class'] = 'FX'
        # False stock/FX flags alone do not prove crypto (e.g. commodities).
    if m.get('fullAssetName'):
        identity['name'] = m['fullAssetName']
    return identity

def identity_conflict(symbol, buy, sell, a=None, b=None):
    left = {**market_identity(buy, symbol), **(a or {})}
    right = {**market_identity(sell, symbol), **(b or {})}
    x, y = left.get('underlying'), right.get('underlying')
    if x and y and x != y:
        return f'underlying mismatch: {buy}={x}, {sell}={y}'
    if symbol == 'QNT' and (not x or not y):
        return f'ambiguous QNT underlying: {buy}/{sell}; identity review required'
    if any(i.get('asset_class') not in (None, 'CRYPTO') for i in (left,right)) and not (x and y and x==y):
        return 'non-crypto underlying requires explicit matching identities'
    x, y = left.get('asset_class'), right.get('asset_class')
    if x and y and x != y:
        return f'asset class mismatch: {buy}={x}, {sell}={y}'
    if (x and x != 'CRYPTO' and not y) or (y and y != 'CRYPTO' and not x):
        return f'unverified non-crypto underlying: {buy}={x}, {sell}={y}'
    return None

def catalog_conflicts(catalogs):
    from itertools import combinations
    symbols = set().union(*(set(c) for c in catalogs.values())) if catalogs else set()
    conflicts = {}
    for symbol in sorted(symbols):
        venues = [v for v, c in catalogs.items() if symbol in c]
        for buy, sell in combinations(venues, 2):
            a = getattr(catalogs[buy], 'identities', {}).get(symbol)
            b = getattr(catalogs[sell], 'identities', {}).get(symbol)
            reason = identity_conflict(symbol, buy, sell, a, b)
            if reason:
                conflicts[symbol] = reason
                break
    return conflicts
