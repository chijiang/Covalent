"""Authenticated loopback HTTP transport for the Desktop sidecar."""

from __future__ import annotations

import hmac
import json
import logging
import asyncio
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

from covalent_desktop.application.status import ServiceStatus, get_service_status
from covalent_desktop.application.workspace import DesktopWorkspace, WorkspaceError

LOGGER = logging.getLogger(__name__)
MAX_AUTH_HEADER_BYTES = 4096


class DesktopHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        status: ServiceStatus,
        workspace: DesktopWorkspace,
    ) -> None:
        super().__init__(address, DesktopRequestHandler)
        self.token = token
        self.service_status = status
        self.workspace = workspace


class DesktopRequestHandler(BaseHTTPRequestHandler):
    server_version = "CovalentDesktop"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._is_authorized():
            self._write_json(
                HTTPStatus.UNAUTHORIZED,
                {"code": "unauthorized", "message": "Invalid service credential"},
            )
            return
        server = cast(DesktopHTTPServer, self.server)
        if self.path == "/healthz":
            payload = server.service_status.to_dict()
        elif self.path == "/agents":
            payload = {"items": server.workspace.list_agents()}
        elif self.path == "/sessions":
            payload = {"items": server.workspace.list_sessions()}
        elif self.path.startswith("/sessions/") and self.path.count("/") == 2:
            try:
                payload = server.workspace.get_session(self.path.split("/")[2])
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
        if self.path not in ("/agents", "/messages"):
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
            workspace = cast(DesktopHTTPServer, self.server).workspace
            if self.path == "/agents":
                payload = workspace.save_agent(data)
            else:
                payload = asyncio.run(
                    workspace.send_message(
                        str(data.get("agent_name", "")),
                        str(data.get("message", "")),
                        str(data["session_id"]) if data.get("session_id") else None,
                        api_key=self.headers.get("X-Model-Key"),
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
            self._write_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "code": "invoke_failed",
                    "message": "Agent call failed; check the model endpoint and credentials",
                },
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
    host: str, port: int, token: str, workspace: DesktopWorkspace
) -> DesktopHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("Desktop service must bind to 127.0.0.1")
    return DesktopHTTPServer((host, port), token, get_service_status(), workspace)
