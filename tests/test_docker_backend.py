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
import json
import os
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
            rewritten = backend.rewrite_command([sys.executable, "-u", runner, "entry.py"])
            self.assertEqual(rewritten, ["python", "-u", "/runners/python_runner.py", "entry.py"])

    def test_rewrite_command_leaves_other_args_intact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._make_backend(Path(tmp))
            rewritten = backend.rewrite_command(["node", "/some/script.js", "--flag"])
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

    async def test_relative_skill_source_dir_is_mounted_absolute(self) -> None:
        # Skill source dirs can be relative (e.g. "skills/built_in/x"); Docker
        # requires absolute bind paths, and they must equal resolved_entry_point()
        # / resolved_working_dir() (os.path.abspath) so the runner can import them
        # inside the container.
        rel = "skills/built_in/skill-x"
        expected = os.path.abspath(rel)
        with tempfile.TemporaryDirectory() as tmp:
            fake_client = _FakeDockerClient()
            backend = self._make_backend(Path(tmp), source_dirs=[rel], client=fake_client)
            await backend.ensure("sess-rel")
            binds = {k: v["bind"] for k, v in fake_client.containers.run_calls[0]["volumes"].items()}
            self.assertIn(expected, binds)
            self.assertTrue(all(p.startswith("/") for p in binds), binds)

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

    async def test_create_container_applies_limits_network_and_tmpfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_client = _FakeDockerClient()
            backend = self._make_backend(Path(tmp), client=fake_client)
            await backend.ensure("sess-limits")
            call = fake_client.containers.run_calls[0]
            self.assertEqual(call["mem_limit"], "512m")
            self.assertEqual(call["pids_limit"], 256)
            self.assertEqual(call["nano_cpus"], 1_000_000_000)
            self.assertEqual(call["network_mode"], "none")
            self.assertEqual(call["tmpfs"], {"/tmp": "size=128m"})

    async def test_startup_sweep_removes_labeled_containers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_client = _FakeDockerClient()
            fake_client.containers.run(image="x", name="covalent-sandbox-a", labels={"covalent.sandbox": "1", "covalent.session": "a"})
            fake_client.containers.run(image="x", name="covalent-sandbox-b", labels={"covalent.sandbox": "1", "covalent.session": "b"})
            backend = self._make_backend(Path(tmp), client=fake_client)
            await backend.startup_sweep()
            self.assertTrue(fake_client.containers._by_name["covalent-sandbox-a"].removed)
            self.assertTrue(fake_client.containers._by_name["covalent-sandbox-b"].removed)

    async def test_list_sandbox_sessions_returns_session_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_client = _FakeDockerClient()
            fake_client.containers.run(image="x", name="covalent-sandbox-a", labels={"covalent.sandbox": "1", "covalent.session": "sess-a"})
            fake_client.containers.run(image="x", name="covalent-sandbox-b", labels={"covalent.sandbox": "1", "covalent.session": "sess-b"})
            fake_client.containers.run(image="x", name="unrelated", labels={"covalent.sandbox": "0"})
            backend = self._make_backend(Path(tmp), client=fake_client)
            self.assertEqual(sorted(await backend.list_sandbox_sessions()), ["sess-a", "sess-b"])

    async def test_stop_removes_untracked_container_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_client = _FakeDockerClient()
            fake_client.containers.run(image="x", name="covalent-sandbox-orph", labels={"covalent.sandbox": "1", "covalent.session": "orph"})
            backend = self._make_backend(Path(tmp), client=fake_client)
            await backend.stop("orph")
            self.assertTrue(fake_client.containers._by_name["covalent-sandbox-orph"].removed)

    async def test_kill_exec_signals_pid_in_container(self) -> None:
        killed: list[list[str]] = []

        class _C:
            def exec_run(self, cmd, **_kw):
                killed.append(list(cmd))
                return types.SimpleNamespace(exit_code=0, output=(b"", b""))

        class _Api:
            def exec_inspect(self, _exec_id):
                return {"Pid": 4242, "ExitCode": None}

        class _Client:
            api = _Api()

        with tempfile.TemporaryDirectory() as tmp:
            backend = self._make_backend(Path(tmp), client=_Client())
            backend._kill_exec(_C(), "exec-1", "TERM")
        self.assertEqual(killed, [["kill", "-TERM", "4242"]])

    async def test_kill_exec_noop_when_no_pid(self) -> None:
        killed: list[list[str]] = []

        class _C:
            def exec_run(self, cmd, **_kw):
                killed.append(list(cmd))

        class _Api:
            def exec_inspect(self, _exec_id):
                return {"Pid": 0}

        class _Client:
            api = _Api()

        with tempfile.TemporaryDirectory() as tmp:
            backend = self._make_backend(Path(tmp), client=_Client())
            backend._kill_exec(_C(), "exec-1", "KILL")
        self.assertEqual(killed, [])

    async def test_metrics_count_start_and_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_client = _FakeDockerClient()
            backend = self._make_backend(Path(tmp), client=fake_client)
            await backend.ensure("s1")
            snap = backend.metrics_snapshot()
            self.assertEqual(snap["live_containers"], 1)
            self.assertEqual(snap["containers_started"], 1)
            await backend.stop("s1")
            snap = backend.metrics_snapshot()
            self.assertEqual(snap["live_containers"], 0)
            self.assertEqual(snap["containers_stopped"], 1)

    async def test_metrics_count_startup_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_client = _FakeDockerClient()
            fake_client.containers.run(image="x", name="covalent-sandbox-a", labels={"covalent.sandbox": "1", "covalent.session": "a"})
            fake_client.containers.run(image="x", name="covalent-sandbox-b", labels={"covalent.sandbox": "1", "covalent.session": "b"})
            backend = self._make_backend(Path(tmp), client=fake_client)
            await backend.startup_sweep()
            self.assertEqual(backend.metrics_snapshot()["containers_swept_startup"], 2)

    async def test_daemon_down_raises_backend_unavailable(self) -> None:
        from agent_framework.runtime.backend import BackendUnavailable

        class _DownContainers:
            def run(self, **_kw):
                raise docker.errors.APIError("daemon down")

            def get(self, _name):
                raise docker.errors.APIError("daemon down")

        class _DownClient(types.SimpleNamespace):
            containers = _DownContainers()

        with tempfile.TemporaryDirectory() as tmp:
            backend = self._make_backend(Path(tmp), client=_DownClient())
            with self.assertRaises(BackendUnavailable):
                await backend.ensure("s1")
            self.assertEqual(backend.metrics_snapshot()["unavailable_errors"], 1)

    async def test_network_mode_bridge_when_agent_has_outbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_client = _FakeDockerClient()
            backend = self._make_backend(Path(tmp), client=fake_client)
            backend.record_session("s-out", "test-agent", ["api.example.com"])
            await backend.ensure("s-out")
            call = fake_client.containers.run_calls[0]
            self.assertEqual(call["network_mode"], "bridge")

    async def test_network_mode_default_when_agent_has_no_outbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_client = _FakeDockerClient()
            backend = self._make_backend(Path(tmp), client=fake_client)
            await backend.ensure("s-none")
            call = fake_client.containers.run_calls[0]
            self.assertEqual(call["network_mode"], "none")  # settings default

    async def test_sandbox_snapshot_returns_session_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_client = _FakeDockerClient()
            backend = self._make_backend(Path(tmp), client=fake_client)
            backend.record_session("s-snap", "my-agent", ["api.example.com"])
            await backend.ensure("s-snap")
            snapshot = await backend.sandbox_snapshot()
            self.assertTrue(snapshot["supported"])
            self.assertEqual(snapshot["live"], 1)
            self.assertEqual(len(snapshot["sessions"]), 1)
            session = snapshot["sessions"][0]
            self.assertEqual(session["session_id"], "s-snap")
            self.assertEqual(session["agent_name"], "my-agent")
            self.assertEqual(session["network_mode"], "bridge")
            self.assertEqual(session["allowed_outbound"], ["api.example.com"])
            self.assertIsNotNone(session["started_at"])
            self.assertIn("config", snapshot)
            self.assertEqual(snapshot["config"]["image"], "covalent-sandbox:dev")


def settings_session_workspace_dir(workspace_root: Path, session_id: str) -> Path:
    """Mirror AppSettings.session_workspace_dir for assertion expectations."""
    return AppSettings(workspace_root_dir=str(workspace_root)).session_workspace_dir(session_id)


class _FakeContainer:
    def __init__(self, name: str, labels: dict[str, str] | None = None) -> None:
        self.id = "cid-" + name
        self.name = name
        self.status = "running"
        self.labels = labels or {}
        self.attrs = types.SimpleNamespace(load=lambda: None)
        self.removed = False

    def reload(self) -> None:
        return None

    def stop(self, **_kwargs) -> None:
        self.status = "exited"

    def remove(self, **_kwargs) -> None:
        self.status = "removed"
        self.removed = True

    def exec_run(self, *_args, **_kwargs):
        return types.SimpleNamespace(exit_code=0, output=(b"", b""))


class _FakeContainers:
    def __init__(self) -> None:
        self.run_calls: list[dict] = []
        self._by_name: dict[str, _FakeContainer] = {}

    def run(self, **kwargs) -> _FakeContainer:
        self.run_calls.append(kwargs)
        name = kwargs.get("name", "anon")
        container = _FakeContainer(name, labels=kwargs.get("labels"))
        self._by_name[name] = container
        return container

    def get(self, name: str) -> _FakeContainer:
        return self._by_name[name]

    def list(self, all: bool = False, filters: dict | None = None) -> list[_FakeContainer]:
        containers = list(self._by_name.values())
        if not filters:
            return containers
        wanted = filters.get("label", [])
        result: list[_FakeContainer] = []
        for c in containers:
            for label_filter in wanted:
                key, _eq, val = label_filter.partition("=")
                if key in c.labels and (not val or c.labels[key] == val):
                    result.append(c)
                    break
        return result


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

    async def test_exec_with_stdin_round_trips(self) -> None:
        result = await self.backend.exec(
            ["python", "-c", "import sys; print('got:' + sys.stdin.readline().strip())"],
            session_id=self.session_id,
            timeout=30.0,
            stdin=b"hello\n",
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn(b"got:hello", result.stdout)

    async def test_workspace_writes_reach_host_but_outside_do_not(self) -> None:
        workspace = self.settings.session_workspace_dir(self.session_id)
        marker = "covalent_escape_" + uuid.uuid4().hex[:8]
        # Write INSIDE the workspace mount -> reaches the host.
        await self.backend.exec(
            ["sh", "-c", f"echo x > {workspace}/{marker}"], session_id=self.session_id, timeout=30.0
        )
        self.assertTrue((workspace / marker).exists())
        # Write OUTSIDE any mount (container /tmp tmpfs) -> must NOT reach the host.
        await self.backend.exec(
            ["sh", "-c", f"echo x > /tmp/{marker}"], session_id=self.session_id, timeout=30.0
        )
        self.assertFalse(Path(f"/tmp/{marker}").exists())

    async def test_network_egress_is_blocked(self) -> None:
        # network_mode=none -> outbound TCP connections fail.
        result = await self.backend.exec(
            ["python", "-c", "import socket; socket.create_connection(('1.2.3.4', 80), 2)"],
            session_id=self.session_id,
            timeout=30.0,
        )
        self.assertNotEqual(result.exit_code, 0)

    async def test_run_shell_tool_executes_in_container(self) -> None:
        from agent_framework.core.shell_tools import RUN_SHELL_TOOL, register_shell_tool

        # register_shell_tool reads the enabled flag off settings; the default
        # asyncSetUp settings don't enable it, so build an enabled settings with
        # the same workspace root the backend already mounted.
        enabled_settings = AppSettings(
            workspace_root_dir=str(self.settings.workspace_root()),
            execution_backend_shell_tool_enabled=True,
        )
        tools: dict[str, object] = {}
        fake_registry = types.SimpleNamespace(
            register_local_tool=lambda name, schema, handler=None: tools.__setitem__(name, handler)
        )
        register_shell_tool(fake_registry, enabled_settings, self.backend)
        self.assertIn(RUN_SHELL_TOOL, tools)
        ctx = types.SimpleNamespace(session_id=self.session_id)
        result = json.loads(await tools[RUN_SHELL_TOOL]({"command": "echo hi; uname -s"}, ctx))
        self.assertEqual(result["execution_backend"], "docker")
        self.assertIn("hi", result["stdout"])
        self.assertIn("Linux", result["stdout"])  # container is Linux, not host Darwin


if __name__ == "__main__":
    unittest.main()
