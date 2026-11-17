"""Bounded execution-book recorder and offline paper replay; never sends orders."""
import argparse
import copy
import json
import sqlite3
import time
from execution import PaperExecutor
from execution_runtime import confirmation

class Recorder:
    def __init__(self,path,executor,max_frames=5000):
        self.db=sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY,config TEXT,seed TEXT,complete INTEGER DEFAULT 0)')
        self.db.execute('CREATE TABLE IF NOT EXISTS frames(session INTEGER,seq INTEGER,time REAL,value TEXT,PRIMARY KEY(session,seq))')
        self.session=self.db.execute('INSERT INTO sessions(config,seed) VALUES (?,?)',(json.dumps(executor.c),json.dumps(executor.s))).lastrowid
        old=[r[0] for r in self.db.execute('SELECT id FROM sessions ORDER BY id DESC LIMIT -1 OFFSET 3')]
        for session in old:
            self.db.execute('DELETE FROM frames WHERE session=?',(session,));self.db.execute('DELETE FROM sessions WHERE id=?',(session,))
        self.db.commit();self.count=0;self.limit=max_frames
    def record(self,books,watches,now):
        if self.count>=self.limit:return
        payload=dict(books=[dict(venue=v,symbol=s,book=b) for (v,s),b in books.items()],watches=copy.deepcopy(watches))
        self.db.execute('INSERT INTO frames VALUES (?,?,?,?)',(self.session,self.count,now,json.dumps(payload,allow_nan=False)))
        self.db.commit();self.count+=1
    def close(self):
        self.db.execute('UPDATE sessions SET complete=1 WHERE id=?',(self.session,));self.db.commit();self.db.close()

def replay(path,session=None):
    from pathlib import Path
    db=sqlite3.connect('file:'+Path(path).resolve().as_posix()+'?mode=ro',uri=True)
    row=db.execute('SELECT id,config,seed FROM sessions '+('WHERE id=?' if session is not None else 'ORDER BY id DESC LIMIT 1'),(session,) if session is not None else ()).fetchone()
    if not row:raise ValueError('no recorded session')
    session,c,seed=row[0],json.loads(row[1]),json.loads(row[2])
    e=PaperExecutor(c,':memory:');e.s=seed;e.save();initial=e.snapshot()['realized_pnl'];count=0;last=0;books={}
    for _,now,value in db.execute('SELECT seq,time,value FROM frames WHERE session=? ORDER BY seq',(session,)):
        frame=json.loads(value);books={(r['venue'],r['symbol']):r['book'] for r in frame['books']}
        e.step(books,now)
        for watch in frame['watches']:
            s=watch['signal'];check=confirmation(s,books,now,c,watch.get('check'))
            if check and check['duration']>=c['confirm_seconds']:e.submit(s,books,now)
        count+=1;last=now
    state=e.snapshot(books,last)
    report=dict(session=session,frames=count,realized_pnl_change=state['realized_pnl']-initial,
        remaining_positions=state['positions'],route_stats=state['route_stats'],halt_reason=state['halt_reason'],
        limitation='Only recorded candidate and position books; not an independent full-market strategy backtest. Open positions remain open at replay end.')
    db.close();e.db.close();return report

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--database',default='data/radar.execution.replay.sqlite');p.add_argument('--session',type=int);p.add_argument('--output',default='output/replay-report.json');a=p.parse_args()
    report=replay(a.database,a.session)
    from pathlib import Path
    Path(a.output).parent.mkdir(parents=True,exist_ok=True);Path(a.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=True,indent=2))
