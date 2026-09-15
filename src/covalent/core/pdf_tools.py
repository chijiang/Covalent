from __future__ import annotations

import json
from typing import Any

import pymupdf

from covalent.core.attachment_processing import (
    MAX_PDF_INLINE_IMAGE_BYTES,
    render_pdf_page_data_url,
)
from covalent.core.workspace_tools import (
    _get_session_workspace_root,
    _relative_path,
    _resolve_workspace_path,
)

PDF_MAX_PAGES_PER_CALL = 9
PDF_MAX_TEXT_CHARS_PER_CALL = 60_000

_READ_PDF_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_pdf",
        "description": (
            "Read a PDF file from the session workspace. "
            "mode='text' extracts page text — use for plain text questions. "
            "mode='image' renders page screenshots for vision — use for scans, "
            "figures, tables, or layout-dependent questions (requires a vision-capable model). "
            "Select pages with 'pages' (e.g. '1,3,5-8' or 'all'); at most 9 pages per call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path to the PDF, e.g. uploads/report.pdf",
                },
                "pages": {
                    "type": "string",
                    "default": "1",
                    "description": "Page selector: 'all', a single page '3', or a list/ranges like '1,3,5-8'. Max 9 pages per call.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["text", "image"],
                    "default": "text",
                },
            },
            "required": ["path"],
        },
    },
}


def register_pdf_tools(registry: Any, settings: Any) -> None:
    registry.register_local_tool(
        "read_pdf",
        _READ_PDF_SCHEMA,
        handler=lambda args, ctx: _read_pdf(settings, ctx, args),
    )


def _parse_page_selector(raw: str | None, page_count: int) -> list[int]:
    selector = (raw or "").strip() or "1"
    if page_count <= 0:
        raise ValueError("PDF has no pages")
    if selector.lower() == "all":
        pages = list(range(1, page_count + 1))
    else:
        pages: list[int] = []
        for token in selector.split(","):
            token = token.strip()
            if not token:
                continue
            if "-" in token:
                start_raw, _, end_raw = token.partition("-")
                try:
                    start, end = int(start_raw), int(end_raw)
                except ValueError as exc:
                    raise ValueError(f"Invalid page selector: '{token}'") from exc
                if start < 1 or end < start:
                    raise ValueError(f"Invalid page range: '{token}'")
                pages.extend(range(start, end + 1))
            else:
                try:
                    pages.append(int(token))
                except ValueError as exc:
                    raise ValueError(f"Invalid page selector: '{token}'") from exc
    unique = sorted(set(pages))
    out_of_range = [page for page in unique if page < 1 or page > page_count]
    if out_of_range:
        raise ValueError(f"Page(s) {out_of_range} out of range; document has {page_count} page(s)")
    if not unique:
        raise ValueError(f"No pages selected; document has {page_count} page(s)")
    if len(unique) > PDF_MAX_PAGES_PER_CALL:
        raise ValueError(
            f"{len(unique)} pages requested; at most {PDF_MAX_PAGES_PER_CALL} pages per call. "
            "Narrow the 'pages' selector and call again."
        )
    return unique


def _read_pdf(settings: Any, context: Any, args: dict[str, Any]) -> str | list[dict[str, Any]]:
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        raise ValueError("read_pdf requires a 'path' argument")
    mode = str(args.get("mode") or "text").strip().lower()
    if mode not in {"text", "image"}:
        raise ValueError(f"Invalid mode '{mode}'; expected 'text' or 'image'")

    root = _get_session_workspace_root(settings, context)
    resolved = _resolve_workspace_path(root, raw_path, must_exist=True)
    if not resolved.is_file():
        raise ValueError(f"Workspace path is not a file: {_relative_path(root, resolved)}")
    if resolved.suffix.lower() != ".pdf":
        with resolved.open("rb") as handle:
            header = handle.read(5)
        if header != b"%PDF-":
            raise ValueError(f"Not a PDF file: {_relative_path(root, resolved)}")

    try:
        document = pymupdf.open(resolved)
    except Exception as exc:
        raise ValueError(f"Cannot open PDF '{_relative_path(root, resolved)}': {exc}") from exc

    try:
        pages = _parse_page_selector(str(args.get("pages") or ""), document.page_count)
        display_path = _relative_path(root, resolved)
        if mode == "text":
            return _read_pdf_text(document, pages, document.page_count, display_path)
        return _read_pdf_images(document, pages, document.page_count, display_path)
    finally:
        document.close()


def _read_pdf_text(
    document: pymupdf.Document,
    pages: list[int],
    page_count: int,
    display_path: str,
) -> str:
    sections: list[str] = []
    for page_number in pages:
        page_text = document[page_number - 1].get_text("text").strip()
        if not page_text:
            page_text = "(No extractable text on this page; try mode='image')"
        sections.append(f"[Page {page_number}]\n{page_text}")

    content = "\n\n".join(sections)
    truncated = len(content) > PDF_MAX_TEXT_CHARS_PER_CALL
    if truncated:
        content = content[:PDF_MAX_TEXT_CHARS_PER_CALL] + "\n...[truncated]"
    return json.dumps(
        {
            "path": display_path,
            "pages": pages,
            "page_count": page_count,
            "truncated": truncated,
            **({"note": "Text truncated; request fewer pages per call."} if truncated else {}),
            "content": content,
        },
        ensure_ascii=False,
    )


def _read_pdf_images(
    document: pymupdf.Document,
    pages: list[int],
    page_count: int,
    display_path: str,
) -> str | list[dict[str, Any]]:
    encoded_bytes = 0
    data_urls: list[str] = []
    for page_number in pages:
        data_url = render_pdf_page_data_url(document[page_number - 1])
        encoded_bytes += len(data_url)
        data_urls.append(data_url)
        if encoded_bytes > MAX_PDF_INLINE_IMAGE_BYTES:
            return json.dumps(
                {
                    "error": (
                        f"Rendering pages {pages[: len(data_urls)]}... exceeds the inline image budget "
                        f"({MAX_PDF_INLINE_IMAGE_BYTES} bytes). Request fewer pages per call or use mode='text'."
                    )
                },
                ensure_ascii=False,
            )

    return [
        {
            "type": "text",
            "text": (
                f"PDF {display_path} pages {pages} rendered as images "
                f"(of {page_count} total pages)"
            ),
        },
        *({"type": "image_url", "image_url": {"url": data_url}} for data_url in data_urls),
    ]
