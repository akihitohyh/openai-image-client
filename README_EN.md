# OpenAI Compatible Image Client

[中文说明](README.md)

## Live Demo

🔗 https://image.hyhschool.com/

A lightweight OpenAI-compatible text-to-image / image-to-image client with two usage modes:

1. Python CLI script
2. Local Web UI

It works well with OpenAI Images API-compatible gateways, proxies, or self-hosted relay services.

## Updated Web UI Screenshot

![OpenAI Image Client Updated Web UI](assets/web-ui-image-mode.png)

---

## Features

- Compatible with `POST /v1/images/generations` and `POST /v1/images/edits`
- Supports both text-to-image and image-to-image modes
- Image-to-image supports reference upload, drag-and-drop, preview, and removal
- Accepts either an API root URL or a full image endpoint
- Includes both CLI and Web UI
- Web settings are stored only in browser `localStorage`
- The backend does not save frontend settings or create backups
- Supports upstream responses with either `b64_json` or `url`

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
  --prompt "A cute orange cat astronaut"
```

---

## 2. Local Web UI

### Highlights

- Neo-Brutalism style interface
- Supports both text-to-image and image-to-image modes
- Image-to-image supports reference upload, drag-and-drop, local preview, and removal
- Image history stays in a single result area
- Click an image to show details and prompt on the right
- Built-in image preview
- Multi-image requests are split into sequential single tasks
- Long-running generation requests now use async job polling to reduce browser-side `Failed to fetch` issues
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
- Image-to-image mode automatically switches to a compatible `images/edits` endpoint
- Reference images are kept only in browser memory and are not backed up by the backend
- Long-running tasks are created as jobs first and then polled by the frontend
- If the upstream returns `b64_json`, the page displays it directly
- If the upstream returns `url`, the backend downloads the image first and then returns it to the frontend
- The backend does not store frontend input settings for you

---

## Changelog

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
