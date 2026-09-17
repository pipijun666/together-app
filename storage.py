"""Media storage with a local cache and an optional private R2 origin."""
import hashlib
import os
from pathlib import Path


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


class MediaStorage:
    """Local cache backed by R2 when R2_* settings are supplied."""

    def __init__(self, cache_dir, remote=None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.remote = remote

    @property
    def remote_enabled(self):
        return self.remote is not None

    def object_key(self, share_id, asset_id, mime):
        extension = {
            'image/webp': 'webp',
            'image/jpeg': 'jpg',
            'image/png': 'png',
            'video/mp4': 'mp4',
        }.get(mime, 'bin')
        return f'media/{share_id}/{asset_id}.{extension}'

    def publish(self, share_id, asset_id, mime):
        path = self.cache_dir / asset_id
        if not path.is_file():
            raise StorageError('本地媒体文件不存在')
        checksum = file_sha256(path)
        key = self.object_key(share_id, asset_id, mime) if self.remote else ''
        if self.remote:
            try:
                self.remote.upload(path, key, mime, {
                    'share-id': share_id,
                    'asset-id': asset_id,
                    'sha256': checksum,
                })
            except Exception as exc:
                raise StorageError('媒体保存到 R2 失败') from exc
        return {'object_key': key, 'sha256': checksum}

    def materialize(self, asset_id, object_key='', expected_size=None, expected_sha256=''):
        path = self.cache_dir / asset_id
        if path.is_file() and (expected_size is None or path.stat().st_size == expected_size):
            return path
        if not self.remote or not object_key:
            raise StorageError('媒体文件不存在')
        temporary = self.cache_dir / (asset_id + '.download')
        temporary.unlink(missing_ok=True)
        try:
            self.remote.download(object_key, temporary)
            if expected_size is not None and temporary.stat().st_size != expected_size:
                raise StorageError('R2 媒体大小校验失败')
            if expected_sha256 and file_sha256(temporary) != expected_sha256:
                raise StorageError('R2 媒体校验失败')
            temporary.replace(path)
            return path
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            if isinstance(exc, StorageError):
                raise
            raise StorageError('媒体暂时无法从 R2 读取') from exc

    def delete(self, asset_id, object_key=''):
        (self.cache_dir / asset_id).unlink(missing_ok=True)
        (self.cache_dir / (asset_id + '.download')).unlink(missing_ok=True)
        if self.remote and object_key:
            try:
                self.remote.delete(object_key)
            except Exception as exc:
                raise StorageError('R2 媒体删除失败') from exc


class R2Remote:
    def __init__(self, endpoint, bucket, access_key, secret_key):
        try:
            import boto3
        except ImportError as exc:
            raise StorageError('启用 R2 需要安装 boto3') from exc
        self.bucket = bucket
        self.client = boto3.client(
            's3', endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name='auto',
        )

    def upload(self, path, key, mime, metadata):
        self.client.upload_file(
            str(path), self.bucket, key,
            ExtraArgs={'ContentType': mime, 'Metadata': metadata},
        )

    def download(self, key, destination):
        self.client.download_file(self.bucket, key, str(destination))

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)


class DualR2StorageAdapter:
    """Adapts DualR2StoragePool to the MediaStorage interface expected by server.py."""

    def __init__(self, pool):
        self.pool = pool
        self.cache_dir = pool.cache_dir

    @property
    def remote_enabled(self):
        return self.pool.is_enabled

    def object_key(self, share_id, asset_id, mime):
        return self.pool.object_key(share_id, asset_id, mime)

    def publish(self, share_id, asset_id, mime):
        path = self.cache_dir / asset_id
        if not path.is_file():
            raise StorageError('本地媒体文件不存在')
        checksum = file_sha256(path)
        if not self.pool.is_enabled:
            return {'object_key': '', 'sha256': checksum}
        res = self.pool.publish_asset(share_id, asset_id, mime)
        return {
            'object_key': res.get('r2_key', ''),
            'sha256': res.get('sha256', checksum),
            'storage_bucket': res.get('storage_bucket', 'a')
        }

    def materialize(self, asset_id, object_key='', expected_size=None, expected_sha256=''):
        path = self.cache_dir / asset_id
        if path.is_file() and (expected_size is None or path.stat().st_size == expected_size):
            return path
        if not self.pool.is_enabled or not object_key:
            if path.is_file():
                return path
            raise StorageError('媒体文件不存在')
        return self.pool.materialize(asset_id, bucket_id='a', r2_key=object_key, expected_size=expected_size)

    def delete(self, asset_id, object_key=''):
        self.pool.delete_asset(asset_id, bucket_id='a', r2_key=object_key)


def storage_from_env(cache_dir):
    if os.environ.get('R2_POOL_A_ACCOUNT_ID') or os.environ.get('R2_POOL_B_ACCOUNT_ID') or os.environ.get('R2_POOL_A_BUCKET'):
        try:
            from storage_r2 import pool_from_env
            pool = pool_from_env(cache_dir)
            if pool.is_enabled:
                return DualR2StorageAdapter(pool)
        except Exception as exc:
            raise StorageError(f'初始化 R2 存储池失败: {exc}') from exc

    values = {name: os.environ.get(name, '').strip() for name in (
        'R2_ENDPOINT', 'R2_BUCKET', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY'
    )}
    configured = [bool(value) for value in values.values()]
    if any(configured) and not all(configured):
        missing = ', '.join(name for name, value in values.items() if not value)
        raise StorageError('R2 配置不完整，缺少：' + missing)
    remote = R2Remote(
        values['R2_ENDPOINT'], values['R2_BUCKET'],
        values['R2_ACCESS_KEY_ID'], values['R2_SECRET_ACCESS_KEY'],
    ) if all(configured) else None
    return MediaStorage(cache_dir, remote)

