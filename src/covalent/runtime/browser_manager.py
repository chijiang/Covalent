"""Browser manager — one shared Playwright Chromium in the API server process,
with per-scope isolated contexts.

The browser runs on the host, the same trust boundary as the other in-process
builtin tools (workspace/PDF); it is NOT sandboxed. Scope keys follow the
workspace precedence (``workspace_scope_id`` → ``execution_scope_id`` →
``session_id``) so collaborating agents in one workspace share cookies and
storage, while distinct sessions stay isolated. Contexts idle past
``browser_idle_timeout_seconds`` are swept by a background task; shutdown
closes everything via ``FrameworkRegistry.aclose``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_SWEEP_INTERVAL_SECONDS = 60.0


class BrowserSession:
    """One scope's browsing state: an isolated BrowserContext plus its Page."""

    def __init__(self, scope_key: str, context: Any, page: Any) -> None:
        self.scope_key = scope_key
        self.context = context
        self.page = page
        self.last_used = time.monotonic()
        # ref ("e1", ...) -> element descriptor from the latest snapshot; used
        # by click/type to resolve refs and stays stale-safe (actions validate).
        self.refs: dict[str, dict[str, Any]] = {}

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def is_alive(self) -> bool:
        try:
            return not self.page.is_closed()
        except Exception:
            return False


class BrowserManager:
    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._playwright: Any = None
        self._browser: Any = None
        self._sessions: dict[str, BrowserSession] = {}
        self._sweeper_task: asyncio.Task[None] | None = None

    async def acquire(self, scope_key: str) -> BrowserSession:
        session = self._sessions.get(scope_key)
        if session is not None and session.is_alive():
            session.touch()
            return session
        if session is not None:
            await self._discard(session)
            self._sessions.pop(scope_key, None)
        browser = await self._ensure_browser()
        async with self._lock:
            session = self._sessions.get(scope_key)
            if session is None or not session.is_alive():
                session = await self._start_session(scope_key, browser)
                self._sessions[scope_key] = session
        session.touch()
        return session

    def release(self, scope_key: str) -> bool:
        """Drop a scope's session (closed by the agent); True if one existed."""
        session = self._sessions.pop(scope_key, None)
        if session is None:
            return False
        asyncio.get_running_loop().create_task(self._discard(session))
        return True

    async def sweep_idle(self) -> int:
        if self._idle_timeout() <= 0:
            return 0
        deadline = self._idle_timeout()
        now = time.monotonic()
        stale = [
            key
            for key, session in self._sessions.items()
            if now - session.last_used > deadline
        ]
        for key in stale:
            session = self._sessions.pop(key, None)
            if session is not None:
                await self._discard(session)
        if stale:
            logger.info("Browser idle sweep closed %d context(s)", len(stale))
        return len(stale)

    async def aclose(self) -> None:
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            try:
                await self._sweeper_task
            except asyncio.CancelledError:
                pass
            self._sweeper_task = None
        for session in self._sessions.values():
            await self._discard(session)
        self._sessions.clear()
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logger.warning("Error closing browser", exc_info=True)
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                logger.warning("Error stopping playwright driver", exc_info=True)

    # -- internals -----------------------------------------------------------

    async def _ensure_browser(self) -> Any:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - guarded at registration
                raise RuntimeError(
                    "The playwright package is not installed. Run `pip install playwright` "
                    "on the API server host."
                ) from exc
            try:
                self._playwright = await async_playwright().start()
                launch_kwargs: dict[str, Any] = {
                    "headless": bool(getattr(self._settings, "browser_headless", True))
                }
                channel = getattr(self._settings, "browser_channel", None)
                if channel:
                    launch_kwargs["channel"] = channel
                executable_path = getattr(self._settings, "browser_executable_path", None)
                if executable_path:
                    launch_kwargs["executable_path"] = executable_path
                self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            except Exception as exc:
                self._browser = None
                if self._playwright is not None:
                    try:
                        await self._playwright.stop()
                    except Exception:
                        pass
                    self._playwright = None
                if "Executable doesn't exist" in str(exc) or "looks like Playwright was just installed" in str(exc):
                    raise RuntimeError(
                        "Playwright browser binary is missing. Run "
                        "`playwright install chromium` on the API server host."
                    ) from exc
                raise
            self._start_sweeper()
            return self._browser

    async def _start_session(self, scope_key: str, browser: Any) -> BrowserSession:
        context = await browser.new_context()
        timeout_ms = int(self._nav_timeout() * 1000)
        context.set_default_timeout(timeout_ms)
        context.set_default_navigation_timeout(timeout_ms)
        page = await context.new_page()
        return BrowserSession(scope_key, context, page)

    async def _discard(self, session: BrowserSession) -> None:
        try:
            await session.context.close()
        except Exception:
            pass

    def _start_sweeper(self) -> None:
        if self._sweeper_task is not None or self._idle_timeout() <= 0:
            return
        self._sweeper_task = asyncio.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
            await self.sweep_idle()

    def _nav_timeout(self) -> float:
        return float(getattr(self._settings, "browser_navigation_timeout_seconds", 30.0) or 30.0)

    def _idle_timeout(self) -> float:
        return float(getattr(self._settings, "browser_idle_timeout_seconds", 900.0) or 0.0)
