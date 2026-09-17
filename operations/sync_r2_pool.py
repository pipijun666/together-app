"""Sync script to backup SQLite database and media assets to the Dual R2 Storage Pool."""

import os
import sys
import sqlite3
import time
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage_r2 import (
    CloudflareR2Client,
    DualR2StoragePool,
    pool_from_env,
    DEFAULT_BUCKET_SAFE_BYTES,
)


def sync_r2():
    env_file = Path('/srv/together/app/.env')
    if not env_file.is_file():
        env_file = Path(__file__).resolve().parent.parent / '.env'
    if env_file.is_file():
        for line in env_file.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

    print("=== 1. Initializing Dual R2 Storage Pool ===")
    cache_dir = Path(os.environ.get('MEDIA_DIR', '/srv/together/data/media'))
    db_path = Path(os.environ.get('DB_PATH', '/srv/together/data/app.sqlite3'))

    pool = pool_from_env(cache_dir)
    if not pool.is_enabled:
        print("Error: R2 Pool is not enabled. Please check environment variables.")
        return 1

    print(f"Client A: {pool.client_a.bucket_name} (Account: {pool.client_a.account_id})")
    print(f"Client B: {pool.client_b.bucket_name} (Account: {pool.client_b.account_id})" if pool.client_b else "Client B: (Not configured)")

    # 1. Back up database to R2
    print("\n=== 2. Backing up SQLite Database to R2 Bucket A ===")
    if db_path.is_file():
        backup_key = f"backups/db/app-{time.strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
        print(f"Uploading {db_path} -> {backup_key} ({db_path.stat().st_size / 1024:.1f} KB)...")
        res = pool.client_a.upload_file(db_path, backup_key, mime_type='application/x-sqlite3')
        print(f"Database backup complete! Size: {res.get('size')} bytes")
    else:
        print(f"Database file not found at {db_path}")

    # 2. Sync media assets to R2 Pool
    print("\n=== 3. Syncing Media Assets to R2 Pool ===")
    if not db_path.is_file():
        print("Database not found, skipping asset sync.")
        return 0

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    cur = conn.cursor()

    # Check assets
    cur.execute("SELECT id, share_id, mime, size FROM assets ORDER BY size ASC")
    rows = cur.fetchall()
    print(f"Found {len(rows)} media records in database.")

    synced_count = 0
    skipped_count = 0
    failed_count = 0
    total_bytes = 0

    # Query current R2 objects in Bucket A to skip already uploaded files
    print("Listing existing objects in Bucket A...")
    try:
        existing_a = pool.client_a.list_objects()
        existing_keys_a = {obj['key'] for obj in existing_a.get('objects', [])}
        print(f"Bucket A currently contains {len(existing_keys_a)} objects.")
    except Exception as e:
        print(f"Warning: Could not list Bucket A ({e}), will upload all.")
        existing_keys_a = set()

    import concurrent.futures
    import threading

    db_lock = threading.Lock()

    def process_asset(row):
        nonlocal synced_count, skipped_count, failed_count, total_bytes
        aid, sid, mime, size = row
        key = pool.object_key(sid, aid, mime)
        local_file = cache_dir / aid

        if not local_file.is_file():
            with db_lock:
                skipped_count += 1
            return

        if key in existing_keys_a:
            with db_lock:
                cur.execute("UPDATE assets SET object_key = ? WHERE id = ? AND (object_key IS NULL OR object_key = '')", (key, aid))
                conn.commit()
                skipped_count += 1
                total_bytes += size
            return

        try:
            pool.client_a.upload_file(local_file, key, mime)
            with db_lock:
                cur.execute("UPDATE assets SET object_key = ? WHERE id = ?", (key, aid))
                conn.commit()
                synced_count += 1
                total_bytes += size
                if synced_count % 25 == 0 or synced_count == len(rows):
                    print(f"Synced {synced_count} assets ({total_bytes / (1024*1024):.2f} MB)...", flush=True)
        except Exception as e:
            print(f"Failed to upload asset {aid} ({key}): {e}", flush=True)
            with db_lock:
                failed_count += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(process_asset, rows))

    print(f"\nSync complete! Synced: {synced_count}, Skipped (already present): {skipped_count}, Failed: {failed_count}", flush=True)
    print(f"Total synced media size: {total_bytes / (1024*1024):.2f} MB", flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(sync_r2())
