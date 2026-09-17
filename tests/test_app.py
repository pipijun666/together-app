import base64
import gzip
import http.cookiejar
import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import dumps,check_password
from server import App,handler_for
from worker import Worker,extract,public_addresses,ImportFailure,fetch_public,social_platform
from link_input import extract_url,source_reference,content_id_for_url
from PIL import Image

TOKEN='worker-test-secret-'+'x'*32
PASSWORD='test-password-123456'

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.app=App(cls.tmp.name,TOKEN)
        cls.app.add_user('one','甲',PASSWORD)
        cls.app.add_user('two','乙',PASSWORD)
        cls.http=ThreadingHTTPServer(('127.0.0.1',0),handler_for(cls.app))
        cls.port=cls.http.server_address[1]
        cls.base='http://127.0.0.1:'+str(cls.port)
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start()
        cls.one=cls.client();cls.two=cls.client();cls.anonymous=cls.client()
        cls.call(cls.one,'/api/login',{'user':'one','password':PASSWORD})
        cls.call(cls.two,'/api/login',{'user':'two','password':PASSWORD})
    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown();cls.http.server_close();cls.tmp.cleanup()
    @staticmethod
    def client():
        return urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    @classmethod
    def call(cls,client,path,data=None,method=None,headers=None):
        h={'Content-Type':'application/json'} if data is not None else {}
        h.update(headers or {})
        req=urllib.request.Request(cls.base+path,data=dumps(data) if data is not None else None,headers=h,method=method or ('POST' if data is not None else 'GET'))
        try:
            with client.open(req,timeout=10) as r:
                raw=r.read();return r.status,json.loads(raw) if 'json' in r.headers.get('Content-Type','') else raw
        except urllib.error.HTTPError as e:
            return e.code,json.loads(e.read())
    def manual(self):
        code,r=self.call(self.one,'/api/shares',{'manual':True,'title':'A private story','body':'This is a manually shared story for two people.','note':'What do you think?'})
        self.assertEqual(code,200);return r['id']
    def test_01_auth_and_draft_visibility(self):
        self.assertEqual(self.call(self.anonymous,'/api/shares')[0],401)
        sid=self.manual()
        self.assertEqual(self.call(self.two,'/api/shares/'+sid)[0],404)
        self.assertEqual(self.call(self.two,'/api/shares/'+sid+'/publish',{})[0],404)
        self.assertEqual(self.call(self.two,'/api/shares/'+sid,{},'DELETE')[0],404)
        self.call(self.one,'/api/shares/'+sid+'/publish',{})
        self.assertEqual(self.call(self.two,'/api/shares/'+sid)[0],200)
    def test_02_comments_reactions_and_logout(self):
        sid=self.manual();self.call(self.one,'/api/shares/'+sid+'/publish',{})
        self.assertEqual(self.call(self.two,'/api/shares/'+sid+'/comment',{'body':'<script>alert(1)</script>我看到了'})[0],200)
        self.call(self.two,'/api/shares/'+sid+'/reaction',{'emoji':'❤️'})
        self.call(self.two,'/api/shares/'+sid+'/reaction',{'emoji':'👀'})
        r=self.call(self.one,'/api/shares/'+sid)[1]['share']
        self.assertEqual(len(r['comments']),1);self.assertEqual(len(r['reactions']),1);self.assertEqual(r['reactions'][0]['emoji'],'👀')
        client=self.client();self.call(client,'/api/login',{'user':'one','password':PASSWORD});self.call(client,'/api/logout',{})
        self.assertEqual(self.call(client,'/api/shares')[0],401)
    def test_03_private_images_and_delete(self):
        sid=self.manual();image=Image.new('RGB',(60,40),'orange');out=io.BytesIO();image.save(out,'PNG')
        self.assertEqual(self.call(self.one,'/api/shares/'+sid+'/image',{'data':base64.b64encode(out.getvalue()).decode()})[0],200)
        src=self.call(self.one,'/api/shares/'+sid)[1]['share']['blocks'][-1]['src']
        self.assertEqual(self.call(self.anonymous,src)[0],401);self.assertEqual(self.call(self.two,src)[0],404)
        self.call(self.one,'/api/shares/'+sid+'/publish',{})
        code,raw=self.call(self.two,src);self.assertEqual(code,200);self.assertEqual(Image.open(io.BytesIO(raw)).format,'WEBP')
        self.call(self.one,'/api/shares/'+sid,{},'DELETE');self.assertEqual(self.call(self.one,src)[0],404)
    def test_04_invalid_and_cross_origin_requests(self):
        self.assertEqual(self.call(self.one,'/api/shares',{'url':'file:///etc/passwd'})[0],400)
        self.assertEqual(self.call(self.one,'/api/shares',{'url':'https://user:pw@example.com/'})[0],400)
        self.assertEqual(self.call(self.one,'/api/shares',{'url':'https://example.com:8080/'})[0],400)
        self.assertEqual(self.call(self.one,'/api/shares',{'url':'https://example.com/'},headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.call(self.anonymous,'/internal/jobs/claim',{})[0],401)
    def test_05_ssrf_addresses(self):
        for address in ('127.0.0.1','10.0.0.1','169.254.169.254','192.168.1.1','::1','::ffff:127.0.0.1','224.0.0.1'):
            family=socket.AF_INET6 if ':' in address else socket.AF_INET
            with patch('worker.socket.getaddrinfo',return_value=[(family,socket.SOCK_STREAM,6,'',(address,443))]):
                with self.assertRaises(ImportFailure):public_addresses('source.example',443)
        with self.assertRaises(ImportFailure):fetch_public('file:///etc/passwd')
    def test_06_extractor_strips_executable_markup(self):
        html=b'<html><title>Article</title><body><nav><p>Ignore navigation</p></nav><article><h1>Heading</h1><p>'+b'Useful content. '*12+b'<script>alert(1)</script></p><img src="/photo.png"><iframe src="https://external.example/"></iframe></article></body></html>'
        r=extract(html,'text/html; charset=utf-8','https://example.com/article')
        self.assertEqual(r['title'],'Article')
        self.assertNotIn('alert',str(r['blocks']));self.assertNotIn('Ignore navigation',str(r['blocks']))
        self.assertIsInstance(r['warnings'],list)
    def test_07_full_import_worker_pipeline(self):
        code,r=self.call(self.one,'/api/shares',{'url':'https://public.example/article','note':'Look at this'})
        self.assertEqual(code,200);sid=r['id']
        html=b'<article><h1>A public article</h1><p>'+b'An interesting story with enough body text for reading. '*5+b'</p><img src="https://cdn.example/photo.png" alt="A picture"></article>'
        img=Image.new('RGB',(24,24),'green');buf=io.BytesIO();img.save(buf,'PNG')
        def fixture(url,*args,**kwargs):
            return (buf.getvalue(),'image/png',url) if 'photo.png' in url else (html,'text/html',url)
        worker=Worker(self.base,TOKEN)
        with patch('worker.fetch_public',side_effect=fixture):self.assertTrue(worker.once())
        s=self.call(self.one,'/api/shares/'+sid)[1]['share'];self.assertEqual(s['status'],'published')
        src=s['blocks'][-1]['src'];self.assertTrue(src.startswith('/media/'));self.assertNotIn('cdn.example',json.dumps(s['blocks']))
        self.assertEqual(self.call(self.two,'/api/shares/'+sid)[0],200)
        self.assertEqual(self.call(self.two,src)[0],200)
    def test_08_lease_recovery_and_stale_results(self):
        sid=self.call(self.one,'/api/shares',{'url':'https://public.example/lease'})[1]['id']
        worker=Worker(self.base,TOKEN);first=worker.api('/internal/jobs/claim',{})['job'];self.assertEqual(first['id'],sid)
        with self.app.db() as db:db.execute('UPDATE shares SET lease_until=? WHERE id=?',(time.time()-1,sid))
        second=worker.api('/internal/jobs/claim',{})['job'];self.assertNotEqual(first['lease'],second['lease'])
        with self.assertRaises(urllib.error.HTTPError):worker.api('/internal/jobs/complete',{**first,'title':'old','blocks':[{'type':'paragraph','text':'A stale worker result must not replace a newer task.'}]})
        payload={**second,'title':'new','blocks':[{'type':'paragraph','text':'The valid current worker result can be accepted here.'}]}
        self.assertTrue(worker.api('/internal/jobs/complete',payload)['ok']);self.assertTrue(worker.api('/internal/jobs/complete',payload)['ok'])
    def test_09_concurrent_claim_unique(self):
        sid=self.call(self.one,'/api/shares',{'url':'https://public.example/concurrent'})[1]['id']
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:Worker(self.base,TOKEN).api('/internal/jobs/claim',{})['job'],range(2)))
        self.assertEqual(sum(x is not None and x['id']==sid for x in results),1)
        job=next(x for x in results if x)
        Worker(self.base,TOKEN).api('/internal/jobs/fail',{**job,'error':'test failure','retryable':False})
    def test_10_partial_images_and_manual_recovery(self):
        sid=self.call(self.one,'/api/shares',{'url':'https://public.example/missing'})[1]['id']
        html=b'<article><p>'+b'A story with unavailable photos still has readable text. '*5+b'</p><img src="https://missing.example/photo.png"></article>'
        def fixture(url,*args,**kwargs):
            if 'missing.example/photo' in url:raise ImportFailure('missing')
            return html,'text/html',url
        with patch('worker.fetch_public',side_effect=fixture):Worker(self.base,TOKEN).once()
        s=self.call(self.one,'/api/shares/'+sid)[1]['share'];self.assertEqual(s['status'],'published')
        if s.get('warnings'): self.assertIn('1 张图片',s['warnings'][-1])
        self.assertEqual(self.call(self.two,'/api/shares/'+sid)[0],200)
    def test_11_reject_fake_images_and_external_blocks(self):
        sid=self.manual()
        self.assertEqual(self.call(self.one,'/api/shares/'+sid+'/image',{'data':base64.b64encode(b'<svg onload="alert(1)"/>').decode()})[0],400)
        with self.app.db() as db:
            with self.assertRaises(Exception):self.app.clean_blocks(db,sid,[{'type':'image','asset':'a'*64,'src':'https://evil.example/image'}])
    def test_12_bounded_gzip_decompression(self):
        raw=b'<article><p>'+b'A readable article. '*20+b'</p></article>'
        compressed=gzip.compress(raw)
        class Response:
            status=200
            def __init__(self,payload):self.stream=io.BytesIO(payload)
            def getheader(self,key,default=None):return {'Content-Encoding':'gzip','Content-Type':'text/html','Content-Length':str(len(compressed))}.get(key,default)
            def read(self,n):return self.stream.read(n)
        with patch('worker.public_addresses',return_value=['93.184.216.34']),patch('worker.socket.create_connection',return_value=object()),patch('worker.http.client.HTTPConnection') as connection:
            connection.return_value.getresponse.return_value=Response(compressed)
            self.assertEqual(fetch_public('http://example.com/article')[0],raw)
            connection.return_value.getresponse.return_value=Response(gzip.compress(b'x'*50000))
            with self.assertRaises(ImportFailure):fetch_public('http://example.com/article',max_bytes=1000)
    def test_13_redirect_to_private_address_is_blocked(self):
        class Response:
            status=302
            def getheader(self,key,default=None):return 'http://127.0.0.1/secret' if key=='Location' else default
        def resolve(host,port,**kwargs):return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1' if host=='127.0.0.1' else '93.184.216.34',port))]
        with patch('worker.socket.getaddrinfo',side_effect=resolve),patch('worker.socket.create_connection',return_value=object()),patch('worker.http.client.HTTPConnection') as connection:
            connection.return_value.getresponse.return_value=Response()
            with self.assertRaises(ImportFailure):fetch_public('http://example.com/article')
            self.assertEqual(connection.call_count,1)
    def test_14_authenticated_worker_diagnostics(self):
        self.assertEqual(self.call(self.anonymous,'/api/system')[0],401)
        with self.app.db() as db:db.execute('DELETE FROM service_state')
        self.assertFalse(self.call(self.one,'/api/system')[1]['worker_recent_contact'])
        self.assertEqual(self.call(self.anonymous,'/internal/heartbeat',{})[0],401)
        Worker(self.base,TOKEN).api('/internal/heartbeat',{})
        self.assertTrue(self.call(self.two,'/api/system')[1]['worker_recent_contact'])
        with self.app.db() as db:db.execute('UPDATE service_state SET last_seen=?',(time.time()-120,))
        self.assertFalse(self.call(self.one,'/api/system')[1]['worker_recent_contact'])
    def test_15_cancel_blocks_late_worker_and_manual_source_edit(self):
        sid=self.call(self.one,'/api/shares',{'url':'https://public.example/cancel'})[1]['id']
        worker=Worker(self.base,TOKEN);job=worker.api('/internal/jobs/claim',{})['job'];self.assertEqual(job['id'],sid)
        worker.api('/internal/jobs/progress',{**job,'phase':'images'})
        self.assertEqual(self.call(self.one,'/api/shares/'+sid)[1]['share']['phase'],'images')
        self.assertEqual(self.call(self.two,'/api/shares/'+sid+'/cancel',{})[0],404)
        self.assertEqual(self.call(self.one,'/api/shares/'+sid+'/cancel',{})[0],200)
        with self.assertRaises(urllib.error.HTTPError):worker.api('/internal/jobs/complete',{**job,'title':'Stale','blocks':[{'type':'paragraph','text':'This must not overwrite the manual draft.'}]})
        self.assertEqual(self.call(self.one,'/api/shares/'+sid+'/edit',{'title':'Manual','body':'Manually supplied source-free content.','note':'','url':''})[0],200)
        s=self.call(self.one,'/api/shares/'+sid)[1]['share']
        self.assertEqual(s['url'],'');self.assertEqual(s['status'],'review');self.assertIsNone(s['fetched']);self.assertEqual(s['metadata'],{})
    def test_16_remove_draft_image_author_only(self):
        sid=self.manual();img=Image.new('RGB',(20,20),'blue');buf=io.BytesIO();img.save(buf,'PNG')
        self.call(self.one,'/api/shares/'+sid+'/image',{'data':base64.b64encode(buf.getvalue()).decode()})
        b=self.call(self.one,'/api/shares/'+sid)[1]['share']['blocks'][-1]
        self.assertEqual(self.call(self.two,'/api/shares/'+sid+'/remove-image',{'asset':b['asset']})[0],404)
        self.assertEqual(self.call(self.one,'/api/shares/'+sid+'/remove-image',{'asset':b['asset']})[0],200)
        self.assertEqual(self.call(self.one,b['src'])[0],404)
        self.assertTrue(all(x['type']!='image' for x in self.call(self.one,'/api/shares/'+sid)[1]['share']['blocks']))
        self.call(self.one,'/api/shares/'+sid+'/publish',{})
        self.assertEqual(self.call(self.one,'/api/shares/'+sid+'/remove-image',{'asset':b['asset']})[0],400)
    def test_17_structured_article_body_and_metadata(self):
        data={'@graph':[{'@type':'NewsArticle','headline':'Structured headline','articleBody':'A full article from structured data. '*8,'author':{'name':'Original Author'},'datePublished':'2026-09-01T12:00:00Z','image':{'url':'/photo.png'}}]}
        html='<html><script type="application/ld+json">'+json.dumps(data)+'</script><body><div>Short shell</div></body></html>'
        result=extract(html.encode(),'text/html','https://example.com/news')
        self.assertEqual(result['title'],'Structured headline');self.assertEqual(result['metadata']['author'],'Original Author')
        self.assertEqual(result['metadata']['published_at'],'2026-09-01T12:00:00Z')
        self.assertIsInstance(result['warnings'],list)
    def test_18_lazy_images_preserve_following_text(self):
        html='<meta name="author" content="Alice"><article><p>'+('Before image. '*10)+'<img src="data:image/png;base64,AAAA" data-srcset="/small.png 400w, /large.png 1200w">After the inline image is still part of the article.</p></article>'
        r=extract(html.encode(),'text/html','https://example.com/story')
        self.assertEqual(r['metadata']['author'],'Alice')
        self.assertTrue(any(b.get('url')=='https://example.com/large.png' for b in r['blocks']))
        self.assertTrue(any('After the inline image' in b.get('text','') for b in r['blocks']))
    def test_19_unicode_url_is_encoded_before_http_request(self):
        class Response:
            status=200
            def getheader(self,key,default=None):return {'Content-Type':'text/html'}.get(key,default)
            def read(self,n):return b''
        with patch('worker.public_addresses',return_value=['93.184.216.34']),patch('worker.socket.create_connection',return_value=object()),patch('worker.http.client.HTTPConnection') as connection:
            connection.return_value.getresponse.return_value=Response()
            fetch_public('http://example.com/新闻?q=你好&existing=%20')
            path=connection.return_value.request.call_args.args[1]
            self.assertEqual(path,'/%E6%96%B0%E9%97%BB?q=%E4%BD%A0%E5%A5%BD&existing=%20')
    def test_20_additive_schema_upgrade_preserves_data(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'app.sqlite3'
            destination=sqlite3.connect(path)
            with self.app.db() as db:db.backup(destination)
            destination.execute('ALTER TABLE shares DROP COLUMN metadata');destination.execute('ALTER TABLE shares DROP COLUMN phase');destination.commit()
            before=destination.execute('SELECT id,title,url,blocks FROM shares ORDER BY id').fetchall();destination.close()
            migrated=App(folder,TOKEN)
            with migrated.db() as db:
                after=[tuple(r) for r in db.execute('SELECT id,title,url,blocks FROM shares ORDER BY id')]
                columns={r['name'] for r in db.execute('PRAGMA table_info(shares)')}
            self.assertEqual(before,after);self.assertIn('metadata',columns);self.assertIn('phase',columns)
    def test_21_env_password_rotation_updates_existing_user(self):
        with tempfile.TemporaryDirectory() as folder:
            app=App(folder,TOKEN)
            app.add_user('one','Old name','old-password-123456')
            with app.db() as db:
                original=db.execute('SELECT password FROM users WHERE id=?',('one',)).fetchone()['password']
                db.execute('INSERT INTO sessions VALUES(?,?,?)',('test-session','one',time.time()+60))
            app.add_user('one','New name','old-password-123456')
            with app.db() as db:
                unchanged=db.execute('SELECT name,password FROM users WHERE id=?',('one',)).fetchone()
                self.assertEqual(unchanged['name'],'New name');self.assertEqual(unchanged['password'],original)
                self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0],1)
            app.add_user('one','New name','new-password-654321')
            with app.db() as db:
                rotated=db.execute('SELECT password FROM users WHERE id=?',('one',)).fetchone()['password']
                self.assertTrue(check_password('new-password-654321',rotated))
                self.assertFalse(check_password('old-password-123456',rotated))
                self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0],0)

    def test_22_share_text_url_extraction_and_platforms(self):
        raw='8.43 复制此链接，打开抖音搜索：https://v.douyin.com/K1MiUfUmPx4/?foo=1&bar=2 直接观看！'
        self.assertEqual(extract_url(raw),'https://v.douyin.com/K1MiUfUmPx4/?foo=1&bar=2')
        self.assertEqual(source_reference(raw)['platform'],'douyin')
        self.assertEqual(source_reference('v.douyin.com/AbCd123/ 复制打开抖音')['url'],'https://v.douyin.com/AbCd123/')
        self.assertEqual(content_id_for_url('https://www.douyin.com/video/7624888803265880255?foo=1'),'7624888803265880255')
        self.assertEqual(content_id_for_url('https://x.com/user/status/123456789'),'123456789')
        self.assertEqual(social_platform('https://www.douyin.com/note/7624888803265880255'),'抖音')
        with self.assertRaises(ValueError):
            extract_url('这里只是一段没有链接的文字')

    def test_23_link_preview_api_accepts_full_share_text(self):
        raw='有趣的视频 https://v.douyin.com/TestCode/?utm_source=copy&foo=bar 打开抖音观看。'
        code,result=self.call(self.one,'/api/link-preview',{'text':raw,'note':'一起看看'})
        self.assertEqual(code,200)
        self.assertEqual(result['status'],'queued')
        self.assertEqual(result['source']['platform'],'douyin')
        self.assertEqual(result['source']['url'],'https://v.douyin.com/TestCode/?utm_source=copy&foo=bar')
        code,pending=self.call(self.one,result['poll_url'])
        self.assertEqual(code,200);self.assertEqual(pending['status'],'queued');self.assertIsNone(pending['content'])
        self.assertEqual(self.call(self.two,result['poll_url'])[0],404)
        self.assertEqual(self.call(self.one,'/api/shares/'+result['id']+'/cancel',{})[0],200)

    def test_24_link_preview_returns_normalized_media(self):
        code,created=self.call(self.one,'/api/link-preview',{'text':'看看 https://www.douyin.com/video/7624888803265880255?from=copy'})
        self.assertEqual(code,200)
        worker=Worker(self.base,TOKEN)
        def fake_download(url,directory,deadline):
            video=directory/'source.mp4';video.write_bytes(
                b'\x00\x00\x00\x14ftypmp42'+b'\x00'*8+b'\x00\x00\x00\x0cmdatdata')
            cover=directory/'cover.png';image=Image.new('RGB',(16,20),'purple');image.save(cover,'PNG')
            info={'id':'7624888803265880255','title':'测试视频标题','description':'测试视频正文',
                  'uploader':'公开作者','timestamp':1788825600,'webpage_url':'https://www.douyin.com/video/7624888803265880255'}
            return info,video,cover
        with patch('worker.download_social',side_effect=fake_download):
            self.assertTrue(worker.once())
        code,result=self.call(self.one,created['poll_url'])
        self.assertEqual(code,200);self.assertEqual(result['status'],'published')
        self.assertEqual(result['source']['platform'],'douyin')
        self.assertEqual(result['source']['content_id'],'7624888803265880255')
        self.assertEqual(result['content']['title'],'测试视频标题')
        self.assertEqual(result['content']['author'],'公开作者')
        self.assertEqual(result['content']['content_type'],'video')
        self.assertTrue(result['content']['cover'].startswith('/media/'))
        self.assertTrue(result['content']['videos'][0].startswith('/media/'))

    def test_26_list_visibility_for_progress_and_drafts(self):
        draft=self.manual()
        draft_ids={s['id'] for s in self.call(self.one,'/api/shares')[1]['shares']}
        other_ids={s['id'] for s in self.call(self.two,'/api/shares')[1]['shares']}
        self.assertIn(draft,draft_ids);self.assertNotIn(draft,other_ids)
        queued=self.call(self.one,'/api/link-preview',{'text':'看看 https://v.douyin.com/ListVisible/?from=copy'})[1]['id']
        other_ids={s['id'] for s in self.call(self.two,'/api/shares')[1]['shares']}
        self.assertIn(queued,other_ids)
        self.call(self.one,'/api/shares/'+queued+'/cancel',{})
        other_ids={s['id'] for s in self.call(self.two,'/api/shares')[1]['shares']}
        self.assertNotIn(queued,other_ids)

    def test_27_client_progress_visibility_and_full_share_text(self):
        script=(Path(__file__).resolve().parents[1]/'client'/'app.js').read_text(encoding='utf-8')
        self.assertEqual(script.count('function renderBlocks'),1)
        self.assertIn("['queued','fetching','published'].includes(s.status)",script)
        self.assertIn("(p.get('share')||'').slice(0,10000)",script)
        self.assertIn('版本 0.5',script)
        self.assertNotIn('版本 0.2',script)

    def test_25_link_form_accepts_full_share_text(self):
        script=(Path(__file__).resolve().parents[1]/'client'/'app.js').read_text(encoding='utf-8')
        self.assertIn("api('/api/link-preview',{text:$('#share-url').value",script)
        self.assertIn('粘贴分享内容或链接',script)
        self.assertIn('maxlength="10000" rows="4"',script)
        self.assertNotIn('<input id="share-url" type="url" ${manual?',script)

    def test_28_douyin_adapter_resolve_and_stream(self):
        from adapters.douyin.adapter import resolve_video_id,DouyinError
        self.assertEqual(resolve_video_id('https://www.douyin.com/video/7683443359242931465'),'7683443359242931465')
        self.assertEqual(resolve_video_id('https://www.iesdouyin.com/share/video/7683443359242931465/?region=CN&mid=1'),'7683443359242931465')
        self.assertEqual(resolve_video_id('https://www.douyin.com/note/7683443359242931465?from=share'),'7683443359242931465')
        with self.assertRaises(DouyinError):
            resolve_video_id('')
        with self.assertRaises(DouyinError):
            resolve_video_id('https://example.com/not-douyin/1234567890123456789')

    def test_29_image_limit_is_ten(self):
        sid=self.manual()
        for index in range(10):
            image=Image.new('RGB',(8+index,8),(index*20,0,0));out=io.BytesIO();image.save(out,'PNG')
            self.assertEqual(self.call(self.one,'/api/shares/'+sid+'/image',{'data':base64.b64encode(out.getvalue()).decode()})[0],200)
        image=Image.new('RGB',(8,8),'white');out=io.BytesIO();image.save(out,'PNG')
        code,result=self.call(self.one,'/api/shares/'+sid+'/image',{'data':base64.b64encode(out.getvalue()).decode()})
        self.assertEqual(code,400);self.assertIn('最多保存 10 张',result['error'])

    def test_30_media_capacity_rejects_new_files(self):
        sid=self.manual();previous=self.app.max_media_bytes
        self.app.max_media_bytes=1
        try:
            image=Image.new('RGB',(8,8),'orange');out=io.BytesIO();image.save(out,'PNG')
            code,result=self.call(self.one,'/api/shares/'+sid+'/image',{'data':base64.b64encode(out.getvalue()).decode()})
            self.assertEqual(code,507);self.assertIn('存储空间不足',result['error'])
        finally:
            self.app.max_media_bytes=previous
        system=self.call(self.one,'/api/system')[1]
        self.assertIn('media_bytes',system);self.assertIn('media_limit_bytes',system)

    def test_31_standby_rejects_all_writes(self):
        previous=self.app.node_role;self.app.node_role='standby'
        try:
            self.assertEqual(self.call(self.one,'/api/shares')[0],200)
            code,result=self.call(self.one,'/api/shares',{'manual':True,'title':'blocked','body':'blocked'})
            self.assertEqual(code,503);self.assertIn('备用节点',result['error'])
            self.assertEqual(self.call(self.anonymous,'/internal/heartbeat',{})[0],503)
            code,health=self.call(self.anonymous,'/healthz')
            self.assertEqual(code,200);self.assertEqual(health['role'],'standby')
        finally:
            self.app.node_role=previous

    def test_32_failed_job_removes_partial_video_upload(self):
        sid=self.call(self.one,'/api/shares',{'url':'https://public.example/video'})[1]['id']
        worker=Worker(self.base,TOKEN)
        job=worker.api('/internal/jobs/claim',{})['job']
        self.assertEqual(job['id'],sid)
        upload='a'*32
        worker.api('/internal/jobs/video-chunk',{**job,'upload':upload,'offset':0,
            'data':base64.b64encode(b'partial-video').decode(),'final':False})
        part=self.app.media/(sid+'-'+upload+'.part')
        self.assertTrue(part.exists())
        worker.api('/internal/jobs/fail',{**job,'error':'network interrupted','retryable':True})
        self.assertFalse(part.exists())

if __name__=='__main__':unittest.main(verbosity=2)
