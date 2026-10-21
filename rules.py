"""Offline classifications are evidence-based hints, never permission to delete."""
from pathlib import Path
import re
import time

RULE_VERSION='2.0'
SENSITIVE=re.compile(r'(^|[/\\])(?:\.env(?:\..*)?|auth\.json|credentials[^/\\]*|id_rsa|id_ed25519|[^/\\]*\.(?:pem|key|pfx|kdbx))$',re.I)
CACHE_SEGMENTS={'node_modules':('依赖目录','review','可重装依赖；先确认包清单、锁文件和未修改依赖'),
 '.next':('构建产物','rebuildable','Next.js 生成目录；关闭开发服务后可重新构建'),
 '.turbo':('构建缓存','rebuildable','Turbo 缓存；可重新生成'),
 '__pycache__':('Python 字节码','rebuildable','Python 自动生成的字节码，可重新生成'),
 '.pytest_cache':('测试缓存','rebuildable','pytest 自动生成的缓存，可重新生成'),
 '.ruff_cache':('检查缓存','rebuildable','Ruff 检查缓存，可重新生成'),
 'npm-cache':('npm 缓存','rebuildable','包下载缓存，可重新下载；离线工作前保留'),
 '.npm':('npm 缓存','rebuildable','包下载缓存，可重新下载；离线工作前保留')}

def classify(path,mtime=0,now=None):
    now=now or time.time();p=str(path).replace('\\','/');low=p.lower();parts=low.split('/')
    if SENSITIVE.search(p):
        return dict(category='secrets',risk='keep',reason='凭据或密钥文件，禁止读取内容或发送 AI',bucket=None)
    if any(s in parts for s in ('.git','worktrees','sessions')) or low.endswith(('.jsonl','.sqlite','.sqlite3','.db')):
        return dict(category='agent' if any(x in low for x in ('.codex','.pi/','.claude','opencode','jsonl')) else 'data',risk='review',reason='会话、数据库或版本历史可能不可重建；核对保留需求',bucket=None)
    if low.endswith(('.sys','.dll','.exe','.vhdx','.vhd')) or '/windows/' in low:
        return dict(category='system',risk='keep',reason='系统、程序或虚拟磁盘，不建议直接手动删除',bucket=None)
    for segment,(category,risk,reason) in CACHE_SEGMENTS.items():
        if segment in parts:
            i=parts.index(segment);bucket='/'.join(p.split('/')[:i+1])
            return dict(category='cache',risk=risk,reason=reason,bucket=bucket)
    for marker,label in [('/.cache/uv/','uv 缓存'),('/uv/cache/','uv 缓存'),('/pip/cache/','pip 缓存'),('/pip/cache','pip 缓存'),('/.gradle/caches/','Gradle 缓存'),('/cache/cache_data/','浏览器缓存'),('/code cache/','浏览器编译缓存'),('/gpucache/','GPU 缓存')]:
        if marker in low:
            pos=low.index(marker)+len(marker.rstrip('/'));bucket=p[:pos]
            return dict(category='cache',risk='rebuildable',reason=label+'，可重新生成；先关闭占用程序',bucket=bucket)
    if '/temp/' in low or '/tmp/' in low or low.endswith(('.tmp','.temp','.dmp')):
        age=max(0,now-mtime) if mtime else 0
        return dict(category='temporary',risk='review' if age<7*86400 else 'candidate',reason='临时或转储文件；核对程序是否仍在使用'+('，已超过 7 天' if age>=7*86400 else ''),bucket=p.rsplit('/',1)[0])
    if any(x in low for x in ('/.ollama/','/huggingface/','/models/')) or low.endswith(('.gguf','.safetensors','.ckpt','.onnx','.pt')):
        return dict(category='model',risk='review',reason='模型权重；确认不再使用且可重新下载后再决定',bucket=None)
    if any(x in low for x in ('/.codex/','/.claude/','/.pi/','/opencode/')):
        return dict(category='agent',risk='review',reason='AI Agent 配置、会话或运行产物，不能仅按路径判断可删',bucket=None)
    if Path(low).suffix in ('.zip','.7z','.rar','.tar','.gz','.iso'):
        return dict(category='archive',risk='review',reason='归档或安装介质，确认不是唯一备份',bucket=None)
    if Path(low).suffix in ('.mp4','.mkv','.mov','.png','.jpg','.pdf','.docx','.csv','.h5','.h5ad'):
        return dict(category='data',risk='review',reason='个人文档、媒体或数据，保留需求需要本人确认',bucket=None)
    return dict(category='other',risk='review',reason='暂无足够证据判断用途，先查看来源与文件内容',bucket=None)

def safe_excerpt(path,limit=4000):
    """Only opted-in AI jobs call this; no credentials, logs, databases or binaries."""
    if SENSITIVE.search(str(path)) or classify(path)['category'] in ('secrets','agent','system'):
        return None
    if Path(path).suffix.lower() not in ('.txt','.md','.py','.js','.ts','.tsx','.jsx','.css','.html','.toml','.yaml','.yml','.json','.csv'):
        return None
    try:
        with open(path,'rb') as f:raw=f.read(limit)
        text=raw.decode('utf-8')
        if '\x00' in text:return None
        text=re.sub(r'(?i)(api[_-]?key|password|token|secret)([\s\"\x27:=]+)[^\s,\"\x27]+',r'\1\2[REDACTED]',text)
        text=re.sub(r'\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}', '[REDACTED]',text)
        return text
    except (OSError,UnicodeError):return None
