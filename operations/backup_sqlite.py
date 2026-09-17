"""Create and verify versioned SQLite snapshots while the service is online."""
import argparse
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


def sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path):
    uri=Path(path).resolve().as_uri()+'?mode=ro'
    db=sqlite3.connect(uri,uri=True)
    try:
        result=db.execute('PRAGMA integrity_check').fetchone()[0]
    finally:
        db.close()
    if result!='ok':
        raise RuntimeError('SQLite integrity_check failed: '+str(result))


def create_snapshot(source,destination):
    source=Path(source).resolve()
    destination=Path(destination).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    final=destination/f'app-{stamp}.sqlite3'
    temporary=destination/(final.name+'.partial')
    temporary.unlink(missing_ok=True)
    try:
        origin=sqlite3.connect(source)
        target=sqlite3.connect(temporary)
        try:
            origin.backup(target,pages=256,sleep=0.05)
        finally:
            target.close()
            origin.close()
        verify(temporary)
        temporary.replace(final)
        metadata={'created_at':stamp,'source':source.name,'size':final.stat().st_size,'sha256':sha256(final)}
        (destination/(final.name+'.json')).write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
        return final
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def prune(destination,keep_recent=288,keep_daily=7,now=None):
    destination=Path(destination).resolve()
    snapshots=sorted(destination.glob('app-????????T??????Z.sqlite3'),reverse=True)
    keep=set(snapshots[:max(0,keep_recent)])
    today=datetime.fromtimestamp(now or time.time(),timezone.utc).date()
    daily={}
    for snapshot in snapshots:
        try:
            created=datetime.strptime(snapshot.stem[4:],'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        age=(today-created.date()).days
        if 0<=age<max(0,keep_daily) and created.date() not in daily:
            daily[created.date()]=snapshot
            keep.add(snapshot)
    for snapshot in snapshots:
        if snapshot not in keep:
            snapshot.unlink()
            snapshot.with_suffix(snapshot.suffix+'.json').unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',required=True)
    parser.add_argument('--destination',required=True)
    parser.add_argument('--keep-recent',type=int,default=288)
    parser.add_argument('--keep-daily',type=int,default=7)
    args=parser.parse_args()
    snapshot=create_snapshot(args.source,args.destination)
    prune(args.destination,args.keep_recent,args.keep_daily)
    print(snapshot)


if __name__=='__main__':
    main()
