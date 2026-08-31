"""MCP SDK client session handling.

Covers the streamable_http transport's yield-shape compatibility: mcp SDK 1.x
yields ``(read, write, get_session_id)`` from ``streamable_http_client`` while
2.0.0 yields ``(read, write)``. The client must work under both, otherwise the
unpack ``ValueError`` surfaces as an opaque
"unhandled errors in a TaskGroup (1 sub-exception)".
"""

from __future__ import annotations

import unittest
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest import mock

from covalent.mcp.client import McpSdkClient
from covalent.mcp.spec import McpServerConfig, McpToolReference
from covalent.registry.registry import FrameworkRegistry


def _streamable_server() -> McpServerConfig:
    return McpServerConfig(
        name="query-server",
        transport="streamable_http",
        url="http://localhost:8012/mcp",
    )


class _FakeClientSession:
    """Stands in for mcp.ClientSession; records initialize and returns tools."""

    instances: list["_FakeClientSession"] = []

    def __init__(self, read, write) -> None:
        self.read = read
        self.write = write
        self.initialized = False
        type(self).instances.append(self)

    async def __aenter__(self) -> "_FakeClientSession":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def initialize(self) -> None:
        self.initialized = True

    async def list_tools(self):
        tool = SimpleNamespace(name="search_instances", description="Search", inputSchema={"type": "object"})
        return SimpleNamespace(tools=[tool])


@contextmanager
def _patch_transports(yield_shape: int, *, capture: dict | None = None):
    """Patch the mcp SDK entry points used by McpSdkClient._session.

    ``yield_shape`` controls how many values the fake streamable_http_client
    context yields (2 for SDK 2.x, 3 for SDK 1.x). ``capture`` collects the
    kwargs the client passed to the fakes for assertions.
    """

    @asynccontextmanager
    async def fake_streamable_http(url, **kwargs):
        if capture is not None:
            capture.update(kwargs)
        if yield_shape == 2:
            yield ("READ", "WRITE")
        else:
            yield ("READ", "WRITE", lambda: None)

    @asynccontextmanager
    async def fake_sse(url, *, headers=None, **kwargs):
        if capture is not None:
            capture["headers"] = headers
        yield ("READ", "WRITE")

    with mock.patch.multiple(
        "mcp.client.streamable_http",
        streamable_http_client=fake_streamable_http,
    ), mock.patch.multiple(
        "mcp.client.sse",
        sse_client=fake_sse,
    ):
        yield


class StreamableHttpYieldShapeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _FakeClientSession.instances = []
        self._session_patch = mock.patch("mcp.ClientSession", _FakeClientSession)
        self._session_patch.start()

    def tearDown(self) -> None:
        self._session_patch.stop()

    async def test_streamable_http_works_with_sdk2_two_tuple(self) -> None:
        with _patch_transports(yield_shape=2):
            tools = await McpSdkClient().list_tools(_streamable_server())
        self.assertEqual([t.tool_name for t in tools], ["search_instances"])
        session = _FakeClientSession.instances[-1]
        self.assertTrue(session.initialized)
        self.assertEqual((session.read, session.write), ("READ", "WRITE"))

    async def test_streamable_http_works_with_sdk1_three_tuple(self) -> None:
        with _patch_transports(yield_shape=3):
            tools = await McpSdkClient().list_tools(_streamable_server())
        self.assertEqual([t.tool_name for t in tools], ["search_instances"])
        session = _FakeClientSession.instances[-1]
        self.assertEqual((session.read, session.write), ("READ", "WRITE"))


class TransportHeaderForwardingTests(unittest.IsolatedAsyncioTestCase):
    """HTTP transports have no process environment: configured env key/values
    must be sent as request headers (e.g. X-API-Key auth on remote MCP servers)."""

    def setUp(self) -> None:
        _FakeClientSession.instances = []
        self._session_patch = mock.patch("mcp.ClientSession", _FakeClientSession)
        self._session_patch.start()

    def tearDown(self) -> None:
        self._session_patch.stop()

    async def test_streamable_http_env_sent_as_httpx_client_headers(self) -> None:
        server = McpServerConfig(
            name="query-server",
            transport="streamable_http",
            url="http://localhost:8012/mcp",
            env={"X-API-Key": "mnk_secret"},
        )
        capture: dict = {}
        with _patch_transports(yield_shape=2, capture=capture):
            await McpSdkClient().list_tools(server)
        http_client = capture.get("http_client")
        self.assertIsNotNone(http_client, "env-configured server must pass a pre-configured httpx client")
        self.assertEqual(http_client.headers.get("x-api-key"), "mnk_secret")

    async def test_streamable_http_without_env_passes_no_http_client(self) -> None:
        capture: dict = {}
        with _patch_transports(yield_shape=2, capture=capture):
            await McpSdkClient().list_tools(_streamable_server())
        self.assertIsNone(capture.get("http_client"))

    async def test_sse_env_sent_as_headers(self) -> None:
        server = McpServerConfig(
            name="query-server",
            transport="sse",
            url="http://localhost:8012/sse",
            env={"X-API-Key": "mnk_secret"},
        )
        capture: dict = {}
        with _patch_transports(yield_shape=2, capture=capture):
            await McpSdkClient().list_tools(server)
        self.assertEqual(capture.get("headers"), {"X-API-Key": "mnk_secret"})


class RegistrySchemaNormalizationTests(unittest.IsolatedAsyncioTestCase):
    """OpenAI-compatible providers reject tool parameters schemas without an
    explicit ``type: "object"`` ("got 'type: null'"). MCP servers that emit
    ``inputSchema: null`` surface as empty/typed-null dicts after client-side
    coercion; the registry must normalize every degenerate shape."""

    class _DegenerateSchemaClient:
        async def list_tools(self, server):
            return [
                # inputSchema null -> coerced to {} by McpSdkClient.list_tools
                McpToolReference(server_name=server.name, tool_name="null_schema", description="d", input_schema={}),
                # explicit null type with real properties
                McpToolReference(server_name=server.name, tool_name="none_type", description="d", input_schema={"type": None, "properties": {"q": {"type": "string"}}}),
                # properties without any type key
                McpToolReference(server_name=server.name, tool_name="no_type", description="d", input_schema={"properties": {"q": {"type": "string"}}}),
                # already valid — must pass through unchanged
                McpToolReference(server_name=server.name, tool_name="valid", description="d", input_schema={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}),
            ]

    async def test_exported_parameters_always_typed_object(self) -> None:
        registry = FrameworkRegistry()
        registry.set_mcp_client(RegistrySchemaNormalizationTests._DegenerateSchemaClient())
        server = _streamable_server()
        exported = await registry._export_mcp_tools(server)
        raw_names = ["null_schema", "none_type", "no_type", "valid"]
        by_name = {raw: tool["function"]["parameters"] for raw, tool in zip(raw_names, exported)}
        self.assertEqual(len(by_name), 4)
        for name, parameters in by_name.items():
            with self.subTest(tool=name):
                self.assertEqual(parameters.get("type"), "object")
                self.assertIsInstance(parameters.get("properties"), dict)
        # properties preserved through normalization
        self.assertEqual(by_name["none_type"]["properties"], {"q": {"type": "string"}})
        self.assertEqual(by_name["no_type"]["properties"], {"q": {"type": "string"}})
        # valid schema untouched
        self.assertEqual(
            by_name["valid"],
            {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        )


if __name__ == "__main__":
    unittest.main()
