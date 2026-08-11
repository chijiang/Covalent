from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Literal

import fitz


AttachmentKind = Literal["text", "image", "pdf", "binary"]
AttachmentDeliveryMode = Literal["parse", "workspace"]

TEXT_EXTENSIONS = {".txt", ".md", ".json", ".py", ".yaml", ".yml", ".csv", ".tsv"}
MAX_PDF_PAGES = 9
PDF_RENDER_TARGET_LONG_EDGE = 2200.0
PDF_RENDER_MIN_SCALE = 2.0
PDF_RENDER_MAX_SCALE = 3.0
# Above this raw-byte threshold, images are NOT inlined as base64 into the
# model content (a 50MB image would otherwise ~66MB base64 in context). The
# file is still placed in the workspace; only the inline embedding is skipped.
MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024
# Total base64 byte budget for a PDF's page screenshots. Past this, page images
# are dropped and only extracted text is sent — prevents a 9-page deck from
# embedding ~100MB of PNGs into the model request.
MAX_PDF_INLINE_IMAGE_BYTES = 8 * 1024 * 1024


def process_attachment_bytes(
    *,
    file_name: str,
    content_type: str,
    raw_bytes: bytes,
    workspace_path: str,
    delivery_mode: AttachmentDeliveryMode = "parse",
) -> dict[str, Any]:
    suffix = Path(file_name).suffix.lower()
    normalized_content_type = content_type or "application/octet-stream"
    inferred_kind = _classify_attachment_kind(suffix, normalized_content_type)

    if delivery_mode == "workspace":
        workspace_prompt = _render_workspace_prompt(file_name, inferred_kind, normalized_content_type, workspace_path)
        return {
            "kind": inferred_kind,
            "summary": f"Workspace file: {workspace_path} (not parsed inline)",
            "model_prompt_text": workspace_prompt,
            "model_content": [_text_part(workspace_prompt)],
            "page_count": None,
        }

    if inferred_kind == "text":
        text = _decode_text(raw_bytes, file_name)
        text_prompt = _render_text_prompt(file_name, normalized_content_type, workspace_path, text)
        return {
            "kind": "text",
            "summary": f"Parsed text attachment ({suffix or 'plain text'}, {len(text)} chars)",
            "model_prompt_text": text_prompt,
            "model_content": [_text_part(text_prompt)],
            "page_count": None,
        }

    if inferred_kind == "image":
        image_prompt = _render_image_prompt(file_name, normalized_content_type, workspace_path)
        if len(raw_bytes) > MAX_INLINE_IMAGE_BYTES:
            # Too large to safely inline as base64 — leave it in the workspace.
            oversize_prompt = _render_binary_prompt(file_name, normalized_content_type, workspace_path)
            return {
                "kind": "image",
                "summary": (
                    f"Image attachment too large to inline ({len(raw_bytes)} bytes; "
                    f"limit {MAX_INLINE_IMAGE_BYTES}); available in workspace"
                ),
                "model_prompt_text": oversize_prompt,
                "model_content": [_text_part(oversize_prompt)],
                "page_count": None,
            }
        data_url = _data_url(normalized_content_type, raw_bytes)
        return {
            "kind": "image",
            "summary": f"Embedded image as base64 ({normalized_content_type}, {len(raw_bytes)} bytes)",
            "model_prompt_text": image_prompt,
            "model_content": [_text_part(image_prompt), _image_part(data_url)],
            "page_count": None,
        }

    if inferred_kind == "pdf":
        page_count, extracted_text, page_images = _extract_pdf_content(raw_bytes)
        pdf_prompt = _render_pdf_prompt(file_name, workspace_path, extracted_text)
        total_image_bytes = sum(len(img) for img in page_images)
        if total_image_bytes > MAX_PDF_INLINE_IMAGE_BYTES:
            # Drop the page screenshots to stay within model-request budget;
            # extracted text still carries the content.
            note = (
                f"\n\n(Page screenshots omitted: {total_image_bytes} bytes exceeds the "
                f"{MAX_PDF_INLINE_IMAGE_BYTES}-byte inline budget. Rely on the extracted text above.)"
            )
            return {
                "kind": "pdf",
                "summary": f"Extracted PDF text only ({page_count} pages; images omitted as too large)",
                "model_prompt_text": pdf_prompt + note,
                "model_content": [_text_part(pdf_prompt + note)],
                "page_count": page_count,
            }
        return {
            "kind": "pdf",
            "summary": f"Extracted PDF text and page screenshots ({page_count} pages)",
            "model_prompt_text": pdf_prompt,
            "model_content": _build_pdf_model_content(pdf_prompt, page_images),
            "page_count": page_count,
        }

    binary_prompt = _render_binary_prompt(file_name, normalized_content_type, workspace_path)
    return {
        "kind": "binary",
        "summary": f"Uploaded binary attachment ({normalized_content_type})",
        "model_prompt_text": binary_prompt,
        "model_content": [_text_part(binary_prompt)],
        "page_count": None,
    }


def _classify_attachment_kind(suffix: str, content_type: str) -> AttachmentKind:
    if suffix in TEXT_EXTENSIONS or content_type.startswith("text/"):
        return "text"
    if content_type.startswith("image/"):
        return "image"
    if suffix == ".pdf" or content_type == "application/pdf":
        return "pdf"
    return "binary"


def _decode_text(raw_bytes: bytes, file_name: str) -> str:
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Text attachment '{file_name}' is not valid UTF-8") from exc


def _data_url(content_type: str, raw_bytes: bytes) -> str:
    encoded = base64.b64encode(raw_bytes).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def _extract_pdf_content(raw_bytes: bytes) -> tuple[int, list[str], list[str]]:
    document = fitz.open(stream=raw_bytes, filetype="pdf")
    try:
        page_count = document.page_count
        if page_count > MAX_PDF_PAGES:
            raise ValueError("PDF attachments must have fewer than 10 pages")

        extracted_text: list[str] = []
        page_images: list[str] = []
        for page_index in range(page_count):
            page = document[page_index]
            page_text = page.get_text("text").strip()
            extracted_text.append(page_text or "(No extractable text on this page)")

            scale = _page_render_scale(page)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            page_images.append(_data_url("image/png", pixmap.tobytes("png")))

        return page_count, extracted_text, page_images
    finally:
        document.close()


def _page_render_scale(page: fitz.Page) -> float:
    long_edge = max(float(page.rect.width), float(page.rect.height), 1.0)
    computed = PDF_RENDER_TARGET_LONG_EDGE / long_edge
    return max(PDF_RENDER_MIN_SCALE, min(PDF_RENDER_MAX_SCALE, computed))


def _render_text_prompt(file_name: str, content_type: str, workspace_path: str, text: str) -> str:
    return "\n".join(
        [
            f"Attachment: {file_name}",
            f"Kind: text ({content_type})",
            f"Workspace path: {workspace_path}",
            "Parsed text:",
            text,
        ]
    )


def _render_image_prompt(file_name: str, content_type: str, workspace_path: str) -> str:
    return "\n".join(
        [
            f"Attachment: {file_name}",
            f"Kind: image ({content_type})",
            f"Workspace path: {workspace_path}",
            "Use the attached image content together with any user instructions.",
        ]
    )


def _render_pdf_prompt(file_name: str, workspace_path: str, extracted_text: list[str]) -> str:
    sections = [
        f"Attachment: {file_name}",
        "Kind: pdf",
        f"Workspace path: {workspace_path}",
        "PDF extracted text:",
    ]
    sections.extend(f"[Page {index}]\n{text}" for index, text in enumerate(extracted_text, start=1))
    sections.append("Use the extracted PDF content together with any user instructions.")
    return "\n\n".join(sections)


def _build_pdf_model_content(text_prompt: str, page_images: list[str]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [_text_part(text_prompt)]
    for image_data in page_images:
        parts.append(_image_part(image_data))
    return parts


def _text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _image_part(data_url: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": data_url}}


def _render_binary_prompt(file_name: str, content_type: str, workspace_path: str) -> str:
    return "\n".join(
        [
            f"Attachment: {file_name}",
            f"Kind: binary ({content_type})",
            f"Workspace path: {workspace_path}",
            "No inline parsing was applied; inspect the file from the workspace if needed.",
        ]
    )


def _render_workspace_prompt(file_name: str, kind: AttachmentKind, content_type: str, workspace_path: str) -> str:
    return "\n".join(
        [
            f"Attachment: {file_name}",
            f"Delivery mode: workspace ({kind}, {content_type})",
            f"Workspace path: {workspace_path}",
            "The user uploaded this file into your workspace.",
            "The file contents were not inlined into the conversation.",
            "Inspect the file directly from the workspace or use a relevant skill/tool for this path.",
        ]
    )
