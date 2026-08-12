"""Execution backend abstraction.

A backend decides *where* a session's skill code and scripts run.

- ``spawn_stream`` — long-lived bidirectional stdio for JSON-RPC skill runners
  (used by :class:`agent_framework.skills.process.SkillProcessManager`).
- ``exec`` — one-shot command for ad-hoc scripts.
- ``ensure``/``stop``/``is_alive`` — per-session environment lifecycle
  (no-ops on FileSystem; container create/teardown on Docker).
- ``aclose`` — release backend-owned resources on shutdown.

``spawn_stream``/``exec`` take a ``session_id`` so a backend can scope execution
to a per-session environment. FileSystem ignores it; Docker routes it to the
session's container. ``spawn_stream`` returns an ``asyncio.subprocess.Process``-
compatible object: FileSystem returns a real subprocess; Docker returns a
:class:`~agent_framework.runtime.docker_process.DockerExecProcess` that quacks
like one (validated by ``script/spike-docker-exec-rpc.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from agent_framework.infra.settings import AppSettings


@dataclass
class ExecResult:
    """Result of a one-shot command execution."""

    exit_code: int
    stdout: bytes
    stderr: bytes


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

    Implementations: :class:`~agent_framework.runtime.filesystem_backend.FileSystemBackend`
    (default), :class:`~agent_framework.runtime.docker_backend.DockerBackend`,
    KubernetesBackend (Phase 3).
    """

    name: str

    async def ensure(self, session_id: str) -> None:
        """Make the per-session execution environment ready. Idempotent."""
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

    async def stop(self, session_id: str) -> None:
        """Tear down the per-session environment."""
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
    from agent_framework.runtime.filesystem_backend import FileSystemBackend

    kind = settings.execution_backend_kind
    if kind == "filesystem":
        return FileSystemBackend(settings)
    if kind == "docker":
        if skill_source_dirs_provider is None:
            raise ValueError("Docker backend requires skill_source_dirs_provider")
        from agent_framework.runtime.docker_backend import DockerBackend

        return DockerBackend(settings, skill_source_dirs_provider)
    if kind == "kubernetes":
        raise NotImplementedError("Kubernetes execution backend lands in Phase 3")
    raise ValueError(f"Unknown execution_backend_kind: {kind!r}")
