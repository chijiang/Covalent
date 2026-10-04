"""Authenticated loopback HTTP transport for the Desktop sidecar."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, unquote, urlsplit

from covalent_desktop.application.status import ServiceStatus, get_service_status
from covalent_desktop.application.workspace import DesktopWorkspace, WorkspaceError

LOGGER = logging.getLogger(__name__)
MAX_AUTH_HEADER_BYTES = 4096
_DOWNLOAD_SCOPE_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,80}")
_DOWNLOAD_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,255}")
_DOWNLOAD_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".xml": "application/xml",
    ".pdf": "application/pdf",
}


class DesktopHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        status: ServiceStatus,
        workspace: DesktopWorkspace,
        downloads_root: Path | None = None,
    ) -> None:
        super().__init__(address, DesktopRequestHandler)
        self.token = token
        self.service_status = status
        self.workspace = workspace
        self.downloads_root = downloads_root


class DesktopRequestHandler(BaseHTTPRequestHandler):
    server_version = "CovalentDesktop"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        request_url = urlsplit(self.path)
        if request_url.path.startswith("/downloads/"):
            # Served before the credential check: the agent's browser navigates
            # without an Authorization header (see _serve_download for guards).
            self._serve_download(request_url.path)
            return
        if not self._is_authorized():
            self._write_json(
                HTTPStatus.UNAUTHORIZED,
                {"code": "unauthorized", "message": "Invalid service credential"},
            )
            return
        server = cast(DesktopHTTPServer, self.server)
        path = request_url.path
        if path == "/healthz":
            payload = server.service_status.to_dict()
        elif path == "/agents":
            include_values = parse_qs(request_url.query).get("include_felines", [])
            if include_values not in ([], ["true"]):
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"code": "invalid_request", "message": "Invalid Felines flag"},
                )
                return
            payload = {"items": server.workspace.list_agents(bool(include_values))}
        elif path == "/agent-options":
            payload = server.workspace.agent_options()
        elif path == "/providers":
            payload = {"items": server.workspace.list_providers()}
        elif path == "/mcp-services":
            payload = {"items": server.workspace.list_mcp_services()}
        elif path == "/skills":
            payload = {"items": server.workspace.list_skills()}
        elif path.startswith("/skill-preview/") and path.count("/") == 2:
            try:
                payload = server.workspace.preview_skill(path.split("/")[2])
            except WorkspaceError as error:
                self._write_json(
                    HTTPStatus(error.status),
                    {"code": error.code, "message": error.message},
                )
                return
        elif path == "/sessions":
            payload = {"items": server.workspace.list_sessions()}
        elif path.startswith("/sessions/") and path.count("/") == 2:
            try:
                payload = server.workspace.get_session(path.split("/")[2])
            except WorkspaceError as error:
                self._write_json(
                    HTTPStatus(error.status),
                    {"code": error.code, "message": error.message},
                )
                return
        elif path.startswith("/sessions/") and path.count("/") == 4:
            _, _, session_id, resource, activity_id = path.split("/")
            if resource != "activity":
                self._write_json(
                    HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "Not found"}
                )
                return
            try:
                payload = server.workspace.get_session_activity(session_id, activity_id)
            except WorkspaceError as error:
                self._write_json(
                    HTTPStatus(error.status),
                    {"code": error.code, "message": error.message},
                )
                return
        else:
            self._write_json(
                HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "Not found"}
            )
            return
        self._write_json(HTTPStatus.OK, payload)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._is_authorized():
            self._write_json(
                HTTPStatus.UNAUTHORIZED,
                {"code": "unauthorized", "message": "Invalid service credential"},
            )
            return
        if self.path not in (
            "/agents",
            "/messages",
            "/messages/stream",
            "/sessions",
            "/providers",
            "/provider-models",
            "/mcp-services",
            "/mcp-inspect",
            "/skills",
            "/skill-update",
            "/skill-upload",
            "/skill-state",
            "/skill-git",
        ):
            self._write_json(
                HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "Not found"}
            )
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > (
                14_000_000 if self.path == "/skill-upload" else 120000
            ):
                raise ValueError("Invalid request size")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")
            workspace = cast(DesktopHTTPServer, self.server).workspace
            if self.path == "/agents":
                payload = workspace.save_agent(data)
            elif self.path == "/sessions":
                payload = workspace.create_chat_session(
                    str(data.get("agent_name", "")), str(data.get("title", ""))
                )
            elif self.path == "/providers":
                payload = workspace.save_provider(data)
            elif self.path == "/mcp-services":
                payload = workspace.save_mcp_service(data)
            elif self.path == "/mcp-inspect":
                payload = {
                    "items": asyncio.run(
                        workspace.inspect_mcp_service(
                            str(data.get("name", "")), data.get("env")
                        )
                    )
                }
            elif self.path == "/skills":
                payload = workspace.create_skill(
                    str(data.get("name", "")), str(data.get("content", ""))
                )
            elif self.path == "/skill-update":
                payload = workspace.update_skill(
                    str(data.get("name", "")), str(data.get("content", ""))
                )
            elif self.path == "/skill-upload":
                payload = workspace.install_skill(
                    str(data.get("name", "")), str(data.get("archive", ""))
                )
            elif self.path == "/skill-state":
                payload = workspace.set_skill_enabled(
                    str(data.get("name", "")), data.get("enabled") is True
                )
            elif self.path == "/skill-git":
                payload = asyncio.run(
                    workspace.sync_git_skills(
                        str(data.get("name", "")),
                        str(data.get("url", "")),
                        str(data["ref"]) if data.get("ref") else None,
                        str(data["subdir"]) if data.get("subdir") else None,
                    )
                )
            elif self.path == "/provider-models":
                payload = {
                    "items": workspace.load_provider_models(
                        str(data.get("name", "")), str(data.get("api_key", ""))
                    )
                }
            else:
                keys = data.get("provider_keys")
                if keys is not None and (
                    not isinstance(keys, dict)
                    or not all(
                        isinstance(k, str) and isinstance(v, str)
                        for k, v in keys.items()
                    )
                ):
                    raise ValueError("Invalid provider credentials")
                answers = data.get("resume_answers")
                if answers is not None and (
                    not isinstance(answers, dict) or len(json.dumps(answers)) > 10000
                ):
                    raise ValueError("Invalid Agent question answers")
                edit_user_index = data.get("edit_user_index")
                if edit_user_index is not None and (
                    isinstance(edit_user_index, bool)
                    or not isinstance(edit_user_index, int)
                    or edit_user_index < 1
                ):
                    raise ValueError("Invalid edited message index")
                if self.path == "/messages/stream":
                    self._stream_message(
                        workspace, data, keys, answers, edit_user_index
                    )
                    return
                payload = asyncio.run(
                    workspace.send_message(
                        str(data.get("agent_name", "")),
                        str(data.get("message", "")),
                        str(data["session_id"]) if data.get("session_id") else None,
                        provider_keys=keys,
                        mcp_env=data.get("mcp_env"),
                        resume_answers=answers,
                        edit_user_index=edit_user_index,
                    )
                )
        except (ValueError, json.JSONDecodeError) as error:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"code": "invalid_request", "message": str(error)},
            )
            return
        except WorkspaceError as error:
            self._write_json(
                HTTPStatus(error.status), {"code": error.code, "message": error.message}
            )
            return
        except Exception:
            LOGGER.exception("Desktop request failed")
            if self.path == "/provider-models":
                code, message = (
                    "provider_models_failed",
                    "Could not load models; check the Provider endpoint and API key",
                )
            elif self.path == "/mcp-inspect":
                code, message = (
                    "mcp_inspect_failed",
                    "Could not connect to the MCP service; check its transport and credentials",
                )
            elif self.path == "/skill-git":
                code, message = (
                    "skill_sync_failed",
                    "Could not sync the Git skill source",
                )
            elif self.path == "/skill-upload":
                code, message = (
                    "skill_upload_failed",
                    "Could not import the skill archive",
                )
            else:
                code, message = (
                    "invoke_failed",
                    "Agent call failed; check the model endpoint and credentials",
                )
            self._write_json(
                HTTPStatus.BAD_GATEWAY,
                {"code": code, "message": message},
            )
            return
        self._write_json(HTTPStatus.OK, payload)

    def _stream_message(self, workspace, data, keys, answers, edit_user_index):
        # A dedicated request owns the run. No unbounded event queue or replay.
        self.connection.settimeout(5)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(event):
            frame = (json.dumps(event, ensure_ascii=True) + "\n").encode()
            if len(frame) > 4 * 1024 * 1024:
                raise ValueError("Stream event exceeds size limit")
            self.wfile.write(frame)
            self.wfile.flush()

        async def run():
            task = asyncio.create_task(
                workspace.send_message(
                    str(data.get("agent_name", "")),
                    str(data.get("message", "")),
                    str(data["session_id"]) if data.get("session_id") else None,
                    provider_keys=keys,
                    mcp_env=data.get("mcp_env"),
                    resume_answers=answers,
                    edit_user_index=edit_user_index,
                    on_event=emit,
                )
            )
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=0.5)
                    if not task.done():
                        # Detect disconnected consumers even during silent model/tool work.
                        emit({"event": "heartbeat"})
                result = await task
                # Only report completion after transcript and pending input are saved.
                emit(
                    {
                        "event": "complete",
                        "payload": {"session_id": result["session_id"]},
                    }
                )
            except (OSError, ConnectionError):
                pass
            except Exception:
                LOGGER.exception("Desktop stream failed")
                try:
                    emit(
                        {
                            "event": "error",
                            "payload": {
                                "message": "Agent stream failed; inspect the conversation before retrying."
                            },
                        }
                    )
                except OSError:
                    pass
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        asyncio.run(run())

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._is_authorized():
            self._write_json(
                HTTPStatus.UNAUTHORIZED,
                {"code": "unauthorized", "message": "Invalid service credential"},
            )
            return
        if self.path.count("/") != 2 or not (
            self.path.startswith("/sessions/")
            or self.path.startswith("/providers/")
            or self.path.startswith("/mcp-services/")
            or self.path.startswith("/skills/")
        ):
            self._write_json(
                HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "Not found"}
            )
            return
        try:
            workspace = cast(DesktopHTTPServer, self.server).workspace
            name = self.path.split("/")[2]
            if self.path.startswith("/sessions/"):
                workspace.delete_session(name)
                self._write_json(HTTPStatus.OK, {"deleted": True})
                return
            if self.path.startswith("/providers/"):
                workspace.delete_provider(name)
            elif self.path.startswith("/mcp-services/"):
                workspace.delete_mcp_service(name)
            else:
                workspace.delete_skill(name)
        except WorkspaceError as error:
            self._write_json(
                HTTPStatus(error.status), {"code": error.code, "message": error.message}
            )
            return
        self._write_json(HTTPStatus.OK, {"deleted": True})

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._is_authorized():
            self._write_json(
                HTTPStatus.UNAUTHORIZED,
                {"code": "unauthorized", "message": "Invalid service credential"},
            )
            return
        if self.path.count("/") != 2 or not self.path.startswith("/sessions/"):
            self._write_json(
                HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "Not found"}
            )
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 120000:
                raise ValueError("Invalid request size")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")
            session_id = self.path.split("/")[2]
            title = data.get("title")
            pinned = data.get("pinned")
            if title is None and pinned is None:
                raise ValueError("Nothing to update")
            if title is not None and not isinstance(title, str):
                raise ValueError("Invalid title")
            if pinned is not None and not isinstance(pinned, bool):
                raise ValueError("Invalid pinned flag")
            workspace = cast(DesktopHTTPServer, self.server).workspace
            if title is not None:
                payload = workspace.rename_session(session_id, title)
            else:
                payload = workspace.set_session_pinned(session_id, pinned)
        except (ValueError, json.JSONDecodeError) as error:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"code": "invalid_request", "message": str(error)},
            )
            return
        except WorkspaceError as error:
            self._write_json(
                HTTPStatus(error.status), {"code": error.code, "message": error.message}
            )
            return
        self._write_json(HTTPStatus.OK, payload)

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.info("desktop_http %s", format % args)

    def _is_authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if len(header.encode("utf-8")) > MAX_AUTH_HEADER_BYTES:
            return False
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        server = cast(DesktopHTTPServer, self.server)
        return hmac.compare_digest(header[len(prefix) :], server.token)

    def _serve_download(self, path: str) -> None:
        # Auth-exempt because browser navigation cannot send credentials. The
        # compensating guards: create_server enforces a loopback-only bind, the
        # Host header is pinned (DNS rebinding), scope/name must match strict
        # patterns, and realpath containment keeps requests inside the session
        # download directory — the same validation Electron applies when it
        # resolves these files for the renderer.
        server = cast(DesktopHTTPServer, self.server)
        if server.downloads_root is None:
            self._write_json(
                HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "Not found"}
            )
            return
        host = self.headers.get("Host", "").split(":")[0].lower()
        if host not in {"127.0.0.1", "localhost", "[::1]"}:
            self._write_json(
                HTTPStatus.FORBIDDEN, {"code": "forbidden", "message": "Invalid host"}
            )
            return
        parts = path.split("/")
        if len(parts) != 4:
            self._write_json(
                HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "Not found"}
            )
            return
        _, _, scope, name = (unquote(part) for part in parts)
        if not _DOWNLOAD_SCOPE_PATTERN.fullmatch(scope):
            self._write_json(
                HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "Not found"}
            )
            return
        if not _DOWNLOAD_NAME_PATTERN.fullmatch(name) or name in {".", ".."}:
            self._write_json(
                HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "Not found"}
            )
            return
        directory = (server.downloads_root / scope).resolve(strict=False)
        try:
            target = (directory / name).resolve(strict=True)
        except OSError:
            self._write_json(
                HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "Not found"}
            )
            return
        if target != directory and directory not in target.parents:
            self._write_json(
                HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "Not found"}
            )
            return
        if not target.is_file():
            self._write_json(
                HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "Not found"}
            )
            return
        content_type = _DOWNLOAD_CONTENT_TYPES.get(
            target.suffix.lower(), "application/octet-stream"
        )
        try:
            size = target.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with target.open("rb") as handle:
                while chunk := handle.read(65536):
                    self.wfile.write(chunk)
        except OSError:
            # Headers may already be on the wire; nothing further to do.
            LOGGER.debug("Download transfer aborted: %s", target, exc_info=True)

    def _write_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def create_server(
    host: str,
    port: int,
    token: str,
    workspace: DesktopWorkspace,
    downloads_root: Path | None = None,
) -> DesktopHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("Desktop service must bind to 127.0.0.1")
    return DesktopHTTPServer(
        (host, port), token, get_service_status(), workspace, downloads_root
    )
