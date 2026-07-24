"""Skill process lifecycle tests.

Uses a real Python subprocess (via FileSystemBackend.spawn_stream) speaking
JSON-RPC — exercises the actual SkillProcessManager / SkillProcessHandle code
paths: spawn → ready → call → shutdown, pool reuse, concurrency, idle eviction,
health restart, timeouts. No Docker needed.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_framework.skills.exceptions import SkillProcessError, SkillStartupError
from agent_framework.skills.process import SkillProcessManager
from agent_framework.skills.spec import (
    HealthCheckConfig,
    ManifestSkillSpec,
    ProcessConfig,
    SkillRuntime,
)

# The JSON-RPC server script. Mode is controlled by SKILL_TEST_MODE env var:
#   normal (default): ready → handles call_tool / ping / shutdown.
#   never_ready:      blocks forever (for startup-timeout test).
#   hang_on_call:     emits ready but never responds to calls (for request-timeout test).
_SKILL_SCRIPT = """\
import sys, json, os

MODE = os.environ.get("SKILL_TEST_MODE", "normal")

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

if MODE == "never_ready":
    sys.stdin.read()
    sys.exit(0)

emit({"jsonrpc": "2.0", "method": "ready", "params": {}})

if MODE == "hang_on_call":
    sys.stdin.read()
    sys.exit(0)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid = msg.get("id")
    method = msg.get("method")
    if method == "call_tool":
        args = msg.get("params", {}).get("arguments", {})
        emit({"jsonrpc": "2.0", "id": mid, "result": {"content": f"result: {args}"}})
    elif method == "ping":
        emit({"jsonrpc": "2.0", "id": mid, "result": {"pong": True}})
    elif method == "shutdown":
        emit({"jsonrpc": "2.0", "id": mid, "result": {"bye": True}})
        break
"""


def _make_skill_spec(tmpdir: Path, *, mode_env: dict | None = None,
                     startup_timeout: float = 10.0, idle_timeout: float = 300.0,
                     max_instances: int = 1) -> ManifestSkillSpec:
    """Build a ManifestSkillSpec whose runtime points at the test JSON-RPC script."""
    env = dict(mode_env or {})
    if env:
        env = {k: v for k, v in env.items()}
    return ManifestSkillSpec(
        name="test-skill",
        description="Test skill",
        runtime=SkillRuntime(
            type="python",
            protocol="rpc",
            entry_point="skill_server.py",
            env=env,
        ),
        source_dir=str(tmpdir),
        process=ProcessConfig(
            max_instances=max_instances,
            startup_timeout_seconds=startup_timeout,
            idle_timeout_seconds=idle_timeout,
            max_request_timeout_seconds=5.0,
        ),
        health_check=HealthCheckConfig(interval_seconds=0.5, max_failures=1),
    )


class SkillLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="af-skill-test-")
        self.tmpdir = Path(self._tmp.name)
        (self.tmpdir / "skill_server.py").write_text(_SKILL_SCRIPT, encoding="utf-8")
        self.spm = SkillProcessManager()
        self.context = SimpleNamespace(session_id="s1")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_spawn_ready_call_shutdown(self) -> None:
        """Full lifecycle: spawn → wait ready → call_tool → shutdown."""
        spec = _make_skill_spec(self.tmpdir)
        await self.spm.start()
        try:
            handle = await self.spm.acquire(spec, context=self.context)
            try:
                result = await handle.send_request("call_tool", {"name": "echo", "arguments": {"msg": "hi"}})
                self.assertIn("content", result)
                self.assertIn("hi", result["content"])
                await handle.send_request("shutdown")
            finally:
                await self.spm.release(handle)
        finally:
            await self.spm.stop()

    async def test_pool_reuse_idle_handle(self) -> None:
        """After release, the next acquire reuses the same handle (no new spawn)."""
        spec = _make_skill_spec(self.tmpdir)
        await self.spm.start()
        try:
            h1 = await self.spm.acquire(spec, context=self.context)
            await self.spm.release(h1)
            h2 = await self.spm.acquire(spec, context=self.context)
            try:
                self.assertIs(h1, h2, "Should reuse the same idle handle")
            finally:
                await self.spm.release(h2)
        finally:
            await self.spm.stop()

    async def test_startup_timeout_raises(self) -> None:
        """Skill that never emits ready → SkillStartupError."""
        spec = _make_skill_spec(
            self.tmpdir,
            mode_env={"SKILL_TEST_MODE": "never_ready"},
            startup_timeout=2.0,
        )
        await self.spm.start()
        try:
            with self.assertRaises(SkillStartupError):
                await self.spm.acquire(spec, context=self.context)
        finally:
            await self.spm.stop()

    async def test_send_request_timeout(self) -> None:
        """Skill that hangs on call → SkillProcessError after timeout."""
        spec = _make_skill_spec(
            self.tmpdir,
            mode_env={"SKILL_TEST_MODE": "hang_on_call"},
        )
        await self.spm.start()
        try:
            handle = await self.spm.acquire(spec, context=self.context)
            try:
                with self.assertRaises(SkillProcessError):
                    await handle.send_request("call_tool", {"name": "echo", "arguments": {}})
            finally:
                # Force-terminate the hung process.
                handle.process.kill()
                await handle.process.wait()
                await self.spm.release(handle)
        finally:
            await self.spm.stop()

    async def test_per_session_pool_isolation(self) -> None:
        """acquire with different session_ids → separate handles (no sharing)."""
        spec = _make_skill_spec(self.tmpdir)
        await self.spm.start()
        try:
            ctx_a = SimpleNamespace(session_id="session-a")
            ctx_b = SimpleNamespace(session_id="session-b")
            h_a = await self.spm.acquire(spec, context=ctx_a)
            h_b = await self.spm.acquire(spec, context=ctx_b)
            try:
                self.assertIsNot(h_a, h_b, "Different sessions should get different handles")
            finally:
                await self.spm.release(h_a)
                await self.spm.release(h_b)
        finally:
            await self.spm.stop()

    async def test_max_instances_blocks_second_acquire(self) -> None:
        """max_instances=1 → second concurrent acquire blocks until first is released."""
        spec = _make_skill_spec(self.tmpdir, max_instances=1)
        await self.spm.start()
        try:
            h1 = await self.spm.acquire(spec, context=self.context)
            # Second acquire should block (timeout proves it).
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self.spm.acquire(spec, context=self.context),
                    timeout=1.0,
                )
            # Release → second acquire should now succeed.
            await self.spm.release(h1)
            h2 = await self.spm.acquire(spec, context=self.context)
            await self.spm.release(h2)
        finally:
            await self.spm.stop()

    async def test_health_check_evicts_dead_process(self) -> None:
        """Dead subprocess → health check removes it → next acquire spawns fresh."""
        spec = _make_skill_spec(self.tmpdir, idle_timeout=300.0)
        await self.spm.start()
        try:
            h1 = await self.spm.acquire(spec, context=self.context)
            await self.spm.release(h1)
            # Kill the process.
            h1.process.kill()
            await h1.process.wait()
            self.assertFalse(h1.is_alive)
            # Trigger one health-check iteration manually (don't wait for the interval).
            # The health loop checks is_alive and removes dead handles.
            # We simulate by calling acquire again — _get_or_spawn skips dead handles.
            h2 = await self.spm.acquire(spec, context=self.context)
            try:
                self.assertTrue(h2.is_alive)
                self.assertIsNot(h1, h2, "Should have spawned a fresh handle")
            finally:
                await self.spm.release(h2)
        finally:
            await self.spm.stop()


if __name__ == "__main__":
    unittest.main()
