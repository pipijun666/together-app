from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit, urlunsplit

TRACKING_PREFIXES = ("utm_",)
RETRYABLE_LOCAL_STATES = {"discovered", "deferred"}
PENDING_STATES = {"submitting", "submit_unknown", "submitted", "queued", "fetching"}


def _query_without_tracking(raw: str) -> str:
    if not raw:
        return ""
    kept = []
    for part in raw.split("&"):
        raw_key = part.split("=", 1)[0]
        if unquote_plus(raw_key).lower().startswith(TRACKING_PREFIXES):
            continue
        kept.append(part)
    return "&".join(kept)


def normalize_url(url: str) -> str:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("candidate URL must be http/https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("candidate URL must not contain userinfo")
    host = parsed.hostname.lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("candidate URL has invalid port") from exc
    host_text = f"[{host}]" if ":" in host else host
    netloc = host_text
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host_text}:{port}"
    return urlunsplit((scheme, netloc, parsed.path or "/",
                       _query_without_tracking(parsed.query), ""))


def dedup_key(url: str) -> str:
    normalized = normalize_url(url)
    return hashlib.sha256(("hotspot:v1\n" + normalized).encode("utf-8")).hexdigest()


class ProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd = None

    def _remove_stale(self):
        try:
            pid = int(self.path.read_text(encoding="ascii").strip())
            os.kill(pid, 0)
        except (FileNotFoundError, ProcessLookupError, ValueError):
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return True
        except (PermissionError, OSError):
            return False
        return False
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError as exc:
                if attempt == 0 and self._remove_stale():
                    continue
                raise RuntimeError("another hotspot collector instance is running") from exc
        os.write(self.fd, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class StateStore:
    def __init__(self, state_dir):
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.path = self.root / "state.sqlite3"
        self.lock_path = self.root / "collector.lock"
        self._init()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _init(self):
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS items(
              dedup_key TEXT PRIMARY KEY,
              normalized_url TEXT NOT NULL,
              source_id TEXT NOT NULL,
              source_item_id TEXT,
              source_name TEXT NOT NULL DEFAULT '',
              title_hint TEXT NOT NULL,
              candidate_json TEXT NOT NULL DEFAULT '{}',
              status TEXT NOT NULL,
              share_id TEXT,
              poll_url TEXT,
              note_marker TEXT,
              first_seen REAL NOT NULL,
              updated REAL NOT NULL,
              last_error TEXT
            );            CREATE UNIQUE INDEX IF NOT EXISTS source_item_unique
              ON items(source_id, source_item_id) WHERE source_item_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS quota(
              day TEXT PRIMARY KEY,
              reserved INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS source_cache(
              source_id TEXT PRIMARY KEY,
              etag TEXT,
              last_modified TEXT,
              updated REAL NOT NULL
            );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(items)")}
            for name, ddl in (
                ("source_name", "TEXT NOT NULL DEFAULT ''"),
                ("candidate_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("poll_url", "TEXT"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE items ADD COLUMN {name} {ddl}")

    def lock(self):
        return ProcessLock(self.lock_path)

    def existing(self, key):
        with self.db() as db:
            row = db.execute("SELECT * FROM items WHERE dedup_key=?", (key,)).fetchone()
            return dict(row) if row else None

    def existing_source_item(self, source_id, source_item_id):
        if not source_item_id:
            return None
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM items WHERE source_id=? AND source_item_id=?",
                (source_id, source_item_id),
            ).fetchone()
            return dict(row) if row else None

    def discover(self, candidate, key, normalized):
        now = time.time()
        payload = json.dumps(candidate.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self.db() as db:
            db.execute("""INSERT OR IGNORE INTO items
                (dedup_key,normalized_url,source_id,source_item_id,source_name,title_hint,
                 candidate_json,status,first_seen,updated)
                VALUES(?,?,?,?,?,?,?,'discovered',?,?)""",
                (key, normalized, candidate.source_id, candidate.source_item_id,
                 candidate.source_name, candidate.title_hint, payload, now, now))
    def reserve_submission(self, key, day, daily_limit, marker):
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM items WHERE dedup_key=?", (key,)).fetchone()
            if not row or row["status"] not in RETRYABLE_LOCAL_STATES:
                return False
            quota = db.execute("SELECT reserved FROM quota WHERE day=?", (day,)).fetchone()
            used = quota["reserved"] if quota else 0
            if used >= daily_limit:
                db.execute("UPDATE items SET status='deferred',updated=?,last_error='daily_limit' WHERE dedup_key=?",
                           (now, key))
                return False
            db.execute("INSERT INTO quota(day,reserved) VALUES(?,1) ON CONFLICT(day) DO UPDATE SET reserved=reserved+1", (day,))
            db.execute("UPDATE items SET status='submitting',note_marker=?,updated=?,last_error=NULL WHERE dedup_key=?",
                       (marker, now, key))
            return True

    def set_status(self, key, status, *, share_id=None, poll_url=None, error=None):
        with self.db() as db:
            db.execute("""UPDATE items SET status=?, share_id=COALESCE(?,share_id),
                poll_url=COALESCE(?,poll_url), last_error=?, updated=? WHERE dedup_key=?""",
                (status, share_id, poll_url, error, time.time(), key))

    def inflight(self):
        placeholders = ",".join("?" for _ in PENDING_STATES)
        with self.db() as db:
            return [dict(row) for row in db.execute(
                f"SELECT * FROM items WHERE status IN ({placeholders}) ORDER BY updated",
                tuple(sorted(PENDING_STATES)))]

    def recover_crashed_submitting(self):
        with self.db() as db:
            db.execute("UPDATE items SET status='submit_unknown',updated=? WHERE status='submitting'", (time.time(),))

    def first_seen(self, key):
        with self.db() as db:
            row = db.execute("SELECT first_seen FROM items WHERE dedup_key=?", (key,)).fetchone()
            return row["first_seen"] if row else None

    def retryable_items(self):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM items WHERE status IN ('discovered','deferred') ORDER BY first_seen")]

    def find_by_marker(self, marker):
        with self.db() as db:
            row = db.execute("SELECT * FROM items WHERE note_marker=?", (marker,)).fetchone()
            return dict(row) if row else None

    def source_cache(self, source_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM source_cache WHERE source_id=?", (source_id,)).fetchone()
            return dict(row) if row else {}

    def update_source_cache(self, source_id, etag=None, last_modified=None):
        with self.db() as db:
            db.execute("""INSERT INTO source_cache(source_id,etag,last_modified,updated)
                VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                etag=excluded.etag,last_modified=excluded.last_modified,updated=excluded.updated""",
                (source_id, etag, last_modified, time.time()))
