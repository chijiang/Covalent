from __future__ import annotations
from collections.abc import Callable, Sequence
from covalent_runtime.ports.execution import ExecutionBackend
from covalent_enterprise.infra.settings import AppSettings

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
    from covalent_execution_native.backend import FileSystemBackend

    kind = settings.execution_backend_kind
    if kind == "filesystem":
        return FileSystemBackend(settings)
    if kind == "docker":
        if skill_source_dirs_provider is None:
            raise ValueError("Docker backend requires skill_source_dirs_provider")
        from covalent_execution_docker.backend import DockerBackend

        return DockerBackend(settings, skill_source_dirs_provider)
    if kind == "kubernetes":
        raise NotImplementedError("Kubernetes execution backend lands in Phase 3")
    raise ValueError(f"Unknown execution_backend_kind: {kind!r}")
