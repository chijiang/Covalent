"""Docker sandbox execution demo.

Demonstrates the minimal lifecycle of running work inside an isolated Docker
sandbox:

  Step 1     Mount a host temp directory into a fresh sandbox container and start it.
  Step 2     `touch file.txt` inside the mounted volume.
  Step 3     `echo "hello world" > file.txt` inside the mounted volume.
  Teardown   Stop and remove the sandbox.
  Verify     Confirm the file persisted on the host (the mount outlives the container).

Run with:  uv run python script/sandbox-testing.py
Requires:  a running Docker daemon reachable via the local socket.
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
from pathlib import Path

import docker

SANDBOX_IMAGE = "alpine:latest"
# Where the host directory appears *inside* the container.
CONTAINER_MOUNT = "/workspace"


def exec_in(container: docker.models.containers.Container, cmd: list[str]) -> str:
    """Run `cmd` inside the container, assert a zero exit code, return stdout as text."""
    result = container.exec_run(cmd)
    output = result.output
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    if result.exit_code != 0:
        raise RuntimeError(f"{cmd!r} failed (exit {result.exit_code}): {output!r}")
    return output


@contextlib.contextmanager
def host_workspace() -> "tuple[Path, bool]":
    """Yield (host_dir, ephemeral).

    Pass a directory path as the first CLI argument to keep the files on disk
    for manual inspection after the run. With no argument, an ephemeral temp
    directory is created and wiped when the script exits.
    """
    if len(sys.argv) > 1:
        host_dir = Path(sys.argv[1]).expanduser().resolve()
        host_dir.mkdir(parents=True, exist_ok=True)
        yield host_dir, False
        return
    with tempfile.TemporaryDirectory(prefix="af-sandbox-") as tmp:
        yield Path(tmp), True


def main() -> int:
    client = docker.from_env()

    # Make sure the sandbox image exists locally; pull it on first run.
    try:
        client.images.get(SANDBOX_IMAGE)
    except docker.errors.ImageNotFound:
        print(f"[setup] pulling {SANDBOX_IMAGE} ...")
        client.images.pull(SANDBOX_IMAGE)

    # The host directory outlives the container, so files written through the
    # mount persist here after the sandbox is torn down.
    with host_workspace() as (host_dir, ephemeral):
        label = "ephemeral — wiped on exit" if ephemeral else "persistent — kept for inspection"
        print(f"[host] workspace dir: {host_dir} ({label})")

        # ---- Step 1: mount the host temp folder into a sandbox and start it ----
        print(f"[step 1] mount {host_dir} -> {CONTAINER_MOUNT} and start sandbox")
        container = client.containers.create(
            image=SANDBOX_IMAGE,
            command=["tail", "-f", "/dev/null"],  # keep the container alive between execs
            working_dir=CONTAINER_MOUNT,
            volumes={str(host_dir): {"bind": CONTAINER_MOUNT, "mode": "rw"}},
            # Production hardening — harmless here, but don't ship without these:
            mem_limit="256m",
            pids_limit=128,
            # network_mode="none",  # set this (or a restricted bridge) to control egress
        )
        try:
            container.start()
            print(f"[step 1] sandbox up: {container.short_id}")

            # ---- Step 2: touch file.txt inside the mounted volume ----
            print("[step 2] touch file.txt")
            exec_in(container, ["touch", "file.txt"])

            # ---- Step 3: write "hello world" into file.txt ----
            print('[step 3] echo "hello world" > file.txt')
            exec_in(container, ["sh", "-c", 'echo "hello world" > file.txt'])

            # Read it back from inside the sandbox for a mid-run sanity check.
            inside = exec_in(container, ["cat", "file.txt"])
            print(f"[step 3] read back inside sandbox: {inside.strip()!r}")
        finally:
            # ---- Teardown: stop and remove the sandbox ----
            print("[teardown] stop + remove sandbox")
            try:
                container.stop(timeout=5)
            except docker.errors.APIError:
                pass
            try:
                container.remove(force=True)
            except docker.errors.APIError:
                pass

        # ---- Verify: the file must be visible on the host after teardown ----
        host_file = host_dir / "file.txt"
        if not host_file.exists():
            print(f"[verify] FAILED: {host_file} not found on host", file=sys.stderr)
            return 1
        content = host_file.read_text()
        print(f"[verify] host file: {host_file}")
        print(f"[verify] host content: {content!r}")
        if content.strip() != "hello world":
            print(f"[verify] FAILED: unexpected content {content!r}", file=sys.stderr)
            return 1
        print("[verify] OK — file persisted on host after sandbox teardown")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
