# OpenAI Compatible Image Client

[English README](README_EN.md)

一个面向 OpenAI Images API 与 xAI Grok Imagine 兼容服务的图片/视频客户端，提供四种入口：

- Python CLI
- 本地 Web UI
- 本地 `stdio` MCP server
- 多租户 Streamable HTTP MCP server

支持文生图、图生图、文生视频和图生视频。MCP 模式使用服务端 named profile，模型不能通过工具参数传入 API Key 或任意 Base URL。

## 功能概览

- OpenAI-compatible `images/generations` 与 `images/edits`
- Grok Imagine 图片与异步视频任务
- OpenAI/Grok 协议感知的 Web 控件
- Web 异步 job、状态轮询、取消和当前页面任务历史
- Web UI 默认支持跨来源 iframe 嵌入，并可按父页面 origin 收紧
- Base64 或远端 URL 媒体结果
- 私网上游默认拒绝，支持精确主机白名单
- MCP named profile、受控本地图片导入、远程租户隔离、资源链接和有界任务队列

## 本次更新

- 抽取 `image_client_core/`，统一 CLI、Web 与 MCP 共用的端点、模型和响应解析逻辑
- Web 任务支持取消排队任务与轮询阶段、有限队列、HTTP/1.1 keep-alive、连接复用和不可变结果缓存
- 修复 MCP 任务取消、过期或淘汰后的结果资产与磁盘配额泄漏
- 前端减少历史区重复渲染和媒体重复下载，参考图只在提交时编码
- 加强 Base64、响应体大小、SSRF、认证、请求体和错误响应校验

完整的审查结果、行为变化和验证记录见 [OPTIMIZATION.md](OPTIMIZATION.md)。

## 项目结构

```text
.
├── generate_image.py             # CLI
├── web_app.py                    # Web 服务与共享生成实现
├── web/                          # 本地构建的 Web 前端
├── mcp_server.py                 # stdio MCP 入口
├── mcp_http_server.py            # Streamable HTTP MCP 入口
├── openai_image_client/
│   ├── http_server.py            # HTTP app、路由与传输防护
│   ├── http_auth.py              # Bearer Token 租户认证
│   ├── tenancy.py                # 租户隔离、共享容量与 RPM
│   └── ...                       # MCP profile/job/asset/service
├── config.json.example           # CLI 示例配置
├── mcp-config.example.json       # MCP named profile 示例
├── mcp-http-auth.example.json    # HTTP 租户认证示例
├── OPTIMIZATION.md               # 本轮审查、修复与验证记录
├── requirements.txt              # CLI/Web 依赖
├── requirements-mcp.txt          # MCP 运行依赖
├── requirements-dev.txt          # 测试与打包依赖
└── pyproject.toml                # MCP 可编辑安装与命令入口
```

## 安装

CLI 和 Web：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

MCP（同时包含 CLI/Web 依赖）：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Windows 激活命令为 `.venv\Scripts\activate`。

## CLI

CLI 按以下优先级读取配置：命令行参数、环境变量、`config.local.json`、`config.json`。

环境变量可避免 Key 出现在进程参数中，但直接输入下面的 `export` 命令仍可能写入 shell history。实际使用时应由 secret manager 或父进程注入，或者先通过 shell 的隐藏输入（如 `read -s`）读取后再 `export`：

```bash
export OPENAI_IMAGE_BASE_URL="https://api.openai.com/v1"
export OPENAI_IMAGE_API_KEY="replace-with-secret"
python generate_image.py --protocol openai --model gpt-image-2 \
  --prompt "A clean product photograph on a neutral studio background"
```

也可以创建本地私有配置：

```bash
cp config.json.example config.local.json
chmod 600 config.local.json
python generate_image.py
```

Grok 图片编辑：

```bash
python generate_image.py \
  --mode edit \
  --protocol grok \
  --model grok-imagine-image \
  --prompt "保留主体，改成夜间电影灯光" \
  --image ./reference.png
```

Grok 视频：

```bash
python generate_image.py \
  --mode video \
  --protocol grok \
  --model grok-imagine-video-1.5 \
  --prompt "A slow cinematic camera move around the subject" \
  --aspect-ratio 16:9 \
  --resolution 720p \
  --duration 10 \
  --timeout 600
```

## Web UI

启动：

```bash
source .venv/bin/activate
python web_app.py
```

默认只监听 `http://127.0.0.1:8000`。如果端口占用，会尝试后续端口；使用 `--strict-port` 可关闭该行为。

### Web 工作方式

1. 前端向 `POST /api/generate-async` 创建 job。
2. 前端轮询 `GET /api/jobs/{job_id}`。
3. 完成后显示图片/视频并将结果追加到当前页面历史。
4. “取消任务”调用 `DELETE /api/jobs/{job_id}`。

取消排队任务会尽快停止；已进入上游 HTTP 调用的任务不能强制中断底层请求，但结果会在提供商返回后被丢弃。历史和媒体预览仅保存在当前页面内存，刷新后清空。

浏览器只持久化非敏感设置，包括安全形式的 Base URL、协议、模式、模型、尺寸、数量、超时和视频选项。以下内容不会写入 `localStorage`：

- API Key
- 提示词
- 参考图
- 生成结果与任务历史

旧版可能保存过 Key 的 `openai-image-client-web:v3` 数据会在页面初始化时删除。

### Web 限制

| 项目 | 限制 |
| --- | --- |
| 请求体 | 48 MiB |
| 单张参考图 | 10 MiB |
| 全部参考图 | 30 MiB |
| 图片数量 | 1-8 |
| 视频数量 | 1-4 |
| OpenAI 编辑参考图 | 最多 16 张 |
| Grok 图片参考图 | 最多 3 张 |
| Grok 视频参考图 | 最多 7 张 |
| 上游超时 | 5-600 秒 |
| Web worker/等待队列 | 2 / 8 |
| 已完成 job 保留 | 1 小时 |

### Web 安全配置

默认配置适合本机使用，不应直接将裸端口暴露到公网。

**iframe 嵌入**

Web UI 默认允许任意 HTTP(S) 父页面通过 iframe 嵌入，不需要额外配置：

```html
<iframe
  src="http://127.0.0.1:8000/"
  title="Image Client"
  sandbox="allow-scripts allow-same-origin allow-downloads"
  style="width:100%;height:900px;border:0"
></iframe>
```

`allow-same-origin` 是必需的；缺少它时 sandbox 会产生 opaque origin，API 写请求将被后端拒绝。`allow-downloads` 用于结果下载。HTTPS 父页面应嵌入 HTTPS 地址，避免 mixed-content 拦截。

默认开放嵌入意味着任意站点都能展示并覆盖这个 UI，存在 clickjacking 风险。共享或公网部署应通过 `OPENAI_IMAGE_CLIENT_EMBED_ORIGINS` 收紧父页面来源：

```bash
# 只允许同源和指定控制台
export OPENAI_IMAGE_CLIENT_EMBED_ORIGINS="self,https://console.example.com"

# 完全禁止 iframe；设为 self 则只允许同源 iframe
export OPENAI_IMAGE_CLIENT_EMBED_ORIGINS="none"
```

变量未设置时等价于 `*`；显式空值或 `none` 表示禁止；`self` 表示同源；其他值必须是逗号分隔的完整 HTTP(S) origin，不能包含路径、查询参数或通配主机。反向代理不能额外发送 `X-Frame-Options: DENY`，也不能附加更严格的第二条 CSP，否则浏览器仍会拒绝嵌入。iframe 来源白名单与 `OPENAI_IMAGE_CLIENT_ALLOWED_ORIGINS` 相互独立。

**私网上游**

Base URL 和上游返回的媒体 URL 会进行 SSRF 检查。解析到回环、局域网、链路本地或其他非公网地址时默认拒绝。若确实需要访问受信任的本地网关，按主机名或 IP 精确放行：

```bash
export OPENAI_IMAGE_CLIENT_TRUSTED_UPSTREAM_HOSTS="gateway.lan,10.0.0.10"
python web_app.py
```

`OPENAI_IMAGE_CLIENT_ALLOW_PRIVATE_UPSTREAMS=1` 会放行所有私网目标，范围过大，只应在隔离的开发环境临时使用。

实际连接会固定到校验通过的 IP，同时保留原始 Host、TLS SNI 和证书主机名校验，避免 DNS 在校验与连接之间重绑定到私网地址。为保证该约束，Web 服务的出站请求不读取 `HTTP_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY`；需要代理访问上游时，应使用管理员控制且已通过 `OPENAI_IMAGE_CLIENT_TRUSTED_UPSTREAM_HOSTS` 精确放行的本地网关地址。

**非回环监听**

监听 `0.0.0.0`、局域网地址或其他非回环地址时，服务拒绝启动，除非设置服务令牌：

```bash
export OPENAI_IMAGE_CLIENT_AUTH_TOKEN="replace-with-a-long-random-token"
python web_app.py --host 0.0.0.0 --port 8000 --strict-port --no-open-browser
```

API 客户端必须发送其中一个头：

```text
Authorization: Bearer <service-token>
X-Image-Client-Token: <service-token>
```

当前 Web 页面没有服务令牌输入框，也不会自动附加该头。远程浏览器使用时，应在前面部署带 TLS 和用户认证的反向代理，由代理向后端注入服务令牌；不要让浏览器直接访问裸端口。跨 Origin 写请求还需显式配置：

```bash
export OPENAI_IMAGE_CLIENT_ALLOWED_ORIGINS="https://images.example.com,https://admin.example.com"
```

令牌不能替代 TLS。避免在明文 HTTP 网络上传输 API Key 或服务令牌。

## MCP Server

MCP server 使用本地 `stdio`，不开放额外网络端口。客户端通过 named profile 选择已配置的提供商；工具 schema 不包含 `api_key` 或 `base_url`。

### Named profile 配置

创建私有配置：

```bash
cp mcp-config.example.json mcp-config.local.json
chmod 600 mcp-config.local.json
export OPENAI_IMAGE_MCP_CONFIG="$PWD/mcp-config.local.json"
export OPENAI_IMAGE_OPENAI_API_KEY="replace-with-secret"
export OPENAI_IMAGE_GROK_API_KEY="replace-with-secret"
```

示例配置通过 `api_key_env` 引用 Key，不把 Key 写入 JSON。`allowed_models` 建议始终显式设置；空数组表示允许 profile 使用任意请求模型。

主要字段：

- `default_profile`：工具未传 `profile` 时使用的 profile
- `profiles.<name>.base_url`：固定上游地址
- `profiles.<name>.api_key_env`：读取 Key 的环境变量名
- `profiles.<name>.protocol`：`auto`、`openai` 或 `grok`
- `image_model` / `video_model`：默认模型
- `allowed_models`：该 profile 的模型白名单
- `allowed_input_roots`：`import_reference_image` 可读取的目录
- `output_dir`：MCP 资源落盘目录
- `max_workers` / `max_queue`：默认 `2` / `20`
- `max_jobs`：内存 job 注册表上限，默认 `200`；达到上限时优先清理最旧的已完成 job
- `job_ttl_seconds`：完成任务元数据保留时间，默认 24 小时
- `max_asset_store_bytes`：本地 `stdio` 的总容量；HTTP 模式下是每租户容量，默认 2 GiB

可用环境变量：

| 变量 | 用途 |
| --- | --- |
| `OPENAI_IMAGE_MCP_CONFIG` | MCP JSON 配置绝对路径 |
| `OPENAI_IMAGE_MCP_PROFILES_JSON` | 通过 JSON 环境变量追加/覆盖 profiles |
| `OPENAI_IMAGE_MCP_DEFAULT_PROFILE` | 覆盖默认 profile |
| `OPENAI_IMAGE_MCP_OUTPUT_DIR` | 覆盖输出目录 |
| `OPENAI_IMAGE_MCP_ALLOWED_INPUT_ROOTS` | 路径分隔符连接的允许目录，Unix 为 `:`，Windows 为 `;` |
| `OPENAI_IMAGE_MCP_MAX_WORKERS` | worker 数，1-8 |
| `OPENAI_IMAGE_MCP_MAX_QUEUE` | 等待队列，0-100 |
| `OPENAI_IMAGE_MCP_MAX_JOBS` | job 注册表上限，1-1000，默认 200 |
| `OPENAI_IMAGE_MCP_JOB_TTL` | job TTL，60-604800 秒 |
| `OPENAI_IMAGE_MCP_MAX_ASSET_STORE_BYTES` | asset store 总字节上限，默认 2 GiB |

未配置 named profile 时，也支持单 profile 兼容变量：`OPENAI_IMAGE_BASE_URL`、`OPENAI_IMAGE_API_KEY`、`OPENAI_IMAGE_PROTOCOL`、`OPENAI_IMAGE_MODEL`、`OPENAI_IMAGE_VIDEO_MODEL`。

MCP 调用同样执行 Web 的私网上游检查。私网 profile 需要在 MCP 子进程环境中设置 `OPENAI_IMAGE_CLIENT_TRUSTED_UPSTREAM_HOSTS`。

### 工具、资源与 prompts

工具：

- `generate_image`：生成 1-4 张图片，等待完成并返回资源链接
- `import_reference_image`：从 `allowed_input_roots` 导入 PNG/JPEG/WebP 并返回 asset ID
- `edit_image`：使用 1-16 个已导入 asset ID 编辑图片
- `generate_video`：创建 1-2 个视频的异步 job，并立即返回 job URI
- `get_job`：读取 job 状态和结果
- `cancel_job`：取消排队/运行 job；运行中的上游响应返回后才会真正结束

资源：

- `image-client://capabilities`
- `image-client://profiles`
- `image-client://jobs`
- `image-client://jobs/{job_id}`
- `image-client://assets`
- `image-client://assets/{asset_id}`

图片结果会作为本地资源保存；不超过 5 MiB 的图片还可内联返回。远端视频 URL 以 `text/uri-list` 指针资源保存。可用 prompts 为 `compose_image_prompt` 和 `compose_edit_prompt`。

### Codex 配置示例

在个人 `~/.codex/config.toml` 或受信任项目的 `.codex/config.toml` 中使用绝对路径：

```toml
[mcp_servers.openai_image_client]
command = "/absolute/path/openai-image-client/.venv/bin/openai-image-client-mcp"
args = []
startup_timeout_sec = 30

[mcp_servers.openai_image_client.env]
OPENAI_IMAGE_MCP_CONFIG = "/absolute/path/openai-image-client/mcp-config.local.json"
OPENAI_IMAGE_OPENAI_API_KEY = "replace-with-secret"
OPENAI_IMAGE_GROK_API_KEY = "replace-with-secret"
```

配置文件包含明文环境变量时应限制为当前用户可读，并确保不提交到 Git。修改后重启 Codex 或新建任务，使 MCP server 重新加载。

### Claude Desktop 配置示例

macOS 文件通常为 `~/Library/Application Support/Claude/claude_desktop_config.json`，Windows 通常为 `%APPDATA%\Claude\claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "openai-image-client": {
      "command": "/absolute/path/openai-image-client/.venv/bin/openai-image-client-mcp",
      "args": [],
      "env": {
        "OPENAI_IMAGE_MCP_CONFIG": "/absolute/path/openai-image-client/mcp-config.local.json",
        "OPENAI_IMAGE_OPENAI_API_KEY": "replace-with-secret",
        "OPENAI_IMAGE_GROK_API_KEY": "replace-with-secret"
      }
    }
  }
}
```

关闭并重新打开 Claude Desktop 后生效。Windows 请将 `command` 改为 `.venv\Scripts\openai-image-client-mcp.exe` 的绝对路径。

### 远程 Streamable HTTP MCP

远程模式通过 Bearer Token 提供多租户 Streamable HTTP MCP。它与本地 `stdio` 并存：本机单用户优先使用 `stdio`；只有需要跨机器接入时才部署 HTTP 服务。

HTTP 服务默认绑定：

```text
http://127.0.0.1:8765/mcp       # MCP endpoint
http://127.0.0.1:8765/healthz   # 最小健康检查
```

必须让服务继续监听回环地址，并通过 Nginx/Caddy 等反向代理终止 TLS。不要直接将 `8765` 端口暴露到局域网或公网。

#### 租户认证配置

远程服务要求 `OPENAI_IMAGE_MCP_HTTP_AUTH_CONFIG` 指向独立 JSON 文件。在 POSIX 系统上该文件权限必须严格为 `0600`：

```bash
cp mcp-http-auth.example.json http-auth.local.json
chmod 600 http-auth.local.json
export OPENAI_IMAGE_MCP_HTTP_AUTH_CONFIG="$PWD/http-auth.local.json"
```

每个租户必须使用唯一 Bearer Token，并独立配置允许的 profiles、默认 profile、scopes、每分钟生成提交数、每分钟通用操作数和可选过期时间：

```json
{
  "tenants": [
    {
      "tenant_id": "codex-personal",
      "token_env": "OPENAI_IMAGE_MCP_TENANT_CODEX_TOKEN",
      "allowed_profiles": ["openai", "grok"],
      "default_profile": "openai",
      "scopes": [
        "mcp:use",
        "image:generate",
        "image:upload",
        "image:read",
        "image:cancel"
      ],
      "requests_per_minute": 30,
      "operations_per_minute": 300,
      "expires_at": null
    }
  ]
}
```

Scopes 的含义：

- `mcp:use`：所有 HTTP MCP 请求的基础权限
- `image:generate`：图片生成、编辑与视频生成
- `image:upload`：Base64 参考图上传
- `image:read`：job、asset、profile 和 capability 读取
- `image:cancel`：取消 job

`requests_per_minute` 范围为 1-1000，只限制图片生成、图片编辑和视频生成的任务提交。`operations_per_minute` 范围为 1-10000，限制每个携带有效租户 Token 的 `/mcp` HTTP 请求，包括初始化、ping、能力列表、未知方法、缺少业务 scope 的调用以及工具、资源和 prompt 请求。请求在认证前被拒绝或因请求体过大返回 `413` 时不计数。成功提交生成任务会消耗一次通用请求额度，再额外消耗一次生成提交额度。省略 `operations_per_minute` 时，默认值为 `max(60, requests_per_minute * 10)`，最高不超过 10000。

每个租户必须且只能配置 `token_env` 或 `token_sha256` 之一：

- `token_env` 引用包含原始 Token 的环境变量；Token 必须为 32-4096 个字符。
- `token_sha256` 只保存原始 Token 的 SHA-256 十六进制摘要，服务端无需保存原文。

生成 256-bit Token：

```bash
openssl rand -hex 32
```

计算已有 Token 的摘要时，不要把 Token 直接写进命令行。可以先通过 secret manager 或隐藏输入放入临时环境变量，再计算：

```bash
printf '%s' "$OPENAI_IMAGE_MCP_REMOTE_TOKEN" | shasum -a 256
```

如果使用 `token_sha256`，客户端仍保存并发送原始 Token；JSON 中只填写输出的 64 位摘要。认证文件、profile 配置及承载原始 Token 的环境文件都不得提交到 Git。

Token 轮换需要重启 HTTP 服务以重新加载配置。直接替换同一租户的 Token 是立即切换；如需重叠窗口，先增加一个使用临时 `tenant_id` 的新凭据，更新客户端后再删除旧租户凭据。可用 `expires_at`（Unix 时间戳）为过渡凭据设置硬过期时间。每个 Token 只分配必需的 profile、scope、生成 RPM 和通用操作 RPM。

#### 启动 HTTP 服务

```bash
export OPENAI_IMAGE_MCP_CONFIG="/etc/openai-image-client/mcp-config.json"
export OPENAI_IMAGE_MCP_HTTP_AUTH_CONFIG="/etc/openai-image-client/http-auth.json"
export OPENAI_IMAGE_MCP_HTTP_HOST="127.0.0.1"
export OPENAI_IMAGE_MCP_HTTP_PORT="8765"
export OPENAI_IMAGE_MCP_HTTP_PUBLIC_URL="https://mcp.example.com/mcp"
export OPENAI_IMAGE_MCP_HTTP_ALLOWED_HOSTS="mcp.example.com"
export OPENAI_IMAGE_MCP_HTTP_ALLOWED_ORIGINS="https://console.example.com"
export OPENAI_IMAGE_MCP_TENANT_CODEX_TOKEN="replace-with-a-long-random-token"
export OPENAI_IMAGE_OPENAI_API_KEY="replace-with-provider-secret"

openai-image-client-mcp-http --check-config
openai-image-client-mcp-http
```

先运行 `--check-config`；只有看到 `HTTP MCP configuration is valid` 后再启动常驻服务。检查会加载 profile、租户认证、HTTP settings，并初始化磁盘配额但不会监听端口。

HTTP 专用变量：

| 变量 | 默认值/范围 | 用途 |
| --- | --- | --- |
| `OPENAI_IMAGE_MCP_HTTP_AUTH_CONFIG` | 必填 | 权限严格为 `0600` 的租户认证 JSON 路径 |
| `OPENAI_IMAGE_MCP_HTTP_HOST` | `127.0.0.1` | Uvicorn 监听地址 |
| `OPENAI_IMAGE_MCP_HTTP_PORT` | `8765`，1-65535 | Uvicorn 监听端口 |
| `OPENAI_IMAGE_MCP_HTTP_ALLOW_NON_LOOPBACK` | `false` | 允许非回环监听；只用于防火墙后的可信私网代理链路 |
| `OPENAI_IMAGE_MCP_HTTP_PUBLIC_URL` | 必填 | 客户端访问的完整 HTTPS URL，路径必须为 `/mcp` 或以 `/mcp` 结尾；仅回环主机可使用 HTTP |
| `OPENAI_IMAGE_MCP_HTTP_ALLOWED_HOSTS` | `PUBLIC_URL` 中的 `host[:port]` | 逗号分隔的精确 Host 白名单，用于 DNS rebinding 防护 |
| `OPENAI_IMAGE_MCP_HTTP_ALLOWED_ORIGINS` | 空 | 逗号分隔的精确 HTTP(S) Origin 白名单，仅浏览器客户端需要 |
| `OPENAI_IMAGE_MCP_HTTP_MAX_BODY_BYTES` | 根据 `max_reference_bytes` 自动计算，最大 160 MiB | `/mcp` POST 请求体上限；不得小于参考图 Base64 所需容量 |
| `OPENAI_IMAGE_MCP_HTTP_MAX_RESOURCE_BYTES` | 16 MiB，1-32 MiB | 单次远程 asset 资源响应上限 |
| `OPENAI_IMAGE_MCP_HTTP_GLOBAL_WORKERS` | `8`，1-64 | 所有租户共享的生成 worker 数 |
| `OPENAI_IMAGE_MCP_HTTP_GLOBAL_QUEUE` | `100`，0-1000 | 所有租户共享的等待队列容量 |
| `OPENAI_IMAGE_MCP_HTTP_GLOBAL_IO_CONCURRENCY` | `8`，1-64 | 所有租户共享的阻塞 I/O 并发上限 |
| `OPENAI_IMAGE_MCP_HTTP_TENANT_IO_CONCURRENCY` | `2`，1-16 | 单租户阻塞 I/O 并发上限 |
| `OPENAI_IMAGE_MCP_HTTP_MAX_INFLIGHT_REQUESTS` | `128`，1-1024 | 同时处理的 `/mcp` HTTP 请求上限 |
| `OPENAI_IMAGE_MCP_HTTP_GLOBAL_ASSET_STORE_BYTES` | `max(10 GiB, max_asset_store_bytes)`，1 MiB-1 TiB | 所有 HTTP 租户合计的 asset 磁盘配额 |
| `OPENAI_IMAGE_MCP_HTTP_LOG_LEVEL` | `INFO` | `DEBUG`、`INFO`、`WARNING`、`ERROR` 或 `CRITICAL` |

`OPENAI_IMAGE_MCP_HTTP_PUBLIC_URL` 必须是客户端最终访问地址，外部路径可以带前缀。例如 `https://mcp.example.com/service/mcp` 合法，反向代理应将该外部路径映射到内部固定的 `/mcp`。`OPENAI_IMAGE_MCP_HTTP_ALLOWED_HOSTS` 应包含代理传入的精确公共 Host。原生 Codex 客户端通常不发送 `Origin`；只有浏览器 MCP 客户端才需要配置 `OPENAI_IMAGE_MCP_HTTP_ALLOWED_ORIGINS`。非回环浏览器 Origin 必须使用 HTTPS；上述地址对应的 Origin 是 `https://mcp.example.com`，不包含路径。两个白名单都不接受通配符，Origin 也不能是 `null`。

默认和推荐方式始终是监听 `127.0.0.1`。只有进程必须直接监听受防火墙保护的可信私网接口、且入口仍由可信反向代理控制时，才同时设置非回环 `OPENAI_IMAGE_MCP_HTTP_HOST` 与 `OPENAI_IMAGE_MCP_HTTP_ALLOW_NON_LOOPBACK=true`；不要用它直接暴露公网端口。本节的 systemd 和 Nginx 示例都保持回环监听。

该服务只验证预分配的静态 Bearer Token，不发布 OAuth discovery，也不提供动态客户端注册或交互式登录。客户端必须预先取得 Token；Codex 必须通过 `bearer_token_env_var` 显式配置承载原始 Token 的环境变量。

HTTP 模式按租户隔离 runtime、job、asset 和输出目录，并执行 profile allowlist、scope 与两层 RPM。当前部署仍要求**单进程、单节点**：runtime 与 job registry 位于进程内，不能使用多个 Uvicorn workers，也不能在没有共享状态和粘性会话的情况下做负载均衡。

远程客户端不能提交服务器本地文件路径。HTTP 模式不注册 `import_reference_image`，必须调用 `upload_reference_image`，通过 `data_base64` 上传参考图，并可传 `filename`。本地 `stdio` 模式仍可在 `allowed_input_roots` 内使用路径导入。

#### 容量、配额与持久化

- `OPENAI_IMAGE_MCP_HTTP_MAX_RESOURCE_BYTES` 只限制远程 `image-client://assets/{asset_id}` 的单次二进制响应；超限 asset 仍保存在磁盘并占用配额，但远程资源读取会失败。
- 生成任务使用共享 worker/queue，并同时执行自动的每租户在途任务上限。单租户配置可使用全部 `workers + queue`；多租户配置中每个租户最多占用 `min(workers, max(1, workers + queue - 1))` 个运行或排队槽位，因此总容量大于 1 时，一个租户不能耗尽全局队列。`1 worker / 0 queue` 没有可预留槽位，必须增加 queue 才能让另一租户在首个任务运行时继续提交。上传、asset/resource 读取和状态操作等阻塞工作同时受全局及每租户 I/O 并发限制。`OPENAI_IMAGE_MCP_HTTP_MAX_INFLIGHT_REQUESTS` 再限制整个 `/mcp` HTTP 请求层的在途数量。
- 每个租户受 `max_asset_store_bytes`（或 `OPENAI_IMAGE_MCP_MAX_ASSET_STORE_BYTES`）限制，所有租户还共同受 `OPENAI_IMAGE_MCP_HTTP_GLOBAL_ASSET_STORE_BYTES` 限制；任一配额耗尽都会拒绝新 asset 写入。
- job registry、job TTL 元数据和两层 RPM 窗口都在内存中，服务重启会丢失 job 状态并重置限流窗口。正常运行时，生成结果会在 job 取消、TTL 过期或注册表淘汰时自动删除并释放配额；独立上传的参考图以及异常退出遗留的孤儿 asset 不会自动清理，并会跨重启继续占用每租户及全局配额。

应监控输出目录容量并制定保留策略。`MCPRuntime.prune_assets()` 可按 job TTL 回收异常退出遗留的孤儿 asset，但当前不会自动调度；生产部署可在停服维护或受控运维任务中调用。若手动清理，先停止服务，成对删除不再需要的 asset 数据与对应 metadata，再重新启动以扫描现存字节并重算全局及每租户配额。不要在服务运行时直接删除文件，否则内存计数不会同步。

#### systemd 单 worker 服务

生产环境应使用专用无登录用户。下面假设代码和虚拟环境位于 `/opt/openai-image-client`，profile/auth 配置位于 `/etc/openai-image-client`，输出写入 `/var/lib/openai-image-client`：

```bash
sudo useradd --system --home /var/lib/openai-image-client \
  --shell /usr/sbin/nologin openai-image
sudo install -d -o root -g openai-image -m 0750 /etc/openai-image-client
sudo install -d -o openai-image -g openai-image -m 0700 /var/lib/openai-image-client
sudo install -o openai-image -g openai-image -m 0600 \
  mcp-config.local.json /etc/openai-image-client/mcp-config.json
sudo install -o openai-image -g openai-image -m 0600 \
  http-auth.local.json /etc/openai-image-client/http-auth.json
sudo install -o root -g root -m 0600 /dev/null \
  /etc/openai-image-client/mcp-http.env
```

`/etc/openai-image-client/mcp-http.env` 包含服务配置和原始 secrets，必须保持 root 所有且权限为 `0600`：

```dotenv
OPENAI_IMAGE_MCP_CONFIG=/etc/openai-image-client/mcp-config.json
OPENAI_IMAGE_MCP_OUTPUT_DIR=/var/lib/openai-image-client
OPENAI_IMAGE_MCP_HTTP_AUTH_CONFIG=/etc/openai-image-client/http-auth.json
OPENAI_IMAGE_MCP_HTTP_HOST=127.0.0.1
OPENAI_IMAGE_MCP_HTTP_PORT=8765
OPENAI_IMAGE_MCP_HTTP_PUBLIC_URL=https://mcp.example.com/mcp
OPENAI_IMAGE_MCP_HTTP_ALLOWED_HOSTS=mcp.example.com
OPENAI_IMAGE_MCP_TENANT_CODEX_TOKEN=replace-with-a-long-random-token
OPENAI_IMAGE_OPENAI_API_KEY=replace-with-provider-secret
```

将以下单元保存为 `/etc/systemd/system/openai-image-mcp.service`；`ExecStart` 直接使用项目入口，该入口固定启动一个 Uvicorn worker：

```ini
[Unit]
Description=OpenAI Image Client Streamable HTTP MCP
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=openai-image
Group=openai-image
WorkingDirectory=/opt/openai-image-client
EnvironmentFile=/etc/openai-image-client/mcp-http.env
ExecStartPre=/opt/openai-image-client/.venv/bin/openai-image-client-mcp-http --check-config
ExecStart=/opt/openai-image-client/.venv/bin/openai-image-client-mcp-http
Restart=on-failure
RestartSec=5s
TimeoutStopSec=45s
UMask=0077

NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectClock=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictRealtime=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
AmbientCapabilities=
StateDirectory=openai-image-client
StateDirectoryMode=0700
ReadWritePaths=/var/lib/openai-image-client

[Install]
WantedBy=multi-user.target
```

确保 `/opt/openai-image-client` 由 root 管理且服务用户不可写，然后启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now openai-image-mcp.service
```

不要改成多 worker，也不要把认证文件或 EnvironmentFile 放进代码目录。修改 Token、profile 或租户配置后重启服务。

#### Nginx TLS 反向代理

以下配置假定证书已由 ACME/运维系统管理：

```nginx
# 放在 nginx.conf 的 http {} 中、server {} 之外
limit_req_zone $binary_remote_addr zone=mcp_per_ip:10m rate=60r/m;

server {
    listen 443 ssl http2;
    server_name mcp.example.com;

    ssl_certificate     /etc/letsencrypt/live/mcp.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mcp.example.com/privkey.pem;

    client_max_body_size 32m;

    location = /healthz {
        proxy_pass http://127.0.0.1:8765/healthz;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_connect_timeout 5s;
    }

    location = /mcp {
        limit_req zone=mcp_per_ip burst=20 nodelay;
        limit_req_status 429;

        proxy_pass http://127.0.0.1:8765/mcp;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Connection "";

        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        proxy_connect_timeout 5s;
        proxy_read_timeout 700s;
        proxy_send_timeout 700s;
    }

    location / {
        return 404;
    }
}
```

若公共入口带路径前缀，例如 `https://mcp.example.com/service/mcp`，设置 `OPENAI_IMAGE_MCP_HTTP_PUBLIC_URL=https://mcp.example.com/service/mcp` 和 `OPENAI_IMAGE_MCP_HTTP_ALLOWED_HOSTS=mcp.example.com`，再将主示例中的外部 location 改为下面的路径；`proxy_pass` 仍指向回环 `/mcp`，并保留主示例中的 `limit_req`、请求头、buffer 和 timeout 指令：

```nginx
location = /service/mcp {
    # Keep the remaining directives identical to the main /mcp location.
    proxy_pass http://127.0.0.1:8765/mcp;
}
```

`/mcp` 需要支持 `GET`、`POST` 和 `DELETE`，且响应可能保持流式连接。默认 20 MiB 参考图编码为 Base64 后约为 26.7 MiB；`client_max_body_size 32m` 为 JSON 和协议字段留出余量，并高于应用默认的约 27.7 MiB 请求体上限。若调整 `max_reference_bytes` 或 `OPENAI_IMAGE_MCP_HTTP_MAX_BODY_BYTES`，应同步调整代理限制。不要在访问日志中记录 `Authorization`。

应用内 `requests_per_minute` 限制生成提交，`operations_per_minute` 限制每个携带有效租户 Token 的 `/mcp` 请求。Nginx 的 IP 级 `limit_req` 在租户身份解析前生效，可限制无效 Token、畸形协议请求和突发流量。可按实际客户端数量调整 `rate` 和 `burst`。如果 Nginx 前面还有负载均衡器，应只对可信代理配置 Real IP，否则所有客户端可能共享同一个限流键。

健康检查不需要 MCP 协议头，也不应返回租户、profiles、路径、Token 或上游状态：

```bash
curl -fsS https://mcp.example.com/healthz
```

不要使用 `/mcp` 作为负载均衡健康探针。Bearer Token 不能替代 TLS。

#### Codex 远程接入

静态 Bearer 模式没有 OAuth discovery，Codex 不能只凭 URL 自动登录。让原始 Token 进入 Codex 进程环境，再注册远程 MCP；不要把 Token 原文写入 `config.toml`：

```bash
export OPENAI_IMAGE_MCP_REMOTE_TOKEN="replace-with-the-tenant-token"

codex mcp add openai-image-client-remote \
  --url https://mcp.example.com/mcp \
  --bearer-token-env-var OPENAI_IMAGE_MCP_REMOTE_TOKEN
```

等价的 Codex TOML：

```toml
[mcp_servers.openai_image_client_remote]
url = "https://mcp.example.com/mcp"
bearer_token_env_var = "OPENAI_IMAGE_MCP_REMOTE_TOKEN"
startup_timeout_sec = 30
tool_timeout_sec = 700
```

修改 Token 后应重启 Codex 或新建任务。`bearer_token_env_var` 只保存变量名；Codex 桌面应用必须从其启动环境或系统 secret manager 获得该变量。

## 安全清单

- 不提交 `config.local.json`、`mcp-config.local.json`、`http-auth.local.json`、`mcp-http.env` 或客户端中含 secrets 的配置
- 私有配置执行 `chmod 600`，输出目录仅授权当前用户
- MCP 只允许必要的 `allowed_input_roots`
- profile 固定 `base_url` 并设置 `allowed_models`
- 远程 MCP 为每个租户使用唯一 Token、最小 scopes/profile/双层 RPM，并将认证文件设为 `0600`
- Web 和 HTTP MCP 保持默认回环监听；远程使用时通过可信反向代理增加 TLS 和入口认证
- 监控每租户及全局 asset 配额；为上传资产和异常退出遗留资产安排受控清理
- 只精确放行需要的私网上游主机
- 泄露或曾写入仓库/浏览器存储的 Key 应立即轮换

## 验证

```bash
# Python/MCP tests
python -m pytest -q

# Frontend build and static checks
npm install
npm run check:frontend
```

测试不会替代真实提供商的额度、模型权限与返回格式验证。

## License

[MIT](LICENSE)
