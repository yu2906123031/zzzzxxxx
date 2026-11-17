"""Offline synthetic DEX arbitrage. No network, keys or live orders."""
import argparse, csv, json, math, random, tempfile, time
from pathlib import Path

class Pool:
    def __init__(self, name, quote, base):
        self.name, self.quote, self.base = name, quote, base
    def output(self, amount, buy):
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError('Invalid amount')
        x, y = (self.quote, self.base) if buy else (self.base, self.quote)
        effective = amount * .997
        return y * effective / (x + effective)
    def swap(self, amount, buy):
        received = self.output(amount, buy)
        if buy:
            self.quote += amount
            self.base -= received
        else:
            self.base += amount
            self.quote -= received
        return received

def quote(a, b, amount):
    return b.output(a.output(amount, True), False)

def flow(pool, rng):
    buy = rng.random() < .5
    pool.swap((pool.quote if buy else pool.base) * rng.uniform(.0001, .012), buy)

def run(steps=100, seed=7, capital=1000, max_trade=200, gas=.15,
        min_profit=.1, slippage_bps=10, loss_limit=20, interval=0,
        output=str(Path(__file__).resolve().parent / 'output/dex-sim')):
    vals = [capital, max_trade, gas, min_profit, slippage_bps, loss_limit, interval]
    if any(not math.isfinite(v) for v in vals) or steps < 1 or capital <= 0 or max_trade <= 0 or gas < 0 or min_profit < 0 or not 0 <= slippage_bps < 10000 or loss_limit <= 0 or interval < 0:
        raise ValueError('Invalid simulation parameters')
    rng = random.Random(seed)
    pools = [Pool('DEX-A', 300000, 100), Pool('DEX-B', 303000, 100)]
    balance, filled, reverted = capital, 0, 0
    Path(output).mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='run-', dir=output))
    print('OFFLINE SYNTHETIC DATA | PAPER ONLY', flush=True)
    with (directory / 'trades.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['step','status','buy','sell','amount','expected_net','conservative_net','realized_net','balance'])
        for step in range(1, steps + 1):
            for pool in pools:
                flow(pool, rng)
            cap = min(max_trade, max(0, balance - gas))
            if cap <= 0:
                break
            options = []
            for a,b in (pools, pools[::-1]):
                for i in range(1,101):
                    amount = cap * i / 100
                    proceeds = quote(a,b,amount)
                    minimum = proceeds * (1-slippage_bps/10000)
                    options.append((minimum-amount-gas, proceeds-amount-gas, amount, minimum, a,b))
            edge, expected, amount, minimum, a,b = max(options, key=lambda x:x[0])
            status, profit = 'SKIP', 0
            if edge >= min_profit:
                for pool in pools:
                    flow(pool, rng)
                if quote(a,b,amount) < minimum:
                    status, profit = 'REVERT', -gas
                    reverted += 1
                else:
                    proceeds = b.swap(a.swap(amount,True),False)
                    status, profit = 'FILLED', proceeds-amount-gas
                    filled += 1
                balance += profit
                print(f'{step:5d} {status:6s} {a.name}->{b.name} net={profit:+.4f} balance={balance:.4f}', flush=True)
            writer.writerow([step,status,a.name,b.name,amount,expected,edge,profit,balance])
            f.flush()
            if capital-balance >= loss_limit or balance <= gas:
                print('STOP: loss limit or insufficient funds')
                break
            if interval:
                time.sleep(interval)
    summary = dict(mode='OFFLINE_SYNTHETIC_PAPER', seed=seed, initial_balance=capital,
        final_balance=balance, net_pnl=balance-capital, filled=filled, reverted=reverted,
        config=dict(steps=steps,max_trade=max_trade,gas=gas,min_profit=min_profit,
                    slippage_bps=slippage_bps,loss_limit=loss_limit), output=str(directory.resolve()))
    (directory/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(json.dumps(summary,indent=2))
    return summary

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--steps',type=int,default=100)
    p.add_argument('--seed',type=int,default=7)
    for name, value in [('capital',1000),('max-trade',200),('gas',.15),('min-profit',.1),('slippage-bps',10),('loss-limit',20),('interval',0)]:
        p.add_argument('--'+name,type=float,default=value)
    p.add_argument('--output',default=str(Path(__file__).resolve().parent/'output/dex-sim'))
    run(**vars(p.parse_args()))
