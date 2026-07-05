# App Tracker（带 token 认证版）

记录手机当前打开的 App，供 AI 查询。基于 [B7-0221/app-tracker](https://github.com/B7-0221/app-tracker) 改造：

- 所有业务接口需要 `Authorization: Bearer <AUTH_TOKEN>`（包括 `/report`，防止别人灌数据）
- 时间戳统一 UTC+8（北京时间）
- 新增 `/stats` 今日统计接口
- 内存存储最多保留最近 200 条；可选 JSON 文件持久化
- 仅依赖 express，无其他依赖

## 接口

| 接口 | 方法 | 说明 |
|---|---|---|
| `/` | GET | 健康检查（唯一不需要 token 的接口，不含数据） |
| `/report` | POST | 上报，body: `{"app": "App名称"}`（JSON 或表单均可） |
| `/recent` | GET | 最近记录，`?limit=N` 控制条数，默认 20 |
| `/current` | GET | 最近一条记录（当前在用什么） |
| `/stats` | GET | 今日（北京时间）各 App 使用次数，按次数降序 |

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `AUTH_TOKEN` | ✅ | 认证 token，不设置服务拒绝启动 |
| `PORT` | ❌ | 监听端口，Zeabur 自动注入，默认 3000 |
| `DATA_FILE` | ❌ | 设置后启用 JSON 文件持久化，如 `/data/records.json`（需给 Zeabur 服务挂载 Volume 到 `/data`；不设置就是纯内存） |

## 部署到 Zeabur

1. Zeabur 控制台 → 项目 → **Add Service → Git**，选择本仓库
2. **Root Directory 设置为 `app-tracker`**（重要：这是仓库的子目录，Zeabur 会识别为 Node.js 项目并用 `npm start` 启动）
3. 在服务的 Variables 里添加 `AUTH_TOKEN=<你的随机字符串>`
4. Networking 里生成域名（`xxx.zeabur.app`）或绑定自己的域名
5. 测试（把域名和 token 替换成你的）：

```bash
TOKEN="你的token"
HOST="https://xxx.zeabur.app"

# 上报一条
curl -X POST "$HOST/report" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"app":"微信"}'

# 查最近记录
curl "$HOST/recent" -H "Authorization: Bearer $TOKEN"

# 当前App / 今日统计
curl "$HOST/current" -H "Authorization: Bearer $TOKEN"
curl "$HOST/stats"   -H "Authorization: Bearer $TOKEN"
```

## iOS 快捷指令设置

对每个想追踪的 App 重复以下步骤：

1. 打开 **快捷指令** App → **自动化** 标签页 → 右上角 **+** → **创建个人自动化**
2. 选择 **App** → 选取要追踪的 App → 勾选 **已打开** → 选择 **立即运行**（不询问）
3. 添加动作，搜索 **获取URL内容**，配置：
   - URL：`https://你的域名/report`
   - 方法：**POST**
   - 头部（Headers）新增一条：
     - 键：`Authorization`
     - 值：`Bearer 你的token`（注意 Bearer 后面有个空格）
   - 请求体：选 **JSON**，新增一个字段：
     - 键：`app`，类型文本，值填 App 名称（如 `微信`）
4. 保存。打开该 App 时会静默发一条上报

> 提示：请求体也可以选"表单"，键 `app` 值同样填 App 名称，服务端两种都支持。

## 本地运行

```bash
cd app-tracker
npm install
AUTH_TOKEN=test123 npm start
```

## 后续：MCP 包装

计划把 `/recent`、`/current`、`/stats` 包装成 MCP 工具，让 Claude 直接调用。待前面步骤跑通后再做。
