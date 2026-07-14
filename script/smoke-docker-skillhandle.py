"""One-off smoke: drive a REAL SkillProcessHandle over a REAL Docker exec via
DockerBackend, against the locally-cached alpine:latest image (no package
mirror needed — the "runner" is a POSIX sh JSON-RPC server).

This closes the one gap the unit tests can't: SkillProcessHandle <-> DockerExecProcess
<-> real docker exec end-to-end. Run:

    uv run python script/smoke-docker-skillhandle.py
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path

from agent_framework.infra.settings import AppSettings
from agent_framework.runtime.docker_backend import DockerBackend
from agent_framework.skills.process import SkillProcessHandle
from agent_framework.skills.spec import ManifestSkillSpec

# Space-tolerant sh JSON-RPC server: SkillProcessHandle.send_request emits
# json.dumps with spaces (e.g. "method": "ping"), so we extract id/method via
# sed that tolerates optional whitespace around the colon.
SERVER = r'''#!/bin/sh
emit() { printf '%s\n' "$1"; }
emiterr() { printf '%s\n' "$1" >&2; }
emiterr "skill server starting"
emit '{"jsonrpc":"2.0","method":"ready","params":{}}'
while IFS= read -r line; do
    [ -z "$line" ] && continue
    id=$(printf '%s' "$line" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p')
    method=$(printf '%s' "$line" | sed -n 's/.*"method"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
    case "$method" in
        ping) emit "{\"jsonrpc\":\"2.0\",\"id\":$id,\"result\":{\"pong\":true}}" ;;
        shutdown) emit "{\"jsonrpc\":\"2.0\",\"id\":$id,\"result\":{\"bye\":true}}"; exit 0 ;;
        *) emit "{\"jsonrpc\":\"2.0\",\"id\":$id,\"error\":{\"code\":-32601,\"message\":\"unknown $method\"}}" ;;
    esac
done
'''


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="af-smoke-"))
    server_dir = tmp / "skill"
    server_dir.mkdir()
    (server_dir / "server.sh").write_text(SERVER)

    settings = AppSettings(
        workspace_root_dir=str(tmp / "ws"),
        execution_backend_docker_image="alpine:latest",  # locally cached, no apk needed
    )
    backend = DockerBackend(settings, skill_source_dirs_provider=lambda: [str(server_dir)])
    session_id = "smoke-" + uuid.uuid4().hex[:8]
    spec = ManifestSkillSpec(name="smoke", description="end-to-end smoke")

    server_path = str(server_dir / "server.sh")
    proc = await backend.spawn_stream(
        ["sh", server_path],
        cwd=str(server_dir),
        env={},
        session_id=session_id,
    )
    handle = SkillProcessHandle(spec=spec, process=proc)
    handle._reader_task = asyncio.create_task(handle._read_loop())
    try:
        await asyncio.wait_for(handle._ready.wait(), timeout=30.0)
        print("[smoke] ready notification received")
        ping = await handle.send_request("ping")
        assert ping["pong"] is True, ping
        print(f"[smoke] ping ok -> {ping}")
        bye = await handle.send_request("shutdown")
        assert bye["bye"] is True, bye
        print(f"[smoke] shutdown ok -> {bye}")
        code = await asyncio.wait_for(proc.wait(), timeout=10.0)
        print(f"[smoke] exec exit code = {code}")
        assert code == 0
    finally:
        handle._reader_task.cancel()
        await backend.aclose()
    print("[smoke] OK — SkillProcessHandle drove a real Docker exec end-to-end")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
