# OpenAI Compatible Image Client

[中文说明](README.md)

An image and video client for OpenAI Images API-compatible services, the native Google Gemini image protocol, and xAI Grok Imagine-compatible services. It provides two entry points:

- Local Web UI (primary)
- Python CLI

It supports text-to-image, image editing, text-to-video, and image-to-video. OpenAI image generation supports GPT Image 2.6 complexity control plus animated GIF and SVG vector output (currently in the Web UI only).

> The MCP servers (stdio and multi-tenant HTTP) were removed starting with this release. Use an earlier release if you still need them.

## Feature Overview

- OpenAI-compatible `images/generations` and `images/edits`
- Native Gemini `models/{model}:generateContent` text-to-image and image editing in the Web UI
- Grok Imagine image and asynchronous video jobs
- Protocol-aware OpenAI/Gemini/Grok Web controls
- Web async jobs, status polling, cancellation, and in-page history
- Cross-origin iframe embedding by default, with an optional parent-origin restriction
- Base64 and remote URL media results
- Private upstreams denied by default with an exact-host allowlist

## Project Structure

```text
.
├── generate_image.py             # CLI
├── web_app.py                    # Web server and shared generation implementation
├── web/                          # Locally built frontend
├── image_client_core/            # Model checks, endpoints, and response handling shared by Web and CLI
├── config.json.example           # CLI example configuration
├── requirements.txt              # CLI/Web dependencies
└── requirements-dev.txt          # Test and packaging dependencies
```

## Installation

CLI and Web:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows, activate with `.venv\Scripts\activate`.

## CLI

The CLI resolves settings in this order: command-line arguments, environment variables, `config.local.json`, then `config.json`.

Environment variables keep keys out of process arguments, but typing the `export` command below with a real value can still put it in shell history. In production, inject the value through a secret manager or parent process, or read it with hidden shell input such as `read -s` before exporting it:

```bash
export OPENAI_IMAGE_BASE_URL="https://api.openai.com/v1"
export OPENAI_IMAGE_API_KEY="replace-with-secret"
python generate_image.py --protocol openai --model gpt-image-2 \
  --prompt "A clean product photograph on a neutral studio background"
```

You can also create a private local config:

```bash
cp config.json.example config.local.json
chmod 600 config.local.json
python generate_image.py
```

Grok image editing:

```bash
python generate_image.py \
  --mode edit \
  --protocol grok \
  --model grok-imagine-image \
  --prompt "Keep the subject and change the scene to cinematic night lighting" \
  --image ./reference.png
```

Grok video:

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

Start the server:

```bash
source .venv/bin/activate
python web_app.py
```

It listens only on `http://127.0.0.1:8000` by default. If the port is occupied, it tries subsequent ports; use `--strict-port` to disable that behavior.

### GPT Image 2.6: Complexity, Animated GIF, and SVG Output

Selecting `gpt-image-2.6`, `gpt-image-2.6-sunburst`, or `gpt-image-2.6-flare` under **OpenAI Image** reveals the options below. Other models neither show nor send these fields. Values follow the official OpenAI [API reference](https://developers.openai.com/api/reference/resources/images/methods/generate) and [image generation guide](https://developers.openai.com/api/docs/guides/image-generation):

| Option | Request field | Values | Notes |
| --- | --- | --- | --- |
| Complexity | `complexity` | `low` / `medium` / `high` | Planning effort, independent of `quality`; "Auto" leaves the upstream default (`high`) |
| Output format | `output_format` | default PNG / `gif` / `svg` | |
| GIF frames | `frame_count` | 2–16, default 9 | GIF only; every frame uses the selected size, so frames × size must fit the upstream pixel budget |
| Frame duration | `frame_duration_ms` | 20–2000 ms, default 200 | GIF only; rounded to 10 ms upstream |
| SVG author model | `svg.model` | optional | SVG only; set it only when the account has SVG model selection, otherwise leave it empty so it is not sent |
| SVG reasoning effort | `svg.reasoning.effort` | `none` … `max` | SVG only, and requires an SVG author model |

Limits:

- SVG is text-to-image only and always `n=1`.
- GIF and SVG cannot stream. The Web UI already uses async jobs; `/api/generate-stream` rejects them.
- GIF and SVG results cannot be reused as references (the upstream accepts PNG, JPEG, and WebP only).
- Result types are detected from file signatures (the official API puts `output_format` at the top level and some relays drop it). SVG assets carry an extra sandboxing CSP, so opening one in a new tab cannot run scripts.

### Native Gemini Image Generation

Select **Gemini Image** in the Web UI, then enter a Base URL, API key, and model name. The model is not fixed; it may be an official model or an identifier supplied by a compatible proxy.

Accepted Base URL forms include:

```text
https://generativelanguage.googleapis.com/v1beta
https://gemini-proxy.example/v1beta
https://gemini-proxy.example/v1beta/models/custom-model:generateContent
```

A Base URL without an API version receives `/v1beta` automatically. Requests carry both `x-goog-api-key` and `Authorization: Bearer` to support the official Google endpoint and Gemini-compatible proxies that use bearer tokens. Cross-origin redirects remain blocked so credentials cannot be forwarded to another host.

Text-to-image and image editing both use native `generateContent`. Prompts and references are placed in `contents[].parts`, references use `inlineData`, and generation settings use `responseModalities: ["TEXT", "IMAGE"]` plus `imageConfig.aspectRatio` and `imageConfig.imageSize`.

Gemini shares these output choices with **Other Models**. The 4K tier does not exceed landscape `3840x2160` or portrait `2160x3840`:

| Ratio | 1K | 2K | 4K |
| --- | --- | --- | --- |
| 1:1 | 1024x1024 | 2048x2048 | 2160x2160 |
| 3:4 | 864x1152 | 1728x2304 | 2160x2880 |
| 4:3 | 1152x864 | 2304x1728 | 2880x2160 |
| 16:9 | 1312x736 | 2624x1472 | 3840x2160 |
| 9:16 | 736x1312 | 1472x2624 | 2160x3840 |
| 2:3 | 832x1248 | 1664x2496 | 2160x3240 |
| 3:2 | 1248x832 | 2496x1664 | 3240x2160 |
| 21:9 | 1568x672 | 3136x1344 | 3808x1632 |

The UI displays the effective pixel reference while the native request sends the corresponding `1K`, `2K`, or `4K` tier. When output count `n` is greater than one, the service calls the upstream sequentially `n` times and takes the first valid image from each response. This produces `n` upstream requests and corresponding charges. Native Gemini support is limited to the Web UI and Web async-job API; CLI protocol support is unchanged.

### Web Workflow

1. The frontend creates a job with `POST /api/generate-async`.
2. It polls `GET /api/jobs/{job_id}`.
3. Completed images/videos are appended to the current page history.
4. Cancel calls `DELETE /api/jobs/{job_id}`.

Queued work can be stopped promptly. Once an upstream HTTP request is running, cancellation cannot forcibly terminate that underlying request, but its result is discarded when the provider returns. History and media previews live only in page memory and disappear on reload.

The browser persists only non-sensitive settings: a safe-form base URL, protocol, mode, model, size, count, timeout, and video options. It never writes these values to `localStorage`:

- API key
- Prompt
- Reference images
- Generated results or job history

Initialization also removes the legacy `openai-image-client-web:v3` record, which may have contained a key.

### Web Limits

| Item | Limit |
| --- | --- |
| Request body | 48 MiB |
| One reference image | 10 MiB |
| All reference images | 30 MiB |
| Image count | 1-8 |
| Video count | 1-4 |
| OpenAI edit references | Up to 16 |
| Gemini references | Up to 16, still subject to the 30 MiB combined limit |
| Grok image references | Up to 3 |
| Grok video references | Up to 7 |
| Upstream timeout | 5-600 seconds |
| Web workers / queued jobs | 2 / 8 |
| Completed job retention | 1 hour |

### Web Security Configuration

The defaults are intended for local use. Do not expose the bare service port directly to the public Internet.

**iframe embedding**

The Web UI allows embedding from any HTTP(S) parent by default, with no extra configuration:

```html
<iframe
  src="http://127.0.0.1:8000/"
  title="Image Client"
  sandbox="allow-scripts allow-same-origin allow-downloads"
  style="width:100%;height:900px;border:0"
></iframe>
```

`allow-same-origin` is required. Without it, the sandbox creates an opaque origin and backend write requests are rejected. `allow-downloads` enables result downloads. An HTTPS parent should embed an HTTPS URL to avoid mixed-content blocking.

Allowing embedding by default means any site can display and overlay this UI, which creates clickjacking risk. Shared or public deployments should restrict parent origins with `OPENAI_IMAGE_CLIENT_EMBED_ORIGINS`:

```bash
# Allow same-origin frames and one control panel
export OPENAI_IMAGE_CLIENT_EMBED_ORIGINS="self,https://console.example.com"

# Disable framing completely; use self for same-origin frames only
export OPENAI_IMAGE_CLIENT_EMBED_ORIGINS="none"
```

An unset variable is equivalent to `*`; an explicit empty value or `none` disables framing; `self` allows same-origin frames; other entries must be comma-separated complete HTTP(S) origins without paths, queries, or wildcard hosts. A reverse proxy must not add `X-Frame-Options: DENY` or a second stricter CSP, because either still blocks embedding. The iframe parent allowlist is independent from `OPENAI_IMAGE_CLIENT_ALLOWED_ORIGINS`.

**Private upstreams**

Base URLs and provider-returned media URLs pass SSRF checks. Loopback, LAN, link-local, and other non-public addresses are rejected by default. Allow only the exact trusted gateway hostname or IP when required:

```bash
export OPENAI_IMAGE_CLIENT_TRUSTED_UPSTREAM_HOSTS="gateway.lan,10.0.0.10"
python web_app.py
```

`OPENAI_IMAGE_CLIENT_ALLOW_PRIVATE_UPSTREAMS=1` allows every private target. It is intentionally broad and should only be used temporarily in an isolated development environment.

The actual connection is pinned to the validated IP while preserving the original Host header, TLS SNI, and certificate hostname verification, preventing DNS from rebinding to a private address between validation and connection. To preserve that guarantee, Web outbound requests do not inherit `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY`. When an upstream requires a proxy, use an administrator-controlled local gateway address explicitly allowed through `OPENAI_IMAGE_CLIENT_TRUSTED_UPSTREAM_HOSTS`.

**Non-loopback binding**

Binding to `0.0.0.0`, a LAN address, or another non-loopback address fails unless a service token is set:

```bash
export OPENAI_IMAGE_CLIENT_AUTH_TOKEN="replace-with-a-long-random-token"
python web_app.py --host 0.0.0.0 --port 8000 --strict-port --no-open-browser
```

API clients must send one of these headers:

```text
Authorization: Bearer <service-token>
X-Image-Client-Token: <service-token>
```

The current Web page has no service-token input and does not attach either header. For remote browser access, place the service behind a TLS reverse proxy with user authentication and have the proxy inject the backend service token. Do not connect browsers directly to the bare port. Cross-origin mutations also require an explicit allowlist:

```bash
export OPENAI_IMAGE_CLIENT_ALLOWED_ORIGINS="https://images.example.com,https://admin.example.com"
```

A token is not a substitute for TLS. Do not transmit provider keys or the service token over plaintext HTTP networks.

## Security Checklist

- Never commit `config.local.json` or any other config containing secrets
- Apply `chmod 600` to private configs and keep output directories user-only
- Keep Web on loopback; use a trusted reverse proxy for remote TLS and edge authentication
- Allow only the exact required private upstream hosts
- Rotate any key previously exposed in source code or browser storage

## Verification

```bash
# Python tests
python -m pytest -q

# Frontend build and static checks
npm install
npm run check:frontend
```

Tests cannot verify provider quotas, model entitlements, or provider-specific response behavior.

## License

[MIT](LICENSE)
