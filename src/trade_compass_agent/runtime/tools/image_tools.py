"""Image tools: image_ocr and image_analyze.

When the main LLM is text-only (e.g. DeepSeek), images are saved to disk and
referenced by path. These tools let the model retrieve image content on demand:

- image_ocr: local OCR text extraction (no external API needed)
- image_analyze: semantic description via a configured vision model
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.providers import ChatMessage, create_vision_client

logger = logging.getLogger(__name__)

_SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}


def _resolve_image_path(path_str: str, config: AppConfig) -> Path | None:
    """Resolve an image path (absolute or relative to data/uploads)."""
    p = Path(path_str)
    if p.is_absolute() and p.is_file():
        return p
    candidate = config.data_dir / "uploads" / path_str
    if candidate.is_file():
        return candidate
    candidate2 = config.data_dir / path_str
    if candidate2.is_file():
        return candidate2
    return None


def _validate_image(path: Path) -> str | None:
    """Return error message if file is not a valid image, else None."""
    if not path.is_file():
        return f"文件不存在: {path}"
    if path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        return f"不支持的图片格式: {path.suffix}"
    if path.stat().st_size > 20 * 1024 * 1024:
        return "图片文件过大 (>20MB)"
    return None


def tool_image_ocr(config: AppConfig, **kwargs: Any) -> str:
    """Extract text from an image using local OCR."""
    path_str = str(kwargs.get("path", ""))
    if not path_str:
        return json.dumps({"error": "path is required"}, ensure_ascii=False)

    path = _resolve_image_path(path_str, config)
    if path is None:
        return json.dumps({"error": f"找不到图片: {path_str}"}, ensure_ascii=False)

    err = _validate_image(path)
    if err:
        return json.dumps({"error": err}, ensure_ascii=False)

    try:
        text = _ocr_extract(path)
        if not text.strip():
            return json.dumps(
                {"path": str(path), "text": "", "note": "未识别到文字内容（图片可能是图表/非文字图片）"},
                ensure_ascii=False,
            )
        return json.dumps({"path": str(path), "text": text}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"OCR 失败: {exc}"}, ensure_ascii=False)


def tool_image_analyze(config: AppConfig, **kwargs: Any) -> str:
    """Describe/analyze an image using a configured vision model."""
    path_str = str(kwargs.get("path", ""))
    prompt = str(kwargs.get("prompt", "请描述这张图片的内容，重点关注与股票/金融/交易相关的信息。"))

    if not path_str:
        return json.dumps({"error": "path is required"}, ensure_ascii=False)

    path = _resolve_image_path(path_str, config)
    if path is None:
        return json.dumps({"error": f"找不到图片: {path_str}"}, ensure_ascii=False)

    err = _validate_image(path)
    if err:
        return json.dumps({"error": err}, ensure_ascii=False)

    client = create_vision_client(config)
    if client is None:
        return json.dumps(
            {"error": "未配置 vision model。请在 config/default.yaml 设置 llm.vision_model"},
            ensure_ascii=False,
        )

    try:
        image_data = base64.b64encode(path.read_bytes()).decode("ascii")
        mime = _mime_from_path(path)
        data_url = f"data:{mime};base64,{image_data}"

        # Ollama's /v1 endpoint has bugs with vision; use native API if available
        if hasattr(client, "name") and "ollama" in getattr(client, "name", ""):
            description = _ollama_vision(client.model, image_data, prompt)
        elif hasattr(client, "_client"):
            response = client._client.chat.completions.create(
                model=client.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                max_tokens=1000,
            )
            description = (response.choices[0].message.content or "").strip()
        else:
            completion = client.complete([ChatMessage(role="user", content=prompt)])
            description = (completion.content or "").strip()

        if not description:
            return json.dumps({"error": "vision model 返回空响应"}, ensure_ascii=False)

        return json.dumps(
            {"path": str(path), "description": description},
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"error": f"图片分析失败: {exc}"}, ensure_ascii=False)


def _ollama_vision(model: str, image_b64: str, prompt: str) -> str:
    """Call Ollama's native /api/chat endpoint for vision (more reliable than /v1)."""
    import httpx
    response = httpx.post(
        "http://localhost:11434/api/chat",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
            "stream": False,
        },
        timeout=120.0,
    )
    response.raise_for_status()
    return response.json().get("message", {}).get("content", "")


def _ocr_extract(path: Path) -> str:
    """Try OCR on macOS (Vision framework via pyobjc) or fall back to pytesseract."""
    import subprocess
    import sys

    if sys.platform == "darwin":
        text = _ocr_macos_vision(path)
        if text is not None:
            return text

    try:
        import pytesseract
        from PIL import Image
        img = Image.open(path)
        return pytesseract.image_to_string(img, lang="chi_sim+eng")
    except ImportError:
        pass

    result = subprocess.run(
        ["tesseract", str(path), "stdout", "-l", "chi_sim+eng"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == 0:
        return result.stdout
    raise RuntimeError(f"OCR 不可用: 请安装 tesseract 或 pytesseract（{result.stderr[:200]}）")


def _ocr_macos_vision(path: Path) -> str | None:
    """Use macOS Vision framework for OCR if pyobjc is available."""
    try:
        import objc  # noqa: F401
        import Vision
        import Quartz

        image_url = Quartz.CFURLCreateFromFileSystemRepresentation(
            None, str(path).encode(), len(str(path).encode()), False
        )
        ci_image = Quartz.CIImage.imageWithContentsOfURL_(image_url)
        if ci_image is None:
            return None

        handler = Vision.VNImageRequestHandler.alloc().initWithCIImage_options_(ci_image, None)
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLanguages_(["zh-Hans", "en"])
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)

        handler.performRequests_error_([request], None)
        results = request.results()
        if not results:
            return ""
        lines = []
        for obs in results:
            candidates = obs.topCandidates_(1)
            if candidates:
                lines.append(candidates[0].string())
        return "\n".join(lines)
    except (ImportError, Exception):
        return None


def _mime_from_path(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
    }.get(ext, "image/png")
