"""Authenticated loopback HTTP transport for the Desktop sidecar."""

from __future__ import annotations

import hmac
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

from covalent_desktop.application.status import ServiceStatus, get_service_status

LOGGER = logging.getLogger(__name__)
MAX_AUTH_HEADER_BYTES = 4096


class DesktopHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], token: str, status: ServiceStatus) -> None:
        super().__init__(address, DesktopRequestHandler)
        self.token = token
        self.service_status = status


class DesktopRequestHandler(BaseHTTPRequestHandler):
    server_version = "CovalentDesktop"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/healthz":
            self._write_json(HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "Not found"})
            return
        if not self._is_authorized():
            self._write_json(
                HTTPStatus.UNAUTHORIZED,
                {"code": "unauthorized", "message": "Invalid service credential"},
            )
            return
        server = cast(DesktopHTTPServer, self.server)
        self._write_json(HTTPStatus.OK, server.service_status.to_dict())

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


def create_server(host: str, port: int, token: str) -> DesktopHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("Desktop service must bind to 127.0.0.1")
    return DesktopHTTPServer((host, port), token, get_service_status())
