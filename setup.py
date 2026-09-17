"""Create local secrets once. Changing passwords later requires database maintenance."""
import argparse
import os
import secrets
from pathlib import Path
from common import ROOT

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',default=str(ROOT/'.env'))
    args=parser.parse_args()
    p=Path(args.out)
    if p.exists():
        raise SystemExit('Configuration already exists; it was not changed.')
    one,two,worker=secrets.token_urlsafe(12),secrets.token_urlsafe(12),secrets.token_urlsafe(32)
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(f'# Keep this file private. Never commit it.\nHOST=127.0.0.1\nPORT=8765\nCOOKIE_SECURE=0\nSERVER_URL=http://127.0.0.1:8765\nWORKER_TOKEN={worker}\nUSER_ONE_NAME=小橙\nUSER_TWO_NAME=小桃\nUSER_ONE_PASSWORD={one}\nUSER_TWO_PASSWORD={two}\n',encoding='utf-8')
    try:
        os.chmod(p,0o600)
    except OSError:
        pass
    print('Created configuration:',p)
    print('Your two login passwords are stored in USER_ONE_PASSWORD and USER_TWO_PASSWORD.')

if __name__=='__main__':
    main()
