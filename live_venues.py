"""INIT-only mainnet adapters. Never log signed requests or private keys."""
import math
import time
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from urllib.parse import urlencode

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.types import Cloid

from feeds import normalize

D = Decimal
HL_URL = 'https://api.hyperliquid.xyz'
ASTER_URL = 'https://fapi.asterdex.com'


class VenueError(RuntimeError):
    pass


def number(value):
    result = D(str(value))
    if not result.is_finite():
        raise VenueError('non-finite exchange value')
    return result


def wire(value):
    return format(number(value), 'f')


def floor_step(value, step):
    return (number(value)/number(step)).to_integral_value(rounding=ROUND_FLOOR)*number(step)


def common_step(a, b):
    a,b = number(a),number(b)
    scale = 10 ** max(0,-a.as_tuple().exponent,-b.as_tuple().exponent)
    return D(math.lcm(int(a*scale),int(b*scale)))/scale


def http(method, url, body=None):
    # Requests exceptions include full URLs: replace them with a safe diagnostic.
    try:
        response = requests.request(method,url,data=body,timeout=(3,5),
                                    headers={'Content-Type':'application/x-www-form-urlencoded'})
    except requests.RequestException:
        raise VenueError('exchange transport failure; order result may be unknown') from None
    try:
        result = response.json()
    except ValueError:
        raise VenueError(f'exchange HTTP {response.status_code}; non-JSON response') from None
    if response.status_code != 200 or isinstance(result,dict) and int(result.get('code',0)) < 0:
        code = result.get('code','unknown') if isinstance(result,dict) else 'unknown'
        raise VenueError(f'exchange HTTP {response.status_code}, code {code}')
    return result


def checked_book(raw, venue):
    if venue == 'hl':
        bids = normalize(raw['levels'][0],'px','sz',True)
        asks = normalize(raw['levels'][1],'px','sz',False)
        stamp = number(raw['time'])/1000
    else:
        bids = normalize(raw['bids'],0,1,True)
        asks = normalize(raw['asks'],0,1,False)
        stamp = number(raw['T'])/1000
    if bids[0][0] >= asks[0][0]:
        raise VenueError('crossed order book')
    return dict(bids=bids,asks=asks,time=float(stamp),received=time.time())


class Hyperliquid:
    name = 'hl'

    def __init__(self, user, key):
        self.user = Account.from_key(key).address if not user else user
        self.info = Info(HL_URL,skip_ws=True,timeout=5)
        self.exchange = Exchange(Account.from_key(key),HL_URL,account_address=self.user,timeout=5)
        matches = [m for m in self.info.meta()['universe'] if m['name']=='INIT' and not m.get('isDelisted',False)]
        if len(matches) != 1:
            raise VenueError('INIT unavailable on Hyperliquid')
        self.decimals = int(matches[0]['szDecimals'])
        self.step = D(10)**-self.decimals
        self.minimum = D('10')
        self.min_qty = self.step
        self.max_qty = D('1e15')

    def price(self, value, buy):
        value = number(value)
        # At most five significant figures and 6-szDecimals decimal places.
        tick = max(D(10)**(value.adjusted()-4),D(10)**-(6-self.decimals))
        return (value/tick).to_integral_value(rounding=ROUND_FLOOR if buy else ROUND_CEILING)*tick

    def book(self):
        return checked_book(self.info.l2_snapshot('INIT'),'hl')

    def position(self):
        rows = [r['position'] for r in self.info.user_state(self.user)['assetPositions'] if r['position']['coin']=='INIT']
        return sum((number(r['szi']) for r in rows),D(0))

    def balance(self):
        mode = self.info.query_user_abstraction_state(self.user)
        if mode in ('unifiedAccount','portfolioMargin'):
            spot = self.info.spot_user_state(self.user)
            rows = [r for r in spot['balances'] if r['coin']=='USDC' and int(r['token'])==0]
            if len(rows)>1:
                raise VenueError('ambiguous USDC balance')
            total = number(rows[0]['total']) if rows else D(0)
            hold = number(rows[0]['hold']) if rows else D(0)
            available = max(D(0),total-hold)
            maintenance = {int(token):number(amount) for token,amount in spot.get('tokenToAvailableAfterMaintenance',[])}
            if 0 not in maintenance and total>0:
                raise VenueError('unified account missing maintenance availability')
            available = min(available,max(D(0),maintenance.get(0,D(0))))
            return dict(currency='USDC',account_mode=mode,wallet_balance=wire(total),
                        available_collateral=wire(available),live_balance_supported=mode=='unifiedAccount')
        if mode not in ('default','disabled','dexAbstraction'):
            raise VenueError('unknown Hyperliquid account mode')
        account = self.info.user_state(self.user)
        return dict(currency='USDC',account_mode=mode,account_equity=wire(account['marginSummary']['accountValue']),
                    available_collateral=wire(account['withdrawable']),live_balance_supported=True)

    def preflight(self):
        signer = self.exchange.wallet.address.lower()
        if signer!=self.user.lower():
            agents = self.info.post('/info',{'type':'extraAgents','user':self.user})
            if not any(a.get('address','').lower()==signer and number(a['validUntil'])>D(str(time.time()*1000)) for a in agents):
                raise VenueError('Hyperliquid API wallet is not authorized or has expired')
        if any(o['coin']=='INIT' for o in self.info.open_orders(self.user)):
            raise VenueError('existing Hyperliquid INIT orders')
        if self.position():
            raise VenueError('existing Hyperliquid INIT position')
        summary = self.balance()
        if not summary['live_balance_supported']:
            raise VenueError('portfolio margin live trial unsupported; no automatic borrowing')
        balance = number(summary['available_collateral'])
        fee = number(self.info.user_fees(self.user)['userCrossRate'])
        return balance,fee

    def leverage_one(self):
        result = self.exchange.update_leverage(1,'INIT',is_cross=True)
        if result.get('status') != 'ok':
            raise VenueError('Hyperliquid leverage update failed')

    def send(self, order):
        self.exchange.set_expires_after(int(time.time()*1000)+5000)
        response = self.exchange.order('INIT',order['buy'],float(order['qty']),float(order['price']),
                                       {'limit':{'tif':'Ioc'}},reduce_only=order['reduce'],
                                       cloid=Cloid.from_str(order['id']))
        if response.get('status') == 'ok':
            statuses = response.get('response',{}).get('data',{}).get('statuses',[])
            if len(statuses)==1 and 'error' in statuses[0]:
                # A definitive exchange rejection is terminal, unlike a timeout.
                return dict(status='REJECTED',qty='0',notional='0',fee='0')
        return None

    def resolve(self, order):
        raw = self.info.query_order_by_cloid(self.user,Cloid.from_str(order['id']))
        if raw.get('status') != 'order':
            return None
        data = raw['order']
        status = data['status']
        if status in ('open','triggered'):
            return None
        detail = data['order']
        if detail['coin']!='INIT' or number(detail['origSz'])!=number(order['qty']) or detail.get('cloid') not in (None,order['id']):
            raise VenueError('Hyperliquid order identity mismatch')
        fills = self.info.user_fills_by_time(self.user,int(order['created']*1000)-1000)
        if len(fills)>=2000:
            raise VenueError('Hyperliquid fills truncated; reconciliation required')
        fills = {f['tid']:f for f in fills if str(f['oid'])==str(detail['oid'])}.values()
        qty = sum((number(f['sz']) for f in fills),D(0))
        expected = number(detail['origSz']) if status=='filled' else number(detail['origSz'])-number(detail['sz'])
        if qty != expected:
            return None  # Fills can lag order status. Never infer zero from missing fills.
        if any(f.get('feeToken','USDC')!='USDC' for f in fills):
            raise VenueError('unsupported Hyperliquid fee currency')
        return dict(status=status,qty=wire(qty),notional=wire(sum((number(f['sz'])*number(f['px']) for f in fills),D(0))),
                    fee=wire(sum((number(f['fee']) for f in fills),D(0))))

    def cancel(self, order):
        self.exchange.set_expires_after(int(time.time()*1000)+5000)
        self.exchange.cancel_by_cloid('INIT',Cloid.from_str(order['id']))


class Aster:
    name = 'aster'

    def __init__(self, user, key, journal):
        self.user = user
        self.wallet = Account.from_key(key)
        self.journal = journal
        meta = http('GET',ASTER_URL+'/fapi/v3/exchangeInfo')
        matches = [m for m in meta['symbols'] if m['symbol']=='INITUSDT' and m['baseAsset']=='INIT'
                   and m['quoteAsset']=='USDT' and m['contractType']=='PERPETUAL' and m['status']=='TRADING']
        if len(matches)!=1:
            raise VenueError('INITUSDT perpetual unavailable on Aster V3')
        filters = {f['filterType']:f for f in matches[0]['filters']}
        self.step = number(filters['LOT_SIZE']['stepSize'])
        self.min_qty = number(filters['LOT_SIZE']['minQty'])
        self.max_qty = number(filters['LOT_SIZE']['maxQty'])
        self.tick = number(filters['PRICE_FILTER']['tickSize'])
        self.minimum = number(filters['MIN_NOTIONAL']['notional'])
        if min(self.step,self.tick,self.minimum)<=0:
            raise VenueError('invalid Aster filters')

    def signed(self, method, path, **params):
        params.update(user=self.user,signer=self.wallet.address,nonce=str(self.journal.nonce()))
        query = urlencode(params)
        message = encode_typed_data(domain_data=dict(name='AsterSignTransaction',version='1',chainId=1666,
                                     verifyingContract='0x0000000000000000000000000000000000000000'),
                                     message_types={'Message':[{'name':'msg','type':'string'}]},message_data={'msg':query})
        signature = self.wallet.sign_message(message).signature.hex()
        body = query+'&signature='+('0x'+signature.removeprefix('0x'))
        url = ASTER_URL+'/fapi/v3/'+path
        return http(method,url+'?'+body if method=='GET' else url,None if method=='GET' else body)

    def price(self, value, buy):
        return (number(value)/self.tick).to_integral_value(rounding=ROUND_FLOOR if buy else ROUND_CEILING)*self.tick

    def book(self):
        return checked_book(http('GET',ASTER_URL+'/fapi/v3/depth?symbol=INITUSDT&limit=1000'),'aster')

    def position(self):
        rows = self.signed('GET','positionRisk',symbol='INITUSDT')
        if any(r['positionSide']!='BOTH' for r in rows):
            raise VenueError('Aster requires One-way mode; refusing to change account mode')
        return sum((number(r['positionAmt']) for r in rows if r['symbol']=='INITUSDT'),D(0))

    def preflight(self):
        if self.signed('GET','positionSide/dual')['dualSidePosition'] is not False:
            raise VenueError('Aster requires One-way mode')
        if self.signed('GET','openOrders',symbol='INITUSDT'):
            raise VenueError('existing Aster INIT orders')
        if self.position():
            raise VenueError('existing Aster INIT position')
        account = self.signed('GET','account')
        if account.get('canTrade') is not True:
            raise VenueError('Aster account cannot trade')
        fee = number(self.signed('GET','commissionRate',symbol='INITUSDT')['takerCommissionRate'])
        return number(account['availableBalance']),fee

    def leverage_one(self):
        if int(self.signed('POST','leverage',symbol='INITUSDT',leverage=1)['leverage'])!=1:
            raise VenueError('Aster leverage update failed')

    def send(self, order):
        self.signed('POST','order',symbol='INITUSDT',side='BUY' if order['buy'] else 'SELL',
                    positionSide='BOTH',type='LIMIT',timeInForce='IOC',quantity=order['qty'],price=order['price'],
                    reduceOnly='true' if order['reduce'] else 'false',newClientOrderId=order['id'],newOrderRespType='RESULT')
        return None

    def resolve(self, order):
        raw = self.signed('GET','order',symbol='INITUSDT',origClientOrderId=order['id'])
        if raw['symbol']!='INITUSDT' or raw['clientOrderId']!=order['id'] or number(raw['origQty'])!=number(order['qty']):
            raise VenueError('Aster order identity mismatch')
        if raw['status'] not in ('FILLED','CANCELED','REJECTED','EXPIRED','EXPIRED_IN_MATCH'):
            return None
        qty = number(raw['executedQty'])
        fills = self.signed('GET','userTrades',symbol='INITUSDT',orderId=raw['orderId'],limit=1000) if qty else []
        if len(fills)>=1000 or sum((number(f['qty']) for f in fills),D(0))!=qty:
            return None
        if any(f['commissionAsset']!='USDT' for f in fills):
            raise VenueError('unsupported Aster commission currency')
        return dict(status=raw['status'],qty=wire(qty),
                    notional=wire(sum((number(f['qty'])*number(f['price']) for f in fills),D(0))),
                    fee=wire(sum((abs(number(f['commission'])) for f in fills),D(0))))

    def cancel(self, order):
        self.signed('DELETE','order',symbol='INITUSDT',origClientOrderId=order['id'])
