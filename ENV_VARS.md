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
| `OMBRE_DASHBOARD_PASSWORD` | 否 | — | 预设 Dashboard 访问密码；设置后覆盖文件存储的密码，首次访问不弹设置向导 |
| `OMBRE_DEHYDRATION_MODEL` | 否 | `deepseek-chat` | 脱水/打标/合并/拆分用的 LLM 模型名（覆盖 `dehydration.model`） |
| `OMBRE_DEHYDRATION_BASE_URL` | 否 | `https://api.deepseek.com/v1` | 脱水模型的 API Base URL（覆盖 `dehydration.base_url`） |
| `OMBRE_MODEL` | 否 | — | `OMBRE_DEHYDRATION_MODEL` 的别名（前者优先） |
| `OMBRE_EMBEDDING_MODEL` | 否 | `gemini-embedding-001` | 向量嵌入模型名（覆盖 `embedding.model`） |
| `OMBRE_EMBEDDING_BASE_URL` | 否 | — | 向量嵌入的 API Base URL（覆盖 `embedding.base_url`；留空则复用脱水配置） |
| `OMBRE_BACKUP_TOKEN` | 否 | — | 推送备份用的 GitHub 个人访问令牌（需 `repo` 权限）。未设置则尝试 `GITHUB_TOKEN`；都没有时跳过备份 |
| `OMBRE_BACKUP_REPO` | 否 | `xinyi010524-blip/ob-backup` | 备份目标私有仓库 `owner/name` |
| `OMBRE_BACKUP_BRANCH` | 否 | `main` | 备份推送的目标分支 |
| `OMBRE_BACKUP_SUBDIR` | 否 | `backups` | 备份 JSON 在仓库内的子目录，也是 `git add` 的**唯一**范围（避免误提交 workflow 文件） |
| `OMBRE_BACKUP_TIME` | 否 | `00:10` | 每日定时备份时间 `HH:MM`（24 小时制，按服务器本地时区） |
| `OMBRE_BACKUP_WORKDIR` | 否 | `{buckets_dir}/.ob-backup-repo` | 备份仓库本地克隆目录（默认放在 buckets 目录下，随持久化磁盘保留） |
| `OMBRE_BACKUP_GIT_NAME` | 否 | `Ombre Brain Backup` | 备份 commit 的 author name |
| `OMBRE_BACKUP_GIT_EMAIL` | 否 | `ombre-backup@users.noreply.github.com` | 备份 commit 的 author email |

## 每日全库备份 (`OMBRE_BACKUP_*`)

服务每天在 `OMBRE_BACKUP_TIME`（默认 `00:10`）将全库（所有桶 + 归档 + feel + 情绪坐标 valence/arousal）导出为单个 JSON，commit 并 push 到独立私有仓库，文件按日期命名 `backup-YYYY-MM-DD.json`，保留全部历史版本。

- 调度器在服务启动后随首个 `/health` 命中懒启动（HTTP 模式下保活循环每 60 秒会 ping `/health`）。
- 手动触发：`POST /api/backup/run`（需 Dashboard 认证）。
- 查看状态：`GET /api/backup/status`。
- **`git add` 范围被严格限定为 `OMBRE_BACKUP_SUBDIR`**（默认 `backups/`），绝不暂存仓库根目录或 `.github/workflows/`，以免触发 GitHub Actions 默认 token 无 `workflow` 权限导致 push 被拒。

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
