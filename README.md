# OpenAI Compatible Image Client

[English README](README_EN.md)

一个简洁的 OpenAI / Grok Imagine 图片与视频生成客户端，包含两种使用方式：

1. Python CLI 脚本
2. 本地 Web UI

适合接兼容 OpenAI Images API、xAI Grok Imagine API 的中转服务、自建代理或网关。

---

## 特性

- 兼容 `POST /v1/images/generations`、`POST /v1/images/edits` 与 xAI `POST /v1/videos/generations`
- Web UI 按作业协议分为 `OpenAI 生图`、`Grok 生图`、`Grok 视频`
- 模型下拉框会随协议自动切换可选项
- OpenAI 生图支持 `gpt-image-2`
- Grok 生图支持 `grok-imagine`、`grok-imagine-edit`、`grok-imagine-image`、`grok-imagine-image-quality`
- Grok 视频支持 `grok-imagine-video-1.5`、`grok-imagine-video`
- CLI 的 `protocol=auto` 会按模型名或 xAI Base URL 自动选择协议，也可强制 `openai` / `grok`
- 支持文生图 / 图生图 / 文生视频 / 图生视频
- 图生图支持参考图上传、拖拽、预览与移除
- 支持填 API 根地址或完整图片接口地址
- 同时提供 CLI 和 Web UI
- Web 设置只保存在浏览器本地 `localStorage`
- 后端不保存前端设置、不做备份
- 支持上游返回 `b64_json` 或 `url`
- Grok 视频会自动创建视频任务并轮询 `request_id`，完成后返回视频 URL

---

## 目录说明

```text
.
├── generate_image.py      # CLI 生图脚本
├── web_app.py             # 本地 Web 服务
├── web/                   # 前端页面
├── config.json.example    # 示例配置
└── requirements.txt
```

---

## 1. CLI 脚本

### 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 配置

建议复制示例配置到本地私有文件：

```bash
cp config.json.example config.local.json
```

然后编辑 `config.local.json`，填写：

- `base_url`
- `api_key`
- `prompt`

脚本会优先读取：

1. `config.local.json`
2. `config.json`

### 运行

```bash
python generate_image.py
```

也可以完全通过命令行参数传入：

```bash
python generate_image.py \
  --base-url "https://your-api-host.com" \
  --api-key "your_api_key" \
  --protocol openai \
  --prompt "A cute orange cat astronaut"
```

Grok Imagine 文生图：

```bash
python generate_image.py \
  --base-url "https://api.x.ai/v1" \
  --api-key "your_xai_api_key" \
  --protocol grok \
  --model "grok-imagine-image" \
  --prompt "A cinematic futuristic city at sunset" \
  --aspect-ratio "16:9" \
  --resolution "2k"
```

图生图参考图：

```bash
python generate_image.py \
  --mode edit \
  --protocol grok \
  --model "grok-imagine-image" \
  --prompt "保留主体构图，改成赛博朋克夜景" \
  --image "./reference.png"
```

Grok Imagine 文生视频：

```bash
python generate_image.py \
  --base-url "https://api.x.ai/v1" \
  --api-key "your_xai_api_key" \
  --protocol grok \
  --mode video \
  --model "grok-imagine-video" \
  --prompt "A cinematic drone shot over a futuristic city at sunset" \
  --aspect-ratio "16:9" \
  --resolution "720p" \
  --duration 10 \
  --timeout 600
```

Grok Imagine 图生视频：

```bash
python generate_image.py \
  --mode edit \
  --protocol grok \
  --model "grok-imagine-video-1.5" \
  --prompt "Animate the subject with subtle camera movement and natural motion" \
  --image "./reference.png" \
  --timeout 600
```

---

## 2. 本地 Web UI

### 特点

- Neo-Brutalism 风格页面
- 作业协议使用分段按钮：`OpenAI 生图`、`Grok 生图`、`Grok 视频`
- 模型使用下拉框，并按当前协议展示对应模型
- 支持文生图 / 图生图 / 文生视频 / 图生视频
- 参考图支持上传、拖拽上传、本地预览与移除
- 图片历史保留在同一个结果区
- 点击图片或视频后右侧显示详细信息与提示词
- 支持图片放大预览与视频播放
- 多图请求会拆成串行单任务执行
- 长耗时图片与视频请求改为异步任务轮询，减少浏览器侧 `Failed to fetch`
- 所有前端设置自动保存在当前浏览器本地

### 启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python web_app.py
```

默认访问地址：

```text
http://127.0.0.1:8000
```

### Windows exe

在 Windows 上安装 Python 3.10+ 后，双击运行：

```text
build_windows.bat
```

构建完成后会生成：

```text
dist\OpenAIImageClient.exe
```

双击 `OpenAIImageClient.exe` 会启动本地 Web 服务，并自动打开浏览器。默认地址仍为：

```text
http://127.0.0.1:8000
```

如果 8000 端口被占用，程序会自动尝试后续端口。也可以在命令行指定：

```bat
OpenAIImageClient.exe --port 8080
OpenAIImageClient.exe --no-open-browser
```

### 说明

- `Base URL` 可填 API 根地址，例如 `https://your-api-host.com`
- 也可直接填完整接口地址，例如 `https://your-api-host.com/v1/images/generations`
- Web UI 的 `Protocol` 分为 `OpenAI 生图`、`Grok 生图`、`Grok 视频`
- `Model` 是下拉框，选项会随当前协议自动变化
- CLI 的 `--protocol auto` 会按模型名或 xAI Base URL 自动识别协议
- 图生图模式会自动请求兼容的 `images/edits` 接口；OpenAI/GPT 使用 multipart，Grok 使用 JSON
- Grok 视频会自动请求 `videos/generations`，前端结果区会显示视频播放器
- 图生图参考图只保存在当前浏览器内存中，后端不做备份
- 长耗时任务会先创建 job，再由前端轮询结果
- 如果上游返回 `b64_json`，页面会直接显示
- 如果上游返回 `url`，后端会先下载图片，再返回给前端显示
- 后端不会替你保存前端输入的配置

---

## 更新记录

### 2026-07-09
- 新增 Grok Imagine 视频协议支持，包含文生视频与图生视频
- Web UI 的协议改为 `OpenAI 生图`、`Grok 生图`、`Grok 视频` 三段式作业模式
- `Model` 改为下拉框，并按当前协议展示对应模型
- 协议切换时同步更新下方文案、参考图数量限制、视频选项与提交按钮文案
- README 移除在线试用入口和 Web UI 截图资源

### 2026-04-28
- 新增图生图模式
- 支持参考图上传、拖拽上传、本地预览与移除
- 图生图结果详情中展示模式、参考图与提示词
- 后端新增 OpenAI-compatible `images/edits` 请求支持
- 保持多图串行任务、单独计算等待时间与超时
- 文生图 / 图生图长耗时请求改为异步轮询，降低 Cloudflare / 浏览器长连接中断导致的 `Failed to fetch`

---

## 致谢

- 🔗 [Linux DO](https://linux.do)
