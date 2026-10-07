"""把二维码 PNG 渲染成终端等宽字符画（不依赖框架，可单测）。

QQ 频道 CLI 的 ``login --json`` 会返回二维码 PNG 的路径与 base64。为了「开机未登录时
把二维码打印到日志/控制台」，这里用半块字符（``▀``/``▄``/``█``）把它画出来：
每个字符表示 2 个纵向像素，因此输出接近正方形，手机可以直接扫。

只用 Pillow（插件 python_dependencies 里已有）；任何失败都返回空串，调用方退化为
「打印授权链接 + 图片路径」。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

#: 渲染宽度（字符数）。60 列左右在常见终端里不会折行，也够扫码分辨率
DEFAULT_ASCII_WIDTH = 60
#: 判定黑/白的灰度阈值（原图极性强：模块=黑=低灰度）
_DARK_THRESHOLD = 128
#: 裁剪后补回的白边比例（二维码需要静默区才好扫）
_QUIET_ZONE_RATIO = 0.06


def _open_grayscale(source: str | bytes | None) -> Any:
    """从 PNG 路径 / base64 字符串 / 原始字节加载灰度图（黑=模块）；失败返回 None。"""
    try:
        from PIL import Image  # 延迟导入：缺 Pillow 时不影响插件加载
    except Exception:  # noqa: BLE001
        return None

    data: Any = None
    if isinstance(source, bytes):
        data = io.BytesIO(source)
    elif isinstance(source, str):
        text = source.strip()
        if not text:
            return None
        candidate = Path(text)
        try:
            if candidate.is_file():
                data = candidate
        except OSError:
            data = None
        if data is None:
            # 当成 base64（CLI 的 qr_code 字段）；容忍 data URL 前缀
            payload = text.split(",", 1)[1] if text.startswith("data:") and "," in text else text
            try:
                data = io.BytesIO(base64.b64decode(payload, validate=False))
            except Exception:  # noqa: BLE001
                return None
    if data is None:
        return None
    try:
        return Image.open(data).convert("L")
    except Exception:  # noqa: BLE001
        return None


def render_qr_ascii(
    source: str | bytes | None,
    *,
    width: int = DEFAULT_ASCII_WIDTH,
) -> str:
    """把二维码渲染成字符画；无法渲染时返回空串。

    Args:
        source: PNG 文件路径、base64 字符串或 PNG 字节。
        width: 输出宽度（字符数）。
    """
    image = _open_grayscale(source)
    if image is None:
        return ""

    try:
        from PIL import Image

        # 1) 求「深色模块」的外接框（反相后取 bbox），据此裁掉自带的静默区
        mask = image.point(lambda value: 255 - value)
        bbox = mask.getbbox()
        if bbox is None:
            return ""
        modules = image.crop(bbox)

        # 2) 补一圈白边当静默区（原图极性强：背景为白）
        pad = max(2, int(max(modules.size) * _QUIET_ZONE_RATIO))
        canvas = Image.new("L", (modules.width + pad * 2, modules.height + pad * 2), 255)
        canvas.paste(modules, (pad, pad))

        # 3) 缩放到目标宽度；高度按比例并取偶数（半块字符按 2 行合并）
        target_w = max(21, min(int(width), 120))
        ratio = canvas.height / max(1, canvas.width)
        target_h = max(2, int(round(target_w * ratio)))
        if target_h % 2:
            target_h += 1
        resized = canvas.resize((target_w, target_h), Image.NEAREST)
        pixels = resized.load()
    except Exception:  # noqa: BLE001
        return ""

    lines: list[str] = []
    for y in range(0, target_h, 2):
        row: list[str] = []
        for x in range(target_w):
            top_dark = pixels[x, y] < _DARK_THRESHOLD
            bottom_dark = pixels[x, y + 1] < _DARK_THRESHOLD if y + 1 < target_h else False
            if top_dark and bottom_dark:
                row.append("█")
            elif top_dark:
                row.append("▀")
            elif bottom_dark:
                row.append("▄")
            else:
                row.append(" ")
        lines.append("".join(row))
    return "\n".join(lines)


def describe_qr_source(data: Any) -> tuple[str, str]:
    """从 ``login --json`` 的 ``data`` 里取 (授权链接, 二维码来源)。

    优先用 PNG 路径（CLI 已落盘），退化到 base64 字段。
    """
    if not isinstance(data, dict):
        return "", ""
    uri = str(data.get("verification_uri") or data.get("verificationUri") or "").strip()
    png = str(data.get("qrcode_path") or data.get("qrcodePath") or "").strip()
    if png:
        return uri, png
    return uri, str(data.get("qr_code") or data.get("qrCode") or "").strip()
