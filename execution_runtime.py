"""Dedicated paper execution polling, independent from full-market scans."""
import concurrent.futures
import copy
import threading
import time
from execution import PaperExecutor, validate_execution
from market_identity import identity_conflict
from depth import size_pair, entry_cost, viable, expected_profit

def confirmation(signal,books,now,config,previous=None):
    from arb import vwap, healthy
    a,b=books.get((signal['buy'],signal['symbol'])),books.get((signal['sell'],signal['symbol']))
    if identity_conflict(signal['symbol'],signal['buy'],signal['sell'],(a or {}).get('identity'),(b or {}).get('identity')):return None
    if not a or not b or not healthy(a,b,now,config):return None
    if max(a['asks'][0][0],b['asks'][0][0])/min(a['asks'][0][0],b['asks'][0][0])>validate_execution(config.get('execution',{}))['max_price_ratio']:return None
    try:q,bp,sp=size_pair(a,b,config,signal['quantity'])
    except ValueError:return None
    spread=(sp-bp)/bp*10000
    cost=entry_cost(config,signal['buy'],signal['sell'])
    deviation=spread-signal['baseline'];edge=deviation-cost
    if not viable(spread,signal['baseline'],cost,config):return None
    profit=expected_profit(q,bp,sp,signal['baseline'],cost)
    if profit<validate_execution(config.get('execution',{}))['min_expected_profit_usd']:return None
    since=previous['since'] if previous and now-previous['checked']<=config['max_quote_age_seconds'] else now
    return dict(since=since,checked=now,duration=now-since,spread=spread,deviation=deviation,
                expected_profit_usd=profit,estimated_edge=edge,quantity=q,cost_bps=cost,buy_price=bp,sell_price=sp,quote_time=min(a['time'],b['time']))

class ExecutionRuntime:
    def __init__(self,config,database,feed):
        self.c=config;self.database=database;self.feed=feed
        self.lock=threading.Lock();self.stop_event=threading.Event();self.ready=threading.Event()
        self.signals={};self.state=dict(mode='PAPER_EXECUTION',positions=[],trades=[],orders=[],accounts=[],events=[],realized_pnl=0,halt_reason=None,time=0)
        self.thread=threading.Thread(target=self.run,name='paper-execution',daemon=True)
        self.thread.start()
        if not self.ready.wait(10):raise RuntimeError('execution worker startup timed out')
        if self.state.get('fatal'):raise RuntimeError(self.state['fatal'])

    def enqueue(self,row):
        with self.lock:
            previous=self.signals.get(row['symbol'])
            if not previous or row['estimated_edge']>=previous['estimated_edge']:
                self.signals[row['symbol']]=dict(row,queued_at=time.time())

    def snapshot(self):
        with self.lock:return copy.deepcopy(self.state)

    def run(self):
        executor=None;recorder=None
        try:
            executor=PaperExecutor(self.c,self.database)
            from replay import Recorder
            from pathlib import Path
            recording_error=None
            try:recorder=Recorder(str(Path(self.database).with_suffix('.replay.sqlite')),executor)
            except Exception as exc:recording_error=str(exc)
            with self.lock:self.state=executor.snapshot()
            self.ready.set()
            watches={}
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                while not self.stop_event.is_set():
                    start=time.monotonic()
                    with self.lock:signals=list(self.signals.values());self.signals.clear()
                    signals=[s for s in signals if time.time()-s['queued_at']<=self.c['max_quote_age_seconds']]
                    occupied={p['symbol'] for p in executor.s['pairs'] if p['state'] not in ('CLOSED','ABORTED')}
                    watches={k:w for k,w in watches.items() if w['signal']['symbol'] not in occupied and time.time()-w['signal']['queued_at']<120}
                    for signal in sorted(signals,key=lambda s:s['estimated_edge'],reverse=True):
                        if signal['symbol'] in occupied:continue
                        key=signal['key']
                        if key in watches:watches[key]['signal']=signal
                        elif len(watches)<executor.e['max_watch_pairs'] and not any(w['signal']['symbol']==signal['symbol'] for w in watches.values()):watches[key]=dict(signal=signal,check=None)
                    targets=executor.targets()
                    for w in watches.values():
                        s=w['signal'];targets.update([(s['buy'],s['symbol']),(s['sell'],s['symbol'])])
                    futures={pool.submit(self.feed.book,v,s):(v,s) for v,s in targets}
                    books,errors={},{}
                    for future in concurrent.futures.as_completed(futures):
                        key=futures[future]
                        try:books[key]=future.result()
                        except Exception as e:errors[':'.join(key)]=str(e)
                    now=time.time()
                    if recorder and recording_error is None:
                        try:recorder.record(books,list(watches.values()),now)
                        except Exception as exc:recording_error=str(exc)
                    executor.step(books,now)
                    watch_rows=[]
                    for key,w in list(watches.items()):
                        s=w['signal'];check=confirmation(s,books,now,self.c,w['check']);w['check']=check
                        if check:
                            watch_rows.append(dict(s,**{k:v for k,v in check.items() if k not in ('since','checked')},sample_time=now,status='PENDING'))
                            if check['duration']>=self.c['confirm_seconds']:
                                executor.submit(s,books,now)
                                del watches[key]
                        else:
                            del watches[key]
                    state=executor.snapshot(books,now)
                    state['recording']=dict(session=recorder.session,frames=recorder.count,limit=recorder.limit,capped=recorder.count>=recorder.limit,error=recording_error) if recorder else dict(error=recording_error)
                    state['watch_rows']=watch_rows
                    state['watch_count']=len(watches)
                    state['feed_errors']=errors;state['cycle_seconds']=time.monotonic()-start
                    with self.lock:self.state=state
                    self.stop_event.wait(max(.1,validate_execution(self.c.get('execution',{}))['poll_seconds']-(time.monotonic()-start)))
        except Exception as e:
            with self.lock:self.state.update(fatal=str(e),halt_reason='execution worker failed: '+str(e))
            self.ready.set()
        finally:
            if recorder:
                try:recorder.close()
                except Exception:pass
            if executor:executor.db.close()

    def close(self):
        self.stop_event.set();self.thread.join(timeout=20)
