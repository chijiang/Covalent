from __future__ import annotations

import base64
import unittest

from covalent.mcp.spec import McpServerConfig, McpToolReference
from covalent.registry.registry import FrameworkRegistry


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


class _FakeMcpClient:
    def __init__(self, tools: list[McpToolReference]) -> None:
        self._tools = tools

    async def list_tools(self, server: McpServerConfig) -> list[McpToolReference]:
        return self._tools


class EncodeMcpToolNameTests(unittest.TestCase):
    def test_compliant_ascii_names_stay_literal(self) -> None:
        name = FrameworkRegistry._encode_mcp_tool_name("query-server", "get_ontology_schema")
        self.assertEqual(name, "mcp__query-server__get_ontology_schema")

    def test_non_compliant_segments_fall_back_to_base64(self) -> None:
        name = FrameworkRegistry._encode_mcp_tool_name("my.server", "获取本体")
        self.assertEqual(name, f"mcp__{_b64('my.server')}__{_b64('获取本体')}")

    def test_segment_containing_separator_is_encoded(self) -> None:
        name = FrameworkRegistry._encode_mcp_tool_name("my__srv", "tool")
        self.assertEqual(name, f"mcp__{_b64('my__srv')}__tool")

    def test_base64_ambiguous_literal_is_encoded(self) -> None:
        ambiguous = _b64("hi")  # valid base64 of another string; must not stay literal
        name = FrameworkRegistry._encode_mcp_tool_name(ambiguous, "tool")
        self.assertNotIn(f"mcp__{ambiguous}__", name)


class DecodeMcpToolNameTests(unittest.TestCase):
    def test_round_trip_literal(self) -> None:
        name = FrameworkRegistry._encode_mcp_tool_name("query-server", "query_neighbors")
        self.assertEqual(FrameworkRegistry._decode_mcp_tool_name(name), ("query-server", "query_neighbors"))

    def test_round_trip_encoded(self) -> None:
        name = FrameworkRegistry._encode_mcp_tool_name("my.server", "获取本体")
        self.assertEqual(FrameworkRegistry._decode_mcp_tool_name(name), ("my.server", "获取本体"))

    def test_legacy_full_base64_name_decodes(self) -> None:
        legacy = f"mcp__{_b64('query-server')}__{_b64('query_paths')}"
        self.assertEqual(FrameworkRegistry._decode_mcp_tool_name(legacy), ("query-server", "query_paths"))

    def test_non_mcp_name_passes_through(self) -> None:
        self.assertEqual(FrameworkRegistry._decode_mcp_tool_name("read_file"), (None, "read_file"))

    def test_malformed_name_has_no_server(self) -> None:
        self.assertEqual(FrameworkRegistry._decode_mcp_tool_name("mcp__onlyserver"), (None, "mcp__onlyserver"))


class NormalizeMcpToolNameTests(unittest.TestCase):
    def test_legacy_name_migrates_to_literal(self) -> None:
        legacy = f"mcp__{_b64('query-server')}__{_b64('get_ontology_schema')}"
        self.assertEqual(
            FrameworkRegistry.normalize_mcp_tool_name(legacy),
            "mcp__query-server__get_ontology_schema",
        )

    def test_new_style_name_is_idempotent(self) -> None:
        name = "mcp__query-server__query_instances"
        self.assertEqual(FrameworkRegistry.normalize_mcp_tool_name(name), name)

    def test_encoded_name_is_idempotent(self) -> None:
        name = FrameworkRegistry._encode_mcp_tool_name("my.server", "获取本体")
        self.assertEqual(FrameworkRegistry.normalize_mcp_tool_name(name), name)

    def test_non_mcp_name_unchanged(self) -> None:
        self.assertEqual(FrameworkRegistry.normalize_mcp_tool_name("read_file"), "read_file")


class DisplayMcpToolNameTests(unittest.TestCase):
    def test_encoded_name_displays_readable(self) -> None:
        encoded = FrameworkRegistry._encode_mcp_tool_name("my.server", "获取本体")
        self.assertEqual(FrameworkRegistry.display_mcp_tool_name(encoded), "mcp__my.server__获取本体")

    def test_literal_name_displays_unchanged(self) -> None:
        self.assertEqual(
            FrameworkRegistry.display_mcp_tool_name("mcp__query-server__query_paths"),
            "mcp__query-server__query_paths",
        )


class ExportMcpToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_literal_name_keeps_description_without_prefix(self) -> None:
        registry = FrameworkRegistry()
        registry.mcp_client = _FakeMcpClient(
            [McpToolReference(server_name="query-server", tool_name="query_paths", description="查询路径")]
        )
        server = McpServerConfig(name="query-server", transport="stdio")
        exported = await registry._export_mcp_tools(server)
        self.assertEqual(len(exported), 1)
        function = exported[0]["function"]
        self.assertEqual(function["name"], "mcp__query-server__query_paths")
        self.assertEqual(function["description"], "查询路径")

    async def test_encoded_name_prefixes_description_with_real_names(self) -> None:
        registry = FrameworkRegistry()
        registry.mcp_client = _FakeMcpClient(
            [McpToolReference(server_name="my.server", tool_name="获取本体", description="获取本体模式")]
        )
        server = McpServerConfig(name="my.server", transport="stdio")
        exported = await registry._export_mcp_tools(server)
        function = exported[0]["function"]
        self.assertEqual(function["name"], f"mcp__{_b64('my.server')}__{_b64('获取本体')}")
        self.assertTrue(function["description"].startswith("[my.server.获取本体] "))
        self.assertIn("获取本体模式", function["description"])

    async def test_encoded_name_without_server_description_gets_fallback_and_prefix(self) -> None:
        registry = FrameworkRegistry()
        registry.mcp_client = _FakeMcpClient(
            [McpToolReference(server_name="my.server", tool_name="query")]
        )
        server = McpServerConfig(name="my.server", transport="stdio")
        exported = await registry._export_mcp_tools(server)
        function = exported[0]["function"]
        self.assertEqual(
            function["description"],
            "[my.server.query] MCP tool 'query' from server 'my.server'",
        )

    async def test_allowed_tool_names_filter_uses_remote_names(self) -> None:
        registry = FrameworkRegistry()
        registry.mcp_client = _FakeMcpClient(
            [
                McpToolReference(server_name="query-server", tool_name="query_paths"),
                McpToolReference(server_name="query-server", tool_name="query_neighbors"),
            ]
        )
        server = McpServerConfig(name="query-server", transport="stdio")
        exported = await registry._export_mcp_tools(server, {"query_paths"})
        self.assertEqual([tool["function"]["name"] for tool in exported], ["mcp__query-server__query_paths"])


if __name__ == "__main__":
    unittest.main()
