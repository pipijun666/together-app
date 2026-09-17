import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'operations'))
from backup_sqlite import create_snapshot,prune
from verify_backup import verify_snapshot


class BackupTests(unittest.TestCase):
    def test_online_snapshot_and_checksum(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'app.sqlite3';destination=root/'backups'
            db=sqlite3.connect(source)
            try:
                db.execute('CREATE TABLE sample(id INTEGER PRIMARY KEY,value TEXT)')
                db.execute('INSERT INTO sample(value) VALUES(?)',('preserved',))
                db.commit()
            finally:
                db.close()
            snapshot=create_snapshot(source,destination)
            metadata=verify_snapshot(snapshot)
            self.assertEqual(metadata['size'],snapshot.stat().st_size)
            db=sqlite3.connect(snapshot)
            try:
                self.assertEqual(db.execute('SELECT value FROM sample').fetchone()[0],'preserved')
            finally:
                db.close()

    def test_prune_keeps_requested_recent_snapshots(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for stamp in ('20260101T000000Z','20260101T010000Z','20260101T020000Z'):
                path=root/f'app-{stamp}.sqlite3';path.write_bytes(b'db')
                path.with_suffix(path.suffix+'.json').write_text('{}',encoding='utf-8')
            prune(root,keep_recent=2,keep_daily=0)
            self.assertEqual(len(list(root.glob('*.sqlite3'))),2)
            self.assertEqual(len(list(root.glob('*.json'))),2)


if __name__=='__main__':
    unittest.main()
