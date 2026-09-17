"""Overseas importer. Fetches public static articles, never executes page scripts."""
import argparse
import base64
import errno
import http.client
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import secrets
import time
import threading
import zlib
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, quote
from common import ROOT, read_env, dumps
from link_input import platform_for_url, content_id_for_url

class ImportFailure(Exception):
    def __init__(self,message,retryable=False):
        super().__init__(message)
        self.retryable=retryable

MAX_SOCIAL_BYTES=200*1024*1024
MIN_FREE_DISK=2*1024*1024*1024  # 下载前至少保留 2 GiB 可用磁盘（§5：低于 2 GiB 拒绝新媒体）
TMP_PREFIX='together-social-'
_ACTIVE_PROCS=[]

def _storage_oserror(exc):
    return isinstance(exc,OSError) and exc.errno in (errno.ENOSPC,errno.EDQUOT,errno.EROFS)

def _kill_tree(proc):
    """Kill the whole process group so yt-dlp/ffmpeg grandchildren die too."""
    if hasattr(os,'killpg'):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (OSError,ProcessLookupError):
            pass
    try:
        proc.kill()
    except Exception:
        pass

# 分享文案中常见的中文/全角尾部标点（含全角右括号），从 worker 侧兜底清理，
# 避免 server 端 link_input 未剥离的字符污染 URL（link_input 属 Codex 范围）。
_TRAILING_CHARS="，。！？；：、）》】』」”）'\"" 

def clean_trailing(url):
    return url.rstrip(_TRAILING_CHARS) if isinstance(url,str) else url

def _on_terminate(signum,frame):
    for proc in list(_ACTIVE_PROCS):
        _kill_tree(proc)
    raise SystemExit(0)

def redact_url(url):
    """Strip query/fragment before storing or logging a source URL."""
    if not url:
        return url
    try:
        p=urlsplit(url)
        return urlunsplit((p.scheme,p.netloc,p.path,'',''))
    except ValueError:
        return str(url)[:2000]

def cleanup_stale_temp():
    """Remove temporary directories left by a killed/impaired previous process."""
    root=Path(tempfile.gettempdir())
    for item in root.glob(TMP_PREFIX+'*'):
        try:
            if item.is_dir():
                shutil.rmtree(item,ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
        except OSError:
            pass

def check_disk_space(path):
    """Refuse to start a download when the server is nearly out of disk."""
    try:
        usage=shutil.disk_usage(str(path))
    except OSError:
        return
    if usage.free<MIN_FREE_DISK:
        raise ImportFailure('服务器存储空间不足，任务已取消；请清理后重试',False)

def run_tool(command,cwd,deadline,timeout_message):
    """Run a subprocess in its own group; classify timeout and disk errors."""
    remaining=max(1,int(deadline-time.monotonic()))
    try:
        proc=subprocess.Popen(command,cwd=str(cwd),stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE,start_new_session=True)
    except OSError as exc:
        if _storage_oserror(exc):
            raise ImportFailure('服务器存储空间不足或写入失败，任务已取消',False) from exc
        raise ImportFailure('解析工具无法启动，请检查部署',False) from exc
    _ACTIVE_PROCS.append(proc)
    try:
        try:
            _,stderr=proc.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            raise ImportFailure(timeout_message,True)
        except OSError as exc:
            _kill_tree(proc)
            if _storage_oserror(exc):
                raise ImportFailure('服务器存储空间不足或写入失败，任务已取消',False) from exc
            raise ImportFailure('解析工具运行异常，请稍后重试',True) from exc
    finally:
        try:
            _ACTIVE_PROCS.remove(proc)
        except ValueError:
            pass
    if proc.returncode and any(marker in (stderr or b'').lower() for marker in
                               (b'no space left on device',b'disk quota exceeded',b'read-only file system')):
        raise ImportFailure('服务器存储空间不足或写入失败，任务已取消',False)
    return proc.returncode

def _env_int(name,default,minimum,maximum):
    try:
        value=int(os.environ.get(name,str(default)))
    except (TypeError,ValueError):
        return default
    return min(max(value,minimum),maximum)

def probe_video(path,deadline):
    """Return a small ffprobe profile, or None when probing is unavailable/invalid."""
    remaining=max(1,min(15,int(deadline-time.monotonic())))
    command=['ffprobe','-v','error','-select_streams','v:0',
        '-show_entries','stream=codec_name,width,height,bit_rate:format=bit_rate,duration',
        '-of','json',str(path)]
    try:
        result=subprocess.run(command,cwd=str(Path(path).parent),stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,timeout=remaining,check=False)
    except (OSError,subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    try:
        payload=json.loads(result.stdout.decode('utf-8'))
        stream=(payload.get('streams') or [])[0]
        fmt=payload.get('format') or {}
        height=int(stream.get('height') or 0)
        width=int(stream.get('width') or 0)
        bitrate=int(stream.get('bit_rate') or fmt.get('bit_rate') or 0)
        duration=float(fmt.get('duration') or 0)
        if not bitrate and duration>0:
            bitrate=int(Path(path).stat().st_size*8/duration)
        return {'codec':str(stream.get('codec_name') or '').lower(),
                'width':width,'height':height,'bitrate':bitrate,'duration':duration}
    except (OSError,ValueError,TypeError,IndexError,KeyError,UnicodeError):
        return None

def mp4_faststart(path):
    """True when the top-level moov box appears before mdat."""
    try:
        total=Path(path).stat().st_size
        offset=0
        with Path(path).open('rb') as stream:
            while offset+8<=total:
                stream.seek(offset)
                header=stream.read(8)
                size=int.from_bytes(header[:4],'big')
                kind=header[4:8]
                header_size=8
                if size==1:
                    extra=stream.read(8)
                    if len(extra)!=8:
                        return False
                    size=int.from_bytes(extra,'big')
                    header_size=16
                elif size==0:
                    size=total-offset
                if size<header_size or offset+size>total:
                    return False
                if kind==b'moov':
                    return True
                if kind==b'mdat':
                    return False
                offset+=size
    except OSError:
        pass
    return False

def optimize_video_for_streaming(path,directory,deadline):
    """Cap expensive video profiles and place MP4 metadata first for progressive playback.

    Optimization is fail-open: errors keep the downloaded source so this performance
    feature cannot turn an otherwise valid import into a failed share.
    """
    path=Path(path)
    if os.environ.get('VIDEO_OPTIMIZE','1').strip().lower() in ('0','false','no','off'):
        return path
    profile=probe_video(path,deadline)
    if not profile:
        return path
    max_height=_env_int('VIDEO_MAX_HEIGHT',720,360,1080)
    max_bitrate=_env_int('VIDEO_MAX_BITRATE',2500000,500000,8000000)
    needs_transcode=(profile['codec']!='h264' or profile['height']>max_height or
                     (profile['bitrate']>0 and profile['bitrate']>max_bitrate))
    if not needs_transcode and path.suffix.lower()=='.mp4' and mp4_faststart(path):
        return path
    check_disk_space(directory)
    target=Path(directory)/('playback-720p.mp4' if needs_transcode else 'playback-faststart.mp4')
    if needs_transcode:
        video_rate=max(400000,min(2200000,max_bitrate-128000))
        scale="scale=w='min(1280,iw)':h='min(%d,ih)':force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2"%max_height
        command=['ffmpeg','-nostdin','-threads','2','-v','error','-y','-i',str(path),
            '-map','0:v:0','-map','0:a:0?','-sn','-dn','-vf',scale,
            '-c:v','libx264','-preset','veryfast','-crf','26','-maxrate',str(video_rate),
            '-bufsize',str(video_rate*2),'-pix_fmt','yuv420p','-c:a','aac','-b:a','96k',
            '-movflags','+faststart',str(target)]
        timeout_message='视频转码超时，请稍后重试'
    else:
        command=['ffmpeg','-nostdin','-threads','2','-v','error','-y','-i',str(path),
            '-map','0:v:0','-map','0:a:0?','-sn','-dn','-c','copy','-movflags','+faststart',str(target)]
        timeout_message='视频索引优化超时，请稍后重试'
    try:
        code=run_tool(command,directory,deadline,timeout_message)
    except ImportFailure:
        target.unlink(missing_ok=True)
        return path
    if code or not target.exists() or target.stat().st_size<16 or target.stat().st_size>MAX_SOCIAL_BYTES:
        target.unlink(missing_ok=True)
        return path
    if needs_transcode and target.stat().st_size>=path.stat().st_size:
        target.unlink(missing_ok=True)
        return path
    return target

def social_platform(url):
    platform=platform_for_url(url)
    return {'douyin':'抖音','tiktok':'TikTok','x':'X','telegram':'Telegram'}.get(platform,'')

def proxy_url():
    """本机海外出口 HTTP 代理（DB-002 隧道 127.0.0.1:8118）。"""
    return (os.environ.get('YTDLP_PROXY') or os.environ.get('PROXY_URL') or '').strip()

def proxy_for_social(url):
    """抖音强制直连；TikTok/X/Telegram 默认走海外代理。"""
    platform=platform_for_url(url)
    if platform=='douyin':
        return ''
    if platform in ('tiktok','x','telegram'):
        return proxy_url()
    return ''

def proxy_for_web(url):
    """普通网页默认直连；仅白名单域名（精确或子域后缀）经代理出口。

    PROXY_WHITELIST 逗号分隔；`example.com` 同时匹配 `example.com` 与
    `*.example.com`；允许写 `.example.com` 或 `*.example.com`。
    """
    proxy=proxy_url()
    if not proxy:
        return ''
    try:
        host=(urlsplit(url).hostname or '').lower().rstrip('.')
    except ValueError:
        return ''
    if not host:
        return ''
    entries=[e.strip().lstrip('*.').lower() for e in os.environ.get('PROXY_WHITELIST','').split(',') if e.strip()]
    for entry in entries:
        if host==entry or host.endswith('.'+entry):
            return proxy
    return ''

def download_social(url,directory,deadline):
    check_disk_space(directory)
    telegram_failure=None
    if platform_for_url(url)=='douyin':
        try:
            from adapters.douyin.adapter import download_social as douyin_download
            from adapters.douyin.adapter import DouyinError
        except ImportError:
            pass  # playwright not installed; fall back to yt-dlp below
        else:
            try:
                return douyin_download(url,directory,deadline)
            except DouyinError as exc:
                raise ImportFailure(str(exc),exc.retryable) from exc
    if platform_for_url(url)=='telegram':
        try:
            from adapters.telegram.adapter import download_social as telegram_download
            from adapters.telegram.adapter import TelegramError
        except ImportError:
            pass  # adapter missing; fall back to yt-dlp below
        else:
            try:
                return telegram_download(url,directory,deadline)
            except TelegramError as exc:
                # Keep the adapter's actionable, already-sanitized failure in case
                # the generic yt-dlp fallback cannot handle this Telegram post.
                telegram_failure=ImportFailure(str(exc),exc.retryable)
    if platform_for_url(url)=='tiktok':
        try:
            from adapters.tiktok.adapter import download_social as tiktok_download
            from adapters.tiktok.adapter import TiktokError
        except ImportError:
            pass  # adapter missing; fall back to yt-dlp below
        else:
            try:
                return tiktok_download(url,directory,deadline)
            except TiktokError as exc:
                raise ImportFailure(str(exc),exc.retryable) from exc
    if platform_for_url(url)=='youtube':
        try:
            from adapters.youtube.adapter import download_social as youtube_download
            from adapters.youtube.adapter import YouTubeError
        except ImportError:
            pass  # adapter missing; fall back to yt-dlp below
        else:
            try:
                return youtube_download(url,directory,deadline)
            except YouTubeError as exc:
                raise ImportFailure(str(exc),exc.retryable) from exc
    output=str(directory/'source.%(ext)s')
    command=[sys.executable,'-m','yt_dlp','--no-config','--no-playlist','--playlist-items','1',
        '--socket-timeout','30','--retries','2','--fragment-retries','2','--max-filesize','200M',
        '--format','bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]',
        '--merge-output-format','mp4','--remux-video','mp4','--write-info-json','--write-thumbnail',
        '--output',output]
    proxy=os.environ.get('YTDLP_PROXY') or os.environ.get('PROXY_URL')
    if proxy and platform_for_url(url)!='douyin':
        # 抖音强制直连；只有非抖音平台才允许 yt-dlp 走海外代理
        command += ['--proxy',proxy]
    command += [url]
    returncode=run_tool(command,directory,deadline,'平台解析超过 10 分钟，请稍后重试')
    if returncode:
        if telegram_failure:
            raise telegram_failure
        raise ImportFailure('平台未提供可导入的公开视频，或暂时限制匿名读取，系统会自动重试',True)
    info_files=list(directory.glob('*.info.json'))
    if not info_files:
        if telegram_failure:
            raise telegram_failure
        raise ImportFailure('平台没有返回可用的公开内容',True)
    try:
        info=json.loads(info_files[0].read_text(encoding='utf-8'))
    except (OSError,ValueError) as exc:
        raise ImportFailure('平台返回的信息无法读取',True) from exc
    videos=[p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in ('.mp4','.m4v','.mov','.mkv','.webm')]
    video=max(videos,key=lambda p:p.stat().st_size) if videos else None
    if video and video.stat().st_size>MAX_SOCIAL_BYTES:
        video.unlink(missing_ok=True)
        video=None
    if video and video.suffix.lower()!='.mp4':
        converted=directory/'source-final.mp4'
        cmd=['ffmpeg','-nostdin','-v','error','-y','-i',str(video),'-vf',"scale='min(1920,iw)':-2",'-c:v','libx264','-preset','veryfast','-crf','20','-c:a','aac','-movflags','+faststart',str(converted)]
        ffmpeg_code=run_tool(cmd,directory,deadline,'视频格式转换超时，请稍后重试')
        if ffmpeg_code:
            raise ImportFailure('视频格式转换失败，请稍后重试',True)
        video=converted
    thumbnails=[p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in ('.webp','.jpg','.jpeg','.png')]
    thumb=max(thumbnails,key=lambda p:p.stat().st_size) if thumbnails else None
    return info,video,thumb

def public_addresses(host,port):
    answers=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)
    addresses=[]
    for family,kind,proto,canon,address in answers:
        ip=ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast or ip.is_unspecified or (isinstance(ip,ipaddress.IPv6Address) and ip.ipv4_mapped and not ip.ipv4_mapped.is_global):
            raise ImportFailure('该链接指向受限制的网络地址')
        if address[0] not in addresses:
            addresses.append(address[0])
    if not addresses:
        raise ImportFailure('无法解析来源网站')
    return addresses

def _validate_target(url):
    """校验目标 URL 形态，并对其 DNS 解析结果做 SSRF 检查。

    返回 (host, port, scheme, addresses)。解析出的每个地址都必须是全局
    公网地址；回环、私网、链路本地、多播、未指定及保留地址一律拒绝。
    """
    p=urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.port not in (None,80,443):
        raise ImportFailure('只支持公开网页的标准 HTTP/HTTPS 地址')
    host=p.hostname.encode('idna').decode('ascii')
    port=p.port or (443 if p.scheme=='https' else 80)
    addresses=public_addresses(host,port)
    return host,port,p.scheme,addresses

def fetch_public(url,max_bytes=3*1024*1024,deadline=None,proxy=''):
    """Validate every redirect and pin the actual socket to a validated public IP.

    proxy 非空时改经本机 HTTP 代理出口（DB-003）。直连与代理两条路径都会在
    发起请求前对目标做 SSRF 校验，并在每次重定向后重新校验新目标。
    """
    if proxy:
        return fetch_via_proxy(url,proxy,max_bytes,deadline)
    for redirect in range(5):
        if deadline and time.monotonic()>deadline:
            raise ImportFailure('导入超过时间限制，请手动补充')
        try:
            host,port,scheme,addresses=_validate_target(url)
            p=urlsplit(url)
            conn=http.client.HTTPConnection(host,port,timeout=15)
            raw=socket.create_connection((addresses[0],port),timeout=15)
            try:
                conn.sock=ssl.create_default_context().wrap_socket(raw,server_hostname=host) if scheme=='https' else raw
            except Exception:
                raw.close()
                raise
            path=quote(p.path or '/',safe="/%:@!$&'()*+,;=-._~")
            if p.query:
                path+='?'+quote(p.query,safe="%:@!$&'()*+,;=/?-._~")
            try:
                conn.request('GET',path,headers={'User-Agent':'TogetherReader/0.2 (private article reader)','Accept':'text/html,image/*;q=0.8','Accept-Encoding':'gzip, deflate','Connection':'close'})
                response=conn.getresponse()
                if response.status in (301,302,303,307,308):
                    location=response.getheader('Location')
                    if not location:
                        raise ImportFailure('来源网站返回了无效跳转')
                    url=urljoin(url,location)
                    continue
                if response.status!=200:
                    raise ImportFailure(f'来源网站返回 HTTP {response.status}；可手动补充文字或截图')
                encoding=response.getheader('Content-Encoding','identity').lower().strip()
                if encoding not in ('identity','gzip','deflate'):
                    raise ImportFailure('来源返回了暂不支持的压缩编码')
                decoder=zlib.decompressobj(16+zlib.MAX_WBITS if encoding=='gzip' else zlib.MAX_WBITS) if encoding!='identity' else None
                declared=response.getheader('Content-Length')
                if declared and int(declared)>max_bytes:
                    raise ImportFailure('来源文件超过大小限制')
                chunks=[]
                size=0
                received=0
                end=min(deadline or float('inf'),time.monotonic()+35)
                while True:
                    if time.monotonic()>end:
                        raise ImportFailure('来源下载超时')
                    chunk=response.read(min(65536,max_bytes+1-received))
                    if not chunk:
                        break
                    received+=len(chunk)
                    if received>max_bytes:
                        raise ImportFailure('来源文件超过大小限制')
                    if decoder:
                        chunk=decoder.decompress(chunk,max_bytes+1-size)
                    chunks.append(chunk)
                    size+=len(chunk)
                    if size>max_bytes:
                        raise ImportFailure('来源文件超过大小限制')
                if decoder and not decoder.eof:
                    raise ImportFailure('来源返回了不完整的压缩内容')
                return b''.join(chunks),response.getheader('Content-Type',''),url
            finally:
                conn.close()
        except ImportFailure:
            raise
        except (ValueError,OSError,http.client.HTTPException,UnicodeError,zlib.error) as exc:
            raise ImportFailure('来源连接失败或证书异常，请稍后重试') from exc
    raise ImportFailure('来源跳转次数过多')

class _NoHttpErrorProcessor(urllib.request.HTTPErrorProcessor):
    """Return every HTTP response so redirects and errors stay under our control."""
    def http_response(self,request,response):
        return response
    https_response=http_response

def fetch_via_proxy(url,proxy,max_bytes=3*1024*1024,deadline=None):
    """经本机 HTTP 代理（DB-002 的 127.0.0.1:8118）抓取公开网页。

    与直连路径一致：每个跳（含每次重定向后的新目标）都先调用
    _validate_target 做 SSRF 校验，再发起请求；代理不可用时按可重试失败上报。
    """
    # urllib 默认会自动跟随重定向，并在 4xx/5xx 时先抛 HTTPError。两种行为都会
    # 绕过下面的逐跳校验或丢失明确错误，因此让所有状态码原样返回后自行处理。
    opener=urllib.request.build_opener(
        urllib.request.ProxyHandler({'http':proxy,'https':proxy}),
        _NoHttpErrorProcessor())
    final_url=url
    for redirect in range(5):
        if deadline and time.monotonic()>deadline:
            raise ImportFailure('导入超过时间限制，请手动补充')
        _validate_target(final_url)  # DNS 解析结果 + 每个重定向目标均拒绝非全局地址
        req=urllib.request.Request(final_url,headers={
            'User-Agent':'TogetherReader/0.2 (private article reader)',
            'Accept':'text/html,image/*;q=0.8','Accept-Encoding':'gzip, deflate'})
        try:
            with opener.open(req,timeout=15) as response:
                if response.status in (301,302,303,307,308):
                    location=response.getheader('Location')
                    if not location:
                        raise ImportFailure('来源网站返回了无效跳转')
                    final_url=urljoin(final_url,location)
                    continue
                if response.status!=200:
                    raise ImportFailure(f'来源网站返回 HTTP {response.status}；可手动补充文字或截图')
                encoding=response.headers.get('Content-Encoding','identity').lower().strip()
                if encoding not in ('identity','gzip','deflate'):
                    raise ImportFailure('来源返回了暂不支持的压缩编码')
                raw=response.read(max_bytes+1)
                if len(raw)>max_bytes:
                    raise ImportFailure('来源文件超过大小限制')
                if encoding=='gzip':
                    try:
                        body=zlib.decompress(raw,16+zlib.MAX_WBITS)
                    except zlib.error:
                        raise ImportFailure('来源返回了不完整的压缩内容')
                elif encoding=='deflate':
                    try:
                        body=zlib.decompress(raw,zlib.MAX_WBITS)
                    except zlib.error:
                        raise ImportFailure('来源返回了不完整的压缩内容')
                else:
                    body=raw
                if len(body)>max_bytes:
                    raise ImportFailure('来源文件超过大小限制')
                return body,response.headers.get('Content-Type',''),final_url
        except ImportFailure:
            raise
        except (ValueError,OSError,urllib.error.URLError,http.client.HTTPException,zlib.error,TimeoutError) as exc:
            # 代理不可用属于基础设施瞬态问题，按可重试失败上报（避免任务无限停在 fetching）
            raise ImportFailure('来源连接失败或代理不可用，请稍后重试',True) from exc
    raise ImportFailure('来源跳转次数过多')

class ArticleParser(HTMLParser):
    VOID={'img','meta','br','hr','input','link','source','wbr','area','base','embed','param','track','col'}
    SKIP={'script','style','nav','header','footer','aside','form','button','noscript','svg','iframe','template'}
    BLOCK={'p','h1','h2','h3','h4','li','blockquote','pre'}
    def __init__(self,base):
        super().__init__(convert_charrefs=True)
        self.base=base
        self.stack=[]
        self.blocks=[]
        self.buffer=[]
        self.current=None
        self.current_main=False
        self.title=''
        self.og_title=''
        self.has_video=False
        self.metadata={}
        self.jsonld=[]
        self.json_buffer=None
        self.json_length=0
        self.picture_src=''
    def image_source(self,attrs):
        for key in ('data-src','data-original','data-lazy-src'):
            if attrs.get(key) and not attrs[key].startswith('data:'):
                return attrs[key]
        srcset=attrs.get('data-srcset') or attrs.get('srcset','')
        options=[]
        for part in srcset.split(',')[:30]:
            bits=part.strip().split()
            if bits and not bits[0].startswith('data:'):
                try:
                    score=float(bits[-1].rstrip('wx')) if len(bits)>1 else 1
                except ValueError:
                    score=1
                options.append((score,bits[0]))
        if options:
            return max(options)[1]
        return attrs.get('src','')
    def hidden(self):
        return any(t in self.SKIP for t in self.stack)
    def in_main(self):
        return any(t in ('article','main') for t in self.stack)
    def flush(self):
        content=re.sub(r'\s+',' ',''.join(self.buffer)).strip()
        if content and self.current:
            kind='heading' if self.current.startswith('h') else ('quote' if self.current=='blockquote' else 'paragraph')
            self.blocks.append((self.current_main,{'type':kind,'text':content[:12000]}))
        self.buffer=[]
        self.current=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='script' and attrs.get('type','').lower()=='application/ld+json':
            self.json_buffer=[]
            self.json_length=0
        if tag=='meta':
            key=(attrs.get('property') or attrs.get('name','')).lower()
            field={'author':'author','article:author':'author','article:published_time':'published_at','datepublished':'published_at','og:site_name':'site_name','og:video':'video_url','og:video:url':'video_url','og:video:secure_url':'video_url'}.get(key)
            if field:
                self.metadata[field]=attrs.get('content','')[:300]
        if tag=='meta' and attrs.get('property')=='og:title':
            self.og_title=attrs.get('content','')
        if tag=='video':
            self.has_video=True
            src=attrs.get('src','')
            if src and not self.metadata.get('video_url'):
                self.metadata['video_url']=urljoin(self.base,src)[:2000]
        elif tag=='source' and 'video' in self.stack and not self.metadata.get('video_url'):
            vsrc=attrs.get('src','')
            if vsrc and not vsrc.startswith('data:'):
                self.metadata['video_url']=urljoin(self.base,vsrc)[:2000]
        elif tag=='iframe':
            self.has_video=True
        if tag in self.BLOCK:
            self.flush()
            if not self.hidden():
                self.current=tag
                self.current_main=self.in_main()
        if tag=='br' and self.current:
            self.buffer.append('\n')
        if tag=='picture':
            self.picture_src=''
        if tag=='source' and 'picture' in self.stack and not self.picture_src:
            self.picture_src=self.image_source(attrs)
        if tag=='img' and not self.hidden():
            continuation,inside=self.current,self.current_main
            self.flush()
            src=self.image_source(attrs)
            if not src or src.startswith('data:'):
                src=self.picture_src
            if src and not src.startswith('data:'):
                self.blocks.append((self.in_main(),{'type':'image','url':urljoin(self.base,src),'alt':attrs.get('alt','')[:300]}))
            self.current,self.current_main=continuation,inside
        if tag not in self.VOID:
            self.stack.append(tag)
    def handle_endtag(self,tag):
        if tag=='script' and self.json_buffer is not None:
            if self.json_length<=256000 and len(self.jsonld)<10:
                try:
                    self.jsonld.append(json.loads(''.join(self.json_buffer)))
                except (ValueError,RecursionError):
                    pass
            self.json_buffer=None
        if tag in self.BLOCK:
            self.flush()
        if tag in self.stack:
            index=len(self.stack)-1-self.stack[::-1].index(tag)
            self.stack=self.stack[:index]
    def handle_data(self,value):
        if self.json_buffer is not None:
            self.json_length+=len(value)
            if self.json_length<=256000:
                self.json_buffer.append(value)
        if 'title' in self.stack:
            self.title+=value
        if self.current and not self.hidden():
            self.buffer.append(value)
    def result(self):
        self.flush()
        main=[b for inside,b in self.blocks if inside]
        selected=main if any(b['type']=='paragraph' for b in main) else [b for _,b in self.blocks]
        # Remove repeated menu-like items; preserve original order.
        result=[]
        seen=set()
        for b in selected:
            key=b.get('text',b.get('url'))
            if key and key not in seen:
                result.append(b)
                seen.add(key)
        title=re.sub(r'\s+',' ',self.og_title or self.title).strip()[:300]
        return title or urlsplit(self.base).hostname,result[:400]

    def structured_article(self):
        pending=list(self.jsonld)
        for _ in range(300):
            if not pending:
                break
            item=pending.pop()
            if isinstance(item,list):
                pending.extend(item[:100])
            elif isinstance(item,dict):
                kinds=item.get('@type',[])
                kinds=[kinds] if isinstance(kinds,str) else kinds
                if isinstance(kinds,list) and any(k in ('Article','NewsArticle','BlogPosting','SocialMediaPosting') for k in kinds if isinstance(k,str)):
                    return item
                graph=item.get('@graph')
                if isinstance(graph,(dict,list)):
                    pending.append(graph)
        return {}

def extract(raw,ctype,url):
    if 'html' not in ctype.lower():
        raise ImportFailure('该链接不是 HTML 图文页面，请手动添加内容')
    charset=re.search(r'charset\s*=\s*["\']?([\w-]+)',ctype,re.I)
    if not charset:
        charset=re.search(r'charset\s*=\s*["\']?([\w-]+)',raw[:4096].decode('ascii','ignore'),re.I)
    encoding=charset.group(1) if charset else 'utf-8'
    try:
        html=raw.decode(encoding,errors='replace')
    except LookupError:
        html=raw.decode('utf-8',errors='replace')
    parser=ArticleParser(url)
    parser.feed(html)
    title,blocks=parser.result()
    structured=parser.structured_article()
    author=structured.get('author','')
    if isinstance(author,list):
        author=author[0] if author else ''
    if isinstance(author,dict):
        author=author.get('name','')
    for key,value in [('author',author),('published_at',structured.get('datePublished',''))]:
        if isinstance(value,str) and value:
            parser.metadata.setdefault(key,value[:300])
    length=sum(len(b.get('text','')) for b in blocks if b['type']=='paragraph')
    structured_used=False
    body=structured.get('articleBody','')
    if length<80 and isinstance(body,str) and len(body.strip())>=80:
        blocks=[{'type':'paragraph','text':line.strip()[:12000]} for line in body[:100000].splitlines() if line.strip()][:350]
        if isinstance(structured.get('headline'),str):
            title=structured['headline'][:300]
        photos=structured.get('image',[])
        if isinstance(photos,(str,dict)):
            photos=[photos]
        if isinstance(photos,list):
            for photo in photos[:10]:
                value=photo.get('url','') if isinstance(photo,dict) else photo
                if isinstance(value,str) and value:
                    blocks.append({'type':'image','url':urljoin(url,value),'alt':''})
        length=sum(len(b.get('text','')) for b in blocks if b['type']=='paragraph')
        structured_used=True
    if length<80:
        raise ImportFailure('未提取到足够正文；登录页、动态帖子和反爬页面请手动补充')
    warnings=[]
    return {'title':title,'blocks':blocks,'warnings':warnings,'metadata':parser.metadata}

class Worker:
    def __init__(self,server,token):
        p=urlsplit(server)
        insecure_test=os.environ.get('ALLOW_INSECURE_HTTP','0')=='1'
        try:
            private_ip=ipaddress.ip_address(p.hostname or '').is_private
        except ValueError:
            private_ip=False
        allowed_http=p.scheme=='http' and (
            p.hostname in ('127.0.0.1','localhost') or (insecure_test and private_ip))
        if p.scheme!='https' and not allowed_http:
            raise ValueError('SERVER_URL must use HTTPS; HTTP is limited to loopback or an explicitly enabled private IP')
        if p.username or p.password or p.query or p.fragment or p.path not in ('','/'):
            raise ValueError('SERVER_URL must be an origin, such as https://app.example.com')
        if len(token)<32:
            raise ValueError('WORKER_TOKEN must contain at least 32 characters')
        self.server,self.token=server.rstrip('/'),token
        # Server credentials must never follow a redirect to another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):
                return None
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def api(self,path,data):
        req=urllib.request.Request(self.server+path,data=dumps(data),headers={'Content-Type':'application/json','Authorization':'Bearer '+self.token},method='POST')
        with self.opener.open(req,timeout=45) as resp:
            return json.load(resp)
    def upload_video(self,job,path,deadline):
        upload=secrets.token_hex(16)
        size=path.stat().st_size
        if size>MAX_SOCIAL_BYTES:
            raise ImportFailure('视频超过 200 MB，已保留标题和封面')
        offset=0
        result=None
        with path.open('rb') as stream:
            while offset<size:
                if time.monotonic()>deadline:
                    raise ImportFailure('导入超过时间限制，请手动补充',True)
                raw=stream.read(3*1024*1024)
                final=offset+len(raw)==size
                result=self.api('/internal/jobs/video-chunk',{**job,'upload':upload,'offset':offset,'data':base64.b64encode(raw).decode(),'final':final})
                offset+=len(raw)
        if not result or not result.get('asset'):
            raise ImportFailure('视频上传没有完成',True)
        return result['asset']

    def process_social(self,job,deadline):
        platform=social_platform(job['url'])
        with tempfile.TemporaryDirectory(prefix='together-social-') as temp:
            info,video,thumb=download_social(job['url'],Path(temp),deadline)
            self.api('/internal/jobs/progress',{**job,'phase':'media'})
            poster=''
            if thumb:
                try:
                    raw=thumb.read_bytes()
                    poster=self.api('/internal/jobs/media',{**job,'data':base64.b64encode(raw).decode()})['asset']
                except Exception:
                    poster=''
            blocks=[]
            description=str(info.get('description') or info.get('fulltitle') or '').strip()
            if description:
                blocks.append({'type':'paragraph','text':description[:12000]})
            if video:
                video=optimize_video_for_streaming(video,Path(temp),deadline)
                asset=self.upload_video(job,video,deadline)
                blocks.append({'type':'video','asset':asset,'poster':poster})
            elif poster:
                blocks.append({'type':'image','asset':poster,'alt':'来源封面'})
            if not blocks:
                raise ImportFailure('平台没有返回可展示的文字、图片或视频',True)
            timestamp=info.get('timestamp')
            published=''
            if isinstance(timestamp,(int,float)):
                published=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(timestamp))
            canonical=redact_url(str(info.get('webpage_url') or info.get('original_url') or job['url']))[:2000]
            platform_id=platform_for_url(canonical)
            metadata={'author':str(info.get('uploader') or info.get('channel') or '')[:300],
                'published_at':published,'site_name':platform,'platform':platform_id,
                'content_id':str(info.get('id') or content_id_for_url(canonical,platform_id))[:300],
                'canonical_url':canonical,'content_type':'video' if video else 'image'}
            title=str(info.get('title') or description or platform+' 分享').strip()[:300]
            warnings=[]
            playlist_count=info.get('playlist_count')
            if info.get('_type')=='playlist' or (isinstance(playlist_count,(int,float)) and playlist_count>1):
                warnings.append('该来源包含多项内容，本次只导入第一项。')
            self.api('/internal/jobs/progress',{**job,'phase':'finishing'})
            self.api('/internal/jobs/complete',{**job,'title':title,'blocks':blocks,'warnings':warnings,'metadata':metadata})
    def process(self,job):
        job={**job,'url':clean_trailing(job.get('url',''))}
        deadline=time.monotonic()+600
        if social_platform(job['url']):
            return self.process_social(job,deadline)
        raw,ctype,final_url=fetch_public(job['url'],deadline=deadline,proxy=proxy_for_web(job['url']))
        content=extract(raw,ctype,final_url)
        self.api('/internal/jobs/progress',{**job,'phase':'images'})
        blocks=[]
        image_count=0
        missing=0
        for b in content['blocks']:
            if b['type']!='image':
                blocks.append(b)
                continue
            if image_count>=10 or time.monotonic()>deadline:
                missing+=1
                continue
            image_count+=1
            try:
                raw,mime,_=fetch_public(b['url'],5*1024*1024,deadline,proxy=proxy_for_web(b['url']))
                if not mime.lower().startswith('image/'):
                    raise ImportFailure('来源不是图片')
                asset=self.api('/internal/jobs/media',{**job,'data':base64.b64encode(raw).decode()})
                blocks.append({'type':'image','asset':asset['asset'],'alt':b['alt']})
            except urllib.error.HTTPError as exc:
                if exc.code in (401,409):
                    raise
                missing+=1
            except (ImportFailure,OSError,urllib.error.URLError):
                missing+=1
        # missing image count is ignored to avoid noisy warning banner
        video_url=content['metadata'].get('video_url','')
        video_asset=''
        if video_url and time.monotonic()<deadline-30:
            with tempfile.TemporaryDirectory(prefix='together-webvideo-') as vtemp:
                try:
                    raw,mime,_=fetch_public(video_url,MAX_SOCIAL_BYTES,deadline,proxy=proxy_for_web(video_url))
                    if len(raw)<50*1024:
                        raise ImportFailure('下载内容不是有效视频')
                    vpath=Path(vtemp)/'web-video.bin'
                    vpath.write_bytes(raw)
                    if not probe_video(vpath,deadline):
                        raise ImportFailure('下载内容不是可播放视频')
                    video=optimize_video_for_streaming(vpath,Path(vtemp),deadline)
                    video_asset=self.upload_video(job,video,deadline)
                except Exception as exc:
                    content['warnings'].append(f'原页视频未导入（{str(exc)[:60]}），已保留图文内容。')
        if video_asset:
            blocks.append({'type':'video','asset':video_asset,'poster':''})
            content['metadata']['content_type']='video'
            content['warnings']=[w for w in content['warnings'] if '本次未导入播放器' not in w]
            content['warnings'].append('已导入原页视频。')
        self.api('/internal/jobs/progress',{**job,'phase':'finishing'})
        self.api('/internal/jobs/complete',{**job,'title':content['title'],'blocks':blocks,'warnings':content['warnings'],'metadata':content['metadata']})
    def once(self):
        job=self.api('/internal/jobs/claim',{})['job']
        if not job:
            return False
        try:
            self.process(job)
            print('Import completed:',job['id'],flush=True)
        except ImportFailure as exc:
            self.api('/internal/jobs/fail',{**job,'error':str(exc),'retryable':exc.retryable})
        except Exception:
            # Lease expiry recovers the task if result/failure transmission also fails.
            try:
                self.api('/internal/jobs/fail',{**job,'error':'采集或传输暂时失败，将自动重试','retryable':True})
            except Exception:
                pass
        return True

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--env',default=str(ROOT/'.env'))
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    read_env(args.env)
    cleanup_stale_temp()
    signal.signal(signal.SIGTERM,_on_terminate)
    signal.signal(signal.SIGINT,_on_terminate)
    worker=Worker(os.environ.get('SERVER_URL','http://127.0.0.1:8765'),os.environ.get('WORKER_TOKEN',''))
    heartbeat_stop=threading.Event()
    def heartbeat():
        # Separate client avoids sharing urllib handlers between threads.
        client=Worker(worker.server,worker.token)
        while not heartbeat_stop.is_set():
            try:
                client.api('/internal/heartbeat',{})
            except Exception:
                pass
            heartbeat_stop.wait(15)
    if not args.once:
        threading.Thread(target=heartbeat,daemon=True).start()
    print('Importer is ready.',flush=True)
    while True:
        try:
            worked=worker.once()
        except Exception:
            worked=False
            print('Waiting for domestic service; will retry.',flush=True)
        if args.once:
            break
        if not worked:
            time.sleep(4)

if __name__=='__main__':
    main()
