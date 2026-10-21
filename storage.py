"""Indexed metadata + append-only observed events. Never writes monitored paths."""
import datetime as dt
import json
import os
from pathlib import Path
from sqlite_runtime import sqlite3,safe_wal
import time
from rules import classify

def data_dir():
    return Path(os.environ.get('DISKSENTRY_DATA',str(Path(os.environ.get('LOCALAPPDATA',str(Path.home()))) / 'DiskSentry')))

def connect(data=None):
    data=Path(data or data_dir());data.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(data/'index.sqlite3',timeout=15);c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=15000');c.execute('PRAGMA journal_mode='+('WAL' if safe_wal() else 'DELETE'));c.execute('PRAGMA synchronous=FULL')
    return c

def initialize(data=None):
    c=connect(data)
    c.executescript('''
    CREATE TABLE IF NOT EXISTS files(p TEXT COLLATE NOCASE PRIMARY KEY,n INTEGER NOT NULL,m REAL NOT NULL,a INTEGER,seen REAL NOT NULL) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS files_size ON files(n DESC);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,ts REAL NOT NULL,p TEXT,kind TEXT,b INTEGER,n INTEGER,delta INTEGER,old TEXT,source TEXT,evidence TEXT,reviewed INTEGER DEFAULT 0);
    CREATE INDEX IF NOT EXISTS events_time ON events(ts);
    CREATE INDEX IF NOT EXISTS events_path ON events(p,ts);
    CREATE TABLE IF NOT EXISTS volumes(ts REAL,root TEXT,free INTEGER,total INTEGER,PRIMARY KEY(ts,root));
    CREATE TABLE IF NOT EXISTS memory(ts REAL PRIMARY KEY,used INTEGER,total INTEGER,processes TEXT);
    CREATE TABLE IF NOT EXISTS folders(p TEXT COLLATE NOCASE PRIMARY KEY,parent TEXT,n INTEGER,files INTEGER,updated REAL);
    CREATE INDEX IF NOT EXISTS folders_parent ON folders(parent);
    CREATE TABLE IF NOT EXISTS candidates(p TEXT COLLATE NOCASE PRIMARY KEY,n INTEGER,files INTEGER,risk TEXT,reason TEXT,updated REAL);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT);
    CREATE TABLE IF NOT EXISTS scan_queue(p TEXT COLLATE NOCASE PRIMARY KEY,root TEXT,priority INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS ai_cache(p TEXT COLLATE NOCASE PRIMARY KEY,fingerprint TEXT,result TEXT,ts REAL);
    CREATE TABLE IF NOT EXISTS ai_jobs(p TEXT COLLATE NOCASE PRIMARY KEY,fingerprint TEXT,status TEXT,requested REAL,error TEXT);
    CREATE TABLE IF NOT EXISTS scan_dirs(p TEXT COLLATE NOCASE PRIMARY KEY,n INTEGER,files INTEGER,scan TEXT);
    CREATE TABLE IF NOT EXISTS candidate_parts(dir TEXT,bucket TEXT,n INTEGER,files INTEGER,risk TEXT,reason TEXT,PRIMARY KEY(dir,bucket));
    ''')
    c.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',('recording_since',json.dumps(time.time())))
    c.commit();c.close()

def rollup(c,directory,delta,count=0):
    for _ in range(40):
        parent=os.path.dirname(directory)
        terminal=parent==directory or not directory
        if terminal:parent=''
        c.execute('INSERT INTO folders VALUES (?,?,?,?,?) ON CONFLICT(p) DO UPDATE SET n=max(0,n+?),files=max(0,files+?),updated=excluded.updated',
            (directory,parent,max(0,delta),max(0,count),time.time(),delta,count))
        if terminal:break
        directory=parent

def meta(c,k,default=None):
    r=c.execute('SELECT v FROM meta WHERE k=?',(k,)).fetchone();return json.loads(r[0]) if r else default

def setmeta(c,k,v):c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(k,json.dumps(v,ensure_ascii=False)))

def observe(c,path,n,m,a=None,source='watch',baseline=False,ts=None,created=False,evidence=None):
    ts=ts or time.time();path=str(path)
    old=c.execute('SELECT n,m,a FROM files WHERE p=?',(path,)).fetchone()
    if old and old['n']==n and abs(old['m']-m)<.00001:return None
    c.execute('INSERT INTO files VALUES (?,?,?,?,?) ON CONFLICT(p) DO UPDATE SET n=excluded.n,m=excluded.m,a=excluded.a,seen=excluded.seen',(path,n,m,a,ts))
    if baseline:return None
    before=old['n'] if old else None
    kind='created' if created else ('discovered' if old is None else ('grown' if n>before else 'shrunk' if n<before else 'modified'))
    delta=n-(before or 0) if old or created else None
    cursor=c.execute('INSERT INTO events(ts,p,kind,b,n,delta,old,source,evidence) VALUES (?,?,?,?,?,?,?,?,?)',
        (ts,path,kind,before,n,delta,None,source,json.dumps(evidence or {},ensure_ascii=False)))
    return cursor.lastrowid

def removed(c,path,ts=None):
    ts=ts or time.time();path=str(path)
    old=c.execute('SELECT n FROM files WHERE p=?',(path,)).fetchone()
    if old:
        c.execute('DELETE FROM files WHERE p=?',(path,))
        c.execute('INSERT INTO events(ts,p,kind,b,n,delta,source) VALUES (?,?,?,?,?,?,?)',(ts,path,'deleted',old['n'],0,-old['n'],'watch'))
    else:
        # Directory deletes are one event per indexed child; no arbitrary path prefix matching.
        prefix=path.rstrip('\\/')+os.sep
        rows=c.execute('SELECT p,n FROM files WHERE p>=? COLLATE NOCASE AND p<? COLLATE NOCASE',(prefix,prefix+'\U0010ffff')).fetchall()
        for r in rows:removed(c,r['p'],ts)

def moved(c,src,dst,ts=None):
    ts=ts or time.time();src=str(src);dst=str(dst)
    r=c.execute('SELECT * FROM files WHERE p=?',(src,)).fetchone()
    if r:
        target=c.execute('SELECT n FROM files WHERE p=?',(dst,)).fetchone()
        if target:removed(c,dst,ts)
        c.execute('DELETE FROM files WHERE p=?',(src,));c.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)',(dst,r['n'],r['m'],r['a'],ts))
        c.execute('INSERT INTO events(ts,p,kind,b,n,delta,old,source) VALUES (?,?,?,?,?,?,?,?)',(ts,dst,'moved',r['n'],r['n'],0,src,'watch'))
        return
    prefix=src.rstrip('\\/')+os.sep
    for r in c.execute('SELECT p FROM files WHERE p>=? COLLATE NOCASE AND p<? COLLATE NOCASE',(prefix,prefix+'\U0010ffff')).fetchall():moved(c,r['p'],dst+r['p'][len(src):],ts)

def date_range(period='today',start=None,end=None):
    today=dt.date.today()
    if period=='custom':
        begin=dt.datetime.fromisoformat(start);finish=dt.datetime.fromisoformat(end)
        if len(end)==10:finish+=dt.timedelta(days=1)
    else:
        finish=dt.datetime.combine(today+dt.timedelta(days=1),dt.time())
        days={'today':0,'yesterday':1,'week':6,'month':29}.get(period,0)
        begin=dt.datetime.combine(today-dt.timedelta(days=days),dt.time())
        if period=='yesterday':finish=dt.datetime.combine(today,dt.time())
    if begin>=finish:raise ValueError('起始时间必须早于结束时间')
    return begin.timestamp(),finish.timestamp()
