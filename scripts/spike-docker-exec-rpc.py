"""Spike: is bidirectional JSON-RPC over a Docker exec socket stable?

Validates the transport for the DockerBackend skill-process model (one session
container + exec'd JSON-RPC runners) from docs/execution-backend-design.md
(Phase 1 risk).

The server is a POSIX shell script on alpine (no image pull needed). The spike
purpose is to validate the DOCKER TRANSPORT — the hijacked exec socket and the
multiplexed framed stream — not any particular server language. Shell's stdio
buffering differs from Python's, which makes this a stricter transport test.

What it exercises against a live Docker daemon:

  0. ready       startup notification (no id) arrives first
  1. basic       single ping round-trip
  2. sequential  100 pings, framed read-loop over time, with avg latency
  3. idle        ping -> 8s idle -> ping (stream stays open)
  4. big resp    server emits a ~200 KB response line (demux reassembly)
  5. pipelined   5 requests sent before any read; all 5 ids matched back
  6. stderr      stderr captured without breaking the stdout RPC channel
  7. shutdown    clean exit; exec inspect reports exit code 0

The client demultiplexes the Docker framed stream itself — the fiddly part this
spike exists to validate.

Run:  uv run python script/spike-docker-exec-rpc.py
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import time
from pathlib import Path

import docker

IMAGE = "alpine:latest"  # already local — no pull, no network dependency
SERVER_PATH_IN_CONTAINER = "/server/skill_server.sh"

# Newline-delimited JSON-RPC server in POSIX sh (alpine/busybox).
# Mirrors the skill protocol: emits a "ready" notification, responds to
# ping / big / stderr_test / shutdown, logs to stderr without touching stdout.
SKILL_SERVER = r'''#!/bin/sh
emit() { printf '%s\n' "$1"; }
emiterr() { printf '%s\n' "$1" >&2; }

emiterr "skill server starting"
# Precompute a large blob for the "big" method — a single ~200 KB JSON line on
# the RESPONSE path, which is what stresses the client's frame reassembly.
BLOB=$(yes A | head -n 200000 | tr -d '\n')
emit '{"jsonrpc":"2.0","method":"ready","params":{}}'

while IFS= read -r line; do
    [ -z "$line" ] && continue
    id=$(printf '%s' "$line" | sed -n 's/.*"id":[[:space:]]*\([0-9][0-9]*\).*/\1/p')
    case "$line" in
        *'"method":"ping"'*)
            emit "{\"jsonrpc\":\"2.0\",\"id\":$id,\"result\":{\"pong\":true}}"
            ;;
        *'"method":"big"'*)
            emit "{\"jsonrpc\":\"2.0\",\"id\":$id,\"result\":{\"blob\":\"$BLOB\"}}"
            ;;
        *'"method":"stderr_test"'*)
            emiterr "STDERR_MARK for id=$id"
            emit "{\"jsonrpc\":\"2.0\",\"id\":$id,\"result\":{\"logged\":true}}"
            ;;
        *'"method":"shutdown"'*)
            emit "{\"jsonrpc\":\"2.0\",\"id\":$id,\"result\":{\"bye\":true}}"
            exit 0
            ;;
        *)
            emit "{\"jsonrpc\":\"2.0\",\"id\":$id,\"error\":{\"code\":-32601,\"message\":\"unknown\"}}"
            ;;
    esac
done
emiterr "stdin eof"
'''


# --------------------------------------------------------------------------- #
# Docker exec channel: demultiplexes the framed stream into stdout lines.
# --------------------------------------------------------------------------- #
class DockerExecChannel:
    """Bidirectional channel over a hijacked exec socket.

    Output (container -> us) is multiplexed when tty=False: every chunk is
    preceded by an 8-byte header [stream_type, 0, 0, 0, length_be32]. We
    reassemble stdout into a buffer and expose readline(); stderr is captured
    inline so it never interleaves with the RPC channel. Input (us -> container
    stdin) is written raw, no framing.
    """

    _HEADER = struct.Struct(">BxxxI")
    STDOUT, STDERR, STDIN = 1, 2, 0

    def __init__(self, sock, *, timeout: float = 30.0) -> None:
        # exec_start(socket=True) returns a socket.SocketIO wrapper; unwrap to
        # the underlying socket so recv/sendall/settimeout are available.
        self._sock = getattr(sock, "_sock", sock)
        self._sock.settimeout(timeout)
        self._out = bytearray()
        self.stderr_lines: list[str] = []

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("docker exec socket closed mid-frame")
            buf.extend(chunk)
        return bytes(buf)

    def _pump_one_frame(self) -> None:
        header = self._recv_exact(8)
        stream_type, length = self._HEADER.unpack(header)
        payload = self._recv_exact(length) if length else b""
        if stream_type == self.STDOUT:
            self._out.extend(payload)
        elif stream_type == self.STDERR:
            text = payload.decode("utf-8", "replace")
            self.stderr_lines.append(text)
            for ln in text.splitlines():
                print(f"  [container:stderr] {ln}", file=sys.stderr)
        # STDIN frames are not expected on the return path.

    def readline(self, *, timeout: float | None = None) -> bytes:
        old = self._sock.gettimeout()
        if timeout is not None:
            self._sock.settimeout(timeout)
        try:
            while b"\n" not in self._out:
                self._pump_one_frame()
        finally:
            self._sock.settimeout(old)
        line, _, self._out = self._out.partition(b"\n")
        return bytes(line)

    def send(self, obj: dict) -> None:
        # Compact JSON (no spaces) — the shell server's case patterns match
        # `"method":"ping"` literally, so avoid json.dumps' default ", "/": ".
        blob = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._sock.sendall(blob.encode("utf-8"))

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Tiny JSON-RPC client helpers
# --------------------------------------------------------------------------- #
class _IdGen:
    def __init__(self) -> None:
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


def read_msg(ch: DockerExecChannel, *, timeout: float | None = None) -> dict:
    line = ch.readline(timeout=timeout)
    if not line:
        raise RuntimeError("server returned an empty line")
    return json.loads(line.decode("utf-8"))


def request(ch: DockerExecChannel, ids: _IdGen, method: str, params: dict | None = None) -> int:
    rid = ids.next()
    ch.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
    return rid


def expect(ch: DockerExecChannel, want_id: int, *, timeout: float | None = None) -> dict:
    """Read until the response with `want_id` arrives, skipping notifications."""
    while True:
        msg = read_msg(ch, timeout=timeout)
        if "id" not in msg:  # notification
            continue
        if msg["id"] == want_id:
            return msg
        raise RuntimeError(f"unexpected response id={msg['id']} while waiting for id={want_id}: {msg}")


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def main() -> int:
    client = docker.from_env()
    api = client.api

    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    try:
        client.images.get(IMAGE)
    except docker.errors.ImageNotFound:
        print(f"[setup] pulling {IMAGE} ...")
        client.images.pull(IMAGE)

    with tempfile.TemporaryDirectory(prefix="af-spike-") as host_dir_str:
        host_dir = Path(host_dir_str)
        (host_dir / "skill_server.sh").write_text(SKILL_SERVER, encoding="utf-8")

        print(f"[setup] starting {IMAGE} container, mounting {host_dir} -> /server")
        container = client.containers.create(
            image=IMAGE,
            command=["tail", "-f", "/dev/null"],
            volumes={str(host_dir): {"bind": "/server", "mode": "rw"}},
            detach=True,
        )
        exec_id: str | None = None
        ch: DockerExecChannel | None = None
        try:
            container.start()
            print(f"[setup] container up: {container.short_id}")

            exec_id = api.exec_create(
                container.id,
                cmd=["sh", SERVER_PATH_IN_CONTAINER],
                stdin=True,
                stdout=True,
                stderr=True,
                tty=False,
            )["Id"]
            sock = api.exec_start(exec_id, socket=True)
            ch = DockerExecChannel(sock)
            print(f"[setup] exec hijacked socket open: exec={exec_id[:12]}")

            ids = _IdGen()

            # 0. ready notification arrives first --------------------------------
            try:
                first = read_msg(ch, timeout=15.0)
                record("0 ready notification",
                       first.get("method") == "ready" and "id" not in first,
                       f"method={first.get('method')}")
            except Exception as e:
                record("0 ready notification", False, repr(e))

            # 1. basic round-trip ------------------------------------------------
            try:
                rid = request(ch, ids, "ping")
                msg = expect(ch, rid, timeout=15.0)
                record("1 basic ping", msg.get("result", {}).get("pong") is True)
            except Exception as e:
                record("1 basic ping", False, repr(e))

            # 2. sequential 100, with latency -----------------------------------
            try:
                t0 = time.monotonic()
                ok_count = 0
                for _ in range(100):
                    rid = request(ch, ids, "ping")
                    msg = expect(ch, rid, timeout=15.0)
                    if msg.get("result", {}).get("pong") is True:
                        ok_count += 1
                elapsed = time.monotonic() - t0
                avg_ms = (elapsed / 100) * 1000
                record("2 sequential x100", ok_count == 100,
                       f"{ok_count}/100 ok, avg {avg_ms:.1f} ms/rt")
            except Exception as e:
                record("2 sequential x100", False, repr(e))

            # 3. idle then resume ------------------------------------------------
            try:
                rid_a = request(ch, ids, "ping")
                expect(ch, rid_a, timeout=15.0)
                time.sleep(8.0)
                rid_b = request(ch, ids, "ping")
                msg_b = expect(ch, rid_b, timeout=15.0)
                record("3 idle 8s then resume", msg_b.get("result", {}).get("pong") is True)
            except Exception as e:
                record("3 idle 8s then resume", False, repr(e))

            # 4. large RESPONSE (~200 KB) — demux reassembly --------------------
            try:
                rid = request(ch, ids, "big")
                msg = expect(ch, rid, timeout=30.0)
                blob_len = len(msg.get("result", {}).get("blob", ""))
                record("4 large response 200KB", blob_len == 200000, f"got {blob_len} bytes")
            except Exception as e:
                record("4 large response 200KB", False, repr(e))

            # 5. pipelined: 5 requests before any read --------------------------
            try:
                sent = [request(ch, ids, "ping") for _ in range(5)]
                got_ids: set[int] = set()
                for _ in range(5):
                    msg = read_msg(ch, timeout=20.0)
                    if "id" in msg and msg.get("result", {}).get("pong") is True:
                        got_ids.add(msg["id"])
                record("5 pipelined x5", got_ids == set(sent),
                       f"matched {len(got_ids & set(sent))}/5")
            except Exception as e:
                record("5 pipelined x5", False, repr(e))

            # 6. stderr isolation ------------------------------------------------
            stderr_before = len(ch.stderr_lines)
            try:
                rid = request(ch, ids, "stderr_test")
                msg = expect(ch, rid, timeout=15.0)
                new_stderr = ch.stderr_lines[stderr_before:]
                has_mark = any("STDERR_MARK" in s for s in new_stderr)
                record("6 stderr isolation",
                       msg.get("result", {}).get("logged") is True and has_mark,
                       f"response ok + {'stderr captured' if has_mark else 'NO stderr captured'}")
            except Exception as e:
                record("6 stderr isolation", False, repr(e))

            # 7. shutdown + clean exit code -------------------------------------
            try:
                rid = request(ch, ids, "shutdown")
                msg = expect(ch, rid, timeout=15.0)
                bye_ok = msg.get("result", {}).get("bye") is True
                time.sleep(0.5)
                exit_code = api.exec_inspect(exec_id).get("ExitCode")
                record("7 shutdown + exit code", bye_ok and exit_code == 0,
                       f"exit_code={exit_code}")
            except Exception as e:
                record("7 shutdown + exit code", False, repr(e))

        finally:
            if ch is not None:
                ch.close()
            print("[teardown] stop + remove container")
            try:
                container.stop(timeout=5)
            except docker.errors.APIError:
                pass
            try:
                container.remove(force=True)
            except docker.errors.APIError:
                pass

    # ----------------------------------------------------------------------- #
    print("\n=== SUMMARY ===")
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  ({detail})" if detail else ""))
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
