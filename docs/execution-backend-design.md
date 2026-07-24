# Execution Backend Design

This document specifies a **pluggable execution backend** for agent sessions:
an abstraction that decides *where* a session's skill code, scripts, and
workspace live and run. Three backends share one interface:

- **FileSystem** — today's behavior: in-process execution on the host (default,
  zero isolation overhead).
- **Docker** — one sandbox container per session; the flagship isolation backend
  for single-host deployments.
- **Kubernetes** — one Pod per session; the cloud/multi-host backend.

A deployment selects a backend via configuration; the framework code above the
backend is identical in all three cases. The Docker backend is verified against
a live daemon via `script/sandbox-testing.py` (Docker Engine 28.5.1, docker-py
7.2.0 — see [Appendix](#appendix-proven-proof-of-concept)).

## Table of Contents

- [Motivation](#motivation)
- [Goals and Non-Goals](#goals-and-non-goals)
- [Current State (Baseline)](#current-state-baseline)
- [The Pluggable Backend Architecture](#the-pluggable-backend-architecture)
- [Backend Implementations](#backend-implementations)
- [Workspace Access (the portability seam)](#workspace-access-the-portability-seam)
- [Process Execution Across Backends](#process-execution-across-backends)
- [Lifecycle and Resource Management](#lifecycle-and-resource-management)
- [Security Model](#security-model)
- [Configuration](#configuration)
- [Behavior Changes and Compatibility](#behavior-changes-and-compatibility)
- [Phased Rollout](#phased-rollout)
- [Testing Strategy](#testing-strategy)
- [Risks and Open Decisions](#risks-and-open-decisions)
- [Appendix: Proven Proof of Concept](#appendix-proven-proof-of-concept)

## Motivation

The framework executes agent-invoked code (skill runtimes, ad-hoc scripts) and
manages a per-session workspace. Today this all happens directly in the backend
host process. We want three things, and we want them **without forcing every
deployment into one model**:

1. **Higher agent autonomy** — widen what an agent may do, contained by an
   OS-level boundary rather than cooperative policy.
2. **Concurrency safety** — hard filesystem isolation between sessions.
3. **Host data safety** — sandboxed code cannot reach the host filesystem or
   exfiltrate over the network.

Different deployments want different points on the isolation/operational-cost
trade-off:

- A developer laptop or a trusted single-host deploy may prefer the zero-cost
  **FileSystem** backend and opt into **Docker** when running untrusted skills.
- A cloud deployment wants **Kubernetes** Pods so isolation rides on existing
  cluster scheduling, network policy, and resource management.

One pluggable abstraction serves all three.

### Threat model

| Code origin | Trust | Backends that should run it |
|-------------|-------|------------------------------|
| Framework + runners (`python_runner.py`, builtin tools) | Trusted | Any |
| Authored / built-in skills | Semi-trusted | Any; Docker/K8s recommended |
| Uploaded / git-synced skills, LLM-authored scripts | **Untrusted** | Docker or Kubernetes only |

FileSystem is acceptable only when all code is trusted. The sandbox backends
exist to contain the untrusted row. None of the three defends against a
privileged attacker controlling the host/daemon/kernel; for that, layer gVisor /
Kata / Firecracker (see [Open Decisions](#risks-and-open-decisions)).

## Goals and Non-Goals

### Goals

- A single `ExecutionBackend` interface implemented by FileSystem, Docker, and
  Kubernetes; selection is deployment-time configuration.
- Framework code (agent loop, model provider, tool registry, workspace tools,
  skill process manager) is **backend-agnostic** — no `if docker:` branches.
- Each sandbox backend gives a session its own filesystem, process tree, network
  namespace, and resource budget.
- FileSystem behavior is byte-for-byte the current behavior (zero regression,
  zero migration) — it is the default and the fallback.
- Docker and Kubernetes enforce network egress, resource ceilings, and clean
  teardown by default.

### Non-Goals

- Strong isolation against kernel exploits / malicious daemon (future track).
- Per-session or per-agent backend selection at runtime (deployment-level only
  for now; per-agent override noted as future).
- Sandboxing the model provider or MCP client connection (trusted, stay on host).
- Removing the cooperative skill permission model — retained as inner fence.

## Current State (Baseline)

What exists today is, in effect, a **hardcoded FileSystem backend**. Naming it
makes the migration a refactor rather than a rewrite:

- **Workspace tools** (`core/workspace_tools.py`, ~15 tools) run in-process via
  `pathlib`/`shutil`, rooted at `_get_session_workspace_root(settings, ctx)` →
  `settings.session_workspace_dir(session_id)`. Containment is the path-prefix
  jail in `_resolve_workspace_path()` (plus an absolute-path→relative shim).
- **Skills** (`skills/process.py`) spawn host subprocesses via
  `SkillProcessManager._spawn` → `asyncio.create_subprocess_exec`, JSON-RPC over
  stdio, pooled with health checks and idle eviction.
- **Scripts** (`run_skill_script`) one-shot `asyncio.create_subprocess_exec`.
- **Permissions** (`skills/permissions.py`) are cooperative: `PermissionChecker`
  injects `SKILL_FS_*` / `SKILL_NET_*` / `SKILL_ALLOW_SUBPROCESS`, honored by the
  SDK interceptors.

Honest limitations: no OS isolation; SDK bypass possible; sessions share one
root partitioned by convention.

## The Pluggable Backend Architecture

A backend owns three responsibilities for a session: (1) a place for code to
run, (2) a way to reach the workspace files, and (3) a lifecycle.

```python
# src/agent_framework/runtime/backend.py (proposed)

from typing import Protocol

class ExecutionBackend(Protocol):
    """Where a session's code runs and where its workspace lives."""

    name: str                                   # "filesystem" | "docker" | "kubernetes"

    async def ensure(self, session_id: str) -> SessionHandle:
        """Make the session's environment ready. Idempotent."""

    async def spawn_stream(self, h: SessionHandle, command: list[str], *,
                           env: dict[str, str], workdir: str,
                           timeout: float | None) -> ProcessStream:
        """Long-lived bidirectional stdio process — for skill JSON-RPC runners."""

    async def exec(self, h: SessionHandle, command: list[str], *,
                   env: dict[str, str], workdir: str, timeout: float | None,
                   stdin: bytes | None = None) -> ExecResult:
        """One-shot command — for ad-hoc scripts."""

    async def workspace(self, h: SessionHandle) -> WorkspaceAccess:
        """How the workspace_* tools reach this session's files."""

    async def put_file(self, h: SessionHandle, host_path: Path, dest: str) -> None: ...
    async def get_file(self, h: SessionHandle, src: str, host_path: Path) -> None: ...

    async def is_alive(self, h: SessionHandle) -> bool: ...
    async def stop(self, h: SessionHandle, *, timeout: float = 10.0) -> None: ...

    async def list_orphans(self) -> list[SessionHandle]:
        """Backends the reaper can sweep on startup/GC."""
```

`ProcessStream` exposes async `read()`/`write()`/`close()` — the existing
`SkillProcessHandle._read_loop` operates on this abstraction unchanged whether
the transport is a local pipe, a Docker exec socket, or a Kubernetes exec
stream.

```
                  ExecutionBackend  (Protocol)
                         ▲
          ┌──────────────┼──────────────┐
          │              │              │
   FileSystemBackend  DockerBackend  KubernetesBackend
   (= today's code)   (SandboxClient   (Pod per session,
                       + image)         k8s API exec)
```

A factory wires the configured backend once at startup:

```python
def make_backend(settings: AppSettings) -> ExecutionBackend:
    match settings.execution_backend.kind:
        case "filesystem":  return FileSystemBackend(settings)
        case "docker":      return DockerBackend(settings)      # wraps a docker client
        case "kubernetes":  return KubernetesBackend(settings)  # wraps a k8s client
```

The previously-proposed `SandboxClient` is absorbed into `DockerBackend` as its
internal Docker API wrapper — same role, now one implementation behind the seam.

## Backend Implementations

### FileSystemBackend (default, = today)

- `ensure()` → no-op (returns a handle carrying `session_workspace_dir(id)`).
- `spawn_stream()` / `exec()` → wrap `asyncio.create_subprocess_exec` (the
  current code, extracted verbatim).
- `workspace()` → `HostPathWorkspace(host_path=session_workspace_dir(id))`.
- No isolation. Zero behavior change from today. This is the baseline every test
  must keep passing against.

### DockerBackend (flagship single-host sandbox)

- `ensure()` → create+start a container (idempotent on session id), image
  `covalent-sandbox:<tag>`, `command=["tail","-f","/dev/null"]`, the session
  workspace bind-mounted to `/workspace`, labels `covalent.sandbox=1` /
  `covalent.session=<id>`, resource budgets applied.
- `spawn_stream()` → Docker exec with an attached socket (JSON-RPC over exec).
- `exec()` → `container.exec_run(...)`.
- `workspace()` → `HostPathWorkspace(host_path=<bind-mounted host dir>)`. Because
  the bind mount is visible on the host, **the workspace tools need no change**.
- `put_file`/`get_file` → `docker cp` / archive APIs.
- `stop()` → `container.stop()` + `container.remove(force=True)`.

Verified primitives: see [Appendix](#appendix-proven-proof-of-concept).

### KubernetesBackend (cloud / multi-host)

- `ensure()` → create a Pod per session (labels `covalent.dev/sandbox=1`,
  `covalent.dev/session=<id>`), image `covalent-sandbox:<tag>`, command keeping
  it alive, a workspace volume (`emptyDir` for ephemeral, `PVC` for durable),
  resource `requests`/`limits`, a restricted `NetworkPolicy`.
- `spawn_stream()` → Kubernetes **exec** over the API (`stream.WebSocketPort`
  / SPDY), bidirectional.
- `exec()` → one-shot k8s exec.
- `workspace()` → **`RemoteWorkspace`** (the pod volume is not visible on the
  backend host), so tools reach files via `exec`/`get_file`/`put_file`. This is
  why this backend depends on the [workspace refactor](#workspace-access-the-portability-seam).
- `stop()` → delete the Pod (+ optional PVC retention snapshot).

### Comparison

| | FileSystem | Docker | Kubernetes |
|---|---|---|---|
| Isolation | None (host) | Container (shared kernel) | Pod (shared kernel + cluster policy) |
| Workspace reach | Host path | Host path (bind mount) | Remote (pod volume) |
| Workspace tool changes | None | None (Phase 1) | Requires `WorkspaceAccess` refactor |
| Network policy | N/A | `--network none` / restricted bridge | `NetworkPolicy` + egress |
| Resource limits | None | `mem/pids/cpu` knobs | Pod `resources.limits` |
| Orphan reaper | N/A | `docker ps --filter label` | `kubectl get pods -l` |
| Best for | Trusted, dev, fallback | Single-host untrusted | Cloud, multi-host, scale |

## Workspace Access (the portability seam)

This is the crux of making the backend pluggable without forking the 15
workspace tools. The key insight:

> **FileSystem and Docker(bind-mount) both expose a host-visible directory.**
> Only Kubernetes (and an optional mount-less Docker mode) does not.

`WorkspaceAccess` has two implementations:

```python
class WorkspaceAccess(Protocol):
    host_path: Path | None        # None ⇒ remote; tools must call methods

class HostPathWorkspace:          # FS + Docker(bind-mount)
    host_path: Path               # tools use pathlib on this — current code, unchanged

class RemoteWorkspace:            # Kubernetes
    host_path = None
    # implements read/write/list/search/... via backend.exec / get_file / put_file
```

This yields a tractable, staged migration:

- **Phase 0–1 (FS + Docker):** change exactly one function —
  `_get_session_workspace_root()` consults the active backend's
  `WorkspaceAccess.host_path` instead of `settings.session_workspace_dir()`
  directly. All 15 tools work unchanged because both backends return a real
  `Path`. **Switching a single-host deployment between FileSystem and Docker is
  a config flip with zero tool-code change.**
- **Phase 2 (workspace refactor):** route the tools through `WorkspaceAccess`
  methods so they also work when `host_path is None`. For FS/Docker this still
  resolves to pathlib under the hood (no performance loss). This unblocks…
- **Phase 3 (Kubernetes):** now possible because the tools are backend-agnostic.

## Process Execution Across Backends

The skill and script paths collapse to backend calls:

- `SkillProcessManager._spawn` today hardcodes
  `asyncio.create_subprocess_exec`. After: it calls
  `backend.spawn_stream(handle, command, env=..., workdir=...)` and wraps the
  returned `ProcessStream` in the **unchanged** `SkillProcessHandle`. The read
  loop, request/response dispatch, health checks, semaphores, and idle eviction
  all operate on the abstract stream. For FileSystem, `spawn_stream` *is* the
  extracted `create_subprocess_exec` — so existing skill behavior is preserved
  exactly.
- `run_skill_script` today one-shots `create_subprocess_exec`. After:
  `backend.exec(handle, command, timeout=...)`. One line.

The transport matrix:

| | `spawn_stream` transport | `exec` transport |
|---|---|---|
| FileSystem | local pipe (`create_subprocess_exec`) | local pipe |
| Docker | Docker exec attached socket | `container.exec_run` |
| Kubernetes | k8s API exec (SPDY/WS) | k8s API exec |

**Validated by spike** (`script/spike-docker-exec-rpc.py`, 8/8 checks pass): a
long-lived JSON-RPC runner over a Docker exec hijacked socket is stable. Measured
~1.5 ms per request/response (including our own demux) when reusing one exec
session — far below the per-`exec_run` cold-start cost, because the runner stays
attached and we drive the socket directly. Three implementation facts the
`DockerBackend.spawn_stream` must encode:

- `exec_start(socket=True)` returns a `socket.SocketIO` wrapper, not a raw
  socket — unwrap `_sock` for `recv` / `sendall` / `settimeout` (or use
  `readinto` / `write`).
- With `tty=False`, output is multiplexed: each chunk is preceded by an 8-byte
  header `[stream_type, 0, 0, 0, length_be32]`. The backend must demultiplex
  (stdout → RPC buffer, stderr → logs) — the fiddly part, now proven against a
  200 KB response reassembled across many frames.
- Input is written raw (no framing) onto the socket.

So the "one container per warm skill" `attach` fallback is **not needed** for
Docker. The same bidirectional-exec question still applies to **k8s exec**
(Phase 3) and remains to be spiked there.

## Lifecycle and Resource Management

Generalized across backends via the `list_orphans()` method:

- **Lazy start:** `backend.ensure(session_id)` on the session's first execution.
- **Stop:** on session end → `backend.stop(handle)`.
- **Startup sweep:** on backend boot, `backend.list_orphans()` returns sandbox
  resources whose session is no longer live; the reaper removes them. Docker
  filters by label; Kubernetes selects pods by label + checks against the live
  session store.
- **Periodic GC:** backstop task reaping sandboxes whose session was evicted.

All sandbox resources carry consistent labels (`covalent.*` /
`covalent.dev.*`) so the same reaper logic names them uniformly.

### Resource budgets

| Dimension | Docker | Kubernetes |
|-----------|--------|------------|
| Memory | `mem_limit` | `resources.limits.memory` |
| Process count | `pids_limit` | `resources.limits` (+ pod PIDs) |
| CPU | `nano_cpus` | `resources.limits.cpu` |
| `/tmp` | `tmpfs` | `emptyDir` with `sizeLimit` |
| Workspace disk | session dir / sized volume | `PVC` / `emptyDir.sizeLimit` |
| Wall-clock | `exec(timeout=...)` | exec timeout |
| Network | restricted bridge / `none` | `NetworkPolicy` |

Defaults (`ResourceBudget.default()`) are conservative; operators override per
deployment. Without these, one runaway agent can starve the host/cluster.

## Security Model

The sandbox backends turn cooperative policy into hard OS/cluster boundaries.
FileSystem provides none of this — which is why it is for trusted code only.

### Network egress — the primary host-safety lever

Because the **model provider runs in the backend, not in the sandbox**, most
skills need no network. Mapping onto the existing skill `permissions.network`:

| Skill declaration | Docker | Kubernetes |
|-------------------|--------|------------|
| `network.allow_outbound: []` (default) | `--network none` | default-deny `NetworkPolicy` |
| `network.allow_outbound: [...]` | restricted bridge + egress allow-list | egress `NetworkPolicy` to listed hosts |

This is strictly stronger than today's SDK-intercepted `SKILL_NET_ALLOW`, which
non-cooperating code can ignore.

### Control-plane hardening

- **Docker:** the backend needs the Docker socket (root-equivalent). Prod must
  front it with a restricting socket-proxy (only `containers.create/exec/start/
  stop/remove/logs`) and run the backend containerized against that proxy. Never
  expose the socket to sandboxed code.
- **Kubernetes:** the backend uses a ServiceAccount bound by **RBAC** to just the
  verbs it needs (`pods`, `pods/exec`) in a namespace; **Pod Security Admission**
  (restricted) forbids privileged pods; `NetworkPolicy` + (if available) a
  service mesh for egress control.

### Image hygiene

`Dockerfile.sandbox` bakes the trusted base (Python + Node + the runners at a
known path). Same image is pushed to the cluster registry for Kubernetes. Pin a
digest; rebuild on runner/dependency change via CI; runtime `pip install` lands
in an ephemeral layer, never the base.

### Defense in depth retained

Cooperative skill permissions stay — inside the sandbox, skills still receive
`SKILL_FS_*` / `SKILL_NET_*` and should honor them. The container/pod is the
hard outer wall; the SDK policy the inner fence.

## Configuration

A new settings block selects and configures the backend:

```python
# infra/settings.py (proposed addition)

class ExecutionBackendSettings:
    kind: Literal["filesystem", "docker", "kubernetes"] = "filesystem"
    docker: DockerBackendSettings | None = None
    kubernetes: KubernetesBackendSettings | None = None

class DockerBackendSettings:
    image: str = "covalent-sandbox:latest"
    network_profile: Literal["none", "restricted"] = "none"
    mem_limit: str = "512m"
    pids_limit: int = 256
    cpus: float = 1.0
    tmpfs_size: str = "128m"
    socket_url: str | None = None          # None ⇒ default unix socket / proxy

class KubernetesBackendSettings:
    image: str = "covalent-sandbox:latest"
    namespace: str = "covalent-sandboxes"
    service_account: str = "covalent-sandbox"
    network_policy: Literal["none", "default-deny"] = "default-deny"
    # resource limits mirror DockerBackendSettings
```

```bash
# .env example
AGENT_FRAMEWORK_EXECUTION_BACKEND__KIND=docker
AGENT_FRAMEWORK_EXECUTION_BACKEND__DOCKER__IMAGE=covalent-sandbox:0.1.0
AGENT_FRAMEWORK_EXECUTION_BACKEND__DOCKER__NETWORK_PROFILE=restricted
```

`make_backend(settings)` validates that the configured backend's dependencies
are present (docker daemon reachable; kubeconfig/context valid) and fails fast
with a clear error otherwise. FileSystem needs nothing and is always available,
so it doubles as the automatic degradation target when a sandbox backend is
unreachable (configurable: strict-fail vs fallback).

## Behavior Changes and Compatibility

- **No change under FileSystem.** It is the default and reproduces today exactly.
- **Absolute-path translation** (`_resolve_workspace_path` mapping `/tmp/x` →
  `tmp/x`) becomes unnecessary inside a real sandbox (`/tmp/x` is genuinely
  inside the container/pod). Dropped for sandbox backends; retained for
  FileSystem. Mitigation: stable mount path (`/workspace`) + optional compat flag.
- **Persistence:** with Docker bind-mount, files survive teardown (verified). For
  Kubernetes (or mount-less Docker), `stop()` must extract artifacts
  (`get_file`) before deletion — notably for the `publish_downloadable_file`
  download flow.
- **Sync/async bridge:** workspace handlers are sync; Phase 2's `WorkspaceAccess`
  methods (needed for remote backends) are async. Handlers should become `async`
  (the registry already supports async dispatch) or bridge via
  `run_coroutine_threadsafe` — designed before Phase 2, not during.

## Phased Rollout

| Phase | Scope | Outcome |
|-------|-------|---------|
| **0 — Seam + FS** | Define `ExecutionBackend` protocol + `make_backend` factory + `FileSystemBackend` (= extracted `asyncio.create_subprocess_exec`). Route `SkillProcessManager._spawn` through `backend.spawn_stream`. Add `execution_backend_kind` config. Wire as `app.state.execution_backend`. Workspace tools untouched (deferred via bind-mount — FS and Docker share the same host path). **Behavior unchanged.** | All existing tests pass (59/59, incl. workspace tool + public invoke regressions); seam exercised by `FileSystemBackend.spawn_stream` unit test. |
| **1 — Docker backend** | Split into **1a (done)** and **1b (done)**. **1a**: `DockerBackend` + `DockerExecProcess` (asyncio-subprocess look-alike over a hijacked exec socket; `SkillProcessHandle` unchanged), `Dockerfile.sandbox`, `session_id` threaded through `acquire`/`_spawn` (pool keyed per `(skill, session)`), `run_skill_script` via `backend.exec`, lazy per-session container bind-mounting skill source dirs + workspace at host-absolute paths, command rewrite (`sys.executable`→`python`, runner path→`/runners/`). **1b**: resource limits (mem/pids/cpu) + `tmpfs /tmp` + `network_mode=none` default; per-session teardown on session DELETE; `startup_sweep` + lifespan reaper (reconciles against the session store); Docker `exec` with `stdin` via a hijacked-socket one-shot. Verified: 76 tests green (FS unchanged) + real-container smokes (`script/smoke-docker-skillhandle.py`, `script/smoke-docker-hardening.py`: stdin round-trip, path containment, network egress blocked). **Still deferred**: sandbox image CI; in-container `kill` for hung execs; egress allow-list / restricted-bridge proxy. Workspace tools unchanged (host path). | Docker backend selectable; skill runner + script execute safely in a container; FS still works. |
| **2 — Workspace refactor** (done) | Introduced the `WorkspaceAccess` seam + `backend.workspace(session_id)`; FS/Docker return a `HostPathWorkspace` (the session workspace dir, bind-mounted for Docker). Workspace resolution now goes **through the backend**: `RunContext.execution_backend` is injected at the run sites, and `_get_session_workspace_root` resolves via `backend.workspace(session_id).host_path` (falling back to settings-derived resolution when no backend is present — so existing tests are unchanged). FS/Docker behavior is identical. `RemoteWorkspace` (`host_path=None`, async file ops over exec) is Phase 3. | Full suite green (existing workspace-tool tests pass unchanged via the fallback); new `test_workspace_access` pins `workspace()` host paths for FS + Docker. |
| **3 — Kubernetes backend** | `KubernetesBackend`: Pod-per-session, k8s exec, RBAC, NetworkPolicy, RemoteWorkspace. Depends on Phase 2. | Cloud backend selectable; same agent surface works. |
| **4 — Hardening** (partial) | **Done (pure code):** sandbox metrics (`SandboxMetrics` counters exposed via `/healthz` → `sandbox` block: live containers + started/stopped/swept/unavailable); graceful failure — `BackendUnavailable` wraps daemon-down errors and surfaces as a clean tool error (`is_error` result / error JSON), no silent host fallback. **Operator config (documented in the Production hardening checklist below, not code):** Docker socket proxy (Tecnativa), `--user`/userns-remap, gVisor/Kata. **Deferred (own phase):** egress allow-list (restricted network + managed allow-list proxy sidecar). | Metrics + graceful failure done; egress + operator hardening documented. |

Phases 0–1 deliver the three motivating goals for single-host untrusted code.
Phase 2 decouples the workspace. Phase 3 unlocks cloud. Phase 4 hardens both.

## Testing Strategy

Current coverage: **172 tests** (up from 109 at Phase 1b). The suite covers:

- **ReAct E2E** (`test_agent_react.py`) — agent loop with canned model, tool calling, session persistence, memory=none, concurrent tool calls.
- **Skill lifecycle** (`test_skill_lifecycle.py`) — spawn→ready→call→shutdown, pool reuse, timeouts, per-session isolation, health eviction.
- **Script execution** (`test_script_execution.py`) — basic, exit codes, args, timeout, stdin, execution_backend marker.
- **Docker backend** (`test_docker_backend.py`) — container lifecycle, exec, metrics, sweep, kill, snapshot, network mode, outbound change, max-sessions semaphore.
- **HTTP CRUD** (`test_sandbox_admin_api.py`, `test_agent_crud_api.py`, `test_skill_config_mcp_api.py`) — sandbox admin endpoints, session delete→backend.stop, session list/get/rename, agent CRUD with allowed_outbound round-trip, skill list/detail/preview/enable/disable/export, config publication, management export/import, MCP inspect/call, healthz.
- **Resilience** (`test_production_readiness.py`) — BackendUnavailable→clean error, input validation, schema guards, concurrent tool calls.
- **Unit:** each backend against a fake client. `make_backend` factory, `ensure` idempotency, `exec` timeout, `stop`/teardown, orphan listing.
- **Adversarial (Phase 4, gated):** path escape, network egress blocked.

### Remaining test gaps (low priority, deferred)

| Priority | Area | What's missing | Why deferred |
|---|---|---|---|
| Low | Attachments / Downloads | `POST /attachments/upload`, `GET /downloads/{sid}/{name}` | Needs real file upload + temp dir fixture |
| Low | Frontend E2E | Playwright smoke — login → agent config → run → verify | Needs Playwright infra not in repo |
| Low | Load / long-running | 20 concurrent sessions, 24h soak | Needs dedicated load-testing tools |

## Risks and Open Decisions

### Resolved by this design

- **Deployment target** — no longer a fork in the road. All three targets are
  supported behind one interface; the deployment picks one in config.

### Validated by spike

- **Skill stdio over Docker exec** (Phase 1): STABLE. `script/spike-docker-exec-rpc.py`
  passes 8/8 — basic ping, 100× sequential (~1.5 ms/rt incl. demux), 8 s
  idle-then-resume, 200 KB response reassembly, 5× pipelined, stderr isolation,
  clean shutdown (exec exit code 0). The one-container-per-session exec model is
  confirmed; no `attach` fallback needed. See [Process Execution Across
  Backends](#process-execution-across-backends) for the three demux rules.

### Must validate before the relevant phase

- **Skill stdio over k8s exec** (Phase 3): same bidirectional question over the
  Kubernetes exec API (SPDY/WebSocket). Unspiked; fallback is one-pod-per-skill.
- **Latency:** a *reused* exec session measures ~1.5 ms/rt — not the 50–200 ms
  feared for per-call `exec_run` — so Phase 2's remote workspace ops are cheaper
  than expected. `search`-style ops still warrant a benchmark.

### Open

- **Kubernetes storage:** `emptyDir` (ephemeral, lost with the pod) vs `PVC`
  (durable, needs a storage class + cleanup). Decide per deployment; default
  `emptyDir` with explicit artifact extraction for downloads.
- **Per-agent backend override:** deployment-level selection is in scope;
  allowing an individual agent to pin a stricter backend (e.g., an agent that
  runs user uploads always on Docker) is a natural future extension.
- **Backend degradation:** when the selected sandbox backend is unreachable,
  strict-fail or fall back to FileSystem? Default strict-fail (fail loud) with an
  opt-in fallback for trusted-only deployments.
- **Stronger isolation:** for genuinely untrusted multi-tenant work, layer
  gVisor (`runsc`) / Kata / Firecracker under the Docker backend, or a sandboxed
  runtime class on Kubernetes. Out of scope here; the seam makes it additive.

## Production Hardening Checklist

Phase 4 delivered the safe defaults and pure-code observability. Running untrusted
agent/skill code in production needs **operator/daemon-level** hardening that can't
land in application code — this is the deployment checklist.

### Docker socket — front it with a restricting proxy
The backend needs Docker socket access, which is **root-equivalent**. Never expose
the raw `/var/run/docker.sock` to the backend process directly; a container escape
that reaches the socket owns the host. Run the backend containerized and point it at
a **restricting socket proxy** (e.g. [Tecnativa docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy)), allowing only:

```
CONTAINERS=1   # create/start/stop/remove/exec/logs — what the backend needs
POST=1         # exec_create needs POST
VERSION=1
# Everything else (images/build, networks, volumes, swarm) = 0
```

Set the backend's docker client at the daemon URL:
`DOCKER_HOST=tcp://socket-proxy:2375`. A compromised sandbox that reaches the backend
still can't `images.build` or create privileged containers.

### uid mapping — run containers as the backend user
A rootful container writes bind-mounted files owned by root, which the (non-root)
backend then can't read or delete — and root-in-container is a larger attack surface.
In `DockerBackend._create_session_container` pass `user=f"{os.getuid()}:{os.getgid()}"`
(or a configured uid), or enable Docker daemon **user namespace remapping**
(`userns-remap`) so container root maps to a non-host-root uid. The `bash`/`sh` skill
SDKs still work; only file ownership changes.

### Resource ceilings — keep the defaults, verify them
1b already sets `mem_limit` / `pids_limit` / `nano_cpus` / `tmpfs` at create time
(`docker_backend.py:_create_session_container`). Verify with `docker inspect
<container> --format '{{.HostConfig.Memory}} {{.HostConfig.PidsLimit}}'` on a live
session container. These are the ceiling; raise them deliberately, never remove them.

### Network egress — `none` by default; allow-list is a future phase
`network_mode=none` (the default) gives sandboxed code **no outbound network** — the
safest and recommended setting. The model provider runs on the **host** (not in the
container), so skill runners need no network by default.

Skills that genuinely need outbound (e.g. fetch from an approved API) today set
`execution_backend_docker_network=bridge`, which is **permissive** (any host/port).
A real **egress allow-list** is deferred: the design is a dedicated Docker network
(`--internal`-flavored) plus a managed **allow-list proxy sidecar** the container
routes through, mapping the skill's existing `permissions.network.allow_outbound`
patterns to actual destination filtering. That's a substantial feature in its own
phase; until then, prefer `network_mode=none` and route any needed calls through the
trusted backend host.

### Stronger isolation for untrusted multi-tenant
Plain Docker shares the host kernel — fine for semi-trusted skill code, risky for a
determined adversary. For genuinely untrusted multi-tenant work, swap the Docker
runtime class: **gVisor** (`--runtime=runsc`) intercepts syscalls in a userspace
kernel; **Kata Containers** runs each container in a lightweight VM. These are
daemon/runtime-class settings — the `covalent-sandbox` image and `DockerBackend` code
work unchanged; only the daemon/`--runtime` flag differs.

### Image hygiene
The GHCR CI workflow (`.github/workflows/sandbox-image.yml`) publishes the image on
`Dockerfile.sandbox`/runners changes. Pin **digests** in production
(`covalent-sandbox@sha256:…`, not `:latest`); rebuild on runner/dependency changes.
Runtime `pip install` inside a container lands in an ephemeral layer, never the base.

## Appendix: Proven Proof of Concept

`script/sandbox-testing.py` verifies the Docker backend's core primitives against
a live daemon (Docker Engine 28.5.1, docker-py 7.2.0). It performs the exact
lifecycle `DockerBackend` will perform:

1. **Ensure + mount:** `containers.create(image, command=["tail","-f",
   "/dev/null"], working_dir="/workspace", volumes={host_dir: {"bind":
   "/workspace", "mode":"rw"}}, mem_limit="256m", pids_limit=128)` → `start()`.
2. **Exec (touch):** `container.exec_run(["touch","file.txt"])`.
3. **Exec (write):** `container.exec_run(["sh","-c","echo hello world >
   file.txt"])`.
4. **Stop:** `container.stop()` + `container.remove(force=True)` in `finally`.
5. **Verify:** read `host_dir/file.txt` on the host — persisted through teardown.

Verified output:

```
[step 1] sandbox up: 7d0ecb5e0770
[step 3] read back inside sandbox: 'hello world'
[teardown] stop + remove sandbox
[verify] host content: 'hello world\n'
[verify] OK — file persisted on host after sandbox teardown
```

This confirms the create/exec/teardown primitives and the bind-mount persistence
that `DockerBackend` rests on. The Kubernetes backend reuses the same agent-side
shape (`ensure`/`exec`/`stop`) over the k8s API; its remaining work is the
Pod/NetworkPolicy/RBAC plumbing and the `RemoteWorkspace` adapter, not the
fundamentals.
