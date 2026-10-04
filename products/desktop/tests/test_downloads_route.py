from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from covalent_agent_kit.tools.workspace_tools import _build_download_payload
from covalent_desktop.api.server import create_server
from covalent_desktop.application.workspace import DesktopWorkspace
from covalent_desktop.infra.local_store import LocalStore

SCOPE = "a" * 32


@pytest.fixture()
def downloads_server(tmp_path: Path):
    downloads_root = tmp_path / "workspaces" / ".covalent" / "downloads"
    workspace = DesktopWorkspace(LocalStore(tmp_path / "desktop.sqlite3"), lambda: None)
    server = create_server(
        "127.0.0.1",
        0,
        secrets.token_urlsafe(32),
        workspace,
        downloads_root=downloads_root,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield downloads_root, base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url: str, headers: dict[str, str] | None = None):
    request = Request(url, headers=headers or {})
    try:
        with urlopen(request, timeout=2) as response:
            return response.status, response.headers, response.read()
    except HTTPError as error:
        return error.code, error.headers, error.read()


def test_published_file_served_without_credential(downloads_server) -> None:
    downloads_root, base = downloads_server
    session_dir = downloads_root / SCOPE
    session_dir.mkdir(parents=True)
    (session_dir / "report.html").write_text(
        "<html><body><h1>artifact</h1></body></html>", encoding="utf-8"
    )
    status, headers, body = _get(f"{base}/downloads/{SCOPE}/report.html")
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert b"artifact" in body


def test_unknown_extension_serves_octet_stream(downloads_server) -> None:
    downloads_root, base = downloads_server
    session_dir = downloads_root / SCOPE
    session_dir.mkdir(parents=True)
    (session_dir / "data.blob").write_bytes(b"\x00\x01")
    status, headers, body = _get(f"{base}/downloads/{SCOPE}/data.blob")
    assert status == 200
    assert headers["Content-Type"] == "application/octet-stream"
    assert body == b"\x00\x01"


def test_missing_downloads_root_returns_404(tmp_path: Path) -> None:
    workspace = DesktopWorkspace(LocalStore(tmp_path / "desktop.sqlite3"), lambda: None)
    server = create_server("127.0.0.1", 0, secrets.token_urlsafe(32), workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, _ = _get(f"http://127.0.0.1:{server.server_address[1]}/downloads/{SCOPE}/x.html")
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("name", ["..", "..%2fsecret.txt", "%2e%2e%2fescape.txt", "sub%2ffile.html"])
def test_path_traversal_rejected(downloads_server, name: str) -> None:
    downloads_root, base = downloads_server
    (downloads_root / SCOPE).mkdir(parents=True)
    status, _, _ = _get(f"{base}/downloads/{SCOPE}/{name}")
    assert status == 404


def test_symlink_escape_rejected(downloads_server) -> None:
    downloads_root, base = downloads_server
    outside = downloads_root.parent / "outside-secret.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("secret", encoding="utf-8")
    session_dir = downloads_root / SCOPE
    session_dir.mkdir(parents=True)
    os.symlink(outside, session_dir / "leak.txt")
    status, _, _ = _get(f"{base}/downloads/{SCOPE}/leak.txt")
    assert status == 404


@pytest.mark.parametrize("scope", ["../..", "a%20b", f"{'x' * 200}"])
def test_invalid_scope_rejected(downloads_server, scope: str) -> None:
    _, base = downloads_server
    status, _, _ = _get(f"{base}/downloads/{scope}/file.html")
    assert status == 404


def test_foreign_host_header_rejected(downloads_server) -> None:
    downloads_root, base = downloads_server
    session_dir = downloads_root / SCOPE
    session_dir.mkdir(parents=True)
    (session_dir / "page.html").write_text("x", encoding="utf-8")
    status, _, _ = _get(
        f"{base}/downloads/{SCOPE}/page.html", headers={"Host": "evil.example"}
    )
    assert status == 403


def test_other_routes_still_require_credential(downloads_server) -> None:
    _, base = downloads_server
    status, _, _ = _get(f"{base}/agents")
    assert status == 401


def test_absolute_base_yields_browser_openable_url(tmp_path: Path) -> None:
    source = tmp_path / "report.html"
    source.write_text("x", encoding="utf-8")
    absolute = _build_download_payload(
        tmp_path,
        SCOPE,
        source,
        content_type="text/html",
        summary="s",
        download_base_path="http://127.0.0.1:8765/downloads",
    )
    assert absolute["download_url"] == f"http://127.0.0.1:8765/downloads/{SCOPE}/report.html"
    relative = _build_download_payload(
        tmp_path, SCOPE, source, content_type="text/html", summary="s"
    )
    assert relative["download_url"] == f"/api/backend/downloads/{SCOPE}/report.html"
