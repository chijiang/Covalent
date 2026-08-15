"""Skill process pool isolation per sandbox instance."""

from __future__ import annotations

import asyncio
import unittest

from covalent.core.types import RunContext
from covalent.skills.process import SkillProcessHandle, SkillProcessManager
from covalent.skills.spec import ManifestSkillSpec, ProcessConfig, SkillRuntime


class _FakeProc:
    def __init__(self) -> None:
        self.returncode = None
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


def _spec(name: str = "echo") -> ManifestSkillSpec:
    return ManifestSkillSpec(
        name=name,
        description="pool isolation test skill",
        runtime=SkillRuntime(type="python", entry_point="server.py"),
        process=ProcessConfig(max_request_timeout_seconds=0.01, startup_timeout_seconds=0.01),
    )


def _handle(spec: ManifestSkillSpec) -> SkillProcessHandle:
    handle = SkillProcessHandle(spec=spec, process=_FakeProc())
    handle._ready.set()
    return handle


class SkillProcessPoolIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = SkillProcessManager()
        self.spawned: list[str | None] = []

        async def fake_spawn(spec, sandbox_instance_id=None):
            self.spawned.append(sandbox_instance_id)
            handle = _handle(spec)
            handle._sandbox_instance_id = sandbox_instance_id
            return handle

        self.manager._spawn = fake_spawn  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        await self.manager.stop()

    async def test_same_instance_reuses_handle_distinct_instances_do_not(self) -> None:
        spec = _spec()
        ctx_a = RunContext(agent_name="m", session_id="sess-1", sandbox_instance_id="sbx-a")
        ctx_b = RunContext(agent_name="d", session_id="sess-1", sandbox_instance_id="sbx-b")

        first = await self.manager.acquire(spec, ctx_a)
        await self.manager.release(first)
        second = await self.manager.acquire(spec, ctx_a)
        self.assertIs(first, second, "same (skill, instance) must reuse the warm handle")
        await self.manager.release(second)

        other = await self.manager.acquire(spec, ctx_b)
        self.assertIsNot(first, other, "distinct sandbox instances must never share a handle")
        self.assertEqual(self.spawned, ["sbx-a", "sbx-b"])

    async def test_legacy_context_keys_pool_by_session(self) -> None:
        spec = _spec()
        legacy = RunContext(agent_name="m", session_id="sess-legacy")
        handle = await self.manager.acquire(spec, legacy)
        await self.manager.release(handle)
        self.assertEqual(self.spawned, ["sess-legacy"])

    async def test_stop_sandbox_instance_evicts_only_that_pools_handles(self) -> None:
        spec = _spec()
        ctx_a = RunContext(agent_name="m", session_id="sess-1", sandbox_instance_id="sbx-a")
        ctx_b = RunContext(agent_name="d", session_id="sess-1", sandbox_instance_id="sbx-b")
        handle_a = await self.manager.acquire(spec, ctx_a)
        await self.manager.release(handle_a)
        handle_b = await self.manager.acquire(spec, ctx_b)
        await self.manager.release(handle_b)

        await self.manager.stop_sandbox_instance("sbx-a")

        # Pool A is gone; pool B still hands out its warm handle.
        again_b = await self.manager.acquire(spec, ctx_b)
        self.assertIs(again_b, handle_b)
        await self.manager.release(again_b)
        again_a = await self.manager.acquire(spec, ctx_a)
        self.assertIsNot(again_a, handle_a)
        await self.manager.release(again_a)
        self.assertEqual(self.spawned, ["sbx-a", "sbx-b", "sbx-a"])


if __name__ == "__main__":
    unittest.main()
