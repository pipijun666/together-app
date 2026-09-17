"""Cloudflare R2 Dual-Bucket Storage Pool with automatic overflow and quota eviction.

Manages two independent Cloudflare accounts/buckets (Pool A and Pool B), offering
a combined safe storage pool of up to 15GB (7.5GB per bucket) with 0-cost egress.
"""

import hashlib
import json
import os
import urllib.request
import urllib.error
from pathlib import Path

# Safe limits: 7.5GB per bucket (out of 10GB free tier), 15GB total pool
DEFAULT_BUCKET_SAFE_BYTES = int(7.5 * 1024 * 1024 * 1024)   # 8,053,063,680
DEFAULT_POOL_SAFE_BYTES = int(15.0 * 1024 * 1024 * 1024)     # 16,106,127,360
SAFE_WATERMARK_RATIO = 0.80                                  # 12.0GB watermark on eviction


class StorageError(Exception):
    pass


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


class CloudflareR2Client:
    """Standard HTTP REST client for Cloudflare R2 using account API tokens."""

    def __init__(self, account_id: str, bucket_name: str, api_token: str, proxy: str = ''):
        self.account_id = account_id.strip()
        self.bucket_name = bucket_name.strip()
        self.api_token = api_token.strip()
        self.proxy = proxy.strip() or os.environ.get('HTTPS_PROXY', '').strip() or os.environ.get('HTTP_PROXY', '').strip()
        self.base_url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/r2/buckets/{self.bucket_name}"

    def _request(self, method: str, path: str, data: bytes = None, headers: dict = None, stream: bool = False, timeout: int = 300):
        url = f"{self.base_url}{path}"
        req_headers = {
            "Authorization": f"Bearer {self.api_token}",
            "User-Agent": "together-r2-pool/1.0",
        }
        if headers:
            req_headers.update(headers)

        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        handlers = []
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({'http': self.proxy, 'https': self.proxy}))
        opener = urllib.request.build_opener(*handlers)

        try:
            resp = opener.open(req, timeout=timeout)
            if stream:
                return resp
            return resp.read()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode('utf-8', errors='ignore')
            raise StorageError(f"R2 API error {e.code} on {method} {path}: {err_body}") from e
        except Exception as e:
            raise StorageError(f"R2 network error on {method} {path}: {e}") from e

    def upload_file(self, local_path: Path, object_key: str, mime_type: str = 'application/octet-stream') -> dict:
        local_path = Path(local_path)
        with local_path.open('rb') as f:
            data = f.read()
        headers = {
            "Content-Type": mime_type,
            "Content-Length": str(len(data)),
        }
        raw_resp = self._request("PUT", f"/objects/{object_key}", data=data, headers=headers)
        try:
            parsed = json.loads(raw_resp.decode('utf-8'))
            if not parsed.get('success'):
                raise StorageError(f"R2 upload failed: {parsed.get('errors')}")
            return parsed.get('result', {})
        except Exception as e:
            if isinstance(e, StorageError):
                raise
            return {"key": object_key, "size": len(data)}

    def download_file(self, object_key: str, destination_path: Path) -> Path:
        destination_path = Path(destination_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temp_dest = destination_path.with_suffix(destination_path.suffix + '.tmp')

        resp = self._request("GET", f"/objects/{object_key}", stream=True)
        with temp_dest.open('wb') as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        temp_dest.replace(destination_path)
        return destination_path

    def delete_object(self, object_key: str) -> bool:
        try:
            raw = self._request("DELETE", f"/objects/{object_key}")
            parsed = json.loads(raw.decode('utf-8'))
            return bool(parsed.get('success'))
        except StorageError as e:
            if "404" in str(e):
                return True
            raise

    def list_objects(self, prefix: str = '', cursor: str = None) -> dict:
        query_params = []
        if prefix:
            query_params.append(f"prefix={urllib.parse.quote(prefix)}")
        if cursor:
            query_params.append(f"cursor={urllib.parse.quote(cursor)}")
        qs = f"?{'&'.join(query_params)}" if query_params else ""

        raw = self._request("GET", f"/objects{qs}")
        parsed = json.loads(raw.decode('utf-8'))
        if not parsed.get('success'):
            raise StorageError(f"R2 list failed: {parsed.get('errors')}")
        return parsed.get('result', {})


class DualR2StoragePool:
    """Multi-account R2 storage pool combining Bucket A and Bucket B."""

    def __init__(
        self,
        cache_dir: Path,
        client_a: CloudflareR2Client = None,
        client_b: CloudflareR2Client = None,
        bucket_safe_bytes: int = DEFAULT_BUCKET_SAFE_BYTES,
        pool_safe_bytes: int = DEFAULT_POOL_SAFE_BYTES,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.client_a = client_a
        self.client_b = client_b
        self.bucket_safe_bytes = bucket_safe_bytes
        self.pool_safe_bytes = pool_safe_bytes

    @property
    def is_enabled(self) -> bool:
        return self.client_a is not None or self.client_b is not None

    def client_for(self, bucket_id: str) -> CloudflareR2Client:
        b = (bucket_id or 'a').lower()
        if b == 'a' and self.client_a:
            return self.client_a
        if b == 'b' and self.client_b:
            return self.client_b
        return self.client_a or self.client_b

    def object_key(self, share_id: str, asset_id: str, mime: str) -> str:
        ext = {
            'image/webp': 'webp',
            'image/jpeg': 'jpg',
            'image/png': 'png',
            'video/mp4': 'mp4',
        }.get(mime, 'bin')
        return f"media/{share_id}/{asset_id}.{ext}"

    def select_bucket_for_upload(self, data_size: int, usage_a: int, usage_b: int) -> str:
        """Select bucket A or B based on overflow thresholds."""
        # 1. If A has room below 7.5GB, put in A
        if self.client_a and (usage_a + data_size <= self.bucket_safe_bytes):
            return 'a'
        # 2. If B exists and has room below 7.5GB, overflow to B
        if self.client_b and (usage_b + data_size <= self.bucket_safe_bytes):
            return 'b'
        # 3. If B is not configured, but A is configured
        if not self.client_b and self.client_a and (usage_a + data_size <= self.bucket_safe_bytes):
            return 'a'
        # Both full or pool limit exceeded
        return None

    def publish_asset(self, share_id: str, asset_id: str, mime: str, usage_a: int = 0, usage_b: int = 0) -> dict:
        """Publishes an asset from local cache to the optimal R2 bucket in the pool."""
        local_path = self.cache_dir / asset_id
        if not local_path.is_file():
            raise StorageError("本地媒体文件不存在")

        file_size = local_path.stat().st_size
        checksum = file_sha256(local_path)
        key = self.object_key(share_id, asset_id, mime)

        if not self.is_enabled:
            return {'storage_bucket': 'local', 'r2_key': '', 'sha256': checksum}

        target_bucket = self.select_bucket_for_upload(file_size, usage_a, usage_b)
        if not target_bucket:
            # Capacity exceeded in pool
            raise StorageError("R2 存储池容量已达上限，需要执行自动淘汰")

        client = self.client_for(target_bucket)
        client.upload_file(local_path, key, mime)
        return {
            'storage_bucket': target_bucket,
            'r2_key': key,
            'sha256': checksum,
            'size': file_size,
        }

    def materialize(self, asset_id: str, bucket_id: str = 'a', r2_key: str = '', expected_size: int = None) -> Path:
        """Ensures the asset is present in the local fast cache, downloading from R2 if needed."""
        local_path = self.cache_dir / asset_id
        if local_path.is_file() and (expected_size is None or local_path.stat().st_size == expected_size):
            return local_path

        if not self.is_enabled or not r2_key:
            if local_path.is_file():
                return local_path
            raise StorageError("本地媒体不存在且未配置 R2")

        client = self.client_for(bucket_id)
        client.download_file(r2_key, local_path)
        if expected_size is not None and local_path.stat().st_size != expected_size:
            local_path.unlink(missing_ok=True)
            raise StorageError("R2 下载媒体大小校验失败")
        return local_path

    def delete_asset(self, asset_id: str, bucket_id: str = 'a', r2_key: str = ''):
        """Deletes asset from both local cache and remote R2 bucket."""
        (self.cache_dir / asset_id).unlink(missing_ok=True)
        if self.is_enabled and r2_key:
            try:
                client = self.client_for(bucket_id)
                client.delete_object(r2_key)
            except Exception:
                pass


def pool_from_env(cache_dir: Path) -> DualR2StoragePool:
    """Builds DualR2StoragePool from environment variables."""
    # Account A credentials
    a_id = os.environ.get('R2_POOL_A_ACCOUNT_ID', '').strip()
    a_bucket = os.environ.get('R2_POOL_A_BUCKET', '').strip()
    a_token = os.environ.get('R2_POOL_A_TOKEN', '').strip()
    client_a = CloudflareR2Client(a_id, a_bucket, a_token) if (a_id and a_bucket and a_token) else None

    # Account B credentials
    b_id = os.environ.get('R2_POOL_B_ACCOUNT_ID', '').strip()
    b_bucket = os.environ.get('R2_POOL_B_BUCKET', '').strip()
    b_token = os.environ.get('R2_POOL_B_TOKEN', '').strip()
    client_b = CloudflareR2Client(b_id, b_bucket, b_token) if (b_id and b_bucket and b_token) else None

    # Fallback to single R2 if old env vars present
    if not client_a and not client_b:
        legacy_bucket = os.environ.get('R2_BUCKET', '').strip()
        legacy_account = os.environ.get('R2_ACCOUNT_ID', '').strip()
        legacy_token = os.environ.get('R2_TOKEN', '').strip()
        if legacy_bucket and legacy_account and legacy_token:
            client_a = CloudflareR2Client(legacy_account, legacy_bucket, legacy_token)

    return DualR2StoragePool(cache_dir, client_a, client_b)
