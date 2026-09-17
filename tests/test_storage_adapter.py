import unittest
from unittest.mock import patch
import os
import tempfile
import shutil
from pathlib import Path
from storage import storage_from_env, DualR2StorageAdapter

class TestStorageAdapter(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_storage_from_env_returns_dual_r2_adapter(self):
        env = {
            'R2_POOL_A_ACCOUNT_ID': 'test_a_id',
            'R2_POOL_A_BUCKET': 'test_a_bucket',
            'R2_POOL_A_TOKEN': 'test_a_token',
        }
        with patch.dict(os.environ, env):
            storage = storage_from_env(self.temp_dir)
            self.assertIsInstance(storage, DualR2StorageAdapter)
            self.assertTrue(storage.remote_enabled)
            key = storage.object_key('share123', 'asset456', 'image/webp')
            self.assertEqual(key, 'media/share123/asset456.webp')

if __name__ == '__main__':
    unittest.main()
