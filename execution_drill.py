"""Live order-book rehearsal. Forced entries are drills, never strategy signals."""
import concurrent.futures
import copy
import json
import time
from pathlib import Path
from execution import PaperExecutor
from feeds import Feed

ROOT=Path(__file__).resolve().parent

def run():
    c=json.loads((ROOT/'config.json').read_text(encoding='utf-8-sig'))
    c.update(symbols=['BTC'],notional_usd=50,take_profit_usd=100,stop_loss_usd=10,cooldown_seconds=1,max_hold_seconds=60)
    c['execution']={**c.get('execution',{}),'slippage_bps':30,'order_timeout_seconds':20,'max_unhedged_seconds':20}
    directory=ROOT/'output'/'execution-drill'/time.strftime('%Y%m%d-%H%M%S')
    directory.mkdir(parents=True,exist_ok=True)
    plans=[('normal','edgex','arcus',None),('partial','robinhood_lighter','popdex','partial_short'),
           ('single_leg_failure','edgex','popdex','reject_short'),('unknown_ack','arcus','robinhood_lighter','unknown_short')]
    cases=[]
    for name,buy,sell,fault in plans:
        e=PaperExecutor(copy.deepcopy(c),str(directory/(name+'.sqlite')),fault)
        cases.append(dict(name=name,buy=buy,sell=sell,executor=e,pair_id=None,submitted=False))
    feed=Feed();deadline=time.monotonic()+150;sample_count=0;errors={}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            while time.monotonic()<deadline:
                jobs={pool.submit(feed.book,v,'BTC'):v for v in c['venues']}
                books={};errors={}
                for f in concurrent.futures.as_completed(jobs):
                    v=jobs[f]
                    try:books[(v,'BTC')]=f.result()
                    except Exception as exc:errors[v]=str(exc)
                now=time.time();sample_count+=1
                for case in cases:
                    e=case['executor'];e.step(books,now)
                    if not case['submitted'] and len(books)==4:
                        ref=max(books[(case['buy'],'BTC')]['asks'][0][0],books[(case['sell'],'BTC')]['bids'][0][0])
                        signal=dict(symbol='BTC',buy=case['buy'],sell=case['sell'],quantity=45/ref,baseline=0,cost_bps=0)
                        case['pair_id']=e.submit(signal,books,now,drill=True)
                        case['submitted']=case['pair_id'] is not None
                    if case['pair_id']:
                        p=next(p for p in e.s['pairs'] if p['id']==case['pair_id'])
                        if p['state']=='OPEN':e.request_close(case['pair_id'],'drill_close')
                print(json.dumps(dict(sample=sample_count,errors=errors,states={case['name']:case['executor'].s['pairs'][-1]['state'] if case['executor'].s['pairs'] else 'WAITING' for case in cases})),flush=True)
                if all(case['submitted'] and case['executor'].s['pairs'][-1]['state'] in ('CLOSED','ABORTED') for case in cases):break
                time.sleep(2)
        results=[]
        for case in cases:
            e=case['executor'];s=e.snapshot(books,time.time())
            (directory/(case['name']+'.json')).write_text(json.dumps(s,indent=2),encoding='utf-8')
            p=e.s['pairs'][-1] if e.s['pairs'] else None
            orders=e.s['orders'];fault=case['name']
            expected_fault=(fault=='normal' or fault=='partial' and any(o['status']=='PARTIAL_CANCELLED' for o in orders) or fault=='single_leg_failure' and any(o['status']=='REJECTED' for o in orders) or fault=='unknown_ack' and any(any(v['status']=='UNKNOWN' for v in o['events']) for o in orders))
            complete=p is not None and p['state'] in ('CLOSED','ABORTED') and not s['positions']
            normal_closed=(fault!='normal' or p is not None and p['state']=='CLOSED')
            results.append(dict(scenario=fault,buy=case['buy'],sell=case['sell'],state=p['state'] if p else 'NOT_SUBMITTED',
                passed=bool(complete and expected_fault and normal_closed and e.reconcile(time.time())),orders=len(orders),
                statuses=[o['status'] for o in orders],net_pnl=s['realized_pnl'],unhedged_quantity=sum(abs(l['quantity']) for l in e.s['ledger'].values()),report=str((directory/(case['name']+'.json')).resolve())))
        report=dict(mode='LIVE_BOOK_PAPER_DRILL',strategy_signal=False,created=time.time(),samples=sample_count,
            assumptions='Forced BTC entries; simulated IOC, 30 bps limit, synthetic partial/reject/unknown faults; no real orders',
            results=results,errors=errors,config=c,artifact_directory=str(directory.resolve()))
        (directory/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        (ROOT/'output/execution-drill/report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report,indent=2),flush=True)
        return all(r['passed'] for r in results)
    finally:
        for case in cases:case['executor'].db.close()

if __name__=='__main__':
    raise SystemExit(0 if run() else 1)
