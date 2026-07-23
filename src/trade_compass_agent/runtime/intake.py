from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any, Literal

from trade_compass_agent.config import AppConfig
from trade_compass_agent.llm.providers import ChatMessage, create_vision_client, is_vision_capable

AttachmentType = Literal["text", "url", "image", "pdf"]

_MAX_TEXT_CHARS = 50_000
_MAX_TEXT_LINES = 2000
_TRUNCATION_NOTE = "\n... [内容已截断，使用 read_file 工具查看完整内容]"


@dataclass(frozen=True)
class Attachment:
    type: AttachmentType
    content: str | None = None
    url: str | None = None
    mime: str | None = None


def parse_attachments(raw: list[dict[str, Any]] | None) -> list[Attachment]:
    if not raw:
        return []
    result: list[Attachment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type", "")).strip().lower()
        if kind not in {"text", "url", "image", "pdf"}:
            continue
        result.append(
            Attachment(
                type=kind,  # type: ignore[arg-type]
                content=item.get("content"),
                url=item.get("url"),
                mime=item.get("mime"),
            )
        )
    return result


def enrich_user_message(
    message: str,
    attachments: list[Attachment] | list[dict[str, Any]] | None,
    *,
    config: AppConfig,
    memory_dir,
) -> str:
    """Append attachment context blocks to the user message."""
    if not config.agent.multimodal and attachments:
        return message

    parsed = attachments if attachments and isinstance(attachments[0], Attachment) else parse_attachments(attachments)  # type: ignore[arg-type]
    if not parsed:
        return message

    blocks = [message.strip()]
    vision_client = None
    for att in parsed:
        if att.type == "text" and att.content:
            blocks.append(_format_text_attachment(att.content, att.mime))
        elif att.type == "url" and att.url:
            blocks.append(f"[附件·链接: {att.url}]\n（使用 fetch_url 工具获取页面内容）")
        elif att.type == "image":
            block, vision_client = _describe_image(att, config=config, client=vision_client)
            blocks.append(block)
        elif att.type == "pdf" and att.content:
            blocks.append(_format_pdf_attachment(att, config))
        else:
            continue

    enriched = "\n\n".join(block for block in blocks if block.strip())
    return enriched


def _format_text_attachment(content: str, mime: str | None = None) -> str:
    """Format a text attachment with line numbers (like claude-code's formatFileLines)."""
    ext = _ext_from_text_mime(mime)
    lines = content.splitlines()
    truncated = False

    if len(content) > _MAX_TEXT_CHARS:
        content = content[:_MAX_TEXT_CHARS]
        lines = content.splitlines()
        truncated = True
    if len(lines) > _MAX_TEXT_LINES:
        lines = lines[:_MAX_TEXT_LINES]
        truncated = True

    numbered = "\n".join(f"{i + 1:>4}| {line}" for i, line in enumerate(lines))
    suffix = _TRUNCATION_NOTE if truncated else ""
    return f"<file type=\"{ext}\">\n{numbered}{suffix}\n</file>"


def _ext_from_text_mime(mime: str | None) -> str:
    if not mime:
        return "text"
    mapping = {
        "text/plain": "txt",
        "text/markdown": "md",
        "text/csv": "csv",
        "application/json": "json",
        "text/x-python": "python",
        "text/javascript": "javascript",
    }
    return mapping.get(mime, "text")


def _format_pdf_attachment(att: Attachment, config: AppConfig) -> str:
    """Extract text from a PDF attachment and format with page markers."""
    if not att.content:
        return "[附件·PDF] (无内容)"

    raw = att.content
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[1] if "," in raw else raw

    pdf_bytes = base64.b64decode(raw)

    # Save to disk for potential tool-based re-reading
    uploads_dir = config.data_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    filename = f"doc-{int(time.time() * 1000)}.pdf"
    filepath = uploads_dir / filename
    filepath.write_bytes(pdf_bytes)

    text = _extract_pdf_text(pdf_bytes)
    if not text:
        size_kb = len(pdf_bytes) / 1024
        return (
            f"[附件·PDF: {filename} ({size_kb:.0f}KB) 保存于 {filepath}]\n"
            f"（PDF 文字提取失败，可使用 image_ocr 对截图进行文字识别）"
        )

    truncated = False
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS]
        truncated = True

    suffix = _TRUNCATION_NOTE if truncated else ""
    return f"<pdf path=\"{filepath}\">\n{text}{suffix}\n</pdf>"


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes using available libraries."""
    # Try pypdf first (lightweight, no external deps)
    try:
        import pypdf
        import io
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages: list[str] = []
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            if page_text.strip():
                pages.append(f"--- 第 {i + 1} 页 ---\n{page_text.strip()}")
        return "\n\n".join(pages)
    except ImportError:
        pass

    # Try pdfplumber (better for tables)
    try:
        import pdfplumber
        import io
        pages: list[str] = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for i, page in enumerate(pdf.pages):
                page_text = page.extract_text() or ""
                if page_text.strip():
                    pages.append(f"--- 第 {i + 1} 页 ---\n{page_text.strip()}")
        return "\n\n".join(pages)
    except ImportError:
        pass

    return ""


def _describe_image(att: Attachment, *, config: AppConfig, client):
    if att.content:
        data_url = att.content if att.content.startswith("data:") else f"data:{att.mime or 'image/png'};base64,{att.content}"
        label = "data-url image"
    elif att.url:
        data_url = att.url
        label = f"image URL {att.url}"
    else:
        return "[附件·图片] (no content or url)", client

    if not config.agent.multimodal:
        return f"[附件·图片 {label}] (vision disabled in config)", client

    # Path A: Main model supports vision → describe immediately (like before)
    if is_vision_capable(config.llm.model):
        try:
            if client is None:
                client = create_vision_client(config)
            if client is None:
                return _save_image_fallback(att, config), client
            description = _vision_describe(client, data_url, att.mime)
            return f"[附件·图片描述]\n{description}", client
        except Exception as exc:
            return f"[附件·图片 {label}] (vision failed: {exc})", client

    # Path B: Main model is text-only → save to disk, insert path reference
    # Model can use image_ocr / image_analyze tools to examine it
    return _save_image_fallback(att, config), client


def _save_image_fallback(att: Attachment, config: AppConfig) -> str:
    """Save image to disk and return a path-reference placeholder."""
    uploads_dir = config.data_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    filename = f"image-{int(time.time() * 1000)}.{_ext_from_mime(att.mime)}"
    filepath = uploads_dir / filename

    if att.content:
        raw = att.content
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[1] if "," in raw else raw
        filepath.write_bytes(base64.b64decode(raw))
    elif att.url:
        return f"[附件·图片 URL: {att.url}]\n（使用 image_analyze 工具分析此图片）"
    else:
        return "[附件·图片] (无内容)"

    size_kb = filepath.stat().st_size / 1024
    return (
        f"[附件·图片: {filepath.name} ({size_kb:.0f}KB) 保存于 {filepath}]\n"
        f"（使用 image_ocr 提取文字，或 image_analyze 进行图片分析）"
    )


def _ext_from_mime(mime: str | None) -> str:
    if not mime:
        return "png"
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/bmp": "bmp",
    }.get(mime, "png")


def _vision_describe(client, data_url: str, mime: str | None) -> str:
    image_url = data_url
    if not data_url.startswith("data:") and not data_url.startswith("http"):
        image_url = f"data:{mime or 'image/png'};base64,{data_url}"

    messages = [
        ChatMessage(
            role="user",
            content="Describe this trading-related image briefly in Chinese (chart, table, or screenshot).",
        )
    ]
    # OpenAI vision via raw client when available
    if hasattr(client, "_client"):
        response = client._client.chat.completions.create(
            model=client.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image briefly in Chinese."},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            max_tokens=300,
        )
        return (response.choices[0].message.content or "").strip() or "(empty vision response)"

    completion = client.complete(messages)
    return (completion.content or "").strip() or "(empty vision response)"


def attachment_from_file_bytes(data: bytes, mime: str) -> Attachment:
    encoded = base64.b64encode(data).decode("ascii")
    return Attachment(type="image", content=encoded, mime=mime)
