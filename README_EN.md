# OpenAI Compatible Image Client

[中文说明](README.md)

An image and video client for OpenAI Images API-compatible and xAI Grok Imagine-compatible services. It provides four entry points:

- Python CLI
- Local Web UI
- Local `stdio` MCP server
- Multi-tenant Streamable HTTP MCP server

It supports text-to-image, image editing, text-to-video, and image-to-video. MCP uses server-side named profiles, so tools cannot accept API keys or arbitrary base URLs.

## Feature Overview

- OpenAI-compatible `images/generations` and `images/edits`
- Grok Imagine image and asynchronous video jobs
- Protocol-aware OpenAI/Grok Web controls
- Web async jobs, status polling, cancellation, and in-page history
- Cross-origin iframe embedding by default, with an optional parent-origin restriction
- Base64 and remote URL media results
- Private upstreams denied by default with an exact-host allowlist
- MCP named profiles, controlled local image imports, remote tenant isolation, resource links, and a bounded job queue

## What's New

- Extracted `image_client_core/` so CLI, Web, and MCP share endpoint, model, and response parsing
- Added cancellation for queued work and polling stages, bounded queues, HTTP/1.1 keep-alive, connection reuse, and immutable result caching
- Fixed MCP result-asset and disk-quota leaks when jobs are cancelled, expired, or evicted
- Reduced history re-renders and repeated media downloads; reference images are encoded only at submission time
- Hardened Base64, response-size, SSRF, authentication, request-body, and error-response handling

See [OPTIMIZATION.md](OPTIMIZATION.md) for the complete review, behavior changes, and verification notes.

## Project Structure

```text
.
├── generate_image.py             # CLI
├── web_app.py                    # Web server and shared generation implementation
├── web/                          # Locally built frontend
├── mcp_server.py                 # stdio MCP entry point
├── mcp_http_server.py            # Streamable HTTP MCP entry point
├── openai_image_client/
│   ├── http_server.py            # HTTP app, routes, and transport guards
│   ├── http_auth.py              # Bearer-token tenant authentication
│   ├── tenancy.py                # Tenant isolation, shared capacity, and RPM
│   └── ...                       # MCP profiles/jobs/assets/service
├── config.json.example           # CLI example configuration
├── mcp-config.example.json       # MCP named-profile example
├── mcp-http-auth.example.json    # HTTP tenant-authentication example
├── OPTIMIZATION.md               # Review, fixes, and verification notes
├── requirements.txt              # CLI/Web dependencies
├── requirements-mcp.txt          # MCP runtime dependencies
├── requirements-dev.txt          # Test and packaging dependencies
└── pyproject.toml                # Editable MCP install and command entry point
```

## Installation

CLI and Web:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

MCP, including the CLI/Web dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
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

## MCP Server

The MCP server uses local `stdio` and opens no additional network port. Clients select a preconfigured named profile; tool schemas contain neither `api_key` nor `base_url`.

### Named Profiles

Create a private configuration:

```bash
cp mcp-config.example.json mcp-config.local.json
chmod 600 mcp-config.local.json
export OPENAI_IMAGE_MCP_CONFIG="$PWD/mcp-config.local.json"
export OPENAI_IMAGE_OPENAI_API_KEY="replace-with-secret"
export OPENAI_IMAGE_GROK_API_KEY="replace-with-secret"
```

The example uses `api_key_env`, so the JSON contains environment variable names rather than keys. Set `allowed_models` explicitly; an empty array allows any model requested through that profile.

Important fields:

- `default_profile`: used when a tool omits `profile`
- `profiles.<name>.base_url`: fixed provider URL
- `profiles.<name>.api_key_env`: name of the environment variable containing the key
- `profiles.<name>.protocol`: `auto`, `openai`, or `grok`
- `image_model` / `video_model`: default models
- `allowed_models`: per-profile model allowlist
- `allowed_input_roots`: directories readable by `import_reference_image`
- `output_dir`: directory containing MCP resources
- `max_workers` / `max_queue`: defaults are `2` / `20`
- `max_jobs`: in-memory job registry limit, default `200`; the oldest terminal jobs are evicted first
- `job_ttl_seconds`: completed job metadata retention, default 24 hours
- `max_asset_store_bytes`: total capacity for local `stdio`; per-tenant capacity in HTTP mode, default 2 GiB

Supported environment variables:

| Variable | Purpose |
| --- | --- |
| `OPENAI_IMAGE_MCP_CONFIG` | Absolute path to the MCP JSON config |
| `OPENAI_IMAGE_MCP_PROFILES_JSON` | Add or override profiles through JSON |
| `OPENAI_IMAGE_MCP_DEFAULT_PROFILE` | Override the default profile |
| `OPENAI_IMAGE_MCP_OUTPUT_DIR` | Override the output directory |
| `OPENAI_IMAGE_MCP_ALLOWED_INPUT_ROOTS` | Path-separated roots (`:` on Unix, `;` on Windows) |
| `OPENAI_IMAGE_MCP_MAX_WORKERS` | Worker count, 1-8 |
| `OPENAI_IMAGE_MCP_MAX_QUEUE` | Queue size, 0-100 |
| `OPENAI_IMAGE_MCP_MAX_JOBS` | Job registry limit, 1-1000, default 200 |
| `OPENAI_IMAGE_MCP_JOB_TTL` | Job TTL, 60-604800 seconds |
| `OPENAI_IMAGE_MCP_MAX_ASSET_STORE_BYTES` | Total asset-store byte limit, default 2 GiB |

When no named profiles exist, the single-profile compatibility variables are also supported: `OPENAI_IMAGE_BASE_URL`, `OPENAI_IMAGE_API_KEY`, `OPENAI_IMAGE_PROTOCOL`, `OPENAI_IMAGE_MODEL`, and `OPENAI_IMAGE_VIDEO_MODEL`.

MCP calls use the same private-upstream checks as Web. A private profile therefore also needs `OPENAI_IMAGE_CLIENT_TRUSTED_UPSTREAM_HOSTS` in the MCP child-process environment.

### Tools, Resources, and Prompts

Tools:

- `generate_image`: generate 1-4 images, wait for completion, and return resource links
- `import_reference_image`: import a PNG/JPEG/WebP from `allowed_input_roots` and return its asset ID
- `edit_image`: edit with 1-16 imported asset IDs
- `generate_video`: queue 1-2 videos and immediately return a job URI
- `get_job`: read job state and result
- `cancel_job`: request cancellation; an active provider call finishes before the worker exits

Resources:

- `image-client://capabilities`
- `image-client://profiles`
- `image-client://jobs`
- `image-client://jobs/{job_id}`
- `image-client://assets`
- `image-client://assets/{asset_id}`

Image results are stored as local resources; images no larger than 5 MiB may also be returned inline. Remote video URLs are stored as `text/uri-list` pointer resources. Available prompts are `compose_image_prompt` and `compose_edit_prompt`.

### Codex Configuration

Use absolute paths in personal `~/.codex/config.toml` or a trusted project's `.codex/config.toml`:

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

Restrict any config containing plaintext environment variables to your user account and never commit it. Restart Codex or start a new task after changing MCP configuration.

### Claude Desktop Configuration

The file is commonly `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS and `%APPDATA%\Claude\claude_desktop_config.json` on Windows:

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

Quit and reopen Claude Desktop after editing. On Windows, use the absolute path to `.venv\Scripts\openai-image-client-mcp.exe` for `command`.

### Remote Streamable HTTP MCP

Remote mode exposes a multi-tenant Streamable HTTP MCP protected by Bearer tokens. It coexists with local `stdio`: prefer `stdio` for one user on one machine, and deploy HTTP only when clients must connect across machines.

The HTTP service binds to these endpoints by default:

```text
http://127.0.0.1:8765/mcp       # MCP endpoint
http://127.0.0.1:8765/healthz   # minimal health check
```

Keep the service on loopback and terminate TLS with Nginx, Caddy, or an equivalent reverse proxy. Never expose port `8765` directly to a LAN or the public Internet.

#### Tenant Authentication Configuration

Remote mode requires `OPENAI_IMAGE_MCP_HTTP_AUTH_CONFIG` to reference a separate JSON file. On POSIX, its mode must be exactly `0600`:

```bash
cp mcp-http-auth.example.json http-auth.local.json
chmod 600 http-auth.local.json
export OPENAI_IMAGE_MCP_HTTP_AUTH_CONFIG="$PWD/http-auth.local.json"
```

Give every tenant a unique Bearer token and separate profile allowlist, default profile, scopes, generation RPM, general-operation RPM, and optional expiry:

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

Scope meanings:

- `mcp:use`: baseline permission required by every HTTP MCP request
- `image:generate`: image generation, image editing, and video generation
- `image:upload`: Base64 reference-image upload
- `image:read`: job, asset, profile, and capability reads
- `image:cancel`: job cancellation

`requests_per_minute` ranges from 1 to 1000 and limits image-generation, image-edit, and video-generation submissions only. `operations_per_minute` ranges from 1 to 10000 and limits every `/mcp` HTTP request carrying a valid tenant token, including initialization, ping, capability listing, unknown methods, calls missing a business scope, and tool, resource, or prompt requests. Requests rejected before authentication or with a `413` oversized-body response are not counted. A successfully submitted generation request consumes one general-request allowance and one additional generation-submission allowance. When omitted, `operations_per_minute` defaults to `max(60, requests_per_minute * 10)`, capped at 10000.

Each tenant must configure exactly one of `token_env` or `token_sha256`:

- `token_env` names an environment variable containing the raw token; the token must contain 32-4096 characters.
- `token_sha256` stores only the raw token's hexadecimal SHA-256 digest, so the server config does not contain the token itself.

Generate a 256-bit token:

```bash
openssl rand -hex 32
```

Do not place an existing token directly in a command line when calculating its digest. Inject it through a secret manager or hidden input first, then run:

```bash
printf '%s' "$OPENAI_IMAGE_MCP_REMOTE_TOKEN" | shasum -a 256
```

With `token_sha256`, the client still stores and sends the raw token; the JSON contains only the resulting 64-character digest. Never commit the auth file, profile config, or an environment file containing raw tokens.

Token rotation requires an HTTP service restart to reload configuration. Replacing one tenant's token is an immediate cutover. For an overlap window, add a temporary second `tenant_id`, update clients, and then remove the old credential. Use `expires_at` (a Unix timestamp) to enforce a hard expiry for transition credentials. Grant each token only the profiles, scopes, generation RPM, and general-operation RPM it needs.

#### Starting the HTTP Service

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

Run `--check-config` first and start the long-running service only after it prints `HTTP MCP configuration is valid`. The check loads profiles, tenant authentication, and HTTP settings and initializes disk quota accounting without opening a listen socket.

HTTP-specific variables:

| Variable | Default/range | Purpose |
| --- | --- | --- |
| `OPENAI_IMAGE_MCP_HTTP_AUTH_CONFIG` | Required | Path to the tenant authentication JSON with mode exactly `0600` |
| `OPENAI_IMAGE_MCP_HTTP_HOST` | `127.0.0.1` | Uvicorn listen address |
| `OPENAI_IMAGE_MCP_HTTP_PORT` | `8765`, 1-65535 | Uvicorn listen port |
| `OPENAI_IMAGE_MCP_HTTP_ALLOW_NON_LOOPBACK` | `false` | Permit a non-loopback listener; only for a trusted private proxy path behind a firewall |
| `OPENAI_IMAGE_MCP_HTTP_PUBLIC_URL` | Required | Complete public HTTPS URL whose path is `/mcp` or ends in `/mcp`; HTTP is accepted only for loopback hosts |
| `OPENAI_IMAGE_MCP_HTTP_ALLOWED_HOSTS` | `host[:port]` from `PUBLIC_URL` | Comma-separated exact Host allowlist for DNS-rebinding protection |
| `OPENAI_IMAGE_MCP_HTTP_ALLOWED_ORIGINS` | Empty | Comma-separated exact HTTP(S) Origin allowlist, needed only by browser clients |
| `OPENAI_IMAGE_MCP_HTTP_MAX_BODY_BYTES` | Derived from `max_reference_bytes`, up to 160 MiB | `/mcp` POST body limit; it must cover the Base64 reference-image payload |
| `OPENAI_IMAGE_MCP_HTTP_MAX_RESOURCE_BYTES` | 16 MiB, 1-32 MiB | Per-response limit for remote asset resources |
| `OPENAI_IMAGE_MCP_HTTP_GLOBAL_WORKERS` | `8`, 1-64 | Generation workers shared across all tenants |
| `OPENAI_IMAGE_MCP_HTTP_GLOBAL_QUEUE` | `100`, 0-1000 | Waiting capacity shared across all tenants |
| `OPENAI_IMAGE_MCP_HTTP_GLOBAL_IO_CONCURRENCY` | `8`, 1-64 | Blocking-I/O concurrency shared across all tenants |
| `OPENAI_IMAGE_MCP_HTTP_TENANT_IO_CONCURRENCY` | `2`, 1-16 | Blocking-I/O concurrency for one tenant |
| `OPENAI_IMAGE_MCP_HTTP_MAX_INFLIGHT_REQUESTS` | `128`, 1-1024 | Concurrent in-flight `/mcp` HTTP request limit |
| `OPENAI_IMAGE_MCP_HTTP_GLOBAL_ASSET_STORE_BYTES` | `max(10 GiB, max_asset_store_bytes)`, 1 MiB-1 TiB | Combined asset-disk quota for all HTTP tenants |
| `OPENAI_IMAGE_MCP_HTTP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |

`OPENAI_IMAGE_MCP_HTTP_PUBLIC_URL` must be the final client-facing endpoint and may contain a path prefix. For example, `https://mcp.example.com/service/mcp` is valid; the reverse proxy must map that external path to the fixed internal `/mcp` route. `OPENAI_IMAGE_MCP_HTTP_ALLOWED_HOSTS` must include the exact public Host forwarded by the proxy. Native Codex clients normally send no `Origin`; only browser MCP clients need `OPENAI_IMAGE_MCP_HTTP_ALLOWED_ORIGINS`. Non-loopback browser origins must use HTTPS; the Origin for the URL above is `https://mcp.example.com`, without a path. Neither allowlist accepts wildcards, and the Origin cannot be `null`.

The recommended configuration always listens on `127.0.0.1`. Set a non-loopback `OPENAI_IMAGE_MCP_HTTP_HOST` together with `OPENAI_IMAGE_MCP_HTTP_ALLOW_NON_LOOPBACK=true` only when the process must bind to a trusted private interface protected by a firewall and a trusted reverse proxy; never use it to expose the application port directly to the Internet. The systemd and Nginx examples below remain on loopback.

This service validates pre-provisioned static Bearer tokens only. It does not publish OAuth discovery or provide dynamic client registration or interactive login. Clients must already possess a token; Codex must explicitly configure the environment variable containing the raw token through `bearer_token_env_var`.

HTTP mode isolates each tenant's runtime, jobs, assets, and output directory, and enforces profile allowlists, scopes, and both RPM limits. Deployment is still limited to **one process on one node**: runtimes and the job registry are process-local, so do not use multiple Uvicorn workers or load-balance without shared state and sticky sessions.

Remote clients cannot submit server-local paths. HTTP mode does not register `import_reference_image`; call `upload_reference_image` with `data_base64` and optional `filename`. Local `stdio` mode can still import paths under `allowed_input_roots`.

#### Capacity, Quotas, and Persistence

- `OPENAI_IMAGE_MCP_HTTP_MAX_RESOURCE_BYTES` limits only a single remote `image-client://assets/{asset_id}` binary response. An oversized asset remains on disk and counts against quota, but its remote resource read fails.
- Generation jobs use the shared worker/queue plus an automatic per-tenant in-flight job limit. A single-tenant configuration can use all `workers + queue` slots. In a multi-tenant configuration, each tenant can occupy at most `min(workers, max(1, workers + queue - 1))` running or queued slots, so one tenant cannot exhaust global capacity when more than one slot exists. A `1 worker / 0 queue` configuration has no slot to reserve; increase the queue if another tenant must submit while the first job runs. Blocking uploads, asset/resource reads, and status operations are also constrained by both global and per-tenant I/O limits. `OPENAI_IMAGE_MCP_HTTP_MAX_INFLIGHT_REQUESTS` separately caps the entire `/mcp` HTTP request layer.
- Each tenant is constrained by `max_asset_store_bytes` (or `OPENAI_IMAGE_MCP_MAX_ASSET_STORE_BYTES`), while all tenants also share `OPENAI_IMAGE_MCP_HTTP_GLOBAL_ASSET_STORE_BYTES`. Exhausting either quota rejects new asset writes.
- The job registry, job-TTL metadata, and both RPM windows are in memory. A restart loses job state and resets rate-limit windows. During normal operation, generated results are deleted and their quota is released when a job is cancelled, expires, or is evicted. Separately uploaded references and orphaned assets left by an unclean shutdown are not automatically removed; they persist across restarts and continue to consume both tenant and global quota.

Monitor the output directory and define an asset-retention policy. `MCPRuntime.prune_assets()` can reclaim orphaned assets left by an unclean shutdown using the job TTL, but it is not scheduled automatically; invoke it only during stopped maintenance or a controlled operations task. For manual cleanup, stop the service, remove unneeded asset data together with the matching metadata, and restart so existing bytes are rescanned and global and per-tenant quota counters are rebuilt. Do not remove files while the service is running because in-memory accounting will not be updated.

#### Single-Worker systemd Service

Use a dedicated non-login account in production. This example assumes the application and virtual environment are under `/opt/openai-image-client`, profile/auth configuration is under `/etc/openai-image-client`, and output is written to `/var/lib/openai-image-client`:

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

`/etc/openai-image-client/mcp-http.env` contains service settings and raw secrets. Keep it root-owned with mode `0600`:

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

Install the following unit as `/etc/systemd/system/openai-image-mcp.service`. `ExecStart` invokes the project entry point, which always starts exactly one Uvicorn worker:

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

Ensure `/opt/openai-image-client` is root-managed and not writable by the service account, then start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now openai-image-mcp.service
```

Do not add multiple workers or keep the auth file or EnvironmentFile in the source tree. Restart the service after changing a token, profile, or tenant configuration.

#### Nginx TLS Reverse Proxy

This example assumes certificates are already managed through ACME or your deployment system:

```nginx
# Place this in nginx.conf's http {}, outside server {}
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

For a public endpoint with a path prefix, such as `https://mcp.example.com/service/mcp`, set `OPENAI_IMAGE_MCP_HTTP_PUBLIC_URL=https://mcp.example.com/service/mcp` and `OPENAI_IMAGE_MCP_HTTP_ALLOWED_HOSTS=mcp.example.com`, then change the main example's external location to the path below. Keep `proxy_pass` on loopback `/mcp`, and retain the main example's `limit_req`, header, buffering, and timeout directives:

```nginx
location = /service/mcp {
    # Keep the remaining directives identical to the main /mcp location.
    proxy_pass http://127.0.0.1:8765/mcp;
}
```

`/mcp` must accept `GET`, `POST`, and `DELETE`, and responses may remain open for streaming. A default 20 MiB reference image expands to about 26.7 MiB in Base64; `client_max_body_size 32m` leaves room for JSON and protocol fields and exceeds the application's default body limit of about 27.7 MiB. If you change `max_reference_bytes` or `OPENAI_IMAGE_MCP_HTTP_MAX_BODY_BYTES`, adjust the proxy limit with it. Do not log the `Authorization` header.

Application-level `requests_per_minute` limits generation submissions, while `operations_per_minute` limits every `/mcp` request carrying a valid tenant token. Nginx per-IP `limit_req` runs before tenant identity is resolved and can constrain invalid tokens, malformed protocol requests, and bursts. Tune `rate` and `burst` for the expected client count. If another load balancer sits in front of Nginx, configure Real IP only for trusted proxies or all clients may share one rate-limit key.

The health check requires no MCP protocol headers and must not disclose tenants, profiles, paths, tokens, or upstream status:

```bash
curl -fsS https://mcp.example.com/healthz
```

Do not use `/mcp` as a load-balancer health probe. A Bearer token is not a substitute for TLS.

#### Connecting Codex Remotely

Static Bearer mode has no OAuth discovery, so Codex cannot log in from the URL alone. Make the raw token available to the Codex process, then register the remote MCP. Do not store the raw token in `config.toml`:

```bash
export OPENAI_IMAGE_MCP_REMOTE_TOKEN="replace-with-the-tenant-token"

codex mcp add openai-image-client-remote \
  --url https://mcp.example.com/mcp \
  --bearer-token-env-var OPENAI_IMAGE_MCP_REMOTE_TOKEN
```

Equivalent Codex TOML:

```toml
[mcp_servers.openai_image_client_remote]
url = "https://mcp.example.com/mcp"
bearer_token_env_var = "OPENAI_IMAGE_MCP_REMOTE_TOKEN"
startup_timeout_sec = 30
tool_timeout_sec = 700
```

Restart Codex or start a new task after changing the token. `bearer_token_env_var` stores only the variable name; the Codex desktop app must receive that variable from its launch environment or an OS secret manager.

## Security Checklist

- Never commit `config.local.json`, `mcp-config.local.json`, `http-auth.local.json`, `mcp-http.env`, or client configs containing secrets
- Apply `chmod 600` to private configs and keep output directories user-only
- Give MCP only the required `allowed_input_roots`
- Fix each profile's `base_url` and set `allowed_models`
- Give every remote MCP tenant a unique token and minimum scopes/profiles/both RPM limits, and keep its auth config at mode `0600`
- Keep Web and HTTP MCP on loopback; use a trusted reverse proxy for remote TLS and edge authentication
- Monitor per-tenant and global asset quotas; schedule controlled cleanup for uploaded and crash-orphaned assets
- Allow only the exact required private upstream hosts
- Rotate any key previously exposed in source code or browser storage

## Verification

```bash
# Python/MCP tests
python -m pytest -q

# Frontend build and static checks
npm install
npm run check:frontend
```

Tests cannot verify provider quotas, model entitlements, or provider-specific response behavior.

## License

[MIT](LICENSE)
