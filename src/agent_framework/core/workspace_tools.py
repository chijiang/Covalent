from __future__ import annotations

import base64
import json
import mimetypes
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from agent_framework.infra.settings import AppSettings

_DEFAULT_MAX_BYTES = 24_000


def _get_session_workspace_root(settings: Any, context: Any) -> Path:
    """Get the workspace root for the current session, or the base root if session workspace is disabled."""
    session_id = getattr(context, "session_id", None)
    if isinstance(session_id, str) and session_id.strip():
        return settings.session_workspace_dir(session_id)
    if settings.session_workspace_enabled and session_id is None:
        raise ValueError("Session workspace is enabled but no session_id found in context")
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
                "description": "Read a file from the workspace root as UTF-8 text or base64 for binary content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "max_bytes": {"type": "integer", "default": _DEFAULT_MAX_BYTES, "minimum": 1},
                    },
                    "required": ["path"],
                },
            },
        },
        handler=lambda args, ctx: _read_workspace_file(settings, ctx, args),
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
        "publish_downloadable_file",
        {
            "type": "function",
            "function": {
                "name": "publish_downloadable_file",
                "description": (
                    "Copy an existing workspace file or system temporary file into the session download area "
                    "and return a user-downloadable link. Use this after generating a file that the user should download."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to an existing file inside the workspace or the system temporary directory.",
                        },
                        "download_name": {
                            "type": "string",
                            "description": "Optional filename to present to the user when downloading.",
                        },
                        "content_type": {
                            "type": "string",
                            "description": "Optional MIME type override for the published download.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "Optional short description shown to the user alongside the download.",
                        },
                        "delete_source": {
                            "type": "boolean",
                            "default": False,
                            "description": "Delete the original file after publishing it.",
                        },
                    },
                    "required": ["path"],
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
    max_bytes = max(1, int(args.get("max_bytes", _DEFAULT_MAX_BYTES)))
    raw = target.read_bytes()
    truncated = len(raw) > max_bytes
    content_bytes = raw[:max_bytes] if truncated else raw
    try:
        content = content_bytes.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(content_bytes).decode("ascii")
        encoding = "base64"

    return json.dumps(
        {
            "path": _relative_path(root, target),
            "encoding": encoding,
            "content": content,
            "truncated": truncated,
            "size_bytes": len(raw),
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

    existed = target.exists()
    target.write_bytes(data)
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


def _publish_downloadable_file(settings: Any, context: Any, download_base_path: str, args: dict[str, Any]) -> str:
    session_id = getattr(context, "session_id", None)
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("publish_downloadable_file requires an active session_id")

    # Get the session workspace root for resolving the source file
    session_root = _get_session_workspace_root(settings, context)
    # But use base workspace root for downloads (shared location)
    base_root = settings.workspace_root()
    
    source = _resolve_publishable_source_path(session_root, str(args.get("path", "")))
    if not source.is_file():
        raise ValueError(f"Publishable path is not a file: {source}")

    requested_name = str(args.get("download_name") or source.name)
    safe_name = _safe_download_filename(requested_name, source.stem or "download")
    target_dir = _download_session_dir(base_root, session_id)
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
    quoted_session = quote(_safe_storage_component(session_id, "session"), safe="")
    quoted_name = quote(target_path.name, safe="")
    summary = str(args.get("summary") or f"Generated by the agent and ready to download.")

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
    
    if must_exist and not resolved.exists():
        raise ValueError(f"Workspace path does not exist: {_relative_path(root, resolved)}")
    return resolved


def _resolve_publishable_source_path(root: Path, raw_path: str) -> Path:
    normalized = raw_path.strip()
    if not normalized:
        raise ValueError("publish_downloadable_file requires a non-empty path")

    candidate = Path(normalized).expanduser()
    
    # If the path is absolute, try the translated (relative) version in the workspace first.
    # This allows agent code like publish_downloadable_file("/tmp/file.pptx") to work
    # with files that were written via write_workspace_file("/tmp/file.pptx").
    if candidate.is_absolute():
        relative_parts = candidate.parts[1:]  # Skip leading '/'
        if relative_parts:
            relative_candidate = Path(*relative_parts)
        else:
            relative_candidate = Path(".")
        
        workspace_path = root / relative_candidate
        if workspace_path.exists():
            return workspace_path
        # If not found in workspace, fall through to check system temp
    
    # Try the path as-is (for direct temp directory files)
    resolved = candidate.resolve(strict=False) if candidate.is_absolute() else root / candidate
    resolved = resolved.resolve(strict=False)
    
    if not resolved.exists():
        raise ValueError(f"Publishable file does not exist: {normalized}")

    temp_root = Path(tempfile.gettempdir()).resolve()
    if resolved == root or root in resolved.parents:
        return resolved
    if resolved == temp_root or temp_root in resolved.parents:
        return resolved
    raise ValueError("publish_downloadable_file only accepts files inside the workspace or the system temporary directory")


def _download_session_dir(root: Path, session_id: str) -> Path:
    return root / ".agent_framework" / "downloads" / _safe_storage_component(session_id, "session")


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


def _relative_path(root: Path, path: Path) -> str:
    if path == root:
        return "."
    return path.relative_to(root).as_posix()


def _entry_for_path(root: Path, path: Path) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": _relative_path(root, path),
        "type": "directory" if path.is_dir() else "file",
    }
    if path.is_file():
        entry["size_bytes"] = path.stat().st_size
    return entry


def _is_hidden(path: Path, root: Path) -> bool:
    if path == root:
        return False
    return any(part.startswith(".") for part in path.relative_to(root).parts)