"""Local JSONL evidence correlation. Never invents a responsible process."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import threading
import time

WRITE_TOOLS=('write','edit','apply_patch','create_file','multiedit','notebookedit')

def timestamp(value):
    try:
        if isinstance(value,(int,float)):return value/1000 if value>1e12 else value
        return dt.datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError):return 0

def read_session(path,agent,max_bytes=262144):
    """Only bounded tail + first metadata line. Raw prompts are never stored/sent."""
    records=[];cwd='';session=Path(path).stem
    try:
        with open(path,'rb') as f:
            first=f.readline(16384)
            try:head=json.loads(first);cwd=head.get('cwd') or head.get('payload',{}).get('cwd','');session=head.get('id') or head.get('payload',{}).get('id',session)
            except (ValueError,TypeError):pass
            f.seek(0,2);length=f.tell();f.seek(max(0,length-max_bytes));raw=f.read(max_bytes)
        if length>max_bytes:raw=raw.split(b'\n',1)[-1]
        for line in raw.splitlines():
            try:obj=json.loads(line)
            except (ValueError,UnicodeError):continue
            ts=timestamp(obj.get('timestamp') or obj.get('time') or obj.get('created_at'));payload=obj.get('payload') or obj.get('message') or obj
            if not isinstance(payload,dict):continue
            calls=[]
            if payload.get('type') in ('function_call','tool_use','toolCall'):calls.append(payload)
            for c in payload.get('content',[]) if isinstance(payload.get('content'),list) else []:
                if isinstance(c,dict) and c.get('type') in ('tool_use','toolCall','function_call'):calls.append(c)
            for call in calls:
                tool=str(call.get('name') or call.get('toolName') or '')
                args=call.get('arguments',call.get('input',{}))
                if isinstance(args,str):
                    try:args=json.loads(args)
                    except ValueError:args={'patch':args[:30000]}
                if not isinstance(args,dict):continue
                paths=[args.get(k) for k in ('path','file_path','filePath','target_file') if isinstance(args.get(k),str)]
                patch_write=False
                for value in args.values():
                    if isinstance(value,str) and '*** ' in value:
                        normalized_patch=value.replace('\\n','\n').replace('\\\\','\\')
                        targets=re.findall(r'\*\*\* (?:Add|Update|Delete) File: ([^\n\r\"]+)',normalized_patch)
                        paths+=targets;patch_write=patch_write or bool(targets)
                for target in paths:
                    if not os.path.isabs(target) and cwd:target=os.path.join(cwd,target)
                    records.append({'path':os.path.normcase(os.path.abspath(target)),'ts':ts,'tool':tool,'write':patch_write or any(t in tool.lower() for t in WRITE_TOOLS)})
        return {'agent':agent,'session':str(session),'jsonl':str(path),'cwd':cwd,'records':records[-200:]}
    except OSError:return None

class SessionIndex:
    def __init__(self):self.lock=threading.Lock();self.sessions={};self.last_refresh=0;self.paths={}
    def refresh(self):
        home=Path.home();roots=[(home/'.codex/sessions','Codex'),(home/'.claude/projects','Claude Code'),(home/'.pi/agent/sessions','Pi Agent'),(home/'.local/share/opencode/storage/session','OpenCode')]
        candidates=[]
        for root,agent in roots:
            if not root.exists():continue
            for directory,dirs,files in os.walk(root):
                dirs[:]=[d for d in dirs if d not in ('node_modules','.git')]
                for name in files:
                    if name.endswith('.jsonl'):
                        p=Path(directory)/name
                        try:candidates.append((p.stat().st_mtime,p,agent))
                        except OSError:pass
        recent=sorted(candidates,reverse=True)[:200]
        with self.lock:
            for m,p,agent in recent:
                old=self.sessions.get(str(p))
                if not old or old[0]!=m:self.sessions[str(p)]=(m,read_session(p,agent))
            self.last_refresh=time.time()
            self._reindex()
    def _reindex(self):
        self.paths={}
        for modified,s in self.sessions.values():
            if s:
                for r in s['records']:self.paths.setdefault(r['path'],[]).append((s,r))
    def match(self,path,when=None):
        normalized=os.path.normcase(os.path.abspath(path));when=when or time.time();best=None
        with self.lock:
            if not self.paths:self._reindex()
            for modified,s in self.sessions.values():
                if not s:continue
                if normalized==os.path.normcase(os.path.abspath(s['jsonl'])):
                    return dict(agent=s['agent'],confidence='direct',reason='此文件本身是 Agent 会话日志',session=s['session'],jsonl=s['jsonl'],time=modified)
            for s,r in self.paths.get(normalized,[]):
                if abs(r['ts']-when)<=300:
                    candidate=dict(agent=s['agent'],confidence='tool-evidence' if r['write'] else 'reference',reason='会话中存在写入工具调用（与文件事件时间相关）' if r['write'] else '会话提及文件，不能证明写入者',session=s['session'],jsonl=s['jsonl'],tool=r['tool'],time=r['ts'])
                    if r['write']:return candidate
                    best=candidate
        if best:return best
        p=normalized.replace('\\','/')
        for marker,agent in [('/.codex/','Codex'),('/.pi/','Pi Agent'),('/.claude/','Claude Code'),('/opencode/','OpenCode')]:
            if marker in p:return dict(agent=agent,confidence='path-inference',reason='依据目录推测，未获取写入进程或具体对话证据')
        return dict(agent='未知 / 用户程序',confidence='unknown',reason='没有可核对的写入进程或会话证据，不能强行归因给用户或 Agent')

INDEX=SessionIndex()

def start_indexer(stop):
    def loop():
        while not stop.is_set():
            try:INDEX.refresh()
            except Exception:pass
            stop.wait(30)
    t=threading.Thread(target=loop,daemon=True);t.start();return t
