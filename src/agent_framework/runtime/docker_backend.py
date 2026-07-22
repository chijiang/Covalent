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
import os
import re
import struct
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import docker

from agent_framework.runtime.backend import BackendUnavailable, ExecResult, ExecutionBackend, HostPathWorkspace
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
_DOCKER_TIMESTAMP_RE = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(?P<fraction>\d+))?(?P<tz>Z|[+-]\d{2}:\d{2})?$"
)

# Docker stream multiplexing (tty=False): 8-byte header [type, 0, 0, 0, len_be32].
_FRAME = struct.Struct(">BxxxI")
_STREAM_STDOUT = 1
_STREAM_STDERR = 2


def _safe_name(value: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in value).strip(".-")
    return safe or "session"


def _docker_timestamp_to_unix(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or raw.startswith("0001-01-01"):
        return None
    match = _DOCKER_TIMESTAMP_RE.match(raw)
    if match is None:
        return None
    fraction = match.group("fraction")
    tz = match.group("tz") or "Z"
    normalized = match.group("base")
    if fraction:
        normalized += "." + fraction[:6].ljust(6, "0")
    normalized += "+00:00" if tz == "Z" else tz
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _recv_exact_blocking(sock, n: int) -> bytes | None:
    """Read exactly n bytes from a blocking socket; None on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


@dataclass
class SandboxMetrics:
    """Lightweight in-process counters for the Docker sandbox (no external dep).

    Exposed via ``/healthz``; ``live`` container count comes from ``len(_sessions)``,
    not a counter.
    """

    containers_started: int = 0
    containers_stopped: int = 0
    containers_swept_startup: int = 0
    unavailable_errors: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "containers_started": self.containers_started,
            "containers_stopped": self.containers_stopped,
            "containers_swept_startup": self.containers_swept_startup,
            "unavailable_errors": self.unavailable_errors,
        }


# Exception classes that mean "the Docker daemon / runtime is unreachable right
# now" rather than a config or logic error — translated to BackendUnavailable.
_UNAVAILABLE_EXC: tuple[type[BaseException], ...] = (
    docker.errors.APIError,
    docker.errors.DockerException,
    ConnectionError,
    OSError,
)


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
        self._max_sessions = settings.execution_backend_docker_max_sessions
        self._idle_timeout = settings.execution_backend_docker_idle_timeout_seconds
        self._client = docker_client  # lazily created on first use
        self._sessions: dict[str, object] = {}
        self._metrics = SandboxMetrics()
        # Per-session metadata: session_id → {agent_name, outbound, started_at}
        self._session_meta: dict[str, dict[str, object]] = {}
        self._session_semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(self._max_sessions) if self._max_sessions > 0 else None
        )

    # -- container lifecycle -------------------------------------------------

    def record_session(self, session_id: str, agent_name: str, allowed_outbound: list[str]) -> None:
        """Record per-session metadata before the container is created. Drives
        network mode (bridge if outbound) and the admin monitoring snapshot."""
        now = time.time()
        self._session_meta[session_id] = {
            "agent_name": agent_name,
            "outbound": list(allowed_outbound) if allowed_outbound else [],
            "started_at": now,
            "last_activity": now,
        }

    def agent_outbound(self, session_id: str) -> list[str]:
        return self._session_meta.get(session_id, {}).get("outbound", [])

    def is_session_tracked(self, session_id: str) -> bool:
        """Whether this backend is actively managing the session's container."""
        return session_id in self._sessions

    def session_idle_seconds(self, session_id: str) -> float | None:
        """Seconds since last activity for a tracked session, or None."""
        meta = self._session_meta.get(session_id)
        if meta is None:
            return None
        return time.time() - float(meta.get("last_activity", meta.get("started_at", time.time())))

    def _api(self):
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def _translate_unavailable(self, fn, *args, **kwargs):
        """Run a blocking docker call; translate daemon-unreachable errors to
        ``BackendUnavailable`` (counted) so callers get a clean, typed failure."""
        try:
            return fn(*args, **kwargs)
        except _UNAVAILABLE_EXC as exc:
            self._metrics.unavailable_errors += 1
            raise BackendUnavailable(f"sandbox backend unavailable: {exc}", cause=exc) from exc

    def metrics_snapshot(self) -> dict[str, object]:
        """Sandbox status for ``/healthz``: live container count + counters."""
        return {"backend": self.name, "live_containers": len(self._sessions), **self._metrics.to_dict()}

    async def sandbox_snapshot(self) -> dict[str, object]:
        """Admin monitoring snapshot: per-session live status + config + metrics."""
        sessions: list[dict[str, object]] = []
        now = time.time()
        for sid, container in list(self._sessions.items()):
            meta = self._session_meta.get(sid, {})
            outbound = list(meta.get("outbound", []) or [])
            alive = await self.is_alive(sid)
            sessions.append(await asyncio.to_thread(self._session_snapshot, sid, container, meta, outbound, alive, now))
        return {
            "backend": self.name,
            "supported": True,
            "snapshot_at": now,
            "live": len(self._sessions),
            "metrics": self._metrics.to_dict(),
            "config": {
                "image": self._image,
                "mem_limit": self._mem_limit,
                "pids_limit": self._pids_limit,
                "cpus": self._nano_cpus / 1e9,
                "network": self._network_mode,
                "tmpfs_size": self._tmpfs_size,
                "reaper_interval_seconds": self._settings.execution_backend_docker_reaper_interval_seconds,
                "max_sessions": self._max_sessions,
                "idle_timeout_seconds": self._idle_timeout,
                "shell_tool_enabled": getattr(self._settings, "execution_backend_shell_tool_enabled", False),
            },
            "sessions": sessions,
        }

    def _session_snapshot(
        self,
        session_id: str,
        container,
        meta: dict[str, object],
        outbound: list[str],
        alive: bool,
        now: float,
    ) -> dict[str, object]:
        attrs = self._container_attrs(container)
        state = attrs.get("State") if isinstance(attrs.get("State"), dict) else {}
        config = attrs.get("Config") if isinstance(attrs.get("Config"), dict) else {}
        host_config = attrs.get("HostConfig") if isinstance(attrs.get("HostConfig"), dict) else {}
        raw_started_at = state.get("StartedAt") if isinstance(state, dict) else None
        raw_created_at = attrs.get("Created")
        started_at = _docker_timestamp_to_unix(raw_started_at) or self._float_or_none(meta.get("started_at"))
        created_at = _docker_timestamp_to_unix(raw_created_at)
        last_activity_at = self._float_or_none(meta.get("last_activity"))
        network_mode = str(host_config.get("NetworkMode") or ("bridge" if outbound else self._network_mode))
        network_policy = "allowlist" if outbound else "disabled" if network_mode == "none" else "custom"
        return {
            "session_id": session_id,
            "agent_name": str(meta.get("agent_name") or ""),
            "container_id": str(getattr(container, "id", "") or ""),
            "container_name": str(getattr(container, "name", "") or ""),
            "container_created_at": created_at,
            "image_id": str(attrs.get("Image") or ""),
            "image_name": str(config.get("Image") or self._image),
            "started_at": started_at or created_at,
            "last_activity_at": last_activity_at,
            "idle_seconds": max(0.0, now - last_activity_at) if last_activity_at else None,
            "status": "running" if alive else str(getattr(container, "status", "") or "stopped"),
            "exit_code": state.get("ExitCode") if isinstance(state, dict) else None,
            "error": state.get("Error") if isinstance(state, dict) else None,
            "network_mode": network_mode,
            "network_policy": network_policy,
            "allowed_outbound": outbound,
            "resources": self._container_resource_snapshot(container),
        }

    def _container_attrs(self, container) -> dict[str, object]:
        attrs = getattr(container, "attrs", None)
        return attrs if isinstance(attrs, dict) else {}

    def _container_resource_snapshot(self, container) -> dict[str, object]:
        resources: dict[str, object] = {
            "cpu_limit": self._nano_cpus / 1e9,
            "memory_limit_config": self._mem_limit,
            "pids_limit": self._pids_limit,
            "tmpfs_size": self._tmpfs_size,
        }
        stats_fn = getattr(container, "stats", None)
        if not callable(stats_fn):
            return resources
        try:
            stats = stats_fn(stream=False)
        except Exception as exc:
            resources["usage_error"] = str(exc)
            return resources
        if not isinstance(stats, dict):
            return resources

        memory_stats = stats.get("memory_stats") if isinstance(stats.get("memory_stats"), dict) else {}
        memory_usage = self._int_or_none(memory_stats.get("usage"))
        memory_limit = self._int_or_none(memory_stats.get("limit"))
        if memory_usage is not None:
            resources["memory_usage_bytes"] = memory_usage
        if memory_limit is not None:
            resources["memory_limit_bytes"] = memory_limit
        if memory_usage is not None and memory_limit:
            resources["memory_percent"] = (memory_usage / memory_limit) * 100

        pids_stats = stats.get("pids_stats") if isinstance(stats.get("pids_stats"), dict) else {}
        pids_current = self._int_or_none(pids_stats.get("current"))
        if pids_current is not None:
            resources["pids_current"] = pids_current

        cpu_percent = self._cpu_percent(stats)
        if cpu_percent is not None:
            resources["cpu_percent"] = cpu_percent
        return resources

    @staticmethod
    def _cpu_percent(stats: dict[str, object]) -> float | None:
        cpu_stats = stats.get("cpu_stats") if isinstance(stats.get("cpu_stats"), dict) else {}
        precpu_stats = stats.get("precpu_stats") if isinstance(stats.get("precpu_stats"), dict) else {}
        cpu_usage = cpu_stats.get("cpu_usage") if isinstance(cpu_stats.get("cpu_usage"), dict) else {}
        precpu_usage = precpu_stats.get("cpu_usage") if isinstance(precpu_stats.get("cpu_usage"), dict) else {}
        total_usage = DockerBackend._int_or_none(cpu_usage.get("total_usage"))
        prev_total_usage = DockerBackend._int_or_none(precpu_usage.get("total_usage"))
        system_usage = DockerBackend._int_or_none(cpu_stats.get("system_cpu_usage"))
        prev_system_usage = DockerBackend._int_or_none(precpu_stats.get("system_cpu_usage"))
        if None in {total_usage, prev_total_usage, system_usage, prev_system_usage}:
            return None
        cpu_delta = total_usage - prev_total_usage
        system_delta = system_usage - prev_system_usage
        if cpu_delta <= 0 or system_delta <= 0:
            return None
        online_cpus = DockerBackend._int_or_none(cpu_stats.get("online_cpus"))
        if online_cpus is None:
            percpu = cpu_usage.get("percpu_usage")
            online_cpus = len(percpu) if isinstance(percpu, list) and percpu else 1
        return (cpu_delta / system_delta) * online_cpus * 100

    @staticmethod
    def _int_or_none(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _float_or_none(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def ensure(self, session_id: str):
        existing = self._sessions.get(session_id)
        if existing is not None:
            self._touch_activity(session_id)
            return existing
        # New session — queue if at capacity (asyncio.Semaphore blocks until a slot frees).
        if self._session_semaphore is not None:
            await self._session_semaphore.acquire()
        try:
            container = await asyncio.to_thread(
                self._translate_unavailable, self._create_session_container, session_id
            )
        except BaseException:
            if self._session_semaphore is not None:
                self._session_semaphore.release()
            raise
        self._sessions[session_id] = container
        self._metrics.containers_started += 1
        self._touch_activity(session_id)
        return container

    def _touch_activity(self, session_id: str) -> None:
        """Update last_activity timestamp for a tracked session."""
        meta = self._session_meta.get(session_id)
        if meta is not None:
            meta["last_activity"] = time.time()

    def workspace(self, session_id: str | None) -> HostPathWorkspace:
        # The session workspace is bind-mounted into the container at this host
        # path, so host-side pathlib sees the same files the container writes.
        if isinstance(session_id, str) and session_id.strip():
            return HostPathWorkspace(host_path=self._settings.session_workspace_dir(session_id))
        return HostPathWorkspace(host_path=self._settings.workspace_root())

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
            network_mode="bridge" if self._session_meta.get(session_id, {}).get("outbound") else self._network_mode,
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
            # Absolute, matching resolved_entry_point()/resolved_working_dir() (both
            # os.path.abspath) so the bind-mounted path equals what the runner imports.
            # Docker rejects relative bind paths.
            host_path = os.path.abspath(str(Path(str(raw)).expanduser()))
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
        self._session_meta.pop(session_id, None)
        if container is not None:
            await asyncio.to_thread(self._remove_container, container)
            self._metrics.containers_stopped += 1
            if self._session_semaphore is not None:
                self._session_semaphore.release()
            return
        await asyncio.to_thread(self._remove_container_by_name, session_id)

    async def aclose(self) -> None:
        containers = list(self._sessions.values())
        self._sessions.clear()
        for container in containers:
            await asyncio.to_thread(self._remove_container, container)
        if containers:
            self._metrics.containers_stopped += len(containers)

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
        if containers:
            self._metrics.containers_swept_startup += len(containers)

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
    def rewrite_command(self, command: list[str]) -> list[str]:
        """The command as it will actually execute inside the container:
        host ``sys.executable`` -> ``python``, host runners dir -> ``/runners/``."""
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
        rewritten = self.rewrite_command(command)
        exec_id, sock = await asyncio.to_thread(
            self._translate_unavailable,
            self._start_exec_socket,
            container.id,
            rewritten,
            str(cwd) if cwd else None,
            self._sanitize_env(env),
        )
        real_sock = getattr(sock, "_sock", sock)  # unwrap SocketIO -> raw socket

        def exit_probe() -> int | None:
            return self._exec_exit_code(exec_id)

        def kill_probe(signal_name: str) -> None:
            self._kill_exec(container, exec_id, signal_name)

        return DockerExecProcess(real_sock, exit_code_probe=exit_probe, kill_probe=kill_probe)

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

    def _kill_exec(self, container, exec_id: str, signal_name: str) -> None:
        """Best-effort: signal the exec process inside the container via its PID.

        ``exec_inspect`` returns the exec process's PID in the container namespace;
        ``kill`` is run inside the same container so the namespace matches. Called
        from ``DockerExecProcess.terminate/kill`` (sync, rare) — a brief blocking
        call, acceptable for a kill.
        """
        try:
            info = self._api().api.exec_inspect(exec_id)
        except Exception:
            return
        pid = info.get("Pid") if isinstance(info, dict) else None
        if not isinstance(pid, int) or pid <= 0:
            return
        try:
            container.exec_run(["kill", f"-{signal_name}", str(pid)])
        except Exception:
            pass

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
        rewritten = self.rewrite_command(command)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._translate_unavailable,
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
