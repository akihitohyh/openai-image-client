# OpenAI Compatible Image Client

[English README](README_EN.md)

一个面向 OpenAI Images API、Google Gemini 原生生图协议与 xAI Grok Imagine 兼容服务的图片/视频客户端，提供两种入口：

- 本地 Web UI（主要入口）
- Python CLI

支持文生图、图生图、文生视频和图生视频。OpenAI 生图支持 GPT Image 2.6 的复杂度、GIF 动图和 SVG 矢量图输出（目前只在 Web UI 中提供）。

> 从这一版开始移除了 MCP server（stdio 与多租户 HTTP 两种）。如仍需要，请使用之前的版本。

## 功能概览

- OpenAI-compatible `images/generations` 与 `images/edits`
- Web UI 支持 Gemini 原生 `models/{model}:generateContent` 文生图与图生图
- Grok Imagine 图片与异步视频任务
- OpenAI/Gemini/Grok 协议感知的 Web 控件
- Web 异步 job、状态轮询、取消和当前页面任务历史
- Web UI 默认支持跨来源 iframe 嵌入，并可按父页面 origin 收紧
- Base64 或远端 URL 媒体结果
- 私网上游默认拒绝，支持精确主机白名单

## 项目结构

```text
.
├── generate_image.py             # CLI
├── web_app.py                    # Web 服务与共享生成实现
├── web/                          # 本地构建的 Web 前端
├── image_client_core/            # Web 与 CLI 共用的模型判断、端点与响应处理
├── config.json.example           # CLI 示例配置
├── requirements.txt              # CLI/Web 依赖
└── requirements-dev.txt          # 测试与打包依赖
```

## 安装

CLI 和 Web：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
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

### Gemini 原生生图

在 Web UI 中选择“Gemini 生图”，填写 Base URL、API Key 和模型名称。模型名称不会固定，可以填写官方模型或兼容代理提供的模型标识。

Base URL 支持以下形式：

```text
https://generativelanguage.googleapis.com/v1beta
https://gemini-proxy.example/v1beta
https://gemini-proxy.example/v1beta/models/custom-model:generateContent
```

未包含 API 版本的 Base URL 会自动补 `/v1beta`。请求同时携带 `x-goog-api-key` 和 `Authorization: Bearer`，用于兼容 Google 官方接口和使用 Bearer Token 的 Gemini 协议代理。跨来源重定向仍会被拒绝，防止凭据被转发到其他主机。

文生图和图生图都使用原生 `generateContent` 请求。提示词和参考图放入 `contents[].parts`，参考图使用 `inlineData`；生成配置使用 `responseModalities: ["TEXT", "IMAGE"]` 以及 `imageConfig.aspectRatio` 和 `imageConfig.imageSize`。

Gemini 与“其他模型”共享以下尺寸选项，4K 档不会超过横向 `3840x2160` 或纵向 `2160x3840`：

| 比例 | 1K | 2K | 4K |
| --- | --- | --- | --- |
| 1:1 | 1024x1024 | 2048x2048 | 2160x2160 |
| 3:4 | 864x1152 | 1728x2304 | 2160x2880 |
| 4:3 | 1152x864 | 2304x1728 | 2880x2160 |
| 16:9 | 1312x736 | 2624x1472 | 3840x2160 |
| 9:16 | 736x1312 | 1472x2624 | 2160x3840 |
| 2:3 | 832x1248 | 1664x2496 | 2160x3240 |
| 3:2 | 1248x832 | 2496x1664 | 3240x2160 |
| 21:9 | 1568x672 | 3136x1344 | 3808x1632 |

界面显示实际像素参考值，原生请求发送对应的 `1K`、`2K` 或 `4K` 档位。生成数量 `n` 大于 1 时，服务按顺序调用上游 `n` 次，每次取第一张有效图片，因此会产生 `n` 次上游请求和相应计费。Gemini 原生协议当前只在 Web UI 和 Web 异步任务 API 中启用；CLI 的协议范围保持不变。

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
| Gemini 参考图 | 最多 16 张，仍受 30 MiB 总限制 |
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

## 安全清单

- 不提交 `config.local.json` 或其他含 secrets 的配置
- 私有配置执行 `chmod 600`，输出目录仅授权当前用户
- Web 保持默认回环监听；远程使用时通过可信反向代理增加 TLS 和入口认证
- 只精确放行需要的私网上游主机
- 泄露或曾写入仓库/浏览器存储的 Key 应立即轮换

## 验证

```bash
# Python tests
python -m pytest -q

# Frontend build and static checks
npm install
npm run check:frontend
```

测试不会替代真实提供商的额度、模型权限与返回格式验证。

## License

[MIT](LICENSE)
