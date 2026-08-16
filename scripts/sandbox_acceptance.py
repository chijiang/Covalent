"""Real-Docker acceptance smoke for per-agent sandbox profiles.

Runs the spec's acceptance scenarios against a real Docker daemon with the
built images (covalent-sandbox:dev + covalent-sandbox-node:dev):

    uv run python scripts/sandbox_acceptance.py

Covers: distinct containers/images per agent in one scope, shared-workspace
file exchange, HOME isolation, per-instance stop, scope teardown, empty
outbound keeping network_mode=none, and stateless run-scope cleanup.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from covalent.infra.settings import AppSettings  # noqa: E402
from covalent.runtime.backend import (  # noqa: E402
    ExecutionTarget,
    SandboxBinding,
    SandboxSpec,
)
from covalent.runtime.docker_backend import DockerBackend  # noqa: E402

PY_IMAGE = "covalent-sandbox:dev"
NODE_IMAGE = "covalent-sandbox-node:dev"
SCOPE = "acceptance-scope-1"


def _spec(image: str, caps: frozenset[str]) -> SandboxSpec:
    return SandboxSpec(
        profile_id=f"acceptance-{image.split(':')[0]}",
        profile_revision=1,
        image=image,
        pull_policy="if_not_present",
        keepalive_command=("tail", "-f", "/dev/null"),
        runtime_capabilities=caps,
        contract_version=1,
        memory_limit="512m",
        pids_limit=256,
        cpus=1.0,
        tmpfs_size="128m",
    )


def _binding(instance_id: str, agent: str, spec: SandboxSpec, outbound: tuple[str, ...] = ()) -> SandboxBinding:
    return SandboxBinding(
        target=ExecutionTarget(
            execution_scope_id=SCOPE,
            session_id=SCOPE,
            workspace_scope_id=SCOPE,
            sandbox_instance_id=instance_id,
            agent_name=agent,
        ),
        spec=spec,
        allowed_outbound=outbound,
    )


def _fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


async def _await_exec(backend: DockerBackend, instance: str, command: list[str], timeout: float = 60.0):
    result = await backend.exec(command, session_id=instance, sandbox_instance_id=instance, timeout=timeout)
    if result.exit_code != 0:
        _fail(f"exec {command} failed ({result.exit_code}): {result.stderr.decode(errors='replace')}")
    return result.stdout


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="covalent-acceptance-") as tmp:
        await _run_acceptance(tmp)


async def _run_acceptance(tmp: str) -> None:
    settings = AppSettings(workspace_root_dir=tmp, execution_backend_kind="docker")
    backend = DockerBackend(settings, skill_source_dirs_provider=lambda: [])
    try:
        await _acceptance_backend_checks(tmp, backend)
    finally:
        await backend.aclose()


async def _acceptance_backend_checks(tmp: str, backend: DockerBackend) -> None:
    settings = AppSettings(workspace_root_dir=tmp, execution_backend_kind="docker")
    py = _binding("sbx-acc-py", "master", _spec(PY_IMAGE, frozenset({"python", "shell"})))
    node = _binding("sbx-acc-node", "delegate", _spec(NODE_IMAGE, frozenset({"nodejs", "shell"})))
    backend.configure(py)
    backend.configure(node)

    workspace = settings.session_workspace_dir(SCOPE)

    # 1-2. Distinct containers/images per agent in one execution scope.
    container_py = await backend.ensure("sbx-acc-py")
    container_node = await backend.ensure("sbx-acc-node")
    if container_py.id == container_node.id:
        _fail("master and delegate share a container")
    image_py = container_py.attrs.get("Config", {}).get("Image")
    image_node = container_node.attrs.get("Config", {}).get("Image")
    if image_py != PY_IMAGE or image_node != NODE_IMAGE:
        _fail(f"unexpected images: {image_py} / {image_node}")
    print(f"PASS: distinct containers ({container_py.id[:12]} python, {container_node.id[:12]} node)")

    # 3. Shared-workspace file exchange: python writes, node reads/modifies.
    await _await_exec(
        backend, "sbx-acc-py",
        ["python", "-c", f"import json; open('{workspace}/handoff.json','w').write(json.dumps({{'value': 1}}))"],
    )
    await _await_exec(
        backend, "sbx-acc-node",
        ["node", "-e", (
            f"const fs=require('fs'); const p='{workspace}/handoff.json';"
            "const o=JSON.parse(fs.readFileSync(p)); o.value=2; o.by='node';"
            "fs.writeFileSync(p, JSON.stringify(o));"
        )],
    )
    seen = (
        await _await_exec(
            backend, "sbx-acc-py",
            ["python", "-c", f"import json; print(json.load(open('{workspace}/handoff.json'))['value'])"],
        )
    ).strip()
    if seen != b"2":
        _fail(f"python did not observe node's write (got {seen!r})")
    print("PASS: shared workspace cross-runtime file exchange")

    # 4. HOME/cache isolation between sibling instances.
    await _await_exec(backend, "sbx-acc-py", ["sh", "-c", "echo py > /home/covalent/who.txt"])
    await _await_exec(backend, "sbx-acc-node", ["sh", "-c", "echo node > /home/covalent/who.txt"])
    py_home = (await _await_exec(backend, "sbx-acc-py", ["cat", "/home/covalent/who.txt"])).strip()
    node_home = (await _await_exec(backend, "sbx-acc-node", ["cat", "/home/covalent/who.txt"])).strip()
    if py_home != b"py" or node_home != b"node":
        _fail(f"HOME isolation broken: {py_home!r} / {node_home!r}")
    print("PASS: instance-private HOME isolation")

    # 5. Empty outbound keeps containers on network_mode=none.
    attrs_py = container_py.attrs.get("HostConfig", {})
    attrs_node = container_node.attrs.get("HostConfig", {})
    if attrs_py.get("NetworkMode") != "none" or attrs_node.get("NetworkMode") != "none":
        _fail(f"expected network_mode=none, got {attrs_py.get('NetworkMode')} / {attrs_node.get('NetworkMode')}")
    print("PASS: empty outbound -> network_mode=none")

    # 6. Stopping one instance leaves the sibling alive.
    await backend.stop_instance("sbx-acc-py")
    if await backend.is_alive("sbx-acc-py"):
        _fail("python instance still alive after stop_instance")
    if not await backend.is_alive("sbx-acc-node"):
        _fail("node sibling died when python instance stopped")
    print("PASS: stop one instance leaves sibling alive")

    # 7. Stateless run-scope cleanup removes containers and private state.
    stateless = _binding(
        "sbx-acc-run",
        "master",
        _spec(PY_IMAGE, frozenset({"python", "shell"})),
    )
    stateless_binding = SandboxBinding(
        target=ExecutionTarget(
            execution_scope_id="acceptance-run-1",
            session_id=None,
            workspace_scope_id="acceptance-run-1",
            sandbox_instance_id="sbx-acc-run",
            agent_name="master",
        ),
        spec=stateless.spec,
        allowed_outbound=(),
    )
    backend.configure(stateless_binding)
    await backend.ensure("sbx-acc-run")
    await backend.stop_scope("acceptance-run-1")
    if await backend.is_alive("sbx-acc-run"):
        _fail("stateless run container survived stop_scope")
    print("PASS: stateless run scope teardown")

    # 8. Scope teardown removes remaining instances.
    await backend.stop_scope(SCOPE)
    if await backend.is_alive("sbx-acc-node"):
        _fail("node container survived scope teardown")
    print("PASS: execution-scope teardown")

    # 9. Binding-service cleanup semantics (needs a database): reset removes
    # the instance-private state; stateless cleanup removes state + workspace.
    database_url = os.environ.get("AGENT_FRAMEWORK_DATABASE_URL")
    if database_url:
        await _acceptance_binding_service_checks(tmp, database_url)
    else:
        print("SKIP: binding-service cleanup checks (set AGENT_FRAMEWORK_DATABASE_URL to enable)")

    print("\nALL ACCEPTANCE CHECKS PASSED")


async def _acceptance_binding_service_checks(workspace_root: str, database_url: str) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from covalent.application.services.sandbox_binding_service import SandboxBindingService
    from covalent.application.services.sandbox_profile_service import SandboxProfileService
    from covalent.infra.sandbox_repository import SandboxRepository

    engine = create_async_engine(database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    repository = SandboxRepository(session_factory)
    settings = AppSettings(workspace_root_dir=workspace_root)
    service = SandboxBindingService(
        repository=repository,
        profile_service=SandboxProfileService(repository, settings),
        settings=settings,
        execution_backend=None,
    )
    scope = "acceptance-bind-1"
    run_scope = "acceptance-run-bind-1"
    try:
        await SandboxProfileService(repository, settings).ensure_seeded()
        await repository.ensure_session_row(scope)
        await repository.create_binding(
            {
                "id": "sbx-acc-bind",
                "execution_scope_id": scope,
                "session_id": scope,
                "scope_kind": "session",
                "agent_name": "master",
                "profile_id": "default",
                "profile_name_snapshot": "default",
                "profile_revision": 1,
                "spec_snapshot": {"profile_id": "default", "profile_revision": 1, "image": PY_IMAGE},
                "allowed_outbound_snapshot": [],
            }
        )
        state_root = Path(workspace_root).resolve() / ".covalent" / "sandbox-state" / scope / "sbx-acc-bind"
        state_root.mkdir(parents=True, exist_ok=True)
        (state_root / "cache.txt").write_text("private")

        if not await service.reset_instance("sbx-acc-bind"):
            _fail("reset_instance returned False for a live binding")
        if state_root.exists():
            _fail(f"reset_instance left the private state dir behind: {state_root}")
        if await repository.get_binding(scope, "master") is not None:
            _fail("reset_instance left the binding row behind")
        print("PASS: reset removes binding + private state")

        # Stateless cleanup removes the temporary workspace and every binding.
        run_workspace = settings.session_workspace_dir(run_scope)
        run_workspace.mkdir(parents=True, exist_ok=True)
        await repository.create_binding(
            {
                "id": "sbx-acc-runbind",
                "execution_scope_id": run_scope,
                "session_id": None,
                "scope_kind": "run",
                "agent_name": "master",
                "profile_id": "default",
                "profile_name_snapshot": "default",
                "profile_revision": 1,
                "spec_snapshot": {"profile_id": "default", "profile_revision": 1, "image": PY_IMAGE},
                "allowed_outbound_snapshot": [],
            }
        )
        await service.cleanup_stateless_run(run_scope)
        if run_workspace.exists():
            _fail("cleanup_stateless_run left the temporary workspace behind")
        if await repository.list_bindings_by_scope(run_scope):
            _fail("cleanup_stateless_run left binding rows behind")
        print("PASS: stateless cleanup removes bindings + temporary workspace")
    finally:
        await repository.delete_bindings_by_scope(scope)
        await repository.delete_bindings_by_scope(run_scope)
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        import shutil as _shutil
        import glob as _glob
        # Defensive: report (not silently leak) any acceptance leftovers.
        leftovers = _glob.glob("/tmp/covalent-acceptance-*") + _glob.glob(
            f"{__import__('tempfile').gettempdir()}/covalent-acceptance-*"
        )
        for leftover in leftovers:
            _shutil.rmtree(leftover, ignore_errors=True)
        if leftovers:
            print(f"cleaned {len(leftovers)} leftover acceptance dir(s)")
