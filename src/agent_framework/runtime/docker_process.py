"""Docker exec process adapter — makes a hijacked exec socket quack like
``asyncio.subprocess.Process``.

``SkillProcessManager`` drives skill runners through ``process.stdin.write``/
``drain``, ``await process.stdout.readline()``, a separate ``_stderr_logger``
reading ``process.stderr.readline()``, and ``returncode``/``terminate()``/
``kill()``/``wait()``. This adapter exposes exactly that surface over a Docker
exec hijacked socket so ``SkillProcessHandle`` needs no changes.

Transport notes (validated by ``script/spike-docker-exec-rpc.py``):
- Output is multiplexed when ``tty=False``: each chunk is preceded by an 8-byte
  header ``[stream_type, 0, 0, 0, length_be32]``. ``readline()`` demuxes frames,
  reassembling stdout into a line buffer and logging stderr inline.
- Input is written raw (no framing) onto the socket.
- ``readline()`` returns ``b""`` on socket close (EOF) so the read loop exits
  cleanly; ``returncode``/``wait()`` are resolved via ``exec_inspect``.

The socket is used in non-blocking mode with ``loop.sock_recv`` /
``loop.sock_sendall`` so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Callable

logger = logging.getLogger(__name__)

_HEADER = struct.Struct(">BxxxI")
_STREAM_STDOUT = 1
_STREAM_STDERR = 2


class _StdinWriter:
    """asyncio ``StreamWriter``-like: ``write`` buffers, ``drain`` sends."""

    def __init__(self, proc: "DockerExecProcess") -> None:
        self._proc = proc
        self._buf = bytearray()

    def write(self, data: bytes) -> None:
        self._buf.extend(data)

    async def drain(self) -> None:
        if not self._buf:
            return
        await self._proc._loop.sock_sendall(self._proc._sock, bytes(self._buf))
        self._buf.clear()


class _StdoutReader:
    """asyncio ``StreamReader``-like: ``readline`` demuxes and returns stdout."""

    def __init__(self, proc: "DockerExecProcess") -> None:
        self._proc = proc

    async def readline(self) -> bytes:
        return await self._proc._read_stdout_line()


class _StderrReader:
    """Stub: always returns EOF. Stderr is captured/logged by the stdout demux
    pump, so ``SkillProcessHandle._stderr_logger`` (which reads
    ``process.stderr.readline()``) exits immediately without touching the socket."""

    async def readline(self) -> bytes:
        return b""


class DockerExecProcess:
    """A Docker exec session presented as an ``asyncio.subprocess.Process``."""

    def __init__(
        self,
        sock,
        *,
        exit_code_probe: Callable[[], int | None],
        log_stderr: Callable[[str], None] | None = None,
        kill_probe: Callable[[str], None] | None = None,
    ) -> None:
        self._sock = sock
        self._sock.setblocking(False)
        self._loop = asyncio.get_running_loop()
        self._stdout_buf = bytearray()
        self._returncode: int | None = None
        self._closed = False
        self._exit_code_probe = exit_code_probe
        self._kill_probe = kill_probe
        self._log_stderr = log_stderr or (lambda text: logger.info("[sandbox:stderr] %s", text.strip()))

        self.stdin = _StdinWriter(self)
        self.stdout = _StdoutReader(self)
        self.stderr = _StderrReader()

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = await self._loop.sock_recv(self._sock, n - len(buf))
            if not chunk:
                raise ConnectionError("docker exec socket closed")
            buf.extend(chunk)
        return bytes(buf)

    async def _read_stdout_line(self) -> bytes:
        """Demux frames until a stdout newline is available; ``b""`` on EOF."""
        while b"\n" not in self._stdout_buf:
            try:
                header = await self._recv_exact(8)
            except ConnectionError:
                self._mark_closed()
                return b""
            stream_type, length = _HEADER.unpack(header)
            payload = await self._recv_exact(length) if length else b""
            if stream_type == _STREAM_STDOUT:
                self._stdout_buf.extend(payload)
            elif stream_type == _STREAM_STDERR:
                self._log_stderr(payload.decode("utf-8", "replace"))
            # stream_type 0 (stdin) is not expected on the return path.
        line, _, self._stdout_buf = self._stdout_buf.partition(b"\n")
        return bytes(line)

    def _mark_closed(self) -> None:
        if self._closed:
            return
        self._closed = True
        code = None
        try:
            code = self._exit_code_probe()
        except Exception:
            code = None
        self._returncode = code if code is not None else 0

    def terminate(self) -> None:
        """Best-effort SIGTERM via in-container ``kill <pid>``, then close the socket."""
        self._signal("TERM")
        self._close_socket()

    def kill(self) -> None:
        """Best-effort SIGKILL via in-container ``kill <pid>``, then close the socket."""
        self._signal("KILL")
        self._close_socket()

    def _signal(self, signal_name: str) -> None:
        probe = self._kill_probe
        if probe is None:
            return
        try:
            probe(signal_name)
        except Exception:
            pass

    def _close_socket(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        self._closed = True

    async def wait(self) -> int:
        """Resolve and return the exec exit code (polls ``exec_inspect``)."""
        while self._returncode is None:
            code = None
            try:
                code = self._exit_code_probe()
            except Exception:
                code = None
            if code is not None:
                self._returncode = code
                break
            await asyncio.sleep(0.2)
        return self._returncode
