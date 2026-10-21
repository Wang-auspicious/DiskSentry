"""Single indexed writer, resumable metadata scan, real-time events + resource history."""
import ctypes
import json
import logging
import os
from pathlib import Path
import queue
import sqlite3
import sys
import threading
import time
import psutil
from storage import initialize,connect,meta,setmeta,observe,removed,moved,rollup,data_dir
from rules import classify

sys.path.insert(0,str(Path(__file__).parent/'vendor'))
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
if os.name=='nt':
    from watchdog.observers import winapi
    winapi.WATCHDOG_FILE_NOTIFY_FLAGS=27

def roots():
    return [p.mountpoint for p in psutil.disk_partitions() if 'fixed' in p.opts and p.fstype]

def atomic(path,value):
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,ensure_ascii=False),encoding='utf8');os.replace(temp,path)

class Pending(FileSystemEventHandler):
    def __init__(self,data):self.lock=threading.Lock();self.items={};self.overflow=0;self.data=str(data).lower()
    def on_any_event(self,e):
        if e.event_type not in ('created','deleted','modified','moved') or (e.is_directory and e.event_type=='modified'):return
        if e.src_path.lower().startswith(self.data+os.sep):return
        with self.lock:
            if len(self.items)>50000:self.overflow+=1;return
            old=self.items.get(e.src_path)
            kind='created' if old and old[0]=='created' and e.event_type=='modified' else e.event_type
            self.items[e.src_path]=(kind,getattr(e,'dest_path',None),e.is_directory,time.time())
    def take(self):
        with self.lock:
            keys=list(self.items)[:1000];return {k:self.items.pop(k) for k in keys}

def processes():
    result=[]
    for p in psutil.process_iter(['pid','name','memory_info','cmdline','create_time']):
        try:
            info=p.info;cmd=' '.join(info['cmdline'] or []).lower();agent=next((a for a in ('codex','claude','opencode','pi-coding-agent','cursor','gemini') if a in cmd),'')
            result.append(dict(pid=info['pid'],name=info['name'],rss=info['memory_info'].rss,agent=agent,created=info['create_time']))
        except (psutil.Error,TypeError,AttributeError):pass
    ordered=sorted(result,key=lambda p:p['rss'],reverse=True);top=ordered[:30]
    return top+[p for p in ordered[30:] if p['agent']]

class Collector:
    def __init__(self,data=None,watch_roots=None):
        self.data=Path(data or data_dir());initialize(self.data);self.c=connect(self.data)
        self.stop=threading.Event();self.pending=Pending(self.data);self.observers={};self.scan_messages=queue.Queue(48)
        self.roots=watch_roots or roots();self.last_resources=0;self.last_folders=0;self.revision=0;self.thread=None
        self.state=dict(pid=os.getpid(),phase='starting',roots=self.roots,heartbeat=time.time(),indexed=self.c.execute('SELECT count(*) FROM files').fetchone()[0],scanned=0,skipped=0,errors=[],coverage_complete=all(meta(self.c,'baseline:'+r,False) for r in self.roots))
        self.scan_acks=queue.SimpleQueue();self.ack_pending=[]

    def resource_sample(self,now=None):
        now=now or time.time()
        for root in self.roots:
            try:
                u=psutil.disk_usage(root);self.c.execute('INSERT OR REPLACE INTO volumes VALUES (?,?,?,?)',(now,root,u.free,u.total))
            except OSError:pass
        v=psutil.virtual_memory();self.c.execute('INSERT OR REPLACE INTO memory VALUES (?,?,?,?)',(now,v.used,v.total,json.dumps(processes())))

    def scan_worker(self):
        c=connect(self.data)
        try:
            for root in self.roots:
                if not meta(c,'baseline:'+root,False) and not c.execute('SELECT 1 FROM scan_queue WHERE root=?',(root,)).fetchone():
                    c.execute('INSERT OR IGNORE INTO scan_queue VALUES (?,?,?)',(root,root,100))
            c.commit()
            c.execute("UPDATE scan_queue SET priority=95 WHERE instr(lower(p),'users')>0");c.commit()
            inflight=set()
            while not self.stop.is_set():
                while not self.scan_acks.empty():inflight.discard(self.scan_acks.get_nowait())
                tasks=c.execute('SELECT * FROM scan_queue ORDER BY priority DESC LIMIT 256').fetchall()
                if not tasks:break
                task=next((t for t in tasks if t['p'] not in inflight),None)
                if task is None:self.stop.wait(.02);continue
                directory=task['p'];root=task['root'];baseline=not meta(c,'baseline:'+root,False)
                if psutil.disk_usage(str(self.data)).free<5_000_000_000:
                    self.state['phase']='index_paused_low_space';self.state['errors']=['索引所在磁盘不足 5 GB；已暂停全盘索引，保留实时监听。'];self.stop.wait(15);continue
                rows=[];children=[];candidates={};total=0;count=0;observed_names=set()
                try:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if self.stop.is_set():return
                            try:
                                s=entry.stat(follow_symlinks=False)
                                if entry.is_dir(follow_symlinks=False):
                                    if getattr(s,'st_file_attributes',0)&0x400:continue
                                    if str(entry.path).lower().startswith(str(self.data).lower()):continue
                                    priority=90 if any(x in entry.path.lower() for x in ('.codex','.pi','.claude','desktop','documents')) else 10
                                    if 'users' in entry.path.lower():priority=95
                                    if any(x in entry.path.lower() for x in ('windows','node_modules','site-packages')):priority=0
                                    children.append((entry.path,root,priority))
                                elif entry.is_file(follow_symlinks=False):
                                    n=s.st_size;rows.append((entry.path,n,s.st_mtime));total+=n;count+=1;observed_names.add(entry.name.casefold())
                                    rule=classify(entry.path,s.st_mtime)
                                    if rule['bucket']:
                                        bucket=rule['bucket'];v=candidates.setdefault(bucket,[0,0,rule]);v[0]+=n;v[1]+=1
                                    if len(rows)>=1000:
                                        self.put(('files',rows,baseline));rows=[];time.sleep(.01)
                            except OSError:self.state['skipped']+=1
                    if rows:self.put(('files',rows,baseline))
                    self.put(('directory',directory,root,children,total,count,candidates,observed_names,baseline))
                    inflight.add(directory)
                except OSError as e:
                    self.state['skipped']+=1
                    if len(self.state['errors'])<30:self.state['errors'].append(str(e))
                    self.put(('skip',directory))
                    inflight.add(directory)
                    time.sleep(.02)
            self.put(('scan_done',))
        except Exception:
            logging.exception('Scanner stopped');self.put(('scan_error',));self.stop.wait(15)
        finally:c.close()

    def put(self,item):
        while not self.stop.is_set():
            try:self.scan_messages.put(item,timeout=.3);return
            except queue.Full:pass

    def launch_scan(self,force=False):
        if self.thread and self.thread.is_alive():return
        if force:
            for root in self.roots:self.c.execute('INSERT OR IGNORE INTO scan_queue VALUES (?,?,?)',(root,root,100))
            self.c.commit()
        self.state['phase']='scanning';self.thread=threading.Thread(target=self.scan_worker,daemon=True);self.thread.start()

    def apply_scan(self,msg):
        if msg[0]=='files':
            for p,n,m in msg[1]:observe(self.c,p,n,m,source='reconcile',baseline=msg[2])
            self.state['scanned']+=len(msg[1])
        elif msg[0]=='directory':
            _,directory,root,children,n,count,candidates,observed_names,baseline=msg
            old=self.c.execute('SELECT n,files FROM scan_dirs WHERE p=?',(directory,)).fetchone()
            if old and not baseline:
                prefix=directory.rstrip('\\/')+os.sep
                for row in self.c.execute('SELECT p FROM files WHERE p>=? COLLATE NOCASE AND p<? COLLATE NOCASE',(prefix,prefix+'\U0010ffff')).fetchall():
                    if os.path.dirname(row['p']).casefold()==directory.casefold() and os.path.basename(row['p']).casefold() not in observed_names:
                        try:os.stat(row['p'])
                        except FileNotFoundError:
                            removed(self.c,row['p']);self.c.execute("UPDATE events SET source='offline-reconcile' WHERE id=last_insert_rowid()")
                        except OSError:pass
            rollup(self.c,directory,n-(old['n'] if old else 0),count-(old['files'] if old else 0))
            self.c.execute('INSERT OR REPLACE INTO scan_dirs VALUES (?,?,?,?)',(directory,n,count,str(time.time())))
            self.c.execute('DELETE FROM candidate_parts WHERE dir=?',(directory,))
            self.c.executemany('INSERT OR REPLACE INTO candidate_parts VALUES (?,?,?,?,?,?)',[(directory,b,v[0],v[1],v[2]['risk'],v[2]['reason']) for b,v in candidates.items()])
            self.c.executemany('INSERT OR IGNORE INTO scan_queue VALUES (?,?,?)',children)
            self.c.execute('DELETE FROM scan_queue WHERE p=?',(directory,))
            self.ack_pending.append(directory)
            if not self.c.execute('SELECT 1 FROM scan_queue WHERE root=?',(root,)).fetchone():setmeta(self.c,'baseline:'+root,True)
            self.state['scanning_path']=directory
        elif msg[0]=='skip':self.c.execute('DELETE FROM scan_queue WHERE p=?',(msg[1],));self.ack_pending.append(msg[1])
        elif msg[0]=='scan_done':
            self.state['phase']='watching';self.state['last_scan']=time.time();self.state['coverage_complete']=True
            setmeta(self.c,'last_scan',time.time())
        elif msg[0]=='scan_error':self.state['phase']='scan_error'

    def live(self):
        for p,(kind,dst,isdir,ts) in self.pending.take().items():
            try:
                if kind in ('moved','deleted'):
                    prefix=p.rstrip('\\/')+os.sep
                    prior=self.c.execute('SELECT p,n FROM files WHERE p=? OR (p>=? COLLATE NOCASE AND p<? COLLATE NOCASE)',(p,prefix,prefix+'\U0010ffff')).fetchall()
                    if kind=='deleted':removed(self.c,p,ts)
                    else:
                        targets=[(dst+r['p'][len(p):],r['n']) for r in prior]
                        for target,_ in targets:
                            existing=self.c.execute('SELECT n FROM files WHERE p=?',(target,)).fetchone()
                            if existing and target.casefold()!=p.casefold():self.adjust_live(target,-existing['n'],-1)
                        moved(self.c,p,dst,ts)
                    for r in prior:self.adjust_live(r['p'],-r['n'],-1)
                    if kind=='moved':
                        for target,n in targets:self.adjust_live(target,n,1)
                        p=dst
                    else:continue
                if kind=='deleted':
                    continue
                if isdir:
                    if kind in ('created','moved'):
                        root=next((r for r in self.roots if p.lower().startswith(r.lower())),self.roots[0]);self.c.execute('INSERT OR IGNORE INTO scan_queue VALUES (?,?,90)',(p,root))
                    continue
                s=os.stat(p,follow_symlinks=False)
                old=self.c.execute('SELECT n FROM files WHERE p=?',(p,)).fetchone()
                event=observe(self.c,p,s.st_size,s.st_mtime,source='watch',ts=ts,created=kind=='created')
                if event:
                    self.adjust_live(p,s.st_size-(old['n'] if old else 0),0 if old else 1)
                    # Only live changes enqueue optional AI, never initialization/reconciliation.
                    if classify(p,s.st_mtime)['category']!='secrets':
                        fp=f'{s.st_size}:{s.st_mtime}';self.c.execute("INSERT INTO ai_jobs VALUES (?,?,'pending',?,NULL) ON CONFLICT(p) DO UPDATE SET fingerprint=excluded.fingerprint,status='pending',requested=excluded.requested,error=NULL",(p,fp,time.time()))
            except FileNotFoundError:
                old=self.c.execute('SELECT n FROM files WHERE p=?',(p,)).fetchone();removed(self.c,p,ts)
                if old:self.adjust_live(p,-old['n'],-1)
            except OSError:pass

    def adjust_live(self,path,delta,count):
        directory=os.path.dirname(path)
        # A directory not yet scanned is added by its first complete snapshot.
        # Updating the cached direct total prevents reconciliation double counting.
        if not self.c.execute('SELECT 1 FROM scan_dirs WHERE p=?',(directory,)).fetchone():return
        self.c.execute('UPDATE scan_dirs SET n=max(0,n+?),files=max(0,files+?) WHERE p=?',(delta,count,directory))
        rollup(self.c,directory,delta,count)
        rule=classify(path)
        if rule['bucket']:
            self.c.execute('INSERT INTO candidate_parts VALUES (?,?,?,?,?,?) ON CONFLICT(dir,bucket) DO UPDATE SET n=max(0,n+?),files=max(0,files+?),risk=excluded.risk,reason=excluded.reason',
                (directory,rule['bucket'],max(0,delta),max(0,count),rule['risk'],rule['reason'],delta,count))

    def run(self):
        self.resource_sample();self.c.commit();self.launch_scan()
        try:
            for root in self.roots:
                try:
                    ob=Observer();ob.schedule(self.pending,root,recursive=True);ob.start();self.observers[root]=ob
                except OSError as e:self.state['errors'].append(str(e))
            next_scan=time.time()+6*3600
            while not self.stop.is_set() and not (self.data/'stop.request').exists():
                started=time.monotonic();self.live()
                # Bound scan work so event ingestion/resource sampling cannot starve.
                for _ in range(256):
                    try:self.apply_scan(self.scan_messages.get_nowait())
                    except queue.Empty:break
                    if time.monotonic()-started>.25:break
                now=time.time()
                if now-self.last_resources>=15:self.resource_sample(now);self.last_resources=now
                if now-self.last_folders>=30:
                    self.c.execute('DELETE FROM candidates');self.c.execute('INSERT INTO candidates SELECT bucket,sum(n),sum(files),risk,reason,? FROM candidate_parts GROUP BY bucket',(now,));self.last_folders=now
                    self.state['indexed']=self.c.execute('SELECT count(*) FROM files').fetchone()[0]
                if (self.data/'rescan.request').exists() or now>next_scan:
                    (self.data/'rescan.request').unlink(missing_ok=True);self.launch_scan(True);next_scan=now+6*3600
                if not (self.thread and self.thread.is_alive()) and self.c.execute('SELECT 1 FROM scan_queue').fetchone():self.launch_scan()
                self.state.update(heartbeat=now,watchers={r:o.is_alive() and all(e.is_alive() for e in o.emitters) for r,o in self.observers.items()},overflow=self.pending.overflow,revision=self.revision)
                self.revision+=1;setmeta(self.c,'status',self.state);self.c.commit();atomic(self.data/'status.json',self.state)
                for p in self.ack_pending:self.scan_acks.put(p)
                self.ack_pending=[]
                # Healthy emitter reconnection; reconciliation is explicitly requested after a gap.
                for root,ob in list(self.observers.items()):
                    if not self.state['watchers'][root]:ob.stop();ob.join(1);new=Observer();new.schedule(self.pending,root,recursive=True);new.start();self.observers[root]=new;next_scan=0
                self.stop.wait(.2)
        finally:
            self.stop.set()
            for ob in self.observers.values():ob.stop()
            for ob in self.observers.values():ob.join(3)
            if self.thread:self.thread.join(3)
            self.state.update(phase='stopped',heartbeat=time.time());setmeta(self.c,'status',self.state);self.c.commit();atomic(self.data/'status.json',self.state);self.c.close()
