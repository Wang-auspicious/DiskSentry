import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from monitor import Monitor, Store, metadata, key, Observer

WORK = Path(__file__).resolve().parents[2] / 'work'
WORK.mkdir(exist_ok=True)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=WORK,prefix='diskwatch-test-')
        self.base=Path(self.temp.name)
        self.root=self.base/'files'
        self.root.mkdir()
        self.store=Store(self.base/'data',100)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def file(self,name,size):
        path=self.root/name
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('wb') as f:
            f.write(b'x'*size)
        return path

    def changes(self):
        return self.store.db.execute('SELECT * FROM changes ORDER BY id').fetchall()

    def test_baseline_is_not_new_and_growth_is_merged(self):
        path=self.file('已有文件.bin',150)
        self.store.observe(metadata(path),baseline=True)
        self.assertEqual(len(self.changes()),0)
        self.file('已有文件.bin',230)
        self.store.observe(metadata(path),reason='modified')
        self.file('已有文件.bin',250)
        self.store.observe(metadata(path),reason='modified')
        rows=self.changes()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['kind'],'grown')
        self.assertEqual((rows[0]['before_size'],rows[0]['after_size'],rows[0]['growth']),(150,250,100))

    def test_new_rename_delete_preserves_growth(self):
        path=self.file('下载.tmp',200)
        self.store.observe(metadata(path),reason='created')
        dest=self.root/'最终 文件.bin'
        path.rename(dest)
        self.store.move(str(path),str(dest))
        self.store.observe(metadata(dest),reason='moved')
        self.assertEqual(len(self.changes()),1)
        self.assertEqual(self.changes()[0]['growth'],200)
        self.assertEqual(self.changes()[0]['kind'],'new')
        dest.unlink()
        self.store.remove(str(dest))
        self.assertEqual(self.changes()[0]['after_size'],0)
        self.assertEqual(self.store.get(dest)['present'],0)

    def test_below_threshold_then_crossing(self):
        path=self.file('small.bin',70)
        self.store.observe(metadata(path),reason='created')
        self.assertIsNone(self.store.get(path))
        self.file('small.bin',150)
        self.store.observe(metadata(path),reason='modified')
        self.assertEqual(self.changes()[0]['kind'],'discovered')

    def test_case_normalization_and_folder_move(self):
        path=self.file('folder/Sub/A.bin',160)
        self.store.observe(metadata(path),baseline=True)
        (self.root/'folder').rename(self.root/'renamed')
        self.store.move(str(self.root/'folder'),str(self.root/'renamed'))
        moved=self.root/'renamed/Sub/A.bin'
        self.assertEqual(self.store.get(str(moved).upper())['size'],160)
        self.assertEqual(self.changes()[0]['growth'],0)

    def test_disappeared_file_found_during_rescan(self):
        path=self.file('missing.bin',140)
        self.store.observe(metadata(path),baseline=True)
        self.store.db.commit()
        path.unlink()
        config={'threshold_mb':.0001}
        (self.base/'data'/'settings.json').write_text(json.dumps(config))
        m=Monitor(self.base/'data',[str(self.root)])
        try:
            m.scan(str(self.root),False)
            self.assertEqual(m.store.get(path)['present'],0)
        finally:
            m.store.db.close()

    def test_permission_error_does_not_mean_deleted(self):
        path=self.file('locked.bin',130)
        self.store.observe(metadata(path),baseline=True)
        self.store.db.commit()
        m=Monitor(self.base/'data',[str(self.root)])
        try:
            with patch('monitor.metadata',side_effect=PermissionError('test ACL')):
                m.scan(str(self.root),False)
            self.assertEqual(m.store.get(path)['present'],1)
        finally:
            m.store.db.close()

    def test_history_survives_restart(self):
        path=self.file('persist.bin',120)
        self.store.observe(metadata(path),reason='created')
        self.store.db.commit()
        other=Store(self.base/'data',100)
        try:
            self.assertEqual(other.get(path)['size'],120)
            self.assertEqual(other.db.execute('SELECT count(*) FROM changes').fetchone()[0],1)
        finally:
            other.db.close()

    def test_hardlink_is_not_treated_as_a_rename(self):
        import os
        path=self.file('original.bin',180)
        self.store.observe(metadata(path),baseline=True)
        other=self.root/'link.bin'
        os.link(path,other)
        self.store.observe(metadata(other),reason='created')
        self.assertIsNotNone(self.store.get(path))
        self.assertIsNotNone(self.store.get(other))


class LiveWindowsTest(unittest.TestCase):
    def test_real_windows_events(self):
        with tempfile.TemporaryDirectory(dir=WORK,prefix='diskwatch-live-') as temp:
            base=Path(temp)
            root=base/'watched'
            root.mkdir()
            data=base/'data'
            data.mkdir()
            (data/'settings.json').write_text(json.dumps({'threshold_mb':.001}))
            existing=root/'existing.bin'
            existing.write_bytes(b'A'*2000)
            m=Monitor(data,[str(root)])
            observer=Observer()
            observer.schedule(m.pending,str(root),recursive=True)
            observer.start()
            try:
                m.scan(str(root),True)
                p=root/'incoming.bin'
                p.write_bytes(b'B'*3000)
                self.wait_for(m,lambda:m.store.get(p) is not None)
                self.assertEqual(m.store.db.execute('SELECT kind FROM changes WHERE path=?',(key(p),)).fetchone()[0],'new')
                with existing.open('ab') as f:
                    f.write(b'C'*1500)
                self.wait_for(m,lambda:m.store.get(existing)['size']==3500)
                dest=root/'renamed.bin'
                p.rename(dest)
                self.wait_for(m,lambda:m.store.get(dest) is not None)
                dest.unlink()
                self.wait_for(m,lambda:m.store.get(dest)['present']==0)
                self.assertEqual(m.store.db.execute('SELECT growth FROM changes WHERE path=?',(key(existing),)).fetchone()[0],1500)
            finally:
                observer.stop()
                observer.join(5)
                m.store.db.close()

    def wait_for(self,m,condition):
        deadline=time.time()+8
        while time.time()<deadline:
            m.flush()
            if condition():
                return
            time.sleep(.15)
        self.fail('Real Windows change was not captured within 8 seconds')


if __name__=='__main__':
    unittest.main(verbosity=2)
