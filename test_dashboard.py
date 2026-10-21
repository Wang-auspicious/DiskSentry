import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from monitor import Store, metadata, Monitor
from dashboard import build_state, initialize_reviews, handler_for

WORK=Path(__file__).resolve().parents[2]/'work'


def timestamp(day,hour=12):
    return dt.datetime.fromisoformat(day+f'T{hour:02}:00:00').timestamp()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=WORK,prefix='history-test-')
        self.root=Path(self.tmp.name)
        self.data=self.root/'data'
        self.data.mkdir()
        (self.data/'settings.json').write_text(json.dumps({'threshold_mb':.000001}))
        initialize_reviews(self.data)
        self.store=Store(self.data,1)
        self.path=self.root/'history.bin'
        self.path.write_bytes(b'x'*100)
        self.store.observe(metadata(self.path),True,now=timestamp('2026-09-09'))
        for day,n,hour in [('2026-09-10',200,0),('2026-09-29',350,23),('2026-09-30',400,0)]:
            self.path.write_bytes(b'x'*n)
            self.store.observe(metadata(self.path),reason='modified',now=timestamp(day,hour))
        self.store.db.commit()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_custom_range_includes_both_end_dates_only(self):
        state=build_state(self.data,'custom','2026-09-10','2026-09-29')
        self.assertEqual(sorted(c['day'] for c in state['changes']),['2026-09-10','2026-09-29'])
        self.assertEqual(state['summary']['grownBytes'],250)
        self.assertEqual(state['summary']['newBytes'],0)

    def test_same_day_and_invalid_date_ranges(self):
        state=build_state(self.data,'custom','2026-09-29','2026-09-29')
        self.assertEqual(state['summary']['grownBytes'],150)
        for start,end in [('2026-09-30','2026-09-10'),('bad','2026-09-10')]:
            with self.assertRaises(ValueError):
                build_state(self.data,'custom',start,end)

    def test_history_retained_when_retention_is_zero(self):
        self.store.prune(0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM changes').fetchone()[0],3)

    def test_reviewed_file_reappears_after_growth(self):
        row=build_state(self.data,'custom','2026-09-10','2026-09-10')['changes'][0]
        self.store.db.execute('INSERT INTO reviews VALUES (?,?,?)',(row['id'],row['version'],time.time()))
        self.store.db.commit()
        checked=build_state(self.data,'custom','2026-09-10','2026-09-10')['changes'][0]
        self.assertTrue(checked['reviewed'])
        self.path.write_bytes(b'x'*600)
        self.store.observe(metadata(self.path),reason='modified',now=timestamp('2026-09-10',20))
        self.store.db.commit()
        changed=build_state(self.data,'custom','2026-09-10','2026-09-10')['changes'][0]
        self.assertFalse(changed['reviewed'])

    def test_baseline_restart_does_not_report_existing_file_as_new(self):
        self.store.db.execute('UPDATE files SET present=0')
        self.store.db.execute('DELETE FROM changes')
        self.store.db.commit()
        monitor=Monitor(self.data,[str(self.root)])
        try:
            monitor.baselining.add(str(self.root))
            monitor.pending.paths[str(self.path)]=('modified',False)
            monitor.flush()
            self.assertEqual(monitor.store.db.execute('SELECT count(*) FROM changes').fetchone()[0],0)
        finally:
            monitor.store.db.close()

    def test_http_access_and_review_guard(self):
        token='test-token-only'
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.data,token))
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def request(path,body=None,headers=None):
            h={'X-DiskWatch-Token':token}|(headers or {})
            encoded=json.dumps(body).encode() if body is not None else None
            return opener.open(urllib.request.Request(base+path,data=encoded,headers=h),timeout=3)
        try:
            with request('/api/state?period=custom&start=2026-09-10&end=2026-09-29') as response:
                self.assertEqual(len(json.load(response)['changes']),2)
            for headers in [{'X-DiskWatch-Token':'wrong'},{'Origin':'https://example.com'},{'Host':'evil.example'}]:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    request('/api/state',headers=headers)
                self.assertEqual(caught.exception.code,403)
            row=build_state(self.data,'custom','2026-09-10','2026-09-10')['changes'][0]
            with request('/api/review',{'items':[{'id':row['id'],'version':'stale'}]}) as response:
                self.assertEqual(response.status,200)
            self.assertFalse(build_state(self.data,'custom','2026-09-10','2026-09-10')['changes'][0]['reviewed'])
            with request('/api/review',{'items':[{'id':row['id'],'version':row['version']}]}) as response:
                self.assertEqual(response.status,200)
            self.assertTrue(build_state(self.data,'custom','2026-09-10','2026-09-10')['changes'][0]['reviewed'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__=='__main__':
    unittest.main(verbosity=2)
