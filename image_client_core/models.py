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


def is_gpt_image_2_5(model: str) -> bool:
    """``gpt-image-2.5`` 及其变体（``-flare`` / ``-sunburst``）。

    刻意与 :func:`is_gpt_image_2` 分开：后者只认 ``gpt-image-2-`` 这个连字符前缀，
    ``gpt-image-2.5`` 是点号，本来就匹配不上。两个系列的能力并不完全相同，合并成
    一个判定会把 gpt-image-2 的限制无依据地套到 2.5 上。
    """
    value = str(model or "").strip().lower()
    return value == "gpt-image-2.5" or value.startswith("gpt-image-2.5-")


def is_gpt_image_2_6(model: str) -> bool:
    """``gpt-image-2.6`` 及其变体（``-sunburst`` / ``-flare``）。

    2.6 是第一个支持 ``complexity``、GIF 动图和 SVG 矢量图输出的系列，所以单独判定，
    不和 2.5 合并。
    """
    value = str(model or "").strip().lower()
    return value == "gpt-image-2.6" or value.startswith("gpt-image-2.6-")


def uses_openai_image_array_field(model: str) -> bool:
    """图生图上传参考图时该用 ``image[]`` 还是 ``image``。

    ``image[]`` 是 gpt-image-2 引入的写法，2.5、2.6 系列沿用。发错字段上游直接 400，
    且错误信息不会说明原因——正是模块开头记录的那个坑。
    """
    return is_gpt_image_2(model) or is_gpt_image_2_5(model) or is_gpt_image_2_6(model)


# 以下取值全部来自 OpenAI 官方文档（2026-11）：
# - API 参考 POST /v1/images/generations、/v1/images/edits（openai-python 3.46.0 同源生成）
# - 指南 https://developers.openai.com/api/docs/guides/image-generation 的 GPT Image 2.6 小节
GPT_IMAGE_COMPLEXITY_VALUES = ("low", "medium", "high")
# API 参考里 frame_count 的 schema 是 minimum 2 / maximum 16，默认 9。
GIF_FRAME_COUNT_MIN = 2
GIF_FRAME_COUNT_MAX = 16
# API 参考给出 minimum 20；指南写明可用范围 20–2000 毫秒，默认 200，上游按 10 ms 取整。
GIF_FRAME_DURATION_MIN_MS = 20
GIF_FRAME_DURATION_MAX_MS = 2000
SVG_REASONING_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# 不支持流式、partial_images、output_compression 的两种输出格式。
NON_STREAMING_OUTPUT_FORMATS = frozenset({"gif", "svg"})


def _optional_bounded_int(value, name: str, minimum: int, maximum: int) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是 {minimum} 到 {maximum} 的整数")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是 {minimum} 到 {maximum} 的整数") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} 必须是 {minimum} 到 {maximum} 的整数")
    return number


def build_gpt_image_format_options(
    *,
    output_format: str = "",
    complexity: str = "",
    frame_count=None,
    frame_duration_ms=None,
    svg_model: str = "",
    svg_reasoning_effort: str = "",
    background: str = "",
    mode: str = "generate",
    image_count: int = 1,
    stream: bool = False,
) -> dict:
    """校验 GPT Image 2.6 新增的请求字段，返回要并入请求体的那部分。

    Web 用这一份规则，放在 image_client_core 里是为了 CLI 以后接入时不用再写一套。这里只校验
    官方文档写明的组合限制（例如 SVG 只能文生图、一次 1 张），不按模型名拦截：
    中转站常把模型改名转发，按名字拦会误伤，交给上游判断更稳妥。
    """
    output_format = str(output_format or "").strip().lower()
    complexity = str(complexity or "").strip().lower()
    svg_model = str(svg_model or "").strip()
    svg_reasoning_effort = str(svg_reasoning_effort or "").strip().lower()
    background = str(background or "").strip().lower()

    options: dict = {}
    if complexity and complexity != "auto":
        if complexity not in GPT_IMAGE_COMPLEXITY_VALUES:
            raise ValueError("complexity 只能是 low、medium 或 high")
        options["complexity"] = complexity

    frames = _optional_bounded_int(frame_count, "frame_count", GIF_FRAME_COUNT_MIN, GIF_FRAME_COUNT_MAX)
    duration = _optional_bounded_int(
        frame_duration_ms,
        "frame_duration_ms",
        GIF_FRAME_DURATION_MIN_MS,
        GIF_FRAME_DURATION_MAX_MS,
    )
    if (frames is not None or duration is not None) and output_format != "gif":
        raise ValueError("frame_count / frame_duration_ms 只能和 output_format=gif 一起使用")
    if (svg_model or svg_reasoning_effort) and output_format != "svg":
        raise ValueError("svg_model / svg_reasoning_effort 只能和 output_format=svg 一起使用")

    if output_format in NON_STREAMING_OUTPUT_FORMATS:
        label = "GIF" if output_format == "gif" else "SVG"
        if stream:
            raise ValueError(f"{label} 输出不支持流式返回，请改用普通或异步生成")
        if background == "transparent":
            raise ValueError("透明背景只能配合 png 或 webp 输出")

    if output_format == "gif":
        if frames is not None:
            options["frame_count"] = frames
        if duration is not None:
            options["frame_duration_ms"] = duration
    elif output_format == "svg":
        if mode == "edit":
            raise ValueError("SVG 输出只支持文生图，图生图接口不支持 SVG")
        if image_count != 1:
            raise ValueError("SVG 输出一次只能生成 1 张，请把 n 设为 1")
        if svg_reasoning_effort and svg_reasoning_effort not in SVG_REASONING_EFFORT_VALUES:
            raise ValueError("svg_reasoning_effort 取值不合法")
        if svg_reasoning_effort and not svg_model:
            raise ValueError("设置 SVG 推理强度时必须同时填写 SVG 编写模型")
        if svg_model:
            # 官方说明：只有账号开通了「SVG 模型选择」才传 svg 对象，否则必须省略。
            svg: dict = {"model": svg_model}
            if svg_reasoning_effort:
                svg["reasoning"] = {"effort": svg_reasoning_effort}
            options["svg"] = svg
    return options


def sniff_image_mime(data: bytes) -> str | None:
    """按文件头判断生成结果的真实类型，认不出时返回 None。

    官方把 ``output_format`` 放在响应顶层而不是每个 ``data[]`` 元素里，中转站也常把它
    丢掉；只靠格式字段会把 GIF/SVG 当成 PNG 交给浏览器，SVG 因此直接显示不出来。
    """
    head = bytes(data[:4096])
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    text = head.lstrip(b"\xef\xbb\xbf").lstrip()
    if text.startswith(b"<") and b"<svg" in head.lower():
        return "image/svg+xml"
    return None


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
