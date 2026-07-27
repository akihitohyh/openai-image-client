# 优化记录 · 2026-07-26

对 `web_app.py` / `generate_image.py` / `web/app.js` / `openai_image_client/` 做的一轮审查与修复。
分四批推进：**A 安全与阻塞 → B 性能 → C 前端 → D 去重重构**。

**验证结果**：145 个 Python 测试 + 9 个前端测试 + 26 项 jsdom 行为检查全部通过，`pyflakes` 无告警。

---

## 目录

- [新增：`image_client_core/` 共享模块](#新增image_client_core-共享模块)
- [A · 安全与阻塞](#a--安全与阻塞)
- [B · 性能](#b--性能)
- [C · 前端](#c--前端)
- [D · 去重重构与四个单边 bug](#d--去重重构与四个单边-bug)
- [验证方法](#验证方法)
- [有意未改动的部分](#有意未改动的部分)
- [已知遗留问题](#已知遗留问题)
- [开发环境备注](#开发环境备注)

---

## 新增：`image_client_core/` 共享模块

`web_app.py` 与 `generate_image.py` 原本各自持有 **13 个字节级完全相同**的函数，另有 9 个已经分叉——而分叉的每一处都是"只有 `web_app.py` 被修好了"。新增的 `image_client_core/` 是这些逻辑的唯一实现。

| 文件 | 内容 |
| --- | --- |
| `image_client_core/endpoints.py` | `normalize_base_url` / `join_url` / `candidate_endpoints` / `video_poll_endpoint_from_generation` / `KNOWN_API_ENDPOINTS` |
| `image_client_core/models.py` | Grok 模型集与画幅集、`parse_size`、`is_gpt_image_2` / `is_grok_image_model` / `is_grok_video_model`、比例与分辨率推导 |
| `image_client_core/responses.py` | 有上限的响应读取与缓存、`decode_base64_limited`、全部诊断与重试判定函数 |

**约束**：该包只允许依赖标准库和 `requests`。独立 Web 应用的依赖仅有 `requests`（见 `requirements.txt`），引入这个包不能破坏这一点——已用只装 `requests` 的干净虚拟环境验证过。

**行数变化**

```
web_app.py         2558 → 2256   (-302)   移除 23 个函数 + 9 个常量
generate_image.py  1164 →  949   (-215)   移除 19 个函数 + 9 个常量
image_client_core/                +395
                          净减 122 行
```

**依赖方向修正**：`openai_image_client/runtime.py:84` 原来是 `import web_app` 读取 Grok 常量（MCP 适配包依赖 Web 入口，方向是反的），现改为从 `image_client_core.models` 读取。

**防回归守卫**：`tests/test_image_client_core.py::test_shared_helpers_are_not_redefined` 用 AST 检查两个入口文件是否又出现了共享函数的本地定义。这条测试比任何单个 bug 修复都重要——它防的正是"只修一半"这个反复发生的模式。

---

## A · 安全与阻塞

### A1 · 未认证远程 DoS — `web_app.py:1932` `_check_api_access`

`hmac.compare_digest` 拒绝含非 ASCII 字符的 `str`：

```python
>>> hmac.compare_digest('ü', 'abc')
TypeError: comparing strings with non-ASCII characters is not supported
```

一个 `Authorization: Bearer ü` 就能让异常逃出 `do_GET`/`do_POST`，客户端收到**连接重置而非 HTTP 响应**，stderr 刷 traceback。同时任何非 ASCII 的服务令牌都会永远认证失败。

**修复**：编码为字节后再比较。

### A2 · `.gitignore` 缺口

- 新增 `*.env`。`README.md` 明确说明 `/etc/openai-image-client/mcp-http.env` "含服务设置和明文密钥"，但 `.env` 只匹配字面名、`.env.*` 只匹配 `.env.foo` 前缀形式，`mcp-http.env` 两者都不匹配。
- `.env.example` 仍可被跟踪（`!.env.example` 否定规则在后，已用 `git check-ignore` 逐个验证）。

> **核实结论：仓库中没有泄露任何密钥。** `git ls-files` 只有 13 个文件；`config.local.json*` 被 `.gitignore` 覆盖且全历史对象中搜索不到；`venv/`、`__pycache__/`、`.DS_Store`、`*.bak-*` 均未被跟踪。无需做历史清理。

### A3 · 取消任务是假的 — `web_app.py:116` / `:129` / `:1580`

`future.cancel()` 对已启动的任务无效，而 Grok 视频轮询循环（最长 600 秒）**从不检查** `job["cancel_requested"]`。`cleanup_jobs` 的 deadline 同理——只改状态，线程照跑。

**修复**：新增 `JobCancelledError` 与 `cancellable_sleep()`（0.5 秒分片），把 `should_cancel` 回调贯穿
`process_request_payload` → `generate_videos` / `generate_images` / `edit_images` → `request_grok_video_api` / `request_image_api`，
替换轮询循环里的裸 `time.sleep`。

同时修掉 `cancel_async_job` 中 `rmtree` 与 worker `write_bytes` 的竞争：未启动的任务直接撤单并清理，已启动的任务由 worker 自己在收到 `JobCancelledError` 后清理，避免产生孤儿文件。

**实测对比**（`MAX_CONCURRENT_JOBS = 2`）

| | 修复前 | 修复后 |
| --- | --- | --- |
| 取消后 worker 线程 | 仍运行，最长 600 秒 | **0.21 秒**内释放 |
| 新提交的任务 | 被接受但**永久卡在 `queued`** | 正常执行 |
| 硬 503 | 再堆积 8 个僵尸任务后出现 | — |

> 早期报告里写的"后续全部 503"不准确。真实症状更隐蔽：任务被**接受**但永远不启动——比直接返回 503 更难排查。

### A4 · 资产与配额永久泄漏 — `openai_image_client/`

两个叠加的问题：

1. `assets.py` **完全没有删除路径**。`StorageQuota.release` 仅在写入失败的回滚分支被调用，配额单调递增，且重启后从磁盘重新累加。撞上上限后服务将永久无法生成。
2. `jobs.py` 在任务被取消时不给 `current.result` 赋值，但 `work()` 早已把资产写盘并计入配额——asset ID 只存在于被丢弃的返回值里，**不可寻址、不可删除、永久占额**。而 `server.py` 中客户端一断开就 `cancel(job_id)`，所以这是常态路径而非边缘情况。

**修复**

| 位置 | 内容 |
| --- | --- |
| `assets.py:415` `delete()` | 删除内容与元数据，释放 `_stored_bytes` 与共享配额；计数器不会跌破 0；元数据指向 store 外时拒绝删除 |
| `assets.py:457` `delete_many()` | 批量删除，逐个容错 |
| `assets.py:466` `prune()` | 按 TTL 与容量上限淘汰（供运维调用，见下） |
| `jobs.py:94` `result_discarder` | 新增构造参数 |
| `jobs.py:120` `_discard_results()` | 覆盖三条丢弃路径：**取消**、**TTL 过期**、**`max_jobs` 挤出**；在锁外调用，因为删除是文件 I/O |
| `runtime.py:45` `discard_result_assets()` | 接线：从 job result 中提取 asset_id 并删除 |
| `runtime.py:137` `prune_assets()` | 显式暴露给运维调度；**不自动调用**，仅用于回收非正常退出留下的孤儿资产 |

**验证是承重的**——把钩子关掉重跑同一个测试：

```
关闭 result_discarder（= 旧代码）: 磁盘残留 2 个文件, 配额永久占用 77 字节
开启 result_discarder（= 新代码）: 文件 0, 配额 0
```

---

## B · 性能

### B5 · 启用 HTTP/1.1 keep-alive — `web_app.py:1883`

`protocol_version` 原为默认的 `HTTP/1.0`，每个响应都关闭 TCP。首屏的 `index.html`(17K) + `app.js`(38K) + `styles.css`(16K) + favicon + **每一张生成图**都要各自新建一个 TCP 连接和一个线程。

同时设置 `timeout = 30` 防 slowloris——原先没有超时，空连接会永久占住一个线程。

> **keep-alive 的尖刺**：任何在**读取请求体之前**就应答的分支，若不关闭连接，剩余请求体会被当成下一个请求的起始行解析，导致连接错位。
>
> 原作者已在 411/413 两处写了 `close_connection = True`（在 HTTP/1.0 下是空操作，现在变成承重的），但另有 5 处缺失，均已补上：未知 POST 路径、认证失败、`Content-Length` 非法（两处）、以及 `do_DELETE`（`web_app.py:2057`–`2080`、`:2149`）。
>
> `end_headers`（`web_app.py:1914`）统一在 `close_connection` 为真时发送 `Connection: close`。SSE 响应没有 `Content-Length`，在 HTTP/1.1 下必须显式关闭连接来界定响应边界（`web_app.py:1739`）。

### B6 · 缓存不变的生成资产 — `web_app.py:108` / `:1897` / `:1962`

`end_headers` 原先对所有 `/api/` 前缀路径贴 `no-store`，而资产路径正是 `/api/assets/<job-uuid>/image-1.png`——内容在任务存续期内不变。配合前端每个任务至少 4 次的全量重渲染，导致整个历史画廊反复重新下载。

**修复**：`/api/assets/` 改为 `private, max-age=3600, immutable` + `ETag`/`304`。

顺带修掉 `_send_asset` 的三个问题：

1. 大小检查原先在 `read_bytes()` **之后**，等于毫无保护 → 改为先 `stat()`
2. 整文件读入内存 → 改为 `shutil.copyfileobj` 流式输出
3. 资产被并发删除（取消/过期清理）时 `read_bytes` 抛 `FileNotFoundError` 打断连接 → 改为返回 404

### B7 · 消除重复校验 — `web_app.py:1409`

`process_request_payload` 会再跑一次 `validate_request_payload`，而该校验会把**每张参考图完整 base64 解码一遍**以统计大小（上限 30MB）。异步路径在入队时已经校验过。

**修复**：新增 `already_validated` 开关，仅异步 worker 使用。同步路径与 MCP 路径仍依赖内部校验，因此不能直接删除。

实测异步路径解码次数 **3 → 2**（`do_POST` 校验 + `build_edit_image_files`）。

### B8 · 复用 HTTP 连接 — `web_app.py:298`

`safe_http_request` 原先调用模块级 `requests.request`，它每次都会新建并丢弃一个 `Session`，也就是每次都要重做 TLS 握手。视频轮询对同一主机每任务要握手几十次。

**修复**：`outbound_session()` 按线程复用 `Session`（`Session` 非线程安全，故用 `threading.local`），挂载 `pool_maxsize=8`、`max_retries=0` 的 `HTTPAdapter`。重定向仍由 `safe_http_request` 自己逐跳做 SSRF 校验。

### 实测

```
1) 同一 socket 上连续两个请求      -> HTTP/1.1 200 OK ×2   （keep-alive 生效）
2) 资产 Cache-Control              -> private, max-age=3600, immutable
   带 ETag 复访                    -> 304
3) index.html                      -> no-store             （未被误缓存）
4) 带体 POST 到未知路径            -> 404 + Connection: close，socket 确实关闭
5) 资产被并发删除                  -> 404                  （不是连接重置）
SSE                                -> Connection: close 一次，Cache-Control 不重复，4 个事件齐全
```

---

## C · 前端

| 编号 | 位置 | 问题 |
| --- | --- | --- |
| **C10** | `web/app.js:245` `setHealthStatus` | 加了 `text-emerald-600`/`text-rose-600` 但没移除 `index.html` 硬写的 `text-slate-500`。编译后的 `styles.css` 中 slate 排在两个颜色类**之后**（偏移 11611 vs 10958/11125），同优先级下后者胜出——**健康指示灯无论后端在线与否都是灰色**。 |
| **C13** | `web/app.js:672` `historyEntrySignature` | `renderHistory` 用 `replaceChildren` 每次重建全部 `<img>`/`<video>`，一个任务至少调用 4 次，造成可见闪烁与（B6 之前的）全量重新下载。改为按 `entry.id` 缓存节点 + 签名比对，未变化的条目复用同一节点（`replaceChildren` 移动既有节点不会重新发起请求）。elapsed 由计时器就地更新，故不参与签名。 |
| **C11a** | `web/app.js:995` `closeLightbox` | 只 `pause()` 不清 `src`——远程 mp4 关闭后继续缓冲，data URL 预览一直保持解码状态。 |
| **C11b** | `web/app.js:443` `buildEditImagePayloads` | 参考图原先以 base64 常驻整个会话（30MB 上限 → 约 41MB 字符串），预览 `<img>` 再持有一份解码副本，`createPayload` 与 `JSON.stringify` 各再拷一份。改为持有 `File` + `blob:` 预览，base64 只在提交瞬间生成；读取改为 `Promise.all` 并发（原为循环内串行 `await`）。 |
| **C11c** | `web/app.js:767` `abortableDelay` | `{ once: true }` 只在 abort **真的触发**时才移除监听器；正常定时器返回的路径上监听器会留在 signal 上，长任务轮询数百次即堆积数百个闭包。 |
| **C12** | `web/app.js` `pollJob` | 本地 deadline 到了直接 `throw`，**不通知服务端**——后端任务继续占用 worker 槽位，并把没人会读取的资产保留到 TTL 过期。改为先 `await requestServerCancellation`。 |
| **#3** | `web/app.js:846` | `abort()` 同步 reject 轮询 → `submitGeneration` 的 `finally` 在下一个微任务就把取消按钮恢复成"取消任务"，抹掉一行之前刚设的"正在取消..."，而服务端取消请求还有最多 3 秒才返回。改为把取消 promise 挂在 task 上，`finally` 等它结束后再恢复按钮。 |
| **#7** | `web/app.js:417` `handleIncomingFiles` | `change` 分支有 `try/catch`，`drop` 分支没有——`FileReader` 失败会变成未处理的 promise rejection，用户什么也看不到。两者统一走同一个 helper。 |
| **#8** | `web/app.js:164` `isKnownSizeOption` | `size` 是唯一未经校验就恢复的设置，陌生值会让 `els.size.value` 变成空字符串，最终提交 `size: ""`。改为对照 `<option>` 校验。 |
| **#14** | `web/app.js` `buildEmptyHistory` | "清空历史"后空状态退化成纯文字，丢失 `index.html` 原有的 SVG 图标。改为克隆原始节点。 |

**性能附带项**：生成图片加 `loading="lazy"` / `decoding="async"`；视频 `preload` 从 `'metadata'` 改为 `'none'`。

> ⚠️ **`preload='none'` 是一个取舍**：历史里 12 个视频不再各发一次 range 请求，代价是视频卡片不再自动显示首帧，需点击才加载。如果更看重首帧可见性，改回 `'metadata'` 或加 `poster` 都只是几行改动。

**重构引入并已修复的风险**：`createPayload` 变为 `async` 后，`activeTask` 赋值前多出一个窗口，连点提交会并发起两个任务——新增 `isPreparingSubmission` 标记堵住。

---

## D · 去重重构与四个单边 bug

以下四个 bug 的共同点：**`web_app.py` 早已修好，`generate_image.py` 因为持有独立副本而一直没修**。抽取到 `image_client_core/` 后自动一并修复。

### 1 · 严格 base64 — `generate_image.py:186` `save_b64_image`

`base64.b64decode()` 默认会**静默丢弃所有非字母表字符**。上游返回的 HTML 错误页或被截断的载荷会被"成功"解码成垃圾，然后当作正常图片写进 `.png`。

实测一个真实的 nginx 504 页面：

```
<!DOCTYPE html><html><head><title>504 Gateway Time-out</title>...
  旧代码 -> 静默解码出 66 字节垃圾并写盘
  新代码 -> ValueError: 媒体文件 不是合法的 Base64 数据（且不留残留文件）
```

> **准确说明**：这只在过滤后长度恰为 4 的倍数时发生（约 1/4 的错误响应）；其余情况旧代码抛裸 `binascii.Error`，同样没有被捕获。两条路径现在都收敛为带清晰信息的 `ValueError`。

共享的 `decode_base64_limited` 在严格校验前先剔除所有空白字符，因此 MIME 折行的合法 base64 仍然可用，同时依旧拒绝 HTML 页面里的 `<` `>` `"` 等字符。

### 2 · 4xx 不再重传整个请求体 — `generate_image.py:604` / `:837`

`likely_gateway_or_upstream_error` 只对 5xx 中断端点探测，因此一次 HTTP 400（提示词非法、内容审核拒绝）会把**整个 multipart 请求体（每张参考图最多 50MB）重新 POST 给第二个候选端点**——白费带宽，且首次请求其实已到达上游时存在重复计费风险。

**修复**：两处探测循环都加上 `likely_non_retryable_client_error`。404/405 **仍然重试**——那是路由探测发现正确路径的机制。

### 3 · 模型名大小写 — `image_client_core/models.py` `is_gpt_image_2`

CLI 原先逐字比较，`--model GPT-Image-2` 会走错分支（表单字段用 `image` 而非 `image[]`，并跳过 `background='transparent'` 的拒绝检查），产生一个 CLI 自己无法解释的上游 400。现在统一做大小写与空白归一化。

### 4 · 中文上游报错乱码 — `image_client_core/responses.py` `preview_response_body`

`response.text` 对没有 charset 的 `text/*` 按 ISO-8859-1 解码（RFC 2616 回退规则），把中文上游错误信息——本工具最主要的排查依据——变成乱码。

```
上游模型通道异常，请稍后重试        （新）
ä¸\x8aæ¸¸æ¨¡å\x9e\x8bé\x80\x9a...   （旧）
```

### 附带修复：两处裸 `.json()` 调用

`generate_image.py:580`（视频轮询）与 `:884`（图片结果）原先直接调用 `.json()` 并假设返回 dict。上游返回 HTTP 200 + 登录页/`text/plain`/顶层数组时会抛裸异常栈。现改为走 `response_json_limited`，同时获得大小上限与顶层类型检查。

### 清理

- 删除死代码 `GPT_IMAGE_2_BACKGROUND_VALUES`（定义后从未引用，实际检查是硬编码的）
- `likely_gateway_or_upstream_error` 移除两个被首个条件完全包含的冗余分支

---

## 验证方法

不只依赖既有测试，针对每一项改动都写了能区分"改前/改后"的检查。

### 重构等价性是证明过的，不是假设的

1. **动手前**：把 23 个待抽取函数逐一对照**两个原始实现**跑约 1000 组输入，含 10×10 的 `width`/`height` 网格（专门覆盖我把两份 `closest_*_ratio` 合并成一个 `_closest_ratio` 的改动）
2. **改完后**：把重构结果对照**改前的代码快照**重跑同一套

两次结果均为 `ALL EQUIVALENT`。

### 关键 fix 都验证过是承重的

对每一项都构造了"旧代码会失败、新代码会通过"的检查：

| 改动 | 旧代码表现 |
| --- | --- |
| A3 取消 | worker 线程取消后仍运行，新任务永久卡在 `queued` |
| A4 资产泄漏 | 残留 2 个文件、77 字节配额永久占用 |
| #3 取消反馈 | DELETE 还在飞时按钮已恢复为"取消任务" |
| C11c 监听器泄漏 | 50 次 tick → `added=50 removed=0` |
| D 防回归守卫 | 故意在 `web_app.py` 末尾塞一个 `is_gpt_image_2` 副本，测试立刻失败 |

### 发现并修正的既有测试缺陷

`tests/test_web_app.py` 中两个 SSRF 重定向测试原先 patch `web_app.requests.request`。`Session` 化后该 patch 失效，而它们**仍然"通过"了——因为真的向 8.8.8.8 发出了网络请求并碰巧收到一个真实的 302**。已改为 patch 新的 `outbound_session` 接缝，现在确实不触网。

### 全量结果

```
Python 测试        145 passed   （新增 tests/test_image_client_core.py，18 项）
前端测试             9 passed   （npm run check:frontend）
jsdom 行为检查      26 passed   （DOM 17 + 轮询/取消 2 + 提交流程 7）
pyflakes             无告警
最小依赖环境         只装 requests 即可 import 并运行 web_app 与 generate_image
```

---

## 有意未改动的部分

以下几处审查中被标记，但核实后判定为**设计选择而非缺陷**，不应在没有产品决策的情况下悄悄改掉。

### 租户配额为共享池 · `tenancy.py`

`_tenant_settings` 的 `replace(...)` 未覆盖 `max_asset_store_bytes`，因此每个租户各拿 2GiB 而共享一个 10GiB 全局配额，5 个租户即可耗尽。

**但** `tests/test_mcp_tenancy.py::test_global_asset_quota_is_shared_across_tenants` 明确定义了"先到先得的共享池"就是预期行为，错误信息也叫 `shared asset store quota exceeded`。有测试背书的设计不应默默更改。

A4 已使配额可回收，因此耗尽不再是永久性的。若需要按租户切分配额，是一次独立的产品决策。

### 单租户可占满 worker 池 · `tenancy.py:120`

`tenant_capacity` 计算下来正好等于 `global_workers`，单个租户可用 8 个并发长任务占满计算资源数分钟。源码注释明说这是有意为之。

---

## 已知遗留问题

### 需要仓库维护者处理

1. **`.gitignore` 的加固仍未提交** ← 最高优先级。当前 `HEAD` 的 `.gitignore` **不包含** `mcp-config.local.json*` 与 `http-auth.local.json*`，而 `README.md` 正指导用户创建这两个文件并写入 API key 与租户令牌。任何现在克隆仓库的人，拿到的忽略规则会跟踪他们的凭据。
2. **整个 MCP 功能 + `image_client_core/` + `tests/` 尚未纳入 git**（`git status` 中为 `??`）。约 3700 行安全敏感代码与 145 个测试目前只存在于工作区，没有历史、没有 CI、没有评审记录。

### 未处理的代码问题

**结构**

- `web_app.py` 仍有 2256 行。审查已列出 14 个自然模块接缝（限制常量 / CSP 策略 / SSRF 防护 / 上游 I/O / 诊断 / 请求体构造 / 校验 / 任务 / SSE / handler / CLI）。
- `web_app.py` 内的 jobs 与 assets 实现是 `openai_image_client/{jobs,assets}.py` 的残缺重实现，理想状态是 web_app 只管 HTTP、委托给包。

**性能（中等收益）**

- `compact_raw_response` 为量取大小而对整个上游响应做 `json.dumps`（可达 32MB base64），而字节长度早已缓存在 `response._image_client_body`
- `cleanup_jobs()` 在每次任务轮询与每次资产请求时都持全局锁扫描全部任务
- `_send_asset` 的 worker 分支在持有 `JOBS_LOCK` 时执行 `rmtree`
- `/api/generate` 同步路径仍把全部媒体 base64 进 JSON 响应（峰值约 120MB）；异步路径已通过资产化解决
- 全站无 gzip：约 72KB 高可压缩的 HTML/CSS/JS 每次原样传输
- `iter_sse_messages` 在自己的大小检查生效**之前**就可能 OOM（`iter_lines` 缓冲整行无上限）
- SSRF 校验存在 TOCTOU（DNS rebinding）：`validate_outbound_url` 解析一次，`requests` 再解析一次

**打包**

- `OpenAIImageClient.spec` 只打包 `web_app.py`，`pyproject.toml` 声明的两个 MCP 入口不在产物中；`build_windows.bat` 也不安装 `requirements-mcp.txt`——Windows 构建出来是没有 MCP 的
- `.spec` 中 `upx=True`，UPX 缺失时静默失效，且常触发杀毒软件启发式告警

**测试配置**

- `requirements-dev.txt` 钉了 `pytest-asyncio`，但所有异步测试用的是 `@pytest.mark.anyio`（anyio 自带插件）。`pytest-asyncio` 完全未被使用，其 strict 模式还可能对这些 marker 报警。
- `pyproject.toml` 没有 `[tool.pytest.ini_options]`（无 `testpaths`、无 marker 注册）

---

## 开发环境备注

**仓库内的 `venv/` 是坏的**——符号链接指向已被移除的 Homebrew `python3.14`。本轮工作未改动它。

重建开发环境：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # 已包含 requirements-mcp.txt -> requirements.txt
.venv/bin/python -m pytest tests/ -q
```

前端检查（需要 `node_modules`）：

```bash
npm install
npm run check:frontend      # build:css + node --check + node --test
```

仅运行 Web 应用或 CLI 时，`requirements.txt` 的 `requests` 就够了：

```bash
python web_app.py
python generate_image.py --help
```
