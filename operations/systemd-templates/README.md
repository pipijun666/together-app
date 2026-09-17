# 一起看 — 服务器 systemd 服务可重建模板（脱敏）

本目录为生产服务器上代理/热点等 systemd 服务的**脱敏可重建模板**，
避免"有效配置只存在于线上 systemd 手改、无法重建"。

## 服务清单与模板说明

| 服务 | 用途 | 说明 |
|---|---|---|
| `mihomo.service` | Clash Meta 代理，监听 127.0.0.1:7890（mixed-port），订阅 token 仅存 /etc/mihomo/config.yaml | 主出口代理 |
| `together-http-proxy.service` | HTTP→SOCKS 转换层：127.0.0.1:8118 → 127.0.0.1:7890（直连 mihomo） | importer 的 PROXY_URL=127.0.0.1:8118 |
| `together-tunnel.service` | SSH 动态隧道备用出口：127.0.0.1:1080 → 远程出口节点 | 备用出口通道 |
| `together-hotspots.service` + `.timer` | 热点采集推送，每 2 小时；报告保留 14 天 | OnCalendar=0/2:00:00，Persistent |
| nginx | 站点反代 + /tr 翻译代理 | 外部 HTTPS 反代服务 |

## 出口拓扑

importer (PROXY_URL=127.0.0.1:8118) → http2socks (8118) → mihomo (7890) → 出口代理
备用：SSH 隧道 1080 → 备用节点（仅出口）

## 重建步骤（若服务器重装）

1. 安装 mihomo + 订阅配置（/etc/mihomo/config.yaml，token 由用户提供，不入库）
2. 安装 above 各 .service/.timer 模板，替换占位符（<ENVFILE>、<PRIVATE_KEY_PATH> 等）
3. `systemctl daemon-reload && systemctl enable --now <unit>`
4. 热点运行环境：/srv/together/app（代码）、/srv/together/hotspots/（hotspots.json、state/、reports/）、/root/together-secrets/hotspots.env（600）

## 脱敏说明

- 不包含：机场订阅 token、SSH 私钥、known_hosts、登录口令、平台 Cookie、API Token。
- 不包含：.env / hotspots.env 内容（口令只在服务器文件，从备份 .env.bak-20260914-134041 恢复历史见 PROJECT-HANDOFF.md REL-002）。
- 占位符明确标注，禁止用真实值填充后提交 Git。
