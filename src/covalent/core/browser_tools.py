"""Browser tools — opt-in Playwright (headless Chromium) tools that run in the
API server process. NOT sandboxed: the browser has the host's network reach,
like every other in-process builtin tool. Registration is gated by
``browser_tools_enabled`` and exposure is a per-agent grant via
``BUILTIN_AGENT_TOOLS`` (never in the default agent toolset).

Interaction follows the snapshot-then-ref pattern proven by @playwright/mcp:
``browser_snapshot`` enumerates visible interactive elements in document
order, tags each with a ``data-cv-ref`` attribute, and renders a ref'd text
tree; ``browser_click``/``browser_type`` resolve refs against that snapshot.
A stale ref (DOM changed underneath) fails with an instruction to re-snapshot
rather than clicking the wrong element.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any
from urllib.parse import urlparse

BROWSER_NAVIGATE_TOOL = "browser_navigate"
BROWSER_SNAPSHOT_TOOL = "browser_snapshot"
BROWSER_SCREENSHOT_TOOL = "browser_screenshot"
BROWSER_CLICK_TOOL = "browser_click"
BROWSER_TYPE_TOOL = "browser_type"
BROWSER_PRESS_KEY_TOOL = "browser_press_key"
BROWSER_EVALUATE_TOOL = "browser_evaluate"
BROWSER_CLOSE_TOOL = "browser_close"

BROWSER_TOOL_NAMES = (
    BROWSER_NAVIGATE_TOOL,
    BROWSER_SNAPSHOT_TOOL,
    BROWSER_SCREENSHOT_TOOL,
    BROWSER_CLICK_TOOL,
    BROWSER_TYPE_TOOL,
    BROWSER_PRESS_KEY_TOOL,
    BROWSER_EVALUATE_TOOL,
    BROWSER_CLOSE_TOOL,
)

_EVALUATE_MAX_CHARS = 20_000
_JPEG_QUALITY_STEPS = (85, 60)

# Same filter for enumeration and ref attribution; querySelectorAll walks in
# document order, so refs are deterministic for a given DOM.
_ENUMERATE_JS = """
() => {
  const SELECTOR = "a[href], button, input, select, textarea, [role], [onclick], [contenteditable]";
  const describe = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    let label = el.getAttribute("aria-label") || el.getAttribute("placeholder")
      || el.getAttribute("title") || el.getAttribute("alt") || "";
    let text = "";
    if (tag === "input" || tag === "textarea") {
      text = (el.value || "").trim();
    } else {
      text = (el.innerText || "").trim().replace(/\\s+/g, " ");
    }
    if (!label) label = text;
    const role = el.getAttribute("role") || ({
      a: el.getAttribute("href") ? "link" : "",
      button: "button", select: "combobox", textarea: "textbox",
    }[tag] || "");
    return {
      ref: el.getAttribute("data-cv-ref") || "",
      tag,
      role,
      type,
      label: label.slice(0, 120),
      value: text !== label ? text.slice(0, 120) : "",
      href: tag === "a" ? (el.getAttribute("href") || "").slice(0, 200) : "",
      disabled: el.disabled === true || el.getAttribute("aria-disabled") === "true",
      checked: el.checked === true,
    };
  };
  const elements = [];
  for (const el of document.querySelectorAll(SELECTOR)) {
    let visible = true;
    try {
      visible = el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
    } catch (err) {
      visible = true;
    }
    if (!visible) continue;
    const tag = el.tagName.toLowerCase();
    if (tag === "input" && (el.getAttribute("type") || "text").toLowerCase() === "hidden") continue;
    const ref = "e" + (elements.length + 1);
    el.setAttribute("data-cv-ref", ref);
    elements.push(describe(el));
  }
  return { title: document.title, url: location.href, elements };
}
"""


def register_browser_tools(registry: Any, settings: Any, manager: Any) -> None:
    for name, schema in _schemas():
        registry.register_local_tool(
            name,
            schema,
            handler=lambda args, ctx, _name=name: _dispatch(_name, manager, settings, ctx, args),
        )


def _schemas() -> list[tuple[str, dict[str, Any]]]:
    def function_schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> tuple[str, dict[str, Any]]:
        return (
            name,
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {"type": "object", "properties": properties, "required": required},
                },
            },
        )

    return [
        function_schema(
            BROWSER_NAVIGATE_TOOL,
            "Navigate the browser to an http(s) URL and return a snapshot of the page's "
            "interactive elements with refs. Use refs with browser_click/browser_type.",
            {
                "url": {"type": "string", "description": "Absolute http(s) URL to open."},
                "timeout_seconds": {"type": "number", "minimum": 1, "description": "Navigation timeout; capped by server config."},
            },
            ["url"],
        ),
        function_schema(
            BROWSER_SNAPSHOT_TOOL,
            "Take a fresh snapshot of the current page: interactive elements with refs "
            "(e1, e2, ...). Take one whenever refs look stale or after page changes.",
            {},
            [],
        ),
        function_schema(
            BROWSER_SCREENSHOT_TOOL,
            "Screenshot the current page and return it as an image (requires a "
            "vision-capable model to interpret). Use for visual/layout inspection.",
            {
                "full_page": {"type": "boolean", "default": False, "description": "Capture the entire scrollable page instead of the viewport."},
                "type": {"type": "string", "enum": ["png", "jpeg"], "default": "png"},
            },
            [],
        ),
        function_schema(
            BROWSER_CLICK_TOOL,
            "Click the element with the given ref from the latest browser_snapshot.",
            {"ref": {"type": "string", "description": "Element ref from the latest snapshot, e.g. e3."}},
            ["ref"],
        ),
        function_schema(
            BROWSER_TYPE_TOOL,
            "Type text into the input/textarea/combobox with the given ref, replacing "
            "its current value. Set submit=true to press Enter afterwards.",
            {
                "ref": {"type": "string", "description": "Element ref from the latest snapshot, e.g. e2."},
                "text": {"type": "string"},
                "submit": {"type": "boolean", "default": False},
            },
            ["ref", "text"],
        ),
        function_schema(
            BROWSER_PRESS_KEY_TOOL,
            "Press a keyboard key on the focused page, e.g. Enter, Escape, Tab, ArrowDown.",
            {"key": {"type": "string", "description": "Key name, e.g. Enter, Escape, Control+a."}},
            ["key"],
        ),
        function_schema(
            BROWSER_EVALUATE_TOOL,
            "Evaluate a JavaScript expression in the page and return the JSON result. "
            "Use for extracting text/metadata the snapshot does not show.",
            {"expression": {"type": "string", "description": "JavaScript expression or function body, e.g. document.body.innerText.slice(0, 2000)."}},
            ["expression"],
        ),
        function_schema(
            BROWSER_CLOSE_TOOL,
            "Close this session's browser context, discarding its cookies and pages.",
            {},
            [],
        ),
    ]


async def _dispatch(name: str, manager: Any, settings: Any, context: Any, args: dict[str, Any]) -> Any:
    scope_key = _scope_key(context)
    if name == BROWSER_CLOSE_TOOL:
        manager.release(scope_key)
        return "Browser context closed."

    session = await manager.acquire(scope_key)
    if name == BROWSER_NAVIGATE_TOOL:
        return await _navigate(session, settings, args)
    if name == BROWSER_SNAPSHOT_TOOL:
        return await _snapshot(session, settings)
    if name == BROWSER_SCREENSHOT_TOOL:
        return await _screenshot(session, settings, args)
    if name == BROWSER_CLICK_TOOL:
        return await _click(session, settings, str(args.get("ref", "")))
    if name == BROWSER_TYPE_TOOL:
        return await _type(session, settings, str(args.get("ref", "")), str(args.get("text", "")), bool(args.get("submit", False)))
    if name == BROWSER_PRESS_KEY_TOOL:
        return await _press_key(session, settings, str(args.get("key", "")))
    if name == BROWSER_EVALUATE_TOOL:
        return await _evaluate(session, settings, str(args.get("expression", "")))
    raise ValueError(f"Unknown browser tool: {name}")


def _scope_key(context: Any) -> str:
    scope_id = (
        getattr(context, "workspace_scope_id", None)
        or getattr(context, "execution_scope_id", None)
        or getattr(context, "session_id", None)
    )
    if isinstance(scope_id, str) and scope_id.strip():
        return scope_id.strip()
    raise ValueError("Browser tools require a session context with a workspace/scope id")


async def _navigate(session: Any, settings: Any, args: dict[str, Any]) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        raise ValueError("browser_navigate requires a 'url'")
    if urlparse(url).scheme.lower() not in ("http", "https"):
        raise ValueError("browser_navigate only accepts http(s) URLs")
    timeout = _cap_timeout(args.get("timeout_seconds"), _nav_timeout(settings))
    try:
        await _bounded(session.page.goto(url, timeout=int(timeout * 1000)), timeout * 2, f"navigation to {url}")
    except Exception as exc:
        raise RuntimeError(f"Navigation to {url} failed: {exc}") from exc
    return await _snapshot(session, settings)


async def _snapshot(session: Any, settings: Any) -> str:
    try:
        payload = await _bounded(session.page.evaluate(_ENUMERATE_JS), _nav_timeout(settings), "snapshot")
    except Exception as exc:
        raise RuntimeError(f"Snapshot failed: {exc}") from exc
    session.refs = {element["ref"]: element for element in payload.get("elements", []) if element.get("ref")}
    return _render_snapshot(payload)


async def _screenshot(session: Any, settings: Any, args: dict[str, Any]) -> list[dict[str, Any]]:
    budget = int(getattr(settings, "browser_max_screenshot_bytes", 8 * 1024 * 1024))
    full_page = bool(args.get("full_page", False))
    image_type = args.get("type", "png")
    if image_type not in ("png", "jpeg"):
        raise ValueError("browser_screenshot type must be 'png' or 'jpeg'")

    raw = await _bounded(
        session.page.screenshot(full_page=full_page, type=image_type),
        _nav_timeout(settings),
        "screenshot",
    )
    mime = f"image/{image_type}"
    if image_type == "png" and len(raw) > budget:
        for quality in _JPEG_QUALITY_STEPS:
            raw = await _bounded(
                session.page.screenshot(full_page=full_page, type="jpeg", quality=quality),
                _nav_timeout(settings),
                "screenshot",
            )
            mime = "image/jpeg"
            if len(raw) <= budget:
                break
    if len(raw) > budget:
        raise ValueError(
            f"Screenshot is {len(raw)} bytes, over the {budget}-byte inline budget. "
            "Retry with full_page=false or type='jpeg'."
        )
    data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    return [
        {"type": "text", "text": f"Screenshot of {session.page.url} ({'full page' if full_page else 'viewport'}, {len(raw)} bytes)"},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]


async def _click(session: Any, settings: Any, ref: str) -> str:
    element = _resolve_ref(session, ref)
    try:
        await _bounded(session.page.locator(f'[data-cv-ref="{ref}"]').click(), _nav_timeout(settings), f"click {ref}")
    except Exception as exc:
        raise RuntimeError(f"Click on {ref} failed (snapshot may be stale): {exc}") from exc
    note = _describe(element)
    return f"Clicked {note}.\n\n" + await _snapshot(session, settings)


async def _type(session: Any, settings: Any, ref: str, text: str, submit: bool) -> str:
    if not text:
        raise ValueError("browser_type requires non-empty 'text'")
    element = _resolve_ref(session, ref)
    locator = session.page.locator(f'[data-cv-ref="{ref}"]')
    try:
        if element.get("tag") == "select":
            await _bounded(locator.select_option(label=text), _nav_timeout(settings), f"select {ref}")
        else:
            await _bounded(locator.fill(text), _nav_timeout(settings), f"type into {ref}")
        if submit:
            await _bounded(locator.press("Enter"), _nav_timeout(settings), f"submit {ref}")
    except Exception as exc:
        raise RuntimeError(f"Type into {ref} failed (snapshot may be stale): {exc}") from exc
    note = _describe(element)
    suffix = " and submitted" if submit else ""
    return f"Typed into {note}{suffix}.\n\n" + await _snapshot(session, settings)


async def _press_key(session: Any, settings: Any, key: str) -> str:
    if not key:
        raise ValueError("browser_press_key requires a 'key'")
    try:
        await _bounded(session.page.keyboard.press(key), _nav_timeout(settings), f"press {key}")
    except Exception as exc:
        raise RuntimeError(f"Press {key} failed: {exc}") from exc
    return f"Pressed {key}.\n\n" + await _snapshot(session, settings)


async def _evaluate(session: Any, settings: Any, expression: str) -> str:
    if not expression.strip():
        raise ValueError("browser_evaluate requires an 'expression'")
    try:
        result = await _bounded(session.page.evaluate(expression), _nav_timeout(settings), "evaluate")
    except Exception as exc:
        raise RuntimeError(f"Evaluate failed: {exc}") from exc
    rendered = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if len(rendered) > _EVALUATE_MAX_CHARS:
        rendered = rendered[:_EVALUATE_MAX_CHARS] + "\n[truncated]"
    return rendered


def _resolve_ref(session: Any, ref: str) -> dict[str, Any]:
    ref = ref.strip()
    element = session.refs.get(ref)
    if element is None:
        raise ValueError(
            f"Unknown ref {ref!r}. Take browser_snapshot again to get fresh refs."
            if session.refs
            else f"Unknown ref {ref!r}. Take browser_snapshot first."
        )
    return element


def _describe(element: dict[str, Any]) -> str:
    role = element.get("role") or element.get("tag") or "element"
    label = element.get("label") or element.get("value") or ""
    return f"{role} {element.get('ref', '')} \"{label}\"".strip()


def _render_snapshot(payload: dict[str, Any]) -> str:
    lines = [f"Page: {payload.get('title') or '(untitled)'}", f"URL: {payload.get('url', '')}", ""]
    elements = payload.get("elements", [])
    if not elements:
        lines.append("(no interactive elements found)")
        return "\n".join(lines)
    lines.append("Interactive elements (use refs with browser_click / browser_type):")
    for element in elements:
        role = element.get("role") or element.get("tag")
        label = element.get("label") or ""
        parts = [f"- {element['ref']} {role}"]
        if label:
            parts.append(f'"{label}"')
        if element.get("value") and element["value"] != label:
            parts.append(f"value={element['value']!r}")
        if element.get("href"):
            parts.append(f"href={element['href']}")
        if element.get("type") and element["type"] not in ("text",):
            parts.append(f"type={element['type']}")
        if element.get("checked"):
            parts.append("[checked]")
        if element.get("disabled"):
            parts.append("[disabled]")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _cap_timeout(raw: Any, cap: float) -> float:
    if raw is None:
        return cap
    try:
        return max(1.0, min(float(raw), cap))
    except (TypeError, ValueError):
        return cap


def _nav_timeout(settings: Any) -> float:
    return float(getattr(settings, "browser_navigation_timeout_seconds", 30.0) or 30.0)


async def _bounded(awaitable: Any, timeout: float, what: str) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"Browser {what} timed out after {timeout:g}s") from exc
