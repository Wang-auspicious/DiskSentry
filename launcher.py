"""Per-user resilient processes. Credentials remain in Pi Agent, outside repository."""
import argparse
from contextlib import closing
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import urllib.request
import winreg
from storage import data_dir,initialize,connect,meta,setmeta

APP=Path(__file__).resolve().parent

def pythonw():
    p=Path(sys.executable).with_name('pythonw.exe');return str(p if p.exists() else Path(sys.executable))

def spawn(*args):
    subprocess.Popen([pythonw(),str(APP/'launcher.py'),*args],cwd=APP,creationflags=0x08000000|0x8,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

def running(data):
    try:s=json.loads((data/'status.json').read_text('utf8'));return time.time()-s['heartbeat']<6 and s.get('phase')!='stopped'
    except (OSError,ValueError,KeyError):return False

def install_startup():
    command=subprocess.list2cmdline([pythonw(),str(APP/'launcher.py'),'--start'])
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
        winreg.SetValueEx(key,'DiskSentry',0,winreg.REG_SZ,command)
        try:winreg.DeleteValue(key,'DiskWatch')
        except FileNotFoundError:pass

def start(data):
    if not running(data):
        (data/'stop.request').unlink(missing_ok=True);spawn('--collector')

def singleton(data,name):
    import msvcrt
    f=open(data/(name+'.lock'),'a+b');f.seek(0)
    if not f.read(1):f.write(b'1');f.flush()
    f.seek(0)
    try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1);return f
    except OSError:f.close();return None

def migrate_history(data):
    old=Path(os.environ.get('LOCALAPPDATA',''))/'DiskWatch/history.sqlite3'
    with closing(connect(data)) as c:
        if meta(c,'legacy_imported',False) or not old.exists():return
        # A read-only SQLite URI; old index and source are left intact.
        with closing(sqlite3.connect(old.as_uri()+'?mode=ro',uri=True)) as source:
            source.row_factory=sqlite3.Row
            for r in source.execute('SELECT * FROM changes'):
                c.execute('INSERT INTO events(ts,p,kind,b,n,delta,source,evidence) VALUES (?,?,?,?,?,?,?,?)',(r['last_at'],r['display'],r['kind'],r['before_size'],r['after_size'],r['after_size']-r['before_size'],'legacy-large-files',json.dumps({'agent':'未知','confidence':'legacy','reason':'旧版每日大文件汇总，非逐次事件'})))
            # Old large-file inventory seeds display only; never label it as a fresh scan.
            for r in source.execute('SELECT * FROM files WHERE present=1'):
                c.execute('INSERT OR IGNORE INTO files VALUES (?,?,?,?,?)',(r['display'],r['size'],r['mtime'],r['allocated'],r['last_seen']))
        setmeta(c,'legacy_imported',True);setmeta(c,'history_gap',{'from':'2026-10-15','until':time.time(),'reason':'旧监控进程已退出，仅遗留少量历史。v2 从本次启用开始连续采样。'});c.commit()

def daemon(data):
    lock=singleton(data,'collector')
    if not lock:return
    logging.basicConfig(handlers=[RotatingFileHandler(data/'collector.log',maxBytes=1_000_000,backupCount=2,encoding='utf8')],level=logging.WARNING)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.GetCurrentProcess.restype=ctypes.c_void_p;kernel.SetPriorityClass.argtypes=[ctypes.c_void_p,ctypes.c_ulong];kernel.SetPriorityClass(kernel.GetCurrentProcess(),0x4000)
    from collector import Collector
    try:
        while not (data/'stop.request').exists():
            try:Collector(data).run();break
            except Exception:logging.exception('Collector restart');time.sleep(3)
    finally:lock.close()

def server_info(data):
    try:
        info=json.loads((data/'dashboard.json').read_text('utf8'))
        req=urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/state",headers={'X-DiskSentry-Token':info['token']})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=3) as r:
            if r.status==200:return info
    except (OSError,ValueError,KeyError):pass

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--collector',action='store_true');parser.add_argument('--serve',action='store_true');parser.add_argument('--start',action='store_true');parser.add_argument('--install',action='store_true');args=parser.parse_args()
    data=data_dir();initialize(data)
    if args.collector:return daemon(data)
    if args.serve:
        lock=singleton(data,'server')
        if lock:
            from service import serve
            serve(data)
        return
    if args.install:install_startup();migrate_history(data)
    start(data)
    if args.start:return
    info=server_info(data)
    if not info:
        spawn('--serve')
        for _ in range(40):
            time.sleep(.2);info=server_info(data)
            if info:break
    if info:
        edge=Path(os.environ.get('PROGRAMFILES(X86)',r'C:\Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe'
        url=f"http://127.0.0.1:{info['port']}/#{info['token']}"
        if edge.exists():subprocess.Popen([str(edge),'--app='+url,'--window-size=1540,980'])
        else:
            import webbrowser
            webbrowser.open(url)

if __name__=='__main__':main()
