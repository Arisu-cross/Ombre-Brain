# 环境变量参考

| 变量名 | 必填 | 默认值 | 说明 |
|--------|------|--------|------|
| `OMBRE_API_KEY` | 是 | — | Gemini / OpenAI-compatible API Key，用于脱水(dehydration)和向量嵌入 |
| `OMBRE_BASE_URL` | 否 | `https://generativelanguage.googleapis.com/v1beta/openai/` | API Base URL（可替换为代理或兼容接口） |
| `OMBRE_TRANSPORT` | 否 | `stdio` | MCP 传输模式：`stdio` / `sse` / `streamable-http` |
| `OMBRE_PORT` | 否 | `8000` | HTTP/SSE 模式监听端口（仅 `sse` / `streamable-http` 生效） |
| `OMBRE_BUCKETS_DIR` | 否 | `./buckets` | 记忆桶文件存放目录（绑定 Docker Volume 时务必设置） |
| `OMBRE_HOOK_URL` | 否 | — | Breath/Dream Webhook 推送地址（POST JSON），留空则不推送 |
| `OMBRE_HOOK_SKIP` | 否 | `false` | 设为 `true`/`1`/`yes` 跳过 Webhook 推送（即使 `OMBRE_HOOK_URL` 已设置） |
| `BREATH_RECENT_N` | 否 | `3` | 无参 breath / 唤醒 / breath-hook 里「最近记下」（hold 写入的动态桶）浮现条数，设 `0` 关闭 |
| `OMBRE_UTC_OFFSET` | 否 | `8` | 记忆时间戳的 UTC 偏移小时数（默认北京时间）。容器多为 UTC，不设的话后半夜的记忆会被记成前一天 |
| `OMBRE_STATELESS` | 否 | `1` | streamable-http 无状态模式 + JSON 响应：每次调用独立请求，服务器重启后旧客户端连接依然可用（常驻型客户端不再需要重启进程）。设 `0` 恢复有状态会话 |
| `OMBRE_DASHBOARD_PASSWORD` | 否 | — | 预设 Dashboard 访问密码；设置后覆盖文件存储的密码，首次访问不弹设置向导 |
| `OMBRE_DEHYDRATION_MODEL` | 否 | `deepseek-chat` | 脱水/打标/合并/拆分用的 LLM 模型名（覆盖 `dehydration.model`） |
| `OMBRE_DEHYDRATION_BASE_URL` | 否 | `https://api.deepseek.com/v1` | 脱水模型的 API Base URL（覆盖 `dehydration.base_url`） |
| `OMBRE_MODEL` | 否 | — | `OMBRE_DEHYDRATION_MODEL` 的别名（前者优先） |
| `OMBRE_EMBEDDING_MODEL` | 否 | `gemini-embedding-001` | 向量嵌入模型名（覆盖 `embedding.model`） |
| `OMBRE_EMBEDDING_BASE_URL` | 否 | — | 向量嵌入的 API Base URL（覆盖 `embedding.base_url`；留空则复用脱水配置） |
| `OMBRE_EMBEDDING_API_KEY` | 否 | — | 向量嵌入专用 API Key（覆盖 `embedding.api_key`）；留空则回退复用 `OMBRE_API_KEY`。需要嵌入与脱水走不同供应商/密钥时设置 |
| `OMBRE_EMBEDDING_ENABLED` | 否 | `true` | 向量嵌入开关；设为 `false`/`0`/`no`/`off` 关闭语义检索，`breath` 自动降级为纯关键词匹配 |
| `OMBRE_BACKUP_TOKEN` | 否 | — | 推送备份用的 GitHub 个人访问令牌（需 `repo` 权限）。未设置则尝试 `GITHUB_TOKEN`；都没有时跳过备份 |
| `OMBRE_BACKUP_REPO` | 否 | `xinyi010524-blip/ob-backup` | 备份目标私有仓库 `owner/name` |
| `OMBRE_BACKUP_BRANCH` | 否 | `main` | 备份推送的目标分支 |
| `OMBRE_BACKUP_SUBDIR` | 否 | `backups` | 备份 JSON 在仓库内的子目录，也是 `git add` 的**唯一**范围（避免误提交 workflow 文件） |
| `OMBRE_BACKUP_TIME` | 否 | `00:10` | 每日定时备份时间 `HH:MM`（24 小时制，按服务器本地时区） |
| `OMBRE_BACKUP_WORKDIR` | 否 | `{buckets_dir}/.ob-backup-repo` | 备份仓库本地克隆目录（默认放在 buckets 目录下，随持久化磁盘保留） |
| `DREAM_ARCHIVE_N` | 否 | `3` | `dream()` 回想时带几条「还没消化过的归档」，设 `0` 关闭（回到只看最近新记的） |
| `DREAM_ARCHIVE_DAYS` | 否 | `14` | 只带最近多少天内的归档 |
| `DREAM_ARCHIVE_PREVIEW` | 否 | `300` | 归档在 dream 里的预览字数；全文用 `dream(detail_ids=...)` |
| `WAKE_DIGEST_HINT_MIN` | 否 | `2` | 唤醒时未消化的归档达到几条才附那行提醒，设 `0` 关闭 |
| `OMBRE_ROLLUP_ENABLED` | 否 | `true` | 归档分层（周记/月记）总开关 |
| `OMBRE_ROLLUP_DAILY_DAYS` | 否 | `7` | 日档保留几天不卷；超过就按自然周合成周记 |
| `OMBRE_ROLLUP_WEEKLY_DAYS` | 否 | `30` | 周记满多少天卷成月记 |
| `OMBRE_ROLLUP_MODEL` | 否 | 沿用脱水配置 | 写周记/月记用的模型（如 `deepseek-chat`） |
| `OMBRE_ROLLUP_BASE_URL` | 否 | 沿用脱水配置 | 写周记/月记的 API 地址（如 `https://api.deepseek.com/v1`） |
| `OMBRE_ROLLUP_API_KEY` | 否 | 沿用脱水配置 | 写周记/月记的 Key；三个都不设就跟脱水走同一家 |
| `OMBRE_ROLLUP_MAX_TOKENS` | 否 | `1500` | 单条周记/月记的生成上限 |
| `OMBRE_ROLLUP_INTERVAL_H` | 否 | `24` | 分层巡查间隔（小时） |
| `OMBRE_BACKUP_GIT_NAME` | 否 | `Ombre Brain Backup` | 备份 commit 的 author name |
| `OMBRE_BACKUP_GIT_EMAIL` | 否 | `ombre-backup@users.noreply.github.com` | 备份 commit 的 author email |

## 每日全库备份 (`OMBRE_BACKUP_*`)

服务每天在 `OMBRE_BACKUP_TIME`（默认 `00:10`）将全库（所有桶 + 归档 + feel + 情绪坐标 valence/arousal）导出为单个 JSON，commit 并 push 到独立私有仓库，文件按日期命名 `backup-YYYY-MM-DD.json`，保留全部历史版本。

- 调度器在服务启动后随首个 `/health` 命中懒启动（HTTP 模式下保活循环每 60 秒会 ping `/health`）。
- 手动触发：`POST /api/backup/run`（需 Dashboard 认证）。
- 查看状态：`GET /api/backup/status`。
- **`git add` 范围被严格限定为 `OMBRE_BACKUP_SUBDIR`**（默认 `backups/`），绝不暂存仓库根目录或 `.github/workflows/`，以免触发 GitHub Actions 默认 token 无 `workflow` 权限导致 push 被拒。

## 归档分层 (`OMBRE_ROLLUP_*`)

`archive_session` 每天写一个日档，唤醒只浮现最近几条 —— 时间一长，更早的日子既不会
自己浮上来，也没有更粗的替身。分层引擎按「越久越粗，但不断线」来收：

| 年龄 | 呈现 |
|---|---|
| 最近 7 天 | 日档原样浮现 |
| 超过 7 天 | 按自然周（周一~周日）合成一条「周记 YYYY-Www」 |
| 周记超过 30 天 | 按月合成一条「月记 YYYY-MM」 |

- **原档永远保留**，只是打上 `rolled_up` 标记后不再单独浮现；搜索照样搜得到，
  周记/月记正文里也写着源档案的 id，随时能查回去。
- 只卷**已经结束**的周期（本周没过完不会提前封盘），同一轮里可以级联
  （日档 → 周记 → 月记），首次上线时积压的历史档案一次补齐。
- 手动触发：`POST /api/rollup/run`；查看状态：`GET /api/rollup/status`（都需认证）。
- 想让它单独走另一家模型（例如周记/月记走 DeepSeek，脱水仍走原来的），设
  `OMBRE_ROLLUP_MODEL` / `_BASE_URL` / `_API_KEY` 三个即可；不设就沿用脱水配置。

## 说明

- `OMBRE_API_KEY` 也可在 `config.yaml` 的 `dehydration.api_key` / `embedding.api_key` 中设置，但**强烈建议**通过环境变量传入，避免密钥写入文件。
- `OMBRE_DASHBOARD_PASSWORD` 设置后，Dashboard 的"修改密码"功能将被禁用（显示提示，建议直接修改环境变量）。未设置则密码存储在 `{buckets_dir}/.dashboard_auth.json`（SHA-256 + salt）。

## Webhook 推送格式 (`OMBRE_HOOK_URL`)

设置 `OMBRE_HOOK_URL` 后，Ombre Brain 会在以下事件发生时**异步**（fire-and-forget，5 秒超时）`POST` JSON 到该 URL：

| 事件名 (`event`) | 触发时机 | `payload` 字段 |
|------------------|----------|----------------|
| `breath` | MCP 工具 `breath()` 返回时 | `mode` (`ok`/`empty`), `matches`, `chars` |
| `dream` | MCP 工具 `dream()` 返回时 | `recent`, `chars` |
| `breath_hook` | HTTP `GET /breath-hook` 命中（SessionStart 钩子） | `surfaced`, `chars` |
| `dream_hook` | HTTP `GET /dream-hook` 命中 | `surfaced`, `chars` |

请求体结构（JSON）：

```json
{
  "event": "breath",
  "timestamp": 1730000000.123,
  "payload": { "...": "..." }
}
```

Webhook 推送失败仅在服务日志中以 WARNING 级别记录，**不会影响 MCP 工具的正常返回**。
