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
        with self.lock:result=self.items;self.items={};return result

def processes():
    result=[]
    for p in psutil.process_iter(['pid','name','memory_info','cmdline','create_time']):
        try:
            info=p.info;cmd=' '.join(info['cmdline'] or []).lower();agent=next((a for a in ('codex','claude','opencode','pi-coding-agent','cursor','gemini') if a in cmd),'')
            result.append(dict(pid=info['pid'],name=info['name'],rss=info['memory_info'].rss,agent=agent,created=info['create_time']))
        except (psutil.Error,TypeError,AttributeError):pass
    return sorted(result,key=lambda p:p['rss'],reverse=True)[:30]

class Collector:
    def __init__(self,data=None,watch_roots=None):
        self.data=Path(data or data_dir());initialize(self.data);self.c=connect(self.data)
        self.stop=threading.Event();self.pending=Pending(self.data);self.observers={};self.scan_messages=queue.Queue(12)
        self.roots=watch_roots or roots();self.last_resources=0;self.last_folders=0;self.revision=0;self.thread=None
        self.state=dict(pid=os.getpid(),phase='starting',roots=self.roots,heartbeat=time.time(),indexed=meta(self.c,'indexed',0),scanned=0,skipped=0,errors=[],coverage_complete=False)

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
            while not self.stop.is_set():
                task=c.execute('SELECT * FROM scan_queue ORDER BY priority DESC LIMIT 1').fetchone()
                if not task:break
                directory=task['p'];root=task['root'];baseline=not meta(c,'baseline:'+root,False)
                rows=[];children=[];candidates={};total=0;count=0
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
                                    if any(x in entry.path.lower() for x in ('windows','node_modules','site-packages')):priority=0
                                    children.append((entry.path,root,priority))
                                elif entry.is_file(follow_symlinks=False):
                                    n=s.st_size;rows.append((entry.path,n,s.st_mtime));total+=n;count+=1
                                    rule=classify(entry.path,s.st_mtime)
                                    if rule['bucket']:
                                        bucket=rule['bucket'];v=candidates.setdefault(bucket,[0,0,rule]);v[0]+=n;v[1]+=1
                                    if len(rows)>=1000:
                                        self.put(('files',rows,baseline));rows=[];time.sleep(.01)
                            except OSError:self.state['skipped']+=1
                    if rows:self.put(('files',rows,baseline))
                    self.put(('directory',directory,root,children,total,count,candidates))
                    # Writer acknowledges by deleting queue row. Keeps current directory replayable on crash.
                    while not self.stop.is_set() and c.execute('SELECT 1 FROM scan_queue WHERE p=?',(directory,)).fetchone():time.sleep(.05)
                except OSError as e:
                    self.state['skipped']+=1
                    if len(self.state['errors'])<30:self.state['errors'].append(str(e))
                    self.put(('skip',directory))
                    time.sleep(.02)
            self.put(('scan_done',))
        except Exception:
            logging.exception('Scanner stopped');self.put(('scan_error',))
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
            _,directory,root,children,n,count,candidates=msg
            old=self.c.execute('SELECT n,files FROM scan_dirs WHERE p=?',(directory,)).fetchone()
            rollup(self.c,directory,n-(old['n'] if old else 0),count-(old['files'] if old else 0))
            self.c.execute('INSERT OR REPLACE INTO scan_dirs VALUES (?,?,?,?)',(directory,n,count,str(time.time())))
            self.c.execute('DELETE FROM candidate_parts WHERE dir=?',(directory,))
            self.c.executemany('INSERT OR REPLACE INTO candidate_parts VALUES (?,?,?,?,?,?)',[(directory,b,v[0],v[1],v[2]['risk'],v[2]['reason']) for b,v in candidates.items()])
            self.c.executemany('INSERT OR IGNORE INTO scan_queue VALUES (?,?,?)',children)
            self.c.execute('DELETE FROM scan_queue WHERE p=?',(directory,))
            if not self.c.execute('SELECT 1 FROM scan_queue WHERE root=?',(root,)).fetchone():setmeta(self.c,'baseline:'+root,True)
            self.state['scanning_path']=directory
        elif msg[0]=='skip':self.c.execute('DELETE FROM scan_queue WHERE p=?',(msg[1],))
        elif msg[0]=='scan_done':
            self.state['phase']='watching';self.state['last_scan']=time.time();self.state['coverage_complete']=True
            setmeta(self.c,'last_scan',time.time())
        elif msg[0]=='scan_error':self.state['phase']='scan_error'

    def live(self):
        for p,(kind,dst,isdir,ts) in self.pending.take().items():
            try:
                if kind=='moved':moved(self.c,p,dst,ts);p=dst
                if kind=='deleted':
                    old=self.c.execute('SELECT n FROM files WHERE p=?',(p,)).fetchone();removed(self.c,p,ts)
                    if old:rollup(self.c,os.path.dirname(p),-old['n'],-1)
                    continue
                if isdir:
                    if kind=='created':
                        root=next((r for r in self.roots if p.lower().startswith(r.lower())),self.roots[0]);self.c.execute('INSERT OR IGNORE INTO scan_queue VALUES (?,?,90)',(p,root))
                    continue
                s=os.stat(p,follow_symlinks=False)
                old=self.c.execute('SELECT n FROM files WHERE p=?',(p,)).fetchone()
                event=observe(self.c,p,s.st_size,s.st_mtime,source='watch',ts=ts,created=kind=='created')
                if event:
                    rollup(self.c,os.path.dirname(p),s.st_size-(old['n'] if old else 0),0 if old else 1)
                    # Only live changes enqueue optional AI, never initialization/reconciliation.
                    if classify(p,s.st_mtime)['category']!='secrets':
                        fp=f'{s.st_size}:{s.st_mtime}';self.c.execute("INSERT INTO ai_jobs VALUES (?,?,'pending',?,NULL) ON CONFLICT(p) DO UPDATE SET fingerprint=excluded.fingerprint,status='pending',requested=excluded.requested,error=NULL",(p,fp,time.time()))
            except FileNotFoundError:removed(self.c,p,ts)
            except OSError:pass

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
                for _ in range(8):
                    try:self.apply_scan(self.scan_messages.get_nowait())
                    except queue.Empty:break
                    if time.monotonic()-started>.25:break
                now=time.time()
                if now-self.last_resources>=15:self.resource_sample(now);self.last_resources=now
                if now-self.last_folders>=30:
                    self.c.execute('DELETE FROM candidates');self.c.execute('INSERT INTO candidates SELECT bucket,sum(n),sum(files),risk,reason,? FROM candidate_parts GROUP BY bucket',(now,));self.last_folders=now
                if (self.data/'rescan.request').exists() or now>next_scan:
                    (self.data/'rescan.request').unlink(missing_ok=True);self.launch_scan(True);next_scan=now+6*3600
                if not (self.thread and self.thread.is_alive()) and self.c.execute('SELECT 1 FROM scan_queue').fetchone():self.launch_scan()
                self.state.update(heartbeat=now,watchers={r:o.is_alive() and all(e.is_alive() for e in o.emitters) for r,o in self.observers.items()},overflow=self.pending.overflow,revision=self.revision)
                self.revision+=1;setmeta(self.c,'status',self.state);self.c.commit();atomic(self.data/'status.json',self.state)
                # Healthy emitter reconnection; reconciliation is explicitly requested after a gap.
                for root,ob in list(self.observers.items()):
                    if not self.state['watchers'][root]:ob.stop();ob.join(1);new=Observer();new.schedule(self.pending,root,recursive=True);new.start();self.observers[root]=new;next_scan=0
                self.stop.wait(.2)
        finally:
            self.stop.set()
            for ob in self.observers.values():ob.stop()
            for ob in self.observers.values():ob.join(3)
            self.state.update(phase='stopped',heartbeat=time.time());setmeta(self.c,'status',self.state);self.c.commit();atomic(self.data/'status.json',self.state);self.c.close()
