"""Tests for the Playwright browser builtin tools.

Layers: registration gating (flag), per-agent exposure, handler behavior
against fake pages (URL scheme, timeout capping, ref resolution, screenshot
budget), pure snapshot rendering, and — when a Chromium binary is available —
an end-to-end pass against a local HTTP server with a real headless browser.
"""

from __future__ import annotations

import base64
import json
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from covalent.core.agent import AgentSpec
from covalent.core.browser_tools import (
    BROWSER_CLICK_TOOL,
    BROWSER_CLOSE_TOOL,
    BROWSER_EVALUATE_TOOL,
    BROWSER_NAVIGATE_TOOL,
    BROWSER_PRESS_KEY_TOOL,
    BROWSER_SCREENSHOT_TOOL,
    BROWSER_SNAPSHOT_TOOL,
    BROWSER_TOOL_NAMES,
    BROWSER_TYPE_TOOL,
    _cap_timeout,
    _render_snapshot,
    register_browser_tools,
)
from covalent.infra.settings import AppSettings
from covalent.model.base import ProviderConfig
from covalent.registry.registry import FrameworkRegistry


def _agent(local_tools: list[str] | None = None) -> AgentSpec:
    return AgentSpec(
        name="a",
        description="test",
        system_prompt="test",
        provider=ProviderConfig(provider="openai_compatible", model="m"),
        local_tools=list(local_tools or []),
    )


_CTX = types.SimpleNamespace(session_id="s1")

_SNAPSHOT_PAYLOAD = {
    "title": "Example Domain",
    "url": "https://example.com/",
    "elements": [
        {"ref": "e1", "tag": "a", "role": "link", "label": "More information", "value": "", "href": "https://www.iana.org/domains/example", "type": "", "checked": False, "disabled": False},
        {"ref": "e2", "tag": "input", "role": "textbox", "label": "Search", "value": "query", "href": "", "type": "search", "checked": False, "disabled": False},
        {"ref": "e3", "tag": "button", "role": "button", "label": "Go", "value": "", "href": "", "type": "", "checked": False, "disabled": True},
    ],
}


class _FakeLocator:
    def __init__(self, page: "_FakePage", selector: str) -> None:
        self._page = page
        self._selector = selector

    async def click(self, **kwargs):
        self._page.calls.append(("click", self._selector))

    async def fill(self, text, **kwargs):
        self._page.calls.append(("fill", self._selector, text))

    async def press(self, key, **kwargs):
        self._page.calls.append(("press", self._selector, key))

    async def select_option(self, **kwargs):
        self._page.calls.append(("select_option", self._selector, kwargs))


class _FakeKeyboard:
    def __init__(self, page: "_FakePage") -> None:
        self._page = page

    async def press(self, key, **kwargs):
        self._page.calls.append(("keyboard_press", key))


class _FakePage:
    def __init__(self, *, snapshot_payload: dict | None = None, png: bytes = b"png", jpeg: bytes = b"jpeg") -> None:
        self.url = "https://example.com/"
        self.snapshot_payload = snapshot_payload or _SNAPSHOT_PAYLOAD
        self.png_bytes = png
        self.jpeg_bytes = jpeg
        self.calls: list[tuple] = []
        self.keyboard = _FakeKeyboard(self)

    async def evaluate(self, script, *args, **kwargs):
        self.calls.append(("evaluate",))
        return self.snapshot_payload

    async def goto(self, url, timeout=None):
        self.calls.append(("goto", url, timeout))
        return object()

    async def screenshot(self, **kwargs):
        self.calls.append(("screenshot", kwargs))
        return self.jpeg_bytes if kwargs.get("type") == "jpeg" else self.png_bytes

    def locator(self, selector: str) -> _FakeLocator:
        self.calls.append(("locator", selector))
        return _FakeLocator(self, selector)


class _FakeSession:
    def __init__(self, page: _FakePage, refs: dict | None = None) -> None:
        self.page = page
        self.refs = refs if refs is not None else {e["ref"]: e for e in _SNAPSHOT_PAYLOAD["elements"]}


class _FakeManager:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.acquired: list[str] = []
        self.released: list[str] = []

    async def acquire(self, scope_key: str):
        self.acquired.append(scope_key)
        return self.session

    def release(self, scope_key: str) -> bool:
        self.released.append(scope_key)
        return True


class RegistrationGatingTests(unittest.TestCase):
    def test_not_registered_when_flag_off(self) -> None:
        registry = FrameworkRegistry()
        from covalent.application.services.management_service import _register_browser_tools_if_enabled
        _register_browser_tools_if_enabled(registry, AppSettings(browser_tools_enabled=False))
        self.assertEqual(registry.local_tools, {})
        self.assertIsNone(registry.browser_manager)

    def test_registered_when_flag_on(self) -> None:
        registry = FrameworkRegistry()
        from covalent.application.services.management_service import _register_browser_tools_if_enabled
        _register_browser_tools_if_enabled(registry, AppSettings(browser_tools_enabled=True))
        for name in BROWSER_TOOL_NAMES:
            self.assertIn(name, registry.local_tools)
        self.assertIsNotNone(registry.browser_manager)

    def test_console_lists_only_registered_browser_tools(self) -> None:
        from covalent.application.services.management_service import (
            _available_local_tool_summaries,
            _register_browser_tools_if_enabled,
        )
        registry = FrameworkRegistry()
        _register_browser_tools_if_enabled(registry, AppSettings(browser_tools_enabled=True))
        names = {s.name for s in _available_local_tool_summaries(registry, AppSettings())}
        self.assertTrue(set(BROWSER_TOOL_NAMES) <= names)

        empty = FrameworkRegistry()
        summaries_off = _available_local_tool_summaries(empty, AppSettings(browser_tools_enabled=True))
        self.assertFalse(set(BROWSER_TOOL_NAMES) & {s.name for s in summaries_off})


class ExposureTests(unittest.IsolatedAsyncioTestCase):
    """Browser tools are a per-agent grant: listed in local_tools -> exposed;
    otherwise invisible even though registered."""

    async def asyncSetUp(self) -> None:
        from covalent.application.services.management_service import _register_browser_tools_if_enabled

        self.registry = FrameworkRegistry()
        _register_browser_tools_if_enabled(self.registry, AppSettings(browser_tools_enabled=True))

    async def test_exposed_when_granted(self) -> None:
        tools = await self.registry.resolve_tools_for_agent(_agent(local_tools=list(BROWSER_TOOL_NAMES)))
        self.assertTrue(set(BROWSER_TOOL_NAMES) <= {t["function"]["name"] for t in tools})

    async def test_not_exposed_without_grant(self) -> None:
        tools = await self.registry.resolve_tools_for_agent(_agent())
        self.assertFalse(set(BROWSER_TOOL_NAMES) & {t["function"]["name"] for t in tools})


class HandlerBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def _make(self, **settings_kwargs):
        settings = AppSettings(browser_tools_enabled=True, **settings_kwargs)
        page = _FakePage()
        session = _FakeSession(page)
        manager = _FakeManager(session)
        tools: dict = {}
        registry = types.SimpleNamespace(
            register_local_tool=lambda name, schema, handler=None: tools.__setitem__(name, handler)
        )
        register_browser_tools(registry, settings, manager)
        return settings, manager, page, tools

    async def test_scope_key_precedence_and_requirement(self) -> None:
        _, manager, _, tools = self._make()
        await tools[BROWSER_SNAPSHOT_TOOL]({}, _CTX)
        self.assertEqual(manager.acquired, ["s1"])
        workspace_ctx = types.SimpleNamespace(workspace_scope_id="ws9", session_id="s1")
        await tools[BROWSER_SNAPSHOT_TOOL]({}, workspace_ctx)
        self.assertEqual(manager.acquired[-1], "ws9")
        with self.assertRaises(ValueError):
            await tools[BROWSER_SNAPSHOT_TOOL]({}, types.SimpleNamespace())

    async def test_navigate_rejects_non_http_schemes(self) -> None:
        _, _, _, tools = self._make()
        for url in ("file:///etc/passwd", "data:text/html,hi", "javascript:alert(1)", "ftp://x"):
            with self.assertRaises(ValueError):
                await tools[BROWSER_NAVIGATE_TOOL]({"url": url}, _CTX)

    async def test_navigate_caps_timeout_and_snapshots(self) -> None:
        _, _, page, tools = self._make(browser_navigation_timeout_seconds=15.0)
        result = await tools[BROWSER_NAVIGATE_TOOL]({"url": "https://example.com", "timeout_seconds": 500}, _CTX)
        self.assertIn('e1 link "More information"', result)
        goto_calls = [c for c in page.calls if c[0] == "goto"]
        self.assertEqual(len(goto_calls), 1)
        self.assertEqual(goto_calls[0][1], "https://example.com")
        self.assertEqual(goto_calls[0][2], 15000)

    async def test_unknown_ref_error_advises_snapshot(self) -> None:
        _, _, _, tools = self._make()
        with self.assertRaises(ValueError) as ctx:
            await tools[BROWSER_CLICK_TOOL]({"ref": "e99"}, _CTX)
        self.assertIn("browser_snapshot", str(ctx.exception))

    async def test_click_and_type_use_ref_locator_then_snapshot(self) -> None:
        _, _, page, tools = self._make()
        clicked = await tools[BROWSER_CLICK_TOOL]({"ref": "e1"}, _CTX)
        self.assertIn("Clicked", clicked)
        self.assertIn("Interactive elements", clicked)
        typed = await tools[BROWSER_TYPE_TOOL]({"ref": "e2", "text": "hello"}, _CTX)
        self.assertIn("Typed into", typed)
        submitted = await tools[BROWSER_TYPE_TOOL]({"ref": "e2", "text": "hello", "submit": True}, _CTX)
        self.assertIn("submitted", submitted)
        selectors = [c[1] for c in page.calls if c[0] == "locator"]
        self.assertEqual(selectors, ['[data-cv-ref="e1"]', '[data-cv-ref="e2"]', '[data-cv-ref="e2"]'])
        fills = [c for c in page.calls if c[0] == "fill"]
        self.assertEqual(fills, [("fill", '[data-cv-ref="e2"]', "hello"), ("fill", '[data-cv-ref="e2"]', "hello")])
        presses = [c for c in page.calls if c[0] == "press"]
        self.assertEqual(presses, [("press", '[data-cv-ref="e2"]', "Enter")])

    async def test_press_key(self) -> None:
        _, _, page, tools = self._make()
        result = await tools[BROWSER_PRESS_KEY_TOOL]({"key": "Enter"}, _CTX)
        self.assertIn("Pressed Enter", result)
        self.assertIn("Interactive elements", result)

    async def test_close_releases_scope(self) -> None:
        _, manager, _, tools = self._make()
        result = await tools[BROWSER_CLOSE_TOOL]({}, _CTX)
        self.assertEqual(manager.released, ["s1"])
        self.assertIn("closed", result.lower())

    async def test_screenshot_returns_image_part(self) -> None:
        _, _, page, tools = self._make()
        page.png_bytes = b"\x89PNG" + b"0" * 128
        result = await tools[BROWSER_SCREENSHOT_TOOL]({}, _CTX)
        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["type"], "text")
        self.assertEqual(result[1]["type"], "image_url")
        self.assertTrue(result[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        decoded = base64.b64decode(result[1]["image_url"]["url"].split(",", 1)[1])
        self.assertEqual(decoded, page.png_bytes)

    async def test_screenshot_falls_back_to_jpeg_over_budget(self) -> None:
        _, _, page, tools = self._make(browser_max_screenshot_bytes=1024)
        page.png_bytes = b"\x89PNG" + b"0" * 4096
        page.jpeg_bytes = b"\xff\xd8" + b"0" * 512
        result = await tools[BROWSER_SCREENSHOT_TOOL]({}, _CTX)
        self.assertTrue(result[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        screenshot_types = [c[1].get("type") for c in page.calls if c[0] == "screenshot"]
        self.assertEqual(screenshot_types, ["png", "jpeg"])

    async def test_screenshot_over_budget_every_format_errors(self) -> None:
        _, _, page, tools = self._make(browser_max_screenshot_bytes=128)
        page.png_bytes = b"p" * 4096
        page.jpeg_bytes = b"j" * 4096
        with self.assertRaises(ValueError) as ctx:
            await tools[BROWSER_SCREENSHOT_TOOL]({}, _CTX)
        self.assertIn("inline budget", str(ctx.exception))

    async def test_evaluate_returns_json(self) -> None:
        _, _, page, tools = self._make()

        async def evaluate(script, *a, **k):
            return {"hello": "world"}

        page.evaluate = evaluate
        result = await tools[BROWSER_EVALUATE_TOOL]({"expression": "1+1"}, _CTX)
        self.assertEqual(json.loads(result), {"hello": "world"})


class SnapshotRenderingTests(unittest.TestCase):
    def test_render_lists_refs_labels_and_flags(self) -> None:
        rendered = _render_snapshot(_SNAPSHOT_PAYLOAD)
        self.assertIn("Page: Example Domain", rendered)
        self.assertIn("URL: https://example.com/", rendered)
        self.assertIn('- e1 link "More information" href=https://www.iana.org/domains/example', rendered)
        self.assertIn("- e2 textbox \"Search\" value='query' type=search", rendered)
        self.assertIn('- e3 button "Go" [disabled]', rendered)

    def test_render_empty_page(self) -> None:
        rendered = _render_snapshot({"title": "", "url": "about:blank", "elements": []})
        self.assertIn("(no interactive elements found)", rendered)

    def test_cap_timeout(self) -> None:
        self.assertEqual(_cap_timeout(None, 30.0), 30.0)
        self.assertEqual(_cap_timeout(500, 30.0), 30.0)
        self.assertEqual(_cap_timeout(5, 30.0), 5.0)
        self.assertEqual(_cap_timeout("bad", 30.0), 30.0)
        self.assertEqual(_cap_timeout(0, 30.0), 1.0)


_INTEGRATION_HTML = (
    b"<html><head><title>Covalent Browser IT</title></head><body>"
    b"<h1>Covalent IT</h1>"
    b"<a href='#next' id='lnk'>Next page</a>"
    b"<input id='q' placeholder='Search' value=''/>"
    b"<button id='go' onclick=\"document.getElementById('st').textContent='clicked'\">Go</button>"
    b"<p id='st'></p>"
    b"</body></html>"
)


class _ITHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_INTEGRATION_HTML)

    def log_message(self, *args) -> None:
        pass


class RealBrowserIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Runs only when a Playwright Chromium binary is installed locally; skips
    silently elsewhere (e.g. CI without browsers)."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                browser.close()
        except Exception:
            raise unittest.SkipTest("Playwright Chromium not installed; skipping real-browser tests")

    async def asyncSetUp(self) -> None:
        from covalent.runtime.browser_manager import BrowserManager

        self.server = HTTPServer(("127.0.0.1", 0), _ITHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}/"
        self.manager = BrowserManager(AppSettings(browser_tools_enabled=True))
        self.tools: dict = {}
        register_browser_tools(
            types.SimpleNamespace(register_local_tool=lambda name, schema, handler=None: self.tools.__setitem__(name, handler)),
            AppSettings(browser_tools_enabled=True),
            self.manager,
        )
        self.ctx = types.SimpleNamespace(session_id="it-session")

    async def asyncTearDown(self) -> None:
        await self.manager.aclose()
        self.server.shutdown()
        self.server.server_close()

    async def test_navigate_click_type_screenshot_end_to_end(self) -> None:
        snapshot = await self.tools[BROWSER_NAVIGATE_TOOL]({"url": self.base}, self.ctx)
        self.assertIn("Covalent Browser IT", snapshot)
        self.assertIn("e1 link", snapshot)

        clicked = await self.tools[BROWSER_CLICK_TOOL]({"ref": "e3"}, self.ctx)
        self.assertIn("Clicked", clicked)
        status = json.loads(await self.tools[BROWSER_EVALUATE_TOOL]({"expression": "document.getElementById('st').textContent"}, self.ctx))
        self.assertEqual(status, "clicked")

        typed = await self.tools[BROWSER_TYPE_TOOL]({"ref": "e2", "text": "covalent query"}, self.ctx)
        self.assertIn("Typed into", typed)
        value = json.loads(await self.tools[BROWSER_EVALUATE_TOOL]({"expression": "document.getElementById('q').value"}, self.ctx))
        self.assertEqual(value, "covalent query")

        result = await self.tools[BROWSER_SCREENSHOT_TOOL]({}, self.ctx)
        self.assertEqual(result[1]["type"], "image_url")
        self.assertTrue(result[1]["image_url"]["url"].startswith("data:image/"))

        closed = await self.tools[BROWSER_CLOSE_TOOL]({}, self.ctx)
        self.assertIn("closed", closed.lower())

    async def test_contexts_are_isolated_per_scope(self) -> None:
        other_ctx = types.SimpleNamespace(session_id="it-session-2")
        await self.tools[BROWSER_NAVIGATE_TOOL]({"url": self.base}, self.ctx)
        await self.tools[BROWSER_NAVIGATE_TOOL]({"url": self.base}, other_ctx)
        await self.tools[BROWSER_TYPE_TOOL]({"ref": "e2", "text": "session one"}, self.ctx)
        await self.tools[BROWSER_TYPE_TOOL]({"ref": "e2", "text": "session two"}, other_ctx)
        first = json.loads(await self.tools[BROWSER_EVALUATE_TOOL]({"expression": "document.getElementById('q').value"}, self.ctx))
        second = json.loads(await self.tools[BROWSER_EVALUATE_TOOL]({"expression": "document.getElementById('q').value"}, other_ctx))
        self.assertEqual(first, "session one")
        self.assertEqual(second, "session two")


if __name__ == "__main__":
    unittest.main()
