import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from storage import MediaStorage, StorageError, storage_from_env


class FakeRemote:
    def __init__(self):
        self.objects = {}

    def upload(self, path, key, mime, metadata):
        self.objects[key] = (Path(path).read_bytes(), mime, metadata)

    def download(self, key, destination):
        Path(destination).write_bytes(self.objects[key][0])

    def delete(self, key):
        self.objects.pop(key, None)


class MediaStorageTests(unittest.TestCase):
    def test_remote_publish_restore_and_delete(self):
        with tempfile.TemporaryDirectory() as folder:
            remote = FakeRemote()
            storage = MediaStorage(folder, remote)
            asset_id = 'a' * 64
            source = Path(folder) / asset_id
            source.write_bytes(b'example-media')

            stored = storage.publish('b' * 32, asset_id, 'video/mp4')
            self.assertEqual(stored['object_key'], f"media/{'b' * 32}/{asset_id}.mp4")
            self.assertEqual(remote.objects[stored['object_key']][1], 'video/mp4')

            source.unlink()
            restored = storage.materialize(
                asset_id, stored['object_key'], len(b'example-media'), stored['sha256']
            )
            self.assertEqual(restored.read_bytes(), b'example-media')

            storage.delete(asset_id, stored['object_key'])
            self.assertFalse(restored.exists())
            self.assertNotIn(stored['object_key'], remote.objects)

    def test_corrupt_remote_file_is_not_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            remote = FakeRemote()
            remote.objects['media/bad.bin'] = (b'wrong', 'application/octet-stream', {})
            storage = MediaStorage(folder, remote)
            with self.assertRaises(StorageError):
                storage.materialize('c' * 64, 'media/bad.bin', 99, '')
            self.assertFalse((Path(folder) / ('c' * 64)).exists())
            self.assertFalse((Path(folder) / ('c' * 64 + '.download')).exists())

    def test_partial_r2_configuration_is_rejected(self):
        names = ('R2_ENDPOINT', 'R2_BUCKET', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY')
        clean = {name: '' for name in names}
        clean['R2_BUCKET'] = 'bucket-only'
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, clean, clear=False):
            with self.assertRaises(StorageError):
                storage_from_env(folder)


if __name__ == '__main__':
    unittest.main()
