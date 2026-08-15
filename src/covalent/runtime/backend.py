"""Execution backend abstraction.

A backend decides *where* a session's skill code and scripts run.

- ``spawn_stream`` — long-lived bidirectional stdio for JSON-RPC skill runners
  (used by :class:`covalent.skills.process.SkillProcessManager`).
- ``exec`` — one-shot command for ad-hoc scripts.
- ``ensure``/``stop``/``is_alive`` — per-session environment lifecycle
  (no-ops on FileSystem; container create/teardown on Docker).
- ``aclose`` — release backend-owned resources on shutdown.

``spawn_stream``/``exec`` take a ``session_id`` so a backend can scope execution
to a per-session environment. FileSystem ignores it; Docker routes it to the
session's container. ``spawn_stream`` returns an ``asyncio.subprocess.Process``-
compatible object: FileSystem returns a real subprocess; Docker returns a
:class:`~covalent.runtime.docker_process.DockerExecProcess` that quacks
like one (validated by ``script/spike-docker-exec-rpc.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from covalent.core.agent import AgentSpec
    from covalent.core.types import RunContext
    from covalent.infra.settings import AppSettings


@dataclass
class ExecResult:
    """Result of a one-shot command execution."""

    exit_code: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class ExecutionTarget:
    """Where a logical sandbox execution belongs.

    Separates conversation identity (``session_id``, may be None for a
    stateless run) from execution identity: the lifecycle scope
    (``execution_scope_id`` = chat session id or run id), the filesystem
    scope shared by collaborating agents (``workspace_scope_id``), and the
    per-agent logical sandbox (``sandbox_instance_id``).
    """

    execution_scope_id: str
    session_id: str | None
    workspace_scope_id: str
    sandbox_instance_id: str
    agent_name: str


@dataclass(frozen=True)
class SandboxSpec:
    """Immutable snapshot of the profile revision a sandbox instance uses."""

    profile_id: str
    profile_revision: int
    image: str
    keepalive_command: tuple[str, ...]
    runtime_capabilities: frozenset[str]
    contract_version: int
    memory_limit: str
    pids_limit: int
    cpus: float
    tmpfs_size: str


@dataclass(frozen=True)
class SandboxBinding:
    """A resolved execution target plus its pinned spec and outbound policy."""

    target: ExecutionTarget
    spec: SandboxSpec
    allowed_outbound: tuple[str, ...]


class ExecutionBindingResolver(Protocol):
    """Resolves the logical sandbox binding for an agent run.

    The concrete implementation lives in the application layer
    (``SandboxBindingService``); the runtime package depends only on this
    protocol. ``resolve`` mutates ``RunContext`` execution identity fields,
    configures the execution backend (without eagerly creating a container),
    and returns the binding for the (execution scope, agent) pair.
    """

    async def resolve(self, agent: "AgentSpec", context: "RunContext") -> SandboxBinding:
        ...


class BackendUnavailable(RuntimeError):
    """The execution backend can't reach its runtime right now (e.g. the Docker
    daemon is down). Callers should surface this as a clean tool error rather than
    a raw infrastructure exception. Carries the underlying cause in ``__cause__``."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        if cause is not None:
            self.__cause__ = cause


class WorkspaceAccess(Protocol):
    """How the workspace file tools reach a session's files.

    ``host_path`` is the on-host directory when the backend exposes one
    (FileSystem, and Docker via a bind mount); the tools use ``pathlib`` on it.
    ``None`` means a remote workspace (e.g. a Kubernetes Pod volume) with no host
    path — the tools would then need backend-mediated file ops (Phase 3).
    """

    host_path: Path | None


@dataclass
class HostPathWorkspace:
    """WorkspaceAccess backed by a host directory (FileSystem + Docker bind-mount)."""

    host_path: Path


class ExecutionBackend(Protocol):
    """Where a session's skill code and scripts run.

    Implementations: :class:`~covalent.runtime.filesystem_backend.FileSystemBackend`
    (default), :class:`~covalent.runtime.docker_backend.DockerBackend`,
    KubernetesBackend (Phase 3).

    Execution is keyed by ``sandbox_instance_id`` (one logical sandbox per
    (execution scope, agent) pair) after ``configure(binding)`` registered it.
    The legacy ``session_id`` arguments remain as a compatibility fallback
    (one instance per session, default settings-derived spec) until all call
    sites move to explicit instance identity.
    """

    name: str

    def configure(self, binding: SandboxBinding) -> None:
        """Register a logical sandbox binding. No container is created until
        the first ``ensure``/``exec``/``spawn_stream`` for its instance."""
        ...

    async def ensure(self, sandbox_instance_id: str) -> None:
        """Make the instance's execution environment ready. Idempotent."""
        ...

    def workspace(self, session_id: str | None) -> WorkspaceAccess:
        """The session's workspace access. For host-path backends this points at a
        host directory (bind-mounted for Docker); remote backends return a
        workspace with ``host_path=None`` (Phase 3)."""
        ...

    async def spawn_stream(
        self,
        command: list[str],
        *,
        cwd: str | Path | None,
        env: dict[str, str],
        session_id: str | None = None,
        sandbox_instance_id: str | None = None,
    ) -> asyncio.subprocess.Process:
        """Start a long-lived process with piped stdio for JSON-RPC."""
        ...

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
        """Run a one-shot command to completion and capture its output."""
        ...

    def rewrite_command(self, command: list[str]) -> list[str]:
        """Return the command as this backend will actually execute it. The
        Docker backend rewrites host-only tokens (``sys.executable`` -> ``python``,
        the host runners dir -> ``/runners/``); FileSystem returns it unchanged.
        Useful for recording the real command in execution traces."""
        ...

    async def stop_instance(self, sandbox_instance_id: str) -> None:
        """Stop and remove one sandbox instance's live container (the logical
        binding is deliberately not the backend's concern)."""
        ...

    async def stop_scope(self, execution_scope_id: str) -> None:
        """Stop every active instance in one execution scope."""
        ...

    async def stop(self, session_id: str) -> None:
        """Compatibility: stop every active instance for a chat session."""
        ...

    def record_session(self, session_id: str, agent_name: str, allowed_outbound: list[str]) -> None:
        """Record per-session metadata (agent name + outbound patterns) before
        the container is created. FS: no-op."""
        ...

    def agent_outbound(self, session_id: str) -> list[str]:
        """Per-agent outbound patterns for this session. FS: returns []."""
        ...

    async def sandbox_snapshot(self) -> dict[str, object]:
        """Admin monitoring snapshot: live sessions, metrics, config. FS / unsupported
        backends return ``{"supported": false}``."""
        ...

    async def is_alive(self, session_id: str) -> bool:
        """Whether the per-session environment is still running."""
        ...

    async def startup_sweep(self) -> None:
        """Reclaim backend-owned resources left by previous runs (e.g. orphan
        containers from a crashed process). Called once at startup."""
        ...

    async def list_sandbox_sessions(self) -> list[str]:
        """Session ids of live sandbox environments this backend knows about
        (across restarts, where applicable). Used by the reaper to reconcile
        against the session store."""
        ...

    async def aclose(self) -> None:
        """Release backend-owned resources. No-op for stateless backends."""
        ...


def make_backend(
    settings: "AppSettings",
    skill_source_dirs_provider: Callable[[], Sequence[str]] | None = None,
) -> ExecutionBackend:
    """Select the execution backend configured by ``settings.execution_backend_kind``.

    ``skill_source_dirs_provider`` (a zero-arg callable returning host skill source
    directories) is required for the Docker backend so it can bind-mount skill code
    into the session container; ignored by FileSystem. Fails fast for backends not
    yet implemented so a misconfiguration surfaces at startup.
    """
    from covalent.runtime.filesystem_backend import FileSystemBackend

    kind = settings.execution_backend_kind
    if kind == "filesystem":
        return FileSystemBackend(settings)
    if kind == "docker":
        if skill_source_dirs_provider is None:
            raise ValueError("Docker backend requires skill_source_dirs_provider")
        from covalent.runtime.docker_backend import DockerBackend

        return DockerBackend(settings, skill_source_dirs_provider)
    if kind == "kubernetes":
        raise NotImplementedError("Kubernetes execution backend lands in Phase 3")
    raise ValueError(f"Unknown execution_backend_kind: {kind!r}")
