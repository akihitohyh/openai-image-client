# OpenAI Compatible Image Client

[中文说明](README.md)

A lightweight OpenAI / Grok Imagine image and video generation client with two usage modes:

1. Python CLI script
2. Local Web UI

It works well with OpenAI Images API-compatible and xAI Grok Imagine-compatible gateways, proxies, or self-hosted relay services.

---

## Features

- Compatible with `POST /v1/images/generations`, `POST /v1/images/edits`, and xAI `POST /v1/videos/generations`
- The Web UI splits work protocols into `OpenAI Image`, `Grok Image`, and `Grok Video`
- The model dropdown changes automatically based on the selected protocol
- OpenAI image generation supports `gpt-image-2`
- Grok image generation supports `grok-imagine`, `grok-imagine-edit`, `grok-imagine-image`, and `grok-imagine-image-quality`
- Grok video generation supports `grok-imagine-video-1.5` and `grok-imagine-video`
- CLI `protocol=auto` selects by model name or xAI Base URL; `openai` / `grok` can be forced
- Supports text-to-image, image-to-image, text-to-video, and image-to-video
- Image-to-image supports reference upload, drag-and-drop, preview, and removal
- Accepts either an API root URL or a full image endpoint
- Includes both CLI and Web UI
- Web settings are stored only in browser `localStorage`
- The backend does not save frontend settings or create backups
- Supports upstream responses with either `b64_json` or `url`
- Grok video automatically creates a video job, polls the returned `request_id`, and returns the final video URL

---

## Project Structure

```text
.
├── generate_image.py      # CLI image generation script
├── web_app.py             # Local web server
├── web/                   # Frontend files
├── config.json.example    # Example config
└── requirements.txt
```

---

## 1. CLI Script

### Install Dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration

It is recommended to copy the example config into a private local file:

```bash
cp config.json.example config.local.json
```

Then edit `config.local.json` and fill in:

- `base_url`
- `api_key`
- `prompt`

The script reads config files in this order:

1. `config.local.json`
2. `config.json`

### Run

```bash
python generate_image.py
```

You can also provide everything via command-line arguments:

```bash
python generate_image.py \
  --base-url "https://your-api-host.com" \
  --api-key "your_api_key" \
  --protocol openai \
  --prompt "A cute orange cat astronaut"
```

Grok Imagine text-to-image:

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

Image-to-image with a reference image:

```bash
python generate_image.py \
  --mode edit \
  --protocol grok \
  --model "grok-imagine-image" \
  --prompt "Keep the subject composition and change it to a cyberpunk night scene" \
  --image "./reference.png"
```

Grok Imagine text-to-video:

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

Grok Imagine image-to-video:

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

## 2. Local Web UI

### Highlights

- Neo-Brutalism style interface
- Protocol is selected with segmented buttons: `OpenAI Image`, `Grok Image`, and `Grok Video`
- Model is selected from a protocol-aware dropdown
- Supports text-to-image, image-to-image, text-to-video, and image-to-video
- Reference images support upload, drag-and-drop, local preview, and removal
- Image history stays in a single result area
- Click an image or video to show details and prompt on the right
- Built-in image preview and video playback
- Multi-image requests are split into sequential single tasks
- Long-running image and video requests use async job polling to reduce browser-side `Failed to fetch` issues
- All frontend settings are automatically stored in the current browser

### Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python web_app.py
```

Default address:

```text
http://127.0.0.1:8000
```

### Notes

- `Base URL` can be an API root, for example `https://your-api-host.com`
- It can also be a full endpoint, for example `https://your-api-host.com/v1/images/generations`
- The Web UI `Protocol` options are `OpenAI Image`, `Grok Image`, and `Grok Video`
- `Model` is a dropdown, and its options change with the selected protocol
- CLI `--protocol auto` detects the protocol from the model name or xAI Base URL
- Image-to-image mode automatically switches to a compatible `images/edits` endpoint; OpenAI/GPT uses multipart while Grok uses JSON
- Grok video calls `videos/generations`, and the Web UI renders a video player
- Reference images are kept only in browser memory and are not backed up by the backend
- Long-running tasks are created as jobs first and then polled by the frontend
- If the upstream returns `b64_json`, the page displays it directly
- If the upstream returns `url`, the backend downloads the image first and then returns it to the frontend
- The backend does not store frontend input settings for you

---

## Changelog

### 2026-07-09
- Added Grok Imagine video support for text-to-video and image-to-video
- Changed the Web UI protocol selector to three work modes: `OpenAI Image`, `Grok Image`, and `Grok Video`
- Changed `Model` to a dropdown with options driven by the selected protocol
- Protocol switching now updates helper text, reference image limits, video options, and submit button text
- Removed the live demo link and Web UI screenshot asset from the README

### 2026-04-28
- Added image-to-image mode
- Added reference image upload, drag-and-drop, local preview, and removal
- Added mode, reference image, and prompt details in result history
- Added OpenAI-compatible `images/edits` backend support
- Kept multi-image requests running as sequential tasks with independent wait time and timeout tracking
- Switched long-running text-to-image / image-to-image requests to async polling to reduce `Failed to fetch` caused by broken long-lived connections

---

## Acknowledgements

- 🔗 [Linux DO](https://linux.do)
