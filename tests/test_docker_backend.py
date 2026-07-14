"""Tests for the Docker execution backend (Phase 1a).

Two layers:

- ``DockerBackendUnitTests`` — NOT gated. Exercises the backend logic and the
  ``DockerExecProcess`` demux with a real ``socket.socketpair()`` and a fake
  Docker client. Runs anywhere.
- ``DockerBackendIntegrationTests`` — gated on a reachable daemon AND the
  ``covalent-sandbox:dev`` image. Drives a real ``SkillProcessHandle`` over a
  container. Build the image first:
      docker build -t covalent-sandbox:dev -f Dockerfile.sandbox .
"""

from __future__ import annotations

import asyncio
import socket
import struct
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path

import docker
from agent_framework.infra.settings import AppSettings
from agent_framework.runtime.docker_backend import DockerBackend, _RUNNERS_HOST_DIR
from agent_framework.runtime.docker_process import DockerExecProcess

IMAGE = "covalent-sandbox:dev"

SERVER = r'''import sys, json
def emit(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()
emit({"jsonrpc": "2.0", "method": "ready", "params": {}})
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    m = json.loads(line)
    mid = m.get("id")
    if m.get("method") == "ping":
        emit({"jsonrpc": "2.0", "id": mid, "result": {"pong": True}})
    elif m.get("method") == "shutdown":
        emit({"jsonrpc": "2.0", "id": mid, "result": {"bye": True}})
        break
'''


def _frame(stream_type: int, payload: bytes) -> bytes:
    return struct.pack(">BxxxI", stream_type, len(payload)) + payload


# --------------------------------------------------------------------------- #
# Unit tests (no daemon required)
# --------------------------------------------------------------------------- #
class DockerBackendUnitTests(unittest.IsolatedAsyncioTestCase):
    def _make_backend(self, tmpdir: Path, *, source_dirs=None, client=None) -> DockerBackend:
        settings = AppSettings(workspace_root_dir=str(tmpdir))
        return DockerBackend(
            settings,
            skill_source_dirs_provider=lambda: list(source_dirs or []),
            docker_client=client,
        )

    def test_rewrite_command_maps_interpreter_and_runners(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._make_backend(Path(tmp))
            runner = str(_RUNNERS_HOST_DIR / "python_runner.py")
            rewritten = backend._rewrite_command([sys.executable, "-u", runner, "entry.py"])
            self.assertEqual(rewritten, ["python", "-u", "/runners/python_runner.py", "entry.py"])

    def test_rewrite_command_leaves_other_args_intact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._make_backend(Path(tmp))
            rewritten = backend._rewrite_command(["node", "/some/script.js", "--flag"])
            self.assertEqual(rewritten, ["node", "/some/script.js", "--flag"])

    def test_sanitize_env_drops_host_path_vars(self) -> None:
        sanitized = DockerBackend._sanitize_env(
            {"PATH": "/host/bin", "PYTHONPATH": "/host/lib", "PYTHONHOME": "/host", "SKILL_NAME": "foo", "HOME": "/h"}
        )
        self.assertNotIn("PATH", sanitized)
        self.assertNotIn("PYTHONPATH", sanitized)
        self.assertNotIn("PYTHONHOME", sanitized)
        self.assertEqual(sanitized["SKILL_NAME"], "foo")
        self.assertEqual(sanitized["HOME"], "/h")

    async def test_ensure_creates_container_with_workspace_and_skill_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            skill_dir = tmp_path / "skill-a"
            skill_dir.mkdir()
            fake_client = _FakeDockerClient()
            backend = self._make_backend(tmp_path, source_dirs=[str(skill_dir)], client=fake_client)

            await backend.ensure("sess-1")

            self.assertEqual(len(fake_client.containers.run_calls), 1)
            call = fake_client.containers.run_calls[0]
            self.assertEqual(call["image"], IMAGE)
            self.assertEqual(call["command"], ["tail", "-f", "/dev/null"])
            self.assertEqual(call["labels"], {"covalent.sandbox": "1", "covalent.session": "sess-1"})
            self.assertEqual(call["name"], "covalent-sandbox-sess-1")
            # The session workspace and the skill source dir are both mounted at
            # their host-absolute paths.
            workspace_host = str(settings_session_workspace_dir(tmp_path, "sess-1"))
            binds = {k: v["bind"] for k, v in call["volumes"].items()}
            self.assertIn(workspace_host, binds)
            self.assertEqual(binds[workspace_host], workspace_host)
            self.assertIn(str(skill_dir), binds)
            self.assertEqual(binds[str(skill_dir)], str(skill_dir))

            # Idempotent: a second ensure reuses the cached container.
            await backend.ensure("sess-1")
            self.assertEqual(len(fake_client.containers.run_calls), 1)

    async def test_demux_readline_reassembles_stdout_and_logs_stderr(self) -> None:
        a, b = socket.socketpair()
        captured: list[str] = []
        proc = DockerExecProcess(a, exit_code_probe=lambda: 0, log_stderr=captured.append)
        # Interleave: a stderr frame, then two stdout lines, one split across frames.
        b.sendall(
            _frame(2, b"noise-1\n")
            + _frame(1, b"hello\n")
            + _frame(1, b"wor")
            + _frame(1, b"ld\n")
        )
        self.assertEqual(await asyncio.wait_for(proc.stdout.readline(), 5), b"hello")
        self.assertEqual(await asyncio.wait_for(proc.stdout.readline(), 5), b"world")
        self.assertTrue(any("noise-1" in c for c in captured))
        a.close()
        b.close()

    async def test_demux_readline_returns_eof_on_socket_close(self) -> None:
        a, b = socket.socketpair()
        proc = DockerExecProcess(a, exit_code_probe=lambda: 0)
        b.close()  # peer closes -> EOF
        self.assertEqual(await asyncio.wait_for(proc.stdout.readline(), 5), b"")
        a.close()


def settings_session_workspace_dir(workspace_root: Path, session_id: str) -> Path:
    """Mirror AppSettings.session_workspace_dir for assertion expectations."""
    return AppSettings(workspace_root_dir=str(workspace_root)).session_workspace_dir(session_id)


class _FakeContainer:
    def __init__(self, name: str) -> None:
        self.id = "cid-" + name
        self.status = "running"
        self.attrs = types.SimpleNamespace(load=lambda: None)

    def reload(self) -> None:
        return None

    def stop(self, **_kwargs) -> None:
        self.status = "exited"

    def remove(self, **_kwargs) -> None:
        self.status = "removed"

    def exec_run(self, *_args, **_kwargs):
        return types.SimpleNamespace(exit_code=0, output=(b"", b""))


class _FakeContainers:
    def __init__(self) -> None:
        self.run_calls: list[dict] = []
        self._by_name: dict[str, _FakeContainer] = {}

    def run(self, **kwargs) -> _FakeContainer:
        self.run_calls.append(kwargs)
        name = kwargs.get("name", "anon")
        container = _FakeContainer(name)
        self._by_name[name] = container
        return container

    def get(self, name: str) -> _FakeContainer:
        return self._by_name[name]


class _FakeDockerClient(types.SimpleNamespace):
    def __init__(self) -> None:
        super().__init__(containers=_FakeContainers())


# --------------------------------------------------------------------------- #
# Integration tests (gated on daemon + image)
# --------------------------------------------------------------------------- #
def _docker_ready() -> bool:
    try:
        docker.from_env().ping()
        return True
    except Exception:
        return False


def _image_present() -> bool:
    try:
        docker.from_env().images.get(IMAGE)
        return True
    except Exception:
        return False


@unittest.skipUnless(
    _docker_ready() and _image_present(),
    f"requires a Docker daemon and the {IMAGE} image (build with: docker build -t {IMAGE} -f Dockerfile.sandbox .)",
)
class DockerBackendIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from agent_framework.skills.process import SkillProcessHandle  # noqa: F401
        from agent_framework.skills.spec import ManifestSkillSpec  # noqa: F401

        self._tmp = tempfile.TemporaryDirectory(prefix="af-docker-test-")
        root = Path(self._tmp.name)
        self.server_dir = root / "skill-src"
        self.server_dir.mkdir()
        (self.server_dir / "server.py").write_text(SERVER)
        self.session_id = "test-" + uuid.uuid4().hex[:8]
        self.settings = AppSettings(workspace_root_dir=str(root / "ws"))
        self.backend = DockerBackend(
            self.settings,
            skill_source_dirs_provider=lambda: [str(self.server_dir)],
        )

    async def asyncTearDown(self) -> None:
        try:
            await self.backend.aclose()
        finally:
            self._tmp.cleanup()

    async def test_spawn_stream_drives_skill_handle(self) -> None:
        from agent_framework.skills.process import SkillProcessHandle
        from agent_framework.skills.spec import ManifestSkillSpec

        spec = ManifestSkillSpec(name="echo", description="integration test skill")
        server_path = str(self.server_dir / "server.py")
        proc = await self.backend.spawn_stream(
            [sys.executable, "-u", server_path],
            cwd=str(self.server_dir),
            env={},
            session_id=self.session_id,
        )
        handle = SkillProcessHandle(spec=spec, process=proc)
        handle._reader_task = asyncio.create_task(handle._read_loop())
        try:
            await asyncio.wait_for(handle._ready.wait(), timeout=20.0)
            ping = await handle.send_request("ping")
            self.assertTrue(ping["pong"])
            bye = await handle.send_request("shutdown")
            self.assertTrue(bye["bye"])
            self.assertEqual(await asyncio.wait_for(proc.wait(), timeout=10.0), 0)
        finally:
            handle._reader_task.cancel()

    async def test_exec_one_shot(self) -> None:
        result = await self.backend.exec(
            ["python", "-c", "print('one-shot-ok')"],
            session_id=self.session_id,
            timeout=30.0,
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn(b"one-shot-ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
