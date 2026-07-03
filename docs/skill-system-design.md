# Skill System Design

This document describes the architecture, progressive disclosure mechanism, execution model, and security boundaries of the agentic framework's skill system.

## Table of Contents

- [Overview](#overview)
- [Skill Specification Model](#skill-specification-model)
- [Skill Loading and Discovery](#skill-loading-and-discovery)
- [Progressive Disclosure Mechanism](#progressive-disclosure-mechanism)
- [Tool Types and Resolution](#tool-types-and-resolution)
- [Execution Backend](#execution-backend)
- [Security Boundaries](#security-boundaries)
- [Agent Delegation](#agent-delegation)
- [Session and History Management](#session-and-history-management)
- [Example: End-to-End Flow](#example-end-to-end-flow)

## Overview

The skill system extends agent capabilities through a plug-in architecture. Skills encapsulate domain knowledge (instructions), executable tools (functions), bundled resources (reference documents), and ad-hoc scripts. The design prioritizes three goals:

1. **Context efficiency** — avoid flooding the LLM context window with skill details that may never be needed.
2. **Safe execution** — run skill code in constrained environments with explicit permission declarations.
3. **Extensibility** — support local files, git repositories, uploaded archives, and MCP (Model Context Protocol) integrations.

### Architecture Diagram

```
AgentSpec
  ├── skills: [skill_name, ...]          ← declarative binding
  ├── mcp_servers: [McpServerConfig]     ← external tool servers
  ├── delegate_agents: [agent_name, ...] ← inter-agent delegation
  │
  ▼
FrameworkRegistry                        ← central routing hub
  ├── skills: {name → SkillSpec}
  ├── manifest_skills: {name → ManifestSkillSpec}
  ├── local_tools: {name → ToolDefinition}
  ├── _skill_tool_map: {tool_name → skill_name}
  ├── mcp_client: McpClient
  └── skill_process_manager: SkillProcessManager
         │
         ▼
     ReactAgentRuntime                   ← ReAct loop with streaming SSE
```

## Skill Specification Model

### SkillSpec (basic)

The simplest form — a declarative record with no execution backend:

```python
class SkillSpec:
    name: str
    description: str
    instructions: str          # embedded into the system prompt
    tools: list[str]           # references to registered local tools
    prompts: list[str]
    resources: list[str]
    metadata: dict[str, Any]
```

Use this for skills that provide only knowledge (instructions + references to existing tools) without their own code.

### ManifestSkillSpec (full)

Extends SkillSpec with runtime execution, scripts, resources, permissions, and process management:

```python
class ManifestSkillSpec:
    name: str
    version: str
    description: str
    instructions: str

    # Execution
    runtime: SkillRuntime | None         # persistent process (python/nodejs)
    tools: list[ToolDeclaration]          # tools served by the runtime process

    # Bundled content
    scripts: list[ScriptDeclaration]      # ad-hoc scripts (python/nodejs/bash)
    resource_files: list[str]             # on-demand resources
    eager_resource_files: list[str]       # pre-loaded into system prompt

    # Security
    permissions: Permissions

    # Process management
    process: ProcessConfig
    health_check: HealthCheckConfig

    # Source tracking
    source_dir: str | None
    source_type: "local" | "git"
    git_url: str | None
    git_ref: str | None
```

#### Key sub-types

| Type | Purpose |
|------|---------|
| `ToolDeclaration` | A single tool with name, description, JSON Schema parameters, and handler routing |
| `ScriptDeclaration` | An ad-hoc script with runtime (python/nodejs/bash), path, timeout |
| `SkillRuntime` | Persistent process config: runtime type, protocol (rpc/callable), entry point, command |
| `Permissions` | Filesystem read/write prefixes, network allow/deny, env var whitelist, subprocess toggle |
| `ProcessConfig` | Max instances, idle timeout, startup timeout, request timeout |
| `HealthCheckConfig` | Check interval, max consecutive failures before restart |

## Skill Loading and Discovery

`SkillLoader` discovers skills from four source categories:

| Category | Directory | Mechanism |
|----------|-----------|-----------|
| `built_in` | `skills/built_in/` | Shipped with the framework |
| `authored` | `skills/authored/` | User-created local skills |
| `uploaded` | `skills/uploaded/` | Uploaded ZIP archives |
| `github_synced` | `skills/github_synced/` | `git clone --depth 1` + periodic `git pull --ff-only` |

### Discovery algorithm

For each source directory, `SkillLoader` recursively scans for `SKILL.md` or `skill.yaml` files (skipping `.git`, `node_modules`, `__pycache__`, etc.):

1. **If `skill.yaml` exists**: parse as `ManifestSkillSpec`, optionally merge with `SKILL.md` frontmatter.
2. **If only `SKILL.md` exists**: parse frontmatter as metadata, use body as `instructions`, then auto-infer:
   - **Runtime + tools**: scan for `src/main.py`, `main.py`, `src/main.js`, `main.js`, `index.js`; if found, introspect exported functions via AST (Python) or regex (Node.js) to auto-generate `ToolDeclaration` list.
   - **Scripts**: scan `scripts/` directory for `.py`, `.js`, `.sh` files.
   - **Resources**: scan `references/`, `resources/`, `assets/` directories; auto-mark `references/quickstart.md` as eager.

### Validation

Before registration, `SkillLoader._validate_manifest` checks:

- Runtime entry point file exists on disk
- No duplicate tool names or script names
- Tools require a runtime (cannot declare tools without runtime)
- All declared script paths exist
- All declared resource paths exist

### Auto-introspection

When no `skill.yaml` is provided, the framework automatically infers tools from the entry point:

- **Python**: `ast.parse` the file, collect public functions (non-`_`-prefixed), extract docstrings as descriptions, map type annotations to JSON Schema types.
- **Node.js**: regex-match `exports.*`, `module.exports.*`, `export function` patterns; extract JSDoc comments.

This means a minimal skill can be just a `SKILL.md` + `src/main.py` with exported functions — the framework infers the rest.

## Progressive Disclosure Mechanism

The core design principle: **the agent only sees what it needs, when it needs it.**

### Layer 1: System Prompt (startup)

When `ReactAgentRuntime._build_system_prompt` runs, each bound skill contributes:

```
Agent system prompt
│
├─ skill.instructions                  ← the skill's usage instructions (markdown)
│
├─ SkillBundle.render_prompt_index()   ← script names + descriptions (NOT script content)
│   "This skill exposes bundled scripts via run_skill_script:
│    - build_presentation: Run bundled script 'scripts/build_presentation.py'
│    - validate: Run bundled script 'scripts/validate.sh'"
│
└─ SkillBundle.render_eager_resources() ← full content of eager resource files (≤4KB each)
    "## Resource: references/quickstart.md
     <quickstart content...>"
```

The agent starts with **summaries** (script names, resource file names) and **pre-loaded eager resources** (typically a quickstart guide). This keeps the initial context compact.

### Layer 2: Conditional Tool Registration

`FrameworkRegistry.resolve_tools_for_agent` does not blindly expose all meta tools. It inspects each manifest skill's capabilities:

```python
if manifest.resource_files or manifest.scripts:
    # Only expose list_skill_files if there's something to list
    local_tool_names.add("list_skill_files")

if manifest.resource_files:
    # Only expose read_skill_resource if there are resources to read
    local_tool_names.add("read_skill_resource")

if manifest.scripts:
    # Only expose run_skill_script if there are scripts to run
    local_tool_names.add("run_skill_script")
```

An agent bound to a skill that has only instructions and tools (no scripts, no resources) sees zero meta tools — the tool surface is minimized.

### Layer 3: On-Demand Access via Meta Tools

When the agent decides it needs more detail, it calls meta tools at runtime:

| Meta Tool | Trigger | What it returns |
|-----------|---------|-----------------|
| `list_skill_files` | Agent wants to know what's available | `{resources: [...], scripts: [...]}` |
| `read_skill_resource` | Agent needs the content of a specific resource file | `{path, encoding, content, truncated}` (max 24KB) |
| `run_skill_script` | Agent needs to execute a script | `{ok, exit_code, command, stdout, stderr}` |

This three-layer approach ensures:
- **Small initial context**: only instructions + index + eager resources.
- **Pay-as-you-go**: the agent only loads full resource content when relevant.
- **Minimal tool surface**: meta tools appear only when the skill has the corresponding content type.

## Tool Types and Resolution

`FrameworkRegistry.resolve_tools_for_agent` assembles the agent's complete tool set from five sources:

### 1. Skill Tools

Declared in `ManifestSkillSpec.tools`. Exposed as OpenAI-compatible function schemas. Executed by routing through `SkillProcessManager` → skill subprocess → JSON-RPC `call_tool`.

### 2. Meta Tools

`list_skill_files`, `read_skill_resource`, `run_skill_script`. Conditionally registered based on skill capabilities (see Layer 2 above). Executed locally via registered handlers.

### 3. MCP Tools

External tools from MCP servers configured on the agent. Tool names are encoded as `mcp__{base64(server)}__{base64(tool)}` to avoid collisions. Execution goes through `McpSdkClient`.

### 4. Builtin Tools

Framework-level tools like `echo` and `get_current_time`. Registered as local tools.

### 5. Delegate Tools

Auto-generated `agent__{delegate_name}` tools when the agent declares `delegate_agents`. These allow one agent to delegate a subtask to another agent within the same framework.

### Resolution Flow

```
ToolCall arrives
  │
  ├─ name in _skill_tool_map?  → SkillProcessManager.acquire() → JSON-RPC → release()
  │
  ├─ name in local_tools?      → handler(args, context) → result
  │
  ├─ name starts with "mcp__"? → McpSdkClient.call_tool(server, tool, args)
  │
  └─ name starts with "agent__"? → ReactAgentRuntime.run(delegate_agent, input)
```

## Execution Backend

The framework uses **local subprocess execution** — skills run as OS-level processes on the same machine. There is no container or VM isolation.

### Process Architecture

```
SkillProcessManager
  ├── _pools: {skill_name → [SkillProcessHandle, ...]}
  ├── _semaphores: {skill_name → Semaphore(max_instances)}
  └── _health_task: periodic health check loop
```

### Skill Process Lifecycle

1. **Spawn** (`_spawn`):
   - Build command: `[interpreter] [runner] [entry_point]` or `[interpreter] [runner_script]` for callable protocol.
   - Inject permission environment variables.
   - Start subprocess with piped stdin/stdout/stderr.
   - Wait for `ready` JSON-RPC notification (or timeout → `SkillStartupError`).

2. **Acquire** (`acquire`):
   - Semaphore controls concurrency up to `max_instances`.
   - Find an available (alive + ready + not busy) handle, or spawn a new one.
   - Mark as busy.

3. **Request** (`SkillProcessHandle.send_request`):
   - Send JSON-RPC request over stdin.
   - Await response on stdout (with per-request timeout).
   - Route response back to the calling future.

4. **Release** (`release`):
   - Mark as not busy, release semaphore slot.
   - Update last-activity timestamp for idle tracking.

5. **Health Check** (background loop, every 30s):
   - Send `ping` to idle processes.
   - Evict processes idle beyond `idle_timeout_seconds`.
   - Restart after `max_failures` consecutive health check failures.

6. **Terminate** (`_terminate`):
   - Send `shutdown` JSON-RPC notification.
   - `SIGTERM`, wait 5s, then `SIGKILL` if necessary.

### Callable vs RPC Protocol

| Protocol | Runner | How tools are dispatched |
|----------|--------|--------------------------|
| `callable` | Framework provides `python_runner.py` / `node_runner.js` | Runner imports the entry point, discovers exported functions, maps tool names to function calls |
| `rpc` | Skill implements its own JSON-RPC server | Framework sends `call_tool` requests; skill handles routing internally |

For `callable` protocol, the framework injects two environment variables:
- `AGENT_FRAMEWORK_SKILL_ENTRYPOINT`: absolute path to the entry point file
- `AGENT_FRAMEWORK_SKILL_TOOL_MAP`: JSON map of `{tool_name: handler_name}`

### Script Execution

Ad-hoc scripts (declared in `scripts:`) bypass the process pool entirely. They are executed via `asyncio.create_subprocess_exec` directly:

```python
command = [runtime_binary, script_path, *positional_args, *named_args]
process = await asyncio.create_subprocess_exec(*command, cwd=skill_root, env=filtered_env)
stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
```

Supported runtimes:
- `python` → `sys.executable`
- `nodejs` → `node`
- `bash` → `bash`

Runtime is either explicitly declared in the script spec or inferred from file extension (`.py`, `.js`, `.sh`).

## Security Boundaries

The security model is **declaration-based with runtime enforcement**. Skills declare what they need in their manifest; the framework injects these declarations as environment variables, and the skill SDK enforces them at the operation level.

### Permission Model

```yaml
permissions:
  filesystem:
    read: ["${SKILL_DIR}/data", "/tmp/skill-output"]
    write: ["/tmp/skill-output"]
  network:
    allow_outbound: ["api.example.com", "*.example.com"]
    deny: ["internal.corp.net"]
  env_vars: ["API_KEY", "DATABASE_URL"]
  subprocess: false
```

#### Filesystem

- `read` / `write` are **prefix whitelists**. The `${SKILL_DIR}` placeholder resolves to the skill's source directory.
- **Read**: if no prefixes declared, access is **unrestricted**. If prefixes are declared, only matching paths are allowed.
- **Write**: if no prefixes declared, **all writes are denied**. Only declared prefixes are writable.
- Enforcement: injected as `SKILL_FS_READ` and `SKILL_FS_WRITE` env vars (OS pathsep-separated). The skill SDK intercepts `open()`, `write()`, etc.

#### Network

- `allow_outbound`: fnmatch patterns for allowed hosts. If empty, all hosts are allowed.
- `deny`: fnmatch patterns for blocked hosts. Evaluated first — deny takes precedence.
- Enforcement: injected as `SKILL_NET_ALLOW` and `SKILL_NET_DENY` env vars. The SDK intercepts HTTP/TCP connections.

#### Environment Variables

- Only a hardcoded system whitelist (`PATH`, `HOME`, `USER`, `TMPDIR`, `LANG`, etc.) plus skill-declared vars are passed to the subprocess.
- All `SKILL_*` prefixed variables are also preserved.

#### Subprocess

- `subprocess: false` (default) prevents the skill from spawning child processes.
- Injected as `SKILL_ALLOW_SUBPROCESS` env var (`1` or `0`).

### Path Traversal Protection

`SkillBundle.resolve_path` prevents directory traversal:

```python
candidate = (self.root / relative_path).resolve()
if self.root != candidate and self.root not in candidate.parents:
    raise SkillBundleError(f"Path escapes skill directory: {relative_path}")
```

This blocks attempts like `../../etc/passwd` in resource reads.

### Resource Size Limits

- `read_skill_resource`: default `max_bytes=24000` (24 KB), caller-adjustable.
- `render_eager_resources`: `max_chars_per_file=4000` (4 KB).
- Content beyond the limit is truncated with a `[truncated]` marker.

### Git Source Validation

When syncing skills from git:
- `git clone --depth 1` (shallow clone only).
- `git pull --ff-only` (no merge commits).
- Git ref is validated against `^[\w./@-]+$` regex before checkout — blocks injection via special characters.

### Process Isolation

- Each skill process runs as a separate OS process (not threads).
- Communication is via JSON-RPC over stdio (stdin/stdout pipes) — no shared memory.
- Stderr is captured separately for logging; it does not interleave with the RPC channel.
- Per-request timeout prevents hung processes from blocking the agent loop.
- Idle eviction reclaims resources from unused skill processes.

### Current Limitations

The security model is **soft enforcement** — it relies on the skill SDK cooperating:
- There is no OS-level sandboxing (no containers, chroot, seccomp, or user namespaces).
- A skill that ignores the SDK's interceptors could theoretically bypass filesystem and network restrictions.
- Environment variable filtering happens at spawn time; the skill process could modify its own env afterward.
- The subprocess flag is enforced by the SDK, not by the OS.

For production deployments with untrusted skills, additional OS-level isolation (containers, VMs) should be layered on top.

## Agent Delegation

Agents can delegate subtasks to other registered agents through auto-generated delegate tools:

1. When `AgentSpec.delegate_agents` lists another agent name, the runtime generates an `agent__{name}` tool.
2. The delegating agent calls this tool with an `input` string.
3. The runtime creates a new ReAct loop for the delegate agent with the given input.
4. **Loop detection**: a `delegation_chain` in the context metadata prevents circular delegation (`A → B → A`).
5. The delegate's final output is returned as the tool result.

Delegation is recursive — a delegate can itself delegate to other agents (subject to the same chain detection).

## Session and History Management

`ReactAgentRuntime` supports persistent sessions through `SessionStore`:

- Messages are loaded at the start of each run (`_load_session_messages`).
- History is capped at `session_history_limit` (default 40 messages) — older messages are truncated.
- After each run, the full message history is persisted (`_persist_session_messages`).
- Delegate runs do **not** share the parent's session — they get a fresh context with delegation metadata only.

### ReAct Loop

The core loop (`_run_stream`) runs for up to `max_iterations` (default 6):

1. Build system prompt with skill disclosures.
2. Resolve tools for the agent.
3. For each iteration:
   - Send generation request to the model provider.
   - If the response contains tool calls → execute them, append results, continue.
   - If the response has no tool calls → this is the final answer, persist and return.
4. If iterations are exhausted, send one final request with the instruction "Tool budget is exhausted. Produce the best possible final answer from prior observations" and `tools=[]` to force a text-only response.

Events are streamed as Server-Sent Events (SSE): `iteration`, `assistant`, `tool_calls`, `tool_results`, `final`.

## Example: End-to-End Flow

Consider a PPTX skill with scripts and eager resources:

```
skills/github_synced/local-pptx-skill/skills/pptx/
  ├── SKILL.md                     ← frontmatter: name, description, eager_resources
  ├── references/
  │   └── quickstart.md            ← eager resource (auto-injected into system prompt)
  └── scripts/
      └── build_presentation.py    ← script (listed in prompt index, executed on demand)
```

**Startup (Layer 1)**:
```
System prompt contains:
  - PPTX skill instructions ("Use this skill for short, polished PowerPoint decks...")
  - Script index: "build_presentation: Run bundled script 'scripts/build_presentation.py'"
  - Eager resource: full content of references/quickstart.md
  - Tools available: list_skill_files, run_skill_script
```

**Agent decides to create a deck (Layer 3)**:
```json
{
  "name": "run_skill_script",
  "arguments": {
    "skill": "pptx",
    "name": "build_presentation",
    "named_args": {"title": "Q3 Report", "slides": "[...]"}
  }
}
```

**Execution**:
```
asyncio.create_subprocess_exec(
  python, scripts/build_presentation.py,
  --title, "Q3 Report",
  --slides, "[...]",
  cwd=<skill source dir>,
  env={SKILL_FS_READ=..., SKILL_FS_WRITE=..., ...}
)
→ {ok: true, stdout: "Created /tmp/skill-output/Q3_Report.pptx", stderr: ""}
```

**Final response**: Agent presents the file path to the user.
