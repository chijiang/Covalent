"""Docker execution backend — runs skill runners/scripts inside per-sandbox-instance
containers, exec'd over a hijacked socket (see :mod:`docker_process`).

One container per logical sandbox instance (an (execution scope, agent) pair,
see ``SandboxBinding``); sibling instances of one session bind-mount the same
session workspace (collaboration) while keeping private process trees, HOME,
caches, and ``/tmp``. Skill source dirs + the session workspace are mounted at
their host-absolute paths (so host-absolute entry points / working dirs resolve
unchanged); the two host-only command tokens that don't exist in the container
are rewritten (``sys.executable`` → ``python``, the host runners directory →
``/runners/``). Every Docker SDK call is blocking, so each is wrapped in
:func:`asyncio.to_thread` to keep the event loop responsive.

Hardening: resource ceilings (mem/pids/cpu, per-instance from the pinned spec)
+ ``tmpfs`` for ``/tmp`` + network isolation (``network_mode`` default ``none``,
bridge only when the binding's outbound policy is non-empty); teardown via
``stop_instance``/``stop_scope``/``stop`` (session compatibility);
``startup_sweep`` reclaims orphan containers; ``list_sandbox_sessions`` feeds
the lifespan reaper; one-shot ``exec`` (with stdin) over a hijacked socket.

Legacy callers that only pass ``session_id`` keep working: the session id is
used as the instance key with a settings-derived default spec, exactly the
pre-instance behavior.

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

from covalent.runtime.backend import (
    BackendUnavailable,
    ExecResult,
    ExecutionBackend,
    ExecutionTarget,
    HostPathWorkspace,
    SandboxBinding,
    SandboxSpec,
)
from covalent.runtime.docker_process import DockerExecProcess

if TYPE_CHECKING:
    from covalent.infra.settings import AppSettings

logger = logging.getLogger(__name__)

_RUNNERS_HOST_DIR = Path(__file__).resolve().parent.parent / "skills" / "runners"
_RUNNERS_CONTAINER_DIR = "/runners"
_HOST_PYTHON = sys.executable
# Env vars that would leak host-specific paths into the container; the image's
# own environment provides correct values for these.
_HOST_ENV_DROP = {"PATH", "PYTHONPATH", "PYTHONHOME"}
_SANDBOX_LABEL = "covalent.sandbox"
_SESSION_LABEL = "covalent.session"
_EXECUTION_SCOPE_LABEL = "covalent.execution-scope"
_INSTANCE_LABEL = "covalent.sandbox-instance"
_AGENT_LABEL = "covalent.agent"
_PROFILE_LABEL = "covalent.sandbox-profile"
_PROFILE_REVISION_LABEL = "covalent.sandbox-profile-revision"
_CONTAINER_HOME = "/home/covalent"
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
    """Run skill runners and scripts inside one container per sandbox instance."""

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
        max_instances = (
            settings.execution_backend_docker_max_instances
            or settings.execution_backend_docker_max_sessions
        )
        self._max_instances = max_instances
        self._idle_timeout = settings.execution_backend_docker_idle_timeout_seconds
        self._client = docker_client  # lazily created on first use
        # sandbox_instance_id -> live container
        self._containers: dict[str, object] = {}
        # sandbox_instance_id -> registered binding (spec + outbound policy)
        self._bindings: dict[str, SandboxBinding] = {}
        # Per-instance creation lock: concurrent tools from one agent must not
        # create duplicate containers (or trip the stale-name retry wrongly).
        self._instance_locks: dict[str, asyncio.Lock] = {}
        # sandbox_instance_id -> {agent_name, outbound, session_id, started_at, ...}
        self._instance_meta: dict[str, dict[str, object]] = {}
        self._metrics = SandboxMetrics()
        self._capacity_semaphore: asyncio.Semaphore | None = (
            asyncio.Semaphore(self._max_instances) if self._max_instances > 0 else None
        )

    # -- binding registration -------------------------------------------------

    def _default_spec(self) -> SandboxSpec:
        """Settings-derived spec used by legacy session-keyed callers."""
        return SandboxSpec(
            profile_id="default",
            profile_revision=1,
            image=self._image,
            keepalive_command=("tail", "-f", "/dev/null"),
            runtime_capabilities=frozenset({"python", "shell"}),
            contract_version=1,
            memory_limit=self._mem_limit,
            pids_limit=self._pids_limit,
            cpus=self._nano_cpus / 1e9,
            tmpfs_size=self._tmpfs_size,
        )

    def configure(self, binding: SandboxBinding) -> None:
        """Register a logical sandbox binding. No container is created here —
        the first ``ensure``/``exec``/``spawn_stream`` for the instance creates
        it from the pinned spec.

        If the outbound policy flips the network mode while a container is
        live, the instance is marked for recreation on its next ``ensure``.
        """
        now = time.time()
        instance_id = binding.target.sandbox_instance_id
        existing_meta = self._instance_meta.get(instance_id, {})
        old_outbound = existing_meta.get("outbound", []) or []
        new_outbound = list(binding.allowed_outbound) if binding.allowed_outbound else []
        needs_recreate = (
            bool(old_outbound) != bool(new_outbound) and instance_id in self._containers
        )
        self._bindings[instance_id] = binding
        self._instance_meta[instance_id] = {
            "agent_name": binding.target.agent_name,
            "session_id": binding.target.session_id,
            "execution_scope_id": binding.target.execution_scope_id,
            "profile_id": binding.spec.profile_id,
            "profile_revision": binding.spec.profile_revision,
            "outbound": new_outbound,
            "started_at": existing_meta.get("started_at", now),
            "last_activity": now,
        }
        if needs_recreate:
            self._instance_meta[instance_id]["needs_recreate"] = True

    def record_session(self, session_id: str, agent_name: str, allowed_outbound: list[str]) -> None:
        """Compatibility: register a legacy session-keyed binding."""
        self.configure(
            SandboxBinding(
                target=ExecutionTarget(
                    execution_scope_id=session_id,
                    session_id=session_id,
                    workspace_scope_id=session_id,
                    sandbox_instance_id=session_id,
                    agent_name=agent_name,
                ),
                spec=self._default_spec(),
                allowed_outbound=tuple(allowed_outbound or []),
            )
        )

    def _binding_for(self, sandbox_instance_id: str, session_id: str | None = None) -> SandboxBinding:
        binding = self._bindings.get(sandbox_instance_id)
        if binding is not None:
            return binding
        # Legacy/unregistered: synthesize the settings-default binding so the
        # pre-instance behavior (one container per session id) still works.
        scope = session_id or sandbox_instance_id
        meta = self._instance_meta.get(sandbox_instance_id, {})
        return SandboxBinding(
            target=ExecutionTarget(
                execution_scope_id=scope,
                session_id=scope,
                workspace_scope_id=scope,
                sandbox_instance_id=sandbox_instance_id,
                agent_name=str(meta.get("agent_name") or ""),
            ),
            spec=self._default_spec(),
            allowed_outbound=tuple(meta.get("outbound", []) or ()),
        )

    def agent_outbound(self, sandbox_instance_id: str) -> list[str]:
        """Outbound patterns registered for an instance (legacy callers pass a
        session id, which was the instance key)."""
        return list(self._instance_meta.get(sandbox_instance_id, {}).get("outbound", []) or [])

    def is_session_tracked(self, session_id: str) -> bool:
        """Whether this backend is actively managing any instance of a session."""
        return any(
            meta.get("session_id") == session_id for meta in self._instance_meta.values()
        )

    def tracked_instance_ids(self) -> list[str]:
        return list(self._containers)

    def instance_session_id(self, sandbox_instance_id: str) -> str | None:
        meta = self._instance_meta.get(sandbox_instance_id)
        session_id = meta.get("session_id") if meta else None
        return str(session_id) if session_id else None

    def session_idle_seconds(self, session_id: str) -> float | None:
        """Seconds since the most recent activity across a session's tracked
        instances, or None when the session has none."""
        idle_values = [
            self._instance_idle_seconds(instance_id)
            for instance_id, meta in self._instance_meta.items()
            if meta.get("session_id") == session_id
        ]
        idle_values = [value for value in idle_values if value is not None]
        return min(idle_values) if idle_values else None

    def instance_idle_seconds(self, sandbox_instance_id: str) -> float | None:
        """Seconds since last activity for a tracked instance, or None."""
        return self._instance_idle_seconds(sandbox_instance_id)

    def _instance_idle_seconds(self, sandbox_instance_id: str) -> float | None:
        meta = self._instance_meta.get(sandbox_instance_id)
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
        return {"backend": self.name, "live_containers": len(self._containers), **self._metrics.to_dict()}

    async def sandbox_snapshot(self) -> dict[str, object]:
        """Admin monitoring snapshot: one flat entry per live instance (carrying
        its session/scope identity) + config + metrics."""
        instances: list[dict[str, object]] = []
        now = time.time()
        for instance_id, container in list(self._containers.items()):
            meta = self._instance_meta.get(instance_id, {})
            binding = self._bindings.get(instance_id)
            outbound = list(meta.get("outbound", []) or [])
            alive = await self.is_alive(instance_id)
            instances.append(
                await asyncio.to_thread(
                    self._instance_snapshot, instance_id, container, meta, binding, outbound, alive, now
                )
            )
        return {
            "backend": self.name,
            "supported": True,
            "snapshot_at": now,
            "live": len(self._containers),
            "metrics": self._metrics.to_dict(),
            "config": {
                "image": self._image,
                "mem_limit": self._mem_limit,
                "pids_limit": self._pids_limit,
                "cpus": self._nano_cpus / 1e9,
                "network": self._network_mode,
                "tmpfs_size": self._tmpfs_size,
                "reaper_interval_seconds": self._settings.execution_backend_docker_reaper_interval_seconds,
                "max_instances": self._max_instances,
                "idle_timeout_seconds": self._idle_timeout,
                "shell_tool_enabled": getattr(self._settings, "execution_backend_shell_tool_enabled", False),
            },
            "sessions": instances,
        }

    def _instance_snapshot(
        self,
        sandbox_instance_id: str,
        container,
        meta: dict[str, object],
        binding: SandboxBinding | None,
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
        session_id = meta.get("session_id")
        return {
            "session_id": str(session_id) if session_id else None,
            "execution_scope_id": str(meta.get("execution_scope_id") or sandbox_instance_id),
            "sandbox_instance_id": sandbox_instance_id,
            "agent_name": str(meta.get("agent_name") or ""),
            "container_id": str(getattr(container, "id", "") or ""),
            "container_name": str(getattr(container, "name", "") or ""),
            "container_created_at": created_at,
            "image_id": str(attrs.get("Image") or ""),
            "image_name": str(config.get("Image") or (binding.spec.image if binding else self._image)),
            "profile_id": str(meta.get("profile_id") or (binding.spec.profile_id if binding else "")),
            "profile_revision": meta.get("profile_revision") or (binding.spec.profile_revision if binding else None),
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

    async def ensure(self, sandbox_instance_id: str):
        """Make the instance's container ready. Per-instance lock with a
        double-check so concurrent tools from one agent never create duplicate
        containers. Queues on the capacity semaphore when at the limit.

        The recreate path (outbound flip) also runs under the lock: pop,
        removal, capacity release, and re-creation are one atomic sequence, so
        a concurrent ``ensure`` can never create a same-name container while
        the old one is still being removed."""
        existing = self._containers.get(sandbox_instance_id)
        if (
            existing is not None
            and not self._instance_meta.get(sandbox_instance_id, {}).get("needs_recreate")
        ):
            self._touch_activity(sandbox_instance_id)
            return existing

        lock = self._instance_locks.setdefault(sandbox_instance_id, asyncio.Lock())
        async with lock:
            # Double-check after acquiring: a concurrent ensure may have created
            # (or recreated) the container while we waited.
            existing = self._containers.get(sandbox_instance_id)
            if existing is not None:
                meta = self._instance_meta.get(sandbox_instance_id, {})
                if meta.get("needs_recreate"):
                    # Network mode changed — remove the old container but keep
                    # the (updated) meta so the new one gets the correct mode.
                    self._containers.pop(sandbox_instance_id, None)
                    meta.pop("needs_recreate", None)
                    await asyncio.to_thread(self._remove_container, existing)
                    self._metrics.containers_stopped += 1
                    self._release_capacity()
                    # Fall through to create a fresh container.
                else:
                    self._touch_activity(sandbox_instance_id)
                    return existing

            binding = self._binding_for(sandbox_instance_id)
            if sandbox_instance_id not in self._bindings:
                # Legacy/unregistered caller: record the synthesized binding so
                # the container is discoverable for stop/snapshot/reaper.
                self.configure(binding)
            if self._capacity_semaphore is not None:
                await self._capacity_semaphore.acquire()
            try:
                container = await asyncio.to_thread(
                    self._translate_unavailable, self._create_instance_container, binding
                )
            except BaseException:
                self._release_capacity()
                raise
            self._containers[sandbox_instance_id] = container
            self._metrics.containers_started += 1
            self._touch_activity(sandbox_instance_id)
            return container

    def _touch_activity(self, sandbox_instance_id: str) -> None:
        """Update last_activity timestamp for a tracked instance."""
        meta = self._instance_meta.get(sandbox_instance_id)
        if meta is not None:
            meta["last_activity"] = time.time()

    def _release_capacity(self) -> None:
        if self._capacity_semaphore is not None:
            self._capacity_semaphore.release()

    def workspace(self, session_id: str | None) -> HostPathWorkspace:
        # The session workspace is bind-mounted into every sibling container at
        # this host path, so host-side pathlib sees the same files the
        # containers write.
        if isinstance(session_id, str) and session_id.strip():
            return HostPathWorkspace(host_path=self._settings.session_workspace_dir(session_id))
        return HostPathWorkspace(host_path=self._settings.workspace_root())

    def _container_name(self, binding: SandboxBinding) -> str:
        scope = _safe_name(binding.target.execution_scope_id)
        short = binding.target.sandbox_instance_id[-12:]
        return f"covalent-sandbox-{scope}-{short}"

    def _instance_state_dir(self, binding: SandboxBinding) -> Path:
        """Instance-private state (HOME, caches), outside the shared session
        workspace and unreachable through workspace file tools."""
        return self._instance_state_dir_by_target(binding.target)

    def _create_instance_container(self, binding: SandboxBinding):
        client = self._api()
        spec = binding.spec
        target = binding.target
        volumes = self._build_volumes(target)
        state_dir = self._instance_state_dir(binding)
        state_dir.mkdir(parents=True, exist_ok=True)
        labels = {
            _SANDBOX_LABEL: "1",
            _EXECUTION_SCOPE_LABEL: target.execution_scope_id,
            _INSTANCE_LABEL: target.sandbox_instance_id,
            _AGENT_LABEL: target.agent_name,
            _PROFILE_LABEL: spec.profile_id,
            _PROFILE_REVISION_LABEL: str(spec.profile_revision),
        }
        if target.session_id:
            labels[_SESSION_LABEL] = target.session_id
        name = self._container_name(binding)
        kwargs = dict(
            image=spec.image,
            command=list(spec.keepalive_command),
            volumes=volumes,
            detach=True,
            labels=labels,
            name=name,
            mem_limit=spec.memory_limit,
            pids_limit=spec.pids_limit,
            nano_cpus=int(spec.cpus * 1e9),
            network_mode="bridge" if binding.allowed_outbound else self._network_mode,
            tmpfs={"/tmp": f"size={spec.tmpfs_size}"},
            environment={
                "HOME": _CONTAINER_HOME,
                "XDG_CACHE_HOME": f"{_CONTAINER_HOME}/.cache",
                "PIP_CACHE_DIR": f"{_CONTAINER_HOME}/.cache/pip",
                "npm_config_cache": f"{_CONTAINER_HOME}/.cache/npm",
            },
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

    def _build_volumes(self, target: ExecutionTarget) -> dict[str, dict[str, str]]:
        volumes: dict[str, dict[str, str]] = {}
        # The shared execution-scope workspace: every sibling instance of the
        # scope bind-mounts the same host directory read/write.
        workspace = self._settings.session_workspace_dir(target.workspace_scope_id)
        workspace.mkdir(parents=True, exist_ok=True)
        workspace_path = str(workspace)
        volumes[workspace_path] = {"bind": workspace_path, "mode": "rw"}
        # Instance-private HOME/cache state.
        state_dir = self._instance_state_dir_by_target(target)
        state_dir.mkdir(parents=True, exist_ok=True)
        volumes[str(state_dir)] = {"bind": _CONTAINER_HOME, "mode": "rw"}
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

    def _instance_state_dir_by_target(self, target: ExecutionTarget) -> Path:
        return (
            self._settings.workspace_root()
            / ".covalent"
            / "sandbox-state"
            / _safe_name(target.execution_scope_id)
            / _safe_name(target.sandbox_instance_id)
        )

    async def is_alive(self, sandbox_instance_id: str) -> bool:
        container = self._containers.get(sandbox_instance_id)
        if container is None:
            return False
        try:
            await asyncio.to_thread(container.reload)
            return container.status == "running"
        except Exception:
            return False

    async def stop_instance(self, sandbox_instance_id: str) -> None:
        """Stop+remove one instance's container. Robust to untracked containers
        (e.g. created by a previous process) by falling back to a name lookup."""
        container = self._containers.pop(sandbox_instance_id, None)
        self._instance_meta.pop(sandbox_instance_id, None)
        self._bindings.pop(sandbox_instance_id, None)
        if container is not None:
            await asyncio.to_thread(self._remove_container, container)
            self._metrics.containers_stopped += 1
            self._release_capacity()
            return
        await asyncio.to_thread(self._remove_container_by_instance, sandbox_instance_id)

    async def stop_scope(self, execution_scope_id: str) -> None:
        """Stop every active instance of one execution scope."""
        for instance_id in self._instance_ids_for_scope(execution_scope_id):
            await self.stop_instance(instance_id)

    async def stop(self, session_id: str) -> None:
        """Compatibility: stop every active instance bound to a chat session."""
        stopped = False
        for instance_id, meta in list(self._instance_meta.items()):
            if meta.get("session_id") == session_id:
                await self.stop_instance(instance_id)
                stopped = True
        if stopped:
            return
        await asyncio.to_thread(self._remove_container_by_name, session_id)

    def _instance_ids_for_scope(self, execution_scope_id: str) -> list[str]:
        instance_ids = [
            instance_id
            for instance_id, meta in self._instance_meta.items()
            if meta.get("execution_scope_id") == execution_scope_id
            or meta.get("session_id") == execution_scope_id
        ]
        # Include tracked containers whose meta was lost (crash-adjacent paths).
        for instance_id, binding in self._bindings.items():
            if binding.target.execution_scope_id == execution_scope_id and instance_id not in instance_ids:
                instance_ids.append(instance_id)
        return instance_ids

    async def aclose(self) -> None:
        containers = list(self._containers.values())
        self._containers.clear()
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

    def _remove_container_by_instance(self, sandbox_instance_id: str) -> None:
        """Find an untracked container by its instance label and remove it."""
        try:
            containers = self._api().containers.list(
                all=True, filters={"label": [f"{_INSTANCE_LABEL}={sandbox_instance_id}"]}
            )
        except Exception:
            return
        for container in containers:
            self._remove_container(container)

    def _remove_container_by_name(self, session_id: str) -> None:
        """Legacy fallback: a session-keyed name from a pre-instance process."""
        for name in (
            f"covalent-sandbox-{_safe_name(session_id)}-{_safe_name(session_id)[-12:]}",
            f"covalent-sandbox-{_safe_name(session_id)}",
        ):
            try:
                container = self._api().containers.get(name)
            except Exception:
                continue
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

    async def list_sandbox_instance_summaries(self) -> list[dict[str, object]]:
        """Per-instance summaries from daemon-labeled containers (across
        restarts): instance id, owning session id (None for run scopes), and
        execution scope. Feeds the reaper's orphan reconciliation."""
        containers = await asyncio.to_thread(self._list_sandbox_containers)
        summaries: list[dict[str, object]] = []
        for container in containers:
            labels = container.labels or {}
            summaries.append(
                {
                    "sandbox_instance_id": labels.get(_INSTANCE_LABEL) or "",
                    "session_id": labels.get(_SESSION_LABEL),
                    "execution_scope_id": labels.get(_EXECUTION_SCOPE_LABEL) or "",
                }
            )
        return summaries

    def is_instance_tracked(self, sandbox_instance_id: str) -> bool:
        """Whether this backend process is actively managing the instance's
        container (as opposed to an orphan from a previous process)."""
        return sandbox_instance_id in self._containers

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
        sandbox_instance_id: str | None = None,
    ):
        instance_id = sandbox_instance_id or session_id
        if not instance_id:
            raise ValueError("DockerBackend.spawn_stream requires a sandbox_instance_id (or legacy session_id)")
        container = await self.ensure(instance_id)
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
        ``kill`` is run inside the same container so the namespace matches.

        This stays synchronous because callers (``DockerExecProcess.terminate``/
        ``kill``) implement the sync ``Process`` protocol used by
        ``SkillProcessManager._terminate``. It is only invoked on the rare
        abnormal-termination path (a hung exec), so a brief blocking HTTP round
        trip to the Docker daemon is acceptable. If the daemon becomes
        consistently slow this could stall the loop; the kill is best-effort
        and swallows errors so the caller still falls through to socket close.
        """
        try:
            info = self._api().api.exec_inspect(exec_id)
        except Exception:
            logger.debug("exec_inspect failed during kill probe", exc_info=True)
            return
        pid = info.get("Pid") if isinstance(info, dict) else None
        if not isinstance(pid, int) or pid <= 0:
            return
        try:
            container.exec_run(["kill", f"-{signal_name}", str(pid)])
        except Exception:
            logger.debug("in-container kill failed for exec %s pid %s", exec_id, pid, exc_info=True)

    async def exec(
        self,
        command: list[str],
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        session_id: str | None = None,
        sandbox_instance_id: str | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        instance_id = sandbox_instance_id or session_id
        if not instance_id:
            raise ValueError("DockerBackend.exec requires a sandbox_instance_id (or legacy session_id)")
        container = await self.ensure(instance_id)
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
