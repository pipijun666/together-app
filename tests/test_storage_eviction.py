import unittest
import tempfile
import time
import os
import shutil
import hashlib
import json
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, r'c:\Users\pipijun\Desktop\android_lab\apk_lab\beta')
from server import App

class TestStorageEvictionAndTranslate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = App(self.tmp, 'a'*32, max_media_bytes=1000)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_eviction_protects_manual_evicts_system(self):
        with self.app.db() as db:
            # 1. Insert an old manual share
            db.execute("INSERT INTO shares(id,author,url,title,note,blocks,metadata,created,status,source_kind) VALUES('m1','one','','manual','','[]','{}',100,'published','manual')")
            db.execute("INSERT INTO assets(id,share_id,mime,size,kind,object_key,sha256) VALUES('am1','m1','image/webp',400,'image','','sha1')")
            # 2. Insert an old system share
            db.execute("INSERT INTO shares(id,author,url,title,note,blocks,metadata,created,status,source_kind) VALUES('s1','system','','sys1','','[]','{}',110,'published','system')")
            db.execute("INSERT INTO assets(id,share_id,mime,size,kind,object_key,sha256) VALUES('as1','s1','image/webp',400,'image','','sha2')")
            
            # Current usage is 800 bytes (limit 1000)
            self.assertEqual(self.app.media_usage(db), 800)
            
            # Now request additional 300 bytes (total 1100 > 1000)
            self.app.ensure_media_capacity(db, 300)
            
            # System share s1 should have been evicted!
            self.assertIsNone(db.execute("SELECT id FROM shares WHERE id='s1'").fetchone())
            self.assertIsNone(db.execute("SELECT id FROM assets WHERE id='as1'").fetchone())
            
            # Manual share m1 must be preserved!
            self.assertIsNotNone(db.execute("SELECT id FROM shares WHERE id='m1'").fetchone())
            self.assertIsNotNone(db.execute("SELECT id FROM assets WHERE id='am1'").fetchone())
            
            # Remaining usage is 400 bytes
            self.assertEqual(self.app.media_usage(db), 400)

    def test_translate_caching(self):
        with self.app.db() as db:
            h = hashlib.sha256('Hello\n---\nWorld'.encode('utf-8')).hexdigest()
            db.execute('CREATE TABLE IF NOT EXISTS translations (hash TEXT PRIMARY KEY, result_json TEXT NOT NULL, created REAL NOT NULL)')
            db.execute('INSERT INTO translations VALUES(?,?,?)', (h, json.dumps({'title': '你好', 'body': '世界'}), time.time()))
            
            res, _ = self.app.translate_content(db, {'title': 'Hello', 'body': 'World'})
            self.assertEqual(res['title'], '你好')
            self.assertEqual(res['body'], '世界')
            self.assertTrue(res.get('cached'))

if __name__ == '__main__':
    unittest.main()
