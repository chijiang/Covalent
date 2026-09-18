from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import mimetypes
import os
import re
import shutil
import threading
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    pass

_DEFAULT_MAX_BYTES = 24_000
_DEFAULT_SEARCH_MAX_MATCHES = 200
_DEFAULT_ZIP_MAX_ENTRIES = 10_000
# `full: true` 设硬顶：整份大文件进入会话上下文后，后续每次模型调用都要重新
# prefill。更大的文件请用 offset/length 或行范围读取；截断提示里已给出替代参数。
_FULL_READ_MAX_BYTES = 96_000


def _get_session_workspace_root(settings: Any, context: Any) -> Path:
    """Get the workspace root for the current execution scope, or the base root
    if session workspaces are disabled.

    The scope key resolves as ``workspace_scope_id`` → ``execution_scope_id`` →
    legacy ``session_id`` (during migration). When the run context carries an
    ``execution_backend`` (production), resolve through it so the backend owns
    workspace access (ready for a remote backend in Phase 3). Otherwise fall
    back to settings-derived resolution (tests, legacy/admin paths).
    """
    backend = getattr(context, "execution_backend", None)
    scope_id = (
        getattr(context, "workspace_scope_id", None)
        or getattr(context, "execution_scope_id", None)
        or getattr(context, "session_id", None)
    )
    if backend is not None and isinstance(scope_id, str) and scope_id.strip():
        host_path = backend.workspace(scope_id).host_path
        if host_path is None:
            raise RuntimeError("execution backend has no host_path for the workspace (remote workspace is Phase 3)")
        return host_path
    if isinstance(scope_id, str) and scope_id.strip():
        return settings.session_workspace_dir(scope_id)
    if settings.session_workspace_enabled and scope_id is None:
        raise ValueError("Session workspace is enabled but no execution scope id found in context")
    return settings.workspace_root()


def register_workspace_tools(registry: Any, settings: Any, *, download_base_path: str = "/api/backend/downloads") -> None:
    registry.register_local_tool(
        "list_workspace_files",
        {
            "type": "function",
            "function": {
                "name": "list_workspace_files",
                "description": "List files and directories inside the workspace root.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "default": "."},
                        "recursive": {"type": "boolean", "default": False},
                        "include_hidden": {"type": "boolean", "default": False},
                        "max_entries": {"type": "integer", "default": 200, "minimum": 1},
                    },
                },
            },
        },
        handler=lambda args, ctx: _list_workspace_files(settings, ctx, args),
    )
    registry.register_local_tool(
        "read_workspace_file",
        {
            "type": "function",
            "function": {
                "name": "read_workspace_file",
                "description": "Read a file from the workspace root as UTF-8 text or base64 for binary content, optionally scoped to a byte or line range.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "full": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Read the full file instead of truncating to max_bytes. "
                                f"Capped at {_FULL_READ_MAX_BYTES} bytes; use offset/length or line ranges for larger files."
                            ),
                        },
                        "max_bytes": {"type": "integer", "default": _DEFAULT_MAX_BYTES, "minimum": 1},
                        "offset": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Zero-based byte offset to start reading from.",
                        },
                        "length": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Maximum number of bytes to read from offset.",
                        },
                        "start_line": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "One-based line number to start reading from.",
                        },
                        "end_line": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "One-based inclusive line number to stop reading at.",
                        },
                        "include_line_numbers": {"type": "boolean", "default": False},
                    },
                    "required": ["path"],
                },
            },
        },
        handler=lambda args, ctx: _read_workspace_file(settings, ctx, args),
    )
    registry.register_local_tool(
        "search_workspace_files",
        {
            "type": "function",
            "function": {
                "name": "search_workspace_files",
                "description": "Search UTF-8 text files in the workspace, similar to grep, and return matching lines with optional context.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string", "default": "."},
                        "glob": {"type": "string", "description": "Optional glob pattern such as **/*.py."},
                        "regex": {"type": "boolean", "default": False},
                        "case_sensitive": {"type": "boolean", "default": True},
                        "include_hidden": {"type": "boolean", "default": False},
                        "context_lines": {"type": "integer", "default": 0, "minimum": 0},
                        "max_matches": {
                            "type": "integer",
                            "default": _DEFAULT_SEARCH_MAX_MATCHES,
                            "minimum": 1,
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        handler=lambda args, ctx: _search_workspace_files(settings, ctx, args),
    )
    registry.register_local_tool(
        "edit_workspace_file",
        {
            "type": "function",
            "function": {
                "name": "edit_workspace_file",
                "description": "Edit a UTF-8 workspace file by replacing exact text or a precise one-based line/column range.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "mode": {"type": "string", "enum": ["replace_text", "replace_range"]},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string", "default": ""},
                        "replacements": {
                            "type": "array",
                            "description": (
                                "replace_text mode only: apply several exact text replacements in ONE atomic call "
                                "(preferred for patching structured files — text-anchored, immune to line shifts). "
                                "Each old_text must match exactly once when its turn comes; if any entry fails "
                                "validation nothing is written. Mutually exclusive with old_text/new_text."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "old_text": {"type": "string"},
                                    "new_text": {"type": "string", "default": ""},
                                },
                                "required": ["old_text"],
                            },
                        },
                        "start_line": {"type": "integer", "minimum": 1},
                        "start_column": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                        "end_column": {"type": "integer", "minimum": 1},
                        "expected_sha256": {
                            "type": "string",
                            "description": "Optional SHA-256 hex digest that the current file content must match before editing.",
                        },
                    },
                    "required": ["path", "mode"],
                },
            },
        },
        handler=lambda args, ctx: _edit_workspace_file(settings, ctx, args),
    )
    registry.register_local_tool(
        "write_workspace_file",
        {
            "type": "function",
            "function": {
                "name": "write_workspace_file",
                "description": "Write a UTF-8 or base64-encoded file within the workspace root.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "encoding": {"type": "string", "enum": ["utf-8", "base64"], "default": "utf-8"},
                        "create_parents": {"type": "boolean", "default": True},
                        "overwrite": {"type": "boolean", "default": True},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        handler=lambda args, ctx: _write_workspace_file(settings, ctx, args),
    )
    registry.register_local_tool(
        "create_workspace_directory",
        {
            "type": "function",
            "function": {
                "name": "create_workspace_directory",
                "description": "Create a directory inside the workspace root.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                    },
                    "required": ["path"],
                },
            },
        },
        handler=lambda args, ctx: _create_workspace_directory(settings, ctx, args),
    )
    registry.register_local_tool(
        "copy_workspace_entry",
        {
            "type": "function",
            "function": {
                "name": "copy_workspace_entry",
                "description": "Copy a file or directory inside the workspace root.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source_path": {"type": "string"},
                        "destination_path": {"type": "string"},
                        "overwrite": {"type": "boolean", "default": False},
                        "create_parents": {"type": "boolean", "default": True},
                    },
                    "required": ["source_path", "destination_path"],
                },
            },
        },
        handler=lambda args, ctx: _copy_workspace_entry(settings, ctx, args),
    )
    registry.register_local_tool(
        "move_workspace_entry",
        {
            "type": "function",
            "function": {
                "name": "move_workspace_entry",
                "description": "Move or rename a file or directory inside the workspace root.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source_path": {"type": "string"},
                        "destination_path": {"type": "string"},
                        "overwrite": {"type": "boolean", "default": False},
                        "create_parents": {"type": "boolean", "default": True},
                    },
                    "required": ["source_path", "destination_path"],
                },
            },
        },
        handler=lambda args, ctx: _move_workspace_entry(settings, ctx, args),
    )
    registry.register_local_tool(
        "delete_workspace_entry",
        {
            "type": "function",
            "function": {
                "name": "delete_workspace_entry",
                "description": "Delete a file or directory inside the workspace root.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "recursive": {"type": "boolean", "default": False},
                        "missing_ok": {"type": "boolean", "default": False},
                    },
                    "required": ["path"],
                },
            },
        },
        handler=lambda args, ctx: _delete_workspace_entry(settings, ctx, args),
    )
    registry.register_local_tool(
        "zip_workspace_entries",
        {
            "type": "function",
            "function": {
                "name": "zip_workspace_entries",
                "description": "Create a zip archive from files or directories inside the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Workspace files or directories to include.",
                        },
                        "output_path": {"type": "string", "description": "Workspace path for the generated .zip file."},
                        "include_hidden": {"type": "boolean", "default": False},
                        "overwrite": {"type": "boolean", "default": False},
                        "max_entries": {"type": "integer", "default": _DEFAULT_ZIP_MAX_ENTRIES, "minimum": 1},
                    },
                    "required": ["paths", "output_path"],
                },
            },
        },
        handler=lambda args, ctx: _zip_workspace_entries(settings, ctx, args),
    )
    registry.register_local_tool(
        "unzip_workspace_archive",
        {
            "type": "function",
            "function": {
                "name": "unzip_workspace_archive",
                "description": "Extract a zip archive into the workspace while rejecting entries that escape the destination.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "archive_path": {"type": "string"},
                        "output_dir": {"type": "string", "default": "."},
                        "overwrite": {"type": "boolean", "default": False},
                        "max_entries": {"type": "integer", "default": _DEFAULT_ZIP_MAX_ENTRIES, "minimum": 1},
                    },
                    "required": ["archive_path"],
                },
            },
        },
        handler=lambda args, ctx: _unzip_workspace_archive(settings, ctx, args),
    )
    registry.register_local_tool(
        "publish_downloadable_file",
        {
            "type": "function",
            "function": {
                "name": "publish_downloadable_file",
                "description": (
                    "Copy an existing workspace file into the session download area "
                    "and return a user-downloadable link. Use this after generating a file that the user should download. "
                    "Pass the generated file location as file_path."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Path to the existing generated file inside the workspace.",
                        },
                        "download_name": {
                            "type": "string",
                            "description": "Optional filename to present to the user when downloading.",
                        },
                        "content_type": {
                            "type": "string",
                            "description": "Optional MIME type override for the published download.",
                        },
                        "description": {
                            "type": "string",
                            "description": "Optional short user-facing description shown alongside the download.",
                        },
                        "delete_source": {
                            "type": "boolean",
                            "default": False,
                            "description": "Delete the original file after publishing it.",
                        },
                    },
                    "required": ["file_path"],
                },
            },
        },
        handler=lambda args, ctx: _publish_downloadable_file(settings, ctx, download_base_path, args),
    )


def _list_workspace_files(settings: Any, context: Any, args: dict[str, Any]) -> str:
    root = _get_session_workspace_root(settings, context)
    target = _resolve_workspace_path(root, str(args.get("path", ".")), must_exist=True)
    recursive = bool(args.get("recursive", False))
    include_hidden = bool(args.get("include_hidden", False))
    max_entries = max(1, int(args.get("max_entries", 200)))

    entries: list[dict[str, Any]] = []
    if target.is_file():
        entries.append(_entry_for_path(root, target))
    else:
        iterator = target.rglob("*") if recursive else target.iterdir()
        for candidate in sorted(iterator):
            if not include_hidden and _is_hidden(candidate, root):
                continue
            entries.append(_entry_for_path(root, candidate))
            if len(entries) >= max_entries:
                break

    return json.dumps(
        {
            "root": str(root),
            "path": _relative_path(root, target),
            "entries": entries,
            "truncated": len(entries) >= max_entries,
        },
        ensure_ascii=False,
        indent=2,
    )


def _read_workspace_file(settings: Any, context: Any, args: dict[str, Any]) -> str:
    root = _get_session_workspace_root(settings, context)
    target = _resolve_workspace_path(root, str(args.get("path", "")), must_exist=True)
    if not target.is_file():
        raise ValueError(f"Workspace path is not a file: {_relative_path(root, target)}")
    raw = target.read_bytes()
    size_bytes = len(raw)
    offset = _optional_int(args.get("offset"))
    length = _optional_int(args.get("length"))
    start_line = _optional_int(args.get("start_line"))
    end_line = _optional_int(args.get("end_line"))
    include_line_numbers = bool(args.get("include_line_numbers", False))
    full = bool(args.get("full", False))

    if offset is not None and offset < 0:
        raise ValueError("offset must be non-negative")
    if length is not None and length < 1:
        raise ValueError("length must be at least 1")
    if (offset is not None or length is not None) and (start_line is not None or end_line is not None):
        raise ValueError("Use either byte range arguments or line range arguments, not both")

    range_info: dict[str, Any] = {}
    truncated = False
    if offset is not None or length is not None:
        start = offset or 0
        if start > size_bytes:
            raise ValueError(f"offset {start} exceeds file size {size_bytes}")
        stop = size_bytes if length is None else min(size_bytes, start + length)
        content_bytes = raw[start:stop]
        truncated = stop < size_bytes
        range_info = {"offset": start, "length": len(content_bytes)}
    elif start_line is not None or end_line is not None:
        content = _read_text_file(target)
        lines = content.splitlines(keepends=True)
        first_line = start_line or 1
        last_line = end_line or len(lines)
        if first_line < 1:
            raise ValueError("start_line must be at least 1")
        if last_line < first_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        selected_lines = lines[first_line - 1 : last_line]
        if include_line_numbers:
            selected_text = "".join(
                f"{line_no}: {line}"
                for line_no, line in zip(
                    range(first_line, first_line + len(selected_lines)),
                    selected_lines,
                    strict=False,
                )
            )
        else:
            selected_text = "".join(selected_lines)
        return json.dumps(
            {
                "path": _relative_path(root, target),
                "encoding": "utf-8",
                "content": selected_text,
                "truncated": last_line < len(lines),
                "size_bytes": size_bytes,
                "start_line": first_line,
                "end_line": first_line + len(selected_lines) - 1 if selected_lines else first_line - 1,
                "total_lines": len(lines),
            },
            ensure_ascii=False,
            indent=2,
        )
    else:
        if full:
            max_bytes = min(size_bytes, _FULL_READ_MAX_BYTES)
        else:
            max_bytes = max(1, int(args.get("max_bytes", _DEFAULT_MAX_BYTES)))
        truncated = size_bytes > max_bytes
        content_bytes = raw[:max_bytes] if truncated else raw

    try:
        content = content_bytes.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(content_bytes).decode("ascii")
        encoding = "base64"

    payload: dict[str, Any] = {
        "path": _relative_path(root, target),
        "encoding": encoding,
        "content": content,
        "truncated": truncated,
        "size_bytes": size_bytes,
        **range_info,
    }
    if full and truncated:
        payload["full_read_capped"] = True
        payload["hint"] = (
            f"Full reads are capped at {_FULL_READ_MAX_BYTES} bytes. Continue with offset="
            f"{len(content_bytes)}, or read a start_line/end_line range, or search_workspace_files."
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _search_workspace_files(settings: Any, context: Any, args: dict[str, Any]) -> str:
    root = _get_session_workspace_root(settings, context)
    target = _resolve_workspace_path(root, str(args.get("path", ".")), must_exist=True)
    query = str(args.get("query", ""))
    if not query:
        raise ValueError("query is required")
    glob_pattern = str(args.get("glob") or "").strip()
    use_regex = bool(args.get("regex", False))
    case_sensitive = bool(args.get("case_sensitive", True))
    include_hidden = bool(args.get("include_hidden", False))
    context_lines = max(0, int(args.get("context_lines", 0)))
    max_matches = max(1, int(args.get("max_matches", _DEFAULT_SEARCH_MAX_MATCHES)))

    if use_regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query, flags)
        except re.error as exc:
            raise ValueError(f"Invalid regex query: {exc}") from exc

        def matches(line: str) -> bool:
            return pattern.search(line) is not None

    else:
        needle = query if case_sensitive else query.lower()

        def matches(line: str) -> bool:
            haystack = line if case_sensitive else line.lower()
            return needle in haystack

    files = [target] if target.is_file() else sorted(candidate for candidate in target.rglob("*") if candidate.is_file())
    results: list[dict[str, Any]] = []
    scanned_files = 0
    skipped_binary = 0
    for candidate in files:
        relative = _relative_path(root, candidate)
        if not include_hidden and _is_hidden(candidate, root):
            continue
        if glob_pattern and not (
            fnmatch.fnmatch(relative, glob_pattern)
            or fnmatch.fnmatch(candidate.name, glob_pattern)
        ):
            continue
        try:
            text = _read_text_file(candidate)
        except UnicodeDecodeError:
            skipped_binary += 1
            continue
        scanned_files += 1
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if not matches(line):
                continue
            start = max(0, index - context_lines)
            stop = min(len(lines), index + context_lines + 1)
            context_rows = [
                {"line_number": line_number, "text": lines[line_number - 1]}
                for line_number in range(start + 1, stop + 1)
            ]
            results.append(
                {
                    "path": relative,
                    "line_number": index + 1,
                    "line": line,
                    "context": context_rows,
                }
            )
            if len(results) >= max_matches:
                return json.dumps(
                    {
                        "query": query,
                        "path": _relative_path(root, target),
                        "matches": results,
                        "match_count": len(results),
                        "scanned_files": scanned_files,
                        "skipped_binary_files": skipped_binary,
                        "truncated": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                )

    return json.dumps(
        {
            "query": query,
            "path": _relative_path(root, target),
            "matches": results,
            "match_count": len(results),
            "scanned_files": scanned_files,
            "skipped_binary_files": skipped_binary,
            "truncated": False,
        },
        ensure_ascii=False,
        indent=2,
    )


def _edit_workspace_file(settings: Any, context: Any, args: dict[str, Any]) -> str:
    root = _get_session_workspace_root(settings, context)
    target = _resolve_workspace_path(root, str(args.get("path", "")), must_exist=True)
    if not target.is_file():
        raise ValueError(f"Workspace path is not a file: {_relative_path(root, target)}")

    mode = str(args.get("mode", ""))
    new_text = str(args.get("new_text", ""))
    # The lock covers the WHOLE read-check-modify-write transaction: reading
    # outside it would let two agents observe the same base content and
    # silently drop the first writer's change.
    with _WorkspacePathLock(target):
        original = _read_text_file(target)
        expected_sha256 = str(args.get("expected_sha256") or "").strip()
        if expected_sha256:
            actual_sha256 = _sha256_text(original)
            if actual_sha256 != expected_sha256:
                raise ValueError("File content does not match expected_sha256")

        replacements = 0
        if mode == "replace_text":
            batch = args.get("replacements")
            if batch is not None:
                # 批量文本替换：逐条"校验恰好命中一次→应用"，全部成功才在锁内落盘。
                # 任一条失败即刻中止且不写入（updated 尚未落盘），天然免疫行号漂移——
                # 锚点是文本而非行号，是并发/批量修补 plan 等结构化文件的首选方式。
                if not isinstance(batch, list) or not batch:
                    raise ValueError("replacements must be a non-empty array when provided")
                if args.get("old_text") is not None or args.get("new_text"):
                    raise ValueError("use either replacements or old_text/new_text, not both")
                updated = original
                for index, entry in enumerate(batch, 1):
                    if not isinstance(entry, dict):
                        raise ValueError(f"replacements[{index}] must be an object with old_text/new_text")
                    old = entry.get("old_text")
                    new = str(entry.get("new_text", ""))
                    if not isinstance(old, str) or old == "":
                        raise ValueError(f"replacements[{index}].old_text is required and must be non-empty")
                    occurrences = updated.count(old)
                    if occurrences != 1:
                        raise ValueError(
                            f"replacements[{index}].old_text must match exactly once; found {occurrences} matches. "
                            "No changes were written."
                        )
                    updated = updated.replace(old, new, 1)
                replacements = len(batch)
            else:
                old_text = args.get("old_text")
                if not isinstance(old_text, str) or old_text == "":
                    raise ValueError("old_text is required for replace_text")
                occurrences = original.count(old_text)
                if occurrences != 1:
                    raise ValueError(f"old_text must match exactly once; found {occurrences} matches")
                updated = original.replace(old_text, new_text, 1)
                replacements = 1
        elif mode == "replace_range":
            updated = _replace_text_range(
                original,
                _required_positive_int(args, "start_line"),
                _required_positive_int(args, "start_column"),
                _required_positive_int(args, "end_line"),
                _required_positive_int(args, "end_column"),
                new_text,
            )
            replacements = 1
        else:
            raise ValueError("mode must be 'replace_text' or 'replace_range'")

        _atomic_write_bytes(target, updated.encode("utf-8"))
    return json.dumps(
        {
            "path": _relative_path(root, target),
            "mode": mode,
            "replacements": replacements,
            "size_bytes": len(updated.encode("utf-8")),
            "sha256": _sha256_text(updated),
        },
        ensure_ascii=False,
        indent=2,
    )


def _write_workspace_file(settings: Any, context: Any, args: dict[str, Any]) -> str:
    root = _get_session_workspace_root(settings, context)
    target = _resolve_workspace_path(root, str(args.get("path", "")), must_exist=False)
    create_parents = bool(args.get("create_parents", True))
    overwrite = bool(args.get("overwrite", True))
    if target.exists() and target.is_dir():
        raise ValueError(f"Cannot write file over directory: {_relative_path(root, target)}")
    if target.exists() and not overwrite:
        raise ValueError(f"Workspace file already exists: {_relative_path(root, target)}")
    if create_parents:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.parent.exists():
        raise ValueError(f"Parent directory does not exist: {_relative_path(root, target.parent)}")

    encoding = str(args.get("encoding", "utf-8"))
    content = str(args.get("content", ""))
    if encoding == "base64":
        data = base64.b64decode(content.encode("ascii"))
    elif encoding == "utf-8":
        data = content.encode("utf-8")
    else:
        raise ValueError(f"Unsupported encoding: {encoding}")

    with _WorkspacePathLock(target):
        existed = target.exists()
        if target.exists() and not overwrite:
            raise ValueError(f"Workspace file already exists: {_relative_path(root, target)}")
        _atomic_write_bytes(target, data)
    return json.dumps(
        {
            "path": _relative_path(root, target),
            "created": not existed,
            "size_bytes": len(data),
        },
        ensure_ascii=False,
        indent=2,
    )


def _create_workspace_directory(settings: Any, context: Any, args: dict[str, Any]) -> str:
    root = _get_session_workspace_root(settings, context)
    target = _resolve_workspace_path(root, str(args.get("path", "")), must_exist=False)
    if target.exists() and not target.is_dir():
        raise ValueError(f"Cannot create directory over file: {_relative_path(root, target)}")
    target.mkdir(parents=True, exist_ok=True)
    return json.dumps(
        {
            "path": _relative_path(root, target),
            "created": True,
        },
        ensure_ascii=False,
        indent=2,
    )


def _copy_workspace_entry(settings: Any, context: Any, args: dict[str, Any]) -> str:
    root = _get_session_workspace_root(settings, context)
    source = _resolve_workspace_path(root, str(args.get("source_path", "")), must_exist=True)
    destination = _resolve_workspace_path(root, str(args.get("destination_path", "")), must_exist=False)
    overwrite = bool(args.get("overwrite", False))
    create_parents = bool(args.get("create_parents", True))
    _validate_entry_operation_paths(root, source, destination, operation="copy")
    _prepare_entry_destination(root, destination, overwrite=overwrite, create_parents=create_parents)

    if source.is_dir():
        # symlinks=False: copy the referenced content rather than the symlink
        # itself, so a symlinked entry inside source cannot drag host files
        # out of root through the copy.
        shutil.copytree(source, destination, symlinks=False)
    else:
        shutil.copy2(source, destination)

    return json.dumps(
        {
            "source_path": _relative_path(root, source),
            "destination_path": _relative_path(root, destination),
            "type": "directory" if destination.is_dir() else "file",
            "copied": True,
        },
        ensure_ascii=False,
        indent=2,
    )


def _move_workspace_entry(settings: Any, context: Any, args: dict[str, Any]) -> str:
    root = _get_session_workspace_root(settings, context)
    source = _resolve_workspace_path(root, str(args.get("source_path", "")), must_exist=True)
    destination = _resolve_workspace_path(root, str(args.get("destination_path", "")), must_exist=False)
    overwrite = bool(args.get("overwrite", False))
    create_parents = bool(args.get("create_parents", True))
    _validate_entry_operation_paths(root, source, destination, operation="move")
    _prepare_entry_destination(root, destination, overwrite=overwrite, create_parents=create_parents)

    shutil.move(str(source), str(destination))

    return json.dumps(
        {
            "source_path": _relative_path(root, source),
            "destination_path": _relative_path(root, destination),
            "type": "directory" if destination.is_dir() else "file",
            "moved": True,
        },
        ensure_ascii=False,
        indent=2,
    )


def _delete_workspace_entry(settings: Any, context: Any, args: dict[str, Any]) -> str:
    root = _get_session_workspace_root(settings, context)
    target = _resolve_workspace_path(root, str(args.get("path", "")), must_exist=False)
    recursive = bool(args.get("recursive", False))
    missing_ok = bool(args.get("missing_ok", False))
    if not target.exists():
        if missing_ok:
            return json.dumps({"path": _relative_path(root, target), "deleted": False}, ensure_ascii=False, indent=2)
        raise ValueError(f"Workspace path does not exist: {_relative_path(root, target)}")
    if target.is_dir():
        if recursive:
            shutil.rmtree(target)
        else:
            target.rmdir()
    else:
        target.unlink()
    return json.dumps(
        {
            "path": _relative_path(root, target),
            "deleted": True,
        },
        ensure_ascii=False,
        indent=2,
    )


def _zip_workspace_entries(settings: Any, context: Any, args: dict[str, Any]) -> str:
    root = _get_session_workspace_root(settings, context)
    raw_paths = args.get("paths", [])
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError("paths must be a non-empty array")
    sources = [_resolve_workspace_path(root, str(raw_path), must_exist=True) for raw_path in raw_paths]
    output = _resolve_workspace_path(root, str(args.get("output_path", "")), must_exist=False)
    include_hidden = bool(args.get("include_hidden", False))
    overwrite = bool(args.get("overwrite", False))
    max_entries = max(1, int(args.get("max_entries", _DEFAULT_ZIP_MAX_ENTRIES)))
    if output.exists() and output.is_dir():
        raise ValueError(f"Cannot write zip over directory: {_relative_path(root, output)}")
    if output.exists() and not overwrite:
        raise ValueError(f"Workspace file already exists: {_relative_path(root, output)}")
    output.parent.mkdir(parents=True, exist_ok=True)

    archived: list[str] = []
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for source in sources:
            candidates = (
                [source]
                if source.is_file()
                else sorted(candidate for candidate in source.rglob("*") if candidate.is_file())
            )
            for candidate in candidates:
                if candidate == output:
                    continue
                if not include_hidden and _is_hidden(candidate, root):
                    continue
                archive_name = _relative_path(root, candidate)
                if len(archived) >= max_entries:
                    raise ValueError(f"Too many entries to zip; reached max_entries={max_entries}")
                archive.write(candidate, archive_name)
                archived.append(archive_name)

    return json.dumps(
        {
            "path": _relative_path(root, output),
            "entry_count": len(archived),
            "entries": archived[:200],
            "entries_truncated": len(archived) > 200,
            "size_bytes": output.stat().st_size,
        },
        ensure_ascii=False,
        indent=2,
    )


def _unzip_workspace_archive(settings: Any, context: Any, args: dict[str, Any]) -> str:
    root = _get_session_workspace_root(settings, context)
    archive_path = _resolve_workspace_path(root, str(args.get("archive_path", "")), must_exist=True)
    output_dir = _resolve_workspace_path(root, str(args.get("output_dir", ".")), must_exist=False)
    overwrite = bool(args.get("overwrite", False))
    max_entries = max(1, int(args.get("max_entries", _DEFAULT_ZIP_MAX_ENTRIES)))
    if not archive_path.is_file():
        raise ValueError(f"Workspace path is not a file: {_relative_path(root, archive_path)}")
    output_dir.mkdir(parents=True, exist_ok=True)

    extracted: list[str] = []
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if len(infos) > max_entries:
            raise ValueError(f"Archive contains {len(infos)} entries, exceeding max_entries={max_entries}")
        for info in infos:
            destination = _safe_zip_destination(output_dir, info.filename, info=info)
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if destination.exists() and not overwrite:
                raise ValueError(f"Destination already exists: {_relative_path(root, destination)}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("wb") as target_file:
                shutil.copyfileobj(source, target_file)
            extracted.append(_relative_path(root, destination))

    return json.dumps(
        {
            "archive_path": _relative_path(root, archive_path),
            "output_dir": _relative_path(root, output_dir),
            "entry_count": len(extracted),
            "entries": extracted[:200],
            "entries_truncated": len(extracted) > 200,
        },
        ensure_ascii=False,
        indent=2,
    )


_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.Lock] = {}


class _WorkspacePathLock:
    """Cooperative in-process lock per absolute workspace path.

    Sibling agents in one execution scope share the workspace and may write the
    same path concurrently. Built-in write tools take this lock so two
    cooperative file operations do not interleave; shell/third-party writes
    remain non-transactional (documented limitation)."""

    def __init__(self, path: Path) -> None:
        self._key = str(path)

    def __enter__(self) -> "_WorkspacePathLock":
        with _PATH_LOCKS_GUARD:
            lock = _PATH_LOCKS.get(self._key)
            if lock is None:
                lock = threading.Lock()
                _PATH_LOCKS[self._key] = lock
        lock.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        with _PATH_LOCKS_GUARD:
            lock = _PATH_LOCKS.get(self._key)
        if lock is not None:
            lock.release()


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write via a sibling temp file + atomic rename, so a concurrent reader
    (in this process or a sibling container's bind mount) never observes a
    partially written file."""
    temp_path = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex[:12]}"
    try:
        with open(temp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _publish_downloadable_file(settings: Any, context: Any, download_base_path: str, args: dict[str, Any]) -> str:
    # Downloads scope to the run's execution scope (workspace_scope_id →
    # execution_scope_id → legacy session_id): stateless memory.mode=none runs
    # have no chat session, and their artifacts live under the run's scope.
    scope_id = (
        getattr(context, "workspace_scope_id", None)
        or getattr(context, "execution_scope_id", None)
        or getattr(context, "session_id", None)
    )
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise ValueError("publish_downloadable_file requires an active session or execution scope")

    # Get the session workspace root for resolving the source file
    session_root = _get_session_workspace_root(settings, context)
    # But use base workspace root for downloads (shared location)
    base_root = settings.workspace_root()

    source = _resolve_publishable_source_path(session_root, str(args.get("file_path", "")))
    if not source.is_file():
        raise ValueError(f"Publishable path is not a file: {source}")

    requested_name = str(args.get("download_name") or source.name)
    safe_name = _safe_download_filename(requested_name, source.stem or "download")
    target_dir = _download_session_dir(base_root, scope_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = _next_available_path(target_dir, safe_name)
    shutil.copy2(source, target_path)

    if bool(args.get("delete_source", False)) and source != target_path:
        try:
            source.unlink()
        except OSError:
            pass

    content_type = str(args.get("content_type") or mimetypes.guess_type(target_path.name)[0] or "application/octet-stream")
    size_bytes = target_path.stat().st_size
    workspace_path = _relative_path(base_root, target_path)
    normalized_base_path = "/" + download_base_path.strip("/")
    quoted_session = quote(_safe_storage_component(scope_id, "session"), safe="")
    quoted_name = quote(target_path.name, safe="")
    summary = str(args.get("description") or "Generated by the agent and ready to download.")

    return json.dumps(
        {
            "id": f"download-{quoted_session}-{quoted_name}",
            "name": target_path.name,
            "size": size_bytes,
            "content_type": content_type,
            "workspace_path": workspace_path,
            "download_url": f"{normalized_base_path}/{quoted_session}/{quoted_name}",
            "download_markdown": f"[Download {target_path.name}]({normalized_base_path}/{quoted_session}/{quoted_name})",
            "summary": summary,
            "published_at": datetime.now(UTC).isoformat(),
        },
        ensure_ascii=False,
        indent=2,
    )


def _resolve_workspace_path(root: Path, raw_path: str, *, must_exist: bool) -> Path:
    root = root.resolve(strict=False)
    normalized = raw_path.strip() or "."
    candidate = Path(normalized).expanduser()

    # If the path is absolute, convert it to relative by stripping the leading slash(es).
    # This ensures paths like /tmp/file.txt are treated as tmp/file.txt and placed in the workspace.
    # This allows agent code with hardcoded absolute paths to still be sandboxed transparently.
    if candidate.is_absolute():
        # Get the path parts excluding the root (first element is '/')
        relative_parts = candidate.parts[1:]  # Skip the leading '/'
        if relative_parts:
            candidate = Path(*relative_parts)
        else:
            candidate = Path(".")

    # Now candidate is always relative; append it to the workspace root
    candidate = root / candidate
    resolved = candidate.resolve(strict=False)

    # Verify the resolved path doesn't escape the root
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"Workspace path escapes root: {raw_path}")

    # Defense against symlink planting: walk from root down toward the resolved
    # path and reject if any intermediate component is a symlink that would
    # escape root. `resolve()` follows symlinks for the final answer, which
    # already catches escapes on read — but a symlinked directory inside the
    # workspace (e.g. link -> /etc) would otherwise let a copytree/move walk
    # out of root through that link before we notice.
    _assert_no_symlink_escape(root, resolved)

    if must_exist and not resolved.exists():
        raise ValueError(f"Workspace path does not exist: {_relative_path(root, resolved)}")
    return resolved


def _assert_no_symlink_escape(root: Path, resolved: Path) -> None:
    """Walk from ``root`` toward ``resolved`` and reject any symlink component
    that points outside ``root``. The resolved final path loose components
    (normalized) are examined; existing parent directories return the path
    itself so the walk terminates cleanly at non-existent tails.
    """
    # Only walk components that are actually under root.
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return  # already rejected upstream
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            target = current.resolve(strict=False)
            if target != root and root not in target.parents:
                raise ValueError(
                    f"Workspace path contains a symlink that escapes root: {part} -> {target}"
                )


def _resolve_publishable_source_path(root: Path, raw_path: str) -> Path:
    root = root.resolve(strict=False)
    normalized = raw_path.strip()
    if not normalized:
        raise ValueError("publish_downloadable_file requires a non-empty file_path")

    candidate = Path(normalized).expanduser()

    # If the path is absolute, translate it to a relative path under the
    # workspace by stripping the leading slash: publish_downloadable_file
    # ("/tmp/file.pptx") resolves to workspace "tmp/file.pptx", which is where
    # write_workspace_file("/tmp/file.pptx") would have placed it. This keeps
    # agent code with hardcoded absolute paths sandboxed transparently.
    if candidate.is_absolute():
        relative_parts = candidate.parts[1:]  # Skip leading '/'
        if relative_parts:
            relative_candidate = Path(*relative_parts)
        else:
            relative_candidate = Path(".")
        resolved = (root / relative_candidate).resolve(strict=False)
    else:
        resolved = (root / candidate).resolve(strict=False)

    # Strict containment: only files inside the workspace root are publishable.
    # (Previously this also allowed anything under the global tempdir, which
    # exposed files other processes had placed in /tmp. Agent code writing to
    # "/tmp/..." via the workspace tools already lands under the workspace, so
    # the global-temp fallback is not needed.)
    if resolved != root and root not in resolved.parents:
        raise ValueError("publish_downloadable_file only accepts files inside the workspace")
    if not resolved.exists():
        raise ValueError(f"Publishable file does not exist: {normalized}")
    _assert_no_symlink_escape(root, resolved)
    return resolved


def _download_session_dir(root: Path, session_id: str) -> Path:
    return root.resolve(strict=False) / ".covalent" / "downloads" / _safe_storage_component(session_id, "session")


def _safe_storage_component(value: str, fallback: str) -> str:
    normalized = value.strip()
    safe = "".join(char if char.isalnum() or char in "._-" else "-" for char in normalized)
    safe = safe.strip(".-")
    return safe or fallback


def _safe_download_filename(value: str, fallback_stem: str) -> str:
    raw_name = Path(value.strip() or fallback_stem).name
    suffix = Path(raw_name).suffix[:32]
    stem = Path(raw_name).stem or fallback_stem
    safe_stem = _safe_storage_component(stem, fallback_stem)
    safe_suffix = "".join(char for char in suffix if char.isalnum() or char in ".-_")
    return f"{safe_stem}{safe_suffix}"


def _next_available_path(directory: Path, file_name: str) -> Path:
    candidate = directory / file_name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        next_candidate = directory / f"{stem}-{counter}{suffix}"
        if not next_candidate.exists():
            return next_candidate
        counter += 1


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        return int(value.strip())
    return None


def _required_positive_int(args: dict[str, Any], key: str) -> int:
    value = _optional_int(args.get(key))
    if value is None:
        raise ValueError(f"{key} is required")
    if value < 1:
        raise ValueError(f"{key} must be at least 1")
    return value


def _read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _replace_text_range(
    text: str,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
    replacement: str,
) -> str:
    if (end_line, end_column) < (start_line, start_column):
        raise ValueError("End position must be greater than or equal to start position")

    start_offset = _line_column_to_offset(text, start_line, start_column)
    end_offset = _line_column_to_offset(text, end_line, end_column)
    return f"{text[:start_offset]}{replacement}{text[end_offset:]}"


def _line_column_to_offset(text: str, line_number: int, column_number: int) -> int:
    current_line = 1
    current_column = 1
    for offset, char in enumerate(text):
        if current_line == line_number and current_column == column_number:
            return offset
        if char == "\n":
            current_line += 1
            current_column = 1
        else:
            current_column += 1
    if current_line == line_number and current_column == column_number:
        return len(text)
    raise ValueError(f"Position line {line_number}, column {column_number} is outside the file")


def _safe_zip_destination(output_dir: Path, archive_name: str, *, info: "zipfile.ZipInfo | None" = None) -> Path:
    normalized_name = archive_name.replace("\\", "/")
    candidate = Path(normalized_name)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"Unsafe zip entry path: {archive_name}")
    destination = (output_dir / candidate).resolve(strict=False)
    resolved_output = output_dir.resolve(strict=False)
    if destination != resolved_output and resolved_output not in destination.parents:
        raise ValueError(f"Zip entry escapes output directory: {archive_name}")
    # Reject symlink entries: a zip can carry a symlink whose target points
    # anywhere on the host; once materialized it would let later workspace
    # reads escape root via _resolve_workspace_path.
    if info is not None and _zip_entry_is_symlink(info):
        raise ValueError(f"Unsafe zip entry path (symlink not allowed): {archive_name}")
    return destination


def _zip_entry_is_symlink(info: "zipfile.ZipInfo") -> bool:
    # UNIX mode is carried in the upper 16 bits of external_attr.
    import stat as _stat
    mode = (info.external_attr >> 16) & 0o7777
    return _stat.S_ISLNK(mode)


def _validate_entry_operation_paths(root: Path, source: Path, destination: Path, *, operation: str) -> None:
    resolved_root = root.resolve(strict=False)
    resolved_source = source.resolve(strict=False)
    resolved_destination = destination.resolve(strict=False)
    if resolved_source == resolved_root:
        raise ValueError(f"Cannot {operation} the workspace root")
    if resolved_source == resolved_destination:
        raise ValueError(f"Cannot {operation} an entry onto itself")
    if source.is_dir() and resolved_source in resolved_destination.parents:
        raise ValueError(f"Cannot {operation} a directory into itself")


def _prepare_entry_destination(root: Path, destination: Path, *, overwrite: bool, create_parents: bool) -> None:
    if destination.exists():
        if not overwrite:
            raise ValueError(f"Workspace path already exists: {_relative_path(root, destination)}")
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    if create_parents:
        destination.parent.mkdir(parents=True, exist_ok=True)
    elif not destination.parent.exists():
        raise ValueError(f"Parent directory does not exist: {_relative_path(root, destination.parent)}")


def _relative_path(root: Path, path: Path) -> str:
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    if resolved_path == resolved_root:
        return "."
    return resolved_path.relative_to(resolved_root).as_posix()


def _entry_for_path(root: Path, path: Path) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": _relative_path(root, path),
        "type": "directory" if path.is_dir() else "file",
    }
    if path.is_file():
        entry["size_bytes"] = path.stat().st_size
    return entry


def _is_hidden(path: Path, root: Path) -> bool:
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    if resolved_path == resolved_root:
        return False
    return any(part.startswith(".") for part in resolved_path.relative_to(resolved_root).parts)
