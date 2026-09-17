# YT-001 YouTube 适配器接入说明（供 Codex INT-004 集成 / 验收）

状态：豆包已完成适配器研发、P1 返修、生产部署与端到端真实链路验证
（2026-09-15）。本文件反映**当前生产实际状态**，覆盖早期"未改 worker"的
旧表述。worker.py 的 youtube 分派已由用户授权在生产 patch（见第 3 节）；
公共 worker.py 的最终代码归属与合并仍由 Codex 决定。

## 1. 文件清单与哈希（当前生产 + 本地一致）

| 文件 | 状态 | SHA-256（前 16） |
|---|---|---|
| `adapters/youtube/adapter.py` | P1 返修后当前版 | `1226ffbeb415ea58…` |
| `adapters/youtube/__init__.py` | 空文件 | `e3b0c44298fc1c14…` |
| `link_input.py` | youtube 识别（未变） | `5c935cbc81367c03…` |
| `tests/test_youtube_adapter.py` | P1 返修后 63 项离线 | — |
| `tests/test_youtube_real.py` | 真实样本（YT_REAL_SOURCE=1 才跑） | — |
| `tests/test_link_input.py` | youtube 用例 | — |

## 2. link_input.py 变更（未变，无需 Codex 改动）

- `platform_for_url`：`youtube.com` / `*.youtube.com` / `youtu.be` → `"youtube"`；
  `youtube.com.evil.com`、`notyoutube.com` 归 `web`（已测）。
- `content_id_for_url`：`watch?v=`、`shorts/<id>`、`youtu.be/<id>`，
  ID 严格 `[A-Za-z0-9_-]{11}`；纯播放列表返回空。

## 3. worker.py 接入（已在生产 patch，待 Codex 本地合并）

生产 `/srv/together/app/worker.py` 当前（脱敏）：

- L244 `social_platform()`：`{...,'youtube':'YouTube'}`
- L318-327 `download_social()` 在 tiktok 分派后插入：
  ```python
  if platform_for_url(url)=='youtube':
      try:
          from adapters.youtube.adapter import download_social as youtube_download
          from adapters.youtube.adapter import YouTubeError
      except ImportError:
          pass
      else:
          try:
              return youtube_download(url,directory,deadline)
          except YouTubeError as exc:
              raise ImportFailure(str(exc),exc.retryable) from exc
  ```
- L246-248 `proxy_url()` 仍为 `os.environ.get('YTDLP_PROXY') or os.environ.get('PROXY_URL')`
  （公共代码未改，归 Codex）。**但生产 `.env.worker` 已删除 `YTDLP_PROXY`，
  故 worker.proxy_url() 实际只返回 `PROXY_URL=http://127.0.0.1:8118`**，
  TikTok/X/Telegram/通用 yt-dlp 兜底全部维持 8118，不被 YouTube 出口污染。

## 4. 代理隔离（P1 返修核心）

- 适配器 `proxy_url()` 只读 **`YOUTUBE_PROXY`**（本平台专属），回退到共享
  `PROXY_URL`；**刻意不读 `YTDLP_PROXY`**。离线单测覆盖：
  - 同时设 `YOUTUBE_PROXY=7890`/`PROXY_URL=8118`/`YTDLP_PROXY=evil` → 返回 7890
  - 只设 `YTDLP_PROXY=evil` → 返回空（不读它）
  - 只设 `PROXY_URL=8118` → 返回 8118
- 生产 `.env.worker` 当前（已脱敏）：
  ```
  PROXY_URL=http://127.0.0.1:8118
  YOUTUBE_PROXY=http://127.0.0.1:7890
  ```
  容器 env 实测确认：无 `YTDLP_PROXY`。
- SSRF：`resolve_video()` 只接受 `youtube.com`/`*.youtube.com`/`youtu.be`，
  显式拒绝 127/10/192.168/172.16-31/localhost/*.local/*.internal 等私网与
  本地伪造 host；下载 URL 由 yt-dlp 从 canonical 解析，不接受用户重定向输入。
  走 HTTP CONNECT 代理（127.0.0.1:7890），DNS 在代理端解析。

## 5. Dockerfile.worker 依赖（待 Codex 评估）

- 生产 importer 容器当前**无 JS runtime**（无 node/deno）；本次端到端视频
  （4K Remaster）avc1 直链无需签名求解即下载成功。
- 新发布/签名常变视频仍建议加 `nodejs`（Debian 12 源 node 18.x，已实测可完整
  提取 1080p avc1 137 + 140）。runtime 名必须是 `node` 或 `deno`（不识别
  `nodejs`）。EJS 组件 `--remote-components ejs:github` 仍由运行时经代理下载，
  **无固定版本/离线可重建交付**——固定版本由 Codex 在 Docker 层集成。

## 6. 适配器行为（P1 返修后）

- 下载前轻量探针 `yt-dlp --skip-download --dump-single-json`：读
  `is_live`/`live_status`/`availability`/`age_limit`。正在直播（is_live /
  live_status=is_live）、未开始首播（is_upcoming）、私有/会员/付费/年龄限制
  → 明确错误，不消耗带宽下载。
- 单视频：`--no-playlist --playlist-items 1`，带 list 只下 v 指定的单视频；
  纯 `/playlist` 拒绝。
- 格式：`bv*[vcodec^=avc1][height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]`，
  `--merge-output-format mp4 --remux-video mp4`。
- **预算/清理**：磁盘阈值 `MIN_FREE_DISK=2GiB`（对齐项目"根盘 <2GiB 拒绝新媒体"）；
  工作区总量 `WORKING_BUDGET=2×200MB`，下载后遍历统计并清理 `.part/.ytdl/.fNNN.*`；
  单个最终视频 >200MB 删除并明确失败；deadline 到期立即抛错不再 `max(1,...)` 启动进程。
- **进程树**：`Popen(start_new_session=True)`，超时 `os.killpg(SIGKILL)`，避免
  yt-dlp 派生的 ffmpeg 残留。
- **不重复实现转码**：适配器只输出 mp4；非 mp4 分轨不自行二次 ffmpeg 转码，
  交回 worker 公共 `optimize_video_for_streaming`。
- 错误分类：bot check/age-confirm/HTTP/超时 → retryable；删除/私有/会员/
  age-restricted/直播/首播 → 按类型明确。不添加 Cookie、不绕过限制。

## 7. 测试结果（2026-09-15 P1 返修后）

- 离线 `tests.test_link_input` + `tests.test_youtube_adapter`：**63 项全通过**
  （WSL Ubuntu-22.04 Python 3.10；Windows 兼容性：已移除 `Path('/tmp')`
  硬编码，改用 `tempfile.TemporaryDirectory`，Codex 报告的 1 项 Windows 不兼容
  已修复）。新增覆盖：私网/本地 host 拒绝、`YOUTUBE_PROXY` 隔离不读全局
  `YTDLP_PROXY`、2GiB 磁盘阈值、deadline 到期不启动子进程、直播/首播/私有/
  会员/年龄限制探针拒绝、无 mp4 时抛错（不返回"成功但无视频"元数据）、
  超 200MB 删除后失败、进程组清理。
- 真实端到端（生产环境 importer，2026-09-15 20:39 CST）：
  账号 one 提交 `https://www.youtube.com/watch?v=dQw4w9WgXcQ` →
  **attempts=1，~140s published**；title="Rick Astley - Never Gonna Give
  You Up (Official Video) (4K Remaster)"；video block asset
  `7c4c1b84…` + poster `3572f4fd…`；metadata.platform=youtube、
  content_type=video、warnings=[]。
- Shorts 真实样本：WSL 出口 IP 对 shorts 持续 bot check（按 retryable skip），
  生产 mihomo 出口未单独复测——**标记为"受限/未验收"，不宣称 Shorts 可用**。

## 8. 生产部署证据（2026-09-15 20:39 CST, Asia/Shanghai）

- 备份：`adapters/youtube/adapter.py.bak-yt-p1-<ts>`、`.env.worker.bak-yt-p1-<ts>`
  （旧全局 `YTDLP_PROXY=7890` 时代由 2026-09-15 上午的
  `/root/yt-ytdeploy-bak-20260915-080529` 保留）。
- 生产 adapter.py SHA-256 = 本地 = `1226ffbeb415ea58379e0be904eeb27e9045a841445ed5593db9933f724d6928`。
- 容器运行时：Python 3.12.14、yt-dlp 2026.08.19、ffmpeg 7.1.5、无 node/deno。
- importer 镜像 digest：`sha256:bfb7931dff3247ddc6bf07ec2dba63b1ef1327693e2dc4b883e231d072d40c86`
  （image id `bfb7931dff32`，2.27GB）。
- 两容器 app-app-1 / app-importer-1 均 healthy。

## 9. 未验证 / 待 Codex 联调

- Shorts 在生产 mihomo 出口的真实风控表现。
- Dockerfile 集成 nodejs 后容器内签名常变视频的 EJS 提取。
- EJS 组件固定版本/离线可重建交付。
- 另一账号手机端实际播放/seek（本次只到 published + video asset 生成）。
