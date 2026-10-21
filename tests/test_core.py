import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from storage import initialize,connect,observe,removed,moved,date_range,rollup,meta
from collector import Collector,Observer
from rules import classify,safe_excerpt
from attribution import read_session,SessionIndex
from service import state,detail,handler
from http.server import ThreadingHTTPServer
import urllib.request,urllib.error

WORK=Path(__file__).resolve().parents[1]/'work';WORK.mkdir(exist_ok=True)

class CoreTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(dir=WORK);self.root=Path(self.temp.name);self.data=self.root/'data';initialize(self.data);self.c=connect(self.data)
 def tearDown(self):self.c.close();self.temp.cleanup()
 def test_small_files_are_recorded(self):
  p=str(self.root/'a.txt');observe(self.c,p,17,1,baseline=True);observe(self.c,p,25,2);r=self.c.execute('select * from events').fetchone();self.assertEqual(r['delta'],8);self.assertEqual(r['b'],17)
 def test_baseline_never_generates_changes_or_ai_jobs(self):
  for i in range(20):observe(self.c,str(self.root/f'{i}.txt'),i,1,baseline=True)
  self.assertEqual(self.c.execute('select count(*) from events').fetchone()[0],0);self.assertEqual(self.c.execute('select count(*) from ai_jobs').fetchone()[0],0)
 def test_unknown_old_size_does_not_invent_disk_growth(self):
  observe(self.c,str(self.root/'unknown'),1000,1);r=self.c.execute('select * from events').fetchone();self.assertIsNone(r['delta']);self.assertEqual(r['kind'],'discovered')
 def test_rename_then_delete(self):
  a=str(self.root/'a.txt');b=str(self.root/'b.txt');observe(self.c,a,20,1,baseline=True);moved(self.c,a,b);removed(self.c,b)
  self.assertEqual([r['delta'] for r in self.c.execute('select * from events order by id')],[0,-20]);self.assertIsNone(self.c.execute('select * from files').fetchone())
 def test_directory_prefix_delete_is_bounded(self):
  a=self.root/'a';p=a/'x';q=self.root/'also/x';observe(self.c,str(p),20,1,baseline=True);observe(self.c,str(q),30,1,baseline=True);removed(self.c,str(a));self.assertEqual(self.c.execute('select p from files').fetchone()[0],str(q))
 def test_timestamp_range_inclusive_dates_and_exclusive_times(self):
  a,b=date_range('custom','2026-09-10','2026-09-29');self.assertEqual(dt.datetime.fromtimestamp(a).day,10);self.assertEqual(dt.datetime.fromtimestamp(b).day,30)
  a,b=date_range('custom','2026-09-10T10:30','2026-09-10T11:45');self.assertEqual(b-a,4500)
  with self.assertRaises(ValueError):date_range('custom','2026-09-30','2026-09-10')
 def test_query_exact_interval_and_unknown_gap(self):
  a,b=date_range('custom','2026-09-10','2026-09-29');p=str(self.root/'f');observe(self.c,p,1,1,baseline=True);observe(self.c,p,10,2,ts=a);observe(self.c,p,20,3,ts=b-1);observe(self.c,p,40,4,ts=b);self.c.commit()
  d=state(self.data,{'period':'custom','start':'2026-09-10','end':'2026-09-29'});self.assertEqual(d['summary']['net'],19);self.assertEqual(d['totalEvents'],2);self.assertEqual(d['disks'],[])
 def test_volume_reconciliation_is_measured_not_fabricated(self):
  a,b=date_range();self.c.executemany('insert into volumes values (?,?,?,?)',[(a+10,'C:\\',1000,2000),(a+20,'C:\\',600,2000)]);observe(self.c,'C:\\file',50,1,baseline=True);observe(self.c,'C:\\file',150,2,ts=a+15);self.c.commit();d=state(self.data,{});self.assertEqual(d['disks'][0]['consumed'],400);self.assertEqual(d['disks'][0]['explained'],100);self.assertEqual(d['disks'][0]['unexplained'],300)
 def test_rules_never_call_models_or_claim_system_safe(self):
  self.assertEqual(classify(r'C:\Windows\system32\a.dll')['risk'],'keep');self.assertEqual(classify(r'C:\repo\.git\objects\abc')['risk'],'review');self.assertEqual(classify(r'C:\repo\__pycache__\a.pyc')['risk'],'rebuildable');self.assertEqual(classify(r'C:\repo\node_modules\a.js')['risk'],'review')
 def test_secret_content_is_never_read(self):
  p=self.root/'.env';p.write_text('API_KEY=secret');self.assertIsNone(safe_excerpt(p));self.assertEqual(classify(p)['risk'],'keep')
 def test_excerpt_redacts_credentials_and_caps_bytes(self):
  p=self.root/'test.py';p.write_text('api_key="sk-secret123456789"\n'+'x'*10000);text=safe_excerpt(p,100);self.assertNotIn('sk-secret',text);self.assertLess(len(text),150)
 def test_session_tool_evidence(self):
  p=self.root/'s.jsonl';target=str(self.root/'file.py');ts=time.time();p.write_text(json.dumps({'type':'session_meta','payload':{'id':'test-session','cwd':str(self.root)}})+'\n'+json.dumps({'timestamp':dt.datetime.fromtimestamp(ts,dt.timezone.utc).isoformat(),'payload':{'type':'function_call','name':'apply_patch','arguments':json.dumps({'patch':'*** Update File: '+target})}}))
  parsed=read_session(p,'Codex');i=SessionIndex();i.sessions[str(p)]=(ts,parsed);self.assertEqual(i.match(target,ts)['confidence'],'tool-evidence');self.assertEqual(i.match(str(self.root/'unrelated'),ts)['confidence'],'unknown')
 def test_details_unknown_path_is_rejected(self):
  with self.assertRaises(ValueError):detail(self.data,str(self.root/'not-indexed'))
 def test_readonly_ai_uses_no_tools_and_cannot_modify_file(self):
  from ai_analysis import analyze
  p=self.root/'script.py';p.write_text('print(42)');before=hashlib.sha256(p.read_bytes()).hexdigest()
  with patch('ai_analysis.completion',return_value={'summary':'test'}) as call:
   analyze(p);self.assertEqual(call.call_args.args[0]['bytes'],9);self.assertNotIn('tools',call.call_args.args[0])
  self.assertEqual(before,hashlib.sha256(p.read_bytes()).hexdigest())
 def test_disabled_or_no_api_preserves_local_features(self):
  with patch('ai_analysis.discover',return_value={'key':None,'model':'deepseek-flash','url':'https://example.com'}):
   self.assertFalse(state(self.data,{})['ai']['configured']);self.assertIsInstance(state(self.data,{})['candidates'],list)
 def test_api_rejects_delete_execute_and_wrong_origin(self):
  srv=ThreadingHTTPServer(('127.0.0.1',0),handler(self.data,'test'));t=threading.Thread(target=srv.serve_forever,daemon=True);t.start();base=f'http://127.0.0.1:{srv.server_port}';opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
  try:
   for route in ('/api/delete','/api/execute','/api/edit'):
    req=urllib.request.Request(base+route,data=b'{}',headers={'X-DiskSentry-Token':'test'})
    with self.assertRaises(urllib.error.HTTPError) as error:opener.open(req)
    self.assertEqual(error.exception.code,404)
   req=urllib.request.Request(base+'/api/state',headers={'X-DiskSentry-Token':'test','Origin':'https://evil.example'})
   with self.assertRaises(urllib.error.HTTPError) as error:opener.open(req)
   self.assertEqual(error.exception.code,403)
  finally:srv.shutdown();srv.server_close();t.join()
 def test_resumable_scan_and_small_live_events(self):
  watch=self.root/'watched';watch.mkdir();(watch/'before.txt').write_text('baseline')
  holder={}
  def run():
   c=Collector(self.data,[str(watch)]);holder['collector']=c;c.run()
  t=threading.Thread(target=run);t.start()
  try:
   self.wait(lambda:self.read_status()['phase']=='watching')
   (watch/'tiny.txt').write_text('new small file')
   self.wait(lambda:self.event_exists(str(watch/'tiny.txt'),'created'))
   (watch/'tiny.txt').write_text('now bigger than before small file')
   self.wait(lambda:self.event_exists(str(watch/'tiny.txt'),'grown'))
   (watch/'tiny.txt').rename(watch/'renamed.txt');self.wait(lambda:self.event_exists(str(watch/'renamed.txt'),'moved'))
   (watch/'renamed.txt').unlink();self.wait(lambda:self.event_exists(str(watch/'renamed.txt'),'deleted'))
  finally:
   if 'collector' in holder:holder['collector'].stop.set()
   t.join(5)
  self.assertFalse(t.is_alive());self.assertEqual(self.c.execute('select count(*) from scan_queue').fetchone()[0],0)
 def read_status(self):
  try:return json.loads((self.data/'status.json').read_text())
  except (OSError,ValueError):return {'phase':'starting'}
 def event_exists(self,p,kind):return self.c.execute('select 1 from events where p=? and kind=?',(p,kind)).fetchone() is not None
 def wait(self,f):
  end=time.time()+12
  while time.time()<end:
   if f():return
   time.sleep(.1)
  self.fail('Real collector did not update within 12 seconds')

if __name__=='__main__':unittest.main(verbosity=2)
