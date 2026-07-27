"""Model identification plus Grok aspect-ratio / resolution derivation.

All model-name checks normalize case and whitespace. ``generate_image.py`` used to
compare ``model == "gpt-image-2"`` verbatim, so ``--model GPT-Image-2`` silently took
the wrong code path (form field ``image`` instead of ``image[]``, and it skipped the
``background=transparent`` rejection) and produced an upstream 400 the CLI could not
explain.
"""

from __future__ import annotations

import math

GROK_IMAGE_GENERATION_MODELS = {
    "grok-imagine-edit",
    "grok-imagine-image",
    "grok-imagine-image-lite",
    "grok-imagine-image-pro",
    "grok-imagine-image-quality",
}

GROK_VIDEO_MODELS = {
    "grok-imagine-video",
    "grok-imagine-video-1.5",
}

GROK_RESOLUTION_VALUES = {"", "1k", "2k"}
GROK_RESPONSE_FORMAT_VALUES = {"", "url", "b64_json"}
GROK_VIDEO_RESOLUTION_VALUES = {"", "480p", "720p", "1080p"}

GROK_SUPPORTED_RATIOS = {
    "1:1",
    "16:9",
    "9:16",
    "4:3",
    "3:4",
    "3:2",
    "2:3",
    "2:1",
    "1:2",
    "19.5:9",
    "9:19.5",
    "20:9",
    "9:20",
    "auto",
}

GROK_VIDEO_SUPPORTED_RATIOS = {
    "1:1",
    "16:9",
    "9:16",
    "4:3",
    "3:4",
    "3:2",
    "2:3",
}


def is_gpt_image_2(model: str) -> bool:
    value = str(model or "").strip().lower()
    return value == "gpt-image-2" or value.startswith("gpt-image-2-")


def is_grok_image_model(model: str) -> bool:
    value = str(model or "").strip().lower()
    return value == "grok-imagine" or any(
        value == item or value.startswith(f"{item}-") for item in GROK_IMAGE_GENERATION_MODELS
    )


def is_grok_video_model(model: str) -> bool:
    value = str(model or "").strip().lower()
    return any(value == item or value.startswith(f"{item}-") for item in GROK_VIDEO_MODELS)


def parse_size(size: str) -> tuple[int, int] | None:
    value = str(size or "").strip().lower()
    if "x" not in value:
        return None
    left, right = value.split("x", 1)
    try:
        width = int(left)
        height = int(right)
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _closest_ratio(width: int, height: int, ratios: set[str], fallback: str) -> str:
    exact_gcd = math.gcd(width, height)
    exact_ratio = f"{width // exact_gcd}:{height // exact_gcd}"
    if exact_ratio in ratios:
        return exact_ratio

    current = width / height
    best_ratio = fallback
    best_delta = float("inf")
    for candidate in ratios - {"auto"}:
        left, right = candidate.split(":", 1)
        candidate_value = float(left) / float(right)
        delta = abs(current - candidate_value)
        if delta < best_delta:
            best_ratio = candidate
            best_delta = delta
    return best_ratio


def closest_grok_ratio(width: int, height: int) -> str:
    return _closest_ratio(width, height, GROK_SUPPORTED_RATIOS, "1:1")


def closest_grok_video_ratio(width: int, height: int) -> str:
    return _closest_ratio(width, height, GROK_VIDEO_SUPPORTED_RATIOS, "16:9")


def grok_aspect_ratio_from_size(size: str) -> str:
    parsed = parse_size(size)
    if not parsed:
        return "1:1"
    return closest_grok_ratio(*parsed)


def grok_resolution_from_size(size: str) -> str:
    parsed = parse_size(size)
    if not parsed:
        return "1k"
    return "2k" if max(parsed) > 1536 else "1k"


def grok_video_aspect_ratio_from_size(size: str) -> str:
    parsed = parse_size(size)
    if not parsed:
        return "16:9"
    return closest_grok_video_ratio(*parsed)


def grok_video_resolution_from_size(size: str) -> str:
    parsed = parse_size(size)
    if not parsed:
        return "480p"
    long_edge = max(parsed)
    if long_edge > 1536:
        return "1080p"
    if long_edge > 720:
        return "720p"
    return "480p"
