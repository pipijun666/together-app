"""YouTube 适配器：解析单个公开视频（watch/shorts/youtu.be）。

使用 yt-dlp（worker 既有依赖）作为下载引擎。新版 YouTube 提取需要
外部 JS 运行时（deno 默认、或 node）与 EJS 挑战组件，否则高清
H.264 格式可能缺失：

- 优先 H.264/AAC 的 mp4_dash/m4a 直链并合并为 MP4；运行时缺失时自动
  降级（仅缺失部分高清格式）。
- --no-playlist/--playlist-items 1 保证带 list 参数也只下载单视频，
  不遍历播放列表/频道；纯列表链接直接拒绝。
- 下载前先用 --skip-download 探针读取 is_live/live_status/availability，
  正在直播/未开始首播/私有/会员专属/年龄限制等返回明确错误，不绕过
  限制、不索取个人 Cookie。
- 媒体只经 YouTube 专属受控代理（YOUTUBE_PROXY，回环固定地址）下载；
  不读取全局 YTDLP_PROXY，避免污染 worker 其他平台的出口选择。输出
  本地临时文件，由 worker 统一做公共转码优化与上传，不让手机直接
  依赖海外媒体 URL。

返回与 yt-dlp/worker 兼容的 (info, video_path, thumb_path)。
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MAX_BYTES = 200 * 1024 * 1024          # 单个最终视频的硬上限
MIN_FREE_DISK = 2 * 1024 * 1024 * 1024  # 对齐项目“根盘低于 2GiB 拒绝新媒体”
# 下载工作区总预算：音频流+视频流+合并输出+info/缩略图之和。
# yt-dlp 合并后会自动删除分轨文件，这里作为下载中途与收尾后的双保险。
WORKING_BUDGET = 2 * MAX_BYTES
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class YouTubeError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def check_disk_space(path):
    usage = shutil.disk_usage(str(path))
    if usage.free < MIN_FREE_DISK:
        raise YouTubeError("服务器存储空间不足，任务已取消", False)


def proxy_url():
    """YouTube 专属出口。

    只读取 YOUTUBE_PROXY（本平台专属），回退到共享 PROXY_URL（回环受控
    代理）；刻意不读 worker 全局的 YTDLP_PROXY，避免改走其它平台的出口。
    返回值应为回环固定地址（127.0.0.1），不接受用户输入。
    """
    return (os.environ.get("YOUTUBE_PROXY") or os.environ.get("PROXY_URL") or "").strip()


def _js_runtime_args():
    """探测可用的 JS 运行时（yt-dlp 解决 YouTube 签名/n 挑战需要）。

    yt-dlp 支持的 runtime 名为 deno / node / bun / quickjs。注意：
    不支持 "nodejs" 这个名字；EJS 挑战需要 node >= 16。两者都缺失时
    返回空（yt-dlp 会警告并缺失部分高清格式，但仍可尝试较低清晰度
    H.264 直链）。
    """
    for name in ("deno", "node"):
        path = shutil.which(name)
        if path:
            return ["--js-runtimes", "%s:%s" % (name, path)]
    return []


def resolve_video(url):
    """校验 YouTube 链接，返回 (canonical_url, video_id)。

    支持：youtube.com/watch?v=、m.youtube.com/watch?v=、
    youtube.com/shorts/<id>、youtu.be/<id>。仅接受明确的 YouTube
    域名（youtube.com 及 *.youtube.com、youtu.be），拒绝
    youtube.com.evil 等误识别与私网/本地伪造域名；纯播放列表链接
    明确拒绝。
    """
    if not isinstance(url, str) or not url.strip():
        raise YouTubeError("没有提供 YouTube 链接")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    # 私网/回环/本地伪造一律拒绝（SSRF 边界）
    if (
        host in ("", "localhost", "ip6-localhost")
        or host.startswith("127.") or host.startswith("10.")
        or host.startswith("192.168.")
        or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host)
        or host.endswith(".local") or host.endswith(".internal")
    ):
        raise YouTubeError("不是有效的 YouTube 链接", False)
    if host == "youtu.be":
        parts = [p for p in parsed.path.split("/") if p]
        if parts and _VIDEO_ID.match(parts[0]):
            return "https://youtu.be/" + parts[0], parts[0]
        raise YouTubeError("不是有效的 YouTube 短链接", False)
    if host == "youtube.com" or host.endswith(".youtube.com"):
        parts = [p for p in parsed.path.split("/") if p]
        values = parse_qs(parsed.query).get("v", [])
        video_id = next((v for v in values if _VIDEO_ID.match(v)), "")
        if video_id:
            return "https://www.youtube.com/watch?v=" + video_id, video_id
        if len(parts) >= 2 and parts[0] == "shorts" and _VIDEO_ID.match(parts[1]):
            return "https://www.youtube.com/watch?v=" + parts[1], parts[1]
        if parts and parts[0] == "playlist":
            raise YouTubeError("暂不支持播放列表，请分享单个视频链接", False)
        raise YouTubeError("无法从链接中识别 YouTube 视频 ID", False)
    raise YouTubeError("不是有效的 YouTube 链接", False)


def _classify_error(output):
    """把 yt-dlp 输出映射为可行动的失败信息（不绕过限制）。"""
    text = (output or "").replace("’", "'").replace("‘", "'")
    if "Sign in to confirm you're not a bot" in text or "Sign in to confirm your age" in text:
        return "平台暂时限制匿名读取，系统会自动重试", True
    if "Video unavailable" in text or "This video is unavailable" in text:
        return "该视频不可用（可能已删除、私有或仅限地区），请确认后重试", False
    if "Private video" in text:
        return "该视频为私密视频，无法读取", False
    if "This video is available to members only" in text or "Members-only" in text:
        return "该视频为会员专属，无法读取", False
    if "age-restricted" in text:
        return "该视频有年龄限制，无法匿名读取", False
    if "Premieres in" in text or "This live event will begin in" in text or "not started" in text:
        return "该视频为直播或未开始首播，暂时无法导入", True
    if "This live stream recording is not available" in text or "isn't available" in text:
        return "该视频（含直播回放）当前不可用", False
    if "is not available" in text:
        return "该视频当前不可用", False
    if "has been removed" in text or "removed for violating" in text:
        return "该视频已被移除", False
    if "ERROR: Unsupported URL" in text:
        return "暂不支持的 YouTube 链接", False
    if "HTTP Error" in text or "Temporary failure" in text or "timed out" in text or "Timeout" in text:
        return "YouTube 暂时无响应，系统会自动重试", True
    return "YouTube 未返回可导入的公开视频，系统会自动重试", True


def _deadline_left(deadline):
    """距 deadline 剩余秒数；到期立即抛错，不再 max(1,...) 强行启动进程。"""
    left = deadline - time.monotonic()
    if left <= 0:
        raise YouTubeError("任务已超过时间预算，请稍后重试", True)
    return left


def _kill_process_tree(proc):
    """跨平台杀进程树。

    Unix：start_new_session 已让子进程独立成组，用 killpg SIGKILL 整组杀掉
    （连带 ffmpeg 合并子进程）。Windows：os.killpg 不存在，用 taskkill /T /F
    按 PID 递归杀子进程树。两者都兜底 proc.kill()。
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _run_yt_dlp(command, cwd, deadline, watch_dir=None):
    """运行 yt-dlp，期间轮询 deadline / 磁盘 / 目录预算，超限杀进程树。

    旧版 communicate(timeout=...) 一次阻塞，资源检查只在返回后执行；
    新版把 stdout/stderr 重定向到临时日志文件，用 wait(timeout=1) 循环，
    每轮检查时间预算、根盘剩余、工作目录总占用，任一超限立即杀进程树。

    返回 (returncode, stdout, stderr)。
    """
    timeout = min(int(_deadline_left(deadline)), 600)
    start = time.monotonic()
    watch_path = Path(watch_dir) if watch_dir else None

    log_fd, log_path = tempfile.mkstemp(
        prefix="ytdlp_", suffix=".log", dir=str(cwd),
    )
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            start_new_session=(sys.platform != "win32"),
        )
        os.close(log_fd)
        log_fd = -1

        while True:
            try:
                proc.wait(timeout=1)
                break  # 进程自然退出
            except subprocess.TimeoutExpired:
                pass

            now = time.monotonic()
            elapsed = now - start
            if elapsed >= timeout:
                _kill_process_tree(proc)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                raise YouTubeError("YouTube 下载超过时间限制，请稍后重试", True)
            if now >= deadline:
                _kill_process_tree(proc)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                raise YouTubeError("任务已超过时间预算，请稍后重试", True)

            # 运行中资源监控：磁盘剩余 + 工作目录总占用
            if watch_path and watch_path.is_dir():
                try:
                    if shutil.disk_usage(str(watch_path)).free < MIN_FREE_DISK:
                        _kill_process_tree(proc)
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            pass
                        raise YouTubeError("服务器存储空间不足，任务已取消", False)
                    if _dir_total_size(watch_path) > WORKING_BUDGET:
                        _kill_process_tree(proc)
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            pass
                        raise YouTubeError(
                            "下载内容超出预算（音频+视频+合并文件），已取消", False,
                        )
                except YouTubeError:
                    raise
                except OSError:
                    # 统计瞬时失败（文件正被写），下轮再查
                    pass

        try:
            with open(log_path, "r", errors="replace") as f:
                output = f.read()
        except OSError:
            output = ""
        return proc.returncode, output, ""
    finally:
        # 兜底：进程若仍在跑（异常路径），杀之
        if "proc" in dir() and proc.poll() is None:
            _kill_process_tree(proc)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        if log_fd >= 0:
            try:
                os.close(log_fd)
            except OSError:
                pass
        try:
            os.unlink(log_path)
        except OSError:
            pass


def _probe_metadata(canonical, directory, proxy, deadline):
    """下载前轻量探针：读取 is_live / live_status / availability。

    用 --skip-download --dump-single-json，不下载媒体；用于在消耗带宽前
    拒绝正在直播/未开始首播/私有/会员/年龄限制等不可导入内容。
    """
    command = [
        sys.executable, "-m", "yt_dlp", "--no-config", "--no-playlist",
        "--skip-download", "--no-warnings", "--dump-single-json",
    ]
    command += _js_runtime_args()
    if proxy:
        command += ["--proxy", proxy]
    command += [canonical]
    rc, out, err = _run_yt_dlp(command, directory, deadline, watch_dir=directory)
    if rc != 0:
        message, retryable = _classify_error((err or "") + "\n" + (out or ""))
        raise YouTubeError(message, retryable)
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        raise YouTubeError("YouTube 返回的信息无法读取", True)


def _reject_if_not_importable(info):
    """根据探针元数据拒绝不可导入内容（直播/首播/受限）。"""
    availability = str(info.get("availability") or "").lower()
    live_status = str(info.get("live_status") or "").lower()
    if info.get("is_live") or live_status == "is_live":
        raise YouTubeError("该视频正在直播，暂时无法导入，请直播结束后再分享", True)
    if live_status == "is_upcoming":
        raise YouTubeError("该视频为未开始首播，暂时无法导入", True)
    if availability == "private" or availability in ("needs_auth", "subscriber_only", "premium", "needs_premium", "members_only"):
        raise YouTubeError("该视频为私密/会员/付费内容，无法匿名读取", False)
    age_limit = int(info.get("age_limit") or 0)
    if availability == "age_restricted" or age_limit >= 18:
        raise YouTubeError("该视频有年龄限制，无法匿名读取", False)


def _dir_total_size(directory):
    return sum(p.stat().st_size for p in Path(directory).iterdir() if p.is_file())


_FRAG = re.compile(r"^\.f\d+\.")


def _cleanup_intermediate(directory):
    """删除 yt-dlp 的分轨/分片/临时文件，只保留最终 mp4/info/缩略图。"""
    for p in Path(directory).iterdir():
        if not p.is_file():
            continue
        name = p.name
        stem, ext = (name.split(".", 1) + [""])[:2] if "." in name else (name, "")
        if name.endswith(".part") or name.endswith(".ytdl") or _FRAG.match("." + ext):
            p.unlink(missing_ok=True)


def download_social(url, directory, deadline):
    canonical, video_id = resolve_video(url)
    check_disk_space(directory)
    _deadline_left(deadline)  # 到期即拒，不启动任何子进程
    proxy = proxy_url()
    # SSRF 防护：强制所有 yt-dlp 流量（含重定向/CDN/EJS）走专属代理，
    # 不允许适配器直连公网或私网。未配置代理时直接失败，不静默降级。
    if not proxy:
        raise YouTubeError("YouTube 代理未配置，拒绝直连", False)

    # 1) 下载前探针：拒绝直播/首播/受限，避免浪费带宽
    meta = _probe_metadata(canonical, directory, proxy, deadline)
    _reject_if_not_importable(meta)

    # 2) 正式下载
    output = str(directory / "%(id)s.%(ext)s")
    command = [
        sys.executable, "-m", "yt_dlp", "--no-config",
        "--no-playlist", "--playlist-items", "1",
        "--socket-timeout", "30", "--retries", "2", "--fragment-retries", "2",
        "--max-filesize", "200M",
        "--format", "bv*[vcodec^=avc1][height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
        "--merge-output-format", "mp4", "--remux-video", "mp4",
        "--write-info-json", "--write-thumbnail",
        "--remote-components", "ejs:github",
        "--output", output,
    ]
    command += _js_runtime_args()
    if proxy:
        command += ["--proxy", proxy]
    command += [canonical]

    rc, out, err = _run_yt_dlp(command, directory, deadline, watch_dir=directory)
    if rc != 0:
        message, retryable = _classify_error((err or "") + "\n" + (out or ""))
        raise YouTubeError(message, retryable)

    # 3) 磁盘/总量双保险（运行中已轮询，这里是收尾确认）
    check_disk_space(directory)
    total = _dir_total_size(directory)
    if total > WORKING_BUDGET:
        _cleanup_intermediate(directory)
        # 清理后若仍超预算，明确失败
        if _dir_total_size(directory) > WORKING_BUDGET:
            raise YouTubeError("下载内容超出预算（音频+视频+合并文件），已取消", False)

    # 4) 选取最终文件：必须是可播放 mp4；非 mp4 不自行二次转码（交回 worker 公共优化）
    info_files = list(Path(directory).glob("*.info.json"))
    if not info_files:
        raise YouTubeError("YouTube 没有返回可用的公开内容", True)
    try:
        raw = json.loads(info_files[0].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise YouTubeError("YouTube 返回的信息无法读取", True)

    videos = [p for p in Path(directory).iterdir()
              if p.is_file() and p.suffix.lower() in (".mp4", ".m4v")]
    video = max(videos, key=lambda p: p.stat().st_size) if videos else None
    if video is None:
        # 无视频流（可能仅字幕/纯音频或下载失败）：不返回"成功但无视频"的元数据
        raise YouTubeError("未取得可播放的视频文件", True)
    if video.stat().st_size > MAX_BYTES:
        video.unlink(missing_ok=True)
        raise YouTubeError("视频超过 200MB 上限，已取消", False)

    thumbs = [p for p in Path(directory).iterdir()
              if p.is_file() and p.suffix.lower() in (".webp", ".jpg", ".jpeg", ".png")]
    thumb = max(thumbs, key=lambda p: p.stat().st_size) if thumbs else None

    info = {
        "id": str(raw.get("id") or video_id)[:300],
        "title": str(raw.get("title") or "YouTube 分享").strip()[:300],
        "description": str(raw.get("description") or "").strip(),
        "uploader": str(raw.get("uploader") or raw.get("channel") or "").strip()[:300],
        "timestamp": raw.get("timestamp"),
        "webpage_url": str(raw.get("webpage_url") or canonical),
        "thumbnail": str(raw.get("thumbnail") or ""),
        "duration": raw.get("duration"),
    }
    return info, video, thumb
