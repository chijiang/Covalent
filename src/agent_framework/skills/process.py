from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any

from agent_framework.skills.exceptions import SkillProcessError, SkillStartupError
from agent_framework.skills.permissions import PermissionChecker
from agent_framework.skills.protocol import JsonRpcResponse
from agent_framework.skills.spec import ManifestSkillSpec

logger = logging.getLogger(__name__)


class SkillProcessHandle:
    """Wraps a single skill subprocess and handles JSON-RPC over stdio."""

    def __init__(self, spec: ManifestSkillSpec, process: asyncio.subprocess.Process) -> None:
        self.spec = spec
        self.process = process
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[JsonRpcResponse]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._last_activity: float = time.monotonic()
        self._health_failures: int = 0
        self._ready = asyncio.Event()
        self._busy = False

    @property
    def is_alive(self) -> bool:
        return self.process.returncode is None

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def is_available(self) -> bool:
        return self.is_alive and self.is_ready and not self._busy

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity

    async def send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._request_id += 1
        msg_id = self._request_id
        message = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        future: asyncio.Future[JsonRpcResponse] = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future

        payload = json.dumps(message, ensure_ascii=False) + "\n"
        assert self.process.stdin is not None
        self.process.stdin.write(payload.encode("utf-8"))
        await self.process.stdin.drain()
        self._last_activity = time.monotonic()

        timeout = self.spec.process.max_request_timeout_seconds
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise SkillProcessError(
                {"code": -32002, "message": f"Request to skill '{self.spec.name}' timed out after {timeout}s"}
            )

        if response.error is not None:
            raise SkillProcessError(response.error.model_dump())
        return response.result

    async def _read_loop(self) -> None:
        assert self.process.stdout is not None
        while self.process.returncode is None:
            try:
                line = await self.process.stdout.readline()
            except asyncio.CancelledError:
                return
            if not line:
                break
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            await self._dispatch(data)

    async def _dispatch(self, data: dict[str, Any]) -> None:
        if "id" in data and "method" not in data:
            # This is a response to a pending request
            msg_id = data["id"]
            future = self._pending.pop(msg_id, None)
            if future and not future.done():
                try:
                    response = JsonRpcResponse.model_validate(data)
                    future.set_result(response)
                except Exception as exc:
                    future.set_exception(exc)
        elif "method" in data and "id" not in data:
            # Notification from skill
            method = data.get("method", "")
            if method == "ready":
                self._ready.set()
                logger.info("Skill '%s' process is ready", self.spec.name)
            elif method == "log":
                params = data.get("params", {})
                level = params.get("level", "info")
                message = params.get("message", "")
                log_method = getattr(logger, level, logger.info)
                log_method("[skill:%s] %s", self.spec.name, message)


class SkillProcessManager:
    """Manages per-skill process pools with health checking, idle eviction,
    and concurrency control via semaphores and busy flags."""

    def __init__(self) -> None:
        self._pools: dict[str, list[SkillProcessHandle]] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._health_task: asyncio.Task[None] | None = None
        self._checker = PermissionChecker()

    async def start(self) -> None:
        self._health_task = asyncio.create_task(self._health_check_loop())

    async def stop(self) -> None:
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
        for pool in self._pools.values():
            for handle in pool:
                await self._terminate(handle)
        self._pools.clear()
        self._semaphores.clear()

    async def acquire(self, spec: ManifestSkillSpec) -> SkillProcessHandle:
        """Acquire a process slot. Blocks if all instances are busy, up to max_instances."""
        if not spec.is_executable:
            raise SkillProcessError({"code": -32002, "message": f"Skill '{spec.name}' is not executable"})
        sem = self._semaphores.get(spec.name)
        if sem is None:
            sem = asyncio.Semaphore(spec.process.max_instances)
            self._semaphores[spec.name] = sem

        await sem.acquire()
        try:
            handle = await self._get_or_spawn(spec)
            handle._busy = True
            return handle
        except Exception:
            sem.release()
            raise

    async def release(self, handle: SkillProcessHandle) -> None:
        """Release a process back to the pool."""
        handle._busy = False
        handle._last_activity = time.monotonic()
        sem = self._semaphores.get(handle.spec.name)
        if sem:
            sem.release()

    def pool_status(self, skill_name: str) -> dict[str, Any]:
        pool = self._pools.get(skill_name, [])
        alive = sum(1 for h in pool if h.is_alive)
        ready = sum(1 for h in pool if h.is_alive and h.is_ready)
        busy = sum(1 for h in pool if h._busy)
        return {"total": len(pool), "alive": alive, "ready": ready, "busy": busy}

    async def stop_skill(self, skill_name: str) -> None:
        pool = self._pools.pop(skill_name, [])
        self._semaphores.pop(skill_name, None)
        for handle in pool:
            await self._terminate(handle)

    async def _get_or_spawn(self, spec: ManifestSkillSpec) -> SkillProcessHandle:
        """Find an available process or spawn a new one."""
        pool = self._pools.setdefault(spec.name, [])
        # Try to find an idle, ready process
        for handle in pool:
            if handle.is_available:
                return handle
        # Spawn a new one (semaphore already limits concurrency)
        handle = await self._spawn(spec)
        pool.append(handle)
        return handle

    async def _spawn(self, spec: ManifestSkillSpec) -> SkillProcessHandle:
        assert spec.runtime is not None
        command = spec.runtime.command or self._default_command(spec.runtime.type)
        extra_args: list[str] = []
        if spec.runtime.type == "python" and not spec.runtime.command:
            extra_args = ["-u"]  # unbuffered stdout for proper pipe reads
        env = self._build_env(spec)
        full_args = self._build_command(spec, command, extra_args, env)

        process = await asyncio.create_subprocess_exec(
            *full_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=spec.resolved_working_dir(),
            env=env,
        )
        handle = SkillProcessHandle(spec=spec, process=process)
        handle._reader_task = asyncio.create_task(handle._read_loop())
        asyncio.create_task(self._stderr_logger(spec.name, process))

        try:
            await asyncio.wait_for(
                handle._ready.wait(),
                timeout=spec.process.startup_timeout_seconds,
            )
        except asyncio.TimeoutError:
            await self._terminate(handle)
            raise SkillStartupError(
                f"Skill '{spec.name}' did not become ready within {spec.process.startup_timeout_seconds}s"
            )

        return handle

    def _default_command(self, runtime_type: str) -> str:
        if runtime_type == "python":
            return sys.executable
        if runtime_type == "nodejs":
            return "node"
        raise ValueError(f"Unsupported runtime type: {runtime_type}")

    def _build_env(self, spec: ManifestSkillSpec) -> dict[str, str]:
        host_env = dict(os.environ)
        host_env["SKILL_DIR"] = spec.source_dir or ""
        host_env["SKILL_NAME"] = spec.name
        host_env["SKILL_RUNTIME"] = spec.runtime.type if spec.runtime else ""
        if spec.runtime:
            host_env.update(spec.runtime.env)
        env = self._checker.filter_env(spec, host_env)
        env = self._checker.inject_permission_env(spec, env)
        self._checker.log_permission_summary(spec)
        if spec.runtime and spec.runtime.protocol == "callable":
            env["AGENT_FRAMEWORK_SKILL_ENTRYPOINT"] = spec.resolved_entry_point()
            env["AGENT_FRAMEWORK_SKILL_TOOL_MAP"] = json.dumps(
                {tool.name: tool.handler or tool.name for tool in spec.tools},
                ensure_ascii=False,
            )
        return env

    def _build_command(
        self,
        spec: ManifestSkillSpec,
        command: str,
        extra_args: list[str],
        env: dict[str, str],
    ) -> list[str]:
        assert spec.runtime is not None
        if spec.runtime.protocol == "rpc":
            return [command] + extra_args + spec.runtime.args + [spec.resolved_entry_point()]
        runner = self._runner_path(spec.runtime.type)
        if spec.runtime.type == "python":
            return [command] + extra_args + [runner] + spec.runtime.args
        return [command, runner] + spec.runtime.args

    def _runner_path(self, runtime_type: str) -> str:
        base_dir = Path(__file__).resolve().parent / "runners"
        if runtime_type == "python":
            return str(base_dir / "python_runner.py")
        if runtime_type == "nodejs":
            return str(base_dir / "node_runner.js")
        raise ValueError(f"Unsupported runtime type: {runtime_type}")

    async def _health_check_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30.0)
                for skill_name, pool in list(self._pools.items()):
                    for handle in pool[:]:
                        if not handle.is_alive:
                            pool.remove(handle)
                            continue
                        if handle.idle_seconds > handle.spec.process.idle_timeout_seconds:
                            logger.info("Evicting idle skill process for '%s'", skill_name)
                            await self._terminate(handle)
                            pool.remove(handle)
                            continue
                        # Only ping processes that aren't currently serving a request
                        if handle._busy:
                            continue
                        try:
                            await asyncio.wait_for(
                                handle.send_request("ping"),
                                timeout=5.0,
                            )
                            handle._health_failures = 0
                        except Exception:
                            handle._health_failures += 1
                            if handle._health_failures >= handle.spec.health_check.max_failures:
                                logger.warning(
                                    "Skill '%s' process failed %d health checks, restarting",
                                    skill_name,
                                    handle._health_failures,
                                )
                                await self._terminate(handle)
                                pool.remove(handle)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Health check loop error: %s", exc)

    async def _terminate(self, handle: SkillProcessHandle) -> None:
        if handle.process.returncode is not None:
            return
        try:
            await handle.send_request("shutdown")
        except Exception:
            pass
        if handle._reader_task:
            handle._reader_task.cancel()
        if handle.process.returncode is not None:
            return
        try:
            handle.process.terminate()
            await asyncio.wait_for(handle.process.wait(), timeout=5.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                handle.process.kill()
                await handle.process.wait()
            except ProcessLookupError:
                pass

    @staticmethod
    async def _stderr_logger(skill_name: str, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        while True:
            try:
                line = await process.stderr.readline()
            except asyncio.CancelledError:
                return
            if not line:
                break
            logger.debug(
                "[skill:%s:stderr] %s", skill_name, line.decode("utf-8", errors="replace").rstrip()
            )
