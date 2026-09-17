"""Verify a received database snapshot and its sidecar checksum."""
import argparse
import json
from pathlib import Path
from backup_sqlite import sha256,verify


def verify_snapshot(path):
    path=Path(path).resolve()
    metadata_path=path.with_suffix(path.suffix+'.json')
    metadata=json.loads(metadata_path.read_text(encoding='utf-8'))
    if path.stat().st_size!=int(metadata['size']):
        raise RuntimeError('backup size does not match metadata')
    if sha256(path)!=metadata['sha256']:
        raise RuntimeError('backup sha256 does not match metadata')
    verify(path)
    return metadata


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('snapshot')
    args=parser.parse_args()
    metadata=verify_snapshot(args.snapshot)
    print('verified',metadata['created_at'],metadata['sha256'])


if __name__=='__main__':
    main()
