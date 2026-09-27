"""Exercise real HTTP streaming against the shared runtime with a gated model."""

import asyncio
import json
import socket
import threading
from urllib.request import Request, urlopen

import pytest

from covalent_desktop.api.server import create_server
from covalent_desktop.application.workspace import WorkspaceError
from covalent_runtime.domain.types import GenerationResponse
from test_workspace import FakeModel, make_workspace


class GatedModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.release = threading.Event()
        self.closed = threading.Event()

    async def stream_generation(self, request):
        try:
            yield "delta", "hello "
            while not self.release.is_set():
                await asyncio.sleep(0.01)
            yield "delta", "world"
            yield "response", GenerationResponse(output_text="hello world")
        finally:
            self.closed.set()


@pytest.fixture
def stream_server(tmp_path):
    model = GatedModel()
    workspace = make_workspace(tmp_path / "stream.sqlite3", model)
    workspace.save_agent(
        {"name": "helper", "model": "fake", "provider_name": "test-provider"}
    )
    session_id = workspace.create_chat_session("helper", "hello")["session_id"]
    server = create_server("127.0.0.1", 0, "test-token", workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield model, workspace, session_id, server.server_address
    finally:
        model.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def request_stream(address, session_id):
    return Request(
        f"http://{address[0]}:{address[1]}/messages/stream",
        data=json.dumps(
            {
                "agent_name": "helper",
                "message": "hello",
                "session_id": session_id,
                "provider_keys": {"test-provider": "secret"},
            }
        ).encode(),
        headers={
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
        },
    )


def test_output_arrives_before_completion_and_terminal_is_persisted(stream_server):
    model, workspace, session_id, address = stream_server
    with urlopen(request_stream(address, session_id), timeout=5) as response:
        first = json.loads(response.readline())
        assert first == {
            "event": "assistant_delta",
            "payload": {"text": "hello ", "iteration": 1},
        }
        assert not model.release.is_set()
        with pytest.raises(WorkspaceError, match="already running"):
            asyncio.run(workspace.send_message("helper", "duplicate", session_id))
        model.release.set()
        events = [json.loads(line) for line in response]
    assert events[-1] == {"event": "complete", "payload": {"session_id": session_id}}
    assert sum(event["event"] == "complete" for event in events) == 1
    assert workspace.get_session(session_id)["messages"][-1]["content"] == "hello world"
    assert workspace.get_session(session_id)["live_reasoning"] is None
    assert model.closed.wait(2)


def test_disconnect_cancels_silent_model_and_releases_run_lock(stream_server):
    model, workspace, session_id, address = stream_server
    request = request_stream(address, session_id)
    connection = socket.create_connection(address, timeout=5)
    body = request.data
    connection.sendall(
        (
            f"POST /messages/stream HTTP/1.1\r\nHost: localhost\r\n"
            f"Authorization: Bearer test-token\r\nContent-Length: {len(body)}\r\n\r\n"
        ).encode()
        + body
    )
    data = b""
    while b"assistant_delta" not in data:
        data += connection.recv(4096)
    connection.shutdown(socket.SHUT_RDWR)
    connection.close()
    assert model.closed.wait(4), "Disconnected run was left executing"
    # Model closure precedes trace/registry cleanup; wait for the full run to exit.
    assert workspace._run_lock.acquire(timeout=3), "Run cleanup did not release its lock"
    workspace._run_lock.release()
    # Cancellation must leave the same workspace available for a later run.
    model.release.set()
    result = asyncio.run(
        workspace.send_message(
            "helper", "next", session_id, provider_keys={"test-provider": "secret"}
        )
    )
    assert result["output_text"] == "hello world"


def test_invalid_run_has_error_without_completion(stream_server):
    _, _, _, address = stream_server
    with urlopen(request_stream(address, "0" * 32), timeout=5) as response:
        events = [json.loads(line) for line in response]
    assert [event["event"] for event in events] == ["error"]
    assert "secret" not in json.dumps(events)


def test_transport_disconnect_cancels_without_socket(tmp_path):
    from covalent_desktop.api.server import DesktopRequestHandler

    model = GatedModel()
    workspace = make_workspace(tmp_path / "disconnect.sqlite3", model)
    workspace.save_agent(
        {"name": "helper", "model": "fake", "provider_name": "test-provider"}
    )
    session_id = workspace.create_chat_session("helper", "hello")["session_id"]
    frames = []

    class Connection:
        def settimeout(self, timeout):
            assert timeout == 5

    class Writer:
        def write(self, data):
            event = json.loads(data)
            if event["event"] == "heartbeat":
                raise BrokenPipeError("consumer disconnected")
            frames.append(event)

        def flush(self):
            pass

    handler = object.__new__(DesktopRequestHandler)
    handler.connection = Connection()
    handler.wfile = Writer()
    handler.send_response = lambda status: None
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler._stream_message(
        workspace,
        {"agent_name": "helper", "message": "hello", "session_id": session_id},
        {"test-provider": "secret"},
        None,
        None,
    )
    assert frames[0]["event"] == "assistant_delta"
    assert model.closed.is_set()
    assert workspace.get_session(session_id)["activity"][-1]["title"] == "error"
    assert not workspace._run_lock.locked()
    assert workspace.get_session(session_id)["live_reasoning"] is None
