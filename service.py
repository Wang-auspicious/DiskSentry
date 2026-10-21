"""Authenticated, loopback-only read API. No delete/edit/command-execution endpoint."""
from contextlib import closing
import csv
import datetime as dt
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from storage import connect,initialize,meta,setmeta,date_range,data_dir
from rules import classify
from attribution import INDEX,start_indexer
from ai_analysis import public_config,worker

APP=Path(__file__).resolve().parent

def identity(path):return hashlib.sha256(str(path).lower().encode()).hexdigest()[:24]
def record(row):
    r=dict(row);r['id']=identity(r['p']);r['rule']=classify(r['p'],r.get('m',0));return r

def query_events(c,begin,end,q='',drive='',kind='',limit=200,offset=0):
    sql='ts>=? AND ts<?';args=[begin,end]
    if q:sql+=' AND instr(lower(p),lower(?))>0';args.append(q)
    if drive:sql+=' AND substr(p,1,?)=? COLLATE NOCASE';args.extend([len(drive),drive])
    if kind:sql+=' AND kind=?';args.append(kind)
    count=c.execute('SELECT count(*) FROM events WHERE '+sql,args).fetchone()[0]
    rows=[dict(r) for r in c.execute('SELECT * FROM events WHERE '+sql+' ORDER BY ts DESC,id DESC LIMIT ? OFFSET ?',args+[limit,offset])]
    for r in rows:r['rule']=classify(r['p']);r['attribution']=json.loads(r.get('evidence') or '{}') or INDEX.match(r['p'],r['ts'])
    return rows,count

def state(data,query):
    begin,end=date_range(query.get('period','today'),query.get('start'),query.get('end'))
    with closing(connect(data)) as c:
        status=meta(c,'status',{});healthy=time.time()-status.get('heartbeat',0)<5 and status.get('phase')!='stopped'
        rows,total=query_events(c,begin,end,query.get('q',''),query.get('drive',''),query.get('kind',''),min(500,max(1,int(query.get('limit',150)))),max(0,int(query.get('offset',0))))
        sums=c.execute('SELECT count(*) count,coalesce(sum(CASE WHEN delta>0 THEN delta ELSE 0 END),0) added,coalesce(sum(CASE WHEN delta<0 THEN -delta ELSE 0 END),0) freed,coalesce(sum(delta),0) net,sum(CASE WHEN delta IS NULL THEN 1 ELSE 0 END) unknown FROM events WHERE ts>=? AND ts<?',(begin,end)).fetchone()
        disks=[]
        roots=[r[0] for r in c.execute('SELECT DISTINCT root FROM volumes')]
        for root in roots:
            current=c.execute('SELECT * FROM volumes WHERE root=? ORDER BY ts DESC LIMIT 1',(root,)).fetchone()
            # Only actual within/before-range samples, never reconstruct a missing historical baseline.
            first=c.execute('SELECT * FROM volumes WHERE root=? AND ts>=? AND ts<? ORDER BY ts LIMIT 1',(root,begin,end)).fetchone()
            last=c.execute('SELECT * FROM volumes WHERE root=? AND ts>=? AND ts<? ORDER BY ts DESC LIMIT 1',(root,begin,end)).fetchone()
            trend=[dict(r) for r in c.execute('SELECT ts,free,total FROM volumes WHERE root=? AND ts>=? AND ts<? ORDER BY ts',(root,begin,end))]
            step=max(1,len(trend)//240);trend=trend[::step]
            consumed=first['free']-last['free'] if first and last else None
            known=c.execute('SELECT coalesce(sum(delta),0) FROM events WHERE substr(p,1,?)=? COLLATE NOCASE AND ts>=? AND ts<=?',(len(root),root,first['ts'] if first else begin,last['ts'] if last else end)).fetchone()[0]
            disks.append(dict(root=root,free=current['free'],total=current['total'],consumed=consumed,explained=known,unexplained=None if consumed is None else consumed-known,trend=trend,sampledFrom=first['ts'] if first else None))
        candidates=[dict(r) for r in c.execute('SELECT * FROM candidates WHERE n>0 ORDER BY n DESC LIMIT 200')]
        for r in candidates:r['id']=identity(r['p'])
        largest=[record(r) for r in c.execute('SELECT * FROM files WHERE n>=? ORDER BY n DESC LIMIT 300',(int(query.get('minBytes',100_000_000)),))]
        parent=query.get('parent','')
        if parent:folders=[dict(r) for r in c.execute('SELECT * FROM folders WHERE parent=? ORDER BY n DESC LIMIT 80',(parent,))]
        else:folders=[dict(r) for r in c.execute("SELECT * FROM folders WHERE p IN (SELECT root FROM volumes) ORDER BY n DESC")]
        mem=[dict(r) for r in c.execute('SELECT ts,used,total FROM memory WHERE ts>=? AND ts<? ORDER BY ts',(begin,end))]
        mem=mem[::max(1,len(mem)//240)]
        latest=c.execute('SELECT * FROM memory ORDER BY ts DESC LIMIT 1').fetchone()
        ai=public_config();ai['enabled']=meta(c,'ai_enabled',True);ai['jobs']={r[0]:r[1] for r in c.execute('SELECT status,count(*) FROM ai_jobs GROUP BY status')}
        storage_size=sum(p.stat().st_size for p in Path(data).glob('index.sqlite3*'))
        return dict(version='2.0',now=time.time(),range=dict(start=begin,end=end),recordingSince=meta(c,'recording_since'),status=status,healthy=healthy,summary=dict(sums),disks=disks,events=rows,totalEvents=total,largest=largest,folders=folders,candidates=candidates,memory=dict(history=mem,current=dict(ts=latest['ts'],used=latest['used'],total=latest['total'],processes=json.loads(latest['processes'])) if latest else None),ai=ai,indexBytes=storage_size)

def detail(data,path,event_id=None):
    with closing(connect(data)) as c:
        row=c.execute('SELECT * FROM files WHERE p=?',(path,)).fetchone()
        event=c.execute('SELECT * FROM events WHERE id=?',(event_id,)).fetchone() if event_id else None
        if not row and not c.execute('SELECT 1 FROM candidates WHERE p=?',(path,)).fetchone() and not c.execute('SELECT 1 FROM events WHERE p=?',(path,)).fetchone():raise ValueError('该路径不在索引中')
        events=[dict(r) for r in c.execute('SELECT * FROM events WHERE p=? ORDER BY ts DESC LIMIT 100',(path,))]
        info=dict(row) if row else {'p':path,'n':event['n'] if event else 0,'m':0}
        cached=c.execute('SELECT * FROM ai_cache WHERE p=?',(path,)).fetchone();job=c.execute('SELECT status,error FROM ai_jobs WHERE p=?',(path,)).fetchone()
        fresh=bool(cached and row and cached['fingerprint']==f"{row['n']}:{row['m']}")
        evidence=INDEX.match(path,event['ts'] if event else info['m'])
        return dict(file=info,rule=classify(path,info.get('m',0)),evidence=evidence,events=events,ai=json.loads(cached['result']) if cached else None,aiFresh=fresh,job=dict(job) if job else None)

def handler(data,token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def allowed(self,auth=True):
            host=f'127.0.0.1:{self.server.server_port}';origin=self.headers.get('Origin')
            return self.headers.get('Host')==host and (origin is None or origin=='http://'+host) and (not auth or secrets.compare_digest(self.headers.get('X-DiskSentry-Token',''),token))
        def send(self,code,value,mime='application/json; charset=utf-8'):
            raw=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
            self.send_response(code);self.send_header('Content-Type',mime);self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer');self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'");self.send_header('Content-Length',str(len(raw)));self.end_headers()
            try:self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):pass
        def do_GET(self):
            u=urllib.parse.urlsplit(self.path);q={k:v[0] for k,v in urllib.parse.parse_qs(u.query).items()}
            if not self.allowed(u.path.startswith('/api/')):return self.send(403,{'error':'只允许本机授权访问'})
            try:
                if u.path=='/api/state':return self.send(200,state(data,q))
                if u.path=='/api/detail':return self.send(200,detail(data,q.get('path',''),q.get('event')))
                if u.path=='/api/export':
                    begin,end=date_range(q.get('period','today'),q.get('start'),q.get('end'))
                    with closing(connect(data)) as c:rows,_=query_events(c,begin,end,q.get('q',''),q.get('drive',''),q.get('kind',''),100000,0)
                    out=io.StringIO();w=csv.writer(out);w.writerow(['时间','路径','类型','之前字节','之后字节','净变化字节','来源','Agent','归因证据'])
                    for r in rows:w.writerow([dt.datetime.fromtimestamp(r['ts']).isoformat(),r['p'],r['kind'],r['b'],r['n'],r['delta'],r['source'],r['attribution'].get('agent'),r['attribution'].get('confidence')])
                    return self.send(200,('\ufeff'+out.getvalue()).encode('utf8'),'text/csv; charset=utf-8')
                assets={'/':('v2.html','text/html; charset=utf-8'),'/app.js':('v2.js','text/javascript; charset=utf-8'),'/style.css':('v2.css','text/css; charset=utf-8'),'/icon.svg':('icon.svg','image/svg+xml')}
                if u.path not in assets:return self.send(404,{'error':'不存在'})
                p,mime=assets[u.path];return self.send(200,(APP/'web'/p).read_bytes(),mime)
            except (ValueError,sqlite3.Error) as e:return self.send(400,{'error':str(e)[:180]})
        def do_POST(self):
            if not self.allowed():return self.send(403,{'error':'未授权'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<=length<=65536:return self.send(413,{'error':'请求过大'})
                body=json.loads(self.rfile.read(length) or b'{}')
                if self.path=='/api/rescan':(Path(data)/'rescan.request').touch()
                elif self.path=='/api/ai-enabled':
                    with closing(connect(data)) as c:setmeta(c,'ai_enabled',bool(body.get('enabled')));c.commit()
                elif self.path=='/api/analyze':
                    path=body.get('path');d=detail(data,path);f=d['file']
                    if d['rule']['category']=='secrets':raise ValueError('敏感文件不允许发送 AI')
                    if not public_config()['configured']:raise ValueError('未配置 API，离线分析仍可使用')
                    with closing(connect(data)) as c:c.execute("INSERT OR REPLACE INTO ai_jobs VALUES (?,?,'manual',?,NULL)",(path,f"{f['n']}:{f.get('m',0)}",time.time()-11));c.commit()
                elif self.path=='/api/review':
                    with closing(connect(data)) as c:c.executemany('UPDATE events SET reviewed=1 WHERE id=?',[(i,) for i in body.get('ids',[])[:1000]]);c.commit()
                elif self.path=='/api/reveal':
                    path=body.get('path');detail(data,path)
                    if os.path.exists(path):subprocess.Popen(['explorer.exe','/select,',path])
                    else:
                        p=Path(path).parent
                        while not p.exists() and p!=p.parent:p=p.parent
                        subprocess.Popen(['explorer.exe',str(p)])
                else:return self.send(404,{'error':'没有此操作；此应用不提供文件编辑、清理或删除接口'})
                self.send(200,{'ok':True})
            except (ValueError,TypeError,OSError,sqlite3.Error) as e:self.send(400,{'error':str(e)[:180]})
    return Handler

def serve(data=None):
    from collector import atomic
    data=Path(data or data_dir());initialize(data);token=secrets.token_urlsafe(32)
    stop=threading.Event();start_indexer(stop);worker(data,stop)
    server=ThreadingHTTPServer(('127.0.0.1',0),handler(data,token));server.daemon_threads=True
    atomic(data/'dashboard.json',{'pid':os.getpid(),'port':server.server_port,'token':token})
    try:server.serve_forever()
    finally:stop.set();server.server_close()
