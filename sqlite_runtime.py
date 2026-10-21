"""Load the application's patched SQLite before Python's older system DLL.

The bundled SQLite is public domain; this does not modify global Python.
If SQLite was already imported by an embedder, storage uses rollback journals
on unpatched versions, rather than enabling affected WAL concurrency.
"""
import ctypes
import os
from pathlib import Path
import sys

_library=None
if os.name=='nt' and '_sqlite3' not in sys.modules:
    p=Path(__file__).resolve().parent/'runtime/sqlite3.dll'
    if p.exists():_library=ctypes.WinDLL(str(p))
import sqlite3

def safe_wal():
    v=sqlite3.sqlite_version_info
    return v>=(3,51,3) or (3,50,7)<=v<(3,51,0) or (3,44,6)<=v<(3,45,0)
