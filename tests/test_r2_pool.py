import unittest
import tempfile
from pathlib import Path
from storage_r2 import (
    DualR2StoragePool,
    DEFAULT_BUCKET_SAFE_BYTES,
    DEFAULT_POOL_SAFE_BYTES,
)

class MockR2Client:
    def __init__(self, name):
        self.name = name
        self.uploaded = {}
        self.deleted = []

    def upload_file(self, local_path, object_key, mime_type):
        self.uploaded[object_key] = Path(local_path).stat().st_size
        return {"key": object_key, "size": self.uploaded[object_key]}

    def delete_object(self, object_key):
        self.deleted.append(object_key)
        self.uploaded.pop(object_key, None)
        return True


class TestDualR2StoragePool(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.temp_dir.name)
        self.mock_a = MockR2Client('a')
        self.mock_b = MockR2Client('b')
        self.pool = DualR2StoragePool(
            self.cache_dir,
            client_a=self.mock_a,
            client_b=self.mock_b,
            bucket_safe_bytes=1000,  # 1000 bytes for testing
            pool_safe_bytes=2000,    # 2000 bytes for testing
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_bucket_selection_and_overflow(self):
        # 1. Bucket A has space -> allocates to 'a'
        target = self.pool.select_bucket_for_upload(data_size=200, usage_a=300, usage_b=0)
        self.assertEqual(target, 'a')

        # 2. Bucket A would exceed 1000 bytes (usage 900 + 200 = 1100) -> overflows to 'b'
        target = self.pool.select_bucket_for_upload(data_size=200, usage_a=900, usage_b=100)
        self.assertEqual(target, 'b')

        # 3. Both Bucket A and B full -> returns None
        target = self.pool.select_bucket_for_upload(data_size=200, usage_a=900, usage_b=900)
        self.assertIsNone(target)

    def test_publish_and_materialize(self):
        # Create a test file
        aid = "test_asset_1"
        test_file = self.cache_dir / aid
        test_file.write_bytes(b"hello media bytes 12345")

        # Publish to pool
        res = self.pool.publish_asset("share_1", aid, "image/jpeg", usage_a=0, usage_b=0)
        self.assertEqual(res['storage_bucket'], 'a')
        self.assertEqual(res['r2_key'], 'media/share_1/test_asset_1.jpg')
        self.assertIn('media/share_1/test_asset_1.jpg', self.mock_a.uploaded)

        # Delete local copy to test materialize
        test_file.unlink()
        self.assertFalse(test_file.exists())

        # Mock download
        self.mock_a.download_file = lambda key, dest: Path(dest).write_bytes(b"hello media bytes 12345")

        # Materialize from R2
        mat_path = self.pool.materialize(aid, bucket_id='a', r2_key=res['r2_key'], expected_size=len(b"hello media bytes 12345"))
        self.assertTrue(mat_path.is_file())
        self.assertEqual(mat_path.read_bytes(), b"hello media bytes 12345")


if __name__ == '__main__':
    unittest.main()
