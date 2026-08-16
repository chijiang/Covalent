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
    tmp = tempfile.mkdtemp(prefix="covalent-acceptance-")
    settings = AppSettings(workspace_root_dir=tmp, execution_backend_kind="docker")
    backend = DockerBackend(settings, skill_source_dirs_provider=lambda: [])
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

    await backend.aclose()
    print("\nALL ACCEPTANCE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
