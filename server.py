"""Domestic API and mobile client; Python 3.11+. Run behind HTTPS for deployment."""
import argparse
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.request
import urllib.parse
from urllib.parse import urlsplit, parse_qs
from common import ROOT, read_env, dumps, password_hash, check_password, token_hash, new_id
from link_input import source_reference, platform_for_url, content_id_for_url
from storage import StorageError, storage_from_env

MAX_BODY = 8 * 1024 * 1024
MAX_VIDEO = 200 * 1024 * 1024
MEDIA_CACHE_CONTROL = 'private, max-age=604800, immutable'
STATUSES = {'queued', 'fetching', 'review', 'published', 'failed'}

class AppError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status

def text(value, limit, required=False):
    if not isinstance(value, str) or len(value) > limit:
        raise AppError('内容格式不正确或超过长度限制')
    value = value.strip()
    if required and not value:
        raise AppError('请填写必填内容')
    return value

def validate_url(url):
    url = text(url, 2000, True)
    try:
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
            raise ValueError()
    except ValueError:
        raise AppError('请输入有效的 http 或 https 链接，不支持账户信息及自定义端口')
    return url

def valid_mp4(path):
    """Validate top-level ISO BMFF box boundaries and require ftyp plus media data."""
    path=Path(path)
    try:
        total=path.stat().st_size
        if total<16:
            return False
        found_ftyp=False
        found_media=False
        offset=0
        with path.open('rb') as stream:
            while offset<total:
                stream.seek(offset)
                header=stream.read(8)
                if len(header)!=8:
                    return False
                box_size=int.from_bytes(header[:4],'big')
                box_type=header[4:8]
                header_size=8
                if box_size==1:
                    extended=stream.read(8)
                    if len(extended)!=8:
                        return False
                    box_size=int.from_bytes(extended,'big')
                    header_size=16
                elif box_size==0:
                    box_size=total-offset
                if box_size<header_size or offset+box_size>total:
                    return False
                found_ftyp=found_ftyp or box_type==b'ftyp'
                found_media=found_media or box_type in (b'moov',b'mdat')
                offset+=box_size
        return offset==total and found_ftyp and found_media
    except OSError:
        return False

class App:
    def __init__(self, directory, worker_token, secure=False, media_storage=None, max_media_bytes=None, node_role='primary', x_accel_media=False, hotspot_token=''):
        if len(worker_token) < 32:
            raise ValueError('WORKER_TOKEN must contain at least 32 characters')
        if hotspot_token and len(hotspot_token) < 32:
            raise ValueError('HOTSPOT_SERVICE_TOKEN must contain at least 32 characters')
        if hotspot_token and hmac.compare_digest(hotspot_token, worker_token):
            raise ValueError('HOTSPOT_SERVICE_TOKEN must differ from WORKER_TOKEN')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.media = self.directory / 'media'
        self.media.mkdir(exist_ok=True)
        self.storage = media_storage or storage_from_env(self.media)
        self.max_media_bytes = max_media_bytes if max_media_bytes is not None else int(os.environ.get('MAX_MEDIA_BYTES',str(10*1024*1024*1024)))
        if self.max_media_bytes <= 0:
            raise ValueError('MAX_MEDIA_BYTES must be positive')
        if node_role not in ('primary','standby'):
            raise ValueError('NODE_ROLE must be primary or standby')
        self.node_role = node_role
        self.x_accel_media = bool(x_accel_media)
        self.db_path = self.directory / 'app.sqlite3'
        self.worker_token, self.hotspot_token, self.secure = worker_token, hotspot_token, secure
        self.rate_lock, self.attempts = threading.Lock(), {}
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT NOT NULL, password TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS shares(
              id TEXT PRIMARY KEY,author TEXT NOT NULL,url TEXT NOT NULL,title TEXT NOT NULL,note TEXT NOT NULL,
              blocks TEXT NOT NULL DEFAULT '[]',warnings TEXT NOT NULL DEFAULT '[]',status TEXT NOT NULL,
              error TEXT NOT NULL DEFAULT '',created REAL NOT NULL,fetched REAL,published REAL,
              attempts INTEGER NOT NULL DEFAULT 0,lease TEXT,lease_until REAL,next_try REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,share_id TEXT NOT NULL,mime TEXT NOT NULL,size INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS comments(id TEXT PRIMARY KEY,share_id TEXT NOT NULL,user_id TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS reactions(share_id TEXT NOT NULL,user_id TEXT NOT NULL,emoji TEXT NOT NULL,PRIMARY KEY(share_id,user_id));
            CREATE TABLE IF NOT EXISTS reads(share_id TEXT NOT NULL,user_id TEXT NOT NULL,PRIMARY KEY(share_id,user_id));
            CREATE INDEX IF NOT EXISTS share_queue ON shares(status,next_try,lease_until);
            CREATE TABLE IF NOT EXISTS service_state(id TEXT PRIMARY KEY,last_seen REAL NOT NULL);
            ''')
            columns={r['name'] for r in db.execute('PRAGMA table_info(shares)')}
            for name,definition in [('metadata',"TEXT NOT NULL DEFAULT '{}'"),('phase',"TEXT NOT NULL DEFAULT ''"),('source_kind',"TEXT NOT NULL DEFAULT 'manual'")]:
                if name not in columns:
                    db.execute(f'ALTER TABLE shares ADD COLUMN {name} {definition}')
            db.execute("CREATE INDEX IF NOT EXISTS idx_shares_source_status_created ON shares(source_kind, status, created)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_shares_author_status_created ON shares(author, status, created)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_comments_share_created ON comments(share_id, created)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_assets_share_id ON assets(share_id)")
            asset_columns={r['name'] for r in db.execute('PRAGMA table_info(assets)')}
            if 'kind' not in asset_columns:
                db.execute("ALTER TABLE assets ADD COLUMN kind TEXT NOT NULL DEFAULT 'image'")
            if 'object_key' not in asset_columns:
                db.execute("ALTER TABLE assets ADD COLUMN object_key TEXT NOT NULL DEFAULT ''")
            if 'sha256' not in asset_columns:
                db.execute("ALTER TABLE assets ADD COLUMN sha256 TEXT NOT NULL DEFAULT ''")
            db.execute('''CREATE TABLE IF NOT EXISTS system_migration_audit(share_id TEXT PRIMARY KEY, previous_author TEXT NOT NULL, evidence_json TEXT NOT NULL, migrated REAL NOT NULL)''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_user(self, uid, name, password):
        with self.db() as db:
            existing = db.execute('SELECT password FROM users WHERE id=?', (uid,)).fetchone()
            if not existing:
                db.execute('INSERT INTO users VALUES(?,?,?)', (uid, name, password_hash(password)))
            elif check_password(password, existing['password']):
                db.execute('UPDATE users SET name=? WHERE id=?', (name, uid))
            else:
                db.execute('UPDATE users SET name=?,password=? WHERE id=?', (name, password_hash(password), uid))
                db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))

    def user(self, handler):
        cookie = SimpleCookie()
        try:
            cookie.load(handler.headers.get('Cookie', ''))
            raw = cookie['session'].value if 'session' in cookie else ''
        except Exception:
            raw = ''
        with self.db() as db:
            row = db.execute('SELECT u.id,u.name FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>?', (token_hash(raw), time.time())).fetchone()
        if not row:
            raise AppError('请先登录你们的空间', 401)
        return dict(row)

    def visible(self, db, sid, uid, author=False):
        row = db.execute('SELECT * FROM shares WHERE id=?', (sid,)).fetchone()
        if not row: raise AppError('找不到这条分享',404)
        if row['source_kind']=='system':
            if row['status']!='published' or author: raise AppError('找不到这条分享',404)
        else:
            private_draft=row['status']=='review'
            if row['author']!=uid and (author or row['status']=='failed' or private_draft): raise AppError('找不到这条分享',404)
        return dict(row)

    def serial(self, db, row, uid, summary=False):
        result = {k: row[k] for k in ('id','author','url','title','note','status','error','created','fetched','published','attempts')}
        result['warnings'] = [] if summary else json.loads(row['warnings'])
        result['metadata'] = json.loads(row['metadata'])
        result['phase'] = row['phase']
        result['source_kind'] = row['source_kind']
        if row['source_kind']=='system':
            result['author'],result['author_name']='system','系统推送'
            if result.get('note'):
                result['note']=re.sub(r'\[hotspot:v1:[a-f0-9]+\]\s*','',result['note']).strip()
        else:
            author_row=db.execute('SELECT name FROM users WHERE id=?',(row['author'],)).fetchone()
            result['author_name']=author_row[0] if author_row else row['author']
        if summary:
            raw_blocks = row['blocks'] or '[]'
            summary_blocks = []
            try:
                parsed_blocks = json.loads(raw_blocks)
                for b in parsed_blocks:
                    b_type = b.get('type')
                    if b_type == 'image':
                        summary_blocks.append({'type': 'image', 'src': b.get('src', ''), 'asset': b.get('asset', '')})
                        break
                    elif b_type == 'video':
                        summary_blocks.append({'type': 'video', 'src': b.get('src', ''), 'poster': b.get('poster', ''), 'poster_asset': b.get('poster_asset', '')})
                        break
            except Exception:
                pass
            result['blocks'] = summary_blocks
        else:
            result['blocks'] = json.loads(row['blocks'])
        result['comments'] = [dict(c) for c in db.execute('SELECT c.id,c.body,c.created,c.user_id,u.name FROM comments c JOIN users u ON u.id=c.user_id WHERE share_id=? ORDER BY created', (row['id'],))]
        result['reactions'] = [dict(r) for r in db.execute('SELECT user_id,emoji FROM reactions WHERE share_id=?', (row['id'],))]
        result['read'] = bool(db.execute('SELECT 1 FROM reads WHERE share_id=? AND user_id=?', (row['id'],uid)).fetchone())
        return result

    def preview(self, db, row, uid):
        share=self.serial(db,row,uid)
        blocks=share['blocks']
        paragraphs=[b['text'] for b in blocks if b.get('type') in ('paragraph','quote')]
        images=[b['src'] for b in blocks if b.get('type')=='image']
        videos=[b['src'] for b in blocks if b.get('type')=='video']
        posters=[b.get('poster','') for b in blocks if b.get('type')=='video' and b.get('poster')]
        metadata=share['metadata']
        platform=metadata.get('platform') or platform_for_url(share['url'])
        content=None
        if share['status'] in ('review','published') and blocks:
            content={
                'title':share['title'],
                'description':'\n\n'.join(paragraphs)[:50000],
                'author':metadata.get('author',''),
                'published_at':metadata.get('published_at',''),
                'content_type':metadata.get('content_type') or ('video' if videos else 'image' if images else 'article'),
                'cover':(posters or images or [''])[0],
                'images':images,
                'videos':videos,
            }
        result={
            'id':share['id'],'status':share['status'],'phase':share['phase'],
            'source':{
                'url':share['url'],
                'canonical_url':metadata.get('canonical_url') or share['url'],
                'platform':platform,
                'content_id':metadata.get('content_id') or content_id_for_url(metadata.get('canonical_url') or share['url'],platform),
            },
            'content':content,'warnings':share['warnings'],
        }
        if share['status']=='failed':
            result['error']=share['error']
        return result

    def cookie(self, value, age=604800):
        return f'session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}' + ('; Secure' if self.secure else '')

    def login(self, h, data):
        uid, password = text(data.get('user',''), 20, True), text(data.get('password',''), 256, True)
        ip, now = h.client_address[0], time.time()
        with self.rate_lock:
            self.attempts = {k: v for k,v in self.attempts.items() if v[1] > now - 600}
            count, started = self.attempts.get(ip, (0, now))
            if count >= 10:
                raise AppError('尝试次数过多，请十分钟后重试', 429)
            self.attempts[ip] = (count+1, started)
        with self.db() as db:
            user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
            if not user or not check_password(password,user['password']):
                raise AppError('身份或口令不正确', 401)
            token = secrets.token_urlsafe(32)
            db.execute('DELETE FROM sessions WHERE expires<?', (now,))
            db.execute('INSERT INTO sessions VALUES(?,?,?)',(token_hash(token),uid,now+604800))
        with self.rate_lock:
            self.attempts.pop(ip,None)
        return {'user':{'id':uid,'name':user['name']}}, {'Set-Cookie':self.cookie(token)}

    def worker(self, h, path, data):
        if self.node_role != 'primary':
            raise AppError('备用节点尚未提升为主节点',503)
        if not hmac.compare_digest(h.headers.get('Authorization',''), 'Bearer '+self.worker_token):
            raise AppError('采集服务凭证无效', 401)
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO service_state VALUES(?,?)',('worker',now))
            if path == '/internal/heartbeat':
                return {'ok':True}
            if path == '/internal/jobs/claim':
                expired=db.execute("SELECT id FROM shares WHERE status='fetching' AND lease_until<?",(now,)).fetchall()
                for expired_row in expired:
                    self.cleanup_video_parts(expired_row['id'])
                db.execute("UPDATE shares SET status='failed',error='采集多次超时，请重试或手动补充',lease=NULL WHERE status='fetching' AND lease_until<? AND attempts>=3", (now,))
                row = db.execute("SELECT * FROM shares WHERE (status='queued' AND next_try<=?) OR (status='fetching' AND lease_until<? AND attempts<3) ORDER BY created LIMIT 1", (now,now)).fetchone()
                if not row:
                    return {'job':None}
                lease = secrets.token_urlsafe(24)
                db.execute("UPDATE shares SET status='fetching',phase='reading',attempts=attempts+1,lease=?,lease_until=? WHERE id=?", (lease,now+720,row['id']))
                return {'job':{'id':row['id'],'url':row['url'],'lease':lease}}
            sid = text(data.get('id',''),32,True)
            row = db.execute('SELECT * FROM shares WHERE id=?',(sid,)).fetchone()
            supplied = text(data.get('lease',''),100,True)
            if not row or not row['lease'] or not hmac.compare_digest(row['lease'],supplied):
                raise AppError('任务已失效',409)
            if row['status'] in ('review','published') and path == '/internal/jobs/complete':
                return {'ok':True}
            if row['status'] != 'fetching' or row['lease_until'] < now:
                raise AppError('任务已失效',409)
            if path == '/internal/jobs/progress':
                phase=data.get('phase')
                if phase not in ('reading','media','images','finishing'):
                    raise AppError('未知导入阶段')
                db.execute('UPDATE shares SET phase=? WHERE id=?',(phase,sid))
                return {'ok':True}
            if path == '/internal/jobs/fail':
                err = text(data.get('error','导入失败'),300)
                status = 'queued' if row['attempts'] < 3 and data.get('retryable') is True else 'failed'
                self.cleanup_video_parts(sid)
                db.execute("UPDATE shares SET status=?,phase='',error=?,lease=NULL,lease_until=0,next_try=? WHERE id=?", (status,err,now+15*row['attempts'],sid))
                return {'ok':True}
            if path == '/internal/jobs/media':
                return self.save_image(db,sid,data.get('data',''))
            if path == '/internal/jobs/video-chunk':
                return self.save_video_chunk(db,sid,data)
            if path == '/internal/jobs/complete':
                title = text(data.get('title',''),300,True)
                blocks = self.clean_blocks(db,sid,data.get('blocks'))
                warnings = data.get('warnings',[])
                if not isinstance(warnings,list) or len(warnings)>30:
                    raise AppError('提示信息格式错误')
                warnings = [text(x,300) for x in warnings]
                metadata=data.get('metadata',{})
                if not isinstance(metadata,dict):
                    raise AppError('来源信息格式错误')
                worker_metadata={k:text(metadata.get(k,''),2000 if k=='canonical_url' else 300) for k in ('author','published_at','site_name','platform','content_id','canonical_url','content_type')}
                try: existing_metadata=json.loads(row['metadata'] or '{}')
                except (TypeError,json.JSONDecodeError): existing_metadata={}
                metadata={**existing_metadata,**worker_metadata}
                if not blocks:
                    raise AppError('未提取到可展示的内容')
                self.cleanup_video_parts(sid)
                db.execute("UPDATE shares SET title=?,blocks=?,warnings=?,metadata=?,phase='',status='published',error='',fetched=?,published=? WHERE id=?",(title,dumps(blocks).decode(),dumps(warnings).decode(),dumps(metadata).decode(),now,now,sid))
                return {'ok':True}
            raise AppError('接口不存在',404)

    def _hotspot_auth(self,h):
        if not self.hotspot_token: raise AppError('系统热点服务未配置',503)
        try: addr=ipaddress.ip_address(h.client_address[0])
        except ValueError: raise AppError('系统热点仅允许本机访问',403)
        if not addr.is_loopback: raise AppError('系统热点仅允许本机访问',403)
        if not hmac.compare_digest(h.headers.get('Authorization',''),'Bearer '+self.hotspot_token): raise AppError('系统热点凭证无效',401)

    def _hotspot_state(self,db,row):
        metadata=json.loads(row['metadata'] or '{}'); platform=metadata.get('platform') or platform_for_url(row['url'])
        result={'id':row['id'],'status':row['status'],'phase':row['phase'],'source':{'url':row['url'],'canonical_url':metadata.get('canonical_url') or row['url'],'platform':platform,'content_id':metadata.get('content_id') or content_id_for_url(metadata.get('canonical_url') or row['url'],platform)},'warnings':json.loads(row['warnings'])}
        if row['status']=='failed': result['error']=row['error']
        return result

    def _collector_metadata(self,value):
        if value is None: return {}
        if not isinstance(value,dict): raise AppError('来源元数据格式错误')
        result={}
        for key in ('source_id','source_name','source_item_id','region','coverage','metric_name','metric_observed_at'):
            if key in value and value[key] is not None: result[key]=text(str(value[key]),300)
        metric=value.get('metric_value')
        if metric is not None:
            if isinstance(metric,bool) or not isinstance(metric,(int,float)): raise AppError('来源热度格式错误')
            result['metric_value']=float(metric)
        return result

    def hotspots(self,h,method,path,data):
        self._hotspot_auth(h)
        if self.node_role!='primary' and method!='GET': raise AppError('备用节点尚未提升为主节点',503)
        with self.db() as db:
            if path=='/internal/hotspots/status' and method=='GET':
                last=db.execute("SELECT last_seen FROM service_state WHERE id='worker'").fetchone(); seen=last[0] if last else None
                return {'worker_recent_contact':bool(seen and time.time()-seen<60),'worker_last_seen':seen,'media_bytes':self.media_usage(db),'media_limit_bytes':self.max_media_bytes}
            if path=='/internal/hotspots' and method=='POST':
                if data.get('author') is not None or data.get('source_kind') is not None: raise AppError('系统身份由服务端固定')
                try: source=source_reference(data.get('text',data.get('url','')))
                except ValueError as exc: raise AppError(str(exc)) from exc
                url=validate_url(source['url']); pending=db.execute("SELECT count(*) FROM shares WHERE status IN ('queued','fetching')").fetchone()[0]
                if pending>=10: raise AppError('等待导入的内容较多，请稍后再试',429)
                sid=new_id(); note=text(data.get('note',''),1000); collector=self._collector_metadata(data.get('source')); metadata=dumps({'collector':collector}).decode(); platform_name={'douyin':'抖音','tiktok':'TikTok','x':'X','telegram':'Telegram','youtube':'YouTube','web':'网页'}.get(source['platform'],source['platform'])
                db.execute("INSERT INTO shares(id,author,url,title,note,status,created,metadata,source_kind) VALUES(?,?,?,?,?,'queued',?,?,'system')",(sid,'system',url,'正在解析'+platform_name+'内容…',note,time.time(),metadata))
                return {'id':sid,'status':'queued','source':source,'poll_url':'/internal/hotspots/'+sid}
            match=re.fullmatch(r'/internal/hotspots/([a-f0-9]{32})',path)
            if match and method=='GET':
                row=db.execute("SELECT * FROM shares WHERE id=? AND source_kind='system'",(match.group(1),)).fetchone()
                if not row: raise AppError('找不到系统热点',404)
                return self._hotspot_state(db,row)
            if path=='/internal/hotspots/reconcile' and method=='POST':
                marker=text(data.get('marker',''),100,True); url=validate_url(data.get('url',''))
                if not re.fullmatch(r'\[hotspot:v1:[a-f0-9]{64}\]',marker): raise AppError('热点标记格式错误')
                rows=db.execute("SELECT * FROM shares WHERE source_kind='system' AND url=? AND substr(note,1,?)=?",(url,len(marker),marker)).fetchall()
                if len(rows)>1: raise AppError('热点对账结果不唯一',409)
                return {'match':None if not rows else {'id':rows[0]['id'],'status':rows[0]['status'],'poll_url':'/internal/hotspots/'+rows[0]['id']}}
            raise AppError('接口不存在',404)

    def migrate_hotspot_history(self,state_path,apply=False):
        state_path=Path(state_path); source=sqlite3.connect('file:'+state_path.as_posix()+'?mode=ro',uri=True); source.row_factory=sqlite3.Row
        try: evidence=[dict(r) for r in source.execute("SELECT share_id,note_marker,candidate_json FROM items WHERE share_id IS NOT NULL AND share_id!=''")]
        finally: source.close()
        result={'eligible':[],'migrated':[],'skipped':{}}
        with self.db() as db:
            for item in evidence:
                sid=str(item.get('share_id') or ''); marker=str(item.get('note_marker') or '')
                try: candidate=json.loads(item.get('candidate_json') or '{}')
                except json.JSONDecodeError: candidate={}
                row=db.execute('SELECT * FROM shares WHERE id=?',(sid,)).fetchone(); valid_marker=bool(re.fullmatch(r'\[hotspot:v1:[a-f0-9]{64}\]',marker)); valid_candidate=isinstance(candidate,dict) and candidate.get('url') and candidate.get('source_id') and candidate.get('source_item_id')
                if not row: result['skipped'][sid]='share_missing'; continue
                if row['source_kind']=='system': result['skipped'][sid]='already_system'; continue
                if not valid_marker or not valid_candidate or not row['note'].startswith(marker) or row['url']!=candidate.get('url') or row['author'] not in ('one','two'): result['skipped'][sid]='evidence_mismatch'; continue
                result['eligible'].append(sid)
                if not apply: continue
                evidence_json=dumps({'marker':marker,'url':candidate['url'],'source_id':candidate['source_id'],'source_item_id':candidate['source_item_id']}).decode(); db.execute('INSERT OR IGNORE INTO system_migration_audit(share_id,previous_author,evidence_json,migrated) VALUES(?,?,?,?)',(sid,row['author'],evidence_json,time.time())); db.execute("UPDATE shares SET author='system',source_kind='system' WHERE id=?",(sid,)); result['migrated'].append(sid)
        return result

    def media_usage(self, db):
        return int(db.execute('SELECT COALESCE(sum(size),0) FROM assets').fetchone()[0])

    def evict_oldest_media(self, db, bytes_needed):
        freed = 0
        rows = db.execute("SELECT id FROM shares WHERE source_kind='system' ORDER BY created ASC").fetchall()
        for row in rows:
            sid = row['id']
            assets = [dict(r) for r in db.execute('SELECT id,object_key,size FROM assets WHERE share_id=?',(sid,)).fetchall()]
            for asset in assets:
                try:
                    self.storage.delete(asset['id'],asset['object_key'])
                except Exception:
                    pass
                freed += int(asset['size'])
            db.execute('DELETE FROM assets WHERE share_id=?',(sid,))
            for table in ('comments','reactions','reads'):
                db.execute(f'DELETE FROM {table} WHERE share_id=?',(sid,))
            db.execute('DELETE FROM shares WHERE id=?',(sid,))
            if freed >= bytes_needed:
                break
        return freed

    def ensure_media_capacity(self, db, additional):
        if additional < 0:
            raise AppError('内容大小无效',400)
        current = self.media_usage(db)
        if current + additional > self.max_media_bytes:
            target = int(self.max_media_bytes * 0.8)
            needed = (current + additional) - target
            if needed > 0:
                self.evict_oldest_media(db, needed)
            if self.media_usage(db) + additional > self.max_media_bytes:
                raise AppError('媒体存储空间不足，已达 10GB 容量上限',507)

    def save_image(self, db, sid, value):
        from PIL import Image, ImageOps, UnidentifiedImageError
        if db.execute("SELECT count(*) FROM assets WHERE share_id=? AND kind='image'",(sid,)).fetchone()[0] >= 10:
            raise AppError('每条分享最多保存 10 张图片')
        if not isinstance(value,str) or len(value)>7*1024*1024:
            raise AppError('图片过大')
        try:
            raw = base64.b64decode(value,validate=True)
            if len(raw)>5*1024*1024:
                raise ValueError()
            Image.MAX_IMAGE_PIXELS = 20_000_000
            with Image.open(io.BytesIO(raw)) as im:
                if im.width*im.height > 20_000_000:
                    raise ValueError()
                im = ImageOps.exif_transpose(im)
                im.thumbnail((2000,2000))
                if im.mode not in ('RGB','RGBA'):
                    im=im.convert('RGBA' if 'transparency' in im.info else 'RGB')
                out=io.BytesIO()
                im.save(out,format='WEBP',quality=85)
                cleaned=out.getvalue()
        except Exception as exc:
            raise AppError('图片无法读取；支持 JPG、PNG、WebP、GIF，最大 5 MB') from exc
        self.ensure_media_capacity(db,len(cleaned))
        asset_id=hashlib.sha256(sid.encode()+cleaned).hexdigest()
        target=self.media / asset_id
        try:
            target.write_bytes(cleaned)
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise AppError('媒体存储空间不足，请删除旧分享后重试',507) from exc
        try:
            stored=self.storage.publish(sid,asset_id,'image/webp')
        except StorageError as exc:
            (self.media / asset_id).unlink(missing_ok=True)
            raise AppError(str(exc),503) from exc
        db.execute('INSERT OR IGNORE INTO assets(id,share_id,mime,size,kind,object_key,sha256) VALUES(?,?,?,?,?,?,?)',(asset_id,sid,'image/webp',len(cleaned),'image',stored['object_key'],stored['sha256']))
        return {'asset':asset_id,'src':'/media/'+asset_id}

    def save_video_chunk(self, db, sid, data):
        try:
            offset=int(data.get('offset',-1))
        except (TypeError,ValueError):
            raise AppError('视频分块位置无效')
        value=data.get('data','')
        if not isinstance(value,str) or len(value)>6*1024*1024:
            raise AppError('视频分块过大')
        try:
            raw=base64.b64decode(value,validate=True)
        except Exception as exc:
            raise AppError('视频分块无法读取') from exc
        upload=text(data.get('upload',''),64,True)
        if not re.fullmatch('[a-f0-9]{32,64}',upload):
            raise AppError('视频上传标识无效')
        part=self.media/(sid+'-'+upload+'.part')
        current=part.stat().st_size if part.exists() else 0
        if offset!=current or current+len(raw)>MAX_VIDEO:
            raise AppError('视频分块顺序或大小无效',409)
        self.ensure_media_capacity(db,current+len(raw))
        try:
            with part.open('ab') as stream:
                stream.write(raw)
        except OSError as exc:
            part.unlink(missing_ok=True)
            raise AppError('媒体存储空间不足，请删除旧分享后重试',507) from exc
        if data.get('final') is not True:
            return {'ok':True,'offset':current+len(raw)}
        if current+len(raw)<12:
            part.unlink(missing_ok=True)
            raise AppError('视频文件无效')
        if not valid_mp4(part):
            part.unlink(missing_ok=True)
            raise AppError('视频必须是 MP4 格式')
        digest_hash=hashlib.sha256(sid.encode())
        with part.open('rb') as stream:
            while True:
                chunk=stream.read(1024*1024)
                if not chunk:
                    break
                digest_hash.update(chunk)
        digest=digest_hash.hexdigest()
        final=self.media/digest
        try:
            part.replace(final)
        except OSError as exc:
            part.unlink(missing_ok=True)
            raise AppError('媒体存储空间不足，请删除旧分享后重试',507) from exc
        size=final.stat().st_size
        if db.execute("SELECT count(*) FROM assets WHERE share_id=? AND kind='video' AND id<>?",(sid,digest)).fetchone()[0]:
            final.unlink(missing_ok=True)
            raise AppError('每条分享最多保存 1 个视频',409)
        try:
            stored=self.storage.publish(sid,digest,'video/mp4')
        except StorageError as exc:
            final.unlink(missing_ok=True)
            raise AppError(str(exc),503) from exc
        db.execute('INSERT OR REPLACE INTO assets(id,share_id,mime,size,kind,object_key,sha256) VALUES(?,?,?,?,?,?,?)',(digest,sid,'video/mp4',size,'video',stored['object_key'],stored['sha256']))
        return {'ok':True,'asset':digest,'src':'/media/'+digest,'size':size}

    def cleanup_video_parts(self,sid):
        for part in self.media.glob(sid+'-*.part'):
            part.unlink(missing_ok=True)

    def clean_blocks(self,db,sid,blocks):
        if not isinstance(blocks,list) or len(blocks)>500:
            raise AppError('正文过长或格式错误')
        result=[]
        image_count=0
        video_count=0
        for b in blocks:
            if not isinstance(b,dict):
                raise AppError('正文格式错误')
            if b.get('type') in ('paragraph','heading','quote'):
                result.append({'type':b['type'],'text':text(b.get('text',''),12000,True)})
            elif b.get('type')=='image':
                image_count+=1
                if image_count>10:
                    raise AppError('每条分享最多展示 10 张图片')
                aid=text(b.get('asset',''),64,True)
                if not db.execute('SELECT 1 FROM assets WHERE id=? AND share_id=?',(aid,sid)).fetchone():
                    raise AppError('图片尚未上传')
                result.append({'type':'image','asset':aid,'src':'/media/'+aid,'alt':text(b.get('alt',''),300)})
            elif b.get('type')=='video':
                video_count+=1
                if video_count>1:
                    raise AppError('每条分享最多展示 1 个视频')
                aid=text(b.get('asset',''),64,True)
                asset=db.execute("SELECT 1 FROM assets WHERE id=? AND share_id=? AND kind='video'",(aid,sid)).fetchone()
                if not asset:
                    raise AppError('视频尚未上传')
                poster=text(b.get('poster',''),64)
                if poster and not db.execute("SELECT 1 FROM assets WHERE id=? AND share_id=? AND kind='image'",(poster,sid)).fetchone():
                    raise AppError('视频封面尚未上传')
                result.append({'type':'video','asset':aid,'src':'/media/'+aid,'poster':('/media/'+poster) if poster else ''})
            else:
                raise AppError('正文包含不支持的内容类型')
        if len(dumps(result))>1024*1024:
            raise AppError('正文过长')
        return result

    def translate_content(self, db, data):
        title = str(data.get('title') or '').strip()[:300]
        body = str(data.get('body') or '').strip()[:50000]
        if not title and not body:
            return {'title': '', 'body': '', 'ok': True}, {}
        combined = f"{title}\n---\n{body}"
        h = hashlib.sha256(combined.encode('utf-8')).hexdigest()
        db.execute('''CREATE TABLE IF NOT EXISTS translations (
            hash TEXT PRIMARY KEY,
            result_json TEXT NOT NULL,
            created REAL NOT NULL
        )''')
        row = db.execute('SELECT result_json FROM translations WHERE hash=?', (h,)).fetchone()
        if row:
            try:
                cached = json.loads(row[0])
                cached['cached'] = True
                return cached, {}
            except Exception:
                pass

        worker_url = os.environ.get('GEMINI_WORKER_URL', '').strip()
        if worker_url:
            try:
                result = self.call_worker_translate(title, body, worker_url)
                db.execute('INSERT OR REPLACE INTO translations VALUES(?,?,?)', (h, dumps(result).decode(), time.time()))
                result['cached'] = False
                return result, {}
            except Exception as e:
                print('Worker translate failed:', type(e).__name__, e, flush=True)

        api_key = os.environ.get('GEMINI_API_KEY', '').strip()
        if api_key:
            try:
                result = self.call_gemini_translate(title, body, api_key)
                db.execute('INSERT OR REPLACE INTO translations VALUES(?,?,?)', (h, dumps(result).decode(), time.time()))
                result['cached'] = False
                return result, {}
            except Exception as e:
                print('Gemini translate failed:', type(e).__name__, e, flush=True)

        try:
            result = self.call_mymemory_translate(title, body)
            db.execute('INSERT OR REPLACE INTO translations VALUES(?,?,?)', (h, dumps(result).decode(), time.time()))
            result['cached'] = False
            return result, {}
        except Exception as e:
            raise AppError('翻译服务暂时不可用，请稍后重试', 503) from e

    def call_worker_translate(self, title, body, worker_url):
        payload = {'title': title, 'body': body}
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            worker_url,
            data=data,
            headers={
                'Content-Type': 'application/json',
                'User-Agent': 'Together-App/1.0',
            }
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return {
                'title': data.get('title', title),
                'body': data.get('body', body),
                'ok': True,
                'provider': data.get('provider', 'gemini-worker')
            }

    def call_gemini_translate(self, title, body, api_key):
        model = os.environ.get('GEMINI_MODEL', 'gemini-flash-lite-latest')
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}'
        prompt = (
            "You are a professional translator. Translate the following title and body text into natural, fluent Simplified Chinese (简体中文).\n"
            "Keep the exact same paragraph breaks. Do not add markdown code blocks, conversational comments or explanations.\n"
            "Output valid JSON formatted exactly as:\n"
            '{"title": "...", "body": "..."}\n\n'
            f"Title:\n{title}\n\n"
            f"Body:\n{body}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"response_mime_type": "application/json"}
        }
        req = urllib.request.Request(url, data=dumps(payload), headers={'Content-Type': 'application/json'})
        proxy_url = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy') or 'http://127.0.0.1:7890'
        proxy_handler = urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url})
        opener = urllib.request.build_opener(proxy_handler)
        with opener.open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            text_resp = data['candidates'][0]['content']['parts'][0]['text']
            parsed = json.loads(text_resp)
            return {'title': parsed.get('title', title), 'body': parsed.get('body', body), 'ok': True, 'provider': 'gemini'}

    def call_mymemory_translate(self, title, body):
        def tr_one(t):
            if not t or not t.strip(): return ''
            q = urllib.parse.quote(t.strip())
            url = f'https://api.mymemory.translated.net/get?q={q}&langpair=en%7Czh-CN'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return data.get('responseData', {}).get('translatedText', t)

        tr_title = tr_one(title) if title else ''
        paragraphs = [p for p in body.split('\n') if p.strip()]
        tr_paras = []
        for p in paragraphs[:6]:
            tr_paras.append(tr_one(p))
        return {'title': tr_title or title, 'body': '\n\n'.join(tr_paras) if tr_paras else body, 'ok': True, 'provider': 'mymemory'}

    def api(self,h,method,path,data,query=None):
        query=query or {}
        if self.node_role != 'primary' and method != 'GET':
            raise AppError('备用节点尚未提升为主节点',503)
        if path=='/api/login' and method=='POST':
            return self.login(h,data)
        user=self.user(h)
        uid=user['id']
        if path=='/api/me' and method=='GET':
            return {'user':user},{}
        if path=='/api/logout' and method=='POST':
            cookie=SimpleCookie(h.headers.get('Cookie',''))
            with self.db() as db:
                db.execute('DELETE FROM sessions WHERE token=?',(token_hash(cookie['session'].value),))
            return {'ok':True},{'Set-Cookie':self.cookie('',0)}
        with self.db() as db:
            if path=='/api/translate' and method=='POST':
                return self.translate_content(db, data)
            if path=='/api/system' and method=='GET':
                last=db.execute("SELECT last_seen FROM service_state WHERE id='worker'").fetchone()
                seen=last[0] if last else None
                return {'version':'0.3.0-test','worker_recent_contact':bool(seen and time.time()-seen<60),'worker_last_seen':seen,
                    'media_bytes':self.media_usage(db),'media_limit_bytes':self.max_media_bytes,
                    'remote_media':self.storage.remote_enabled},{}
            if path=='/api/link-preview' and method=='POST':
                if data.get('source_kind')=='system' or data.get('author')=='system': raise AppError('不能伪造系统身份')
                try:
                    source=source_reference(data.get('text',data.get('url','')))
                except ValueError as exc:
                    raise AppError(str(exc)) from exc
                url=validate_url(source['url'])
                pending=db.execute("SELECT count(*) FROM shares WHERE status IN ('queued','fetching')").fetchone()[0]
                if pending>=10:
                    raise AppError('等待导入的内容较多，请稍后再试',429)
                sid=new_id()
                note=text(data.get('note',''),1000)
                platform_name={'douyin':'抖音','tiktok':'TikTok','x':'X','telegram':'Telegram','web':'网页'}[source['platform']]
                db.execute('INSERT INTO shares(id,author,url,title,note,status,created) VALUES(?,?,?,?,?,?,?)',(sid,uid,url,'正在解析'+platform_name+'内容…',note,'queued',time.time()))
                return {'id':sid,'status':'queued','source':source,'poll_url':'/api/link-preview/'+sid},{}
            preview_match=re.fullmatch(r'/api/link-preview/([a-f0-9]{32})',path)
            if preview_match:
                if method!='GET':
                    raise AppError('不支持的操作',405)
                row=self.visible(db,preview_match.group(1),uid,True)
                return self.preview(db,row,uid),{}
            if path=='/api/shares' and method=='GET':
                values=query.get('source_kind',[])
                if len(values)>1 or (values and values[0] not in ('manual','system')): raise AppError('来源分类不正确')
                kind=values[0] if values else None
                try: limit=int(query.get('limit',[15])[0])
                except Exception: limit=15
                limit=max(1,min(limit,50))
                try: offset=int(query.get('offset',[0])[0])
                except Exception: offset=0
                offset=max(0,min(offset,100_000))

                if kind=='system':
                    total=db.execute("SELECT count(*) FROM shares WHERE source_kind='system' AND status='published'").fetchone()[0]
                    rows=db.execute("SELECT * FROM shares WHERE source_kind='system' AND status='published' ORDER BY COALESCE(published,created) DESC LIMIT ? OFFSET ?",(limit,offset)).fetchall()
                elif kind=='manual':
                    total=db.execute("SELECT count(*) FROM shares WHERE source_kind='manual' AND ((status NOT IN ('failed','review')) OR author=?)",(uid,)).fetchone()[0]
                    rows=db.execute("SELECT * FROM shares WHERE source_kind='manual' AND ((status NOT IN ('failed','review')) OR author=?) ORDER BY COALESCE(published,created) DESC LIMIT ? OFFSET ?",(uid,limit,offset)).fetchall()
                else:
                    total=db.execute("SELECT count(*) FROM shares WHERE (source_kind='system' AND status='published') OR (source_kind='manual' AND ((status NOT IN ('failed','review')) OR author=?))",(uid,)).fetchone()[0]
                    rows=db.execute("SELECT * FROM shares WHERE (source_kind='system' AND status='published') OR (source_kind='manual' AND ((status NOT IN ('failed','review')) OR author=?)) ORDER BY COALESCE(published,created) DESC LIMIT ? OFFSET ?",(uid,limit,offset)).fetchall()
                is_summary = query.get('summary',['0'])[0] in ('1','true','yes')
                return {'shares':[self.serial(db,r,uid,summary=is_summary) for r in rows],'total':total,'has_more':(offset+len(rows)<total)},{}
            if path=='/api/shares' and method=='POST':
                if data.get('source_kind')=='system' or data.get('author')=='system': raise AppError('不能伪造系统身份')
                note=text(data.get('note',''),1000)
                manual=data.get('manual') is True
                if manual and not data.get('url'):
                    url=''
                else:
                    try:
                        url=validate_url(source_reference(data.get('text',data.get('url','')))['url'])
                    except ValueError as exc:
                        raise AppError(str(exc)) from exc
                sid=new_id()
                title=text(data.get('title',''),300) or ('手动分享' if manual else '正在准备内容…')
                if not manual:
                    pending=db.execute("SELECT count(*) FROM shares WHERE status IN ('queued','fetching')").fetchone()[0]
                    if pending>=10:
                        raise AppError('等待导入的内容较多，请稍后再试',429)
                db.execute('INSERT INTO shares(id,author,url,title,note,status,created) VALUES(?,?,?,?,?,?,?)',(sid,uid,url,title,note,'review' if manual else 'queued',time.time()))
                if manual:
                    body=text(data.get('body',''),50000,True)
                    blocks=[{'type':'paragraph','text':s.strip()} for s in body.split('\n') if s.strip()]
                    blocks=self.clean_blocks(db,sid,blocks)
                    db.execute('UPDATE shares SET blocks=? WHERE id=?',(dumps(blocks).decode(),sid))
                return {'id':sid},{}
            match=re.fullmatch(r'/api/shares/([a-f0-9]{32})(?:/(publish|comment|reaction|read|retry|edit|image|cancel|remove-image))?',path)
            if not match:
                raise AppError('接口不存在',404)
            sid,action=match.groups()
            if method!='GET': db.execute('BEGIN IMMEDIATE')
            row=self.visible(db,sid,uid,False)
            owner_action=action in ('publish','retry','edit','image','cancel','remove-image')
            if row['source_kind']=='system':
                if owner_action or (method=='DELETE' and row['status']!='published'): raise AppError('找不到这条分享',404)
            elif (owner_action or method=='DELETE') and row['author']!=uid:
                raise AppError('找不到这条分享',404)
            if not action and method=='GET':
                return {'share':self.serial(db,row,uid)},{}
            if not action and method=='DELETE':
                self.cleanup_video_parts(sid)
                for table in ('comments','reactions','reads'):
                    db.execute(f'DELETE FROM {table} WHERE share_id=?',(sid,))
                assets=[dict(r) for r in db.execute('SELECT id,object_key FROM assets WHERE share_id=?',(sid,))]
                for asset in assets:
                    try:
                        self.storage.delete(asset['id'],asset['object_key'])
                    except StorageError as exc:
                        raise AppError(str(exc),503) from exc
                db.execute('DELETE FROM assets WHERE share_id=?',(sid,))
                db.execute('DELETE FROM shares WHERE id=?',(sid,))
                return {'ok':True},{}
            if method!='POST':
                raise AppError('不支持的操作',405)
            if action=='publish':
                if row['status']!='review' or not json.loads(row['blocks']):
                    raise AppError('请先完成内容导入或补充正文')
                db.execute("UPDATE shares SET status='published',published=?,lease=NULL WHERE id=?",(time.time(),sid))
            elif action=='retry':
                if row['status']!='failed' or not row['url']:
                    raise AppError('当前内容无需重试')
                pending=db.execute("SELECT count(*) FROM shares WHERE status IN ('queued','fetching')").fetchone()[0]
                if pending>=10:
                    raise AppError('等待导入的内容较多，请稍后再试',429)
                self.cleanup_video_parts(sid)
                db.execute("UPDATE shares SET status='queued',phase='',error='',attempts=0,lease=NULL,next_try=0 WHERE id=?",(sid,))
            elif action=='cancel':
                if row['status'] not in ('queued','fetching'):
                    raise AppError('这条内容已不在导入中，请刷新查看',409)
                self.cleanup_video_parts(sid)
                db.execute("UPDATE shares SET status='failed',phase='',lease=NULL,error='已停止自动导入，可以手动补充正文或重新导入' WHERE id=?",(sid,))
            elif action=='edit':
                if row['status'] not in ('review','failed'):
                    raise AppError('请等待导入结束；已发布的内容不能修改')
                body=text(data.get('body',''),50000,True)
                blocks=[{'type':'paragraph','text':s.strip()} for s in body.split('\n') if s.strip()]
                blocks += [b for b in json.loads(row['blocks']) if b['type']=='image']
                blocks=self.clean_blocks(db,sid,blocks)
                url=text(data.get('url',row['url']),2000)
                if url:
                    url=validate_url(url)
                changed=url!=row['url']
                warnings=['已更换来源链接；当前内容未经重新抓取，请核对。'] if changed else json.loads(row['warnings'])
                db.execute("UPDATE shares SET title=?,note=?,url=?,blocks=?,metadata=?,fetched=?,status='review',error='',warnings=?,phase='',lease=NULL WHERE id=?",(text(data.get('title',''),300,True),text(data.get('note',''),1000),url,dumps(blocks).decode(),'{}' if changed else row['metadata'],None if changed else row['fetched'],dumps(warnings).decode(),sid))
            elif action=='remove-image':
                if row['status'] not in ('review','failed'):
                    raise AppError('只能移除尚未发布的图片')
                aid=text(data.get('asset',''),64,True)
                asset=db.execute('SELECT id,object_key FROM assets WHERE id=? AND share_id=?',(aid,sid)).fetchone()
                if not asset:
                    raise AppError('图片不存在',404)
                blocks=[b for b in json.loads(row['blocks']) if b.get('asset')!=aid]
                db.execute('UPDATE shares SET blocks=? WHERE id=?',(dumps(blocks).decode(),sid))
                try:
                    self.storage.delete(asset['id'],asset['object_key'])
                except StorageError as exc:
                    raise AppError(str(exc),503) from exc
                db.execute('DELETE FROM assets WHERE id=?',(aid,))
            elif action=='image':
                if row['status'] not in ('review','failed'):
                    raise AppError('只能在待确认的内容中添加图片')
                asset=self.save_image(db,sid,data.get('data',''))
                blocks=json.loads(row['blocks'])
                blocks.append({'type':'image','asset':asset['asset'],'src':asset['src'],'alt':'手动补充的图片'})
                db.execute('UPDATE shares SET blocks=? WHERE id=?',(dumps(blocks).decode(),sid))
            elif action in ('comment','reaction','read'):
                if row['status']!='published':
                    raise AppError('发布后才能互动')
                if action=='comment':
                    db.execute('INSERT INTO comments VALUES(?,?,?,?,?)',(new_id(),sid,uid,text(data.get('body',''),2000,True),time.time()))
                elif action=='reaction':
                    emoji=data.get('emoji','')
                    if emoji not in ('👀','❤️','🤔','😂',''):
                        raise AppError('不支持的表情')
                    db.execute('DELETE FROM reactions WHERE share_id=? AND user_id=?',(sid,uid))
                    if emoji:
                        db.execute('INSERT INTO reactions VALUES(?,?,?)',(sid,uid,emoji))
                else:
                    db.execute('INSERT OR IGNORE INTO reads VALUES(?,?)',(sid,uid))
            else:
                raise AppError('接口不存在',404)
            return {'ok':True},{}

def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,fmt,*args):
            # Do not log URL query strings, cookies, article contents or credentials.
            pass

        def reply(self,status,body,ctype='application/json; charset=utf-8',headers=None):
            if not isinstance(body,bytes):
                body=dumps(body)
            headers=headers or {}
            self.send_response(status)
            self.send_header('Content-Type',ctype)
            self.send_header('Content-Length',str(len(body)))
            if 'Cache-Control' not in headers:
                self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Frame-Options','SAMEORIGIN')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self'; connect-src 'self'; font-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            for k,v in headers.items():
                self.send_header(k,v)
            self.end_headers()
            if self.command!='HEAD':
                self.wfile.write(body)

        def run_request(self):
            self.connection.settimeout(30)
            request_url=urlsplit(self.path); path=request_url.path; query=parse_qs(request_url.query,keep_blank_values=True)
            try:
                data={}
                if self.command in ('POST','DELETE'):
                    if self.headers.get('Transfer-Encoding'):
                        raise AppError('不支持的请求编码',400)
                    size=int(self.headers.get('Content-Length','0'))
                    if size<0 or size>MAX_BODY:
                        raise AppError('上传内容过大',413)
                    if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                        raise AppError('需要 JSON 请求',415)
                    if path.startswith('/api/'):
                        origin=self.headers.get('Origin')
                        if origin:
                            orig_host = urlsplit(origin).netloc.split(':')[0]
                            req_host = (self.headers.get('Host') or '').split(':')[0]
                            if orig_host and req_host and orig_host != req_host:
                                raise AppError('请求来源不正确',403)
                    data=json.loads(self.rfile.read(size)) if size else {}
                    if not isinstance(data,dict):
                        raise AppError('请求格式不正确')
                if path=='/healthz' and self.command in ('GET','HEAD'):
                    return self.reply(200,{'ok':True,'role':app.node_role})
                if path.startswith('/internal/hotspots'):
                    return self.reply(200,app.hotspots(self,self.command,path,data))
                if path.startswith('/internal/'):
                    if self.command!='POST': raise AppError('不支持的操作',405)
                    return self.reply(200,app.worker(self,path,data))
                if path.startswith('/api/'):
                    value,headers=app.api(self,self.command,path,data,query=query)
                    return self.reply(200,value,headers=headers)
                if self.command not in ('GET','HEAD'):
                    raise AppError('接口不存在',404)
                if path.startswith('/media/'):
                    user=app.user(self)
                    aid=path[len('/media/'):]
                    if not re.fullmatch('[a-f0-9]{64}',aid):
                        raise AppError('图片不存在',404)
                    with app.db() as db:
                        asset=db.execute('SELECT * FROM assets WHERE id=?',(aid,)).fetchone()
                        if not asset:
                            raise AppError('图片不存在',404)
                        app.visible(db,asset['share_id'],user['id'])
                    try:
                        file=app.storage.materialize(aid,asset['object_key'],asset['size'],asset['sha256'])
                    except StorageError as exc:
                        raise AppError(str(exc),503) from exc
                    size=file.stat().st_size
                    etag='"'+aid+'"'
                    cache_headers={'Accept-Ranges':'bytes','Cache-Control':MEDIA_CACHE_CONTROL,
                                   'ETag':etag,'Vary':'Cookie'}
                    range_header=self.headers.get('Range','')
                    if not range_header and self.headers.get('If-None-Match','').strip()==etag:
                        self.send_response(304)
                        for key,value in cache_headers.items():
                            self.send_header(key,value)
                        self.end_headers()
                        return
                    if app.x_accel_media:
                        internal='/__together_video/' if asset['kind']=='video' else '/__together_image/'
                        self.send_response(200)
                        self.send_header('Content-Type',asset['mime'])
                        self.send_header('X-Accel-Redirect',internal+aid)
                        for key,value in cache_headers.items():
                            self.send_header(key,value)
                        self.send_header('X-Content-Type-Options','nosniff')
                        self.end_headers()
                        return
                    if range_header:
                        match=re.fullmatch(r'bytes=(\d*)-(\d*)',range_header.strip())
                        if not match:
                            raise AppError('请求范围无效',416)
                        start=int(match.group(1) or 0)
                        end=int(match.group(2) or size-1)
                        if start<0 or end<start or start>=size:
                            raise AppError('请求范围超出文件大小',416)
                        end=min(end,size-1)
                        length=end-start+1
                        self.send_response(206)
                        self.send_header('Content-Type',asset['mime'])
                        self.send_header('Content-Length',str(length))
                        self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                        for key,value in cache_headers.items():
                            self.send_header(key,value)
                        self.send_header('X-Content-Type-Options','nosniff')
                        self.end_headers()
                        with file.open('rb') as stream:
                            stream.seek(start)
                            remaining=length
                            while remaining:
                                chunk=stream.read(min(65536,remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining-=len(chunk)
                        return
                    return self.reply(200,file.read_bytes(),asset['mime'],cache_headers)
                files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8'),'/video.css':('video.css','text/css; charset=utf-8'),'/manifest.webmanifest':('manifest.webmanifest','application/manifest+json'),'/sw.js':('sw.js','text/javascript'),'/icon.svg':('icon.svg','image/svg+xml'),'/icon-192.png':('icon-192.png','image/png'),'/icon-512.png':('icon-512.png','image/png'),'/avatar-orange.svg':('avatar-orange.svg','image/svg+xml'),'/avatar-peach.svg':('avatar-peach.svg','image/svg+xml'),'/avatar-system.svg':('avatar-system.svg','image/svg+xml')}
                if path not in files:
                    raise AppError('页面不存在',404)
                filename,ctype=files[path]
                if path in ('/', '/app.js', '/style.css', '/video.css', '/sw.js'):
                    cache_headers={'Cache-Control':'no-cache'}
                else:
                    cache_headers={'Cache-Control':'public, max-age=86400'}
                return self.reply(200,(ROOT/'client'/filename).read_bytes(),ctype,cache_headers)
            except AppError as e:
                self.reply(e.status,{'error':e.message})
            except (ValueError,json.JSONDecodeError):
                self.reply(400,{'error':'请求格式不正确'})
            except (BrokenPipeError,ConnectionResetError):
                pass
            except Exception as e:
                print('Request failed:',type(e).__name__,flush=True)
                self.reply(500,{'error':'服务暂时出错，请重试'})

        do_GET=run_request
        do_HEAD=run_request
        do_POST=run_request
        do_DELETE=run_request
    return Handler

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--env',default=str(ROOT/'.env'))
    parser.add_argument('--host',default=None)
    parser.add_argument('--port',type=int,default=None)
    args=parser.parse_args()
    read_env(args.env)
    app=App(os.environ.get('DATA_DIR',str(ROOT/'data')),os.environ.get('WORKER_TOKEN',''),os.environ.get('COOKIE_SECURE','0')=='1',
        node_role=os.environ.get('NODE_ROLE','primary'),
        x_accel_media=os.environ.get('MEDIA_X_ACCEL','0')=='1',
        hotspot_token=os.environ.get('HOTSPOT_SERVICE_TOKEN',''))
    for uid,label in [('one','USER_ONE'),('two','USER_TWO')]:
        pw=os.environ.get(label+'_PASSWORD','')
        if len(pw)<12:
            raise SystemExit(label+'_PASSWORD must contain at least 12 characters; run setup.py')
        app.add_user(uid,os.environ.get(label+'_NAME','小橙' if uid=='one' else '小桃'),pw)
    host=args.host or os.environ.get('HOST','127.0.0.1')
    port=args.port or int(os.environ.get('PORT','8765'))
    print(f'Together is ready: http://{host}:{port}',flush=True)
    ThreadingHTTPServer((host,port),handler_for(app)).serve_forever()

if __name__=='__main__':
    main()
