# 热点新闻采集与现有解析器集成契约 v1

制定日期：2026-09-14。核对代码基线：`d992677`。
状态：v1采集模块已实现；HS-003 已完成网络地址回退与配置边界代码收口，待 Codex 审核及豆包按统一任务板做服务器隔离复验。部署状态仍以 `PROJECT-HANDOFF.md` 为准。
执行分工更新（用户最新指示，2026-09-14）：HS-001现由通过MCP访问本机的ChatGPT接手，豆包转为APP界面优化，Codex总负责。本说明覆盖下文旧执行者称谓；已有collectors工作区成果保留并盘点后继续，实际进度以统一任务板为准。
唯一进度与分工入口：项目根目录 `PROJECT-HANDOFF.md`。本文件是技术规格，不是第二份交接记录。

## 1. 目标和第一版边界

自动获取配置来源中的热点候选链接，筛选、去重、限流后，通过现有服务端接口提交，由既有 importer 完成正文、图片和视频解析及保存。客户端继续使用现有分享列表和媒体播放器。

流程：来源 RSS/Atom 或公开 JSON 榜单 → Candidate → 校验/排序/去重 → Together API → 既有队列 → 国内 importer/美国代理 → 分享和私有媒体 → 测试报告。

首页落点：第一阶段解析成功的published分享直接进入现有 `/api/shares` 首页列表，note标注自动热点。热点采集是独立服务，但不另建业务分享库或公开API；若后续需要专门热点栏目或筛选字段，由ChatGPT提案、Codex冻结接口并集成。client/Android目前由豆包独占修改，ChatGPT不得同时编辑这些文件。

- 第一版实现 RSS 2.0/Atom 适配器，以及可配置字段路径的公开 JSON 榜单适配器；实际可用来源必须由执行者查证，并记录来源官方地址、格式、采集日期、可访问性和使用条件。不能把未验证或模拟榜单描述成真实成功。
- RSS 的新内容称为“最新资讯”；仅有明确榜单或来源热度依据时称为“热点”。不使用随机数伪造热度。
- 只抓无需登录的公开来源；不实现登录、验证码处理或付费内容获取。
- 采集器只交付链接和来源信息，不自行下载正文媒体，不替换抖音/Telegram/yt-dlp等解析器，不接触内部任务租约。
- 默认 `dry-run`。真实提交需要显式 `--submit`；先接入独立测试实例，通过审核后再由 Codex 部署到生产并启用定时运行。
- 本轮选择普通公开新闻正文页验证；四个平台可作扩展样本。用户已确认抖音/TikTok/X/Telegram初步可解析；单个来源失败仍单独记录，不由此模块修改平台适配器或放宽网络校验。
- 默认单进程，每30分钟执行一次；默认配额为每轮3条、每日10条、最多2个未完成解析任务。三项均允许显式配置，代码硬上限分别为 `per_run_limit=20`、`daily_limit=100`、`max_inflight=10`；超过硬上限、负数或非整数配置必须拒绝启动。`max_inflight` 仅限制未决任务数量，不改变 importer 单 Worker 的解析并发。定时执行由部署调度器负责，脚本本身支持单轮运行。

## 2. 角色、文件边界与交付

| 角色 | 工作 |
|---|---|
| Codex | 冻结接口、审核代码和测试、处理核心服务改动、部署与最终验收；继续抖音诊断 |
| Gemini或豆包，二选一执行 | 实现采集、来源适配、去重、API客户端、测试、脱敏证据；先在统一任务板登记领取 |

实现文件范围：

```text
collectors/hotspots/__init__.py
collectors/hotspots/__main__.py
collectors/hotspots/models.py
collectors/hotspots/sources.py
collectors/hotspots/state.py
collectors/hotspots/client.py
collectors/hotspots/runner.py
config/hotspots.example.json
tests/test_hotspot_*.py
tests/fixtures/hotspots/*
```

执行者可按上述边界合理拆文件。默认使用Python标准库；新增依赖应先在统一交接记录说明理由，由Codex集成。不得直接改动 `server.py`、`worker.py`、`storage.py`、既有适配器、Android、生产Compose、真实env或部署服务。可以只读使用 `link_input.source_reference`，不得改写其行为。真实源的网络校验不能依赖“只是采集脚本”而省略。

若发现必须新增服务端接口，先交付提案或独立补丁供Codex审核；不得把本文件中未实现的能力伪装成现有API。

## 3. 来源配置与 Candidate 契约

配置示例（example.com只是格式占位，不是已验证来源）：

以下 `3/10/2` 是默认值而不是硬上限；显式配置的硬上限为 `20/100/10`，与 `runner.load_config()` 保持一致。生产灰度值属于部署配置，不反向修改本技术契约的默认值。

```json
{
  "schema_version": 1,
  "timezone": "Asia/Shanghai",
  "per_run_limit": 3,
  "daily_limit": 10,
  "max_inflight": 2,
  "freshness_hours": 48,
  "sources": [{
    "id": "news-example",
    "name": "示例资讯",
    "kind": "rss",
    "url": "https://example.com/feed.xml",
    "enabled": false,
    "priority": 50,
    "network": "direct",
    "allowed_article_hosts": ["example.com"]
  }]
}
```

JSON源另有 `mapping`：`items_path`、`url_path`、`title_path`、可选 `id_path`、`published_at_path`、`rank_path`，采用有限点分路径（如 `data.items`），不允许执行表达式或代码。每个适配器输出相同结构：

```json
{
  "schema_version": 1,
  "source_id": "news-example",
  "source_item_id": "optional-stable-id",
  "source_name": "示例资讯",
  "url": "https://example.com/news/123?lang=zh",
  "title_hint": "榜单提供的标题",
  "published_at": "2026-09-14T01:00:00Z",
  "discovered_at": "2026-09-14T02:00:00Z",
  "rank": 1,
  "summary_hint": "可选，来源提供的摘要",
  "region": "可选地区",
  "metric_name": "可选，同源热度指标名",
  "metric_value": 12345,
  "metric_observed_at": "2026-09-16T02:00:00Z",
  "coverage": "可选来源覆盖范围"
}
```

必填：`schema_version/source_id/source_name/url/title_hint/discovered_at`。其余缺失为null。`region/metric_name/metric_value/metric_observed_at/coverage` 为 HS-004 向后兼容可选字段；热度只在同源同指标范围比较。时间统一RFC3339 UTC；原始时区须转换，无法解析的发布时间为null，不以采集时间冒充发布时间。标题最长300字符、摘要1000字符、URL最多4096字符；禁止HTML当作可执行内容。

榜单项必须链接到具体文章/帖子，不能把榜单页、搜索页当作原文。禁止直接抓标题关联的第一个搜索结果替代缺失URL。

排序：来源priority降序；同源有rank时从小到大，无rank按发布时间降序；再以URL稳定排序。每轮轮转各源，避免单源占满；不合并不同平台的原始热度单位。过期候选跳过；无发布时间者按本地首次发现时间控制48小时窗口并在报告标明。

适配器函数：`fetch_candidates(source_config, *, fetcher, now) -> list[Candidate]`。`fetcher`与时钟应可注入，便于离线测试；一个来源失败不阻断其他来源。

### 3.1 HS-004 社交来源扩展

- `telegram_public_channel`：仅所选公开频道 `https://t.me/s/<channel>`；输出稳定消息 permalink、原发布时间与页面 views。其覆盖范围必须写为 selected public channel，不能称 Telegram 全站热榜。
- `x_trends`：官方 WOEID Trends + Recent Search 组合；Bearer Token 只从 `bearer_token_env` 指定环境变量读取，默认 `X_API_BEARER_TOKEN`。`max_trends` 1–5；无凭据时来源失败但不影响其他源。趋势关键词本身不发布，必须解析到具体公开 status URL。
- `tiktok_creative_center`：当前只生成来源级 `source_observations`。没有稳定原视频 permalink 时不生成 Candidate，不把趋势页或 CDN URL 当原帖。Research API 未获得批准前不得假设可用。
- 三类社交源示例均默认 disabled。真实能力矩阵和最近验证证据见 `artifacts/HS-004/ChatGPT/SOURCE-MATRIX.md`。

## 4. 现有服务端 HTTP 契约（已实现）

> **SYS-COLLECT 过渡说明（2026-09-16）**：以下 `/api/login`、`/api/link-preview`、个人 Cookie 轮询是现有 legacy 契约，用于历史兼容和隔离回归，不再作为新“系统热点”自动发布身份。Runner 已支持注入 `transport=`；系统 transport 可声明 `requires_login=False` 并以 `create_candidate(candidate, note)` 接收完整 Candidate，因此不会调用 one/two 个人登录。统一交接已冻结未来提交入口 `POST /internal/hotspots` 与固定系统作者，但专用令牌的具体 HTTP 头、轮询/对账只读路由尚未由 SYS-CORE 冻结；采集端当前只提供 transport 边界与 fixture，不猜测这些路由，也不偷偷回退个人账号。 queued/fetching 创建结果中的 `poll_url` 由采集状态持久化；系统 transport 只接受服务端返回的同源绝对路径。缺失或跨域 `poll_url` 时保持 pending 并报告协议错误，不猜 legacy 路由。仅 legacy `TogetherClient` 可在没有 `poll_url` 时回退 `/api/link-preview/<id>`。


生产入口 `https://your-domain.example`；测试必须使用独立DATA_DIR、测试账号与端口。API客户端不得把生产域名作为默认值。公网必须验证TLS；本地测试可用回环HTTP及测试实例的 `COOKIE_SECURE=0`，不得为生产关闭Secure Cookie。

### 4.1 登录

```http
POST /api/login
Content-Type: application/json

{"user":"one","password":"<从私有环境变量读取>"}
```

200返回 `{"user":{"id":"one","name":"..."}}` 并设置 `session` Cookie。用户ID是 `one` 或 `two`，不是显示名viewer1/viewer2。复用cookie jar，仅存内存，不打印密码或Cookie。遇401可重新登录一次；持续失败停止提交，避免触发登录限流。

Legacy配置变量：`HOTSPOT_API_BASE`、`HOTSPOT_USER_ID`、`HOTSPOT_USER_PASSWORD`、`HOTSPOT_STATE_DIR`。禁止使用root密码、WORKER_TOKEN或私钥作为采集认证。该个人账号路径只保留历史兼容/隔离测试；新 SYS-COLLECT 自动发布必须等待专用系统 transport，不能把 one/two 改名或继续用个人 Cookie 冒充“系统管理员”。

### 4.2 创建解析任务

```http
POST /api/link-preview
Content-Type: application/json

{"text":"https://example.com/news/123?lang=zh","note":"[hotspot:v1:<完整64位去重键>] 自动热点｜示例资讯｜榜单标题"}
```

所有JSON由序列化库编码，Content-Length由HTTP库计算。Origin可不发送；若发送必须严格匹配API base的协议和Host。不要向来源网站发送登录Cookie或Authorization。

200示例：

```json
{"id":"0123456789abcdef0123456789abcdef","status":"queued","source":{"url":"https://example.com/news/123?lang=zh","platform":"web","content_id":""},"poll_url":"/api/link-preview/0123456789abcdef0123456789abcdef"}
```

`text`最大10000字符、`note`最大1000字符；`title_hint/summary_hint`不会覆盖最终正文或解析标题。来源元数据完整保存于采集器状态库，note仅用于来源标注及提交核对。

**当前接口没有批量提交、机器人token、来源元数据专用字段或Idempotency-Key支持。不要发明这些能力。**200仅代表排队成功，不代表解析成功。失败和review仅创建账号可见；queued/fetching可被双方看见；解析器成功会直接published，不能把这条接口当作“只预览不发布”。

### 4.3 查询进度与资源

`GET /api/link-preview/{id}`：使用同一账号Cookie。返回 `id/status/phase/source/content/warnings`，failed时另含 `error`；**此响应没有attempts字段**。仅接受同源且格式为上述路径的poll_url，不跟随其跳转去其他主机。

- `queued/fetching`：处理中，content可能为null。
- `published`：成功，content包括 `title/description/author/published_at/content_type/cover/images/videos`。
- `review`：待人工检查，采集器不得自动调用publish。
- `failed`：终止失败，记录原因，不自动调用retry，也不换新ID重新提交。

图片/视频为需要鉴权的本服务 `/media/` 路径；获取时保持同源、携带Cookie。验证视频用Range请求，不全量下载200MB文件做常规测试。

`GET /api/system` 返回worker_recent_contact、media_bytes、media_limit_bytes等；没有队列容量/磁盘剩余字段。`GET /api/shares`仅返回账号可见的最近200条，用于超时核对而非无限历史库。删除测试分享使用 `DELETE /api/shares/{id}`，只能删除本次测试明确登记的ID。

### 4.4 错误与重试

错误体通常为 `{"error":"..."}`。处理规则：

| 情形 | 采集器行为 |
|---|---|
| 400/413/415 | 记录输入或协议错误，不重复提交 |
| 401 | 重新登录一次，仍失败则停止 |
| 403 | 权限/来源错误，停止对应操作 |
| 404 查询 | 记录deleted_or_missing，不自动补建 |
| 429 创建 | 当前队列已满，候选deferred，指数退避至后续轮次；不循环轰炸 |
| 503 standby/不可用 | 暂停新提交；已有任务保留待后续核对 |
| 507或解析结果提示空间不足 | 停止本轮新增媒体任务，报告人工清理；禁止自动删旧分享 |
| 创建超时/断线/无法确认的5xx/坏响应 | 标为submit_unknown，按下一节核对，禁止盲目重发POST |
| GET临时网络错误 | 有限退避重试，不改变任务ID |

## 5. 状态、去重和异常恢复

使用独立 `HOTSPOT_STATE_DIR/state.sqlite3`，不得读写业务 `app.sqlite3`。生产候选目录为 `/srv/together/hotspots`，执行者此阶段不创建生产服务。限制状态目录访问权限；部署时单个实例，进程锁防重入。

- URL规范化只统一scheme/host大小写、默认端口、移除fragment；保留路径大小写、查询参数及顺序。只按明确白名单去除 `utm_*` 等追踪参数，禁止去掉未知参数、签名、内容ID。
- 去重键 `sha256("hotspot:v1\n" + normalized_url)`（UTF-8），跨来源同URL共用键；唯一索引保障同一状态库不重复入队。另存 `(source_id, source_item_id)`用于识别同源重复；不能仅凭标题去重。不同短链或不同媒体报道同一事件不承诺语义去重。
- 状态：discovered → deferred/submitting → submitted → queued/fetching → published/review/failed；另有submit_unknown、deleted_or_missing、rejected。
- 每次POST前先事务记录submitting及日额度预留；收到成功ID立即持久化。进程崩溃遗留submitting按submit_unknown恢复。 创建响应若提供 `poll_url` 同步持久化，用于跨轮恢复；它不是可执行外部URL，必须保持同源路径语义。
- 未知提交结果：GET /api/shares按note完整去重标记、作者及URL核对；唯一匹配则关联已有ID；没有匹配或多匹配则保持unknown并报告，不能因“最近200条未找到”就认为从未提交。脚本重启也不得自动重发。unknown计入日额度和未决预算，防止故障时连续堆积。
- published/failed等结果保留去重墓碑，删除分享不触发自动重建；第一版不自动过期去重记录。单实例状态丢失后的全局幂等无法由现有API保证，必须如实说明；未来服务端幂等接口由Codex设计。
- 轮询默认10秒并加入小幅抖动；每轮总运行预算5分钟。到期只记录pending并退出，不判为解析失败、不取消任务；下轮先恢复未完成轮询，再考虑新候选。上游解析器已有独立重试，采集器不叠加重试解析。
- 来源每次尝试的连接/TLS/响应头/正文读取/重定向后续连接共享15秒网络预算、最多2次、每源间隔至少1秒。实现使用单一 monotonic deadline 与连接 watchdog；每个 I/O 阶段开始前按剩余预算刷新 socket timeout，正文按固定小块分段读取并在块间复核 deadline，不能靠持续慢速分块把总网络时间无限延长。DNS解析返回后必须按剩余预算继续；同步 OS resolver 本身无法由 Python 强制中断，若 DNS 返回时预算已耗尽则立即停止。同一DNS结果含多个已验证公网地址时可在该共享预算内顺序连接回退，不能为每个地址重新获得完整15秒。压缩响应与解压后正文均最大2MiB；gzip 必须有界流式展开，达到上限即终止，禁止先完整解压再检查。最大候选50条/源、重定向最多3跳。持久化ETag/Last-Modified，304复用已处理状态。

## 6. 网络和隔离

来源URL来自显式配置白名单，新闻URL来自不可信内容；二者都须检查协议、域名、端口和地址。只允许HTTP/HTTPS、80/443，拒绝userinfo、回环/私网/链路本地/保留地址及DNS重绑定。逐跳校验并固定经过校验的地址；不能只验证首次URL后交给自动重定向。

正文仍由现有worker检查；采集阶段对RSS/JSON及重定向的安全验证同样必需。XML禁外部实体/DTD，防实体展开；压缩体同时限制压缩前后大小，gzip 采用有界分段解压并在超过2MiB时立即拒绝。HTML标签、来源字段和响应中的指令都作为数据处理。

`network=direct`默认，禁用系统代理环境继承；海外源可显式设 `network=source_proxy` 并要求 `HOTSPOT_SOURCE_PROXY`。该代理只允许无认证 `http://` 本机/私网端点，禁止 URL 内用户名密码、路径、查询和 fragment，也不得用于 Together 登录/API 请求。目标每一跳仍先由采集器 DNS 解析并要求全部地址为公网；HTTP CONNECT 的目标是已经校验的具体 IP，而 Host/TLS SNI 保持原域名，因此代理侧 DNS 不能替代或绕过 SSRF 校验。HTTPS→HTTP 重定向直接拒绝，跨 origin 重定向剥离 Authorization/Cookie/Proxy-Authorization。若本机 Clash fake-IP DNS 返回非公网地址（当前 TikTok Creative Center 可出现），应标记来源受阻/跳过，不临时放宽校验。

## 7. CLI 与报告接口（待实现的模块契约）

```text
python -m collectors.hotspots --config config/hotspots.example.json --dry-run --report <path>
python -m collectors.hotspots --config <private-config> --submit --report <path>
python -m unittest discover -s tests -p "test_hotspot_*.py" -v
```

不带模式时等同dry-run，两个模式互斥。dry-run不登录、不创建分享，只输出候选/筛选原因；不得把候选标成submitted/published或消耗正式日额度。支持注入fixture来源和隔离API地址。退出码：0正常（可含deferred/pending）、1配置/认证/状态库等致命失败、2部分来源或解析失败。JSON报告字段稳定：

```json
{"schema_version":1,"run_id":"<uuid>","started_at":"<UTC>","finished_at":"<UTC>","mode":"dry-run","source_commit":"<full sha>","counts":{"discovered":0,"deduplicated":0,"submitted":0,"published":0,"failed":0,"pending":0,"unknown":0},"items":[{"dedup_key":"<sha256>","source_id":"news-example","share_id":null,"status":"deferred","reason":"daily_limit"}]}
```

日志与报告不包含密码、Cookie、token、带敏感签名参数的URL。原始URL如需持久化仅放受限状态库；共享报告显示无查询参数URL或哈希。没有执行的测试明确标为not_run，不填pass。

## 8. 必做测试及验收矩阵

全部先在隔离实例运行，production默认禁止。测试fixtures必须自包含，无真实账户或私密帖子内容。

1. RSS/Atom/JSON提取、缺字段、时间转换、摘要与正文区分、实际文章链接识别；至少两种来源格式。
2. 纯链接/中文分享文案/查询参数；不同来源同URL去重、同标题不同URL保留、相同source_item_id重复、未知查询参数保留。
3. dry-run零POST、每日/每轮额度、进程锁、跨日额度、来源间公平选择、排队满429、worker失联暂停。
4. POST成功但响应丢失、成功ID持久化前崩溃、GET历史窗口内无匹配、多匹配、重启恢复：不得盲目创建第二份分享。
5. queued→fetching→published、failed/review、查询404、轮询超时跨轮恢复；不读取不存在的attempts字段。
6. 来源/API认证隔离、401一次重登、TLS失败拒绝、私网URL/DNS混合地址/逐跳跳转、XML实体及超限压缩体。
7. 真实本地HTTP链路：启动项目测试server与原有worker，通过可控fetcher替身提供文章/图片；从候选到分享持久化、另一账号可读，证明不是只mock提交响应。替身不能修改生产网络防护。
8. 真实公开来源：执行者核实两个来源，至少一个完整跑通“来源→原文→已有解析器→published→另一账号读取”；报告日期、来源证据、脱敏任务ID、标题/块类型、耗时。真实解析失败与采集成功分开统计，不把queued当成功。无法联网或未有测试服务时明确not_run，保留复跑命令。
9. 不影响现有测试：在依赖可用环境跑原有全量unittest；若环境不具备条件如实记录，不能引用先前145项为本轮结果。清理仅限明确记录的测试分享/独立测试目录。

## 9. 交付给 Codex 的内容

- 实现代码、示例配置（默认来源disabled）、fixtures和自动化测试。
- 本文件契约有偏差时，明确列出差异及原因，等待集成审核，不自行扩展核心API。
- 将测试命令、真实来源核查、运行环境、结果计数、脱敏报告路径、实现提交、未验证项和下一步追加到 `PROJECT-HANDOFF.md`，不另建HANDOFF/TASK/REVIEW文档。
- 推荐报告放 `artifacts/hotspots/<run_id>/report.json`，是否纳入Git由Codex审查；真实状态库、env及日志不入Git。
- 网页版Gemini无法运行环境时交付完整文件或unified diff及未执行测试清单，由豆包或Codex在本地执行；不得声称已自行跑完测试。

初次派工提示：请先阅读PROJECT-HANDOFF.md，再按本规格v1领取HS-001，仅在限定文件范围实施。完成离线与隔离端到端测试后提交补丁和脱敏结果供Codex复核；生产定时任务由Codex最终集成部署。
