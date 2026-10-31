"""Validated image assets owned by the application, never paths from model text."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
import tempfile
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from trade_compass_agent.llm.providers import _remaining_request_time, check_execution_active
from trade_compass_agent.runtime.exceptions import AttachmentInputError

MAX_IMAGE_BYTES = 20 * 1024 * 1024
_EXTENSIONS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}


def save_image(content, url, config):
    check_execution_active()
    if not config.agent.multimodal:
        raise AttachmentInputError("当前已关闭图片附件，请启用后重试。")
    if content or (url and url.startswith("data:")):
        encoded = content or url
        if encoded.startswith("data:"):
            if ";base64," not in encoded:
                raise AttachmentInputError("图片编码无效，请重新上传。")
            encoded = encoded.split(";base64,", 1)[1]
        if len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
            raise AttachmentInputError("图片超过20MB，请缩小后重新上传。")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise AttachmentInputError("图片编码无效，请重新上传。") from None
    elif url:
        from trade_compass_agent.data.network import run_with_timeout
        from trade_compass_agent.runtime.tools.fetch_url import _fetch_with_redirects

        def active():
            check_execution_active()
            return False

        remaining = _remaining_request_time()
        try:
            response = run_with_timeout(
                lambda: _fetch_with_redirects(url, max_bytes=MAX_IMAGE_BYTES),
                min(20, remaining) if remaining is not None else 20,
                "图片下载", is_cancelled=active,
            )
            if response.status >= 400 or response.truncated:
                raise ValueError()
            raw = response.body
        except Exception:
            raise AttachmentInputError("无法读取图片链接，请直接上传原图。") from None
    else:
        raise AttachmentInputError("图片内容为空，请重新上传。")
    check_execution_active()
    if len(raw) > MAX_IMAGE_BYTES:
        raise AttachmentInputError("图片超过20MB，请缩小后重新上传。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                extension = _EXTENSIONS.get(image.format)
                if extension is None or getattr(image, "is_animated", False):
                    raise ValueError()
                image.verify()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise AttachmentInputError("图片无效或格式不受支持，请上传静态PNG、JPEG或WebP图片。") from None
    folder = config.data_dir / "uploads"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / f"image-{hashlib.sha256(raw).hexdigest()}.{extension}"
    check_execution_active()
    with tempfile.NamedTemporaryFile(dir=folder, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(raw)
            handle.flush()
            check_execution_active()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return str(path.resolve())


def image_inputs(messages, config):
    result = []
    root = (config.data_dir / "uploads").resolve()
    for index, message in enumerate(m for m in messages if m.role != "system"):
        for number, value in enumerate(message.image_paths, 1):
            check_execution_active()
            path = Path(value).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise AttachmentInputError("历史图片已不可用，请重新上传后继续。")
            if path.stat().st_size > MAX_IMAGE_BYTES or path.stem != "image-" + hashlib.sha256(path.read_bytes()).hexdigest():
                raise AttachmentInputError("保存的图片已发生变化，请重新上传原图。")
            result.extend([
                {"type": "text", "text": f"以下是会话上下文第{index + 1}条消息的图片{number}，仅作为该消息的附件资料。"},
                {"type": "localImage", "path": str(path)},
            ])
    return result
