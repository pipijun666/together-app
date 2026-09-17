"""Validate deployment environment files without printing secret values.

DB-001 更新：国内单机拓扑不再使用 WireGuard。primary 不需要 WG_BIND_IP；
worker 的回环 HTTP（http://127.0.0.1:8765）与 worker.py 行为一致，无需
ALLOW_INSECURE_HTTP。primary 额外评估系统资源：无 Swap 给警告，
根盘可用空间低于 2 GiB 给出警告（运行时仍会拒绝新媒体）。
"""
import argparse
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit


def load_values(path):
    values={}
    for raw in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line=raw.strip()
        if line and not line.startswith('#') and '=' in line:
            key,value=line.split('=',1)
            values[key.strip()]=value.strip()
    return values


def placeholder(value):
    lowered=value.lower()
    return not value or 'replace' in lowered or 'not-used' in lowered


def validate_token(values,errors):
    token=values.get('WORKER_TOKEN','')
    if placeholder(token) or len(token)<32:
        errors.append('WORKER_TOKEN must be a real random value with at least 32 characters')


def validate_worker(values,errors):
    validate_token(values,errors)
    raw=values.get('SERVER_URL','')
    try:
        parsed=urlsplit(raw)
        host=parsed.hostname or ''
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
            raise ValueError()
        valid_server=(parsed.scheme=='https' and bool(host)) or (
            parsed.scheme=='http' and host in ('127.0.0.1','localhost'))
        if not valid_server:
            raise ValueError()
    except ValueError:
        errors.append('SERVER_URL must be HTTPS or loopback HTTP')
    if values.get('HTTP_PROXY') or values.get('HTTPS_PROXY'):
        errors.append('do not set HTTP_PROXY/HTTPS_PROXY; use PROXY_URL so Douyin stays direct')
    proxy=values.get('PROXY_URL') or values.get('YTDLP_PROXY') or ''
    try:
        parsed=urlsplit(proxy)
        if (parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost') or
                parsed.port!=8118 or parsed.username or parsed.password or
                parsed.path not in ('','/') or parsed.query or parsed.fragment):
            raise ValueError()
    except ValueError:
        errors.append('PROXY_URL or YTDLP_PROXY must be http://127.0.0.1:8118')
    for entry in values.get('PROXY_WHITELIST','').split(','):
        domain=entry.strip().lower().lstrip('*.')
        if domain and not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',domain):
            errors.append('PROXY_WHITELIST contains an invalid domain')
            break


def validate_primary(values,errors):
    validate_token(values,errors)
    if values.get('NODE_ROLE','primary')!='primary':
        errors.append('domestic primary must use NODE_ROLE=primary')
    if values.get('COOKIE_SECURE')!='1':
        errors.append('domestic primary must use COOKIE_SECURE=1')
    for key in ('USER_ONE_PASSWORD','USER_TWO_PASSWORD'):
        value=values.get(key,'')
        if placeholder(value) or len(value)<12:
            errors.append('%s must be a real value with at least 12 characters' % key)
    if values.get('MAX_MEDIA_BYTES')!='8589934592':
        errors.append('MAX_MEDIA_BYTES must be 8589934592 for the current 8 GiB limit')


def validate_standby(values,errors):
    validate_token(values,errors)
    if values.get('NODE_ROLE')!='standby':
        errors.append('standby must start with NODE_ROLE=standby')


def system_warnings(role, values):
    """部署前资源评估：Swap 缺失给警告；仅 primary 关注根盘可用空间。"""
    warnings=[]
    if role!='primary':
        return warnings
    try:
        swaps=Path('/proc/swaps').read_text(encoding='utf-8').splitlines()
        if len(swaps)<2:  # 只有表头或为空
            warnings.append('服务器当前无 Swap；建议评估增加 1–2 GiB Swap 后再上线')
    except OSError:
        pass  # 非 Linux 环境跳过
    try:
        target=Path(values.get('DATA_DIR','/srv/together/data'))
        probe=target if target.exists() else Path('/')
        free_bytes=shutil.disk_usage(str(probe)).free
        if free_bytes < 2*1024*1024*1024:
            warnings.append(f'根盘可用空间不足 2 GiB（当前约 {free_bytes//(1024*1024)} MiB），将拒绝新媒体导入，请先清理或扩容')
    except OSError:
        pass
    return warnings


def validate(role,values,check_tools=False):
    errors=[]
    {'worker':validate_worker,'primary':validate_primary,'standby':validate_standby}[role](values,errors)
    if check_tools and not shutil.which('docker'):
        errors.append('docker is not available on PATH')
    return errors


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--role',choices=('primary','worker','standby'),required=True)
    parser.add_argument('--env',required=True)
    parser.add_argument('--check-tools',action='store_true')
    args=parser.parse_args()
    values=load_values(args.env)
    errors=validate(args.role,values,args.check_tools)
    for item in errors:
        print('ERROR:',item)
    for item in system_warnings(args.role, values):
        print('WARN:',item)
    if errors:
        raise SystemExit(1)
    print('OK:',args.role,'deployment environment passed preflight')


if __name__=='__main__':
    main()
