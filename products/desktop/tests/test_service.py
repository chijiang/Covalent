from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from covalent_desktop.application.status import PROTOCOL_VERSION, get_service_status


def _readline(stream: object, output: queue.Queue[str]) -> None:
    output.put(stream.readline())  # type: ignore[attr-defined]


def test_status_contract() -> None:
    status = get_service_status().to_dict()
    assert status == {
        "status": "ok",
        "service_version": "0.1.0",
        "protocol_version": PROTOCOL_VERSION,
        "capabilities": ["health"],
    }


def test_sidecar_handshake_auth_and_shutdown() -> None:
    token = secrets.token_urlsafe(32)
    env = os.environ.copy()
    env["COVALENT_DESKTOP_SERVICE_TOKEN"] = token
    process = subprocess.Popen(
        [sys.executable, "-m", "covalent_desktop", "serve", "--port", "0"],
        cwd=Path(__file__).resolve().parents[3],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_readline, args=(process.stdout, lines), daemon=True).start()
    try:
        handshake_line = lines.get(timeout=10)
        handshake = json.loads(handshake_line)
        assert handshake["type"] == "ready"
        assert handshake["host"] == "127.0.0.1"
        assert handshake["protocol_version"] == PROTOCOL_VERSION
        assert handshake["port"] > 0
        assert token not in handshake_line

        url = f"http://127.0.0.1:{handshake['port']}/healthz"
        with pytest.raises(HTTPError) as error:
            urlopen(url, timeout=2)
        assert error.value.code == 401

        request = Request(url, headers={"Authorization": f"Bearer {token}"})
        with urlopen(request, timeout=2) as response:
            body = json.load(response)
        assert body["status"] == "ok"
        assert body["protocol_version"] == PROTOCOL_VERSION
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    assert process.returncode == 0
