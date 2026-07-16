"""Docker execution backend — runs skill runners/scripts inside a per-session
container, exec'd over a hijacked socket (see :mod:`docker_process`).

Per-session container; bind-mount skill source dirs + the session workspace at
their host-absolute paths (so host-absolute entry points / working dirs resolve
unchanged); rewrite the two host-only command tokens that don't exist in the
container (``sys.executable`` → ``python``, the host runners directory →
``/runners/``). Every Docker SDK call is blocking, so each is wrapped in
:func:`asyncio.to_thread` to keep the event loop responsive.

Phase 1b hardening: resource ceilings (mem/pids/cpu) + ``tmpfs`` for ``/tmp`` +
network isolation (``network_mode`` default ``none``); per-session teardown via
``stop`` (called from the session DELETE handler); ``startup_sweep`` reclaims
orphan containers from previous runs; ``list_sandbox_sessions`` feeds the
lifespan reaper; one-shot ``exec`` (with stdin) over a hijacked socket.

Deferred: sandbox image CI; in-container ``kill`` for hung execs (the
``DockerExecProcess`` socket-close fallback remains); egress allow-list /
restricted-bridge proxy (network is ``none`` or permissive ``bridge`` for now).
"""

from __future__ import annotations

import asyncio
import logging
import struct
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from agent_framework.runtime.backend import ExecResult, ExecutionBackend
from agent_framework.runtime.docker_process import DockerExecProcess

if TYPE_CHECKING:
    from agent_framework.infra.settings import AppSettings

logger = logging.getLogger(__name__)

_RUNNERS_HOST_DIR = Path(__file__).resolve().parent.parent / "skills" / "runners"
_RUNNERS_CONTAINER_DIR = "/runners"
_HOST_PYTHON = sys.executable
# Env vars that would leak host-specific paths into the container; the image's
# own environment provides correct values for these.
_HOST_ENV_DROP = {"PATH", "PYTHONPATH", "PYTHONHOME"}
_SANDBOX_LABEL = "covalent.sandbox"
_SESSION_LABEL = "covalent.session"

# Docker stream multiplexing (tty=False): 8-byte header [type, 0, 0, 0, len_be32].
_FRAME = struct.Struct(">BxxxI")
_STREAM_STDOUT = 1
_STREAM_STDERR = 2


def _safe_name(value: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in value).strip(".-")
    return safe or "session"


def _recv_exact_blocking(sock, n: int) -> bytes | None:
    """Read exactly n bytes from a blocking socket; None on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


class DockerBackend(ExecutionBackend):
    """Run skill runners and scripts inside one container per session."""

    name = "docker"

    def __init__(
        self,
        settings: "AppSettings",
        skill_source_dirs_provider: Callable[[], Sequence[str]],
        docker_client=None,
    ) -> None:
        self._settings = settings
        self._skill_source_dirs_provider = skill_source_dirs_provider
        self._image = settings.execution_backend_docker_image
        self._mem_limit = settings.execution_backend_docker_mem_limit
        self._pids_limit = settings.execution_backend_docker_pids_limit
        self._nano_cpus = int(settings.execution_backend_docker_cpus * 1e9)
        self._network_mode = settings.execution_backend_docker_network
        self._tmpfs_size = settings.execution_backend_docker_tmpfs_size
        self._client = docker_client  # lazily created on first use
        self._sessions: dict[str, object] = {}

    # -- container lifecycle -------------------------------------------------
    def _api(self):
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    async def ensure(self, session_id: str):
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        container = await asyncio.to_thread(self._create_session_container, session_id)
        self._sessions[session_id] = container
        return container

    def _create_session_container(self, session_id: str):
        client = self._api()
        volumes = self._build_volumes(session_id)
        name = f"covalent-sandbox-{_safe_name(session_id)}"
        kwargs = dict(
            image=self._image,
            command=["tail", "-f", "/dev/null"],
            volumes=volumes,
            detach=True,
            labels={_SANDBOX_LABEL: "1", _SESSION_LABEL: session_id},
            name=name,
            mem_limit=self._mem_limit,
            pids_limit=self._pids_limit,
            nano_cpus=self._nano_cpus,
            network_mode=self._network_mode,
            tmpfs={"/tmp": f"size={self._tmpfs_size}"},
        )
        try:
            return client.containers.run(**kwargs)
        except Exception:
            # A stale container with the same name (previous run) — remove and retry once.
            try:
                client.containers.get(name).remove(force=True)
            except Exception:
                pass
            return client.containers.run(**kwargs)

    def _build_volumes(self, session_id: str) -> dict[str, dict[str, str]]:
        volumes: dict[str, dict[str, str]] = {}
        workspace = self._settings.session_workspace_dir(session_id)
        workspace.mkdir(parents=True, exist_ok=True)
        workspace_path = str(workspace)
        volumes[workspace_path] = {"bind": workspace_path, "mode": "rw"}
        try:
            source_dirs = list(self._skill_source_dirs_provider() or [])
        except Exception:
            source_dirs = []
        for raw in source_dirs:
            host_path = str(Path(str(raw)).expanduser())
            volumes[host_path] = {"bind": host_path, "mode": "rw"}
        return volumes

    async def is_alive(self, session_id: str) -> bool:
        container = self._sessions.get(session_id)
        if container is None:
            return False
        try:
            await asyncio.to_thread(container.reload)
            return container.status == "running"
        except Exception:
            return False

    async def stop(self, session_id: str) -> None:
        """Stop+remove the session's container. Robust to untracked containers
        (e.g. created by a previous process) by falling back to a name lookup."""
        container = self._sessions.pop(session_id, None)
        if container is not None:
            await asyncio.to_thread(self._remove_container, container)
            return
        await asyncio.to_thread(self._remove_container_by_name, session_id)

    async def aclose(self) -> None:
        containers = list(self._sessions.values())
        self._sessions.clear()
        for container in containers:
            await asyncio.to_thread(self._remove_container, container)

    def _remove_container(self, container) -> None:
        try:
            container.stop(timeout=5)
        except Exception:
            pass
        try:
            container.remove(force=True)
        except Exception:
            pass

    def _remove_container_by_name(self, session_id: str) -> None:
        name = f"covalent-sandbox-{_safe_name(session_id)}"
        try:
            container = self._api().containers.get(name)
        except Exception:
            return
        self._remove_container(container)

    # -- sweep / reaper support ---------------------------------------------
    def _list_sandbox_containers(self) -> list:
        try:
            return list(self._api().containers.list(all=True, filters={"label": [f"{_SANDBOX_LABEL}=1"]}))
        except Exception:
            return []

    async def startup_sweep(self) -> None:
        """Remove all covalent sandbox containers — orphans from a previous run."""
        containers = await asyncio.to_thread(self._list_sandbox_containers)
        for container in containers:
            await asyncio.to_thread(self._remove_container, container)

    async def list_sandbox_sessions(self) -> list[str]:
        """Session ids of sandbox containers known to the daemon (across restarts)."""
        containers = await asyncio.to_thread(self._list_sandbox_containers)
        sessions: list[str] = []
        for container in containers:
            sid = (container.labels or {}).get(_SESSION_LABEL)
            if sid:
                sessions.append(sid)
        return sessions

    # -- execution -----------------------------------------------------------
    def _rewrite_command(self, command: list[str]) -> list[str]:
        runners_host = str(_RUNNERS_HOST_DIR)
        rewritten: list[str] = []
        for arg in command:
            if arg and arg == _HOST_PYTHON:
                rewritten.append("python")
            elif arg.startswith(runners_host):
                rel = arg[len(runners_host):].lstrip("/")
                rewritten.append(f"{_RUNNERS_CONTAINER_DIR}/{rel}")
            else:
                rewritten.append(arg)
        return rewritten

    @staticmethod
    def _sanitize_env(env: dict[str, str] | None) -> dict[str, str]:
        if not env:
            return {}
        return {k: v for k, v in env.items() if k not in _HOST_ENV_DROP}

    async def spawn_stream(
        self,
        command: list[str],
        *,
        cwd: str | Path | None,
        env: dict[str, str],
        session_id: str | None = None,
    ):
        if not session_id:
            raise ValueError("DockerBackend.spawn_stream requires a session_id")
        container = await self.ensure(session_id)
        rewritten = self._rewrite_command(command)
        exec_id, sock = await asyncio.to_thread(
            self._start_exec_socket, container.id, rewritten, str(cwd) if cwd else None, self._sanitize_env(env)
        )
        real_sock = getattr(sock, "_sock", sock)  # unwrap SocketIO -> raw socket

        def probe() -> int | None:
            return self._exec_exit_code(exec_id)

        return DockerExecProcess(real_sock, exit_code_probe=probe)

    def _start_exec_socket(self, container_id, command, workdir, env):
        api = self._api().api
        exec_id = api.exec_create(
            container_id,
            cmd=command,
            stdin=True,
            stdout=True,
            stderr=True,
            tty=False,
            environment=env or None,
            workdir=workdir,
        )["Id"]
        sock = api.exec_start(exec_id, socket=True)
        return exec_id, sock

    def _exec_exit_code(self, exec_id: str) -> int | None:
        try:
            info = self._api().api.exec_inspect(exec_id)
        except Exception:
            return None
        code = info.get("ExitCode")
        if isinstance(code, int) and code >= 0:
            return code
        return None

    async def exec(
        self,
        command: list[str],
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        session_id: str | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        if not session_id:
            raise ValueError("DockerBackend.exec requires a session_id")
        container = await self.ensure(session_id)
        rewritten = self._rewrite_command(command)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._exec_one_shot,
                    container.id,
                    rewritten,
                    str(cwd) if cwd else None,
                    self._sanitize_env(env),
                    stdin,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return ExecResult(exit_code=124, stdout=b"", stderr=f"timed out after {timeout}s".encode())

    def _exec_one_shot(
        self, container_id, command, workdir, env, stdin_bytes
    ) -> ExecResult:
        """One-shot exec over a hijacked socket (supports stdin), blocking.

        Run via ``asyncio.to_thread``. Reuses the Docker framed-stream demux to
        separate stdout/stderr; resolves the exit code via ``exec_inspect``.
        """
        api = self._api().api
        exec_id = api.exec_create(
            container_id,
            cmd=command,
            stdin=stdin_bytes is not None,
            stdout=True,
            stderr=True,
            tty=False,
            environment=env or None,
            workdir=workdir,
        )["Id"]
        sock = api.exec_start(exec_id, socket=True)
        real = getattr(sock, "_sock", sock)
        real.setblocking(True)
        out = bytearray()
        err = bytearray()
        try:
            if stdin_bytes:
                try:
                    real.sendall(stdin_bytes)
                    real.shutdown(1)  # signal EOF on the write half
                except OSError:
                    pass
            while True:
                header = _recv_exact_blocking(real, 8)
                if not header:
                    break
                stream_type, length = _FRAME.unpack(header)
                payload = _recv_exact_blocking(real, length) if length else b""
                if payload is None:
                    break
                if stream_type == _STREAM_STDOUT:
                    out.extend(payload)
                elif stream_type == _STREAM_STDERR:
                    err.extend(payload)
        finally:
            try:
                real.close()
            except OSError:
                pass
        code = self._exec_exit_code(exec_id)
        return ExecResult(
            exit_code=code if code is not None else -1,
            stdout=bytes(out),
            stderr=bytes(err),
        )
