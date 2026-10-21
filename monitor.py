"""DiskWatch: local metadata-only monitor, with watchdog's Windows backend."""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import threading
import time

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP / 'vendor'))
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
if os.name == 'nt':
    from watchdog.observers import winapi
    # Access-time notifications can feed the scanner's own metadata reads back
    # into the watcher. Only names, file sizes and writes matter to DiskWatch.
    winapi.WATCHDOG_FILE_NOTIFY_FLAGS = (winapi.FILE_NOTIFY_CHANGE_FILE_NAME |
        winapi.FILE_NOTIFY_CHANGE_DIR_NAME | winapi.FILE_NOTIFY_CHANGE_SIZE |
        winapi.FILE_NOTIFY_CHANGE_LAST_WRITE)

DEFAULTS = {'threshold_mb': 100, 'rescan_hours': 6, 'retention_days': 0}


def data_path():
    return Path(os.environ.get('DISKWATCH_DATA', str(Path(os.environ['LOCALAPPDATA']) / 'DiskWatch')))


def config_at(data):
    try:
        return DEFAULTS | json.loads((data / 'settings.json').read_text('utf-8'))
    except (OSError, ValueError):
        return DEFAULTS.copy()


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    for _ in range(5):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            time.sleep(.05)


def key(path):
    return os.path.normcase(os.path.abspath(path))


def local_day(ts):
    return dt.datetime.fromtimestamp(ts).strftime('%Y-%m-%d')


def fixed_drives():
    kernel = ctypes.windll.kernel32
    drives = []
    for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
        root = letter + ':\\'
        if kernel.GetDriveTypeW(root) != 3:
            continue
        serial = ctypes.c_ulong()
        if not kernel.GetVolumeInformationW(root, None, 0, ctypes.byref(serial), None, None, None, 0):
            continue
        drives.append((root, str(serial.value)))
    return drives


def allocated_size(path, fallback):
    if os.name != 'nt':
        return fallback
    func = ctypes.WinDLL('kernel32', use_last_error=True).GetCompressedFileSizeW
    func.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    func.restype = ctypes.c_ulong
    high = ctypes.c_ulong()
    ctypes.set_last_error(0)
    low = func(str(path), ctypes.byref(high))
    if low == 0xffffffff and ctypes.get_last_error():
        return fallback
    return (high.value << 32) | low


def metadata(path):
    s = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(s.st_mode):
        return None
    return {'path': str(path), 'size': s.st_size, 'allocated': allocated_size(path, s.st_size),
            'identity': f'{s.st_dev}:{s.st_ino}', 'mtime': s.st_mtime}


class Store:
    def __init__(self, data, threshold=100_000_000):
        data.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(data / 'history.sqlite3', timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            PRAGMA busy_timeout=30000;
            CREATE TABLE IF NOT EXISTS files (
              path TEXT PRIMARY KEY, display TEXT, size INTEGER, allocated INTEGER,
              identity TEXT, mtime REAL, first_seen REAL, last_seen REAL, present INTEGER);
            CREATE INDEX IF NOT EXISTS file_identity ON files(identity);
            CREATE TABLE IF NOT EXISTS changes (
              id INTEGER PRIMARY KEY, day TEXT, path TEXT, display TEXT, kind TEXT,
              before_size INTEGER, after_size INTEGER, peak_size INTEGER, growth INTEGER,
              first_at REAL, last_at REAL, old_path TEXT, source TEXT,
              UNIQUE(day,path));
            CREATE INDEX IF NOT EXISTS changes_time ON changes(last_at);
            CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT);
        ''')
        self.threshold = threshold

    def get(self, path):
        return self.db.execute('SELECT * FROM files WHERE path=?', (key(path),)).fetchone()

    def get_meta(self, name, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE name=?', (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, name, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (name, json.dumps(value)))

    def change(self, path, kind, before, after, now, source, old_path=''):
        p = key(path)
        day = local_day(now)
        old = self.db.execute('SELECT * FROM changes WHERE day=? AND path=?', (day, p)).fetchone()
        growth = max(0, after - before)
        if old:
            # One row per path/day. Retain the initial size and accumulate observed growth.
            merged_kind = old['kind']
            if kind == 'new' or old['kind'] == 'removed':
                merged_kind = kind
            self.db.execute('''UPDATE changes SET display=?,kind=?,after_size=?,peak_size=?,
                growth=growth+?,last_at=?,old_path=CASE WHEN ?!='' THEN ? ELSE old_path END
                WHERE id=?''', (str(path), merged_kind, after, max(old['peak_size'], after),
                                growth, now, old_path, old_path, old['id']))
        else:
            self.db.execute('''INSERT INTO changes
                (day,path,display,kind,before_size,after_size,peak_size,growth,first_at,last_at,old_path,source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (day,p,str(path),kind,before,after,max(before,after),growth,now,now,old_path,source))

    def observe(self, info, baseline=False, reason='scan', now=None):
        if info is None:
            return
        now = time.time() if now is None else now
        path, size = info['path'], info['size']
        p = key(path)
        old = self.get(path)
        if size < self.threshold and not old:
            return
        # Recover missed renames by stable identity only when the former path is gone.
        if not old and info['identity'].split(':')[-1] != '0':
            candidate = self.db.execute('SELECT display FROM files WHERE identity=? AND present=1',
                                        (info['identity'],)).fetchone()
            if candidate:
                try:
                    os.stat(candidate['display'])
                except FileNotFoundError:
                    self.move(candidate['display'], path, now)
                    old = self.get(path)
                except OSError:
                    pass
        previous = old['size'] if old and old['present'] else 0
        if not baseline:
            if not old or not old['present']:
                self.change(path, 'new' if reason == 'created' else 'discovered', 0, size, now, reason)
            elif old['identity'] != info['identity']:
                self.change(path, 'replaced', previous, size, now, reason)
            elif size != previous:
                kind = 'grown' if size > previous else 'shrunk'
                self.change(path, kind, previous, size, now, reason)
        self.db.execute('''INSERT INTO files VALUES (?,?,?,?,?,?,?,?,1)
            ON CONFLICT(path) DO UPDATE SET display=excluded.display,size=excluded.size,
            allocated=excluded.allocated,identity=excluded.identity,mtime=excluded.mtime,
            last_seen=excluded.last_seen,present=1''',
            (p,str(path),size,info['allocated'],info['identity'],info['mtime'],
             old['first_seen'] if old else now,now))

    def remove(self, path, now=None):
        now = time.time() if now is None else now
        p = key(path)
        prefix = p.rstrip(os.sep) + os.sep
        rows = self.db.execute('''SELECT * FROM files WHERE present=1 AND
            (path=? OR substr(path,1,?)=?)''', (p,len(prefix),prefix)).fetchall()
        for row in rows:
            self.change(row['display'], 'removed', row['size'], 0, now, 'deleted')
            self.db.execute('UPDATE files SET present=0,last_seen=? WHERE path=?', (now,row['path']))

    def move(self, src, dest, now=None):
        now = time.time() if now is None else now
        source = key(src)
        prefix = source.rstrip(os.sep) + os.sep
        rows = self.db.execute('SELECT * FROM files WHERE present=1 AND (path=? OR substr(path,1,?)=?)',
                               (source,len(prefix),prefix)).fetchall()
        for row in rows:
            target = str(dest) + row['display'][len(str(src)):]
            targetkey = key(target)
            if row['path'] == targetkey:
                self.db.execute('UPDATE files SET display=? WHERE path=?', (target,targetkey))
                continue
            # Preserve today's creation/growth row when a download gets its final name.
            day = local_day(now)
            self.db.execute('DELETE FROM changes WHERE day=? AND path=?', (day,targetkey))
            self.db.execute('UPDATE changes SET path=?,display=?,old_path=? WHERE day=? AND path=?',
                            (targetkey,target,row['display'],day,row['path']))
            self.db.execute('DELETE FROM files WHERE path=?', (targetkey,))
            self.db.execute('UPDATE files SET path=?,display=?,last_seen=? WHERE path=?',
                            (targetkey,target,now,row['path']))
            self.change(target,'moved',row['size'],row['size'],now,'moved',row['display'])

    def prune(self, days):
        if not days:
            self.db.commit()
            return
        cutoff = time.time() - days * 86400
        self.db.execute('DELETE FROM changes WHERE last_at<?', (cutoff,))
        self.db.execute('DELETE FROM files WHERE present=0 AND last_seen<?', (cutoff,))
        self.db.commit()


class Pending(FileSystemEventHandler):
    def __init__(self, ignored):
        self.lock = threading.Lock()
        self.paths = {}
        self.moves = []
        self.overflow = False
        self.received = 0
        self.ignored = key(ignored).rstrip(os.sep) + os.sep

    def on_any_event(self, event):
        if event.event_type not in ('created','modified','deleted','moved'):
            return
        if key(event.src_path).startswith(self.ignored):
            return
        if event.is_directory and event.event_type == 'modified':
            return
        with self.lock:
            self.received += 1
            if len(self.paths) + len(self.moves) > 40000:
                self.overflow = True
                return
            if event.event_type == 'moved':
                self.moves.append((event.src_path,event.dest_path))
                self.paths[event.dest_path] = ('moved',event.is_directory)
            else:
                prev = self.paths.get(event.src_path)
                reason = event.event_type
                if prev and prev[0] == 'created' and reason == 'modified':
                    reason = 'created'
                self.paths[event.src_path] = (reason,event.is_directory)

    def take(self):
        with self.lock:
            paths, moves = self.paths, self.moves
            self.paths, self.moves = {}, []
            return paths,moves


class Monitor:
    def __init__(self, data, roots=None):
        self.data = data
        self.config = config_at(data)
        self.store = Store(data, int(self.config['threshold_mb'] * 1_000_000))
        if self.store.get_meta('recording_since') is None:
            earliest=self.store.db.execute('SELECT min(first_seen) FROM files').fetchone()[0]
            self.store.set_meta('recording_since',earliest or time.time())
            self.store.db.commit()
        self.pending = Pending(data)
        self.roots_override = roots
        self.observers = {}
        self.baselining = set()
        self.initial_event_cutoff = time.time()
        self.startup_scan = True
        self.state = {'pid':os.getpid(),'started':time.time(),'phase':'starting', 'roots':[],
                      'scanned_files':0,'skipped_count':0,'skipped_paths':[], 'watch_errors':[]}
        previous_scan = self.store.get_meta('scan_status',{})
        for field in ('last_scan','skipped_count','skipped_paths','scanned_files'):
            if field in previous_scan:
                self.state[field] = previous_scan[field]
        self.last_status = 0
        self.last_flush = 0
        self.last_checkpoint = 0
        self.stop_file = data / 'stop.request'
        self.rescan_file = data / 'rescan.request'

    def status(self, force=False):
        now = time.time()
        if force or now-self.last_status > 2:
            self.state['heartbeat'] = now
            self.state['threshold_mb'] = self.config['threshold_mb']
            self.state['received_events'] = self.pending.received
            self.state['watchers'] = {r: bool(o.is_alive() and all(e.is_alive() for e in o.emitters))
                                      for r,o in self.observers.items()}
            self.state['large_files'] = self.store.db.execute(
                'SELECT count(*) FROM files WHERE present=1 AND size>=?', (self.store.threshold,)).fetchone()[0]
            atomic_json(self.data / 'status.json', self.state)
            self.last_status = now

    def skip(self, path, error):
        self.state['skipped_count'] += 1
        if len(self.state['skipped_paths']) < 50:
            self.state['skipped_paths'].append({'path':str(path),'reason':str(error)})

    def root_baseline(self, path):
        return any(key(path).startswith(key(r)) for r in self.baselining)

    def flush(self):
        paths,moves = self.pending.take()
        for src,dest in moves:
            self.store.move(src,dest)
        for path,(reason,is_dir) in paths.items():
            if is_dir and reason != 'deleted':
                continue
            try:
                info = metadata(path)
                if info:
                    old = self.store.get(path)
                    baseline = self.root_baseline(path) and reason != 'created' and (old is None or not old['present'])
                    # A live create may have already been seen by the concurrent baseline walk.
                    if reason == 'created' and old and old['first_seen'] >= self.initial_event_cutoff:
                        today = self.store.db.execute('SELECT 1 FROM changes WHERE day=? AND path=?',
                                                      (local_day(time.time()),key(path))).fetchone()
                        if not today and info['size'] >= self.store.threshold:
                            self.store.change(path,'new',0,info['size'],time.time(),'created')
                    self.store.observe(info,baseline,reason)
            except FileNotFoundError:
                self.store.remove(path)
            except OSError:
                # An inaccessible file must not be falsely reported as deleted.
                pass
        self.store.db.commit()
        self.last_flush = time.time()

    def checkpoint(self):
        now=time.time()
        if now-self.last_checkpoint < .25:
            return
        self.last_checkpoint=now
        if now-self.last_flush >= 1:
            self.flush()
        self.status()
        if self.stop_file.exists():
            raise InterruptedError('Stop requested')

    def scan(self, root, baseline):
        self.state['phase'] = 'baseline' if baseline else 'reconciling'
        self.state['scanning_root'] = root
        if baseline:
            self.baselining.add(root)
        stack = [root]
        while stack:
            directory = stack.pop()
            if key(directory).startswith(key(self.data)):
                continue
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            attrs = entry.stat(follow_symlinks=False)
                            if entry.is_dir(follow_symlinks=False):
                                if getattr(attrs,'st_file_attributes',0) & 0x400:
                                    self.skip(entry.path,'目录链接 / 挂载点：跳过以避免重复和循环')
                                else:
                                    stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                self.state['scanned_files'] += 1
                                if attrs.st_size >= self.store.threshold:
                                    self.store.observe(metadata(entry.path),baseline,'scan')
                        except OSError as exc:
                            self.skip(entry.path,exc)
                        if self.state['scanned_files'] % 512 == 0:
                            self.checkpoint()
                        if self.state['scanned_files'] % 4096 == 0:
                            time.sleep(.015)
            except OSError as exc:
                self.skip(directory,exc)
            self.checkpoint()
        # Check known files individually: a skipped directory does not imply deletion.
        rows = self.store.db.execute('SELECT display FROM files WHERE present=1 AND substr(path,1,?)=?',
                                     (len(key(root)),key(root))).fetchall()
        for idx,row in enumerate(rows):
            try:
                self.store.observe(metadata(row['display']),baseline,'scan')
            except FileNotFoundError:
                self.store.remove(row['display'])
            except OSError:
                pass
            if idx % 100 == 0:
                self.checkpoint()
        self.flush()
        self.baselining.discard(root)
        self.store.set_meta('last_scan:'+key(root),time.time())
        self.store.db.commit()

    def run(self):
        try:
            while not self.stop_file.exists():
                volumes = [(r,'test') for r in self.roots_override] if self.roots_override else fixed_drives()
                active = {root for root,_ in volumes}
                self.state['roots'] = sorted(active)
                for root in list(self.observers):
                    observer = self.observers[root]
                    healthy = observer.is_alive() and all(e.is_alive() for e in observer.emitters)
                    if root not in active or not healthy:
                        observer.stop()
                        observer.join(3)
                        del self.observers[root]
                        if root in active:
                            self.state['watch_errors'].append(f'{root} 监听中断，正在重连并补扫')
                            self.store.set_meta('last_scan:'+key(root),0)
                force = self.rescan_file.exists() or self.pending.overflow
                self.pending.overflow = False
                if self.rescan_file.exists():
                    self.rescan_file.unlink(missing_ok=True)
                scans = []
                for root,serial in volumes:
                    if root not in self.observers:
                        try:
                            observer = Observer(timeout=1)
                            observer.schedule(self.pending,root,recursive=True)
                            observer.start()
                            self.observers[root] = observer
                        except OSError as exc:
                            self.state['watch_errors'].append(f'{root}: {exc}')
                    volume_key = 'volume:'+key(root)
                    previous_serial = self.store.get_meta(volume_key)
                    baseline = previous_serial != serial
                    if previous_serial is not None and baseline:
                        # Drive letters can be reused. Do not compare a new volume to an old one.
                        self.store.db.execute('UPDATE files SET present=0 WHERE substr(path,1,?)=?',
                                              (len(key(root)),key(root)))
                    last = self.store.get_meta('last_scan:'+key(root),0)
                    if baseline or force or self.startup_scan or time.time()-last > self.config['rescan_hours']*3600:
                        scans.append((root,serial,baseline))
                if scans:
                    scans.sort(key=lambda scan: not scan[2])
                    self.baselining.update(root for root,serial,baseline in scans if baseline)
                    self.state.update(scanned_files=0,skipped_count=0,skipped_paths=[])
                    for root,serial,baseline in scans:
                        self.scan(root,baseline)
                        self.store.set_meta('volume:'+key(root),serial)
                    self.state['last_scan'] = time.time()
                    self.store.set_meta('scan_status',self.state.copy())
                    self.store.prune(self.config['retention_days'])
                self.startup_scan = False
                self.state['phase'] = 'watching'
                self.state['watch_errors'] = self.state['watch_errors'][-10:]
                self.status(True)
                for _ in range(15):
                    if self.stop_file.exists() or self.rescan_file.exists():
                        break
                    self.flush()
                    self.status()
                    time.sleep(2)
        except InterruptedError:
            pass
        except Exception:
            logging.exception('Monitor failed')
            self.state['phase'] = 'error'
            self.state['error'] = '监控遇到错误，请查看 monitor.log 并重新启动'
            raise
        finally:
            for observer in self.observers.values():
                observer.stop()
            for observer in self.observers.values():
                observer.join(3)
            self.flush()
            if self.state['phase'] != 'error':
                self.state['phase'] = 'stopped'
            self.status(True)
            self.store.db.close()


def read_status(data):
    try:
        return json.loads((data / 'status.json').read_text('utf-8'))
    except (OSError,ValueError):
        return {}


def is_running(data):
    s = read_status(data)
    return s.get('phase') not in (None,'stopped','error') and time.time()-s.get('heartbeat',0) < 20


def pythonw():
    path = Path(sys.executable).with_name('pythonw.exe')
    return str(path if path.exists() else Path(sys.executable))


def start_monitor(data):
    if is_running(data):
        return
    data.mkdir(parents=True,exist_ok=True)
    (data/'stop.request').unlink(missing_ok=True)
    subprocess.Popen([pythonw(),str(APP/'monitor.py'),'--daemon','--data',str(data)],
                     cwd=APP,creationflags=0x08000000 | 0x00000008,
                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def daemon(data,roots=None):
    import msvcrt
    data.mkdir(parents=True,exist_ok=True)
    lock = open(data/'monitor.lock','a+b')
    lock.seek(0)
    if not lock.read(1):
        lock.write(b'1')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        lock.close()
        return
    handler = RotatingFileHandler(data/'monitor.log',maxBytes=1_000_000,backupCount=2,encoding='utf-8')
    logging.basicConfig(level=logging.WARNING,handlers=[handler],format='%(asctime)s %(levelname)s %(message)s')
    try:
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetCurrentProcess.restype=ctypes.c_void_p
        kernel.SetPriorityClass.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
        kernel.SetPriorityClass.restype=ctypes.c_int
        if not kernel.SetPriorityClass(kernel.GetCurrentProcess(),0x4000):
            logging.warning('Unable to lower process priority: %s',ctypes.get_last_error())
        while not (data/'stop.request').exists():
            try:
                Monitor(data,roots).run()
                break
            except Exception:
                logging.exception('Restarting monitor after failure')
                for _ in range(15):
                    if (data/'stop.request').exists():
                        break
                    time.sleep(2)
    finally:
        lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--daemon',action='store_true')
    parser.add_argument('--start',action='store_true')
    parser.add_argument('--data',type=Path,default=data_path())
    parser.add_argument('--root',action='append')
    args = parser.parse_args()
    if args.daemon:
        daemon(args.data,args.root)
    elif args.start:
        start_monitor(args.data)
    else:
        from dashboard import open_dashboard
        open_dashboard(args.data)
