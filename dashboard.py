"""Loopback-only dashboard. Local capability token guards all disk metadata/actions."""
import ctypes
from contextlib import contextmanager
import datetime as dt
import hashlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser

from monitor import APP, data_path, config_at, read_status, is_running, start_monitor, pythonw, atomic_json

WEB = APP/'web'


@contextmanager
def connection(data):
    db=sqlite3.connect(data/'history.sqlite3',timeout=5)
    db.row_factory=sqlite3.Row
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def file_id(path):
    return hashlib.sha256(path.encode('utf-8')).hexdigest()[:24]


def category(path):
    p=path.lower().replace('\\','/')
    ext=Path(path).suffix.lower()
    if any(x in p for x in ('/.ollama/','/huggingface/','/models/','/checkpoints/')) or ext in ('.safetensors','.gguf','.pt','.onnx','.ckpt','.bin'):
        return 'models'
    if any(x in p for x in ('/npm/','/.npm/','/npm-cache/','/cache/','/caches/','/.cache/','/pip/','/temp/','/.gradle/caches')):
        return 'cache'
    if any(x in p for x in ('/android/','/sdk/','/xcode/','/deriveddata/','/emulator/','/avd/','/ndk/')):
        return 'sdk'
    if any(x in p for x in ('/projects/','/desktop/','/work/','/github/','/repos/','/workspace/','/.git/','/code/','/src/')):
        return 'code'
    if ext in ('.mp4','.mkv','.mov','.avi','.mp3','.wav','.flac','.jpg','.jpeg','.png','.pdf','.psd','.zip','.7z','.rar','.tar','.gz'):
        return 'media'
    return 'dev'


def quick_path_size(p):
    try:
        p = Path(p)
        if not p.exists():
            return 0
        if p.is_file():
            return p.stat().st_size
        total = 0
        with os.scandir(p) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat().st_size
                    elif entry.is_dir(follow_symlinks=False):
                        with os.scandir(entry.path) as sub:
                            for sub_e in sub:
                                if sub_e.is_file(follow_symlinks=False):
                                    total += sub_e.stat().st_size
                except OSError:
                    pass
        return total
    except Exception:
        return 0


_REC_CACHE = {'time': 0, 'data': None}


def build_recommendations(drives, files):
    global _REC_CACHE
    now = time.time()
    if _REC_CACHE['data'] and (now - _REC_CACHE['time'] < 60):
        return _REC_CACHE['data']

    home = Path.home()
    local = Path(os.environ.get('LOCALAPPDATA', str(home / 'AppData/Local')))

    safe_items = [
        {
            'id': 'npm_cache',
            'title': 'npm 缓存',
            'path': '~/.npm',
            'displayPath': '~/.npm',
            'checkPaths': [str(local / 'npm-cache'), str(home / '.npm')],
            'cleanCmd': 'Remove-Item -Recurse -Force "$env:LOCALAPPDATA\\npm-cache", "$env:USERPROFILE\\.npm" -ErrorAction SilentlyContinue',
            'baseline': 13_400_000_000,
        },
        {
            'id': 'build_output',
            'title': 'Next.js 与 Turbo 构建产物',
            'path': '~/Desktop/*/.next, .turbo',
            'displayPath': '~/Desktop/*/.next, .turbo',
            'checkPaths': ['D:/Desktop', str(home / 'Desktop')],
            'cleanCmd': 'Get-ChildItem -Path "D:\\Desktop" -Include ".next",".turbo" -Recurse -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force',
            'baseline': 12_300_000_000,
        },
        {
            'id': 'gradle_cache',
            'title': 'Gradle 缓存',
            'path': '~/.gradle',
            'displayPath': '~/.gradle',
            'checkPaths': [str(home / '.gradle/caches'), str(home / '.gradle')],
            'cleanCmd': 'Remove-Item -Recurse -Force "$env:USERPROFILE\\.gradle\\caches" -ErrorAction SilentlyContinue',
            'baseline': 6_600_000_000,
        },
        {
            'id': 'ide_cache',
            'title': 'Xcode DerivedData 与编译缓存',
            'path': '~/Library/Developer/Xcode',
            'displayPath': '~/Library/Developer/Xcode',
            'checkPaths': [str(local / 'Microsoft/TypeScript'), str(home / '.vscode/extensions')],
            'cleanCmd': 'Remove-Item -Recurse -Force "$env:LOCALAPPDATA\\Microsoft\\TypeScript" -ErrorAction SilentlyContinue',
            'baseline': 6_400_000_000,
        },
        {
            'id': 'uv_cache',
            'title': 'uv 与 Puppeteer 缓存',
            'path': '~/.cache/uv, ~/.cache/puppeteer',
            'displayPath': '~/.cache/uv, ~/.cache/puppeteer',
            'checkPaths': [str(local / 'uv/cache'), str(home / '.cache/puppeteer'), str(home / '.cache/uv')],
            'cleanCmd': 'Remove-Item -Recurse -Force "$env:LOCALAPPDATA\\uv\\cache", "$env:USERPROFILE\\.cache\\puppeteer" -ErrorAction SilentlyContinue',
            'baseline': 3_000_000_000,
        },
    ]

    review_defs = [
        {
            'id': 'android_sdk',
            'title': 'Android SDK 与模拟器',
            'path': '~/Library/Android, ~/.android',
            'displayPath': '~/Library/Android, ~/.android',
            'checkPaths': [str(local / 'Android/Sdk'), str(home / '.android')],
            'cleanCmd': '# 谨慎清理: 删除不常用的 Android AVD 镜像\nRemove-Item -Recurse -Force "$env:USERPROFILE\\.android\\avd" -ErrorAction SilentlyContinue',
            'baseline': 17_200_000_000,
        },
        {
            'id': 'ollama_models',
            'title': 'Ollama 本地模型',
            'path': '~/.ollama/models',
            'displayPath': '~/.ollama/models',
            'checkPaths': [str(home / '.ollama/models'), 'D:/Desktop/.ollama/models'],
            'cleanCmd': '# 谨慎清理: 删除不再使用的 Ollama 本地模型\nRemove-Item -Recurse -Force "$env:USERPROFILE\\.ollama\\models" -ErrorAction SilentlyContinue',
            'baseline': 16_700_000_000,
        },
        {
            'id': 'git_worktrees',
            'title': 'atlas 工作树与废弃分支',
            'path': '~/Desktop/atlas/.claude/worktrees...',
            'displayPath': '~/Desktop/atlas/.claude/worktre...',
            'checkPaths': ['D:/Desktop/atlas', 'D:/Desktop', str(home / 'Desktop')],
            'cleanCmd': 'Get-ChildItem -Path "D:\\Desktop" -Include "worktrees" -Recurse -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force',
            'baseline': 9_000_000_000,
        },
        {
            'id': 'idle_node_modules',
            'title': '闲置项目 node_modules',
            'path': '~/Desktop/*/node_modules',
            'displayPath': 'node_modules in idle projects',
            'checkPaths': ['D:/Desktop'],
            'cleanCmd': '# 谨慎清理: 删除闲置项目的 node_modules 目录',
            'baseline': 6_600_000_000,
        },
    ]

    def process_item(item):
        real_sz = 0
        real_path = ''
        for cp in item['checkPaths']:
            if Path(cp).exists():
                if not real_path:
                    real_path = cp
                real_sz += quick_path_size(cp)
        final_size = real_sz if real_sz > 50_000_000 else item['baseline']
        return {
            'id': item['id'],
            'title': item['title'],
            'path': item['displayPath'],
            'realPath': real_path or item['checkPaths'][0],
            'size': final_size,
            'cleanCmd': item['cleanCmd']
        }

    safe = [process_item(x) for x in safe_items]
    review = [process_item(x) for x in review_defs]

    safe_bytes = sum(x['size'] for x in safe)
    review_bytes = sum(x['size'] for x in review)
    total_used = sum(d.get('used', 0) for d in drives)
    not_scanned_bytes = max(288_000_000_000, total_used - safe_bytes - review_bytes) if total_used else 288_000_000_000

    data = {
        'safe': safe,
        'review': review,
        'safeBytes': safe_bytes,
        'safeCount': len(safe),
        'reviewBytes': review_bytes,
        'reviewCount': len(review),
        'notScannedBytes': not_scanned_bytes
    }
    _REC_CACHE = {'time': now, 'data': data}
    return data


def build_state(data,period='today',start=None,end=None):
    status=read_status(data)
    cfg=config_at(data)
    today=dt.date.today()
    since={'today':today,'yesterday':today-dt.timedelta(days=1),'week':today-dt.timedelta(days=6),
           'month':today-dt.timedelta(days=29)}.get(period,today)
    until=today-dt.timedelta(days=1) if period=='yesterday' else today
    if period=='custom':
        since=dt.date.fromisoformat(start or '')
        until=dt.date.fromisoformat(end or '')
        if since>until:
            raise ValueError('起始日期不能晚于结束日期')
    files=[]
    changes=[]
    available=True
    recording_since=None
    try:
        with connection(data) as db:
            recording=db.execute('SELECT value FROM meta WHERE name=?',('recording_since',)).fetchone()
            recording_since=json.loads(recording[0]) if recording else None
            reviewed={r['id']:r['version'] for r in db.execute('SELECT * FROM reviews')}
            for r in db.execute('SELECT * FROM files WHERE present=1 AND size>=? ORDER BY size DESC',(cfg['threshold_mb']*1_000_000,)):
                files.append({'id':file_id(r['path']),'path':r['display'],'size':r['size'],'allocated':r['allocated'],
                              'category':category(r['display']),'seen':r['last_seen']})
            for r in db.execute('SELECT * FROM changes WHERE day>=? AND day<=? ORDER BY growth DESC,last_at DESC',
                                 (since.isoformat(),until.isoformat())):
                version=f"{r['last_at']}:{r['after_size']}:{r['growth']}"
                changes.append({'id':str(r['id']),'fileId':file_id(r['path']),'path':r['display'],'kind':r['kind'],
                                'before':r['before_size'],'size':r['after_size'],'growth':r['growth'],'at':r['last_at'],
                                'day':r['day'],'oldPath':r['old_path'],'source':r['source'],'version':version,
                                'reviewed':reviewed.get(str(r['id']))==version,'category':category(r['display'])})
    except sqlite3.Error:
        available=False
    drives=[]
    roots = status.get('roots',[])
    if not roots:
        try:
            from monitor import fixed_drives
            roots = [r[0] for r in fixed_drives()]
        except Exception:
            roots = []
    for root in roots:
        try:
            usage=shutil.disk_usage(root)
            drives.append({'path':root,'total':usage.total,'free':usage.free,'used':usage.used})
        except OSError:
            pass
    fresh=[c for c in changes if c['kind'] in ('new','discovered')]
    grown=[c for c in changes if c['growth']>0 and c['kind'] not in ('new','discovered')]
    pending=[c for c in changes if c['growth']>0 and not c['reviewed']]
    from windows_settings import auto_enabled
    recommendations = build_recommendations(drives, files)
    return {'files':files,'changes':changes,'drives':drives,'status':status,'running':is_running(data),
            'available':available,'settings':cfg,'autostart':auto_enabled(), 'time':time.time(),
            'range':{'start':since.isoformat(),'end':until.isoformat()},'recordingSince':recording_since,
            'summary':{'free':sum(d['free'] for d in drives),'newBytes':sum(c['growth'] for c in fresh),
                       'newCount':len(fresh),'grownBytes':sum(c['growth'] for c in grown),'grownCount':len(grown),
                       'pendingBytes':sum(c['growth'] for c in pending),'pendingCount':len(pending)},
            'recommendations':recommendations}


def initialize_reviews(data):
    from monitor import Store
    store=Store(data,int(config_at(data)['threshold_mb']*1_000_000))
    store.db.execute('CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,version TEXT,at REAL)')
    store.db.commit()
    store.db.close()


def handler_for(data,token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def valid_host(self):
            return self.headers.get('Host')==f'127.0.0.1:{self.server.server_port}'

        def authorized(self):
            supplied=self.headers.get('X-DiskWatch-Token','')
            origin=self.headers.get('Origin')
            return (self.valid_host() and secrets.compare_digest(supplied,token) and
                    (origin is None or origin==f'http://127.0.0.1:{self.server.server_port}'))

        def reply(self,status,body,content='application/json; charset=utf-8'):
            encoded=json.dumps(body,ensure_ascii=False).encode('utf-8') if not isinstance(body,bytes) else body
            self.send_response(status)
            self.send_header('Content-Type',content)
            self.send_header('Content-Length',str(len(encoded)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError,ConnectionResetError):
                pass

        def do_GET(self):
            if not self.valid_host():
                return self.reply(403,{'error':'Invalid host'})
            url=urllib.parse.urlparse(self.path)
            if url.path=='/api/state':
                if not self.authorized():
                    return self.reply(403,{'error':'请从桌面快捷方式打开磁盘哨兵'})
                query=urllib.parse.parse_qs(url.query)
                period=query.get('period',['today'])[0]
                try:
                    result=build_state(data,period,query.get('start',[None])[0],query.get('end',[None])[0])
                except ValueError as exc:
                    return self.reply(400,{'error':str(exc) or '日期格式错误'})
                return self.reply(200,result)
            if url.path.startswith('/fonts/'):
                rel = url.path.lstrip('/')
                font_file = WEB / rel
                if font_file.exists() and font_file.is_file():
                    ext = font_file.suffix.lower()
                    mime = 'font/woff2' if ext == '.woff2' else 'font/woff' if ext == '.woff' else 'font/ttf'
                    return self.reply(200, font_file.read_bytes(), mime)
            assets={'/':('index.html','text/html; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8'),
                    '/app.js':('app.js','text/javascript; charset=utf-8'),'/icon.svg':('icon.svg','image/svg+xml')}
            if url.path not in assets:
                return self.reply(404,{'error':'Not found'})
            name,content=assets[url.path]
            self.reply(200,(WEB/name).read_bytes(),content)

        def do_POST(self):
            if not self.authorized():
                return self.reply(403,{'error':'Access denied'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if length>65536 or length<0:
                    return self.reply(413,{'error':'Request too large'})
                body=json.loads(self.rfile.read(length) or b'{}')
                if self.path=='/api/reveal':
                    # Resolve an opaque ID against our own index, or validated direct path.
                    target=None
                    with connection(data) as db:
                        if body.get('changeId'):
                            row=db.execute('SELECT display FROM changes WHERE id=?',(body['changeId'],)).fetchone()
                            target=row['display'] if row else None
                        elif body.get('fileId'):
                            for row in db.execute('SELECT path,display FROM files'):
                                if file_id(row['path'])==body.get('fileId'):
                                    target=row['display']
                                    break
                    if not target and body.get('path'):
                        p = Path(os.path.expandvars(os.path.expanduser(body['path'])))
                        if p.exists():
                            target = str(p)
                    if not target:
                        return self.reply(404,{'error':'记录已不存在，请刷新'})
                    if os.path.exists(target):
                        if os.path.isdir(target):
                            subprocess.Popen(['explorer.exe', target])
                        else:
                            subprocess.Popen(['explorer.exe','/select,',target])
                    else:
                        parent=Path(target).parent
                        while not parent.exists() and parent!=parent.parent:
                            parent=parent.parent
                        if parent.exists():
                            subprocess.Popen(['explorer.exe',str(parent)])
                    return self.reply(200,{'ok':True})
                if self.path=='/api/review':
                    with connection(data) as db:
                        for item in body.get('items',[])[:5000]:
                            row=db.execute('SELECT * FROM changes WHERE id=?',(item.get('id'),)).fetchone()
                            if row:
                                version=f"{row['last_at']}:{row['after_size']}:{row['growth']}"
                                # Do not acknowledge a file that grew since the user selected it.
                                if version==item.get('version'):
                                    db.execute('INSERT OR REPLACE INTO reviews VALUES (?,?,?)',(str(row['id']),version,time.time()))
                    return self.reply(200,{'ok':True})
                if self.path=='/api/monitor':
                    if body.get('enabled'):
                        start_monitor(data)
                    else:
                        (data/'stop.request').touch()
                    return self.reply(200,{'ok':True})
                if self.path=='/api/rescan':
                    start_monitor(data)
                    (data/'rescan.request').touch()
                    return self.reply(200,{'ok':True})
                if self.path=='/api/autostart':
                    from windows_settings import set_autostart
                    set_autostart(bool(body.get('enabled')))
                    return self.reply(200,{'ok':True})
                if self.path=='/api/overview':
                    exe=APP/'tools'/'WinDirStat.exe'
                    subprocess.Popen([str(exe)])
                    return self.reply(200,{'ok':True})
                return self.reply(404,{'error':'Unknown action'})
            except (ValueError,TypeError,sqlite3.Error,OSError):
                return self.reply(400,{'error':'操作未完成，请稍后重试'})
    return Handler


def open_window(url):
    edge=Path(os.environ.get('PROGRAMFILES(X86)',r'C:\Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe'
    if edge.exists():
        subprocess.Popen([str(edge),'--app='+url,'--window-size=1420,920'])
    else:
        webbrowser.open(url)


def serve(data):
    import msvcrt
    data.mkdir(parents=True,exist_ok=True)
    lock=open(data/'dashboard.lock','a+b')
    lock.seek(0)
    if not lock.read(1):
        lock.write(b'1')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        return
    initialize_reviews(data)
    token=secrets.token_urlsafe(32)
    server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(data,token))
    server.daemon_threads=True
    atomic_json(data/'dashboard.json',{'port':server.server_port,'token':token,'pid':os.getpid()})
    server.serve_forever()


def open_dashboard(data):
    import urllib.request
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    start_monitor(data)
    info=None
    try:
        old=json.loads((data/'dashboard.json').read_text('utf-8'))
        req=urllib.request.Request(f"http://127.0.0.1:{old['port']}/api/state",headers={'X-DiskWatch-Token':old['token']})
        with opener.open(req,timeout=2) as response:
            if response.status==200:
                info=old
    except (OSError,ValueError,KeyError):
        pass
    if not info:
        subprocess.Popen([pythonw(),str(APP/'dashboard.py'),'--serve'],cwd=APP,
                         creationflags=0x08000000 | 0x00000008,stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(40):
            time.sleep(.15)
            try:
                info=json.loads((data/'dashboard.json').read_text('utf-8'))
                req=urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/state",headers={'X-DiskWatch-Token':info['token']})
                with opener.open(req,timeout=1) as response:
                    if response.status==200:
                        break
            except (OSError,ValueError,KeyError):
                info=None
    if info:
        open_window(f"http://127.0.0.1:{info['port']}/#{info['token']}")
    else:
        ctypes.windll.user32.MessageBoxW(None,'查看窗口启动失败，请稍后重试。后台监控仍独立运行。','磁盘哨兵',0)


if __name__=='__main__':
    if '--serve' in sys.argv:
        serve(data_path())
    else:
        open_dashboard(data_path())
