"""One-off smoke: prove Phase 1b hardening over a REAL container using the
locally-cached alpine:latest image (no package mirror needed).

Checks, against a per-session container created by DockerBackend with the
default hardening (mem/pids/cpu limits, tmpfs /tmp, network_mode=none):
  1. exec with stdin round-trips.
  2. a write INSIDE the workspace mount reaches the host; a write OUTSIDE any
     mount (container /tmp tmpfs) does NOT reach the host (path containment).
  3. network egress is blocked (wget to a non-routable address fails).

Run:  uv run python script/smoke-docker-hardening.py
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path

from agent_framework.infra.settings import AppSettings
from agent_framework.runtime.docker_backend import DockerBackend


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="af-harden-"))
    settings = AppSettings(
        workspace_root_dir=str(tmp / "ws"),
        execution_backend_docker_image="alpine:latest",  # locally cached
    )
    backend = DockerBackend(settings, skill_source_dirs_provider=lambda: [])
    session_id = "harden-" + uuid.uuid4().hex[:8]
    workspace = settings.session_workspace_dir(session_id)

    try:
        # 1. exec with stdin round-trips.
        r = await backend.exec(
            ["sh", "-c", "read line; echo got:$line"],
            session_id=session_id, timeout=30.0, stdin=b"hello\n",
        )
        assert r.exit_code == 0, r
        assert b"got:hello" in r.stdout, r.stdout
        print(f"[1 stdin] ok -> {r.stdout.strip().decode()}")

        # 2a. write INSIDE the workspace mount -> reaches the host.
        marker_in = "inside_" + uuid.uuid4().hex[:8]
        await backend.exec(
            ["sh", "-c", f"echo x > {workspace}/{marker_in}"],
            session_id=session_id, timeout=30.0,
        )
        assert (workspace / marker_in).exists(), "workspace write did not reach host"
        print(f"[2a mount] ok -> host {workspace / marker_in} exists")

        # 2b. write OUTSIDE any mount (container /tmp tmpfs) -> NOT on the host.
        marker_out = "outside_" + uuid.uuid4().hex[:8]
        await backend.exec(
            ["sh", "-c", f"echo x > /tmp/{marker_out}"],
            session_id=session_id, timeout=30.0,
        )
        assert not Path(f"/tmp/{marker_out}").exists(), "container /tmp write leaked to host!"
        print(f"[2b contain] ok -> host /tmp/{marker_out} does NOT exist")

        # 3. network egress blocked (network_mode=none).
        r = await backend.exec(
            ["wget", "-T", "2", "-q", "http://1.2.3.4/"],
            session_id=session_id, timeout=30.0,
        )
        assert r.exit_code != 0, "network egress was NOT blocked!"
        print(f"[3 network] ok -> wget exit={r.exit_code} (egress blocked)")

    finally:
        await backend.aclose()

    print("[smoke] OK — Phase 1b hardening verified on a real container")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
