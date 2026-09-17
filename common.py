"""Shared helpers. No external services or credentials are bundled."""
import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def read_env(path):
    path = Path(path)
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())

def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return salt + ':' + digest

def check_password(password, stored):
    return hmac.compare_digest(password_hash(password, stored.split(':')[0]), stored)

def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()

def new_id():
    return secrets.token_hex(16)
